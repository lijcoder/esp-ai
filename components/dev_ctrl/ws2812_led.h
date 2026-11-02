/*
 * WS2812 RGB LED 驱动（dev_ctrl 内部实现，周期精确 bit-bang，不依赖 RMT 驱动）。
 *
 * 为什么自写而不引第三方：只点亮一颗灯、只改颜色，且 esp_driver_rmt 的 IRAM 占用
 * （~2.2KB）会击穿内部 RAM 水位硬约束（CONVENTIONS §4）。详见 ws2812_led.c 头注释。
 *
 * 时序参数运行期可调（`ws2812_set_timing`），用于在真机上试出板载灯实际认可的时序；
 * `ws2812_measure_*` 供 `dev led selfcheck` 打印实测开销。
 */
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

/** 配置 GPIO 为输出、初始化时序状态并熄灯（重复调用返回 ESP_ERR_INVALID_STATE） */
esp_err_t ws2812_init(int gpio);

/** 设置 RGB 颜色（发送一帧 24bit，约 30us 关中断；未初始化返回 ESP_ERR_INVALID_STATE） */
esp_err_t ws2812_set_rgb(uint8_t r, uint8_t g, uint8_t b);

/** 发送任意字节序列（MSB 先发；n ≤ 8），用于试 3 字节 RGB / 4 字节 RGBW 等变体 */
esp_err_t ws2812_send_bytes(const uint8_t *data, size_t n);

/** 实测四段脉宽（ns）：T0H/T0L/T1H/T1L 的「写高→等待→写低」实际耗时 */
esp_err_t ws2812_measure_segments(uint16_t *t0h, uint16_t *t0l, uint16_t *t1h, uint16_t *t1l);

/** 切换时序预设（0..ws2812_preset_count()-1）；applied 回填实际生效的预设号 */
esp_err_t ws2812_set_timing(int preset, int *applied);

/** 直接设定四段时序（ns），用于真机扫描（不改变预设表） */
esp_err_t ws2812_set_timing_raw(uint16_t t0h_ns, uint16_t t0l_ns, uint16_t t1h_ns, uint16_t t1l_ns);

/** 当前时序预设号（-1 = 手工时序） */
int ws2812_get_preset(void);

/** 预设名（打印用；越界返回 "?"） */
const char *ws2812_preset_name(int preset);

/** 预设总数 */
unsigned ws2812_preset_count(void);

/** 实测 `wait_cycles` 平均超出目标值的 cycle 数（时序校准用，越小越准） */
unsigned ws2812_measure_overhead(void);

/** 实测发送一帧（24bit）消耗的 cycle 数（含帧前复位等待之后的纯发送时间） */
unsigned ws2812_measure_frame_cycles(uint32_t grb);

#ifdef __cplusplus
}
#endif
