# 项目状态（STATUS）

> **唯一进度来源**：会话开工先读本文件。
> 任务定义与工期见 [ROADMAP.md](ROADMAP.md)；设计见 [ARCHITECTURE.md](ARCHITECTURE.md)；规范见 [CONVENTIONS.md](CONVENTIONS.md)。
> 维护规则：完成任务立即更新本文件（§1 当前状态 + §2 看板 + §3 变更 + §5 基线），**不要改 AGENTS.md**。

## 1. 当前状态

| 项目 | 内容 |
|---|---|
| 当前阶段 | **P0 地基（已完成）→ P1 联网（进行中）** |
| 当前任务 | P1 T1.5 完成：`tools/ws_mock_server.py` 落地（握手 / 心跳 / 事件下发 / 二进制帧 / interrupt / 重连 / 鉴权 / 场景开关），`--self-test` 9/9 通过，模拟设备端两轮语音 + 断线重连实测通过；契约缺口登记 PROTOCOL.md，`ws://`（本地）vs `wss://`（线上）证书策略登记 PRODUCT_IDEAS §8 X2 |
| 下一步 | P1：T1.1 `config_store`（nvs 分区已就绪）→ T1.2a 事件定义 → T1.3 `net_mgr` → T1.4 `transport_ws`（用 mock 联调）→ T1.6 联调与基线 |
| 最后更新 | 2026-10-30 |
| 阻塞项 | 无（开发板 `/dev/cu.usbmodem5CF90746951` 可烧录）；X1「提示符与日志互冲」仍按 PRODUCT_IDEAS §8 方案 A 接受，真机验收用"脚本读串口 + `log * warn` 降噪"替代口径 |

## 2. 任务看板

状态图例：☐ 未开始 / ◐ 进行中 / ☑ 已完成 / ✗ 阻塞

| 阶段 | 编号 | 任务 | 状态 |
|---|---|---|---|
| P0 | T0.1 | 替换分区表并烧录校验（9 分区 + coredump） | ☑ |
| P0 | T0.2 | 组件骨架（5 个已建，其余随任务创建） | ☑ |
| P0 | T0.3 | board：引脚、按键 | ☑ |
| P0 | T0.4 | audio_io：双 I2S（16k 采集 / 48k 播放） | ☑ |
| P0 | T0.5 | diag + esp_console 框架 | ☑ |
| P0 | T0.6 | POC 迁移，删除旧代码 | ☑ |
| P0 | T0.7 | P0 交付自检 + 基线记录（两轮均通过） | ☑ |
| P0 | T0.7a | diag 增 `mic stat`/`mic reset`（基线观测，T0.7 前置） | ☑ |
| P0 | T0.7b | diag 增 `sys crash`，验证 coredump 落盘/解析链路 | ☑ |
| P1 | T1.1 | config_store | ☐ |
| P1 | T1.2 | event_bus | ☐ |
| P1 | T1.3 | net_mgr（WiFi/重连/SNTP） | ☐ |
| P1 | T1.4 | transport_ws | ☐ |
| P1 | T1.5 | ws_mock_server.py | ☑ |
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
| 2026-10-30 | T0.5 | 新增 `components/diag`：启动信息横幅 + UART0 `esp_console` REPL（`esp-ai>`）+ `sys info/heap/tasks/reboot`、`log`（15 个固定 TAG，未知 TAG/非法级别报错）+ 可选堆监控任务；`main` 首位调用 `diag_init()`；`sdkconfig.defaults.esp32s3` 开 `FREERTOS_USE_TRACE_FACILITY`/`GENERATE_RUN_TIME_STATS`（`sys tasks` 需要）；`idf.py build` 通过，交互验收并入 T0.7① |
| 2026-10-30 | 文档 | 真机验收发现 console 提示符被日志冲掉，登记为已知问题 X1：PRODUCT_IDEAS §8 技术调研记录（现象/根因/6 个候选方案对比/重评触发条件），§6 记录"暂不优化"决策；AGENTS 文档地图补 §8 |
| 2026-10-30 | **fix(audio_io)** | **修复"唤醒'嗨，乐鑫'无反应"**：根因是 `i2s_channel_read/write` 的超时参数单位是**毫秒**，`audio_io` 误传 `pdMS_TO_TICKS(100)`；本工程 `CONFIG_FREERTOS_HZ=100` 使其等于 10ms，而读满 512 帧 @16k 需 32ms → 首次 DMA 预填耗尽后每次读都 `ESP_ERR_TIMEOUT` → `audio_fe` 收到 -1 后喂全零 → AFE 输入恒为静音（-69.2dBFS）→ WakeNet 永不触发。改为直接传毫秒；超时但已读到部分数据时返回实际帧数。真机复测：3/3 唤醒成功、"我在呢"提示音正常 |
| 2026-10-30 | **fix(audio_fe)** | 同链路健壮性：读失败返回负值时原代码用 `buf + got * sizeof(int16_t)` 做 memset，`got=-1` 会从 `buf-2` 起越界写堆；改为读失败按 0 帧处理并整帧补零。**不加临时打点**，麦克风电平/底噪观测能力按 CONVENTIONS §8 后续以 console 命令（如 `mic stat`）补齐 |
| 2026-10-30 | T0.7 | 第一轮真机自检（部分完成）：安静室内底噪麦克风 RMS≈40~270、AFE 输入约 -45~-53 dBFS；正常说话 RMS 数百~4k；"嗨，乐鑫" 3/3 唤醒；提示音"我在呢"声音正常；唤醒后 feed 暂停/恢复（R1/R2）日志符合预期。以上电平数据由临时打点测得（打点已移除），待 console 命令补齐后复测入基线；console 交互验收仍受 X1 影响 |
| 2026-10-30 | T0.7a | 新增 `mic stat`/`mic reset`：`audio_fe` 暴露只读统计（sum_sq/peak/sample_count/帧数/读失败/唤醒数/VAD/feed 状态），feed 任务内只做整数累加、临界区只合并，`sqrt/log10` 放 console 侧；顺手修掉"暂停收音补零只清 1 通道"的隐患（缓冲是 chunksize×nch）；`diag` 以 `PRIV_REQUIRES audio_fe` 私有依赖；`idf.py build` 通过且零警告，size 956,180 B（+3,436 B，主要来自 newlib math），DIRAM 131,358 B（+40 B）；真机验收随 T0.7①C 进行 |
| 2026-10-30 | T0.7①C | 真机复测（`mic stat`）：**底噪** 安静段 RMS 104~220（-49.0~-43.5 dBFS）、峰值 502（-36.3 dBFS），与历史 40~270 / -45~-53 dBFS 吻合；**说话** 峰值 4228（-17.7 dBFS）；**唤醒 3/3**（21.7s / 24.4s / 27.1s），R1 播放期 feed 暂停、R2 恢复均可见；console 验收通过（`log` 副作用生效、`mic stat/reset`、`sys info/heap/tasks`、`sys reboot` 见重启横幅）。首次采样（开机窗口）环境在放音乐/视频，RMS 2686~4149、峰值 30208（-0.7 dBFS），**不作为底噪基线**。采样期另出现第 4 次唤醒（用户说话/环境声期间），安静环境需复核是否误唤醒（并入 T5.2） |
| 2026-10-30 | **fix(diag)** | 真机发现 `sys tasks` 恒为"共 0 个任务"：本内核版本的 `uxTaskGetSystemState` **不支持**"传 `NULL` 查数量"，容量不足时直接返回 0；改为用 `uxTaskGetNumberOfTasks()+8` 定容量并加失败提示。修复后读到 8 个任务（afe_feed / afe_fetch / player / console_repl / IDLE0 / IDLE1 / ipc0 / ipc1），`afe_fetch` 栈剩 2060/4096 B、`afe_feed` 5932/8192 B；size 956,272 B（+92 B） |
| 2026-10-30 | T0.7b | 新增 `sys crash`（仅 `CONFIG_DIAG_ENABLE_CRASH_TEST=y` 编译，默认 n，量产必须关闭）：真机验证 coredump 闭环 —— panic reason `diag crash test` → `Core dump has been saved to flash.` → 重启后 `Core dump data checksum is correct` / `Found core dump 14404 bytes in flash @ 0x9a0000`（占 64K 分区 22%，余量充足）；`idf.py coredump-info` 还原崩溃任务 `console_repl` 与全部 8 个任务调用栈（`cmd_sys` → `diag.c:297`、`feed_task` → `audio_fe.c:89`、`player_task` → `audio_player.c:48` 等，带源文件行号）；验证后已清空 coredump 分区；size 974,328 B（+220 B） |
| 2026-10-30 | T0.1 | 分区表切换到 ARCH §9.1 定稿（nvs/otadata/phy_init/ota_0/ota_1/model/assets/coredump/storage；app 偏移 0x10000→0x20000，model 5168K→1536K）；`gen_esp32part.py` 校验无重叠越界；`erase-flash` 全片擦除后重烧四段；bootloader 识别 9 个分区、app 从 0x20000 启动、`esp_core_dump_flash` 找到 coredump 分区、srmodels 从 1536K 分区加载成功；sdkconfig 由 defaults 重新生成（仅 coredump 相关项变化，其余逐项核对保留）；启用 `CONFIG_ESP_COREDUMP_ENABLE_TO_FLASH=y`；size 974,108 B（+17.8KB，coredump 代码 + ISR 栈 +560B/核），DIRAM 137,578 B（+6.2KB） |
| 2026-10-30 | T0.7② | 分区表改后回归：唤醒 3/3（10.9s/16.3s/29.9s）、R1 `feed paused`→播放 12800 样本→R2 `feed resumed`（间隔 1.35s，符合 300ms 延时）、底噪 RMS 167~291（-45.9~-41.0 dBFS）、说话峰值 4148~4189（-18.0 dBFS）、`sys tasks` 8 个任务、运行期内部 RAM 峰值 127,468 B / PSRAM 峰值 692,444 B、无 ERROR 日志。附带观察：两轮采样都在 3 次唤醒后出现第 4 次唤醒（用户说话/环境声期间），需在安静环境复核是否误唤醒（并入 T5.2） |
| 2026-10-30 | T1.5 | 新增 `tools/ws_mock_server.py`（单文件，PEP 723 依赖 `websockets>=14`，`uv run` 即用）：按 ARCH §6.1–6.3 实现 hello / ping-pong / audio_start-end / asr-llm-tts 下发 / interrupt / music 骨架，8 字节二进制帧收发与严格校验，`--self-test` 9 项，`--no-tts`/`--drop-pong`/`--close-after`/`--save-upload`/`--scenario normal\|error\|music`；补 `tools/README.md`。**零固件改动**（size 保持 974,108 B）。实测：自测 9/9 PASS；模拟设备客户端 ping rtt 0.3ms、100 帧 TTS 流 seq 连续（末帧相对首帧 2.2s）、第 2 轮 interrupt 后不再有 `tts_end`；no-tts/error/music/close-after/upload 五个 CLI 分支均验证通过 |
| 2026-10-30 | 文档 | T1.5 暴露的契约缺口登记到 `PROTOCOL.md`「实现中发现的契约待确认项」7 条（hello 无应答、二进制帧无 sid、音乐 sid 归属、error 码表、鉴权关闭码、pong 格式、本地 ws 与线上 wss 的证书策略），P2/T2.5 定稿时逐条确认；证书策略同时登记 `PRODUCT_IDEAS.md` §8 X2 |

## 4. 阻塞项

| 任务 | 阻塞原因 | 需要谁 | 处理计划 |
|---|---|---|---|
| — | 无 | — | — |

## 5. 基线记录

| 日期 | 阶段 | 固件 size | 内部 RAM 峰值 | PSRAM 峰值 | 备注 |
|---|---|---|---|---|---|
| 2026-10-30 | P0 之前（POC） | 891,984 B | 126,554 B（DIRAM 已用） | — | 迁移前基线 |
| 2026-10-30 | P0（MVP 重构） | 899,988 B | 126,682 B（DIRAM 已用，剩余 215,078 B） | — | build 通过；未烧录真机验证 |
| 2026-10-30 | P0（T0.5 diag） | 952,764 B | 131,318 B（DIRAM 已用，剩余 210,442 B） | — | build 通过；+52.8KB 主要来自 console/linenoise（12.4KB）+ UART 驱动（17.2KB）+ vfs/printf；+4.6KB 为 REPL 任务栈；运行期堆水位待烧录后用 `sys heap` 采 |
| 2026-10-30 | P0（T0.7 唤醒修复后） | 952,744 B | 131,318 B（DIRAM 已用，剩余 210,442 B） | —（开机横幅 PSRAM 剩余 8,386,148 B，峰值未采） | 真机验证：3/3 唤醒 + 提示音正常；对比 T0.5 基线 -20B（净变化 = 超时单位修复 + 读失败健壮性 - 临时诊断打点）。实测底噪：麦克风 RMS≈40~270、AFE 输入 -45~-53 dBFS（临时打点测得，打点已移除） |
| 2026-10-30 | P0（T0.7a mic stat） | 956,180 B | 131,358 B（DIRAM 已用，剩余 210,402 B） | —（未采） | `idf.py build` 通过、零警告；+3,436 B 主要来自 newlib math（`sqrt`/`log10`）+ 命令文本 + 统计代码，DIRAM +40 B（统计变量 + 临界区）；真机未验证，基线数据待 T0.7①C 用 `mic stat` 复测 |
| 2026-10-30 | P0（T0.7①C 真机基线） | 956,272 B | 运行期内部 RAM 峰值占用 126,924 B（`sys heap` 口径 total−min_free，最低水位 198,943 B，剩余 203,479 B） | PSRAM 峰值占用 692,444 B（剩余 7,693,300 B，最大块 7,602,176 B） | 唤醒 3/3；底噪 RMS 104~220（-49.0~-43.5 dBFS）/峰值 502；说话峰值 4228（-17.7 dBFS）；`sys tasks` 修复后 8 个任务；固件含 T0.7a + diag 修复 |
| 2026-10-30 | P0（T0.1 分区表 + coredump） | 974,108 B | 运行期内部 RAM 峰值占用 127,468 B（`sys heap` 口径 total−min_free，最低水位 192,979 B，剩余 197,543 B） | PSRAM 峰值占用 692,444 B（剩余 7,693,300 B） | 真机：9 分区生效、app@0x20000、model@0x620000（1536K）、coredump 可用；唤醒 3/3 + R1/R2 正常；+17.8KB 来自 coredump |
| 2026-10-30 | P1（T1.5 mock 工具） | 974,108 B（未变） | —（本任务无固件改动，沿用 P0/T0.1 行） | —（同上） | 仅新增 `tools/ws_mock_server.py` + `tools/README.md` + `.gitignore` 一行；`idf.py build`/`size` 未重跑（无固件差异），P1 固件基线等 T1.1 落地后重采 |
| — | P2 |  |  |  |  |
| — | P3 |  |  |  |  |
| — | P4 |  |  |  |  |
| — | P5 |  |  |  |  |
