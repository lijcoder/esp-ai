# 开发规范（CONVENTIONS）

> 本文件是编码、分层、内存、提交规则的**唯一来源**；AGENTS.md 只做索引入口与红线。
> 设计看 [ARCHITECTURE.md](ARCHITECTURE.md)，计划看 [ROADMAP.md](ROADMAP.md)，方向看 [PRODUCT_IDEAS.md](PRODUCT_IDEAS.md)。

## 1. 文档边界与更新义务

| 文档 | 只回答 | 禁止出现 |
|---|---|---|
| `ARCHITECTURE.md` | 是什么 / 怎么设计 | 阶段（一期/二期）、工期、启动时机 |
| `ROADMAP.md` | 何时做 / 做到什么程度 | 设计方案细节、任务进度 |
| `STATUS.md` | 现在做到哪 / 下一步 / 实测基线 | 设计方案、计划细节 |
| `PRODUCT_IDEAS.md` | 要不要做 / 值不值得 | 已确定的实现细节 |
| `HARDWARE.md` | 硬件与电气约束 | 软件设计、排期 |
| `AGENTS.md` | 文档索引 + 全局红线 | 进度、方案（保持稳定，仅结构/流程变动时修改） |
| `PENDING.md` | 待验证 / 待决定的问题队列（每条带提出时间、验收方法、关闭条件） | 进度、设计方案、排期（关闭后结论写回 STATUS/ARCH，本文件只留归档行） |

更新义务：架构变更 → 改 ARCHITECTURE；计划变更 → 改 ROADMAP；**任务进度与基线 → 改 STATUS.md（不动 AGENTS.md）**；新方向 → 先写 PRODUCT_IDEAS；硬件变更 → 改 HARDWARE。 未做真机验证的项 / 被暂缓的决定 → 登记 `PENDING.md`（带时间与验收方法），关闭时结论回写 STATUS 并在 PENDING §4 归档。协议字段变更需同时改 ARCHITECTURE §6 与 `PROTOCOL.md`。

## 2. 分层与依赖

1. 只允许上层依赖下层：L0 `board`/`audio_io` → L1 `config_store`/`net_mgr`/`transport_ws`/`event_bus` → L2 `audio_fe`/`audio_player`/`codec_enc` → L3 `voice_cloud`/`music_cloud`/`dev_ctrl`/`diag` → L4 `voice_session`/`music_session`。
2. 同层通信走 `event_bus` 或队列，禁止直接互调。
3. 组件只暴露 `include/<module>.h`，其余符号 `static`。
4. `main/` 只做初始化编排（≤150 行），不放业务逻辑。
5. 可调参数走组件 `Kconfig`，禁止硬编码引脚/地址/采样率/阈值。
6. 新增模块前先在 ARCHITECTURE §4 登记，再建目录。

## 3. 实时与音频

- 音频任务内禁止：`malloc`、网络/HTTP、Flash 读写、阻塞等待、长持有 mutex。
- 采样率固定：麦克风 16kHz、功放 48kHz，运行中不切换。
- **超时参数必核对单位**：IDF I2S 驱动 `i2s_channel_read/write` 的最后一个参数是**毫秒**，禁止传 `pdMS_TO_TICKS()`；只有 FreeRTOS API（`TickType_t` 参数，如 `fetch_with_delay`）才用 tick。单位错会让读写持续超时、功能静默失效（2026-10-30 唤醒失效即此原因，见 STATUS）。
- 半双工规则 R1–R5（ARCHITECTURE §3.3）是硬约束，任何改动必须同步架构文档。
- **半双工门控唯一入口（T3.2）**：暂停/恢复收音只能经 `audio_fe_feed_hold()/audio_fe_feed_release()`（原因位 `PROMPT`/`TTS`/`PROBE`，位图归零才恢复）；禁止直接改 `audio_fe` 内部标志或用 `audio_fe_set_feed_enabled()` 绕开仲裁（该接口仅为探针/旧脚本兼容）。R5 的恢复丢弃窗口用 `audio_fe_feed_discard_ms()` 在 release 之前设置。
- 打断淡出与 duck 统一在 `audio_player` 的输出级（`audio_player_fade_out_and_stop()` / `audio_player_set_duck()`），播放循环不得自己再做一套淡出。
- **混音/音量/限幅唯一入口（T3.3）**：三通道混音、音量（20ms 插值）、-1dBFS 软限幅、通道包络淡入淡出全部在 `audio_player` 的混音级（`mix_render()`），通道 producer 只产出**原始** 48k 单声道样本；禁止在 producer 里再乘音量/做限幅/做出口淡出（会双算）。通道增益语义：TTS 0dB、PROMPT 在 TTS 活跃时 -6dB、MUSIC 在语音活跃或 `set_duck(true)` 时 -12dB，斜坡 100ms 线性。新功能要"多路同时出声"时，必须注册/复用通道 ring，不要另起播放循环。
- **提示音资源唯一入口（T3.5）**：提示音一律从 `assets` 分区以 **pack** 形式提供（`tools/prompts_src/*.wav` → `tools/audio_pack.py`），固件只经 `prompt_assets_get()` 取；**禁止再把 PCM 以 C 数组/字面量编进 app**（T3.6 前的临时方案已删除），也禁止在业务组件里直接读 `assets` 分区。更新提示音走 `audio_pack.py --flash`，不改固件/不进代码评审。
- **异常反馈唯一入口（T3.6）**：异常→用户的反馈统一经 `feedback` 组件（`fb_notify(kind, code, sid)`）：它先投 `APP_EVT_FEEDBACK` 语义事件，再驱动当前语音通道。**禁止其它模块自己调 `audio_player_play_pcm()` 播错误提示音**；以后加屏幕/灯光等展示方式就订阅 `APP_EVT_FEEDBACK`（新组件），不要改 `feedback`/`voice_session`，也不要绕开它另做一套反馈。提示音 R1 门控统一用 `AUDIO_FE_HOLD_PROMPT`。
- 播放必须淡入 10ms / 淡出 100ms；混音后软限幅 -1dBFS。
- 唤醒词/VAD 回调里只投递事件，不做耗时操作。
- 关键路径打点：唤醒、上行首帧、TTS 首帧、打断、起播。

## 4. 内存

- 大块缓冲（播放 ring、解码、HTTP）必须 PSRAM：`heap_caps_malloc(size, MALLOC_CAP_SPIRAM)`。
- I2S DMA 缓冲必须内部 RAM：`MALLOC_CAP_DMA | MALLOC_CAP_INTERNAL`。
- 内部 RAM 预算（**X3-E 定稿，2026-10-31**）：**静态 DIRAM + 运行期堆峰值 ≤ 440KB**，且**最坏场景（wss/TLS 握手期）`sys heap` 最低水位 ≥ 30KB**；超预算先改设计，不靠砍功能硬压。
  - 口径与实测：`idf.py size` 的 DIRAM 已用 + `sys heap` 的「峰值占用」（internal），两者相加对照 440KB；`sys heap` 的「最低水位」对照 30KB。
  - 当前值（**T3.5 后，2026-11-02**）：静态 **199,766 B** + 内部堆峰值 **225,120 B**（`ws://` 真实语音轮口径）= **424,886 B**（距 440KB 上限余约 15.4KB）；`ws://` 最低水位 **31,987 B**（≥30KB，余 1,267 B，最紧口径）；提示音 PCM 在 `assets` 分区、**不占 RAM**（mmap 只占 MMU 页）；bin 1,941,792 B、PSRAM 峰值 1,596,380 B；无新增任务。**wss 握手期未复测**（估算 ~31.6KB），T5.3 复测。以下为历史值：T3.6 时静态 **199,286 B** + 内部堆峰值 **224,924 B**（`ws://` 真实语音轮口径）= **424,210 B**（距 440KB 上限余约 26KB）；`ws://` 最低水位 **32,679 B**（≥30KB，余 2,679 B）；共 4 条提示音 PCM 在 flash rodata（~177KB，不计 RAM）；无新增任务。**wss 握手期未复测**（估算 ~32.2KB），T5.3 复测。以下为历史值：T3.4 时静态 **199,094 B** + 内部堆峰值 **224,812 B**（`ws://` 真实语音轮口径）= **423,906 B**（距 440KB 上限余量 26,628 B）；`ws://` 最低水位 **32,983~33,131 B**（≥30KB）；**wss 握手期（公网 echo，idle）静态 + 峰值 = 199,094 + 224,604 = 423,698 B、最低水位 33,271 B**（T3.3 为 423,138 B / 33,711 B，本次 -440B 水位来自 FOLLOW_UP 状态与 esp_timer，仍 ≥30KB）；PSRAM 峰值见 T3.3（1,596,388 B，T3.4 未新增缓冲）。**T3.4（dev_ctrl 按键/LED/静音）静态 DIRAM 199,370 B（+276 B）、不建任务**（两个 esp_timer + 一次 GPIO 读；LED 用周期精确 bit-bang 而非 RMT——RMT 驱动的 IRAM 占用 ~2.2KB + 运行时 symbol 缓冲会击穿水位，实测未引入）；后续仍需节约内部 RAM。T3.3 时为 425,342 B / ws 31,571 B / PSRAM 1,596,388 B；T3.2 时为 424,214 B / ws 32,859 B / PSRAM 1,556,040 B；T3.1 时为 424,830 B / 31,891~32,971 B / PSRAM 1,556,032 B；T2.4 时为 424,478 B / wss 32,671~32,719 B / PSRAM 1,446,724 B；T2.3 时为 424,506 B / wss 32,471~32,643 B / PSRAM 2,895,336 B；T2.5 时为 424,658 B / wss 32,339 B / PSRAM 902,908 B；T2.2 时为 409,918 B / wss 47,051~52,839 B。
  - 两条必须同时满足；**最坏场景下「最低水位 ≥30KB」是更强约束**（堆总量 ~259,843 B → 堆峰值上限约 229,843 B，折合静态+堆 ≤426.8KB）；440KB 上限用于吸收静态 DIRAM 后续增长。**T2.4 后水位余量只剩 ~2KB：后续任务不得再新增内部 RAM 任务栈/静态/堆占用，需要缓冲时放 PSRAM 或复用现有任务。T3.1 即据此把 `voice_session` 改为事件驱动、不新建任务（ARCH §9.3）。**
  - TLS 记录缓冲与握手堆自 X3-F 起在 PSRAM，不计入内部 RAM；旧口径「峰值 ≤310KB」写于 P0（未含 WiFi 栈且只统计堆），已作废。
- 长时运行场景必须监控堆水位（`diag`），不允许无限增长。

## 5. 代码规范

- 命名：函数/变量 `snake_case`，宏/常量 `UPPER_CASE`，文件与模块同名。
- 对外接口统一返回 `esp_err_t`，错误码定义在组件头文件。
- 日志 TAG 固定：`BOARD/AUDIO_IO/AFE/PLAYER/ENC/NET/WS/VC/VS/MC/MS/DEV/FB/PROMPT/DIAG/CFG/EVT/APP`（`APP` 属 `main/` 启动编排，`EVT` 属 `event_bus` 基础服务，`FB` 属 `feedback` 异常反馈 T3.6，`PROMPT` 属 `prompt_store` 提示音资源 T3.5）；status 迁移 INFO、异常 WARN、失败 ERROR。
- 注释写"为什么"而非"做了什么"，中文注释。
- 组件目录模板：

```
components/<module>/
├── CMakeLists.txt
├── Kconfig
├── idf_component.yml       # 可选：本组件的托管依赖（如 esp-sr）
├── README.md               # 必需：组件作用、接口、依赖、配置、约束、后续
├── include/<module>.h      # 唯一对外头文件
└── <module>.c              # 内部实现
```

每个组件目录必须有 `README.md`，内容至少包含：作用（一句话）、对外接口表、依赖、Kconfig 配置、关键约束/后续计划。新增组件时同步补 README。

托管依赖（`idf_component.yml`）声明在**实际使用它的组件**目录下，不要一律堆到 `main/`：
`main` 只是 `idf.py create-manifest` 的默认位置；组件管理器的依赖解析按组件进行。

## 6. 协议

- 以 ARCHITECTURE §6 为准；二进制帧头 8 字节（`0x56 0x4F` + ver/type/seq/len），布局不得私自变更。
- 所有下行事件带 `sid`；端侧必须丢弃 `sid` 不匹配的迟到帧。
- 收发错误必须有日志与错误码，不允许静默丢弃。

## 7. Git

- 提交信息：`feat(<组件>): 描述`、`fix(<组件>): 描述`、`refactor:`、`docs:`、`chore:`。
- 一个提交只做一件事；大改前先提交，保证可回滚。
- 不提交生成物：`build/`、`sdkconfig`、`managed_components/`、`*.log`、`*.bin`。

## 8. 调试

- 串口 115200（UART0，GPIO43/44），`idf.py monitor` 直接看日志。
- 新增调试能力优先做成 console 命令（ARCHITECTURE §10.2），不要留临时打印。
- 内存问题先 `sys heap`/`sys tasks`；音频问题先 `mic stat`，再 `tools/record_wav.py` 抓 WAV（**该脚本 T5.2 才交付，当前不存在**）。
- **唤醒/交互语音测试先回放、别喊话**：测"嗨，乐鑫"用 `afplay tools/lexin.wav`（macOS 扬声器 → 设备麦克风）触发 WakeNet，不需要人工喊；测交互语音用 `afplay tools/今天天气如何.wav` / `afplay tools/今天吃了什么.wav`，必须在 `APP: prompt done, feed resumed` **之后**放（R1 播放提示音期间 AFE 吃静音）。设备侧 `idf.py -p <PORT> monitor` 看 `APP: wake detected` → `AFE: feed paused` → `PLAYER: play 12800 samples` → `APP: prompt done, feed resumed`；交互语音在 P1 无 APP 日志，用 `mic stat`（`mic reset` → 回放 → `mic stat`）看 `VAD 人声`/电平、确认 `累计唤醒 0`（不误唤醒）。**音量按用户定稿：系统音量正常 31%、上限 50%（不得突破）+ 默认增益**（70% 已否决）；`-v` 放大须满足 系统音量×增益 ≤50%（31% + `-v 1.5` ≈ 46.5% 可）。**自动回归用 `uv run tools/voice_test.py --volume 50 --wakes 3`**（脚本临时把音量调到上限、结束恢复；断言唤醒链路/误唤醒/I2S 失败/ERROR，退出码 0/1）。回放前确认系统未静音、设备麦克风未被挡；无唤醒时依次查音量/距离/输出路由 → 设备是否在播放（播放中唤醒被忽略）→ `mic stat` 电平。素材属性、实测数据与脚本参数见 `tools/README.md`。
- **上行音频调试（T2.1 起）**：`afe uplink on|off|stat [ms]` 看写入/读取/丢弃/首帧延迟/500ms 分段 RMS；自动回归 `uv run tools/afe_uplink_test.py --volume 50`（15 项断言，独占串口）。AFE fetch 每帧 512 样本（32ms），与 Opus 20ms 帧不整除，编码前必须按 320 样本重组。
- **上行 Opus 编码调试（T2.2 起）**：`enc test [ms] [tone|uplink]`（tone=1kHz 正弦测编码数值、uplink=真实链路，默认自动选；uplink 模式会先排空 ring 积压）、`enc stat`、`enc dump on|off`（最多 50 帧 hex，给 PC 侧 PyAV 独立解码）、`enc reset`；自动回归 `uv run tools/opus_enc_test.py`（45 项断言，A=tone 数值基准 / B=语音链路 / C=内存泄漏，独占串口）。固件为 libopus **定点** + VBR（浮点实现实测崩溃、CBR 实测 2.4× CPU，均写进 STATUS 与组件 README）；编码只能串行调用（thread-safe 伪栈按线程各 120KB PSRAM），且**不得在音频任务内调用**。
- **MP3 播放调试（T2.3 起）**：`player load begin` → 分块 `player load <base64>`（≤240 字符/行，串口无流控）→ `player load end` → `player mp3`（播放任务整段解码后播放）→ `player stat`（帧数/样本/采样率/声道/时长/坏帧/单帧耗时）→ `player pcm <off> <n>`（取回设备解码 PCM 供 PC 侧与 FFmpeg 逐样本对拍）。自动回归 `uv run tools/mp3_test.py`（48 项断言，素材现生成 + 真实电台流，独占串口）。探针、512KB PSRAM 上传缓冲与 console 1024B 行缓冲都由 `CONFIG_DIAG_ENABLE_PLAYER_TEST` 控制，**量产必须关闭**。
- **下行 TTS 流式播放调试（T2.4 起）**：`tts_end/0x02` 链路自动回归 `uv run tools/tts_stream_test.py`（193 项断言：`vc start→stop` 确定性触发一轮、mock 下发真实 24k MP3，断言 `first_sound_us<150ms`、稳态 `underrun=0`、PyAV 解码对拍、stall 注入欠载恢复、`vc cancel` 停声≤150ms、窗口外帧丢弃、合成帧降级无 ERROR、10 轮堆/PSRAM 稳定；独占串口、约 2 分钟）；heap-only 20 轮用 `--skip-abort --skip-stall --skip-late --skip-synthetic --heap-rounds 20`。设备侧观测：`player stat` 的 `stream:` 行（fed/decoded/samples/rate/underrun/dropped/bad/first_sound_us/dur_ms）、`player stream [chunk] [gap_ms]`（不联网自测流式路径）、`vc stat` 的「音频丢弃 N 帧 字节 M」。**跑 tts 测试时宿主不要并发重负载**：mock 被抢占 >150ms 会造成一次真欠载（环境噪声，重跑即可，不要改阈值）。
- 云端/WS 问题先跑 mock，别直接上设备：`uv run tools/ws_mock_server.py --self-test`（**19 项**，不需要硬件）确认协议链路，再起 `uv run tools/ws_mock_server.py` 给设备当服务端；字段口径对照 `docs/PROTOCOL.md`（含待确认项）。**mock 已默认对每条连接开 `TCP_NODELAY`**：Nagle + 端侧 delayed ACK 会把均匀 TTS 帧攒批 ~200ms，造成端侧假欠载；真实云端同样必须关 Nagle。T3.6 起 mock 还支持 `--error-code N`、`--error-stage {after-audio-end,after-asr,after-tts-start}`、`--tts-bad-codec` 注入异常路径。
- 崩溃先看串口 panic 输出，再解析 coredump 分区（`coredump` @0x9a0000/64K，已开 `CONFIG_ESP_COREDUMP_ENABLE_TO_FLASH`）：
  `idf.py -p <PORT> coredump-info`（寄存器/调用栈/任务列表）、`idf.py -p <PORT> coredump-debug`（gdb）；也可 `-s core.dump` 存盘后 `coredump-info -c core.dump` 离线解析。
- **三通道混音调试（T3.3 起）**：console `player mix stat` 看 `MIXSTAT`（`vol/vol_q8/vol_target/limiter/limiter_peak/clip/mix_peak/block_max_us/cap_len/cap_pos`）与三行 `MIXCH`（`active/gain_q8/level_ms/peak`；gain_q8 256=0dB、128≈-6dB、64≈-12dB）；`player mix tone <TTS|PROMPT|MUSIC> <freq> <ms> [amp]` 往通道塞正弦探针（可三路叠加，播放器空闲时自动起混音泵，真实播放中拒绝）；`player mix vol <0~100>` 运行时改音量（20ms 斜坡、不落盘）；`player mix cap <n>` + `player mix dump <off> <n>` 抓混音输出（48k 单声道，`cap_pos` 是实际抓到的帧数，dump 只能读 `[0, cap_pos)`），供 PC 侧 FFT/幅度/包络/限幅对拍；`player mix stop` 停探针。自动回归 `uv run tools/mixer_test.py`（**113 项断言**：三通道幅度比 1:0.5:0.25、音量线性、软限幅不削波、淡入淡出无 pop、10 轮堆稳定；`--skip-heap` 快速冒烟）。**dump 是整段一次 write 输出**（逐行 printf 会被并发日志插字符流导致 PC 侧丢行）。
- **端到端链路自测（T2.6 起）**：`uv run tools/e2e_voice_test.py` 走真实用户路径（afplay 唤醒 → 提示音 → 自动起轮 → 交互语音 → mock 真实 MP3 流式播放 → R2），10 轮逐轮断言（P2 三条 + 半双工/拾音/堆/PSRAM/任务/无 ERROR）；`--dry-run` 不连设备。与 `voice_cloud_test`（协议/事件、合成帧、`vc start` 手动触发）、`tts_stream_test`（`vc start/stop` + 真实 MP3 播放硬指标、含 abort/stall/late-bin）三方分工。拾音判据带环境噪声兜底（baseline=min(播放前后静音峰)，≥800 重试→WARN），**不要用重试掩盖固件 bug**。
- **解析 coredump 必须用崩溃那一刻的 ELF**（`build/esp-ai.elf`，含符号与地址布局）：崩溃后先备份该文件再改代码/重建；要交付或现场测试的固件需把 elf 与 commit 一起归档，否则 coredump 无法还原调用栈。
- **会话状态机调试（T3.1 起）**：`vs status` 看当前状态/sid/轮次与异常计数；`vs start`/`vs interrupt` 手动起轮/打断；迁移日志 `VS: state A -> B（sid=N，停留 M ms，原因=…）`。唤醒链路日志自 T3.1 起由 `VS` 输出（`VS: wake detected` / `VS: prompt done, feed resumed`），测试脚本正则用 `(?:APP|VS)` 兼容新旧固件。错误路径用 mock 验证：`--scenario error`（云端 error → ERROR）、`--think-ms 12000`（THINKING 10s 超时 → ERROR）；T3.6 起这些路径都会播提示音（`FB`/`VS: 错误反馈…` 日志），脚本断言见下一条。
- **mock 真 ASR（T3.1 起，测试用）**：`uv run --with av --with numpy --with sherpa-onnx tools/ws_mock_server.py --asr sensevoice` 让 mock 把上行 Opus 帧解码后用本地 SenseVoice 识别（`tools/mock_asr.py`，独立于 DSH；默认假 ASR 路径不装这些依赖）；`uv run tools/mock_asr.py --test <wav>` 可离线验证识别；真机回归用 `uv run tools/voice_cloud_test.py --asr --asr-expect <子串>`。限制：离线整段识别、无中间结果，失败回退 `--tts-text`，只用于测试链路。
- **mock 本地 TTS（T3.1 起，测试用）**：`uv run tools/mock_tts.py --text "…" --out /tmp/reply.mp3` 用 Piper（MIT，中文音色 60MB，CPU 实时）生成 **24kHz 单声道 MP3**，再 `uv run tools/ws_mock_server.py --tts-mp3 /tmp/reply.mp3` 让设备播真实语音；模型缓存 `~/.cache/esp-ai-mock-tts/piper/`，`--download` 首次准备。更高自然度方案（Kokoro/MeloTTS/CosyVoice）暂缓，先用 Piper。
- **连续对话调试（T3.7 起）**：默认开启续听窗口（`CONFIG_VS_FOLLOWUP_ENABLE=y`，5s / 最多 5 轮 / 人声确认 250ms）；`vs status` 看「续听」统计与窗口剩余；`vs followup on|off` 运行期开关（不持久化，单轮回归/嘈杂环境用 off）。真机用例 `uv run tools/voice_cloud_test.py --followup --followup-rounds 2`（窗口内 `afplay` 语音即可起下一轮，无需唤醒）；`e2e_voice_test` 每轮会先 `vs followup off` 保持单轮语义。已知限制：环境持续人声可能误触发续听轮（确认窗口+轮数上限兜底）。
- **按键/静音调试（T3.4 起）**：`dev status` 看静音状态/当前音量/短按与长按计数/事件投递失败；`dev mute [on|off]` 等价长按静音（音量 0，恢复时回原值，不落 NVS）。自动回归 `uv run tools/dev_ctrl_test.py`（21 项不含打断 / 33 项含打断：短按起轮、长按静音往返、播放中按键打断 R4 停声 <100ms、续听窗口内短按退出、堆稳定；`--skip-interrupt` 不需要 mock）。**按键不用手按**：DevKit 的 USB 自动下载电路把 DTR 接到 GPIO0、RTS 接到 EN，脚本保持 `rts=False` 只翻转 `dtr` 即可模拟 BOOT 按键，走的是真实 GPIO 扫描路径；脚本**独占串口**，不要与 `idf.py monitor` 同时开（会抢 DTR）。
- **异常反馈调试（T3.6 起）**：`fb status` 看活动反馈/通道统计/四类计数；`fb reset` 清统计；`fb test <net|cloud|timeout|tts>` 直接播一条提示音（不起会话，回板人工听感验收用）。自动回归 `uv run tools/error_prompt_test.py`（**场景 A~J、289 项断言**：离线唤醒 / 中途断网 / 云端 error 1 / 云端 3001 / THINKING 超时 / 设备侧 TTS 失败（`--tts-bad-codec`）/ 打断不误播 / 正常轮不误播 / SPEAKING 中报错先停 TTS 再播 / 10 轮稳定；`--self-test` 51 项离线、`--dry-run`、`--scenarios A-C`；脚本独占串口）。设备侧日志契约：`VS: 错误反馈（类别=…，code=…，sid=…）→ 提示音（R1）`、`VS: 错误反馈完成（类别=…，ok=…，时长=… ms）→ IDLE`、`FB: 反馈 kind=… → 播放提示音（…）`、`VS: 会话进入 ERROR（…）`。**注意**：离线唤醒（ws 未 READY）走"不播我在呢、直接播网络未连接"的 T3.6 新链路，脚本已用双分支兼容；单轮/健康检查时 `VS: 会话进入 ERROR（…）` 是预期日志，不算错误（panic/assert 仍 FAIL）。
- **提示音资源调试（T3.5 起）**：`prompt info` 看 assets pack 状态（mode=flash-mmap/psram-fallback/none、pack 版本/条目/字节数/加载耗时/映射地址、5 条提示音的采样点/时长/PC 地址）；pack 缺失/损坏时 `prompt_store_init()` 报 ERROR 且业务降级（唤醒跳过"我在呢"直接起轮、错误反馈只日志），`prompt info` 显示失败原因。更新提示音：改 `tools/prompts_src/*.wav`（或 `uv run tools/prompt_gen.py` 重生成）→ `uv run tools/audio_pack.py` 生成 `build/assets.bin` → `uv run tools/audio_pack.py --flash <PORT>` → 重启生效（**app 固件不用重编**）；`uv run tools/error_prompt_test.py` 回归四类提示音。pack 格式与校验口径见 `components/prompt_store/README.md`、ARCH §3.4。
- **板载 LED 已于 2026-11-02 移除**（板载 WS2812 解码不可靠、满白电流触发 BOD，见 PENDING PD-003）：不要在没有硬件结论前重新引入 LED 驱动；`dev_ctrl` 现在只把 GPIO48 拉低保持。
- **mock 回声测试（T3.1 起，不要固定文案）**：`uv run --with av --with numpy --with sherpa-onnx --with piper-tts --with lameenc tools/ws_mock_server.py --asr sensevoice --tts-piper` —— 设备说的话先 ASR 识别，再把**识别文本**用 Piper 合成 24kHz MP3 下发，设备复读；同时验证 ASR 正确性与 TTS/播放链路。`--tts-piper` 与 `--tts-mp3` 互斥、必须配 `--asr`；合成失败只 WARN 回退合成帧。

## 9. 禁止事项

- 禁止把音频大缓冲放内部 RAM。
- 禁止在音频任务里做 TLS/HTTP/Flash。
- 禁止硬编码 WiFi、服务器地址、设备 token（走 Kconfig/NVS）。
- 禁止绕过 `voice_session` 直接改会话状态。
- 禁止绕过 `audio_fe_feed_hold/release` 直接改半双工门控（含新增第三路 R1/R2 时各自造开关）。
- 禁止绕开 `feedback` 另播错误提示音 / 另做一套异常反馈（新通道订阅 `APP_EVT_FEEDBACK`）。
- 禁止把提示音 PCM 编进 app（必须走 `assets` 分区 pack + `prompt_assets_get()`，见 §3）。
- 禁止引入未评估的第三方组件（先评估内存/许可证并记入文档）。
- 禁止在未更新架构文档的情况下改变分层、协议或分区布局。
