# 项目状态（STATUS）

> **唯一进度来源**：会话开工先读本文件。
> 任务定义与工期见 [ROADMAP.md](ROADMAP.md)；设计见 [ARCHITECTURE.md](ARCHITECTURE.md)；规范见 [CONVENTIONS.md](CONVENTIONS.md)。
> 维护规则：完成任务立即更新本文件（§1 当前状态 + §2 看板 + §3 变更 + §5 基线），**不要改 AGENTS.md**。

## 1. 当前状态

| 项目 | 内容 |
|---|---|
| 当前阶段 | **P0 地基** |
| 当前任务 | T0.1 替换 `partitions.csv` |
| 下一步 | T0.2 建 15 个组件骨架 |
| 最后更新 | 2026-10-30 |
| 阻塞项 | 无 |

## 2. 任务看板

状态图例：☐ 未开始 / ◐ 进行中 / ☑ 已完成 / ✗ 阻塞

| 阶段 | 编号 | 任务 | 状态 |
|---|---|---|---|
| P0 | T0.1 | 替换分区表并烧录校验 | ☐ |
| P0 | T0.2 | 建 15 个组件骨架 | ☐ |
| P0 | T0.3 | board：引脚、LED、按键 | ☐ |
| P0 | T0.4 | audio_io：双 I2S + 环形缓冲 | ☐ |
| P0 | T0.5 | diag + esp_console 框架 | ☐ |
| P0 | T0.6 | POC 迁移，删除旧代码 | ☐ |
| P0 | T0.7 | P0 交付自检 + 基线记录 | ☐ |
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
| 2026-10-30 | — | 文档体系建立：ARCHITECTURE / ROADMAP / CONVENTIONS / HARDWARE / PRODUCT_IDEAS / STATUS |

## 4. 阻塞项

| 任务 | 阻塞原因 | 需要谁 | 处理计划 |
|---|---|---|---|
| — | 无 | — | — |

## 5. 基线记录

| 日期 | 阶段 | 固件 size | 内部 RAM 峰值 | PSRAM 峰值 | 备注 |
|---|---|---|---|---|---|
| 2026-10-30 | P0 之前（POC） | 891,984 B | 126,554 B（DIRAM 已用） | — | 迁移前基线 |
| — | P0 |  |  |  |  |
| — | P1 |  |  |  |  |
| — | P2 |  |  |  |  |
| — | P3 |  |  |  |  |
| — | P4 |  |  |  |  |
| — | P5 |  |  |  |  |
