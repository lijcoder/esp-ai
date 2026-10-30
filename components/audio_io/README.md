# audio_io —— I2S 收发 HAL（L0 板级/驱动层）

**作用**：封装两路 I2S 硬件，向上提供"读麦克风 / 写喇叭 / 开关喇叭时钟"三个原语。采样率在这里定死，上层不需要关心 I2S 细节。

## 通道配置

| 通道 | 外设 | 参数 |
|---|---|---|
| 采集 | `I2S_NUM_0` RX | 16kHz、32bit 槽位、单声道、左声道（INMP441 的 L/R 接 GND），读取后 `>>16` 转 int16 |
| 播放 | `I2S_NUM_1` TX | 48kHz、16bit、立体声（单声道数据复制到 L/R），MAX98357A 无需 MCLK |

## 对外接口

| 接口 | 说明 |
|---|---|
| `audio_io_init()` | 初始化两路 I2S（RX 立即启动，TX 保持关闭） |
| `audio_io_read_mic(dst, frames, timeout_ms)` | 读单声道 int16，返回实际帧数 |
| `audio_io_write_spk(data, frames, timeout_ms)` | 写交织立体声 int16，返回实际帧数 |
| `audio_io_spk_enable(bool)` | 播放前后开关 TX 时钟，避免静音期底噪与 pop |
| `audio_io_spk_is_enabled()` | 查询 TX 是否使能 |

## 关键约束

- **采样率固定**：16k 采集 / 48k 播放，运行中不切换（见 [docs/ARCHITECTURE.md](../../docs/ARCHITECTURE.md) §3）。
- RX 中间缓冲（1024 帧）与 I2S DMA 缓冲都在**内部 RAM**；大块播放缓冲由 `audio_player` 管理。
- 本组件**不做重采样、不做音量、不做混音**，这些属于 `audio_player`。
- **超时单位是毫秒**：`timeout_ms` 直接传给 IDF `i2s_channel_read/write`（该 API 最后参数也是毫秒），**禁止再套 `pdMS_TO_TICKS()`**——本工程 `CONFIG_FREERTOS_HZ=100`，套一层会让 100ms 变成 10ms，读 512 帧 @16k（需 32ms）必然超时，表现为唤醒静默失效（见 CONVENTIONS §3、STATUS 2026-10-30）。
- 超时但已读到部分数据时返回**实际帧数**，调用方补零；只有完全没拿到数据才返回 -1。
- 写 480 帧 @48k 约需 10ms，`timeout_ms` 建议 ≥100。

## 依赖

`driver`（I2S）、`board`（引脚定义）。

对应架构：[docs/ARCHITECTURE.md](../../docs/ARCHITECTURE.md) §2、§3.1、§3.2、§9.3。
