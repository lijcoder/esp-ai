# music_session（L4 音乐编排，T4.3）

## 作用

音乐播放的**状态机与抢占编排**：起播/暂停/继续/停止/切歌、`music_request` 兜底、
与语音会话的抢占（语音活跃时暂停让路、**回 IDLE 保持暂停**，T4.4 pause 模式）、
`music_status` 周期上报与 EOF 探测。
协议解析归 L3 `music_cloud`，播放引擎归 L2 `audio_player`；本组件**不建任务**，只用两个 `esp_timer`。

## 对外接口

| 接口 | 说明 |
|---|---|
| `ms_init()` | 注册 `music_cloud` 回调、订阅 `APP_EVT_VOICE_STATE_CHANGED`、建两个 `esp_timer`；不建任务 |
| `ms_play(url)` | 直接起播（console/探针，不走协议）；http(s) 且长度 < `MS_URL_MAX` |
| `ms_pause(fade_ms)` | 用户暂停（latch，pause_reason=1）；会话结束不自动恢复 |
| `ms_resume()` | 用户继续（清 latch）；失败按 Kconfig 兜底一次 `music_request` |
| `ms_stop()` | 停止（播放/暂停都可用；暂停中停止丢弃 ring） |
| `ms_request(action,index)` | 上行 `music_request`（目前只支持 `MC_REQUEST_ACTION_PLAY`；index 0/-1=服务端自选） |
| `ms_get_state()` / `ms_state_name()` | 当前状态 / 状态名（`idle/playing/paused/stopped/error`） |
| `ms_info_read()` | 快照：state/mid/have_mid/progress/duration/url/title/pause_reason/paused |
| `ms_stats_read()/reset()` | 迁移/曲目/起播失败/暂停/继续/抢占/停止/EOF/兜底等统计 |

纯逻辑（auto-pause、pause_reason 合并、mid 过滤、control 应用、兜底判定）
在 `include/music_policy.h`，无 IDF 依赖，由 `test/host/music_policy_test.c` 覆盖。

## 状态机

| from → to | 触发 |
|---|---|
| IDLE → PLAYING | `music_track` 起播成功 / `ms_play()` |
| PLAYING → PLAYING | 新 `music_track`（切歌：先 stop 再 play，`MS_PLAY_RETRY_MS × MS_PLAY_MAX_RETRIES`） |
| PLAYING → PAUSED | 用户 pause / 语音抢占（pause_reason=1/2） |
| PAUSED → PLAYING | 用户 resume（显式“继续”）；T4.4 起语音回 IDLE **不再**自动恢复 |
| PLAYING/PAUSED → STOPPED | `music_control{stop}` / `ms_stop()` / EOF（非暂停态） |
| PLAYING → ERROR | HTTP/解码失败、起播重试耗尽；同时投 `APP_EVT_MUSIC_ERROR` |
| PAUSED → STOPPED | 暂停中 stop（丢弃 ring） |

每次迁移：INFO 日志（mid、停留时长、reason）+ 立即 `music_status` + 投
`APP_EVT_MUSIC_STATE_CHANGED`（payload：`app_evt_music_state_t`，20B，见 `event_bus.h`）。

## 与语音抢占（契约 §4）

- 订阅 `APP_EVT_VOICE_STATE_CHANGED`（同层经 event_bus，不改 `voice_session`）；
- 语音进入 LISTENING/THINKING/SPEAKING/FOLLOW_UP/ERROR → 音乐 PLAYING 且非用户暂停时 auto-pause；
- 语音回 IDLE **保持暂停**（T4.4 pause 模式，用户 2026-11-11 定稿）：不自动续播，
  等用户说“继续”（`music_control{resume}`）或点新歌；
- 云端 `music_control{pause}` = 用户暂停 latch（reason=1）；`{resume}` 清 latch（语音仍活跃也允许，
  PUMP 会排在 TTS 作业之后）；
- 不做 duck/叠加（T4.4 定稿走暂停保留，duck 留作后续体验模式），不关麦。

## 配置

`MS_PAUSE_FADE_MS`（100）、`MS_AUTO_PAUSE_ON_VOICE`（y）、
`MS_RESUME_FALLBACK_REQUEST`（y）、`MS_STATUS_PERIOD_MS`（5000）、`MS_PLAY_RETRY_MS`（120）、
`MS_PLAY_MAX_RETRIES`（15）、`MS_EOF_GUARD_MS`（1500，T4.2 保护窗口迁移）。

## 依赖

`music_cloud`（L3，下行事件/上行 status/request）、`audio_player`（L2）、`event_bus`（L1）、`esp_timer`。
只能上层依赖下层；`music_cloud` 不 include 本组件，L3 → L4 只经注册回调。

## 约束 / 已知限制

- 不 malloc、不申请 PSRAM、不建任务；静态 DIRAM 增量目标 ≤ +1.5KB；
- 所有播放器调用只在 `ms_play` 定时器上下文串行；临界区内不打印、不调播放器；
- 暂停保留 CH_MUSIC ring 与 wr/rd，恢复从暂停点续播（不从头发）；
- 长暂停（服务端 idle 超时/TCP 断）后 resume 可能拿不到数据：EOF 探测转
  STOPPED/ERROR 并兜底一次 `music_request`（T4.5 用 Range 做真续播）；
- 新 `music_track` 视为新的播放请求（即使处于用户暂停也会起播）；如需严格 latch 需产品确认。
