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


/* ---------------- MP3 解码播放（T2.3，minimp3） ----------------
 * 状态与 PCM 缓冲在 audio_player_init() 里一次性分配到 PSRAM（播放任务内禁止 malloc）；
 * 同一时刻只允许一段 MP3（解码输出复用同一块 PSRAM 缓冲），播放中再调用返回
 * ESP_ERR_INVALID_STATE。解码 + 重采样 + 淡入淡出 + 音量都在播放任务内串行完成。
 */
esp_err_t audio_player_play_mp3(const uint8_t *mp3, size_t len);

/* 是否具备 MP3 能力（PSRAM 分配失败时降级为不可用，提示音/上行链路不受影响） */
bool audio_player_mp3_ready(void);

/* 播放统计与最近一次 MP3 解码统计（diag 的 player stat 用；任意任务上下文可读） */
typedef struct {
    uint32_t frames;        /* 解码帧数 */
    uint32_t samples;       /* 每声道样本数（源采样率） */
    uint32_t rate;          /* 源采样率（Hz） */
    uint8_t channels;       /* 源声道数（1/2） */
    uint32_t duration_ms;   /* samples / rate */
    uint32_t skipped_bytes; /* ID3/sync 等跳过的字节 */
    uint32_t bad_frames;    /* 解帧失败次数 */
    uint32_t decode_us_avg; /* 平均单帧解码耗时 */
    uint32_t decode_us_max; /* 最大单帧解码耗时 */
} audio_player_mp3_stats_t;

typedef struct {
    uint32_t jobs_done;   /* 完成的播放次数（PCM + MP3） */
    uint32_t pcm_jobs;    /* 其中 PCM */
    uint32_t mp3_jobs;    /* 其中 MP3（成功解码） */
    uint32_t mp3_errors;  /* MP3 解码失败次数 */
    uint32_t last_play_ms;/* 最近一次播放实际耗时（含尾部静音冲刷，不含 300ms R2 延时） */
    audio_player_mp3_stats_t mp3;
} audio_player_stats_t;

/* 开发期探针（diag 的 player pcm 用）：读取最近一次 MP3 解码的单声道 PCM，
 * 让 PC 侧能与 FFmpeg/PyAV 参考解码逐样本对拍——"能出声"之外的真机正确性证据。 */
esp_err_t audio_player_mp3_pcm_read(int16_t *dst, size_t offset, size_t count);

esp_err_t audio_player_stats_read(audio_player_stats_t *out);
void audio_player_stats_reset(void);

/* 音量 0~100（软件数字音量） */
esp_err_t audio_player_set_volume(uint8_t volume);
uint8_t audio_player_get_volume(void);
