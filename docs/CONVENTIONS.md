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
- 内部 RAM 预算：峰值 ≤ 310KB，保留 ≥ 30KB；超预算先改设计，不靠砍功能硬压。
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
- 内存问题先 `sys heap`/`sys tasks`；音频问题先 `mic stat`，再 `tools/record_wav.py` 抓 WAV。
- 云端/WS 问题先跑 mock，别直接上设备：`uv run tools/ws_mock_server.py --self-test`（9 项，不需要硬件）确认协议链路，再起 `uv run tools/ws_mock_server.py` 给设备当服务端；字段口径对照 `docs/PROTOCOL.md`（含待确认项）。
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
