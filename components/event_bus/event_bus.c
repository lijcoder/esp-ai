/*
 * event_bus —— 全局事件定义与分发（L1 基础服务）。
 *
 * 为什么自建独立循环而不共用系统 default loop：WiFi/esp_netif/系统组件也挂在 default
 * loop 上，共用会让"网络事件处理"与"业务事件处理"互相阻塞，且无法控制任务优先级与
 * 队列长度。这里创建独立循环，任务属性写入 ARCHITECTURE §9.3，便于 `sys tasks` 观测。
 */
#include "event_bus.h"

#include <string.h>

#include "esp_log.h"
#include "sdkconfig.h"

static const char *TAG = "EVT";

/* 事件基定义：全工程只能有一处（头文件里是 ESP_EVENT_DECLARE_BASE） */
ESP_EVENT_DEFINE_BASE(APP_EVT);

/* 任务属性与 ARCHITECTURE §9.3 一致；改动必须同步架构文档 */
#define EVT_TASK_NAME  "evt"
#define EVT_TASK_STACK 4096
#define EVT_TASK_PRIO  5
#define EVT_CORE_ID    0

/* 处理器槽位上限：P1 只有 trace + 少量模块订阅，8 个足够；满了返回 ESP_ERR_NO_MEM */
#define EVT_MAX_HANDLERS 8

typedef struct {
    bool used;
    app_evt_t evt;
    app_evt_handler_t cb;
    void *arg;
    esp_event_handler_instance_t instance;
} handler_slot_t;

static esp_event_loop_handle_t s_loop;
static handler_slot_t s_slots[EVT_MAX_HANDLERS];

/* 事件名表：下标即 app_evt_t 数值；新增事件必须在此补名字（漏了会显示 UNKNOWN） */
static const char *const k_evt_names[APP_EVT_COUNT] = {
    [APP_EVT_NET_UP] = "NET_UP",
    [APP_EVT_NET_DOWN] = "NET_DOWN",
    [APP_EVT_WAKE_DETECTED] = "WAKE_DETECTED",
    [APP_EVT_VAD_SPEECH_START] = "VAD_SPEECH_START",
    [APP_EVT_VAD_SPEECH_END] = "VAD_SPEECH_END",
    [APP_EVT_ASR_PARTIAL] = "ASR_PARTIAL",
    [APP_EVT_ASR_FINAL] = "ASR_FINAL",
    [APP_EVT_TTS_START] = "TTS_START",
    [APP_EVT_TTS_END] = "TTS_END",
    [APP_EVT_VOICE_STATE_CHANGED] = "VOICE_STATE_CHANGED",
    [APP_EVT_BUTTON] = "BUTTON",
    [APP_EVT_MUSIC_STATE_CHANGED] = "MUSIC_STATE_CHANGED",
    [APP_EVT_MUSIC_ERROR] = "MUSIC_ERROR",
};

_Static_assert(sizeof(k_evt_names) / sizeof(k_evt_names[0]) == APP_EVT_COUNT,
               "k_evt_names 与 app_evt_t 数量不一致：新增事件时补名字");
_Static_assert(sizeof(app_evt_data_t) == APP_EVT_DATA_MAX,
               "app_evt_data_t 超过 APP_EVT_DATA_MAX：payload 必须保持定长");

const char *app_evt_name(app_evt_t evt)
{
    if ((int)evt < 0 || evt >= APP_EVT_COUNT) {
        return "UNKNOWN";
    }
    const char *name = k_evt_names[evt];
    return name != NULL ? name : "UNKNOWN";
}

static handler_slot_t *find_slot(app_evt_t evt, app_evt_handler_t cb, void *arg)
{
    for (size_t i = 0; i < EVT_MAX_HANDLERS; i++) {
        if (s_slots[i].used && s_slots[i].evt == evt && s_slots[i].cb == cb && s_slots[i].arg == arg) {
            return &s_slots[i];
        }
    }
    return NULL;
}

/* esp_event 回调 → 业务处理器：槽位指针作为 handler_arg，保证回调能找回 cb/arg */
static void dispatch_raw(void *arg, esp_event_base_t base, int32_t evt_id, void *data)
{
    (void)base;
    handler_slot_t *slot = (handler_slot_t *)arg;
    if (slot != NULL && slot->cb != NULL) {
        slot->cb(slot->arg, (app_evt_t)evt_id, (const app_evt_data_t *)data);
    }
}

esp_err_t app_evt_init(void)
{
    if (s_loop != NULL) {
        return ESP_ERR_INVALID_STATE;
    }

    const esp_event_loop_args_t args = {
        .queue_size = CONFIG_EVENT_BUS_QUEUE_SIZE,
        .task_name = EVT_TASK_NAME,
        .task_stack_size = EVT_TASK_STACK,
        .task_priority = EVT_TASK_PRIO,
        .task_core_id = EVT_CORE_ID,
    };

    esp_err_t err = esp_event_loop_create(&args, &s_loop);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "创建事件循环失败: %s", esp_err_to_name(err));
        return err;
    }

    ESP_LOGI(TAG, "事件总线就绪：queue=%d，任务 %s（核 %d / 优先级 %d / 栈 %d B）",
             CONFIG_EVENT_BUS_QUEUE_SIZE, EVT_TASK_NAME, EVT_CORE_ID, EVT_TASK_PRIO,
             EVT_TASK_STACK);
    return ESP_OK;
}

esp_err_t app_evt_register(app_evt_t evt, app_evt_handler_t cb, void *arg)
{
    if (s_loop == NULL) {
        return ESP_ERR_INVALID_STATE;
    }
    if (cb == NULL || (evt != APP_EVT_ANY && ((int)evt < 0 || evt >= APP_EVT_COUNT))) {
        return ESP_ERR_INVALID_ARG;
    }
    if (find_slot(evt, cb, arg) != NULL) {
        return ESP_ERR_INVALID_STATE; /* 重复注册：调用方按"已开启"处理即可 */
    }

    handler_slot_t *slot = NULL;
    for (size_t i = 0; i < EVT_MAX_HANDLERS; i++) {
        if (!s_slots[i].used) {
            slot = &s_slots[i];
            break;
        }
    }
    if (slot == NULL) {
        ESP_LOGE(TAG, "处理器槽位已满（%d 个）", EVT_MAX_HANDLERS);
        return ESP_ERR_NO_MEM;
    }

    const int32_t evt_id = (evt == APP_EVT_ANY) ? (int32_t)ESP_EVENT_ANY_ID : (int32_t)evt;
    esp_err_t err = esp_event_handler_instance_register_with(s_loop, APP_EVT, evt_id, dispatch_raw,
                                                             slot, &slot->instance);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "注册处理器失败（%s）: %s", app_evt_name(evt), esp_err_to_name(err));
        return err;
    }

    slot->used = true;
    slot->evt = evt;
    slot->cb = cb;
    slot->arg = arg;
    ESP_LOGD(TAG, "注册处理器：%s", evt == APP_EVT_ANY ? "ANY" : app_evt_name(evt));
    return ESP_OK;
}

esp_err_t app_evt_unregister(app_evt_t evt, app_evt_handler_t cb, void *arg)
{
    if (s_loop == NULL) {
        return ESP_ERR_INVALID_STATE;
    }

    handler_slot_t *slot = find_slot(evt, cb, arg);
    if (slot == NULL) {
        return ESP_ERR_NOT_FOUND;
    }

    const int32_t evt_id = (evt == APP_EVT_ANY) ? (int32_t)ESP_EVENT_ANY_ID : (int32_t)evt;
    esp_err_t err = esp_event_handler_instance_unregister_with(s_loop, APP_EVT, evt_id, slot->instance);
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "注销处理器失败（%s）: %s", app_evt_name(evt), esp_err_to_name(err));
        return err;
    }

    memset(slot, 0, sizeof(*slot));
    ESP_LOGD(TAG, "注销处理器：%s", evt == APP_EVT_ANY ? "ANY" : app_evt_name(evt));
    return ESP_OK;
}

esp_err_t app_evt_post(app_evt_t evt, const app_evt_data_t *data)
{
    if (s_loop == NULL) {
        return ESP_ERR_INVALID_STATE;
    }
    if ((int)evt < 0 || evt >= APP_EVT_COUNT) {
        return ESP_ERR_INVALID_ARG;
    }

    /*
     * 超时传 0：非阻塞。音频任务内投递时绝不能被队列阻塞（CONVENTIONS §3）。
     * 带 payload 时 esp_event 内部会 calloc 一份定长拷贝，所以音频任务只许投递 data == NULL。
     */
    esp_err_t err = esp_event_post_to(s_loop, APP_EVT, (int32_t)evt, data,
                                      data != NULL ? sizeof(*data) : 0, 0);
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "投递 %s 失败: %s（队列满则事件被丢弃）", app_evt_name(evt),
                 esp_err_to_name(err));
        return err;
    }

    ESP_LOGD(TAG, "投递 %s%s", app_evt_name(evt), data != NULL ? "（带 payload）" : "");
    return ESP_OK;
}
