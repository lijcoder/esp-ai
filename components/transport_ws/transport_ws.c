/*
 * transport_ws —— 云端 WebSocket 传输（L1 基础服务）。
 *
 * 为什么用 esp_websocket_client：RFC 6455 的掩码/分片/关闭握手自己写容易埋 bug
 * （T1.5 的 mock 用 websockets 库，对协议严格），而官方组件是 Apache-2.0、随 IDF 生态维护；
 * 代价是内部 RAM 与一个库任务，实测值记入 STATUS 基线，评估见 README「依赖评估」。
 *
 * 为什么自己再建一个 `ws` 任务：
 *   1. 发送必须非阻塞（上层可能来自音频链路，只允许入队，ARCHITECTURE §4.3 第 2 条）；
 *   2. 重连退避必须是 1→2→5→10→30s（ARCHITECTURE §6.1），官方自动重连是固定间隔；
 *   3. 心跳是 JSON 文本 ping/pong（§6.1），不是 WS 协议 ping，需要自己的计时。
 * 该任务按 ARCHITECTURE §9.3 固定为核 0 / 优先级 5 / 6KB；esp_websocket_client 的
 * `ws_client` 任务是库任务（类似 wifi/sys_evt），接收回调就在它的上下文里执行。
 */
#include "transport_ws.h"

#include <inttypes.h>
#include <stdio.h>
#include <string.h>

#include "esp_crt_bundle.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "esp_websocket_client.h"
#include "event_bus.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/task.h"
#include "net_mgr.h"
#include "sdkconfig.h"

static const char *TAG = "WS";

/* ---------------- 协议常量（ARCHITECTURE §6.1 定稿，改动须先改架构文档） ---------------- */

#define WS_PING_INTERVAL_MS    20000 /* 每 20s 发一次 {"t":"ping"} */
#define WS_PONG_TIMEOUT_MS     10000 /* 10s 内没收到 pong 判离线 */
#define WS_HELLO_ACK_TIMEOUT_MS 5000 /* hello 后 5s 没收到 hello_ack 判建连失败 */
#define WS_CONNECT_TIMEOUT_MS  15000 /* TCP/TLS/WS 握手超时；TLS 首次握手较慢，留足余量 */

/* 重连退避：1s → 2s → 5s → 10s → 30s，之后保持 30s 无限重试（ARCHITECTURE §6.1） */
static const uint32_t k_backoff_ms[] = {1000, 2000, 5000, 10000, 30000};
#define BACKOFF_STEPS (sizeof(k_backoff_ms) / sizeof(k_backoff_ms[0]))

/* 鉴权失败的应用关闭码（PROTOCOL.md P-5 定稿） */
#define WS_CLOSE_BAD_DEVICE 4001
#define WS_CLOSE_BAD_TOKEN  4002

/* 状态机 tick：200ms 足够覆盖心跳/超时/退避精度，任务其余时间阻塞在命令队列上 */
#define WS_TICK_MS 200

#define WS_CMD_QUEUE_DEPTH 16

/* 发送队列槽数：由 Kconfig 总容量 / 单槽大小推出（PSRAM 分配） */
#define WS_TX_SLOTS (CONFIG_WS_TX_QUEUE_BYTES / CONFIG_WS_TX_MSG_BYTES)

typedef enum {
    WS_MSG_TEXT = 0, /* data 里是 JSON 文本 */
    WS_MSG_BIN,      /* data 里是完整二进制帧（8B 帧头 + payload） */
} ws_msg_kind_t;

/* 发送队列单条消息（放 PSRAM，见 Kconfig WS_TX_QUEUE_BYTES 的说明）
 * 字段顺序刻意让 sizeof(ws_tx_msg_t) == CONFIG_WS_TX_MSG_BYTES（避免对齐填充多占 PSRAM） */
typedef struct {
    uint16_t len;
    uint8_t kind;
    uint8_t data[CONFIG_WS_TX_MSG_BYTES - 3];
} ws_tx_msg_t;

typedef enum {
    WS_CMD_START = 0,
    WS_CMD_STOP,
    WS_CMD_RECONNECT,
    WS_CMD_NET_UP,
    WS_CMD_NET_DOWN,
    WS_CMD_CLIENT_CONNECTED,
    WS_CMD_CLIENT_READY,
    WS_CMD_CLIENT_DISCONNECTED,
    WS_CMD_TX_QUEUED, /* 仅用于唤醒 `ws` 任务去发队列 */
} ws_cmd_t;

/* ---------------- 全局状态 ---------------- */

static bool s_inited;
static bool s_started;                    /* 上层是否请求保持在线 */
static ws_state_t s_state = WS_STATE_IDLE;
static ws_recv_cb_t s_recv_cb;
static void *s_recv_arg;

static char s_uri[512];                   /* 实际连接 URI（含 query；不打印） */
static char s_endpoint[96];               /* 脱敏端点，只用于日志/状态 */
static bool s_url_configured;
static bool s_tls;

static esp_websocket_client_handle_t s_client;
static QueueHandle_t s_cmd_q;
static QueueHandle_t s_tx_q;
static StaticQueue_t s_tx_q_static;
static uint8_t *s_tx_q_storage;           /* PSRAM */

/* 收包重组：客户端按 buffer_size 分片回调，这里按 payload_offset 拼回整帧 */
static uint8_t s_rx_buf[CONFIG_WS_RX_BUFFER_BYTES];
static size_t s_rx_len_expected;
static uint8_t s_rx_opcode;

/* 时间基准（esp_timer_get_time，微秒；0 表示无待办） */
static int64_t s_connect_deadline_us;
static int64_t s_ack_deadline_us;
static int64_t s_next_ping_us;
static int64_t s_ping_sent_us;
static int64_t s_pong_deadline_us;
static int64_t s_backoff_until_us;

/* 退避与上行序号 */
static size_t s_backoff_idx;
static uint32_t s_retry_count;
static uint16_t s_tx_seq;

/* 观测计数（客户端任务与 ws 任务都会写，仅用于观测，允许良性竞争） */
static uint32_t s_connect_count;
static uint32_t s_last_rtt_ms;
static uint32_t s_tx_text;
static uint32_t s_tx_bin;
static uint32_t s_tx_dropped;
static uint32_t s_rx_text;
static uint32_t s_rx_bin;
static uint32_t s_rx_bad;
static volatile int s_last_close_code;

/* "等待网络"日志只打一次，避免 200ms tick 刷屏 */
static bool s_wait_logged;

/* 主动 stop/destroy 期间客户端也会抛 ERROR：这类是预期内的，降级成 DEBUG */
static volatile bool s_stopping;

static int64_t now_us(void)
{
    return esp_timer_get_time();
}

/* ---------------- 基础工具 ---------------- */

const char *ws_state_name(ws_state_t state)
{
    switch (state) {
    case WS_STATE_IDLE:
        return "IDLE";
    case WS_STATE_WAIT_NET:
        return "WAIT_NET";
    case WS_STATE_CONNECTING:
        return "CONNECTING";
    case WS_STATE_CONNECTED:
        return "CONNECTED";
    case WS_STATE_READY:
        return "READY";
    case WS_STATE_BACKOFF:
        return "BACKOFF";
    default:
        return "UNKNOWN";
    }
}

static void post_cmd(ws_cmd_t cmd)
{
    if (s_cmd_q == NULL) {
        return;
    }
    /* 队列项固定 1 字节（uint8_t）：收发两侧类型必须严格一致，
     * 直接用枚举当队项会在接收侧留下未初始化的高字节（T1.4 真机联调踩过） */
    const uint8_t raw = (uint8_t)cmd;
    if (xQueueSend(s_cmd_q, &raw, 0) != pdTRUE) {
        /* 命令承载状态迁移，丢了会让状态机停在旧状态：正常路径远低于 16 深，真丢了必须可见 */
        ESP_LOGE(TAG, "命令队列满，丢弃命令 %d", (int)cmd);
    }
}

/* 丢帧是高频率但可恢复的事件（如未就绪时上层仍发 audio_start）：首帧 + 每 100 帧报一次 */
static void log_tx_drop(const char *what, esp_err_t err)
{
    if (s_tx_dropped == 1 || (s_tx_dropped % 100) == 0) {
        ESP_LOGW(TAG, "%s 丢弃（%s），累计 %" PRIu32 " 帧", what, esp_err_to_name(err),
                 s_tx_dropped);
    }
}

/*
 * 极简 JSON 字符串字段提取：只服务传输层的 hello_ack/pong 判定，不解析业务报文。
 * 不做转义/嵌套处理——业务 JSON 由 T2.5 voice_cloud 解析。
 */
static bool json_get_str(const uint8_t *buf, size_t len, const char *key, char *out,
                         size_t out_len)
{
    char pattern[16];
    const int n = snprintf(pattern, sizeof(pattern), "\"%s\"", key);
    if (n <= 0 || (size_t)n >= sizeof(pattern)) {
        return false;
    }
    const size_t plen = (size_t)n;

    for (size_t i = 0; i + plen < len; i++) {
        if (memcmp(buf + i, pattern, plen) != 0) {
            continue;
        }
        size_t j = i + plen;
        while (j < len && (buf[j] == ' ' || buf[j] == '\t')) {
            j++;
        }
        if (j >= len || buf[j] != ':') {
            continue;
        }
        j++;
        while (j < len && (buf[j] == ' ' || buf[j] == '\t')) {
            j++;
        }
        if (j >= len || buf[j] != '"') {
            continue;
        }
        j++;
        size_t k = 0;
        while (j < len && buf[j] != '"' && k + 1 < out_len) {
            out[k++] = (char)buf[j++];
        }
        if (j >= len || buf[j] != '"') {
            return false;
        }
        out[k] = '\0';
        return true;
    }
    return false;
}

/* ---------------- URI 与端点 ---------------- */

static bool uri_append(const char *text)
{
    const size_t used = strlen(s_uri);
    const size_t add = strlen(text);
    if (used + add >= sizeof(s_uri)) {
        return false;
    }
    memcpy(s_uri + used, text, add + 1);
    return true;
}

/*
 * 契约 URL 必须带 device_id/token/ver（ARCHITECTURE §6.1）。Kconfig 只填基址时由端侧补齐，
 * 避免每次联调都要手工拼 query；已经在 URL 里的参数不重复追加。
 */
static bool build_uri(void)
{
    if (CONFIG_AI_WS_URL[0] == '\0') {
        return false;
    }
    if (strncmp(CONFIG_AI_WS_URL, "ws://", 5) != 0 &&
        strncmp(CONFIG_AI_WS_URL, "wss://", 6) != 0) {
        ESP_LOGE(TAG, "CONFIG_AI_WS_URL 的 scheme 非法（只支持 ws:// 与 wss://）");
        return false;
    }

    snprintf(s_uri, sizeof(s_uri), "%s", CONFIG_AI_WS_URL);
    const bool has_query = strchr(CONFIG_AI_WS_URL, '?') != NULL;
    char sep = has_query ? '&' : '?';
    char part[160];

    if (strstr(CONFIG_AI_WS_URL, "device_id=") == NULL) {
        snprintf(part, sizeof(part), "%cdevice_id=%s", sep, CONFIG_AI_DEVICE_ID);
        sep = '&';
        if (!uri_append(part)) {
            ESP_LOGE(TAG, "URL 过长：追加 device_id 失败");
            return false;
        }
    }
    if (CONFIG_AI_TOKEN[0] != '\0' && strstr(CONFIG_AI_WS_URL, "token=") == NULL) {
        snprintf(part, sizeof(part), "%ctoken=%s", sep, CONFIG_AI_TOKEN);
        sep = '&';
        if (!uri_append(part)) {
            ESP_LOGE(TAG, "URL 过长：追加 token 失败");
            return false;
        }
    }
    if (strstr(CONFIG_AI_WS_URL, "ver=") == NULL) {
        snprintf(part, sizeof(part), "%cver=1", sep);
        if (!uri_append(part)) {
            ESP_LOGE(TAG, "URL 过长：追加 ver 失败");
            return false;
        }
    }

    /* 日志只保留 scheme://host[:port]/path：query 里有 token，绝不打日志 */
    const char *q = strchr(s_uri, '?');
    size_t n = q != NULL ? (size_t)(q - s_uri) : strlen(s_uri);
    if (n >= sizeof(s_endpoint)) {
        n = sizeof(s_endpoint) - 1;
    }
    memcpy(s_endpoint, s_uri, n);
    s_endpoint[n] = '\0';
    return true;
}

/* ---------------- 接收：分片重组 + 帧校验 ---------------- */

static void on_rx_message(uint8_t opcode, const uint8_t *buf, size_t len)
{
    if (opcode == 0x2) { /* 二进制：ARCHITECTURE §6.2 */
        if (len < WS_FRAME_HEADER_LEN || buf[0] != WS_FRAME_MAGIC0 || buf[1] != WS_FRAME_MAGIC1 ||
            buf[2] != WS_FRAME_VER) {
            s_rx_bad++;
            ESP_LOGW(TAG, "丢弃坏二进制帧（len=%u 或 magic/ver 不符）", (unsigned)len);
            return;
        }
        const uint8_t type = buf[3];
        const uint16_t seq = (uint16_t)(((uint16_t)buf[4] << 8) | buf[5]);
        const uint16_t plen = (uint16_t)(((uint16_t)buf[6] << 8) | buf[7]);
        if ((size_t)plen + WS_FRAME_HEADER_LEN != len) {
            s_rx_bad++;
            ESP_LOGW(TAG, "丢弃坏二进制帧：头里 len=%u 与实际 payload %u 不符", (unsigned)plen,
                     (unsigned)(len - WS_FRAME_HEADER_LEN));
            return;
        }
        if (type != WS_FRAME_TYPE_AUDIO_DOWN) {
            s_rx_bad++;
            ESP_LOGW(TAG, "丢弃未知下行帧类型 0x%02X（只接受 0x02）", type);
            return;
        }
        s_rx_bin++;
        if (s_recv_cb != NULL) {
            const ws_frame_hdr_t hdr = {.ver = buf[2], .type = type, .seq = seq};
            s_recv_cb(&hdr, buf + WS_FRAME_HEADER_LEN, plen, s_recv_arg);
        }
        return;
    }

    /* 文本：传输层只消费 hello_ack / pong，其余原样交给上层 */
    s_rx_text++;
    char t[16];
    if (json_get_str(buf, len, "t", t, sizeof(t))) {
        if (strcmp(t, "pong") == 0) {
            if (s_ping_sent_us > 0) {
                s_last_rtt_ms = (uint32_t)((now_us() - s_ping_sent_us) / 1000);
            }
            s_pong_deadline_us = 0;
            ESP_LOGD(TAG, "收到 pong，rtt=%" PRIu32 " ms", s_last_rtt_ms);
            return;
        }
        if (strcmp(t, "hello_ack") == 0) {
            post_cmd(WS_CMD_CLIENT_READY);
            return;
        }
    }
    if (s_recv_cb != NULL) {
        s_recv_cb(NULL, buf, len, s_recv_arg);
    }
}

static void handle_data_event(const esp_websocket_event_data_t *ed)
{
    if (ed == NULL || ed->data_ptr == NULL || ed->data_len <= 0) {
        return;
    }
    /* WS 协议级 ping/pong 控制帧：契约用 JSON 文本心跳，这里不处理 */
    if (ed->op_code == 0x9 || ed->op_code == 0xA) {
        return;
    }

    if (ed->payload_len <= 0 || (size_t)ed->payload_len > sizeof(s_rx_buf)) {
        if (ed->payload_offset == 0) {
            s_rx_bad++;
            ESP_LOGW(TAG, "下行消息过长（%d B > %u B），丢弃", ed->payload_len,
                     (unsigned)sizeof(s_rx_buf));
        }
        return;
    }
    if (ed->payload_offset == 0) {
        s_rx_len_expected = (size_t)ed->payload_len;
        s_rx_opcode = ed->op_code;
    } else if (s_rx_len_expected != (size_t)ed->payload_len) {
        s_rx_bad++;
        s_rx_len_expected = 0;
        ESP_LOGW(TAG, "下行分片不一致，丢弃当前消息");
        return;
    }
    if ((size_t)ed->payload_offset + (size_t)ed->data_len > s_rx_len_expected) {
        s_rx_bad++;
        s_rx_len_expected = 0;
        ESP_LOGW(TAG, "下行分片越界，丢弃当前消息");
        return;
    }

    memcpy(s_rx_buf + ed->payload_offset, ed->data_ptr, (size_t)ed->data_len);
    if ((size_t)ed->payload_offset + (size_t)ed->data_len < s_rx_len_expected) {
        return; /* 消息还没收全，等后续分片 */
    }

    const size_t total = s_rx_len_expected;
    const uint8_t opcode = s_rx_opcode;
    s_rx_len_expected = 0;
    on_rx_message(opcode, s_rx_buf, total);
}

/* ---------------- 客户端事件（在 `ws_client` 任务上下文） ---------------- */

static void ws_event_handler(void *arg, esp_event_base_t base, int32_t id, void *data)
{
    (void)arg;
    (void)base;
    const esp_websocket_event_data_t *ed = (const esp_websocket_event_data_t *)data;

    switch (id) {
    case WEBSOCKET_EVENT_CONNECTED:
        post_cmd(WS_CMD_CLIENT_CONNECTED);
        break;
    case WEBSOCKET_EVENT_CLOSED:
    case WEBSOCKET_EVENT_DISCONNECTED:
        /* CLOSED（干净关闭）与 DISCONNECTED 可能各来一次，ws 任务用 s_client 判重 */
        if (ed != NULL) {
            s_last_close_code = ed->close_status_code;
        }
        post_cmd(WS_CMD_CLIENT_DISCONNECTED);
        break;
    case WEBSOCKET_EVENT_ERROR:
        if (ed != NULL) {
            s_last_close_code = ed->close_status_code;
            if (s_stopping) {
                /* 主动 stop 时连接被我们自己掐断，客户端报错属于预期噪音 */
                ESP_LOGD(TAG, "主动断开期间的客户端错误（已忽略）");
                break;
            }
            /* 注意：只有 TCP_TRANSPORT 类错误才会填 esp_tls_* 字段，
             * 其它类型读它们是未初始化值（真机首次联调打出过垃圾数字） */
            if (ed->error_handle.error_type == WEBSOCKET_ERROR_TYPE_TCP_TRANSPORT) {
                ESP_LOGE(TAG,
                         "WS 错误：TCP/TLS 失败（tls_err=0x%x stack=%d verify=0x%x sock_errno=%d）",
                         ed->error_handle.esp_tls_last_esp_err,
                         ed->error_handle.esp_tls_stack_err,
                         ed->error_handle.esp_tls_cert_verify_flags,
                         ed->error_handle.esp_transport_sock_errno);
            } else {
                ESP_LOGE(TAG, "WS 错误：type=%d，握手 HTTP 状态=%d",
                         (int)ed->error_handle.error_type,
                         ed->error_handle.esp_ws_handshake_status_code);
            }
        }
        post_cmd(WS_CMD_CLIENT_DISCONNECTED);
        break;
    case WEBSOCKET_EVENT_DATA:
        handle_data_event(ed);
        break;
    default:
        break;
    }
}

/* ---------------- 连接生命周期（全部在 `ws` 任务里） ---------------- */

static void client_destroy(void)
{
    if (s_client == NULL) {
        return;
    }
    /* stop 会等客户端任务退出；事件回调里禁止调用，所以统一在这里做 */
    s_stopping = true;
    (void)esp_websocket_client_stop(s_client);
    (void)esp_websocket_client_destroy(s_client);
    s_client = NULL;
    s_stopping = false;
    s_connect_deadline_us = 0;
    s_ack_deadline_us = 0;
    s_pong_deadline_us = 0;
    s_ping_sent_us = 0;
    s_wait_logged = false;
    /* 未发出去的旧帧不能带到下一次连接（否则会把上一轮音频补发给服务端） */
    if (s_tx_q != NULL) {
        xQueueReset(s_tx_q);
    }
}

static void backoff_reset(void)
{
    s_backoff_idx = 0;
    s_retry_count = 0;
    s_backoff_until_us = 0;
}

static void enter_wait_net(const char *why)
{
    s_state = WS_STATE_WAIT_NET;
    if (!s_wait_logged && why != NULL) {
        ESP_LOGI(TAG, "等待网络就绪后建连（%s）", why);
        s_wait_logged = true;
    }
}

static void backoff_schedule(const char *why)
{
    const size_t idx = s_backoff_idx < BACKOFF_STEPS ? s_backoff_idx : BACKOFF_STEPS - 1;
    const uint32_t delay_ms = k_backoff_ms[idx];
    if (s_backoff_idx + 1 < BACKOFF_STEPS) {
        s_backoff_idx++;
    }
    s_retry_count++;
    s_backoff_until_us = now_us() + (int64_t)delay_ms * 1000;
    s_state = WS_STATE_BACKOFF;
    ESP_LOGW(TAG, "%s，%u ms 后重连（第 %" PRIu32 " 次）", why != NULL ? why : "连接失败",
             (unsigned)delay_ms, s_retry_count);
}

static void start_attempt(void)
{
    if (!s_started || !s_url_configured) {
        return;
    }
    if (s_client != NULL) {
        client_destroy();
    }
    if (!net_is_up()) {
        enter_wait_net("网络未就绪");
        return;
    }
    if (s_tls && !net_is_time_synced()) {
        /* wss 校验证书需要可信时间：GOT_IP 到 SNTP 对时实测约 40ms，等下一 tick 再试 */
        enter_wait_net("等待 SNTP 对时（wss 证书校验）");
        return;
    }

    esp_websocket_client_config_t cfg = {0};
    cfg.uri = s_uri;
    cfg.disable_auto_reconnect = true; /* 退避由本组件控制（1/2/5/10/30s） */
    cfg.network_timeout_ms = 10000;    /* 与心跳 10s 超时同量级 */
    cfg.buffer_size = CONFIG_WS_RX_BUFFER_BYTES;
    cfg.task_stack = CONFIG_WS_CLIENT_TASK_STACK;
    cfg.task_prio = 5;
    cfg.task_core_id_set = true;
    cfg.task_core_id = 0; /* 网络路径固定核 0，核 1 只跑音频 */
    cfg.task_name = "ws_client";
    cfg.ping_interval_sec = 0;         /* 契约用 JSON 文本 ping，不用 WS 协议 ping */
    cfg.disable_pingpong_discon = true;
    cfg.crt_bundle_attach = esp_crt_bundle_attach; /* 只对 wss 生效 */

    s_client = esp_websocket_client_init(&cfg);
    if (s_client == NULL) {
        ESP_LOGE(TAG, "创建 WS 客户端失败（内部 RAM 不足）");
        backoff_schedule("创建客户端失败");
        return;
    }

    s_last_close_code = 0;
    esp_err_t err = esp_websocket_register_events(s_client, WEBSOCKET_EVENT_ANY, ws_event_handler,
                                                  NULL);
    if (err == ESP_OK) {
        err = esp_websocket_client_start(s_client);
    }
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "启动 WS 客户端失败: %s", esp_err_to_name(err));
        client_destroy();
        backoff_schedule("启动客户端失败");
        return;
    }

    s_state = WS_STATE_CONNECTING;
    s_connect_deadline_us = now_us() + (int64_t)WS_CONNECT_TIMEOUT_MS * 1000;
    ESP_LOGI(TAG, "开始连接（第 %" PRIu32 " 次）：%s", s_retry_count + 1, s_endpoint);
}

static void send_ping(void)
{
    static const char k_ping[] = "{\"t\":\"ping\"}";
    const int sent = esp_websocket_client_send_text(s_client, k_ping, (int)(sizeof(k_ping) - 1),
                                                    pdMS_TO_TICKS(CONFIG_WS_SEND_TIMEOUT_MS));
    if (sent < 0) {
        /* 发送失败通常意味着连接已断：不在这里重试，等客户端事件/超时统一走退避 */
        ESP_LOGW(TAG, "发送 ping 失败（连接可能已断）");
        return;
    }
    s_ping_sent_us = now_us();
    s_next_ping_us = s_ping_sent_us + (int64_t)WS_PING_INTERVAL_MS * 1000;
    s_pong_deadline_us = s_ping_sent_us + (int64_t)WS_PONG_TIMEOUT_MS * 1000;
    ESP_LOGD(TAG, "已发 ping，等 pong（%d ms 超时）", WS_PONG_TIMEOUT_MS);
}

static void on_client_connected(void)
{
    if (s_client == NULL) {
        return;
    }
    s_state = WS_STATE_CONNECTED;
    s_connect_deadline_us = 0;

    char hello[192];
    const int n = snprintf(hello, sizeof(hello),
                           "{\"t\":\"hello\",\"device_id\":\"%s\",\"ver\":1}",
                           CONFIG_AI_DEVICE_ID);
    if (n <= 0 || (size_t)n >= sizeof(hello)) {
        ESP_LOGE(TAG, "hello 报文组装失败");
        client_destroy();
        backoff_schedule("hello 组装失败");
        return;
    }
    const int sent = esp_websocket_client_send_text(s_client, hello, n,
                                                    pdMS_TO_TICKS(CONFIG_WS_SEND_TIMEOUT_MS));
    if (sent < 0) {
        client_destroy();
        backoff_schedule("发送 hello 失败");
        return;
    }
    s_ack_deadline_us = now_us() + (int64_t)WS_HELLO_ACK_TIMEOUT_MS * 1000;
    ESP_LOGI(TAG, "已连接 %s，已发 hello，等待 hello_ack", s_endpoint);
}

static void on_client_ready(void)
{
    if (s_client == NULL || s_state != WS_STATE_CONNECTED) {
        return;
    }
    s_state = WS_STATE_READY;
    s_ack_deadline_us = 0;
    s_connect_count++;
    backoff_reset();
    s_next_ping_us = now_us() + (int64_t)WS_PING_INTERVAL_MS * 1000;
    s_pong_deadline_us = 0;
    ESP_LOGI(TAG, "握手完成（hello_ack），链路可用：%s", s_endpoint);
}

static void on_client_disconnected(void)
{
    if (s_client == NULL) {
        return; /* CLOSED/DISCONNECTED 重复事件：已经处理过 */
    }
    const int code = s_last_close_code;
    client_destroy();

    if (code == WS_CLOSE_BAD_DEVICE || code == WS_CLOSE_BAD_TOKEN) {
        s_state = WS_STATE_IDLE;
        ESP_LOGE(TAG, "鉴权失败（关闭码 %d，%s 不被服务端接受）：停止自动重连，"
                      "修正配置后重启或执行 ws reconnect",
                 code, code == WS_CLOSE_BAD_DEVICE ? "device_id" : "token");
        return;
    }
    if (!s_started) {
        s_state = WS_STATE_IDLE;
        return;
    }
    if (!net_is_up()) {
        enter_wait_net("网络已断开");
        return;
    }

    char why[64];
    if (code != 0) {
        snprintf(why, sizeof(why), "连接关闭（code=%d）", code);
    } else {
        snprintf(why, sizeof(why), "连接断开");
    }
    backoff_schedule(why);
}

/* ---------------- 发送队列出队 ---------------- */

static void drain_tx(void)
{
    ws_tx_msg_t msg;

    if (s_tx_q == NULL || s_client == NULL || s_state != WS_STATE_READY) {
        return;
    }
    while (xQueueReceive(s_tx_q, &msg, 0) == pdTRUE) {
        const int sent =
            (msg.kind == WS_MSG_BIN)
                ? esp_websocket_client_send_bin(s_client, (const char *)msg.data, msg.len,
                                                pdMS_TO_TICKS(CONFIG_WS_SEND_TIMEOUT_MS))
                : esp_websocket_client_send_text(s_client, (const char *)msg.data, msg.len,
                                                 pdMS_TO_TICKS(CONFIG_WS_SEND_TIMEOUT_MS));
        if (sent < 0) {
            ESP_LOGW(TAG, "发送失败（连接可能已断），清空剩余待发帧等重连");
            xQueueReset(s_tx_q);
            return;
        }
    }
}

/* ---------------- 状态机 tick 与命令处理 ---------------- */

static void handle_tick(void)
{
    const int64_t now = now_us();

    switch (s_state) {
    case WS_STATE_WAIT_NET:
        start_attempt();
        break;
    case WS_STATE_CONNECTING:
        if (s_connect_deadline_us > 0 && now > s_connect_deadline_us) {
            client_destroy();
            backoff_schedule("建连超时（TCP/TLS/WS 握手）");
        }
        break;
    case WS_STATE_CONNECTED:
        if (s_ack_deadline_us > 0 && now > s_ack_deadline_us) {
            client_destroy();
            backoff_schedule("hello_ack 超时");
        }
        break;
    case WS_STATE_READY:
        if (s_pong_deadline_us > 0 && now > s_pong_deadline_us) {
            ESP_LOGE(TAG, "心跳超时：%d ms 内未收到 pong，判定断线", WS_PONG_TIMEOUT_MS);
            s_pong_deadline_us = 0;
            client_destroy();
            if (s_started) {
                backoff_schedule("心跳超时");
            } else {
                s_state = WS_STATE_IDLE;
            }
        } else if (now >= s_next_ping_us) {
            send_ping();
        }
        break;
    case WS_STATE_BACKOFF:
        if (now >= s_backoff_until_us) {
            s_state = WS_STATE_WAIT_NET;
        }
        break;
    default:
        break;
    }
}

static void handle_cmd(ws_cmd_t cmd)
{
    switch (cmd) {
    case WS_CMD_START:
        if (!s_started) {
            s_started = true;
            backoff_reset();
            s_wait_logged = false;
            s_state = WS_STATE_WAIT_NET;
            ESP_LOGI(TAG, "请求保持在线：网络就绪后建连");
        }
        break;
    case WS_CMD_STOP:
        s_started = false;
        client_destroy();
        s_state = WS_STATE_IDLE;
        ESP_LOGI(TAG, "已下线（停止自动重连）");
        break;
    case WS_CMD_RECONNECT:
        s_started = true;
        backoff_reset();
        s_wait_logged = false;
        client_destroy();
        s_state = WS_STATE_WAIT_NET;
        ESP_LOGI(TAG, "收到主动重连请求：已重置退避，立即重连");
        break;
    case WS_CMD_NET_UP:
        if (!s_started) {
            break;
        }
        /* 网络换了新地址/新链路：退避从头开始 */
        backoff_reset();
        s_wait_logged = false;
        if (s_state == WS_STATE_CONNECTING || s_state == WS_STATE_CONNECTED ||
            s_state == WS_STATE_READY) {
            /* 已经连上/正在连：NET_UP 只是对"网络就绪"的确认，不能打断进行中的握手
             * （T1.4 真机联调发现 tick 先建连、NET_UP 又拆掉重连，导致多一次 hello 失败） */
            break;
        }
        client_destroy();
        s_state = WS_STATE_WAIT_NET;
        ESP_LOGI(TAG, "网络就绪，准备建连");
        break;
    case WS_CMD_NET_DOWN:
        if (s_state == WS_STATE_READY || s_state == WS_STATE_CONNECTED ||
            s_state == WS_STATE_CONNECTING) {
            client_destroy();
            s_state = s_started ? WS_STATE_WAIT_NET : WS_STATE_IDLE;
            ESP_LOGW(TAG, "网络断开：停止 WebSocket，等 NET_UP 后自动恢复");
        } else if (s_state == WS_STATE_BACKOFF) {
            s_state = s_started ? WS_STATE_WAIT_NET : WS_STATE_IDLE;
        }
        break;
    case WS_CMD_CLIENT_CONNECTED:
        on_client_connected();
        break;
    case WS_CMD_CLIENT_READY:
        on_client_ready();
        break;
    case WS_CMD_CLIENT_DISCONNECTED:
        on_client_disconnected();
        break;
    case WS_CMD_TX_QUEUED:
        /* 只是把任务从等待中叫醒；出队在 drain_tx() 统一做 */
        break;
    default:
        break;
    }
}

static void ws_task(void *arg)
{
    (void)arg;
    uint8_t raw;

    for (;;) {
        if (xQueueReceive(s_cmd_q, &raw, pdMS_TO_TICKS(WS_TICK_MS)) == pdTRUE) {
            handle_cmd((ws_cmd_t)raw);
        }
        handle_tick();
        drain_tx();
    }
}

/* ---------------- event_bus 接线（在 `evt` 任务上下文） ---------------- */

static void on_net_up_cb(void *arg, app_evt_t evt, const app_evt_data_t *data)
{
    (void)arg;
    (void)evt;
    (void)data;
    post_cmd(WS_CMD_NET_UP);
}

static void on_net_down_cb(void *arg, app_evt_t evt, const app_evt_data_t *data)
{
    (void)arg;
    (void)evt;
    (void)data;
    post_cmd(WS_CMD_NET_DOWN);
}

/* ---------------- 对外 API ---------------- */

esp_err_t ws_init(void)
{
    if (s_inited) {
        return ESP_ERR_INVALID_STATE;
    }

    s_url_configured = build_uri();
    if (s_url_configured) {
        s_tls = strncmp(s_uri, "wss://", 6) == 0;
        if (s_tls) {
            ESP_LOGI(TAG, "云端端点（wss，TLS 证书走 CRT bundle）：%s", s_endpoint);
        } else {
            ESP_LOGW(TAG, "使用明文 ws://（%s）：仅供本地 mock 联调，量产必须用 wss://",
                     s_endpoint);
        }
        if (CONFIG_AI_DEVICE_ID[0] == '\0') {
            ESP_LOGW(TAG, "CONFIG_AI_DEVICE_ID 为空，hello 会带空 device_id");
        }
    } else {
        ESP_LOGW(TAG, "未配置有效的 CONFIG_AI_WS_URL，云端链路关闭（本地唤醒/播放不受影响）");
    }

    /* 发送队列放 PSRAM（CONVENTIONS §4；X3 方案 D）；内存不足只降级离线，不阻塞启动 */
    const size_t slots = WS_TX_SLOTS;
    if (s_url_configured) {
        if (slots < 2) {
            ESP_LOGE(TAG, "发送队列配置非法：%d B / %d B = %u 槽（至少 2 槽）",
                     CONFIG_WS_TX_QUEUE_BYTES, CONFIG_WS_TX_MSG_BYTES, (unsigned)slots);
            s_url_configured = false;
        } else {
            const size_t bytes = slots * sizeof(ws_tx_msg_t);
            s_tx_q_storage = heap_caps_malloc(bytes, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
            if (s_tx_q_storage == NULL) {
                ESP_LOGE(TAG, "发送队列分配失败（PSRAM %u B），云端链路关闭", (unsigned)bytes);
                s_url_configured = false;
            } else {
                s_tx_q = xQueueCreateStatic(slots, sizeof(ws_tx_msg_t), s_tx_q_storage,
                                            &s_tx_q_static);
                if (s_tx_q == NULL) {
                    heap_caps_free(s_tx_q_storage);
                    s_tx_q_storage = NULL;
                    ESP_LOGE(TAG, "创建发送队列失败（内存不足），云端链路关闭");
                    s_url_configured = false;
                }
            }
        }
    }

    s_cmd_q = xQueueCreate(WS_CMD_QUEUE_DEPTH, sizeof(uint8_t));
    if (s_cmd_q == NULL) {
        ESP_LOGE(TAG, "创建命令队列失败（内存不足）");
        return ESP_ERR_NO_MEM;
    }

    /* 网络状态只经 event_bus 获取（CONVENTIONS §2：同层不直连） */
    esp_err_t err = app_evt_register(APP_EVT_NET_UP, on_net_up_cb, NULL);
    if (err == ESP_OK) {
        err = app_evt_register(APP_EVT_NET_DOWN, on_net_down_cb, NULL);
    }
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "注册网络事件处理器失败: %s", esp_err_to_name(err));
        return err;
    }

    if (xTaskCreatePinnedToCore(ws_task, "ws", CONFIG_WS_TASK_STACK, NULL, 5, NULL, 0) != pdPASS) {
        ESP_LOGE(TAG, "创建 ws 任务失败（栈 %d B）", CONFIG_WS_TASK_STACK);
        return ESP_ERR_NO_MEM;
    }

    s_inited = true;
    if (s_url_configured) {
        ESP_LOGI(TAG, "初始化完成：发送队列 %u 槽 × %u B（PSRAM）", (unsigned)slots,
                 (unsigned)sizeof(ws_tx_msg_t));
    }
    return ESP_OK;
}

esp_err_t ws_start(void)
{
    if (!s_inited) {
        return ESP_ERR_INVALID_STATE;
    }
    if (!s_url_configured) {
        ESP_LOGW(TAG, "未配置可用的 WS 端点，忽略 ws_start");
        return ESP_ERR_INVALID_STATE;
    }
    post_cmd(WS_CMD_START);
    return ESP_OK;
}

esp_err_t ws_stop(void)
{
    if (!s_inited) {
        return ESP_ERR_INVALID_STATE;
    }
    post_cmd(WS_CMD_STOP);
    return ESP_OK;
}

esp_err_t ws_register_cb(ws_recv_cb_t cb, void *arg)
{
    if (!s_inited) {
        return ESP_ERR_INVALID_STATE;
    }
    s_recv_cb = cb;
    s_recv_arg = arg;
    return ESP_OK;
}

static esp_err_t tx_enqueue_msg(ws_tx_msg_t *msg)
{
    if (!s_inited || s_tx_q == NULL) {
        return ESP_ERR_INVALID_STATE;
    }
    if (s_state != WS_STATE_READY) {
        s_tx_dropped++;
        log_tx_drop("帧", ESP_ERR_INVALID_STATE);
        return ESP_ERR_INVALID_STATE;
    }
    if (xQueueSend(s_tx_q, msg, 0) != pdTRUE) {
        s_tx_dropped++;
        log_tx_drop("帧", ESP_ERR_TIMEOUT);
        return ESP_ERR_TIMEOUT;
    }
    post_cmd(WS_CMD_TX_QUEUED);
    return ESP_OK;
}

esp_err_t ws_send_text(const char *json)
{
    if (json == NULL) {
        return ESP_ERR_INVALID_ARG;
    }
    if (!s_inited) {
        return ESP_ERR_INVALID_STATE;
    }
    const size_t len = strlen(json);
    if (len > sizeof(((ws_tx_msg_t *)0)->data)) {
        ESP_LOGE(TAG, "文本帧过长（%u B > %u B）", (unsigned)len,
                 (unsigned)sizeof(((ws_tx_msg_t *)0)->data));
        return ESP_ERR_INVALID_SIZE;
    }

    ws_tx_msg_t msg = {0};
    msg.kind = WS_MSG_TEXT;
    msg.len = (uint16_t)len;
    memcpy(msg.data, json, len);

    const esp_err_t err = tx_enqueue_msg(&msg);
    if (err == ESP_OK) {
        s_tx_text++;
    }
    return err;
}

esp_err_t ws_send_bin(uint8_t type, const void *payload, size_t len)
{
    if (payload == NULL && len > 0) {
        return ESP_ERR_INVALID_ARG;
    }
    if (type != WS_FRAME_TYPE_AUDIO_UP) {
        /* 下行类型 0x02 只由服务端发；端侧发错类型属于契约违例，直接拒绝 */
        ESP_LOGW(TAG, "拒绝发送非法帧类型 0x%02X（端侧只允许 0x01 上行）", type);
        return ESP_ERR_INVALID_ARG;
    }
    if (WS_FRAME_HEADER_LEN + len > sizeof(((ws_tx_msg_t *)0)->data)) {
        ESP_LOGE(TAG, "二进制帧过长（payload %u B，单槽上限 %u B）", (unsigned)len,
                 (unsigned)sizeof(((ws_tx_msg_t *)0)->data));
        return ESP_ERR_INVALID_SIZE;
    }

    ws_tx_msg_t msg = {0};
    msg.kind = WS_MSG_BIN;
    msg.len = (uint16_t)(WS_FRAME_HEADER_LEN + len);
    msg.data[0] = WS_FRAME_MAGIC0;
    msg.data[1] = WS_FRAME_MAGIC1;
    msg.data[2] = WS_FRAME_VER;
    msg.data[3] = type;
    msg.data[4] = (uint8_t)(s_tx_seq >> 8);
    msg.data[5] = (uint8_t)(s_tx_seq & 0xFF);
    msg.data[6] = (uint8_t)(len >> 8);
    msg.data[7] = (uint8_t)(len & 0xFF);
    if (len > 0) {
        memcpy(msg.data + WS_FRAME_HEADER_LEN, payload, len);
    }

    const esp_err_t err = tx_enqueue_msg(&msg);
    if (err == ESP_OK) {
        s_tx_seq++;
        s_tx_bin++;
    }
    return err;
}

esp_err_t ws_reconnect(void)
{
    if (!s_inited) {
        return ESP_ERR_INVALID_STATE;
    }
    if (!s_url_configured) {
        return ESP_ERR_INVALID_STATE;
    }
    post_cmd(WS_CMD_RECONNECT);
    return ESP_OK;
}

bool ws_is_ready(void)
{
    return s_state == WS_STATE_READY;
}

esp_err_t ws_status_read(ws_status_t *out)
{
    if (out == NULL) {
        return ESP_ERR_INVALID_ARG;
    }
    if (!s_inited) {
        return ESP_ERR_INVALID_STATE;
    }

    memset(out, 0, sizeof(*out));
    out->state = s_state;
    out->started = s_started;
    out->url_configured = s_url_configured;
    out->tls = s_tls;
    out->connect_count = s_connect_count;
    out->retry_count = s_retry_count;
    out->last_rtt_ms = s_last_rtt_ms;
    out->tx_text = s_tx_text;
    out->tx_bin = s_tx_bin;
    out->tx_dropped = s_tx_dropped;
    out->rx_text = s_rx_text;
    out->rx_bin = s_rx_bin;
    out->rx_bad = s_rx_bad;
    out->queue_cap = (uint32_t)WS_TX_SLOTS;
    out->queue_used = s_tx_q != NULL ? (uint32_t)uxQueueMessagesWaiting(s_tx_q) : 0;
    if (s_state == WS_STATE_BACKOFF) {
        const int64_t remain = s_backoff_until_us - now_us();
        out->next_retry_ms = remain > 0 ? (uint32_t)(remain / 1000) : 0;
    }
    snprintf(out->endpoint, sizeof(out->endpoint), "%s", s_endpoint);
    return ESP_OK;
}
