/*
 * dev_ctrl —— 按键扫描 / 长按静音（L3 能力，T3.4）的唯一对外头文件。
 *
 * 职责边界（ARCHITECTURE §4.1/§4.2/§7/§8.2）：
 *   - GPIO0 按键扫描（10ms 采样 + 30ms 去抖 + 600ms 长按），投递 APP_EVT_BUTTON；
 *     短按由 voice_session 消费（会话中 = R4 打断、IDLE = 起轮、FOLLOW_UP 窗口内 = 退出续听）；
 *   - 长按 = 静音切换（音量 0 / 恢复原值），本组件直接执行。
 *
 * **LED 状态指示已移除**（2026-11-02 用户决定）：板载 WS2812（GPIO48）数据解码不可靠，
 * 任何帧都显示满白，满白电流叠加 WiFi 启动峰值会触发 BOD 复位；详见 docs/PENDING.md PD-003。
 * 本组件现在只把该引脚拉低保持（防止上电噪声锁存亮色），不再驱动任何帧。
 */
#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

/* 按键/静音观测统计（`dev status` 用，累计值） */
typedef struct {
    uint32_t short_presses; /* 短按次数（已投递） */
    uint32_t long_presses;  /* 长按次数（已处理静音切换） */
    uint32_t posts_dropped; /* 事件队列满导致的投递失败次数 */
} dev_stats_t;

/* 按键动作回调（附加观测用；与 APP_EVT_BUTTON 并存，回调在 esp_timer 任务上下文） */
typedef void (*dev_button_cb_t)(void *arg, uint8_t action);

/**
 * @brief 初始化按键扫描（须在 board_init / audio_player_init 之后）。
 *        LED 引脚在此被拉低并保持（LED 功能已移除）。
 */
esp_err_t dev_init(void);

/** 注册按键动作回调（可选；重复注册覆盖前一次） */
esp_err_t dev_register_cb(dev_button_cb_t cb, void *arg);

/** 当前是否处于静音（长按切换；运行期状态，不持久化） */
bool dev_is_muted(void);

/** 静音切换（长按与 console `dev mute` 共用入口）：mute=true 音量→0，false 恢复原音量 */
esp_err_t dev_set_mute(bool mute);

/** 读取统计（out 为空返回 ESP_ERR_INVALID_ARG） */
esp_err_t dev_stats_read(dev_stats_t *out);

/** 清零统计 */
esp_err_t dev_stats_reset(void);

#ifdef __cplusplus
}
#endif
