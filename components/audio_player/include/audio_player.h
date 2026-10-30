#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "esp_err.h"

typedef void (*audio_player_done_cb_t)(void *arg);

/**
 * @brief 初始化播放引擎（单播放任务，绑核 1）
 */
esp_err_t audio_player_init(void);

/**
 * @brief 注册"一次播放完成"回调
 *
 * 回调在播放任务上下文执行（已包含淡出、静音冲刷与尾部延时），
 * 适合用来恢复半双工 feed，回调内不要做耗时操作。
 */
void audio_player_register_done_cb(audio_player_done_cb_t cb, void *arg);

/**
 * @brief 播放一段 PCM（16bit 单声道，任意采样率，内部重采样到 48k）
 *
 * @param pcm          PCM 数据（调用方保证生命周期覆盖播放过程）
 * @param samples      采样点数量（单声道）
 * @param sample_rate  源采样率，如 16000
 * @return ESP_OK / ESP_ERR_INVALID_ARG / ESP_ERR_TIMEOUT（队列满）
 */
esp_err_t audio_player_play_pcm(const int16_t *pcm, size_t samples, uint32_t sample_rate);

/* 是否有播放任务在进行（含队列中的任务） */
bool audio_player_is_busy(void);

/* 音量 0~100（软件数字音量） */
esp_err_t audio_player_set_volume(uint8_t volume);
uint8_t audio_player_get_volume(void);
