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
 * 下行 TTS 音频（T2.4）：`tts_start` 里 audio_player_stream_open(1, sr) 成功即暂停收音（R1），
 * `tts_end` 前的 0x02 帧全部 audio_player_stream_write()；`tts_end` → stream_end 排空收尾；
 * vc_cancel/云端 error/断链/新轮次/响应超时 → stream_abort 立即停声。收音恢复（R2）不在
 * vc 任务里做，而是由 audio_player 的完成回调（附加订阅槽，src=TTS_STREAM）恢复——成功、
 * 失败、打断都会回调，且回调发生在 player 任务上下文（只做一次原子标志赋值，不阻塞）。
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
#include "audio_player.h"
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
static volatile bool s_cloud_error;   /* 本轮收到云端 error（T3.1：ROUND_END 原因用） */
static volatile bool s_pending_start; /* 有待处理的 vc_start */
static volatile uint16_t s_pending_sid;

/* 下行 TTS 音频（T2.4）：以下标志跨 ws_client / vc / player 任务 */
static bool s_done_subscribed;      /* audio_player 完成回调订阅成功（R2 依赖） */
static volatile bool s_audio_attempted; /* 本轮 tts_start 尝试过开流（用于 tts_end 日志） */
static volatile bool s_tts_fail_reported; /* 本轮已上报"设备侧 TTS 无法播放"（T3.6，每轮一次） */
static int64_t s_audio_drop_warn_us;    /* 窗口外/写失败丢弃日志限频（1/秒） */

/* 以下只由 vc 任务读写 */
static int64_t s_round_start_us;
static int64_t s_wait_deadline_us;
static uint32_t s_round_frames;
static uint32_t s_round_bytes;
static uint32_t s_round_audio_frames; /* 本轮成功交给播放器的 0x02 帧数（T3.1 修 T2.6 遗留⑱） */

/* sid 自增（vc_start_auto）：可能被多个调用方并发触发，用临界区保护 */
static uint16_t s_next_sid;
static portMUX_TYPE s_sid_mux = portMUX_INITIALIZER_UNLOCKED;

/* T3.7 续听预录：voice_session（evt/esp_timer 任务）起停，vc 任务消费；用临界区保护 */
static bool s_preroll;
static portMUX_TYPE s_preroll_mux = portMUX_INITIALIZER_UNLOCKED;

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

/*
 * T3.1：刷新"等响应/断流"超时 deadline。
 * 收到有效下行数据（tts_start / 0x02 帧）时调用 —— 长 TTS（>10s）与播放中的短暂抖动
 * 不应被误判超时；真正的超时口径 = 连续 CONFIG_VC_RESPONSE_TIMEOUT_MS 没有任何数据。
 * 写入方在 `ws_client` 任务、读取方在 `vc` 任务：int64 在 32 位核上非原子，必须走临界区。
 */
static void vc_refresh_deadline(void)
{
    portENTER_CRITICAL(&s_stat_mux);
    s_wait_deadline_us = esp_timer_get_time() + (int64_t)CONFIG_VC_RESPONSE_TIMEOUT_MS * 1000;
    portEXIT_CRITICAL(&s_stat_mux);
}

static int64_t vc_wait_deadline(void)
{
    portENTER_CRITICAL(&s_stat_mux);
    const int64_t deadline = s_wait_deadline_us;
    portEXIT_CRITICAL(&s_stat_mux);
    return deadline;
}

/* ---------------- 下行 TTS 音频（T2.4，0x02 → audio_player 流式播放） ---------------- */

/* 丢弃 1 帧 0x02：计数 + 限频日志（窗口外/收尾后/播放器不可用都会走到这里） */
static void vc_audio_drop(size_t len, const char *why)
{
    stat_add(&s_stats.rx_audio_dropped, 1);
    stat_add(&s_stats.rx_audio_dropped_bytes, (uint32_t)len);

    const int64_t now = esp_timer_get_time();
    if (now - s_audio_drop_warn_us >= 1000000) {
        s_audio_drop_warn_us = now;
        ESP_LOGW(TAG, "丢弃下行音频帧（%s，%u B；累计丢弃 %u 帧 / %u B）", why, (unsigned)len,
                 (unsigned)s_stats.rx_audio_dropped, (unsigned)s_stats.rx_audio_dropped_bytes);
    }
}

/*
 * 打断流式播放：立刻关 TX（无淡出），未播数据丢弃；player 之后回调 ok=false → R2 恢复收音。
 * 未开流时 audio_player_stream_abort() 返回 ESP_ERR_INVALID_STATE，按无操作处理（不刷屏）。
 * 可在任意任务调用（vc_cancel 在 console/main 上下文，error/tts_end 在 ws_client 上下文）。
 */
static void vc_audio_abort(const char *why)
{
    if (!audio_player_stream_active()) {
        return; /* 没开流/已收尾：abort 无事可做，这不是错误 */
    }
    esp_err_t err = audio_player_stream_abort();
    if (err == ESP_OK) {
        ESP_LOGI(TAG, "stream abort（%s）→ 立即停声", why);
    } else if (err != ESP_ERR_INVALID_STATE) {
        ESP_LOGW(TAG, "stream abort 失败（%s）: %s", why, esp_err_to_name(err));
    }
}

/* audio_player 完成回调（player 任务上下文）：流式播放结束（成功/失败/打断）→ R2 恢复收音 */
static void vc_stream_done_cb(audio_player_src_t src, bool ok, void *arg)
{
    (void)arg;

    if (src != AUDIO_PLAYER_SRC_TTS_STREAM) {
        return;
    }
    if (audio_player_stream_active()) {
        /* 极端竞态：上一路回调前新流已经打开并自己暂停了 feed，这里不能恢复 */
        ESP_LOGD(TAG, "stream done 但新流已开始，跳过 R2");
        return;
    }
    audio_fe_feed_release(AUDIO_FE_HOLD_TTS);
    ESP_LOGI(TAG, "stream done → feed resumed（ok=%d）", (int)ok);
}

/* tts_start：开流（codec=1/mp3）；成功即暂停收音（R1），失败则本轮音频只计数丢弃 */
static void vc_report_tts_fail(vc_tts_fail_t why)
{
    if (s_tts_fail_reported) {
        return; /* 每轮只报一次（多路 tts_start/重复事件不重复提示） */
    }
    s_tts_fail_reported = true;
    ESP_LOGW(TAG, "设备侧 TTS 无法播放（原因=%d），上报 voice_session 做错误反馈", (int)why);
    vc_emit(VC_EVT_TTS_FAIL, s_sid, NULL, 0, 0, 0, (int32_t)why);
}

static void vc_audio_start(uint8_t codec_id, uint32_t sample_rate)
{
    s_audio_attempted = true;

    if (codec_id != APP_EVT_CODEC_MP3) {
        ESP_LOGW(TAG, "tts_start 非 MP3 音频，本轮不播放（只计数丢弃）");
        vc_report_tts_fail(VC_TTS_FAIL_NOT_MP3);
        return;
    }
    if (sample_rate == 0) {
        ESP_LOGW(TAG, "tts_start 缺采样率，本轮不播放（只计数丢弃）");
        vc_report_tts_fail(VC_TTS_FAIL_NO_SAMPLE_RATE);
        return;
    }
    if (audio_player_stream_active()) {
        /* 上一路流还在收尾/被打断中：open 会失败，先请求 abort 再试一次 */
        ESP_LOGW(TAG, "上一路 TTS 流未收尾，先 abort 再开流");
        (void)audio_player_stream_abort();
    }
    if (!audio_player_mp3_ready()) {
        ESP_LOGW(TAG, "MP3 能力不可用，本轮 TTS 音频不播放（只计数丢弃）");
        vc_report_tts_fail(VC_TTS_FAIL_MP3_UNAVAILABLE);
        return;
    }
    if (audio_player_is_busy()) {
        ESP_LOGW(TAG, "播放器忙（提示音/本地播放），本轮 TTS 音频不播放（只计数丢弃）");
        vc_report_tts_fail(VC_TTS_FAIL_PLAYER_BUSY);
        return;
    }

    esp_err_t err = audio_player_stream_open(1, sample_rate);
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "流式播放 open 失败（%s），本轮 TTS 音频不播放（只计数丢弃）",
                 esp_err_to_name(err));
        vc_report_tts_fail(VC_TTS_FAIL_OPEN_FAILED);
        return;
    }

    /* R1：流式播放期间暂停收音，避免扬声器回采自激（R2 由完成回调恢复）。
     * T3.2 起走 audio_fe 的原因位仲裁（与提示音的 PROMPT 位互不覆盖）。 */
    if (s_done_subscribed) {
        audio_fe_feed_hold(AUDIO_FE_HOLD_TTS);
    } else {
        ESP_LOGE(TAG, "未订阅播放完成回调，跳过 R1（否则流结束后无法恢复收音）");
    }
    ESP_LOGI(TAG, "stream open codec=mp3 sr=%u，feed paused (R1)", (unsigned)sample_rate);
}

/* tts_end：排空已收数据后收尾（100ms fade + 静音冲刷 + 300ms 尾部延时后回调 ok=true） */
static void vc_audio_end(void)
{
    if (!audio_player_stream_active()) {
        if (s_audio_attempted) {
            ESP_LOGW(TAG, "tts_end 但本轮没有打开的流（开流失败/无可播数据）");
        }
        return;
    }
    esp_err_t err = audio_player_stream_end();
    if (err != ESP_OK && err != ESP_ERR_INVALID_STATE) {
        ESP_LOGW(TAG, "stream end 失败: %s", esp_err_to_name(err));
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
            ESP_LOGW(TAG, "tts_start codec=%s 非 mp3（端侧只实现 MP3），本轮音频将无法播放", codec);
        }
        ESP_LOGI(TAG, "TTS start sid=%u codec=%s sr=%d", (unsigned)sid, codec, (int)sr_raw);
        /* T3.1：一旦 TTS 开始下发，超时口径从"等响应"切到"数据断流" */
        if (s_state == VC_STATE_WAIT_RESPONSE) {
            vc_refresh_deadline();
        }
        vc_audio_start(codec_id, (uint32_t)sr_raw);
        vc_post_tts_start(sid, codec_id, (uint32_t)sr_raw);
        vc_emit(VC_EVT_TTS_START, sid, NULL, 0, codec_id, (uint32_t)sr_raw, 0);
        return;
    }

    if (strcmp(t, "tts_end") == 0) {
        /* 先请求排空收尾再记账：保证脚本看到的日志顺序是 tts_end → stream end */
        vc_audio_end();
        const uint32_t frames = s_round_audio_frames;
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
        s_cloud_error = true;
        ESP_LOGE(TAG, "云端 error sid=%u code=%d msg=%s（本轮结束，同轮不重试）", (unsigned)sid,
                 (int)code, msg);
        vc_audio_abort("cloud error");
        vc_emit(VC_EVT_CLOUD_ERROR, sid, msg, (uint16_t)strlen(msg), 0, 0, code);
        vc_stop_waiting();
    }
}

/* 下行二进制帧（TTS 音频 0x02）：窗口内 write 给播放器，窗口外/播放器不可用丢弃并计数 */
static void vc_handle_audio(const ws_frame_hdr_t *frame, const uint8_t *payload, size_t len)
{
    (void)frame;

    stat_add(&s_stats.rx_audio_bytes, (uint32_t)len);

    /* 帧头里没有 sid（ARCH §6.2，P-2 定稿为"按 audio_start→tts_end 顺序归属"）：
     * 合法窗口 = 已发 audio_end、还没收到 tts_end（tts_end 之后到达的都是迟到帧）。 */
    if (s_state != VC_STATE_WAIT_RESPONSE || s_tts_end_seen) {
        vc_audio_drop(len, "窗口外");
        return;
    }
    /* 窗口内但播放器还没开流（tts_start 未到/开流失败/播放器忙）：本帧无法播放 */
    if (!audio_player_stream_active()) {
        vc_audio_drop(len, "播放器未开流");
        return;
    }
    if (payload == NULL || len == 0 || len > 2048) {
        /* 契约 §1.2.2：单次 write ≤2KB；超限/空帧属于对端违约，按丢弃处理 */
        vc_audio_drop(len, "帧长非法");
        return;
    }

    esp_err_t err = audio_player_stream_write(payload, len);
    if (err != ESP_OK) {
        vc_audio_drop(len, err == ESP_ERR_NO_MEM ? "ring 满" : esp_err_to_name(err));
        return;
    }

    stat_add(&s_stats.rx_audio_frames, 1);
    s_round_audio_frames++;
    /* T3.1：收到有效数据就刷新"响应/数据超时"（见 vc_refresh_deadline） */
    if (s_state == VC_STATE_WAIT_RESPONSE) {
        vc_refresh_deadline();
    }
    const uint32_t count = s_round_audio_frames;
    if (count == 1 || (count % 25) == 0) {
        ESP_LOGI(TAG, "下行音频帧 #%u len=%u", (unsigned)count, (unsigned)len);
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
    vc_handle_audio(frame, payload, len);
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

/*
 * T3.1：把"一轮的生命周期节点"通知 voice_session（会话状态机）。
 * 回调在 vc/ws_client 任务上下文执行，只允许置标志/非阻塞调用。
 */
static void vc_emit_uplink_end(vc_finish_t why)
{
    vc_cb_t cb = s_cb;

    if (cb == NULL) {
        return;
    }
    vc_event_msg_t msg = {
        .evt = VC_EVT_UPLINK_END,
        .sid = s_sid,
        .code = (int32_t)why,
    };
    cb(&msg, s_cb_arg);
}

static void vc_emit_round_end(vc_round_end_t result)
{
    vc_cb_t cb = s_cb;

    if (cb == NULL) {
        return;
    }
    vc_event_msg_t msg = {
        .evt = VC_EVT_ROUND_END,
        .sid = s_sid,
        .round_end = result,
    };
    cb(&msg, s_cb_arg);
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
        vc_refresh_deadline();
        vc_emit_uplink_end(why); /* T3.1：LISTENING → THINKING 的触发点 */
    }
    return sent_end;
}

/* 取走预录标记（vc 任务侧用）：true 表示本轮可以接管已写入的环形缓冲 */
static bool vc_preroll_take(void)
{
    bool was;
    portENTER_CRITICAL(&s_preroll_mux);
    was = s_preroll;
    s_preroll = false;
    portEXIT_CRITICAL(&s_preroll_mux);
    return was;
}

esp_err_t vc_preroll_start(void)
{
    if (!s_inited) {
        return ESP_ERR_INVALID_STATE;
    }
    if (!ws_is_ready()) {
        return ESP_ERR_INVALID_STATE;
    }
    if (audio_fe_uplink_active()) {
        return ESP_ERR_INVALID_STATE; /* diag 探针占用 / 上一轮还没停 */
    }
    esp_err_t err = audio_fe_uplink_start();
    if (err != ESP_OK) {
        return err;
    }
    portENTER_CRITICAL(&s_preroll_mux);
    s_preroll = true;
    portEXIT_CRITICAL(&s_preroll_mux);
    ESP_LOGI(TAG, "续听预录开始（起轮时保留最近 %d ms 前导）", CONFIG_VC_UPLINK_PREROLL_MS);
    return ESP_OK;
}

void vc_preroll_stop(void)
{
    if (!vc_preroll_take()) {
        return;
    }
    audio_fe_uplink_stop();
    ESP_LOGD(TAG, "续听预录放弃（窗口结束/重新唤醒/起轮失败）");
}

/* 预录裁前导：只保留最近 CONFIG_VC_UPLINK_PREROLL_MS 的音频，丢掉更早的静音 */
static void vc_uplink_preroll_trim(void)
{
#if CONFIG_VC_UPLINK_PREROLL_MS > 0
    const uint32_t keep = (uint32_t)CONFIG_VC_UPLINK_PREROLL_MS * 16u; /* 16k 单声道：16 样本/ms */
    audio_fe_uplink_stats_t st;
    if (audio_fe_uplink_stats_read(&st) != ESP_OK) {
        return;
    }
    uint32_t avail = st.samples_written - st.samples_read;
    if (avail <= keep) {
        ESP_LOGI(TAG, "续听预录 %u ms（未超裁剪上限，全部上传）", (unsigned)(avail / 16u));
        return;
    }
    int16_t tmp[CODEC_ENC_FRAME_SAMPLES];
    uint32_t dropped = 0;
    while (avail > keep) {
        uint32_t want = avail - keep;
        if (want > CODEC_ENC_FRAME_SAMPLES) {
            want = CODEC_ENC_FRAME_SAMPLES;
        }
        int got = audio_fe_uplink_read(tmp, want, 0);
        if (got <= 0) {
            break;
        }
        avail -= (uint32_t)got;
        dropped += (uint32_t)got;
    }
    ESP_LOGI(TAG, "续听预录：保留 %u ms 前导（丢弃更早的 %u ms 静音）", (unsigned)(avail / 16u),
             (unsigned)(dropped / 16u));
#endif
}

static void vc_begin_round(uint16_t sid)
{
    char json[VC_JSON_MAX];
    esp_err_t err;

    s_abort = false;
    s_finish_req = false;
    s_tts_end_seen = false;
    s_cloud_error = false;
    s_audio_attempted = false;
    s_tts_fail_reported = false;
    s_sid = sid;
    s_round_frames = 0;
    s_round_bytes = 0;
    s_round_audio_frames = 0;
    stat_set(&s_stats.first_frame_us, 0);
    s_state = VC_STATE_IDLE;

    /* 新轮次覆盖旧轮次：上一路 TTS 若还在收尾/播放，立刻打断（R2 由完成回调恢复） */
    vc_audio_abort("new round");

    /* T3.7：续听轮可能带着"预录"的麦克风音频进来，此时不要清空缓冲 */
    const bool preroll = vc_preroll_take();

    if (!ws_is_ready()) {
        if (preroll) {
            audio_fe_uplink_stop();
        }
        ESP_LOGW(TAG, "云端未就绪，跳过本轮（sid=%u）", (unsigned)sid);
        return;
    }
    if (!preroll && audio_fe_uplink_active()) {
        ESP_LOGW(TAG, "上行接口已被其它使用方占用（diag `afe uplink on`？），本轮跳过");
        return;
    }

    err = enc_open();
    if (err != ESP_OK) {
        if (preroll) {
            audio_fe_uplink_stop();
        }
        ESP_LOGE(TAG, "打开编码器失败: %s", esp_err_to_name(err));
        return;
    }
    (void)enc_reset();

    if (preroll) {
        /* 保留人声起始：预录缓冲里是「说话前静音 + 起轮前那几帧人声」 */
        vc_uplink_preroll_trim();
    } else {
        err = audio_fe_uplink_start();
        if (err != ESP_OK) {
            ESP_LOGE(TAG, "启动上行失败: %s", esp_err_to_name(err));
            enc_close();
            return;
        }
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
    vc_audio_abort(vc_finish_name(why)); /* 上行阶段一般没有流，防御性处理 */
    stat_add(&s_stats.rounds_aborted, 1);
    ESP_LOGW(TAG, "round aborted sid=%u（%s，未发 audio_end）", (unsigned)s_sid,
             vc_finish_name(why));

    /* T3.1：告诉会话状态机本轮异常结束（取消/断链/新轮次/收尾失败） */
    vc_round_end_t result;
    if (why == VC_FIN_LINK_LOST) {
        result = VC_ROUND_ABORT_LINK;
    } else if (why == VC_FIN_ABORT) {
        result = s_pending_start ? VC_ROUND_ABORT_NEW : VC_ROUND_ABORT_CANCEL;
    } else {
        /* VAD/时长/stop 收尾但 audio_end 没发出去 → 起轮/发送失败 */
        result = VC_ROUND_ABORT_SETUP;
    }
    vc_emit_round_end(result);
}

static void vc_wait_response(void)
{
    bool link_lost = false;

    while (s_state == VC_STATE_WAIT_RESPONSE) {
        if (s_abort || s_pending_start) { /* 新唤醒/取消：立即结束本轮等待 */
            s_state = VC_STATE_IDLE;
            break;
        }
        if (!ws_is_ready()) { /* 断链：本轮不可能再收到 tts_end，立刻打断在播音频 */
            link_lost = true;
            s_state = VC_STATE_IDLE;
            break;
        }
        const int64_t remain_us = vc_wait_deadline() - esp_timer_get_time();
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

    if (s_tts_end_seen) {
        stat_add(&s_stats.rounds_done, 1);
        ESP_LOGI(TAG, "round done sid=%u", (unsigned)s_sid);
        vc_emit_round_end(VC_ROUND_DONE);
        return;
    }

    /* 没有 tts_end 就结束：在播的 TTS 音频立刻打断（契约 §2.4），再按原因记账/打日志 */
    vc_audio_abort("no tts_end");
    stat_add(&s_stats.rounds_aborted, 1);
    vc_round_end_t result;
    if (s_pending_start) {
        result = VC_ROUND_ABORT_NEW;
        ESP_LOGW(TAG, "round aborted sid=%u（new-round）", (unsigned)s_sid);
    } else if (s_abort) {
        result = VC_ROUND_ABORT_CANCEL;
        ESP_LOGW(TAG, "round aborted sid=%u（cancel）", (unsigned)s_sid);
    } else if (link_lost) {
        result = VC_ROUND_ABORT_LINK;
        ESP_LOGW(TAG, "round aborted sid=%u（link-lost）", (unsigned)s_sid);
    } else if (s_cloud_error) {
        result = VC_ROUND_ABORT_ERROR;
        ESP_LOGW(TAG, "round aborted sid=%u（cloud-error）", (unsigned)s_sid);
    } else {
        result = VC_ROUND_ABORT_TIMEOUT;
        ESP_LOGW(TAG, "等待云端响应超时（%d ms），sid=%u 结束", CONFIG_VC_RESPONSE_TIMEOUT_MS,
                 (unsigned)s_sid);
    }
    vc_emit_round_end(result);
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
            /* 未 READY / 编码器失败 / 上行占用：已打日志，通知会话状态机本轮没起来 */
            vc_emit_round_end(VC_ROUND_ABORT_SETUP);
            continue;
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

    /* R2：订阅播放完成回调（附加槽，只注册一次）。src==TTS_STREAM 时恢复收音；
     * 订阅失败不致命，但 TTS 播放期间就不能暂停收音（否则没有恢复路径）。 */
    err = audio_player_subscribe_done(vc_stream_done_cb, NULL);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "订阅播放完成回调失败: %s（TTS 播放将不暂停收音）", esp_err_to_name(err));
    } else {
        s_done_subscribed = true;
    }

    BaseType_t ok = xTaskCreatePinnedToCore(vc_task, "vc", CONFIG_VC_TASK_STACK, NULL,
                                            CONFIG_VC_TASK_PRIORITY, &s_task, CONFIG_VC_TASK_CORE);
    if (ok != pdPASS) {
        ESP_LOGE(TAG, "创建 vc 任务失败（栈 %d B）", CONFIG_VC_TASK_STACK);
        if (s_done_subscribed) {
            (void)audio_player_unsubscribe_done(vc_stream_done_cb);
            s_done_subscribed = false;
        }
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
    /* 先在调用方上下文立即打断在播 TTS（不改状态/不阻塞），保证"喊停即停"；
     * vc 任务醒来后还会再防御性 abort 一次（已 active=false 时是 no-op）。 */
    vc_audio_abort("cancel");
    if (s_state == VC_STATE_WAIT_RESPONSE) {
        vc_stop_waiting();
    }
    if (s_task != NULL) {
        xTaskNotifyGive(s_task);
    }
    return ESP_OK;
}

/* T3.2 R4：按键打断 = 先给云端发 interrupt 报文，再本地作废本轮。
 * 报文失败（WS 未 READY/队列满）只 WARN，不阻塞打断流程（本地必须立即停）。
 * 端侧不等待服务端应答（PROTOCOL §8：服务端收到 interrupt 后停发并作废 sid）。 */
esp_err_t vc_interrupt(void)
{
    char json[VC_JSON_MAX];
    const uint16_t sid = s_sid;

    if (!s_inited) {
        return ESP_ERR_INVALID_STATE;
    }
    /* 没有进行中的轮次（例：唤醒提示音阶段还没 audio_start）：不发 interrupt 报文，
     * 否则会把上一轮的 sid 当作"作废目标"发给服务端，属噪声。 */
    if (s_state == VC_STATE_IDLE) {
        ESP_LOGI(TAG, "interrupt: 无进行中的轮次，跳过报文（sid=%u）", (unsigned)sid);
        return ESP_OK;
    }
    snprintf(json, sizeof(json), "{\"t\":\"interrupt\",\"sid\":%u}", (unsigned)sid);
    if (vc_send_json(json)) {
        ESP_LOGW(TAG, "interrupt sent sid=%u", (unsigned)sid);
    } else {
        ESP_LOGW(TAG, "interrupt 未发出（sid=%u），仅本地作废本轮", (unsigned)sid);
    }
    return vc_cancel();
}

bool vc_is_busy(void)
{
    return s_state != VC_STATE_IDLE;
}

bool vc_is_ready(void)
{
    return s_inited && ws_is_ready() && s_state == VC_STATE_IDLE && !s_pending_start;
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
