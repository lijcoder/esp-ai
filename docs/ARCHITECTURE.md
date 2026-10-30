# 智能语音硬件 —— 系统架构（v1.0，已确定方案）

> 适用范围：ESP32-S3-N16R8 + INMP441 麦克风 + MAX98357A 功放 + 3020 喇叭
> 文档定位：**本文件只描述已经确定、需要实现的方案**。尚未纳入的功能统一放在文末「附录 A」。
> 文档索引：本文 = 架构决策；[ROADMAP.md](ROADMAP.md) = 任务拆分与验收；[PRODUCT_IDEAS.md](PRODUCT_IDEAS.md) = 待决策方向。
> 硬件接线、电气参数、自检清单见 [HARDWARE.md](HARDWARE.md)。云端服务端由后端实现，端侧按第 6 节契约对接。

---

## 1. 功能范围（确定）

| 功能 | 实现方式 |
|---|---|
| 本地唤醒 | ESP-SR WakeNet9，唤醒词"嗨，乐鑫"（`wn9_hilexin`） |
| 唤醒应答 | 播放本地提示音（assets 分区，如"我在呢"） |
| 语音对话 | 唤醒 → 上行 Opus 音频 → 云端 ASR/LLM → TTS(MP3) 流式下行 → 边下边播 |
| 打断 | 按键打断（半双工，播放中不听麦克风） |
| 音乐播放 | MP3 播放；端侧同时支持 HTTP(S) 流式与本地文件两种入口；曲目来源后续接入 |
| 状态指示 | WS2812 状态灯 + 本地提示音 |
| 交互按键 | 短按：打断/开始对话；长按：静音/恢复 |
| 调试 | esp_console 命令行 + 分级日志 + 堆/任务监控 |
| 网络 | WiFi STA，账号密码编译期写死；断线自动重连 |

当前版本明确不做：配网、OTA、屏幕与 UI、AEC/语音打断、定制唤醒词、SD 卡、电池与低功耗、离线命令词。这些在附录 A 列出预留点。

---

## 2. 已确定的硬件约束

| 项目 | 确定值 | 对软件的影响 |
|---|---|---|
| 主控 | ESP32-S3，240MHz，16MB Flash（QIO 80MHz），8MB Octal PSRAM 80MHz | 音频大缓冲全部放 PSRAM |
| 麦克风 | INMP441 接 `I2S_NUM_0` RX：**16kHz**、32bit 槽位、单声道、左声道（L/R 接 GND） | AFE 输入固定 16k；代码中 `>>16` 取高 16bit |
| 功放 | MAX98357A 接 `I2S_NUM_1` TX：**48kHz**、16bit、立体声（单声道数据复制到 L/R） | 所有播放音频统一重采样到 48k |
| 回采参考 | **无**（麦克风只有数字输出，功放只有数字输入） | 无 AEC，当前采用半双工；AFE 输入格式固定 `"M"` |
| 音量控制 | 无数字音量（MAX98357A 增益由 SD_MODE 电阻固定） | 软件 16bit 数字音量 + 软限幅 |
| 拾音 | 单麦全向，远场约 2~3m | 唤醒阈值按实测标定 |
| 引脚 | MIC: 5/6/4；AMP: 41/42/40；LED: 48；按键: 0 | 避开 26-37/19-20/43-44 |
| 内部 RAM | 当前固件 DIRAM 已用 137.6KB / 341.8KB（2026-10-30，含 coredump） | 设计目标：峰值占用 ≤ 310KB，保留 ≥ 30KB 内部堆 |
| 分区 | 见第 9.1 节（定稿） | app 双槽位预留给后续 OTA，代码当前版本不实现 |

**结论**：音频链路为 16k 采集 + 48k 播放的双时钟结构；当前不实现 AEC，采用半双工。

---

## 3. 音频链路设计（确定）

### 3.1 采集

```
INMP441 ──I2S0 RX 16k──> audio_io(32bit→int16) ──> audio_fe 环形输入
                                                        │
                                    AFE: NS + VAD + WakeNet（无 AEC）
                                                        │
                        ┌───────────────────────────────┴──────────────┐
                        ▼                                              ▼
                唤醒事件 / VAD 事件                          fetch 16k 单声道音频
                        │                                              │
                        ▼                                              ▼
                  event_bus                                   codec_enc(Opus 16kbps)
```

- AFE 配置：`afe_config_init("M", models, AFE_TYPE_SR, AFE_MODE_LOW_COST)`；
- 模型：`wn9_hilexin` + `vadnet1_medium`（已在 model 分区）；
- 采集分帧由 AFE `get_feed_chunksize()` 决定，采集任务只做 I2S 读 + 格式转换 + `feed()`。

### 3.2 播放

```
TTS(MP3) ─┐
提示音(PCM)├─> 三通道 ringbuffer(PSRAM) ─> 解码/重采样48k ─> 混音 ─> 音量 ─> 软限幅 ─> I2S1 TX 48k
音乐(MP3) ─┘                                                    (软件音量 0~100)
```

- 三个独立通道：`CH_TTS`、`CH_PROMPT`、`CH_MUSIC`，优先级 TTS > PROMPT > MUSIC；
- 混音为饱和加法（16bit 限幅），音乐在语音播放时 duck 到 -12dB；
- 淡入 10ms / 淡出 100ms，防止最大音量下切换产生 pop；
- 软限幅器上限 -1dBFS，避免 3W 功放削波失真。

### 3.3 半双工规则（明确约定）

| 规则 | 内容 |
|---|---|
| R1 | 进入 `SPEAKING` 时立即 `audio_fe_set_feed_enabled(false)`，麦克风数据丢弃（不送 AFE） |
| R2 | `TTS_END` 后延迟 **300ms** 恢复 feed，吸收喇叭尾音与功放余响 |
| R3 | 音乐播放不关闭麦克风（保持唤醒检测）；唤醒命中后 100ms 内把音乐 duck 到 -12dB |
| R4 | 按键打断：任意状态 → 播放通道 100ms 淡出 → 取消云端会话 → 恢复 feed → 回到 `IDLE` |
| R5 | 恢复 feed 后丢弃最早 50ms 的音频，避免打断瞬间的噪声触发 VAD |

### 3.4 提示音

- 存放在 `assets` 分区（PCM16 / 16k 单声道），启动时加载索引；
- 播放时重采样到 48k，走 `CH_PROMPT`；
- 唤醒应答"我在呢"、错误提示、按键音为三组固定资源；
- 提示音播放中若 TTS 到达，挂起提示音，TTS 优先。

### 3.5 音量与限幅

- 音量 0~100，默认 60，存 NVS（`config_store`）；
- 软件音量：`sample * vol / 100`，静音即 0；
- 限幅：混音后超过 -1dBFS 时按比例压回（软限幅，无削波）；
- 音量变化时 20ms 线性插值，避免突变。

### 3.6 上行编码（确定）

- 编码：Opus，16kHz、单声道、16kbps、20ms 一帧（320 采样）；
- 帧头 8 字节（见 6.2），seq 单调递增；
- VAD 判定静音持续 800ms 或累计收音 8s 时发送 `audio_end`；
- 上行带宽约 2KB/s，WiFi 抖动下仍稳定。

### 3.7 下行编码（确定）

- TTS 与音乐统一使用 **MP3** 解码（端侧只实现 MP3 一种解码器）；
- TTS：24kHz 单声道（服务端按契约输出）；
- 音乐：MP3，44.1k/48k 单声道或立体声，解码后重采样到 48k 输出；
- 流式播放：收到首帧后启动解码任务，预缓冲 200ms 数据再出声，防抖动。

---

## 4. 分层架构与模块（确定）

```
┌─────────────────────────────────────────────────────────┐
│ L4 业务编排   voice_session（会话状态机） music_session    │
├─────────────────────────────────────────────────────────┤
│ L3 能力       voice_cloud  music_cloud  dev_ctrl  diag   │
├─────────────────────────────────────────────────────────┤
│ L2 音频引擎   audio_fe  audio_player  codec_enc          │
├─────────────────────────────────────────────────────────┤
│ L1 基础服务   net_mgr  transport_ws  config_store  event_bus │
├─────────────────────────────────────────────────────────┤
│ L0 板级/驱动  board  audio_io                            │
└─────────────────────────────────────────────────────────┘
```

> **L 指 Layer（调用层级）**：数字越大越靠上层。依赖方向只能从大编号指向小编号（L4→L3→L2→L1→L0），禁止反向依赖或跨层直连；同层之间通过 `event_bus`/队列通信。`assets` 是纯资源组件，不参与分层。

### 4.1 模块清单

| 模块 | 层级 | 确定职责 | 对外接口 |
|---|---|---|---|
| `board` | L0 板级/驱动 | 引脚定义、按键、初始化顺序（LED 待 `dev_ctrl` 任务接入） | `board_init()`, `board_button_pressed()` |
| `audio_io` | L0 板级/驱动 | I2S0 RX（16k 采集）/ I2S1 TX（48k 播放）HAL | `audio_io_init()`, `audio_io_read_mic()`, `audio_io_write_spk()`, `audio_io_spk_enable()` |
| `config_store` | L1 基础服务 | NVS 读写（音量、设备状态）+ 独占 `nvs_flash_init`；编译期常量（WiFi/WS/设备参数）走 Kconfig | `cfg_init()`, `cfg_get_int()`, `cfg_get_int_or()`, `cfg_set_int()`, `cfg_get_str()`, `cfg_set_str()`, `cfg_get_volume()`, `cfg_set_volume()`, `cfg_erase_key()` |
| `net_mgr` | L1 基础服务 | WiFi STA 连接、退避重连、SNTP 对时、网络事件（无独立任务：事件回调 + 一次性定时器） | `net_init()`, `net_is_up()`, `net_wait_ready(ms)`, `net_is_time_synced()`, `net_get_info()`, `net_reconnect()` |
| `transport_ws` | L1 基础服务 | wss 客户端：连接/重连/心跳/发送队列/二进制帧收发 | `ws_start()`, `ws_send_text()`, `ws_send_bin()`, `ws_register_cb()` |
| `event_bus` | L1 基础服务 | 全局事件定义与分发（基于 esp_event 独立循环，任务/队列可控） | `app_evt_init()`, `app_evt_register()`, `app_evt_unregister()`, `app_evt_post()`, `app_evt_name()` |
| `audio_fe` | L2 音频引擎 | AFE 封装：feed 开关、唤醒/VAD 回调（上行音频读取随 P2 增加） | `audio_fe_init(cb)`, `audio_fe_start()`, `audio_fe_set_feed_enabled()`, `audio_fe_wakenet_name()` |
| `audio_player` | L2 音频引擎 | 播放引擎：PCM 播放 + 重采样到 48k + 软件音量 + 淡入淡出（多通道/MP3/HTTP 随 P2–P4 增加） | `audio_player_init()`, `audio_player_play_pcm()`, `audio_player_register_done_cb()`, `audio_player_set_volume()` |
| `codec_enc` | L2 音频引擎 | Opus 编码（16k/16kbps/20ms） | `enc_open()`, `enc_encode(pcm, out, len)`, `enc_close()` |
| `voice_cloud` | L3 能力 | 语音云协议：会话建立、上行音频、下行事件解析与分发 | `vc_start()`, `vc_send_audio()`, `vc_cancel()`, `vc_register_cb()` |
| `music_cloud` | L3 能力 | 曲目请求与播放状态上报协议 | `mc_request(next/prev/id)`, `mc_report(state, progress)` |
| `dev_ctrl` | L3 能力 | 按键事件（短按/长按）、LED 状态模式、静音切换 | `dev_init()`, `dev_register_cb()` |
| `diag` | L3 能力 | esp_console 命令、日志级别控制、堆/任务周期监控 | `diag_init()` |
| `voice_session` | L4 业务编排 | 会话状态机 + 半双工调度 + 超时重试 | `vs_init()`, `vs_start()`, `vs_interrupt()`, `vs_get_state()` |
| `music_session` | L4 业务编排 | 音乐播放控制：播放/暂停/继续/停止/切歌；与语音抢占 | `ms_init()`, `ms_play()`, `ms_pause()`, `ms_resume()`, `ms_stop()` |
| `assets`（临时） | 资源（不分层） | 内置提示音 PCM 资源；P3（T3.5）迁到 `assets` 分区后删除该组件 | `prompt_wo_zai_ne_pcm[]` |

### 4.2 事件总线（确定）

```c
ESP_EVENT_DECLARE_BASE(APP_EVT);   /* event_bus.h；定义在 event_bus.c */

typedef enum {
    APP_EVT_ANY = -1,              /* 仅用于注册：订阅全部应用事件 */
    APP_EVT_NET_UP, APP_EVT_NET_DOWN,
    APP_EVT_WAKE_DETECTED,
    APP_EVT_VAD_SPEECH_START, APP_EVT_VAD_SPEECH_END,
    APP_EVT_ASR_PARTIAL, APP_EVT_ASR_FINAL,
    APP_EVT_TTS_START, APP_EVT_TTS_END,
    APP_EVT_VOICE_STATE_CHANGED,
    APP_EVT_BUTTON,
    APP_EVT_MUSIC_STATE_CHANGED, APP_EVT_MUSIC_ERROR,
    APP_EVT_COUNT,                 /* 哨兵：事件数量 */
} app_evt_t;
```

**payload**：统一装进定长 32 字节的 `app_evt_data_t`（union），投递时整块拷贝，发布方无需保活；处理器拿到的指针只在回调内有效。P1 只定义网络事件，其余事件投递 `NULL`，由使用方落地时补定义（文本类 payload 的所有权语义随 T2.5/T3.1 定稿）：

| 事件 | payload 结构 |
|---|---|
| `APP_EVT_NET_UP` | `app_evt_net_up_t { char ip[16]; int8_t rssi; }` |
| `APP_EVT_NET_DOWN` | `app_evt_net_down_t { uint8_t reason; uint32_t retry_ms; }` |

**投递与线程约束**：

- 独立循环不占系统 default loop（避免与 WiFi/`esp_netif` 处理器互相阻塞），队列长度 `CONFIG_EVENT_BUS_QUEUE_SIZE`（默认 16），投递一律非阻塞（`ticks_to_wait = 0`），满则丢弃 + WARN；
- 处理器在 `evt` 任务上下文执行，不得做耗时/阻塞操作；
- **音频实时任务（`afe_feed`/`afe_fetch`）只能投递无 payload 事件**：esp_event 带 payload 时会内部 `calloc` 拷贝，违反 §4.3 第 2 条。

### 4.3 依赖规则（强制）

1. 只允许上层依赖下层；同层通过 `event_bus` 通信。
2. 音频任务内禁止网络、Flash、malloc、阻塞等待。
3. 每个组件只暴露 `include/<module>.h`，其余符号内部可见。
4. 大块缓冲一律 `heap_caps_malloc(..., MALLOC_CAP_SPIRAM)`；I2S DMA 缓冲必须内部 RAM。
5. 每个组件带 `Kconfig`，禁止把可调参数硬编码在 `.c` 里。

---

## 5. 项目结构（确定）

```
esp-ai/
├── CMakeLists.txt
├── partitions.csv                  # 第 9.1 节定稿
├── sdkconfig.defaults
├── sdkconfig.defaults.esp32s3
├── main/
│   ├── CMakeLists.txt
│   ├── app_main.c                  # 初始化编排（≤150 行）
│   └── Kconfig.projbuild           # WiFi/服务器/设备参数
├── components/
│   ├── board/
│   ├── audio_io/
│   ├── audio_fe/
│   ├── audio_player/               # decoder_mp3.c / resample.c / mixer.c / source_http.c / source_file.c
│   ├── assets/                     # 临时：内置提示音 PCM（P3 迁到 assets 分区后删除）
│   ├── codec_enc/
│   ├── config_store/
│   ├── net_mgr/
│   ├── transport_ws/
│   ├── event_bus/
│   ├── voice_cloud/
│   ├── music_cloud/
│   ├── voice_session/
│   ├── music_session/
│   ├── dev_ctrl/
│   └── diag/
├── docs/
│   ├── ARCHITECTURE.md             # 本文档
│   ├── HARDWARE.md
│   └── PROTOCOL.md                 # 第 6 节契约的详细版（后续实现时同步补全）
├── tools/
│   ├── ws_mock_server.py           # 本地云端模拟（开发联调用）
│   ├── audio_pack.py               # 提示音格式转换与 assets 打包
│   └── record_wav.py               # 串口采集 WAV，用于调唤醒/AEC 实测
└── test/host/                      # 帧头解析、状态机、协议编解码单测
```

### 5.1 现有 POC 迁移映射（确定）

| 现有文件/函数 | 迁入 |
|---|---|
| `rx_init()` / `tx_init()` | `components/board`（引脚）+ `components/audio_io` |
| `feed_Task()` | `components/audio_fe` 内部采集任务 |
| `detect_Task()` | `components/audio_fe` 回调 → `event_bus` |
| `play_voice_task()` | `components/audio_player` 的 `CH_PROMPT` 路径 |
| `voice_data.c` | 转为 `components/assets`（临时内置），P3 迁到 `assets` 分区 |
| `app_main()` | `main/app_main.c` 初始化编排 |

迁移后 `main/` 只保留组装逻辑，原 `esp-ai.c` 删除。

---

## 6. 云端对接契约（端侧实现，服务端按此对接）

### 6.1 连接与鉴权

- 地址：`wss://<host>/v1/voice?device_id=<id>&token=<token>&ver=1`
- 参数来自 Kconfig：`CONFIG_AI_WS_URL`、`CONFIG_AI_DEVICE_ID`、`CONFIG_AI_TOKEN`
- 连接成功后发送 `{"t":"hello","device_id":..,"ver":1}`
- 心跳：每 20s 发 `{"t":"ping"}`，10s 内未收到 `pong` 判定断线
- 重连退避：1s → 2s → 5s → 10s → 30s（上限），无限重试直到网络恢复

### 6.2 二进制帧（音频）

```
 0    1    2    3    4    5    6    7
+----+----+----+----+----+----+----+
|0x56|0x4F|ver |type| seq(2B BE) | len(2B BE) |
+----+----+----+----+----+----+----+
| payload: 编码音频数据 ...          |
```

| type | 方向 | 内容 |
|---|---|---|
| 0x01 | ↑ | 上行麦克风音频（Opus 16k/16kbps/20ms） |
| 0x02 | ↓ | 下行 TTS 音频（MP3 24k 单声道） |

### 6.3 文本帧（JSON）

**上行**
| t | 说明 |
|---|---|
| `hello` | 连接后握手，带 device_id/ver |
| `audio_start` | 开始一轮上行，带 `sid` |
| `audio_end` | 上行结束（VAD 静音或超时） |
| `interrupt` | 用户打断，带 `sid` |
| `ping` | 心跳 |
| `music_request` | 请求曲目：`{"t":"music_request","action":"play","id":..,"index":..}` |
| `music_status` | 播放状态上报：`{"t":"music_status","state":"playing","progress_ms":..}` |

**下行**
| t | 说明 |
|---|---|
| `asr_partial` / `asr_final` | ASR 中间/最终结果，带 `sid`、`text` |
| `llm_delta` | LLM 增量文本，带 `sid`、`text` |
| `tts_start` / `tts_end` | TTS 起止，带 `sid`、`codec`、`sr` |
| `music_track` | 曲目信息：`{"t":"music_track","sid":..,"url":"https://...mp3","codec":"mp3","duration_ms":..,"title":..}` |
| `error` | 错误码与描述 |
| `pong` | 心跳应答 |

规则：所有下行事件都带 `sid`；端侧丢弃 `sid` 不匹配当前会话的迟到帧。

### 6.4 时延目标（端侧可达性约定）

| 指标 | 目标 |
|---|---|
| 唤醒 → 上行首帧 | < 200ms |
| TTS 首帧 → 出声 | < 150ms |
| 按键打断停声 | < 100ms |
| 音乐起播（首帧到出声） | < 1.5s |
| 连续播放卡顿率 | < 0.1% |

---

## 7. 会话状态机（确定）

| 状态 | 行为 | 进入 | 退出 |
|---|---|---|---|
| `IDLE` | 只听唤醒，音乐可播放 | 上电 / 会话结束 | 唤醒 → `LISTENING`；按键 → `LISTENING` |
| `LISTENING` | 上行音频，VAD 判停 | 唤醒/按键 | 静音 800ms 或 8s 超时 → `THINKING` |
| `THINKING` | 等 ASR/TTS，播放提示音可配置 | `LISTENING` 结束 | `tts_start` → `SPEAKING`；10s 超时 → `ERROR` |
| `SPEAKING` | 播放 TTS，关闭麦克风输入（R1） | `tts_start` | `tts_end` → 延时 300ms 恢复 feed → `IDLE`；按键 → `IDLE` |
| `ERROR` | 播放错误提示音，上报日志 | 超时/云端 error | 提示音结束 → `IDLE`（同轮不重试） |

规则：
1. 每轮会话 `sid` 自增（uint16），异步帧必须匹配当前 `sid`；
2. 状态迁移统一经 `voice_session`，其它模块不得直接改状态；
3. 所有迁移打 INFO 日志（`voice` tag），包含 `sid` 与停留时长；
4. `ERROR` 状态不自动重试，由用户再次唤醒触发新一轮。

---

## 8. 关键流程

### 8.1 一轮语音对话

```
IDLE ──唤醒──> LISTENING ──VAD静音──> THINKING ──tts_start──> SPEAKING ──tts_end──> IDLE
   │                │                      │                      │
 wake 事件      上行Opus→WS            ASR/LLM事件             TTS MP3帧→解码→48k→喇叭
```

### 8.2 按键打断（半双工）

```
任意状态 ──按键──> player fade_out(100ms) → 发送 interrupt → 作废当前 sid
                → audio_fe_set_feed_enabled(true) → 丢弃 50ms → IDLE
                                                 （再按一次进入 LISTENING）
```

### 8.3 音乐播放

```
music_session ──mc_request──> 服务端 ──music_track{url}──> player_play_uri(CH_MUSIC, url)
        │                                                        │
   music_status 每 5s 上报                              HTTP 流式拉取 → MP3 解码 → 48k
        │                                                        │
        └──────────── 暂停/继续/停止/切歌 ◄──────────────────────┘
```

### 8.4 音乐播放中唤醒

```
音乐播放中 ──WakeNet 命中──> 音乐 duck(-12dB, 100ms) ──> LISTENING
                                  │（会话结束）
                                  ▼
                        音乐恢复原音量；若用户说"暂停音乐"由云端指令处理
```

### 8.5 断线重连

- `net_mgr` 检测到断开 → `APP_EVT_NET_DOWN`（**只在已联网后掉线时投递**；上电首次连不上不投，避免上层处理正常中间态），payload 带驱动原因码与本次退避延时；
- 正在进行的会话立即终止，播放淡出，回 `IDLE`；
- 自动重连成功后重新 `ws_start()` + `hello`；
- 重连期间唤醒仍可用（本地唤醒不依赖网络），进入 `LISTENING` 时若网络未恢复则播"网络未连接"提示音并回 `IDLE`。

---

## 9. 分区与资源

### 9.1 分区表（定稿）

| Name | Type | SubType | Offset | Size | 用途 |
|---|---|---|---|---|---|
| nvs | data | nvs | 0x9000 | 24K | 音量、设备状态 |
| otadata | data | ota | 0xf000 | 8K | 预留给后续 OTA |
| phy_init | data | phy | 0x11000 | 4K | RF 校准 |
| ota_0 | app | ota_0 | 0x20000 | 3M | 当前运行固件 |
| ota_1 | app | ota_1 | 0x320000 | 3M | 预留 OTA 槽位（当前版本不用） |
| model | data | spiffs | 0x620000 | 1536K | srmodels（WakeNet + VAD） |
| assets | data | spiffs | 0x7a0000 | 2M | 提示音、默认配置 |
| coredump | data | coredump | 0x9a0000 | 64K | 崩溃现场（已启用落盘：`CONFIG_ESP_COREDUMP_ENABLE_TO_FLASH=y`，`idf.py coredump` 解析） |
| storage | data | fat | 0x9b0000 | 6400K | 本地音乐/缓存/日志 |

说明：分区表一次定稿，app 双槽位为后续 OTA 预留（当前版本不实现 OTA 代码）；尾部预留 64KB。

### 9.2 内存预算

| 资源 | 分配 |
|---|---|
| 内部 RAM | TLS ~45KB、WS ~24KB、I2S DMA、任务栈、编码器状态；峰值 ≤ 310KB，保留 ≥ 30KB |
| PSRAM | AFE 缓冲 ~200KB、播放 ring 1MB（三通道合计）、HTTP 缓冲 128KB、解码器状态、预留 4MB+ |
| Flash | app 2×3M（一槽使用）、model 1.5M、assets 2M、storage 6.25M |

### 9.3 任务与核心（确定）

| 任务 | 核 | 优先级 | 栈 | 职责 |
|---|---|---|---|---|
| `afe_feed` | 1 | 8 | 4KB | I2S 读 + AFE feed（实时性最高） |
| `afe_fetch` | 1 | 6 | 4KB | 唤醒/VAD/上行音频输出 |
| `player` | 1 | 5 | 6KB | MP3 解码 + 重采样 + 混音 + I2S 写 |
| `evt` | 0 | 5 | 4KB | 事件分发（`event_bus` 自建 esp_event 循环，T1.2a 起） |
| `ws` | 0 | 5 | 6KB | WebSocket/TLS 收发 |
| `music_http` | 0 | 4 | 6KB | HTTP 拉流 |
| `voice_session` | 0 | 4 | 4KB | 状态机事件循环 |
| `dev` | 0 | 3 | 3KB | 按键扫描 + LED 驱动 |
| `diag` | 0 | 1 | 4KB | console + 周期监控 |

原则：核 1 只跑音频路径，不受网络抖动影响；跨核只通过队列/ringbuffer。

> 上表是**应用任务**；IDF 库任务（`sys_evt`、`wifi`、`esp_timer`、`Tmr Svc`、`IDLE`、`ipc`）不在此表。
> `net_mgr` 的 WiFi/IP 事件处理器运行在 `sys_evt` 任务上，其栈由 `CONFIG_ESP_SYSTEM_EVENT_TASK_STACK_SIZE=4096` 提供（P1 T1.3 实测默认 2304 B 时只剩 380 B，已加栈）。

---

## 10. 网络与调试（确定）

### 10.1 WiFi

- STA 模式，SSID/密码走 Kconfig（`CONFIG_AI_WIFI_SSID` / `CONFIG_AI_WIFI_PASSWORD`），编译进固件；驱动存储用 `WIFI_STORAGE_RAM`，避免 NVS 里旧凭证覆盖配置；
- 上电自动连接，失败重试；运行中断线自动重连（退避 1s→30s，无限重试）；
- 关闭 modem sleep（`WIFI_PS_NONE`）：语音要低时延，避免省电引入上下行抖动；
- 开启 SNTP（服务器 `CONFIG_NET_SNTP_SERVER`，默认 `ntp.aliyun.com`），同步时间用于 TLS 证书校验；**联网成功（GOT_IP）时立即触发对时**，并关闭 lwIP 首次请求的随机启动延迟（`CONFIG_LWIP_SNTP_STARTUP_DELAY=n`），实测对时落在地址就绪后 ~40ms（改前最坏 33s）；
- 未配置 SSID 时 `net_mgr` 降级为离线（只 WARN），本地唤醒/播放不受影响，`transport_ws` 会因 `net_is_up()` 为 false 而拒绝启动；
- WiFi 与 PSRAM 同时使用，注意 `CONFIG_ESP32S3_DATA_CACHE_64KB` 已满足（现有配置）。

### 10.2 调试能力（diag + esp_console）

串口 115200（UART0，GPIO43/44），`idf.py monitor` 直接可用。确定命令集：

| 命令 | 作用 |
|---|---|
| `help` | 列出所有命令 |
| `sys info` | 版本、复位原因、运行时长、内存水位 |
| `sys heap` | 内部/PSRAM 剩余与碎片 |
| `sys tasks` | 任务栈水位、CPU 占用 |
| `sys reboot` | 软复位 |
| `log <tag> <level>` | 运行时调整日志级别 |
| `wifi status` / `wifi reconnect` | 查看/重连 WiFi |
| `net ping <host>` | 网络连通性 |
| `mic stat` | 麦克风 RMS/峰值、VAD 状态 |
| `cfg info` / `cfg get\|set\|setstr\|erase <key>` | 配置持久化调试：NVS 条目用量、原始值读写、删除键恢复缺省（存储层语义，不做业务校验） |
| `evt list` / `evt post <name\|id>` / `evt burst <n>` / `evt trace on\|off` | 事件总线观测：事件清单、投递（网络事件带示例 payload）、突发投递验证非阻塞/队列满、订阅打印分发结果 |
| `audio vol <0-100>` | 设置音量 |
| `audio play <file\|url>` | 手测播放 |
| `audio stop` | 停止播放 |
| `voice state` / `voice start` / `voice interrupt` / `voice wake on\|off` | 会话调试 |
| `music play <url>` / `music pause` / `music resume` / `music stop` | 音乐调试 |

日志 TAG 固定：`BOARD`、`AUDIO_IO`、`AFE`、`PLAYER`、`ENC`、`NET`、`WS`、`VC`、`VS`、`MC`、`MS`、`DEV`、`DIAG`、`CFG`、`EVT`、`APP`（`APP` 属 `main/` 启动编排，`EVT` 属 `event_bus` 基础服务，均不占业务层级）。

---

## 附录 A：暂缓功能与架构预留点

以下功能当前版本不实现，架构已预留接入点，后续实现时按本文档对应章节扩展即可。各项评估见 [PRODUCT_IDEAS.md](PRODUCT_IDEAS.md)。

| 功能 | 架构预留点 |
|---|---|
| **配网**（BLE/SoftAP） | `net_mgr` 的凭证来源抽象为 `wifi_cred_provider`；当前实现为 Kconfig 常量 |
| **OTA** | 分区表已含 `ota_0/ota_1/otadata`；`ota_svc` 组件未创建 |
| **屏幕 / UI** | 未纳入；可新增 `ui` 组件（LVGL），消费 `event_bus` 事件 |
| **定制唤醒词** | `audio_fe` 模型名走 Kconfig（当前固定 `wn9_hilexin`） |
| **音乐源接入** | `audio_player` 已抽象 `source_http` / `source_file`；`music_cloud` 协议已定义 |
| **AEC / 语音打断** | AFE 支持 `"MR"`/`"MMR"` 显式参考通道；`audio_fe` 预留参考通道拼接入口；样板可加第二颗 INMP441 |
| **SD 卡本地音乐** | `audio_player` 的 `source_file` 可直接指向 FATFS 挂载点 |
| **电源 / 电池管理** | 预留 `power_mgr` 组件位置与 `BAT_ADC` 引脚（GPIO1） |
| **离线命令词** | model 分区有 1.5MB 余量，可加 MultiNet 模型 |

## 附录 B：待定输入（不阻塞端侧开发）

| 事项 | 现状 | 影响范围 |
|---|---|---|
| 音乐源形态（曲库直链 / SD 卡） | 端侧播放器已同时支持 URL 与本地文件，具体源后续接入 | 仅 `music_cloud` 请求字段与资源准备 |
| 云端服务端实现 | 端侧契约已定（第 6 节），服务端按契约实现 | 无端侧改动 |
| 设备鉴权方式 | 当前用 Kconfig 写死的 `device_id` + `token` | 后续可换成动态下发，仅改 `config_store` 与握手 |
