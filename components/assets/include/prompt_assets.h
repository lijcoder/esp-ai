#pragma once

#include <stddef.h>
#include <stdint.h>

/*
 * 本地提示音资源（临时方案：编译进固件；P3 T3.5 迁到 assets 分区）
 * 格式：16bit 单声道 PCM，16kHz
 */
extern const int16_t prompt_wo_zai_ne_pcm[];
extern const size_t prompt_wo_zai_ne_samples;
