/*
 * WS2812 RGB LED 驱动（dev_ctrl 内部实现，周期精确 bit-bang，不依赖 RMT 驱动）。
 *
 * 为什么自写而不引第三方：只点亮一颗灯、只改颜色，且 esp_driver_rmt 的 IRAM 占用
 * （~2.2KB）会击穿内部 RAM 水位硬约束（CONVENTIONS §4）。详见 ws2812_led.c 头注释。
 */
#pragma once

#include <stdint.h>

#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

/** 配置 GPIO 为输出、初始化时序状态并熄灯（重复调用返回 ESP_ERR_INVALID_STATE） */
esp_err_t ws2812_init(int gpio);

/** 设置 RGB 颜色（发送一帧 24bit，约 30us 关中断；未初始化返回 ESP_ERR_INVALID_STATE） */
esp_err_t ws2812_set_rgb(uint8_t r, uint8_t g, uint8_t b);

#ifdef __cplusplus
}
#endif
