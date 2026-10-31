/*
 * voice_cloud —— 语音云协议（L3 能力层）的唯一对外头文件。
 *
 * 职责边界（ARCHITECTURE §3.6、§4.1、§6、§9.3；PROTOCOL.md）：
 *   - 一轮语音会话的**协议与链路**：`audio_start` → 读 `audio_fe` 上行 PCM（320 样本/20ms）
 *     → `enc_encode()` → `ws_send_bin(0x01)` → `enc_flush()` 尾帧 → `audio_end`；
 *   - 下行文本事件解析（asr_partial/asr_final/llm_delta/tts_start/tts_end/error），按 `sid`
 *     丢弃迟到帧，再分发：`event_bus` 应用事件 + `vc_register_cb()` 业务回调；
 *   - **不含会话状态机**：抓 VAD、半双工 R1–R5、错误提示音、按键打断归 T3.1 `voice_session`；
 *     本组件的三态（IDLE/UPLINK/WAIT_RESPONSE）只为"一轮上行"服务，T3.1 落地后由它驱动。
 *
 * 线程模型（ARCHITECTURE §9.3）：一个 `vc` 任务（核 0 / 优先级 4 / 8KB），上行循环与
 * 编码都在该任务里串行（libopus 状态/伪栈在 PSRAM，任务栈只需 8KB）。
 * 下行回调在 `ws_client` 任务上下文执行（`transport_ws` 的收包任务），**禁止阻塞**。
 *
 * 与本组件的三个使用约束：
 *   1. 编码器按轮次 open/close（`enc test` 等 console 探针在 IDLE 时可用，轮次中会被拒绝）；
 *   2. 上行接口（`audio_fe_uplink_*`）同一时刻只有一个消费方：轮次中 `afe uplink on` 无效；
 *   3. 下行二进制帧（TTS 音频）本任务只统计并丢弃（T2.4 接入解码播放），不消费 payload。
 */
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

/* 一轮语音的链路状态（不是会话状态机，见文件头说明） */
typedef enum {
    VC_STATE_IDLE = 0,      /* 没有进行中的轮次 */
    VC_STATE_UPLINK,        /* audio_start 已发，正在上行 Opus 帧 */
    VC_STATE_WAIT_RESPONSE, /* audio_end 已发，等 asr/tts 事件 */
} vc_state_t;

/* 下行事件（本组件解析后交给业务回调；event_bus 只覆盖其中 4 种） */
typedef enum {
    VC_EVT_ASR_PARTIAL = 0, /* 中间识别结果 */
    VC_EVT_ASR_FINAL,       /* 最终识别结果 */
    VC_EVT_LLM_DELTA,       /* LLM 增量文本（无对应 event_bus 事件，仅走回调） */
    VC_EVT_TTS_START,       /* 云端 TTS 起播（随后是下行 0x02 音频帧） */
    VC_EVT_TTS_END,         /* 本轮 TTS 结束 */
    VC_EVT_CLOUD_ERROR,     /* 云端 error 事件（错误码见 PROTOCOL.md 错误码表） */
} vc_event_t;

typedef struct {
    vc_event_t evt;
    uint16_t sid;            /* 所属会话 */
    const char *text;        /* ASR/LLM 文本（UTF-8，可能截断；**只在回调内有效**） */
    uint16_t text_len;       /* text 实际字节数 */
    uint8_t codec;           /* VC_EVT_TTS_START：1=mp3（0=未知） */
    uint32_t sample_rate;    /* VC_EVT_TTS_START：采样率（Hz） */
    int32_t code;            /* VC_EVT_CLOUD_ERROR：云端错误码 */
} vc_event_msg_t;

/* 业务回调：在 `ws_client` 任务上下文执行，禁止阻塞/耗时；text 返回后失效 */
typedef void (*vc_cb_t)(const vc_event_msg_t *msg, void *arg);

/* 观测统计（仅供 diag / 测试脚本断言，字段为累计值） */
typedef struct {
    uint32_t rounds_started;   /* 发起轮数 */
    uint32_t rounds_done;      /* 收到 tts_end 正常结束的轮数 */
    uint32_t rounds_aborted;   /* 中断/断链/响应超时结束的轮数 */
    uint32_t up_frames;        /* 上行 Opus 帧数（含 enc_flush 尾帧） */
    uint32_t up_bytes;         /* 上行 Opus 字节数（不含 8B 帧头） */
    uint32_t up_encode_errors; /* 编码失败次数 */
    uint32_t up_send_errors;   /* 入队失败次数（未 READY / 队列满） */
    uint32_t up_read_timeouts; /* 上行读超时次数（音频还没到） */
    uint32_t rx_text;          /* 收到的下行文本帧数（业务事件，传输层已滤掉 pong/hello_ack） */
    uint32_t rx_audio_frames;  /* 收到的下行音频帧数（T2.5 只统计，T2.4 播放） */
    uint32_t rx_dropped_sid;   /* 因 sid 不匹配/无会话而丢弃的迟到事件数 */
    uint32_t rx_parse_errors;  /* 下行 JSON 解析失败数 */
    uint32_t rx_cloud_errors;  /* 云端 error 事件数 */
    uint32_t first_frame_us;   /* 最近一轮：vc_start → 首帧入队的耗时（µs）；0 表示未测到 */
    uint32_t last_round_ms;    /* 最近一轮上行时长（ms） */
    uint16_t last_sid;         /* 最近一轮 sid */
    bool last_round_vad_end;   /* 最近一轮收尾原因：true=VAD 静音，false=累计时长超时 */
} vc_stats_t;

/**
 * @brief 初始化：注册 WS 主接收回调 + 建 `vc` 任务
 *
 * @note 只应在 app_main 中调用一次，且必须在 ws_init() 之后（回调要挂到已初始化的传输层）；
 *       重复调用返回 ESP_ERR_INVALID_STATE。
 */
esp_err_t vc_init(void);

/**
 * @brief 发起一轮语音（异步）：等 `vc` 任务处理时检查网络、打开编码器、发 audio_start
 *
 * 若当前已有轮次，会**作废当前轮次**（不发 audio_end）再开始新轮次，保证"新唤醒覆盖旧轮次"。
 *
 * @return ESP_OK 已受理 / ESP_ERR_INVALID_STATE 未初始化或云端未 READY
 *         （未 READY 时调用方按"网络未就绪"降级，不要重试等待）
 */
esp_err_t vc_start(uint16_t sid);

/* 同上，sid 由本组件自增（每轮 +1，uint16 回绕；T2.5 简化口径，T3.1 接管后由调用方给） */
esp_err_t vc_start_auto(void);

/**
 * @brief 请求优雅结束当前上行：补尾帧 + 发 audio_end，然后等云端响应（非阻塞）
 *
 * 仅对 UPLINK 状态有效；VC_STATE_WAIT_RESPONSE / IDLE 时为 no-op。
 */
esp_err_t vc_stop(void);

/* 立即放弃当前轮次（不补尾帧、不发 audio_end）；打断/断网路径用 */
esp_err_t vc_cancel(void);

/* 是否有一轮在进行（UPLINK 或 WAIT_RESPONSE） */
bool vc_is_busy(void);

vc_state_t vc_get_state(void);
const char *vc_state_name(vc_state_t state);

/* 当前轮次 sid（IDLE 时为最近一轮的 sid） */
uint16_t vc_current_sid(void);

/* 注册业务回调（覆盖语义，传 NULL 取消）；与 event_bus 事件同时分发 */
esp_err_t vc_register_cb(vc_cb_t cb, void *arg);

/* 读统计快照 / 清零统计（任意任务上下文可调用；不影响进行中的轮次） */
esp_err_t vc_stats_read(vc_stats_t *out);
esp_err_t vc_stats_reset(void);

#ifdef __cplusplus
}
#endif
