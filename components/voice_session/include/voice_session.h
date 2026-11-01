/*
 * voice_session —— 语音会话状态机（L4 业务编排）的唯一对外头文件。
 *
 * 职责边界（ARCHITECTURE §7、§8.1–8.2、§4.1；PROTOCOL.md §3.3）：
 *   - 会话状态机 IDLE / LISTENING / THINKING / SPEAKING / ERROR，**状态迁移只在本组件发生**；
 *   - 每轮 `sid` 分配（uint16 自增、0 保留），通过 `vc_start(sid)` 交给 voice_cloud 执行；
 *   - 超时与错误处理策略：THINKING 10s 超时（计时在 voice_cloud 的协议超时里）→ ERROR；
 *     云端 `error` 码 → ERROR；断链/打断 → IDLE；ERROR 同轮不重试；
 *   - 唤醒编排（T1.2b）：订阅 event_bus 的 WAKE_DETECTED / VAD / NET_DOWN，唤醒后播应答提示音
 *     （R1 暂停收音）→ 提示音播完恢复收音（R2 由 voice_cloud 负责 TTS 侧的对应规则）→ 起一轮；
 *   - 打断接口 `vs_interrupt()`：打断骨架，按键来源由 T3.4 `dev_ctrl` 接线。
 *
 * 线程模型：**不新建任务**。事件处理在 `evt` 任务上下文（event_bus 分发），voice_cloud 回调在
 * `ws_client`/`vc` 任务上下文，播放完成回调在 `player` 任务上下文，console 命令在 REPL 任务。
 * 所有 handler 只做"状态迁移 + 非阻塞调用"，禁止阻塞（CONVENTIONS §2/§3）。
 * 之所以不建 4KB 任务：T2.4 实测 wss 握手期内部堆最低水位只剩 32.7KB，再起任务会击穿
 * "≥30KB" 铁律（CONVENTIONS §4）。此口径已写入 ARCHITECTURE §9.3 与 STATUS。
 *
 * 与 voice_cloud 的分工：voice_cloud 是"协议执行器"（一轮上行/下行解析/0x02 播放/R1–R2），
 * 本组件是"编排者"（何时起轮、状态、错误策略）。其它模块不得绕过本组件改会话状态
 * （CONVENTIONS §9）。
 */
#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

/* 会话状态（ARCHITECTURE §7；数值写入 APP_EVT_VOICE_STATE_CHANGED 的 payload） */
typedef enum {
    VS_STATE_IDLE = 0,      /* 只听唤醒，音乐可播放（P4） */
    VS_STATE_LISTENING,     /* 已唤醒（或手动开始），播提示音 / 上行、VAD 判停 */
    VS_STATE_THINKING,      /* audio_end 已发，等 ASR/TTS */
    VS_STATE_SPEAKING,      /* 正在播放 TTS（R1：麦克风输入关闭） */
    VS_STATE_ERROR,         /* 超时/云端错误：上报日志 + 播错误提示（提示音 T3.6） */
    VS_STATE_COUNT,
} vs_state_t;

/* 状态迁移原因（写日志与事件 payload；只允许追加） */
typedef enum {
    VS_REASON_NONE = 0,
    VS_REASON_WAKE = 1,           /* 唤醒词命中 */
    VS_REASON_START = 2,          /* vs_start() 主动开始 */
    VS_REASON_UPLINK_END = 3,     /* audio_end 已发出（VAD 静音/时长上限/stop） */
    VS_REASON_TTS_START = 4,      /* 云端开始下发 TTS */
    VS_REASON_TTS_END = 5,        /* 云端 TTS 下发结束 */
    VS_REASON_STREAM_DONE = 6,    /* TTS 流式播放完成（含打断 ok=false） */
    VS_REASON_ROUND_DONE = 7,     /* voice_cloud 报告本轮正常结束 */
    VS_REASON_CLOUD_ERROR = 8,    /* 云端 error 事件 */
    VS_REASON_TIMEOUT = 9,        /* 等响应超时 */
    VS_REASON_LINK_LOST = 10,     /* WS 断链 */
    VS_REASON_INTERRUPT = 11,     /* vs_interrupt()（按键打断，T3.2/T3.4） */
    VS_REASON_NET_DOWN = 12,      /* 网络断开事件 */
    VS_REASON_CLOUD_NOT_READY = 13, /* 起轮时云端未 READY（ARCH §8.5 离线降级） */
    VS_REASON_PROMPT_FAILED = 14, /* 唤醒应答提示音播放失败 */
    VS_REASON_START_FAILED = 15,  /* 起轮失败（编码器/上行占用/发送失败） */
    VS_REASON_ERROR_DONE = 16,    /* ERROR 状态处理结束，回 IDLE */
    VS_REASON_ROUND_ABORT = 17,   /* voice_cloud 报告的其它异常结束（cancel/new/setup） */
} vs_reason_t;

/* 观测统计（diag `vs status` / 测试脚本断言用；累计值，临界区内合并） */
typedef struct {
    uint32_t transitions;      /* 状态迁移次数 */
    uint32_t rounds_started;   /* 进入 LISTENING 的轮数 */
    uint32_t rounds_completed; /* 正常回到 IDLE 的轮数 */
    uint32_t rounds_errored;   /* 进入 ERROR 的轮数 */
    uint32_t round_aborts;     /* voice_cloud 报告的异常结束数 */
    uint32_t interrupts;       /* vs_interrupt() 次数 */
    uint32_t wakes_dropped;    /* 会话中/播放器忙时忽略的唤醒数 */
    uint32_t timeouts;         /* THINKING 超时数 */
    uint32_t cloud_errors;     /* 云端 error 数 */
    uint16_t last_sid;         /* 最近一轮 sid */
    uint32_t last_state_ms;    /* 上一次状态停留时长（ms） */
    uint8_t state;             /* 当前 vs_state_t（快照） */
} vs_stats_t;

/**
 * @brief 初始化：订阅 event_bus（WAKE/VAD/NET_DOWN 等）、注册 voice_cloud 回调与播放完成回调
 *
 * @note 只应在 app_main 中调用一次，必须在 app_evt_init / vc_init / audio_player_init 之后；
 *       重复调用返回 ESP_ERR_INVALID_STATE。**不建任务**（见文件头内存约束）。
 */
esp_err_t vs_init(void);

/**
 * @brief 主动开始一轮会话（按键/调试路径；不播唤醒应答提示音，直接进入上行）
 *
 * @return ESP_OK 已受理 / ESP_ERR_INVALID_STATE 未初始化或已有会话进行中
 */
esp_err_t vs_start(void);

/**
 * @brief 打断当前会话（按键打断骨架，T3.2/T3.4 接线）：立即取消云端轮次并回 IDLE
 *
 * @return ESP_OK 已打断 / ESP_ERR_INVALID_STATE 未初始化或当前没有会话
 */
esp_err_t vs_interrupt(void);

/* 是否处于非 IDLE 状态 */
bool vs_is_busy(void);

vs_state_t vs_get_state(void);
const char *vs_state_name(vs_state_t state);
const char *vs_reason_name(vs_reason_t reason);

/* 当前轮次 sid（IDLE 时为最近一轮的 sid） */
uint16_t vs_current_sid(void);

/* 读统计快照 / 清零统计（任意任务上下文可调用） */
esp_err_t vs_stats_read(vs_stats_t *out);
esp_err_t vs_stats_reset(void);

#ifdef __cplusplus
}
#endif
