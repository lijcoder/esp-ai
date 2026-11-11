/*
 * music_session —— 音乐播放编排（L4，T4.3）。
 *
 * 分层（ARCHITECTURE §4.1/§4.3；build/t43_contract.md §1）：
 *   music_cloud (L3) ──注册回调（ws_client 上下文，禁止阻塞）──▶ 本组件
 *   本组件 ──只在 esp_timer 上下文串行调用──▶ audio_player (L2)
 *   语音状态经 event_bus（同层）订阅，不改 voice_session。
 *
 * 线程模型（不建任务，见 ARCHITECTURE §9.3）：
 *   - 两个 esp_timer：`ms_play`（起播/切歌重试 + 所有播放器命令串行执行）、
 *     `ms_status`（周期 music_status + EOF 探测）；
 *   - 所有状态迁移都发生在 `ms_play` 定时器回调里，用 CAS（期望状态→目标状态）；
 *   - `music_cloud` 回调、语音事件、console API 只做判定/调度，不直接调播放器。
 *
 * 内存（CONVENTIONS §4）：只有状态/统计/URL(320B)/标题(48B) 等定长静态变量；
 * 不 malloc、不申请 PSRAM、不建任务。
 */
#include "music_session.h"

#include <stdio.h>
#include <string.h>

#include "audio_player.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "event_bus.h"
#include "freertos/FreeRTOS.h"
#include "music_policy.h"
#include "sdkconfig.h"

static const char *TAG = "MS";

/* 播放器混音固定 48k（ARCHITECTURE §3.2；played_frames 都是 48k 帧） */
#define MS_PLAYER_SAMPLE_RATE 48000u
/* 起播后的保护窗口：music_http 任务创建 → 真正 music_start() 有 HTTP 建连延迟，
 * 这段时间 active 还是 false，不能据此判"播完了"（沿用 T4.2 的口径）。 */
#define MS_EOF_GUARD_US ((int64_t)CONFIG_MS_EOF_GUARD_MS * 1000)
/* 命令延时：让出当前回调上下文（ws/evt/console），并给 host 测试留确定性 */
#define MS_CMD_DELAY_US 1000ull
#define MS_FADE_MAX_MS  1000u

/* 纯逻辑头里的数值必须与真实枚举一致（host 单测无法 include mc_state_t） */
_Static_assert(MC_STATE_IDLE == MS_POLICY_STATE_IDLE, "mc_state_t IDLE 数值漂移");
_Static_assert(MC_STATE_PLAYING == MS_POLICY_STATE_PLAYING, "mc_state_t PLAYING 数值漂移");
_Static_assert(MC_STATE_PAUSED == MS_POLICY_STATE_PAUSED, "mc_state_t PAUSED 数值漂移");
_Static_assert(MC_STATE_STOPPED == MS_POLICY_STATE_STOPPED, "mc_state_t STOPPED 数值漂移");
_Static_assert(MC_STATE_ERROR == MS_POLICY_STATE_ERROR, "mc_state_t ERROR 数值漂移");
_Static_assert(MC_ACTION_PAUSE == 0 && MC_ACTION_RESUME == 1 && MC_ACTION_STOP == 2,
               "mc_action_t 数值漂移（music_policy 依赖）");

/* 事件 payload 的 reason（契约 §7） */
enum {
    MS_REASON_START = 0,       /* 起播/切歌 */
    MS_REASON_CONTROL = 1,     /* 协议控制 */
    MS_REASON_EOF = 2,         /* 自然结束/流中断 */
    MS_REASON_ERROR = 3,       /* HTTP/解码/起播重试耗尽 */
    MS_REASON_VOICE = 4,       /* 语音抢占 */
    MS_REASON_USER_PAUSE = 5,  /* 用户暂停 */
    MS_REASON_USER_RESUME = 6, /* 用户继续 */
    MS_REASON_STOP = 7,        /* 停止 */
};

typedef enum {
    MS_CMD_NONE = 0,
    MS_CMD_PLAY,   /* 起播/切歌（need_stop 时先停旧流） */
    MS_CMD_PAUSE,  /* reason=MS_PAUSE_REASON_* */
    MS_CMD_RESUME,
    MS_CMD_STOP,
} ms_cmd_kind_t;

typedef struct {
    uint8_t kind;      /* ms_cmd_kind_t */
    uint8_t reason;    /* MS_CMD_PAUSE：MS_PAUSE_REASON_*；PLAY：事件 reason */
    uint8_t need_stop; /* MS_CMD_PLAY：起播前先 stop_url（切歌） */
    uint8_t retry;     /* MS_CMD_PLAY/PAUSE：已重试次数 */
    uint16_t fade_ms;  /* MS_CMD_PAUSE */
} ms_cmd_t;

/* ---------------- 全局状态 ---------------- */

static bool s_inited;
static portMUX_TYPE s_mux = portMUX_INITIALIZER_UNLOCKED;

/* 以下受 s_mux 保护 */
static mc_state_t s_state = MC_STATE_IDLE;
static uint8_t s_pause_reason = MS_PAUSE_REASON_NONE;
static bool s_have_mid;
static uint16_t s_mid;
static int32_t s_duration_ms;
static char s_url[MS_URL_MAX];
static char s_title[MS_TITLE_MAX];
static uint32_t s_progress_base_frames; /* 本曲目起播前播放器已播帧数（切歌基准） */
static uint32_t s_progress_ms;          /* 最近一次已知进度（统计不可用时兜底） */
static int64_t s_state_since_us;
static bool s_voice_active;     /* 语音是否处于活跃态（供起播后自动让路判断） */
static bool s_resume_watch;     /* resume 后等待数据；用于长暂停断流兜底 */
static uint32_t s_resume_watch_frames;
static bool s_fallback_sent;    /* 本暂停周期是否已发过兜底 music_request */
static bool s_track_pending;    /* 语音活跃时收到 music_track：排队到语音回 IDLE 再起播 */
static bool s_defer_resume;     /* 语音活跃时收到 resume：排队到语音回 IDLE 再续播（否则抢播放器让 TTS 失败） */
static uint8_t s_defer_pause_reason; /* 起播/切歌进行中收到的暂停请求，起播成功后补执行 */
static uint16_t s_req_id;       /* music_request 端侧自增序号（0 保留） */
static ms_stats_t s_stats;
static ms_cmd_t s_cmd;
static bool s_cmd_valid;

/* 以下只在 esp_timer 上下文写（ms_play 回调） */
static int64_t s_play_started_us;

static esp_timer_handle_t s_play_timer;
static esp_timer_handle_t s_status_timer;

/* ---------------- 小工具 ---------------- */

static void stat_add(uint32_t *field, uint32_t value)
{
    portENTER_CRITICAL(&s_mux);
    *field += value;
    portEXIT_CRITICAL(&s_mux);
}

static const char *ms_reason_name(uint8_t reason)
{
    switch (reason) {
    case MS_REASON_START: return "start";
    case MS_REASON_CONTROL: return "control";
    case MS_REASON_EOF: return "eof";
    case MS_REASON_ERROR: return "error";
    case MS_REASON_VOICE: return "voice";
    case MS_REASON_USER_PAUSE: return "user-pause";
    case MS_REASON_USER_RESUME: return "user-resume";
    case MS_REASON_STOP: return "stop";
    default: return "?";
    }
}

static uint32_t ms_player_played_frames(void)
{
    audio_player_music_stats_t pst;
    if (audio_player_music_stats_read(&pst) != ESP_OK) {
        return 0;
    }
    return pst.played_frames;
}

/*
 * 已播时长 = (播放器累计帧 - 本曲目起播前基准) / 48k。
 * 切歌时旧统计还在、新曲目真正 start 后统计清零（played < base）→ 基准自动归零；
 * 暂停时 played_frames 冻结，progress 自然冻结（契约 §2.3/§5）。
 */
static uint32_t ms_progress_from_player(void)
{
    audio_player_music_stats_t pst;
    uint32_t base;

    if (audio_player_music_stats_read(&pst) != ESP_OK) {
        portENTER_CRITICAL(&s_mux);
        const uint32_t fallback = s_progress_ms;
        portEXIT_CRITICAL(&s_mux);
        return fallback;
    }

    portENTER_CRITICAL(&s_mux);
    base = s_progress_base_frames;
    if (pst.played_frames < base) {
        s_progress_base_frames = 0; /* 播放器统计已重置（新曲目真正 start） */
        base = 0;
    }
    portEXIT_CRITICAL(&s_mux);
    return (uint32_t)((uint64_t)(pst.played_frames - base) * 1000ull / MS_PLAYER_SAMPLE_RATE);
}

/* ---------------- 状态迁移（只在 esp_timer 上下文） ---------------- */

static bool ms_transition(mc_state_t from, mc_state_t to, uint8_t reason)
{
    bool ok = false;
    uint16_t mid = 0;
    int32_t dur_ms = 0;
    uint32_t progress_ms = 0;
    uint32_t stay_ms = 0;

    progress_ms = ms_progress_from_player();

    portENTER_CRITICAL(&s_mux);
    if (s_state == from) {
        const int64_t now = esp_timer_get_time();
        stay_ms = (uint32_t)((now - s_state_since_us) / 1000);
        s_state = to;
        s_state_since_us = now;
        if (to == MC_STATE_PAUSED) {
            if (reason == MS_REASON_VOICE) {
                s_pause_reason = MS_PAUSE_REASON_VOICE;
            } else if (reason == MS_REASON_USER_PAUSE) {
                s_pause_reason = MS_PAUSE_REASON_USER;
            }
        } else {
            s_pause_reason = MS_PAUSE_REASON_NONE;
        }
        if (to == MC_STATE_STOPPED || to == MC_STATE_ERROR) {
            s_resume_watch = false;
        }
        s_progress_ms = progress_ms;
        mid = s_have_mid ? s_mid : 0;
        dur_ms = s_duration_ms;
        s_stats.transitions++;
        ok = true;
    }
    portEXIT_CRITICAL(&s_mux);

    if (!ok) {
        ESP_LOGW(TAG, "状态迁移被拒绝：期望 %s（当前不是该状态）→ %s",
                 mc_state_name(from), mc_state_name(to));
        return false;
    }

    ESP_LOGI(TAG, "state %s -> %s（mid=%u，停留 %u ms，原因=%s，progress=%u ms）",
             mc_state_name(from), mc_state_name(to), (unsigned)mid, (unsigned)stay_ms,
             ms_reason_name(reason), (unsigned)progress_ms);

    app_evt_data_t ev;
    memset(&ev, 0, sizeof(ev));
    ev.music_state.mid = mid;
    ev.music_state.from = (uint8_t)from;
    ev.music_state.to = (uint8_t)to;
    ev.music_state.reason = reason;
    ev.music_state.progress_ms = progress_ms;
    ev.music_state.dur_ms = (uint32_t)dur_ms;
    (void)app_evt_post(APP_EVT_MUSIC_STATE_CHANGED, &ev);
    if (to == MC_STATE_ERROR) {
        (void)app_evt_post(APP_EVT_MUSIC_ERROR, &ev);
    }

    /* 每次迁移立即上报一次（周期上报由 ms_status 定时器驱动） */
    (void)mc_report_status(to, progress_ms);
    return true;
}

/* ---------------- 命令调度（任意上下文，只置命令 + 起一次性定时器） ---------------- */

static void ms_schedule(uint8_t kind, uint8_t reason, uint16_t fade_ms, uint8_t need_stop,
                        uint8_t retry, uint64_t delay_us)
{
    portENTER_CRITICAL(&s_mux);
    s_cmd.kind = kind;
    s_cmd.reason = reason;
    s_cmd.need_stop = need_stop;
    s_cmd.retry = retry;
    s_cmd.fade_ms = fade_ms;
    s_cmd_valid = true;
    portEXIT_CRITICAL(&s_mux);

    (void)esp_timer_stop(s_play_timer);
    const esp_err_t err = esp_timer_start_once(s_play_timer, delay_us != 0 ? delay_us : MS_CMD_DELAY_US);
    if (err != ESP_OK) {
        ESP_LOGD(TAG, "命令定时器启动失败: %s（等待下一次调度）", esp_err_to_name(err));
    }
}

/* 自动续作（起播重试/切歌下一步/起播后的补暂停）只在命令槽空闲时排队：
 * 若执行期间已有更新的用户/语音命令到达，让它生效，不能把新命令覆盖掉。 */
static bool ms_schedule_if_idle(uint8_t kind, uint8_t reason, uint16_t fade_ms, uint8_t retry,
                                uint64_t delay_us)
{
    portENTER_CRITICAL(&s_mux);
    if (s_cmd_valid) {
        portEXIT_CRITICAL(&s_mux);
        ESP_LOGD(TAG, "已有更新的命令排队，丢弃自动续作（kind=%u）", (unsigned)kind);
        return false;
    }
    s_cmd.kind = kind;
    s_cmd.reason = reason;
    s_cmd.need_stop = 0;
    s_cmd.retry = retry;
    s_cmd.fade_ms = fade_ms;
    s_cmd_valid = true;
    portEXIT_CRITICAL(&s_mux);

    (void)esp_timer_stop(s_play_timer);
    const esp_err_t err = esp_timer_start_once(s_play_timer, delay_us != 0 ? delay_us : MS_CMD_DELAY_US);
    if (err != ESP_OK) {
        ESP_LOGD(TAG, "自动续作定时器启动失败: %s", esp_err_to_name(err));
    }
    return true;
}

/* 取消尚未执行的某类命令（例如语音回 IDLE 撤销排队中的 auto-pause） */
static bool ms_cancel_cmd(uint8_t kind)
{
    bool hit = false;
    portENTER_CRITICAL(&s_mux);
    if (s_cmd_valid && s_cmd.kind == kind) {
        s_cmd_valid = false;
        hit = true;
    }
    portEXIT_CRITICAL(&s_mux);
    if (hit) {
        (void)esp_timer_stop(s_play_timer);
    }
    return hit;
}

static uint16_t ms_next_req_id(void)
{
    portENTER_CRITICAL(&s_mux);
    s_req_id++;
    if (s_req_id == 0) {
        s_req_id = 1;
    }
    const uint16_t id = s_req_id;
    portEXIT_CRITICAL(&s_mux);
    return id;
}

/* resume 失败/长暂停断流的兜底：每个暂停周期最多一次 music_request */
static void ms_fallback_request(void)
{
    bool already;
    portENTER_CRITICAL(&s_mux);
    already = s_fallback_sent;
    if (!already) {
        s_fallback_sent = true;
        s_stats.fallback_requests++;
    }
    portEXIT_CRITICAL(&s_mux);
    if (already) {
        return;
    }
    const uint16_t id = ms_next_req_id();
    ESP_LOGW(TAG, "续播兜底：music_request play（id=%u，index=0 服务端自选）", (unsigned)id);
    const esp_err_t err = mc_send_request(MC_REQUEST_ACTION_PLAY, id, 0);
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "兜底 music_request 未发出: %s", esp_err_to_name(err));
    }
}

/* ---------------- 播放器命令执行（esp_timer 上下文） ---------------- */

static void ms_do_play(const ms_cmd_t *cmd)
{
    char url[MS_URL_MAX];
    mc_state_t cur;
    bool voice_active;

    portENTER_CRITICAL(&s_mux);
    memcpy(url, s_url, sizeof(url));
    cur = s_state;
    voice_active = s_voice_active;
    portEXIT_CRITICAL(&s_mux);

    if (url[0] == '\0') {
        stat_add(&s_stats.dropped, 1);
        ESP_LOGW(TAG, "起播命令没有 URL，丢弃");
        return;
    }

    if (cmd->need_stop) {
        /* 切歌：先停旧流（music_http 任务异步退出），下一个命令再 play（契约 §3） */
        const esp_err_t serr = audio_player_music_stop_url();
        if (serr != ESP_OK && serr != ESP_ERR_INVALID_STATE) {
            ESP_LOGW(TAG, "切歌前停止旧流失败: %s（继续尝试起播）", esp_err_to_name(serr));
        }
        (void)ms_schedule_if_idle(MS_CMD_PLAY, cmd->reason, 0, 0,
                                  (uint64_t)CONFIG_MS_PLAY_RETRY_MS * 1000ull);
        return;
    }

    const esp_err_t err = audio_player_music_play_url(url);
    if (err == ESP_OK) {
        uint8_t deferred;
        portENTER_CRITICAL(&s_mux);
        s_play_started_us = esp_timer_get_time();
        s_resume_watch = false;
        s_fallback_sent = false;
        s_progress_ms = 0;
        s_track_pending = false;
        deferred = s_defer_pause_reason;
        s_defer_pause_reason = MS_PAUSE_REASON_NONE;
        portEXIT_CRITICAL(&s_mux);

        (void)ms_transition(cur, MC_STATE_PLAYING, MS_REASON_START);
        if (deferred != MS_PAUSE_REASON_NONE) {
            /* 起播/切歌期间收到的暂停请求：现在补执行，避免音乐在语音期间出声 */
            (void)ms_schedule_if_idle(MS_CMD_PAUSE, deferred, CONFIG_MS_PAUSE_FADE_MS, 0,
                                      MS_CMD_DELAY_US);
        } else if (CONFIG_MS_AUTO_PAUSE_ON_VOICE && voice_active) {
            (void)ms_schedule_if_idle(MS_CMD_PAUSE, MS_PAUSE_REASON_VOICE, CONFIG_MS_PAUSE_FADE_MS,
                                      0, MS_CMD_DELAY_US);
        }
        return;
    }

    /* 切歌/上一路刚停：music_http 任务还在退出，播放器返回 INVALID_STATE → 稍后重试 */
    if ((err == ESP_ERR_INVALID_STATE || err == ESP_ERR_NO_MEM || err == ESP_ERR_TIMEOUT) &&
        cmd->retry < CONFIG_MS_PLAY_MAX_RETRIES) {
        ESP_LOGD(TAG, "play_url 暂不可用（%s），%d/%d 次重试", esp_err_to_name(err),
                 (int)(cmd->retry + 1), CONFIG_MS_PLAY_MAX_RETRIES);
        (void)ms_schedule_if_idle(MS_CMD_PLAY, cmd->reason, 0, (uint8_t)(cmd->retry + 1),
                                  (uint64_t)CONFIG_MS_PLAY_RETRY_MS * 1000ull);
        return;
    }

    stat_add(&s_stats.play_errors, 1);
    ESP_LOGE(TAG, "起播失败: %s（url %u B，重试 %u 次后放弃）", esp_err_to_name(err),
             (unsigned)strlen(url), (unsigned)cmd->retry);
    portENTER_CRITICAL(&s_mux);
    s_track_pending = false;
    s_defer_pause_reason = MS_PAUSE_REASON_NONE;
    portEXIT_CRITICAL(&s_mux);
    (void)ms_transition(cur, MC_STATE_ERROR, MS_REASON_ERROR);
}

static void ms_do_pause(const ms_cmd_t *cmd)
{
    mc_state_t cur;
    uint8_t reason;

    portENTER_CRITICAL(&s_mux);
    cur = s_state;
    reason = s_pause_reason;
    portEXIT_CRITICAL(&s_mux);

    if (cur == MC_STATE_PAUSED) {
        /* 已暂停：只更新原因（用户 pause 覆盖语音抢占的 latch） */
        const uint8_t merged = ms_policy_pause_reason_merge(reason, cmd->reason);
        portENTER_CRITICAL(&s_mux);
        s_pause_reason = merged;
        portEXIT_CRITICAL(&s_mux);
        return;
    }
    if (cur != MC_STATE_PLAYING) {
        stat_add(&s_stats.dropped, 1);
        return;
    }

    const uint32_t fade = cmd->fade_ms != 0 ? cmd->fade_ms : CONFIG_MS_PAUSE_FADE_MS;
    const esp_err_t err = audio_player_music_pause(fade);
    if (err == ESP_OK) {
        portENTER_CRITICAL(&s_mux);
        s_stats.pauses++;
        if (cmd->reason == MS_PAUSE_REASON_VOICE) {
            s_stats.steals++;
        }
        s_fallback_sent = false; /* 新暂停周期，允许一次兜底 */
        portEXIT_CRITICAL(&s_mux);
        const uint8_t ev_reason =
            (cmd->reason == MS_PAUSE_REASON_VOICE) ? MS_REASON_VOICE : MS_REASON_USER_PAUSE;
        (void)ms_transition(cur, MC_STATE_PAUSED, ev_reason);
        return;
    }

    /* 起播命令刚成功、music_http 还没走到 music_start()：暂停暂时不可用 → 重试 */
    if (err == ESP_ERR_INVALID_STATE && cmd->retry < CONFIG_MS_PLAY_MAX_RETRIES) {
        (void)ms_schedule_if_idle(MS_CMD_PAUSE, cmd->reason, cmd->fade_ms,
                                  (uint8_t)(cmd->retry + 1),
                                  (uint64_t)CONFIG_MS_PLAY_RETRY_MS * 1000ull);
        return;
    }

    stat_add(&s_stats.dropped, 1);
    ESP_LOGW(TAG, "暂停失败（%s），保持 %s", esp_err_to_name(err), mc_state_name(cur));
}

static void ms_do_resume(const ms_cmd_t *cmd)
{
    (void)cmd;
    mc_state_t cur;

    portENTER_CRITICAL(&s_mux);
    cur = s_state;
    portEXIT_CRITICAL(&s_mux);
    if (cur != MC_STATE_PAUSED) {
        stat_add(&s_stats.dropped, 1);
        return;
    }

    const esp_err_t err = audio_player_music_resume();
    if (err == ESP_OK) {
        const uint32_t frames = ms_player_played_frames();
        portENTER_CRITICAL(&s_mux);
        s_stats.resumes++;
        s_resume_watch = true;
        s_resume_watch_frames = frames;
        s_fallback_sent = false;
        portEXIT_CRITICAL(&s_mux);
        (void)ms_transition(cur, MC_STATE_PLAYING, MS_REASON_USER_RESUME);
        return;
    }

    stat_add(&s_stats.resume_failures, 1);
    bool already;
    portENTER_CRITICAL(&s_mux);
    already = s_fallback_sent;
    portEXIT_CRITICAL(&s_mux);
    ESP_LOGW(TAG, "续播失败（%s），保持暂停%s", esp_err_to_name(err),
             already ? "（本周期已发过兜底）" : "并发起兜底");
    if (ms_policy_resume_fallback(CONFIG_MS_RESUME_FALLBACK_REQUEST, already, MC_STATE_PAUSED)) {
        ms_fallback_request();
    }
}

static void ms_do_stop(const ms_cmd_t *cmd)
{
    (void)cmd;
    mc_state_t cur;

    portENTER_CRITICAL(&s_mux);
    cur = s_state;
    portEXIT_CRITICAL(&s_mux);

    if (cur != MC_STATE_PLAYING && cur != MC_STATE_PAUSED) {
        /* 停止命令可能来自"起播还没成功"：仍尝试停掉拉流任务，不改状态 */
        (void)audio_player_music_stop_url();
        return;
    }

    const esp_err_t err = audio_player_music_stop_url();
    if (err != ESP_OK && err != ESP_ERR_INVALID_STATE) {
        ESP_LOGW(TAG, "停止音乐失败: %s", esp_err_to_name(err));
    }
    portENTER_CRITICAL(&s_mux);
    s_stats.stops++;
    s_fallback_sent = false;
    s_resume_watch = false;
    s_track_pending = false;
    s_defer_pause_reason = MS_PAUSE_REASON_NONE;
    portEXIT_CRITICAL(&s_mux);
    (void)ms_transition(cur, MC_STATE_STOPPED, MS_REASON_STOP);
}

static void ms_play_timer_cb(void *arg)
{
    (void)arg;
    ms_cmd_t cmd;

    portENTER_CRITICAL(&s_mux);
    if (!s_cmd_valid) {
        portEXIT_CRITICAL(&s_mux);
        return;
    }
    cmd = s_cmd;
    s_cmd_valid = false;
    portEXIT_CRITICAL(&s_mux);

    switch (cmd.kind) {
    case MS_CMD_PLAY: ms_do_play(&cmd); break;
    case MS_CMD_PAUSE: ms_do_pause(&cmd); break;
    case MS_CMD_RESUME: ms_do_resume(&cmd); break;
    case MS_CMD_STOP: ms_do_stop(&cmd); break;
    default: break;
    }
}

/* ---------------- 周期上报 + EOF 探测（esp_timer 上下文） ---------------- */

static void ms_report_now(void)
{
    mc_state_t state;
    portENTER_CRITICAL(&s_mux);
    state = s_state;
    portEXIT_CRITICAL(&s_mux);
    const uint32_t progress = ms_progress_from_player();
    portENTER_CRITICAL(&s_mux);
    s_progress_ms = progress;
    portEXIT_CRITICAL(&s_mux);
    (void)mc_report_status(state, progress);
}

static void ms_status_timer_cb(void *arg)
{
    (void)arg;
    mc_state_t cur;
    bool cmd_pending;
    bool resume_watch;
    uint32_t watch_frames;
    int64_t started_us;

    portENTER_CRITICAL(&s_mux);
    cur = s_state;
    cmd_pending = s_cmd_valid;
    resume_watch = s_resume_watch;
    watch_frames = s_resume_watch_frames;
    started_us = s_play_started_us;
    portEXIT_CRITICAL(&s_mux);

    if (cur == MC_STATE_PLAYING) {
        /* 起播/切歌/暂停命令执行中：旧流可能已停、新流还没起，不能当成"播完了" */
        if (cmd_pending) {
            return;
        }
        if (!audio_player_music_active()) {
            if (esp_timer_get_time() - started_us <= MS_EOF_GUARD_US) {
                return; /* 保护窗口内：music_http 还在建连/预填 */
            }
            audio_player_music_stats_t pst;
            const bool had_sound =
                (audio_player_music_stats_read(&pst) == ESP_OK && pst.played_frames > 0);
            stat_add(&s_stats.eofs, 1);
            if (had_sound) {
                (void)ms_transition(cur, MC_STATE_STOPPED, MS_REASON_EOF);
            } else {
                ESP_LOGE(TAG, "播放器已停且没有解出过声音（HTTP/解码失败）");
                (void)ms_transition(cur, MC_STATE_ERROR, MS_REASON_ERROR);
            }
            if (resume_watch && CONFIG_MS_RESUME_FALLBACK_REQUEST) {
                /* 长暂停后继续拿不到数据（服务端 idle 超时/TCP 断）：兜底一次 music_request */
                ms_fallback_request();
            }
            return;
        }
        if (resume_watch) {
            audio_player_music_stats_t pst;
            if (audio_player_music_stats_read(&pst) == ESP_OK && pst.played_frames > watch_frames) {
                portENTER_CRITICAL(&s_mux);
                s_resume_watch = false;
                portEXIT_CRITICAL(&s_mux);
            }
        }
        ms_report_now();
        return;
    }

    if (cur == MC_STATE_PAUSED) {
        /* 暂停/继续/切歌命令执行中：等命令落地再判断，避免中间态误报 stopped/error */
        if (cmd_pending) {
            return;
        }
        /* paused 必须真实：播放器已不在（起播失败/流结束/暂停请求恰好落在收尾窗口）时
         * 不能继续谎报 paused。 */
        if (!audio_player_music_active()) {
            audio_player_music_stats_t pst;
            const bool had_sound =
                (audio_player_music_stats_read(&pst) == ESP_OK && pst.played_frames > 0);
            stat_add(&s_stats.eofs, 1);
            ESP_LOGW(TAG, "暂停态下播放器已结束（had_sound=%d）→ %s", had_sound ? 1 : 0,
                     had_sound ? "stopped" : "error");
            (void)ms_transition(cur, had_sound ? MC_STATE_STOPPED : MC_STATE_ERROR,
                                had_sound ? MS_REASON_EOF : MS_REASON_ERROR);
            return;
        }
        /* paused 仍周期上报（progress 冻结，契约 §5） */
        ms_report_now();
    }
}

/* ---------------- music_cloud 回调（ws_client 上下文，禁止阻塞） ---------------- */

static esp_err_t ms_pause_internal(uint8_t reason, uint32_t fade_ms);
static esp_err_t ms_resume_internal(void);
static esp_err_t ms_stop_internal(void);

static void ms_on_track(const mc_evt_t *evt)
{
    const uint32_t base_frames = ms_player_played_frames();
    mc_state_t cur;
    bool need_stop;

    portENTER_CRITICAL(&s_mux);
    s_mid = evt->mid;
    s_have_mid = true;
    s_defer_resume = false;
    s_duration_ms = evt->duration_ms;
    memcpy(s_url, evt->url, sizeof(s_url));
    memcpy(s_title, evt->title, sizeof(s_title));
    s_progress_base_frames = base_frames;
    s_progress_ms = 0;
    s_resume_watch = false;
    s_fallback_sent = false;
    s_stats.tracks++;
    /* 切歌进行中若已有排队的暂停命令，先记下来，新曲目起播成功后补执行 */
    if (s_cmd_valid && s_cmd.kind == MS_CMD_PAUSE) {
        s_defer_pause_reason =
            ms_policy_pause_reason_merge(s_defer_pause_reason, s_cmd.reason);
    }
    cur = s_state;
    const bool voice_active = s_voice_active;
    portEXIT_CRITICAL(&s_mux);

    /* 语音活跃时不要立刻起播：此时播放器可能正被 TTS/提示音占用，music_start 会失败，
     * 重试窗口（~1.8s）覆盖不了长 TTS。先暂停旧音乐，把曲目排队到语音回 IDLE 再起播。 */
    if (voice_active) {
        portENTER_CRITICAL(&s_mux);
        s_track_pending = true;
        portEXIT_CRITICAL(&s_mux);
        ESP_LOGI(TAG, "语音活跃中收到 music_track mid=%u → 排队到语音回 IDLE 后起播",
                 (unsigned)evt->mid);
        (void)ms_pause_internal(MS_PAUSE_REASON_VOICE, CONFIG_MS_PAUSE_FADE_MS);
        return;
    }

    portENTER_CRITICAL(&s_mux);
    s_track_pending = false;
    portEXIT_CRITICAL(&s_mux);
    need_stop = audio_player_music_active() || cur == MC_STATE_PLAYING || cur == MC_STATE_PAUSED;
    ESP_LOGI(TAG, "music_track mid=%u，%s起播（dur=%d ms）", (unsigned)evt->mid,
             need_stop ? "切歌：" : "", (int)evt->duration_ms);
    ms_schedule(MS_CMD_PLAY, MS_REASON_START, 0, need_stop ? 1 : 0, 0, MS_CMD_DELAY_US);
}

static void ms_on_control(const mc_evt_t *evt)
{
    esp_err_t err = ESP_ERR_INVALID_STATE;

    switch (evt->action) {
    case MC_ACTION_PAUSE:
        err = ms_pause_internal(MS_PAUSE_REASON_USER, CONFIG_MS_PAUSE_FADE_MS);
        break;
    case MC_ACTION_RESUME:
        err = ms_resume_internal();
        break;
    case MC_ACTION_STOP:
        err = ms_stop_internal();
        break;
    default:
        break;
    }
    if (err == ESP_OK) {
        stat_add(&s_stats.controls, 1);
    } else {
        stat_add(&s_stats.dropped, 1);
        ESP_LOGW(TAG, "music_control %s 在当前状态被忽略", mc_action_name(evt->action));
    }
}

static void ms_mc_cb(void *arg, const mc_evt_t *evt)
{
    (void)arg;
    if (evt == NULL) {
        return;
    }
    if (evt->kind == MC_EVT_TRACK) {
        ms_on_track(evt);
    } else if (evt->kind == MC_EVT_CONTROL) {
        ms_on_control(evt);
    }
}

/* ---------------- 内部动作（任意上下文：只调度） ---------------- */

static esp_err_t ms_pause_internal(uint8_t reason, uint32_t fade_ms)
{
    mc_state_t cur;
    uint8_t pending_reason = MS_PAUSE_REASON_NONE;
    bool play_pending;
    bool resume_pending;

    portENTER_CRITICAL(&s_mux);
    s_defer_resume = false;
    cur = s_state;
    play_pending = (s_cmd_valid && s_cmd.kind == MS_CMD_PLAY);
    resume_pending = (s_cmd_valid && s_cmd.kind == MS_CMD_RESUME);
    if (s_cmd_valid && s_cmd.kind == MS_CMD_PAUSE) {
        pending_reason = s_cmd.reason;
    }
    portEXIT_CRITICAL(&s_mux);

    if (cur == MC_STATE_PAUSED) {
        if (resume_pending) {
            (void)ms_cancel_cmd(MS_CMD_RESUME); /* 语音又活跃：撤销排队中的显式续播 */
        }
        portENTER_CRITICAL(&s_mux);
        s_pause_reason = ms_policy_pause_reason_merge(s_pause_reason, reason);
        portEXIT_CRITICAL(&s_mux);
        return ESP_OK;
    }
    const uint8_t merged = ms_policy_pause_reason_merge(pending_reason, reason);
    if (play_pending) {
        /* 起播/切歌命令还没执行（cur 可能是 IDLE/STOPPED/PLAYING/PAUSED）：
         * 记下暂停原因，起播成功后补做，避免新曲目在语音期间出声。 */
        portENTER_CRITICAL(&s_mux);
        s_defer_pause_reason = ms_policy_pause_reason_merge(s_defer_pause_reason, merged);
        portEXIT_CRITICAL(&s_mux);
        return ESP_OK;
    }
    if (cur != MC_STATE_PLAYING) {
        return ESP_ERR_INVALID_STATE;
    }

    ms_schedule(MS_CMD_PAUSE, merged, (uint16_t)fade_ms, 0, 0, MS_CMD_DELAY_US);
    return ESP_OK;
}

static esp_err_t ms_resume_internal(void)
{
    mc_state_t cur;
    bool pause_pending;

    portENTER_CRITICAL(&s_mux);
    cur = s_state;
    pause_pending = (s_cmd_valid && s_cmd.kind == MS_CMD_PAUSE);
    portEXIT_CRITICAL(&s_mux);

    if (cur == MC_STATE_PLAYING && pause_pending) {
        /* 暂停淡出还没开始：用户继续 → 直接撤销暂停命令 */
        (void)ms_cancel_cmd(MS_CMD_PAUSE);
        portENTER_CRITICAL(&s_mux);
        s_defer_pause_reason = MS_PAUSE_REASON_NONE;
        s_pause_reason = MS_PAUSE_REASON_NONE;
        portEXIT_CRITICAL(&s_mux);
        return ESP_OK;
    }
    if (cur != MC_STATE_PAUSED) {
        return ESP_ERR_INVALID_STATE;
    }
    if (pause_pending) {
        (void)ms_cancel_cmd(MS_CMD_PAUSE); /* 暂停已执行，排队的重复暂停无意义 */
    }
    /* 语音活跃（提示音/THINKING/SPEAKING/FOLLOW_UP）：此刻续播会占用单作业播放器，
     * 紧接着的 TTS 会 start 失败（真机实测 VS: tts-fail）。先排队，语音回 IDLE 再续播。 */
    if (s_voice_active) {
        portENTER_CRITICAL(&s_mux);
        s_defer_resume = true;
        portEXIT_CRITICAL(&s_mux);
        ESP_LOGI(TAG, "语音活跃中收到 resume → 排队到语音回 IDLE 后续播");
        return ESP_OK;
    }
    ms_schedule(MS_CMD_RESUME, MS_REASON_USER_RESUME, 0, 0, 0, MS_CMD_DELAY_US);
    return ESP_OK;
}

static esp_err_t ms_stop_internal(void)
{
    mc_state_t cur;
    bool pending;

    portENTER_CRITICAL(&s_mux);
    cur = s_state;
    pending = s_cmd_valid;
    portEXIT_CRITICAL(&s_mux);

    if (cur == MC_STATE_IDLE && !audio_player_music_active() && !pending) {
        return ESP_ERR_INVALID_STATE;
    }
    portENTER_CRITICAL(&s_mux);
    s_track_pending = false;
    s_defer_pause_reason = MS_PAUSE_REASON_NONE;
    s_defer_resume = false;
    portEXIT_CRITICAL(&s_mux);
    ms_schedule(MS_CMD_STOP, MS_REASON_STOP, 0, 0, 0, MS_CMD_DELAY_US);
    return ESP_OK;
}

/* ---------------- 语音抢占（event_bus，evt 任务上下文） ---------------- */

static void ms_on_voice_state(void *arg, app_evt_t evt, const app_evt_data_t *data)
{
    (void)arg;
    (void)evt;
    const uint8_t to = (data != NULL) ? data->voice_state.to : 0;
    const bool voice_active = ms_policy_voice_active(to);
    mc_state_t cur;
    uint8_t reason;

    portENTER_CRITICAL(&s_mux);
    s_voice_active = voice_active;
    cur = s_state;
    reason = s_pause_reason;
    portEXIT_CRITICAL(&s_mux);

    if (voice_active) {
        /* 新的一轮开始（LISTENING/FOLLOW_UP）才撤回排队中的续播；THINKING/SPEAKING
         * 期间必须保留——"继续"指令正是在 THINKING 到达的（真机首跑：每次状态迁移都清，
         * 排队续播永远丢失）。 */
        if (to == 1 /* LISTENING */ || to == 4 /* FOLLOW_UP */) {
            portENTER_CRITICAL(&s_mux);
            s_defer_resume = false;
            portEXIT_CRITICAL(&s_mux);
        }
        if (ms_policy_auto_pause(cur, reason, to, CONFIG_MS_AUTO_PAUSE_ON_VOICE)) {
            ESP_LOGI(TAG, "语音进入活跃态（to=%u）→ auto-pause（抢占让路）", (unsigned)to);
            (void)ms_pause_internal(MS_PAUSE_REASON_VOICE, CONFIG_MS_PAUSE_FADE_MS);
        } else if (cur == MC_STATE_PAUSED && reason == MS_PAUSE_REASON_VOICE) {
            (void)ms_cancel_cmd(MS_CMD_RESUME);
        }
        return;
    }

    /* 语音回 IDLE：先撤销所有"因语音而排队的暂停"（可能发生在切歌/起播进行中），
     * 否则起播成功后会补一次永远不会被恢复的暂停。 */
    portENTER_CRITICAL(&s_mux);
    if (s_defer_pause_reason == MS_PAUSE_REASON_VOICE) {
        s_defer_pause_reason = MS_PAUSE_REASON_NONE;
    }
    const bool cancel_voice_pause = (s_cmd_valid && s_cmd.kind == MS_CMD_PAUSE &&
                                     s_cmd.reason == MS_PAUSE_REASON_VOICE);
    portEXIT_CRITICAL(&s_mux);
    if (cancel_voice_pause) {
        (void)ms_cancel_cmd(MS_CMD_PAUSE);
    }

    /* 语音期间排队的曲目优先于"恢复旧曲目"：点歌/下一首在 TTS 结束后才起播 */
    bool pending_track;
    bool defer_resume;
    portENTER_CRITICAL(&s_mux);
    pending_track = s_track_pending;
    defer_resume = s_defer_resume;
    if (pending_track) {
        s_track_pending = false;
        defer_resume = false; /* 新曲目会直接起播，不需要再恢复旧曲目 */
    }
    s_defer_resume = false;
    portEXIT_CRITICAL(&s_mux);
    if (!pending_track && defer_resume) {
        portENTER_CRITICAL(&s_mux);
        const mc_state_t st = s_state;
        portEXIT_CRITICAL(&s_mux);
        if (st == MC_STATE_PAUSED) {
            ESP_LOGI(TAG, "语音回 IDLE → 执行排队中的续播");
            ms_schedule(MS_CMD_RESUME, MS_REASON_USER_RESUME, 0, 0, 0, MS_CMD_DELAY_US);
        }
    }
    if (pending_track) {
        portENTER_CRITICAL(&s_mux);
        const mc_state_t st = s_state;
        portEXIT_CRITICAL(&s_mux);
        const bool need_stop =
            audio_player_music_active() || st == MC_STATE_PLAYING || st == MC_STATE_PAUSED;
        ESP_LOGI(TAG, "语音回 IDLE → 起播排队的曲目（mid=%u）", (unsigned)mc_current_mid());
        ms_schedule(MS_CMD_PLAY, MS_REASON_START, 0, need_stop ? 1 : 0, 0, MS_CMD_DELAY_US);
        return;
    }

    /* T4.4（用户 2026-11-11 定稿）：语音回 IDLE **不再自动恢复**音乐——打断后音乐保持暂停，
     * 等用户明确说"继续"（`music_control{resume}`）或点新歌时再动。
     * 这里唯一保留的是"语音期间排队的显式续播"（上面 defer_resume 分支）。 */
    ESP_LOGD(TAG, "语音回 IDLE：音乐保持暂停（T4.4 pause 模式，不自动恢复）");
}

/* ---------------- 对外 API ---------------- */

esp_err_t ms_init(void)
{
    esp_err_t err;

    if (s_inited) {
        return ESP_ERR_INVALID_STATE;
    }

    portENTER_CRITICAL(&s_mux);
    s_state = MC_STATE_IDLE;
    s_pause_reason = MS_PAUSE_REASON_NONE;
    s_have_mid = false;
    s_mid = 0;
    s_duration_ms = 0;
    s_url[0] = '\0';
    s_title[0] = '\0';
    s_progress_base_frames = 0;
    s_progress_ms = 0;
    s_state_since_us = esp_timer_get_time();
    s_voice_active = false;
    s_resume_watch = false;
    s_resume_watch_frames = 0;
    s_fallback_sent = false;
    s_track_pending = false;
    s_defer_pause_reason = MS_PAUSE_REASON_NONE;
    s_req_id = 0;
    memset(&s_stats, 0, sizeof(s_stats));
    memset(&s_cmd, 0, sizeof(s_cmd));
    s_cmd_valid = false;
    portEXIT_CRITICAL(&s_mux);
    s_play_started_us = 0;

    err = mc_register_cb(ms_mc_cb, NULL);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "注册 music_cloud 回调失败: %s（ms_init 必须在 mc_init 之后）",
                 esp_err_to_name(err));
        return err;
    }
    err = app_evt_register(APP_EVT_VOICE_STATE_CHANGED, ms_on_voice_state, NULL);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "订阅 VOICE_STATE_CHANGED 失败: %s", esp_err_to_name(err));
        (void)mc_register_cb(NULL, NULL);
        return err;
    }

    const esp_timer_create_args_t play_args = {
        .callback = ms_play_timer_cb,
        .arg = NULL,
        .name = "ms_play",
    };
    err = esp_timer_create(&play_args, &s_play_timer);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "创建起播定时器失败: %s", esp_err_to_name(err));
        goto fail_evt;
    }
    const esp_timer_create_args_t status_args = {
        .callback = ms_status_timer_cb,
        .arg = NULL,
        .name = "ms_status",
    };
    err = esp_timer_create(&status_args, &s_status_timer);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "创建上报定时器失败: %s", esp_err_to_name(err));
        goto fail_play;
    }
    err = esp_timer_start_periodic(s_status_timer,
                                   (uint64_t)CONFIG_MS_STATUS_PERIOD_MS * 1000ull);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "启动上报定时器失败: %s", esp_err_to_name(err));
        goto fail_status;
    }

    s_inited = true;
    ESP_LOGI(TAG,
             "ready（上报 %d ms，起播重试 %d ms × %d，暂停淡出 %d ms；不建任务，两个 esp_timer）",
             CONFIG_MS_STATUS_PERIOD_MS, CONFIG_MS_PLAY_RETRY_MS, CONFIG_MS_PLAY_MAX_RETRIES,
             CONFIG_MS_PAUSE_FADE_MS);
    return ESP_OK;

fail_status:
    (void)esp_timer_delete(s_status_timer);
    s_status_timer = NULL;
fail_play:
    (void)esp_timer_delete(s_play_timer);
    s_play_timer = NULL;
fail_evt:
    (void)app_evt_unregister(APP_EVT_VOICE_STATE_CHANGED, ms_on_voice_state, NULL);
    (void)mc_register_cb(NULL, NULL);
    return err;
}

esp_err_t ms_play(const char *url)
{
    if (!s_inited) {
        return ESP_ERR_INVALID_STATE;
    }
    if (url == NULL || url[0] == '\0') {
        return ESP_ERR_INVALID_ARG;
    }
    const size_t len = strlen(url);
    if (len >= (size_t)MS_URL_MAX) {
        return ESP_ERR_INVALID_SIZE;
    }
    if (strncmp(url, "http://", 7) != 0 && strncmp(url, "https://", 8) != 0) {
        return ESP_ERR_INVALID_ARG;
    }

    const uint32_t base_frames = ms_player_played_frames();
    mc_state_t cur;
    bool active = audio_player_music_active();

    portENTER_CRITICAL(&s_mux);
    memcpy(s_url, url, len + 1);
    s_title[0] = '\0';
    s_duration_ms = 0;
    s_progress_base_frames = base_frames;
    s_progress_ms = 0;
    s_resume_watch = false;
    s_fallback_sent = false;
    s_track_pending = false;
    s_defer_pause_reason = MS_PAUSE_REASON_NONE;
    cur = s_state;
    portEXIT_CRITICAL(&s_mux);

    active = active || cur == MC_STATE_PLAYING || cur == MC_STATE_PAUSED;
    ESP_LOGI(TAG, "ms play（%s，%u B）", active ? "切歌" : "起播", (unsigned)len);
    ms_schedule(MS_CMD_PLAY, MS_REASON_START, 0, active ? 1 : 0, 0, MS_CMD_DELAY_US);
    return ESP_OK;
}

esp_err_t ms_pause(uint32_t fade_ms)
{
    if (!s_inited) {
        return ESP_ERR_INVALID_STATE;
    }
    if (fade_ms == 0) {
        fade_ms = CONFIG_MS_PAUSE_FADE_MS;
    } else if (fade_ms > MS_FADE_MAX_MS) {
        fade_ms = MS_FADE_MAX_MS;
    }
    return ms_pause_internal(MS_PAUSE_REASON_USER, fade_ms);
}

esp_err_t ms_resume(void)
{
    if (!s_inited) {
        return ESP_ERR_INVALID_STATE;
    }
    return ms_resume_internal();
}

esp_err_t ms_stop(void)
{
    if (!s_inited) {
        return ESP_ERR_INVALID_STATE;
    }
    return ms_stop_internal();
}

esp_err_t ms_request(uint8_t action, int32_t index)
{
    if (!s_inited) {
        return ESP_ERR_INVALID_STATE;
    }
    if (action != MC_REQUEST_ACTION_PLAY) {
        return ESP_ERR_INVALID_ARG;
    }
    return mc_send_request(action, ms_next_req_id(), index);
}

mc_state_t ms_get_state(void)
{
    mc_state_t state;
    portENTER_CRITICAL(&s_mux);
    state = s_state;
    portEXIT_CRITICAL(&s_mux);
    return state;
}

const char *ms_state_name(mc_state_t state)
{
    return mc_state_name(state);
}

esp_err_t ms_info_read(ms_info_t *out)
{
    if (out == NULL) {
        return ESP_ERR_INVALID_ARG;
    }
    if (!s_inited) {
        return ESP_ERR_INVALID_STATE;
    }
    const uint32_t progress = ms_progress_from_player();

    portENTER_CRITICAL(&s_mux);
    out->state = s_state;
    out->paused = (s_state == MC_STATE_PAUSED);
    out->have_mid = s_have_mid;
    out->pause_reason = s_pause_reason;
    out->mid = s_have_mid ? s_mid : 0;
    out->duration_ms = s_duration_ms;
    memcpy(out->url, s_url, sizeof(out->url));
    memcpy(out->title, s_title, sizeof(out->title));
    s_progress_ms = progress;
    portEXIT_CRITICAL(&s_mux);
    out->progress_ms = progress;
    return ESP_OK;
}

esp_err_t ms_stats_read(ms_stats_t *out)
{
    if (out == NULL) {
        return ESP_ERR_INVALID_ARG;
    }
    portENTER_CRITICAL(&s_mux);
    *out = s_stats;
    portEXIT_CRITICAL(&s_mux);
    return ESP_OK;
}

esp_err_t ms_stats_reset(void)
{
    portENTER_CRITICAL(&s_mux);
    memset(&s_stats, 0, sizeof(s_stats));
    portEXIT_CRITICAL(&s_mux);
    return ESP_OK;
}
