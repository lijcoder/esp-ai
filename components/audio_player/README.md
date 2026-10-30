# audio_player —— 播放引擎（L2 音频引擎层）

**作用**：把任意采样率的单声道 PCM 变成 48kHz 立体声输出，负责音量、淡入淡出、尾音处理，并在播放结束后通知业务层。上层（`main`、后续 `voice_session`）不需要关心音频搬运动作。

## 当前能力（MVP）

- 独立播放任务（核 1、优先级 5），任务队列深度 4；
- 线性重采样到 48kHz（如提示音 16k → 48k），单声道复制到 L/R；
- 软件音量 0~100（MAX98357A 无数字音量，增益由 SD 电阻固定）；
- 淡入 10ms / 淡出 100ms，混音后限幅（防 3W 功放削波）；
- 播放结束：100ms 静音冲刷 + 150ms 延时 + 关 TX 时钟；
- 完成回调带 300ms 尾部延时（半双工规则 R2 用它恢复 `audio_fe` 收音）。

## 对外接口

| 接口 | 说明 |
|---|---|
| `audio_player_init()` | 创建队列与播放任务 |
| `audio_player_play_pcm(pcm, samples, rate)` | 播放一段 PCM（调用方保证数据生命周期） |
| `audio_player_register_done_cb(cb, arg)` | 注册播放完成回调（任务上下文，勿耗时） |
| `audio_player_is_busy()` | 是否正在播放（含排队） |
| `audio_player_set_volume(v)` / `get_volume()` | 软件音量 0~100 |

## 配置（Kconfig）

| 配置 | 默认 | 说明 |
|---|---|---|
| `AUDIO_PLAYER_DEFAULT_VOLUME` | 60 | 默认音量 |
| `AUDIO_PLAYER_TAIL_DELAY_MS` | 300 | 完成回调前的尾部延时（R2） |

## 依赖

`audio_io`（写 I2S）。播放缓冲在**内部 RAM**，队列中的 PCM 指针由调用方管理。

## 后续（P2–P4）

按架构目标形态扩展：

- 多通道：`CH_TTS` / `CH_PROMPT` / `CH_MUSIC` + 混音 + ducking；
- MP3 解码（`decoder_mp3.c`，Helix）与流式播放；
- 音源抽象：`source_http`（HTTP 流）/ `source_file`（FATFS）。

对应架构：[docs/ARCHITECTURE.md](../../docs/ARCHITECTURE.md) §3.2、§3.5、§3.7、§9.3。
