# tools/ —— 开发辅助工具

开发期脱机联调/真机自测用的工具与素材，**不进固件、不占设备资源**。

| 文件 | 用途 | 用法 |
|---|---|---|
| `ws_mock_server.py` | 本地「云端」WS 模拟服务（T1.5），设备联调不用等后端 | 见下文 |
| `voice_test.py` | 语音链路真机自动自测：唤醒 + 交互语音回放 + 串口断言 | `uv run tools/voice_test.py --volume 50`，见下文 |
| `lexin.wav` | 唤醒词"嗨，乐鑫"测试音频，**替代人工喊话** | `afplay tools/lexin.wav`，见下文 |
| `今天天气如何.wav` | 交互语音素材（模拟用户提问），**替代人工说话** | `afplay tools/今天天气如何.wav`，见下文 |
| `今天吃了什么.wav` | 交互语音素材（模拟用户提问） | `afplay tools/今天吃了什么.wav`，见下文 |
| `opus_enc_test.py` | 上行 Opus 编码真机自测（T2.2）：串口抓帧 + PyAV 独立解码断言 + 内存/泄漏 | `uv run tools/opus_enc_test.py --self-test`，见下文 |
| `voice_cloud_test.py` | 语音云端到端真机自测（T2.5）：起 mock → 唤醒 → 上行 Opus → 下行事件 → sid/统计断言 | `uv run tools/voice_cloud_test.py`，见下文 |
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
| `--inject-late` | 关 | 每轮先注入一个 `sid+1` 的 `asr_final`，验证端侧迟到帧过滤（T2.5） |
| `--verbose` | 关 | 打印上行 payload 前 16 字节（库自身的握手 DEBUG 已压到 WARNING） |
| `--self-test` | — | 进程内自测，不需要硬件 |

## 约束与边界

- **只做开发联调工具**，不进固件、不占设备资源；零固件改动。
- 传输：本地 mock 是 **`ws://`**（不加密）；线上契约是 **`wss://`**（ARCH §6.1）。TLS 与证书验证策略（自签 CA / 开发期跳过校验）在 T1.4 决定，见 [PRODUCT_IDEAS.md](../docs/PRODUCT_IDEAS.md) §8 X2。
- 服务端**不主动**发 WS 协议层 ping（`ping_interval=None`），也**不主动**发应用层 `{"t":"ping"}`：§6.1 只定义了端侧 20s 上行心跳，避免出现两份心跳互相干扰。
- 坏帧（magic/ver/len/type 不符、非 JSON 文本帧）一律 WARN + 计数 + 丢弃，但**不断开连接**——联调时希望看到问题累积，而不是一次错误就把会话踢掉。
- `scenario=music` 只回 `music_track`，`url` 不可播放（T1.5 不提供 HTTP 音频服务，P4 前补）；`music_status` 只记日志。
- 契约已由 T2.5 全部定稿并回填 [PROTOCOL.md](../docs/PROTOCOL.md)：`hello_ack` / 鉴权关闭码 / `pong` 口径 / 双传输 / 二进制帧 `sid` 关联（P-2）/ `error` 码表（P-4）；只剩音乐事件 `sid` 归属留 T4.2（P-3）。mock 的 `error` 用 `code=1`（通用）。

## 后续（未做）

- `voice_cloud_test.py` 已承接 P2 端到端断言（唤醒 → 上行 Opus → ASR/LLM/TTS → 收尾/中止 + sid 过滤）；后续 T2.3/T2.4 加「下行音频可解码/可出声」断言，P3 起增加会话状态机、半双工、打断断言；T5.2 用回放 + `record_wav.py` 产出唤醒率/误唤醒基线。
- 真实 MP3 素材 + 断言可解码（T2.3/T2.4）。
- HTTP 音频服务（`music_track.url` 可播放、支持 Range 请求），P4（T4.1/T4.2）前补。
- `wss` + 自签证书（T1.4 决定证书策略后）。
- 上行 Opus 帧解码校验 → **已交付 `opus_enc_test.py`**（PyAV 独立解码 + 频谱/码率/泄漏断言，见上）；真机联调在 T2.2 固件就绪后由 Lead 通知。
