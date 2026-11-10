/*
 * music_cloud —— 音乐云协议（L3 能力层，T4.2）的唯一对外头文件。
 *
 * 职责边界（ARCHITECTURE §4.1、§6.3、§8.3；PROTOCOL.md §3.2/§3.3/§7）：
 *   - 下行 `music_track`（曲目 URL）→ `audio_player_music_play_url()`（T4.1 的 source_http）；
 *   - 下行 `music_control`（stop）→ `audio_player_music_stop_url()`；
 *     pause/resume 的**执行**归 T4.3（播放器还没有暂停能力），本组件只解析/计数/告警；
 *   - 上行 `music_status`（state/progress_ms）：播放中每 CONFIG_MC_STATUS_PERIOD_MS 一次，
 *     状态变化时立即一次；
 *   - 音乐事件用**连接内自增的 `mid`**（P-3 定稿），不参与语音 `sid` 的轮次窗口过滤。
 *
 * 不做什么：
 *   - 不解析/拼接 URL、不搜曲、不做 NLU（音乐意图归云端；URL 只出现在 `music_track`）；
 *   - 不含播放列表：下一首/上一首由服务端直接下发**新的 `music_track`**；
 *   - 不含 `music_session`（播放/暂停/切歌状态机、与语音抢占）——归 T4.3；
 *   - `music_request`（端侧主动点歌）也归 T4.3。
 *
 * 线程模型：不建任务。
 *   - 下行解析在 `ws_client` 任务上下文（transport_ws 的附加订阅槽），禁止阻塞；
 *   - 起播/重试与 5s 上报在两个 `esp_timer` 回调里；播放器调用只在定时器上下文串行发生
 *     （切歌 = 先 stop 再 start，停止是异步的，所以要重试而不是立刻 play）。
 */
#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "esp_err.h"
#include "sdkconfig.h"

#ifdef __cplusplus
extern "C" {
#endif

/* URL 上限与 audio_player 的 music_http 任务保持一致（超长直接拒绝，不截断）。
 * 关掉 CONFIG_AUDIO_PLAYER_ENABLE_MP3 时该符号不生成，这里给同值兜底，保证组件可单独编译。 */
#ifndef CONFIG_AUDIO_PLAYER_MUSIC_URL_MAX
#define CONFIG_AUDIO_PLAYER_MUSIC_URL_MAX 320
#endif
#define MC_URL_MAX CONFIG_AUDIO_PLAYER_MUSIC_URL_MAX
/* 曲目标题只用于日志/console 展示，超长截断 */
#define MC_TITLE_MAX 48

/* 音乐通道状态。PAUSED 预留给 T4.3（本任务不会进入该状态）。 */
typedef enum {
    MC_STATE_IDLE = 0,  /* 本次上电还没播过音乐 */
    MC_STATE_PLAYING,   /* 正在拉流/播放 */
    MC_STATE_PAUSED,    /* 预留（T4.3：暂停执行） */
    MC_STATE_STOPPED,   /* 收到 stop，或流自然结束 */
    MC_STATE_ERROR,     /* 拉流/解码失败（HTTP 错误、URL 非法等） */
} mc_state_t;

/* 只读快照（`diag` 的 `mc status` 用；URL 供人工核对，不要长期持有） */
typedef struct {
    mc_state_t state;
    bool have_mid;          /* 当前连接内是否已接受过音乐事件 */
    uint16_t mid;           /* 当前曲目 mid */
    uint32_t progress_ms;   /* 已播时长（来自播放器 played_frames） */
    int32_t duration_ms;    /* 0 = 直播流/时长未知（不能等 EOF 收尾） */
    char url[MC_URL_MAX];   /* 当前曲目 URL（可能为空） */
    char title[MC_TITLE_MAX];
} mc_info_t;

/* 观测统计（累计值，仅供 diag / 测试脚本断言） */
typedef struct {
    uint32_t rx_track;        /* 接受的 music_track 数 */
    uint32_t rx_control;      /* 接受的 music_control 数 */
    uint32_t rx_parse_errors; /* 下行 JSON / 字段非法 */
    uint32_t rx_dropped_mid;  /* mid 过期（旧连接/旧曲目）而丢弃 */
    uint32_t rx_dropped_dup;  /* 重复的 music_track（同 mid） */
    uint32_t rx_dropped_bad;  /* URL 非法/超长、codec 非 mp3 等 */
    uint32_t play_errors;     /* play_url 重试后仍失败 */
    uint32_t ignored_actions; /* pause/resume（T4.3 才执行） */
    uint32_t status_sent;     /* music_status 入队成功 */
    uint32_t status_failed;   /* music_status 未发（WS 未 READY/队列满） */
    uint32_t eofs;            /* 播放结束（自然 EOF 或流中断）次数 */
} mc_stats_t;

/**
 * @brief 初始化：订阅 WS 附加接收槽 + 建两个 esp_timer（不建任务）
 *
 * @note 只应在 app_main 中调用一次，且必须在 ws_init()/audio_player_init() 之后；
 *       重复调用返回 ESP_ERR_INVALID_STATE。
 */
esp_err_t mc_init(void);

/* 当前状态（任意上下文可调用） */
mc_state_t mc_get_state(void);
const char *mc_state_name(mc_state_t state);

/* 当前曲目 mid（没有曲目时返回 0）；主要用于日志/断言 */
uint16_t mc_current_mid(void);

/* 读状态快照（任意上下文可调用） */
esp_err_t mc_info_read(mc_info_t *out);

/* 立即上报一次 music_status（console `mc report` 探针用；不影响周期上报） */
esp_err_t mc_report_status(void);

/* 读统计快照 / 清零（不影响进行中的播放） */
esp_err_t mc_stats_read(mc_stats_t *out);
esp_err_t mc_stats_reset(void);

#ifdef __cplusplus
}
#endif
