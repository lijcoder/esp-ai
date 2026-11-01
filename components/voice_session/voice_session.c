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
#include "audio_io.h"
#include "audio_player.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "event_bus.h"
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
static portMUX_TYPE s_mux = portMUX_INITIALIZER_UNLOCKED;

/* ERROR 状态停留（T3.6 播错误提示音用；=0 时立即回 IDLE，不创建计时器） */
#if CONFIG_VS_ERROR_HOLD_MS > 0
static esp_timer_handle_t s_error_timer;
#endif

/* ---------------- 状态迁移 ---------------- */

const char *vs_state_name(vs_state_t state)
{
    switch (state) {
    case VS_STATE_IDLE: return "IDLE";
    case VS_STATE_LISTENING: return "LISTENING";
    case VS_STATE_THINKING: return "THINKING";
    case VS_STATE_SPEAKING: return "SPEAKING";
    case VS_STATE_ERROR: return "ERROR";
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
    default: return "unknown";
    }
}

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

    vs_notify(from, to, reason, sid, dur_ms);
    return true;
}

/* 开始新一轮（IDLE → LISTENING）：分配 sid；返回 false 表示已有会话/未初始化 */
static bool vs_begin_session(vs_reason_t reason, uint16_t *sid_out)
{
    const int64_t now = esp_timer_get_time();
    uint16_t sid;

    portENTER_CRITICAL(&s_mux);
    if (!s_inited || s_state != VS_STATE_IDLE) {
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
    s_state = VS_STATE_LISTENING;
    s_state_since_us = now;
    s_stats.transitions++;
    s_stats.rounds_started++;
    s_stats.last_sid = sid;
    s_stats.last_state_ms = 0;
    s_stats.state = (uint8_t)VS_STATE_LISTENING;
    portEXIT_CRITICAL(&s_mux);

    vs_notify(VS_STATE_IDLE, VS_STATE_LISTENING, reason, sid, 0);
    if (sid_out != NULL) {
        *sid_out = sid;
    }
    return true;
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

#if CONFIG_VS_ERROR_HOLD_MS > 0
static void vs_error_timer_cb(void *arg)
{
    (void)arg;
    (void)vs_transition(VS_STATE_ERROR, VS_STATE_IDLE, VS_REASON_ERROR_DONE);
}
#endif

/*
 * 进入 ERROR：LISTENING/THINKING/SPEAKING → ERROR。
 * ARCH §7：ERROR 播错误提示音（资源 T3.6）后回 IDLE、同轮不重试；当前提示音未接入，
 * 由 `CONFIG_VS_ERROR_HOLD_MS` 控制停留（默认 0 = 立即回 IDLE，日志里仍可见迁移）。
 */
static void vs_enter_error(vs_reason_t reason)
{
    const vs_state_t st = vs_get_state();
    if (st != VS_STATE_LISTENING && st != VS_STATE_THINKING && st != VS_STATE_SPEAKING) {
        return;
    }
    if (!vs_transition(st, VS_STATE_ERROR, reason)) {
        return;
    }
    ESP_LOGE(TAG, "会话进入 ERROR（%s），本轮不自动重试", vs_reason_name(reason));

    /* 防御：让协议侧也结束本轮（cloud error / 超时路径下 vc 多半已结束，cancel 幂等） */
    (void)vc_cancel();

#if CONFIG_VS_ERROR_HOLD_MS > 0
    if (s_error_timer != NULL) {
        esp_timer_stop(s_error_timer);
        esp_timer_start_once(s_error_timer, (uint64_t)CONFIG_VS_ERROR_HOLD_MS * 1000);
        return;
    }
#endif
    (void)vs_transition(VS_STATE_ERROR, VS_STATE_IDLE, VS_REASON_ERROR_DONE);
}

/* ---------------- voice_cloud 回调（ws_client / vc 任务上下文） ---------------- */

static void vs_on_round_end(vc_round_end_t result)
{
    switch (result) {
    case VC_ROUND_DONE:
        /* 正常收尾：有音频正在排空时等 STREAM_DONE；无音频可播（tts_start 未到/坏帧）直接回 IDLE */
        if (vs_get_state() == VS_STATE_SPEAKING && !audio_player_stream_active() &&
            vs_transition(VS_STATE_SPEAKING, VS_STATE_IDLE, VS_REASON_ROUND_DONE)) {
            portENTER_CRITICAL(&s_mux);
            s_stats.rounds_completed++;
            portEXIT_CRITICAL(&s_mux);
        }
        return;

    case VC_ROUND_ABORT_TIMEOUT:
        portENTER_CRITICAL(&s_mux);
        s_stats.timeouts++;
        portEXIT_CRITICAL(&s_mux);
        vs_enter_error(VS_REASON_TIMEOUT);
        break;

    case VC_ROUND_ABORT_ERROR:
        vs_enter_error(VS_REASON_CLOUD_ERROR); /* CLOUD_ERROR 事件先到时会幂等返回 */
        break;

    case VC_ROUND_ABORT_LINK:
        vs_abort_to_idle(VS_REASON_LINK_LOST);
        break;

    case VC_ROUND_ABORT_CANCEL:
    case VC_ROUND_ABORT_NEW:
    case VC_ROUND_ABORT_SETUP:
    default:
        vs_abort_to_idle(VS_REASON_ROUND_ABORT);
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
        vs_enter_error(VS_REASON_CLOUD_ERROR);
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
        /* TTS 流式播放结束（成功/失败/打断）：R2 恢复收音在 voice_cloud，本组件只收状态 */
        if (vs_transition(VS_STATE_SPEAKING, VS_STATE_IDLE, VS_REASON_STREAM_DONE)) {
            portENTER_CRITICAL(&s_mux);
            s_stats.rounds_completed++;
            portEXIT_CRITICAL(&s_mux);
            ESP_LOGI(TAG, "speaking done（ok=%d）→ IDLE", (int)ok);
        }
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
     * 日志文案与 T1.2b 之前的 APP 版本保持一致（测试脚本依赖） */
    audio_fe_feed_release(AUDIO_FE_HOLD_PROMPT);
    ESP_LOGI(TAG, "prompt done, feed resumed");

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
        ESP_LOGW(TAG, "起轮失败（sid=%u）: %s（网络未就绪，本轮降级结束）", (unsigned)sid,
                 esp_err_to_name(err));
        (void)vs_transition(VS_STATE_LISTENING, VS_STATE_IDLE, VS_REASON_CLOUD_NOT_READY);
    }
}

/* ---------------- event_bus 事件（evt 任务上下文） ---------------- */

static void vs_on_wake(void)
{
    ESP_LOGI(TAG, "wake detected");
    if (vs_get_state() != VS_STATE_IDLE) {
        portENTER_CRITICAL(&s_mux);
        s_stats.wakes_dropped++;
        portEXIT_CRITICAL(&s_mux);
        ESP_LOGW(TAG, "会话进行中（%s），忽略本次唤醒", vs_state_name(vs_get_state()));
        return;
    }
    /* R3：音乐/本地长播中唤醒**不丢弃**（麦克风本来就没关）。单输出通道（P4 才有混音）
     * 下先把音乐淡出、把通道让给提示音/TTS；duck 叠加播放归 T3.3 混音 + T4.4。 */
    bool music_playing = false;
    if (audio_player_is_busy()) {
        audio_player_src_t cur = AUDIO_PLAYER_SRC_PCM;
        if (audio_player_current_src(&cur) && cur == AUDIO_PLAYER_SRC_MP3_FILE) {
            music_playing = true;
        } else {
            /* 提示音/TTS 收尾中：本地会话不插队（避免两路提示音叠在一起） */
            portENTER_CRITICAL(&s_mux);
            s_stats.wakes_dropped++;
            portEXIT_CRITICAL(&s_mux);
            ESP_LOGW(TAG, "播放器忙（提示音/TTS 收尾），忽略本次唤醒");
            return;
        }
    }

    uint16_t sid;
    if (!vs_begin_session(VS_REASON_WAKE, &sid)) {
        return;
    }

    if (music_playing) {
        ESP_LOGW(TAG, "音乐播放中唤醒（R3）：淡出音乐让出输出通道 %d ms（混音叠加归 T3.3/P4）",
                 CONFIG_AUDIO_PLAYER_INTERRUPT_FADE_MS);
        esp_err_t ferr = audio_player_fade_out_and_stop(CONFIG_AUDIO_PLAYER_INTERRUPT_FADE_MS);
        if (ferr != ESP_OK) {
            ESP_LOGW(TAG, "音乐淡出请求失败（%s），提示音将排队等它播完",
                     esp_err_to_name(ferr));
        }
    }

    /* R1：应答提示音播放期间暂停收音（原因位 PROMPT，与 TTS 的 R1 互不覆盖） */
    portENTER_CRITICAL(&s_mux);
    s_prompt_playing = true;
    portEXIT_CRITICAL(&s_mux);
    audio_fe_feed_hold(AUDIO_FE_HOLD_PROMPT);

    esp_err_t err = audio_player_play_pcm(prompt_wo_zai_ne_pcm, prompt_wo_zai_ne_samples,
                                          AUDIO_IO_MIC_SAMPLE_RATE);
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
    ESP_LOGW(TAG, "网络断开（reason=%u，重试 %u ms）→ 结束当前会话",
             down != NULL ? (unsigned)down->reason : 0u,
             down != NULL ? (unsigned)down->retry_ms : 0u);
    (void)vc_cancel();
    vs_abort_to_idle(VS_REASON_NET_DOWN);
}

static void vs_on_app_evt(void *arg, app_evt_t evt, const app_evt_data_t *data)
{
    (void)arg;

    switch (evt) {
    case APP_EVT_WAKE_DETECTED:
        vs_on_wake();
        break;
    case APP_EVT_VAD_SPEECH_START:
        ESP_LOGD(TAG, "VAD 人声开始（state=%s）", vs_state_name(vs_get_state()));
        break;
    case APP_EVT_VAD_SPEECH_END:
        ESP_LOGD(TAG, "VAD 人声结束（state=%s）", vs_state_name(vs_get_state()));
        break;
    case APP_EVT_NET_DOWN:
        vs_on_net_down(data);
        break;
    case APP_EVT_BUTTON: {
        /* T3.2 定稿 payload；T3.4 只负责 GPIO 扫描/去抖与投递。
         * 短按：会话中 = 打断（R4）；IDLE = 开始一轮（ARCH §8.2）。长按归 T3.4 静音。 */
        const uint8_t action = (data != NULL) ? data->button.action : APP_EVT_BUTTON_SHORT;
        if (action == APP_EVT_BUTTON_LONG) {
            ESP_LOGI(TAG, "长按事件（静音功能归 T3.4），忽略");
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

    s_state_since_us = esp_timer_get_time();
    s_state = VS_STATE_IDLE;
    s_inited = true;
    ESP_LOGI(TAG, "ready（%d 态事件驱动，无独立任务；异常保持 %d ms；sid 由本组件分配）",
             (int)VS_STATE_COUNT, CONFIG_VS_ERROR_HOLD_MS);
    return ESP_OK;
}

esp_err_t vs_start(void)
{
    if (!s_inited) {
        return ESP_ERR_INVALID_STATE;
    }

    uint16_t sid;
    if (!vs_begin_session(VS_REASON_START, &sid)) {
        ESP_LOGW(TAG, "已有会话进行中（%s），忽略 vs_start", vs_state_name(vs_get_state()));
        return ESP_ERR_INVALID_STATE;
    }

    esp_err_t err = vc_start(sid);
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "手动起轮失败: %s", esp_err_to_name(err));
        (void)vs_transition(VS_STATE_LISTENING, VS_STATE_IDLE, VS_REASON_CLOUD_NOT_READY);
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

    /* R4：① 播放通道淡出（请求在这里、执行在 player 任务，80ms 见 Kconfig）；
     *     ② 给云端发 interrupt 报文并作废本轮（vc_interrupt）；
     *     ③ R5：恢复收音前先置 50ms 丢弃窗口（若提示音/TTS 还压着 PROMPT/TTS 位，
     *        则由各自完成回调释放后自动开窗）；
     *     ④ 回 IDLE。提示音/TTS 的 hold 位由播放完成回调释放，这里不抢着释放，
     *        否则淡出的尾音会被麦克风拾进 AFE。 */
    if (audio_player_is_busy()) {
        esp_err_t ferr = audio_player_fade_out_and_stop(CONFIG_AUDIO_PLAYER_INTERRUPT_FADE_MS);
        if (ferr != ESP_OK) {
            ESP_LOGW(TAG, "淡出请求失败（%s），按立即打断处理", esp_err_to_name(ferr));
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
    portEXIT_CRITICAL(&s_mux);
    return ESP_OK;
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
