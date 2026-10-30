#pragma once

#include <stdbool.h>

#include "esp_err.h"

/*
 * 板级引脚定义（v1 样机：ESP32-S3-N16R8 + INMP441 + MAX98357A）
 * 详细接线与电气约束见 docs/HARDWARE.md。
 */
#define BOARD_MIC_BCLK_GPIO  5   /* INMP441 SCK */
#define BOARD_MIC_WS_GPIO    6   /* INMP441 WS  */
#define BOARD_MIC_DIN_GPIO   4   /* INMP441 SD  */
#define BOARD_AMP_BCLK_GPIO  41  /* MAX98357A BCLK */
#define BOARD_AMP_WS_GPIO    42  /* MAX98357A LRC  */
#define BOARD_AMP_DOUT_GPIO  40  /* MAX98357A DIN  */
#define BOARD_LED_GPIO       48  /* WS2812（本期仅保留定义） */
#define BOARD_BUTTON_GPIO    0   /* 板载 BOOT 键 */

esp_err_t board_init(void);

/* 返回 true 表示按键按下（低电平有效） */
bool board_button_pressed(void);
