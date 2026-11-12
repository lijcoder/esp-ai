/*
 * voice_session —— 语音会话状态机（L4 业务编排）的唯一对外头文件。
 *
 * 职责边界（ARCHITECTURE §7、§8.1–8.2、§4.1；PROTOCOL.md §3.3）：
 *   - 会话状态机 IDLE / LISTENING / THINKING / SPEAKING / FOLLOW_UP / ERROR，
 *     **状态迁移只在本组件发生**；
 *   - 每轮 `sid` 分配（uint16 自增、0 保留），通过 `vc_start(sid)` 交给 voice_cloud 执行；
 *   - 超时与错误处理策略：THINKING 10s 超时（计时在 voice_cloud 的协议超时里）→ ERROR；
 *     云端 `error` 码 → ERROR；断链/打断 → IDLE；ERROR 同轮不重试；
 *   - 异常反馈（T3.6）：四类异常（网络未连接/云端 error/THINKING 超时/TTS 失败）→ 进 ERROR
 *     并调用 `feedback` 组件播放提示音；提示音完成回调驱动 `ERROR → IDLE`，
 *     `CONFIG_VS_ERROR_HOLD_MS` 只作兜底看门狗。**本组件不直接播错误提示音**（语音只是
 *     feedback 的一个通道，屏幕/灯光等未来通道订阅 `APP_EVT_FEEDBACK` 即可）；
 *   - 连续对话（T3.7）：TTS 正常播完后进入 FOLLOW_UP 续听窗口（`CONFIG_VS_FOLLOWUP_*`），
 *     窗口内 VAD 判到人声即起下一轮（不再唤醒、不再播应答提示音）；静音超时/轮数上限/
 *     按键/断网 → IDLE；窗口内再次唤醒 = 重开一轮（播应答提示音、轮数清零）；
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
    VS_STATE_ERROR,         /* 异常反馈中（T3.6）：播错误提示音，完成/看门狗后回 IDLE */
    VS_STATE_FOLLOW_UP,     /* 连续对话续听窗口（T3.7）：TTS 播完等用户直接说话 */
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
    VS_REASON_FOLLOWUP_SPEECH = 18,  /* 续听窗口内检测到人声，直接起下一轮（T3.7） */
    VS_REASON_FOLLOWUP_TIMEOUT = 19, /* 续听窗口超时无语音，回 IDLE */
    VS_REASON_FOLLOWUP_MAX = 20,     /* 达到连续对话轮数上限，回 IDLE */
    VS_REASON_FOLLOWUP_WAKE = 21,    /* 续听窗口内再次唤醒：重开一轮（播提示音） */
    VS_REASON_FOLLOWUP_OFF = 22,     /* 运行期关闭连续对话，退出续听窗口 */
    VS_REASON_NET_OFFLINE = 23,      /* 网络未连接/链路不可用 → 错误反馈（T3.6） */
    VS_REASON_TTS_FAIL = 24,         /* 设备侧/云端 TTS 播放失败 → 错误反馈（T3.6） */
    VS_REASON_MUSIC_START = 25,      /* 音乐开始播放 → 退出续听窗口（T4.4） */
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
    uint32_t wakes_during_tts; /* TTS 播放中检测到的唤醒数（C4 spike，仅计数） */
    uint32_t timeouts;         /* THINKING 超时数 */
    uint32_t cloud_errors;     /* 云端 error 数 */
    /* ---- T3.7 连续对话（FOLLOW_UP） ---- */
    uint32_t followup_rounds;  /* 免唤醒续听轮数（不含首轮唤醒轮） */
    uint32_t followup_hits;    /* 窗口内检测到人声、成功起轮次数 */
    uint32_t followup_timeouts;/* 窗口静音超时回 IDLE 次数 */
    uint32_t followup_max_exits; /* 达到轮数上限回 IDLE 次数 */
    uint32_t followup_remaining_ms; /* 当前续听窗口剩余（非 FOLLOW_UP 时为 0） */
    /* ---- T3.6 异常反馈（feedback 组件） ---- */
    uint32_t error_feedback_plays;  /* 请求并成功启动语音反馈的次数 */
    uint32_t error_feedback_ok;     /* 提示音正常播完次数 */
    uint32_t error_feedback_fail;   /* 提示音失败/被丢弃/看门狗次数 */
    uint32_t error_watchdog;        /* CONFIG_VS_ERROR_HOLD_MS 兜底超时次数 */
    uint32_t error_by_kind[5];      /* 各反馈类别次数（下标 = fb_kind_t：1=net-offline
                                     * 2=cloud-error 3=timeout 4=tts-fail；0 不用） */
    uint32_t last_error_prompt_ms;  /* 最近一次错误提示音实际时长（ms） */
    int32_t last_error_code;        /* 最近一次原始错误码（云端 error code / vc_tts_fail_t） */
    uint8_t last_error_kind;        /* 最近一次反馈类别（fb_kind_t 数值） */
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

/* 连续对话续听窗口的运行时开关（默认取 CONFIG_VS_FOLLOWUP_ENABLE；T3.7）
 * 关闭时立即退出 FOLLOW_UP；用于测试/调试保持单轮行为。CONFIG 关闭时返回 NOT_SUPPORTED。 */
esp_err_t vs_set_followup_enabled(bool enabled);
bool vs_followup_enabled(void);

/* 读统计快照 / 清零统计（任意任务上下文可调用） */
esp_err_t vs_stats_read(vs_stats_t *out);
esp_err_t vs_stats_reset(void);

#ifdef __cplusplus
}
#endif
