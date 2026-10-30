# 智能语音硬件 —— 开发排期（v1.0）

> 文档定位：任务拆分、工期、依赖与验收。
> 架构决策见 [ARCHITECTURE.md](ARCHITECTURE.md)；硬件见 [HARDWARE.md](HARDWARE.md)；待决策方向见 [PRODUCT_IDEAS.md](PRODUCT_IDEAS.md)。
> 更新规则：进度与基线写入 [STATUS.md](STATUS.md)；本文件只在计划变更时更新；采纳 PRODUCT_IDEAS 中的方向后，先更新架构文档，再更新本排期。

**排期基线**：1 人全职，31 个工作日 + 3 天缓冲 ≈ 7 周。
**总原则**：P0（地基）与 P2（语音闭环）不可压缩；每阶段结束必须 build 通过、可演示、记录 size 与内存基线。

---

## 1. 排期总览

| 阶段 | 主题 | 工期 | 工作日 | 前置 | 关键交付物 |
|---|---|---|---|---|---|
| **P0** | 地基 | 3 天 | D1–D3 | — | 分区表、15 个组件骨架、board/audio_io/diag、POC 迁移 |
| **P1** | 联网 | 5 天 | D4–D8 | P0 | config_store、net_mgr、transport_ws、event_bus、mock 服务 |
| **P2** | 语音闭环 | 6 天 | D9–D14 | P1 | audio_fe 事件化、Opus 编码、MP3 流式播放、voice_cloud |
| **P3** | 体验 | 5 天 | D15–D19 | P2 | 会话状态机、半双工、按键/LED、提示音、错误处理 |
| **P4** | 音乐 | 7 天 | D20–D26 | P3 | 源抽象、music_cloud、music_session、ducking |
| **P5** | 稳定与联调 | 5 天 | D27–D31 | P4 | console 补全、实测基线、24h 压测、后端联调 |
| 缓冲 | — | 3 天 | D32–D34 | — | 用于吸收 P0–P5 的溢出 |

依赖关系：**P0 → P1 → P2 → P3 → P4 → P5 串行**。
可并行项：`ws_mock_server.py`（T1.5）可与 T1.3/T1.4 并行；`audio_player` 的 MP3 解码预研可在 P1 期间穿插进行。

---

## 2. 阶段任务清单

### P0 地基（D1–D3）

> 参考：ARCHITECTURE §2、§3.1–3.2、§5.1、§9.1、§9.3、§10.2；HARDWARE §2–§3

| 编号 | 任务 | 产出 |
|---|---|---|
| T0.1 | 替换 `partitions.csv` 为定稿方案，校验并通过烧录 | 分区表可用，model 分区可被 `esp_srmodel_init` 正常加载 |
| T0.2 | 建 15 个组件骨架（`components/<module>/`：CMakeLists + `include/*.h` + Kconfig + 空实现） | `idf.py build` 通过 |
| T0.3 | `board`：引脚宏、LED/按键驱动、初始化入口 | LED 可控、按键可读 |
| T0.4 | `audio_io`：I2S0 RX 16k（32bit→int16）、I2S1 TX 48k 立体声、环形缓冲 | 采集/播放通路可用，48k 播测试音正常 |
| T0.5 | `diag`：日志 TAG 规范落地、启动信息、`esp_console` 框架 + `sys info/heap/tasks`、`log` 命令 | console 可交互 |
| T0.6 | POC 迁移：AFE 唤醒 → `audio_fe` 回调 → `audio_player` 播放提示音（`CH_PROMPT`）；删除 `main/esp-ai.c` 旧代码 | 唤醒 + 提示音功能不退化 |
| T0.7 | 交付自检：烧录、唤醒、播放、console；记录固件 size 与内存水位基线 | P0 验收报告（数据记录在 STATUS.md 基线表） |

**验收**：`idf.py build` 通过；说"嗨，乐鑫"播放应答；`sys heap` 可用；`main/` ≤ 150 行。

### P1 联网（D4–D8）

> 参考：ARCHITECTURE §4.2、§6.1–6.3、§8.5、§10.1

| 编号 | 任务 | 产出 |
|---|---|---|
| T1.1 | `config_store`：Kconfig 常量（WiFi/WS/设备参数）+ NVS 读写封装 | 音量等运行态可持久化 |
| T1.2 | `event_bus`：事件定义、注册/分发封装、日志打点 | 模块间解耦通道就绪 |
| T1.3 | `net_mgr`：STA 连接、状态机、退避重连（1s→30s）、SNTP 对时、事件上报 | 上电自动联网，断网可恢复 |
| T1.4 | `transport_ws`：wss 客户端、二进制帧收发、发送队列、心跳（20s ping/pong）、断线重连 | 与服务器可收发数据 |
| T1.5 | `tools/ws_mock_server.py`：本地模拟服务（握手/心跳/事件下发/语音音频回放） | 脱机可联调 |
| T1.6 | 联调：联网、心跳、断网恢复、mock 收发；记录内存增量 | P1 验收报告 |

**验收**：断线重连 < 5s；mock 服务收发正常；内部 RAM 峰值记录且 ≤ 310KB。

### P2 语音闭环（D9–D14）

> 参考：ARCHITECTURE §3.1–3.2、§3.6–3.7、§6、§8.1

| 编号 | 任务 | 产出 |
|---|---|---|
| T2.1 | `audio_fe`：唤醒/VAD 回调、`set_feed_enabled()`、上行 16k 音频输出接口 | 事件驱动的语音前端 |
| T2.2 | `codec_enc`：Opus 16k/单声道/16kbps/20ms 帧编码 | 上行编码可输出帧 |
| T2.3 | `audio_player`：MP3 解码（Helix）、重采样到 48k、单通道播放 | 本地 MP3 可播放 |
| T2.4 | `audio_player`：流式播放接口（`play_stream` 回调取数据） | TTS 流式播放能力 |
| T2.5 | `voice_cloud`：`hello` 握手、`audio_start/audio_end`、二进制帧收发、`tts_start/tts_end` 解析 | 端云语音通道打通 |
| T2.6 | 端到端自测：mock 服务下发 MP3 音频，设备播放 | 完整"说话 → 听回答"链路 |

**验收**：唤醒后上行音频可被服务端收到；收到的 MP3 可流式播放；TTS 首帧到出声 < 150ms。

### P3 体验（D15–D19）

> 参考：ARCHITECTURE §3.3–3.5、§7、§9.3

| 编号 | 任务 | 产出 |
|---|---|---|
| T3.1 | `voice_session`：状态机（IDLE/LISTENING/THINKING/SPEAKING/ERROR）、`sid` 规则、超时与错误处理 | 会话可控 |
| T3.2 | 半双工规则 R1–R5 实现与真机验证 | 播放不误唤醒、打断可恢复 |
| T3.3 | 三通道混音、音量 0~100、软限幅 -1dBFS、淡入淡出 | 播放质量达标 |
| T3.4 | `dev_ctrl`：短按打断/开始对话、长按静音、LED 状态模式 | 物理交互可用 |
| T3.5 | 提示音资源制作 + `tools/audio_pack.py` 打包到 assets 分区 | 提示音可配置 |
| T3.6 | 错误提示：网络未连接、TTS 失败、超时 | 异常有明确反馈 |

**验收**：按键打断 < 100ms；连续 10 轮对话内部堆稳定（无单调下降）；LED 状态与实际状态一致。

### P4 音乐（D20–D26）

> 参考：ARCHITECTURE §3.2、§6.3、§8.3–8.4

| 编号 | 任务 | 产出 |
|---|---|---|
| T4.1 | 源抽象：`source_http`（流式拉取、超时/重试）、`source_file`（FATFS） | 两种来源可播放 |
| T4.2 | `music_cloud`：`music_request` 请求、`music_track` 解析、`music_status` 每 5s 上报 | 音乐协议闭环 |
| T4.3 | `music_session`：播放/暂停/继续/停止/切歌、状态机、与语音抢占 | 音乐控制完整 |
| T4.4 | 音乐 ducking（-12dB）与"音乐播放中唤醒"流程 | 语音优先体验 |
| T4.5 | 播放稳定性：预缓冲 200ms、断流重试、错误恢复 | 长播不中断 |

**验收**：连续播放 30 分钟无卡顿；起播 < 1.5s；唤醒时音乐 duck 正常、会话后恢复。

### P5 稳定与联调（D27–D31）

> 参考：ARCHITECTURE §6、§9.2、§10.2；HARDWARE §8

| 编号 | 任务 | 产出 |
|---|---|---|
| T5.1 | console 命令补全：`mic stat`/`audio`/`voice`/`music`/`wifi`/`net ping` | 调试命令齐全 |
| T5.2 | `tools/record_wav.py` 串口采集，输出唤醒率/误唤醒基线数据 | 语音指标基线 |
| T5.3 | 24h 压测：周期打印堆/任务水位、播放+对话交替、记录异常 | 稳定性报告 |
| T5.4 | 与后端真实服务按架构第 6 节契约联调，问题清单闭环 | 端云链路可交付 |
| T5.5 | 文档收尾：`PROTOCOL.md` 补全、HARDWARE 自检结果、README 更新 | 文档齐备 |

**验收**：24h 无重启、无内存泄漏；真实服务下完成语音对话 + 音乐播放；问题清单清零。

---

## 3. 阶段依赖与并行

```
P0 ──> P1 ──> P2 ──> P3 ──> P4 ──> P5
 │       │
 │       └─ ws_mock_server.py 可提前并行（解锁 P2~P4 自测）
 └─ audio_io/board 完成后，audio_player 的 MP3 解码预研可穿插在 P1 期间
```

- 后端服务端进度不影响端侧 P0–P4（用 mock 服务兜底）。
- P5 的后端联调是唯一强依赖服务端的任务，若服务端延期，可顺延 P5.4，不阻塞前四项。

---

## 4. 排期风险与缓冲

| 风险 | 影响 | 缓冲安排 |
|---|---|---|
| Opus 库集成/性能不达标 | P2 延期 | 用缓冲 1 天；必要时降级为 PCM16（架构需同步更新） |
| MP3 解码内存/CPU 超预期 | P2/P4 延期 | 用缓冲 1 天；降低预缓冲、解码放核 1 |
| 唤醒率/误唤醒不达标 | P3 体验 | 用缓冲 2 天做麦克风结构/阈值调优 |
| 后端接口延期 | P5 联调 | mock 兜底，联调顺延，不占端侧排期 |
| 内部 RAM 超预算 | 全阶段 | 立即停下优化（改 PSRAM 分配/精简 TLS），不靠缓冲硬扛 |

---

## 5. 交付与提测标准

每个阶段结束必须满足：

1. `idf.py build` 通过，`idf.py size` 无异常增长；
2. 本阶段验收项逐条演示通过；
3. 记录固件 size、内部 RAM/PSRAM 峰值到 [STATUS.md](STATUS.md) 的基线表；
4. 相关文档同步更新（架构 → ARCHITECTURE；协议 → PROTOCOL；进度 → STATUS；排期 → 本文）。

提测（P5 结束）额外要求：24h 压测报告、已知问题清单、烧录与调试说明。

---

## 6. 变更记录

| 日期 | 版本 | 变更内容 |
|---|---|---|
| 2026-10-30 | v1.0 | 初版：P0–P5 排期、任务清单、验收标准、风险缓冲 |
