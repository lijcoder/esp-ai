/*
 * event_bus —— 全局事件定义与分发（L1 基础服务）的唯一对外头文件。
 *
 * 同层模块（net_mgr / transport_ws / audio_fe / voice_cloud ...）之间禁止直接互调，
 * 统一经本组件投递/订阅事件（CONVENTIONS §2）。事件 ID 与 ARCHITECTURE §4.2 一致，
 * 增删事件必须同步架构文档。
 */
#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "esp_err.h"
#include "esp_event.h"

#ifdef __cplusplus
extern "C" {
#endif

/* 应用事件基：所有应用事件都挂在 APP_EVT 下（定义在 event_bus.c，此处只声明） */
ESP_EVENT_DECLARE_BASE(APP_EVT);

/*
 * 全局事件 ID（ARCHITECTURE §4.2 定稿）。
 * 数值即日志/调试口可见的 ID：只允许在末尾追加，改动顺序需要同步架构文档。
 */
typedef enum {
    APP_EVT_ANY = -1,               /* 仅用于注册：订阅 APP_EVT 下所有事件 */
    APP_EVT_NET_UP = 0,             /* 网络就绪，payload: app_evt_net_up_t */
    APP_EVT_NET_DOWN,               /* 网络断开，payload: app_evt_net_down_t */
    APP_EVT_WAKE_DETECTED,          /* 唤醒词命中，无 payload */
    APP_EVT_VAD_SPEECH_START,       /* 人声开始，无 payload */
    APP_EVT_VAD_SPEECH_END,         /* 人声结束，无 payload */
    APP_EVT_ASR_PARTIAL,            /* ASR 中间结果，payload: app_evt_asr_t（T2.5 定稿） */
    APP_EVT_ASR_FINAL,              /* ASR 最终结果，payload: app_evt_asr_t（T2.5 定稿） */
    APP_EVT_TTS_START,              /* 云端 TTS 起播，payload: app_evt_tts_start_t（T2.5 定稿） */
    APP_EVT_TTS_END,                /* 云端 TTS 结束，payload: app_evt_tts_end_t（T2.5 定稿） */
    APP_EVT_VOICE_STATE_CHANGED,    /* 会话状态迁移，payload 待 T3.1 定稿 */
    APP_EVT_BUTTON,                 /* 按键动作，payload: app_evt_button_t（T3.2 定稿） */
    APP_EVT_MUSIC_STATE_CHANGED,    /* 音乐状态变化，payload 待 T4.3 定稿 */
    APP_EVT_MUSIC_ERROR,            /* 音乐播放出错，payload 待 T4.3 定稿 */
    APP_EVT_FEEDBACK,               /* 异常反馈（T3.6），payload: app_evt_feedback_t；
                                     * 语义事件：语音只是当前反馈通道，屏幕/灯光等未来通道
                                     * 订阅本事件即可，不需要改 feedback/voice_session */
    APP_EVT_COUNT,                  /* 哨兵：事件数量，不是合法事件 */
} app_evt_t;

/* 事件 payload 容器固定 32 字节：跨任务投递时整块拷贝，发布方无需保活 */
#define APP_EVT_DATA_MAX 32
/* IPv4 点分字符串最大 15 字符 + '\0' */
#define APP_EVT_IP_MAX 16

typedef struct {
    char ip[APP_EVT_IP_MAX]; /* 本机 IPv4（"192.168.1.42"） */
    int8_t rssi;             /* 关联 AP 的信号强度（dBm，负值） */
} app_evt_net_up_t;

typedef struct {
    uint8_t reason;    /* WiFi/NETIF 断开原因码，原样透传（见 esp_wifi_types.h 的 wifi_err_reason_t） */
    uint32_t retry_ms; /* net_mgr 的下次重连延时（退避 1s→30s）；0 表示不再重试 */
} app_evt_net_down_t;

/* ---------------- 语音事件 payload（T2.5 定稿，见 ARCHITECTURE §4.2） ---------------- */

/* ASR 文本最大字节数：payload 定长 32B − sid(2) − len(2) = 28。
 * 中文 UTF-8 每字 3B，约 9 个汉字；更长的文本会被截断，完整文本只在 VC 侧 INFO 日志里。 */
#define APP_EVT_TEXT_MAX 28

/* APP_EVT_ASR_PARTIAL / APP_EVT_ASR_FINAL：识别文本（可能截断，len 为实际拷贝字节数） */
typedef struct {
    uint16_t sid;                /* 所属会话 */
    uint16_t len;                /* text 有效字节数（≤ APP_EVT_TEXT_MAX） */
    char text[APP_EVT_TEXT_MAX]; /* UTF-8，不保证 '\0' 结尾（按 len 读） */
} app_evt_asr_t;

/* TTS codec 取值（ARCHITECTURE §3.7：端侧只实现 MP3） */
#define APP_EVT_CODEC_UNKNOWN 0
#define APP_EVT_CODEC_MP3 1

/* APP_EVT_TTS_START：云端开始下发 TTS（binary 帧随后到达，T2.4 解码播放） */
typedef struct {
    uint16_t sid;
    uint8_t codec; /* APP_EVT_CODEC_* */
    uint8_t reserved;
    uint32_t sr; /* 采样率（Hz），契约里 tts_start.sr */
} app_evt_tts_start_t;

/* APP_EVT_TTS_END：本轮 TTS 下发结束（frames = 本轮收到的下行音频帧数） */
typedef struct {
    uint16_t sid;
    uint16_t frames;
} app_evt_tts_end_t;

/* ---------------- 会话状态事件 payload（T3.1 定稿，见 ARCHITECTURE §4.2/§7） ---------------- */

/* APP_EVT_BUTTON（T3.2 定稿）：按键动作。T3.4 的 dev_ctrl 只负责 GPIO 扫描/去抖并投递，
 * voice_session 消费：短按 = 打断当前会话（IDLE 下 = 开始对话，ARCH §8.2），长按 = 静音（T3.4）。 */
typedef enum {
    APP_EVT_BUTTON_SHORT = 1, /* 短按（<1s） */
    APP_EVT_BUTTON_LONG = 2,  /* 长按（≥1s，T3.4 接线） */
} app_evt_button_action_t;

typedef struct {
    uint8_t action; /* app_evt_button_action_t；非法值按短按处理 */
    uint8_t reserved[3];
} app_evt_button_t;

/* APP_EVT_VOICE_STATE_CHANGED：会话状态迁移。from/to 是 voice_session 的 vs_state_t 数值
 * （0=IDLE / 1=LISTENING / 2=THINKING / 3=SPEAKING / 4=ERROR / 5=FOLLOW_UP），reason 是
 * vs_reason_t 数值。event_bus 是 L1，不能 include L4 的 voice_session.h，所以这里用数值 +
 * 注释契约。 */
typedef struct {
    uint16_t sid;
    uint8_t from;
    uint8_t to;
    uint8_t reason;
    uint8_t reserved[3];
    uint32_t dur_ms; /* 迁移前状态停留时长（ms） */
} app_evt_voice_state_t;

/* APP_EVT_FEEDBACK（T3.6）：异常反馈语义事件。
 * kind 数值与 feedback 组件的 fb_kind_t 一致：1=net-offline / 2=cloud-error / 3=timeout /
 * 4=tts-fail（event_bus 是 L1，不能 include L3 的 feedback 头，故用数值 + 注释契约）。
 * 当前反馈通道是语音（feedback 组件内部完成），未来屏幕/灯光组件订阅本事件即可，
 * 不需要改 feedback/voice_session——这就是"语音只是一种反馈方式"的扩展点。 */
typedef struct {
    uint16_t sid;  /* 所属会话（无会话语境为最近 sid 或 0） */
    int32_t code;  /* 原始错误码（云端 error code / vc_tts_fail_t / 0） */
    uint8_t kind;  /* fb_kind_t 数值 */
    uint8_t reserved[3];
} app_evt_feedback_t;

/*
 * 事件 payload 统一容器：定长 32 字节，跨任务投递时整块拷贝，发布方无需保活。
 * 新增字段时不得让 union 超过 APP_EVT_DATA_MAX；payload 超过 32B 的文本类数据
 * （完整 ASR 文本）不放事件，改由发布方日志/组件 API 提供（T3.1 再定所有权）。
 */
typedef union {
    app_evt_net_up_t net_up;
    app_evt_net_down_t net_down;
    app_evt_asr_t asr;
    app_evt_tts_start_t tts_start;
    app_evt_tts_end_t tts_end;
    app_evt_voice_state_t voice_state;
    app_evt_button_t button;
    app_evt_feedback_t feedback;
    uint8_t raw[APP_EVT_DATA_MAX];
} app_evt_data_t;

/*
 * 事件处理器：在 event_bus 的 `evt` 任务上下文（核 0）执行，不要做耗时/阻塞操作。
 * data 指向本次事件的 payload 副本，只在回调内有效（返回后即被释放）；无 payload 时为 NULL。
 */
typedef void (*app_evt_handler_t)(void *arg, app_evt_t evt, const app_evt_data_t *data);

/**
 * @brief 创建独立的事件循环（不占用系统默认循环）并启动 `evt` 分发任务
 *
 * @note 只应在 app_main 中调用一次，且早于任何 register/post；重复调用返回 ESP_ERR_INVALID_STATE。
 * @note 任务属性固定为 ARCHITECTURE §9.3：核 0 / 优先级 5 / 栈 4KB；队列长度走 Kconfig。
 */
esp_err_t app_evt_init(void);

/**
 * @brief 注册事件处理器
 *
 * @param evt  事件 ID；APP_EVT_ANY 表示订阅全部应用事件
 * @param cb   处理器（在 evt 任务上下文执行）
 * @param arg  透传给 cb 的参数（调用方保证生命周期）
 *
 * @return
 *   - ESP_OK 成功
 *   - ESP_ERR_INVALID_STATE 尚未 app_evt_init()，或同一 (evt, cb, arg) 重复注册
 *   - ESP_ERR_INVALID_ARG 参数非法
 *   - ESP_ERR_NO_MEM 处理器槽位（8 个）已满
 */
esp_err_t app_evt_register(app_evt_t evt, app_evt_handler_t cb, void *arg);

/**
 * @brief 注销事件处理器（参数必须与注册时完全一致）
 *
 * @return ESP_OK / ESP_ERR_INVALID_STATE（未初始化）/ ESP_ERR_NOT_FOUND（未注册）
 */
esp_err_t app_evt_unregister(app_evt_t evt, app_evt_handler_t cb, void *arg);

/**
 * @brief 投递事件（非阻塞）
 *
 * 队列满时立即返回 ESP_ERR_TIMEOUT（打 WARN 并丢弃），调用方不得等待。
 *
 * @param data payload；NULL 表示该事件无 payload
 *
 * @note **音频实时任务（afe_feed/afe_fetch）只能投递无 payload 的事件**：
 *       esp_event 在带 payload 时会内部 calloc 一份拷贝（IDF 5.4 实现），
 *       在音频任务里调用违反 CONVENTIONS §3 的"禁止 malloc/阻塞"。
 * @note 返回 ESP_ERR_NO_MEM 说明 payload 拷贝分配失败（内部 RAM 紧张）。
 */
esp_err_t app_evt_post(app_evt_t evt, const app_evt_data_t *data);

/* 事件名（日志/调试口用）；非法或未命名事件返回 "UNKNOWN" */
const char *app_evt_name(app_evt_t evt);

#ifdef __cplusplus
}
#endif
