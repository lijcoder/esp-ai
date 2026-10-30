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
