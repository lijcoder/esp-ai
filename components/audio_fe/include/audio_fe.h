#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "esp_err.h"

/* 语音前端事件（本期只实现唤醒与 VAD 状态迁移） */
typedef enum {
    AUDIO_FE_EVT_WAKE = 0,          /* 唤醒词命中 */
    AUDIO_FE_EVT_VAD_SPEECH_START,  /* 人声开始 */
    AUDIO_FE_EVT_VAD_SPEECH_END,    /* 人声结束 */
} audio_fe_event_t;

typedef void (*audio_fe_cb_t)(audio_fe_event_t evt, void *arg);

/**
 * @brief 初始化 AFE（模型从 model 分区加载，输入格式由 Kconfig 决定，本期固定 "M"）
 *
 * @param cb   事件回调（在 fetch 任务上下文调用，回调内不要做耗时操作）
 * @param arg  回调参数
 */
esp_err_t audio_fe_init(audio_fe_cb_t cb, void *arg);

/* 创建 feed / fetch 两个任务并开始运行（feed 绑核 1） */
esp_err_t audio_fe_start(void);

/* 半双工控制：false 时麦克风数据丢弃，喂静音给 AFE（规则 R1/R2） */
void audio_fe_set_feed_enabled(bool enabled);
bool audio_fe_feed_enabled(void);

/* 已加载的唤醒词模型名，如 "wn9_hilexin"；未初始化时返回 NULL */
const char *audio_fe_wakenet_name(void);

/* ---------------- 麦克风观测统计（供 diag 的 mic stat 使用） ---------------- */

/*
 * 统计口径 = 麦克风真实采集数据（送 AFE 之前）。
 * 半双工暂停收音（R1）时仍继续累计，便于确认"麦克风还在采、只是 AFE 吃静音"。
 */
typedef struct {
    uint64_t sum_sq;          /* 平方和（int64 累加），调用方自行算 RMS */
    uint32_t sample_count;    /* 窗口内累计采样点数 */
    int32_t peak;             /* 窗口内绝对值峰值，满量程 32768 */
    uint32_t frames_fed;      /* 已投喂 AFE 的帧数（含补零帧） */
    uint32_t mic_read_errors; /* I2S 读失败次数 */
    uint32_t wake_count;      /* 唤醒命中次数 */
    uint32_t chunksize;       /* AFE 每帧采样数；0 表示 feed 任务未运行 */
    uint32_t sample_rate;     /* 麦克风采样率（Hz） */
    bool feed_enabled;        /* 当前是否收音（R1 暂停时为 false） */
    bool vad_active;          /* 当前 VAD 是否判定为人声 */
} audio_fe_stats_t;

/* 读取统计快照（不清零；任意任务上下文可调用） */
esp_err_t audio_fe_stats_read(audio_fe_stats_t *out);

/* 清零统计窗口（帧数、失败次数、唤醒次数、电平窗口一起清零） */
esp_err_t audio_fe_stats_reset(void);
