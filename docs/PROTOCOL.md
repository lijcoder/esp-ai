# WebSocket 协议（PROTOCOL）

> 本文件是 [ARCHITECTURE.md](ARCHITECTURE.md) §6 契约的详细展开。
> **当前状态：空壳**。P2（T2.5）实现 `voice_cloud` 时按 §6 补全字段表、错误码表与示例报文；在此之前一切以实现 ARCHITECTURE §6 为准。

## 待补内容（P2 时填写）

- [ ] 握手与鉴权字段完整定义（`hello` 参数、token 刷新策略）
- [ ] 二进制帧头逐字节说明与示例
- [ ] 上行事件（`audio_start` / `audio_end` / `interrupt` / `ping` / `music_request` / `music_status`）字段表
- [ ] 下行事件（`asr_partial` / `asr_final` / `llm_delta` / `tts_start` / `tts_end` / `music_track` / `error` / `pong`）字段表
- [ ] 错误码表与端侧处理策略
- [ ] 版本兼容策略（`ver` 字段、能力协商）
- [ ] 与后端联调的报文样例

## 实现中发现的契约待确认项（T1.5 mock 联调期间，2026-10-30）

> 这些不是 `tools/ws_mock_server.py` 的私自定义，而是从"实现方视角"看 §6 契约缺的字段/规则。
> P2（T2.5 `voice_cloud`）定稿本文件时逐条确认，涉及 §6 字段的**先改 ARCHITECTURE §6 再改代码**。

| 编号 | 待确认项 | 现状 / 影响 | 建议口径 | 定稿时机 |
|---|---|---|---|---|
| P-1 | `hello` 是否需要服务端应答 | §6.1 只定义了端侧发 `hello`，没定义云端回什么：端侧无法判断鉴权是否成功，T1.4 可能被迫靠"收到任意事件"间接确认 | 增加 `hello_ack`（带 `ver`）或明确"鉴权成功即静默、失败回 `error` + 关闭码" | ✅ T1.4 已定稿（见下方「已定稿口径」） |
| P-2 | 二进制音频帧如何与会话 `sid` 关联 | §6.2 帧头没有 `sid`，但 §6.3 要求迟到帧按 `sid` 丢弃；TTS 是多帧流，跨轮次迟到帧单帧无法判定 | 约定"同一连接内按 `audio_start → tts_end` 顺序关联"，或帧头扩展 `sid`（改 §6.2，需评估端侧解析成本） | T2.5 |
| P-3 | 音乐事件 `music_track`/`music_status` 的 `sid` 由谁生成、与语音 `sid` 是否同一空间 | §7 只规定语音每轮 `sid` 自增；mock 暂用独立计数器（从 1 起） | 明确音乐 `sid` 生成方与作用域（连接内 / 设备内） | T4.2 |
| P-4 | `error` 事件的错误码表 | §6.3 只有"错误码与描述"：端侧 P3 错误提示（T3.6）无法穷举，mock 暂用 `code=1` | 定义 `code` 枚举（网络 / 鉴权 / ASR / TTS / 超时 / 内部）+ 端侧处理策略 | T2.5 |
| P-5 | 鉴权失败的关闭码 / 表现 | §6.1 只说用 `token` 鉴权，未定义失败表现；mock 暂用应用码 4001（device_id）/4002（token） | 约定关闭码或 `error` 事件二选一（建议 4001，便于端侧 `disconnect_reason` 分支） | ✅ T1.4 已定稿（见下方「已定稿口径」） |
| P-6 | `pong` 的字段与 RTT 口径 | §6.1 只写端侧 20s 一次 `ping`、10s 内未收 `pong` 判离线；`pong` 是否带 `sid`/时间戳未给 | 给出 `pong` 完整样例与 RTT 计算口径（用设备本地 tick 即可，不带时间戳） | ✅ T1.4 已定稿（见下方「已定稿口径」） |
| P-7 | 本地联调 `ws://` 与线上 `wss://` 的证书验证策略 | 端侧必须同时支持两种传输才能本地联调；自签证书处理方式未定 | 见 [PRODUCT_IDEAS.md](PRODUCT_IDEAS.md) §8 X2 | ✅ T1.4 已定稿（见下方「已定稿口径」） |

> 已在 mock 中按"最小假设"实现，任何一条定稿后：先改 ARCHITECTURE §6（如涉及字段），再改 mock + 端侧，最后回填本表状态。

## 已定稿口径（T1.4，2026-10-31）

> 均已同步到 [ARCHITECTURE.md](ARCHITECTURE.md) §6.1、§6.3 与 `tools/ws_mock_server.py`；端侧实现见 `components/transport_ws`（T1.4）。

| 编号 | 定稿内容 | 落地位置 |
|---|---|---|
| P-1 | `hello` 后服务端**必须**回 `{"t":"hello_ack","ver":1}`；端侧 5s 内未收到按建连失败处理（关闭 + 退避重连）。mock 收到 hello 立即回 ack | ARCH §6.1/§6.3；mock `_on_text` 的 hello 分支；自测「hello 握手」用例 |
| P-5 | 鉴权失败用应用关闭码：**4001 = device_id 非法，4002 = token 非法**；端侧 ERROR 日志后不自动重试，等 `NET_UP`/`ws reconnect` 再试 | ARCH §6.1；mock 的 `CLOSE_BAD_DEVICE`/`CLOSE_BAD_TOKEN`；transport_ws 的 close 分支 |
| P-6 | `pong` 固定为 `{"t":"pong"}`（不带 `sid`/时间戳）；RTT 由端侧用 `esp_timer` 本地 tick 计算，`ws status` 显示最近一次 RTT | ARCH §6.1/§6.3；transport_ws 心跳状态机 |
| P-7 | 采用 PRODUCT_IDEAS §8 X2 **方案 A 双传输**：由 `CONFIG_AI_WS_URL` 的 scheme 决定 `ws://`/`wss://`，同一个客户端实现；发布检查强制量产只用 `wss://`（`ws://` 启动时打 WARN） | X2 状态回填；transport_ws 的 scheme 分支 + 发布检查项 |
