# ws_mock_server.py —— 本地「云端」WS 模拟服务（T1.5）

**作用**：在真实后端就绪前，用一台电脑模拟云端 WebSocket 服务（握手、心跳、事件下发、TTS 音频流），让端侧 T1.4/T1.6 以及 P2–P4 全部能脱机联调（ROADMAP §2.0 P1 顺序 0：可最早启动，端侧不必等后端）。

契约唯一依据是 [ARCHITECTURE.md](../docs/ARCHITECTURE.md) §6.1–6.3（连接/二进制帧/文本帧）与 §7（`sid` 规则）；本工具**不发明协议字段**，发现的缺口只登记在 [PROTOCOL.md](../docs/PROTOCOL.md) 的「实现中发现的契约待确认项」。

## 依赖与运行

单文件脚本，依赖用 PEP 723 内联声明（`websockets>=14`），不需要 `pip install`、不需要虚拟环境：

```bash
uv run tools/ws_mock_server.py --self-test      # 自测，不需要硬件/不需要手敲命令
uv run tools/ws_mock_server.py                  # 起服务，打印可直连的 ws:// URL
```

- 本机 `python3` 没有 pip、也没有装 websockets，所以用 `uv run` 自动装到隔离环境（Python 3.12 / websockets 17.4 已验证）。
- 只用标准库的实现不予考虑：WS 握手/掩码/分片/关闭握手自己写容易引入额外 bug，联调工具不值得。

## 快速开始

### 1. 自测（主验收手段）

```bash
uv run tools/ws_mock_server.py --self-test
# [1/9] PASS  hello 握手与参数解析
# ...
# self-test: 9/9 PASS   退出码 0
```

进程内起服务 + 起客户端，覆盖 9 项：hello 解析、ping→pong、一轮语音的事件顺序/`sid` 回填/二进制帧头、坏帧容错、`interrupt` 取消、断线重连、TTS 流中 ping、token 拒绝关闭码、`--drop-pong`。

### 2. 起服务并手工联调

终端 A：

```bash
uv run tools/ws_mock_server.py --port 8765
```

启动横幅会打印可直接填 `CONFIG_AI_WS_URL` 的地址（含局域网 IP，设备与电脑需同一网段）：

```
ws://192.168.x.x:8765/v1/voice?device_id=dev1&token=dev-token&ver=1
```

终端 B（手工当一次设备）：

```bash
uv run --with websockets python -m websockets "ws://127.0.0.1:8765/v1/voice?device_id=dev1&token=dev-token&ver=1"
> {"t":"hello","device_id":"dev1","ver":1}
> {"t":"audio_start","sid":1}
> {"t":"audio_end","sid":1}          # 之后会连续收到 asr/llm/tts 事件与二进制帧
> {"t":"ping"}                       # 立即回 {"t":"pong"}
```

服务端每个收发事件打一行日志（`C1` 是连接号），会话结束打印统计摘要：

```
[23:25:00.075] INFO  C1  连接建立 peer=192.168.1.30:51000 path=/v1/voice?device_id=esp32-dev1&token=dev-token&ver=1
[23:25:00.076] INFO  C1  <-- hello device_id=esp32-dev1 ver=1
[23:25:00.076] INFO  C1  <-- 上行音频 seq=0 len=40 sid=1
[23:25:00.076] INFO  C1  <-- audio_end sid=1（本轮上行 5 帧）
[23:25:00.377] INFO  C1  --> asr_partial sid=1 text='今天天气不错，我'
[23:25:00.377] INFO  C1  --> tts_start sid=1
[23:25:00.397] INFO  C1  --> bin type=0x02 seq=0 len=192 sid=1
...
[23:25:03.436] INFO  C1  连接关闭 | 时长 3.4s | 上行 5 帧/200B | 下行 103 帧/19776B | ping=1 pong=1 | 语音轮次=2 打断=1 坏帧=0 seq跳变=0
```

## 一轮语音的下发时序（`--scenario normal`）

```
设备 → mock:  audio_start{sid}  →  [上行 0x01 二进制帧...]  →  audio_end{sid}
mock → 设备:  （--think-ms 延时，模拟云端思考）
              asr_partial{sid,text} → asr_final{sid,text} → llm_delta{sid,text} × N
              → tts_start{sid,codec:"mp3",sr:24000} → 0x02 二进制帧 × --max-tts-frames
              → tts_end{sid}
```

- `sid` 由**端侧**在 `audio_start` 里给出（ARCH §7 规则 1），mock 原样回填到所有下行事件。
- 二进制帧头 8 字节：`0x56 0x4F | ver=1 | type | seq(2B BE) | len(2B BE)`；下行 `type=0x02`，`seq` 从 0 递增。**帧头不含 `sid`**，端侧按"当前会话"关联（契约缺口见 PROTOCOL.md）。
- TTS payload 是**合成数据**（前 4 字节形似 MP3 帧同步头，其余填 0），只用于帧级联调，**不是可解码 MP3**：本机没有 ffmpeg，真实 MP3 素材在 T2.3 做解码器时再补。

## CLI 参数

| 参数 | 默认 | 说明 |
|---|---|---|
| `--host` / `--port` | `0.0.0.0` / `8765` | 监听地址与端口；`--port 0` 由系统分配（自测用） |
| `--device-id` / `--token` | 空（不校验） | 非空则校验 query 参数，不匹配即拒绝连接（关闭码 4001/4002） |
| `--scenario` | `normal` | `normal` / `error`（回 `error` 事件）/ `music`（回 `music_track`） |
| `--think-ms` | 300 | `audio_end` → 首个下行事件的延时，用于测端侧 THINKING 超时 |
| `--chunk-ms` / `--chunk-bytes` | 20 / 192 | 二进制帧间隔与合成 payload 长度（24k/32kbps 20ms ≈ 192B） |
| `--max-tts-frames` | 100 | 一轮 TTS 帧上限（100 帧 ≈ 2s） |
| `--tts-text` | 内置中文 | 模拟识别文本，同时用于 `asr_*`/`llm_delta` |
| `--no-tts` | 关 | 只发 JSON（`tts_start`/`tts_end` 仍发），不发二进制帧；P1 握手/心跳联调够用 |
| `--drop-pong` | 关 | 收到 `ping` 不回，测端侧 10s 心跳超时判定 |
| `--close-after S` | 0（关） | 到点主动断开（code 1000），测端侧断线重连 |
| `--save-upload DIR` | 空 | 把上行 payload 按 `c<连接>-sid<sid>-seq<seq>.bin` 落盘（P2 核对"真的在发音频"） |
| `--verbose` | 关 | 打印上行 payload 前 16 字节（库自身的握手 DEBUG 已压到 WARNING） |
| `--self-test` | — | 进程内自测，不需要硬件 |

## 约束与边界

- **只做开发联调工具**，不进固件、不占设备资源；零固件改动。
- 传输：本地 mock 是 **`ws://`**（不加密）；线上契约是 **`wss://`**（ARCH §6.1）。TLS 与证书验证策略（自签 CA / 开发期跳过校验）在 T1.4 决定，见 [PRODUCT_IDEAS.md](../docs/PRODUCT_IDEAS.md) §8 X2。
- 服务端**不主动**发 WS 协议层 ping（`ping_interval=None`），也**不主动**发应用层 `{"t":"ping"}`：§6.1 只定义了端侧 20s 上行心跳，避免出现两份心跳互相干扰。
- 坏帧（magic/ver/len/type 不符、非 JSON 文本帧）一律 WARN + 计数 + 丢弃，但**不断开连接**——联调时希望看到问题累积，而不是一次错误就把会话踢掉。
- `scenario=music` 只回 `music_track`，`url` 不可播放（T1.5 不提供 HTTP 音频服务，P4 前补）；`music_status` 只记日志。
- 契约缺口（`hello` 无应答、二进制帧无 `sid`、音乐事件 `sid` 归属、`error` 码表、鉴权关闭码、服务端是否需主动心跳）不在这里私自定义，统一登记在 PROTOCOL.md，P2 补 `voice_cloud` 时一并定稿。

## 后续（未做）

- 真实 MP3 素材 + 断言可解码（T2.3/T2.4）。
- HTTP 音频服务（`music_track.url` 可播放、支持 Range 请求），P4（T4.1/T4.2）前补。
- `wss` + 自签证书（T1.4 决定证书策略后）。
- 上行 Opus 帧解码校验（需 Opus 库），P2（T2.2）后可选。
