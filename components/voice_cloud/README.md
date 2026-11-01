# voice_cloud —— 语音云协议（L3 能力）

**作用（一句话）**：把 `audio_fe` 的上行 PCM、`codec_enc` 的 Opus、`transport_ws` 的 WS 传输串成**一轮真实可跑的端云语音会话**：
发 `audio_start` → 20ms Opus 帧上行 → VAD 静音 800ms / 累计 8s → `audio_end`；同时解析下行
`asr_*` / `llm_delta` / `tts_*` / `error`，按 `sid` 丢弃迟到帧并分发（`event_bus` 事件 + 业务回调），
并把 `tts_start`~`tts_end` 窗口内的 0x02 帧交给 `audio_player` **流式解码播放**（T2.4，半双工 R1/R2）。

契约依据：[ARCHITECTURE.md](../../docs/ARCHITECTURE.md) §3.6、§4.1、§6、§9.3；字段/码表见 [PROTOCOL.md](../../docs/PROTOCOL.md)。

## 职责边界（重要）

| 属于本组件 | **不**属于本组件 |
|---|---|
| 一轮上行链路（读 PCM → 编码 → 发帧 → 收尾）、协议超时 | 会话状态机 `IDLE/LISTENING/THINKING/SPEAKING/ERROR`（T3.1 `voice_session`，已落地） |
| `audio_start` / `audio_end` 报文（`sid` 由 `voice_session` 传入） | VAD/唤醒事件接线（T1.2b 已由 `voice_session` 消费）、会话级错误策略与提示音 |
| 下行语音事件解析、`sid` 过滤、事件/回调分发 | 错误提示音（T3.6）、按键打断（T3.2/T3.4） |
| 下行 0x02 → `audio_player` 流式播放的接线（开流/写帧/收尾/打断 + R1/R2） | MP3 解码与 I2S 输出本身（`audio_player`，T2.4-A） |
| 上行/下行统计（`vc status`） | 音乐协议（T4.2） |

## 对外接口（`include/voice_cloud.h`）

| 接口 | 说明 |
|---|---|
| `vc_init()` | 注册 WS 主接收回调 + 建 `vc` 任务（核 0 / 优先级 4 / 8KB，ARCH §9.3）。必须在 `ws_init()` 之后调用 |
| `vc_start(sid)` / `vc_start_auto()` | 发起一轮（异步）。**产品路径由 `voice_session` 传入它分配的 sid**；`*_auto` 只留给 console 探针与测试脚本（每轮自增，0 保留）。云端未 READY 返回 `ESP_ERR_INVALID_STATE`，调用方按"网络未就绪"降级，**不要重试等待** |
| `vc_stop()` | 优雅收尾：补 `enc_flush` 尾帧 + 发 `audio_end`（用于调试/按键路径） |
| `vc_cancel()` | 立即放弃：不补尾帧、不发 `audio_end` |
| `vc_is_busy()` / `vc_get_state()` / `vc_state_name()` / `vc_current_sid()` | 三态观测（`IDLE` / `UPLINK` / `WAIT_RESPONSE`） |
| `vc_register_cb(cb, arg)` | 业务回调（覆盖语义），与 `event_bus` 事件同时分发；回调在 `ws_client` 任务上下文，**禁止阻塞**，`text` 只在回调内有效。T3.1 起事件枚举增加 `VC_EVT_UPLINK_END`（`code`=收尾原因）与 `VC_EVT_ROUND_END`（`round_end`=`vc_round_end_t`），供 `voice_session` 驱动状态机 |
| `vc_stats_read()` / `vc_stats_reset()` | 轮次/上行帧/字节/首帧耗时/下行事件/`sid` 丢弃/解析失败/下行音频播放与丢弃等统计（`diag` 的 `vc status` 使用） |

## 一轮的时序

```
voice_session（IDLE→LISTENING，提示音播完、sid 已分配）
  └─ vc_start(sid)
       └─ [vc 任务] enc_open → audio_fe_uplink_start → ws "audio_start"
            └─ 循环：audio_fe_uplink_read(320) → enc_encode → ws_send_bin(0x01)
                 ├─ VAD 连续静音 800ms（至少出现过人声）→ 收尾
                 ├─ 累计 8s → 收尾
                 ├─ vc_stop() → 收尾
                 └─ vc_cancel() / WS 掉线 → 直接放弃（不发 audio_end）
            └─ 收尾：enc_flush 尾帧 → ws "audio_end" → enc_close → uplink_stop
            └─ WAIT_RESPONSE：等 asr/llm/tts 事件（10s 超时，收到数据后刷新为"断流超时"）
                 ├─ tts_start → audio_player_stream_open(1, sr) + 暂停收音（R1）
                 │    └─ 窗口内 0x02 帧 → audio_player_stream_write()（≤2KB/次）
                 ├─ tts_end   → audio_player_stream_end()（排空 + 100ms fade + 300ms 尾部）
                 │    └─ 播放完成回调（src=TTS_STREAM）→ 恢复收音（R2）→ round done
                 ├─ error / 断链 / 新轮次 / 响应超时 / vc_cancel()
                 │    └─ audio_player_stream_abort()（立即停声，回调 ok=false → R2）
                 └─ 超时      → WARN + VC_EVT_ROUND_END(TIMEOUT)，回 IDLE
```

> T3.1 起，`audio_end` 发出后回调 `VC_EVT_UPLINK_END`、每轮结束回调 `VC_EVT_ROUND_END`；
> 状态迁移由 `voice_session` 决定，本组件只报告事实。

## 下行事件处理

| 下行 `t` | 处理 |
|---|---|
| `asr_partial` / `asr_final` | 投 `APP_EVT_ASR_PARTIAL/FINAL`（`app_evt_asr_t`，文本截断到 28B）+ `vc_register_cb` 回调；完整文本打 INFO 日志 |
| `llm_delta` | 仅回调 + DEBUG 日志（ARCH §4.2 未定义对应 event_bus 事件） |
| `tts_start` | 校验 `codec=mp3`、记 `sr`；`audio_player_stream_open(1, sr)` 成功 → `audio_fe_set_feed_enabled(false)`（R1），失败只 WARN；投 `APP_EVT_TTS_START` + 回调；打开下行 0x02 接收窗口 |
| `tts_end` | `audio_player_stream_end()` 排空收尾；投 `APP_EVT_TTS_END`（带**本轮**下行帧数，T3.1 修正了原先的会话累计口径）+ 回调；关闭窗口，本轮结束 |
| `error` | ERROR 日志（码表见 PROTOCOL §4）+ `audio_player_stream_abort()` + 回调；结束本轮，不自动重试 |
| 下行 0x02 帧 | 窗口内（`WAIT_RESPONSE` 且未收 `tts_end`）→ `audio_player_stream_write()`（成功计入「音频帧」）；窗口外/收尾后/播放器未开流/ring 满 → 丢弃 + 计入「音频丢弃」帧/字节（限频 WARN） |
| 其它 `t` | WARN 后忽略（契约外事件，如 T2.5 阶段的 `music_track`） |
| 无/非法 `sid`、`sid ≠ 当前` | 丢弃 + 计数（`vc status` 的「sid 丢弃」），不结束本轮 |

## 依赖

- `audio_fe`（上行 PCM 环形缓冲 + 半双工 feed 开关，T2.1）
- `audio_player`（流式播放 API / 完成回调附加订阅槽，T2.4-A）
- `codec_enc`（Opus 编码，T2.2）
- `transport_ws`（发送队列、8B 帧头、`seq` 自增、主接收回调，T1.4）
- `event_bus`（ASR/TTS 应用事件，L1）

## Kconfig

| 配置 | 默认 | 说明 |
|---|---|---|
| `VC_TASK_STACK` | 8192 | `vc` 任务栈；8KB 足够（编码临时数组在 PSRAM 伪栈），实测轮次中最低剩余 3,212 B |
| `VC_TASK_PRIORITY` / `VC_TASK_CORE` | 4 / 0 | 低于 `ws`/`evt`，绑协议核；核 1 只跑音频实时路径 |
| `VC_VAD_SILENCE_MS` | 800 | VAD 连续静音多久收尾（ARCH §3.6） |
| `VC_MAX_UTTER_MS` | 8000 | 单轮上行上限（用户不说话时兜底） |
| `VC_READ_TIMEOUT_MS` | 50 | `audio_fe_uplink_read()` 等待上限；也决定 `vc_cancel()` 的响应粒度 |
| `VC_RESPONSE_TIMEOUT_MS` | 10000 | 等云端响应超时（ARCH §7 THINKING 10s）；收到 `tts_start`/0x02 数据会把 deadline 刷新为"连续无数据 10s"，避免长 TTS/短暂抖动被误判超时 |

## 关键约束

1. **编码只在 `vc` 任务里串行调用**：音频任务（`afe_feed`/`afe_fetch`）内不得编码/发送/分配（CONVENTIONS §3）。
2. **编码器按轮次 open/close**：轮次中 `enc test`/`enc open`/`enc close` 会被 `diag` 拒绝（提示先 `vc cancel`）；IDLE 时 console 探针可用。
3. **上行接口独占**：轮次中 `afe uplink on` 会被拒绝；反之若检测到上行已被 console 占用，本轮跳过（WARN）。
4. **发送一律非阻塞**：`ws_send_text/bin` 失败只计数 + 限频 WARN，绝不等待重试；队列满丢帧由传输层负责。
5. **`sid` 由 `voice_session` 分配**（T3.1 已完成，PROTOCOL §3.3）：`vc_start(sid)` 传入；`vc_start_auto()` 仅测试/探针用，迟到帧规则不变。
6. **文本截断**：event_bus payload 定长 32B，ASR 文本最多 28B（约 9 个汉字）；完整文本在 VC 日志/回调里。
7. `vc_start*` 是异步的：返回 `ESP_OK` 只表示已受理，真正开轮要看 `vc status` / 日志。
8. **流式播放的写帧在 `ws_client` 任务上下文**（下行回调里）：`audio_player_stream_write()` 只做 PSRAM ring 拷贝（单次 ≤2KB，非阻塞）；ring 满返回 `ESP_ERR_NO_MEM`，本组件计入「音频丢弃」并限频 WARN，不重试。
9. **R2 恢复收音靠完成回调，不在 `vc` 任务里做**：`vc_init()` 里 `audio_player_subscribe_done()` 注册一次（附加槽，≤3 个，不要重复注册）；回调在 `player` 任务上下文，只做一次 `audio_fe_set_feed_enabled(true)`，禁止阻塞/耗时。成功、失败、打断都会回调。
10. **打断是立即关 TX（无淡出）**：`vc_cancel`/云端 error/断链/新轮次/响应超时都调 `audio_player_stream_abort()`；未开流时返回 `ESP_ERR_INVALID_STATE` 按无操作处理，不刷屏。
11. **VAD 静音仍用 `audio_fe_stats_read().vad_active` 轮询**（50Hz）：T1.2b 已把 VAD 事件接到 event_bus，但本组件在 `vc` 任务里需要一个同步快照判"连续静音"，轮询成本可忽略；改为事件驱动留待后续按需优化。

## 真机验收（T2.5，2026-10-31）

- **自动测试**：`uv run tools/voice_cloud_test.py` **43/43 PASS**（唤醒 → 上行 193~197 帧/29.5~30.9 B/帧（~12.4kbps）→ `audio_end` 原因 `vad-silence` → ASR/TTS 事件 → 100 帧下行 → `round done`；`vc stop` 收尾原因 `stop-request`；`vc cancel` 立即放弃；event_bus 收到 ASR_FINAL/TTS_START/TTS_END；内部堆 ≥30KB；无 ERROR）。
- **迟到帧过滤**：`uv run tools/voice_cloud_test.py --inject-late`（mock 每轮注入 `sid+1` 的 `asr_final`）**43/43 PASS**，端侧 `dropped_sid=1`。
- **回归**：`voice_test.py --volume 50` 16/16；`ws_mock_server.py --self-test` 9/9。
- **资源**：静态 DIRAM 196,670 B；wss 握手期内部堆峰值 227,988 B、最低水位 32,339 B（静态+峰值 424,658 B ≤ 440KB）；PSRAM 峰值 902,908 B；任务数 16→17（+`vc`）。

## 真机验收（T2.4-B，2026-10-31）

- **下行流式播放**：`uv run tools/tts_stream_test.py` **193/193 PASS**（mock 真实 24k 单声道 MP3、
  202 帧/19392 B）：`first_sound_us` 20~29 ms（< 150 ms）、稳态 `underrun=0`、`decoded==fed==202`
  == PyAV 参考、`dur≈4850 ms`、`bad=0`、`ring_ms=0`；`vc` 窗口内 `音频帧 202`、窗口外
  `音频丢弃 2 帧 字节 192`（`--tts-late-bin`）；abort（`vc cancel`）停声延迟 **51 ms**（≤150 ms，
  目标 100 ms），打断后不再解码、回调 ok=false；stall 场景 `underrun=1` 且恢复、`rounds_done=1`；
  合成帧降级无 ERROR/panic、`tts_end` 正常收尾、R2 收音恢复；连续 10 轮内部堆波动 88 B、
  PSRAM 首末差 0 B。
- **回归**：`voice_cloud_test.py` **43/43**（含 stop/cancel/中止计数）、`voice_test.py
  --volume 50 --wakes 3` **16/16**。
- **起播门槛**：`AUDIO_PLAYER_STREAM_PREROLL_FRAMES` 默认 2 → **4**（T2.4-B 真机迭代，中间验证过 3：
  2 帧时 I2S DMA 预填 40 ms 后仅剩 ~8 ms 余量，偶发一次起播 `underrun=1`；4 帧 96 ms 后余量
  56 ms，20 轮 heap-only + 全量 193 项稳定 0 underrun；首声仍 20~29 ms < 150 ms）。
- **测试环境坑**：mock 侧 TCP 未关 Nagle + 端侧 delayed ACK 会让 0x02 帧攒批 ~200 ms 再交付，
  造成假欠载（`tts_stream_test.py` 曾随机 FAIL underrun=0）；mock 已对每条 accept 连接设
  `TCP_NODELAY=1`（T2.4-C2）。真实云端同样应关闭 Nagle。

## 踩坑记录

1. **中文文本被二次编码成乱码**：手写 JSON 扫描器最初把原始 UTF-8 字节逐个当 codepoint 再编码，中文变 `ä»å¤©…`。修法：`vc_decode_str()` 对 `\uXXXX` 转义才做码点解码，**原始多字节 UTF-8 原样透传**。联调时看到 ASR 日志乱码先查这里。
2. **`vc_start` 与 `begin_round` 的 `abort` 竞态**：`begin_round` 会清 `s_abort`，若先置 abort 再置 pending，新轮次可能被自己的 abort 立刻打断。最终用"pending 只表示有新请求、循环里检查 pending 即放弃旧轮次"的顺序，`vc_start` 不再直接置 abort。
3. **VAD 静音判定**：AFE 的 VAD 事件回调在 main 注册（T1.2b 未完成），`voice_cloud` 用 `audio_fe_stats_read().vad_active` 50Hz 轮询 + "至少出现过一次人声"标记，避免刚启动（提示音余音）就误收尾；参数走 Kconfig。
4. **`ws rxlog` 与业务回调互斥（已修）**：P1 的单回调 API 会被 `diag` 的 `ws rxlog` 覆盖、静默丢业务事件；T2.5 给 `transport_ws` 加了附加订阅（`ws_subscribe`），rxlog 走附加槽位。
5. **非契约端点会刷 WARN**：用公网 `wss://echo.websocket.org` 测 TLS/内存时，回显端点会把 `hello` 回显成非契约下行文本，`voice_cloud` 按"未知事件/非法文本"WARN 后忽略，属预期（见 PROTOCOL §8）。

## 真机验收（T3.1，2026-11-01）

- `voice_cloud_test.py` **50/50 PASS**（T2.5 的 43 项 + 7 项会话语义）：VS 迁移序列
  `IDLE→LISTENING→THINKING→SPEAKING→IDLE`、迁移 `sid` 一致、event_bus 收到
  `VOICE_STATE_CHANGED`、`vs status` 开始/完成各 1 且无异常/超时；`--inject-late` 同为 50/50。
- `tts_stream_test.py` **193/193 PASS**（`TTS end（本轮下行音频 30 帧）` 验证 per-round 计数；
  手动 `vc start/stop/cancel` 路径不受状态机影响）。
- 手动验证错误路径：云端 `error` → VS `THINKING→ERROR→IDLE`；mock `--think-ms 12000` →
  `THINKING` 停留 10000 ms 后 `→ERROR`，迟到帧按窗口外丢弃；`vs interrupt` 立即中止轮次。

## 后续

- 错误提示音（T3.6）接入后，ERROR 状态的停留时长改为提示音时长（`CONFIG_VS_ERROR_HOLD_MS`）；
- 按键打断（T3.2/T3.4）改为 R4 完整路径（100ms 淡出 + interrupt 报文 + 50ms 丢弃）；
- 如需进一步降 CPU，可把 VAD 静音判定改为事件驱动（T1.2b 已具备事件源）。
