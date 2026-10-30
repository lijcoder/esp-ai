# assets —— 内置提示音资源（临时组件，不参与分层）

**作用**：在"资源分区"方案落地前，把本地提示音（16bit / 16kHz / 单声道 PCM）以 C 数组形式编进固件，供 `audio_player` 播放。

> **临时方案**：按架构 §3.4，提示音最终存放在 `assets` SPIFFS 分区；P3（T3.5）迁过去后**删除本组件**，改为分区内索引加载。

## 当前资源

| 符号 | 内容 |
|---|---|
| `prompt_wo_zai_ne_pcm[]` | 提示音"我在呢"，16kHz / 16bit / 单声道，12800 采样点（0.8 秒） |
| `prompt_wo_zai_ne_samples` | 采样点数量 |

数据由原 `main/voice_data.c` 的字节数组转换而来（`int16` 小端）。

## 对外接口

仅两个外部符号（见 `include/prompt_assets.h`），无函数、无依赖。

## 新增提示音的约定

1. 格式统一 16kHz / 16bit / 单声道（其它采样率播放器能处理，但资源统一便于管理）；
2. 转成 C 数组时按 `int16_t` 生成，不要用 `uint8_t` 强转（对齐问题）；
3. P3 之后改为通过 `tools/audio_pack.py` 打包进 `assets` 分区，不再新增本组件的 `.c` 文件。

对应架构：[docs/ARCHITECTURE.md](../../docs/ARCHITECTURE.md) §3.4（提示音）、§9.1（assets 分区）。
