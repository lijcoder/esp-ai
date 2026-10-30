/*
 * net_mgr —— WiFi STA 连接、断线重连、SNTP 对时（L1 基础服务）。
 *
 * 为什么做成"无任务"实现：WiFi/IP 事件由 IDF 在系统默认事件循环任务里回调，
 * 退避重连用一次性 esp_timer 完成，不需要再加一个常驻任务（ARCHITECTURE §9.3 未列 net 任务）。
 * 状态变化只经 event_bus 对外通告（NET_UP / NET_DOWN），同层模块不直连（CONVENTIONS §2）。
 */
#include "net_mgr.h"

#include <string.h>
#include <time.h>

#include "esp_event.h"
#include "esp_log.h"
#include "esp_netif.h"
#include "esp_netif_sntp.h"
#include "esp_timer.h"
#include "esp_wifi.h"
#include "event_bus.h"
#include "freertos/FreeRTOS.h"
#include "freertos/event_groups.h"
#include "sdkconfig.h"

static const char *TAG = "NET";

/*
 * 重连退避序列：ARCHITECTURE §6.1 定稿（1s → 2s → 5s → 10s → 30s，之后保持 30s 无限重试）。
 * 这是协议/架构常量而不是可调参数，改动必须先改架构文档，所以不拆成 Kconfig。
 */
static const uint32_t k_backoff_ms[] = {1000, 2000, 5000, 10000, 30000};
#define BACKOFF_STEPS (sizeof(k_backoff_ms) / sizeof(k_backoff_ms[0]))

#define NET_UP_BIT BIT0

static bool s_inited;
static bool s_configured;
static net_state_t s_state = NET_STATE_IDLE;
static EventGroupHandle_t s_evt_group;
static esp_timer_handle_t s_retry_timer;
static esp_netif_t *s_netif;

static char s_ip[NET_IP_STR_MAX];
static char s_ssid[NET_SSID_STR_MAX];
static int8_t s_rssi;
static bool s_time_synced;
static bool s_sntp_ready;

static size_t s_backoff_idx;
static uint32_t s_retry_count;
static uint32_t s_next_retry_ms;

const char *net_state_name(net_state_t state)
{
    switch (state) {
    case NET_STATE_IDLE:
        return "IDLE";
    case NET_STATE_CONNECTING:
        return "CONNECTING";
    case NET_STATE_UP:
        return "UP";
    default:
        return "UNKNOWN";
    }
}

static void stop_retry_timer(void)
{
    if (s_retry_timer == NULL) {
        return;
    }
    if (esp_timer_is_active(s_retry_timer)) {
        esp_timer_stop(s_retry_timer);
    }
    s_next_retry_ms = 0;
}

/* 按当前退避索引安排一次重连，返回本次使用的延时（毫秒） */
static uint32_t schedule_retry(void)
{
    const size_t idx = s_backoff_idx < BACKOFF_STEPS ? s_backoff_idx : BACKOFF_STEPS - 1;
    const uint32_t delay_ms = k_backoff_ms[idx];

    if (s_backoff_idx + 1 < BACKOFF_STEPS) {
        s_backoff_idx++;
    }
    s_retry_count++;
    stop_retry_timer();
    s_next_retry_ms = delay_ms;

    esp_err_t err = esp_timer_start_once(s_retry_timer, (uint64_t)delay_ms * 1000);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "退避定时器启动失败: %s", esp_err_to_name(err));
        s_next_retry_ms = 0;
    }
    return delay_ms;
}

static void reset_backoff(void)
{
    stop_retry_timer();
    s_backoff_idx = 0;
    s_retry_count = 0;
}

static void on_net_down(uint8_t reason, uint32_t retry_ms)
{
    app_evt_data_t data = {0};
    data.net_down.reason = reason;
    data.net_down.retry_ms = retry_ms;
    esp_err_t err = app_evt_post(APP_EVT_NET_DOWN, &data);
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "投递 NET_DOWN 失败: %s", esp_err_to_name(err));
    }
}

static void on_net_up(void)
{
    wifi_ap_record_t ap = {0};
    s_rssi = 0;
    if (esp_wifi_sta_get_ap_info(&ap) == ESP_OK) {
        s_rssi = ap.rssi;
    }

    app_evt_data_t data = {0};
    snprintf(data.net_up.ip, sizeof(data.net_up.ip), "%s", s_ip);
    data.net_up.rssi = s_rssi;
    esp_err_t err = app_evt_post(APP_EVT_NET_UP, &data);
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "投递 NET_UP 失败: %s", esp_err_to_name(err));
    }
}

/* SNTP 同步回调：在 SNTP/lwIP 上下文执行，只做记录与日志（系统时间已由 lwIP 写入） */
static void on_time_synced(struct timeval *tv)
{
    struct tm tm_utc;
    time_t sec = tv->tv_sec;
    gmtime_r(&sec, &tm_utc);

    s_time_synced = true;
    ESP_LOGI(TAG, "SNTP 对时成功：%04d-%02d-%02d %02d:%02d:%02d UTC", tm_utc.tm_year + 1900,
             tm_utc.tm_mon + 1, tm_utc.tm_mday, tm_utc.tm_hour, tm_utc.tm_min, tm_utc.tm_sec);
}

static void retry_timer_cb(void *arg)
{
    (void)arg;
    s_next_retry_ms = 0;
    ESP_LOGI(TAG, "退避结束，发起第 %u 次重连", (unsigned)s_retry_count);

    esp_err_t err = esp_wifi_connect();
    if (err != ESP_OK) {
        /* 驱动暂时不可用（如正在停止）：继续按退避重试，不放弃 */
        ESP_LOGW(TAG, "发起重连失败: %s，继续退避重试", esp_err_to_name(err));
        schedule_retry();
    }
}

static void on_sta_disconnected(uint8_t reason)
{
    const bool was_up = (s_state == NET_STATE_UP);

    s_state = NET_STATE_CONNECTING;
    s_ip[0] = '\0';
    s_rssi = 0;
    if (s_evt_group != NULL) {
        xEventGroupClearBits(s_evt_group, NET_UP_BIT);
    }

    const uint32_t delay_ms = schedule_retry();
    ESP_LOGW(TAG, "连接断开（reason=%u，已联网过=%d），%u ms 后重试（第 %u 次）", reason,
             (int)was_up, (unsigned)delay_ms, (unsigned)s_retry_count);

    /* 只有"从已联网掉线"才对外通告 NET_DOWN：上电首次连不上时上层无需处理 */
    if (was_up) {
        on_net_down(reason, delay_ms);
    }
}

static void wifi_event_handler(void *arg, esp_event_base_t base, int32_t id, void *data)
{
    (void)arg;
    (void)base;

    switch (id) {
    case WIFI_EVENT_STA_START:
        ESP_LOGI(TAG, "WiFi 已启动，开始连接 SSID=%s", s_ssid);
        esp_wifi_connect();
        break;
    case WIFI_EVENT_STA_CONNECTED:
        ESP_LOGI(TAG, "已关联 AP，等待 DHCP");
        s_state = NET_STATE_CONNECTING;
        break;
    case WIFI_EVENT_STA_DISCONNECTED: {
        const wifi_event_sta_disconnected_t *ev = (const wifi_event_sta_disconnected_t *)data;
        on_sta_disconnected(ev != NULL ? ev->reason : 0);
        break;
    }
    default:
        break;
    }
}

static void ip_event_handler(void *arg, esp_event_base_t base, int32_t id, void *data)
{
    (void)arg;
    (void)base;

    if (id == IP_EVENT_STA_GOT_IP) {
        const ip_event_got_ip_t *ev = (const ip_event_got_ip_t *)data;
        snprintf(s_ip, sizeof(s_ip), IPSTR, IP2STR(&ev->ip_info.ip));
        s_state = NET_STATE_UP;
        reset_backoff();
        if (s_evt_group != NULL) {
            xEventGroupSetBits(s_evt_group, NET_UP_BIT);
        }
        ESP_LOGI(TAG, "已联网：ip=%s，开始等待 SNTP 对时", s_ip);

        /* 联网瞬间重启 SNTP：避免"启动时还没有链路"造成的首次请求失败重试，
         * TLS 校验证书前需要可信时间，这里主动触发一次（已对时则不重复） */
        if (s_sntp_ready && !s_time_synced) {
            esp_err_t sntp_err = esp_netif_sntp_start();
            if (sntp_err != ESP_OK) {
                ESP_LOGW(TAG, "触发 SNTP 对时失败: %s", esp_err_to_name(sntp_err));
            }
        }

        on_net_up();
    } else if (id == IP_EVENT_STA_LOST_IP) {
        s_state = NET_STATE_CONNECTING;
        s_ip[0] = '\0';
        if (s_evt_group != NULL) {
            xEventGroupClearBits(s_evt_group, NET_UP_BIT);
        }
        /* 地址丢失通常由 DHCP 续租/链路抖动触发，WiFi 会自动重连，这里只记录 */
        ESP_LOGW(TAG, "IP 地址丢失，等待驱动重新获取");
    }
}

esp_err_t net_init(void)
{
    if (s_inited) {
        return ESP_ERR_INVALID_STATE;
    }

    snprintf(s_ssid, sizeof(s_ssid), "%s", CONFIG_AI_WIFI_SSID);

    if (CONFIG_AI_WIFI_SSID[0] == '\0') {
        /* 不配置 WiFi 也要能跑本地唤醒/播放：降级为离线，由上层用 net_is_up() 判断 */
        ESP_LOGW(TAG, "未配置 CONFIG_AI_WIFI_SSID，联网功能关闭（本地唤醒/播放不受影响）");
        s_state = NET_STATE_IDLE;
        s_inited = true;
        return ESP_OK;
    }

    s_evt_group = xEventGroupCreate();
    if (s_evt_group == NULL) {
        ESP_LOGE(TAG, "创建网络事件组失败（内存不足）");
        return ESP_ERR_NO_MEM;
    }

    const esp_timer_create_args_t timer_args = {
        .callback = retry_timer_cb,
        .arg = NULL,
        .name = "net_retry",
    };
    esp_err_t err = esp_timer_create(&timer_args, &s_retry_timer);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "创建退避定时器失败: %s", esp_err_to_name(err));
        return err;
    }

    /* WiFi/IP 事件挂在系统默认循环上（与 event_bus 的独立循环互不影响） */
    err = esp_event_loop_create_default();
    if (err != ESP_OK && err != ESP_ERR_INVALID_STATE) {
        ESP_LOGE(TAG, "创建默认事件循环失败: %s", esp_err_to_name(err));
        return err;
    }
    err = esp_netif_init();
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "esp_netif 初始化失败: %s", esp_err_to_name(err));
        return err;
    }

    s_netif = esp_netif_create_default_wifi_sta();
    if (s_netif == NULL) {
        ESP_LOGE(TAG, "创建 STA netif 失败");
        return ESP_FAIL;
    }

    const wifi_init_config_t init_cfg = WIFI_INIT_CONFIG_DEFAULT();
    err = esp_wifi_init(&init_cfg);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "WiFi 初始化失败: %s", esp_err_to_name(err));
        return err;
    }

    /* 凭证唯一来源是 Kconfig：用 RAM 存储，避免 NVS 里残留旧 SSID 覆盖配置 */
    err = esp_wifi_set_storage(WIFI_STORAGE_RAM);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "设置 WiFi 存储方式失败: %s", esp_err_to_name(err));
        return err;
    }

    err = esp_event_handler_register(WIFI_EVENT, ESP_EVENT_ANY_ID, wifi_event_handler, NULL);
    if (err == ESP_OK) {
        err = esp_event_handler_register(IP_EVENT, IP_EVENT_STA_GOT_IP, ip_event_handler, NULL);
    }
    if (err == ESP_OK) {
        err = esp_event_handler_register(IP_EVENT, IP_EVENT_STA_LOST_IP, ip_event_handler, NULL);
    }
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "注册网络事件处理器失败: %s", esp_err_to_name(err));
        return err;
    }

    wifi_config_t wifi_cfg = {0};
    if (strlen(CONFIG_AI_WIFI_SSID) >= sizeof(wifi_cfg.sta.ssid)) {
        ESP_LOGW(TAG, "SSID 超过 %u 字节，驱动侧会截断", (unsigned)sizeof(wifi_cfg.sta.ssid));
    }
    snprintf((char *)wifi_cfg.sta.ssid, sizeof(wifi_cfg.sta.ssid), "%s", CONFIG_AI_WIFI_SSID);
    snprintf((char *)wifi_cfg.sta.password, sizeof(wifi_cfg.sta.password), "%s",
             CONFIG_AI_WIFI_PASSWORD);
    wifi_cfg.sta.threshold.authmode = CONFIG_AI_WIFI_PASSWORD[0] != '\0' ? WIFI_AUTH_WPA2_PSK
                                                                        : WIFI_AUTH_OPEN;

    err = esp_wifi_set_mode(WIFI_MODE_STA);
    if (err == ESP_OK) {
        err = esp_wifi_set_config(WIFI_IF_STA, &wifi_cfg);
    }
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "配置 STA 失败: %s", esp_err_to_name(err));
        return err;
    }

    /* SNTP 在联网前启动，由 lwIP 自动重试；对时回调里只记录状态（TLS 校验证书前用） */
    esp_sntp_config_t sntp_cfg = ESP_NETIF_SNTP_DEFAULT_CONFIG(CONFIG_NET_SNTP_SERVER);
    sntp_cfg.sync_cb = on_time_synced;
    err = esp_netif_sntp_init(&sntp_cfg);
    if (err != ESP_OK) {
        /* 对时失败不影响联网与本地功能，只影响 TLS 证书校验 */
        ESP_LOGW(TAG, "SNTP 初始化失败: %s（TLS 校验前可能缺少可信时间）", esp_err_to_name(err));
    } else {
        s_sntp_ready = true;
        /* 首次请求的随机启动延迟已由 CONFIG_LWIP_SNTP_STARTUP_DELAY=n 关闭，
         * 因此 GOT_IP 后重启 SNTP 会立即发请求，不必等最多 5s */
    }

    err = esp_wifi_start();
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "WiFi 启动失败: %s", esp_err_to_name(err));
        return err;
    }

    /* 语音场景要低时延：关闭 modem sleep，避免 WiFi 省电引入上下行抖动 */
    err = esp_wifi_set_ps(WIFI_PS_NONE);
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "关闭 WiFi 省电失败: %s（可能增加链路时延）", esp_err_to_name(err));
    }

    s_state = NET_STATE_CONNECTING;
    s_configured = true;
    s_inited = true;
    ESP_LOGI(TAG, "STA 已启动：SSID=%s（密码不打印），SNTP 服务器=%s", s_ssid,
             CONFIG_NET_SNTP_SERVER);
    return ESP_OK;
}

bool net_is_up(void)
{
    return s_state == NET_STATE_UP;
}

esp_err_t net_wait_ready(uint32_t timeout_ms)
{
    if (!s_inited || !s_configured) {
        return ESP_ERR_INVALID_STATE;
    }
    if (s_state == NET_STATE_UP) {
        return ESP_OK;
    }

    const EventBits_t bits = xEventGroupWaitBits(s_evt_group, NET_UP_BIT, pdFALSE, pdTRUE,
                                                 pdMS_TO_TICKS(timeout_ms));
    return (bits & NET_UP_BIT) != 0 ? ESP_OK : ESP_ERR_TIMEOUT;
}

bool net_is_time_synced(void)
{
    return s_time_synced;
}

esp_err_t net_get_info(net_info_t *out)
{
    if (out == NULL) {
        return ESP_ERR_INVALID_ARG;
    }
    if (!s_inited) {
        return ESP_ERR_INVALID_STATE;
    }

    memset(out, 0, sizeof(*out));
    out->state = s_state;
    out->time_synced = s_time_synced;
    out->rssi = s_rssi;
    out->retry_count = s_retry_count;
    out->next_retry_ms = s_next_retry_ms;
    snprintf(out->ip, sizeof(out->ip), "%s", s_ip);
    snprintf(out->ssid, sizeof(out->ssid), "%s", s_ssid);
    return ESP_OK;
}

esp_err_t net_reconnect(void)
{
    if (!s_inited || !s_configured) {
        return ESP_ERR_INVALID_STATE;
    }

    reset_backoff();
    ESP_LOGI(TAG, "收到主动重连请求，重置退避并断开当前连接");
    esp_err_t err = esp_wifi_disconnect();
    if (err != ESP_OK) {
        /* 可能当前就不在连接状态：仍尝试发起一次连接 */
        ESP_LOGW(TAG, "esp_wifi_disconnect 返回 %s，直接发起连接", esp_err_to_name(err));
        esp_wifi_connect();
    }
    return ESP_OK;
}
