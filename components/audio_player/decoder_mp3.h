#pragma once

/*
 * MP3 解码（audio_player 内部接口，T2.3）。
 * 对外只暴露在 audio_player.h：本头文件不导出给其它组件。
 */

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "esp_err.h"

typedef struct {
    uint32_t frames;          /* 成功解码的 MP3 帧数 */
    uint32_t samples;         /* 每声道解码样本数（源采样率） */
    uint32_t rate;            /* 源采样率（Hz） */
    uint8_t channels;         /* 源声道数（1/2） */
    uint32_t skipped_bytes;   /* ID3/sync 搜索等被跳过的字节数 */
    uint32_t bad_frames;      /* 解帧失败次数 */
    uint32_t decode_us_total; /* 累计解码耗时（us） */
    uint32_t decode_us_max;   /* 单帧最大解码耗时（us） */
} mp3_dec_stats_t;

/* 解码器状态与 PCM 缓冲全部预分配在 PSRAM（播放任务内禁止 malloc）。
 * 失败时 MP3 能力降级为不可用，提示音/上行链路不受影响。 */
esp_err_t mp3_dec_init(void);
bool mp3_dec_ready(void);

/* 解码整段 MP3 → 单声道 int16 PCM（源采样率）。
 * 输出指向内部静态 PSRAM 缓冲，下次调用覆盖；不重采样（由播放任务统一做）。
 * 返回 ESP_ERR_NO_MEM 表示素材超过 AUDIO_PLAYER_MP3_PCM_MAX_BYTES。 */
esp_err_t mp3_dec_decode_all(const uint8_t *data, size_t len,
                             const int16_t **pcm, size_t *samples, uint32_t *rate);

void mp3_dec_stats_read(mp3_dec_stats_t *out);
void mp3_dec_stats_reset(void);
