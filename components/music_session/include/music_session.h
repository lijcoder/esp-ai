/*
 * music_session —— 音乐播放编排（L4 业务层，T4.3）的唯一对外头文件。
 *
 * 职责（ARCHITECTURE §4.1/§7/§8.3/§8.4；build/t43_contract.md §2.2/§3/§4/§5）：
 *   - 音乐状态机：IDLE/PLAYING/PAUSED/STOPPED/ERROR，每次迁移立即上报 `music_status` +
 *     投递 `APP_EVT_MUSIC_STATE_CHANGED`（ERROR 时同时投 `APP_EVT_MUSIC_ERROR`）；
 *   - 起播 / 切歌（先 stop 再 play，CONFIG_MS_PLAY_RETRY_MS × CONFIG_MS_PLAY_MAX_RETRIES 重试）、
 *     暂停（用户 latch / 语音抢占）、继续（含 resume 失败兜底 `music_request`）、停止；
 *   - 与语音抢占：订阅 `APP_EVT_VOICE_STATE_CHANGED`，语音进入活跃态 auto-pause（暂停让路），
 *     **语音回 IDLE 保持暂停**（T4.4 pause 模式：不自动续播，等用户明确说"继续"）；
 *     用户暂停不被覆盖；
 *   - 周期上报 + EOF 探测（一个 esp_timer）。
 *
 * 不做什么：
 *   - 不解析协议、不拼 JSON/URL（归 L3 `music_cloud`）；
 *   - 不建任务、不 malloc/PSRAM；只用两个 esp_timer（起播/重试 + 周期上报/EOF 探测）；
 *   - 不关麦、不做 duck/叠加（T4.4 定稿走"暂停保留"，duck 留作后续体验模式）。
 *
 * 线程模型：
 *   - 所有播放器调用与状态迁移只在 esp_timer（`ms_play` / `ms_status`）上下文发生；
 *   - `music_cloud` 回调在 `ws_client` 上下文，只拷贝/调度，不阻塞；
 *   - 语音事件在 `evt` 上下文，只做判定 + 调度；
 *   - 对外 API 可在任意上下文调用（内部只置命令/读快照）。
 */
#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "esp_err.h"
#include "music_cloud.h"

#ifdef __cplusplus
extern "C" {
#endif

/* URL/标题上限沿用协议层（同一份定长缓冲） */
#define MS_URL_MAX   MC_URL_MAX
#define MS_TITLE_MAX MC_TITLE_MAX

/* 暂停原因（ms_info_t.pause_reason）：与 music_policy.h 一致 */
#define MS_PAUSE_REASON_NONE  0
#define MS_PAUSE_REASON_USER  1
#define MS_PAUSE_REASON_VOICE 2

/* 只读快照（`diag` 的 `ms status` 用；URL 供人工核对，不要长期持有） */
typedef struct {
    mc_state_t state;
    bool paused;            /* state == MC_STATE_PAUSED（冗余，便于断言） */
    bool have_mid;          /* 当前连接内是否已接受过音乐事件 */
    uint8_t pause_reason;   /* 0=无、1=用户、2=语音抢占 */
    uint16_t mid;           /* 当前曲目 mid（无曲目为 0） */
    uint32_t progress_ms;   /* 已播毫秒（暂停时冻结） */
    int32_t duration_ms;    /* 0 = 直播流/时长未知 */
    char url[MS_URL_MAX];   /* 当前/最近曲目 URL（可能为空） */
    char title[MS_TITLE_MAX];
} ms_info_t;

/* 观测统计（累计值，仅供 diag / 测试脚本断言；`ms reset` 清零） */
typedef struct {
    uint32_t transitions;       /* 状态迁移次数（含 PLAYING→PLAYING 切歌） */
    uint32_t tracks;            /* 接受并尝试起播的 music_track 数 */
    uint32_t play_errors;       /* 起播重试耗尽 / 起播失败 */
    uint32_t pauses;            /* 成功暂停次数（用户 + 语音） */
    uint32_t resumes;           /* 成功继续次数 */
    uint32_t steals;            /* 其中语音抢占 auto-pause 次数 */
    uint32_t stops;             /* 停止次数 */
    uint32_t eofs;              /* 自然结束 / 流中断次数 */
    uint32_t controls;          /* 应用成功的 music_control 数 */
    uint32_t dropped;           /* 当前状态不接受而丢弃的控制/请求 */
    uint32_t resume_failures;   /* 继续失败次数 */
    uint32_t fallback_requests; /* 兜底发出的 music_request 数 */
} ms_stats_t;

/**
 * @brief 初始化：注册 music_cloud 回调 + 订阅 APP_EVT_VOICE_STATE_CHANGED + 建两个 esp_timer
 *
 * @note 只应在 app_main 中调用一次，且必须在 mc_init()（ws_init 之后）与 app_evt_init() 之后；
 *       重复调用返回 ESP_ERR_INVALID_STATE。不建任务。
 */
esp_err_t ms_init(void);

/* 直接起播（console/探针；不走协议）：URL 只接受 http(s) 且长度 < MS_URL_MAX。 */
esp_err_t ms_play(const char *url);

/* 用户暂停（latch，会话结束不自动恢复）；fade_ms=0 用 CONFIG_MS_PAUSE_FADE_MS。 */
esp_err_t ms_pause(uint32_t fade_ms);

/* 用户继续（清 latch 并续播；resume 失败按 Kconfig 兜底一次 music_request）。 */
esp_err_t ms_resume(void);

/* 停止（播放/暂停都可用；暂停中停止会丢弃 ring）。 */
esp_err_t ms_stop(void);

/* 上行 music_request（action 目前只支持 MC_REQUEST_ACTION_PLAY；index 0/-1=服务端自选）。 */
esp_err_t ms_request(uint8_t action, int32_t index);

/* 当前状态 / 状态名（任意上下文可调用） */
mc_state_t ms_get_state(void);
const char *ms_state_name(mc_state_t state);

/* 读状态快照（任意上下文可调用；progress 来自播放器 played_frames） */
esp_err_t ms_info_read(ms_info_t *out);

/* 读统计快照 / 清零（不影响进行中的播放） */
esp_err_t ms_stats_read(ms_stats_t *out);
esp_err_t ms_stats_reset(void);

#ifdef __cplusplus
}
#endif
