/*
 * dev_ctrl —— 按键扫描 / LED 状态指示 / 静音切换（T3.4，ARCHITECTURE §4.1/§7/§8.2）。
 *
 * 设计要点：
 *   - **不新建任务**（CONVENTIONS §4 内部 RAM 约束）：按键扫描用 10ms 周期 esp_timer 采样
 *     GPIO0 电平（连续 N 次一致才认定电平变化 = 去抖），长按阈值在按住期间累计判定；
 *     LED 闪烁用 25ms 周期 esp_timer（静态色时回调一次比较后即返回，不做任何 I/O）。
 *   - 短按/长按都投递 APP_EVT_BUTTON（payload 为 T3.2 定稿的 app_evt_button_t），
 *     短按由 voice_session 消费（会话中 = R4 打断、IDLE = 起一轮）；
 *     长按在本组件内直接做静音切换（音量 0 / 恢复原音量）+ LED 指示，voice_session 忽略长按。
 *   - LED 跟随会话状态：订阅 APP_EVT_VOICE_STATE_CHANGED，把 `to` 状态映射成颜色模式；
 *     静音期间一律覆盖为白色常亮（指示优先级最高）。
 */
#include "dev_ctrl.h"

#include <string.h>

#include "board.h"
#include "driver/gpio.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "event_bus.h"

#if CONFIG_DEV_LED_ENABLE
#include "audio_player.h"
#include "ws2812_led.h"
#endif

#define TAG "DEV"

/* ---------------- 按键扫描（10ms 周期 esp_timer，去抖 + 短/长按判定） ---------------- */

#define SCAN_PERIOD_MS 10
#define LED_TICK_MS    25

/* 连续一致采样数（CONFIG_DEV_BUTTON_DEBOUNCE_MS 内不含抖动才认账） */
#define STABLE_SAMPLES (CONFIG_DEV_BUTTON_DEBOUNCE_MS / SCAN_PERIOD_MS)

#if CONFIG_DEV_ENABLE

static bool s_inited;
static dev_stats_t s_stats;

/* 去抖状态机（只在 esp_timer 任务上下文访问，无锁） */
static bool s_level;      /* 去抖后的认定电平（true=按下） */
static bool s_raw_last;   /* 上次采样电平 */
static int s_stable_cnt;  /* 与上次采样连续一致的次数 */
static uint32_t s_hold_ms;/* 按下已持续时长 */
static bool s_long_fired; /* 本次按压是否已触发过长按 */

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

/* ---------------- LED 状态指示 ---------------- */

#if CONFIG_DEV_LED_ENABLE

typedef struct {
    uint8_t r, g, b;  /* 0/1 相对分量，渲染时乘亮度 */
    bool blink;       /* 是否闪烁 */
    uint16_t half_ms; /* 半周期（亮/灭各一半），必须是 LED_TICK_MS 的整数倍 */
} led_pattern_t;

static const led_pattern_t s_patterns[DEV_LED_MODE_COUNT] = {
    [DEV_LED_IDLE]      = { .g = 1 },                                /* 绿 常亮 */
    [DEV_LED_LISTENING] = { .g = 1, .b = 1 },                        /* 青 常亮 */
    [DEV_LED_THINKING]  = { .r = 1, .g = 1, .blink = true, .half_ms = 250 },
    [DEV_LED_SPEAKING]  = { .b = 1 },                                /* 蓝 常亮 */
    [DEV_LED_FOLLOW_UP] = { .g = 1, .blink = true, .half_ms = 250 }, /* 绿 闪（续听窗口） */
    [DEV_LED_ERROR]     = { .r = 1, .blink = true, .half_ms = 125 }, /* 红 快闪 */
    [DEV_LED_MUTE]      = { .r = 1, .g = 1, .b = 1 },                /* 白 常亮（静音覆盖） */
};

/* AUTO 模式跟随会话状态；强制模式（console 探针）直接用 s_led_mode */
static dev_led_mode_t s_led_mode = DEV_LED_AUTO;
static uint8_t s_vs_state; /* 最近一次会话状态（vs_state_t 数值） */
static bool s_muted;
static uint8_t s_saved_volume = 100; /* 静音前的音量（恢复用） */
static uint32_t s_blink_ticks;       /* 闪烁相位计数 */

/* vs_state_t 数值 → LED 模式（event_bus payload 契约 ARCH §4.2；T3.7 追加 FOLLOW_UP=5） */
static dev_led_mode_t state_to_mode(uint8_t state)
{
    switch (state) {
    case 0:  return DEV_LED_IDLE;      /* VS_STATE_IDLE */
    case 1:  return DEV_LED_LISTENING; /* VS_STATE_LISTENING */
    case 2:  return DEV_LED_THINKING;  /* VS_STATE_THINKING */
    case 3:  return DEV_LED_SPEAKING;  /* VS_STATE_SPEAKING */
    case 4:  return DEV_LED_ERROR;     /* VS_STATE_ERROR */
    case 5:  return DEV_LED_FOLLOW_UP; /* VS_STATE_FOLLOW_UP */
    default: return DEV_LED_IDLE;      /* 未知状态按 IDLE 兜底 */
    }
}

static void led_render(bool on)
{
    /* AUTO = 跟随最近会话状态；否则用强制模式。静音覆盖优先级最高（白色常亮） */
    const dev_led_mode_t mode =
        (s_led_mode == DEV_LED_AUTO) ? state_to_mode(s_vs_state) : s_led_mode;
    const led_pattern_t *p = &s_patterns[s_muted ? DEV_LED_MUTE : mode];
    const uint8_t br = (uint8_t)CONFIG_DEV_LED_BRIGHTNESS;
    /* WS2812 很亮，亮度取 Kconfig（默认 24/255）避免刺眼；通道忙丢帧不影响指示用途 */
    esp_err_t err = ws2812_set_rgb(on ? (uint8_t)(p->r * br) : 0,
                                   on ? (uint8_t)(p->g * br) : 0,
                                   on ? (uint8_t)(p->b * br) : 0);
    if (err == ESP_OK) {
        s_stats.led_frames++;
    }
}

/* 模式/静音/状态变化后重绘：闪烁模式从"亮"相位开始，状态切换可立即感知 */
static void led_apply(void)
{
    s_blink_ticks = 0;
    led_render(true);
}

static void led_timer_cb(void *arg)
{
    (void)arg;
    const dev_led_mode_t mode =
        (s_led_mode == DEV_LED_AUTO) ? state_to_mode(s_vs_state) : s_led_mode;
    const led_pattern_t *p = &s_patterns[s_muted ? DEV_LED_MUTE : mode];
    if (!p->blink) {
        return; /* 静态色：一次比较后即返回 */
    }
    s_blink_ticks++;
    const uint32_t half_ticks = p->half_ms / LED_TICK_MS;
    led_render((s_blink_ticks / half_ticks) % 2 == 0);
}

static void on_voice_state(void *arg, app_evt_t evt, const app_evt_data_t *data)
{
    (void)arg;
    (void)evt;
    /* 事件回调在 evt 任务上下文：只做赋值 + 一次 LED 帧发送（非阻塞） */
    if (data != NULL) {
        s_vs_state = data->voice_state.to;
        ESP_LOGD(TAG, "vs state -> %u", (unsigned)s_vs_state);
    }
    if (s_led_mode == DEV_LED_AUTO) {
        led_apply(); /* AUTO：按最新状态重绘 */
    }
}

#endif /* CONFIG_DEV_LED_ENABLE */

/* ---------------- 对外 API ---------------- */

esp_err_t dev_init(void)
{
    if (s_inited) {
        return ESP_ERR_INVALID_STATE;
    }
    esp_err_t err = ESP_OK;

#if CONFIG_DEV_LED_ENABLE
    /* 订阅状态事件（dev_ctrl=L3 订阅 L1 事件总线，合规） */
    err = app_evt_register(APP_EVT_VOICE_STATE_CHANGED, on_voice_state, NULL);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "订阅 VOICE_STATE_CHANGED 失败: %s", esp_err_to_name(err));
        return err;
    }

    err = ws2812_init(BOARD_LED_GPIO);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "WS2812 初始化失败: %s", esp_err_to_name(err));
        (void)app_evt_unregister(APP_EVT_VOICE_STATE_CHANGED, on_voice_state, NULL);
        return err;
    }
    led_apply(); /* 上电初始态 = IDLE（vs_init 之前没有状态迁移事件） */
#endif

    static esp_timer_handle_t s_scan_timer;
    const esp_timer_create_args_t scan_args = {
        .callback = scan_timer_cb,
        .name = "dev_scan",
    };
    err = esp_timer_create(&scan_args, &s_scan_timer);
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

#if CONFIG_DEV_LED_ENABLE
    static esp_timer_handle_t s_led_timer;
    const esp_timer_create_args_t led_args = {
        .callback = led_timer_cb,
        .name = "dev_led",
    };
    err = esp_timer_create(&led_args, &s_led_timer);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "LED 闪烁定时器创建失败: %s", esp_err_to_name(err));
        esp_timer_stop(s_scan_timer);
        esp_timer_delete(s_scan_timer);
        return err;
    }
    err = esp_timer_start_periodic(s_led_timer, LED_TICK_MS * 1000);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "LED 闪烁定时器启动失败: %s", esp_err_to_name(err));
        esp_timer_delete(s_led_timer);
        esp_timer_stop(s_scan_timer);
        esp_timer_delete(s_scan_timer);
        return err;
    }
#endif

    /* 初始电平认定：以上电当前采样为准（正常上电按键为释放态；按住 BOOT 复位会进下载模式，
     * 见 HARDWARE §2 strapping 说明，固件侧无需特殊处理） */
    s_raw_last = (gpio_get_level((gpio_num_t)BOARD_BUTTON_GPIO) == 0);
    s_level = s_raw_last;
    s_stable_cnt = STABLE_SAMPLES;
    s_hold_ms = 0;
    s_long_fired = false;

    s_inited = true;
    ESP_LOGI(TAG, "ready: button=GPIO%d (debounce %dms, long %dms), led=%s",
             BOARD_BUTTON_GPIO, CONFIG_DEV_BUTTON_DEBOUNCE_MS,
             CONFIG_DEV_BUTTON_LONG_PRESS_MS,
             CONFIG_DEV_LED_ENABLE ? "on" : "off");
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
#if CONFIG_DEV_LED_ENABLE
    return s_muted;
#else
    return false;
#endif
}

esp_err_t dev_set_mute(bool mute)
{
#if !CONFIG_DEV_LED_ENABLE
    (void)mute;
    return ESP_ERR_NOT_SUPPORTED; /* LED/静音随 CONFIG_DEV_LED_ENABLE 裁剪 */
#else
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
        ESP_LOGI(TAG, "静音开（原音量 %u，LED 白色常亮）", (unsigned)s_saved_volume);
    } else {
        err = audio_player_set_volume(s_saved_volume);
        if (err != ESP_OK) {
            ESP_LOGE(TAG, "取消静音失败: %s", esp_err_to_name(err));
            return err;
        }
        ESP_LOGI(TAG, "静音关（音量恢复 %u）", (unsigned)s_saved_volume);
    }
    s_muted = mute;
    led_apply();
    return ESP_OK;
#endif
}

esp_err_t dev_led_set_mode(dev_led_mode_t mode)
{
#if !CONFIG_DEV_LED_ENABLE
    (void)mode;
    return ESP_ERR_NOT_SUPPORTED;
#else
    if ((unsigned)mode >= (unsigned)DEV_LED_MODE_COUNT) {
        return ESP_ERR_INVALID_ARG;
    }
    if (!s_inited) {
        return ESP_ERR_INVALID_STATE;
    }
    if (mode == DEV_LED_AUTO) {
        s_led_mode = DEV_LED_AUTO; /* 恢复跟随：按最近一次会话状态重绘 */
        led_apply();
        return ESP_OK;
    }
    s_led_mode = mode;
    led_apply();
    return ESP_OK;
#endif
}

dev_led_mode_t dev_led_get_mode(void)
{
#if CONFIG_DEV_LED_ENABLE
    return s_led_mode;
#else
    return DEV_LED_AUTO;
#endif
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

#else /* !CONFIG_DEV_ENABLE */

/* 组件被编译期关闭时保持链接可用：全部接口空实现 */
esp_err_t dev_init(void) { return ESP_OK; }
esp_err_t dev_register_cb(dev_button_cb_t cb, void *arg) { (void)cb; (void)arg; return ESP_OK; }
bool dev_is_muted(void) { return false; }
esp_err_t dev_set_mute(bool mute) { (void)mute; return ESP_ERR_NOT_SUPPORTED; }
esp_err_t dev_led_set_mode(dev_led_mode_t mode) { (void)mode; return ESP_ERR_NOT_SUPPORTED; }
dev_led_mode_t dev_led_get_mode(void) { return DEV_LED_AUTO; }
esp_err_t dev_stats_read(dev_stats_t *out) { if (out) memset(out, 0, sizeof(*out)); return ESP_OK; }
esp_err_t dev_stats_reset(void) { return ESP_OK; }

#endif /* CONFIG_DEV_ENABLE */
