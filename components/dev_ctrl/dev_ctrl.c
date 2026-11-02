/*
 * dev_ctrl —— 按键扫描 / 长按静音（L3 能力，T3.4）。
 *
 * 职责边界（ARCHITECTURE §4.1/§4.2/§7/§8.2）：
 *   - GPIO0（板载 BOOT 键）短按/长按扫描与去抖，投递 APP_EVT_BUTTON（payload T3.2 定稿）；
 *     短按由 voice_session 消费（会话中 = R4 打断、IDLE = 起一轮、FOLLOW_UP 窗口内 = 退出续听）；
 *   - 长按 = 静音切换（audio_player 音量 0 / 恢复原音量），本组件直接处理。
 *
 * **LED 状态指示已移除**（2026-11-02 用户决定，见 docs/PENDING.md PD-003）：
 *   板载 WS2812（GPIO48）实测数据解码不可靠 —— 任何帧（含全黑帧）都显示满白，
 *   满白电流（~60mA）叠加 WiFi 启动峰值会让 5V 掉到 BOD 阈值、设备反复复位。
 *   已排除寄存器写法/引脚/时序数学/固件版本/RGBW 变体；冷上电后不驱动即稳定。
 *   现在只在初始化时把该引脚**拉低并保持**，防止上电噪声让灯锁存出亮色再拖垮供电。
 *   历史实现见 git 4f5d152（`components/dev_ctrl/ws2812_led.c`）。
 *
 * 线程模型：**不新建任务**（内部 RAM 水位余量极小，CONVENTIONS §4）。
 *   按键扫描用 10ms 周期 esp_timer，回调只做 GPIO 读 + 整数状态机，
 *   不阻塞、不分配内存。
 */
#include "dev_ctrl.h"

#include <string.h>

#include "audio_player.h"
#include "board.h"
#include "driver/gpio.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "event_bus.h"

#define TAG "DEV"

/* 按键扫描周期（去抖与长按判定都基于它，10ms 是响应与抖动的折中） */
#define SCAN_PERIOD_MS 10

/* 连续一致采样数（CONFIG_DEV_BUTTON_DEBOUNCE_MS 内不含抖动才认账） */
#define STABLE_SAMPLES (CONFIG_DEV_BUTTON_DEBOUNCE_MS / SCAN_PERIOD_MS)

static bool s_inited;
static dev_stats_t s_stats;

/* 静音状态：只改 audio_player 音量，不依赖任何 LED */
static bool s_muted;
static uint8_t s_saved_volume = 100; /* 静音前的音量（恢复用） */

/* 去抖状态机（只在 esp_timer 任务上下文访问，无锁） */
static bool s_level;       /* 去抖后的认定电平（true=按下） */
static bool s_raw_last;    /* 上次采样电平 */
static int s_stable_cnt;   /* 与上次采样连续一致的次数 */
static uint32_t s_hold_ms; /* 按下已持续时长 */
static bool s_long_fired;  /* 本次按压是否已触发过长按 */

static dev_button_cb_t s_user_cb;
static void *s_user_cb_arg;

static void post_button(uint8_t action)
{
    app_evt_data_t data = { 0 };
    data.button.action = action;
    esp_err_t err = app_evt_post(APP_EVT_BUTTON, &data);
    if (err != ESP_OK) {
        s_stats.posts_dropped++;
        ESP_LOGW(TAG, "APP_EVT_BUTTON 投递失败（队列满）: %s", esp_err_to_name(err));
    }
}

static void fire_short(void)
{
    s_stats.short_presses++;
    ESP_LOGI(TAG, "按键：短按");
    post_button(APP_EVT_BUTTON_SHORT);
    if (s_user_cb) {
        s_user_cb(s_user_cb_arg, APP_EVT_BUTTON_SHORT);
    }
}

static void fire_long(void)
{
    s_stats.long_presses++;
    ESP_LOGI(TAG, "按键：长按（%ums）→ 切换静音", (unsigned)s_hold_ms);
    /* 长按语义归本组件（ARCH §4.2）：静音切换；事件照投，供 evt trace/脚本观测 */
    (void)dev_set_mute(!dev_is_muted());
    post_button(APP_EVT_BUTTON_LONG);
    if (s_user_cb) {
        s_user_cb(s_user_cb_arg, APP_EVT_BUTTON_LONG);
    }
}

static void scan_timer_cb(void *arg)
{
    (void)arg;
    const bool pressed = (gpio_get_level((gpio_num_t)BOARD_BUTTON_GPIO) == 0);

    if (pressed != s_raw_last) {
        /* 电平抖动：重新起算稳定计数 */
        s_raw_last = pressed;
        s_stable_cnt = 0;
    } else if (s_stable_cnt < STABLE_SAMPLES) {
        s_stable_cnt++;
    }

    if (s_stable_cnt < STABLE_SAMPLES || pressed == s_level) {
        /* 稳定按下期间：累计按住时长，到阈值触发一次长按（每次按压只触发一次） */
        if (pressed && s_level && !s_long_fired) {
            s_hold_ms += SCAN_PERIOD_MS;
            if (s_hold_ms >= (uint32_t)CONFIG_DEV_BUTTON_LONG_PRESS_MS) {
                s_long_fired = true;
                fire_long();
            }
        }
        return;
    }

    /* 电平已连续 STABLE_SAMPLES 次与认定态不同 → 认定一次边沿 */
    s_level = pressed;
    if (pressed) {
        s_hold_ms = 0;
        s_long_fired = false;
    } else if (!s_long_fired && s_hold_ms >= (uint32_t)CONFIG_DEV_BUTTON_DEBOUNCE_MS) {
        fire_short(); /* 释放时未达长按阈值 → 短按 */
    }
}

/* LED 已停用，但引脚必须**主动拉低**：悬空/噪声可能让 WS2812 锁存出亮色（含满白），
 * 满白电流会拖垮 5V 造成 BOD 复位（PENDING PD-003 实测）。输出低电平对 WS2812 就是
 * "无数据线活动"，灯保持熄灭，且不占 RAM、不发帧。 */
static void led_pin_force_off(void)
{
    const gpio_config_t led_cfg = {
        .pin_bit_mask = 1ULL << BOARD_LED_GPIO,
        .mode = GPIO_MODE_OUTPUT,
        .pull_up_en = GPIO_PULLUP_DISABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type = GPIO_INTR_DISABLE,
    };
    esp_err_t err = gpio_config(&led_cfg);
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "LED 引脚 GPIO%d 配置失败: %s（不影响按键/静音）",
                 BOARD_LED_GPIO, esp_err_to_name(err));
        return;
    }
    (void)gpio_set_level((gpio_num_t)BOARD_LED_GPIO, 0);
}

esp_err_t dev_init(void)
{
    if (s_inited) {
        return ESP_ERR_INVALID_STATE;
    }

    /* LED 已移除：只把引脚按住低电平，避免上电噪声锁存亮色 */
    led_pin_force_off();

    static esp_timer_handle_t s_scan_timer;
    const esp_timer_create_args_t scan_args = {
        .callback = scan_timer_cb,
        .name = "dev_scan",
    };
    esp_err_t err = esp_timer_create(&scan_args, &s_scan_timer);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "按键扫描定时器创建失败: %s", esp_err_to_name(err));
        return err;
    }
    err = esp_timer_start_periodic(s_scan_timer, SCAN_PERIOD_MS * 1000);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "按键扫描定时器启动失败: %s", esp_err_to_name(err));
        esp_timer_delete(s_scan_timer);
        return err;
    }

    /* 初始电平认定：以上电当前采样为准（正常上电按键为释放态；按住 BOOT 复位会进下载模式，
     * 见 HARDWARE §2 strapping 说明，固件侧无需特殊处理） */
    s_raw_last = (gpio_get_level((gpio_num_t)BOARD_BUTTON_GPIO) == 0);
    s_level = s_raw_last;
    s_stable_cnt = STABLE_SAMPLES;
    s_hold_ms = 0;
    s_long_fired = false;

    s_inited = true;
    ESP_LOGI(TAG, "ready: button=GPIO%d (debounce %dms, long %dms)；LED 功能已移除"
                  "（GPIO%d 保持低电平，见 PENDING PD-003）",
             BOARD_BUTTON_GPIO, CONFIG_DEV_BUTTON_DEBOUNCE_MS,
             CONFIG_DEV_BUTTON_LONG_PRESS_MS, BOARD_LED_GPIO);
    return ESP_OK;
}

esp_err_t dev_register_cb(dev_button_cb_t cb, void *arg)
{
    s_user_cb = cb;
    s_user_cb_arg = arg;
    return ESP_OK;
}

bool dev_is_muted(void)
{
    return s_muted;
}

esp_err_t dev_set_mute(bool mute)
{
    if (!s_inited) {
        return ESP_ERR_INVALID_STATE;
    }
    if (mute == s_muted) {
        return ESP_OK;
    }
    esp_err_t err;
    if (mute) {
        /* 保存当前音量再归零；NVS 配置音量不动（重启后按 NVS 恢复） */
        s_saved_volume = audio_player_get_volume();
        err = audio_player_set_volume(0);
        if (err != ESP_OK) {
            ESP_LOGE(TAG, "静音失败: %s", esp_err_to_name(err));
            return err;
        }
        ESP_LOGI(TAG, "静音开（原音量 %u）", (unsigned)s_saved_volume);
    } else {
        err = audio_player_set_volume(s_saved_volume);
        if (err != ESP_OK) {
            ESP_LOGE(TAG, "取消静音失败: %s", esp_err_to_name(err));
            return err;
        }
        ESP_LOGI(TAG, "静音关（音量恢复 %u）", (unsigned)s_saved_volume);
    }
    s_muted = mute;
    return ESP_OK;
}

esp_err_t dev_stats_read(dev_stats_t *out)
{
    if (out == NULL) {
        return ESP_ERR_INVALID_ARG;
    }
    *out = s_stats;
    return ESP_OK;
}

esp_err_t dev_stats_reset(void)
{
    memset(&s_stats, 0, sizeof(s_stats));
    return ESP_OK;
}
