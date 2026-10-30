#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "esp_err.h"

/* 采样率固定（见 docs/ARCHITECTURE.md §2、§3） */
#define AUDIO_IO_MIC_SAMPLE_RATE 16000
#define AUDIO_IO_SPK_SAMPLE_RATE 48000

/**
 * @brief 初始化两路 I2S：I2S0 = INMP441 采集（16k/32bit 槽位/单声道左声道），
 *        I2S1 = MAX98357A 播放（48k/16bit/立体声，单声道数据复制到左右）。
 */
esp_err_t audio_io_init(void);

/**
 * @brief 读取麦克风数据（已转 int16 单声道）
 *
 * @param dst         输出缓冲区，长度 >= frames
 * @param frames      期望帧数（16k 采样点）
 * @param timeout_ms  超时，0 表示不等待
 * @return 实际读到的帧数，<0 表示失败
 */
int audio_io_read_mic(int16_t *dst, size_t frames, uint32_t timeout_ms);

/**
 * @brief 写播放数据（48k/16bit/立体声交织）
 *
 * @param data        交织的左右声道数据
 * @param frames      帧数（每帧 2 个 int16）
 * @param timeout_ms  超时，0 表示不等待
 * @return 实际写入帧数，<0 表示失败
 */
int audio_io_write_spk(const int16_t *data, size_t frames, uint32_t timeout_ms);

/* 播放通道时钟开关（播放前开、播放结束后关，避免静音期底噪与 pop） */
esp_err_t audio_io_spk_enable(bool enable);
bool audio_io_spk_is_enabled(void);
