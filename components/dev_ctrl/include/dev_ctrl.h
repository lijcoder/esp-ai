/*
 * dev_ctrl —— 按键扫描 / LED 状态指示 / 静音切换（L3 能力，T3.4）。
 *
 * 职责边界（ARCHITECTURE §4.1/§4.2/§7）：
 *   - GPIO0（板载 BOOT 键）短按/长按扫描与去抖，投递 APP_EVT_BUTTON（payload T3.2 定稿）；
 *     短按事件由 voice_session 消费（会话中 = R4 打断、IDLE = 起一轮）；
 *   - 长按 = 静音切换（audio_player 音量 0 / 恢复原音量）+ LED 指示，本组件直接处理；
 *   - LED（WS2812，GPIO48）按会话状态显示：订阅 APP_EVT_VOICE_STATE_CHANGED 映射颜色模式；
 *
 * 线程模型：**不新建任务**（内部 RAM 水位余量极小，CONVENTIONS §4）。
 *   - 按键扫描：10ms 周期 esp_timer（esp_timer 任务上下文，只做 GPIO 读 + 整数状态机）；
 *   - LED 闪烁：25ms 周期 esp_timer，静态色时回调直接返回；
 *   - 事件订阅回调在 `evt` 任务上下文，只做内存赋值 + LED 帧发送（RMT 硬件自治，非阻塞）。
 */
#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

/* LED 显示模式（AUTO = 跟随会话状态；其余为 console 探针强制指定） */
typedef enum {
    DEV_LED_AUTO = 0,     /* 跟随 APP_EVT_VOICE_STATE_CHANGED */
    DEV_LED_IDLE,         /* 绿色常亮（暗） */
    DEV_LED_LISTENING,    /* 青色常亮 */
    DEV_LED_THINKING,     /* 黄色 2Hz 闪烁 */
    DEV_LED_SPEAKING,     /* 蓝色常亮 */
    DEV_LED_FOLLOW_UP,    /* 绿色 2Hz 闪烁（续听窗口） */
    DEV_LED_ERROR,        /* 红色 4Hz 闪烁 */
    DEV_LED_MUTE,         /* 白色常亮（静音覆盖，优先级最高） */
    DEV_LED_MODE_COUNT,
} dev_led_mode_t;

/* 按键/LED 观测统计（`dev status` 用，累计值） */
typedef struct {
    uint32_t short_presses;  /* 短按次数（已投递） */
    uint32_t long_presses;   /* 长按次数（已处理静音切换） */
    uint32_t posts_dropped;  /* 事件队列满导致的投递失败次数 */
    uint32_t led_frames;     /* LED 已发送帧数（WS2812 事务数） */
} dev_stats_t;

/* 按键动作回调（附加观测用；与 APP_EVT_BUTTON 并存，回调在 esp_timer 任务上下文） */
typedef void (*dev_button_cb_t)(void *arg, uint8_t action);

/**
 * @brief 初始化按键扫描与 LED（须在 board_init / audio_player_init / vs_init 之后：
 *        依赖 GPIO 已配置、音量已恢复、订阅状态事件时不漏初始迁移）。
 */
esp_err_t dev_init(void);

/** 注册按键动作回调（可选；重复注册覆盖前一次） */
esp_err_t dev_register_cb(dev_button_cb_t cb, void *arg);

/** 当前是否处于静音（长按切换；运行期状态，不持久化） */
bool dev_is_muted(void);

/** 静音切换（长按与 console `dev mute` 共用入口）：mute=true 音量→0，false 恢复原音量 */
esp_err_t dev_set_mute(bool mute);

/** 设置 LED 模式（DEV_LED_AUTO 恢复跟随会话状态；console `dev led` 探针用） */
esp_err_t dev_led_set_mode(dev_led_mode_t mode);

/** 当前 LED 模式 */
dev_led_mode_t dev_led_get_mode(void);

/** 读取统计（out 为空返回 ESP_ERR_INVALID_ARG） */
esp_err_t dev_stats_read(dev_stats_t *out);

/** 清零统计 */
esp_err_t dev_stats_reset(void);

#ifdef __cplusplus
}
#endif
