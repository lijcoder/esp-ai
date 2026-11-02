/*
 * WS2812 RGB LED 驱动（dev_ctrl 内部实现，周期精确 bit-bang）。
 *
 * 为什么不用 RMT：
 *   内部 RAM 预算余量极小（CONVENTIONS §4：wss 握手期堆水位硬约束 ≥30KB）。
 *   `esp_driver_rmt` 会往 IRAM 里塞 ~2.2KB（rmt_tx 的 ISR/事务函数必须 IRAM_ATTR），
 *   再加 RMT 通道/symbol 缓冲的运行时堆分配，合计吃掉 ~3.4KB 内部池 → 直接击穿水位。
 *   本驱动用「关中断 + 读 CCOUNT 精确延时」bit-bang 24bit 一帧：
 *     - 静态代价：本文件几百字节代码（IRAM）+ 3 个静态变量，无第三方依赖；
 *     - 时间代价：一帧 24bit × 1.25us ≈ 30us 关中断，只在状态变化/闪烁边沿调用
 *       （最快 4Hz 闪烁，每次状态切换才发帧），对 WiFi/I2S（DMA 缓冲 ms 级）无影响；
 *     - 精度：240MHz 下 1 cycle = 4.17ns，WS2812 容差 ±150ns，CCOUNT 自旋足够。
 *   全部时序代码 IRAM_ATTR（关中断期间不能 cache miss）。
 */
#include "ws2812_led.h"

#include "driver/gpio.h"
#include "esp_cpu.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "soc/gpio_struct.h"

static const char *TAG = "DEV";

/*
 * 时序目标 @240MHz（1 cycle = 1/240 us）。
 * WS2812B datasheet：T0H 0.35±0.15us、T0L 0.8±0.15us、T1H 0.7±0.15us、T1L 0.6±0.15us，
 * 复位 ≥50us。这里的取值刻意落在窗口偏低端：`wait_cycles` 的读 CCOUNT + 分支开销
 * （约 25~35ns）会叠加到高电平段上，叠加后正好落在标称值附近。
 */
#define CPU_MHZ          240
#define NS_TO_CYCLES(ns) (((ns) * CPU_MHZ) / 1000)
#define WS_T0H_CYCLES    NS_TO_CYCLES(350)
#define WS_T0L_CYCLES    NS_TO_CYCLES(900)
#define WS_T1H_CYCLES    NS_TO_CYCLES(750)
#define WS_T1L_CYCLES    NS_TO_CYCLES(500)
#define WS_RESET_CYCLES  NS_TO_CYCLES(60000) /* 60us ≥ datasheet 50us */

#define WS_BITS          24
#define WS_GPIO_HI_BITS  32 /* GPIO<32 走 out_w1ts/w1tc，否则 out1_w1ts/w1tc */

static int s_gpio = -1;
static bool s_use_out1;     /* GPIO>=32 → 用 out1 寄存器组 */
static uint32_t s_pin_mask; /* 在对应寄存器组内的位掩码 */
static uint32_t s_last_end; /* 上一帧结束时的 CCOUNT（保证帧间低电平 ≥50us） */

/* 精确等待：自旋读 CCOUNT（不受编译器优化/缓存影响） */
static inline IRAM_ATTR void wait_cycles(uint32_t cycles)
{
    const uint32_t start = esp_cpu_get_cycle_count();
    while ((uint32_t)(esp_cpu_get_cycle_count() - start) < cycles) {
    }
}

static inline IRAM_ATTR void pin_hi(void)
{
    if (s_use_out1) {
        GPIO.out1_w1ts.val = s_pin_mask;
    } else {
        GPIO.out_w1ts = s_pin_mask;
    }
}

static inline IRAM_ATTR void pin_lo(void)
{
    if (s_use_out1) {
        GPIO.out1_w1tc.val = s_pin_mask;
    } else {
        GPIO.out_w1tc = s_pin_mask;
    }
}

/* 一帧 24bit（物理顺序 G-R-B，MSB first）：整帧关中断保证时序 */
static IRAM_ATTR void ws_send_frame(uint32_t grb)
{
    portDISABLE_INTERRUPTS();
    /* 与上一帧之间保持足够的复位低电平 */
    while ((uint32_t)(esp_cpu_get_cycle_count() - s_last_end) < WS_RESET_CYCLES) {
    }
    for (int i = WS_BITS - 1; i >= 0; i--) {
        const uint32_t one = (grb >> i) & 1u;
        pin_hi();
        wait_cycles(one ? WS_T1H_CYCLES : WS_T0H_CYCLES);
        pin_lo();
        wait_cycles(one ? WS_T1L_CYCLES : WS_T0L_CYCLES);
    }
    s_last_end = esp_cpu_get_cycle_count();
    portENABLE_INTERRUPTS();
}

esp_err_t ws2812_init(int gpio)
{
    if (s_gpio >= 0) {
        return ESP_ERR_INVALID_STATE;
    }
    if (gpio < 0 || gpio > 48) {
        return ESP_ERR_INVALID_ARG;
    }

    /* board 只配置了按键等引脚（LED 定义保留未用），输出方向由本驱动自己配 */
    const gpio_config_t led_cfg = {
        .pin_bit_mask = 1ULL << gpio,
        .mode = GPIO_MODE_OUTPUT,
        .pull_up_en = GPIO_PULLUP_DISABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type = GPIO_INTR_DISABLE,
    };
    esp_err_t err = gpio_config(&led_cfg);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "LED GPIO%d 配置失败: %s", gpio, esp_err_to_name(err));
        return err;
    }

    s_gpio = gpio;
    s_use_out1 = (gpio >= WS_GPIO_HI_BITS);
    s_pin_mask = 1u << (s_use_out1 ? (gpio - WS_GPIO_HI_BITS) : gpio);
    s_last_end = esp_cpu_get_cycle_count();

    pin_lo();
    err = ws2812_set_rgb(0, 0, 0);
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "上电熄灯帧失败: %s", esp_err_to_name(err));
    }
    ESP_LOGI(TAG, "ws2812 ready (bitbang gpio=%d, frame=%d cycles)", gpio,
             WS_BITS * (WS_T0H_CYCLES + WS_T0L_CYCLES));
    return ESP_OK;
}

esp_err_t ws2812_set_rgb(uint8_t r, uint8_t g, uint8_t b)
{
    if (s_gpio < 0) {
        return ESP_ERR_INVALID_STATE;
    }
    ws_send_frame(((uint32_t)g << 16) | ((uint32_t)r << 8) | b);
    return ESP_OK;
}
