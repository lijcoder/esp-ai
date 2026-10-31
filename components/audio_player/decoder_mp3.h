#pragma once

/*
 * MP3 解码（audio_player 内部接口，T2.3 整段 / T2.4 流式）。
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

/* ---------------- 流式解码（T2.4，TTS 下行） ----------------
 * 与 decode_all 共用同一份 PSRAM 状态（同一时刻只会用其中一条路径）。
 * 线程约定：全部函数只在 player 任务内调用；ring 满丢块需要重新同步时，
 * audio_player 先记录标志、回到 player 任务再调 mp3_dec_stream_resync()。
 *
 * 跨 chunk 的半帧：数据先进 PSRAM pending 区；只有凑满一个完整帧（按帧头长度判断）
 * 才交给 minimp3，避免 minimp3 在"数据不足"分支里 memset 解码器状态（会丢比特池）。
 */
void mp3_dec_stream_reset(void);

/* 追加一段下行 MP3 字节。单次超过内部 pending 容量返回 ESP_ERR_NO_MEM（丢弃整段）。 */
esp_err_t mp3_dec_stream_feed(const uint8_t *data, size_t len);

/* 尝试解出下一帧（内部会跳过 ID3v2/垃圾字节并计数）：
 *   ESP_OK            → *pcm 指向内部 PSRAM 单声道帧缓冲，参数有效（下次调用前有效）
 *   ESP_ERR_NOT_FOUND → 数据不足，需要继续 feed
 *   ESP_ERR_INVALID_STATE / INVALID_ARG → 未初始化或参数为空 */
esp_err_t mp3_dec_stream_frame(const int16_t **pcm, uint32_t *samples, uint32_t *rate,
                               uint8_t *channels, uint32_t *frame_bytes);

/* 丢弃 pending 里的半帧并重新找同步字（ring 满丢块后调用）。 */
void mp3_dec_stream_resync(void);
