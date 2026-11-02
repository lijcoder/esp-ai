/*
 * prompt_store —— 提示音资源存储（L1 基础资源，T3.5 起从 assets 分区加载）。
 *
 * 设计（方案 D，用户 2026-11-02 拍板）：
 *   - assets 分区（2M @0x7a0000）存放自定义只读 pack（格式见 prompt_pack.h）；
 *   - 启动时校验 pack 头部/条目表（纯逻辑在 prompt_pack.c，可 host 单测），
 *     然后 **esp_partition_mmap 只读映射**，`prompt_assets_get()` 直接返回映射区指针
 *     —— PCM 与 T3.6 的 flash rodata 行为一致：零 PSRAM 拷贝、零内部堆开销；
 *   - mmap 失败时回退「分块读入 PSRAM」，并校验每条 PCM 的 CRC32（降级路径更严格）；
 *   - pack 缺失/损坏不致命：init 返回错误、get 返回 NULL，业务按"提示音不可用"降级。
 *
 * 为什么不用 SPIFFS（ROADMAP 原措辞）：SPIFFS mount 要占内部堆，而本项目 wss 水位余量
 * 只有 ~2.6KB（CONVENTIONS §4）；自定义 pack 的校验/映射几乎零 RAM，且"单独烧分区替换"
 * 的目标同样满足。详见 docs/ARCHITECTURE.md §3.4。
 */
#include "prompt_assets.h"

#include <stdio.h>
#include <string.h>

#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_partition.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "prompt_pack.h"

static const char *TAG = "PROMPT";

#define PROMPT_PART_LABEL "assets"
#define PROMPT_PART_TYPE ESP_PARTITION_TYPE_DATA
#define PROMPT_PART_SUBTYPE ESP_PARTITION_SUBTYPE_DATA_SPIFFS /* 分区表 marker，内容不是 SPIFFS */
#define PROMPT_READ_CHUNK 1024
#define PROMPT_INFO_ERR_MAX 56

static bool s_inited;
static esp_err_t s_init_err = ESP_ERR_INVALID_STATE;
static bool s_ready;
static prompt_store_mode_t s_mode = PROMPT_STORE_MODE_NONE;
static prompt_store_info_t s_info;
static prompt_clip_t s_clips[PROMPT_COUNT];
static prompt_pack_entry_t s_entries[PROMPT_PACK_MAX_ENTRIES];
static uint32_t s_first_offset; /* 头部区字节数（映射后也用于指针换算） */
static uint16_t s_entry_count;
static const void *s_base;
static esp_partition_mmap_handle_t s_mmap_handle;

static const char *const k_names[PROMPT_COUNT] = {
    [PROMPT_WAKE_OK] = "wake",
    [PROMPT_NET_OFFLINE] = "net-offline",
    [PROMPT_CLOUD_ERROR] = "cloud-error",
    [PROMPT_TIMEOUT] = "timeout",
    [PROMPT_TTS_FAIL] = "tts-fail",
};

static void set_error(const char *msg)
{
    s_ready = false;
    s_mode = PROMPT_STORE_MODE_NONE;
    s_info.ready = false;
    s_info.mode = (uint8_t)PROMPT_STORE_MODE_NONE;
    snprintf(s_info.error, sizeof(s_info.error), "%s", msg);
    ESP_LOGE(TAG, "提示音资源不可用：%s（业务降级：缺提示音）", msg);
}

/* 把 pack 条目填成对外的 prompt_clip_t 表；返回 0 成功 */
static int fill_clips(void)
{
    for (uint16_t id = 0; id < PROMPT_COUNT; id++) {
        const int idx = prompt_pack_find(s_entries, s_entry_count, (uint8_t)id);
        if (idx < 0) {
            return -1;
        }
        const prompt_pack_entry_t *e = &s_entries[idx];
        s_clips[id].pcm = (const int16_t *)((const uint8_t *)s_base + e->offset);
        s_clips[id].samples = e->length / 2;
        s_clips[id].sample_rate = e->sample_rate;
    }
    return 0;
}

/* 映射失败时的降级：分块读入 PSRAM，并逐条校验 PCM CRC32 */
static esp_err_t load_to_psram(const esp_partition_t *part, uint32_t total_len)
{
    void *buf = heap_caps_malloc(total_len, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (buf == NULL) {
        ESP_LOGE(TAG, "PSRAM 分配 %u B 失败", (unsigned)total_len);
        return ESP_ERR_NO_MEM;
    }
    uint8_t chunk[PROMPT_READ_CHUNK];
    uint32_t off = 0;
    while (off < total_len) {
        uint32_t n = total_len - off;
        if (n > sizeof(chunk)) {
            n = sizeof(chunk);
        }
        esp_err_t err = esp_partition_read(part, off, chunk, n);
        if (err != ESP_OK) {
            ESP_LOGE(TAG, "读 assets 分区失败（off=%u）: %s", (unsigned)off, esp_err_to_name(err));
            heap_caps_free(buf);
            return err;
        }
        memcpy((uint8_t *)buf + off, chunk, n);
        off += n;
    }

    for (uint16_t i = 0; i < s_entry_count; i++) {
        const prompt_pack_entry_t *e = &s_entries[i];
        const uint32_t crc = prompt_pack_crc32(0, (const uint8_t *)buf + e->offset, e->length);
        if (crc != e->crc32) {
            ESP_LOGE(TAG, "PCM CRC 不匹配（id=%u，期望 %08x 实际 %08x）", (unsigned)e->id,
                     (unsigned)e->crc32, (unsigned)crc);
            heap_caps_free(buf);
            return ESP_ERR_INVALID_CRC;
        }
    }

    s_base = buf;
    s_mode = PROMPT_STORE_MODE_PSRAM;
    ESP_LOGW(TAG, "已回退 PSRAM 加载（%u B，CRC 校验通过）", (unsigned)total_len);
    return ESP_OK;
}

esp_err_t prompt_store_init(void)
{
    if (s_inited) {
        return s_init_err;
    }
    s_inited = true;
    const int64_t t0 = esp_timer_get_time();
    memset(&s_info, 0, sizeof(s_info));
    memset(s_clips, 0, sizeof(s_clips));
    strncpy(s_info.error, "未初始化", sizeof(s_info.error) - 1);

    const esp_partition_t *part =
        esp_partition_find_first(PROMPT_PART_TYPE, PROMPT_PART_SUBTYPE, PROMPT_PART_LABEL);
    if (part == NULL) {
        set_error("assets 分区未找到（分区表/烧录缺失？）");
        s_init_err = ESP_ERR_NOT_FOUND;
        return s_init_err;
    }

    /* 头部+条目表最大 344B；读固定上限，校验器会检查 count/total_len */
    uint8_t hdrbuf[PROMPT_PACK_HDR_SIZE + PROMPT_PACK_MAX_ENTRIES * PROMPT_PACK_ENTRY_SIZE];
    esp_err_t err = esp_partition_read(part, 0, hdrbuf, sizeof(hdrbuf));
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "读 assets 分区头部失败: %s", esp_err_to_name(err));
        set_error("读 assets 分区头部失败");
        s_init_err = err;
        return s_init_err;
    }

    uint32_t total_len = 0;
    const prompt_pack_err_t perr =
        prompt_pack_validate(hdrbuf, sizeof(hdrbuf), part->size, PROMPT_COUNT, s_entries,
                             PROMPT_PACK_MAX_ENTRIES, &total_len);
    if (perr != PROMPT_PACK_OK) {
        char msg[PROMPT_INFO_ERR_MAX];
        snprintf(msg, sizeof(msg), "pack 校验失败：%s", prompt_pack_err_str(perr));
        set_error(msg);
        s_init_err = ESP_ERR_INVALID_STATE;
        return s_init_err;
    }
    s_entry_count = PROMPT_COUNT;
    s_first_offset = prompt_pack_table_len(PROMPT_COUNT);

    /* 优先零拷贝：映射到 64KB 对齐的映射尺寸（映射尺寸 ≤ 分区容量） */
    uint32_t map_len = (total_len + PROMPT_PACK_MAP_ALIGN - 1) & ~(uint32_t)(PROMPT_PACK_MAP_ALIGN - 1);
    if (map_len == 0 || map_len > part->size) {
        map_len = part->size;
    }
    const void *base = NULL;
    esp_partition_mmap_handle_t handle = 0;
    err = esp_partition_mmap(part, 0, map_len, ESP_PARTITION_MMAP_DATA, &base, &handle);
    if (err == ESP_OK) {
        s_base = base;
        s_mode = PROMPT_STORE_MODE_MMAP;
        s_mmap_handle = handle;
    } else {
        ESP_LOGW(TAG, "mmap 失败（%s）→ 尝试 PSRAM 降级加载", esp_err_to_name(err));
        s_base = NULL;
        s_mode = PROMPT_STORE_MODE_NONE;
        err = load_to_psram(part, total_len);
        if (err != ESP_OK) {
            set_error("mmap 失败且 PSRAM 降级加载失败");
            s_init_err = err;
            return s_init_err;
        }
    }

    if (fill_clips() != 0) {
        set_error("pack 条目缺 ID（wake/net/cloud/timeout/tts 需齐全）");
        s_init_err = ESP_ERR_INVALID_STATE;
        return s_init_err;
    }

    s_ready = true;
    s_init_err = ESP_OK;
    s_info.ready = true;
    s_info.mode = (uint8_t)s_mode;
    s_info.version = PROMPT_PACK_VERSION;
    s_info.count = s_entry_count;
    s_info.total_len = total_len;
    s_info.base = s_base;
    s_info.load_us = (uint32_t)(esp_timer_get_time() - t0);
    snprintf(s_info.error, sizeof(s_info.error), "-");

    ESP_LOGI(TAG, "assets pack 就绪：mode=%s ver=%u entries=%u size=%u B load=%u us",
             s_mode == PROMPT_STORE_MODE_MMAP ? "mmap" : "psram", (unsigned)s_info.version,
             (unsigned)s_info.count, (unsigned)total_len, (unsigned)s_info.load_us);
    return ESP_OK;
}

bool prompt_store_ready(void)
{
    return s_ready;
}

esp_err_t prompt_store_info(prompt_store_info_t *out)
{
    if (out == NULL) {
        return ESP_ERR_INVALID_ARG;
    }
    if (!s_inited) {
        return ESP_ERR_INVALID_STATE;
    }
    *out = s_info;
    return ESP_OK;
}

const prompt_clip_t *prompt_assets_get(prompt_id_t id)
{
    if (!s_ready || (unsigned)id >= (unsigned)PROMPT_COUNT) {
        return NULL;
    }
    const prompt_clip_t *clip = &s_clips[id];
    return (clip->pcm != NULL && clip->samples > 0) ? clip : NULL;
}

const char *prompt_assets_name(prompt_id_t id)
{
    if ((unsigned)id >= (unsigned)PROMPT_COUNT) {
        return "?";
    }
    return k_names[id];
}
