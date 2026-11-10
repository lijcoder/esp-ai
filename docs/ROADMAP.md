# 智能语音硬件 —— 开发排期（v1.7）

> 文档定位：任务拆分、工期、依赖与验收。
> 架构决策见 [ARCHITECTURE.md](ARCHITECTURE.md)；硬件见 [HARDWARE.md](HARDWARE.md)；待决策方向见 [PRODUCT_IDEAS.md](PRODUCT_IDEAS.md)。
> 更新规则：进度与基线写入 [STATUS.md](STATUS.md)；本文件只在计划变更时更新；采纳 PRODUCT_IDEAS 中的方向后，先更新架构文档，再更新本排期。

**排期基线**：1 人全职，31 个工作日 + 3 天缓冲 ≈ 7 周。
**总原则**：P0（地基）与 P2（语音闭环）不可压缩；每阶段结束必须 build 通过、可演示、记录 size 与内存基线。

---

## 1. 排期总览

| 阶段 | 主题 | 工期 | 工作日 | 前置 | 关键交付物 |
|---|---|---|---|---|---|
| **P0** | 地基 | 3 天 | D1–D3 | — | 组件化骨架、diag/console、真机自检、分区表（验证后执行，见 §2.0） |
| **P1** | 联网 | 5 天 | D4–D8 | P0 | config_store、net_mgr、transport_ws、event_bus、mock 服务 |
| **P2** | 语音闭环 | 6 天 | D9–D14 | P1 | audio_fe 事件化、Opus 编码、MP3 流式播放、voice_cloud |
| **P3** | 体验 | 5 天 | D15–D19 | P2 | 会话状态机、半双工、按键/静音、连续对话、提示音、错误反馈（T3.5 assets 迁移收尾） |
| **P4** | 音乐 | 7 天 | D20–D26 | P3 | 源抽象、music_cloud、music_session、ducking |
| **P5** | 稳定与联调 | 5 天 | D27–D31 | P4 | console 补全、实测基线、24h 压测、后端联调 |
| 缓冲 | — | 3 天 | D32–D34 | — | 用于吸收 P0–P5 的溢出 |

依赖关系：**P0 → P1 → P2 → P3 → P4 → P5 串行**。
可并行项：`ws_mock_server.py`（T1.5）可与 T1.3/T1.4 并行；`audio_player` 的 MP3 解码预研可在 P1 期间穿插进行。
**阶段内执行顺序见 §2.0 —— 任务编号是标识，不是执行顺序。**

---

## 2.0 执行顺序与依据

**编号是任务标识，不是执行顺序。** 排序遵循三条原则：

1. **先建观测能力**：能验证再动手，避免盲调；
2. **依赖在前**：被依赖的任务必须先完成；
3. **高风险/不可逆改动放在验证之后**：先证明链路，再改 flash 布局，改完立即回归。

### P0 剩余顺序

| 顺序 | 任务 | 为什么在这个位置 | 前置 |
|---|---|---|---|
| 1 | T0.5 diag + esp_console | 后续每个任务都靠它自测；没有 console，T0.7 只能靠猜 | — |
| 2 | T0.7a diag 增 `mic stat` | T0.7① 的电平基线必须可复现：原数据来自已删除的临时打点，违反 CONVENTIONS §8；先有观测再验收 | T0.5 |
| 3 | T0.7 第一轮真机自检 | 用 `mic stat` 复测唤醒/提示音/麦克风 RMS/底噪，建立基线与信任 | T0.5、T0.7a |
| 4 | T0.1 分区表替换 + 重烧 | 改 app 偏移、新增 assets/ota 槽位，影响面大；链路已验证后再改，出问题能区分"布局问题"还是"代码问题" | T0.7 第一轮 |
| 5 | T0.7 第二轮回归 | 分区表改动后重跑关键项，确认无回归 | T0.1 |

> T0.1 的 `nvs` 分区是 **P1 的硬前置**（T1.1 `config_store`、T1.3 WiFi 都依赖 NVS），不是"可延后的 OTA 预留"；同时它改 app 偏移，必须一次到位、改完立即回归。

### P1 顺序

| 顺序 | 任务 | 依据 | 前置 |
|---|---|---|---|
| 0 | T1.5 ws_mock_server.py | 可最早启动，端侧不必等后端 | — |
| 1 | T1.1 config_store | WiFi 凭证/音量要落盘，后续都依赖 | — |
| 2 | T1.2a event_bus 事件定义 | net_mgr 与 transport_ws 同为 L1，必须经事件总线通信；事件接口先定，T1.3/T1.4 不返工 | T1.1 |
| 3 | T1.3 net_mgr | 联网是 WS/云端前提（产出 `NET_UP/DOWN` 事件） | T1.1、T1.2a |
| 4 | T1.4 transport_ws | 用 mock 服务即可联调（订阅 `NET_UP`） | T1.3、T1.5 |
| 5 | T1.2b 事件接线 | 唤醒/VAD/播放改走事件，随 T2.5/T3.1 落地，避免 P2 返工 | T1.4 |
| 6 | T1.6 联调与基线 | 收口 | 1–5 |

### P2 双轨并行

| 轨 | 顺序 | 任务 | 说明 |
|---|---|---|---|
| A 上行 | A1→A2→A3 | T2.1 audio_fe 上行读取 → T2.2 Opus 编码 → T2.5 voice_cloud | 用 mock 服务验证"设备能发音频" |
| B 下行 | B1→B2 | T2.3 MP3 解码 → T2.4 流式播放 | 用 mock 下发 MP3 验证"设备能播云音频" |
| 收口 | — | T2.6 端到端 | A、B 都完成后做；单人串行建议 A→B→T2.6，两人可并行 |

### P3–P5 顺序（依据）

- **P3**：T3.1 状态机（依赖 P2 事件）→ T3.2 半双工 R1–R5（MVP 已有 R1/R2）→ T3.3 混音/音量/限幅 → T3.4 按键/LED → T3.6 错误提示 → T3.5 assets 迁移（不阻塞主链路，放最后；前置 T0.1）。
  **T3.7（连续对话）为 T3.1 后新增的 P3 附加任务**（来自 PRODUCT_IDEAS Q6 决策），建议在 T3.2 之后实施；与 T3.4 的按键退出有接口交集（窗口内短按 = 退出续听）。
- **P4**：T4.1 源抽象 → T4.2 music_cloud → T4.3 music_session → T4.4 ducking → T4.5 稳定性（先能播、再协议、再抢占）。
- **P5**：T5.1 console 补全（依赖全部模块）→ T5.2 实测基线 → T5.3 24h 压测 → T5.4 后端联调 → T5.5 文档收口。

> 阶段之间仍串行（P0→P5）；并行只发生在标注的双轨与工具任务上。

---

## 2. 阶段任务清单

### P0 地基（D1–D3）

> 参考：ARCHITECTURE §2、§3.1–3.2、§5.1、§9.1、§9.3、§10.2；HARDWARE §2–§3

| 编号 | 任务 | 产出 |
|---|---|---|
| T0.1 | 替换 `partitions.csv` 为定稿方案，校验并通过烧录 | 分区表可用，model 分区可被 `esp_srmodel_init` 正常加载 |
| T0.2 | 建 P0 所需组件骨架（本轮 5 个：board/audio_io/audio_fe/audio_player/assets；其余模块随对应任务创建，避免空骨架） | `idf.py build` 通过 |
| T0.3 | `board`：引脚宏、按键初始化/读取（LED 驱动留给 `dev_ctrl`/P3） | 按键可读、引脚集中定义 |
| T0.4 | `audio_io`：I2S0 RX 16k（32bit→int16）、I2S1 TX 48k 立体声 | 采集/播放通路可用（真机播测试音在 T0.7 验证） |
| T0.5 | `diag`：日志 TAG 规范落地、启动信息、`esp_console` 框架 + `sys info/heap/tasks`、`log` 命令 | console 可交互 |
| T0.6 | POC 迁移：AFE 唤醒 → `audio_fe` 回调 → `audio_player` 播放提示音（`CH_PROMPT`）；删除 `main/esp-ai.c` 旧代码 | 唤醒 + 提示音功能不退化 |
| T0.7 | 两轮真机自检：① T0.5 后验证唤醒/提示音/麦克风 RMS/底噪；② T0.1 分区表改完后回归关键项 | 两轮验收记录，数据写入 STATUS.md 基线表 |
| T0.7a | `diag` 增 `mic stat`/`mic reset`：`audio_fe` 暴露只读统计（电平 RMS/峰值、帧数、I2S 读失败、唤醒次数、VAD、feed 状态） | 底噪/说话电平可在设备上直接读取，T0.7① 基线与 T5.2 实测可复现 |
| T0.7b | `diag` 增 `sys crash`（Kconfig 开发开关）：主动 panic 验证 coredump 落盘与 `idf.py coredump-info` 解析 | coredump 排障链路可用（ARCH §9.1、CONVENTIONS §8） |

**验收**：`idf.py build` 通过；说"嗨，乐鑫"播放应答；`sys heap` 可用；`main/` ≤ 150 行。

### P1 联网（D4–D8）

> 参考：ARCHITECTURE §4.2、§6.1–6.3、§8.5、§10.1

| 编号 | 任务 | 产出 |
|---|---|---|
| T1.1 | `config_store`：Kconfig 常量（WiFi/WS/设备参数）+ NVS 读写封装 | 音量等运行态可持久化 |
| T1.2a | `event_bus` 事件定义：13 个事件 + 定长 payload + 注册/分发封装（独立 esp_event 循环）+ `evt` 观测命令 | 模块间解耦通道就绪，`evt` 可观测 |
| T1.2b | 事件接线：唤醒/VAD/播放改走 `event_bus`，随 T2.5/T3.1 落地 | 业务链路走事件，避免 P2 返工 |
| T1.3 | `net_mgr`：STA 连接、状态机、退避重连（1s→30s）、SNTP 对时、事件上报 | 上电自动联网，断网可恢复 |
| T1.4 | `transport_ws`：wss 客户端、二进制帧收发、发送队列、心跳（20s ping/pong）、断线重连 | 与服务器可收发数据 |
| T1.5 | `tools/ws_mock_server.py`：本地模拟服务（握手/心跳/事件下发/语音音频回放） | 脱机可联调 |
| T1.6 | 联调：联网、心跳、断网恢复、mock 收发；记录内存增量 | P1 验收报告 |

**验收**：断线重连 < 5s；mock 服务收发正常；内部 RAM 按 CONVENTIONS §4 口径记录（静态 DIRAM + 堆峰值 ≤ 440KB，wss 握手期最低水位 ≥ 30KB）。

### P2 语音闭环（D9–D14）

> 参考：ARCHITECTURE §3.1–3.2、§3.6–3.7、§6、§8.1
>
> **P2 已完成（2026-10-31）**：三条验收全部 PASS（端到端 e2e 10/10、TTS 首帧 7.4~74ms、上行 vad-silence/四口径一致），数据见 [STATUS.md](STATUS.md) §5；下一阶段 P3。

| 编号 | 任务 | 产出 |
|---|---|---|
| T2.1 | `audio_fe`：唤醒/VAD 回调、`set_feed_enabled()`、上行 16k 音频输出接口 | 事件驱动的语音前端 |
| T2.2 | `codec_enc`：Opus 16k/单声道/16kbps/20ms 帧编码 | 上行编码可输出帧 |
| T2.3 | `audio_player`：MP3 解码（**minimp3**，状态/临时区在 PSRAM）、重采样到 48k、单通道播放（整段解码，T2.4 再流式） | 本地 MP3 可播放 |
| T2.4 | `audio_player`：流式播放（下行 0x02 写入 PSRAM ring、边收边解、起播门槛 4 帧/120ms、欠载补静音；`voice_cloud` 接线 + R1/R2） | TTS 流式播放能力 |
| T2.5 | `voice_cloud`：`hello` 握手、`audio_start/audio_end`、二进制帧收发、`tts_start/tts_end` 解析 | 端云语音通道打通 |
| T2.6 | 端到端自测：mock 服务下发 MP3 音频，设备播放 | 完整"说话 → 听回答"链路 |

**验收**：唤醒后上行音频可被服务端收到；收到的 MP3 可流式播放；TTS 首帧到出声 < 150ms。

### P3 体验（D15–D19）

> 参考：ARCHITECTURE §3.3–3.5、§7、§9.3
>
> **P3 完成（2026-11-02）**：T3.1（会话状态机）/T3.2（半双工 R1–R5）/T3.3（三通道混音、音量 20ms 插值、-1dBFS 软限幅、通道 duck/淡入淡出）/T3.7（连续对话免唤醒 follow-up）/T3.4（dev_ctrl 按键扫描+去抖、长按静音，不建任务；LED 状态灯后因硬件解码问题移除）/T3.6（错误提示：`feedback` 异常反馈组件 + 四类提示音 + `APP_EVT_FEEDBACK` 可扩展事件，真机 `error_prompt_test` 289 项）/T3.5（提示音迁 `assets` 分区：自定义只读 pack + `esp_partition_mmap`，可单独烧分区替换；app ≈−173KB）全部完成，数据见 [STATUS.md](STATUS.md) §5；**P3 收口，下一阶段 P4 音乐**。

| 编号 | 任务 | 产出 |
|---|---|---|
| T3.1 | `voice_session`：状态机（IDLE/LISTENING/THINKING/SPEAKING/ERROR）、`sid` 规则、超时与错误处理 | 会话可控 |
| T3.2 | 半双工规则 R1–R5 实现与真机验证 | 播放不误唤醒、打断可恢复 |
| T3.3 | 三通道混音、音量 0~100、软限幅 -1dBFS、淡入淡出 | 播放质量达标 |
| T3.4 | `dev_ctrl`：短按打断/开始对话、长按静音（**LED 状态模式 2026-11-02 移除**，板载 WS2812 硬件不可靠，见 PENDING PD-003） | 物理交互可用 |
| T3.5 | **提示音迁 assets 分区（2026-11-02 完成，方案 D）**：自定义只读 pack（`tools/audio_pack.py` 由 `tools/prompts_src/*.wav` 生成）+ `components/prompt_store`（启动校验 → `esp_partition_mmap` 零拷贝，失败回退 PSRAM）+ 删除 `components/assets`；接口 `prompt_assets_get()` 不变；**单独烧 assets 分区即可替换提示音（app 不动）**；pack 缺失/损坏降级不崩溃。**决策**：不用 SPIFFS（mount 占内部堆，水位余量仅 ~2.6KB），见 ARCH §3.4/PRODUCT_IDEAS §6 | 提示音可单独烧 `assets` 分区替换（不重编固件）；app 固件体积下降 ≈173KB（−176,656 B） |
| T3.6 | **错误提示（2026-11-02 完成）**：新增 `feedback` 异常反馈组件（L3，不建任务）——四类异常（网络未连接 / 云端 error / THINKING 超时 / TTS 失败）→ `ERROR` + 对应中文提示音，播完由完成回调回 `IDLE`（`CONFIG_VS_ERROR_HOLD_MS=3000` 仅兜底看门狗）；语音只是**当前唯一通道**，`APP_EVT_FEEDBACK` 语义事件供未来屏幕/灯光订阅（不改 `feedback`/`voice_session`）；唤醒时云端未 READY 直接给"网络未连接"反馈；会话中途断网也出声；设备侧 TTS 无法播放（`VC_EVT_TTS_FAIL`）也归"语音播放失败" | 异常有明确反馈（语音 + 日志 + 可断言状态） |
| T3.7 | **连续对话（免唤醒 follow-up）**：`FOLLOW_UP` 续听窗口（默认 5s / 最多 5 轮续听 / 人声确认 250ms）、窗口内直接说话免唤醒且不播提示音；静音超时/轮数上限/按键/断网退出；窗口内唤醒 = 重开一轮；`vs followup on\|off` 运行期开关 | 免唤醒连续多轮对话 |

**验收**：按键打断 < 100ms；连续 10 轮对话内部堆稳定（无单调下降）；**四类异常各自有提示音 + 日志（T3.6 验收：`error_prompt_test` 场景 A~J 289 项）**；**提示音可单独烧 `assets` 分区替换（T3.5 验收：app 不变，替换后重启生效）**。~~LED 状态与实际状态一致~~（LED 已于 2026-11-02 移除，见 PENDING PD-003）。

### P4 音乐（D20–D26）

> 参考：ARCHITECTURE §3.2、§6.3、§8.3–8.4

| 编号 | 任务 | 产出 |
|---|---|---|
| T4.1 | 源抽象：`source_http`（流式拉取、超时/重试；**只做远端 URL，不做 `source_file`**，2026-11-10 用户决定） | 远端 URL 可播放 |
| T4.2 | **`music_cloud`（2026-11-10 完成，构建口径）**：`music_track`/`music_control` 解析、`mid` 过滤、`music_status` 每 5s 上报；**`music_request`（端侧主动点歌）与 `pause/resume` 执行移入 T4.3**（ARCH §6.3 边界） | 音乐协议闭环（真机待板 PV-013） |
| T4.3 | `music_session`：播放/暂停/继续/停止/切歌、状态机、与语音抢占；**含 T4.2 移入的 `music_request` 与 `pause/resume` 执行（需给 `source_http` 加暂停能力）** | 音乐控制完整 |
| T4.4 | 音乐 ducking（-12dB）与"音乐播放中唤醒"流程 | 语音优先体验 |
| T4.5 | 播放稳定性：预缓冲 200ms、断流重试、错误恢复 | 长播不中断 |

> **T4.2 完成（2026-11-10，构建通过、真机待板 PV-013）**：新增 `music_cloud`（L3，不建任务）——WS 附加订阅槽解析 `music_track`/`music_control`，按连接内 `mid` 过滤（track 只收更新的、control 允许等于当前曲目），起播/切歌在 `esp_timer` 串行执行（切歌先 stop 再 play，120ms×15 重试），播放中每 5s 上行 `music_status{state,mid,progress_ms}` + 状态变化立即一次，周期定时器兼任 EOF/失败探测；`pause/resume` 只解析计数（执行归 T4.3）；`voice_cloud` 对音乐事件静默跳过；`diag` 增 `mc status|report|reset`。构建：bin 1,982,880 B / 总镜像 1,982,760 B / 静态 DIRAM 201,046 B（+784 B），无新任务。数据见 [STATUS.md](STATUS.md) §5。

**验收**：连续播放 30 分钟无卡顿；起播 < 1.5s；唤醒时音乐 duck 正常、会话后恢复。

### P5 稳定与联调（D27–D31）

> 参考：ARCHITECTURE §6、§9.2、§10.2；HARDWARE §8

| 编号 | 任务 | 产出 |
|---|---|---|
| T5.1 | console 命令补全：`mic stat`/`audio`/`voice`/`music`/`wifi`/`net ping` | 调试命令齐全 |
| T5.2 | `tools/record_wav.py` 串口采集，输出唤醒率/误唤醒基线数据 | 语音指标基线 |
| T5.3 | 24h 压测：周期打印堆/任务水位、播放+对话交替、记录异常 | 稳定性报告 |
| T5.4 | 与后端真实服务按架构第 6 节契约联调，问题清单闭环 | 端云链路可交付 |
| T5.5 | 文档收尾：`PROTOCOL.md` 补全、HARDWARE 自检结果、README 更新；**确认关闭开发期调试开关**（`DIAG_ENABLE_CRASH_TEST` 等） | 文档齐备、量产配置干净 |

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
| Opus 库集成/性能不达标 | P2 延期 | 用缓冲 1 天；必要时降级为 PCM16（架构需同步更新）。**已排除（T2.2，2026-10-31）**：microOpus 定点 + VBR 实测单帧 5.6~7.2ms（20ms 预算的 28~36%）、内部 RAM 增量 0（状态/伪栈在 PSRAM）、wss 最低水位 39,763 B → 无需降级，缓冲不动用 |
| MP3 解码内存/CPU 超预期 | P2/P4 延期 | 用缓冲 1 天；降低预缓冲、解码放核 1 |
| 唤醒率/误唤醒不达标 | P3 体验 | 用缓冲 2 天做麦克风结构/阈值调优 |
| 后端接口延期 | P5 联调 | mock 兜底，联调顺延，不占端侧排期 |
| 内部 RAM 超预算 | 全阶段 | 立即停下优化（改 PSRAM 分配/精简 TLS），不靠缓冲硬扛。**T2.5 后 wss 最低水位 32,339 B（距 30KB 线仅 2.3KB）**：T2.3/T2.4 新增任务栈/解码缓冲前必须先按 CONVENTIONS §4 估算，必要时复用 `player` 栈、解码缓冲全放 PSRAM |

---

## 5. 交付与提测标准

每个阶段结束必须满足：

1. `idf.py build` 通过，`idf.py size` 无异常增长；
2. 本阶段验收项逐条演示通过；
3. 记录固件 size、内部 RAM/PSRAM 峰值到 [STATUS.md](STATUS.md) 的基线表；
4. 相关文档同步更新（架构 → ARCHITECTURE；协议 → PROTOCOL；进度 → STATUS；排期 → 本文）；
5. **执行阶段回归清单**：说唤醒词有应答、console 命令可用、日志无 ERROR、连续 10 轮播放/对话后堆无单调下降。

提测（P5 结束）额外要求：24h 压测报告、已知问题清单、烧录与调试说明。

---

## 6. 变更记录

| 日期 | 版本 | 变更内容 |
|---|---|---|
| 2026-10-30 | v1.0 | 初版：P0–P5 排期、任务清单、验收标准、风险缓冲 |
| 2026-10-30 | v1.1 | T0.2 调整为按需创建组件骨架（MVP 先建 5 个），进度与基线改由 STATUS.md 维护 |
| 2026-10-30 | v1.2 | T0.3 去掉 LED（移到 dev_ctrl）、T0.4 去掉环形缓冲（由 audio_player 队列承担），与已实现代码对齐 |
| 2026-10-30 | v1.3 | 重排执行顺序：观测能力（diag/console）前置、分区表后移到真机验证之后；T0.7 明确两轮自检；新增 §2.0 顺序依据与 P2 双轨并行 |
| 2026-10-30 | v1.4 | 新增 T0.7a（`diag` 增 `mic stat`，0.5d）并前置于 T0.7①，保证电平基线可复现；P0 剩余顺序改为 T0.7a → T0.7① → T0.1 → T0.7②；明确 T0.1 的 `nvs` 是 P1 硬前置 |
| 2026-10-30 | v1.5 | 新增 T0.7b（`sys crash` + coredump 链路验证，0.2d）；T5.5 增加“关闭开发期调试开关”一项 |
| 2026-10-31 | v1.6 | 拆分 T1.2 为 T1.2a（事件定义 + 分发封装 + `evt` 观测，0.4d）/ T1.2b（业务接线，随 T2.5/T3.1）；P1 顺序改为 T1.5 → T1.1 → T1.2a → T1.3 → T1.4 → T1.2b → T1.6，理由是 net_mgr/transport_ws 同为 L1 必须先有事件通道 |
| 2026-10-31 | v1.7 | §4 风险表「Opus 库集成/性能不达标」标注**已排除**（T2.2 定稿定点 + VBR，CPU/内存均达标，不降级 PCM16）；任务清单与顺序不变 |
