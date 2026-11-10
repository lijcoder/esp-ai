# music_cloud —— 音乐云协议（L3，T4.2）

**作用**：把云端下发的音乐事件翻译成端侧播放动作，并把播放状态按 5s 周期上报，
让"点歌 → 拉流播放 → 状态回传"这条链路在端侧闭环。

## 职责边界

| 方向 | 事件 | 动作 |
|---|---|---|
| 下行 | `music_track{mid,url,codec,duration_ms,title}` | `audio_player_music_play_url(url)`（T4.1 `source_http`：HTTP(S) → 独立 minimp3 → 48k → `CH_MUSIC`）；**切歌 = 先 stop 旧的再起新的** |
| 下行 | `music_control{mid,action}` | `stop` → `audio_player_music_stop_url()`；`pause`/`resume` → **只计数 + WARN**（执行归 T4.3，播放器还没有暂停能力） |
| 上行 | `music_status{state,mid,progress_ms}` | 播放中每 `CONFIG_MC_STATUS_PERIOD_MS`（默认 5s）一次；状态变化（playing/stopped/error）立即一次 |
| 事件 | `APP_EVT_MUSIC_STATE_CHANGED` / `APP_EVT_MUSIC_ERROR` | 无 payload 广播（payload 契约待 T4.3 定稿） |

**不做**：`music_request`（端侧主动点歌）、暂停/继续/切歌状态机、与语音抢占、ducking ——
分别归 T4.3/T4.4。端侧也不做 NLU、不从 `llm_delta` 文本里抠 URL（URL 只认 `music_track`）。

## `mid` 规则（P-3 定稿，2026-11-10）

音乐事件用**连接内自增**的 `mid`，与语音 `sid` 无关：音乐可以跨语音轮次播放，也可以在没有
会话时开始，用 `sid` 过滤会被"轮次结束/迟到帧丢弃"误杀。

- `music_track`：只接受比当前**更新**的 `mid`；同 `mid` 视为重复（不重复起播），旧 `mid` 丢弃；
- `music_control`：允许 `mid == 当前曲目`（暂停/停止本来就指向正在播的这首），只丢弃更旧的；
- WS 断链后（周期定时器发现 `ws_is_ready()==false`）清掉比较基准：新连接内服务端的 `mid`
  从头开始；**音乐本身可以继续播**（本地拉流，不依赖 WS）。

## 对外接口

| 接口 | 说明 |
|---|---|
| `mc_init()` | 订阅 WS 附加接收槽（不抢 `voice_cloud` 主槽）+ 建两个 `esp_timer`；必须在 `ws_init`/`audio_player_init` 之后 |
| `mc_get_state()` / `mc_state_name()` | 状态（IDLE/PLAYING/PAUSED/STOPPED/ERROR；PAUSED 预留 T4.3） |
| `mc_current_mid()` | 当前曲目 mid（0 = 无） |
| `mc_info_read()` | 状态快照（state/mid/进度/时长/URL/标题），`diag` 用 |
| `mc_report_status()` | 立即上报一次 `music_status`（console 探针） |
| `mc_stats_read()` / `mc_stats_reset()` | 统计（track/control/丢弃/起播失败/上报成败/EOF） |

## 线程模型（不建任务）

```
ws_client 任务 ──transport_ws 附加订阅槽──> 解析 music_track/music_control
                                              │ 更新状态（临界区）+ 记录待播 URL
                                              ▼
esp_timer 任务 ── mc_play（one-shot）──> audio_player_music_play_url()（失败按 120ms 重试）
esp_timer 任务 ── mc_report（periodic 5s）──> ws_send_text(music_status) + 播放结束探测
```

- 下行回调里**禁止阻塞**：只有定长 JSON 扫描（零分配）与临界区内的 memcpy/赋值；
- 播放器调用**只在 `esp_timer` 回调里串行发生**：切歌要先 stop 再 play，而 stop 是异步的
  （`music_http` 任务等 HTTP 读超时/淡出后退出），所以在定时器里重试，避免 ws 回调与定时器
  并发调播放器；
- 周期定时器同时负责"播放结束探测"：`audio_player_music_active()` 变 false 即判定 EOF/失败，
  起播后 1.5s 内不判（任务创建到 `music_start()` 有延迟）。

## 依赖

`transport_ws`（WS 收发/附加订阅）、`audio_player`（音乐通道 API + 播放统计）、
`event_bus`（语义事件）、`esp_timer`；无第三方依赖。

## 配置（Kconfig）

| 项 | 默认 | 说明 |
|---|---|---|
| `CONFIG_MC_STATUS_PERIOD_MS` | 5000 | `music_status` 周期；调小增加 WS 上行与发送队列占用 |
| `CONFIG_MC_PLAY_RETRY_MS` | 120 | 切歌/起播失败的重试间隔 |
| `CONFIG_MC_PLAY_MAX_RETRIES` | 15 | 最大重试次数（120ms × 15 ≈ 1.8s，覆盖旧任务退出窗口） |

URL 上限复用 `CONFIG_AUDIO_PLAYER_MUSIC_URL_MAX`（320B），超长/非 `http(s)`/`codec!=mp3`
一律拒绝（不截断、不猜）。

## 内存与实时约束（CONVENTIONS §3/§4）

- 不建任务、不 malloc：定长缓冲（URL 320B、标题 48B、报文 96B）都在栈上/静态；
- 不发 PSRAM 申请；状态用 `portMUX` 临界区保护，临界区内不打印、不调播放器；
- 音频实时任务（`afe_feed`/`afe_fetch`/`player`）完全不碰本组件。

## 调试

```
mc status                 # state/mid/进度/URL + 统计
mc report                 # 立即上报一次 music_status
mc reset                  # 清统计
ws send {"t":"music_track","mid":1,"url":"http://192.168.1.5:9000/media/demo.mp3","codec":"mp3","duration_ms":6300,"title":"晴天"}
ws send {"t":"music_control","mid":1,"action":"stop"}
log MC debug              # 打印完整 URL 与重试细节
```

真机链路（mock `--music`）见 `tools/README.md`；当前无板，验收登记在 `docs/PENDING.md`。

## 后续

- **T4.3 `music_session`**：暂停/继续（`source_http` 需要暂停能力）、切歌状态机、`music_request`；
- **T4.4**：音乐 ducking / 播放中唤醒的恢复；
- **T4.5**：预缓冲/断流重试/错误恢复（本组件的起播重试只是切歌过渡，不替代断流重连）。
