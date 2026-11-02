/*
 * feedback —— 异常反馈路由（L3 能力层，T3.6）的唯一对外头文件。
 *
 * 职责边界（ARCHITECTURE §4.1/§7）：
 *   - 把"异常反馈"抽象成一个语义事件 + 若干反馈通道；**语音只是当前唯一通道**；
 *   - 语义事件 APP_EVT_FEEDBACK（payload app_evt_feedback_t）在 fb_notify() 调用方上下文发布，
 *     未来的屏幕/灯光等组件订阅该事件即可展示反馈，**不需要改本组件或 voice_session**；
 *   - 当前通道：从 assets 的提示音表取 PCM → audio_player_play_pcm()（CH_PROMPT）
 *     + R1 门控 audio_fe_feed_hold(AUDIO_FE_HOLD_PROMPT)，播放完成回调里释放并上报；
 *   - 若 TTS 流正在播放（云端 error / 中途断网发生在 SPEAKING），先等 TTS 流的完成回调
 *     （voice_cloud 的 abort 由 voice_session 触发）再补播提示音，避免 play_pcm 被拒而静默。
 *
 * 线程模型：**不建任务/不定时器**。fb_notify() 可被 evt/ws_client/vc/console 任务调用，
 * 只做 PSRAM/队列层面的非阻塞操作；完成回调在 `player` 任务上下文执行，必须非阻塞。
 * **禁止从音频实时任务（afe_feed/afe_fetch）调用 fb_notify()**：发布带 payload 的事件会
 * 在 esp_event 内部 calloc（CONVENTIONS §3/§4）。
 */
#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "esp_err.h"
#include "feedback_policy.h"

#ifdef __cplusplus
extern "C" {
#endif

/* 反馈完成回调：在 `player` 任务上下文执行（ok=false 表示播放失败/被中止）。
 * 只注册一个（voice_session 用于 ERROR → IDLE 收尾）。 */
typedef void (*fb_done_cb_t)(fb_kind_t kind, bool ok, uint32_t dur_ms, void *arg);

typedef struct {
    uint32_t plays;     /* 开始播放的反馈次数 */
    uint32_t done_ok;   /* 提示音正常播完次数 */
    uint32_t done_fail; /* 提示音失败/被中止次数 */
    uint32_t deferred;  /* 因 TTS 流占用而延迟播放的次数 */
    uint32_t rejected;  /* 忙/资源不可用而丢弃的次数 */
    uint32_t aborts;    /* 被 fb_abort() 中止的次数 */
    uint32_t by_kind[FB_KIND_COUNT]; /* 各反馈类别计数（下标同 fb_kind_t） */
    uint32_t last_dur_ms;            /* 最近一次提示音实际时长 */
    uint8_t active_kind;             /* 当前/待播放的类别（FB_KIND_NONE=空闲） */
    bool pending;                    /* 正在等 TTS 流结束补播 */
} fb_stats_t;

/**
 * @brief 初始化反馈路由（订阅播放完成回调；不建任务）
 *
 * @note 必须在 audio_player_init() / app_evt_init() 之后调用；只允许一次。
 */
esp_err_t fb_init(void);

/**
 * @brief 请求一次异常反馈（非阻塞）
 *
 * 语义事件 APP_EVT_FEEDBACK 立即发布；语音通道随后开始播放。若上一次反馈仍在播放/
 * 等待补播，本次直接丢弃（返回 ESP_ERR_INVALID_STATE）并计入 rejected。
 *
 * @param kind 反馈类别（fb_kind_t，1..4）
 * @param code 原始错误码（云端 error code / vc_tts_fail_t / 0）
 * @param sid  所属会话（无会话语境传 0 或最近 sid）
 *
 * @return ESP_OK 已受理；ESP_ERR_INVALID_STATE 未初始化/忙；ESP_ERR_INVALID_ARG 非法 kind；
 *         ESP_ERR_NOT_SUPPORTED 提示音资源不可用（不启动播放，也不回调 done）
 */
esp_err_t fb_notify(fb_kind_t kind, int32_t code, uint16_t sid);

/**
 * @brief 中止当前/待播反馈（看门狗兜底用；语音通道淡出后释放 R1 门控）
 *
 * 不会回调 fb_done_cb（调用方自己决定状态收尾）。
 */
esp_err_t fb_abort(void);

bool fb_is_active(void);      /* 正在播放或等待补播 */
fb_kind_t fb_active_kind(void); /* 当前类别；空闲返回 FB_KIND_NONE */

void fb_register_done_cb(fb_done_cb_t cb, void *arg);

esp_err_t fb_stats_read(fb_stats_t *out);
void fb_stats_reset(void);

#ifdef __cplusplus
}
#endif
