/*
 * prompt_assets_test —— assets 提示音资源表的 host 单测（不依赖 ESP-IDF）。
 *
 * 契约来源：build/t36_contract.md §5.3（T3.6）
 *   - PROMPT_COUNT == 5；每个 id 取到非空 clip、sample_rate==16000、samples>0；
 *   - 时长：wake 0.75~0.85 s；4 条错误提示 0.6~2.0 s；
 *   - 峰值 12000~14500（约 -8.7~-7.1 dBFS）；|mean|<100（无直流）；首/尾 20ms 内 |x|<200（防 pop）；
 *   - prompt_assets_name 与预期一致；非法 id 返回 NULL / "?"。
 *
 * 为什么能在 host 跑：assets 资源/表只依赖 <stddef.h>/<stdint.h>，没有 esp_log/FreeRTOS，
 * 直接 `cc` 编译即可；无需串口/硬件/IDF。
 */
#include <stdarg.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

#include "prompt_assets.h"

#define EXPECT_SAMPLE_RATE 16000u
#define EDGE_MS 20                     /* 首尾淡入淡出窗口 */
#define EDGE_ABS_MAX 200               /* 窗口内 |x| 上限（防 pop） */
#define PEAK_MIN 12000                 /* 契约峰值范围（约 -8.7~-7.1 dBFS） */
#define PEAK_MAX 14500
#define DC_ABS_MAX 100.0               /* |mean| 上限（无直流偏置） */
#define WAKE_DUR_MIN 0.75
#define WAKE_DUR_MAX 0.85
#define ERROR_DUR_MIN 0.60
#define ERROR_DUR_MAX 2.00

_Static_assert(PROMPT_COUNT == 5, "T3.6 契约：提示音表必须恰好 5 项（wake + 4 条错误提示）");

static int s_pass;
static int s_fail;

#if defined(__GNUC__) || defined(__clang__)
static void check(bool ok, const char *fmt, ...) __attribute__((format(printf, 2, 3)));
#endif

static void check(bool ok, const char *fmt, ...)
{
    va_list ap;
    va_start(ap, fmt);
    printf("  %s  ", ok ? "PASS" : "FAIL");
    vprintf(fmt, ap);
    printf("\n");
    va_end(ap);
    if (ok) {
        s_pass++;
    } else {
        s_fail++;
    }
}

static int abs_int32(int32_t value)
{
    return value < 0 ? -value : value;
}

int main(void)
{
    static const char *const k_names[PROMPT_COUNT] = {
        "wake", "net-offline", "cloud-error", "timeout", "tts-fail",
    };
    /* wake 是 0.75~0.85s；其余 4 条错误提示 0.6~2.0s */
    static const bool k_is_wake[PROMPT_COUNT] = {true, false, false, false, false};

    printf("===== T3.6 host 单测：assets 提示音资源表 =====\n");

    check(PROMPT_COUNT == 5, "PROMPT_COUNT==5（实际 %d）", (int)PROMPT_COUNT);

    for (int id = 0; id < (int)PROMPT_COUNT; id++) {
        const prompt_id_t pid = (prompt_id_t)id;
        const char *name = k_names[id];
        const prompt_clip_t *clip = prompt_assets_get(pid);

        check(clip != NULL, "%s: prompt_assets_get 非空", name);
        if (clip == NULL) {
            continue;
        }
        check(clip->pcm != NULL, "%s: pcm 指针非空", name);
        check(clip->samples > 0, "%s: samples>0（实际 %zu）", name, clip->samples);
        check(clip->sample_rate == EXPECT_SAMPLE_RATE,
              "%s: sample_rate==%u（实际 %u）", name, EXPECT_SAMPLE_RATE, clip->sample_rate);
        if (clip->pcm == NULL || clip->samples == 0 || clip->sample_rate == 0) {
            continue;
        }

        const double duration_s = (double)clip->samples / (double)clip->sample_rate;
        if (k_is_wake[id]) {
            check(duration_s >= WAKE_DUR_MIN && duration_s <= WAKE_DUR_MAX,
                  "%s: 时长 %.3f s 在 %.2f~%.2f s（唤醒应答）", name, duration_s,
                  WAKE_DUR_MIN, WAKE_DUR_MAX);
        } else {
            check(duration_s >= ERROR_DUR_MIN && duration_s <= ERROR_DUR_MAX,
                  "%s: 时长 %.3f s 在 %.2f~%.2f s（错误提示）", name, duration_s,
                  ERROR_DUR_MIN, ERROR_DUR_MAX);
        }

        int32_t peak = 0;
        int64_t sum = 0;
        for (size_t i = 0; i < clip->samples; i++) {
            const int32_t sample = clip->pcm[i];
            const int32_t magnitude = abs_int32(sample);
            if (magnitude > peak) {
                peak = magnitude;
            }
            sum += sample;
        }
        check(peak >= PEAK_MIN && peak <= PEAK_MAX,
              "%s: 峰值 %d 在 %d~%d（归一化 ±3%%）", name, (int)peak, PEAK_MIN, PEAK_MAX);

        const double mean = (double)sum / (double)clip->samples;
        const double mean_abs = mean < 0 ? -mean : mean;
        check(mean_abs < DC_ABS_MAX, "%s: |mean|=%.2f < %.0f（无直流偏置）", name, mean_abs,
              DC_ABS_MAX);

        const size_t edge = (size_t)clip->sample_rate * EDGE_MS / 1000u;
        if (clip->samples <= 2 * edge) {
            check(false, "%s: samples 过短（%zu ≤ 2×%zu），无法检查首尾淡入淡出", name,
                  clip->samples, edge);
        } else {
            int32_t head_peak = 0;
            int32_t tail_peak = 0;
            for (size_t i = 0; i < edge; i++) {
                const int32_t head = abs_int32(clip->pcm[i]);
                const int32_t tail = abs_int32(clip->pcm[clip->samples - 1 - i]);
                if (head > head_peak) {
                    head_peak = head;
                }
                if (tail > tail_peak) {
                    tail_peak = tail;
                }
            }
            check(head_peak < EDGE_ABS_MAX, "%s: 首 %ums 内峰值 %d < %d（防 pop）", name,
                  (unsigned)EDGE_MS, (int)head_peak, EDGE_ABS_MAX);
            check(tail_peak < EDGE_ABS_MAX, "%s: 尾 %ums 内峰值 %d < %d（防 pop）", name,
                  (unsigned)EDGE_MS, (int)tail_peak, EDGE_ABS_MAX);
        }

        const char *actual_name = prompt_assets_name(pid);
        check(actual_name != NULL && strcmp(actual_name, name) == 0,
              "%s: prompt_assets_name == \"%s\"（实际 \"%s\"）", name, name,
              actual_name != NULL ? actual_name : "(null)");
    }

    /* 非法 id：契约要求取资源返回 NULL、名字返回 "?" */
    check(prompt_assets_get((prompt_id_t)PROMPT_COUNT) == NULL, "非法 id=PROMPT_COUNT: 返回 NULL");
    check(prompt_assets_get((prompt_id_t)-1) == NULL, "非法 id=-1: 返回 NULL");
    check(prompt_assets_name((prompt_id_t)PROMPT_COUNT) != NULL &&
              strcmp(prompt_assets_name((prompt_id_t)PROMPT_COUNT), "?") == 0,
          "非法 id=PROMPT_COUNT: name 返回 \"?\"");
    check(prompt_assets_name((prompt_id_t)-1) != NULL &&
              strcmp(prompt_assets_name((prompt_id_t)-1), "?") == 0,
          "非法 id=-1: name 返回 \"?\"");

    /* 参考信息（不参与判定）：wake 是否复用旧数组，避免重复 rodata */
    const prompt_clip_t *wake = prompt_assets_get(PROMPT_WAKE_OK);
    if (wake != NULL &&
        (wake->pcm == prompt_wo_zai_ne_pcm || wake->samples == prompt_wo_zai_ne_samples)) {
        printf("  INFO  wake 与旧数组 prompt_wo_zai_ne_pcm 一致（无重复 rodata）\n");
    }

    printf("\nPROMPT_ASSETS_TEST: %s（%d/%d 项）\n", s_fail == 0 ? "PASS" : "FAIL", s_pass,
           s_pass + s_fail);
    return s_fail == 0 ? 0 : 1;
}
