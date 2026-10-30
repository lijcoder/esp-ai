/*
 * config_store —— 配置持久化（L1 基础服务）。
 *
 * 为什么单独成组件：音量/设备状态这类运行态参数要落 NVS，而 WiFi/WS 参数是编译期常量
 * （Kconfig）。把"谁负责 nvs_flash_init、命名空间、键名、缺省值、越界兜底"收在一处，
 * 避免 net_mgr / transport_ws / voice_session 各自 nvs_open 造成重复初始化与键名漂移。
 *
 * 分层：L1，只依赖 IDF 的 nvs_flash；不得反向依赖 L2/L3。
 * 线程安全：NVS 自身线程安全，这里不加锁；但 Flash 写会阻塞，禁止在音频实时任务里调用。
 */
#include "config_store.h"

#include <inttypes.h>
#include <string.h>

#include "esp_log.h"
#include "nvs.h"
#include "nvs_flash.h"
#include "sdkconfig.h"

static const char *TAG = "CFG";

/* 命名空间与键名是持久化契约的一部分：改名会让老设备上的配置"消失"，改动需评估迁移 */
static const char *const k_namespace = "cfg";
static const char *const k_key_volume = "volume";

static nvs_handle_t s_nvs;
static bool s_inited;

static esp_err_t ensure_inited(void)
{
    if (!s_inited) {
        ESP_LOGE(TAG, "未初始化，先调用 cfg_init()");
        return ESP_ERR_INVALID_STATE;
    }
    return ESP_OK;
}

static esp_err_t check_key(const char *key)
{
    if (key == NULL || key[0] == '\0') {
        return ESP_ERR_INVALID_ARG;
    }
    if (strlen(key) > NVS_KEY_NAME_MAX_SIZE - 1) {
        ESP_LOGE(TAG, "key 过长（>%d 字节）：%s", NVS_KEY_NAME_MAX_SIZE - 1, key);
        return ESP_ERR_INVALID_ARG;
    }
    return ESP_OK;
}

esp_err_t cfg_init(void)
{
    if (s_inited) {
        ESP_LOGW(TAG, "重复初始化，忽略");
        return ESP_ERR_INVALID_STATE;
    }

    esp_err_t err = nvs_flash_init();
    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        /* 唯一允许丢配置的路径：分区写满或固件版本不兼容，擦除后重建 */
        ESP_LOGW(TAG, "NVS 需要重建（%s），擦除分区后重试（配置将丢失）", esp_err_to_name(err));
        err = nvs_flash_erase();
        if (err == ESP_OK) {
            err = nvs_flash_init();
        }
    }
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "NVS 初始化失败: %s", esp_err_to_name(err));
        return err;
    }

    err = nvs_open(k_namespace, NVS_READWRITE, &s_nvs);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "打开命名空间 %s 失败: %s", k_namespace, esp_err_to_name(err));
        return err;
    }
    s_inited = true;

    /* 启动日志要能回答"这次开机音量是哪来的"：NVS 里存过 vs 用缺省。
     * 键存在但越界时 cfg_get_volume() 已 clamp 并 WARN，这里仍标"来自 NVS"。 */
    int32_t stored = 0;
    bool has_stored = (cfg_get_int(k_key_volume, &stored) == ESP_OK);
    int volume = CONFIG_CFG_VOLUME_DEFAULT;
    esp_err_t verr = cfg_get_volume(&volume);
    ESP_LOGI(TAG, "NVS 就绪：namespace=%s，音量=%d（%s）", k_namespace, volume,
             (verr != ESP_OK) ? "读取失败，用缺省"
                              : (has_stored ? "来自 NVS" : "缺省值"));
    return ESP_OK;
}

esp_err_t cfg_get_int(const char *key, int32_t *out)
{
    if (out == NULL) {
        return ESP_ERR_INVALID_ARG;
    }
    esp_err_t err = ensure_inited();
    if (err == ESP_OK) {
        err = check_key(key);
    }
    if (err != ESP_OK) {
        return err;
    }

    err = nvs_get_i32(s_nvs, key, out);
    if (err != ESP_OK && err != ESP_ERR_NVS_NOT_FOUND) {
        ESP_LOGE(TAG, "读 int 失败 key=%s: %s", key, esp_err_to_name(err));
    }
    return err;
}

esp_err_t cfg_get_int_or(const char *key, int32_t fallback, int32_t *out)
{
    esp_err_t err = cfg_get_int(key, out);
    if (err == ESP_ERR_NVS_NOT_FOUND) {
        *out = fallback;
        return ESP_OK;
    }
    return err;
}

esp_err_t cfg_set_int(const char *key, int32_t value)
{
    esp_err_t err = ensure_inited();
    if (err == ESP_OK) {
        err = check_key(key);
    }
    if (err != ESP_OK) {
        return err;
    }

    err = nvs_set_i32(s_nvs, key, value);
    if (err == ESP_OK) {
        err = nvs_commit(s_nvs);
    }
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "写 int 失败 key=%s: %s", key, esp_err_to_name(err));
        return err;
    }
    ESP_LOGD(TAG, "写入 key=%s（int）", key);
    return ESP_OK;
}

esp_err_t cfg_get_str(const char *key, char *buf, size_t len)
{
    if (buf == NULL || len == 0) {
        return ESP_ERR_INVALID_ARG;
    }
    esp_err_t err = ensure_inited();
    if (err == ESP_OK) {
        err = check_key(key);
    }
    if (err != ESP_OK) {
        return err;
    }

    size_t required = len; /* in: 缓冲大小；out: 实际需要的字节数 */
    err = nvs_get_str(s_nvs, key, buf, &required);
    if (err == ESP_ERR_NVS_INVALID_LENGTH) {
        ESP_LOGW(TAG, "读 str 缓冲不足 key=%s（需要 %u 字节）", key, (unsigned)required);
    } else if (err != ESP_OK && err != ESP_ERR_NVS_NOT_FOUND) {
        ESP_LOGE(TAG, "读 str 失败 key=%s: %s", key, esp_err_to_name(err));
    }
    return err;
}

esp_err_t cfg_set_str(const char *key, const char *value)
{
    if (value == NULL) {
        return ESP_ERR_INVALID_ARG;
    }
    esp_err_t err = ensure_inited();
    if (err == ESP_OK) {
        err = check_key(key);
    }
    if (err != ESP_OK) {
        return err;
    }

    err = nvs_set_str(s_nvs, key, value);
    if (err == ESP_OK) {
        err = nvs_commit(s_nvs);
    }
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "写 str 失败 key=%s: %s", key, esp_err_to_name(err));
        return err;
    }
    /* 只打长度不打内容：将来 device token 可能走字符串接口 */
    ESP_LOGD(TAG, "写入 key=%s（str，%u 字节）", key, (unsigned)strlen(value));
    return ESP_OK;
}

esp_err_t cfg_erase_key(const char *key)
{
    esp_err_t err = ensure_inited();
    if (err == ESP_OK) {
        err = check_key(key);
    }
    if (err != ESP_OK) {
        return err;
    }

    err = nvs_erase_key(s_nvs, key);
    if (err == ESP_OK) {
        err = nvs_commit(s_nvs);
    }
    if (err != ESP_OK && err != ESP_ERR_NVS_NOT_FOUND) {
        ESP_LOGE(TAG, "删除 key=%s 失败: %s", key, esp_err_to_name(err));
    }
    return err;
}

esp_err_t cfg_get_volume(int *out)
{
    if (out == NULL) {
        return ESP_ERR_INVALID_ARG;
    }
    esp_err_t err = ensure_inited();
    if (err != ESP_OK) {
        return err;
    }

    int32_t raw = CONFIG_CFG_VOLUME_DEFAULT;
    err = nvs_get_i32(s_nvs, k_key_volume, &raw);
    if (err == ESP_ERR_NVS_NOT_FOUND) {
        raw = CONFIG_CFG_VOLUME_DEFAULT;
    } else if (err != ESP_OK) {
        ESP_LOGE(TAG, "读音量失败: %s", esp_err_to_name(err));
        return err;
    }

    /* 读取侧也 clamp：防止历史脏数据/手工写的 150 直接进播放器 */
    if (raw < 0 || raw > 100) {
        ESP_LOGW(TAG, "音量越界 %" PRId32 "，按 [0,100] clamp 处理", raw);
        raw = (raw < 0) ? 0 : 100;
    }
    *out = (int)raw;
    return ESP_OK;
}

esp_err_t cfg_set_volume(int volume)
{
    if (volume < 0) {
        ESP_LOGW(TAG, "音量 %d < 0，按 0 处理", volume);
        volume = 0;
    } else if (volume > 100) {
        ESP_LOGW(TAG, "音量 %d > 100，按 100 处理", volume);
        volume = 100;
    }
    ESP_LOGI(TAG, "保存音量=%d", volume);
    return cfg_set_int(k_key_volume, (int32_t)volume);
}

esp_err_t cfg_get_usage(uint32_t *used_entries, uint32_t *free_entries)
{
    if (used_entries == NULL || free_entries == NULL) {
        return ESP_ERR_INVALID_ARG;
    }

    nvs_stats_t stats;
    esp_err_t err = nvs_get_stats(NULL, &stats); /* NULL = 默认 "nvs" 分区 */
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "读取 NVS 统计失败: %s", esp_err_to_name(err));
        return err;
    }
    *used_entries = (uint32_t)stats.used_entries;
    *free_entries = (uint32_t)stats.free_entries;
    return ESP_OK;
}
