# 项目状态（STATUS）

> **唯一进度来源**：会话开工先读本文件。
> 任务定义与工期见 [ROADMAP.md](ROADMAP.md)；设计见 [ARCHITECTURE.md](ARCHITECTURE.md)；规范见 [CONVENTIONS.md](CONVENTIONS.md)。
> 维护规则：完成任务立即更新本文件（§1 当前状态 + §2 看板 + §3 变更 + §5 基线），**不要改 AGENTS.md**。

## 1. 当前状态

| 项目 | 内容 |
|---|---|
| 当前阶段 | **P0 地基** |
| 当前任务 | T0.5 diag + esp_console 框架 |
| 下一步 | T0.7 第一轮真机自检（唤醒/提示音/RMS/底噪）→ T0.1 分区表 → T0.7 回归 |
| 最后更新 | 2026-10-30 |
| 阻塞项 | T0.7 烧录验证需接开发板（当前环境未检测到串口） |

## 2. 任务看板

状态图例：☐ 未开始 / ◐ 进行中 / ☑ 已完成 / ✗ 阻塞

| 阶段 | 编号 | 任务 | 状态 |
|---|---|---|---|
| P0 | T0.1 | 替换分区表并烧录校验 | ☐ |
| P0 | T0.2 | 组件骨架（5 个已建，其余随任务创建） | ☑ |
| P0 | T0.3 | board：引脚、按键 | ☑ |
| P0 | T0.4 | audio_io：双 I2S（16k 采集 / 48k 播放） | ☑ |
| P0 | T0.5 | diag + esp_console 框架 | ☐ |
| P0 | T0.6 | POC 迁移，删除旧代码 | ☑ |
| P0 | T0.7 | P0 交付自检 + 基线记录 | ◐ |
| P1 | T1.1 | config_store | ☐ |
| P1 | T1.2 | event_bus | ☐ |
| P1 | T1.3 | net_mgr（WiFi/重连/SNTP） | ☐ |
| P1 | T1.4 | transport_ws | ☐ |
| P1 | T1.5 | ws_mock_server.py | ☐ |
| P1 | T1.6 | P1 联调与基线 | ☐ |
| P2 | T2.1 | audio_fe 事件化 | ☐ |
| P2 | T2.2 | codec_enc（Opus） | ☐ |
| P2 | T2.3 | audio_player MP3 解码 | ☐ |
| P2 | T2.4 | audio_player 流式播放 | ☐ |
| P2 | T2.5 | voice_cloud 协议 | ☐ |
| P2 | T2.6 | 端到端自测 | ☐ |
| P3 | T3.1 | voice_session 状态机 | ☐ |
| P3 | T3.2 | 半双工 R1–R5 | ☐ |
| P3 | T3.3 | 混音/音量/限幅 | ☐ |
| P3 | T3.4 | dev_ctrl 按键/LED | ☐ |
| P3 | T3.5 | 提示音 + assets 打包 | ☐ |
| P3 | T3.6 | 错误提示 | ☐ |
| P4 | T4.1 | 源抽象（http/file） | ☐ |
| P4 | T4.2 | music_cloud | ☐ |
| P4 | T4.3 | music_session | ☐ |
| P4 | T4.4 | 音乐 ducking/唤醒 | ☐ |
| P4 | T4.5 | 播放稳定性 | ☐ |
| P5 | T5.1 | console 命令补全 | ☐ |
| P5 | T5.2 | 唤醒率/误唤醒实测 | ☐ |
| P5 | T5.3 | 24h 压测 | ☐ |
| P5 | T5.4 | 后端契约联调 | ☐ |
| P5 | T5.5 | 文档收尾 | ☐ |

## 3. 最近变更

| 日期 | 任务 | 说明 |
|---|---|---|
| 2026-10-30 | 文档 | 文档体系建立：ARCHITECTURE / ROADMAP / CONVENTIONS / HARDWARE / PRODUCT_IDEAS / STATUS |
| 2026-10-30 | T0.2–T0.4、T0.6 | 最小可用版本重构：5 个组件（board/audio_io/audio_fe/audio_player/assets）+ `idf.py build` 通过；旧 `main/esp-ai.c`、`main/voice_data.c` 删除 |
| 2026-10-30 | T0.2 | 每个组件补 `README.md`（作用/接口/依赖/配置/约束）；`idf_component.yml` 从 `main/` 移到 `components/audio_fe/`（依赖跟着使用方走） |
| 2026-10-30 | 文档 | 架构 §4.1 增加"层级"列并按 L0–L4 重排、补 L 定义；CONVENTIONS 组件模板补 README/清单规则；ROADMAP 任务措辞与实现对齐 |
| 2026-10-30 | P0 小结 | T0.2/T0.3/T0.4/T0.6 完成；T0.7 待真机烧录验证 |
| 2026-10-30 | 排期 | 按 ROADMAP v1.3 重排：T0.5 前置、T0.1 后移到真机验证后、T0.7 分两轮；剩余顺序 T0.5 → T0.7① → T0.1 → T0.7② |
| 2026-10-30 | 规范 | 日志 TAG 固定清单补 `APP`（`main/` 启动编排，不占组件层级）；CONVENTIONS §5 与 ARCHITECTURE §10.2 同步，供 T0.5 的 `log` 命令已知 TAG 表使用 |

## 4. 阻塞项

| 任务 | 阻塞原因 | 需要谁 | 处理计划 |
|---|---|---|---|
| — | 无 | — | — |

## 5. 基线记录

| 日期 | 阶段 | 固件 size | 内部 RAM 峰值 | PSRAM 峰值 | 备注 |
|---|---|---|---|---|---|
| 2026-10-30 | P0 之前（POC） | 891,984 B | 126,554 B（DIRAM 已用） | — | 迁移前基线 |
| 2026-10-30 | P0（MVP 重构） | 899,988 B | 126,682 B（DIRAM 已用，剩余 215,078 B） | — | build 通过；未烧录真机验证 |
| — | P1 |  |  |  |  |
| — | P2 |  |  |  |  |
| — | P3 |  |  |  |  |
| — | P4 |  |  |  |  |
| — | P5 |  |  |  |  |
