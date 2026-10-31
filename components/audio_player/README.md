# audio_player —— 播放引擎（L2 音频引擎层）

**作用**：把任意采样率的单声道 PCM（或一段 MP3）变成 48kHz 立体声输出，负责解码、音量、淡入淡出、尾音处理，并在播放结束后通知业务层。上层（`main`、后续 `voice_cloud`/`voice_session`）不关心音频搬运动作。

## 当前能力

- 独立播放任务（核 1、优先级 5、栈 6KB），任务队列深度 4；
- **PCM 播放**：线性重采样到 48kHz（如提示音 16k → 48k），单声道复制到 L/R；
- **MP3 播放（T2.3）**：vendored [minimp3](third_party/minimp3/README.md)（CC0）解码 → 双声道取 (L+R)/2 → 单声道 PSRAM 缓冲 → 与 PCM 同一条重采样/音量/淡入淡出链路；
- 软件音量 0~100（MAX98357A 无数字音量，增益由 SD 电阻固定）；
- 淡入 10ms / 淡出 100ms，混音后限幅（防 3W 功放削波）；
- 播放结束：100ms 静音冲刷 + 150ms 延时 + 关 TX 时钟；
- 完成回调带 300ms 尾部延时（半双工规则 R2 用它恢复 `audio_fe` 收音）；
- 播放/解码统计（`audio_player_stats_read()`），供 `diag` 的 `player stat` 观测。

## 对外接口

| 接口 | 说明 |
|---|---|
| `audio_player_init()` | 初始化队列/播放任务；同时预分配 MP3 解码状态与 PCM 缓冲（PSRAM） |
| `audio_player_play_pcm(pcm, samples, rate)` | 播放一段 PCM（调用方保证数据生命周期） |
| `audio_player_play_mp3(mp3, len)` | 播放一段 MP3 码流：播放任务内整段解码后播（调用方保证码流生命周期；同一时刻只允许一段） |
| `audio_player_mp3_ready()` | MP3 能力是否可用（PSRAM 分配失败时降级，提示音链路不受影响） |
| `audio_player_register_done_cb(cb, arg)` | 注册播放完成回调（任务上下文，勿耗时） |
| `audio_player_is_busy()` | 是否正在播放（含解码阶段与排队） |
| `audio_player_set_volume(v)` / `get_volume()` | 软件音量 0~100 |
| `audio_player_stats_read(out)` / `stats_reset()` | 播放统计 + 最近一次 MP3 解码统计 |
| `audio_player_mp3_pcm_read(dst, off, n)` | 开发期探针：读取最近一次解码 PCM，供 PC 侧与 FFmpeg 逐样本对拍 |

## 配置（Kconfig）

| 配置 | 默认 | 说明 |
|---|---|---|
| `AUDIO_PLAYER_DEFAULT_VOLUME` | 60 | 默认音量 |
| `AUDIO_PLAYER_TAIL_DELAY_MS` | 300 | 完成回调前的尾部延时（R2） |
| `AUDIO_PLAYER_ENABLE_MP3` | y | 启用 MP3 解码（关闭后 `play_mp3` 返回 `ESP_ERR_NOT_SUPPORTED`，固件约省 40KB Flash） |
| `AUDIO_PLAYER_MP3_PCM_MAX_BYTES` | 1572864（1.5MB） | MP3 解码输出 PCM 缓冲上限（PSRAM 预分配；24k 约 32s / 44.1k 约 17s） |

## 依赖

`audio_io`（写 I2S）、`esp_timer`（解码/播放耗时统计）；MP3 解码器源码在 `third_party/minimp3/`（vendored，不依赖组件管理器）。

## 内存与实时约束（CONVENTIONS §3/§4）

- I2S 输出块（480 帧 ×2ch ×2B）在**内部 RAM**（DMA 需要），播放任务内不做动态分配；
- MP3 解码状态 `mp3dec_t` 6,668 B + 临时区 `mp3dec_scratch_t` 16,256 B + 单帧输出 4,608 B、PCM 缓冲全部在 **PSRAM** → 内部 RAM 增量 0；
- minimp3 上游把 16.3KB 临时区放在任务栈上（6KB 栈必爆），本项目用 3 处补丁改成调用方注册（见 `third_party/minimp3/README.md`）；
- 解码在播放任务内串行执行：24k 单声道 1.77 ms/帧（帧长 24ms）、44.1k 立体声 4.45 ms/帧（帧长 26.1ms），可实时；
- 队列非阻塞；队列满丢请求并 WARN；`play_mp3` 在播放中复用同一 PCM 缓冲会互相覆盖，故直接拒绝（`ESP_ERR_INVALID_STATE`）。

## 后续（P2-T2.4 起）

- **T2.4 流式播放**：`voice_cloud` 下行 0x02 帧 → 边收边解 → 预缓冲 200ms 出声（首帧到出声 <150ms 是 P2 验收项），届时 PCM 缓冲改为小 ring、`mp3_dec_decode_all()` 拆成 feed/frame；
- 多通道：`CH_TTS` / `CH_PROMPT` / `CH_MUSIC` + 混音 + ducking（P3/P4）；
- 音源抽象：`source_http`（HTTP 流）/`source_file`（FATFS），P4。

对应架构：[docs/ARCHITECTURE.md](../../docs/ARCHITECTURE.md) §3.2、§3.7、§9.3；验收：[docs/STATUS.md](../../docs/STATUS.md) §5。
