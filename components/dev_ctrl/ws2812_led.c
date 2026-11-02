/*
 * WS2812 RGB LED 驱动（dev_ctrl 内部实现，周期精确 bit-bang）。
 *
 * 为什么不用 RMT：
 *   内部 RAM 预算余量极小（CONVENTIONS §4：wss 握手期堆水位硬约束 ≥30KB）。
 *   `esp_driver_rmt` 会往 IRAM 里塞 ~2.2KB（rmt_tx 的 ISR/事务函数必须 IRAM_ATTR），
 *   再加 RMT 通道/symbol 缓冲的运行时堆分配，合计吃掉 ~3.4KB 内部池 → 直接击穿水位。
 *   本驱动用「关中断 + 读 CCOUNT 精确延时」bit-bang 24bit 一帧：
 *     - 静态代价：本文件几百字节代码（IRAM）+ 几个静态变量，无第三方依赖；
 *     - 时间代价：一帧 24bit × 1.25us ≈ 30us 关中断，只在状态变化/闪烁边沿调用
 *       （最快 4Hz 闪烁，每次状态切换才发帧），对 WiFi/I2S（DMA 缓冲 ms 级）无影响；
 *     - 精度：240MHz 下 1 cycle = 4.17ns，WS2812 容差 ±150ns，CCOUNT 自旋足够。
 *   全部时序代码 IRAM_ATTR（关中断期间不能 cache miss）。
 *
 * T3.4 真机问题（2026-11-02）：板载 RGB 灯只显示恒定亮白、切换颜色无效 →
 * 每个 bit 都被 LED 解成 1。为此把时序参数改成**运行期可调**（`ws2812_set_timing`），
 * 并提供 `ws2812_measure_*` 供 `dev led selfcheck` 打印实测值，
 * 便于在不重烧的前提下用 `dev led timing <preset>` 逐个试出这块板子真正认的时序。
 */
#include "ws2812_led.h"

#include "driver/gpio.h"
#include "esp_cpu.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "soc/gpio_struct.h"

static const char *TAG = "DEV";

/*
 * 时序预设（@240MHz，1 cycle = 1/240 us；数值为**目标**值，`wait_cycles` 的
 * 读 CCOUNT + 分支开销（实测见 `dev led selfcheck`）会叠加到每一段上）。
 * WS2812B datasheet：T0H 0.4±0.15us、T0L 0.85±0.15us、T1H 0.85±0.15us、
 * T1L 0.4±0.15us，复位 ≥50us。
 */
#define CPU_MHZ          240
#define NS_TO_CYCLES(ns) (((ns) * CPU_MHZ) / 1000)
#define WS_RESET_CYCLES  NS_TO_CYCLES(60000) /* 60us ≥ datasheet 50us */

typedef struct {
    uint16_t t0h_ns, t0l_ns, t1h_ns, t1l_ns;
    const char *name;
} ws2812_preset_t;

static const ws2812_preset_t s_presets[] = {
    /* 0：T3.4 原值（高电平刻意偏低端，本意是让开销叠加后落在标称值） */
    { 350, 900, 750, 500, "balanced-350/900/750/500" },
    /* 1：经典 WS2812B 值（多数库使用） */
    { 400, 850, 800, 450, "datasheet-400/850/800/450" },
    /* 2：短高电平（若解码阈值偏低，用这个更保险） */
    { 300, 1000, 700, 500, "short-high-300/1000/700/500" },
    /* 3：更短高电平 + 更长低电平（劣质克隆/电平勉强时） */
    { 250, 1100, 650, 550, "very-short-high-250/1100/650/550" },
    /* 4：很慢的位周期（1.5us），对时序容差最宽容 */
    { 400, 1100, 900, 600, "slow-1.5us-400/1100/900/600" },
};
#define WS_PRESET_COUNT (sizeof(s_presets) / sizeof(s_presets[0]))

#define WS_BITS          24
#define WS_GPIO_HI_BITS  32 /* GPIO<32 走 out_w1ts/w1tc，否则 out1_w1ts/w1tc */

static int s_gpio = -1;
static bool s_use_out1;     /* GPIO>=32 → 用 out1 寄存器组 */
static uint32_t s_pin_mask; /* 在对应寄存器组内的位掩码 */
static uint32_t s_last_end; /* 上一帧结束时的 CCOUNT（保证帧间低电平 ≥50us） */

/* 当前生效的时序（运行期可改，见 ws2812_set_timing） */
static uint32_t s_t0h, s_t0l, s_t1h, s_t1l;
static int s_preset = 1; /* 默认用 datasheet 值 */

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
    /* 每次进帧只读一次时序变量（IRAM 内读 DRAM 静态变量，代价固定且已计入开销） */
    const uint32_t t0h = s_t0h, t0l = s_t0l, t1h = s_t1h, t1l = s_t1l;

    portDISABLE_INTERRUPTS();
    /* 与上一帧之间保持足够的复位低电平 */
    while ((uint32_t)(esp_cpu_get_cycle_count() - s_last_end) < WS_RESET_CYCLES) {
    }
    for (int i = WS_BITS - 1; i >= 0; i--) {
        const bool one = ((grb >> i) & 1u) != 0;
        pin_hi();
        wait_cycles(one ? t1h : t0h);
        pin_lo();
        wait_cycles(one ? t1l : t0l);
    }
    s_last_end = esp_cpu_get_cycle_count();
    portENABLE_INTERRUPTS();
}

/* 把某一档预设的 ns 值灌进运行期变量 */
static void apply_preset(int index)
{
    const ws2812_preset_t *p = &s_presets[index];
    s_t0h = NS_TO_CYCLES(p->t0h_ns);
    s_t0l = NS_TO_CYCLES(p->t0l_ns);
    s_t1h = NS_TO_CYCLES(p->t1h_ns);
    s_t1l = NS_TO_CYCLES(p->t1l_ns);
    s_preset = index;
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
    apply_preset(s_preset);

    pin_lo();
    err = ws2812_set_rgb(0, 0, 0);
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "上电熄灯帧失败: %s", esp_err_to_name(err));
    }
    ESP_LOGI(TAG, "ws2812 ready (bitbang gpio=%d, preset=%d %s)",
             gpio, s_preset, s_presets[s_preset].name);
    return ESP_OK;
}

/* 发送任意字节序列（每字节 MSB 先发）：用于试 3 字节(RGB) / 4 字节(RGBW) 等变体 */
static IRAM_ATTR void ws_send_bytes_raw(const uint8_t *data, size_t n)
{
    const uint32_t t0h = s_t0h, t0l = s_t0l, t1h = s_t1h, t1l = s_t1l;

    portDISABLE_INTERRUPTS();
    while ((uint32_t)(esp_cpu_get_cycle_count() - s_last_end) < WS_RESET_CYCLES) {
    }
    for (size_t k = 0; k < n; k++) {
        const uint8_t byte = data[k];
        for (int i = 7; i >= 0; i--) {
            const bool one = ((byte >> i) & 1u) != 0;
            pin_hi();
            wait_cycles(one ? t1h : t0h);
            pin_lo();
            wait_cycles(one ? t1l : t0l);
        }
    }
    s_last_end = esp_cpu_get_cycle_count();
    portENABLE_INTERRUPTS();
}

esp_err_t ws2812_send_bytes(const uint8_t *data, size_t n)
{
    if (s_gpio < 0) {
        return ESP_ERR_INVALID_STATE;
    }
    if (data == NULL || n == 0 || n > 8) {
        return ESP_ERR_INVALID_ARG;
    }
    ws_send_bytes_raw(data, n);
    return ESP_OK;
}

esp_err_t ws2812_measure_segments(uint16_t *t0h, uint16_t *t0l, uint16_t *t1h, uint16_t *t1l)
{
    if (s_gpio < 0 || t0h == NULL || t0l == NULL || t1h == NULL || t1l == NULL) {
        return ESP_ERR_INVALID_STATE;
    }
    /* 直接量「写高 → 等待 → 写低」的实际 cycle 数：这就是 LED 看到的高电平宽度 */
    uint32_t d[4];
    const uint32_t seg[4] = { s_t0h, s_t0l, s_t1h, s_t1l };
    for (int k = 0; k < 4; k++) {
        portDISABLE_INTERRUPTS();
        const uint32_t t_start = esp_cpu_get_cycle_count();
        if ((k & 1) == 0) {
            pin_hi();
        } else {
            pin_lo();
        }
        wait_cycles(seg[k]);
        const uint32_t t_end = esp_cpu_get_cycle_count();
        if ((k & 1) == 0) {
            pin_lo();
        } else {
            pin_hi();
        }
        portENABLE_INTERRUPTS();
        d[k] = t_end - t_start;
    }
    /* cycle → ns（四舍五入） */
    *t0h = (uint16_t)((d[0] * 1000u + CPU_MHZ / 2) / CPU_MHZ);
    *t0l = (uint16_t)((d[1] * 1000u + CPU_MHZ / 2) / CPU_MHZ);
    *t1h = (uint16_t)((d[2] * 1000u + CPU_MHZ / 2) / CPU_MHZ);
    *t1l = (uint16_t)((d[3] * 1000u + CPU_MHZ / 2) / CPU_MHZ);
    s_last_end = esp_cpu_get_cycle_count();
    return ESP_OK;
}

esp_err_t ws2812_set_rgb(uint8_t r, uint8_t g, uint8_t b)
{
    if (s_gpio < 0) {
        return ESP_ERR_INVALID_STATE;
    }
    /* WS2812 物理顺序是 G-R-B */
    ws_send_frame(((uint32_t)g << 16) | ((uint32_t)r << 8) | b);
    return ESP_OK;
}

esp_err_t ws2812_set_timing(int preset, int *applied)
{
    if (s_gpio < 0) {
        return ESP_ERR_INVALID_STATE;
    }
    if (preset < 0 || (size_t)preset >= WS_PRESET_COUNT) {
        return ESP_ERR_INVALID_ARG;
    }
    apply_preset(preset);
    if (applied != NULL) {
        *applied = s_preset;
    }
    return ESP_OK;
}

esp_err_t ws2812_set_timing_raw(uint16_t t0h_ns, uint16_t t0l_ns, uint16_t t1h_ns, uint16_t t1l_ns)
{
    if (s_gpio < 0) {
        return ESP_ERR_INVALID_STATE;
    }
    if (t0h_ns == 0 || t0l_ns == 0 || t1h_ns == 0 || t1l_ns == 0) {
        return ESP_ERR_INVALID_ARG;
    }
    s_t0h = NS_TO_CYCLES(t0h_ns);
    s_t0l = NS_TO_CYCLES(t0l_ns);
    s_t1h = NS_TO_CYCLES(t1h_ns);
    s_t1l = NS_TO_CYCLES(t1l_ns);
    s_preset = -1; /* 手工时序，不对应预设 */
    return ESP_OK;
}

int ws2812_get_preset(void)
{
    return s_preset;
}

const char *ws2812_preset_name(int preset)
{
    if (preset < 0 || (size_t)preset >= WS_PRESET_COUNT) {
        return "?";
    }
    return s_presets[preset].name;
}

unsigned ws2812_preset_count(void)
{
    return (unsigned)WS_PRESET_COUNT;
}

unsigned ws2812_measure_overhead(void)
{
    /* 连续调用 wait_cycles(240)（=1000ns）多次，测平均超出量（循环开销 + 函数进出） */
    const int n = 200;
    const uint32_t target = NS_TO_CYCLES(1000);
    const uint32_t start = esp_cpu_get_cycle_count();
    portDISABLE_INTERRUPTS();
    for (int i = 0; i < n; i++) {
        wait_cycles(target);
    }
    portENABLE_INTERRUPTS();
    const uint32_t elapsed = (uint32_t)(esp_cpu_get_cycle_count() - start);
    const uint32_t avg = elapsed / (uint32_t)n;
    return (avg > target) ? (avg - target) : 0;
}

unsigned ws2812_measure_frame_cycles(uint32_t grb)
{
    /* 整帧耗时（含帧前复位等待），用于确认实际位周期有没有被开销拉长 */
    /* 先等复位窗口过去，避免把上一次的等待算进来 */
    portDISABLE_INTERRUPTS();
    while ((uint32_t)(esp_cpu_get_cycle_count() - s_last_end) < WS_RESET_CYCLES) {
    }
    portENABLE_INTERRUPTS();
    const uint32_t start = esp_cpu_get_cycle_count();
    ws_send_frame(grb);
    return (uint32_t)(esp_cpu_get_cycle_count() - start);
}
