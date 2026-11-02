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
| 代码怎么写才合规 | `docs/CONVENTIONS.md` | 全文（分层/音频/内存/提交规则） |
| 引脚 / 接线 / 电源 / 自检 | `docs/HARDWARE.md` | §2 引脚、§3 接线、§5 电源、§8 自检 |
| 新功能要不要做 / 已知技术问题怎么取舍 | `docs/PRODUCT_IDEAS.md` | §5 优先级矩阵、§6 决策记录、§8 技术调研记录 |
| 暂缓功能预留了什么 | `docs/ARCHITECTURE.md` | 附录 A（预留点）、附录 B（待定输入） |
| 有哪些待验证 / 待决定的事（含提出时间与关闭条件） | `docs/PENDING.md` | §2 待真机验证、§3 待决定、§4 已关闭归档、§5 时间线 |
| 协议字段细节 | `docs/PROTOCOL.md` | 完整契约（T2.5 定稿）：握手/帧头/上下行字段表/错误码表/版本兼容/样例；设计概览见 ARCHITECTURE §6 |
| 文档怎么维护 / 边界 | `docs/CONVENTIONS.md` | §1 文档边界 |

## 3. 构建、烧录与真机测试

- 直接执行：`idf.py build`、`idf.py -p <PORT> flash monitor`、`idf.py size`。
- **不要 `source export.sh`**：本机是 EIM 安装方式，PATH 已就绪，source 反而报错。
- 提交前必须：build 通过 + size 无明显增长。
- **唤醒别喊话**：测"嗨，乐鑫"用 `afplay tools/lexin.wav`（Mac 扬声器回放、设备麦克风拾音），不需要人工喊；`idf.py -p <PORT> monitor` 侧预期 `VS: wake detected`（旧固件 `APP:`）→ `AFE: feed paused` → `PLAYER: play ...` → `(VS|APP): prompt done, feed resumed`。**离线时（ws 未 READY）T3.6 起不播"我在呢"、直接播"网络未连接"提示音后回 IDLE**（`voice_test.py` 双分支兼容）。**音量按用户定稿：系统音量正常 31%、上限 50%（不得超过；70% 已否决）、`afplay` 默认增益**；`-v` 放大须满足 系统音量×增益 ≤50%（如 31% + `-v 1.5` ≈ 46.5% 可）。用法与注意事项见 `tools/README.md`。
- **交互语音也回放**：唤醒提示音结束（`feed resumed`）之后用 `afplay tools/今天天气如何.wav` / `afplay tools/今天吃了什么.wav` 模拟用户说话；**自动回归一条命令**：`uv run tools/voice_test.py --volume 50`（覆盖 3 个素材 + 串口断言，音量临时调到上限后自动恢复）。实测与参数见 `tools/README.md`。
- **端云链路自动回归（T2.5 起）**：`uv run tools/voice_cloud_test.py` 自己拉起 mock、复位设备、回放唤醒+交互语音，断言上行 Opus/下行 ASR-TTS 事件/`sid` 过滤/收尾与内存，43 项 PASS 才算过；`--inject-late` 额外验证迟到帧丢弃。参数见 `tools/README.md`。
- **异常反馈自动回归（T3.6 起）**：`uv run tools/error_prompt_test.py` 自己拉起 mock/复位设备/回放唤醒，覆盖四类异常（网络未连接 / 云端 error / THINKING 超时 / TTS 失败）+「播放中打断不误播 / 正常轮不误播 / SPEAKING 中云端报错先停 TTS 再播 / 10 轮稳定」，**289 项 PASS** 才算过（`--self-test` 离线 51 项、`--dry-run`、`--scenarios A-C` 可挑场景；设备侧 `fb status` / `fb test <net|cloud|timeout|tts>` 可人工试听）。参数见 `tools/README.md`。

## 4. 铁律（违反会返工，完整版见 CONVENTIONS）

1. 只允许上层依赖下层；同层走 `event_bus`；`main/` ≤150 行。
2. 音频任务内禁止 `malloc` / 网络 / Flash / 阻塞。
3. 大缓冲放 PSRAM，I2S DMA 放内部 RAM；内部 RAM（静态 DIRAM + 堆峰值）≤440KB 且 wss 握手期最低水位 ≥30KB（口径见 CONVENTIONS §4）。
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
- [ ] 因无板/环境等原因未做真机验证的项 → `docs/PENDING.md` 已登记（带时间与验收方法）
