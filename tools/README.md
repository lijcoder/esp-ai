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
| `tts_stream_test.py` | 下行 TTS **流式播放**真机自测（T2.4）：真实 MP3 经 WS 0x02 下发 → 首帧延迟/欠载/解码对拍/abort/排空/10 轮堆稳定 | `uv run tools/tts_stream_test.py`，见下文 |
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
uv run tools/voice_cloud_test.py                        # 默认 --volume 50，全量 43 项
uv run tools/voice_cloud_test.py --inject-late          # 让 mock 注入 sid+1 的迟到事件，验证端侧过滤
uv run tools/voice_cloud_test.py --volume 31 --log /tmp/vc.log
uv run tools/voice_cloud_test.py --no-mock              # 已有 mock 在跑（跳过 mock 侧断言）
uv run tools/voice_cloud_test.py --dry-run              # 只检查素材/音量/串口
```

**断言**：设备启动 + `hello_ack`；唤醒链路（`wake detected`→`feed paused`→`PLAYER: play`→`prompt done`）；`round start` / 上行首帧 <200ms / `audio_end` 原因 `vad-silence` / `tts_start`(mp3/24000) / `tts_end` / `round done`；event_bus 收到 `ASR_FINAL`/`TTS_START`/`TTS_END`；上行帧数 ≥60、平均帧长 20~60B、首帧耗时、下行 100 帧与 `tts_end` 计数一致、`sid` 丢弃/解析失败/云端 error 为 0；mock 侧 sid 一致、逐帧收到、`seq` 无跳变、无坏帧；`vc stop` 收尾原因 `stop-request`、`vc cancel` 立即放弃；内部堆最低水位 ≥30KB、`vc` 任务栈剩余 ≥1KB、无 ERROR。**T2.5 真机实测 43/43 PASS**（含 `--inject-late`）。

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

## tts_stream_test.py —— 下行 TTS 流式播放真机自测（T2.4）

**作用**：验证「真实 MP3 经 WS 0x02 帧下发 → 端侧流式解码播放」这条链路，断言音频侧硬指标
（不是"能不能收到帧"，而是"首帧多久出声、会不会欠载、解出来对不对、打断停不停、收尾排不排空"）。

一条命令完成：PC 侧生成素材 → 起 mock（`--tts-mp3`）→ 复位设备 → `vc start` → 轮询
`vc status` 到 `UPLINK` → `vc stop` 确定性触发一轮（**不依赖唤醒词/人声/VAD**）→ mock 下发
真实 MP3 → 逐项断言 → 汇总 PASS/FAIL（退出码 0/1，最后一行 `TTS_STREAM_TEST: PASS|FAIL`）。

```bash
uv run tools/tts_stream_test.py                    # 全量（默认复位设备、独占串口，约 1.5~2 分钟）
uv run tools/tts_stream_test.py --dry-run          # 只生成素材 + PyAV 参考，不连设备
uv run tools/tts_stream_test.py --no-reset         # 设备已在跑
uv run tools/tts_stream_test.py --skip-heap        # 跳过 10 轮堆稳定（快速冒烟）
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

### 六个场景与断言

| # | 场景 | 关键断言 |
|---|---|---|
| 1 | 正常一轮（真实 MP3） | `first_sound_us<150000`（日志 `first sound <150ms` 双口径）；稳态 `underrun=0`、`dropped=0`、`bad=0`；`fed/decoded` 与 PyAV 参考 ±2 帧、`samples` ±2 帧；`tts_end` 排空（`ring_ms=0`、`decoded==fed`、日志先 `tts_end` 后 `stream end`、`dur≈素材时长`）；设备收到字节==素材字节；mock 发送帧数/字节==素材；`vc` 下行帧数==素材帧数、窗口内丢弃 0；R2 收音恢复 |
| 2 | abort 停声 | 播放中 `vc cancel` → `PLAYER: stream abort` 延迟 ≤150ms（目标 100ms，串口 10ms 轮询）；`active=0` 后 decoded 不再增长；`round aborted` + 回 IDLE |
| 3 | 连续 10 轮堆/PSRAM | 每轮 `underrun=0`、decoded 完整；10 轮内部堆波动 ≤2KB 且不单调下降；PSRAM 首末差 ≤8KB、波动 ≤64KB、不单调下降（预热 1 轮不计入） |
| 4 | stall 欠载 | mock 在 `--tts-stall-at` 帧后停发 `--tts-stall-ms`（默认 600ms）：`underrun>=1`、日志 `stream underrun`；停发不丢帧、恢复后 decoded 完整、`rounds_done=1`；mock 日志确认 stall；**`dur` 用物理口径 `[素材时长, 素材时长+stall_ms+300ms]`**（stall 按设计补静音，会顶破 ±10%，与实现无关） |
| 5 | 窗口外帧丢弃 | mock `--tts-late-bin`：`tts_start` 前 / `tts_end` 后各 1 帧 0x02；`vc`「音频丢弃」≥2 帧且字节>0；窗口内计数/播放仍==素材帧数（不误播）；无 ERROR |
| 6 | 合成帧降级 | mock 下发合成帧（不可解码）：本轮仍 `round done`、`active=0`、decoded≤2、收音恢复、**无 ERROR/panic**（bad data 只允许 WARN） |

另有全程健康检查：只按 `E (…)`/panic 判失败（`round aborted` 的 W 是预期）；`--no-mock`
时跳过 mock 侧断言与依赖重启 mock 的场景 4~6，并打印 WARN。

**`dur` 断言口径（T2.4-C3，避免误判）**：场景 1（normal）与 5（late-bin）无 stall，`dur` 应
≈ 素材时长，用 **±10%（至少 ±100ms）**；场景 4（stall）按设计在欠载期补静音，物理范围是
**`[素材时长, 素材时长 + stall_ms + 300ms 余量]`**，不能用 ±10%——实测 preroll=2 时 dur≈5310ms、
preroll=3 时 dur=5342ms，均落在物理范围内（通用 ±10% 上限 5332ms 会随机 FAIL，与实现无关）。
判定逻辑见 `tts_stream_test.check_duration()`。

### 与 voice_cloud_test.py 的分工（P2 验收两条都要跑）

| | `voice_cloud_test.py`（T2.5） | `tts_stream_test.py`（T2.4） |
|---|---|---|
| 触发方式 | 回放唤醒词 + 交互语音（真实用户路径） | `vc start`→`vc stop`（确定性，不依赖唤醒/VAD） |
| mock 下发 | **合成帧**（只验协议/帧计数，不可解码） | **真实 MP3**（按帧切分、实时节奏） |
| 关注点 | 握手/上行 Opus/ASR-LLM-TTS 事件/sid 过滤/收尾/内存水位 | 首帧延迟/预缓冲/欠载/解码对拍/abort 停声/排空/10 轮堆稳定 |
| 额外参数 | `--inject-late`（sid 迟到事件） | `--tts-late-bin`（窗口外 0x02 帧）、`--stall-*` |

### 参数 / 依赖 / 限制

- 参数：`--port`、`--mock-port`（默认 8765）、`--no-mock`、`--no-reset`、`--dry-run`、`--log`、
  `--round-timeout`（默认 30s）、`--think-ms`（mock 首事件延时，默认 100）、`--pace-pct`（默认 80，
  见下）、`--material-repeats`（默认 3）、`--kbps`（默认 32）、`--heap-rounds`（默认 10）、
  `--stall-ms`/`--stall-at`（默认 600/30）、`--skip-heap|--skip-abort|--skip-stall|--skip-late|--skip-synthetic|--skip-scenarios`。
- **`--pace-pct` 为什么默认 80 而不是 100**：mock 缺省实时节奏（100% = 契约默认）；但 macOS
  `asyncio.sleep` 每帧超调约 1.7ms（≈7%），100% 会让发送平均慢于播放、产生**假欠载**。本脚本用
  80%（≈20.9ms/帧 vs 播放 24ms/帧）让发送略快，给端侧留缓冲；真实欠载由场景 4 的 stall 强制制造。
- 依赖：PEP 723 内联 `pyserial` / `lameenc` / `av` / `numpy`，`uv run` 自动安装。脚本**独占串口**
  （先关 `idf.py monitor`）；**不改系统音量**（本测试不依赖 Mac 扬声器回放，与 `voice_test.py`
  的音量口径无关，也不会外放）。
- 场景 4~6 需要脚本自己拉起 mock 并在场景间**重启 mock**（切换 `--tts-mp3`/stall/late 参数），
  重启后脚本用 `ws reconnect` 让设备快速重连；`--no-mock` 或端口被占用（复用外部 mock）时这三项跳过。
- mock 已默认对所有连接开 `TCP_NODELAY`（T2.4-C2）。若真机稳态仍出现 `underrun>0`，先确认跑的是带
  NODELAY 修复的 mock（`ws_mock_server.py --self-test` 含该断言、启动横幅有 `TCP_NODELAY=1` 标注），
  再排查；**不要通过放宽 underrun 阈值或调大 pace 掩盖**（真实云端同样应关 Nagle）。
- 设备侧探针：`player stat` 的 stream 行由 T2.4-A 常驻提供；`player reset` 会清空流式统计（契约 §1.3），
  每个场景前脚本都会 `vc reset` + `player reset` 让计数从 0 开始。

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

### 与 voice_cloud_test.py / tts_stream_test.py 的分工（P2 验收三条都要跑）

| | `voice_cloud_test.py`（T2.5） | `tts_stream_test.py`（T2.4） | `e2e_voice_test.py`（T2.6） |
|---|---|---|---|
| 触发方式 | 回放唤醒词 + 交互语音（真实路径，1 轮） | `vc start→stop` 确定性触发（不依赖唤醒） | **回放唤醒词 + 交互语音，10 轮** |
| mock 下发 | 合成帧（只验协议/帧计数） | 真实 MP3 | **真实 MP3** |
| 关注点 | 上行 Opus、ASR/TTS 事件、sid 过滤、stop/cancel 收尾 | 首帧延迟/欠载/解码对拍/abort/排空/10 轮堆稳定 | **端到端链路 + R1/R2 + 统计对账 + 10 轮稳定性 + 播放期拾音** |
| 额外参数 | `--inject-late` | `--tts-late-bin`、`--stall-*` | `--max-retries`、`--allow-max-duration`、`--playback-warmup/window` |
| 不覆盖 | 真实 MP3 播放、10 轮 | 唤醒/VAD/真实用户路径 | abort 停声延迟、stall 欠载、迟到帧、合成帧降级（这些仍由 tts_stream_test 覆盖） |

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

- `voice_cloud_test.py` 已承接 P2 端到端断言（唤醒 → 上行 Opus → ASR/LLM/TTS → 收尾/中止 + sid 过滤）；`mp3_test.py` 承接下行解码断言（T2.3 已交付）；**`tts_stream_test.py` 承接 T2.4 流式播放断言**（真实 MP3 → 首帧延迟/欠载/解码对拍/abort/排空/堆稳定）；P3 起增加会话状态机、半双工、打断断言；T5.2 用回放 + `record_wav.py` 产出唤醒率/误唤醒基线。
- 下行流式播放断言（T2.4）→ **已交付 `tts_stream_test.py`**；mock 真实 MP3 下发 → **已交付 `--tts-mp3`**。
- HTTP 音频服务（`music_track.url` 可播放、支持 Range 请求），P4（T4.1/T4.2）前补。
- `wss` + 自签证书（T1.4 决定证书策略后）。
- 上行 Opus 帧解码校验 → **已交付 `opus_enc_test.py`**（PyAV 独立解码 + 频谱/码率/泄漏断言，见上）；真机联调在 T2.2 固件就绪后由 Lead 通知。
