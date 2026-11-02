/*
 * feedback_policy_test —— 异常反馈纯逻辑的 host 单测（不依赖 ESP-IDF）。
 *
 * 契约来源：build/t36_contract.md §3.2（T3.6）
 *   云端 error code 3001/3002 → FB_KIND_TTS_FAIL（播「语音播放失败」）；
 *   其余（1/1001/2001/2002/9001/0/未知）→ FB_KIND_CLOUD_ERROR（播「服务出了点问题」）。
 * 同时校验 kind 数值（写入 APP_EVT_FEEDBACK.payload.kind，event_bus 用数值契约）、
 * 名称与合法性判定。
 *
 * 为什么能在 host 跑：feedback_policy.h 是 static inline 纯逻辑头，无 esp_log/FreeRTOS。
 */
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <inttypes.h>

#include "feedback_policy.h"

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

static void check_cloud_code(int32_t code, fb_kind_t expect, const char *expect_name)
{
    char label[128];
    const fb_kind_t actual = fb_kind_from_cloud_code(code);
    snprintf(label, sizeof(label), "cloud code %" PRId32 " → %s（实际 %s）", code, expect_name,
             fb_kind_name(actual));
    check(actual == expect, label);
}

int main(void)
{
    printf("===== T3.6 host 单测：feedback_policy（云端错误码 → 反馈类别）=====\n");

    /* fb_kind_t 数值是 APP_EVT_FEEDBACK payload 的契约（contract §3.1）：1/2/3/4 */
    check(FB_KIND_NONE == 0, "FB_KIND_NONE == 0");
    check(FB_KIND_NET_OFFLINE == 1, "FB_KIND_NET_OFFLINE == 1（payload kind=1）");
    check(FB_KIND_CLOUD_ERROR == 2, "FB_KIND_CLOUD_ERROR == 2（payload kind=2）");
    check(FB_KIND_TIMEOUT == 3, "FB_KIND_TIMEOUT == 3（payload kind=3）");
    check(FB_KIND_TTS_FAIL == 4, "FB_KIND_TTS_FAIL == 4（payload kind=4）");
    check(FB_KIND_COUNT == 5, "FB_KIND_COUNT == 5（哨兵）");

    /* TTS 失败类：3001=TTS 合成/下发失败、3002=TTS 超时 */
    check_cloud_code(3001, FB_KIND_TTS_FAIL, "tts-fail");
    check_cloud_code(3002, FB_KIND_TTS_FAIL, "tts-fail");

    /* 其余全部归通用"服务异常" */
    static const int32_t k_cloud_codes[] = {1, 1001, 2001, 2002, 9001, 0, -1, 12345, 2999, 3000,
                                           3003, 65535};
    for (size_t i = 0; i < sizeof(k_cloud_codes) / sizeof(k_cloud_codes[0]); i++) {
        check_cloud_code(k_cloud_codes[i], FB_KIND_CLOUD_ERROR, "cloud-error");
    }

    /* 类别名称（FB 日志与测试脚本断言用） */
    check(strcmp(fb_kind_name(FB_KIND_NET_OFFLINE), "net-offline") == 0,
          "name(NET_OFFLINE) == \"net-offline\"");
    check(strcmp(fb_kind_name(FB_KIND_CLOUD_ERROR), "cloud-error") == 0,
          "name(CLOUD_ERROR) == \"cloud-error\"");
    check(strcmp(fb_kind_name(FB_KIND_TIMEOUT), "timeout") == 0, "name(TIMEOUT) == \"timeout\"");
    check(strcmp(fb_kind_name(FB_KIND_TTS_FAIL), "tts-fail") == 0,
          "name(TTS_FAIL) == \"tts-fail\"");
    check(strcmp(fb_kind_name(FB_KIND_NONE), "?") == 0, "name(NONE) == \"?\"");
    check(strcmp(fb_kind_name(FB_KIND_COUNT), "?") == 0, "name(COUNT) == \"?\"");
    check(strcmp(fb_kind_name((fb_kind_t)99), "?") == 0, "name(越界 99) == \"?\"");

    /* 合法性判定：只有 1..4 合法（0=NONE 与 COUNT 都不能拿来 notify） */
    check(!fb_kind_valid(FB_KIND_NONE), "valid(NONE) == false");
    check(!fb_kind_valid(FB_KIND_COUNT), "valid(COUNT) == false");
    check(!fb_kind_valid((fb_kind_t)-1), "valid(-1) == false");
    check(fb_kind_valid(FB_KIND_NET_OFFLINE), "valid(NET_OFFLINE) == true");
    check(fb_kind_valid(FB_KIND_CLOUD_ERROR), "valid(CLOUD_ERROR) == true");
    check(fb_kind_valid(FB_KIND_TIMEOUT), "valid(TIMEOUT) == true");
    check(fb_kind_valid(FB_KIND_TTS_FAIL), "valid(TTS_FAIL) == true");

    printf("\nFEEDBACK_POLICY_TEST: %s（%d/%d 项）\n", s_fail == 0 ? "PASS" : "FAIL", s_pass,
           s_pass + s_fail);
    return s_fail == 0 ? 0 : 1;
}
