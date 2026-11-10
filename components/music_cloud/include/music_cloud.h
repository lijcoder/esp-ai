/*
 * music_cloud —— 音乐云协议（L3 能力层）的唯一对外头文件。
 *
 * 职责边界（T4.3 起为**纯协议**；ARCHITECTURE §4.1/§6.3/§8.3，PROTOCOL.md §3.1–3.3/§7）：
 *   - 下行 `music_track` / `music_control` 的**零分配解析**与 `mid` 过滤（P-3 语义不变）；
 *   - 把解析结果通过 `mc_register_cb()` 交给 L4 `music_session`（回调在 `ws_client` 上下文，
 *     禁止阻塞；`music_cloud` 不 include L4，依赖方向 L3 → L4 只经注册回调）；
 *   - 上行 `music_status` 编码 + 发送（周期由 `music_session` 驱动）；
 *   - 上行 `music_request` 编码 + 发送；
 *   - 协议统计；断链（`APP_EVT_NET_DOWN` / WS 重连）后清 `mid` 比较基准。
 *
 * 不做什么：
 *   - **不调用任何播放器接口**（不依赖 `audio_player`），不建定时器、不建任务；
 *   - 不驱动状态机、不做起播/切歌重试/EOF 探测/暂停继续执行（全部归 `music_session`）；
 *   - 不解析/拼接 URL、不搜曲、不做 NLU（音乐意图归云端；URL 只出现在 `music_track`）；
 *   - 不含播放列表：下一首/上一首由服务端直接下发**新的 `music_track`**。
 *
 * 线程模型：不建任务、不建定时器。
 *   - 下行解析在 `ws_client` 任务上下文（transport_ws 的附加订阅槽），只做定长解析、
 *     临界区内的 `mid` 比较与回调派发，禁止阻塞/网络/Flash；
 *   - 上行发送可由任意上下文调用（`ws_send_text()` 非阻塞入队）；
 *   - 状态用 `s_mux` 临界区保护；临界区内只做 memcpy/赋值，不打印、不回调。
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

/* 音乐通道状态（枚举值不变；PAUSED 自 T4.3 起由 music_session 使用） */
typedef enum {
    MC_STATE_IDLE = 0,  /* 本次上电还没播过音乐 */
    MC_STATE_PLAYING,   /* 正在拉流/播放 */
    MC_STATE_PAUSED,    /* 暂停（CH_MUSIC ring 保留，进度冻结） */
    MC_STATE_STOPPED,   /* 收到 stop，或流自然结束 */
    MC_STATE_ERROR,     /* 拉流/解码失败（HTTP 错误、起播重试耗尽等） */
} mc_state_t;

/* music_control 的控制动作（PROTOCOL §3.2） */
typedef enum {
    MC_ACTION_PAUSE = 0,
    MC_ACTION_RESUME,
    MC_ACTION_STOP,
} mc_action_t;

/* music_request 的 action（PROTOCOL §3.1：目前只有 play）。
 * 与 `mc_action_t` 是两个域（pause=0 不能当请求动作），所以单独定义常量。 */
#define MC_REQUEST_ACTION_PLAY 0

typedef enum {
    MC_EVT_TRACK = 0, /* 下行 music_track（新的播放/切歌请求） */
    MC_EVT_CONTROL,   /* 下行 music_control（pause/resume/stop） */
} mc_evt_kind_t;

/* 交给 L4 的解析结果。只在本回调内有效；需要保留请自行拷贝。 */
typedef struct {
    mc_evt_kind_t kind;
    uint8_t action;      /* kind=MC_EVT_CONTROL 时有效（mc_action_t） */
    uint16_t mid;
    int32_t duration_ms; /* kind=TRACK；0=直播流/时长未知 */
    char url[MC_URL_MAX];   /* kind=TRACK */
    char title[MC_TITLE_MAX];
} mc_evt_t;

/* 回调在 `ws_client` 任务上下文执行，禁止阻塞；单槽覆盖（传 NULL 清除）。 */
typedef void (*mc_evt_cb_t)(void *arg, const mc_evt_t *evt);

/* 协议统计（累计值，仅供 diag / 测试脚本断言） */
typedef struct {
    uint32_t rx_track;        /* 接受的 music_track 数 */
    uint32_t rx_control;      /* 接受的 music_control 数 */
    uint32_t rx_parse_errors; /* 下行 JSON / 字段非法 */
    uint32_t rx_dropped_mid;  /* mid 过期（旧连接/旧曲目）而丢弃 */
    uint32_t rx_dropped_dup;  /* 重复的 music_track（同 mid） */
    uint32_t rx_dropped_bad;  /* URL 非法/超长、codec 非 mp3 等 */
    uint32_t status_sent;     /* music_status 入队成功 */
    uint32_t status_failed;   /* music_status 未发（WS 未 READY/队列满） */
    uint32_t request_sent;    /* music_request 入队成功 */
    uint32_t request_failed;  /* music_request 未发（WS 未 READY/队列满/参数非法） */
} mc_stats_t;

/**
 * @brief 初始化：订阅 WS 附加接收槽 + 订阅 APP_EVT_NET_DOWN；不建任务/定时器
 *
 * @note 只应在 app_main 中调用一次，且必须在 ws_init()/app_evt_init() 之后；
 *       重复调用返回 ESP_ERR_INVALID_STATE。
 */
esp_err_t mc_init(void);

/**
 * @brief 注册/覆盖下行音乐事件回调（单槽）
 *
 * @note 回调在 `ws_client` 任务上下文执行，**禁止阻塞**；传 cb=NULL 清除。
 *       必须在 mc_init() 之后调用。
 */
esp_err_t mc_register_cb(mc_evt_cb_t cb, void *arg);

/**
 * @brief 编码并发送一次 music_status（状态与进度由 music_session 提供）
 *
 * @param state       当前音乐状态（枚举值不变）
 * @param progress_ms 已播毫秒（暂停态应传冻结值）
 * @return ESP_OK / ESP_ERR_INVALID_STATE（ws 未 READY）/ ESP_ERR_INVALID_ARG / 发送错误
 *
 * @note ws 未 READY 时只累加 status_failed，不打印 WARN（周期上报会刷屏）。
 */
esp_err_t mc_report_status(mc_state_t state, uint32_t progress_ms);

/**
 * @brief 编码并发送 music_request（端侧主动点歌）
 *
 * @param action 目前只支持 MC_REQUEST_ACTION_PLAY（0）
 * @param id     端侧自增请求序号（仅用于日志关联）
 * @param index  曲库索引；0/-1 = 服务端自选
 */
esp_err_t mc_send_request(uint8_t action, uint16_t id, int32_t index);

/* 当前曲目 mid（没有曲目时返回 0）；主要用于日志/断言 */
uint16_t mc_current_mid(void);
/* 当前连接内是否已接受过音乐事件（mid 比较基准有效） */
bool mc_have_mid(void);

/* 控制动作名（"pause"/"resume"/"stop"；非法值返回 "?"） */
const char *mc_action_name(uint8_t action);
const char *mc_state_name(mc_state_t state);

/* 读统计快照 / 清零（不影响进行中的播放） */
esp_err_t mc_stats_read(mc_stats_t *out);
esp_err_t mc_stats_reset(void);

#ifdef __cplusplus
}
#endif
