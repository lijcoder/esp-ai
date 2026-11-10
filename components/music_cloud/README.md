# music_cloud —— 音乐云协议（L3，纯协议）

**作用**：解析云端下发的 `music_track` / `music_control`、做 `mid` 过滤、把结果通过注册回调交给
L4 `music_session`，并负责上行 `music_status` / `music_request` 的编码与发送。
**T4.3 起不再调用播放器、不再建定时器**。

## 职责边界

| 方向 | 事件 | 本组件动作 |
|---|---|---|
| 下行 | `music_track{mid,url,codec,duration_ms,title}` | 零分配解析 + `mid` 过滤 → `mc_evt_t{kind=TRACK}` → 注册回调 |
| 下行 | `music_control{mid,action}` | 零分配解析 + `mid` 过滤 → `mc_evt_t{kind=CONTROL}` → 注册回调 |
| 上行 | `music_status{state,mid,progress_ms}` | `mc_report_status(state, progress_ms)`：编码 + `ws_send_text()`；周期由 session 驱动 |
| 上行 | `music_request{action,id,index}` | `mc_send_request(action,id,index)`：编码 + `ws_send_text()` |
| 事件 | `APP_EVT_NET_DOWN` | 清 `mid` 比较基准（音乐本身继续播） |

**不做**：起播/切歌重试、暂停/继续/停止执行、EOF 探测、周期定时器、与语音抢占 —— 全部归
T4.3 `music_session`。端侧也不做 NLU、不从 `llm_delta` 文本里抠 URL（URL 只认 `music_track`）。

## `mid` 规则（P-3 定稿，2026-11-10）

音乐事件用**连接内自增**的 `mid`，与语音 `sid` 无关：音乐可以跨语音轮次播放，也可以在没有
会话时开始，用 `sid` 过滤会被"轮次结束/迟到帧丢弃"误杀。

- `music_track`：只接受比当前**更新**的 `mid`；同 `mid` 视为重复（不重复起播），旧 `mid` 丢弃；
- `music_control`：允许 `mid == 当前曲目`（暂停/停止本来就指向正在播的这首），只丢弃更旧的；
- 断链后清比较基准：订阅 `APP_EVT_NET_DOWN`；另外每次处理音乐事件前比对
  `ws_status.connect_count`，兜住"WiFi 未断但 WS 重连"（新连接 `mid` 从头发，旧基准会把
  新曲目全部误判过期）。音乐本身可以继续播（本地拉流，不依赖 WS）。

## 对外接口

| 接口 | 说明 |
|---|---|
| `mc_init()` | 订阅 WS 附加接收槽 + `APP_EVT_NET_DOWN`；不建任务/定时器；必须在 `ws_init()` 之后 |
| `mc_register_cb(cb, arg)` | 注册/覆盖下行事件回调（单槽；`ws_client` 上下文，禁止阻塞；`cb=NULL` 清除） |
| `mc_report_status(state, progress_ms)` | 编码并发送一次 `music_status`；ws 未 READY 只计数 |
| `mc_send_request(action, id, index)` | 编码并发送 `music_request`（`action=MC_REQUEST_ACTION_PLAY`；index 0/-1=服务端自选） |
| `mc_current_mid()` / `mc_have_mid()` | 当前曲目 mid / 当前连接内基准是否有效 |
| `mc_action_name()` / `mc_state_name()` | 动作名（pause/resume/stop）/ 状态名（idle/playing/paused/stopped/error） |
| `mc_stats_read()` / `mc_stats_reset()` | 协议统计（见下） |

统计字段：`rx_track / rx_control / rx_parse_errors / rx_dropped_mid / rx_dropped_dup /
rx_dropped_bad / status_sent / status_failed / request_sent / request_failed`。

## 线程模型（不建任务、不建定时器）

```
ws_client 任务 ──transport_ws 附加订阅槽──> 解析 + mid 过滤 ──回调──> music_session (L4)
任意上下文 ──mc_report_status / mc_send_request──> ws_send_text()（非阻塞入队）
evt 任务 ──APP_EVT_NET_DOWN──> 清 mid 基准
```

- 下行回调里**禁止阻塞**：只有定长 JSON 扫描（零分配）、临界区内的 `mid` 比较与回调派发；
- 临界区内只做 memcpy/赋值：**不打印、不回调、不调播放器**；
- 依赖方向只允许 L4 → L3；L3 不 include L4，事件交给 L4 只经注册回调。

## 依赖

`transport_ws`（WS 收发/附加订阅/状态快照）、`event_bus`（NET_DOWN）；无第三方依赖、无 `audio_player`。

## 配置

本组件没有 Kconfig 参数。上报周期/起播重试/暂停淡出等在 `music_session` 的 `MS_*`
（`CONFIG_MC_*` 已全部移除）。URL 上限复用 `CONFIG_AUDIO_PLAYER_MUSIC_URL_MAX`（320B），
标题上限 `MC_TITLE_MAX=48`；超长/非 `http(s)`/`codec!=mp3` 一律拒绝（不截断、不猜）。

## 内存与实时约束（CONVENTIONS §3/§4）

- 不建任务、不 malloc、不申请 PSRAM：下行解析缓冲在 `ws_client` 任务栈上，报文 96B 在栈上；
- 状态用 `portMUX` 临界区保护，临界区内不打印、不调播放器；
- 音频实时任务（`afe_feed`/`afe_fetch`/`player`）完全不碰本组件。

## 调试

```
mc status                 # mid/have_mid + 协议统计（状态/进度看 ms status）
mc report                 # 从 music_session 取当前状态/进度，立即上报一次 music_status
mc reset                  # 清协议统计
ws send {"t":"music_track","mid":1,"url":"http://192.168.1.5:9000/media/demo.mp3","codec":"mp3","duration_ms":6300,"title":"晴天"}
ws send {"t":"music_control","mid":1,"action":"pause"}
log MC debug              # 打印完整 URL / music_status 细节
```

真机链路（mock `--music`）见 `tools/README.md`；当前无板，验收登记在 `docs/PENDING.md`。

## 后续

- T4.4：音乐 ducking / 播放中唤醒的叠加（协议层不变）；
- T4.5：断流重连/seek/Range 的错误恢复（协议字段可能新增，届时同步 PROTOCOL/ARCH）。
