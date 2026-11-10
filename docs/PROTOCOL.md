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
| `interrupt` | 用户打断（R4：T3.2 已实现，`voice_session`→`vc_interrupt()`；发后立即本地作废本轮，不等应答） | `sid`(uint16) | `{"t":"interrupt","sid":1}` |
| `ping` | 每 20s | 无 | `{"t":"ping"}` |
| `music_request` | 音乐控制（端侧主动点歌，**T4.3 实现**；mock 已支持） | `action`(str)、`id`(int，端侧自增请求序号，仅用于日志关联)、`index`(int，曲目索引，`0`/`-1` = 服务端自选) | `{"t":"music_request","action":"play","id":3,"index":0}` |
| `music_status` | 播放状态上报（T4.2 实现、**T4.3 增 `paused`**）：播放中每 5s + 每次状态变化立即一次；暂停期照常周期上报但 `progress_ms` 冻结 | `state`(str，`playing`/`paused`/`stopped`/`error`)、`mid`(uint16)、`progress_ms`(int) | `{"t":"music_status","state":"paused","mid":1,"progress_ms":12000}` |

`audio_end` 的收尾原因由端侧日志区分（`vad-silence` / `max-duration` / `stop-request`），**不上报文**（避免与服务端字段耦合）。

### 3.2 下行事件

| `t` | 字段 | 示例 | 端侧处理（T2.5/T4.2） |
|---|---|---|---|
| `hello_ack` | `ver`(int) | `{"t":"hello_ack","ver":1}` | 传输层消费，进入 READY |
| `asr_partial` | `sid`(uint16)、`text`(str) | `{"t":"asr_partial","sid":1,"text":"今天天气"}` | 投 `APP_EVT_ASR_PARTIAL` + 回调 |
| `asr_final` | `sid`、`text` | `{"t":"asr_final","sid":1,"text":"今天天气如何"}` | 投 `APP_EVT_ASR_FINAL` + 回调 |
| `llm_delta` | `sid`、`text` | `{"t":"llm_delta","sid":1,"text":"今天"}` | 仅回调（无 event_bus 事件） |
| `tts_start` | `sid`、`codec`(str)、`sr`(int) | `{"t":"tts_start","sid":1,"codec":"mp3","sr":24000}` | 投 `APP_EVT_TTS_START` + 回调；打开 0x02 接收窗口 |
| `tts_end` | `sid` | `{"t":"tts_end","sid":1}` | 投 `APP_EVT_TTS_END` + 回调；关闭窗口、本轮结束 |
| `error` | `sid`、`code`(int)、`msg`(str) | `{"t":"error","sid":1,"code":3001,"msg":"TTS failed"}` | ERROR 日志 + 回调；结束本轮，不自动重试 |
| `pong` | 无 | `{"t":"pong"}` | 传输层消费（RTT 用端侧 tick 计算） |
| `music_track` | `mid`(uint16)、`url`、`codec`、`duration_ms`（**0 = 直播流/时长未知**）、`title`（可选 `artist`） | `{"t":"music_track","mid":1,"url":"http://192.168.1.5:9000/x.mp3","codec":"mp3","duration_ms":269000,"title":"晴天"}` | **T4.2 解析 / T4.3 起播（`music_session`）**：`url` 只接受 `http(s)` 且 ≤ 320B，`codec` 必须是 `mp3`；`mid` 必须比当前新（同 mid 视为重复、不重复起播），否则丢弃计数；接受后由 `music_session` 起播/切歌（先停旧流再起新流，120ms × 15 重试） |
| `music_control` | `mid`(uint16)、`action`(`pause`/`resume`/`stop`) | `{"t":"music_control","mid":1,"action":"pause"}` | **T4.2 解析 / T4.3 执行**：`pause` = 用户暂停 latch（会话结束不自动恢复）→ `audio_player_music_pause()`；`resume` = 清 latch 并从暂停点续播（不回到文件头）；`stop` → `audio_player_music_stop_url()` 丢弃 ring；允许 `mid == 当前曲目`、只丢弃更旧的；下一首/上一首不发它，改用新的 `music_track` |

### 3.3 `sid` 规则与迟到帧过滤

- 每轮语音 `sid` 由**端侧**生成、uint16 自增（T3.1 起由 `voice_session` 分配，经 `vc_start(sid)` 传给 `voice_cloud`；`vc_start_auto()` 仅 console 探针/测试脚本使用）；
- 服务端下发语音事件时必须原样回填 `sid`；
- 端侧规则：**无 `sid`、`sid` 非法、或与当前轮次不一致的事件一律丢弃**，计入 `vc status` 的「sid 丢弃」；不报错、不结束本轮。
- 迟到帧过滤已用 mock `--inject-late`（每轮注入一个 `sid+1` 的 `asr_final`）做真机验证；T3.1 回归仍 50/50 PASS。
- **音乐事件不吃 `sid`**（P-3）：`music_track` / `music_control` 用连接内自增的 `mid`，因为音乐可以跨语音轮次、也可在没有会话时开始；端侧按 `t` 分流，`voice_cloud` 的 `sid` 过滤只作用于语音事件（音乐事件在 `voice_cloud` 里静默跳过，不 WARN、不计入 sid 丢弃）。
- **`mid` 过滤（T4.2 落地）**：`music_track` 只接受比当前**更新**的 `mid`（同 `mid` 丢弃为重复）；`music_control` 允许 `mid == 当前曲目`（暂停/停止本就指向这首），只丢弃更旧的；WS 断链后清比较基准（新连接内 `mid` 重新计数，音乐本身可继续播）。
- **暂停/继续不影响 `mid` 基准（T4.3）**：暂停是端侧播放状态，不消耗 `mid`；暂停期间到达的 `music_control` 仍按 `mid` 规则判别（同一曲目允许），恢复后同一 `mid` 仍有效。

### 3.4 未知事件 / 非法文本

- 未定义的 `t`：WARN 一次后忽略，不影响当前轮次（音乐事件自 T4.2 起由 `music_cloud` 处理，不在本行范围内）；
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
| P-3 | 音乐事件 `sid` 生成方/作用域 | ✅ **已定稿（2026-11-10，T4.2 端侧落地）**：音乐事件用**独立 `mid`**（连接内自增，不复用语音 `sid`；理由与过滤规则见 §3.3）。mock 的 `--music` 与端侧 `music_cloud` 均已按此实现 |
| P-4 | `error` 错误码表 | ✅ T2.5：码表见 §4 |
| P-5 | 鉴权失败关闭码 | ✅ T1.4：应用关闭码 4001/4002 |
| P-6 | `pong` 字段与 RTT 口径 | ✅ T1.4：固定 `{"t":"pong"}`，RTT 端侧 tick |
| P-7 | `ws://` / `wss://` 证书策略 | ✅ T1.4：按 URL scheme 双传输，量产强制 `wss://` |
| P-8 | `music_request` 字段语义 | ✅ **T4.3 定稿**：`action` 目前只有 `play`；`id` = 端侧自增 uint16 请求序号（只用于日志关联，服务端可忽略）；`index` = 曲目索引，`0`/`-1` = 由服务端自选（曲库/推荐）。原表 `id`/`index` 为 int 与样例 `"id":"a1"` 的冲突以本行为准 |
| P-9 | 暂停要不要上报 | ✅ **T4.3 定稿**：`music_status.state` 增 `paused`；暂停期仍每 5s 上报但 `progress_ms` 冻结（服务端据此判断暂停时长）；`stop` 后上报一次 `stopped` |

## 8. 联调工具与已知差异

- `tools/ws_mock_server.py` 实现本文件全部契约；`--self-test` **19 项**不需硬件（含 TCP_NODELAY、真实 MP3 帧切分、`--error-code/--error-stage/--tts-bad-codec` 注入）；`--inject-late` 注入迟到帧验证端侧 `sid` 过滤；`--tts-mp3 <file>` 下发真实 MP3（T2.4）并测流式播放；`--tts-stall-ms/--tts-stall-at` 制造欠载；`--tts-late-bin` 注入窗口外二进制帧；`--no-tts` / `--scenario error|music` / `--save-upload` 等见 `tools/README.md`。**T3.6 错误路径联调参数**：`--error-code N`（error.code，3001/3002 端侧归"语音播放失败"）、`--error-stage {after-audio-end,after-asr,after-tts-start}`（报错时机；`after-tts-start` 用于"SPEAKING 中云端报错，先停 TTS 再播提示音"）、`--tts-bad-codec`（tts_start 故意发 `codec:"pcm"`，触发端侧"设备侧 TTS 无法播放"上报）。
- 缺省 TTS 二进制仍是**合成 payload（不可解码的伪 MP3）**，仅用于帧级/协议联调；真实 MP3 播放用 `--tts-mp3`（`tools/tts_stream_test.py` 自动生成并下发 24k 单声道素材）。
- **mock 默认对每条连接开 TCP_NODELAY**（T2.4 定稿）：Nagle + 端侧 delayed ACK 会把 20~30ms 均匀下发的 TTS 帧攒批 ~200ms，端侧流式 ring 被打空造成假欠载。**真实云端同样必须关闭 Nagle**。
- 公网 `wss://echo.websocket.org/.ws` 之类回显端点**不符合本协议**（不回 `hello_ack`、会把 `hello` 原样回显），只用于 TLS 路径/内存水位验证；端侧对非契约下行文本会 WARN 后忽略，属预期。
- **M1 音乐（2026-11-10，mock 侧，不依赖 LLM 工具）**：`--music` 用一张确定性规则表把 ASR 文本映射成音乐意图——「暂停/继续/下一首/上一首/停止」→ `music_control`，点歌 → 曲库检索后下发 `music_track`；命中时不进 LLM，回答用固定确认语（仍走 TTS 下发）。没有 `--music-url` 时 mock 自带 HTTP 服务下发示例 MP3（`http://<局域网IP>:<端口>/media/demo.mp3`），真机可直接播。曲库：`--music-catalog tools/…json`（`{"tracks":[{"title","artist","url","duration_ms","keywords"}]}`）。曲目可以是**电台直播流**（如 `--music-url http://.../live.mp3 --music-title 清听电台`，`duration_ms=0`，不要求准备本地 MP3）；也可用 `--music-search-url` 接**任意**返回 `{"tracks":[{title,url,duration_ms}]}` 的搜索端点（mock 不内置任何站点抓取/签名，失败回退曲库）；自测：`uv run tools/mock_music_test.py`（178 项，离线）。LLM/Hermes 只负责「说话」；URL 只走 `music_track`。**端侧 T4.3 起会主动发 `music_request`**（"继续"失败兜底与 `ms request play [index]` 探针），mock 已支持：收到后回一条新的 `music_track`（`mid` 继续自增）。
- 端侧上报的 `audio_end` 原因、`vc` 统计等调试信息**不上报文**，只走日志/console，见 `docs/ARCHITECTURE.md` §10.2。
