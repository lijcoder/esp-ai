/*
 * music_cloud —— 音乐云协议（L3 能力层，T4.2）。
 *
 * 协议（ARCHITECTURE §6.3、§8.3；PROTOCOL.md §3.2/§3.3/§7）：
 *
 *   下行 music_track   {"mid":N,"url":"http(s)://...","codec":"mp3","duration_ms":D,"title":"..."}
 *        → audio_player_music_play_url(url)（T4.1 source_http：HTTP(S) → 独立 minimp3 → CH_MUSIC）
 *   下行 music_control {"mid":N,"action":"stop|pause|resume"}
 *        → stop 调 audio_player_music_stop_url()；pause/resume 的执行归 T4.3（播放器还没有
 *          暂停能力），本任务只解析、计数、WARN，不伪造 PAUSED 状态
 *   上行 music_status  {"t":"music_status","state":"playing|stopped|error","mid":N,"progress_ms":P}
 *        → 播放中每 CONFIG_MC_STATUS_PERIOD_MS 一次 + 每次状态变化立即一次
 *
 * 为什么音乐事件不吃语音 sid（P-3，2026-11-10 定稿）：
 *   音乐跨语音轮次播放（TTS 播完继续放），也可能在没有会话时开始；用语音 sid 过滤会被
 *   「轮次结束 / 迟到帧丢弃」误杀。音乐事件带**连接内自增的 mid**，由本组件独立过滤：
 *   - music_track：只接受比当前新的 mid（同 mid 视为重复，旧 mid 丢弃）；
 *   - music_control：允许 mid == 当前曲目（暂停/停止本来就指向正在播的这首），只丢弃更旧的。
 *
 * 线程模型（不建任务，见 ARCHITECTURE §9.3）：
 *   - 下行解析在 `ws_client` 任务上下文（transport_ws 的附加订阅槽），只做定长解析与
 *     临界区内的状态更新，禁止阻塞/网络/Flash；
 *   - 起播/重试与周期上报在两个 esp_timer 回调（`esp_timer` 任务上下文）。播放器调用
 *     **只在定时器回调里发生**：切歌要先 stop 再 play，而 stop 是异步的（music_http 任务
 *     等 HTTP 读超时/淡出后退出），所以在定时器里重试，避免与 ws 回调并发调播放器。
 *   - 状态用 `s_mux` 临界区保护；临界区内只做 memcpy/赋值，不打印、不调用播放器。
 *
 * 内存（CONVENTIONS §4）：只有几个定长小缓冲（URL 320B、标题 48B、状态 JSON 96B）与统计；
 * 不 malloc、不建任务、不占 PSRAM。JSON 扫描器与 voice_cloud 同源（零分配手写扫描），
 * 若后续再加第三个使用方，再抽成 L1 公共组件。
 */
#include "music_cloud.h"

#include <stdio.h>
#include <string.h>

#include "audio_player.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "event_bus.h"
#include "freertos/FreeRTOS.h"
#include "transport_ws.h"

static const char *TAG = "MC";

/* music_status 报文：字段都很短，96B 足够（mid 最大 5 位、progress 最大 10 位） */
#define MC_JSON_MAX 96
/* 下行 t / action / codec 字段的定长缓冲 */
#define MC_EVT_TYPE_MAX 16
#define MC_ACTION_MAX 16
#define MC_CODEC_MAX 8
/* 起播后的保护窗口：music_http 任务创建到真正 music_start() 有延迟（含 HTTP 建连），
 * 这段时间内 audio_player_music_active() 还是 false，不能据此判定"播完了"。 */
#define MC_EOF_GUARD_US (1500 * 1000)
/* 播放器混音固定 48k（ARCHITECTURE §3.2；written/played_frames 都是 48k 帧） */
#define MC_PLAYER_SAMPLE_RATE 48000u

/* ---------------- 全局状态 ---------------- */

static bool s_inited;
static portMUX_TYPE s_mux = portMUX_INITIALIZER_UNLOCKED;

/* 以下受 s_mux 保护 */
static mc_state_t s_state = MC_STATE_IDLE;
static bool s_have_mid;      /* 当前连接内是否已接受过音乐事件（mid 比较基准有效） */
static uint16_t s_mid;       /* 当前曲目 mid */
static int32_t s_duration_ms;/* 0 = 直播流/时长未知 */
static char s_url[MC_URL_MAX];      /* 当前曲目 URL（仅供 diag 展示/日志） */
static char s_title[MC_TITLE_MAX];  /* 当前曲目标题 */
static char s_pending_url[MC_URL_MAX]; /* 待起播 URL；空串 = 没有待办 */
static uint32_t s_progress_ms;  /* 最近一次上报的已播时长（播放器统计不可用时兜底） */
static uint32_t s_progress_base_frames; /* 本曲目起播前播放器已播帧数（切歌对基准，见下） */

/* 以下只在 esp_timer 回调（起播链路）里读写 */
static uint8_t s_play_retry;
static int64_t s_play_started_us; /* play_url 成功时刻，用于 EOF 保护窗口 */

/* 统计：ws_client 与 esp_timer 两个上下文都会写，用 s_mux 合并 */
static mc_stats_t s_stats;

static esp_timer_handle_t s_report_timer; /* 周期上报 + 播放结束探测 */
static esp_timer_handle_t s_play_timer;   /* 起播/切歌重试（one-shot） */

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

/* ---------------- 状态与上报 ---------------- */

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

/*
 * 已播时长 = (播放器累计帧 - 本曲目起播前基准) / 48k。
 * 为什么要有基准：切歌时旧统计还在，而 audio_player_music_start() 才把统计清零；
 * play_url 成功到任务真正 start 之间读到的还是上一首的数。基准在收到新 music_track
 * 时记下旧值，开始新曲后统计清零（played < base）→ 基准自动归零。
 */
static uint32_t progress_from_player(void)
{
    audio_player_music_stats_t st;
    uint32_t base;
    uint32_t played;
    uint32_t fallback;

    if (audio_player_music_stats_read(&st) != ESP_OK) {
        portENTER_CRITICAL(&s_mux);
        fallback = s_progress_ms;
        portEXIT_CRITICAL(&s_mux);
        return fallback;
    }
    played = st.played_frames;
    portENTER_CRITICAL(&s_mux);
    base = s_progress_base_frames;
    if (played < base) {
        s_progress_base_frames = 0; /* 播放器统计已重置（新曲目真正 start） */
        base = 0;
    }
    portEXIT_CRITICAL(&s_mux);
    return (uint32_t)((uint64_t)(played - base) * 1000ull / MC_PLAYER_SAMPLE_RATE);
}

esp_err_t mc_info_read(mc_info_t *out)
{
    if (out == NULL) {
        return ESP_ERR_INVALID_ARG;
    }
    portENTER_CRITICAL(&s_mux);
    out->state = s_state;
    out->have_mid = s_have_mid;
    out->mid = s_mid;
    out->duration_ms = s_duration_ms;
    memcpy(out->url, s_url, sizeof(out->url));
    memcpy(out->title, s_title, sizeof(out->title));
    portEXIT_CRITICAL(&s_mux);
    /* 播放器统计要读 audio_player 的临界区，放在 s_mux 之外（避免嵌套临界区） */
    out->progress_ms = progress_from_player();
    return ESP_OK;
}

esp_err_t mc_report_status(void)
{
    mc_info_t info;
    char json[MC_JSON_MAX];
    int written;
    esp_err_t err;

    if (!s_inited) {
        return ESP_ERR_INVALID_STATE;
    }
    if (!ws_is_ready()) {
        /* 断网时不发（transport 的丢帧 WARN 会被这里每 5s 一次刷出来），只计数 */
        stat_add(&s_stats.status_failed, 1);
        return ESP_ERR_INVALID_STATE;
    }
    (void)mc_info_read(&info);
    /* 记住最近一次进度：播放器统计不可用（停止后/出错）时，stopped 报文沿用这个值 */
    portENTER_CRITICAL(&s_mux);
    s_progress_ms = info.progress_ms;
    portEXIT_CRITICAL(&s_mux);

    written = snprintf(json, sizeof(json),
                       "{\"t\":\"music_status\",\"state\":\"%s\",\"mid\":%u,\"progress_ms\":%u}",
                       mc_state_name(info.state), (unsigned)info.mid, (unsigned)info.progress_ms);
    if (written <= 0 || (size_t)written >= sizeof(json)) {
        stat_add(&s_stats.status_failed, 1);
        ESP_LOGW(TAG, "music_status 报文超长（%d B），未发送", written);
        return ESP_ERR_INVALID_SIZE;
    }

    err = ws_send_text(json);
    if (err != ESP_OK) {
        /* WS 未 READY / 发送队列满：只在 DEBUG 打点（断网时每 5s 一次 WARN 会刷屏） */
        stat_add(&s_stats.status_failed, 1);
        ESP_LOGD(TAG, "music_status 未发出: %s", esp_err_to_name(err));
        return err;
    }
    stat_add(&s_stats.status_sent, 1);
    ESP_LOGD(TAG, "music_status → %s（%u ms）", mc_state_name(info.state),
             (unsigned)info.progress_ms);
    return ESP_OK;
}

static void set_state(mc_state_t state, const char *why)
{
    mc_state_t old;

    portENTER_CRITICAL(&s_mux);
    old = s_state;
    s_state = state;
    portEXIT_CRITICAL(&s_mux);

    if (old == state) {
        return;
    }
    ESP_LOGI(TAG, "state %s → %s（%s）", mc_state_name(old), mc_state_name(state), why);
    /* 语义事件：payload 待 T4.3 定稿，这里只投无 payload 的广播（零分配） */
    if (state == MC_STATE_ERROR) {
        (void)app_evt_post(APP_EVT_MUSIC_ERROR, NULL);
    }
    (void)app_evt_post(APP_EVT_MUSIC_STATE_CHANGED, NULL);
    /* 状态变化立即上报一次（周期上报在 5s 之后） */
    (void)mc_report_status();
}

/* ---------------- 起播（只在 esp_timer 上下文执行） ---------------- */

static void play_timer_cb(void *arg)
{
    char url[MC_URL_MAX];
    bool have_url = false;
    esp_err_t err;

    (void)arg;

    portENTER_CRITICAL(&s_mux);
    if (s_pending_url[0] != '\0') {
        memcpy(url, s_pending_url, sizeof(url));
        have_url = true;
    }
    portEXIT_CRITICAL(&s_mux);
    if (!have_url) {
        return;
    }

    err = audio_player_music_play_url(url);
    if (err == ESP_OK) {
        portENTER_CRITICAL(&s_mux);
        s_pending_url[0] = '\0';
        s_progress_ms = 0;
        portEXIT_CRITICAL(&s_mux);
        s_play_retry = 0;
        s_play_started_us = esp_timer_get_time();
        set_state(MC_STATE_PLAYING, "play_url 成功");
        return;
    }

    /* 切歌/上一路刚停：music_http 任务还在退出，播放器返回 INVALID_STATE → 稍后重试。
     * NO_MEM（任务创建失败）也重试几次，避免瞬时内存吃紧直接判死。 */
    if ((err == ESP_ERR_INVALID_STATE || err == ESP_ERR_NO_MEM) &&
        s_play_retry < CONFIG_MC_PLAY_MAX_RETRIES) {
        s_play_retry++;
        ESP_LOGD(TAG, "play_url 暂不可用（%s），%d/%d 次重试", esp_err_to_name(err),
                 (int)s_play_retry, CONFIG_MC_PLAY_MAX_RETRIES);
        (void)esp_timer_start_once(s_play_timer, (uint64_t)CONFIG_MC_PLAY_RETRY_MS * 1000ull);
        return;
    }

    ESP_LOGE(TAG, "起播失败: %s（url %u B，重试 %u 次后放弃）", esp_err_to_name(err),
             (unsigned)strlen(url), (unsigned)s_play_retry);
    stat_add(&s_stats.play_errors, 1);
    portENTER_CRITICAL(&s_mux);
    s_pending_url[0] = '\0';
    portEXIT_CRITICAL(&s_mux);
    s_play_retry = 0;
    set_state(MC_STATE_ERROR, "play_url 失败");
}

/* 安排一次起播：need_stop=true 表示当前还有音乐在播（切歌），先停再排队 */
static void schedule_play(bool need_stop)
{
    if (need_stop) {
        const esp_err_t err = audio_player_music_stop_url();
        if (err != ESP_OK && err != ESP_ERR_INVALID_STATE) {
            ESP_LOGW(TAG, "切歌前停止旧流失败: %s（继续尝试起播）", esp_err_to_name(err));
        }
    }
    s_play_retry = 0;
    (void)esp_timer_stop(s_play_timer);
    /* 需要等旧任务退出时用重试间隔起步；空闲起播只等 1ms（让出 ws 回调上下文） */
    (void)esp_timer_start_once(s_play_timer,
                               (uint64_t)(need_stop ? CONFIG_MC_PLAY_RETRY_MS : 1) * 1000ull);
}

/* ---------------- 下行事件处理（ws_client 任务上下文） ---------------- */

static void handle_control(const uint8_t *payload, size_t len)
{
    const uint8_t *v = NULL;
    const uint8_t *v_end = NULL;
    int32_t mid_raw = -1;
    char action[MC_ACTION_MAX] = "";
    bool stale = false;

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

    /* 音乐控制允许 mid == 当前曲目（暂停/停止本来就指向正在播的这首），只丢弃更旧的 */
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

    stat_add(&s_stats.rx_control, 1);
    ESP_LOGI(TAG, "music_control mid=%d action=%s", (int)mid_raw, action);

    if (strcmp(action, "stop") == 0) {
        portENTER_CRITICAL(&s_mux);
        s_pending_url[0] = '\0'; /* 取消未开始的起播 */
        portEXIT_CRITICAL(&s_mux);
        (void)esp_timer_stop(s_play_timer);
        const esp_err_t err = audio_player_music_stop_url();
        if (err != ESP_OK && err != ESP_ERR_INVALID_STATE) {
            ESP_LOGW(TAG, "停止音乐失败: %s", esp_err_to_name(err));
        }
        set_state(MC_STATE_STOPPED, "music_control stop");
        return;
    }
    if (strcmp(action, "pause") == 0 || strcmp(action, "resume") == 0) {
        /* 播放器目前只有 play_url/stop_url，没有暂停/继续；真正执行归 T4.3 music_session。
         * 这里不伪造 PAUSED 状态（否则会上报与听感不符的 state）。 */
        stat_add(&s_stats.ignored_actions, 1);
        ESP_LOGW(TAG, "music_control %s 暂未实现（T4.3 交付），本事件忽略（mid=%d）", action,
                 (int)mid_raw);
        return;
    }
    stat_add(&s_stats.rx_parse_errors, 1);
    ESP_LOGW(TAG, "未知 music_control action=%s，丢弃", action);
}

static void handle_track(const uint8_t *payload, size_t len)
{
    const uint8_t *v = NULL;
    const uint8_t *v_end = NULL;
    int32_t mid_raw = -1;
    int32_t duration_ms = 0;
    char url[MC_URL_MAX];
    char title[MC_TITLE_MAX] = "";
    char codec[MC_CODEC_MAX] = "mp3";
    bool stale = false;
    bool duplicate = false;
    bool need_stop;

    if (!mc_find_key(payload, len, "mid", &v, &v_end) || !mc_decode_int(v, v_end, &mid_raw) ||
        mid_raw < 0 || mid_raw > 0xFFFF) {
        stat_add(&s_stats.rx_parse_errors, 1);
        ESP_LOGW(TAG, "music_track 缺 mid 或非法（%u B），丢弃", (unsigned)len);
        return;
    }
    if (!mc_find_key(payload, len, "url", &v, &v_end) ||
        !mc_decode_str(v, v_end, url, sizeof(url), NULL)) {
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
        (void)mc_decode_str(v, v_end, title, sizeof(title), NULL);
    }

    /* URL 只来自 music_track，端侧只校验前缀/长度（ARCH §6.3；禁止从文本里抠 URL）。
     * 解码时可能被截断：长度顶到上限就按"超长"拒绝，不拿半截 URL 去请求。 */
    if (url[0] == '\0' || (strncmp(url, "http://", 7) != 0 && strncmp(url, "https://", 8) != 0)) {
        stat_add(&s_stats.rx_dropped_bad, 1);
        ESP_LOGW(TAG, "music_track URL 非法（非 http(s)），丢弃（mid=%d）", (int)mid_raw);
        return;
    }
    if (strlen(url) >= (size_t)(MC_URL_MAX - 1)) {
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

    /* mid 过滤：同 mid 视为重复（避免同一条曲目被重复起播），旧 mid 丢弃 */
    uint32_t base_frames = 0;
    {
        audio_player_music_stats_t pst;
        if (audio_player_music_stats_read(&pst) == ESP_OK) {
            base_frames = pst.played_frames; /* 切歌基准：旧曲目已播帧数 */
        }
    }
    portENTER_CRITICAL(&s_mux);
    if (s_have_mid) {
        const int16_t diff = (int16_t)((uint16_t)mid_raw - s_mid);
        stale = (diff < 0);
        duplicate = (diff == 0);
    }
    if (!stale && !duplicate) {
        s_mid = (uint16_t)mid_raw;
        s_have_mid = true;
        s_duration_ms = duration_ms;
        s_progress_ms = 0;
        s_progress_base_frames = base_frames;
        memcpy(s_url, url, sizeof(s_url));
        memcpy(s_title, title, sizeof(s_title));
        memcpy(s_pending_url, url, sizeof(s_pending_url));
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

    stat_add(&s_stats.rx_track, 1);
    ESP_LOGI(TAG, "music_track mid=%d title=%s dur=%d url_len=%u", (int)mid_raw,
             title[0] != '\0' ? title : "-", (int)duration_ms, (unsigned)strlen(url));
    ESP_LOGD(TAG, "music_track url=%s", url);

    need_stop = audio_player_music_active() || (mc_get_state() == MC_STATE_PLAYING);
    schedule_play(need_stop);
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

/* ---------------- 周期上报 + 播放结束探测（esp_timer 任务上下文） ---------------- */

static void report_timer_cb(void *arg)
{
    (void)arg;

    if (!ws_is_ready()) {
        /* 连接重建后服务端的 mid 从头开始：清掉比较基准（音乐本身可以继续播） */
        portENTER_CRITICAL(&s_mux);
        s_have_mid = false;
        portEXIT_CRITICAL(&s_mux);
        return;
    }

    if (mc_get_state() != MC_STATE_PLAYING) {
        return;
    }

    /* 切歌中（新 URL 已排进起播定时器）：旧流已停、新流还没起，不能当成"播完了" */
    bool pending;
    portENTER_CRITICAL(&s_mux);
    pending = (s_pending_url[0] != '\0');
    portEXIT_CRITICAL(&s_mux);
    if (pending) {
        return;
    }

    /* 播放结束探测：music_http 正常情况下由服务端关流/EOF 结束；HTTP 失败也会让
     * audio_player_music_active() 变 false。起播后 1.5s 内不判（任务还没跑到 music_start）。 */
    if (!audio_player_music_active() && esp_timer_get_time() - s_play_started_us > MC_EOF_GUARD_US) {
        audio_player_music_stats_t st;
        const bool had_sound =
            (audio_player_music_stats_read(&st) == ESP_OK && st.played_frames > 0);
        if (had_sound) {
            stat_add(&s_stats.eofs, 1);
            set_state(MC_STATE_STOPPED, "流结束");
        } else {
            stat_add(&s_stats.eofs, 1);
            set_state(MC_STATE_ERROR, "没有解出声音（HTTP/解码失败）");
        }
        return;
    }

    (void)mc_report_status();
}

/* ---------------- 对外 API ---------------- */

esp_err_t mc_init(void)
{
    uint32_t token = 0;
    esp_err_t err;

    if (s_inited) {
        return ESP_ERR_INVALID_STATE;
    }
    /* 附加订阅槽（不影响 voice_cloud 的主回调，也不影响 diag 的 `ws rxlog`） */
    err = ws_subscribe(mc_ws_cb, NULL, &token);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "订阅 WS 下行失败: %s（mc_init 必须在 ws_init 之后）", esp_err_to_name(err));
        return err;
    }

    const esp_timer_create_args_t report_args = {
        .callback = report_timer_cb,
        .arg = NULL,
        .name = "mc_report",
    };
    err = esp_timer_create(&report_args, &s_report_timer);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "创建上报定时器失败: %s", esp_err_to_name(err));
        goto fail;
    }
    const esp_timer_create_args_t play_args = {
        .callback = play_timer_cb,
        .arg = NULL,
        .name = "mc_play",
    };
    err = esp_timer_create(&play_args, &s_play_timer);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "创建起播定时器失败: %s", esp_err_to_name(err));
        goto fail;
    }
    err = esp_timer_start_periodic(s_report_timer, (uint64_t)CONFIG_MC_STATUS_PERIOD_MS * 1000ull);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "启动上报定时器失败: %s", esp_err_to_name(err));
        goto fail;
    }

    s_inited = true;
    ESP_LOGI(TAG, "ready（上报周期 %d ms，play 重试 %d ms × %d；不建任务）",
             CONFIG_MC_STATUS_PERIOD_MS, CONFIG_MC_PLAY_RETRY_MS, CONFIG_MC_PLAY_MAX_RETRIES);
    return ESP_OK;

fail:
    if (s_report_timer != NULL) {
        (void)esp_timer_delete(s_report_timer);
        s_report_timer = NULL;
    }
    if (s_play_timer != NULL) {
        (void)esp_timer_delete(s_play_timer);
        s_play_timer = NULL;
    }
    (void)ws_unsubscribe(token);
    return err;
}

mc_state_t mc_get_state(void)
{
    mc_state_t state;

    portENTER_CRITICAL(&s_mux);
    state = s_state;
    portEXIT_CRITICAL(&s_mux);
    return state;
}

uint16_t mc_current_mid(void)
{
    uint16_t mid;

    portENTER_CRITICAL(&s_mux);
    mid = s_have_mid ? s_mid : 0;
    portEXIT_CRITICAL(&s_mux);
    return mid;
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
