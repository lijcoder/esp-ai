# 项目状态（STATUS）

> **唯一进度来源**：会话开工先读本文件。
> 任务定义与工期见 [ROADMAP.md](ROADMAP.md)；设计见 [ARCHITECTURE.md](ARCHITECTURE.md)；规范见 [CONVENTIONS.md](CONVENTIONS.md)。
> 维护规则：完成任务立即更新本文件（§1 当前状态 + §2 看板 + §3 变更 + §5 基线），**不要改 AGENTS.md**。

## 1. 当前状态

| 项目 | 内容 |
|---|---|
| 当前阶段 | **P0 地基（已完成）→ P1 联网（进行中）** |
| 当前任务 | P1 T1.4 完成：新增 `components/transport_ws`（L1，ws/wss 客户端：连接 + 退避重连 1/2/5/10/30s + `hello`/`hello_ack` 握手 + 20s ping / 10s pong 心跳 + 8B 二进制帧 + 8KB PSRAM 非阻塞发送队列；自建 `ws` 任务核 0/优先级 5/6KB，TLS 与收包在 `esp_websocket_client` 的 `ws_client` 库任务）+ `diag` 的 `ws status`/`ws reconnect` + `main` 在 `net_init` 后 `ws_init()`/`ws_start()`；`tools/ws_mock_server.py` 补 `hello_ack`（自测 9/9 PASS）。真机 9 项全部 PASS：上电 3.70s 联网→3.65s 握手完成；20s 心跳 RTT 94~234ms；服务端关闭后 1.43s 重建（<5s）；不可达时退避严格 1/2/5/10/30s 且恢复后即连；`wifi reconnect`→`NET_DOWN` 停 WS、`NET_UP` 后 130ms 恢复；token 错误（关闭码 4002）ERROR 后不再自动重试；坏 magic/ver/len/type 与 3021B 超长消息全部丢弃且连接不崩；`--drop-pong` 10s 判离线并重连；公网 `wss://` TLS 握手成功。固件 1,721,744 B（+197.5KB：TLS 握手 + 证书 bundle 为主），内部 RAM 峰值 209,252 B（ws://）/ 236,544 B（wss 握手期，最低水位 27,655 B 略低于 30KB 保留线，优化按 X3 排 P2 前），PSRAM 峰值 702,648 B，任务数 16 |
| 下一步 | P1：T1.6 联调与基线收口（联网/心跳/断网恢复/mock 收发 + 记录内存增量，产出 P1 验收报告）；T1.2b（唤醒/VAD/播放改走事件）随 P2 的 T2.5/T3.1 落地 |
| 最后更新 | 2026-10-31 |
| 阻塞项 | 无（开发板 `/dev/cu.usbmodem5CF90746951` 可烧录）；X1「提示符与日志互冲」仍按 PRODUCT_IDEAS §8 方案 A 接受；**内部 RAM 余量**：wss 握手期最低水位 27,655 B（低于 CONVENTIONS §4 的保留 30KB 线），按 X3 排在 P2 前统一优化；X2 已定稿（本地 ws:// + 线上 wss 双传输） |

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
| P1 | T1.1 | config_store | ☑ |
| P1 | T1.2 | event_bus（a 事件定义/分发封装；b 业务接线） | ◐ |
| P1 | T1.3 | net_mgr（WiFi/重连/SNTP） | ☑ |
| P1 | T1.4 | transport_ws | ☑ |
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
| 2026-10-30 | T1.1 | 新增 `components/config_store`（L1，只依赖 `nvs_flash`）：独占 `nvs_flash_init`（含 NO_FREE_PAGES/NEW_VERSION 擦除重试）、命名空间 `cfg`、键 `volume`（i32）、通用 int/str 读写 + `cfg_get_volume/cfg_set_volume`（写前 clamp、读侧兜底 clamp + WARN）+ `cfg_erase_key`/`cfg_get_usage`；`main/Kconfig.projbuild` 定义 `CONFIG_AI_WIFI_SSID/PASSWORD/WS_URL/DEVICE_ID/TOKEN`（默认空，真实值只放本地 `sdkconfig`）；`app_main` 在 `diag_init` 后 `cfg_init`、`audio_player_init` 后按 NVS 音量设置播放器；`diag` 增 `cfg info/get/set/setstr/erase`（`PRIV_REQUIRES config_store nvs_flash`）；`ARCHITECTURE §4.1` 接口列同步。真机验收 20/20 PASS：`cfg set volume 42` → 重启 `CFG: 音量=42（来自 NVS）` + `PLAYER: volume=42` + `APP: volume restored: 42`；`cfg set volume 150` 落盘原值、`cfg info` 读侧 clamp 100 + `W (..) CFG: 音量越界 150`；`cfg erase volume` + 重启回缺省 60；`cfg setstr/get dev_name` 正常；`cfg set volume abc` 拒绝；`cfg get` 保留"读原始值"的底层语义。`idf.py build` 零警告；固件 995,372 B（+21.3KB，NVS 引擎/分区访问为主）、DIRAM 137,610 B（+32 B）、运行期内部 RAM 峰值 128,500 B（+1,032 B）、PSRAM 峰值 692,476 B（+32 B）、任务数仍 8 |
| 2026-10-31 | T1.2a | 新增 `components/event_bus`（L1，只依赖 `esp_event`）：`APP_EVT` 基 + 13 个事件（与 ARCH §4.2 逐字一致，`APP_EVT_ANY` 用于订阅全部）+ 定长 32B `app_evt_data_t`（P1 只有 `NET_UP`/`NET_DOWN` 带 payload，其余投 `NULL`；文本类 payload 所有权留给 T2.5/T3.1）+ `app_evt_init/register/unregister/post/name`（8 个处理器槽位、post 非阻塞、队列满 WARN + `ESP_ERR_TIMEOUT`）；自建独立 esp_event 循环（任务 `evt` 核 0/优先级 5/栈 4KB，队列 `CONFIG_EVENT_BUS_QUEUE_SIZE` 默认 16），不占 default loop 以免与 WiFi/esp_netif 互相阻塞；**音频任务只许投无 payload 事件**（esp_event 带 payload 会内部 `calloc`）写入头文件与 README；`diag` 新增 `evt list/post/burst/trace`（`PRIV_REQUIRES event_bus`），日志 TAG 增 `EVT`（16 个）；`main/app_main.c` 在 `cfg_init` 后加一行 `app_evt_init()`，业务接线不动（T1.2b）。真机 10/10 PASS：`evt list` 13 个事件；trace 下 `NET_UP ip=10.0.0.42 rssi=-42`、`NET_DOWN reason=2 retry=1000 ms` 在 `evt` 任务上下文打印；`WAKE_DETECTED` 无 payload 正常；非法事件名拒绝（`ESP_ERR_INVALID_ARG`）；`burst 64` 成功 48/丢弃 16（queue=16，不阻塞、WARN 可见）；trace off 后 `evt post BUTTON` 不再打印；唤醒 4/4 + R1/R2 正常（`feed paused`→`play 12800 samples`→`feed resumed`）；`log` 列出 16 个 TAG（含 `EVT`）且 `log EVT debug` 可设；`sys tasks` 9 个任务（`evt` 栈剩余 1,660/4,096 B）；无 ERROR。`idf.py build` 零警告；固件 1,003,936 B（+8,564 B：`esp_event` 3.3KB + `event_bus` 1.0KB + `evt` 命令/文本），DIRAM 138,070 B（+460 B），运行期内部 RAM 峰值 133,460 B（+4,960 B，`evt` 任务栈 4KB 为主），PSRAM 峰值 693,200 B（+724 B）。附注：`app_evt_post()` 的投递打点是 DEBUG 级，本工程 `CONFIG_LOG_MAXIMUM_LEVEL=INFO` 时编译期被裁掉，运行期观测用 `evt trace on` |
| 2026-10-31 | T1.3 | 新增 `components/net_mgr`（L1）：`net_init/is_up/wait_ready/is_time_synced/get_info/reconnect`；WiFi STA（凭证只来自 Kconfig，驱动用 `WIFI_STORAGE_RAM` 防 NVS 陈旧值；`WIFI_PS_NONE` 降时延），`WIFI_EVENT/IP_EVENT` 回调 + 一次性 `esp_timer` 做退避（**无独立任务**），退避序列按 ARCH §6.1 定稿 1/2/5/10/30s 无限重试；GOT_IP 投 `NET_UP`（ip + rssi）、**已联网后**掉线投 `NET_DOWN`（reason + retry_ms），上电首次连不上不投；SNTP 关闭 lwIP 随机启动延迟（`CONFIG_LWIP_SNTP_STARTUP_DELAY=n`）并在 GOT_IP 时重启 SNTP；未配置 SSID 时只 WARN 降级离线，本地功能不受影响。`diag` 新增 `wifi status`/`wifi reconnect`（T5.1 的这两个命令提前交付，`net ping` 仍留 T5.1）；`main/app_main.c` 在 `audio_fe_start` 之后 `net_init()`。发现并修复：WiFi 事件在 `sys_evt` 任务（默认栈 2304 B）里回调，实测只剩 380 B，`CONFIG_ESP_SYSTEM_EVENT_TASK_STACK_SIZE` 加到 4096 B（写入 defaults）。真机 12/12 PASS：上电 2.28s 起 STA / 2.47s 关联 / 5.61s IP / 5.65s SNTP 对时；`wifi reconnect` 全程 4.20s（<5s）；错误 SSID 测试退避 1000/2000/5000/10000/30000 ms、reason=201、`NET_DOWN` 不投、无崩溃，恢复 SSID 后自动联网；唤醒 2/2 + R1/R2 正常。`idf.py build` 零警告；固件 1,524,256 B（+520.3KB：esp_wifi/wpa_supplicant/mbedtls/lwip/esp_netif），DIRAM 189,834 B（+51,764 B），运行期内部 RAM 峰值 192,572 B（+59,112 B，剩余 79,459 B，最大空闲块 31,744 B），PSRAM 峰值 693,540 B，任务数 14 |

| 2026-10-31 | T1.4 | 新增 `components/transport_ws`（L1）：`ws_init/start/stop/register_cb/send_text/send_bin/reconnect/is_ready/status_read`；连接与退避（1/2/5/10/30s）、`hello`→`hello_ack`、20s ping / 10s pong 超时、8B 帧头（magic/ver/type/seq/len）收发与校验、下行分片重组（2KB 内部缓冲）、8KB PSRAM 非阻塞发送队列（满即丢 + 限频 WARN）、鉴权关闭码 4001/4002 不自动重试、wss 建连前等 SNTP 对时、日志不回显 query；自建 `ws` 任务（核 0/优先级 5/6KB），`espressif/esp_websocket_client 1.8.0`（Apache-2.0）在其 `ws_client` 任务收包（栈 6KB，TLS 后余 3.4KB）。`diag` 增 `ws status`/`ws reconnect`；`main` 在 `net_init` 后 `ws_init`/`ws_start`（`main/` 106 行）。**真机修 3 个问题**：命令队列 1B/4B 类型不匹配导致 `ws_start` 无效、200ms tick 与 `NET_UP` 重复建连、ERROR 事件读未初始化 `esp_tls_*` 字段打出垃圾值。`idf.py build` 零警告；固件 1,721,744 B（+197,488 B：TLS 握手 + CRT bundle + ws 代码）、DIRAM 192,986 B（+3,152 B）。真机：上电 3.70s 联网→3.65s 握手完成、20s 心跳 RTT 94~234ms、服务端关闭 1.43s 重建、退避 1/2/5/10/30s、`wifi reconnect`→NET_DOWN 停 WS 且 NET_UP 后 130ms 恢复、4002 停止重试、坏帧 5/5 丢弃、hello_ack 5s 超时、`--drop-pong` 10s 心跳超时、公网 `wss://` TLS 握手成功；内部 RAM 峰值 209,252 B（ws://）/236,544 B（wss，最低水位 27,655 B）、PSRAM 峰值 702,648 B、任务数 16（+`ws`、`ws_client`） |
| 2026-10-31 | 工具/文档 | 新增 `tools/lexin.wav`（"嗨，乐鑫"录音，24 kHz/16bit/mono/1.28 s）：用 Mac 扬声器 `afplay` 回放给设备麦克风，**替代人工喊话**测唤醒；真机 7/7 唤醒（系统音量 70 + `afplay -v 1.5` 3/3、默认音量 31 + 默认 `afplay` 4/4），串口链路 `APP: wake detected` → `AFE: feed paused` → `PLAYER: play 12800 samples` → `APP: prompt done, feed resumed` 完整；规则写入 `AGENTS.md §3`、`CONVENTIONS.md §8`、`tools/README.md`。**零固件改动**（build/size 不变） |

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
| 2026-10-30 | P1（T1.1 config_store） | 995,372 B（+21,264 B，NVS 引擎 + 分区访问为主） | 运行期内部 RAM 峰值占用 128,500 B（`sys heap` 口径 total−min_free，最低水位 191,851 B，剩余 196,267 B） | PSRAM 峰值占用 692,476 B（剩余 7,693,264 B，最大块 7,602,176 B） | 真机 20/20 PASS（持久化/缺省/clamp/erase/字符串/非法参数）；`idf.py build` 零警告；DIRAM 137,610 B（+32 B）、任务数 8 不变；NVS 分区 24K（空闲 755 条目，命名空间自身占 1 条目） |
| 2026-10-31 | P1（T1.2a event_bus） | 1,003,936 B（+8,564 B：`esp_event` 3.3KB + `event_bus` 1.0KB + `evt` 命令/文本） | 运行期内部 RAM 峰值占用 133,460 B（`sys heap` 口径 total−min_free，最低水位 186,387 B，剩余 190,551 B） | PSRAM 峰值占用 693,200 B（剩余 7,693,228 B，最大块 7,602,176 B） | 真机 10/10 PASS；任务数 8→9（新增 `evt`，核 0/优先级 5/栈 4KB，剩余 1,660 B）；唤醒 4/4 + R1/R2 正常；`idf.py build` 零警告；DIRAM 138,070 B（+460 B） |
| 2026-10-31 | P1（T1.3 net_mgr） | 1,524,256 B（+520,320 B：`esp_wifi`/`wpa_supplicant`/`mbedtls`/`lwip`/`esp_netif` + `net_mgr`） | 运行期内部 RAM 峰值占用 192,572 B（`sys heap` 口径 total−min_free，总量 267,327 B，最低水位 74,755 B，剩余 79,459 B，最大空闲块 31,744 B） | PSRAM 峰值占用 693,540 B（剩余 7,693,064 B，最大块 7,602,176 B） | 真机 12/12 PASS；上电 2.28s 起 STA / 5.61s IP / 5.65s SNTP；`wifi reconnect` 4.20s；任务数 9→14（+`wifi`/`sys_evt`/`esp_timer`/`Tmr Svc`/`tiT`）；`sys_evt` 栈剩余 2,172/4,096 B（原 2304 B 只剩 380 B，已加栈）；零警告；**T1.4 TLS 需关注最大空闲块 31.7KB** |
| 2026-10-31 | P1（T1.4 transport_ws） | 1,721,744 B（+197,488 B：TLS 握手 + 证书 bundle + ws_client/transport_ws 代码；X3 方案 A/B 只动内存配置、不改镜像大小） | 运行期内部 RAM 峰值占用 **209,252 B**（`ws://` 连接稳态）/ **236,544 B**（`wss` TLS 握手期；剩余 76,351 B，最大块 28,672 B，**最低水位 27,655 B**） | PSRAM 峰值占用 702,648 B（剩余 7,684,832 B，最大块 7,602,176 B） | 真机 9 项 PASS（握手/心跳/重连/退避/断网恢复/鉴权失败/坏帧/心跳超时/wss TLS）；固件 1,721,744 B、DIRAM 192,986 B（+3,152 B）、任务数 16（+`ws` 6KB、`ws_client` 6KB 库任务）；`ws` 栈余 3,576 B、`ws_client` 栈余 3,432 B（TLS 后）；`idf.py build` 零警告。**风险**：wss 握手期内部 RAM 最低水位 27,655 B，低于 CONVENTIONS §4 的保留 30KB 线，按 X3 排在 P2 前统一优化 |
| — | P2 |  |  |  |  |
| — | P3 |  |  |  |  |
| — | P4 |  |  |  |  |
| — | P5 |  |  |  |  |
