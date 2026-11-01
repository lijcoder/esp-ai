# tools/ —— 开发辅助工具

开发期脱机联调/真机自测用的工具与素材，**不进固件、不占设备资源**。

| 文件 | 用途 | 用法 |
|---|---|---|
| `ws_mock_server.py` | 本地「云端」WS 模拟服务（T1.5 / T2.4），设备联调不用等后端 | 见下文 |
| `voice_test.py` | 语音链路真机自动自测：唤醒 + 交互语音回放 + 串口断言 | `uv run tools/voice_test.py --volume 50`，见下文 |
| `lexin.wav` | 唤醒词"嗨，乐鑫"测试音频，**替代人工喊话** | `afplay tools/lexin.wav`，见下文 |
| `今天天气如何.wav` | 交互语音素材（模拟用户提问），**替代人工说话** | `afplay tools/今天天气如何.wav`，见下文 |
| `今天吃了什么.wav` | 交互语音素材（模拟用户提问） | `afplay tools/今天吃了什么.wav`，见下文 |
| `opus_enc_test.py` | 上行 Opus 编码真机自测（T2.2）：串口抓帧 + PyAV 独立解码断言 + 内存/泄漏 | `uv run tools/opus_enc_test.py --self-test`，见下文 |
| `voice_cloud_test.py` | 语音云端到端真机自测（T2.5）：起 mock → 唤醒 → 上行 Opus → 下行事件 → sid/统计断言 | `uv run tools/voice_cloud_test.py`，见下文 |
| `tts_stream_test.py` | 下行 TTS **流式播放**真机自测（T2.4 / T3.2 R3–R5）：真实 MP3 经 WS 0x02 下发 → 首帧延迟/欠载/解码对拍/abort/排空/10 轮堆稳定；可选 R4 按键打断、R5 无丢弃、R3 ducking、提示音 R1 原因位 | `uv run tools/tts_stream_test.py`，见下文 |
| `e2e_voice_test.py` | **端到端语音**真机自测（T2.6）：唤醒 → 上行 Opus → 真实 MP3 下行流式播放 → R2，10 轮稳定性 + P2 验收断言 | `uv run tools/e2e_voice_test.py`，见下文 |
| `record_wav.py` | 串口采集 WAV，用于唤醒/AEC 实测（**T5.2 计划，尚未实现**） | — |

## 语音测试素材 —— lexin.wav + 两个交互语音

**作用**：agent 不能说话，真机测唤醒/语音交互一律用 Mac 扬声器回放 `tools/` 里的 WAV、设备麦克风拾音，
**不需要人工喊话/说话**；回放可重复、可计时、可断言，唤醒率/误唤醒的对比才有意义。

| 文件 | 内容 | 用途 | 时长 | 大小 | 文件峰值 / RMS |
|---|---|---|---|---|---|
| `lexin.wav` | "嗨，乐鑫" | 唤醒（WakeNet9 `wn9_hilexin`） | 1.28 s | 61,484 B | -9.1 / -26.5 dBFS |
| `今天天气如何.wav` | 一句提问 | 交互语音：唤醒提示音结束后模拟"用户说话" | 1.60 s | 76,844 B | -13.7 / -28.1 dBFS |
| `今天吃了什么.wav` | 一句提问 | 交互语音：同上 | 1.04 s | 49,964 B | -4.7 / -19.3 dBFS |

**通用属性**：24 kHz / 16 bit PCM / mono。素材经空气传到设备、设备按 16 kHz 采集，音高/语速正常，
**不要为 16 kHz 重采样**，直接回放原文件。
**流式 WAV 头（重要）**：三个文件的 RIFF/data 长度字段是 `0x7FFFFFxx` 占位值，不是严格 WAV；
`afplay` 会按文件尾正常播完，但用 Python `wave` 或 ESP32 文件播放等**严格解析器**时长度会错
（`wave` 按占位长度会算出约 4.47 万秒/12.4 小时，实际只有 1~2 秒），需按"实际文件大小 − 44 B"截断——`voice_test.py` 已按此口径解析。

### 音量口径（用户定稿，不得擅自改）

- 系统输出音量 **正常 31%、上限 50%**（50% 不得突破；第一次 70% + `afplay -v 1.5` 被用户否决为太响）；`afplay` **默认增益**。
- `-v` 放大须满足 **系统音量 × 增益 ≤ 50%**：31% + `-v 1.5` ≈ 46.5% 可以；50% + `-v` 会超限，不可以。
- **自动化回归建议 `--volume 50`**（脚本临时调到上限、结束恢复原值）：环境噪声大时 31% 唤醒率会下降，见下方实测。

**实测（2026-10-31，ESP32-S3-N16R8，固件 1,721,744 B T1.4，设备静置桌面）**：

- 安静环境（底噪 RMS 80~220）：31% 唤醒 **6/6**，链路完整。
- 环境噪声偏高（`mic stat` 基线 RMS 1,100~1,500）：31% 唤醒 **5/9**（三轮分别 1/3、2/3、2/3）；50% 唤醒 **6/6**。
- 交互语音：两段素材都被设备麦克风拾到且 AFE **VAD=人声**；**误唤醒 0**（31%/50% 所有轮次）；无 ERROR/panic；回放后 `feed resumed`（R2）。
- 设备侧 `mic stat` 电平（窗口含前后静音，随距离/窗口长度波动，仅供对照）：

| 素材 | 31%：RMS / 峰值 | 50%：RMS / 峰值 |
|---|---|---|
| `今天天气如何.wav` | 90~330 / 302~1,123 | 289~453 / 1,262~1,487 |
| `今天吃了什么.wav` | 316~526 / 1,084~1,448 | 395~546 / 2,239~3,081 |

> 结论：3 个素材均可用，无问题。**当前 P1 阶段**交互语音只验证"麦克风采集 + VAD 人声 + 不误唤醒"；
> **P2 起**（`voice_cloud` + Opus 上行）用同一套回放做 ASR → LLM → TTS 端到端断言；
> **P3 起**用于会话状态机/半双工/打断；**T5.2** 用回放做唤醒率/误唤醒基线。

### 手工回放（不用脚本时）

```bash
# 终端 A：观察设备日志（注意：idf.py monitor 会复位设备，等初始化/联网完成后再回放）
idf.py -p /dev/cu.usbmodem5CF90746951 monitor

# 终端 B：唤醒（正常 31%、上限 50%；默认增益即可，不要靠调系统音量放大）
afplay tools/lexin.wav
# 等到 "APP: prompt done, feed resumed" 之后再放交互语音：R1 播放提示音期间 AFE 吃的是静音
afplay tools/今天天气如何.wav
```

**唤醒预期日志**（回放后 ~0.1 s 内唤醒，随后播放 1.28 s 提示音）：

```
I (22470) APP: wake detected
I (22470) AFE: feed paused (half-duplex)      # R1：播放前暂停收音
I (22470) PLAYER: play 12800 samples @16000 Hz (vol=60)
I (23820) AFE: feed resumed                   # R2：播放结束恢复收音
I (23820) APP: prompt done, feed resumed
```

**交互语音预期**（P1：无会话状态机，不会出现 APP 日志）：
`mic reset` → 回放 → `mic stat`，应看到 `VAD 人声`、电平/峰值明显高于底噪、`累计唤醒 0`（不误唤醒）、`收音 开启`。

**注意事项**：

- 设备在播放提示音/音频期间再次回放会被忽略（`APP: player busy, ignore wake`）；连续回放间隔建议 ≥2 s。
- 交互语音必须在 `feed resumed` 之后放：R1 期间 AFE 输入是静音，放进去等于没测。
- 交互语音电平偏低（尤其 `今天天气如何.wav`）：底噪高/距离远时后续 ASR 可能识别不到；先试 50% 上限音量，仍不行再考虑重录。
- 无唤醒排查顺序：macOS 静音/输出路由（`osascript -e 'get volume settings'`）→ 电脑与设备距离/朝向（扬声器对准设备麦克风）→ 设备是否在播放 → `mic stat` 电平是否正常（安静室内底噪 RMS 约 40~300）。
- 回放会**外放声音**，仅在用户知情/同意时使用；音量口径见上。

## voice_test.py —— 语音链路自动自测（改完音频相关代码的回归手段）

**作用**：一条命令完成"复位 → 基线 → 唤醒 ×N → 交互语音回放 → 结束健康检查"，自动解析串口日志并断言；
不需要人喊话、不需要盯串口。改到音频/会话链路的任务都应跑一遍（P1 收尾 T1.6 起加入回归）。

```bash
uv run tools/voice_test.py --volume 50 --wakes 3 --log /tmp/voice_test.log
uv run tools/voice_test.py --dry-run      # 只检查素材/音量/串口，不连设备
uv run tools/voice_test.py --no-reset --wakes 1   # 设备已在跑、不想复位
```

**断言项**（全部 PASS 退出码 0，任一 FAIL 退出码 1）：

- 系统输出未静音、音量 ≤ 50%；`--volume` 临时改音量，结束（含异常/Ctrl-C）恢复原值。
- 素材存在且为 24 kHz / 16 bit / mono；设备启动/控制台就绪，启动无语音相关 ERROR。
- `mic stat` 基线 I2S 读失败 0 次；基线 RMS 偏高时 WARN（环境噪声会拉低唤醒率）。
- 唤醒链路完整顺序：`wake detected → feed paused → PLAYER: play → prompt done, feed resumed`；默认要求全部成功，`--min-wake-pass` 可放宽。
- 两个交互语音：误唤醒 0、回放后 `收音 开启`（R2）、I2S 读失败 0、无 ERROR；VAD 非人声只 WARN（环境相关）。
- 结束 `sys info` 无 ERROR/panic。

**参数**：`--port`（默认自动探测唯一的 `/dev/cu.usbmodem*`）、`--wakes`（默认 3）、`--min-wake-pass`（默认 = `--wakes`）、
`--volume`（0~50，临时设置并恢复）、`--no-reset`、`--pause-between`（默认 2 s）、`--wake-timeout`（默认 6 s）、
`--boot-timeout`、`--log`、`--dry-run`。

**依赖与限制**：PEP 723 内联声明 `pyserial>=3.5`，`uv run` 自动安装；本机 `python3` 已装 pyserial 时也可直接
`python3 tools/voice_test.py`。脚本**独占串口**（运行前关掉 `idf.py monitor`），默认会复位设备。
唤醒响应耗时（实测 1.7~2.0 s）含 `afplay` 启动开销，只作相对参考，不是端侧唤醒时延。

---

## voice_cloud_test.py —— 语音云端到端真机自测（T2.5）

**作用**：一条命令完成「起本地 mock → 复位设备 → 回放唤醒词 → 回放交互语音 → 断言上行/下行/统计 → 测 `vc stop`/`vc cancel`」，覆盖 T2.5 的端到端链路。脚本会自己拉起 `ws_mock_server.py`（端口被占用时复用并跳过 mock 侧断言），独占串口；音量默认临时调到 50% 上限、结束恢复。

```bash
uv run tools/voice_cloud_test.py                        # 默认 --volume 50，全量 50 项（T3.1 起含 VS 断言）
uv run tools/voice_cloud_test.py --inject-late          # 让 mock 注入 sid+1 的迟到事件，验证端侧过滤
uv run tools/voice_cloud_test.py --volume 31 --log /tmp/vc.log
uv run tools/voice_cloud_test.py --no-mock              # 已有 mock 在跑（跳过 mock 侧断言）
uv run tools/voice_cloud_test.py --asr --asr-expect 天气  # mock 用本地 SenseVoice 真 ASR，断言识别文本
uv run tools/voice_cloud_test.py --dry-run              # 只检查素材/音量/串口
```

**断言**：设备启动 + `hello_ack`；唤醒链路（`wake detected`→`feed paused`→`PLAYER: play`→`prompt done`，T3.1 起未知前缀由 `(?:APP|VS)` 兼容）；`round start` / 上行首帧 <200ms / `audio_end` 原因 `vad-silence` / `tts_start`(mp3/24000) / `tts_end` / `round done`；event_bus 收到 `ASR_FINAL`/`TTS_START`/`TTS_END`；上行帧数 ≥60、平均帧长 20~60B、首帧耗时、下行 100 帧与 `tts_end` 计数一致、`sid` 丢弃/解析失败/云端 error 为 0；mock 侧 sid 一致、逐帧收到、`seq` 无跳变、无坏帧；`vc stop` 收尾原因 `stop-request`、`vc cancel` 立即放弃；**T3.1 会话状态机**：`VS` 迁移序列必须为 `IDLE→LISTENING→THINKING→SPEAKING→IDLE`、迁移日志 `sid` 与本轮一致、event_bus 收到 `VOICE_STATE_CHANGED`、`vs status` 轮次开始/完成各 1 且无异常/超时；内部堆最低水位 ≥30KB、`vc` 任务栈剩余 ≥1KB、无 ERROR。**T3.1 真机实测 50/50 PASS**（含 `--inject-late` 50/50；T2.5 时为 43/43）。
加 `--asr` 时额外断言：设备 `VC: ASR final` 的文本非空、与 mock 侧 SenseVoice 识别结果一致、不是 `--tts-text` 固定文案；`--asr-expect <子串>` 再校验内容（mock 子进程由脚本用 `uv --with` 拉起，本脚本自身不需要额外依赖）。

## afe_uplink_test.py —— 上行 PCM 接口真机自测（T2.1）

**作用**：验证 `audio_fe` 的上行 16k PCM 输出接口（`audio_fe_uplink_*`）在真机上的行为，
是 T2.2（Opus 编码）/T2.5（`voice_cloud`）之前的上行链路基线。一条命令完成：复位 → 精确分帧读出 →
回放可闻 → 溢出去 → 反复开关无泄漏 → 唤醒链路回归。

```bash
uv run tools/afe_uplink_test.py --volume 50 --log /tmp/afe_uplink.log
uv run tools/afe_uplink_test.py --no-reset          # 设备已在跑、不想复位
```

**断言项**（15 项，全 PASS 退出码 0）：

- 环形缓冲已分配（32768 样本 / 2048ms / 64KB PSRAM）；
- 320 样本精确成帧（AFE fetch 512 样本 → 消费端重组）、样本/时长比例 16k ±10%、无丢弃、读超时 ≤1、首帧 <200ms；
- 回放能被上行拾到：**3s 1kHz 音调 + 4s 窗口**，比较同一窗口内的「音调段 RMS」与「静音段 RMS」（≥ max(300, 静音段×1.5)）；
  用音调而非语音，是因为本机系统音量上限 50% 时语音在设备侧只比底噪高 1~4dB，波动大、易误判；
- 交互语音回放时上行有真实音频（非全零/静音）；
- 溢出策略：停读 3.5s（>2.048s 缓冲）后 `samples_dropped` 增长、最高占用=缓冲容量、恢复后仍能读；
- 反复 `on/off` 10 次：内部 RAM / PSRAM 剩余 Δ ≤ 4KB；
- 唤醒链路完整（`wake detected → feed paused → PLAYER: play → prompt done, feed resumed`），无音频链路 ERROR。

**参数**：`--port`、`--no-reset`、`--boot-timeout`、`--volume`（临时调系统音量，结束恢复；上限 50）、`--log`。

**依赖与限制**：PEP 723 内联 `pyserial>=3.5`；脚本**独占串口**（先关 `idf.py monitor`）；需要 Mac 扬声器回放
（音调 WAV 临时生成在系统临时目录，结束删除，不进仓库）。环境底噪偏高时会自动重试 3 轮。

---

## opus_enc_test.py —— 上行 Opus 编码真机自测（T2.2）

**作用**：验证 `codec_enc` 的上行 Opus 输出（16k / 单声道 / 20ms，目标 16kbps；最终选型 VBR，实测平均约 13~14kbps，ARCHITECTURE §3）。设备把编码帧 dump 成
hex，脚本用 **PyAV（libopus）独立解码**后再断言，不采信设备自报数值。一条命令覆盖：tone 源数值基准 → uplink 真实链路
（回放交互语音）→ 连续多次调用的内存/泄漏，共 40+ 项 PASS/FAIL，退出码 0/1。

```bash
uv run tools/opus_enc_test.py --self-test              # 离线自检（不连串口/设备，12 项，先跑这个）
uv run tools/opus_enc_test.py --log /tmp/opus_enc.log  # 真机全量（串口独占，默认复位设备）
uv run tools/opus_enc_test.py --no-reset --window-ms 2000
uv run tools/opus_enc_test.py --keep-wav /tmp/opus_wavs   # 保留解码 WAV（afplay 试听云端将收到的声音）
```

### 设备侧 console 契约（`codec_enc` 提供，脚本只消费输出）

| 命令 | 输出 |
|---|---|
| `enc dump on\|off` | `ENC dump on` / `ENC dump off` |
| `enc test <ms> [tone\|uplink]` | 起始行 + dump 行（每帧一行）+ 结束行，见下 |
| `enc stat` / `enc reset` | `ENC stat: open=.. frames=.. ...` / `ENC stats reset` |

- 起始行：`ENC TEST start: source=tone|uplink window_ms=N dump=on|off`
- dump 行（`enc dump on` 时每帧一行）：`ENCDUMP <seq> <len> <hex>`，`seq` 从 0 递增、`hex` 小写无分隔；
- 结束行：`ENC TEST done: source=.. frames=.. samples=.. bytes=.. bytes_per_frame=.. bitrate_kbps=.. encode_avg_ms=.. encode_max_ms=.. max_packet=.. errors=.. carry=.. elapsed_ms=.. internal_free_delta=.. psram_free_delta=..`
  （当前固件该行还多 `read_timeouts=..`、`stack_free=..`（REPL 任务栈余量）等额外字段，脚本忽略未知字段、不判失败）；
- **dump 缓冲上限**（当前固件 `ENC_DUMP_FRAMES=50`，即 1s）：窗口超过 1s 时只 dump 前 50 帧，随后打印
  `ENC dump missed: N 帧（超出缓冲未记录）` 与 `ENC dump done: n=50`。脚本对账口径是
  `dump 帧数 + missed == done.frames`（两者相等或截断均可），**解码/频谱/时长断言用实际抓到的帧（1s）**，
  结构性数值门槛（frames、bytes/frame、bitrate、耗时）用整窗口的 `done.frames`。
  若要 2000ms 全帧抓取，需固件把 `ENC_DUMP_FRAMES` 提到 ≥ `window_ms/20 + 1`（如 128/200），或把 `--window-ms` 降到 1000。
- **uplink 测试前排空 ring 积压**：固件在 `enc test ... uplink` 开始时用 timeout=0 循环丢弃 start 前采集的旧数据，
  并打印 `ENC TEST: 已丢弃 N 帧积压（start 前采集的旧数据）`。该行出现在 `ENC TEST start` **之前**，脚本忽略；
  因此 uplink 窗口帧数应 ≈ `window_ms/20`（实测 2000ms → 100 帧），B 用例容差 ±3 只吸收调度边界。

### 设备侧输出字段含义（工具断言口径）

| 字段 | 含义 / 断言口径 |
|---|---|
| `source` | 编码源：`tone`（设备内置 1kHz 正弦）/ `uplink`（`audio_fe` 上行 PCM）；起始行与结束行必须一致 |
| `window_ms` | 编码窗口毫秒数；帧数期望 `window_ms/20`（tone ±2；uplink ±3，固件测试前排空 ring 积压） |
| `frames` | 实际编码帧数（整窗口）；与 dump 行数对账 `dump + missed == frames`（允许 dump 缓冲截断） |
| `samples` | 消费的输入样本数（16k 单声道，20ms=320） |
| `bytes` / `bytes_per_frame` | 编码总字节 / 平均每帧字节；avg 期望 **26~46 B**（VBR 实测 33~35 B） |
| `bitrate_kbps` | 设备自算码率；期望 **11~18 kbps**（VBR 实测 13~14 kbps） |
| `encode_avg_ms` / `encode_max_ms` | 单帧编码耗时；期望 **≤10ms / ≤15ms**（VBR 实测 5.3~6.7 / 7.9~8.6ms） |
| `max_packet` | 最大 Opus 包长（字节），仅记录（Opus 包上限 1275B） |
| `errors` / `carry` | 编码错误数 / 未凑满 320 样本的余数；两者必须为 **0** |
| `elapsed_ms` | 整个 `enc test` 的墙钟时长（≈window_ms），仅记录 |
| `internal_free_delta` / `psram_free_delta` | 本次 test 前后内部 RAM / PSRAM 剩余变化（正=占用增加），用于泄漏判断 |

**为什么是 VBR（2026-10-31 真机实测）**：CBR 单帧编码 **16.2ms**（rate loop 反复重编码），VBR 仅 **6.7ms**（2.4×），
20ms 帧预算下选 VBR；代价是 VBR 平均码率实测约 **13~14kbps**（tone 34.5B/frame、语音 33.2B/frame，低于 16kbps 目标，
属正常 VBR 行为）。因此 `bytes_per_frame` / `bitrate_kbps` / `encode_avg_ms` 阈值按实测放宽为 26~46B / 11~18kbps / ≤10ms。

### 断言口径

- **A. tone 源数值基准**：`enc dump on` → `enc test <ms> tone` → 抓 `ENCDUMP` → PyAV 独立解码断言：
  解码失败帧 0（含 corrupt）；**解码帧率 ∈ {16000, 48000}**（Opus 原生输出，设备实测 **48000 / 单帧 960 样本**，不是
  16k/320——16k 只是我们重采样后的分析采样率）；Opus TOC 单声道 + 20ms 帧（设备实测首字节 `0x48` = config 9
  SILK NB 20ms + mono；PyAV 对裸 Opus 包解码时原生 layout 可能显示 `stereo`——无容器 extradata 时 libopus
  默认按 2ch 输出，单声道以 TOC 为准，重采样 downmix 后 RMS/FFT 正常）；**总时长 ≈ 抓帧数×20ms（±5%，
  按解码帧原生 `sample_rate` 折算）**；
  1kHz FFT 主峰 **1000±20Hz** 且 [980,1020]Hz 能量占比 **>50%**（频率轴按分析 PCM 实际采样率算）；
  frames ≈ window_ms/20±2；avg bytes/frame **26~46 B**；bitrate **11~18 kbps**；`encode_avg_ms ≤10`、
  `encode_max_ms ≤15`；`errors=0`、`carry=0`。另校验 `ENCDUMP` 的 seq 连续、`len` 与 hex 字节数自洽、hex 小写。
  设备 dump 缓冲不足时按上述截断口径对账（不因截断判失败）。
- **B. uplink 源真实链路**：`afe uplink on` → 静音基线 `enc test 500 uplink` → 后台 `afplay tools/今天天气如何.wav`
  （默认增益、不改系统音量）→ `enc test <ms> uplink`。数值门槛同 A（无 1kHz 频谱断言，改为音频证据）。
  **"有语音"用三条 OR 自适应判据**（不设绝对音量地板，见下文「解码量纲与 B 语音判据」）：
  ① 最高 100ms 分段 RMS ≥ max(静音基线×3, 30)；② 最高段 > 全段中位数×1.3；③ 300~3400Hz 语音频带能量占比 > 0.6；
  另有整段解码 RMS > 静音基线×1.5；
  `frames` 应 ≈ `window_ms/20`：固件在 uplink 测试前用 timeout=0 循环**排空 ring 积压**（并打印
  `ENC TEST: 已丢弃 N 帧积压`，位于 start 行之前、脚本忽略），实测 2000ms → **100 帧**；容差 **±3**
  吸收调度边界（tone 源压固定帧数，仍严格 ±2）；
  日志无 **I2S 读失败**、无 **ERROR/panic**（网络/WS 重连噪音按 `voice_test.py` 口径忽略，`读失败 0 次` 统计行不算失败）；
  结束务必 `enc dump off` + `afe uplink off`（异常退出时脚本也会在 finally 里补发）。
- **C. 内存/泄漏**：连续 3 次 `enc test 1000 tone`，记录每次 `internal_free_delta`/`psram_free_delta` 与
  `sys heap` 前后差值。断言内部 RAM 总增量 **≤8KB**、第 2/3 次内部 Δ **≤4KB**（无单调下降）；
  PSRAM 首次分配 **≤64KB**、后续 Δ **≤4KB**（不持续下降）；三次均 `errors=0`/`carry=0`、frames 50±2。

### 解码量纲与 B 语音判据

- **分析 PCM 量纲**：解码帧原生是 48k `fltp`（值域 [-1,1]），脚本把它重采样为 16k/mono/s16 写 WAV。
  量纲核对：`fltp × 32768 == libopus 解码器原生 s16`，分析 PCM 与之一致（**没有** downmix 0.5 或写 WAV 再除 32768）。
  - 设备 A tone（`ENC_TONE_AMPLITUDE=8000` ≈ 0.244FS）实测：分析 PCM **RMS 5556 / peak 8210**，
    与 libopus s16 解码 5555 / 8229 一致；`--self-test` 的 **S12** 用 libopus 原生 s16 交叉校验（比值 1.000，判据 ±10%）。
- **B 段绝对电平取决于 Mac 系统输出音量**（脚本按任务约定**不改系统音量**，默认增益）：两份真机日志里
  B dump 解码 RMS 仅 **49~58**（≈ -56dBFS）、峰值 168~428（音量小），但 100ms 分段包络随回放出现明显起伏，
  帧长 30B 左右（B12 已作为"有声"的独立证据）。因此 B17 只做**定性**判据、不设 150 这类绝对电平地板；
  三条 OR 判据在低音量下仍能稳定通过（实测最高段 79~108、中位 54~57、静音基线 7~10）。
- 注意两份真机日志里 B 段解码音频的 300~3400Hz 能量占比只有 **~1.3%**（主峰 145Hz、A tone 才是 1000Hz），
  与 A tone 的"99.9% 在语音频带"完全不同；若需要"语音频带占比"作为硬判据，先确认现场回放音量/麦克风路是否正常
  （建议用 `tools/afe_uplink_test.py --volume 50` 或把系统音量调到 50% 再抓 B 段）。

### --self-test（不需要硬件）

PyAV 的 libopus 编码器现场造 1kHz 正弦 Opus 帧（16k/单声道/20ms/16kbps，每帧 320 样本），把
「合成 `ENC TEST start`/`ENCDUMP`/`ENC TEST done` → 解析 → 独立解码 → 频谱/时长/帧数」整条断言逻辑跑在**真实帧**上，
另加 500Hz 负例（不得被判成 1kHz）证明断言不是写死的；S11 覆盖 dump 缓冲截断对账；**S12 交叉校验解码量纲**
（分析 PCM s16 RMS ≈ libopus 原生 s16 RMS，±10%）。S5 断言的是**解码帧率 ∈ {16000, 48000}**
（本机 libopus 解码器输出 48k，与真机一致），不是写死 16k。本机已验证 `av` 19.x 含 `libopus` 编码器与 `opus` 解码器。

### 参数 / 依赖 / 限制

- 参数：`--port`（默认自动探测唯一的 `/dev/cu.usbmodem*`）、`--window-ms`（默认 2000）、`--no-reset`、
  `--boot-timeout`、`--log`、`--keep-wav <dir>`、`--self-test`。
- `--keep-wav <dir>`：把解码后的 `A_tone.wav`（1kHz 正弦）/ `B_base.wav`（静音基线）/ `B_speech.wav`（回放的交互语音）
  保存到该目录并在结束前打印路径，可直接 `afplay` 试听「云端将收到的声音」；不加该参数时中间 WAV 落在系统临时目录、跑完删除。
- PEP 723 内联 `pyserial>=3.5`、`av>=12`，`uv run` 自动安装（首次会下载 PyAV/libopus 轮子，约 18MB）。
- **脚本独占串口**：运行前关掉 `idf.py monitor`（串口只有一个）；默认复位设备，`--no-reset` 用于设备已在跑。
- 用例 B 会 `afplay` 外放交互语音；**不改系统音量、默认增益**（音量口径见上文「音量口径」）。
- 本机 PyAV wheel 未带 numpy，脚本**不调 `to_ndarray()`**（避免 fltp float 值域 [-1,1] 被当 s16 截断成 0、误判全静音）：
  解码后统一重采样成 16k/mono/s16 写临时 WAV，再用标准库 `wave` 读样本做 FFT/RMS（FFT 纯 Python，2s@16k 约 0.05s，
  中间 WAV 默认落在系统临时目录、结束删除；`--keep-wav` 可留存）。设备帧解码原生输出是 **48k**（单帧 960 样本），时长断言按原生 `sample_rate`
  折算，不写死 16k。

---

## mp3_test.py —— 下行 MP3 解码真机自测（T2.3）

一条命令验证"设备能把 MP3 解对、真的从功放放出来"，不需要人工听（听感只作旁证）：

```
uv run tools/mp3_test.py                       # 默认自动探测串口；会抓一次真实电台流
uv run tools/mp3_test.py --skip-radio          # 离线：只跑本地生成的两组素材
uv run tools/mp3_test.py --dry-run             # 只生成素材 + 打印 PyAV 参考值（不碰设备）
uv run tools/mp3_test.py --repeat 3 --min-peak 1500 --no-reset
```

**素材**（现生成，不入库）：
- `24k/mono`：lameenc 编的 1kHz 正弦 4s @32kbps —— 模拟 TTS（服务端契约 24k 单声道）；
- `44.1k/stereo`：440Hz/880Hz 双声道 4s @128kbps —— 模拟音乐，顺带验证 (L+R)/2 下混；
- `电台流`：`curl` 抓 `http://lhttp.qingting.fm/live/20210755/64k.mp3` 前 ~4s（44.1k stereo
  64kbps）；**从流中间接入**，首帧不完整 → 顺带考验同步字搜索与比特池重置。

**流程**：PC 侧 PyAV 独立解码出参考（帧数/样本数/采样率/声道/单声道 PCM）→
`player load begin` + base64 分块（240 字符/行，串口无流控，最后按总长度校验，不符整段重传）→
`player load end` → `player mp3` → 等 `PLAYER: mp3 decoded` / `PLAYER: job done` →
`player stat` 比对统计 → `player pcm <off> <n>` 取回设备解码 PCM 与参考**逐样本**对拍。

**断言口径（48 项）**：
- 上传字节数 == 素材长度；
- 设备解码帧数/样本数 == PyAV 参考（电台流允许**少 8 帧**，因从流中间接入/截断）；
- 采样率、声道数一致；时长误差 ≤1%（电台 ≤5%）；
- 坏帧 0；干净素材跳过字节 0；单帧解码耗时 < 一帧音频时长（可实时）；
- 设备 PCM 与 FFmpeg `max|Δ| ≤ 2 LSB`（实测 1）；电台流用 RMS 量级一致兜底；
- 播放中 `mic stat` 峰值 ≥ `--min-peak`（默认 1000）→ 证明 PCM 真的从功放出来；
- 重复播放 `--repeat` 次后：`player stat` 计数自洽、内部 RAM 差 ≤8KB、PSRAM 增量 0；
- 全程无 ERROR/panic（`voice_test.error_lines` 的 TAG 过滤规则）。

**依赖**：`pyserial` / `lameenc`（MP3 编码，本机没有 ffmpeg/lame）/ `av`（PyAV 参考解码）/ `numpy`。
脚本独占串口（先关 `idf.py monitor`）。设备侧探针由 `CONFIG_DIAG_ENABLE_PLAYER_TEST=y` 提供
（**仅开发期**：开启后 console 行缓冲 128→1024 B、`player load` 用 512KB PSRAM 上传缓冲；量产必须关闭）。

**边界**：T2.3 是"整段解码后播放"；T2.4 流式播放的首帧延迟/欠载/abort/排空断言由
`tts_stream_test.py` 承接（见下一节），mock 真实 MP3 下发由 `ws_mock_server.py --tts-mp3` 提供。

## tts_stream_test.py —— 下行 TTS 流式播放真机自测（T2.4 / T3.2 R3–R5）

**作用**：验证「真实 MP3 经 WS 0x02 帧下发 → 端侧流式解码播放」这条链路，断言音频侧硬指标
（不是"能不能收到帧"，而是"首帧多久出声、会不会欠载、解出来对不对、打断停不停、收尾排不排空"）。
T3.2 起额外提供四个**可选**场景（默认不跑）：`--interrupt`（R4 播放中按键打断）、
`--r5`（正常收尾不产生 discard）、`--r3-duck`（音乐 ducking 探针）、`--prompt-r1`（提示音 R1 原因位），
见下文「T3.2 附加场景」。

一条命令完成：PC 侧生成素材 → 起 mock（`--tts-mp3`）→ 复位设备 → `vc start` → 轮询
`vc status` 到 `UPLINK` → `vc stop` 确定性触发一轮（**不依赖唤醒词/人声/VAD**）→ mock 下发
真实 MP3 → 逐项断言 → 汇总 PASS/FAIL（退出码 0/1，最后一行 `TTS_STREAM_TEST: PASS|FAIL`）。

```bash
uv run tools/tts_stream_test.py                    # 全量（默认复位设备、独占串口，约 1.5~2 分钟）
uv run tools/tts_stream_test.py --dry-run          # 只生成素材 + PyAV 参考 + 自检解析逻辑，不连设备
uv run tools/tts_stream_test.py --self-test        # 只做 T3.2 新解析/断言逻辑离线自检（33 项）
uv run tools/tts_stream_test.py --no-reset         # 设备已在跑
uv run tools/tts_stream_test.py --skip-heap        # 跳过 10 轮堆稳定（快速冒烟）
uv run tools/tts_stream_test.py --interrupt        # 附加 R4：真实 MP3 播放中按键打断 + 回归轮次
uv run tools/tts_stream_test.py --r5               # 附加 R5：正常收尾轮不产生 discard
uv run tools/tts_stream_test.py --r3-duck          # 附加 R3：player duck on/off 前后 out_peak 对比
uv run tools/tts_stream_test.py --prompt-r1        # 附加：提示音 R1 原因位（hold_mask=0x1，需外放）
uv run tools/tts_stream_test.py --log /tmp/tts_stream.log
```

### 素材（现生成、不入库）

`tools/今天天气如何.wav`（24 kHz / 16 bit / mono，1.6s）用 **lameenc** 转 24k 单声道 MP3，
默认拼接 3 份 ≈4.8s（`--material-repeats` 可调），码率默认 32 kbps（24ms/帧、96 B/帧）。
PyAV 独立解码出参考：`frames=202 samples=116352 rate=24000 ch=1 dur=4848 ms`（CBR 干净素材，
设备 decoded 应与参考一致，容差 ±2 帧）。

### 设备侧输出契约（脚本只消费，不猜字段）

脚本按 `.t2.4-contract.md` §4 / §2.7 的**逐字格式**解析；契约没写的字段一律不猜：

- `player stat` 追加行：`stream: active=.. fed=.. bytes=.. decoded=.. samples=.. rate=.. ch=.. ring_ms=.. high_ms=.. underrun=.. dropped=.. bad=.. first_sound_us=.. dur_ms=..`
- `PLAYER: stream open codec=mp3 sr=24000` / `PLAYER: stream first sound after <n> ms (prebuffer <n> ms)` / `PLAYER: stream end frames=.. bytes=.. decoded=.. dur=.. ms underrun=.. bad=..` / `PLAYER: stream abort (frames=.., drop .. B)`
- `vc stat` 下行行追加：`；音频丢弃 <n> 帧 字节 <n>`
- 轮次触发/收尾继续用 T2.5 已有日志：`VC: round start` / `TTS start` / `TTS end` / `round done` / `round aborted`

### 场景与断言（基础 6 个，另有可选 T3.2 场景 7~10）

| # | 场景 | 关键断言 |
|---|---|---|
| 1 | 正常一轮（真实 MP3） | `first_sound_us<150000`（日志 `first sound <150ms` 双口径）；稳态 `underrun=0`、`dropped=0`、`bad=0`；`fed/decoded` 与 PyAV 参考 ±2 帧、`samples` ±2 帧；`tts_end` 排空（`ring_ms=0`、`decoded==fed`、日志先 `tts_end` 后 `stream end`、`dur≈素材时长`）；设备收到字节==素材字节；mock 发送帧数/字节==素材；`vc` 下行帧数==素材帧数、窗口内丢弃 0；R2 收音恢复 |
| 2 | abort 停声 | 播放中 `vc cancel` → `PLAYER: stream abort` 延迟 ≤150ms（目标 100ms，串口 10ms 轮询）；`active=0` 后 decoded 不再增长；`round aborted` + 回 IDLE |
| 3 | 连续 10 轮堆/PSRAM | 每轮 `underrun=0`、decoded 完整；10 轮内部堆波动 ≤2KB 且不单调下降；PSRAM 首末差 ≤8KB、波动 ≤64KB、不单调下降（预热 1 轮不计入） |
| 4 | stall 欠载 | mock 在 `--tts-stall-at` 帧后停发 `--tts-stall-ms`（默认 600ms）：`underrun>=1`、日志 `stream underrun`；停发不丢帧、恢复后 decoded 完整、`rounds_done=1`；mock 日志确认 stall；**`dur` 用物理口径 `[素材时长, 素材时长+stall_ms+300ms]`**（stall 按设计补静音，会顶破 ±10%，与实现无关） |
| 5 | 窗口外帧丢弃 | mock `--tts-late-bin`：`tts_start` 前 / `tts_end` 后各 1 帧 0x02；`vc`「音频丢弃」≥2 帧且字节>0；窗口内计数/播放仍==素材帧数（不误播）；无 ERROR |
| 6 | 合成帧降级 | mock 下发合成帧（不可解码）：本轮仍 `round done`、`active=0`、decoded≤2、收音恢复、**无 ERROR/panic**（bad data 只允许 WARN） |
| 7（可选 `--interrupt`） | **R4 按键打断**（T3.2） | 播放中 `hold_mask=0x2`（仅 TTS 位）+ 收音暂停；`evt post BUTTON`（必须先处于 SPEAKING）→ 打断链路日志齐全、`last_stop_ms<100`、`discard_samples≈800`、打断后 `hold_mask=0x0`、mock `interrupt` 计数=1 且不再发 `tts_end`；随后再跑一轮正常播放（回归 + R5） |
| 8（可选 `--r5`） | **R5 正常收尾无丢弃**（T3.2） | `mic reset` 后正常一轮：`discard_samples==0`、`hold_mask=0x0`、收音恢复「开启」、hold/release 计数各 ≥1 |
| 9（可选 `--r3-duck`） | **R3 音乐 ducking**（T3.2） | 恒定幅度 1kHz MP3 播放中：`player duck on` 后 `out_peak` ≈0.25× 基线（0.15~0.45），`player duck off` 后恢复 ≥0.7× 基线，`duck on` 字段 0/1 正确切换 |
| 10（可选 `--prompt-r1`） | **提示音 R1 原因位**（T3.2 统一门控） | `afplay lexin.wav` 唤醒 → 提示音期间 `hold_mask=0x1`（仅 PROMPT 位）+ 收音暂停 → `prompt done, feed resumed` 后 `hold_mask=0x0` + 收音开启 |

另有全程健康检查：只按 `E (…)`/panic 判失败（`round aborted` 的 W 是预期）；`--no-mock`
时跳过 mock 侧断言与依赖重启 mock 的场景 4~6，并打印 WARN。

**`dur` 断言口径（T2.4-C3，避免误判）**：场景 1（normal）与 5（late-bin）无 stall，`dur` 应
≈ 素材时长，用 **±10%（至少 ±100ms）**；场景 4（stall）按设计在欠载期补静音，物理范围是
**`[素材时长, 素材时长 + stall_ms + 300ms 余量]`**，不能用 ±10%——实测 preroll=2 时 dur≈5310ms、
preroll=3 时 dur=5342ms，均落在物理范围内（通用 ±10% 上限 5332ms 会随机 FAIL，与实现无关）。
判定逻辑见 `tts_stream_test.check_duration()`。

### T3.2 附加场景：R4 按键打断 / R5 无丢弃 / R3 ducking / 提示音 R1（默认不跑）

这四个场景**默认关闭**，需要 Lead 的 T3.2 固件契约就绪后手动加参数跑；固件缺契约时只影响这些
可选场景（记 FAIL 并在输出里提示"契约未就绪"），基础 6 个场景不受影响。新增解析逻辑可先用
`--self-test`（33 项、不连设备）或 `--dry-run`（素材/PyAV 参考 + 自检）离线验证。

**T3.2「统一门控」直接证据（4 条 hold_mask 断言）**：T3.2 的核心是 R1/R2 从
「两套独立开关」改成 `audio_fe` **原因位仲裁**（`AUDIO_FE_HOLD_PROMPT=1<<0` / `AUDIO_FE_HOLD_TTS=1<<1`，
位图归零才真正开麦）。脚本用三条断言直接证明走的是仲裁器、两路 owner 互不覆盖：

| 时机 | 期望 | 由谁断言 |
|---|---|---|
| TTS 播放中（`tts_start` 后 decoded≥8 帧、`stream active=1`） | `hold_mask == 0x2`（只有 TTS 位）+ `状态: 收音 暂停`；日志 `AFE: feed paused (holds=0x02)` | `--interrupt` 场景内、`evt post BUTTON` 之前 |
| R4 打断完成、`AFE: feed resumed` 之后 | `hold_mask == 0x0` + 收音开启、`discard_samples≈800` | `--interrupt` 场景内（已有） |
| 唤醒提示音播放中（`AFE: feed paused` 触发后**紧接着**读） | `hold_mask == 0x1`（只有 PROMPT 位）+ 收音暂停；日志 `AFE: feed paused (holds=0x01)` | `--prompt-r1` 场景 |
| 提示音播完（`prompt done, feed resumed` 后） | `hold_mask == 0x0` + 收音开启 | `--prompt-r1` 场景 |

> 注意：`hold_mask` 只在对应播放期间非 0，窗口很短（提示音 1.28s、TTS 数秒），脚本都是
> **日志触发后立即读**，不用固定 sleep；旧实现的「两套独立开关」在一个 owner 释放时会把另一个
> owner 的暂停顶掉，这四条 mask 断言就是为了抓这种回归。

**设备侧新增可观测契约（脚本只消费、正则容错，不绑死完整行；已对照 Lead 的固件实现核对）**：

| 来源 | 格式 / 子串 | 断言口径 |
|---|---|---|
| `mic stat` 追加行 | `门控: holds=<n> releases=<n> hold_mask=0x<hex> discards=<n> discard_samples=<n> last_discard_ms=<n>`（累计值；旧草稿前缀 `feed:` 也认） | `mic reset` 会把 holds/releases/discards/discard_samples/last_discard_ms 一起清零，**hold_mask 是状态不清**；R4/R5 都用"先 reset 再读绝对值"口径 |
| `player stat` 追加行 | `duck: on=<0\|1> gain_q8=<n> out_peak=<n> stop: last_stop_ms=<n> stops=<n>`（早期草稿字段名 `last_req_to_tx_off_ms` 也认） | `last_stop_ms`=淡出请求→输出静音（Kconfig 淡出 **80ms**=3840 帧，实测 **79~95ms**——最后一块内就归零，**79 是正常值**），R4 只断言上界 `<100`、不设下界；`out_peak`=最近 **320ms** 输出峰值（32×10ms 环），R3 用 duck 前后对比；`stops` 同步递增，脚本用它防 `last_stop_ms=0` 假通过 |
| 探针命令 | `player duck on\|off`（回显 `PLAYERDUCK on=0\|1`）、`player stop [ms]`（默认 80，手动验 R4 淡出用） | R3 用 duck on/off；`player stop` 本脚本不直接用（停声以 stat 为准） |
| 按键事件 | `evt post BUTTON`（payload `action=短按`，经 event_bus → voice_session R4 链路） | **必须在 SPEAKING 时投**：IDLE 下该事件会被 `voice_session` 当成"按键起一轮" |
| 打断链路日志 | `AFE: feed paused (holds=0x…)` / `VS: 打断当前会话（state=…）` / `PLAYER: fade out <ms> ms → stop` / `VC: interrupt sent sid=` / `AFE: feed resumed (discard 50 ms, hold_mask=0x…)` / `VS: state SPEAKING -> IDLE（…原因=interrupt）` | 全部按**子串/正则**匹配（日志含中文与耗时字段，不绑死整行）；`PLAYER: stop` 不一定单独打，停声证据用 `player stat` 的 `last_stop_ms` |
| 相关 Kconfig（默认值） | `AUDIO_PLAYER_INTERRUPT_FADE_MS=80`、`AUDIO_FE_RESUME_DISCARD_MS=50`（→800 样本 @16k）、`AUDIO_PLAYER_DUCK_DB=12`（≈0.25 倍）、`AUDIO_PLAYER_DUCK_RAMP_MS=100`、`AUDIO_PLAYER_TAIL_DELAY_MS=300` | 改这些值会改变断言阈值（`last_stop_ms`/`discard_samples`/duck 比率/恢复时机），改前先同步脚本口径 |
| R4 固件流程 | `vs_interrupt()`：player 淡出请求 → `audio_fe_feed_discard_ms(50)`（TTS hold 还压着时记 pending，release 时才真正开窗）→ `vc_interrupt()` 发 interrupt 报文 + 本地作废本轮 → 回 IDLE；TTS 的 hold 位由播放完成回调释放 | 所以脚本在"播放中（hold 之后）`mic reset`"时 **holds 会被清零、releases 仍 ≥1**：R4 只断言 releases≥1 + discard/hold_mask/收音，**不断言 holds 具体值** |

**`--interrupt`（R4）断言清单**：

1. **用 `vs start` 起轮**（不能用 `vc start`：T2.4 的确定性探针不经过 `voice_session`，VS 会停在 IDLE，`evt post BUTTON` 会被当成「按键开始对话」起新轮、走不到 `vs_interrupt`；这是真机联调实测踩到的，见下）→ `vc stop` → `tts_start` → mock 真实 MP3 播放中：`stream active=1`、已解码 ≥`--interrupt-after-frames`（默认 8 帧 ≈0.4s）、**`vs status == SPEAKING`**；
2. **统一门控证据**：播放中 `mic stat` 的 `hold_mask == 0x2`（只有 TTS 位）+ 收音「暂停」；日志 `AFE: feed paused (holds=0x02)`（`holds` 掩码按十六进制解析，精确等于 TTS 位）；
3. **打断前 `mic reset`**（只清计数、hold_mask 保留）→ 串口发 `evt post BUTTON`；
4. 打断链路：`VC: interrupt sent sid=`（sid 与本轮一致）、`VS: 打断当前会话`、`PLAYER: fade out <ms> ms`、`VS: state SPEAKING -> IDLE`（原因含 interrupt/button）、`AFE: feed resumed`（若带 `discard <n> ms` 再断言 20~100ms）、vc 回 IDLE、stream `active=0`；
5. 停声证据：`player stat` 的 `duck: … last_stop_ms=<n>` **< `--stop-ms-max`（默认 100ms）**，打印 `stops/duck on/gain_q8/out_peak`；
6. 设备侧不再出现本轮 `tts_end`；mock 日志出现 `<-- interrupt sid=` **恰好 1 次**（= `interrupts` 计数 1）、`下发被取消 sid=`，且 **没有 `--> tts_end sid=`**；
7. `mic stat`（打断后等 300ms）：`discard_samples≈800`（50ms@16k，容差 ±320=±1 帧）、`hold_mask=0x0`、收音「开启」、**releases≥1**（holds 取决于 reset 与 hold 的先后——脚本在 hold 之后 reset，holds 会被清零；**不断言 holds 具体值**，只打印）；
8. 回归：`vc reset`+`player reset`+`mic reset` 后再跑一轮正常播放（全套正常断言 + R5 discard==0 + mock 下发完成），证明打断没有破坏后续轮次。

> **R4 真机联调坑（2026-11-01）**：首版脚本用 `vc start` 起轮，`evt post BUTTON` 被
> `vs_on_app_evt` 的 IDLE 分支当成「按键开始对话」（日志 `VS: 按键开始对话（IDLE 短按 → LISTENING）`，
> 原轮次 `round aborted（new-round）`），打断链路完全没触发。产品路径（唤醒→VS LISTENING→TTS→SPEAKING）
> 天然满足"必须 SPEAKING"，所以这是**测试注入方式**的问题、不是固件缺陷：R4 场景必须用 `vs start`
> 让 VS 真正进 SPEAKING，并在发 BUTTON 前断言 `vs status`。已真机验证通过。

**`--r5` 断言清单**：`mic reset` → 正常一轮 → `mic stat` 断言 `discard_samples==0`、`hold_mask=0x0`、收音「开启」、holds/releases ≥1（既有 `check_no_discard()`；`--interrupt` 的回归轮被同一函数覆盖）。

**`--r3-duck` 断言清单**：PC 侧 lameenc 现生成恒定幅度 1kHz 音调 MP3（`--duck-seconds` 默认 4s）→ `player load` 上传 → `player mp3` 播放 → 播放中读 `out_peak` 基线 → `player duck on` 等 `--duck-window`（默认 **0.55s**）→ `out_peak` 为基线 **0.15~0.45 倍**（目标 0.25）→ `player duck off` 等一个窗口 → 恢复 **≥0.7 倍**基线；全程校验 `duck on` 字段 0/1。**为什么窗口默认 0.55s 而不是刚好 0.32s**：`out_peak` 是最近 **320ms** 输出峰值（PEAK_RING_LEN=32×10ms），而 duck 增益按 **100ms** 斜坡走完；窗口太短会把「斜坡开始时还很响的块」算进峰值，导致比率虚高、随机 FAIL。0.55s ≈ 320ms 窗口 + 100ms 斜坡 + 串口往返余量。该场景依赖 `CONFIG_DIAG_ENABLE_PLAYER_TEST=y`（`player load/mp3/duck` 探针，**开发期探针，量产必须关**）。

**`--prompt-r1` 断言清单**：先等 vc IDLE（播放器忙时唤醒会被丢弃）→ `afplay tools/lexin.wav`
（Mac 扬声器**外放**，系统音量临时到 `--volume` 默认 50、结束恢复、支持 `--wake-retries` 默认 2 次）→
等 `AFE: feed paused`（R1）→ **立即**读 `mic stat`：`hold_mask==0x1`（仅 PROMPT 位）、收音「暂停」、
日志 `holds=0x01` → 等 `prompt done, feed resumed`（R2）→ 再读 `hold_mask==0x0`、收音「开启」。
该场景放在所有 vc/duck 场景**最后**执行（唤醒会让 VS 进 LISTENING，可能自动起一轮）。

**前置条件与注意事项**：

- **固件必须含 T3.2 契约**（`mic stat` 门控行 / `player stat` duck+stop 行 / `evt post BUTTON` 短按打断 / R4 链路日志）；固件未就绪时这些场景会 FAIL（输出会提示契约缺失），不要据此判断实现好坏。
- 这些场景**独占串口**：真机验证前先和 Lead 确认时间窗（同一时间只能一个人用设备），并先关 `idf.py monitor`。
- `--interrupt` 需要脚本**自己拉起 mock**（`--tts-mp3`）：`--no-mock` / mock 端口被占用（复用外部 mock）时会 WARN 并跳过打断场景（mock 侧 `interrupt`/`tts_end` 无法断言）。
- `--r3-duck` 需要开发探针配置 `CONFIG_DIAG_ENABLE_PLAYER_TEST=y`；与 mock / vc 无关，放在所有 vc 场景之后执行（`player mp3` 直连播放路径）。
- `--prompt-r1` 会**外放**（`afplay tools/lexin.wav`）并依赖 Mac 扬声器→设备麦克风；系统静音/音量过低/环境噪声大时会漏唤醒（脚本按 `--wake-retries` 重试，仍失败记 FAIL + WARN「属环境问题」）。排查唤醒先用 `uv run tools/voice_test.py --volume 50 --wakes 3`，不要据此判断门控逻辑。
- 断言阈值都可用 `--stop-ms-max` 等参数微调，但**不要为通过而放宽**：R4 `<100ms`、R5 `discard==0`、R3 `0.15~0.45`、hold_mask 精确位（0x2/0x1/0x0）都是 T3.2 的验收口径。

### T3.2 真机验收基线（2026-11-01，固件 T3.2 最终版）

| 跑法 | 结果 | 说明 |
|---|---|---|
| 全量 `--interrupt --r5 --r3-duck` | **307/307 PASS，exit 0** | **基础 193 项全绿** + 114 项 T3.2 R4/R5/R3 新断言 |
| 聚焦 `--interrupt --r5 --r3-duck --prompt-r1 --skip-heap --skip-abort --skip-scenarios` | **162/162 PASS，exit 0** | 四个附加场景（含提示音 R1 原因位）一次跑全绿 |
| `--prompt-r1` 单独（`--skip-heap --skip-abort --skip-scenarios`） | **48/48 PASS**（复跑 2 次） | 环境噪声高时需 1~3 次回放唤醒（`--wake-retries 4`），未改固件断言 |

关键实测值（真机）：

- `hold_mask` 四条证据：TTS 播放中 **0x2**（仅 TTS 位）→ R4 打断后 **0x0**；提示音期间 **0x1**（仅 PROMPT 位）→ 播完 **0x0**；配 `AFE: feed paused (holds=0x02/0x01)` 日志；
- R4：`last_stop_ms` **80 / 77 / 64 ms**（三次，全部 `<100`；64 略低于初始估计的 79~95，断言只设上界）、`stops=1`、`PLAYER: fade out 80 ms → stop`、`VC: interrupt sent sid=`（mock 侧 `<-- interrupt` 计数=1、`下发被取消`、无 `--> tts_end`）；
- R4 丢弃：`discard_samples=800`、`last_discard_ms=50`、`discards=1`（512+288，与 AFE chunksize 口径一致）；
- R5/回归轮：`discard_samples=0`、`hold_mask=0x0`、收音开启、holds=1/releases=1；
- R3 duck：`out_peak` 基线 **15563** → duck 后 **3890**（0.25×）→ 恢复 **15563**（1.00×）；
- 10 轮堆稳定：PSRAM 首末差 **0 B**、内部堆波动达标、全程无 ERROR/panic。

环境口径：本机当前环境底噪偏高（`mic stat` RMS 1000~1500、VAD 常报人声），`--prompt-r1` 唤醒会抖；按 `--wake-retries` 重试，仍失败按 README「环境失败」处理（先用 `voice_test.py --volume 50 --wakes 3` 排查），**不要改固件断言**。

### 与 voice_cloud_test.py 的分工（P2 验收两条都要跑）

| | `voice_cloud_test.py`（T2.5） | `tts_stream_test.py`（T2.4 / T3.2） |
|---|---|---|
| 触发方式 | 回放唤醒词 + 交互语音（真实用户路径） | `vc start`→`vc stop`（确定性，不依赖唤醒/VAD） |
| mock 下发 | **合成帧**（只验协议/帧计数，不可解码） | **真实 MP3**（按帧切分、实时节奏） |
| 关注点 | 握手/上行 Opus/ASR-LLM-TTS 事件/sid 过滤/收尾/内存水位 | 首帧延迟/预缓冲/欠载/解码对拍/abort 停声/排空/10 轮堆稳定 |
| T3.2 半双工 | 端到端路径（唤醒→R1/R2） | **R4 按键打断 / R5 无丢弃 / R3 ducking / 提示音 R1 原因位**（确定性注入） |
| 额外参数 | `--inject-late`（sid 迟到事件） | `--tts-late-bin`（窗口外 0x02 帧）、`--stall-*`、`--interrupt`/`--r5`/`--r3-duck` |

### 参数 / 依赖 / 限制

- 参数：`--port`、`--mock-port`（默认 8765）、`--no-mock`、`--no-reset`、`--dry-run`、`--log`、
  `--round-timeout`（默认 30s）、`--think-ms`（mock 首事件延时，默认 100）、`--pace-pct`（默认 80，
  见下）、`--material-repeats`（默认 3）、`--kbps`（默认 32）、`--heap-rounds`（默认 10）、
  `--stall-ms`/`--stall-at`（默认 600/30）、`--skip-heap|--skip-abort|--skip-stall|--skip-late|--skip-synthetic|--skip-scenarios`。
- **T3.2 可选场景参数**：`--interrupt`、`--r5`、`--r3-duck`、`--prompt-r1`；`--interrupt-after-frames`（默认 8，
  起播后解码多少帧再打断）、`--interrupt-timeout`（默认 5s，等打断链路日志）、`--stop-ms-max`
  （默认 100ms，`last_stop_ms` 上限）、`--duck-seconds`/`--duck-warmup`/`--duck-window`
  （R3 音调素材时长 4s / 基线窗口 0.6s / duck 窗口 0.55s，见 R3 断言清单）；`--prompt-r1` 相关：
  `--volume`（默认 50=用户定稿上限，0=不改系统音量）、`--wake-timeout`（默认 5s）、
  `--wake-retries`（默认 2）、`--prompt-timeout`（默认 8s）；`--self-test` 只做离线解析自检。
- **`--pace-pct` 为什么默认 80 而不是 100**：mock 缺省实时节奏（100% = 契约默认）；但 macOS
  `asyncio.sleep` 每帧超调约 1.7ms（≈7%），100% 会让发送平均慢于播放、产生**假欠载**。本脚本用
  80%（≈20.9ms/帧 vs 播放 24ms/帧）让发送略快，给端侧留缓冲；真实欠载由场景 4 的 stall 强制制造。
- 依赖：PEP 723 内联 `pyserial` / `lameenc` / `av` / `numpy`，`uv run` 自动安装。脚本**独占串口**
  （先关 `idf.py monitor`）；**默认不改系统音量、不外放**（T2.4 场景不依赖 Mac 扬声器回放）；
  只有 `--prompt-r1` 例外：外放 `afplay lexin.wav`，并临时把系统音量调到 `--volume`（默认 50）后恢复。
- 场景 4~6 需要脚本自己拉起 mock 并在场景间**重启 mock**（切换 `--tts-mp3`/stall/late 参数），
  重启后脚本用 `ws reconnect` 让设备快速重连；`--no-mock` 或端口被占用（复用外部 mock）时这三项跳过。
- mock 已默认对所有连接开 `TCP_NODELAY`（T2.4-C2）。若真机稳态仍出现 `underrun>0`，先确认跑的是带
  NODELAY 修复的 mock（`ws_mock_server.py --self-test` 含该断言、启动横幅有 `TCP_NODELAY=1` 标注），
  再排查；**不要通过放宽 underrun 阈值或调大 pace 掩盖**（真实云端同样应关 Nagle）。
- 设备侧探针：`player stat` 的 stream 行由 T2.4-A 常驻提供；`player reset` 会清空流式统计（契约 §1.3），
  每个场景前脚本都会 `vc reset` + `player reset` 让计数从 0 开始。T3.2 的 `--r3-duck` 还依赖
  `CONFIG_DIAG_ENABLE_PLAYER_TEST=y` 的 `player load/mp3/duck` 探针（开发期，量产必须关）。
- **离线先自检**：`--self-test`（33 项，不连设备）验证 T3.2 新契约的正则/解析/阈值判定（含负例、hold_mask 原因位）；
  `--dry-run` 额外生成素材 + PyAV 参考并跑同一套自检。真机跑 `--interrupt/--r5/--r3-duck` 前务必
  先跑这两个，避免把脚本自身问题误判成固件问题。

## mixer_test.py —— 三通道混音真机验收（T3.3-B）

**作用**：验证 T3.3 的「三通道混音（CH_TTS/CH_PROMPT/CH_MUSIC）→ 音量 → -1dBFS 软限幅 →
I2S」数值正确性，不做"能出声"的定性判断，而是把 48k 单声道混音输出**抓回 PC 做 FFT/包络/阶跃
断言**：三通道叠加的幅度比与通道 duck、音量线性、软限幅上限与 limiter/clip 计数、10ms 淡入 /
100ms 淡出 / 通道启停无 pop、10 轮堆/PSRAM 稳定。只消费固件探针输出、不改固件、不联网、不唤醒、
不外放（不需要 Mac 扬声器），是 `audio_player` 混音改动后的定点回归手段。

```bash
uv run tools/mixer_test.py                      # 全量真机验收（默认不复位设备，约 60~90s，独占串口）
uv run tools/mixer_test.py --self-test          # 离线自检解析/FFT/断言逻辑（50 项，不连设备）
uv run tools/mixer_test.py --dry-run            # 不连设备：打印执行计划 + 跑同一套离线自检
uv run tools/mixer_test.py --skip-heap          # 跳过 F 健康场景（快速冒烟）
uv run tools/mixer_test.py --reset              # 先复位设备再等 console 就绪
uv run tools/mixer_test.py --port /dev/cu.usbmodemXXXX --log /tmp/mixer_test.log
```

一条命令完成：打开串口 → 等 `player mix stat` 可解析（确认 T3.3 探针已就绪）→ `player mix stop` +
`player mix reset` 清理 → 依次跑 6 个场景（每场景前 `stop`/`reset`，场景间留 200ms）→ 汇总
`[PASS]/[FAIL] 场景名 描述` + 最后一行 `MIXER_TEST: PASS|FAIL`，退出码 0/1。

### 设备侧探针契约（固件输出以此为准，脚本只消费、不猜字段）

契约出处：T3.3-A 固件实现——`components/diag/diag.c` 的 `print_mix_stat()` 与 `player mix`
子命令（stat/reset/vol/tone/stop/cap/dump 的 printf 格式）、`components/audio_player/include/
audio_player.h` 的 `audio_player_mix_*` 接口注释、`audio_player.c` 的 `mix_render()`（duck/淡入
淡出/音量/`mix_soft_limit()`）与 `audio_player_mix_capture*`。**格式以固件实测输出为准**，有偏差
先回报 Lead 再同步本节；脚本开头的 docstring 是逐字契约副本，本表只列用途：

| 探针 | 输出 | 脚本用途 |
|---|---|---|
| `player mix stat` | 4 行：`MIXSTAT vol/vol_q8/vol_target/limiter/limiter_peak/clip/mix_peak/block_max_us/**cap_len/cap_pos**` + `MIXCH ch=TTS\|PROMPT\|MUSIC active/gain_q8/level_ms/peak` | A/C/D/E/F 的 clip/limiter/mix_peak/通道 duck 证据；`cap_len`=武装帧数、`cap_pos`=实际抓到帧数（决定 dump 上界）；就绪探测与初始音量 |
| `player mix reset` | `MIXRESET ok=1` | 每场景前清 limiter/clip/mix_peak/block_max 与通道 peak |
| `player mix vol <0..100>` | `MIXVOL vol=<n> err=ESP_OK` | A/C/D/E/F 前置 vol=100；B 的 100/50/0 三档；结束恢复初始音量 |
| `player mix tone <CH> <freq 100~4000> <ms 50~10000> [amp 100~20000]` | `MIXTONE ch/freq/ms/amp/err` | A/C 三通道叠加；B/D 单通道；E 先后叠加 MUSIC→PROMPT |
| `player mix stop` | `MIXSTOP ok=1` | 每场景前清理；脚本开头/结束清理 |
| `player mix cap <1..16384>` | `MIXCAP armed=<frames>` | 抓取武装（取满自动停；播放空闲时等下一块；**叠加场景可在泵运行时重新武装**） |
| `player mix dump <off> <n 1..512>` | `MIXCAP off=<off> count=<n>` + 每行 16 个 `%04x`（**连写、无分隔符**，一行 64 字符）+ `MIXCAPEND`；越界时只有一行 `MIXCAP err=ESP_ERR_INVALID_STATE` | 取回抓取数据（int16 二补码，`fc18`→-1000）；`off+count > cap_pos` 视为抓取不完整/失败，脚本每个场景先 stat 取 `cap_pos` 再只 dump `[0, cap_pos)` |

字段语义：`gain_q8` 256=0dB、128≈-6dB、64≈-12dB；`mix_peak` 是最近 ~320ms 输出峰值；`limiter`
是最近窗口内软限幅样本数；`clip` 是硬削波次数（必须恒 0）；`block_max_us` 是最近单块混音+限幅
耗时（10ms 预算）；`cap_len` 是最近一次 `cap` 武装帧数、`cap_pos` 是**实际已抓帧数**（播放提前
结束时 < cap_len；A/B/C/E 音调 >341ms 应取满，D 的 300ms 音调 cap_pos=14400）。脚本先 stat 取
`cap_pos`，再按它决定 dump 范围；`MIXSTAT` 缺这两个字段时脚本直接报"需要最新 T3.3-A 固件"。所有命令发送为一行文本 + `\r\n`；设备日志/console 混流，脚本只按上面固定前缀
匹配、其它行丢弃，命令发出后最多等 2s（dump 因数据回传放宽到 3s）。

### 场景与断言表

| 场景 | 指令 | 断言（阈值） |
|---|---|---|
| A 三通道混音 | 先 tone TTS 1000 / PROMPT 440 / MUSIC 1760（各 **2500ms** amp 8000）→ 等 **0.35s**（duck 斜坡走完）→ 再 `cap 16384` → FFT 窗 0..8192 | 三频幅度 ≈8000/4000/2000（**±8%**，即 prompt duck -6dB、music duck -12dB）；比值 1:0.5:0.25（±8%）；`gain_q8` 256/128/64（±8）；三通道 `active=1`；抓取取满 `cap_pos==cap_len`；`mix_peak` 与抓取峰值一致（±2%）；`clip=0`；`block_max_us` 超 10ms 预算只记 **WARN**（性能观测项，偶发抢占不算功能回归） |
| B 音量线性 | 先 `vol 100/50/0`，再 `cap` → tone MUSIC 1000/1500ms amp 8000 | 稳态幅度 ≈8000/4000/**0**（**±2%**）；vol 0 档**稳态窗**全零且 `mix_peak=0`（20ms 音量斜坡只在混音泵渲染时推进，0 档抓取开头 ~20ms 有上一档残余音量）；各档 `active=1` |
| C 软限幅 | `cap` → tone TTS 440 / PROMPT 1000 / MUSIC 1760（各 1500ms amp 20000） | 稳态窗逐样本 `|x|≤29196+32`（-1dBFS）；无 `|x|≥32000` 硬削样本；`limiter>0`；`limiter_peak>29196`；`clip=0`；`mix_peak` 与峰值 ±2% |
| D 淡入淡出 | **先** `cap 16384` → tone TTS 1000/300ms amp 8000 → 只 dump `cap_pos`（≈14400） | 抓取含整段 300ms（14400±480 帧）；起始 10ms 包络从 0 单调升（首帧很小、首 1ms 峰值 ≤稳态 15%）；结束前 100ms 包络单调降、最后 10ms ≤稳态 15%、**末 1ms 包络 <5% 稳态**；全程相邻样本 `|Δ|≤1600` |
| E 通道启停不 pop | tone MUSIC 1760/1500ms → 300ms 后 `cap` 武装 → 叠加 tone PROMPT 440/800ms | **用 440Hz 能量定位叠加点 onset**，稳态窗从 `onset+10ms(淡入)+100ms(duck 斜坡)` 起（自适应 4096~8192 帧）：440Hz 峰 ≈8000（7000~9000）；1760Hz 峰 ≈2000（1700~2300，PROMPT 活跃令 MUSIC duck -12dB）；RMS≈5831（5200~6500）；onset 处 10ms 窗 440Hz 仍低（证明抓到叠加点）；全程 `|Δ|≤3480`（1760+440 合成斜率上限），无单点跳变；`clip=0` |
| F 健康 | 10 轮 `stop/reset → cap 2048 → 三通道 150ms 音调 → sys heap` | 内部堆峰谷差 ≤2KB；PSRAM 峰谷差 ≤4KB；每轮 `clip=0`；全程日志无真错误（`E (`/`ERROR`/`panic`/`assert`/`Guru`）；**连接/重连类既有噪声忽略**：TAG ∈ {NET, WS, wifi, websocket_client, esp-tls, transport_base, transport_ws, transport_ssl, transport_tcp, esp_netif, net80211, phy_init, esp_wifi, wpa_supplicant, …}，或行内含 `Failed to open a new connection`/`Error connecting to host`/`esp_transport_connect`/`WS 错误`/`连接断开`/`后重连`（mixer_test 不起 mock，设备反复重连 192.168.1.11:8765 会刷这些 E 行） |

### 与其它脚本的分工（P3 验收各跑各的，阈值互不复用）

- `mixer_test.py`（本脚本）：**T3.3 混音链路的数值正确性**——三通道叠加/duck/音量/软限幅/
  淡入淡出/堆稳定。不联网、不唤醒，用 `player mix tone` 探针音调 + `cap/dump` 抓取。
- `tts_stream_test.py`：**T2.4 真实 MP3 流式播放**的音频硬指标（首帧延迟/欠载/abort/打断），
  用 mock 下发真实 MP3；`--r3-duck` 是旧的 ducking 探针（`player duck`），与 T3.3 三通道混音
  的 `MIXCH gain_q8` 是两个层面，不要互相替代。
- `mp3_test.py`：整段 MP3 **解码对拍**（`player pcm` vs PyAV）；`voice_cloud_test.py`：端云协议/
  事件/sid 过滤。P3 验收时本脚本与它们都要跑，但各自只看自己的契约。

### 参数 / 依赖 / 限制

- 参数：`--port`、`--log`（设备日志文件；末尾会追加 `[ASSERT]` 断言明细，含每条 FAIL 的实测
  detail，方便真机跑完事后定位）、`--dry-run`、`--self-test`、`--reset`、`--ready-timeout`（默认 15s）、
  `--heap-rounds`（默认 10）、`--skip-heap`、`--cap-frames`（默认 16384=341ms）、`--steady-start`
  （稳态窗起点，默认 4800=100ms）、`--fft-n`（默认 8192≈170ms）、`--dump-chunk`（默认 512）。
- 依赖：PEP 723 内联 `pyserial>=3.5` + `numpy>=1.26`，`uv run` 自动安装；不用 scipy（rFFT + 汉宁窗，
  幅度 ≈`2·|X[k]|/sum(win)`，取最近 bin±1 后再按汉宁窗 `sinc(δ)/(1-δ²)` 反卷积校正 scalloping loss——
  1000Hz/1760Hz 在 8192 点时分别偏离 bin 中心 0.33/0.37 bin，直接取峰会低估 7%~9% 而吃掉 ±8% 容差）。
- 脚本**独占串口**（先关 `idf.py monitor`）；默认**不复位**设备（烧录后设备已在跑），加 `--reset`
  才会复位。进入时读一次 `player mix stat` 的 `vol`，A/C/D/E/F 前置 `vol 100` 作为幅度基准，
  结束 `player mix vol <原值>` 恢复（该探针不落盘，重上电回配置值）。
- **A/E 真机时序校准（2026-11-02 实测回灌）**：`player mix tone` 每条命令约 100ms 往返，
  PROMPT/MUSIC 分别在 ~100/~200ms 才真正起播；且 duck 斜坡从"通道真正 active"才开始走。
  因此 A 必须"先起三路 → 等 0.35s（duck 斜坡走完）→ 再 arm cap"（否则抓取窗前段是部分未 duck
  的混合区，440/1760 幅度偏高——真机首轮就是这么 FAIL 的）；E 的 cap 与 tone 命令之间也有
  ~50~100ms 往返，抓取窗开头是"MUSIC 全量"段，必须先用 440Hz 能量找到叠加点 onset，再从
  `onset+110ms` 起取稳态窗（总抓取 16384 帧在 onset≈50~100ms 时仍够 170ms 窗，不够时自适应缩窗）。
- **E 为什么不是两路都 8000**：PROMPT 活跃时 MUSIC 会 duck 到 -12dB（固件
  `voice_on = tts_on || prompt_on`），所以稳态是 440Hz≈8000 + 1760Hz≈2000，RMS≈5831。
- **D 为什么 dump `cap_pos`（14400）而不是 16384**：`audio_player_mix_capture_read()` 按实际抓取数
  `cap_pos` 校验（`off+count > cap_pos` 回 `MIXCAP err`）；300ms tone 恰好渲染 30 个 10ms 块 =
  14400 帧后泵退出，故 `cap_len=16384、cap_pos=14400`，只取 `[0, cap_pos)`。A/B/C/E 音调时长
  >341ms，抓取取满（`cap_pos==cap_len==16384`），脚本会断言"取满"再按固定稳态窗 dump。
- **D 的淡出尾值**：固件旧实现淡出步进 `65536/4800` 向下取整=13，走到最后还剩 ~4.8%（真机实测
  末样本 |x|=381），已由 Lead 改为向上取整改烧；脚本阈值按契约"末 1ms 包络 <5% 稳态"断言，
  固件修复后应≈0（自检里 4.8% 边界正例 / 6% 负例都覆盖）。
- **B 的 vol 0 为什么查稳态窗**：音量 20ms 斜坡在 `mix_render()` 里按块推进，set vol 时若泵没在跑，
  目标值要等下一次泵启动才斜坡过去；从 50 切到 0 时抓取开头 ~20ms 仍有残余音量，100ms 后的稳态窗
  才能严格全零（`mix_peak` 的 320ms 窗口也已越过这段）。
- 串口读取用 5ms timeout + 按 `in_waiting` 整批取，而不是既有脚本的 `read(8192)`（pyserial 会等满
  timeout 才返回，50ms 会在每条命令/每个 sleep 上累积 ~100ms 误差，把 E 的叠加点推出抓取窗）。
- 抓取数据里的 `mix_peak` 与 `cap` 窗不是同一段时间：A/C 在抓取取满后、音调仍播放时读 `stat`
  （mix_peak 最近 ~320ms 有效），再 dump 稳态窗，两者峰值应一致（±2%）；D（300ms 音调已结束）不比对
  mix_peak。
- **F 的日志错误过滤口径**（与 `voice_test.py`/`tts_stream_test.py` 一致并扩展）：按 TAG 白名单忽略
  网络/WS/连接重连类 E 行（`NET`/`WS`/`wifi`/`websocket_client`/`esp-tls`/`transport_base`/
  `transport_ws`/`transport_ssl`/`transport_tcp`/`esp_netif`/`net80211`/`phy_init`/`esp_wifi`/
  `wpa_supplicant` 等），并加一层行内兜底（`Failed to open a new connection` / `Error connecting to
  host` / `esp_transport_connect` / `WS 错误` / `连接断开` / `后重连`）。**panic / assert failed /
  Guru / 其它 `ERROR` 不被白名单放行**，仍判 F FAIL——不掩盖真实错误。mixer_test 不启动 mock，
  设备反复重连 192.168.1.11:8765 时 `transport_base/transport_ws/websocket_client` 的 E 行属噪声。
- 环境性失败口径：串口超时、回显被日志冲掉、抓取不足等记 `WARN` 并**整场景重试一次**；固件真的
  不满足阈值时只 `FAIL`，**不改阈值、不用重试掩盖问题**。
- **离线先自检**：真机前先跑 `--self-test`（46 项，覆盖 MIXSTAT（含 cap_len/cap_pos 与旧固件兼容）/
  MIXCH/MIXVOL/MIXTONE/MIXCAP/dump 解析（含固件"连写 `%04x`"格式与 `MIXCAP err`）、uint16→int16、
  FFT 幅度与幅度比、音量线性、E 的 duck 幅度/RMS、淡入/淡出/斜率/软限幅/堆判定，含正负例）和
  `--dry-run`（打印执行计划 + 同一套自检）。真机基线（每场景实测值）待 Lead 烧录 T3.3 固件后回填本节。

## e2e_voice_test.py —— 端到端语音真机自测（T2.6，P2 收口）

**作用**：把 T2.5（`voice_cloud` 端云协议）与 T2.4（`audio_player` 流式播放）拼成**真实用户路径**
并做 10 轮稳定性验证：`afplay lexin.wav` 唤醒 → 提示音 → **自动起轮** → `afplay 今天天气如何.wav`
模拟用户说话 → 上行 Opus → mock asr/llm/tts_start → **真实 MP3 0x02 流** → 设备流式播放 →
`tts_end` 排空 → `feed resumed`（R2），逐轮断言并汇总（最后一行机器可读 + 退出码 0/1）。
这是 P2 验收「完整说话→听回答链路」的脚本；真实云端联调（T5.4）前，它是端到端能力的最终回归。

一条命令完成：PC 侧生成 TTS 素材（`tools/今天天气如何.wav` → lameenc 24k 单声道 MP3，拼接 3 份
≈4.8s，与 `tts_stream_test.py` 同口径 **202 帧 / 19392 B / 4848 ms**，PyAV 独立解码出参考）→ 起本地
mock（`--tts-mp3 <file> --tts-pace-pct 80`，自动拉起/回收）→ 复位设备 → 等 `WS: 握手完成（hello_ack）`
→ 回放唤醒词（系统音量临时调到 **50%** 上限，结束恢复；`afplay` 默认增益）→ 等
`APP: prompt done, feed resumed` 与自动轮次 `VC: round start`（= UPLINK）→ 回放交互语音 →
等 `audio_end（vad-silence）` → 下行真实 MP3 流式播放 → 等 `stream end` / `round done` /
`VC: stream done → feed resumed（R2）` → 逐轮断言 → 10 轮汇总 PASS/FAIL。

```bash
uv run tools/e2e_voice_test.py                 # 全量 10 轮（默认复位设备、独占串口，约 3~4 分钟）
uv run tools/e2e_voice_test.py --dry-run       # 只生成素材 + PyAV 参考 + 环境/串口检查，不碰设备
uv run tools/e2e_voice_test.py --rounds 1 --min-rounds 1   # 冒烟 1 轮
uv run tools/e2e_voice_test.py --keep-volume   # 不动系统音量（唤醒率取决于当前音量）
uv run tools/e2e_voice_test.py --allow-max-duration        # 环境人声持续时放宽 vad-silence 断言
uv run tools/e2e_voice_test.py --no-reset --log /tmp/e2e.log
```

### 每轮断言（每条附设备日志/统计证据）

| 组 | 断言 |
|---|---|
| 唤醒链路 | `wake detected` → `AFE: feed paused` → `PLAYER: play N samples` → `prompt done, feed resumed` **四段齐全且顺序正确** |
| 自动起轮 | 提示音后由 `vc_start_auto()` 自动起轮（日志 `VC: round start sid`）；`round start sid == audio_end sid == mock audio_start sid` 三者一致（不依赖脚本发 `vc start`） |
| 上行 | `audio_end 原因=vad-silence`；上行帧 ≥60；上行首帧 <200000 us；设备 `audio_end` 帧数 == mock 逐帧计数 == mock `audio_end` 计数；上行 seq 无跳变/无坏帧 |
| 下行 | `tts_start` codec=mp3/sr=24000 且 sid 一致；`tts_end` 计数 ==202；设备收到 19392 B；`fed==decoded==202`、`samples==116352`；`first_sound_us<150000`（另断言日志 `stream first sound <150 ms` 双口径）；稳态 `underrun=0`、`bad=0`、`dropped=0`、`ring_ms=0`、`dur≈4848 ms`（±10%） |
| 半双工 R1/R2 | `VC: stream open ... feed paused (R1)` 出现且 sr=24000；播放中 `mic stat` 状态=**收音 暂停**；收流后 `VC: stream done → feed resumed（ok=1）`，且顺序 `tts_end → stream end → feed resumed`；结束后 `mic stat` 状态=**收音 开启** |
| 拾音（功放真出声，**噪声鲁棒**） | 播放期 `mic stat` 稳态峰值 vs baseline=**min(播放前静音窗口峰, 播放后静音窗口峰)**（播放后窗口在 R2 后取，`--post-baseline-window` 默认 0.8s）。**baseline<800**：严格判据 `播放峰 ≥ max(1000, 1.5×baseline)`；**baseline≥800**：环境人声/噪声已盖过外放拾音 → 整轮重试（同 `--max-retries` 预算），重试耗尽记 **WARN/inconclusive**（汇总单列计数+百分比，不判 FAIL）。起播头 ~1s 拾音偏弱，先 `--playback-warmup`（1.2s）再取 `--playback-window`（1.2s）稳态窗口；不 warmup 会把「起播瞬间」误判成播放期 |
| 统计自洽 | `vc status`：发起/完成/中止 = 1/1/0、上行帧==audio_end 计数、下行音频帧==202、音频丢弃 0、sid 丢弃 0、解析失败 0、云端 error 0 |
| mock 侧 | 收到 hello/audio_start/audio_end；sid 三方一致；逐帧 1:1；`tts 下发完成 frames=202 bytes=19392 mode=mp3` |
| 健康 | 本轮无 `E (...)`/panic（`round aborted` 的 W 是预期，不算错误） |

**10 轮汇总断言**：成功轮次 ≥ `--min-rounds`（默认 8）/10；所有成功轮次**硬断言**全绿；内部堆 10 轮
波动 ≤2KB 且不单调下降；PSRAM 首末差 ≈0（≤8KB）且不单调下降；任务数不变；全部成功轮次欠载总数 ==0；
全程无 ERROR/panic；mock 全程无 seq 跳变/坏帧。拾音判据的 **WARN/inconclusive 单独计数并打印百分比**
（不判 FAIL，但不得静默）。

### 环境重试策略（不掩盖固件问题）

真机链路依赖 Mac 扬声器回放与设备麦克风，**环境底噪/人声会同时影响唤醒率和 VAD 收尾**：
当环境有人声/噪声持续时，`voice_cloud` 的 VAD 可能一直等不到 800ms 静音，本轮会走 8s 上限
（`audio_end 原因=max-duration`）。这类**环境噪声**不是固件缺陷，脚本按「每轮最多重试 2 次」处理：

- 每次尝试先记录静音段底噪（RMS/峰值）；唤醒未触发或 `audio_end=max-duration` → 该次尝试的断言行
  **整体丢弃**（打印 WARN，不污染 PASS/FAIL），整轮重试，最多 `--max-retries`（默认 2）次；
- 拾音判据的 baseline=min(播放前, 播放后静音窗口峰值)。若 baseline≥**800**（环境噪声已盖过外放拾音）
  → 该轮按环境性整轮重试（同上预算）；重试耗尽仍噪声时，拾音项记 **WARN/inconclusive**
  （汇总单列「轮次/百分比/baseline/播放峰」，不判 FAIL、不静默），其余硬断言仍要求全绿；
- 重试耗尽 → 本轮记为「环境失败」，**不计入断言**（对应 voice_cloud_test 的 `--allow-max-duration` 口径）；
  最终要求仍是 **≥8/10 成功轮次且全部成功轮次全绿**，并有至少 1 次干净 10/10 的验收记录；
- 若唤醒已触发但提示音链路/下行/统计不完整 → **硬失败**（不做环境重试），避免把固件 bug 当噪声；
- `--allow-max-duration`：直接把 `max-duration` 视为可接受（环境人声持续时用，仍会打 WARN）。

环境持续不佳（多轮重试仍失败）时先安静环境/靠近设备复跑，并把 `mic stat` 底噪与失败轮次证据一起上报
（本项目口径：环境噪声导致的失败不是固件问题，**不要改阈值掩盖**）。

### 实测基线（2026-10-31，T2.6-A / T2.6-A2，固件 T2.4 最终版 + 默认音量 100，本地 ws mock）

- **拾音判据噪声鲁棒化后连续两次独立跑均 10/10 轮 PASS**（第 1 次 667 项、第 2 次 665 项断言，
  退出码 0；项数差异来自环境重试的 WARN 行数），耗时 2 分 59 秒 ~ 3 分 34 秒。
- 拾音判据两次跑**全部严格通过**（baseline 88~630 < 800，播放峰 1743~2537，逐轮 baseline/播放峰
  见汇总表）。lead 复跑时 baseline 1348/1540 的噪声场景按新口径走「整轮重试 → WARN/inconclusive」
  （离线用例已覆盖：同一输入判 noisy、不产生 FAIL 行；重试耗尽仍记 WARN 并单列计数）。
- 逐轮（第 1 次）：上行 191~226 帧、首帧 8569~35836 us、`first_sound_us` **7429/30696/41789**
  （min/中位/max，全部 <150000）、`underrun=0`、`decoded=fed=202`、`dur=4850~4853 ms`、
  播放峰 1766~2537、baseline 160~630。
- 环境重试实况：第 1 次跑轮 2 首试 `max-duration`、次试唤醒被环境人声打断（底噪 RMS 467/峰值 2262），
  第 3 次全绿；第 2 次跑 10 轮全部首试通过。
- 内存/任务（两次一致）：内部堆剩余 43,311~43,527 B（10 轮波动 **216 B**、不单调下降）、PSRAM
  剩余 6,845,192 B（首末差 **0**）、任务数恒 **17**；`sys heap` 内部峰值占用 228,652 B /
  最低水位 31,595~32,351 B（ws:// mock 口径；wss 口径见 T2.4 基线）。
- 环境：系统音量 28% → 临时 50% → 恢复；安静段底噪 RMS 56~467、峰值 88~2,262（间歇人声会拉高，
  是环境重试与拾音 inconclusive 的来源）。

### 实测基线（2026-11-01，T3.1 会话状态机，本地 ws mock）

- **10/10 轮 PASS、677 项断言全过**（失败项 0）；唤醒 10/10、内部堆 43,051~43,103 B（波动 52 B、
  非单调）、PSRAM 首末差 0、任务数恒 17（**未新增任务**）、欠载 0、无 ERROR/panic。
- 逐轮：上行 191~277 帧、首帧 22,970~30,693 us、`first_sound_us` **7,986/16,918/29,864**
  （min/中位/max，全部 <150000）、`underrun=0`、`decoded=fed=202`、`dur=4849~4853 ms`。
- 拾音判据 3/10 轮 inconclusive（环境 baseline 1055/1298/3516 ≥ 800，播放峰 2195~4559），
  按 T2.6-A2 口径记 WARN 不判 FAIL，硬断言仍全绿。
- 会话状态机：本轮日志出现 `VS: state IDLE -> LISTENING` → `LISTENING -> THINKING` →
  `THINKING -> SPEAKING` → `SPEAKING -> IDLE`（sid 恒定），与 `voice_cloud_test` 的状态断言一致。
- **环境噪声口径**：同日中午环境底噪升高（基线峰 1,100~2,000）后复跑，出现 8/10（失败轮 2）与
  7/10（失败轮 3），失败轮**全部**是 `max-duration`（VAD 8000ms 未静音）的环境失败，成功轮断言全绿；
  同期 `voice_test.py --volume 50 --wakes 3` 仍 16/16（唤醒 3/3、底噪 RMS 1008），确认非固件回归。
  按 T2.6-A2 口径：环境失败不计入硬断言，需在安静环境复跑取 10/10 记录。

### 与 voice_cloud_test.py / tts_stream_test.py 的分工（P2 验收三条都要跑）

| | `voice_cloud_test.py`（T2.5） | `tts_stream_test.py`（T2.4 / T3.2） | `e2e_voice_test.py`（T2.6） |
|---|---|---|---|
| 触发方式 | 回放唤醒词 + 交互语音（真实路径，1 轮） | `vc start→stop` 确定性触发（不依赖唤醒） | **回放唤醒词 + 交互语音，10 轮** |
| mock 下发 | 合成帧（只验协议/帧计数） | 真实 MP3 | **真实 MP3** |
| 关注点 | 上行 Opus、ASR/TTS 事件、sid 过滤、stop/cancel 收尾 | 首帧延迟/欠载/解码对拍/abort/排空/10 轮堆稳定 | **端到端链路 + R1/R2 + 统计对账 + 10 轮稳定性 + 播放期拾音** |
| 额外参数 | `--inject-late` | `--tts-late-bin`、`--stall-*`、`--interrupt`/`--r5`/`--r3-duck` | `--max-retries`、`--allow-max-duration`、`--playback-warmup/window` |
| 不覆盖 | 真实 MP3 播放、10 轮 | 唤醒/VAD/真实用户路径 | abort 停声延迟、stall 欠载、迟到帧、合成帧降级、R4 打断/R5 无丢弃/R3 ducking（这些仍由 tts_stream_test 覆盖） |

### 参数 / 依赖 / 限制

- 参数：`--port`、`--mock-port`（默认 8765）、`--rounds`（默认 10）、`--min-rounds`（默认 8）、
  `--max-retries`（默认 2）、`--volume`（默认 50=用户定稿上限，结束恢复）/`--keep-volume`、
  `--allow-max-duration`、`--think-ms`（mock 首事件延时，默认 300）、`--pace-pct`（mock 真实 MP3 节奏，
  默认 80，原因同 `tts_stream_test`：macOS `asyncio.sleep` 每帧超调 ~1.7ms，100% 会产生假欠载）、
  `--material-repeats`/`--kbps`、`--boot-timeout`/`--wake-timeout`/`--round-timeout`/`--tts-timeout`/
  `--drain-timeout`/`--idle-timeout`、`--pause-between`、`--playback-warmup`/`--playback-window`/
  `--post-baseline-window`、
  `--no-mock`/`--no-reset`/`--dry-run`/`--log`。
- 依赖：PEP 723 内联 `pyserial` / `lameenc` / `av` / `numpy`（素材生成 + PyAV 参考解码），`uv run` 自动安装。
  脚本**独占串口**（先关 `idf.py monitor`）；默认复位设备；会真实外放声音（mock 下发 4.85s 中文 TTS，
  设备音量 100），属正常现象。
- 设备侧依赖：`CONFIG_DIAG_ENABLE_PLAYER_TEST=y` 的 `player stat` stream 行 / `mic stat`
  （开发期探针，**量产必须关闭**）；`vc start/stop/cancel/reset` 与 `vc status` 的字段格式以
  `docs/ARCHITECTURE.md` §6/§10.2 为准（脚本只消费、不猜字段）。
- 跑测试时**宿主不要并发重负载**：mock 进程被抢占 >150ms 会造成 1 次真欠载（环境噪声，重跑即可，
  不要改阈值/调大 pace 掩盖）。真实云端同样必须关 Nagle（见 `ws_mock_server.py` 章节）。

## ws_mock_server.py —— 本地「云端」WS 模拟服务（T1.5 / T2.4）

**作用**：在真实后端就绪前，用一台电脑模拟云端 WebSocket 服务（握手、心跳、事件下发、TTS 音频流），让端侧 T1.4/T1.6 以及 P2–P4 全部能脱机联调（ROADMAP §2.0 P1 顺序 0：可最早启动，端侧不必等后端）。T2.4 起支持 `--tts-mp3` 下发**真实 MP3**（按帧切分、实时节奏），并可用 `--tts-stall-*`/`--tts-late-bin` 制造欠载与窗口外帧。

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
# [1/13] PASS  hello / hello_ack 握手与参数解析
# ...
# self-test: 13/13 PASS   退出码 0
```

进程内起服务 + 起客户端，覆盖 **13 项**：`hello`/`hello_ack` 握手、**accept 连接 `TCP_NODELAY=1`（getsockopt 校验，防 Nagle 假欠载）**、ping→pong、一轮语音的事件顺序/`sid` 回填/二进制帧头、**tts-mp3（真实 MP3 按帧切分/帧头连续/实时节奏）**、**tts-stall（中途停发且不丢帧）**、**tts-late-bin（tts_start 前/tts_end 后各 1 帧窗口外注入）**、坏帧容错、`interrupt` 取消、断线重连、TTS 流中 ping、token 拒绝关闭码、`--drop-pong`。

自测用的真实 MP3 以 base64 内嵌在脚本里（lameenc 生成、22 帧/528ms），**不依赖 `av`/`lameenc`**，
仍是一行命令零硬件；内嵌码流同时验证解析器能跳过 ID3v2 头。

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

### 3. 真 ASR：mock 用本地 SenseVoice 识别上行音频（T3.1 测试用）

默认 mock 的 `asr_final.text` 是固定文案（`--tts-text`），只能验证链路。加 `--asr` 后，mock 会把
`audio_start → audio_end` 之间的上行 **Opus 帧**解码成 16 kHz 单声道 PCM，再用
**SenseVoiceSmall（sherpa-onnx，纯 CPU，离线）** 识别，`asr_partial/asr_final` 用真实识别结果。

实现独立于 DSH：识别/解码代码在 [mock_asr.py](mock_asr.py)（自带 PEP 723 依赖声明），模型按
固定 revision + SHA-256 校验；只在 `--asr` 时被 mock 延迟 import，**默认假 ASR 路径仍零额外依赖**。

```bash
# mock 侧（依赖用 uv --with 临时装；首次可加 --asr-download 下载 ~239MB 模型）
uv run --with av --with numpy --with sherpa-onnx tools/ws_mock_server.py \
    --asr sensevoice --asr-download --tts-mp3 /tmp/tts.mp3
# 只准备模型 / 离线验证识别效果（不连设备、不需要 mock）
uv run tools/mock_asr.py --download
uv run tools/mock_asr.py --test tools/今天天气如何.wav tools/今天吃了什么.wav
#   -> '今天天气如何？' / '今天吃的什么？'（音频/推理耗时随行打印）

# 真机回归（脚本自己拉 mock；--asr-expect 可选，断言识别文本包含子串）
uv run tools/voice_cloud_test.py --asr --asr-expect 天气
```

mock 日志会打印识别结果与耗时：

```
[ASR] 加载 SenseVoice（/Users/xxx/.dsh/speech-to-text/sensevoice/models/sensevoice-onnx，threads=2，language=auto）...
[ASR] 就绪（加载 0.35s）
C1  --> asr_final sid=7 text='今天天气如何？'
C1  [ASR] 识别结果: '今天天气如何？'（81 帧 / 音频 1.62s / 推理 0.05s / 总耗时 0.22s）
```

模型查找顺序：`--asr-model-dir` → 环境变量 `MOCK_ASR_MODEL_DIR` → `~/.cache/esp-ai-mock-asr/sensevoice`
→ 复用 `~/.dsh/speech-to-text/sensevoice/models/sensevoice-onnx`（纯数据复用，不 import DSH 代码）。

**真机验收（2026-11-01）**：`uv run tools/voice_cloud_test.py --asr --asr-expect 天气` **55/55 PASS**（连续两次）：mock 识别 `今天天气如何？`，设备 `VC: ASR final` 文本与之一致、非固定文案；上行 201 帧、首帧 23.3ms、内部堆最低 33,875 B。

**限制（测试用途，不追求产品级）**：离线整段识别，没有中间结果（`asr_partial` 取文本前半段）；
识别失败/设备无声时只 WARN 并回退 `--tts-text`，不打断协议链路；单轮默认最多送 30s 音频
（`--asr-max-seconds`）；SenseVoice 支持 auto/中/英/粤/日/韩（`--asr-language`）。

## mock_tts.py —— 本地 TTS 生成 mock 回复音频（Piper，T3.1 测试用）

**作用**：把文字在 PC 侧合成为 **24kHz 单声道 MP3**，交给 `ws_mock_server.py --tts-mp3` 下发，让设备在联调里播放「与文本对应」的真实语音（TTS 链路/音质/时延验收不再只有合成帧或固定 MP3）。

选型 **Piper（rhasspy/piper-voices，VITS，MIT）**：单音色 20~60MB、CPU 实时、许可干净；本工具预置 `zh_CN-huayan-medium`。Kokoro-82M / MeloTTS 试听体积/依赖更大，本项目暂不接入（2026-11-01 决定，先用 Piper，以后再加）。

依赖用 PEP 723 内联声明（`piper-tts`/`lameenc`/`numpy`），`uv run` 即用；模型缓存在 `~/.cache/esp-ai-mock-tts/piper/`（家目录不可写时自动退到 `~/.cache/uv/...` 或临时目录；可用 `--model-dir`/`MOCK_TTS_CACHE` 覆盖）。

```bash
uv run tools/mock_tts.py --download                                   # 首次准备模型（约 60MB，失败自动换 hf-mirror）
uv run tools/mock_tts.py --text "今天天气不错，适合出门散步。" --out /tmp/reply.mp3
printf '好的，我在呢。' | uv run tools/mock_tts.py --speed 1.1 --out /tmp/reply.mp3

# 交给 mock 让设备播
uv run tools/ws_mock_server.py --tts-mp3 /tmp/reply.mp3 --tts-pace-pct 80
```

**参数**：`--text`/`--text-file`/stdin、`--out`（默认 `/tmp/mock_tts.mp3`）、`--voice`（默认 `zh_CN-huayan-medium`）、`--speed`（Piper length_scale，>1 更慢，<1 更快）、`--model/--config`（任意 Piper 音色）、`--download`。

**实测（2026-11-01，Mac + 真机）**：`今天天气不错，适合出门散步。` → 2.87s / 24kHz 单声道 / 23,424B / 生成 1.89s；mock `--tts-mp3` 下发 **122 帧**，设备 `tts_start sr=24000` → 播完 → `round done` 正常（Mac 外放与设备扬声器均试听）。首次调用含模型加载约 1.5~2s，之后约 0.9~1.9s/句。

**限制**：预置中文音色只有 `huayan-medium`（换音色用 `--model/--config` 指定 Piper voices 里其它音色）；线性重采样到 24k（测试够用）；情感/口语感一般（比 `macOS say` 清晰，仍偏播报）；长文本单次合成、不流式。

## 回声测试：ASR 识别文本 → Piper 复读（不要固定文案）

`ws_mock_server.py --tts-piper` 把收到的设备语音先 ASR 识别，再把**识别出的文本**用本地 Piper 合成 24kHz MP3 下发；设备会把"它听到的话"复读出来 —— 既验证 ASR 正确性，又验证 TTS/下行播放链路（识别错时复读的也是错字，一听就知道）。

```bash
# 依赖一次性装进 uv 环境（ASR + TTS 两套）；首次音色缺失加 --tts-piper-download
uv run --with av --with numpy --with sherpa-onnx --with piper-tts --with lameenc \
  tools/ws_mock_server.py --asr sensevoice --tts-piper
```

- `--tts-piper` 与 `--tts-mp3` **互斥**；必须同时开 `--asr`（没有识别文本可复读会直接报错）。
- 设备侧日志：`VC: ASR final sid=1: 今天天气如何？` → 约 1~2s 后 `TTS start sid=1 codec=mp3 sr=24000` → 复读；`round done` 正常。
- mock 侧日志：`[ASR] 识别结果: '…'` → `[TTS] 复读合成：'…' → N 帧` → `tts 下发完成 frames=N mode=mp3`。
- 可调：`--tts-piper-voice`（默认 `zh_CN-huayan-medium`）、`--tts-piper-speed`（>1 更慢）、`--tts-piper-model-dir`、`--tts-pace-pct`（默认 100=实时）。
- **实测（2026-11-01，真机）**：设备说「今天天气如何」→ ASR `今天天气如何？` → Piper 复读（77 帧 / 14,784B / 1.80s 音频）；`audio_end → tts_start` ≈ **1.9s**（ASR 0.31s + Piper 合成 1.38s），设备播完 `round done`；假客户端离线复测同链路端到端 3.38s（63 帧）。
- **限制**：识别错则复读错（这是刻意的）；合成失败只 WARN 并回退合成帧；Piper 单次合成约 1~2s（首次含模型加载），太长的句子会增加等待（仍 < 设备 10s 响应超时）。

## 一轮语音的下发时序（`--scenario normal`）

```
设备 → mock:  audio_start{sid}  →  [上行 0x01 二进制帧...]  →  audio_end{sid}
mock → 设备:  （--think-ms 延时，模拟云端思考）
              asr_partial{sid,text} → asr_final{sid,text} → llm_delta{sid,text} × N
              → tts_start{sid,codec:"mp3",sr:24000} → 0x02 二进制帧
                （缺省合成帧 × --max-tts-frames；--tts-mp3 时 = MP3 帧数，按帧时长实时节奏）
              → tts_end{sid}
```

`--tts-late-bin` 会在 `tts_start` 前、`tts_end` 后各多注入 1 帧 0x02（窗口外，端侧应丢弃）；
`--tts-stall-ms/--tts-stall-at` 在指定帧后停发 n ms（制造端侧欠载），两者缺省关闭、不影响既有协议测试口径。

- `sid` 由**端侧**在 `audio_start` 里给出（ARCH §7 规则 1），mock 原样回填到所有下行事件。
- 二进制帧头 8 字节：`0x56 0x4F | ver=1 | type | seq(2B BE) | len(2B BE)`；下行 `type=0x02`，`seq` 从 0 递增。**帧头不含 `sid`**，端侧按"当前会话"关联（契约缺口见 PROTOCOL.md）。
- TTS payload 缺省是**合成数据**（前 4 字节形似 MP3 帧同步头，其余填 0），只用于帧级联调、**不是可解码 MP3**（协议/sid/帧计数测试用）；给 `--tts-mp3 <file>` 时改为**真实 MP3**，纯标准库按 MPEG Layer III 帧边界切分、一个 0x02 帧 = 一个 MP3 帧，缺省按帧时长实时发送（24ms/帧）。
- `--tts-mp3` 只支持 MPEG Layer III（TTS 契约 24k 单声道；lameenc/PyAV 产物都是 Layer III）；文件头 ID3v2 自动跳过，尾部 ID3v1/APE 标签忽略；解析失败直接退出码 2，不会静默发空流。

## CLI 参数

| 参数 | 默认 | 说明 |
|---|---|---|
| `--host` / `--port` | `0.0.0.0` / `8765` | 监听地址与端口；`--port 0` 由系统分配（自测用） |
| `--device-id` / `--token` | 空（不校验） | 非空则校验 query 参数，不匹配即拒绝连接（关闭码 4001/4002） |
| `--scenario` | `normal` | `normal` / `error`（回 `error` 事件）/ `music`（回 `music_track`） |
| `--think-ms` | 300 | `audio_end` → 首个下行事件的延时，用于测端侧 THINKING 超时 |
| `--chunk-ms` / `--chunk-bytes` | 20 / 192 | 合成帧间隔与 payload 长度（24k/32kbps 20ms ≈ 192B；`--tts-mp3` 时节奏按 MP3 帧时长） |
| `--max-tts-frames` | 合成帧 100 / MP3 全发 | 一轮 TTS 帧上限（100 帧 ≈ 2s）；0 表示不限 |
| `--tts-mp3 FILE` | 空（合成帧） | **T2.4**：下发真实 MP3（按 MP3 帧边界切分，缺省按帧时长实时节奏） |
| `--tts-stall-ms N` | 0 | **T2.4**：在 `--tts-stall-at` 帧后停发 N ms，制造端侧欠载 |
| `--tts-stall-at N` | 30 | stall 帧序号（0 基） |
| `--tts-late-bin` | 关 | **T2.4**：每轮 `tts_start` 前 / `tts_end` 后各发 1 帧 0x02（验证窗口外丢弃） |
| `--tts-pace-pct P` | 100 | 真实 MP3 发送节奏（帧时长百分比）：100=实时；测试脚本会传 80 避免 asyncio 超调造成假欠载 |
| `--tts-text` | 内置中文 | 模拟识别文本，同时用于 `asr_*`/`llm_delta` |
| `--no-tts` | 关 | 只发 JSON（`tts_start`/`tts_end` 仍发），不发二进制帧；P1 握手/心跳联调够用 |
| `--drop-pong` | 关 | 收到 `ping` 不回，测端侧 10s 心跳超时判定 |
| `--close-after S` | 0（关） | 到点主动断开（code 1000），测端侧断线重连 |
| `--save-upload DIR` | 空 | 把上行 payload 按 `c<连接>-sid<sid>-seq<seq>.bin` 落盘（P2 核对"真的在发音频"） |
| `--inject-late` | 关 | 每轮先注入一个 `sid+1` 的 `asr_final`，验证端侧迟到帧过滤（T2.5） |
| `--asr {none,sensevoice}` | `none` | **T3.1**：用本地 SenseVoice 识别上行音频（真 ASR）；需 `uv --with av numpy sherpa-onnx` |
| `--asr-model-dir DIR` | 空（自动找缓存） | SenseVoice 模型目录（`model.int8.onnx` + `tokens.txt`） |
| `--asr-download` | 关 | 模型缺失时自动下载（约 239MB，带 SHA-256 校验） |
| `--asr-threads N` | 2 | ONNX 推理线程数 |
| `--asr-language L` | `auto` | `auto/zh/en/yue/ja/ko` |
| `--asr-max-seconds S` | 30 | 单轮最多送 ASR 的音频秒数（超出部分不参与识别） |
| `--tts-piper` | 关 | **回声测试**：ASR 识别文本 → 本地 Piper 合成后下发（与 `--tts-mp3` 互斥，需 `--asr`） |
| `--tts-piper-voice` / `--tts-piper-speed` | `zh_CN-huayan-medium` / 1.0 | Piper 音色与语速（length_scale，>1 更慢） |
| `--tts-piper-model-dir` / `--tts-piper-download` | 空 / 关 | Piper 音色目录 / 缺失时下载（约 60MB） |
| `--verbose` | 关 | 打印上行 payload 前 16 字节（库自身的握手 DEBUG 已压到 WARNING） |
| `--self-test` | — | 进程内自测，不需要硬件 |

## 约束与边界

- **只做开发联调工具**，不进固件、不占设备资源；零固件改动。
- 传输：本地 mock 是 **`ws://`**（不加密）；线上契约是 **`wss://`**（ARCH §6.1）。TLS 与证书验证策略（自签 CA / 开发期跳过校验）在 T1.4 决定，见 [PRODUCT_IDEAS.md](../docs/PRODUCT_IDEAS.md) §8 X2。
- 服务端**不主动**发 WS 协议层 ping（`ping_interval=None`），也**不主动**发应用层 `{"t":"ping"}`：§6.1 只定义了端侧 20s 上行心跳，避免出现两份心跳互相干扰。
- 坏帧（magic/ver/len/type 不符、非 JSON 文本帧）一律 WARN + 计数 + 丢弃，但**不断开连接**——联调时希望看到问题累积，而不是一次错误就把会话踢掉。
- **每条 accept 连接默认开 `TCP_NODELAY`（关 Nagle）**：mock 每 ~20ms 下发一个 0x02 帧，若被 Nagle 攒批（叠加端侧 delayed ACK 的 ~200ms），设备会每 200–250ms 才集中收一批，ring 耗尽被误判成端侧 `underrun>0` 的**假欠载**（T2.4-C2 真机实测：`tts_stream_test.py` 稳态 underrun=1）。实现方式：`serve(..., process_request=...)` 在握手前对 `connection.transport` 调 `setsockopt(IPPROTO_TCP, TCP_NODELAY, 1)`，handler 里再兜底设一次；`--self-test` 有一条用 `getsockopt` 校验。**真实云端实现同样必须关 Nagle**；这是服务端/测试环境的修复，**不放宽契约 `underrun=0` 阈值**。
- `scenario=music` 只回 `music_track`，`url` 不可播放（T1.5 不提供 HTTP 音频服务，P4 前补）；`music_status` 只记日志。
- `--tts-mp3` 的帧切分/节奏是**工具行为**，不改协议：帧头 8B / seq / tts_start·tts_end 顺序与合成帧模式完全一致；`--tts-late-bin`/`--tts-stall-*` 都只影响 mock 发送时序，端侧契约不变。
- 契约已由 T2.5 全部定稿并回填 [PROTOCOL.md](../docs/PROTOCOL.md)：`hello_ack` / 鉴权关闭码 / `pong` 口径 / 双传输 / 二进制帧 `sid` 关联（P-2）/ `error` 码表（P-4）；只剩音乐事件 `sid` 归属留 T4.2（P-3）。mock 的 `error` 用 `code=1`（通用）。

## 后续（未做）

- `voice_cloud_test.py` 已承接 P2 端到端断言（唤醒 → 上行 Opus → ASR/LLM/TTS → 收尾/中止 + sid 过滤）；`mp3_test.py` 承接下行解码断言（T2.3 已交付）；**`tts_stream_test.py` 承接 T2.4 流式播放断言**（真实 MP3 → 首帧延迟/欠载/解码对拍/abort/排空/堆稳定）；P3 半双工：T3.1 会话状态机由 `voice_cloud_test.py` 覆盖，**T3.2 R4 按键打断 / R5 无丢弃 / R3 ducking 由 `tts_stream_test.py` 的 `--interrupt`/`--r5`/`--r3-duck` 覆盖**（真机验收等 Lead 固件就绪后跑）；T5.2 用回放 + `record_wav.py` 产出唤醒率/误唤醒基线。
- 下行流式播放断言（T2.4）→ **已交付 `tts_stream_test.py`**；mock 真实 MP3 下发 → **已交付 `--tts-mp3`**。
- HTTP 音频服务（`music_track.url` 可播放、支持 Range 请求），P4（T4.1/T4.2）前补。
- `wss` + 自签证书（T1.4 决定证书策略后）。
- 上行 Opus 帧解码校验 → **已交付 `opus_enc_test.py`**（PyAV 独立解码 + 频谱/码率/泄漏断言，见上）；真机联调在 T2.2 固件就绪后由 Lead 通知。
- 本地 TTS 扩展（Kokoro-82M / MeloTTS / CosyVoice 等更高自然度方案）→ **2026-11-01 暂缓**，先用 `mock_tts.py`（Piper）；以后需要再加。
