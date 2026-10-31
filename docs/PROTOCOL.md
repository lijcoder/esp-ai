# WebSocket 协议（PROTOCOL）

> 本文件是 [ARCHITECTURE.md](ARCHITECTURE.md) §6 契约的详细展开。
> **状态：T2.5 定稿（2026-10-31）** —— 端侧 `components/voice_cloud` 与 `tools/ws_mock_server.py` 已按本文件实现；
> 字段/码表有变时**先改 ARCHITECTURE §6，再改 mock + 端侧，最后回填本文件**。
> 未列出的字段服务端可自由新增（端侧忽略），见 §5 版本兼容。

## 1. 连接与握手

### 1.1 端点与参数

```
wss://<host>/v1/voice?device_id=<id>&token=<token>&ver=1
```

| 参数 | 来源 | 必填 | 说明 |
|---|---|---|---|
| `device_id` | `CONFIG_AI_DEVICE_ID`（量产应由配网/后端下发，P5 再定） | 是 | 设备唯一标识；URL 里缺省时端侧补齐 |
| `token` | `CONFIG_AI_TOKEN` | 是 | 鉴权票据；**不进日志**，端侧只打印 scheme+host+path |
| `ver` | 端侧固定 `1` | 是 | 协议版本，见 §5 |

- 传输：线上只用 `wss://`；本地联调可用 `ws://`（由 `CONFIG_AI_WS_URL` 的 scheme 决定，X2 方案 A）。`wss` 建连前要求 SNTP 已对时。
- **token 刷新策略（当前）**：编译期常量 + 重启生效；鉴权失败（4002）不自动重试，需修正配置后重启或 `ws reconnect`。动态签发/续期留到 P5 后端契约联调。

### 1.2 握手时序

```
设备                                服务端
  │ ── TCP/TLS/WebSocket 建连 ──────▶ │
  │ ── {"t":"hello","device_id":…,"ver":1} ──▶ │
  │ ◀── {"t":"hello_ack","ver":1} ── │   （5s 内未收到 → 端侧判建连失败，关闭 + 退避重连）
  │ ── READY：可收发音视频事件 ──────▶ │
```

- 心跳：端侧每 20s 发 `{"t":"ping"}`；服务端必须回 `{"t":"pong"}`；10s 未收 `pong` 判离线。
- 重连退避：1s → 2s → 5s → 10s → 30s（上限），无限重试直到网络恢复；每次重连都重发 `hello`。
- 鉴权失败关闭码：**4001 = device_id 非法，4002 = token 非法**；端侧 ERROR 日志后不再自动重试。

## 2. 二进制帧（音频）

### 2.1 帧头（8 字节，定稿，不得私改）

```
 0    1    2    3    4    5    6    7
+----+----+----+----+----+----+----+
|0x56|0x4F|ver |type| seq(2B BE) | len(2B BE) |
+----+----+----+----+----+----+----+
| payload: 编码音频数据 ...          |
```

| 字节 | 字段 | 值 / 说明 |
|---|---|---|
| 0–1 | magic | `0x56 0x4F`（'V','O'） |
| 2 | `ver` | `1`（不支持其它值，端侧丢弃并计入 `rx_bad`） |
| 3 | `type` | `0x01` 上行 / `0x02` 下行（见 2.2） |
| 4–5 | `seq` | 大端，每端独立、按帧自增、uint16 回绕；**由传输层维护**，业务层不参与 |
| 6–7 | `len` | 大端，payload 字节数；必须等于实际 payload 长度 |

### 2.2 帧类型

| type | 方向 | 内容 | 典型长度 |
|---|---|---|---|
| `0x01` | ↑ | 上行麦克风音频：Opus 16kHz 单声道 VBR（20ms/帧，目标 16kbps） | 25~45 B |
| `0x02` | ↓ | 下行 TTS 音频：MP3 24kHz 单声道（服务端按契约输出） | ~192 B/20ms |

上行编码实现见 `components/codec_enc`（定点 libopus + VBR）；下行解码播放见 T2.3/T2.4。

### 2.3 `sid` 关联（定稿 P-2）

**帧头不带 `sid`**。约定：同一连接内，音频帧按 `audio_start → tts_end` 的顺序归属当前轮次。

端侧实现（`voice_cloud`）：
- 收到 `audio_start` 后开始上行，`0x01` 帧连续发送；
- 发 `audio_end` 后进入"等待响应"窗口，只在该窗口内接收并计数 `0x02` 帧；
- `tts_end` 结束窗口；窗口外到达的 `0x02`（上一轮残留）直接丢弃，不计数、不播放；
- 因此单连接同一时刻最多一个语音轮次（T3.1 会更严格地隔离会话）。

### 2.4 示例（上行一帧 Opus，payload 33B）

```
56 4F 01 01 00 A7 00 21 <33 字节 Opus 数据>
```

## 3. 文本帧（JSON）

所有文本帧为 UTF-8 JSON 对象（`ensure_ascii` 不做要求，中文按 UTF-8 原样传输）；端侧对未知字段忽略。

### 3.1 上行事件

| `t` | 触发 | 字段 | 示例 |
|---|---|---|---|
| `hello` | 建连后 | `device_id`(str)、`ver`(int) | `{"t":"hello","device_id":"esp32-dev1","ver":1}` |
| `audio_start` | 每轮开始 | `sid`(uint16) | `{"t":"audio_start","sid":1}` |
| `audio_end` | VAD 静音 800ms 或累计 8s（或调试 `vc stop`） | `sid`(uint16) | `{"t":"audio_end","sid":1}` |
| `interrupt` | 用户打断（T3.1 接线） | `sid`(uint16) | `{"t":"interrupt","sid":1}` |
| `ping` | 每 20s | 无 | `{"t":"ping"}` |
| `music_request` | 音乐控制（P4） | `action`(str)、`id`/`index`(int) | `{"t":"music_request","action":"play","id":"a1","index":0}` |
| `music_status` | 播放状态上报（P4） | `state`(str)、`progress_ms`(int) | `{"t":"music_status","state":"playing","progress_ms":12000}` |

`audio_end` 的收尾原因由端侧日志区分（`vad-silence` / `max-duration` / `stop-request`），**不上报文**（避免与服务端字段耦合）。

### 3.2 下行事件

| `t` | 字段 | 示例 | 端侧处理（T2.5） |
|---|---|---|---|
| `hello_ack` | `ver`(int) | `{"t":"hello_ack","ver":1}` | 传输层消费，进入 READY |
| `asr_partial` | `sid`(uint16)、`text`(str) | `{"t":"asr_partial","sid":1,"text":"今天天气"}` | 投 `APP_EVT_ASR_PARTIAL` + 回调 |
| `asr_final` | `sid`、`text` | `{"t":"asr_final","sid":1,"text":"今天天气如何"}` | 投 `APP_EVT_ASR_FINAL` + 回调 |
| `llm_delta` | `sid`、`text` | `{"t":"llm_delta","sid":1,"text":"今天"}` | 仅回调（无 event_bus 事件） |
| `tts_start` | `sid`、`codec`(str)、`sr`(int) | `{"t":"tts_start","sid":1,"codec":"mp3","sr":24000}` | 投 `APP_EVT_TTS_START` + 回调；打开 0x02 接收窗口 |
| `tts_end` | `sid` | `{"t":"tts_end","sid":1}` | 投 `APP_EVT_TTS_END` + 回调；关闭窗口、本轮结束 |
| `error` | `sid`、`code`(int)、`msg`(str) | `{"t":"error","sid":1,"code":3001,"msg":"TTS failed"}` | ERROR 日志 + 回调；结束本轮，不自动重试 |
| `pong` | 无 | `{"t":"pong"}` | 传输层消费（RTT 用端侧 tick 计算） |
| `music_track` | `sid`、`url`、`codec`、`duration_ms`、`title` | 见 ARCH §6.3 | T4.2 实现；当前端侧记 WARN 后忽略 |

### 3.3 `sid` 规则与迟到帧过滤

- 每轮语音 `sid` 由**端侧**生成、uint16 自增（T2.5 简化：`voice_cloud` 内部计数，T3.1 接管后由会话状态机分配）；
- 服务端下发语音事件时必须原样回填 `sid`；
- 端侧规则：**无 `sid`、`sid` 非法、或与当前轮次不一致的事件一律丢弃**，计入 `vc status` 的「sid 丢弃」；不报错、不结束本轮。
- 迟到帧过滤已用 mock `--inject-late`（每轮注入一个 `sid+1` 的 `asr_final`）做真机验证。

### 3.4 未知事件 / 非法文本

- 未定义的 `t`（如音乐事件在 T2.5 阶段）：WARN 一次后忽略，不影响当前轮次；
- 非 JSON 文本帧或缺少 `t`：计入「解析失败」，WARN 后丢弃（服务端契约违例）。

## 4. 错误码表（定稿 P-4）

`error` 事件的 `code` 为十进制整数；端侧处理策略统一为「ERROR 日志 + 结束本轮（不自动重试）」，用户再次唤醒触发新一轮；提示音由 T3.6 实现。

| code | 含义 | 端侧建议处理 |
|---|---|---|
| `1` | 通用 / 未分类错误（mock `--scenario error` 使用） | 结束本轮，记录日志 |
| `1001` | 鉴权/会话失效（可伴随 4002 关闭） | 结束本轮；等 `ws reconnect`/`NET_UP`，不自动重试 |
| `2001` | ASR 失败（服务端识别失败） | 结束本轮，可提示"没听清" |
| `2002` | ASR 超时 | 结束本轮 |
| `3001` | TTS 合成/下发失败 | 结束本轮，可提示"服务繁忙" |
| `3002` | TTS 超时 | 结束本轮 |
| `9001` | 服务端内部错误 | 结束本轮，可提示"服务异常" |

> 端侧不依据 `code` 自动重试（避免放大故障）；需要重试策略时在 T3.6/T5.4 与后端约定。

## 5. 版本兼容策略

- `ver=1` 为当前唯一版本；`hello`/`hello_ack` 的 `ver` 必须为 `1`（mock 对 `ver!=1` 继续服务但 WARN）。
- **新增字段**：服务端可加、端侧必须忽略未知字段（已实现）；不升 `ver`。
- **新增事件 `t`**：端侧忽略 + WARN（不崩、不影响当前轮次）；需要端侧消费时先改 ARCH §6.3 再改代码。
- **变更帧头/`ver`**：必须升 `ver`，端侧不识别时丢弃帧并计入 `rx_bad`。
- 能力协商（如 `hello` 里声明支持的 codec）：暂不实现，服务端按 §6.2/§6.3 固定格式下发即可；T5.4 后端联调时再评估。

## 6. 报文样例（与 mock 一致）

```
设备 → 云端
{"t":"hello","device_id":"esp32-dev1","ver":1}
{"t":"audio_start","sid":1}
（二进制 0x01 帧连续上行 ~50 帧/s）
{"t":"audio_end","sid":1}

云端 → 设备
{"t":"hello_ack","ver":1}
{"t":"asr_partial","sid":1,"text":"今天天气"}
{"t":"asr_final","sid":1,"text":"今天天气如何"}
{"t":"llm_delta","sid":1,"text":"今天"}
{"t":"tts_start","sid":1,"codec":"mp3","sr":24000}
（二进制 0x02 帧连续下发，100 帧 ≈ 2s）
{"t":"tts_end","sid":1}
{"t":"pong"}
```

## 7. 待确认项与定稿结果

| 编号 | 待确认项 | 状态 / 定稿 |
|---|---|---|
| P-1 | `hello` 是否有应答 | ✅ T1.4：服务端必须回 `hello_ack`（5s 超时） |
| P-2 | 二进制帧如何关联 `sid` | ✅ T2.5：帧头不加 `sid`，按 `audio_start → tts_end` 顺序归属（§2.3） |
| P-3 | 音乐事件 `sid` 生成方/作用域 | ⏳ T4.2；mock 暂用连接内独立计数器 |
| P-4 | `error` 错误码表 | ✅ T2.5：码表见 §4 |
| P-5 | 鉴权失败关闭码 | ✅ T1.4：应用关闭码 4001/4002 |
| P-6 | `pong` 字段与 RTT 口径 | ✅ T1.4：固定 `{"t":"pong"}`，RTT 端侧 tick |
| P-7 | `ws://` / `wss://` 证书策略 | ✅ T1.4：按 URL scheme 双传输，量产强制 `wss://` |

## 8. 联调工具与已知差异

- `tools/ws_mock_server.py` 实现本文件全部契约；`--self-test` 9 项不需硬件；`--inject-late` 注入迟到帧验证端侧 `sid` 过滤；`--no-tts` / `--scenario error|music` / `--save-upload` 等见 `tools/README.md`。
- mock 下发的 TTS 二进制是**合成 payload（不可解码的伪 MP3）**，仅用于帧级联调；真实 MP3 播放由 T2.3 用真实素材补。
- 公网 `wss://echo.websocket.org/.ws` 之类回显端点**不符合本协议**（不回 `hello_ack`、会把 `hello` 原样回显），只用于 TLS 路径/内存水位验证；端侧对非契约下行文本会 WARN 后忽略，属预期。
- 端侧上报的 `audio_end` 原因、`vc` 统计等调试信息**不上报文**，只走日志/console，见 `docs/ARCHITECTURE.md` §10.2。
