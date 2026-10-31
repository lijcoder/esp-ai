# tools/ —— 开发辅助工具

开发期脱机联调/真机自测用的工具与素材，**不进固件、不占设备资源**。

| 文件 | 用途 | 用法 |
|---|---|---|
| `ws_mock_server.py` | 本地「云端」WS 模拟服务（T1.5），设备联调不用等后端 | 见下文 |
| `lexin.wav` | "嗨，乐鑫"唤醒测试音频：电脑扬声器回放给设备麦克风，**替代人工喊话** | `afplay tools/lexin.wav`，见下文 |
| `record_wav.py` | 串口采集 WAV，用于唤醒/AEC 实测（**T5.2 计划，尚未实现**） | — |

## lexin.wav —— 唤醒测试音频

**作用**：本地唤醒链路自测时，用电脑扬声器播放这段"嗨，乐鑫"录音，让设备麦克风拾音触发 WakeNet，**不需要每次由人喊唤醒词**（agent 测试时也无法喊话）。

| 属性 | 值 |
|---|---|
| 采样率 / 位深 / 声道 | 24000 Hz / 16 bit PCM / mono |
| 时长 / 大小 | 1.28 s / 61,484 B |
| 内容 | "嗨，乐鑫"（对应设备 WakeNet9 模型 `wn9_hilexin`） |

**音量（已按用户听感定稿）**：回放时**保持系统输出音量不变（本机 31%）**、`afplay` **默认增益**。真机实测（2026-10-31，ESP32-S3-N16R8，设备静置桌面，共 7/7 唤醒、链路完整）：第一次 系统 70% + `afplay -v 1.5` → 3/3 唤醒但**太响，已废弃**；第二次 系统 31% → 4/4 唤醒（2 次 `-v 1.5` + 2 次默认增益），**用户确认音量正好，以后按此音量**（推荐默认增益；实测上限 `-v 1.5`，不得调高系统音量）。

**用法**：

```bash
# 终端 A：观察设备日志（注意：idf.py monitor 会复位设备，等初始化/联网完成后再回放）
idf.py -p /dev/cu.usbmodem5CF90746951 monitor

# 终端 B：回放（保持系统音量不变；本机 31% + 默认增益即合适，不要加 -v、不要调系统音量）
afplay tools/lexin.wav
```

**预期日志**（回放后 ~0.1 s 内唤醒，随后播放 1.28 s 提示音）：

```
I (22470) APP: wake detected
I (22470) AFE: feed paused (half-duplex)      # R1：播放前暂停收音
I (22470) PLAYER: play 12800 samples @16000 Hz (vol=60)
I (23820) AFE: feed resumed                   # R2：播放结束恢复收音
I (23820) APP: prompt done, feed resumed
```

**注意事项**：

- 设备在播放提示音/音频期间再次回放会被忽略（`APP: player busy, ignore wake`）；连续回放间隔建议 ≥2 s。
- 24 kHz 素材经空气传到设备、设备按 16 kHz 采集，音高/语速正常，**不影响唤醒**：直接回放原文件，不要为 16 kHz 重采样。
- 无唤醒排查顺序：macOS 静音/输出路由（`osascript -e 'get volume settings'`）→ 电脑与设备距离/朝向（扬声器对准设备麦克风）→ 设备是否在播放 → `mic stat` 电平是否正常（安静室内底噪 RMS 约 40~300）。
- 回放会**外放声音**，仅在用户知情/同意时使用；**音量按用户定稿：系统音量保持 31%（不变）+ `afplay` 默认增益**，`-v 1.5` 已是上限，禁止调高系统音量（70% + `-v 1.5` 实测过响，用户已明确否决）。

---

## ws_mock_server.py —— 本地「云端」WS 模拟服务（T1.5）

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
# [1/9] PASS  hello / hello_ack 握手与参数解析
# ...
# self-test: 9/9 PASS   退出码 0
```

进程内起服务 + 起客户端，覆盖 9 项：`hello`/`hello_ack` 握手、ping→pong、一轮语音的事件顺序/`sid` 回填/二进制帧头、坏帧容错、`interrupt` 取消、断线重连、TTS 流中 ping、token 拒绝关闭码、`--drop-pong`。

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
< {"t":"hello_ack","ver":1}       # ARCH §6.1（T1.4 定稿）：服务端必须回 ack
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
- T1.4 已定稿的 4 条契约（`hello_ack`、鉴权关闭码 4001/4002、`pong` 字段与 RTT 口径、`ws://`/`wss://` 双传输）已同步到 ARCHITECTURE §6.1/§6.3 与本脚本，见 PROTOCOL.md「已定稿口径」；剩余缺口（二进制帧无 `sid`、音乐事件 `sid` 归属、`error` 码表）继续登记在 PROTOCOL.md，P2 补 `voice_cloud` 时定稿。

## 后续（未做）

- 真实 MP3 素材 + 断言可解码（T2.3/T2.4）。
- HTTP 音频服务（`music_track.url` 可播放、支持 Range 请求），P4（T4.1/T4.2）前补。
- `wss` + 自签证书（T1.4 决定证书策略后）。
- 上行 Opus 帧解码校验（需 Opus 库），P2（T2.2）后可选。
