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

/* ---------------- 上行 16k 单声道 PCM 输出（T2.1） ----------------
 *
 * 数据源 = AFE fetch 输出（已降噪的 16k/单声道/int16），不是原始麦克风。
 * audio_fe 在 afe_fetch 任务里**只做一次 memcpy 写入 PSRAM 环形缓冲**；
 * 编码/发送必须由消费端在自己的任务里做（CONVENTIONS §3：音频任务内禁止
 * malloc/网络/阻塞），这也是不用"回调"而用环形缓冲的原因。
 *
 * 使用约定（详见 audio_fe README）：
 *   1. 进入 LISTENING 前 start()，进入 SPEAKING（R1）或打断时 stop()；
 *   2. feed 暂停（R1）时 AFE 吃静音、输出也是静音，**不能**用 feed 开关代替 stop；
 *   3. read() 按需取样本，AFE 每帧长度由实现决定，**不要假设等于 Opus 20ms 帧**；
 *   4. stop() 会丢弃残留数据；统计为累计值，可单独 reset。
 */

/* 分配缓冲并开始写入（清空残留 + 复位本次会话统计）；未初始化/未分配返回错误 */
esp_err_t audio_fe_uplink_start(void);

/* 停止写入并丢弃残留数据（不影响 feed 状态） */
void audio_fe_uplink_stop(void);

bool audio_fe_uplink_active(void);

/**
 * @brief 精确读取最多 samples 个样本（阻塞到够数或超时）
 *
 * @param dst         输出缓冲，由调用方提供（接口内不分配内存）
 * @param samples     期望样本数（16k 单声道 int16）
 * @param timeout_ms  超时；0 表示不等待
 * @return >0 实际读到的样本数（超时可能少于 samples）；0 无数据（未启动/超时）；
 *         <0 为 -esp_err_t
 */
int audio_fe_uplink_read(int16_t *dst, size_t samples, uint32_t timeout_ms);

typedef struct {
    uint32_t samples_written; /* AFE 输出累计写入样本数（16k 单声道） */
    uint32_t samples_read;    /* 消费端累计读走样本数 */
    uint32_t samples_dropped; /* 环形缓冲满时丢弃的最旧样本数 */
    uint32_t read_timeouts;   /* read() 超时返回 0 的次数 */
    uint32_t ring_samples;    /* 环形缓冲容量（样本，2 的幂） */
    uint32_t high_water;      /* 历史最高占用（样本） */
    uint32_t first_frame_us;  /* start() → 首次成功读出耗时（µs） */
    bool active;              /* 当前是否在写入 */
} audio_fe_uplink_stats_t;

esp_err_t audio_fe_uplink_stats_read(audio_fe_uplink_stats_t *out);
esp_err_t audio_fe_uplink_stats_reset(void);
