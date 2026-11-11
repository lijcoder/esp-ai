/*
 * music_policy_test —— 音乐编排纯逻辑的 host 单测（不依赖 ESP-IDF）。
 *
 * 契约来源：build/t43_contract.md §3/§4/§5、build/t44_contract.md §2/§4
 *   - 语音活跃态 → auto-pause；语音回 IDLE **保持暂停**（T4.4 pause 模式，无 auto-resume）；
 *   - pause_reason 优先级：用户（1）> 语音抢占（2）；
 *   - mid 过滤：music_track 只接受更新（同 mid 重复）；music_control 允许同 mid，只丢更旧；
 *   - music_control 应用结果（pause/resume/stop 与当前状态的组合）；
 *   - resume 失败兜底判定（开关打开、本暂停周期未发过、确实处于 PAUSED）；
 *   - 状态迁移表（契约 §3）。
 *
 * 为什么能在 host 跑：music_policy.h 是 static inline 纯逻辑头，无 esp_log/FreeRTOS/sdkconfig。
 */
#include <inttypes.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

#include "music_policy.h"

static int s_pass;
static int s_fail;

static void check(bool ok, const char *what)
{
    printf("  %s  %s\n", ok ? "PASS" : "FAIL", what);
    if (ok) {
        s_pass++;
    } else {
        s_fail++;
    }
}

int main(void)
{
    printf("===== T4.3/T4.4 host 单测：music_policy（暂停抢占/保持暂停/mid/控制/兜底）=====\n");

    /* 状态/pause_reason 数值是 ms_info_t 与事件 payload 的契约（契约 §7） */
    check(MS_POLICY_STATE_IDLE == 0 && MS_POLICY_STATE_PLAYING == 1 &&
              MS_POLICY_STATE_PAUSED == 2 && MS_POLICY_STATE_STOPPED == 3 &&
              MS_POLICY_STATE_ERROR == 4,
          "状态数值 0/1/2/3/4 与 mc_state_t 一致");
    check(MS_POLICY_REASON_NONE == 0 && MS_POLICY_REASON_USER == 1 &&
              MS_POLICY_REASON_VOICE == 2,
          "pause_reason 数值 0/1/2（无/用户/语音）");

    /* 语音活跃态：LISTENING(1)/THINKING(2)/SPEAKING(3)/ERROR(4)/FOLLOW_UP(5) */
    static const uint8_t k_active[] = {1, 2, 3, 4, 5};
    for (size_t i = 0; i < sizeof(k_active) / sizeof(k_active[0]); i++) {
        char label[64];
        snprintf(label, sizeof(label), "voice_state=%u 是活跃态", k_active[i]);
        check(ms_policy_voice_active(k_active[i]), label);
    }
    check(!ms_policy_voice_active(0), "voice_state=0（IDLE）不是活跃态");
    check(!ms_policy_voice_active(6), "voice_state=6（越界）不是活跃态");
    check(!ms_policy_voice_active(255), "voice_state=255 不是活跃态");

    /* auto-pause：仅 PLAYING + 非用户暂停 + 语音活跃 + 开关打开 */
    check(ms_policy_auto_pause(MS_POLICY_STATE_PLAYING, MS_POLICY_REASON_NONE, 1, true),
          "auto-pause：playing + reason=0 + LISTENING");
    check(ms_policy_auto_pause(MS_POLICY_STATE_PLAYING, MS_POLICY_REASON_NONE, 5, true),
          "auto-pause：playing + reason=0 + FOLLOW_UP");
    check(ms_policy_auto_pause(MS_POLICY_STATE_PLAYING, MS_POLICY_REASON_NONE, 4, true),
          "auto-pause：playing + reason=0 + ERROR（错误提示音也让路）");
    check(!ms_policy_auto_pause(MS_POLICY_STATE_PLAYING, MS_POLICY_REASON_NONE, 0, true),
          "不 auto-pause：语音回 IDLE");
    check(!ms_policy_auto_pause(MS_POLICY_STATE_PLAYING, MS_POLICY_REASON_USER, 3, true),
          "不 auto-pause：用户暂停 latch（reason=1）");
    check(!ms_policy_auto_pause(MS_POLICY_STATE_PAUSED, MS_POLICY_REASON_VOICE, 3, true),
          "不 auto-pause：已经 paused");
    check(!ms_policy_auto_pause(MS_POLICY_STATE_PLAYING, MS_POLICY_REASON_NONE, 3, false),
          "不 auto-pause：Kconfig 关闭");
    check(!ms_policy_auto_pause(MS_POLICY_STATE_STOPPED, MS_POLICY_REASON_NONE, 3, true),
          "不 auto-pause：stopped");

    /* T4.4（2026-11-11 用户定稿）：语音回 IDLE **不自动恢复**音乐（pause 模式）。
     * 这里不再有 auto-resume 策略；显式"继续"走 music_control，不在此纯逻辑层判定。 */

    /* pause_reason 优先级：用户 > 语音 */
    check(ms_policy_pause_reason_merge(0, 0) == 0, "merge(0,0)=0");
    check(ms_policy_pause_reason_merge(0, 1) == 1, "merge(0,用户)=用户");
    check(ms_policy_pause_reason_merge(0, 2) == 2, "merge(0,语音)=语音");
    check(ms_policy_pause_reason_merge(1, 2) == 1, "merge(用户,语音)=用户（latch 不被覆盖）");
    check(ms_policy_pause_reason_merge(2, 1) == 1, "merge(语音,用户)=用户");
    check(ms_policy_pause_reason_merge(2, 2) == 2, "merge(语音,语音)=语音");
    check(ms_policy_pause_reason_merge(1, 1) == 1, "merge(用户,用户)=用户");
    check(ms_policy_pause_reason_merge(1, 0) == 1, "merge(用户,无)=用户");
    check(ms_policy_pause_reason_merge(2, 0) == 2, "merge(语音,无)=语音");

    /* mid 过滤：track 只接受更新，同 mid 重复 */
    check(ms_policy_mid_verdict(false, 0, 42, true) == MS_POLICY_MID_OK,
          "track：没有基准时接受任意 mid");
    check(ms_policy_mid_verdict(true, 5, 6, true) == MS_POLICY_MID_OK, "track：mid 5→6 接受");
    check(ms_policy_mid_verdict(true, 5, 5, true) == MS_POLICY_MID_DUP, "track：同 mid 视为重复");
    check(ms_policy_mid_verdict(true, 5, 4, true) == MS_POLICY_MID_STALE, "track：旧 mid 丢弃");
    check(ms_policy_mid_verdict(true, 65535, 0, true) == MS_POLICY_MID_OK,
          "track：uint16 回绕 65535→0 接受");
    check(ms_policy_mid_verdict(true, 0, 65535, true) == MS_POLICY_MID_STALE,
          "track：uint16 回绕 0←65535 丢弃");

    /* mid 过滤：control 允许同 mid，只丢更旧 */
    check(ms_policy_mid_verdict(false, 0, 42, false) == MS_POLICY_MID_OK,
          "control：没有基准时接受任意 mid");
    check(ms_policy_mid_verdict(true, 5, 5, false) == MS_POLICY_MID_OK,
          "control：同 mid 接受（指向当前曲目）");
    check(ms_policy_mid_verdict(true, 5, 4, false) == MS_POLICY_MID_STALE,
          "control：旧 mid 丢弃");
    check(ms_policy_mid_verdict(true, 5, 6, false) == MS_POLICY_MID_OK,
          "control：更新的 mid 接受");
    check(ms_policy_mid_verdict(true, 65535, 0, false) == MS_POLICY_MID_OK,
          "control：uint16 回绕 65535→0 接受");

    /* music_control 应用结果 */
    check(ms_policy_control(MS_POLICY_STATE_PLAYING, 0) == MS_POLICY_CTRL_PAUSE,
          "control pause：playing → PAUSE");
    check(ms_policy_control(MS_POLICY_STATE_PAUSED, 0) == MS_POLICY_CTRL_LATCH,
          "control pause：paused → LATCH（更新用户暂停原因）");
    check(ms_policy_control(MS_POLICY_STATE_IDLE, 0) == MS_POLICY_CTRL_IGNORE,
          "control pause：idle 忽略");
    check(ms_policy_control(MS_POLICY_STATE_STOPPED, 0) == MS_POLICY_CTRL_IGNORE,
          "control pause：stopped 忽略");
    check(ms_policy_control(MS_POLICY_STATE_ERROR, 0) == MS_POLICY_CTRL_IGNORE,
          "control pause：error 忽略");
    check(ms_policy_control(MS_POLICY_STATE_PAUSED, 1) == MS_POLICY_CTRL_RESUME,
          "control resume：paused → RESUME");
    check(ms_policy_control(MS_POLICY_STATE_PLAYING, 1) == MS_POLICY_CTRL_IGNORE,
          "control resume：playing 忽略");
    check(ms_policy_control(MS_POLICY_STATE_IDLE, 1) == MS_POLICY_CTRL_IGNORE,
          "control resume：idle 忽略");
    check(ms_policy_control(MS_POLICY_STATE_PLAYING, 2) == MS_POLICY_CTRL_STOP,
          "control stop：playing → STOP");
    check(ms_policy_control(MS_POLICY_STATE_PAUSED, 2) == MS_POLICY_CTRL_STOP,
          "control stop：paused → STOP（丢弃 ring）");
    check(ms_policy_control(MS_POLICY_STATE_IDLE, 2) == MS_POLICY_CTRL_IGNORE,
          "control stop：idle 忽略");
    check(ms_policy_control(MS_POLICY_STATE_ERROR, 2) == MS_POLICY_CTRL_IGNORE,
          "control stop：error 忽略");
    check(ms_policy_control(MS_POLICY_STATE_PLAYING, 99) == MS_POLICY_CTRL_IGNORE,
          "control 非法 action 忽略");

    /* resume 失败兜底 */
    check(ms_policy_resume_fallback(true, false, MS_POLICY_STATE_PAUSED),
          "兜底：开关开 + 未发过 + paused → 发");
    check(!ms_policy_resume_fallback(true, true, MS_POLICY_STATE_PAUSED),
          "兜底：本暂停周期已发过 → 不再发");
    check(!ms_policy_resume_fallback(false, false, MS_POLICY_STATE_PAUSED),
          "兜底：Kconfig 关闭 → 不发");
    check(!ms_policy_resume_fallback(true, false, MS_POLICY_STATE_PLAYING),
          "兜底：playing → 不发");
    check(!ms_policy_resume_fallback(true, false, MS_POLICY_STATE_STOPPED),
          "兜底：stopped → 不发");

    /* 状态迁移表（契约 §3） */
    check(ms_policy_transition_allowed(MS_POLICY_STATE_IDLE, MS_POLICY_STATE_PLAYING),
          "迁移：idle → playing");
    check(!ms_policy_transition_allowed(MS_POLICY_STATE_IDLE, MS_POLICY_STATE_PAUSED),
          "迁移：idle → paused 不允许");
    check(ms_policy_transition_allowed(MS_POLICY_STATE_PLAYING, MS_POLICY_STATE_PLAYING),
          "迁移：playing → playing（切歌）");
    check(ms_policy_transition_allowed(MS_POLICY_STATE_PLAYING, MS_POLICY_STATE_PAUSED),
          "迁移：playing → paused");
    check(ms_policy_transition_allowed(MS_POLICY_STATE_PLAYING, MS_POLICY_STATE_STOPPED),
          "迁移：playing → stopped");
    check(ms_policy_transition_allowed(MS_POLICY_STATE_PLAYING, MS_POLICY_STATE_ERROR),
          "迁移：playing → error");
    check(!ms_policy_transition_allowed(MS_POLICY_STATE_PLAYING, MS_POLICY_STATE_IDLE),
          "迁移：playing → idle 不允许（经 stopped/error）");
    check(ms_policy_transition_allowed(MS_POLICY_STATE_PAUSED, MS_POLICY_STATE_PLAYING),
          "迁移：paused → playing");
    check(ms_policy_transition_allowed(MS_POLICY_STATE_PAUSED, MS_POLICY_STATE_STOPPED),
          "迁移：paused → stopped");
    check(ms_policy_transition_allowed(MS_POLICY_STATE_PAUSED, MS_POLICY_STATE_ERROR),
          "迁移：paused → error");
    check(ms_policy_transition_allowed(MS_POLICY_STATE_STOPPED, MS_POLICY_STATE_PLAYING),
          "迁移：stopped → playing（重新点歌）");
    check(ms_policy_transition_allowed(MS_POLICY_STATE_ERROR, MS_POLICY_STATE_PLAYING),
          "迁移：error → playing（重新点歌）");
    check(!ms_policy_transition_allowed(MS_POLICY_STATE_STOPPED, MS_POLICY_STATE_PAUSED),
          "迁移：stopped → paused 不允许");
    check(!ms_policy_transition_allowed(MS_POLICY_STATE_PAUSED, MS_POLICY_STATE_PAUSED),
          "迁移：paused → paused 不允许（幂等不产生迁移）");

    printf("\nMUSIC_POLICY_TEST: %s（%d/%d 项）\n", s_fail == 0 ? "PASS" : "FAIL", s_pass,
           s_pass + s_fail);
    return s_fail == 0 ? 0 : 1;
}
