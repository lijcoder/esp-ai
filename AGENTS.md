# AGENTS.md —— 会话入口

**项目**：ESP32-S3-N16R8 智能语音硬件 —— 本地唤醒（"嗨，乐鑫"）+ 云端语音对话 + MP3 音乐播放。ESP-IDF 5.4.4。

本文件是**稳定入口**：只做文档索引 + 全局红线，不写进度、不写方案。
**维护规则**：只在文档增删、会话工作流程或全局红线变化时修改；日常进度写 `docs/STATUS.md`，不要动本文件。

## 1. 开工顺序（每个会话按此执行）

1. 读 `docs/STATUS.md` → 当前阶段、当前任务、下一步、阻塞项。
2. 读 `docs/ROADMAP.md` **§2 对应阶段小节** → 任务定义与"参考"章节（先读再动手）。
3. 读 `docs/CONVENTIONS.md`（分层/音频/内存/提交规则，必读）。
4. 实现 → `idf.py build` + `idf.py size` 验证。
5. 收尾 → 更新 `docs/STATUS.md`（状态/变更/基线）；架构有变则同步 `ARCHITECTURE.md`。

> 不要凭记忆开发：架构、协议、分区、状态机以 `docs/ARCHITECTURE.md` 当前版本为准。

## 2. 文档地图（想知道什么 → 读哪份文件）

| 我想知道 | 读 | 关键位置 |
|---|---|---|
| 现在做到哪、下一步做什么 | `docs/STATUS.md` | §1 当前状态、§2 任务看板 |
| 任务定义 / 工期 / 验收标准 | `docs/ROADMAP.md` | §2 任务清单、§5 交付标准 |
| 某个功能怎么设计 | `docs/ARCHITECTURE.md` | §1 范围、§3 音频、§4 模块、§6 协议、§7 状态机、§9 分区 |
| 代码怎么写才合规 | `docs/CONVENTIONS.md` | 全文（约 90 行） |
| 引脚 / 接线 / 电源 / 自检 | `docs/HARDWARE.md` | §2 引脚、§3 接线、§5 电源、§8 自检 |
| 新功能要不要做 / 已知技术问题怎么取舍 | `docs/PRODUCT_IDEAS.md` | §5 优先级矩阵、§6 决策记录、§8 技术调研记录 |
| 暂缓功能预留了什么 | `docs/ARCHITECTURE.md` | 附录 A（预留点）、附录 B（待定输入） |
| 协议字段细节 | `docs/PROTOCOL.md` | 空壳，P2 补全；此前以 ARCHITECTURE §6 为准 |
| 文档怎么维护 / 边界 | `docs/CONVENTIONS.md` | §1 文档边界 |

## 3. 构建、烧录与真机测试

- 直接执行：`idf.py build`、`idf.py -p <PORT> flash monitor`、`idf.py size`。
- **不要 `source export.sh`**：本机是 EIM 安装方式，PATH 已就绪，source 反而报错。
- 提交前必须：build 通过 + size 无明显增长。
- **唤醒别喊话**：测"嗨，乐鑫"用 `afplay tools/lexin.wav`（Mac 扬声器回放、设备麦克风拾音），不需要人工喊；`idf.py -p <PORT> monitor` 侧预期 `APP: wake detected` → `AFE: feed paused` → `PLAYER: play ...` → `APP: prompt done, feed resumed`。用法与注意事项见 `tools/README.md`。

## 4. 铁律（违反会返工，完整版见 CONVENTIONS）

1. 只允许上层依赖下层；同层走 `event_bus`；`main/` ≤150 行。
2. 音频任务内禁止 `malloc` / 网络 / Flash / 阻塞。
3. 大缓冲放 PSRAM，I2S DMA 放内部 RAM；内部 RAM 峰值 ≤310KB 且保留 ≥30KB。
4. 采样率固定：MIC 16kHz / AMP 48kHz，运行中不切换。
5. 半双工 R1–R5（ARCHITECTURE §3.3）不得私自改。
6. 参数走 Kconfig，禁止硬编码 WiFi/服务器/引脚/阈值。
7. 下行事件只认当前 `sid`，迟到帧必须丢弃。
8. 改分层/协议/分区/状态机 → 必须同步 `ARCHITECTURE.md`。
9. 每个任务完成 → 必须回写 `docs/STATUS.md`（状态、变更、基线）。
10. 新功能先确认在 ARCHITECTURE 范围内，否则先写 `PRODUCT_IDEAS.md`。

## 5. 会话收尾清单

- [ ] `idf.py build` 通过，`idf.py size` 结果记入 `docs/STATUS.md` 基线表
- [ ] `docs/STATUS.md` 的当前状态与任务看板已更新
- [ ] 架构/协议有变 → `ARCHITECTURE.md`（及 `PROTOCOL.md`）已同步
- [ ] 新增不确定项 → `PRODUCT_IDEAS.md` 已记录
