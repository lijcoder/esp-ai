/*
 * feedback —— 异常反馈路由（L3，T3.6）实现。
 *
 * 设计要点见 feedback.h；本文件只做三件事：
 *   1. 把"异常反馈"作为语义事件发布（APP_EVT_FEEDBACK），让未来的屏幕/灯光通道可订阅；
 *   2. 当前唯一通道 = 语音：assets 提示音 → audio_player CH_PROMPT（R1 门控 PROMPT）；
 *   3. 与 TTS 流互斥的补播：play_pcm 在流播放期间会被拒，等 TTS 完成回调再补一次。
 *
 * 内存约束（CONVENTIONS §4）：不建任务、不定时器、不分配内部 RAM 大块；静态只增加
 * 一个小 req 结构 + 统计；提示音 PCM 在 flash（const），通道 ring 由 audio_player 复用。
 */
#include "feedback.h"

#include <string.h>

#include "audio_fe.h"
#include "audio_player.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "event_bus.h"
#include "freertos/FreeRTOS.h"
#include "prompt_assets.h"
#include "sdkconfig.h"

static const char *TAG = "FB";

/* 反馈类别 → 提示音资源（资源层临时方案；T3.5 迁 assets 分区时本映射不变） */
static prompt_id_t kind_to_prompt(fb_kind_t kind)
{
    switch (kind) {
    case FB_KIND_NET_OFFLINE: return PROMPT_NET_OFFLINE;
    case FB_KIND_CLOUD_ERROR: return PROMPT_CLOUD_ERROR;
    case FB_KIND_TIMEOUT:     return PROMPT_TIMEOUT;
    case FB_KIND_TTS_FAIL:    return PROMPT_TTS_FAIL;
    default:                  return PROMPT_COUNT;
    }
}

typedef struct {
    bool active;     /* 语音通道正在播 */
    bool pending;    /* 已受理、等 TTS 流结束后补播 */
    fb_kind_t kind;
    int32_t code;
    uint16_t sid;
    int64_t start_us;
} fb_req_t;

static portMUX_TYPE s_mux = portMUX_INITIALIZER_UNLOCKED;
static bool s_inited;
static bool s_done_subscribed;
static fb_req_t s_req;
static fb_stats_t s_stats;
static fb_done_cb_t s_done_cb;
static void *s_done_arg;

static void fb_emit_event(fb_kind_t kind, int32_t code, uint16_t sid)
{
    app_evt_data_t data;
    memset(&data, 0, sizeof(data));
    data.feedback.kind = (uint8_t)kind;
    data.feedback.code = code;
    data.feedback.sid = sid;

    esp_err_t err = app_evt_post(APP_EVT_FEEDBACK, &data);
    if (err != ESP_OK) {
        /* 事件只是给未来通道的扩展点，失败不影响语音反馈 */
        ESP_LOGW(TAG, "发布 APP_EVT_FEEDBACK 失败: %s（语音通道不受影响）",
                 esp_err_to_name(err));
    }
}

static void stat_kind_inc(fb_kind_t kind)
{
    portENTER_CRITICAL(&s_mux);
    if (kind > FB_KIND_NONE && kind < FB_KIND_COUNT) {
        s_stats.by_kind[kind]++;
    }
    portEXIT_CRITICAL(&s_mux);
}

/* 语音通道收尾：更新统计 + 回调 voice_session（在 player 任务上下文） */
static void fb_finish(fb_kind_t kind, bool ok, uint32_t dur_ms)
{
    portENTER_CRITICAL(&s_mux);
    if (ok) {
        s_stats.done_ok++;
    } else {
        s_stats.done_fail++;
    }
    s_stats.last_dur_ms = dur_ms;
    portEXIT_CRITICAL(&s_mux);

    ESP_LOGI(TAG, "提示音完成 kind=%s ok=%d dur=%u ms", fb_kind_name(kind), (int)ok,
             (unsigned)dur_ms);

    const fb_done_cb_t cb = s_done_cb;
    void *arg = s_done_arg;
    if (cb != NULL) {
        cb(kind, ok, dur_ms, arg); /* player 任务上下文：voice_session 只做状态迁移 */
    }
}

/* 尝试启动语音通道（可被 fb_notify / TTS 完成回调调用） */
static void fb_start_audio(void)
{
    fb_kind_t kind;
    int32_t code;
    uint16_t sid;

    portENTER_CRITICAL(&s_mux);
    if (!s_req.pending || s_req.active) {
        portEXIT_CRITICAL(&s_mux);
        return;
    }
    kind = s_req.kind;
    code = s_req.code;
    sid = s_req.sid;
    portEXIT_CRITICAL(&s_mux);

    /* TTS 流占用时 play_pcm 必被拒：不报错、等 TTS 完成回调再补播 */
    if (audio_player_stream_active()) {
        portENTER_CRITICAL(&s_mux);
        s_stats.deferred++;
        portEXIT_CRITICAL(&s_mux);
        ESP_LOGW(TAG, "播放器被 TTS 流占用，延迟播放（kind=%s，code=%d，sid=%u）",
                 fb_kind_name(kind), (int)code, (unsigned)sid);
        return;
    }

    const prompt_id_t pid = kind_to_prompt(kind);
    const prompt_clip_t *clip = prompt_assets_get(pid);
    if (clip == NULL || clip->pcm == NULL || clip->samples == 0) {
        ESP_LOGE(TAG, "提示音资源不可用（kind=%s），本次反馈不播", fb_kind_name(kind));
        portENTER_CRITICAL(&s_mux);
        s_req.pending = false;
        s_stats.rejected++;
        portEXIT_CRITICAL(&s_mux);
        return;
    }

    portENTER_CRITICAL(&s_mux);
    if (!s_req.pending) { /* 期间被 fb_abort 取消 */
        portEXIT_CRITICAL(&s_mux);
        return;
    }
    s_req.pending = false;
    s_req.active = true;
    s_req.start_us = esp_timer_get_time();
    s_stats.plays++;
    portEXIT_CRITICAL(&s_mux);

    /* R1：提示音播放期间暂停收音，避免扬声器回采自激（与唤醒应答共用 PROMPT 原因位） */
    audio_fe_feed_hold(AUDIO_FE_HOLD_PROMPT);
    esp_err_t err = audio_player_play_pcm(clip->pcm, clip->samples, clip->sample_rate);
    if (err != ESP_OK) {
        audio_fe_feed_release(AUDIO_FE_HOLD_PROMPT);

        /* 竞态：检查与入队之间 TTS 流恰好开了 → 回到 pending，等其完成回调补播 */
        if (err == ESP_ERR_INVALID_STATE && audio_player_stream_active()) {
            portENTER_CRITICAL(&s_mux);
            s_req.active = false;
            s_req.pending = true;
            s_stats.plays--;
            s_stats.deferred++;
            portEXIT_CRITICAL(&s_mux);
            ESP_LOGW(TAG, "开播瞬间 TTS 流占用，延迟播放（kind=%s）", fb_kind_name(kind));
            return;
        }

        portENTER_CRITICAL(&s_mux);
        s_req.active = false;
        portEXIT_CRITICAL(&s_mux);
        ESP_LOGE(TAG, "提示音播放失败（kind=%s）: %s", fb_kind_name(kind),
                 esp_err_to_name(err));
        fb_finish(kind, false, 0);
        return;
    }

    ESP_LOGI(TAG, "反馈 kind=%s code=%d sid=%u → 播放提示音（%s=%u 样本）", fb_kind_name(kind),
             (int)code, (unsigned)sid, prompt_assets_name(pid), (unsigned)clip->samples);
}

/* audio_player 完成回调（player 任务上下文）：PCM 完成 → 收尾；TTS 流完成 → 补播 */
static void fb_on_player_done(audio_player_src_t src, bool ok, void *arg)
{
    (void)arg;

    if (src == AUDIO_PLAYER_SRC_PCM) {
        bool ours = false;
        fb_kind_t kind = FB_KIND_NONE;
        uint32_t dur_ms = 0;

        portENTER_CRITICAL(&s_mux);
        if (s_req.active) {
            ours = true;
            kind = s_req.kind;
            s_req.active = false;
            const int64_t now_us = esp_timer_get_time();
            const int64_t dur = now_us - s_req.start_us;
            dur_ms = dur > 0 ? (uint32_t)(dur / 1000) : 0;
        }
        portEXIT_CRITICAL(&s_mux);

        if (!ours) {
            return; /* 唤醒应答提示音等其它 PCM：不归本组件管 */
        }
        audio_fe_feed_release(AUDIO_FE_HOLD_PROMPT);
        fb_finish(kind, ok, dur_ms);
        return;
    }

    if (src == AUDIO_PLAYER_SRC_TTS_STREAM) {
        bool retry = false;
        portENTER_CRITICAL(&s_mux);
        retry = s_req.pending && !s_req.active;
        portEXIT_CRITICAL(&s_mux);
        if (retry) {
            fb_start_audio();
        }
    }
}

esp_err_t fb_init(void)
{
    if (s_inited) {
        return ESP_ERR_INVALID_STATE;
    }

    esp_err_t err = audio_player_subscribe_done(fb_on_player_done, NULL);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "订阅播放完成回调失败: %s（异常反馈将无法收尾）", esp_err_to_name(err));
        return err;
    }
    s_done_subscribed = true;
    memset(&s_req, 0, sizeof(s_req));
    s_inited = true;
    ESP_LOGI(TAG, "ready（语义事件 APP_EVT_FEEDBACK + 语音通道 CH_PROMPT；不建任务）");
    return ESP_OK;
}

esp_err_t fb_notify(fb_kind_t kind, int32_t code, uint16_t sid)
{
    if (!s_inited) {
        return ESP_ERR_INVALID_STATE;
    }
    if (!fb_kind_valid(kind)) {
        return ESP_ERR_INVALID_ARG;
    }

    const prompt_clip_t *clip = prompt_assets_get(kind_to_prompt(kind));
    if (clip == NULL || clip->pcm == NULL || clip->samples == 0) {
        stat_kind_inc(kind);
        ESP_LOGE(TAG, "提示音资源缺失（kind=%s），反馈丢弃", fb_kind_name(kind));
        return ESP_ERR_NOT_SUPPORTED;
    }

    portENTER_CRITICAL(&s_mux);
    if (s_req.active || s_req.pending) {
        s_stats.rejected++;
        portEXIT_CRITICAL(&s_mux);
        ESP_LOGW(TAG, "反馈被丢弃（kind=%s，reason=busy）", fb_kind_name(kind));
        return ESP_ERR_INVALID_STATE;
    }
    s_req.pending = true;
    s_req.kind = kind;
    s_req.code = code;
    s_req.sid = sid;
    s_stats.by_kind[kind]++;
    portEXIT_CRITICAL(&s_mux);

    /* 先发布语义事件（调用方上下文，非音频任务）：未来屏幕/灯光通道订阅这一条即可 */
    fb_emit_event(kind, code, sid);
    fb_start_audio();
    return ESP_OK;
}

esp_err_t fb_abort(void)
{
    if (!s_inited) {
        return ESP_ERR_INVALID_STATE;
    }

    bool was_active = false;
    fb_kind_t kind = FB_KIND_NONE;

    portENTER_CRITICAL(&s_mux);
    if (s_req.active || s_req.pending) {
        was_active = s_req.active;
        kind = s_req.kind;
        s_req.active = false;
        s_req.pending = false;
        s_stats.aborts++;
    }
    portEXIT_CRITICAL(&s_mux);

    if (kind == FB_KIND_NONE) {
        return ESP_OK; /* 空闲：幂等 */
    }

    if (was_active) {
        (void)audio_player_fade_out_and_stop(CONFIG_AUDIO_PLAYER_INTERRUPT_FADE_MS);
        audio_fe_feed_release(AUDIO_FE_HOLD_PROMPT);
        ESP_LOGW(TAG, "反馈被中止（kind=%s，淡出并释放 R1）", fb_kind_name(kind));
    } else {
        ESP_LOGW(TAG, "待播反馈被取消（kind=%s）", fb_kind_name(kind));
    }
    return ESP_OK;
}

bool fb_is_active(void)
{
    portENTER_CRITICAL(&s_mux);
    const bool active = s_req.active || s_req.pending;
    portEXIT_CRITICAL(&s_mux);
    return active;
}

fb_kind_t fb_active_kind(void)
{
    portENTER_CRITICAL(&s_mux);
    const fb_kind_t kind = (s_req.active || s_req.pending) ? s_req.kind : FB_KIND_NONE;
    portEXIT_CRITICAL(&s_mux);
    return kind;
}

void fb_register_done_cb(fb_done_cb_t cb, void *arg)
{
    portENTER_CRITICAL(&s_mux);
    s_done_cb = cb;
    s_done_arg = arg;
    portEXIT_CRITICAL(&s_mux);
}

esp_err_t fb_stats_read(fb_stats_t *out)
{
    if (out == NULL) {
        return ESP_ERR_INVALID_ARG;
    }
    portENTER_CRITICAL(&s_mux);
    *out = s_stats;
    out->active_kind = (uint8_t)((s_req.active || s_req.pending) ? s_req.kind : FB_KIND_NONE);
    out->pending = s_req.pending;
    if (s_req.active && s_req.start_us > 0) {
        const int64_t dur_us = esp_timer_get_time() - s_req.start_us;
        out->last_dur_ms = dur_us > 0 ? (uint32_t)(dur_us / 1000) : 0;
    }
    portEXIT_CRITICAL(&s_mux);
    return ESP_OK;
}

void fb_stats_reset(void)
{
    portENTER_CRITICAL(&s_mux);
    memset(&s_stats, 0, sizeof(s_stats));
    portEXIT_CRITICAL(&s_mux);
}
