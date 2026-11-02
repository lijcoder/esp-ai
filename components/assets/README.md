# assets —— 内置提示音资源（临时组件，不参与分层）

**作用**：在"资源分区"方案落地前，把本地提示音（16bit / 16kHz / 单声道 PCM）以 C 数组形式编进固件，供 `audio_player` 经 `prompt_assets_get()` 取资源播放（唤醒应答 + T3.6 异常反馈）。

> **临时方案**：按架构 §3.4，提示音最终存放在 `assets` SPIFFS 分区；P3（T3.5）迁过去后**删除本组件**，改为分区内索引加载。上层只依赖 `prompt_assets_get()`，迁移时不动调用方。

## 资源表（5 条，均 16kHz / 16bit / 单声道）

| name（`prompt_assets_name`） | 枚举 | C 符号 | 文案 | 采样点 | 时长 | 峰值 | PCM |
|---|---|---|---|---|---|---|---|
| `wake` | `PROMPT_WAKE_OK` | `prompt_wo_zai_ne_pcm[]` | 我在呢（原资源，未改动） | 12800 | 800.0 ms | 13311（-7.82 dBFS） | 25.0 KB |
| `net-offline` | `PROMPT_NET_OFFLINE` | `prompt_net_offline_pcm[]` | 网络未连接 | 17201 | 1075.1 ms | 13311（-7.82 dBFS） | 33.6 KB |
| `cloud-error` | `PROMPT_CLOUD_ERROR` | `prompt_cloud_error_pcm[]` | 服务出了点问题 | 23487 | 1467.9 ms | 13311（-7.82 dBFS） | 45.9 KB |
| `timeout` | `PROMPT_TIMEOUT` | `prompt_timeout_pcm[]` | 响应超时 | 17325 | 1082.8 ms | 13311（-7.82 dBFS） | 33.8 KB |
| `tts-fail` | `PROMPT_TTS_FAIL` | `prompt_tts_fail_pcm[]` | 语音播放失败 | 19769 | 1235.6 ms | 13311（-7.82 dBFS） | 38.6 KB |

- 4 条错误提示音 PCM 合计 **181,164 B（176.9 KB）**，契约上限 ≈250KB。
- 峰值统一 13311 =「我在呢」实测峰值（-7.82 dBFS），保证各类提示音等响（音量仍由 `audio_player` 混音级统一控制，资源侧不再乘音量）。
- 试听 WAV（16kHz/16bit/mono，`build/` 已 gitignore）：`build/t36_prompts/{wake,net-offline,cloud-error,timeout,tts-fail}.wav`。
- 数据来源：`prompt_wo_zai_ne.c` 由原 `main/voice_data.c` 字节数组转换；4 条错误提示音由 `tools/prompt_gen.py` 用 macOS `say -v Tingting` 生成，**勿手改**。

## 对外接口

见 `include/prompt_assets.h`（T3.6 由 Lead 冻结，改接口前先对齐）：

| 接口 | 说明 |
|---|---|
| `const prompt_clip_t *prompt_assets_get(prompt_id_t id)` | 取资源；非法 id 返回 `NULL`。返回 `{pcm, samples, sample_rate}`，`sample_rate` 固定 16000，调用方不得释放/修改，不拷贝 |
| `const char *prompt_assets_name(prompt_id_t id)` | 短名（日志/测试脚本断言用）；非法 id 返回 `"?"` |
| `PROMPT_WAKE_OK` ~ `PROMPT_COUNT-1` | `prompt_id_t` 枚举；`PROMPT_COUNT == 5` |

兼容旧符号（T3.1 之前的唤醒提示音直接引用，T3.5 迁移后删除）：`prompt_wo_zai_ne_pcm[]` / `prompt_wo_zai_ne_samples`。

内部说明：静态表在 `prompt_assets.c`（flash rodata，无运行时初始化）；采样点宏在 `prompt_pcm_sizes.h`（脚本生成），各 `prompt_*.c` 用 `_Static_assert` 与之对齐，防止"表里长度 ≠ 数组长度"导致静默越界。

## 生成命令（`tools/prompt_gen.py`）

```bash
# 生成 4 条错误提示音 C 数组 + prompt_pcm_sizes.h + 试听 WAV（默认 build/t36_prompts）
uv run tools/prompt_gen.py

# 只校验已生成的 C 数组（不依赖 say/afconvert，可在 CI/非 macOS 跑）
uv run tools/prompt_gen.py --check

# 指定/关闭试听输出
uv run tools/prompt_gen.py --audition-dir build/t36_prompts
uv run tools/prompt_gen.py --no-audition
```

处理链（契约 §4）：`say -v Tingting` 合成 → `afconvert` 转 16k 单声道 → 裁首尾静音（保留 30ms 余量；个别短句无前导静音则补 30ms 数字静音）→ 去直流 → 峰值归一 13311 → 首尾 15ms 线性淡入淡出 → 写 C 数组。脚本按确定性语速梯子挑选第一个让 4 条全部满足约束的语速（本机 `-r 200`），无随机数，同机重复运行输出逐字节稳定；每次打印每条的文件、采样点、时长 ms、峰值、RMS、sha256。

`--check` 断言（失败退出码 1）：时长 0.8~1.6s、峰值 13311±3%（12912~13710）、首/尾 20ms 内 |x|<200、|mean|<100、`prompt_pcm_sizes.h` 宏与数组长度一致、`prompt_assets.c` 静态表项/名字完整。**重新生成后必须重跑 `--check` 再提交。**

新增/修改文案：改 `tools/prompt_gen.py` 的 `PROMPTS` 列表（文案 + 符号/枚举/短名），再跑生成命令；不要手工新增本组件的 `.c` 资源文件。

## 依赖与约束

- 生成仅限 macOS（`say`/`afconvert`）；固件侧无额外依赖（纯 rodata）。
- 采样率固定 16kHz：上层 `audio_player` 播放 PCM 提示音按其采样率转换到 48k 输出（见 `audio_player` README）。
- 资源必须保持等响（峰值 13311）与防 pop（首尾静音 + 淡入淡出），否则试听/真机主观音量会忽大忽小、起播有"咔哒"声。

## T3.5 迁移说明（资源分区）

1. 用 `tools/audio_pack.py` 把 5 条提示音打包进 `assets` SPIFFS 分区（16k/16bit/mono 原样，索引存 name/offset/samples）；
2. **只改本组件实现**：`prompt_assets_get()` 改为从分区索引读取（首次用时映射/加载到 PSRAM 或只读 mmap），`prompt_assets_name()` 保持不变；上层 `voice_session` / `feedback` 一行不改；
3. 删除 4 个 `prompt_*.c`、`prompt_pcm_sizes.h`、`prompt_wo_zai_ne.c` 及兼容旧符号，`CMakeLists.txt` 只留加载实现；
4. 迁移后本 README 挪到架构 §3.4/§9.1 的资源说明，`tools/prompt_gen.py` 的 `--audition-dir` 仍可用来生成/校验素材。

对应架构：[docs/ARCHITECTURE.md](../../docs/ARCHITECTURE.md) §3.4（提示音）、§9.1（assets 分区）；契约：`build/t36_contract.md` §4。
