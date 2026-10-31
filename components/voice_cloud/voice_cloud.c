/*
 * voice_cloud —— 语音云协议（L3 能力层）。
 *
 * 一轮会话的端侧动作（ARCHITECTURE §3.6、§6、§8.1；PROTOCOL.md）：
 *
 *   vc_start(sid)
 *     └─ enc_open/enc_reset → audio_fe_uplink_start → ws "audio_start"
 *        └─ [vc 任务] audio_fe_uplink_read(320) → enc_encode() → ws_send_bin(0x01) ...
 *             ├─ VAD 连续静音 800ms  → enc_flush 尾帧 + ws "audio_end"
 *             ├─ 累计收音 8s        → 同上（兜底）
 *             ├─ vc_stop()          → 同上（调试/按键路径）
 *             └─ vc_cancel()/断链   → 直接丢弃，不发 audio_end
 *        └─ 等 asr_…/llm_delta/tts_start/tts_end/error（sid 过滤）→ event_bus + vc_register_cb
 *
 * 为什么编码不另建任务：内部 RAM 只剩十几 KB（CONVENTIONS §4），codec_enc 的调用约定
 * 就是"由调用方任务串行调用"，libopus 的临时数组已由伪栈放 PSRAM（T2.2），8KB 栈足够。
 *
 * 为什么用轮询 audio_fe_stats_read().vad_active 判静音：AFE 的 VAD 事件回调在音频任务
 * 上下文（main 注册），voice_cloud 不想跨模块接管回调；stats 是只读快照，50Hz 轮询成本
 * 可忽略，且能顺带拿到"是否出现过人声"，避免刚启动就误判静音收尾。
 */
#include "voice_cloud.h"

#include <stdio.h>
#include <string.h>

#include "audio_fe.h"
#include "codec_enc.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "event_bus.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "transport_ws.h"

static const char *TAG = "VC";

/* 一轮的 JSON 都很短（audio_start/audio_end），64B 足够 */
#define VC_JSON_MAX 64
/* ASR/LLM 文本缓冲：放在 ws_client 任务栈上（6KB），96B 足够常见短句 */
#define VC_TEXT_MAX 96
/* 下行事件里 sid 缺省时的哨兵 */
#define VC_SID_NONE (-1)

/* ---------------- 全局状态（跨任务） ---------------- */

static bool s_inited;
static TaskHandle_t s_task;
static vc_cb_t s_cb;
static void *s_cb_arg;

static volatile vc_state_t s_state = VC_STATE_IDLE;
static volatile uint16_t s_sid; /* 当前轮次 sid（IDLE 时是最近一轮） */
static volatile bool s_abort;         /* 立即放弃当前轮次（vc_cancel / 新轮次覆盖 / 断链） */
static volatile bool s_finish_req;    /* 请求优雅收尾（vc_stop） */
static volatile bool s_tts_end_seen;  /* 本轮已收到 tts_end（ws_client 任务写） */
static volatile bool s_pending_start; /* 有待处理的 vc_start */
static volatile uint16_t s_pending_sid;

/* 以下只由 vc 任务读写 */
static int64_t s_round_start_us;
static int64_t s_wait_deadline_us;
static uint32_t s_round_frames;
static uint32_t s_round_bytes;

/* sid 自增（vc_start_auto）：可能被多个调用方并发触发，用临界区保护 */
static uint16_t s_next_sid;
static portMUX_TYPE s_sid_mux = portMUX_INITIALIZER_UNLOCKED;

/* 统计：vc 任务与 ws_client 任务都会写，用临界区合并；读侧整块拷贝 */
static vc_stats_t s_stats;
static portMUX_TYPE s_stat_mux = portMUX_INITIALIZER_UNLOCKED;

static void stat_add(uint32_t *field, uint32_t value)
{
    portENTER_CRITICAL(&s_stat_mux);
    *field += value;
    portEXIT_CRITICAL(&s_stat_mux);
}

static void stat_set(uint32_t *field, uint32_t value)
{
    portENTER_CRITICAL(&s_stat_mux);
    *field = value;
    portEXIT_CRITICAL(&s_stat_mux);
}

/* ---------------- 极简 JSON 顶层字段提取 ----------------
 *
 * 为什么不用 cJSON：下行回调在 `ws_client` 任务里，走 malloc 会在内部 RAM 上产生不可控的
 * 瞬时占用（wss 握手期最低水位只剩 ~31KB），而我们只需要 4~6 个顶层字段；手写扫描器
 * 零分配、可预测。只支持契约里的扁平对象（数组/嵌套对象按值跳过，不解析）。
 */

static const uint8_t *vc_skip_ws(const uint8_t *p, const uint8_t *end)
{
    while (p < end && (*p == ' ' || *p == '\t' || *p == '\n' || *p == '\r')) {
        p++;
    }
    return p;
}

/* p 指向 '"'；返回结束引号之后的位置，非法返回 NULL */
static const uint8_t *vc_skip_string(const uint8_t *p, const uint8_t *end)
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

/* 返回值的下一个字符位置（逗号/右括号/空白处），非法返回 NULL */
static const uint8_t *vc_skip_value(const uint8_t *p, const uint8_t *end)
{
    p = vc_skip_ws(p, end);
    if (p >= end) {
        return NULL;
    }
    if (*p == '"') {
        return vc_skip_string(p, end);
    }
    if (*p == '{' || *p == '[') {
        int depth = 0;
        while (p < end) {
            if (*p == '"') {
                const uint8_t *after = vc_skip_string(p, end);
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
static bool vc_find_key(const uint8_t *buf, size_t len, const char *key, const uint8_t **val,
                        const uint8_t **val_end)
{
    const uint8_t *p = buf;
    const uint8_t *end = buf + len;
    const size_t key_len = strlen(key);

    p = vc_skip_ws(p, end);
    if (p >= end || *p != '{') {
        return false;
    }
    p++;

    for (;;) {
        p = vc_skip_ws(p, end);
        if (p >= end) {
            return false;
        }
        if (*p == '}') {
            return false;
        }
        if (*p != '"') {
            return false;
        }
        const uint8_t *key_start = p + 1;
        const uint8_t *after_key = vc_skip_string(p, end);
        if (after_key == NULL) {
            return false;
        }
        const size_t this_len = (size_t)((after_key - 1) - key_start);
        const bool match = (this_len == key_len && memcmp(key_start, key, key_len) == 0);

        p = vc_skip_ws(after_key, end);
        if (p >= end || *p != ':') {
            return false;
        }
        p = vc_skip_ws(p + 1, end);
        const uint8_t *v = p;
        const uint8_t *v_end = vc_skip_value(p, end);
        if (v_end == NULL) {
            return false;
        }
        if (match) {
            *val = v;
            *val_end = v_end;
            return true;
        }

        p = vc_skip_ws(v_end, end);
        if (p >= end) {
            return false;
        }
        if (*p == ',') {
            p++;
            continue;
        }
        return false; /* '}' 或非法 */
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
        return; /* 截断：不再写（不切多字节字符） */
    }
    memcpy(out + *n, bytes, count);
    *n += count;
}

/* 解析 JSON 字符串值（含转义）；out 始终 '\0' 结尾，*out_len 为解码后字节数 */
static bool vc_decode_str(const uint8_t *v, const uint8_t *v_end, char *out, size_t cap,
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
            /* 原始 UTF-8 直接透传：服务端 ASR 文本是 UTF-8 中文（不是 \u 转义），
             * 若按字节逐个当 codepoint 再编码会二次编码成乱码（T2.5 真机联调发现）。 */
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
                /* 代理对：\uD83D\uDE00 这类（ASR 文本通常不会，但不能解成乱码） */
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

static bool vc_decode_int(const uint8_t *v, const uint8_t *v_end, int32_t *out)
{
    if (v == NULL || v_end == NULL || out == NULL) {
        return false;
    }
    const uint8_t *p = vc_skip_ws(v, v_end);
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

/* ---------------- 下行事件分发 ---------------- */

static void vc_emit(vc_event_t evt, uint16_t sid, const char *text, uint16_t text_len,
                    uint8_t codec, uint32_t sample_rate, int32_t code)
{
    vc_cb_t cb = s_cb;

    if (cb == NULL) {
        return;
    }
    vc_event_msg_t msg = {
        .evt = evt,
        .sid = sid,
        .text = text,
        .text_len = text_len,
        .codec = codec,
        .sample_rate = sample_rate,
        .code = code,
    };
    cb(&msg, s_cb_arg);
}

static void vc_post_asr(app_evt_t app_evt, uint16_t sid, const char *text, uint16_t text_len)
{
    app_evt_data_t data;

    memset(&data, 0, sizeof(data));
    data.asr.sid = sid;
    data.asr.len = text_len > APP_EVT_TEXT_MAX ? APP_EVT_TEXT_MAX : text_len;
    memcpy(data.asr.text, text, data.asr.len);
    esp_err_t err = app_evt_post(app_evt, &data);
    if (err != ESP_OK) {
        ESP_LOGD(TAG, "投递 %s 失败: %s", app_evt_name(app_evt), esp_err_to_name(err));
    }
}

static void vc_post_tts_start(uint16_t sid, uint8_t codec, uint32_t sr)
{
    app_evt_data_t data;

    memset(&data, 0, sizeof(data));
    data.tts_start.sid = sid;
    data.tts_start.codec = codec;
    data.tts_start.sr = sr;
    esp_err_t err = app_evt_post(APP_EVT_TTS_START, &data);
    if (err != ESP_OK) {
        ESP_LOGD(TAG, "投递 TTS_START 失败: %s", esp_err_to_name(err));
    }
}

static void vc_post_tts_end(uint16_t sid, uint16_t frames)
{
    app_evt_data_t data;

    memset(&data, 0, sizeof(data));
    data.tts_end.sid = sid;
    data.tts_end.frames = frames;
    esp_err_t err = app_evt_post(APP_EVT_TTS_END, &data);
    if (err != ESP_OK) {
        ESP_LOGD(TAG, "投递 TTS_END 失败: %s", esp_err_to_name(err));
    }
}

/* 结束等待：把 vc 任务从 wait_response 里叫醒 */
static void vc_stop_waiting(void)
{
    s_state = VC_STATE_IDLE;
    if (s_task != NULL) {
        xTaskNotifyGive(s_task);
    }
}

/* 下行文本帧（在 `ws_client` 任务上下文） */
static void vc_handle_text(const uint8_t *payload, size_t len)
{
    const uint8_t *v = NULL;
    const uint8_t *v_end = NULL;
    char t[16];
    char text[VC_TEXT_MAX];
    uint16_t text_len = 0;
    int32_t sid_raw = VC_SID_NONE;
    bool has_sid;

    stat_add(&s_stats.rx_text, 1);

    if (!vc_find_key(payload, len, "t", &v, &v_end) ||
        !vc_decode_str(v, v_end, t, sizeof(t), NULL)) {
        stat_add(&s_stats.rx_parse_errors, 1);
        ESP_LOGW(TAG, "下行文本缺 t 字段或非法（%u B）", (unsigned)len);
        return;
    }

    has_sid = vc_find_key(payload, len, "sid", &v, &v_end) && vc_decode_int(v, v_end, &sid_raw);

    /* §6.3：所有语音下行事件都带 sid；本组件只处理本轮事件，迟到/错位一律丢弃 */
    const bool is_voice_evt = (strcmp(t, "asr_partial") == 0 || strcmp(t, "asr_final") == 0 ||
                               strcmp(t, "llm_delta") == 0 || strcmp(t, "tts_start") == 0 ||
                               strcmp(t, "tts_end") == 0 || strcmp(t, "error") == 0);
    if (!is_voice_evt) {
        ESP_LOGW(TAG, "未处理的下行事件 t=%s（T2.5 只处理语音事件）", t);
        return;
    }
    if (!has_sid || sid_raw < 0 || sid_raw > 0xFFFF || s_state == VC_STATE_IDLE ||
        (uint16_t)sid_raw != s_sid) {
        stat_add(&s_stats.rx_dropped_sid, 1);
        ESP_LOGD(TAG, "丢弃迟到/不匹配事件 t=%s sid=%d（当前 sid=%u state=%s）", t, (int)sid_raw,
                 (unsigned)s_sid, vc_state_name(s_state));
        return;
    }

    const uint16_t sid = (uint16_t)sid_raw;

    if (strcmp(t, "asr_partial") == 0 || strcmp(t, "asr_final") == 0) {
        const bool is_final = (t[4] == 'f');
        if (!vc_find_key(payload, len, "text", &v, &v_end) ||
            !vc_decode_str(v, v_end, text, sizeof(text), &text_len)) {
            stat_add(&s_stats.rx_parse_errors, 1);
            ESP_LOGW(TAG, "%s 缺 text 字段，丢弃", t);
            return;
        }
        ESP_LOGI(TAG, "ASR%s sid=%u: %s", is_final ? " final" : "", (unsigned)sid, text);
        vc_post_asr(is_final ? APP_EVT_ASR_FINAL : APP_EVT_ASR_PARTIAL, sid, text, text_len);
        vc_emit(is_final ? VC_EVT_ASR_FINAL : VC_EVT_ASR_PARTIAL, sid, text, text_len, 0, 0, 0);
        return;
    }

    if (strcmp(t, "llm_delta") == 0) {
        if (vc_find_key(payload, len, "text", &v, &v_end)) {
            (void)vc_decode_str(v, v_end, text, sizeof(text), &text_len);
        }
        ESP_LOGD(TAG, "LLM delta sid=%u: %s", (unsigned)sid, text);
        vc_emit(VC_EVT_LLM_DELTA, sid, text, text_len, 0, 0, 0);
        return;
    }

    if (strcmp(t, "tts_start") == 0) {
        char codec[8] = "mp3";
        int32_t sr_raw = 0;
        uint8_t codec_id = APP_EVT_CODEC_MP3;

        if (vc_find_key(payload, len, "codec", &v, &v_end)) {
            (void)vc_decode_str(v, v_end, codec, sizeof(codec), NULL);
        }
        if (vc_find_key(payload, len, "sr", &v, &v_end)) {
            (void)vc_decode_int(v, v_end, &sr_raw);
        }
        if (strcmp(codec, "mp3") != 0) {
            codec_id = APP_EVT_CODEC_UNKNOWN;
            ESP_LOGW(TAG, "tts_start codec=%s 非 mp3（端侧只实现 MP3），T2.4 将无法播放", codec);
        }
        ESP_LOGI(TAG, "TTS start sid=%u codec=%s sr=%d", (unsigned)sid, codec, (int)sr_raw);
        vc_post_tts_start(sid, codec_id, (uint32_t)sr_raw);
        vc_emit(VC_EVT_TTS_START, sid, NULL, 0, codec_id, (uint32_t)sr_raw, 0);
        return;
    }

    if (strcmp(t, "tts_end") == 0) {
        const uint32_t frames = s_stats.rx_audio_frames;
        ESP_LOGI(TAG, "TTS end sid=%u（本轮下行音频 %u 帧）", (unsigned)sid, (unsigned)frames);
        vc_post_tts_end(sid, (uint16_t)frames);
        vc_emit(VC_EVT_TTS_END, sid, NULL, 0, 0, 0, 0);
        s_tts_end_seen = true;
        vc_stop_waiting();
        return;
    }

    /* error */
    {
        int32_t code = 0;
        char msg[48] = "";

        if (vc_find_key(payload, len, "code", &v, &v_end)) {
            (void)vc_decode_int(v, v_end, &code);
        }
        if (vc_find_key(payload, len, "msg", &v, &v_end)) {
            (void)vc_decode_str(v, v_end, msg, sizeof(msg), NULL);
        }
        stat_add(&s_stats.rx_cloud_errors, 1);
        ESP_LOGE(TAG, "云端 error sid=%u code=%d msg=%s（本轮结束，同轮不重试）", (unsigned)sid,
                 (int)code, msg);
        vc_emit(VC_EVT_CLOUD_ERROR, sid, msg, (uint16_t)strlen(msg), 0, 0, code);
        vc_stop_waiting();
    }
}

/* 下行二进制帧（TTS 音频）：T2.5 只统计，解码播放由 T2.4 接管 */
static void vc_handle_audio(const ws_frame_hdr_t *frame, size_t len)
{
    (void)frame;

    /* 帧头里没有 sid（ARCH §6.2，P-2 定稿为"按 audio_start→tts_end 顺序归属"），
     * 只在"已发 audio_end、等响应"的窗口里记账；IDLE 时到达的是上一轮迟到帧，直接丢。 */
    if (s_state != VC_STATE_WAIT_RESPONSE) {
        return;
    }
    stat_add(&s_stats.rx_audio_frames, 1);
    const uint32_t count = s_stats.rx_audio_frames;
    if (count == 1 || (count % 25) == 0) {
        ESP_LOGI(TAG, "下行音频帧 #%u len=%u（T2.5 只统计，T2.4 接入播放）", (unsigned)count,
                 (unsigned)len);
    }
}

/* transport_ws 的入站回调：在 `ws_client` 任务上下文，禁止阻塞 */
static void vc_ws_cb(const ws_frame_hdr_t *frame, const uint8_t *payload, size_t len, void *arg)
{
    (void)arg;

    if (frame == NULL) {
        vc_handle_text(payload, len);
        return;
    }
    vc_handle_audio(frame, len);
}

/* ---------------- 一轮上行 ---------------- */

typedef enum {
    VC_FIN_VAD = 0,   /* VAD 静音够 800ms */
    VC_FIN_MAX,       /* 累计 8s 超时 */
    VC_FIN_REQ,       /* vc_stop() */
    VC_FIN_ABORT,     /* vc_cancel() / 新轮次覆盖 */
    VC_FIN_LINK_LOST, /* WS 掉线 */
} vc_finish_t;

static const char *vc_finish_name(vc_finish_t why)
{
    switch (why) {
    case VC_FIN_VAD: return "vad-silence";
    case VC_FIN_MAX: return "max-duration";
    case VC_FIN_REQ: return "stop-request";
    case VC_FIN_ABORT: return "cancel";
    case VC_FIN_LINK_LOST: return "link-lost";
    default: return "unknown";
    }
}

/* 发 JSON 文本（audio_start/audio_end）；失败只记统计，不阻塞 */
static bool vc_send_json(const char *json)
{
    esp_err_t err = ws_send_text(json);

    if (err != ESP_OK) {
        stat_add(&s_stats.up_send_errors, 1);
        ESP_LOGW(TAG, "发送文本失败: %s（ws 未 READY 或队列满）", esp_err_to_name(err));
        return false;
    }
    return true;
}

/* 收尾：补尾帧 + audio_end + 释放上行资源；返回是否成功把 audio_end 发出去 */
static bool vc_end_uplink(vc_finish_t why)
{
    uint8_t tail[CODEC_ENC_MAX_PACKET];
    size_t tail_len = 0;
    char json[VC_JSON_MAX];
    bool sent_end;

    if (enc_flush(tail, sizeof(tail), &tail_len) == ESP_OK && tail_len > 0) {
        if (ws_send_bin(WS_FRAME_TYPE_AUDIO_UP, tail, tail_len) == ESP_OK) {
            s_round_frames++;
            s_round_bytes += (uint32_t)tail_len;
            stat_add(&s_stats.up_frames, 1);
            stat_add(&s_stats.up_bytes, (uint32_t)tail_len);
        } else {
            stat_add(&s_stats.up_send_errors, 1);
        }
    }
    enc_close();

    snprintf(json, sizeof(json), "{\"t\":\"audio_end\",\"sid\":%u}", (unsigned)s_sid);
    sent_end = vc_send_json(json);

    audio_fe_uplink_stop();

    const uint32_t round_ms = (uint32_t)((esp_timer_get_time() - s_round_start_us) / 1000);
    portENTER_CRITICAL(&s_stat_mux);
    s_stats.last_round_ms = round_ms;
    s_stats.last_sid = s_sid;
    s_stats.last_round_vad_end = (why == VC_FIN_VAD);
    portEXIT_CRITICAL(&s_stat_mux);
    ESP_LOGI(TAG, "audio_end sid=%u 原因=%s 上行 %u 帧/%u B 用时 %u ms%s", (unsigned)s_sid,
             vc_finish_name(why), (unsigned)s_round_frames, (unsigned)s_round_bytes,
             (unsigned)round_ms, sent_end ? "" : "（audio_end 未发出）");

    s_tts_end_seen = false;
    if (sent_end) {
        s_state = VC_STATE_WAIT_RESPONSE;
        s_wait_deadline_us = esp_timer_get_time() + (int64_t)CONFIG_VC_RESPONSE_TIMEOUT_MS * 1000;
    }
    return sent_end;
}

static void vc_begin_round(uint16_t sid)
{
    char json[VC_JSON_MAX];
    esp_err_t err;

    s_abort = false;
    s_finish_req = false;
    s_tts_end_seen = false;
    s_sid = sid;
    s_round_frames = 0;
    s_round_bytes = 0;
    stat_set(&s_stats.first_frame_us, 0);
    s_state = VC_STATE_IDLE;

    if (!ws_is_ready()) {
        ESP_LOGW(TAG, "云端未就绪，跳过本轮（sid=%u）", (unsigned)sid);
        return;
    }
    if (audio_fe_uplink_active()) {
        ESP_LOGW(TAG, "上行接口已被其它使用方占用（diag `afe uplink on`？），本轮跳过");
        return;
    }

    err = enc_open();
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "打开编码器失败: %s", esp_err_to_name(err));
        return;
    }
    (void)enc_reset();

    err = audio_fe_uplink_start();
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "启动上行失败: %s", esp_err_to_name(err));
        enc_close();
        return;
    }

    snprintf(json, sizeof(json), "{\"t\":\"audio_start\",\"sid\":%u}", (unsigned)sid);
    if (!vc_send_json(json)) {
        audio_fe_uplink_stop();
        enc_close();
        return;
    }

    s_round_start_us = esp_timer_get_time();
    s_state = VC_STATE_UPLINK;
    stat_add(&s_stats.rounds_started, 1);
    ESP_LOGI(TAG, "round start sid=%u（VAD 静音 %d ms / 上限 %d ms 收尾）", (unsigned)sid,
             CONFIG_VC_VAD_SILENCE_MS, CONFIG_VC_MAX_UTTER_MS);
}

static vc_finish_t vc_uplink_loop(void)
{
    int16_t pcm[CODEC_ENC_FRAME_SAMPLES];
    uint8_t out[CODEC_ENC_MAX_PACKET];
    bool speech_seen = false;
    bool first_frame_logged = false;
    int64_t last_speech_us = 0;
    const int64_t silence_us = (int64_t)CONFIG_VC_VAD_SILENCE_MS * 1000;
    const int64_t max_utter_us = (int64_t)CONFIG_VC_MAX_UTTER_MS * 1000;

    for (;;) {
        if (s_abort || s_pending_start) {
            return VC_FIN_ABORT;
        }
        if (s_finish_req) {
            return VC_FIN_REQ;
        }
        if (!ws_is_ready()) {
            return VC_FIN_LINK_LOST;
        }

        const int64_t now = esp_timer_get_time();
        audio_fe_stats_t fe;
        if (audio_fe_stats_read(&fe) == ESP_OK && fe.vad_active) {
            speech_seen = true;
            last_speech_us = now;
        }
        if (speech_seen && now - last_speech_us >= silence_us) {
            return VC_FIN_VAD;
        }
        if (now - s_round_start_us >= max_utter_us) {
            return VC_FIN_MAX;
        }

        const int got = audio_fe_uplink_read(pcm, CODEC_ENC_FRAME_SAMPLES, CONFIG_VC_READ_TIMEOUT_MS);
        if (got < 0) {
            stat_add(&s_stats.up_encode_errors, 1); /* 读到错误也计入本组错误数，便于观测 */
            vTaskDelay(pdMS_TO_TICKS(5));
            continue;
        }
        if (got == 0) {
            stat_add(&s_stats.up_read_timeouts, 1);
            continue;
        }

        size_t out_len = 0;
        esp_err_t err = enc_encode(pcm, (size_t)got, out, sizeof(out), &out_len);
        if (err != ESP_OK) {
            stat_add(&s_stats.up_encode_errors, 1);
            ESP_LOGW(TAG, "编码失败: %s", esp_err_to_name(err));
            continue;
        }
        if (out_len == 0) {
            continue;
        }

        err = ws_send_bin(WS_FRAME_TYPE_AUDIO_UP, out, out_len);
        if (err != ESP_OK) {
            stat_add(&s_stats.up_send_errors, 1);
            if (!ws_is_ready()) {
                return VC_FIN_LINK_LOST;
            }
            continue;
        }

        s_round_frames++;
        s_round_bytes += (uint32_t)out_len;
        stat_add(&s_stats.up_frames, 1);
        stat_add(&s_stats.up_bytes, (uint32_t)out_len);

        if (!first_frame_logged) {
            first_frame_logged = true;
            const uint32_t first_us = (uint32_t)(esp_timer_get_time() - s_round_start_us);
            stat_set(&s_stats.first_frame_us, first_us);
            ESP_LOGI(TAG, "上行首帧 %u us（目标 <200000 us）", (unsigned)first_us);
        }
    }
}

static void vc_abort_round(vc_finish_t why)
{
    audio_fe_uplink_stop();
    enc_close();
    s_state = VC_STATE_IDLE;
    stat_add(&s_stats.rounds_aborted, 1);
    ESP_LOGW(TAG, "round aborted sid=%u（%s，未发 audio_end）", (unsigned)s_sid,
             vc_finish_name(why));
}

static void vc_wait_response(void)
{
    bool got_end = false;

    while (s_state == VC_STATE_WAIT_RESPONSE) {
        if (s_abort || s_pending_start) { /* 新唤醒/取消：立即结束本轮等待 */
            s_state = VC_STATE_IDLE;
            break;
        }
        const int64_t remain_us = s_wait_deadline_us - esp_timer_get_time();
        if (remain_us <= 0) {
            s_state = VC_STATE_IDLE;
            break;
        }
        uint32_t wait_ms = (uint32_t)(remain_us / 1000);
        if (wait_ms > 200) {
            wait_ms = 200;
        }
        if (wait_ms == 0) {
            wait_ms = 1;
        }
        (void)ulTaskNotifyTake(pdTRUE, pdMS_TO_TICKS(wait_ms));
    }

    got_end = s_tts_end_seen;
    if (got_end) {
        stat_add(&s_stats.rounds_done, 1);
        ESP_LOGI(TAG, "round done sid=%u", (unsigned)s_sid);
        return;
    }
    if (!s_abort) {
        stat_add(&s_stats.rounds_aborted, 1);
        ESP_LOGW(TAG, "等待云端响应超时（%d ms），sid=%u 结束", CONFIG_VC_RESPONSE_TIMEOUT_MS,
                 (unsigned)s_sid);
    }
}

static void vc_task(void *arg)
{
    (void)arg;

    for (;;) {
        if (!s_pending_start) {
            (void)ulTaskNotifyTake(pdTRUE, portMAX_DELAY);
            continue;
        }

        portENTER_CRITICAL(&s_sid_mux);
        const uint16_t sid = s_pending_sid;
        s_pending_start = false;
        portEXIT_CRITICAL(&s_sid_mux);

        vc_begin_round(sid);
        if (s_state != VC_STATE_UPLINK) {
            continue; /* 未 READY / 编码器失败 / 上行占用：已打日志 */
        }

        const vc_finish_t why = vc_uplink_loop();
        const bool aborted = (why == VC_FIN_ABORT || why == VC_FIN_LINK_LOST);
        if (aborted) {
            vc_abort_round(why);
            continue; /* 新轮次（若有）由外层 pending_start 处理 */
        }
        if (!vc_end_uplink(why)) {
            vc_abort_round(why); /* audio_end 都没发出去，不再等响应 */
            continue;
        }
        vc_wait_response();
    }
}

/* ---------------- 对外 API ---------------- */

esp_err_t vc_init(void)
{
    if (s_inited) {
        return ESP_ERR_INVALID_STATE;
    }

    esp_err_t err = ws_register_cb(vc_ws_cb, NULL);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "注册 WS 接收回调失败: %s（vc_init 必须在 ws_init 之后）",
                 esp_err_to_name(err));
        return err;
    }

    BaseType_t ok = xTaskCreatePinnedToCore(vc_task, "vc", CONFIG_VC_TASK_STACK, NULL,
                                            CONFIG_VC_TASK_PRIORITY, &s_task, CONFIG_VC_TASK_CORE);
    if (ok != pdPASS) {
        ESP_LOGE(TAG, "创建 vc 任务失败（栈 %d B）", CONFIG_VC_TASK_STACK);
        return ESP_ERR_NO_MEM;
    }

    s_state = VC_STATE_IDLE;
    s_inited = true;
    ESP_LOGI(TAG, "ready（task %d B / core %d / prio %d）", CONFIG_VC_TASK_STACK,
             CONFIG_VC_TASK_CORE, CONFIG_VC_TASK_PRIORITY);
    return ESP_OK;
}

esp_err_t vc_start(uint16_t sid)
{
    if (!s_inited) {
        return ESP_ERR_INVALID_STATE;
    }
    if (!ws_is_ready()) {
        ESP_LOGW(TAG, "云端未 READY，跳过本轮（本地唤醒/播放不受影响）");
        return ESP_ERR_INVALID_STATE;
    }

    /* 只置 pending、不置 abort：新轮次是否作废旧轮次由 vc 任务在循环里判断
     * （begin_round 会清 abort，先置 abort 再置 pending 存在"被清掉"的竞态）。 */
    portENTER_CRITICAL(&s_sid_mux);
    s_pending_sid = sid;
    s_pending_start = true;
    portEXIT_CRITICAL(&s_sid_mux);

    if (s_task != NULL) {
        xTaskNotifyGive(s_task);
    }
    return ESP_OK;
}

esp_err_t vc_start_auto(void)
{
    uint16_t sid;

    portENTER_CRITICAL(&s_sid_mux);
    s_next_sid++;
    if (s_next_sid == 0) {
        s_next_sid = 1; /* 0 保留给"未分配" */
    }
    sid = s_next_sid;
    portEXIT_CRITICAL(&s_sid_mux);

    return vc_start(sid);
}

esp_err_t vc_stop(void)
{
    if (!s_inited) {
        return ESP_ERR_INVALID_STATE;
    }
    if (s_state == VC_STATE_UPLINK) {
        s_finish_req = true;
        if (s_task != NULL) {
            xTaskNotifyGive(s_task);
        }
    }
    return ESP_OK;
}

esp_err_t vc_cancel(void)
{
    if (!s_inited) {
        return ESP_ERR_INVALID_STATE;
    }
    s_abort = true;
    if (s_state == VC_STATE_WAIT_RESPONSE) {
        vc_stop_waiting();
    }
    if (s_task != NULL) {
        xTaskNotifyGive(s_task);
    }
    return ESP_OK;
}

bool vc_is_busy(void)
{
    return s_state != VC_STATE_IDLE;
}

vc_state_t vc_get_state(void)
{
    return s_state;
}

uint16_t vc_current_sid(void)
{
    return s_sid;
}

const char *vc_state_name(vc_state_t state)
{
    switch (state) {
    case VC_STATE_IDLE: return "IDLE";
    case VC_STATE_UPLINK: return "UPLINK";
    case VC_STATE_WAIT_RESPONSE: return "WAIT_RESPONSE";
    default: return "UNKNOWN";
    }
}

esp_err_t vc_register_cb(vc_cb_t cb, void *arg)
{
    s_cb = cb;
    s_cb_arg = arg;
    return ESP_OK;
}

esp_err_t vc_stats_read(vc_stats_t *out)
{
    if (out == NULL) {
        return ESP_ERR_INVALID_ARG;
    }
    portENTER_CRITICAL(&s_stat_mux);
    *out = s_stats;
    portEXIT_CRITICAL(&s_stat_mux);
    return ESP_OK;
}

esp_err_t vc_stats_reset(void)
{
    uint16_t last_sid;
    uint32_t last_round_ms;

    portENTER_CRITICAL(&s_stat_mux);
    last_sid = s_stats.last_sid;
    last_round_ms = s_stats.last_round_ms;
    memset(&s_stats, 0, sizeof(s_stats));
    s_stats.last_sid = last_sid;
    s_stats.last_round_ms = last_round_ms;
    portEXIT_CRITICAL(&s_stat_mux);
    return ESP_OK;
}
