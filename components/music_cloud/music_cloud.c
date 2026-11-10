/*
 * music_cloud —— 音乐云协议（L3 能力层，T4.3 起为**纯协议**）。
 *
 * 协议（ARCHITECTURE §6.3、§8.3；PROTOCOL.md §3.1–3.3/§7）：
 *
 *   下行 music_track   {"mid":N,"url":"http(s)://...","codec":"mp3","duration_ms":D,"title":"..."}
 *        → 解析 + mid 过滤 → mc_evt_t{kind=TRACK} → music_session（起播/切歌归 L4）
 *   下行 music_control {"mid":N,"action":"stop|pause|resume"}
 *        → 解析 + mid 过滤 → mc_evt_t{kind=CONTROL} → music_session（执行归 L4）
 *   上行 music_status  {"t":"music_status","state":"playing|paused|stopped|error","mid":N,"progress_ms":P}
 *        → 状态/进度由 music_session 传入；周期由 session 驱动
 *   上行 music_request {"t":"music_request","action":"play","id":N,"index":I}
 *        → music_session 兜底点在 esp_timer 上下文串行调用
 *
 * 为什么音乐事件不吃语音 sid（P-3，2026-11-10 定稿）：
 *   音乐跨语音轮次播放（TTS 播完继续放），也可能在没有会话时开始；用语音 sid 过滤会被
 *   「轮次结束 / 迟到帧丢弃」误杀。音乐事件带**连接内自增的 mid**，由本组件独立过滤：
 *   - music_track：只接受比当前新的 mid（同 mid 视为重复，旧 mid 丢弃）；
 *   - music_control：允许 mid == 当前曲目（暂停/停止本来就指向正在播的这首），只丢弃更旧的。
 *   断链后清比较基准：订阅 APP_EVT_NET_DOWN；同时用 ws_status.connect_count 兜住
 *   "WiFi 未断但 WS 重连"（新连接 mid 从头发，旧基准会让所有新曲目被误判过期）。
 *
 * 线程模型（不建任务、不建定时器，见 ARCHITECTURE §9.3）：
 *   - 下行解析在 `ws_client` 任务上下文（transport_ws 的附加订阅槽），只做定长解析与
 *     临界区内的 mid 比较，禁止阻塞/网络/Flash；
 *   - 回调把结果交给 L4；L3 不 include L4，依赖方向只允许 L4 → L3。
 *   - 状态用 `s_mux` 临界区保护；临界区内只做 memcpy/赋值，不打印、不回调。
 *
 * 内存（CONVENTIONS §4）：下行解析用任务栈上的定长缓冲（URL 320B/标题 48B），无静态
 * 大缓冲；不 malloc、不建任务、不占 PSRAM。
 */
#include "music_cloud.h"

#include <stdio.h>
#include <string.h>

#include "esp_log.h"
#include "event_bus.h"
#include "freertos/FreeRTOS.h"
#include "transport_ws.h"

static const char *TAG = "MC";

/* music_status / music_request 报文：字段都很短，96B 足够 */
#define MC_JSON_MAX 96
/* 下行 t / action / codec 字段的定长缓冲 */
#define MC_EVT_TYPE_MAX 16
#define MC_ACTION_MAX 16
#define MC_CODEC_MAX 8

/* ---------------- 全局状态 ---------------- */

static bool s_inited;
static portMUX_TYPE s_mux = portMUX_INITIALIZER_UNLOCKED;

/* 以下受 s_mux 保护 */
static bool s_have_mid; /* 当前连接内是否已接受过音乐事件（mid 比较基准有效） */
static uint16_t s_mid;  /* 当前曲目 mid */
static mc_stats_t s_stats;
static mc_evt_cb_t s_cb;
static void *s_cb_arg;
static uint32_t s_conn_gen; /* ws 握手完成次数快照（重连检测） */

static uint32_t s_ws_token;

static void stat_add(uint32_t *field, uint32_t value)
{
    portENTER_CRITICAL(&s_mux);
    *field += value;
    portEXIT_CRITICAL(&s_mux);
}

/* ---------------- 极简 JSON 顶层字段提取 ----------------
 *
 * 为什么不用 cJSON：下行回调在 `ws_client` 任务里（栈 6KB），cJSON 的 malloc 会在内部 RAM
 * 上产生不可控瞬时占用；契约里的下行音乐事件都是扁平对象，只需 4~5 个顶层字段。
 * 只支持扁平对象（嵌套对象/数组按值跳过，不解析）。与 voice_cloud 的扫描器同源。
 */

static const uint8_t *mc_skip_ws(const uint8_t *p, const uint8_t *end)
{
    while (p < end && (*p == ' ' || *p == '\t' || *p == '\n' || *p == '\r')) {
        p++;
    }
    return p;
}

/* 从 '"' 开始跳过字符串（含转义），返回结束引号之后的位置；未闭合返回 NULL */
static const uint8_t *mc_skip_string(const uint8_t *p, const uint8_t *end)
{
    if (p >= end || *p != '"') {
        return NULL;
    }
    p++;
    while (p < end) {
        if (*p == '\\') {
            p += 2;
            continue;
        }
        if (*p == '"') {
            return p + 1;
        }
        p++;
    }
    return NULL;
}

/* 跳过任意 JSON 值（字符串/数字/字面量/嵌套对象或数组），返回值之后的位置 */
static const uint8_t *mc_skip_value(const uint8_t *p, const uint8_t *end)
{
    if (p >= end) {
        return NULL;
    }
    if (*p == '"') {
        return mc_skip_string(p, end);
    }
    if (*p == '{' || *p == '[') {
        int depth = 0;
        while (p < end) {
            if (*p == '"') {
                const uint8_t *after = mc_skip_string(p, end);
                if (after == NULL) {
                    return NULL;
                }
                p = after;
                continue;
            }
            if (*p == '{' || *p == '[') {
                depth++;
            } else if (*p == '}' || *p == ']') {
                depth--;
                if (depth <= 0) {
                    return p + 1;
                }
            }
            p++;
        }
        return NULL;
    }
    while (p < end && *p != ',' && *p != '}' && *p != ']' && *p != ' ' && *p != '\t' &&
           *p != '\n' && *p != '\r') {
        p++;
    }
    return p;
}

/* 在顶层对象里找 key；找到时 val/val_end 界定原始值（字符串含引号） */
static bool mc_find_key(const uint8_t *buf, size_t len, const char *key, const uint8_t **val,
                        const uint8_t **val_end)
{
    const uint8_t *p = buf;
    const uint8_t *end = buf + len;
    const size_t key_len = strlen(key);

    p = mc_skip_ws(p, end);
    if (p >= end || *p != '{') {
        return false;
    }
    p++;

    for (;;) {
        p = mc_skip_ws(p, end);
        if (p >= end || *p == '}') {
            return false;
        }
        if (*p != '"') {
            return false;
        }
        const uint8_t *key_start = p + 1;
        const uint8_t *after_key = mc_skip_string(p, end);
        if (after_key == NULL) {
            return false;
        }
        const size_t this_len = (size_t)((after_key - 1) - key_start);
        const bool match = (this_len == key_len && memcmp(key_start, key, key_len) == 0);

        p = mc_skip_ws(after_key, end);
        if (p >= end || *p != ':') {
            return false;
        }
        p = mc_skip_ws(p + 1, end);
        const uint8_t *v = p;
        const uint8_t *v_end = mc_skip_value(p, end);
        if (v_end == NULL) {
            return false;
        }
        if (match) {
            *val = v;
            *val_end = v_end;
            return true;
        }

        p = mc_skip_ws(v_end, end);
        if (p >= end) {
            return false;
        }
        if (*p == ',') {
            p++;
            continue;
        }
        return false;
    }
}

static void utf8_put(char *out, size_t cap, size_t *n, uint32_t cp)
{
    uint8_t bytes[4];
    size_t count;

    if (cp <= 0x7F) {
        bytes[0] = (uint8_t)cp;
        count = 1;
    } else if (cp <= 0x7FF) {
        bytes[0] = (uint8_t)(0xC0 | (cp >> 6));
        bytes[1] = (uint8_t)(0x80 | (cp & 0x3F));
        count = 2;
    } else if (cp <= 0xFFFF) {
        bytes[0] = (uint8_t)(0xE0 | (cp >> 12));
        bytes[1] = (uint8_t)(0x80 | ((cp >> 6) & 0x3F));
        bytes[2] = (uint8_t)(0x80 | (cp & 0x3F));
        count = 3;
    } else {
        bytes[0] = (uint8_t)(0xF0 | (cp >> 18));
        bytes[1] = (uint8_t)(0x80 | ((cp >> 12) & 0x3F));
        bytes[2] = (uint8_t)(0x80 | ((cp >> 6) & 0x3F));
        bytes[3] = (uint8_t)(0x80 | (cp & 0x3F));
        count = 4;
    }
    if (*n + count > cap - 1) {
        return; /* 截断：不写半个多字节字符 */
    }
    memcpy(out + *n, bytes, count);
    *n += count;
}

/* 解析 JSON 字符串值（含转义）；out 始终 '\0' 结尾，*out_len 为解码后字节数 */
static bool mc_decode_str(const uint8_t *v, const uint8_t *v_end, char *out, size_t cap,
                          uint16_t *out_len)
{
    if (out == NULL || cap == 0 || v == NULL || v_end == NULL) {
        return false;
    }
    out[0] = '\0';
    if (out_len != NULL) {
        *out_len = 0;
    }
    if (v >= v_end || *v != '"' || v_end - v < 2) {
        return false;
    }

    const uint8_t *p = v + 1;
    const uint8_t *end = v_end - 1; /* 结束引号 */
    size_t n = 0;

    while (p < end) {
        uint32_t cp = 0;

        if (*p != '\\') {
            /* 原始 UTF-8 直接透传：服务端中文标题是 UTF-8（不是 \u 转义） */
            if (*p >= 0x80) {
                size_t need = (*p >= 0xF0) ? 4 : (*p >= 0xE0) ? 3 : (*p >= 0xC0) ? 2 : 1;
                if (p + need > end || n + need > cap - 1) {
                    break;
                }
                memcpy(out + n, p, need);
                n += need;
                p += need;
                continue;
            }
            cp = *p++;
        } else {
            p++;
            if (p >= end) {
                break;
            }
            switch (*p++) {
            case '"': cp = '"'; break;
            case '\\': cp = '\\'; break;
            case '/': cp = '/'; break;
            case 'b': cp = 0x08; break;
            case 'f': cp = 0x0C; break;
            case 'n': cp = '\n'; break;
            case 'r': cp = '\r'; break;
            case 't': cp = '\t'; break;
            case 'u': {
                uint32_t u = 0;
                if (p + 4 > end) {
                    p = end;
                    break;
                }
                bool ok = true;
                for (int i = 0; i < 4; i++) {
                    const uint8_t h = *p++;
                    u <<= 4;
                    if (h >= '0' && h <= '9') {
                        u |= (uint32_t)(h - '0');
                    } else if (h >= 'a' && h <= 'f') {
                        u |= (uint32_t)(h - 'a' + 10);
                    } else if (h >= 'A' && h <= 'F') {
                        u |= (uint32_t)(h - 'A' + 10);
                    } else {
                        ok = false;
                        break;
                    }
                }
                if (!ok) {
                    cp = 0xFFFD;
                    break;
                }
                /* 代理对（标题里的 emoji 等）：\uD83D\uDE00 */
                if (u >= 0xD800 && u <= 0xDBFF && p + 6 <= end && p[0] == '\\' && p[1] == 'u') {
                    uint32_t lo = 0;
                    bool lo_ok = true;
                    const uint8_t *q = p + 2;
                    for (int i = 0; i < 4; i++) {
                        const uint8_t h = q[i];
                        lo <<= 4;
                        if (h >= '0' && h <= '9') {
                            lo |= (uint32_t)(h - '0');
                        } else if (h >= 'a' && h <= 'f') {
                            lo |= (uint32_t)(h - 'a' + 10);
                        } else if (h >= 'A' && h <= 'F') {
                            lo |= (uint32_t)(h - 'A' + 10);
                        } else {
                            lo_ok = false;
                            break;
                        }
                    }
                    if (lo_ok && lo >= 0xDC00 && lo <= 0xDFFF) {
                        u = 0x10000 + ((u - 0xD800) << 10) + (lo - 0xDC00);
                        p += 6;
                    }
                }
                cp = u;
                break;
            }
            default:
                cp = (uint8_t) * (p - 1);
                break;
            }
        }
        utf8_put(out, cap, &n, cp);
    }

    out[n] = '\0';
    if (out_len != NULL) {
        *out_len = (uint16_t)n;
    }
    return true;
}

static bool mc_decode_int(const uint8_t *v, const uint8_t *v_end, int32_t *out)
{
    if (v == NULL || v_end == NULL || out == NULL) {
        return false;
    }
    const uint8_t *p = mc_skip_ws(v, v_end);
    bool neg = false;
    if (p < v_end && *p == '-') {
        neg = true;
        p++;
    }
    if (p >= v_end || *p < '0' || *p > '9') {
        return false;
    }
    int64_t value = 0;
    while (p < v_end && *p >= '0' && *p <= '9') {
        value = value * 10 + (*p - '0');
        if (value > 2147483647LL) {
            return false;
        }
        p++;
    }
    *out = (int32_t)(neg ? -value : value);
    return true;
}

/* ---------------- 状态名 / 动作名 ---------------- */

const char *mc_state_name(mc_state_t state)
{
    switch (state) {
    case MC_STATE_IDLE: return "idle";
    case MC_STATE_PLAYING: return "playing";
    case MC_STATE_PAUSED: return "paused";
    case MC_STATE_STOPPED: return "stopped";
    case MC_STATE_ERROR: return "error";
    default: return "unknown";
    }
}

const char *mc_action_name(uint8_t action)
{
    switch (action) {
    case MC_ACTION_PAUSE: return "pause";
    case MC_ACTION_RESUME: return "resume";
    case MC_ACTION_STOP: return "stop";
    default: return "?";
    }
}

/* ---------------- 断链清 mid 基准 ---------------- */

static void mc_on_net_down(void *arg, app_evt_t evt, const app_evt_data_t *data)
{
    (void)arg;
    (void)evt;
    (void)data;
    portENTER_CRITICAL(&s_mux);
    s_have_mid = false; /* 新连接内 mid 从头发；音乐本身可以继续播 */
    portEXIT_CRITICAL(&s_mux);
}

/*
 * WS 可能在没有 NET_DOWN 时重连（服务端重启/踢下线）：connect_count 在 hello_ack 后自增。
 * 每次处理音乐事件前对一次快照，变化就清基准——否则新连接的 mid=1 会被旧基准判成过期。
 */
static void mc_sync_generation(void)
{
    ws_status_t ws;
    if (ws_status_read(&ws) != ESP_OK) {
        return;
    }
    portENTER_CRITICAL(&s_mux);
    if (ws.connect_count != s_conn_gen) {
        s_conn_gen = ws.connect_count;
        s_have_mid = false;
    }
    portEXIT_CRITICAL(&s_mux);
}

/* ---------------- 回调派发（ws_client 上下文） ---------------- */

static void mc_dispatch(const mc_evt_t *evt)
{
    mc_evt_cb_t cb;
    void *arg;

    portENTER_CRITICAL(&s_mux);
    cb = s_cb;
    arg = s_cb_arg;
    portEXIT_CRITICAL(&s_mux);
    if (cb != NULL) {
        cb(arg, evt);
    }
}

/* ---------------- 下行事件处理 ---------------- */

static void handle_track(const uint8_t *payload, size_t len)
{
    const uint8_t *v = NULL;
    const uint8_t *v_end = NULL;
    int32_t mid_raw = -1;
    int32_t duration_ms = 0;
    char codec[MC_CODEC_MAX] = "mp3";
    bool stale = false;
    bool duplicate = false;
    mc_evt_t evt;

    memset(&evt, 0, sizeof(evt));
    evt.kind = MC_EVT_TRACK;

    if (!mc_find_key(payload, len, "mid", &v, &v_end) || !mc_decode_int(v, v_end, &mid_raw) ||
        mid_raw < 0 || mid_raw > 0xFFFF) {
        stat_add(&s_stats.rx_parse_errors, 1);
        ESP_LOGW(TAG, "music_track 缺 mid 或非法（%u B），丢弃", (unsigned)len);
        return;
    }
    if (!mc_find_key(payload, len, "url", &v, &v_end) ||
        !mc_decode_str(v, v_end, evt.url, sizeof(evt.url), NULL)) {
        stat_add(&s_stats.rx_parse_errors, 1);
        ESP_LOGW(TAG, "music_track 缺 url，丢弃（mid=%d）", (int)mid_raw);
        return;
    }
    if (mc_find_key(payload, len, "codec", &v, &v_end)) {
        (void)mc_decode_str(v, v_end, codec, sizeof(codec), NULL);
    }
    if (mc_find_key(payload, len, "duration_ms", &v, &v_end)) {
        (void)mc_decode_int(v, v_end, &duration_ms);
    }
    if (mc_find_key(payload, len, "title", &v, &v_end)) {
        (void)mc_decode_str(v, v_end, evt.title, sizeof(evt.title), NULL);
    }

    /* URL 只来自 music_track，端侧只校验前缀/长度（ARCH §6.3；禁止从文本里抠 URL）。
     * 解码时可能被截断：长度顶到上限就按"超长"拒绝，不拿半截 URL 去请求。 */
    if (evt.url[0] == '\0' ||
        (strncmp(evt.url, "http://", 7) != 0 && strncmp(evt.url, "https://", 8) != 0)) {
        stat_add(&s_stats.rx_dropped_bad, 1);
        ESP_LOGW(TAG, "music_track URL 非法（非 http(s)），丢弃（mid=%d）", (int)mid_raw);
        return;
    }
    if (strlen(evt.url) >= (size_t)(MC_URL_MAX - 1)) {
        stat_add(&s_stats.rx_dropped_bad, 1);
        ESP_LOGW(TAG, "music_track URL 超长（上限 %d B），丢弃（mid=%d）", MC_URL_MAX,
                 (int)mid_raw);
        return;
    }
    if (strcmp(codec, "mp3") != 0) {
        stat_add(&s_stats.rx_dropped_bad, 1);
        ESP_LOGW(TAG, "music_track codec=%s 非 mp3（端侧只实现 MP3），丢弃（mid=%d）", codec,
                 (int)mid_raw);
        return;
    }
    if (duration_ms < 0) {
        duration_ms = 0;
    }

    /* 断链后基准失效；mid 过滤：同 mid 视为重复（避免同一条曲目被重复起播），旧 mid 丢弃 */
    mc_sync_generation();
    portENTER_CRITICAL(&s_mux);
    if (s_have_mid) {
        const int16_t diff = (int16_t)((uint16_t)mid_raw - s_mid);
        stale = (diff < 0);
        duplicate = (diff == 0);
    }
    if (!stale && !duplicate) {
        s_mid = (uint16_t)mid_raw;
        s_have_mid = true;
    }
    portEXIT_CRITICAL(&s_mux);

    if (stale) {
        stat_add(&s_stats.rx_dropped_mid, 1);
        ESP_LOGD(TAG, "丢弃过期 music_track mid=%d（当前 %u）", (int)mid_raw,
                 (unsigned)mc_current_mid());
        return;
    }
    if (duplicate) {
        stat_add(&s_stats.rx_dropped_dup, 1);
        ESP_LOGW(TAG, "重复的 music_track mid=%d，忽略", (int)mid_raw);
        return;
    }

    evt.mid = (uint16_t)mid_raw;
    evt.duration_ms = duration_ms;
    stat_add(&s_stats.rx_track, 1);
    ESP_LOGI(TAG, "music_track mid=%d title=%s dur=%d url_len=%u", (int)mid_raw,
             evt.title[0] != '\0' ? evt.title : "-", (int)duration_ms,
             (unsigned)strlen(evt.url));
    ESP_LOGD(TAG, "music_track url=%s", evt.url);
    mc_dispatch(&evt);
}

static void handle_control(const uint8_t *payload, size_t len)
{
    const uint8_t *v = NULL;
    const uint8_t *v_end = NULL;
    int32_t mid_raw = -1;
    char action[MC_ACTION_MAX] = "";
    bool stale = false;
    mc_evt_t evt;

    memset(&evt, 0, sizeof(evt));
    evt.kind = MC_EVT_CONTROL;

    if (!mc_find_key(payload, len, "mid", &v, &v_end) || !mc_decode_int(v, v_end, &mid_raw) ||
        mid_raw < 0 || mid_raw > 0xFFFF) {
        stat_add(&s_stats.rx_parse_errors, 1);
        ESP_LOGW(TAG, "music_control 缺 mid 或非法（%u B），丢弃", (unsigned)len);
        return;
    }
    if (!mc_find_key(payload, len, "action", &v, &v_end) ||
        !mc_decode_str(v, v_end, action, sizeof(action), NULL)) {
        stat_add(&s_stats.rx_parse_errors, 1);
        ESP_LOGW(TAG, "music_control 缺 action，丢弃（mid=%d）", (int)mid_raw);
        return;
    }

    if (strcmp(action, "pause") == 0) {
        evt.action = MC_ACTION_PAUSE;
    } else if (strcmp(action, "resume") == 0) {
        evt.action = MC_ACTION_RESUME;
    } else if (strcmp(action, "stop") == 0) {
        evt.action = MC_ACTION_STOP;
    } else {
        stat_add(&s_stats.rx_parse_errors, 1);
        ESP_LOGW(TAG, "未知 music_control action=%s，丢弃（mid=%d）", action, (int)mid_raw);
        return;
    }

    /* 音乐控制允许 mid == 当前曲目（暂停/停止本来就指向正在播的这首），只丢弃更旧的 */
    mc_sync_generation();
    portENTER_CRITICAL(&s_mux);
    if (s_have_mid && (int16_t)((uint16_t)mid_raw - s_mid) < 0) {
        stale = true;
    }
    portEXIT_CRITICAL(&s_mux);
    if (stale) {
        stat_add(&s_stats.rx_dropped_mid, 1);
        ESP_LOGD(TAG, "丢弃过期 music_control mid=%d（当前 %u）", (int)mid_raw,
                 (unsigned)mc_current_mid());
        return;
    }

    evt.mid = (uint16_t)mid_raw;
    stat_add(&s_stats.rx_control, 1);
    ESP_LOGI(TAG, "music_control mid=%d action=%s", (int)mid_raw, mc_action_name(evt.action));
    mc_dispatch(&evt);
}

/* transport_ws 的附加订阅回调：在 `ws_client` 任务上下文，禁止阻塞 */
static void mc_ws_cb(const ws_frame_hdr_t *frame, const uint8_t *payload, size_t len, void *arg)
{
    const uint8_t *v = NULL;
    const uint8_t *v_end = NULL;
    char type[MC_EVT_TYPE_MAX];

    (void)arg;

    if (frame != NULL) {
        return; /* 下行二进制（TTS 音频 0x02）归 voice_cloud */
    }
    if (payload == NULL || len == 0) {
        return;
    }
    if (!mc_find_key(payload, len, "t", &v, &v_end) ||
        !mc_decode_str(v, v_end, type, sizeof(type), NULL)) {
        /* 不是合法扁平 JSON：语音事件也长这样但一定带 t，解析失败说明对端违约；
         * 这里只计数不告警（voice_cloud 会给出权威的 WARN）。 */
        stat_add(&s_stats.rx_parse_errors, 1);
        return;
    }
    if (strcmp(type, "music_track") == 0) {
        handle_track(payload, len);
        return;
    }
    if (strcmp(type, "music_control") == 0) {
        handle_control(payload, len);
        return;
    }
    /* 其它事件（语音 asr/llm/tts/error、pong、hello_ack）归 voice_cloud / transport_ws */
}

/* ---------------- 上行发送 ---------------- */

esp_err_t mc_report_status(mc_state_t state, uint32_t progress_ms)
{
    char json[MC_JSON_MAX];
    int written;
    esp_err_t err;
    uint16_t mid;

    if (!s_inited) {
        return ESP_ERR_INVALID_STATE;
    }
    if (state < MC_STATE_IDLE || state > MC_STATE_ERROR) {
        stat_add(&s_stats.status_failed, 1);
        return ESP_ERR_INVALID_ARG;
    }
    if (!ws_is_ready()) {
        /* 断网时不发（transport 的丢帧 WARN 会被周期上报刷出来），只计数 */
        stat_add(&s_stats.status_failed, 1);
        return ESP_ERR_INVALID_STATE;
    }
    mid = mc_current_mid();

    written = snprintf(json, sizeof(json),
                       "{\"t\":\"music_status\",\"state\":\"%s\",\"mid\":%u,\"progress_ms\":%u}",
                       mc_state_name(state), (unsigned)mid, (unsigned)progress_ms);
    if (written <= 0 || (size_t)written >= sizeof(json)) {
        stat_add(&s_stats.status_failed, 1);
        ESP_LOGW(TAG, "music_status 报文超长（%d B），未发送", written);
        return ESP_ERR_INVALID_SIZE;
    }

    err = ws_send_text(json);
    if (err != ESP_OK) {
        /* WS 未 READY / 发送队列满：只 DEBUG 打点（断网时周期上报会刷屏） */
        stat_add(&s_stats.status_failed, 1);
        ESP_LOGD(TAG, "music_status 未发出: %s", esp_err_to_name(err));
        return err;
    }
    stat_add(&s_stats.status_sent, 1);
    ESP_LOGD(TAG, "music_status → %s（mid=%u，%u ms）", mc_state_name(state), (unsigned)mid,
             (unsigned)progress_ms);
    return ESP_OK;
}

esp_err_t mc_send_request(uint8_t action, uint16_t id, int32_t index)
{
    char json[MC_JSON_MAX];
    int written;
    esp_err_t err;

    if (!s_inited) {
        return ESP_ERR_INVALID_STATE;
    }
    if (action != MC_REQUEST_ACTION_PLAY) {
        stat_add(&s_stats.request_failed, 1);
        ESP_LOGW(TAG, "music_request action=%u 未实现（只支持 play）", (unsigned)action);
        return ESP_ERR_INVALID_ARG;
    }
    if (!ws_is_ready()) {
        stat_add(&s_stats.request_failed, 1);
        return ESP_ERR_INVALID_STATE;
    }

    written = snprintf(json, sizeof(json),
                       "{\"t\":\"music_request\",\"action\":\"play\",\"id\":%u,\"index\":%d}",
                       (unsigned)id, (int)index);
    if (written <= 0 || (size_t)written >= sizeof(json)) {
        stat_add(&s_stats.request_failed, 1);
        ESP_LOGW(TAG, "music_request 报文超长（%d B），未发送", written);
        return ESP_ERR_INVALID_SIZE;
    }

    err = ws_send_text(json);
    if (err != ESP_OK) {
        stat_add(&s_stats.request_failed, 1);
        ESP_LOGD(TAG, "music_request 未发出: %s", esp_err_to_name(err));
        return err;
    }
    stat_add(&s_stats.request_sent, 1);
    ESP_LOGI(TAG, "music_request → play（id=%u，index=%d）", (unsigned)id, (int)index);
    return ESP_OK;
}

/* ---------------- 对外 API ---------------- */

esp_err_t mc_init(void)
{
    esp_err_t err;
    ws_status_t ws;

    if (s_inited) {
        return ESP_ERR_INVALID_STATE;
    }
    if (ws_status_read(&ws) == ESP_OK) {
        s_conn_gen = ws.connect_count;
    }
    /* 附加订阅槽（不影响 voice_cloud 的主回调，也不影响 diag 的 `ws rxlog`） */
    err = ws_subscribe(mc_ws_cb, NULL, &s_ws_token);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "订阅 WS 下行失败: %s（mc_init 必须在 ws_init 之后）", esp_err_to_name(err));
        return err;
    }
    err = app_evt_register(APP_EVT_NET_DOWN, mc_on_net_down, NULL);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "订阅 NET_DOWN 失败: %s", esp_err_to_name(err));
        (void)ws_unsubscribe(s_ws_token);
        return err;
    }
    s_inited = true;
    ESP_LOGI(TAG, "ready（纯协议：解析/mid 过滤/status/request；不建任务、不建定时器、不调播放器）");
    return ESP_OK;
}

esp_err_t mc_register_cb(mc_evt_cb_t cb, void *arg)
{
    if (!s_inited) {
        return ESP_ERR_INVALID_STATE;
    }
    portENTER_CRITICAL(&s_mux);
    s_cb = cb;
    s_cb_arg = arg;
    portEXIT_CRITICAL(&s_mux);
    return ESP_OK;
}

uint16_t mc_current_mid(void)
{
    uint16_t mid;

    portENTER_CRITICAL(&s_mux);
    mid = s_have_mid ? s_mid : 0;
    portEXIT_CRITICAL(&s_mux);
    return mid;
}

bool mc_have_mid(void)
{
    bool have;

    portENTER_CRITICAL(&s_mux);
    have = s_have_mid;
    portEXIT_CRITICAL(&s_mux);
    return have;
}

esp_err_t mc_stats_read(mc_stats_t *out)
{
    if (out == NULL) {
        return ESP_ERR_INVALID_ARG;
    }
    portENTER_CRITICAL(&s_mux);
    *out = s_stats;
    portEXIT_CRITICAL(&s_mux);
    return ESP_OK;
}

esp_err_t mc_stats_reset(void)
{
    portENTER_CRITICAL(&s_mux);
    memset(&s_stats, 0, sizeof(s_stats));
    portEXIT_CRITICAL(&s_mux);
    return ESP_OK;
}
