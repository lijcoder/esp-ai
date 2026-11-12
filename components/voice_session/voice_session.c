/*
 * voice_session —— 语音会话状态机（L4 业务编排）。
 *
 * 设计要点（为什么这样写）：
 *   1. **不新建任务**：T2.4 实测 wss 握手期内部堆最低水位只剩 32.7KB，ARCH §9.3 原计划的
 *      4KB `voice_session` 任务会击穿 CONVENTIONS §4 的 "≥30KB" 铁律。状态机由事件驱动：
 *      event_bus 的 `evt` 任务、voice_cloud 的 `ws_client`/`vc` 任务、`player` 完成回调、
 *      console 任务都是触发点，handler 只做"迁移 + 非阻塞调用"。
 *   2. **状态迁移集中在 vs_transition()/vs_begin_session()**：跨任务并发靠临界区做 CAS
 *      （期望状态 → 目标状态），不匹配就丢弃（迟到事件/重复完成回调天然幂等）。
 *   3. **sid 由本组件分配**（PROTOCOL §3.3）：uint16 自增、0 保留给"未分配"，通过
 *      `vc_start(sid)` 交给 voice_cloud；voice_cloud 不再为产品路径自增 sid。
 *   4. **超时策略**：THINKING 的 10s 计时在 voice_cloud 的协议超时里（`VC_RESPONSE_TIMEOUT_MS`），
 *      超时后由 `VC_EVT_ROUND_END(TIMEOUT)` 通知本组件 → ERROR。避免两套计时器互相打架。
 *   5. **R1/R2**：T3.2 起暂停/恢复收音统一走 `audio_fe` 的原因位仲裁
 *      （PROMPT / TTS 两个 owner，位图归零才真正开麦），提示音与 TTS 不再互相覆盖；
 *      唤醒提示音 R1/R2 在本组件，TTS 的 R1/R2 在 voice_cloud。
 *   6. **R4/R5**：`vs_interrupt()`（按键事件或 console）→ player 淡出 → `vc_interrupt()`
 *      发 interrupt 报文 → 回 IDLE；恢复收音前设 50ms 丢弃窗口（R5）。
 *   7. **R3**：音乐播放中唤醒不再丢弃，先把音乐淡出让出单输出通道（混音叠加归 T3.3/P4）。
 */
#include "voice_session.h"

#include <string.h>

#include "audio_fe.h"
#include "audio_player.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "event_bus.h"
#include "feedback.h"
#include "freertos/FreeRTOS.h"
#include "prompt_assets.h"
#include "sdkconfig.h"
#include "voice_cloud.h"

static const char *TAG = "VS";

/* 以下全部由 s_mux 保护（状态/统计/sid）；isr 里不访问 */
static bool s_inited;
static vs_state_t s_state = VS_STATE_IDLE;
static uint16_t s_sid;
static uint16_t s_next_sid;
static int64_t s_state_since_us;
static bool s_prompt_playing; /* LISTENING 阶段应答提示音在播（R1 由本组件恢复） */
static vs_stats_t s_stats;
static bool s_followup_enabled = CONFIG_VS_FOLLOWUP_ENABLE; /* 运行期开关（T3.7） */
static portMUX_TYPE s_mux = portMUX_INITIALIZER_UNLOCKED;

/* ERROR 状态停留（T3.6 播错误提示音用；=0 时立即回 IDLE，不创建计时器） */
#if CONFIG_VS_ERROR_HOLD_MS > 0
static esp_timer_handle_t s_error_timer;
#endif

/* ---- T3.7 连续对话：续听窗口（一次性 esp_timer，不新建任务） ---- */
#if CONFIG_VS_FOLLOWUP_ENABLE
static esp_timer_handle_t s_followup_timer;
static int64_t s_followup_deadline_us; /* s_mux 保护 */
static uint32_t s_followup_rounds;     /* 本次会话已用续听轮数，s_mux 保护 */
static bool s_followup_confirming;     /* VAD 人声确认中（s_mux 保护） */
static void vs_followup_stop(void);
static void vs_start_followup_round(void);
#endif

/* 前向声明：vs_transition 需要按目标/来源停止/启动续听窗口计时器；
 * vs_enter_error 被续听起轮失败等早于其定义的路径调用（T3.6） */
static bool vs_transition(vs_state_t expect, vs_state_t to, vs_reason_t reason);
static void vs_enter_error(fb_kind_t kind, int32_t code);

/* ---------------- 状态迁移 ---------------- */

const char *vs_state_name(vs_state_t state)
{
    switch (state) {
    case VS_STATE_IDLE: return "IDLE";
    case VS_STATE_LISTENING: return "LISTENING";
    case VS_STATE_THINKING: return "THINKING";
    case VS_STATE_SPEAKING: return "SPEAKING";
    case VS_STATE_ERROR: return "ERROR";
    case VS_STATE_FOLLOW_UP: return "FOLLOW_UP";
    default: return "UNKNOWN";
    }
}

const char *vs_reason_name(vs_reason_t reason)
{
    switch (reason) {
    case VS_REASON_NONE: return "none";
    case VS_REASON_WAKE: return "wake";
    case VS_REASON_START: return "start";
    case VS_REASON_UPLINK_END: return "uplink-end";
    case VS_REASON_TTS_START: return "tts-start";
    case VS_REASON_TTS_END: return "tts-end";
    case VS_REASON_STREAM_DONE: return "stream-done";
    case VS_REASON_ROUND_DONE: return "round-done";
    case VS_REASON_CLOUD_ERROR: return "cloud-error";
    case VS_REASON_TIMEOUT: return "timeout";
    case VS_REASON_LINK_LOST: return "link-lost";
    case VS_REASON_INTERRUPT: return "interrupt";
    case VS_REASON_NET_DOWN: return "net-down";
    case VS_REASON_CLOUD_NOT_READY: return "cloud-not-ready";
    case VS_REASON_PROMPT_FAILED: return "prompt-failed";
    case VS_REASON_START_FAILED: return "start-failed";
    case VS_REASON_ERROR_DONE: return "error-done";
    case VS_REASON_ROUND_ABORT: return "round-abort";
    case VS_REASON_FOLLOWUP_SPEECH: return "followup-speech";
    case VS_REASON_FOLLOWUP_TIMEOUT: return "followup-timeout";
    case VS_REASON_FOLLOWUP_MAX: return "followup-max";
    case VS_REASON_FOLLOWUP_WAKE: return "followup-wake";
    case VS_REASON_FOLLOWUP_OFF: return "followup-off";
    case VS_REASON_NET_OFFLINE: return "net-offline";
    case VS_REASON_TTS_FAIL: return "tts-fail";
    case VS_REASON_MUSIC_START: return "music-start";
    default: return "unknown";
    }
}

/* ---------------- T3.7 连续对话（续听窗口） ---------------- */

#if CONFIG_VS_FOLLOWUP_ENABLE
static void vs_followup_arm(void)
{
    portENTER_CRITICAL(&s_mux);
    s_followup_deadline_us = esp_timer_get_time() + (int64_t)CONFIG_VS_FOLLOWUP_WINDOW_MS * 1000;
    s_followup_confirming = false;
    portEXIT_CRITICAL(&s_mux);
    esp_timer_stop(s_followup_timer);
    esp_err_t err = esp_timer_start_once(s_followup_timer, (uint64_t)CONFIG_VS_FOLLOWUP_WINDOW_MS * 1000);
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "续听窗口计时启动失败: %s", esp_err_to_name(err));
    }
    /* 窗口一开就开始预录：续听轮要等人声确认（250ms + VAD 判定延迟）才起轮，
     * 若到起轮才开上行，用户这句话的开头会被整段丢掉（T3.7 缺陷，2026-11-03 修）。 */
    err = vc_preroll_start();
    if (err != ESP_OK) {
        ESP_LOGD(TAG, "续听预录未启动（%s），续听轮可能丢起始语音", esp_err_to_name(err));
    }
}

static void vs_followup_stop(void)
{
    if (s_followup_timer != NULL) {
        esp_timer_stop(s_followup_timer);
    }
    portENTER_CRITICAL(&s_mux);
    s_followup_deadline_us = 0;
    s_followup_confirming = false;
    portEXIT_CRITICAL(&s_mux);
    vc_preroll_stop();
}

static void vs_followup_timer_cb(void *arg)
{
    (void)arg;
    bool confirming = false;
    portENTER_CRITICAL(&s_mux);
    confirming = s_followup_confirming;
    s_followup_confirming = false;
    if (!confirming) {
        s_followup_deadline_us = 0;
    }
    portEXIT_CRITICAL(&s_mux);

    if (confirming) {
        /* 确认期满：VAD 人声持续了 confirm_ms，真正起轮 */
        ESP_LOGD(TAG, "续听人声确认通过（%d ms）", CONFIG_VS_FOLLOWUP_CONFIRM_MS);
        vs_start_followup_round();
        return;
    }

    if (vs_transition(VS_STATE_FOLLOW_UP, VS_STATE_IDLE, VS_REASON_FOLLOWUP_TIMEOUT)) {
        portENTER_CRITICAL(&s_mux);
        s_stats.followup_timeouts++;
        portEXIT_CRITICAL(&s_mux);
        ESP_LOGI(TAG, "续听窗口超时（%d ms 无语音）→ IDLE", CONFIG_VS_FOLLOWUP_WINDOW_MS);
    }
}

/* 窗口内 VAD 人声开始：先确认 CONFIG_VS_FOLLOWUP_CONFIRM_MS，再起轮（抗短噪声） */
static void vs_followup_on_voice_start(void)
{
#if CONFIG_VS_FOLLOWUP_CONFIRM_MS > 0
    int64_t remain_us;
    portENTER_CRITICAL(&s_mux);
    if (s_followup_confirming) {
        portEXIT_CRITICAL(&s_mux);
        return; /* 已在确认中 */
    }
    s_followup_confirming = true;
    remain_us = s_followup_deadline_us - esp_timer_get_time();
    portEXIT_CRITICAL(&s_mux);
    if (remain_us <= (int64_t)CONFIG_VS_FOLLOWUP_CONFIRM_MS * 1000) {
        /* 窗口即将结束：直接起轮，不再确认 */
        portENTER_CRITICAL(&s_mux);
        s_followup_confirming = false;
        portEXIT_CRITICAL(&s_mux);
        vs_start_followup_round();
        return;
    }
    esp_timer_stop(s_followup_timer);
    esp_timer_start_once(s_followup_timer, (uint64_t)CONFIG_VS_FOLLOWUP_CONFIRM_MS * 1000);
#else
    vs_start_followup_round();
#endif
}

/* 确认期间 VAD 人声结束：取消确认，恢复剩余窗口计时 */
static void vs_followup_on_voice_end(void)
{
#if CONFIG_VS_FOLLOWUP_CONFIRM_MS > 0
    int64_t remain_us;
    portENTER_CRITICAL(&s_mux);
    if (!s_followup_confirming) {
        portEXIT_CRITICAL(&s_mux);
        return;
    }
    s_followup_confirming = false;
    remain_us = s_followup_deadline_us - esp_timer_get_time();
    portEXIT_CRITICAL(&s_mux);
    esp_timer_stop(s_followup_timer);
    if (remain_us <= 0) {
        if (vs_transition(VS_STATE_FOLLOW_UP, VS_STATE_IDLE, VS_REASON_FOLLOWUP_TIMEOUT)) {
            portENTER_CRITICAL(&s_mux);
            s_stats.followup_timeouts++;
            portEXIT_CRITICAL(&s_mux);
        }
        return;
    }
    esp_timer_start_once(s_followup_timer, (uint64_t)remain_us);
#endif
}
#else
static void vs_followup_arm(void) {}
static void vs_followup_stop(void) {}
#endif

/* 迁移完成后的日志 + 事件广播（在临界区外做，避免在关中断时 printf） */
static void vs_notify(vs_state_t from, vs_state_t to, vs_reason_t reason, uint16_t sid,
                      uint32_t dur_ms)
{
    ESP_LOGI(TAG, "state %s -> %s（sid=%u，停留 %u ms，原因=%s）", vs_state_name(from),
             vs_state_name(to), (unsigned)sid, (unsigned)dur_ms, vs_reason_name(reason));

    app_evt_data_t data;
    memset(&data, 0, sizeof(data));
    data.voice_state.sid = sid;
    data.voice_state.from = (uint8_t)from;
    data.voice_state.to = (uint8_t)to;
    data.voice_state.reason = (uint8_t)reason;
    data.voice_state.dur_ms = dur_ms;
    esp_err_t err = app_evt_post(APP_EVT_VOICE_STATE_CHANGED, &data);
    if (err != ESP_OK) {
        ESP_LOGD(TAG, "投递 VOICE_STATE_CHANGED 失败: %s", esp_err_to_name(err));
    }
}

/* 期望状态匹配才迁移（CAS）；返回是否真的迁移了 */
static bool vs_transition(vs_state_t expect, vs_state_t to, vs_reason_t reason)
{
    const int64_t now = esp_timer_get_time();
    vs_state_t from;
    uint16_t sid;
    uint32_t dur_ms;

    portENTER_CRITICAL(&s_mux);
    if (s_state != expect) {
        portEXIT_CRITICAL(&s_mux);
        return false;
    }
    from = s_state;
    sid = s_sid;
    dur_ms = (uint32_t)((now - s_state_since_us) / 1000);
    s_state = to;
    s_state_since_us = now;
    s_stats.transitions++;
    s_stats.last_sid = sid;
    s_stats.last_state_ms = dur_ms;
    s_stats.state = (uint8_t)to;
    if (to == VS_STATE_ERROR) {
        s_stats.rounds_errored++;
    }
    portEXIT_CRITICAL(&s_mux);

    /* 离开 FOLLOW_UP 时停窗；进入 FOLLOW_UP 时开窗（T3.7） */
    if (from == VS_STATE_FOLLOW_UP) {
        vs_followup_stop();
    }
    vs_notify(from, to, reason, sid, dur_ms);
    if (to == VS_STATE_FOLLOW_UP) {
        vs_followup_arm();
#if CONFIG_VS_FOLLOWUP_ENABLE
        ESP_LOGI(TAG, "续听窗口开启（%d ms，静音超时/轮数上限/按键退出）",
                 CONFIG_VS_FOLLOWUP_WINDOW_MS);
#endif
    }
    return true;
}

/* 开始新一轮（IDLE → LISTENING；T3.7 也用于 FOLLOW_UP → LISTENING 重新唤醒） */
static bool vs_begin_session(vs_state_t expect, vs_reason_t reason, uint16_t *sid_out)
{
    const int64_t now = esp_timer_get_time();
    uint16_t sid;

    portENTER_CRITICAL(&s_mux);
    if (!s_inited || s_state != expect) {
        portEXIT_CRITICAL(&s_mux);
        return false;
    }
    s_next_sid++;
    if (s_next_sid == 0) {
        s_next_sid = 1; /* 0 保留给"未分配" */
    }
    sid = s_next_sid;
    s_sid = sid;
    s_prompt_playing = false;
#if CONFIG_VS_FOLLOWUP_ENABLE
    s_followup_rounds = 0; /* 重新唤醒：连续对话计数清零 */
#endif
    s_state = VS_STATE_LISTENING;
    s_state_since_us = now;
    s_stats.transitions++;
    s_stats.rounds_started++;
    s_stats.last_sid = sid;
    s_stats.last_state_ms = 0;
    s_stats.state = (uint8_t)VS_STATE_LISTENING;
    portEXIT_CRITICAL(&s_mux);

    /* 重新唤醒/按键起轮：上一续听窗口若还在预录，丢掉它（本轮要从提示音之后重新收音） */
    vc_preroll_stop();

    vs_notify(expect, VS_STATE_LISTENING, reason, sid, 0);
    if (sid_out != NULL) {
        *sid_out = sid;
    }
    return true;
}

/* T3.7：续听窗口内检测到人声 → FOLLOW_UP → LISTENING，直接起下一轮（不播提示音、不唤醒） */
static bool vs_begin_followup_round(uint16_t *sid_out)
{
#if CONFIG_VS_FOLLOWUP_ENABLE
    const int64_t now = esp_timer_get_time();
    uint16_t sid;

    portENTER_CRITICAL(&s_mux);
    if (!s_inited || s_state != VS_STATE_FOLLOW_UP) {
        portEXIT_CRITICAL(&s_mux);
        return false;
    }
    s_next_sid++;
    if (s_next_sid == 0) {
        s_next_sid = 1;
    }
    sid = s_next_sid;
    s_sid = sid;
    s_followup_rounds++;
    s_state = VS_STATE_LISTENING;
    s_state_since_us = now;
    s_stats.transitions++;
    s_stats.rounds_started++;
    s_stats.followup_rounds++;
    s_stats.followup_hits++;
    s_stats.last_sid = sid;
    s_stats.last_state_ms = 0;
    s_stats.state = (uint8_t)VS_STATE_LISTENING;
    portEXIT_CRITICAL(&s_mux);

    vs_notify(VS_STATE_FOLLOW_UP, VS_STATE_LISTENING, VS_REASON_FOLLOWUP_SPEECH, sid, 0);
    if (sid_out != NULL) {
        *sid_out = sid;
    }
    return true;
#else
    (void)sid_out;
    return false;
#endif
}

/* T3.7：确认通过后真正起一个续听轮（不播提示音） */
static void vs_start_followup_round(void)
{
#if CONFIG_VS_FOLLOWUP_ENABLE
    uint16_t sid;
    if (!vs_begin_followup_round(&sid)) {
        return;
    }
    ESP_LOGI(TAG, "续听窗口内检测到人声 → 起续听轮（sid=%u，不播提示音）", (unsigned)sid);
    esp_err_t err = vc_start(sid);
    if (err != ESP_OK) {
        vc_preroll_stop(); /* 起轮失败：预录没人接管，立刻停掉释放上行接口 */
        ESP_LOGW(TAG, "续听起轮失败（sid=%u）: %s（网络未就绪 → 网络未连接反馈）",
                 (unsigned)sid, esp_err_to_name(err));
        vs_enter_error(FB_KIND_NET_OFFLINE, 0);
    }
#endif
}

/*
 * 一轮正常播完：进入续听窗口（T3.7）或回 IDLE。
 * ok=false（播放被打断/失败）不续听，直接回 IDLE。
 */
static void vs_round_completed(vs_reason_t reason, bool ok)
{
    int entered = -1;
#if CONFIG_VS_FOLLOWUP_ENABLE
    /* 音乐已经在出声：续听窗口没有意义（麦克风只会拾取音乐），直接回 IDLE。
     * 排队的曲目/续播在 SPEAKING→FOLLOW_UP 时由 music_session 提前起播，
     * 起播后本组件会收到 MUSIC_STATE_CHANGED(PLAYING) 再退出续听（见 vs_on_app_evt）。 */
    const bool music_playing =
        audio_player_music_active() && !audio_player_music_paused();
    if (ok && s_followup_enabled && vs_get_state() == VS_STATE_SPEAKING && !music_playing) {
        uint32_t used;
        portENTER_CRITICAL(&s_mux);
        used = s_followup_rounds;
        portEXIT_CRITICAL(&s_mux);
        if (used >= CONFIG_VS_FOLLOWUP_MAX_ROUNDS) {
            portENTER_CRITICAL(&s_mux);
            s_stats.followup_max_exits++;
            portEXIT_CRITICAL(&s_mux);
            entered = 1;
            ESP_LOGI(TAG, "连续对话达到轮数上限（%d 轮）→ 回 IDLE",
                     CONFIG_VS_FOLLOWUP_MAX_ROUNDS);
        } else if (vs_transition(VS_STATE_SPEAKING, VS_STATE_FOLLOW_UP, reason)) {
            entered = 0;
        }
    }
#else
    (void)ok;
#endif

    if (entered == 0) {
        portENTER_CRITICAL(&s_mux);
        s_stats.rounds_completed++;
        portEXIT_CRITICAL(&s_mux);
        return;
    }
    if (vs_get_state() != VS_STATE_SPEAKING) {
        return;
    }
    (void)vs_transition(VS_STATE_SPEAKING, VS_STATE_IDLE,
                        entered == 1 ? VS_REASON_FOLLOWUP_MAX : reason);
    portENTER_CRITICAL(&s_mux);
    s_stats.rounds_completed++;
    portEXIT_CRITICAL(&s_mux);
}

/* 任意活跃状态 → IDLE（打断/断链/云端取消；ERROR 状态不在这里收，由 ERROR 保持计时器收尾） */
static void vs_abort_to_idle(vs_reason_t reason)
{
    const vs_state_t st = vs_get_state();
    if (st == VS_STATE_IDLE || st == VS_STATE_ERROR) {
        return;
    }
    if (vs_transition(st, VS_STATE_IDLE, reason)) {
        ESP_LOGW(TAG, "会话中止（%s）→ IDLE", vs_reason_name(reason));
    }
}

/* ERROR 状态兜底看门狗（T3.6）：正常情况下提示音完成回调会先回 IDLE，
 * 这里只兜"提示音卡住/播放器异常/回调丢失"，避免状态机卡在 ERROR 且 R1 门控不释放。 */
#if CONFIG_VS_ERROR_HOLD_MS > 0
static void vs_error_timer_cb(void *arg)
{
    (void)arg;
    ESP_LOGW(TAG, "错误反馈看门狗超时（%d ms）→ 强制回 IDLE", CONFIG_VS_ERROR_HOLD_MS);
    portENTER_CRITICAL(&s_mux);
    s_stats.error_watchdog++;
    s_stats.error_feedback_fail++;
    portEXIT_CRITICAL(&s_mux);
    (void)fb_abort();
    (void)vs_transition(VS_STATE_ERROR, VS_STATE_IDLE, VS_REASON_ERROR_DONE);
}
#endif

/* fb_kind_t → 迁移原因（写日志/事件 payload 用） */
static vs_reason_t vs_reason_for_kind(fb_kind_t kind)
{
    switch (kind) {
    case FB_KIND_NET_OFFLINE: return VS_REASON_NET_OFFLINE;
    case FB_KIND_TIMEOUT:     return VS_REASON_TIMEOUT;
    case FB_KIND_TTS_FAIL:    return VS_REASON_TTS_FAIL;
    case FB_KIND_CLOUD_ERROR:
    default:                  return VS_REASON_CLOUD_ERROR;
    }
}

/*
 * 进入 ERROR：LISTENING/THINKING/SPEAKING → ERROR（T3.6）。
 * ARCH §7：ERROR 播错误提示音后回 IDLE、同轮不重试。提示音由 feedback 组件播放
 * （语音只是当前反馈通道；语义事件 APP_EVT_FEEDBACK 供未来屏幕/灯光通道订阅）；
 * 播放完成回调 vs_on_fb_done() 驱动 ERROR → IDLE；CONFIG_VS_ERROR_HOLD_MS 只作兜底。
 */
static void vs_enter_error(fb_kind_t kind, int32_t code)
{
    const vs_state_t st = vs_get_state();
    if (st != VS_STATE_LISTENING && st != VS_STATE_THINKING && st != VS_STATE_SPEAKING) {
        ESP_LOGD(TAG, "异常反馈忽略（state=%s，kind=%s）", vs_state_name(st),
                 fb_kind_name(kind));
        return;
    }

    const vs_reason_t reason = vs_reason_for_kind(kind);
    if (!vs_transition(st, VS_STATE_ERROR, reason)) {
        return;
    }

    portENTER_CRITICAL(&s_mux);
    s_stats.last_error_kind = (uint8_t)kind;
    s_stats.last_error_code = code;
    if (kind > FB_KIND_NONE && kind < FB_KIND_COUNT) {
        s_stats.error_by_kind[kind]++;
    }
    portEXIT_CRITICAL(&s_mux);

    ESP_LOGE(TAG, "会话进入 ERROR（%s，code=%d），本轮不自动重试", fb_kind_name(kind),
             (int)code);

    /* 防御：让协议侧也结束本轮（cloud error / 超时路径下 vc 多半已结束，cancel 幂等） */
    (void)vc_cancel();

    /* 交给 feedback：语音通道 + 语义事件（未来屏幕/灯光通道订阅事件） */
    const uint16_t sid = vs_current_sid();
    const esp_err_t ferr = fb_notify(kind, code, sid);
    if (ferr != ESP_OK) {
        ESP_LOGW(TAG, "错误反馈未启动（%s）→ 直接回 IDLE", esp_err_to_name(ferr));
        portENTER_CRITICAL(&s_mux);
        s_stats.error_feedback_fail++;
        portEXIT_CRITICAL(&s_mux);
        (void)vs_transition(VS_STATE_ERROR, VS_STATE_IDLE, VS_REASON_ERROR_DONE);
        return;
    }

    portENTER_CRITICAL(&s_mux);
    s_stats.error_feedback_plays++;
    portEXIT_CRITICAL(&s_mux);
    ESP_LOGI(TAG, "错误反馈（类别=%s，code=%d，sid=%u）→ 提示音（R1）", fb_kind_name(kind),
             (int)code, (unsigned)sid);

#if CONFIG_VS_ERROR_HOLD_MS > 0
    if (s_error_timer != NULL) {
        esp_timer_stop(s_error_timer);
        esp_timer_start_once(s_error_timer, (uint64_t)CONFIG_VS_ERROR_HOLD_MS * 1000);
    }
#endif
}

/* feedback 完成回调（player 任务上下文）：提示音播完/失败 → 结束 ERROR 状态 */
static void vs_on_fb_done(fb_kind_t kind, bool ok, uint32_t dur_ms, void *arg)
{
    (void)arg;

    portENTER_CRITICAL(&s_mux);
    if (ok) {
        s_stats.error_feedback_ok++;
    } else {
        s_stats.error_feedback_fail++;
    }
    s_stats.last_error_prompt_ms = dur_ms;
    portEXIT_CRITICAL(&s_mux);

    ESP_LOGI(TAG, "错误反馈完成（类别=%s，ok=%d，时长=%u ms）→ IDLE", fb_kind_name(kind),
             (int)ok, (unsigned)dur_ms);

#if CONFIG_VS_ERROR_HOLD_MS > 0
    if (s_error_timer != NULL) {
        esp_timer_stop(s_error_timer);
    }
#endif

    if (vs_get_state() == VS_STATE_ERROR) {
        (void)vs_transition(VS_STATE_ERROR, VS_STATE_IDLE, VS_REASON_ERROR_DONE);
    }
}

/* ---------------- voice_cloud 回调（ws_client / vc 任务上下文） ---------------- */

static void vs_on_round_end(vc_round_end_t result)
{
    switch (result) {
    case VC_ROUND_DONE:
        /* 正常收尾：有音频正在排空时等 STREAM_DONE；无音频可播直接结束本轮（可能进续听窗口） */
        if (vs_get_state() == VS_STATE_SPEAKING && !audio_player_stream_active()) {
            vs_round_completed(VS_REASON_ROUND_DONE, true);
        }
        return;

    case VC_ROUND_ABORT_TIMEOUT:
        portENTER_CRITICAL(&s_mux);
        s_stats.timeouts++;
        portEXIT_CRITICAL(&s_mux);
        vs_enter_error(FB_KIND_TIMEOUT, 0);
        break;

    case VC_ROUND_ABORT_ERROR:
        /* CLOUD_ERROR 事件先到时会带具体 code，这条用于兜底（幂等） */
        vs_enter_error(FB_KIND_CLOUD_ERROR, 0);
        break;

    case VC_ROUND_ABORT_LINK:
        /* 链路不可用（WS 断链）：对用户等同"网络未连接"，给本地提示音反馈（T3.6） */
        vs_enter_error(FB_KIND_NET_OFFLINE, 0);
        break;

    case VC_ROUND_ABORT_CANCEL:
    case VC_ROUND_ABORT_NEW:
    default:
        vs_abort_to_idle(VS_REASON_ROUND_ABORT);
        break;

    case VC_ROUND_ABORT_SETUP:
        /* 起轮失败：云端未 READY 时按"网络未连接"反馈；其它（编码器/上行占用）保持静默中止 */
        if (!vc_is_ready()) {
            vs_enter_error(FB_KIND_NET_OFFLINE, 0);
        } else {
            vs_abort_to_idle(VS_REASON_ROUND_ABORT);
        }
        break;
    }

    portENTER_CRITICAL(&s_mux);
    s_stats.round_aborts++;
    portEXIT_CRITICAL(&s_mux);
}

static void vs_on_vc_event(const vc_event_msg_t *msg, void *arg)
{
    (void)arg;
    if (msg == NULL) {
        return;
    }

    switch (msg->evt) {
    case VC_EVT_UPLINK_END:
        /* audio_end 已发出 = LISTENING 结束，进入 THINKING 等 ASR/TTS */
        if (vs_transition(VS_STATE_LISTENING, VS_STATE_THINKING, VS_REASON_UPLINK_END)) {
            ESP_LOGI(TAG, "上行结束（收尾=%d），等云端响应", (int)msg->code);
        }
        break;

    case VC_EVT_TTS_START:
        (void)vs_transition(VS_STATE_THINKING, VS_STATE_SPEAKING, VS_REASON_TTS_START);
        break;

    case VC_EVT_TTS_END:
        /* 不在这里回 IDLE：播放任务可能还在排空 ring（等 STREAM_DONE / ROUND_END） */
        break;

    case VC_EVT_CLOUD_ERROR:
        portENTER_CRITICAL(&s_mux);
        s_stats.cloud_errors++;
        portEXIT_CRITICAL(&s_mux);
        /* 按 PROTOCOL 错误码分流：3001/3002 = TTS 失败 → "语音播放失败"；其余 → "服务异常" */
        vs_enter_error(fb_kind_from_cloud_code(msg->code), msg->code);
        break;

    case VC_EVT_TTS_FAIL:
        /* 设备侧无法播放云端 TTS（codec 不支持/开流失败等）：给"语音播放失败"反馈 */
        ESP_LOGW(TAG, "设备侧 TTS 无法播放（原因=%d）→ 语音播放失败反馈", (int)msg->code);
        vs_enter_error(FB_KIND_TTS_FAIL, msg->code);
        break;

    case VC_EVT_ROUND_END:
        vs_on_round_end(msg->round_end);
        break;

    default:
        break;
    }
}

/* ---------------- 播放完成回调（player 任务上下文） ---------------- */

static void vs_on_play_done(audio_player_src_t src, bool ok, void *arg)
{
    (void)arg;

    if (src == AUDIO_PLAYER_SRC_TTS_STREAM) {
        /* TTS 流式播放结束（成功/失败/打断）：R2 恢复收音在 voice_cloud，本组件收状态。
         * T3.7：播放正常结束 → FOLLOW_UP 续听窗口；ok=false（打断/失败）直接回 IDLE。 */
        ESP_LOGI(TAG, "speaking done（ok=%d）", (int)ok);
        vs_round_completed(VS_REASON_STREAM_DONE, ok);
        return;
    }

    if (src != AUDIO_PLAYER_SRC_PCM) {
        return; /* 本地 MP3 探针等不涉及会话状态 */
    }

    portENTER_CRITICAL(&s_mux);
    const bool was_prompt = s_prompt_playing;
    s_prompt_playing = false;
    portEXIT_CRITICAL(&s_mux);
    if (!was_prompt) {
        return;
    }

    /* R2：唤醒应答播完恢复收音（T3.2 起走原因位仲裁，release 幂等）；
     * 日志文案与 T1.2b 之前的 APP 版本保持一致（测试脚本依赖）。
     * T3.6：若已进入 ERROR（如提示音期间断网），PROMPT 位由 feedback 的错误提示音
     * 完成回调释放，这里不能抢先 release，否则错误提示音播放期间麦克风会被打开。 */
    const bool error_owns_prompt = (vs_get_state() == VS_STATE_ERROR);
    if (!error_owns_prompt) {
        /* R5：提示音尾音（I2S DMA + 房间混响）不要进入第一帧上行 */
        audio_fe_feed_discard_ms(CONFIG_AUDIO_FE_RESUME_DISCARD_MS);
        audio_fe_feed_release(AUDIO_FE_HOLD_PROMPT);
        ESP_LOGI(TAG, "prompt done, feed resumed");
    } else {
        ESP_LOGW(TAG, "唤醒提示音播完但已进入 ERROR：R1 交给错误提示音完成回调释放");
    }

    if (vs_get_state() != VS_STATE_LISTENING) {
        return; /* 提示音期间已被打断/断网：本轮不再起 */
    }
    if (!ok) {
        ESP_LOGW(TAG, "唤醒应答播完但状态异常（ok=false），本轮放弃");
        (void)vs_transition(VS_STATE_LISTENING, VS_STATE_IDLE, VS_REASON_PROMPT_FAILED);
        return;
    }

    /* 提示音播完、收音已恢复，才是"用户开始说话"的起点：起一轮端云会话 */
    const uint16_t sid = vs_current_sid();
    esp_err_t err = vc_start(sid);
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "起轮失败（sid=%u）: %s（网络未就绪 → 网络未连接反馈）", (unsigned)sid,
                 esp_err_to_name(err));
        vs_enter_error(FB_KIND_NET_OFFLINE, 0);
    }
}

/* ---------------- event_bus 事件（evt 任务上下文） ---------------- */

static void vs_on_wake(void)
{
    ESP_LOGI(TAG, "wake detected");
    const vs_state_t prev = vs_get_state();
    const bool from_followup = (prev == VS_STATE_FOLLOW_UP);
    if (from_followup) {
        /* T3.7：续听窗口内再次唤醒 = 重开一轮（播应答提示音、续听计数清零） */
        ESP_LOGI(TAG, "续听窗口内再次唤醒 → 重开一轮（播应答提示音，续听计数清零）");
        vs_followup_stop();
    } else if (prev != VS_STATE_IDLE) {
        portENTER_CRITICAL(&s_mux);
        s_stats.wakes_dropped++;
        portEXIT_CRITICAL(&s_mux);
        ESP_LOGW(TAG, "会话进行中（%s），忽略本次唤醒", vs_state_name(vs_get_state()));
        return;
    }
    /* R3：音乐播放中唤醒**不丢弃**（麦克风本来就没关）。T4.3 起音乐暂停/恢复统一由
     * music_session 订阅 VOICE_STATE_CHANGED 处理（见 components/music_session）；
     * 这里只保留"播放器忙（提示音/TTS 收尾）→ 忽略唤醒"判断，不再淡出音乐。 */
    /* 只有"音乐正在出声"才允许在播放器忙时放行唤醒；音乐暂停时播放器忙 = 提示音/TTS 收尾 */
    const bool music_playing = audio_player_music_active() && !audio_player_music_paused();
    if (audio_player_is_busy() && !music_playing) {
        /* 提示音/TTS 收尾中：本地会话不插队（避免两路提示音叠在一起） */
        portENTER_CRITICAL(&s_mux);
        s_stats.wakes_dropped++;
        portEXIT_CRITICAL(&s_mux);
        ESP_LOGW(TAG, "播放器忙（提示音/TTS 收尾），忽略本次唤醒");
        return;
    }

    uint16_t sid;
    const vs_state_t expect = from_followup ? VS_STATE_FOLLOW_UP : VS_STATE_IDLE;
    const vs_reason_t reason = from_followup ? VS_REASON_FOLLOWUP_WAKE : VS_REASON_WAKE;
    if (!vs_begin_session(expect, reason, &sid)) {
        if (from_followup) {
            /* 兜底：重开失败时不能让状态机卡在无计时器的 FOLLOW_UP */
            vs_abort_to_idle(VS_REASON_ROUND_ABORT);
        }
        return;
    }

    /* T3.6：云端现在起不了轮（未联网/WS 未 READY）就不要先播"我在呢"再播"网络未连接"，
     * 直接给"网络未连接"反馈（也顺带确认了唤醒），避免两段提示音连播。 */
    if (!vc_is_ready()) {
        ESP_LOGW(TAG, "唤醒时云端未就绪（ws 未 READY）→ 网络未连接反馈（不播应答提示音）");
        vs_enter_error(FB_KIND_NET_OFFLINE, 0);
        return;
    }

    /* 音乐播放中唤醒不再在这里淡出：暂停/保持暂停归 music_session（T4.4 pause 模式）。 */

    const prompt_clip_t *wake_clip = prompt_assets_get(PROMPT_WAKE_OK);
    if (wake_clip == NULL || wake_clip->pcm == NULL || wake_clip->samples == 0) {
        /* T3.5 降级：assets pack 缺失/损坏 → 跳过"我在呢"，直接起轮（不能卡会话） */
        ESP_LOGW(TAG, "唤醒应答提示音不可用（assets pack 缺失/损坏）→ 跳过提示音直接起轮");
        const uint16_t fallback_sid = vs_current_sid();
        esp_err_t serr = vc_is_ready() ? vc_start(fallback_sid) : ESP_ERR_INVALID_STATE;
        if (serr != ESP_OK) {
            ESP_LOGW(TAG, "降级起轮失败: %s → 网络未连接反馈", esp_err_to_name(serr));
            vs_enter_error(FB_KIND_NET_OFFLINE, 0);
        }
        return;
    }

    /* R1：应答提示音播放期间暂停收音（原因位 PROMPT，与 TTS 的 R1 互不覆盖） */
    portENTER_CRITICAL(&s_mux);
    s_prompt_playing = true;
    portEXIT_CRITICAL(&s_mux);
    audio_fe_feed_hold(AUDIO_FE_HOLD_PROMPT);

    esp_err_t err = audio_player_play_pcm(wake_clip->pcm, wake_clip->samples,
                                          wake_clip->sample_rate);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "唤醒应答播放失败: %s", esp_err_to_name(err));
        portENTER_CRITICAL(&s_mux);
        s_prompt_playing = false;
        portEXIT_CRITICAL(&s_mux);
        audio_fe_feed_release(AUDIO_FE_HOLD_PROMPT);
        (void)vs_transition(VS_STATE_LISTENING, VS_STATE_IDLE, VS_REASON_PROMPT_FAILED);
    }
}

static void vs_on_net_down(const app_evt_data_t *data)
{
    const app_evt_net_down_t *down = (data != NULL) ? &data->net_down : NULL;
    const uint32_t reason_code = (down != NULL) ? down->reason : 0u;
    ESP_LOGW(TAG, "网络断开（reason=%u，重试 %u ms）→ 结束当前会话",
             (unsigned)reason_code, (unsigned)(down != NULL ? down->retry_ms : 0u));

    const vs_state_t st = vs_get_state();
    (void)vc_cancel();

    if (st == VS_STATE_LISTENING || st == VS_STATE_THINKING || st == VS_STATE_SPEAKING) {
        /* 会话中途断网（T3.6 用户拍板：也要出声）→ 网络未连接提示音 → IDLE */
        vs_enter_error(FB_KIND_NET_OFFLINE, (int32_t)reason_code);
        return;
    }
    if (st == VS_STATE_FOLLOW_UP) {
        /* 续听窗口按 T3.7 规则退出，随后补一声本地提示音（不进 ERROR：窗口本就要结束） */
        vs_followup_stop();
        (void)vs_transition(VS_STATE_FOLLOW_UP, VS_STATE_IDLE, VS_REASON_NET_DOWN);
        const esp_err_t ferr = fb_notify(FB_KIND_NET_OFFLINE, (int32_t)reason_code,
                                         vs_current_sid());
        if (ferr != ESP_OK) {
            ESP_LOGW(TAG, "断网反馈未启动（%s）", esp_err_to_name(ferr));
        }
        return;
    }
    if (st == VS_STATE_ERROR) {
        return; /* 已经在错误反馈中（如 TTS 播放中收到 error 后又断网），不重复提示 */
    }
    /* IDLE：无会话可中止；待机断网不出声（独立"断网状态反馈"仍归 PD-002） */
    ESP_LOGI(TAG, "待机状态网络断开，无会话可中止（不出声）");
}

/* T3.7：续听窗口内 VAD 判到人声 → 确认后起下一轮（不播提示音、不再唤醒） */
static void vs_on_vad_speech_start(void)
{
    if (vs_get_state() != VS_STATE_FOLLOW_UP) {
        ESP_LOGD(TAG, "VAD 人声开始（state=%s）", vs_state_name(vs_get_state()));
        return;
    }
#if CONFIG_VS_FOLLOWUP_ENABLE
    vs_followup_on_voice_start();
#else
    ESP_LOGD(TAG, "VAD 人声开始（FOLLOW_UP 未启用）");
#endif
}

/* T3.7：续听窗口内 VAD 人声提前结束（未达确认时长）→ 取消本次确认 */
static void vs_on_vad_speech_end(void)
{
    if (vs_get_state() != VS_STATE_FOLLOW_UP) {
        ESP_LOGD(TAG, "VAD 人声结束（state=%s）", vs_state_name(vs_get_state()));
        return;
    }
#if CONFIG_VS_FOLLOWUP_ENABLE
    vs_followup_on_voice_end();
#endif
}

static void vs_on_app_evt(void *arg, app_evt_t evt, const app_evt_data_t *data)
{
    (void)arg;

    switch (evt) {
    case APP_EVT_WAKE_DETECTED:
        vs_on_wake();
        break;
    case APP_EVT_VAD_SPEECH_START:
        vs_on_vad_speech_start();
        break;
    case APP_EVT_VAD_SPEECH_END:
        vs_on_vad_speech_end();
        break;
    case APP_EVT_NET_DOWN:
        vs_on_net_down(data);
        break;
    case APP_EVT_MUSIC_STATE_CHANGED:
        /* T4.4：音乐起播 = 用户要听歌，不是要继续对话；立刻退出续听窗口，
         * 避免麦克风拾取音乐、也避免“继续播放”要等 5s 窗口超时才生效。 */
        if (data != NULL && data->music_state.to == 1 /* MC_STATE_PLAYING */ &&
            vs_get_state() == VS_STATE_FOLLOW_UP) {
            ESP_LOGI(TAG, "音乐开始播放 → 退出续听窗口（原因=music-start）");
            (void)vs_transition(VS_STATE_FOLLOW_UP, VS_STATE_IDLE, VS_REASON_MUSIC_START);
        }
        break;
    case APP_EVT_BUTTON: {
        /* T3.2 定稿 payload；T3.4 起由 dev_ctrl 做 GPIO 扫描/去抖与投递。
         * 短按：会话中 = 打断（R4）；IDLE = 开始一轮（ARCH §8.2）。
         * 长按：静音切换已由 dev_ctrl 自己完成（ARCH §4.2 分工），本组件只记日志不动作。 */
        const uint8_t action = (data != NULL) ? data->button.action : APP_EVT_BUTTON_SHORT;
        if (action == APP_EVT_BUTTON_LONG) {
            ESP_LOGD(TAG, "长按事件（静音已由 dev_ctrl 处理），会话侧忽略");
            break;
        }
        if (vs_get_state() == VS_STATE_IDLE) {
            ESP_LOGI(TAG, "按键开始对话（IDLE 短按 → LISTENING）");
            (void)vs_start();
        } else {
            (void)vs_interrupt();
        }
        break;
    }
    default:
        /* VOICE_STATE_CHANGED 是本组件自己投递的 */
        break;
    }
}

/* ---------------- 对外 API ---------------- */

esp_err_t vs_init(void)
{
    if (s_inited) {
        return ESP_ERR_INVALID_STATE;
    }

    esp_err_t err = app_evt_register(APP_EVT_ANY, vs_on_app_evt, NULL);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "订阅事件总线失败: %s（vs_init 必须在 app_evt_init 之后）",
                 esp_err_to_name(err));
        return err;
    }

    err = vc_register_cb(vs_on_vc_event, NULL);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "注册 voice_cloud 回调失败: %s", esp_err_to_name(err));
        (void)app_evt_unregister(APP_EVT_ANY, vs_on_app_evt, NULL);
        return err;
    }

    err = audio_player_subscribe_done(vs_on_play_done, NULL);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "订阅播放完成回调失败: %s", esp_err_to_name(err));
        (void)vc_register_cb(NULL, NULL);
        (void)app_evt_unregister(APP_EVT_ANY, vs_on_app_evt, NULL);
        return err;
    }

#if CONFIG_VS_ERROR_HOLD_MS > 0
    const esp_timer_create_args_t timer_args = {
        .callback = vs_error_timer_cb,
        .name = "vs_err",
    };
    err = esp_timer_create(&timer_args, &s_error_timer);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "创建 ERROR 保持计时器失败: %s", esp_err_to_name(err));
        (void)audio_player_unsubscribe_done(vs_on_play_done);
        (void)vc_register_cb(NULL, NULL);
        (void)app_evt_unregister(APP_EVT_ANY, vs_on_app_evt, NULL);
        return err;
    }
#endif

#if CONFIG_VS_FOLLOWUP_ENABLE
    const esp_timer_create_args_t followup_args = {
        .callback = vs_followup_timer_cb,
        .name = "vs_fup",
    };
    err = esp_timer_create(&followup_args, &s_followup_timer);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "创建续听窗口计时器失败: %s", esp_err_to_name(err));
        (void)audio_player_unsubscribe_done(vs_on_play_done);
        (void)vc_register_cb(NULL, NULL);
        (void)app_evt_unregister(APP_EVT_ANY, vs_on_app_evt, NULL);
        return err;
    }
#endif

    /* T3.6：反馈完成回调（player 任务上下文）驱动 ERROR → IDLE */
    fb_register_done_cb(vs_on_fb_done, NULL);

    s_state_since_us = esp_timer_get_time();
    s_state = VS_STATE_IDLE;
    s_inited = true;
#if CONFIG_VS_FOLLOWUP_ENABLE
    ESP_LOGI(TAG,
             "ready（%d 态事件驱动，无独立任务；异常反馈看门狗 %d ms；"
             "续听窗口 %d ms / 最多 %d 轮）",
             (int)VS_STATE_COUNT, CONFIG_VS_ERROR_HOLD_MS, CONFIG_VS_FOLLOWUP_WINDOW_MS,
             CONFIG_VS_FOLLOWUP_MAX_ROUNDS);
#else
    ESP_LOGI(TAG, "ready（%d 态事件驱动，无独立任务；异常反馈看门狗 %d ms；续听窗口关）",
             (int)VS_STATE_COUNT, CONFIG_VS_ERROR_HOLD_MS);
#endif
    return ESP_OK;
}

esp_err_t vs_start(void)
{
    if (!s_inited) {
        return ESP_ERR_INVALID_STATE;
    }

    uint16_t sid;
    if (!vs_begin_session(VS_STATE_IDLE, VS_REASON_START, &sid)) {
        ESP_LOGW(TAG, "已有会话进行中（%s），忽略 vs_start", vs_state_name(vs_get_state()));
        return ESP_ERR_INVALID_STATE;
    }

    esp_err_t err = vc_start(sid);
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "手动起轮失败: %s（网络未就绪 → 网络未连接反馈）", esp_err_to_name(err));
        vs_enter_error(FB_KIND_NET_OFFLINE, 0);
        return err;
    }
    return ESP_OK;
}

esp_err_t vs_interrupt(void)
{
    if (!s_inited) {
        return ESP_ERR_INVALID_STATE;
    }
    if (vs_get_state() == VS_STATE_IDLE) {
        return ESP_ERR_INVALID_STATE;
    }

    portENTER_CRITICAL(&s_mux);
    s_stats.interrupts++;
    portEXIT_CRITICAL(&s_mux);
    ESP_LOGW(TAG, "打断当前会话（state=%s）", vs_state_name(vs_get_state()));

    if (vs_get_state() == VS_STATE_ERROR) {
        /* T3.6：错误提示音期间按键 = 中止这次反馈并回 IDLE（不向云端发 interrupt：本轮早已作废） */
        (void)fb_abort();
#if CONFIG_VS_ERROR_HOLD_MS > 0
        if (s_error_timer != NULL) {
            esp_timer_stop(s_error_timer);
        }
#endif
        ESP_LOGW(TAG, "ERROR 反馈被按键中止 → IDLE");
        (void)vs_transition(VS_STATE_ERROR, VS_STATE_IDLE, VS_REASON_INTERRUPT);
        return ESP_OK;
    }

    /* R4（T4.4 修订）：
     *     ① 语音类内容在播（提示音/TTS/整段 MP3）→ 播放通道淡出并丢弃；
     *     ② 只有音乐在播 → **暂停并保留**（不清 ring/进度，语义同语音打断）；
     *     ③ 给云端发 interrupt 报文并作废本轮（vc_interrupt）；
     *     ④ R5：恢复收音前先置 50ms 丢弃窗口（若提示音/TTS 还压着 PROMPT/TTS 位，
     *        则由各自完成回调释放后自动开窗）；
     *     ⑤ 回 IDLE。 */
    if (audio_player_voice_active()) {
        esp_err_t ferr = audio_player_fade_out_and_stop(CONFIG_AUDIO_PLAYER_INTERRUPT_FADE_MS);
        if (ferr != ESP_OK) {
            ESP_LOGW(TAG, "淡出请求失败（%s），按立即打断处理", esp_err_to_name(ferr));
        }
    } else if (audio_player_music_active() && !audio_player_music_paused()) {
        /* T4.4：按键打断音乐 = 暂停保留（用户可再说"继续"从暂停点续播） */
        esp_err_t perr = audio_player_music_pause(CONFIG_AUDIO_PLAYER_INTERRUPT_FADE_MS);
        if (perr != ESP_OK && perr != ESP_ERR_INVALID_STATE) {
            ESP_LOGW(TAG, "音乐暂停请求失败（%s）", esp_err_to_name(perr));
        } else {
            ESP_LOGI(TAG, "按键打断：音乐暂停保留（可从暂停点续播）");
        }
    }
    audio_fe_feed_discard_ms(CONFIG_AUDIO_FE_RESUME_DISCARD_MS);
    (void)vc_interrupt();
    vs_abort_to_idle(VS_REASON_INTERRUPT);
    return ESP_OK;
}

bool vs_is_busy(void)
{
    return vs_get_state() != VS_STATE_IDLE;
}

vs_state_t vs_get_state(void)
{
    return s_state;
}

uint16_t vs_current_sid(void)
{
    return s_sid;
}

esp_err_t vs_stats_read(vs_stats_t *out)
{
    if (out == NULL) {
        return ESP_ERR_INVALID_ARG;
    }
    portENTER_CRITICAL(&s_mux);
    *out = s_stats;
    out->state = (uint8_t)s_state;
    out->last_sid = s_sid;
#if CONFIG_VS_FOLLOWUP_ENABLE
    out->followup_remaining_ms = 0;
    if (s_state == VS_STATE_FOLLOW_UP && s_followup_deadline_us > 0) {
        const int64_t remain_us = s_followup_deadline_us - esp_timer_get_time();
        out->followup_remaining_ms = remain_us > 0 ? (uint32_t)(remain_us / 1000) : 0;
    }
#endif
    portEXIT_CRITICAL(&s_mux);
    return ESP_OK;
}

esp_err_t vs_set_followup_enabled(bool enabled)
{
#if CONFIG_VS_FOLLOWUP_ENABLE
    portENTER_CRITICAL(&s_mux);
    s_followup_enabled = enabled;
    portEXIT_CRITICAL(&s_mux);
    if (!enabled && vs_get_state() == VS_STATE_FOLLOW_UP) {
        vs_followup_stop();
        (void)vs_transition(VS_STATE_FOLLOW_UP, VS_STATE_IDLE, VS_REASON_FOLLOWUP_OFF);
    }
    ESP_LOGI(TAG, "续听窗口运行期开关：%s", enabled ? "开" : "关");
    return ESP_OK;
#else
    (void)enabled;
    return ESP_ERR_NOT_SUPPORTED;
#endif
}

bool vs_followup_enabled(void)
{
    return s_followup_enabled;
}

esp_err_t vs_stats_reset(void)
{
    portENTER_CRITICAL(&s_mux);
    const uint16_t last_sid = s_sid;
    const uint32_t last_ms = s_stats.last_state_ms;
    memset(&s_stats, 0, sizeof(s_stats));
    s_stats.last_sid = last_sid;
    s_stats.last_state_ms = last_ms;
    portEXIT_CRITICAL(&s_mux);
    return ESP_OK;
}
