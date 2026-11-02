#pragma once

#include <stddef.h>
#include <stdint.h>

/*
 * 提示音采样点数与 PCM 数组声明（由 tools/prompt_gen.py 生成，勿手改）。
 *
 * 为什么单独生成一个头文件：prompt_assets.c 的静态表要求采样点数是编译期常量，
 * 而各数组的 sizeof 在别的编译单元不可见；把长度固化成宏后，各 prompt_*.c 再用
 * _Static_assert 校验自己的数组长度，避免「表里长度与数组不符」导致静默越界。
 * 重新生成提示音后本文件随之更新；T3.5 迁到 assets 分区后与本组件一起删除。
 */

#define PROMPT_SAMPLE_RATE_HZ 16000
#define PROMPT_WAKE_OK_SAMPLES 12800
#define PROMPT_NET_OFFLINE_SAMPLES 17201
#define PROMPT_CLOUD_ERROR_SAMPLES 23487
#define PROMPT_TIMEOUT_SAMPLES 17325
#define PROMPT_TTS_FAIL_SAMPLES 19769

extern const int16_t prompt_net_offline_pcm[];
extern const int16_t prompt_cloud_error_pcm[];
extern const int16_t prompt_timeout_pcm[];
extern const int16_t prompt_tts_fail_pcm[];
