# feedback —— 异常反馈路由（L3，T3.6）

**作用**：把"异常"抽象成**语义事件 + 可插拔反馈通道**。当前唯一的通道是**语音提示音**；
以后要加屏幕/灯光时，新组件订阅 `APP_EVT_FEEDBACK` 事件即可，**不需要改本组件或
voice_session**——这就是"语音只是一种反馈方式"的扩展点。

## 职责边界

- 输入：`fb_notify(kind, code, sid)`（由 voice_session 在异常路径调用，L4 → L3 直接调用）；
- 输出：
  1. `APP_EVT_FEEDBACK` 事件（payload `app_evt_feedback_t`，kind/code/sid）——语义广播，
     未来屏幕/灯光通道订阅它做展示；
  2. 语音通道：`assets` 提示音 → `audio_player_play_pcm()`（`CH_PROMPT`）+ R1 门控
     `audio_fe_feed_hold(AUDIO_FE_HOLD_PROMPT)`；播完/失败回调 `fb_done_cb`。
- 不做：会话状态机（归 `voice_session`）、协议（归 `voice_cloud`）、提示音资源制作
  （归 `assets` / T3.5 的 assets 分区）。

## 反馈类别（`feedback_policy.h`，数值写入事件 payload，只允许追加）

| kind | 名称 | 触发 |
|---|---|---|
| 1 | `net-offline` | 唤醒/起轮时云端未就绪、会话中 `NET_DOWN`、WS 断链 |
| 2 | `cloud-error` | 云端 `error` 事件（1/1001/2001/2002/9001/未知） |
| 3 | `timeout` | THINKING 等响应超时（`VC_RESPONSE_TIMEOUT_MS`，默认 10s） |
| 4 | `tts-fail` | 云端 TTS 错误 3001/3002，或设备侧无法播放 TTS（`VC_EVT_TTS_FAIL`） |

`fb_kind_from_cloud_code()` 是纯函数，放在无 IDF 依赖的 `feedback_policy.h` 里，供
`test/host` 直接编译单测。

## 对外接口

| 接口 | 说明 |
|---|---|
| `fb_init()` | 订阅播放完成回调（**不建任务/定时器**）；必须在 `audio_player_init`/`app_evt_init` 之后 |
| `fb_notify(kind, code, sid)` | 请求一次反馈：发布语义事件 + 启动语音通道；忙则丢弃（返回 `ESP_ERR_INVALID_STATE`）；**禁止在音频实时任务调用**（事件带 payload 会内部 calloc） |
| `fb_abort()` | 中止当前/待播反馈（看门狗兜底用；淡出 + 释放 R1 门控） |
| `fb_is_active()` / `fb_active_kind()` | 观测 |
| `fb_register_done_cb(cb, arg)` | 完成回调（`player` 任务上下文；voice_session 用来驱动 ERROR → IDLE） |
| `fb_stats_read/reset()` | 统计（`diag` 的 `fb status`） |

## TTS 流竞态（为什么有 pending）

TTS 流播放期间 `audio_player_play_pcm()` 会返回 `ESP_ERR_INVALID_STATE`（契约：流式期间
拒绝 PCM）。若异常发生在 SPEAKING（云端 error / 中途断网），本组件先把反馈置为
"pending"，等 `AUDIO_PLAYER_SRC_TTS_STREAM` 的完成回调（voice_session 已先行 abort）后再补播，
避免"错误静默丢失"。

## 内存与实时约束（CONVENTIONS §3/§4）

- 不建任务、不定时器、不在音频任务内 malloc；静态只增加一个小状态结构 + 统计；
- 提示音 PCM 在 flash rodata（`assets` 组件），通道 ring 复用 `audio_player` 的三通道混音；
- `fb_notify()` 只做非阻塞调用；完成回调在 `player` 任务上下文，禁止阻塞。

## 配置与调试

- 无 Kconfig（类别与提示音映射是产品行为，走代码 + `assets` 表）；
- console：`fb status` / `fb reset` / `fb test <net|cloud|timeout|tts>`（手动试听，不起会话）；
- 日志 TAG：`FB`（`log FB debug` 可调）。

## 后续（P4 / PD-002）

- 屏幕/灯光通道：新组件订阅 `APP_EVT_FEEDBACK` 即可，无需改本组件；
- 待机断网的持续状态反馈（PD-002）可复用本组件的 kind 体系扩展（例如新增 `net-state`）。
