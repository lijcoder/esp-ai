# 待验证 / 待决定问题登记（PENDING）

> **本文件的唯一边界**：登记「**已知问题、待真机验证项、被暂缓的决定**」——每条必须带**提出时间**、来源、验收方法与关闭条件。
> 进度与实测基线归 [STATUS.md](STATUS.md)，设计归 [ARCHITECTURE.md](ARCHITECTURE.md)，要不要做归 [PRODUCT_IDEAS.md](PRODUCT_IDEAS.md)，排期归 [ROADMAP.md](ROADMAP.md)。
> **不要在这里写进度**：条目关闭时，结论写回 STATUS §5 基线（或对应设计文档），然后在本文件 §4 归档一行。

## 1. 登记与关闭规则

1. **谁都可以加**：任何"现在不做/做不了、但以后必须处理"的事都进来，尤其是**因开发板不在手边而无法真机验证**的项。
2. 每条必须写清 5 项：**提出时间**（年月日 + 时刻，Asia/Shanghai）、**来源**（谁在什么时候提出的）、**现象/问题**、**验收方法**（回板后怎么证明它好了）、**关闭条件**。
3. 编号只追加不改序：`PV-###` = 待真机验证（Pending Verification），`PD-###` = 待决定（Pending Decision）。
4. 关闭时在 §4 归档，写明关闭时间、结论、证据（测试项数/实测值/文档位置），**不删除历史**。
5. 与本文件冲突时以 STATUS §4「阻塞项」为准（那是影响当前推进的硬阻塞；本文件是排队中的待办）。

> **时间口径**：标注「采样」的时间来自会话运行时上下文（精确到分）；标注「约」的是相邻采样点之间的估算（误差分钟级）。

---

## 2. 待真机验证（PV）

### PV-001 · T3.4 LED 逐色目视 + 物理按键按压
| 项 | 内容 |
|---|---|
| 提出时间 | **2026-11-02 11:01（采样）** 建本文件时登记；"板子不在现场"的确认见 10:27 |
| 来源 | T3.4 收尾（2026-11-01 22:42 开工，2026-11-01 夜用户目视反馈） |
| 现象/问题 | 自动化脚本只能读到「LED 模式名 + 帧计数」，**证明不了 WS2812 真的点对了颜色**；物理 BOOT 键的真实按压（触感/机械抖动）也只有脚本用 DTR 模拟过，未人手按过 |
| 已获得的部分证据 | 2026-11-01 夜用户目视到板载灯**白色常亮**（= 静音 / `dev led mute` 模式）。白色需 R/G/B 三通道全开且 24bit 全对 → **bit-bang 时序（T0H/T1H/复位）、GPIO48 引脚、亮度渲染均已证实正确**（时序错只会剩绿色或乱色） |
| 待验证 | ① `dev led idle` 绿 / `listening` 青 / `thinking` 黄闪 2Hz / `speaking` 蓝 / `followup` 绿闪 2Hz / `error` 红闪 4Hz / `mute` 白，逐个目视；② `dev led auto` 后走一轮对话，确认颜色随状态机自动变化；③ **手按 BOOT**：短按出现 `DEV: 按键：短按`，长按（≥600ms）切静音且松开不误报短按 |
| 验收方法 | 回板后 `uv run tools/dev_ctrl_test.py`（53 项，走真实 GPIO 路径）+ 人工目视 7 种颜色 + 手按 2 次 |
| 关闭条件 | 脚本 53/53 + 7 色目视 + 手按短/长按各 1 次均符合预期 |
| 关联 | STATUS §1 阻塞项㉒a、§5 基线 T3.4 行；`components/dev_ctrl/README.md` LED 映射表 |

### PV-004 · T3.4 物理 BOOT 键真实按压（LED 已移除，只留按键）
| 项 | 内容 |
|---|---|
| 提出时间 | **2026-11-02 21:4x（采样）**（PV-001 拆分而来） |
| 来源 | LED 功能移除后，T3.4 剩下的物理交互只有按键 |
| 现象/问题 | 按键路径已用 DTR→GPIO0 模拟验证（走真实 GPIO 扫描），但**未人手按过**：机械抖动特性、手感、以及"长按 600ms 不误报短按"在真实按压下是否一致 |
| 验收方法 | 回板后：手按 BOOT 短按 3 次（应各出现 `DEV: 按键：短按` 并起轮/打断）、长按 2 次（应切换静音、松开不追加短按）；对照 `dev status` 计数 |
| 关闭条件 | 手按短/长按各 ≥2 次符合预期，且计数与日志一致 |
| 关联 | STATUS §1 阻塞项㉔、`tools/dev_ctrl_test.py` 场景 A/B |


---|---|
| 提出时间 | **2026-11-02 22:5x（采样）**，T3.6 真机功能验收通过后登记 |
| 来源 | T3.6 交付；提示音由 `tools/prompt_gen.py`（macOS `say -v Tingting`，语速 -r 200）生成，**机器只能验证时长/峰值/门控，音色/语速/响度是否舒服必须人耳听** |
| 现象/问题 | 四类提示音（网络未连接 / 服务出了点问题 / 响应超时 / 语音播放失败）在设备扬声器上的清晰度、语速、与「我在呢」/TTS 的音量平衡未经用户确认；`say` 合成音色偏播报感 |
| 现成证据 | 峰值统一 -7.82 dBFS（与「我在呢」等响）、时长 1075/1468/1083/1236 ms；设备侧播放链路正常（门控 hold_mask 0x01→0x00、`PLAYER: play`、`FB: 提示音完成`）；试听 WAV 在 `build/t36_prompts/`（Mac 可直接听） |
| 验收方法 | ① 设备上 `fb test net|cloud|timeout|tts` 逐条听；② 真实触发四类异常各一次；③ 若语速/音色不满意，改 `tools/prompt_gen.py` 文案/语速（或 T3.5 正式制作资源）后重生成并重烧 |
| 关闭条件 | 用户确认四条提示音清楚可区分、不刺耳、音量与 TTS 平衡 |
| 关联 | `components/assets/README.md`、`tools/prompt_gen.py`、STATUS §5 T3.6 行 |

### PV-002 · wss 握手期内部 RAM 水位未复测
| 项 | 内容 |
|---|---|
| 提出时间 | **2026-11-02 11:01（采样）** 建本文件时登记 |
| 来源 | T3.4 基线记录（2026-11-01 23:04 采样）；T3.7 实测 wss 握手期 33,271 B，T3.4 新增静态 +276 B + 两处 esp_timer；T3.6 又加了 `feedback` 静态与一个看门狗 `esp_timer`（静态 +144 B） |
| 现象/问题 | T3.4/T3.6 都未临时切 `wss://` 复测握手期水位。按"T3.7 实测 33,271 B − T3.4 ~0.4KB − T3.6 ~0.7KB"**估算 ~32.2KB**，仍属推算不是实测 |
| 待验证 | 切公网 `wss://echo.websocket.org/.ws` 冷启动 2 次，采 `sys heap` 内部最低水位与峰值；测完恢复本地 `ws://` 并重烧 |
| 验收方法 | 实测最低水位 ≥ 30KB；静态 DIRAM + 堆峰值 ≤ 440KB |
| 关闭条件 | 2 次采样均达标，数值写入 STATUS §5 + ARCH §9.2 |
| 关联 | CONVENTIONS §4；ARCH §9.2 的 T3.4 段（已标注"估算"） |

---|---|
| 提出时间 | **2026-11-02 约 10:55**（用户问"没有真机验证能开发吗"时确定的工作方式） |
| 来源 | 用户确认开发板不在身边后作出的决定 |
| 现象/问题 | 无板开发只能证明「编译 + 静态逻辑 + PC 侧工具」，**任何出声、分区挂载、真机时序都无法证明** |
| **2026-11-02 更新** | **T3.6 已于当夜真机验收通过**（`error_prompt_test` 场景 A~J 289 项 + 四类提示音实测 + 回归），本条目只剩 **T3.5** |
| 待验证（T3.5） | `assets` 分区镜像能否挂载、从分区加载的提示音能否正常播放、单独烧 `assets` 分区替换提示音（不重编固件） |
| 验收方法 | 回板后按 `tools/error_prompt_test.py` / `voice_test.py` 等跑回归；T3.5 另需单独烧 `assets` 分区验证替换 |
| 关闭条件 | T3.5 真机验收表全绿，STATUS 把「代码完成，待真机验收」改为 ☑ |
| 关联 | STATUS §4 阻塞项；ROADMAP P3 的 T3.5 |


### PV-006 · e2e 端到端在低噪声环境复跑（T3.5 收尾时环境噪声高）
| 项 | 内容 |
|---|---|
| 提出时间 | **2026-11-03 00:0x（采样）**，T3.5 真机验收时登记 |
| 来源 | T3.5 验收回归（2026-11-02 23:5x~00:0x）：`e2e_voice_test` 1/10 轮、`voice_cloud_test` 50/51（均 `audio_end=max-duration`/唤醒未触发） |
| 现象/问题 | 当夜环境噪声升高（e2e 日志：唤醒未触发（底噪 RMS 476~538 / 峰值 1473~2031）、VAD 持续判人声导致 8s 上限收尾、拾音基线峰值 1081 ≥ 800 判环境性）；**成功轮断言 115/116 全绿**，非固件行为异常 |
| 同期对照（同一固件/同一包） | `voice_test` **16/16**、`afe_uplink_test` **15/15**（复跑）、`error_prompt_test` **290/290**、手工 `afplay tools/lexin.wav` 唤醒 **2/2**、`tts_stream` 193/193、`dev_ctrl` 42/42、`mixer` 113/113、`mp3` 48/48、`opus` 45/45 → 链路未回归 |
| 待验证 | 安静环境（基线 RMS ≤300）或白天复跑 `uv run tools/e2e_voice_test.py`，期望 ≥8/10 轮成功（与 T3.6 的 10/10 同口径）；`voice_cloud_test` 期望 51/51 |
| 关闭条件 | e2e ≥8/10 且 voice_cloud 51/51（环境噪声导致的 WARN 可保留），数据写回 STATUS §5 |
| 关联 | STATUS §1 阻塞项⑦（环境噪声口径）、STATUS §5 T3.5 行；`tools/e2e_voice_test.py` 环境重试逻辑 |


### PV-007 · mock 拆分后 4 个真机脚本复跑（工具重构，板子不在手边）
| 项 | 内容 |
|---|---|
| 提出时间 | **2026-11-03 19:2x（采样）**，拆分完成、self-test 通过后登记 |
| 来源 | 用户拍板"现在就拆，板子回来再测试"；`tools/ws_mock_server.py` 由 2,157 行拆为 6 个模块（入口 ~110 行） |
| 现象/问题 | 拆分是**纯搬移**（拆分前后逐函数 AST 比对 **78/78 一致**，唯一变化是入口 `main` 重写与 `Session` 改继承 `DownlinkMixin`）；`--self-test` **19/19 PASS** 只证明 mock 自身协议行为，**4 个真机脚本是 mock 对外行为的最终证据**，当前无板未复跑 |
| 待验证 | 回板后依次跑：`uv run tools/tts_stream_test.py`（覆盖下行 TTS 流的起播/欠载/abort/排空/DownlinkMixin 路径）、`uv run tools/error_prompt_test.py`（错误注入三段时机 + 坏 codec）、`uv run tools/voice_cloud_test.py`（上行事件/sid）、`uv run tools/e2e_voice_test.py`（端到端 ≥8/10 轮） |
| 验收方法 | 三项指标与拆分前基线一致：tts_stream 全场景 PASS、error_prompt **290/290**、voice_cloud **43/43**（环境噪声口径按 PV-006）、e2e ≥8/10；并确认 mock 日志格式未变（`tts 下发完成 frames=`、`TTS 模式: 真实 MP3`、`C1` 前缀），脚本无需改动 |
| 关闭条件 | 四项全绿（e2e 受环境噪声影响时按 PV-006 口径复跑），数据写回 STATUS §3 该行 |
| 关联 | STATUS §3 2026-11-03 行；`tools/README.md`「实现结构」；PV-006（环境噪声） |

### PV-009 · 经 USB Hub 供电时 WiFi 发射瞬间掉压复位（Brownout / TG1WDT）
| 项 | 内容 |
|---|---|
| 提出时间 | **2026-11-03 21:1x（采样）**，用户报告"开发板一直在重启"，当日排查；换原生 USB 口后未再复现 |
| 来源 | 用户报告 + 串口观测（60s 内 6 次 `E BOD` + 3 次 `rst:0x8 (TG1WDT_SYS_RST)`） |
| 现象/问题 | 每次复位都卡在 `wifi: state: init→auth→assoc`（射频首次发射）：`E BOD: Brownout detector was triggered` + `rst:0x3`，`Saved PC=0x40379c09`（`rtc_brownout_isr_handler`）；其余复位 `Saved PC=0x4037a069`（`panicHandler`，panic 打印来不及输出）。BOD 等级已是最宽松的 **2.44V**（`CONFIG_ESP_BROWNOUT_DET_LVL_SEL_7`），说明 3.3V 轨确实塌到 ~2.4V。固件为已验收的 T3.5 版本（`6ed1766`），代码未变；`ioreg` 显示板子挂在**两级 USB 2.0 Hub**（`4-Port USB 2.0 Hub → USB 2.0 Hub`）上，同 Hub 还有视频转接头/无线接收器 |
| 现状 | 换插板子**原生 USB 口**后，20+ 分钟观察 0 次 BOD、可正常连续对话（非 24h 压测，未换线材/未量轨压） |
| 待验证 | ① 用 5V/2A 适配器或直插机身 USB 口供电，冷启动 30 次统计 BOD 次数；② 万用表/示波器量 WiFi 关联瞬间 5V 与 3.3V 轨最低电压；③ 确认是否与 Hub 级联/线材压降相关（换线、换口对比） |
| 验收方法 | 上述条件之一稳定运行 ≥2h 无 BOD；或定位到具体压降点（线材/Hub/LDO） |
| 关闭条件 | 连续 2h（含 ≥50 次 WiFi 关联/重连）0 次 BOD，且结论写入 HARDWARE §1/§5 |
| 关联 | HARDWARE §1（5V/2A 供电要求）、PD-003（LED 满白 60mA 曾触发同类 BOD）、STATUS §3 2026-11-03 行 |

---

### PV-010 · mock LLM→TTS 流水线在真机上的首声/欠载/打断回归
| 项 | 内容 |
|---|---|
| 提出时间 | **2026-11-10 11:30（采样）**，工具优化收尾时登记 |
| 来源 | mock 新增可选流水线 `--llm-tts-stream`（2026-11-10，默认关闭）：开启后 LLM 边出字、按标点分句、Qwen3-TTS 边合成边下发，`tts_start` 可能早于 LLM 完成，0x02 与剩余 `llm_delta` 交错 |
| 现象/问题 | 离线假设备（`mock_offline_test --expect-pipeline`）已验证首声早于 LLM 完成 2.2~2.5s、9s 音频无欠载；但**未在 ESP32 真机**验证设备端 `THINKING→SPEAKING` 时序、R1/R2、播放连续性、按键打断/断网收尾。端侧代码按 sid/窗口处理事件，理论上兼容，但真机 ring/解码/节奏仍需实测 |
| 验收方法 | 回板后跑 `uv run tools/voice_cloud_test.py`（单轮 + `--followup --followup-rounds 2`）、`uv run tools/e2e_voice_test.py`、`uv run tools/tts_stream_test.py`；观察 `VS` 迁移、`PLAYER: stream ... underrun=0`、`round done`、R2、无 ERROR；再默认（不加 `--llm-tts-stream`）做一次 A/B |
| 关闭条件 | 真机 ≥2 轮：首声早于 LLM 完成、`underrun=0`、R2 正常、无 ERROR/断流；按键打断与中途断网各 1 次正常回 IDLE |
| 关联 | `tools/mock_pipeline.py`、`tools/README.md`「LLM 边出字边播」、STATUS §3 2026-11-10 行、PROTOCOL §3.2 |

### PV-011 · M1 音乐链路（mock `--music`）真机验证
| 项 | 内容 |
|---|---|
| 提出时间 | **2026-11-10 16:2x（采样）**，M1 mock 侧交付时登记 |
| 来源 | 用户确认「后端 mock 接的是 Hermes 网关、不能传 tool，ESP32 也不该做 NLU」后，采纳 M1 双通道方案：mock 用确定性规则表把 ASR 文本变成 `music_track` / `music_control`，LLM 只负责说话 |
| 现象/问题 | 离线假设备已验证 178 项（意图/曲库/HTTP Range/事件顺序/未启用不误发），但**未在 ESP32 真机**验证：设备能拉到 mock 内置 HTTP 的 `music_track.url`、能播 6.3s 示例曲目、说「暂停/继续/下一首/停止」时的端侧响应（T4.2/T4.3 实现前只能验到 `vc` 层 WARN 忽略或 T4.1 的 `music play` 探针） |
| 验收方法 | 回板后：`uv run tools/ws_mock_server.py --music --asr sensevoice` + 设备 `CONFIG_AI_WS_URL` 指向 mock；说「嗨，乐鑫 → 放首歌」：设备端 `PLAYER: play`/`VC: music_track`（T4.2 后）、音频真的出声；URL 用 mock 内置示例曲目或**公网电台直播流**（`--music-url … --music-title …`，无需自备 MP3）；再说「暂停/继续/下一首/停止」各一次，对照 mock 日志 `[MUSIC] ...` 与设备端动作 |
| 关闭条件 | 真机 ≥1 轮点歌出声（设备拉流成功、时长与 `duration_ms` 同量级）+ 四条控制指令各 1 次端侧动作正确；T4.1/T4.2 落地后由对应任务回写 STATUS §5 |
| 关联 | `tools/mock_music.py`、`tools/mock_music_test.py`、`docs/ARCHITECTURE.md` §6.3、`docs/PROTOCOL.md` §3.2/§7（P-3）、STATUS §3 2026-11-10 行 |

### PV-012 · T4.1 远端 MP3 音源真机验证（板子未连接）
| 项 | 内容 |
|---|---|
| 提出时间 | **2026-11-10 18:1x（采样）**，T4.1 固件构建通过后登记 |
| 来源 | T4.1（`source_http` + `music` 探针）实现完成；本机 `ls /dev/tty.usbmodem*` **无设备**（只有 Bluetooth/debug console），无法烧录/串口验证 |
| 现象/问题 | 构建通过不代表能播：需验证 ① `music play` 后 `music_http` 任务能建、HTTP/TLS 能连、`first_sound <1500ms`；② 44.1k stereo 320kbps 素材重采样后稳态 `underruns=0`；③ `music stop` 后 ≤3s `active=0`/任务消失/内部堆回落；④ 错误路径（非法 URL、404、断流）不崩、不泄漏；⑤ 播放期内部堆/水位（预计 -14KB 左右）与 PSRAM 增量；⑥ 长播/电台流（Content-Length=-1）行为 |
| 验收方法 | 板子接上后：`uv run tools/music_play_test.py --url 'http://<Mac局域网IP>:9009/media/<曲目>.mp3'`（内部覆盖 play/stat/stop/heap/tasks/ERROR 断言）；再跑 `--url 'http://lhttp.qingting.fm/live/20210755/64k.mp3'` 验直播流；最后用 `curl` 造 404 与 kill HTTP 服务验错误路径。`sys heap`/`sys tasks` 采播放期/停止后数值 |
| 关闭条件 | 脚本全 PASS：首声 <1500ms、稳态 underruns=0、stop ≤3s 且任务消失、内部堆停止后回落（≤4KB 残留）、无 ERROR/panic；电台流连续 60s 无欠载；结果回写 STATUS §5 并关闭本项 |
| 关联 | `components/audio_player/source_http.c`、`include/audio_player.h`（music API）、`components/diag/diag.c`（`music`）、`tools/music_play_test.py`、STATUS §3 2026-11-10 T4.1 行、ARCH §3.7/§9.3 |

### PV-013 · T4.2 `music_cloud` 真机验证（板子未连接）
| 项 | 内容 |
|---|---|
| 提出时间 | **2026-11-10 18:4x（采样）**，T4.2 固件构建通过后登记 |
| 来源 | T4.2（`music_cloud`：`music_track`/`music_control` 解析、`mid` 过滤、5s `music_status`）实现完成；本机仍只有 Bluetooth/debug console，无 USB 串口，无法烧录 |
| 现象/问题 | 构建通过不等于链路通，需验证：① mock `--music` 点歌 → 设备拉到 mock 内置 HTTP 的示例曲目并出声；② `mc status` 的 state/mid/progress/URL 与 `music stat` 一致；③ `music_status` 每 ~5s 一条、字段正确，stop 后有一条 `stopped`；④ `music_control stop` 能让音乐 ≤1s 内淡出停止；⑤ `pause`/`resume` 按设计**只 WARN + 计数、不改变状态、不误停**；⑥ `mid` 过滤：重复 `music_track`（同 mid）不重复起播、过期 mid 丢弃（可用 `ws send` 注入）；⑦ 切歌：新 `music_track` 到达时先停旧流再起新流（重试窗口内不报错）；⑧ 错误路径：非 http(s) URL、404、`codec:"pcm"` 只 WARN/ERROR 计数、不崩 |
| 验收方法 | 回板后：`uv run tools/ws_mock_server.py --music` + 设备 `CONFIG_AI_WS_URL` 指向 mock → 说「嗨，乐鑫 → 放首歌」→ `mc status` 看 state=playing/mid/progress 递增、mock 日志出现 `music_status`；再说「停止」看设备 `MC state playing → stopped`；用 `ws send` 注入同 mid/旧 mid 的 `music_track` 验重复与过期丢弃；注入非 http(s) URL 与 `codec:"pcm"` 验拒绝路径；最后 `sys heap`/`sys tasks` 采播放期与停止后数值 |
| 关闭条件 | 真机 ≥1 轮点歌出声（设备拉流成功、`mc status` 显示 playing、mock 收到 ≥2 条 `music_status`）；stop 1 次生效且上报 stopped；pause/resume 各 1 次按"忽略且不误停"验收；重复/过期 mid 各丢弃 1 次；无 ERROR/panic；结果回写 STATUS §5 并关闭本项 |
| 关联 | `components/music_cloud/`、`components/diag/diag.c`（`mc`）、`components/voice_cloud/voice_cloud.c`（音乐事件跳过）、`tools/ws_mock_server.py --music`、PROTOCOL §3.1/§3.2/§7（P-3）、STATUS §3 2026-11-10 T4.2 行、ARCH §6.3/§8.3 |

> **2026-11-10 T4.3 起**：本项 ⑤ 的验收口径已过时——`pause`/`resume` 不再"只 WARN"，改为真正执行（用户暂停 latch / 从暂停点续播），这部分验收并入 **PV-014**。

### PV-014 · T4.3 `music_session` 真机验证（板子未连接）
| 项 | 内容 |
|---|---|
| 提出时间 | **2026-11-10 19:0x（采样）**，T4.3 固件构建通过后登记 |
| 来源 | T4.3（`music_session`：暂停/继续/停止/切歌、`music_request`、与语音抢占）实现完成；本机仍只有 Bluetooth/debug console，无 USB 串口，无法烧录 |
| 现象/问题 | 构建通过不等于听感/时序正确，需验证：① mock `--music` 点歌出声（`ms status` playing、mock 收到 `music_status`）；② 语音「暂停」→ ≤1s 内静音、上报 `state:"paused"` 且 `progress_ms` 冻结；③ 语音「继续」→ **从暂停点续播**（不是文件开头），progress 接着涨；④ 唤醒自动暂停：播放中说「嗨，乐鑫」→ 音乐 100ms 淡出、会话正常；说完**无音乐意图** → 自动恢复；⑤ 用户暂停 latch：说「暂停」后会话结束**不得**自动恢复；再说「继续」→ 恢复；⑥ 切歌：说「下一首」→ 新 `music_track`，先停旧流再起新流、无 ERROR；⑦ 长暂停（>服务端 idle 超时，如 90s）后「继续」→ 不卡死，转 ERROR/STOPPED 并发一次 `music_request`，服务端回新曲目；⑧ 资源：暂停/继续各 10 次内部堆无单调下降、`sys tasks` 任务数不变（无新任务）、停止后内部堆回落；⑨ 错误路径：非 http(s)/404 不崩 |
| 验收方法 | 回板后：`uv run tools/ws_mock_server.py --music` + 设备 `CONFIG_AI_WS_URL` 指向 mock；`ms status` / `mc status` 观察状态与统计；`afplay tools/lexin.wav` 唤醒后依次说「暂停」「继续」「下一首」「停止」（mock 固定确认语回报）；长暂停用 mock 暂停后等 90s 再「继续」；每步采 `sys heap` / `sys tasks` 数值 |
| 关闭条件 | ①~⑨ 全过：暂停 ≤1s 静音、继续不从头、用户暂停不被覆盖、唤醒自动暂停 + 会后恢复、长暂停有兜底、无 ERROR/panic、无内存泄漏；结果回写 STATUS §5 并关闭本项 |
| 关联 | `components/music_session/`、`components/audio_player/{audio_player.c,source_http.c}`、`components/music_cloud/`、`components/diag/diag.c`（`ms`）、`tools/ws_mock_server.py --music`、ARCH §3.2/§6.3/§7/§8.3/§8.4、PROTOCOL §3.1（P-8/P-9）、STATUS §3 2026-11-10 T4.3 行 |

## 3. 待决定 / 已被暂缓（PD）

### PD-001 · LED 亮度太亮晃眼（用户 2026-11-02 反馈）
| 项 | 内容 |
|---|---|
| 提出时间 | **2026-11-02 约 11:00**（用户口述反馈，11:01 登记） |
| 来源 | **用户直接反馈**：「灯的亮度能调节吗？太亮太晃眼了」（针对 2026-11-01 夜看到的白灯常亮） |
| 现象/问题 | 亮度是**编译期常量** `CONFIG_DEV_LED_BRIGHTNESS`（默认 24/255 ≈ 9%），改一次要重烧；**白色是全部模式里最刺眼的**（R/G/B 三通道全开，主观亮度约为单色 2~3 倍），静音白灯正好撞在最亮档；默认 24 是开发时拍的值，**没做过观感校准** |
| 处置方向（已定，待执行） | 回板后做三件事：① 默认亮度下调（建议 8 左右）；② **白灯单独降权**（如 `br/3`），避免三通道叠加刺眼；③ 加运行期命令 `dev bright <0~255>` + 亮度写 NVS（复用 `config_store`，key 如 `led_br`），重启保持，不用重烧 |
| 为什么暂缓 | 亮度是纯主观项，**必须眼睛看着调**；开发板不在手边时改了也无法确认，而且每次都要重烧，反馈回路太长 |
| 用户决定 | **2026-11-02 21:4x（采样）最终决定：整颗灯不要了** —— LED 功能整体移除，亮度问题随功能一起作废（见 PD-003 与 §4 归档） |
| 验收方法 | 回板后 `dev bright 4/8/16/32` 逐档目视选值 → 确认 NVS 掉电保持 → 确认 7 种模式在选定亮度下均可见且不刺眼 |
| 关闭条件 | 用户确认"不刺眼且看得清"；默认值写回 Kconfig，`dev bright` 与 NVS 键写入 ARCH §4.1/§10.2 与组件 README |
| 关联 | STATUS §1 阻塞项㉒、`components/dev_ctrl/Kconfig`、`ws2812_led.c` 的 `led_render()` |

### PD-004 · 云端 TTS 电平口径与端侧兜底（嚓嚓/破音的处置方向）
| 项 | 内容 |
|---|---|
| 提出时间 | **2026-11-03 21:5x（采样）**，用户报告"下发的 MP3 对话有嚓嚓声，唤醒提示音没有" |
| 来源 | 用户实测 + 本会话测量（Piper 合成样例/Mac 侧解码） |
| 现象/问题 | 提示音是 `say` 生成、峰值 **-7.82 dBFS**（RMS p90 -19.1 dBFS）；云端 TTS 路径的素材（mock Piper）峰值 **-0.52 dBFS**、RMS p90 **-10.6 dBFS**，响 **7~10 dB**，且安静段噪声底 -44.5 dBFS（提示音 -82.5 dBFS，VITS 呼吸噪声）、4-10kHz 相对能量高 ~6 dB。端侧音量 100 时只有 TTS 会越过软限幅膝点（-1.92 dBFS）并把 3W 喇叭推到近满功率 → 听感"嚓嚓"/破音。**这是电平/内容口径问题，不是 MP3 解码或流式路径的回归**（T2.3/T2.4 的 PCM 对拍仍 max\|Δ\|=1 LSB） |
| 现状 | 2026-11-03：mock 侧加过峰值归一用于验证；**2026-11-04：TTS 引擎整体换成 Qwen3-TTS（MLX）**——输出峰值实测 **-5~-12 dBFS**（句子/语气有波动，仍低于端侧软限幅膝点 -1.92 dBFS）（Piper 是 0 dBFS、停顿还有 -44 dBFS 噪声），测试侧的电平/噪底来源已消失，归一默认改回**关闭**（`MOCK_TTS_PEAK_DBFS` 可强制）。**真实云端 TTS 的电平口径与端侧兜底仍未定** |
| 处置方向（待拍板） | ① **契约侧（推荐）**：PROTOCOL/ARCH 约定云端 TTS 输出峰值 ≤ -6 dBFS，后端负责归一；② **端侧兜底**：给 `CH_TTS` 固定衰减 -3~-6 dB（不影响提示音，改 `audio_player` + ARCH §3.5）；③ 降默认音量（`CFG_VOLUME_DEFAULT` 100→70，所有输出一起变）；④ 换 Piper 音色/提 MP3 码率（只影响 mock 听感，不解决真实后端） |
| 验收方法 | 说一句 → TTS 播放无"嚓嚓"、无 `player stat` 的 limiter 触发（或 limiter 触发但听感可接受）；对比 `MOCK_TTS_PEAK_DBFS=off` 的同一句 |
| 关闭条件 | 选定方向并落地（契约文字或端侧衰减），真机听感通过 |
| 关联 | ARCH §3.5（音量/限幅）、§3.7（TTS 下行）；`tools/mock_tts.py`；STATUS §3 2026-11-03 行 |

### PD-005 · 大模型（LLM）模块测试与 mock 的 `--llm` 步骤（用户 2026-11-04 提出）
| 项 | 内容 |
|---|---|
| 提出时间 | **2026-11-04 12:4x（采样）**，用户原话：「之后还希望能大模型生成答案也可以测试」（模块测试，不用开发板） |
| 来源 | 用户对 ASR/TTS 模块测试的同批要求（1、2 已交付：`tools/asr_test.py`、`tools/tts_test.py`） |
| 现状 | **2026-11-04 已落地**：`tools/llm_client.py`（OpenAI 兼容流式，纯标准库）+ `tools/llm_test.py`（离线模块测试）；mock 新增 `--llm`：`audio_end → asr_final → llm_delta（真增量）→ tts_start → 0x02 → tts_end`，回答交给 Qwen3-TTS。模型与网关地址只走环境变量（不写进文件）；实测所选模型首包 ~1.0~1.4s，另有模型会先吐 1k+ 字思考、首包 ~6s（不适合语音）。失败回退"复读 ASR 文本" |
| 目标 | ① `tools/llm_test.py`：文字 → LLM → 文字，独立可测（不接设备）；② mock 增加 `--llm`：`ASR 文本 → LLM 回答 → Qwen3-TTS → 下发`，与现在的复读模式互斥；③ 模型选择待定（本地 MLX 小模型 / 云端 API 二选一，需先定口径） |
| 待定问题 | ① **多轮上下文**（现在每轮独立、不带历史）；② **回答长度**（`LLM_MAX_TOKENS` 默认 256，长回答会让设备上行/下行超时）；③ **LLM→TTS 流水线**（现在是"等回答完整再 TTS"，可改为按句出结果就合成，首声更快）；④ 失败回退策略（当前回退复读；也许该播"服务繁忙"提示音） |
| 验收方法 | 不接设备：`uv run tools/llm_test.py --text "今天天气怎么样"` 打印回答；接 mock 后：说一句 → 设备播出的**回答**与 `llm_test` 同模型同 prompt 的输出一致，且 `llm_delta` 是回答文本而非复读。**已完成离线验证**：mock 假客户端收到 20+ 个 `llm_delta` 增量、269 帧 TTS、首帧 253ms；真机待验 |
| 关闭条件 | 两个工具/开关落地、离线自测通过、文档（tools/README、STATUS）同步 |
| 关联 | `tools/mock_downlink.py` 的 `_asr_text`/`llm_delta` 生成处、`tools/mock_cli.py`、`tools/mock_tts.py`；PD-004（TTS 电平） |

### PD-006 · 音乐 `mid`/`music_status` 与"实际出声"的时序（语音期间排队曲目）
| 项 | 内容 |
|---|---|
| 提出时间 | **2026-11-10 19:1x（采样）**，T4.3 实现时暴露 |
| 来源 | 播放器仍是**单作业**模型：语音活跃期间到达的新 `music_track` 必须先排队，等 TTS/提示音结束（语音回 IDLE）才能起播，否则 `music_start()` 失败、重试窗口覆盖不了长 TTS 会误报 ERROR |
| 现象/问题 | ① `mid` 在 `music_track` 被接受时（L3）即更新，排队期间上报的 `music_status` 可能**已经带新 mid**、state 还是旧的 playing/paused、progress=0；② 云端 `music_control{resume}` 在 TTS 播放中会把状态置 `playing`，但 PUMP 排在 TTS 之后才出声 → "上报 playing" 早于 "实际出声"。对"按 `mid` + progress 跟踪播放"的后端可能造成短暂不一致（不影响端侧听感） |
| 处置方向（待定） | ① **推荐**：接受现状——T4.4 上"音乐继续播 + duck"叠加方案后，TTS 期间音乐不再让路，排队场景消失，问题自然收敛；② 排队期间上报新状态（如需协议加 `pending`/`buffering`）；③ 后端按 `state+progress` 判活，不依赖 mid↔出声严格同步 |
| 为什么暂缓 | 属 T4.3 过渡实现的副作用，改协议/状态机的代价大于收益；T4.4 会改播放器叠加模型 |
| 验证/关闭条件 | 真机 **PV-014 ⑥**（语音中说"下一首"）确认端侧行为可接受；**T4.4 定稿时**确认 overlay 后的时序并回写 PROTOCOL（或后端明确接受现状） |
| 关联 | `components/music_session/music_session.c`（`s_track_pending`/`s_defer_pause_reason`）、ARCH §8.3「已知限制」、STATUS §3 T4.3 行、PV-014 |

### PD-002 · 断网没有独立的视觉/听觉反馈（设计缺口）
| 项 | 内容 |
|---|---|
| 提出时间 | **2026-11-02 约 10:57**（用户问"LED 如何知道网络连接失败"时暴露） |
| 来源 | 用户提问 + 代码复核（`vs_on_net_down()`） |
| 现象/问题 | `NET_DOWN` 的处理是 `vc_cancel()` + `vs_abort_to_idle(NET_DOWN)` —— 状态机回 `IDLE`，**灯变绿常亮，和"一切正常"一模一样**。红灯只对应"会话中云端报错/超时"，**不覆盖"网断了"**；待机状态断网更是完全看不出（始终绿灯） |
| 现状可用钩子 | `APP_EVT_VOICE_STATE_CHANGED` 的 payload 里**已经带 `reason`**（`net-down` / `timeout` / `cloud-error` / `interrupt` 等 23 个原因码，`vs status` 可见），只是 `dev_ctrl` 目前忽略 `reason`（只消费 `to`） |
| 处置方向（若做） | ① "无网络"作为**持续状态**独立于会话状态显示/提示（否则待机断网永远看不出）；② 与 `feedback` 的 kind 体系统一设计（可扩展 `fb_kind_t`，或在 IDLE 订阅 NET_DOWN 后自行提示），别做成两套反馈 |
| 用户决定 | **2026-11-02 约 11:00 明确"先不了，后面再说"**（不在本轮做） |
| **T3.6 后的状态（2026-11-02）** | **会话中/中途断网已出声**（"网络未连接"提示音，用户 2026-11-02 拍板要求）；**待机（IDLE）断网仍无任何反馈**（本任务未做，保持暂缓）。LED 已移除，PD-002 的"灯光双闪"方向作废，未来通道走 `APP_EVT_FEEDBACK`（屏幕/其它）或独立"网络状态"反馈 |
| 关闭条件 | 决定做 → 设计写入 ARCH §7/§5 并并入 T3.6（或新任务）；决定不做 → 在 PRODUCT_IDEAS §6 记录决策与理由 |

### PD-003 · 板载 WS2812 解码异常：任何帧都显示满白，且满白电流导致 BOD 反复复位
| 项 | 内容 |
|---|---|
| 提出时间 | **2026-11-02 20:33（采样）** 用户报告「白灯常亮，特别亮」；排查至 21:35 |
| 来源 | 用户目视 + 本会话真机排查（`dev led <mode>` 七色测试 / T0H 扫描 / 纯色与 RGBW 变体 / 脉宽实测） |
| 现象 | 板载 RGB 灯（USB 口旁，GPIO48）**只会亮、不会按帧变色**：7 种模式（绿/青/黄/蓝/红闪/白）目视完全不变；发「全黑帧」也仍是满白；偶发**一次蓝色闪**。满白电流（3×约 20mA ≈ 60mA）叠加 WiFi 启动峰值 → **BOD 掉压复位**：串口 `E BOD: Brownout detector was triggered` + `rst:0x3 (RTC_SW_SYS_RST)`，每 ~1.9s 循环一次，设备不可用 |
| 已排除 | ① **寄存器写法**：与 IDF 官方 `gpio_ll_set_level` 逐条一致（S3：GPIO≥32 用 `out1_w1ts/out1_w1tc`，`1<<(n-32)`）；② **引脚**：冷上电前后对比证明灯确实由 GPIO48 驱动（未驱动时不亮）；③ **时序数学**：`dev led selfcheck` 实测帧耗时 8515 cycle（理论 7200）、位周期 ≈1478ns，`dev led measure` 实测 T0H≈492ns / T1H≈913ns（与 datasheet 400/850 + 循环开销 75ns 吻合）；④ **固件版本**：回退到已提交的 T3.4 固件同样现象；⑤ **RGBW 4 字节变体**：`dev led raw 24 0 0 0` / `... 255` 仍是白色 |
| 已确认的副作用链 | LED 满白电流 + WiFi 启动 = BOD。**冷上电**（拔 USB ≥5s，LED 断电复位熄灭）后：LED 固件关闭（不碰 GPIO48）→ 30 秒观察 **0 次 BOD、0 次复位、WiFi 正常联网（5.7s）** |
| 用户决定 | **2026-11-02 21:4x（采样）：「干脆不要这颗灯了」** —— 移除 LED 状态指示功能 |
| 当前处置（已落地） | ① `CONFIG_DEV_LED_ENABLE` 默认改 **n**（GPIO48 完全不碰，避免再触发 BOD）；② **静音与 LED 解耦** —— 长按静音在 LED 关闭时仍可用（实测 100→0→100，`dev status` 计数正确）；③ 保留调试旋钮：`dev bright <0-255>`（运行期亮度）、`dev led rgb/raw/measure/timing/selfcheck/sweep` |
| 待验证 / 下一步 | ① **量 LED 供电电压**（3.3V 还是 5V）与数据线电平：若 LED 由 5V 供电而数据只有 3.3V，属经典 VIH 不足（WS2812 要求 ≥0.7×VDD≈3.5V），需电平转换或把 LED 改 3.3V 供电；② 用逻辑分析仪或 **RMT RX 环回**抓 GPIO48 实际波形（看边沿/过冲/是否有毛刺）；③ 试 **RMT 后端**发送（标准外设时序，排除 bit-bang 微小偏差）；④ 试 GPIO 驱动能力档位 / 串一个小电阻抑制振铃；⑤ 若确认为硬件限制，评估放弃板载灯或换可用引脚（需改 HARDWARE §2） |
| 关闭条件 | 7 色目视正确 + 满白电流消失（WiFi 启动不再 BOD）+ 亮度可调（PD-001 一起关闭） |
| 关联 | PD-001（亮度）、STATUS §1 阻塞项、`components/dev_ctrl/ws2812_led.c`、`components/diag/diag.c` 的 `dev led` 命令 |

---

## 4. 已关闭归档

| 编号 | 关闭时间 | 结论 | 证据 |
|---|---|---|---|
| PD-001 LED 亮度太亮 | 2026-11-02 21:4x（采样） | **作废（随 LED 功能移除）**：不需要再调亮度 | PD-003 决定移除 LED 功能；本项同时关闭 |
| PD-003 板载 WS2812 解码异常（满白→BOD） | 2026-11-02 21:4x（采样） | **决定不做**：整颗灯移除，固件只把 GPIO48 拉低保持（防止上电噪声锁存亮色），设备恢复稳定可用 | 移除后真机 20 秒 0 BOD / WiFi 正常（5764ms 联网）；`dev_ctrl_test --skip-interrupt` 21/21；实现留档于 git 4f5d152 + 084ef4f |
| PV-001 T3.4 LED 逐色目视 + 物理按压 | 2026-11-02 21:4x（采样） | **LED 部分作废**（灯已移除）；**物理按键按压部分并入下方 PV-004** | — |
| PV-005 T3.6 提示音听感 | **2026-11-02 23:0x（采样）** | **用户确认通过**：设备上 `fb test net/cloud/timeout/tts` 与试听 WAV 均接受（音色/语速/响度 OK，与「我在呢」等响、不刺耳） | 用户口头确认 2026-11-02；峰值统一 -7.82 dBFS、时长 1075/1468/1083/1236 ms；试听 WAV `build/t36_prompts/`；后续 T3.5 迁 assets 分区时资源不变 |
| PV-003 T3.5 无板开发部分 | **2026-11-02 23:5x（采样）** | **真机验收通过**：`assets` 分区 pack 挂载/mmap 加载（303~310us）、5 条提示音播放正常、`error_prompt_test` 290/290、**单独烧 assets 分区替换提示音成功（app bin sha256 不变）**、坏 pack 降级不崩 | `prompt info`（mode=flash-mmap, ver=1, count=5, 181300B）、swap timeout 17325→19769→恢复、坏包 0xFF→ERROR+降级；见 STATUS §5 T3.5 行 |
| PV-008 T3.7 续听预录修复真机复验 | **2026-11-03 21:5x（采样）** | **用户真机确认通过**：连续对话多轮 ASR 文本完整（修复前只有唤醒轮完整、续听轮缺句首）；设备侧日志出现 `VC: 续听预录开始` / `续听预录：保留 N ms 前导`；窗口超时/退出后预录停止不影响下一轮 | 用户 2026-11-03 21:5x 口头确认；固件 `dfd48b4-dirty`（Compile time Nov 3 2026 21:42:58，bin 1,942,448 B）经原生 USB 烧入后实测；代码/设计见 STATUS §3 2026-11-03 行、ARCH §3.3/§7 |

---

## 5. 时间线（便于回溯"什么时候的问题"）

- **2026-11-10 16:2x（采样）**：新增 PV-011（M1 音乐链路真机验证）；同日 P-3 按「音乐事件用独立 `mid`」定稿建议落地到 mock 与 PROTOCOL。
- **2026-11-10 18:1x（采样）**：新增 PV-012（T4.1 真机验证）；T4.1 固件构建通过，但本机无 USB 串口，未烧录。
- **2026-11-10 18:4x（采样）**：新增 PV-013（T4.2 `music_cloud` 真机验证）；T4.2 固件构建通过（bin 1,982,880 B / DIRAM 201,046 B），仍无 USB 串口。
- **2026-11-10 19:1x（采样）**：T4.3 `music_session` 完成（构建口径）：暂停/继续**保 ring 从暂停点续播**、语音抢占（用户暂停 latch 优先）、`music_request` 兜底、`music_status` 增 `paused`；bin **1,993,152 B** / 静态 DIRAM **200,870 B（−176 B）**、host 单测 **76/76**；新增 **PV-014**（真机 9 项）与 **PD-006**（语音期间排队曲目的 mid/status 时序）。

| 时间（Asia/Shanghai） | 事件 |
|---|---|
| 2026-11-01 22:42（采样） | T3.4（dev_ctrl 按键/LED/静音）开工 |
| 2026-11-01 22:54 → 23:04（采样） | T3.4 代码与测试推进；`dev_ctrl_test` 53/53 + 回归全绿，固件烧入 |
| 2026-11-01 夜 | **用户目视到板载灯白色常亮** → 证实 bit-bang 时序 / GPIO48 / 亮度渲染正确（PV-001 部分证据） |
| 2026-11-02 10:27（采样） | 用户问「灯的作用是什么 / 为什么要做这个功能」；同轮确认**开发板已不在身边** |
| 2026-11-02 10:51（采样） | 用户问 T3 剩余任务 → 确认剩 T3.6、T3.5 两个 |
| 2026-11-02 约 10:55 | 用户问「没有真机验证能开发吗」→ 确定"代码完成、待真机验收"的工作方式（PV-003） |
| 2026-11-02 约 10:57 | 用户问 LED 如何获知网络失败等状态 → 暴露断网无独立反馈的设计缺口（PD-002） |
| 2026-11-02 约 11:00 | 用户反馈 **LED 太亮晃眼**（PD-001）并决定"先不动，等板子回来"；要求建本文件登记 |
| 2026-11-02 11:01（采样） | 建 `docs/PENDING.md`，登记 PV-001~003 / PD-001~002 |
| 2026-11-02 20:33（采样） | 用户带回开发板并连上；报告「白灯常亮、特别亮」（PD-003 起点） |
| 2026-11-02 20:36→21:20 | 七色测试无变化 → 时序自检/脉宽实测 → 发现 BOD 掉压复位 → 回退固件对比 → T0H 扫描/纯色/RGBW 变体均无效 |
| 2026-11-02 21:35（采样） | 冷上电验证：LED 断电熄灭后 30 秒 0 BOD、WiFi 正常 → 确认「满白电流 + WiFi」是复位原因；LED 默认关闭 + 静音解耦 |
| 2026-11-02 22:01（采样） | 用户要求"下个任务发我" → 出 T3.6 任务单 |
| 2026-11-02 22:0x（采样） | 用户拍板：①用**中文提示语音**；②会话中途断网也出声；③设备侧 TTS 播不出来也提示；④开发板可用；⑤**错误反馈要可扩展**（以后可能有屏幕/灯光，语音只是一种方式） |
| 2026-11-02 22:1x→22:5x | T3.6 实现 + 真机验收：`feedback` 组件（`APP_EVT_FEEDBACK` 语义事件 + 语音通道）、四类提示音（`tools/prompt_gen.py` 生成）、`error_prompt_test` 场景 A~J 289 项、回归全绿（详见 STATUS §3/§5） |
| 2026-11-02 23:0x（采样） | 用户确认 T3.6 验收（含 4 条提示音听感）→ **PV-005 关闭归档**；下一任务 T3.5 |
| 2026-11-02 23:0x→23:5x | T3.5 完成：用户拍板方案 D（自定义只读 pack + mmap）；`tools/prompts_src`+`audio_pack.py`+`prompt_store`；真机 error_prompt 290/290、只烧 assets 替换验证、坏包降级；**PV-003 关闭**；P3 收口 |
| 2026-11-03 00:0x（采样） | T3.5 验收回归遇环境噪声：e2e 1/10、vc 50/51（均环境性）；voice/afe 复跑通过、手工唤醒 2/2 → 登记 **PV-006**（安静环境复跑） |
| 2026-11-03 19:1x→19:3x（采样） | 用户要求拆 `tools/ws_mock_server.py`（"现在就拆，板子回来再测试"）→ 拆为 6 模块（行为不变，AST 78/78 一致、`--self-test` 19/19），登记 **PV-007**（4 个真机脚本复跑） |
| 2026-11-03 21:3x（采样） | 用户报告：只有唤醒那轮 ASR 完整，续听轮只听到后半句 → 定位为续听起轮前语音未上行（`vc_begin_round()` 才 `audio_fe_uplink_start()` 且清空缓冲），当日修复（`vc_preroll_*` + `VC_UPLINK_PREROLL_MS`）并烧入，登记 PV-008 |
| 2026-11-03 21:5x（采样） | 用户确认续听预录修复有效 → **PV-008 关闭归档**；同一时段把数据线换到板子原生 USB 口后，未再出现 `E BOD` 掉压复位（观察 20 余分钟，非 24h 压测） |
| 2026-11-03 22:0x（采样） | 用户报告下发 TTS 有"嚓嚓"、提示音没有 → 测量定位为**电平差 7~10 dB**（TTS 峰值 -0.52 vs 提示音 -7.82 dBFS）+ VITS 噪底/高频偏多；mock 侧加峰值归一验证，端侧兜底登记 **PD-004** |
| 2026-11-04 12:4x（采样） | 用户拍板：TTS **去掉 Piper、只用 Qwen3-TTS**，并要**不接开发板**的模块测试（ASR/TTS，后续 LLM）→ 当日落地：`mock_tts` 重写 + mock `--tts-qwen` 流式下发 + 新增 `tools/tts_test.py`、`tools/asr_test.py`；LLM 部分登记 **PD-005** |
| 2026-11-04 14:2x（采样） | 用户给出 LLM 网关与 token（支持流式）→ 当日落地 `tools/llm_client.py` + `tools/llm_test.py` + mock `--llm`（ASR → LLM → Qwen3-TTS）；离线假客户端验证 269 帧/首帧 253ms；PD-005 主体完成，余多轮上下文/长度上限/TTS 按句流水线待定 |
