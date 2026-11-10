/*
 * music_policy —— `music_session` 的**纯逻辑**（无 IDF 依赖，可 host 单测）。
 *
 * 拆出来的原因：暂停抢占/自动恢复、mid 过滤、控制动作应用、resume 兜底这些决策最容易写错，
 * 而它们不依赖 FreeRTOS/esp_timer/播放器；`music_session.c` 只负责"什么时候调用/怎么执行"。
 *
 * 契约来源：build/t43_contract.md §3/§4/§5。
 *   - 状态数值与 `mc_state_t` 一致（IDLE=0/PLAYING=1/PAUSED=2/STOPPED=3/ERROR=4）；
 *   - pause_reason：0=无、1=用户、2=语音抢占；
 *   - 语音状态数值与 `vs_state_t` 一致（0=IDLE、1=LISTENING、2=THINKING、3=SPEAKING、
 *     4=ERROR、5=FOLLOW_UP）。
 * 为避免 host 端依赖 sdkconfig/esp_err，本头文件只用 uint8_t/bool；music_session.c 里有
 * `_Static_assert` 把这些数值钉死到真实枚举。
 */
#pragma once

#include <stdbool.h>
#include <stdint.h>

/* 与 mc_state_t 对齐（只用于纯逻辑判断） */
#define MS_POLICY_STATE_IDLE    0u
#define MS_POLICY_STATE_PLAYING 1u
#define MS_POLICY_STATE_PAUSED  2u
#define MS_POLICY_STATE_STOPPED 3u
#define MS_POLICY_STATE_ERROR   4u

/* pause_reason（ms_info_t.pause_reason） */
#define MS_POLICY_REASON_NONE  0u
#define MS_POLICY_REASON_USER  1u
#define MS_POLICY_REASON_VOICE 2u

/* 语音活跃态：LISTENING/THINKING/SPEAKING/ERROR/FOLLOW_UP（IDLE=0 之外全是活跃态）。
 * 契约 §4 的活跃态列表与此一致（含 ERROR——错误提示音也要音乐让路）。 */
static inline bool ms_policy_voice_active(uint8_t voice_state)
{
    return voice_state >= 1u && voice_state <= 5u;
}

/* pause_reason 合并优先级：用户暂停 > 语音抢占。用户 latch 不能被语音覆盖。 */
static inline uint8_t ms_policy_pause_reason_merge(uint8_t current, uint8_t incoming)
{
    if (current == MS_POLICY_REASON_USER || incoming == MS_POLICY_REASON_USER) {
        return MS_POLICY_REASON_USER;
    }
    if (current == MS_POLICY_REASON_VOICE || incoming == MS_POLICY_REASON_VOICE) {
        return MS_POLICY_REASON_VOICE;
    }
    return MS_POLICY_REASON_NONE;
}

/* 语音进入活跃态时是否要 auto-pause：
 * 音乐必须正在播、不是用户暂停、功能开关打开。 */
static inline bool ms_policy_auto_pause(uint8_t music_state, uint8_t pause_reason,
                                        uint8_t voice_state, bool enabled)
{
    return enabled && music_state == MS_POLICY_STATE_PLAYING && ms_policy_voice_active(voice_state) &&
           pause_reason != MS_POLICY_REASON_USER;
}

/* 语音回 IDLE 时是否要 auto-resume：
 * 只有"因语音抢占而暂停"（reason=2）才恢复；用户暂停（reason=1）保持暂停。 */
static inline bool ms_policy_auto_resume(uint8_t music_state, uint8_t pause_reason,
                                         uint8_t voice_state, bool enabled)
{
    return enabled && music_state == MS_POLICY_STATE_PAUSED && pause_reason == MS_POLICY_REASON_VOICE &&
           !ms_policy_voice_active(voice_state);
}

/* ---- mid 过滤（P-3）：music_track 只接受更新；music_control 允许同 mid，只丢更旧 ---- */
typedef enum {
    MS_POLICY_MID_OK = 0, /* 接受 */
    MS_POLICY_MID_STALE,  /* 过期（比当前旧），丢弃 */
    MS_POLICY_MID_DUP,    /* 同 mid 的 music_track 重复，丢弃 */
} ms_policy_mid_t;

static inline ms_policy_mid_t ms_policy_mid_verdict(bool have_mid, uint16_t current_mid,
                                                     uint16_t incoming_mid, bool is_track)
{
    if (!have_mid) {
        return MS_POLICY_MID_OK;
    }
    /* 与协议一致：uint16 回绕按有符号差值比较 */
    const int16_t diff = (int16_t)(incoming_mid - current_mid);
    if (diff < 0) {
        return MS_POLICY_MID_STALE;
    }
    if (diff == 0 && is_track) {
        return MS_POLICY_MID_DUP;
    }
    return MS_POLICY_MID_OK;
}

/* ---- music_control 应用结果 ---- */
typedef enum {
    MS_POLICY_CTRL_IGNORE = 0, /* 当前状态不接受该动作 */
    MS_POLICY_CTRL_PAUSE,      /* PLAYING → PAUSED（开始淡出） */
    MS_POLICY_CTRL_LATCH,      /* 已 PAUSED：只更新 pause_reason（用户暂停 latch） */
    MS_POLICY_CTRL_RESUME,     /* PAUSED → PLAYING */
    MS_POLICY_CTRL_STOP,       /* PLAYING/PAUSED → STOPPED */
} ms_policy_ctrl_t;

/* action 数值与 mc_action_t 一致：0=pause、1=resume、2=stop */
static inline ms_policy_ctrl_t ms_policy_control(uint8_t music_state, uint8_t action)
{
    switch (action) {
    case 0: /* pause */
        if (music_state == MS_POLICY_STATE_PLAYING) {
            return MS_POLICY_CTRL_PAUSE;
        }
        if (music_state == MS_POLICY_STATE_PAUSED) {
            return MS_POLICY_CTRL_LATCH;
        }
        return MS_POLICY_CTRL_IGNORE;
    case 1: /* resume */
        return (music_state == MS_POLICY_STATE_PAUSED) ? MS_POLICY_CTRL_RESUME
                                                       : MS_POLICY_CTRL_IGNORE;
    case 2: /* stop */
        return (music_state == MS_POLICY_STATE_PLAYING || music_state == MS_POLICY_STATE_PAUSED)
                   ? MS_POLICY_CTRL_STOP
                   : MS_POLICY_CTRL_IGNORE;
    default:
        return MS_POLICY_CTRL_IGNORE;
    }
}

/* resume 失败兜底：开关打开、本暂停周期还没发过、音乐确实停在 PAUSED。 */
static inline bool ms_policy_resume_fallback(bool enabled, bool already_requested,
                                             uint8_t music_state)
{
    return enabled && !already_requested && music_state == MS_POLICY_STATE_PAUSED;
}

/* 状态机允许的迁移（契约 §3 表格；PAUSED→PLAYING 也允许"新 music_track 直接起播"）。
 * 仅用于自检/日志断言：执行路径仍以 session 的 CAS 为准。 */
static inline bool ms_policy_transition_allowed(uint8_t from, uint8_t to)
{
    if (from == to) {
        return from == MS_POLICY_STATE_PLAYING; /* PLAYING→PLAYING = 切歌 */
    }
    switch (from) {
    case MS_POLICY_STATE_IDLE:
        return to == MS_POLICY_STATE_PLAYING;
    case MS_POLICY_STATE_PLAYING:
        return to == MS_POLICY_STATE_PAUSED || to == MS_POLICY_STATE_STOPPED ||
               to == MS_POLICY_STATE_ERROR;
    case MS_POLICY_STATE_PAUSED:
        return to == MS_POLICY_STATE_PLAYING || to == MS_POLICY_STATE_STOPPED ||
               to == MS_POLICY_STATE_ERROR;
    case MS_POLICY_STATE_STOPPED:
    case MS_POLICY_STATE_ERROR:
        return to == MS_POLICY_STATE_PLAYING; /* 重新点歌 */
    default:
        return false;
    }
}
