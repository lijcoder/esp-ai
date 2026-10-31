#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "esp_err.h"

/*
 * Opus 上行编码（L2 音频引擎）——架构 §3.6 定稿：16kHz / 单声道 / 16kbps / 20ms 一帧。
 *
 * 定位：纯编解码器，**不建任务、不碰网络**。调用方（T2.5 的 voice_cloud 任务）在
 * 自己的任务里按 320 样本一帧取 PCM → enc_encode() → ws_send_bin()。
 * 之所以不建任务：内部 RAM 只剩十几 KB（CONVENTIONS §4），编码任务栈 6~8KB 与
 * voice_cloud 任务栈重复，串在一个任务里最省；libopus 的临时内存走 PSRAM 伪栈
 * （microOpus 的 Kconfig），任务栈不需要为编码放大。
 *
 * 音频任务（afe_feed/afe_fetch）**禁止**调用本模块：编码是毫秒级 CPU 工作，
 * 且 libopus 首次调用会在堆上分配伪栈（音频任务内禁止 malloc）。
 */

#define CODEC_ENC_SAMPLE_RATE 16000
#define CODEC_ENC_CHANNELS 1
#define CODEC_ENC_FRAME_MS 20
/* 20ms @ 16kHz = 320 样本；与 audio_fe_uplink_read() 的成帧口径一致 */
#define CODEC_ENC_FRAME_SAMPLES (CODEC_ENC_SAMPLE_RATE / 1000 * CODEC_ENC_FRAME_MS)
/* 单帧输出缓冲上限：20ms@32kbps≈80B，取 512B 留足 VBR 尖峰与后续调码率 */
#define CODEC_ENC_MAX_PACKET 512

typedef struct {
    uint32_t frames;       /* 成功编码帧数 */
    uint32_t samples;      /* 成功编码样本数（= frames * 320） */
    uint32_t bytes;        /* 累计输出字节数 */
    uint32_t errors;       /* 编码失败次数 */
    uint32_t max_packet;   /* 观察到的单帧最大字节数 */
    uint32_t encode_us_sum;/* 累计编码耗时（µs），算均值用 */
    uint32_t encode_us_max;/* 单帧最大编码耗时（µs） */
    size_t carry_samples;  /* 尚未凑满一帧、留在内部 carry 缓冲的样本数 */
} codec_enc_stats_t;

/* 打开编码器（幂等）：按 Kconfig 配置 codec；状态/伪栈由 libopus 放置（见 Kconfig）。
 * 首次调用会在堆上分配编码器状态（PSRAM 优先），不要在音频任务里调用。 */
esp_err_t enc_open(void);

/* 关闭编码器并释放 libopus 分配的状态；关闭后 enc_encode() 返回 ESP_ERR_INVALID_STATE */
esp_err_t enc_close(void);

bool enc_is_open(void);

/* 新会话开始：复位 Opus 内部状态 + 丢弃 carry 残留（不清统计；统计用 enc_stats_reset） */
esp_err_t enc_reset(void);

/**
 * @brief 编码任意长度 PCM，内部 carry 缓冲负责把任意长度重组为 20ms 帧
 *
 * 允许传入非 320 整数倍的数据（例如 audio_fe_uplink_read() 超时只拿到一部分），
 * 未凑满一帧的尾部留在 carry，下次调用继续拼；会话结束用 enc_flush() 补零发尾帧。
 * 每次调用输出 0~N 个完整帧，`*out_len` 为写入字节数（首尾相接）。
 *
 * @param pcm      16k/单声道/int16 PCM（可为 NULL 当 samples=0）
 * @param samples  本次样本数
 * @param out      输出缓冲；容量需 >= 完整帧数 * CODEC_ENC_MAX_PACKET，
 *                 帧数 = (carry + samples) / 320（用 enc_carry_samples() 预估）
 * @param cap      输出缓冲容量（字节）
 * @param out_len  实际写入字节数（可为 NULL）
 * @return ESP_OK / ESP_ERR_INVALID_STATE（未打开）/ ESP_ERR_INVALID_ARG /
 *         ESP_ERR_INVALID_SIZE（cap 不足，此时不消费数据）/ ESP_FAIL（Opus 编码失败，已计数）
 */
esp_err_t enc_encode(const int16_t *pcm, size_t samples, uint8_t *out, size_t cap, size_t *out_len);

/**
 * @brief 会话收尾：把 carry 里的剩余样本补零成完整 20ms 帧编码输出
 *
 * @param out/ cap/out_len 同上；最多输出一帧
 * @return ESP_OK（含"carry 为空、输出 0 字节"）；其余同 enc_encode()
 */
esp_err_t enc_flush(uint8_t *out, size_t cap, size_t *out_len);

size_t enc_carry_samples(void);

/* 读统计快照（不修改状态；任意任务上下文可调用） */
esp_err_t enc_stats_read(codec_enc_stats_t *out);

/* 清统计窗口（帧数/字节/耗时/错误；不动编码器状态与 carry） */
esp_err_t enc_stats_reset(void);
