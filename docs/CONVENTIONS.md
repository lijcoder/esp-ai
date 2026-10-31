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

更新义务：架构变更 → 改 ARCHITECTURE；计划变更 → 改 ROADMAP；**任务进度与基线 → 改 STATUS.md（不动 AGENTS.md）**；新方向 → 先写 PRODUCT_IDEAS；硬件变更 → 改 HARDWARE。协议字段变更需同时改 ARCHITECTURE §6 与 `PROTOCOL.md`。

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
- 播放必须淡入 10ms / 淡出 100ms；混音后软限幅 -1dBFS。
- 唤醒词/VAD 回调里只投递事件，不做耗时操作。
- 关键路径打点：唤醒、上行首帧、TTS 首帧、打断、起播。

## 4. 内存

- 大块缓冲（播放 ring、解码、HTTP）必须 PSRAM：`heap_caps_malloc(size, MALLOC_CAP_SPIRAM)`。
- I2S DMA 缓冲必须内部 RAM：`MALLOC_CAP_DMA | MALLOC_CAP_INTERNAL`。
- 内部 RAM 预算（**X3-E 定稿，2026-10-31**）：**静态 DIRAM + 运行期堆峰值 ≤ 440KB**，且**最坏场景（wss/TLS 握手期）`sys heap` 最低水位 ≥ 30KB**；超预算先改设计，不靠砍功能硬压。
  - 口径与实测：`idf.py size` 的 DIRAM 已用 + `sys heap` 的「峰值占用」（internal），两者相加对照 440KB；`sys heap` 的「最低水位」对照 30KB。
  - 当前值（T2.4 后，2026-10-31）：静态 **196,998 B** + wss 堆峰值 **227,480 B** = **424,478 B**（距 440KB 上限余量 26,082 B），wss 最低水位 **32,671~32,719 B**（距 30KB 线余量 1.95~2.05KB）；`ws://` 内部堆峰值约 226.5KB；PSRAM 峰值 **1,446,724 B**（wss 冷启动口径；PCM 上限 1.5MB→512KB 后大幅下降）。T2.3 时为 424,506 B / 32,471~32,643 B / PSRAM 2,895,336 B；T2.5 时为 424,658 B / 32,339 B / PSRAM 902,908 B；T2.2 时为 409,918 B / 47,051~52,839 B。
  - 两条必须同时满足；**最坏场景下「最低水位 ≥30KB」是更强约束**（堆总量 ~259,843 B → 堆峰值上限约 229,843 B，折合静态+堆 ≤426.8KB）；440KB 上限用于吸收静态 DIRAM 后续增长。**T2.4 后水位余量只剩 ~2KB：后续任务不得再新增内部 RAM 任务栈/静态/堆占用，需要缓冲时放 PSRAM 或复用现有任务。**
  - TLS 记录缓冲与握手堆自 X3-F 起在 PSRAM，不计入内部 RAM；旧口径「峰值 ≤310KB」写于 P0（未含 WiFi 栈且只统计堆），已作废。
- 长时运行场景必须监控堆水位（`diag`），不允许无限增长。

## 5. 代码规范

- 命名：函数/变量 `snake_case`，宏/常量 `UPPER_CASE`，文件与模块同名。
- 对外接口统一返回 `esp_err_t`，错误码定义在组件头文件。
- 日志 TAG 固定：`BOARD/AUDIO_IO/AFE/PLAYER/ENC/NET/WS/VC/VS/MC/MS/DEV/DIAG/CFG/EVT/APP`（`APP` 属 `main/` 启动编排，`EVT` 属 `event_bus` 基础服务）；状态迁移 INFO、异常 WARN、失败 ERROR。
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
- 云端/WS 问题先跑 mock，别直接上设备：`uv run tools/ws_mock_server.py --self-test`（**13 项**，不需要硬件）确认协议链路，再起 `uv run tools/ws_mock_server.py` 给设备当服务端；字段口径对照 `docs/PROTOCOL.md`（含待确认项）。**mock 已默认对每条连接开 `TCP_NODELAY`**：Nagle + 端侧 delayed ACK 会把均匀 TTS 帧攒批 ~200ms，造成端侧假欠载；真实云端同样必须关 Nagle。
- 崩溃先看串口 panic 输出，再解析 coredump 分区（`coredump` @0x9a0000/64K，已开 `CONFIG_ESP_COREDUMP_ENABLE_TO_FLASH`）：
  `idf.py -p <PORT> coredump-info`（寄存器/调用栈/任务列表）、`idf.py -p <PORT> coredump-debug`（gdb）；也可 `-s core.dump` 存盘后 `coredump-info -c core.dump` 离线解析。
- **解析 coredump 必须用崩溃那一刻的 ELF**（`build/esp-ai.elf`，含符号与地址布局）：崩溃后先备份该文件再改代码/重建；要交付或现场测试的固件需把 elf 与 commit 一起归档，否则 coredump 无法还原调用栈。

## 9. 禁止事项

- 禁止把音频大缓冲放内部 RAM。
- 禁止在音频任务里做 TLS/HTTP/Flash。
- 禁止硬编码 WiFi、服务器地址、设备 token（走 Kconfig/NVS）。
- 禁止绕过 `voice_session` 直接改会话状态。
- 禁止引入未评估的第三方组件（先评估内存/许可证并记入文档）。
- 禁止在未更新架构文档的情况下改变分层、协议或分区布局。
