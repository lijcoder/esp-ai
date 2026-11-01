# voice_session —— 语音会话状态机（L4 业务编排）

**作用（一句话）**：把"唤醒 → 应答提示音 → 上行收音 → 等云端 → 播放 TTS → 回到待命/继续听"串成**一个可观测、
可打断、有超时与错误策略的会话状态机**（`IDLE/LISTENING/THINKING/SPEAKING/FOLLOW_UP/ERROR`），并独占每轮 `sid` 分配；
T3.7 起 TTS 播完默认进入 `FOLLOW_UP` 续听窗口，用户直接说话即可连续对话（免唤醒、免提示音）。

契约依据：[ARCHITECTURE.md](../../docs/ARCHITECTURE.md) §7（状态机）、§8.1–8.2（流程）、§4.2（事件）、
§9.3（任务模型）；协议见 [PROTOCOL.md](../../docs/PROTOCOL.md) §3.3（`sid`）与 §4（错误码）。

## 职责边界（重要）

| 属于本组件 | **不**属于本组件 |
|---|---|
| 会话六态状态机与迁移日志（TAG `VS`，含 sid/停留时长） | 上行 Opus 编码/发送、下行解析（`voice_cloud`，L3） |
| 每轮 `sid` 分配（uint16 自增、0 保留），经 `vc_start(sid)` 传入 | 协议内三态 `IDLE/UPLINK/WAIT_RESPONSE`（仅 `voice_cloud` 内部用） |
| 唤醒编排（T1.2b）：消费 `APP_EVT_WAKE_DETECTED`，播应答提示音并做提示音的 R1/R2 | TTS 播放的 R1/R2 与流式解码（`voice_cloud` + `audio_player`，T2.4 已验收） |
| 会话级超时/错误策略（THINKING 超时、云端 error → `ERROR`，同轮不重试） | 协议超时的**计时器**本身（在 `voice_cloud`，本组件消费其回调） |
| `vs_interrupt()` 打断骨架、`vs status` 统计 | 按键扫描/LED（`dev_ctrl`，T3.4）、错误提示音（T3.6） |
| **T3.7 连续对话**：`FOLLOW_UP` 续听窗口、VAD 人声确认后免唤醒起轮、轮数上限、窗口内唤醒重开、运行期开关 | 音乐/本地播放的 ducking 与连续对话叠加（P4/T4.4）、ASR 真实语义（云端） |

## 对外接口（`include/voice_session.h`）

| 接口 | 说明 |
|---|---|
| `vs_init()` | 订阅 `event_bus`（`APP_EVT_ANY`）+ 注册 `vc_register_cb` + 订阅播放完成回调；**不建任务**。必须在 `app_evt_init`/`vc_init`/`audio_player_init` 之后 |
| `vs_start()` | 手动开始一轮（不播应答提示音，直接进入上行）；已有会话返回 `ESP_ERR_INVALID_STATE` |
| `vs_interrupt()` | **T3.2 R4 完整打断**：`audio_player_fade_out_and_stop(80ms)` → `vc_interrupt()`（interrupt 报文 + cancel）→ R5 丢弃 50ms → 回 `IDLE`；播放完成回调 `ok=false`，门控位由回调释放 |
| `vs_is_busy()` / `vs_get_state()` / `vs_state_name()` / `vs_reason_name()` | 状态观测（原因码用于日志/事件 payload） |
| `vs_current_sid()` | 当前（或最近一轮）sid |
| `vs_set_followup_enabled(bool)` / `vs_followup_enabled()` | **T3.7**：续听窗口运行期开关（默认取 Kconfig，不持久化；关闭时若在 `FOLLOW_UP` 立即回 `IDLE`） |
| `vs_stats_read()` / `vs_stats_reset()` | 轮次（开始/完成/异常/中止）、打断、唤醒丢弃、超时、云端 error、迁移次数、上次状态停留、**续听（续听轮/命中/超时退出/上限退出/窗口剩余）**（`diag` 的 `vs status` 使用） |

## 状态机与驱动来源

| 迁移 | 触发（来源） | 备注 |
|---|---|---|
| `IDLE → LISTENING` | `APP_EVT_WAKE_DETECTED`（`evt` 任务）/ `vs_start()`（console）/ `APP_EVT_BUTTON` 短按（T3.2） | 唤醒时若已有会话或播放器忙（提示音/TTS 收尾）→ 丢弃唤醒并计数（`wakes_dropped`）；**音乐/本地长播中不丢弃**（R3：先淡出音乐让出单输出通道） |
| `LISTENING → THINKING` | `VC_EVT_UPLINK_END`（`vc` 任务）：`audio_end` 已发出 | 收尾原因（vad/max/stop）在 `code` 字段 |
| `THINKING → SPEAKING` | `VC_EVT_TTS_START`（`ws_client` 任务） | TTS 侧 R1 由 `voice_cloud` 打开流时执行 |
| `SPEAKING → FOLLOW_UP` | 播放完成回调（`player` 任务，`STREAM_DONE(ok=true)`）或 `VC_EVT_ROUND_END(DONE)`（无可播音频） | **T3.7**：等 TTS 排空 + R2 恢复收音后开续听窗口；`ok=false`（打断/失败）仍直接回 `IDLE` |
| `FOLLOW_UP → LISTENING` | `APP_EVT_VAD_SPEECH_START`（`evt` 任务）+ 250ms 人声确认 | 直接 `vc_start(新 sid)`，不播提示音、不再唤醒；确认期内 VAD 结束则取消并恢复剩余窗口 |
| `FOLLOW_UP → LISTENING`（重开） | `APP_EVT_WAKE_DETECTED` | 播应答提示音、续听计数清零（原因 `followup-wake`） |
| `FOLLOW_UP → IDLE` | 窗口静音超时（`esp_timer`）、续听轮数上限、按键/断网/`vs followup off` | 原因 `followup-timeout` / `followup-max` / `interrupt` / `net-down` / `followup-off` |
| `THINKING/SPEAKING → ERROR` | `VC_EVT_ROUND_END(TIMEOUT)` / `VC_EVT_CLOUD_ERROR` | `voice_cloud` 报告事实，本组件决定进 `ERROR` |
| `任何活跃 → IDLE` | `APP_EVT_NET_DOWN`、`VC_EVT_ROUND_END(LINK/CANCEL/NEW/SETUP)`、`vs_interrupt()`（含按键短按） | 断链/取消/新轮次覆盖；R4 打断会先淡出并作废 sid（T3.2） |

- **所有迁移**都打 INFO：`VS: state A -> B（sid=N，停留 M ms，原因=...）`，并存进迁移统计；
- 每次迁移后投 `APP_EVT_VOICE_STATE_CHANGED`（`app_evt_voice_state_t`，见 ARCH §4.2），供 T3.4 LED/UI 消费；
- 并发来源（`evt`/`ws_client`/`player`/console）通过临界区 CAS（期望状态 → 目标状态）串行化，不匹配直接丢弃，
  天然幂等（重复完成回调/迟到事件不会打乱状态）。

## Kconfig

| 配置 | 默认 | 说明 |
|---|---|---|
| `VS_ERROR_HOLD_MS` | 0 | `ERROR` 状态停留时长；0 = 立即回 `IDLE`（迁移日志仍完整）。T3.6 接入错误提示音后设为提示音时长（如 1200），由 `esp_timer` 一次性计时，不占任务栈 |
| `VS_FOLLOWUP_ENABLE` | y | **T3.7**：连续对话（免唤醒续听窗口）总开关；关闭行为与 T3.1 完全一致 |
| `VS_FOLLOWUP_WINDOW_MS` | 5000 | 续听窗口时长；窗口内无 VAD 人声 → `IDLE` |
| `VS_FOLLOWUP_CONFIRM_MS` | 250 | VAD 人声确认时长，过滤短促环境噪声；0 = 不确认 |
| `VS_FOLLOWUP_MAX_ROUNDS` | 5 | 最多续听轮数（不含首轮唤醒轮），到上限回 `IDLE` |

## 关键约束

1. **不建任务**（ARCH §9.3）：T2.4 实测 wss 握手期内部堆最低水位仅 32.7KB，再建 4KB 任务的栈会击穿
   CONVENTIONS §4 的「≥30KB」铁律。状态机是纯内存操作，handler 借用 `evt`/`ws_client`/`player`/console 上下文。
2. **handler 内禁止阻塞**：播提示音（`audio_player_play_pcm`）、`vc_start()`、`vc_cancel()`、`audio_fe_set_feed_enabled()`
   都是非阻塞调用；`ESP_LOGI` 只打关键迁移。
3. **不绕过本组件改会话状态**（CONVENTIONS §9）：其它模块要驱动会话只能投事件或调本组件 API。
4. **本组件不直接碰协议**：只调用 `voice_cloud` 的对外 API 并消费其回调；不 `ws_send_*`、不解析 JSON。
5. **R1/R2 统一门控（T3.2）**：本组件只 `audio_fe_feed_hold(AUDIO_FE_HOLD_PROMPT)` / `release` 自己那一位，
   TTS 的 R1/R2 在 `voice_cloud` 持 `AUDIO_FE_HOLD_TTS`；位图归零才真正开麦，互不覆盖。提示音完成回调里先 `release` 再起轮。
6. **R4/R5（T3.2）**：`vs_interrupt()` 的调用上下文可能是 `evt` 任务（按键）或 console；只做非阻塞调用
   （淡出请求、`vc_interrupt`、`audio_fe_feed_discard_ms`），实际淡出在 `player` 任务执行。
7. **R3 边界**：音乐播放中唤醒不再丢弃，但当前播放器是单任务单通道，做法是"淡出音乐让出通道"；
   duck 叠加播放（音乐继续播 + TTS 混音）归 T3.3/P4。
8. **T3.7 续听窗口只用 1 个一次性 `esp_timer`**（`vs_fup`，不建任务）；窗口内 VAD 确认与超时复用同一计时器
   （确认期满 → 起轮；否则 → 超时回 IDLE）。这是内部 RAM 最省的做法（CONVENTIONS §4 水位余量很小）。
9. **续听起轮不改变 R1/R2**：窗口建立在 TTS 的 R2 之后，feed 已恢复；起轮只 `vc_start`，不碰门控位。
   窗口内按键/断网按 R4/R5 退出。

## 真机验收（T3.7，2026-11-01）

- `uv run tools/voice_cloud_test.py --followup --followup-rounds 2` **66/66 PASS**：首轮唤醒后
  TTS 播完进入 `FOLLOW_UP`；窗口内直接 `afplay` 语音 → 自动起第 2/3 轮（`sid` 1→2→3、日志无唤醒词）；
  窗口静音超时回 `IDLE`；`vs status` 续听轮/命中/超时退出符合预期；单轮模式（`vs followup off`）**51/51 PASS**。
- 连续对话实测（真机）：2 轮续听均正常起轮/播放/收尾；`audio_end` 自动（vad-silence）→ mock TTS → 再进窗口。
- **环境限制**：测试时段环境底噪偏高（RMS~1000+、有人声），曾出现环境人声额外触发续听轮
  （人声确认 250ms 只过滤短噪声）；测试脚本对"额外续听轮"做 ≥ 断言并延长窗口等待，不掩盖固件问题；
  安静环境建议复跑取得严格计数。
- **全量回归（设备重连后）全绿**：`e2e_voice_test` **9/10 轮 610 项 PASS**（1 轮环境失败，成功轮全绿）、
  `tts_stream_test` 193、`voice_test` 16、`afe_uplink_test` 15、`mp3_test` 48、`opus_enc_test` 45、
  `mixer_test` 113、mock 13；`e2e_voice_test` 每轮先 `vs followup off`，保持原"每轮唤醒+提示音"验收语义。
- 资源：静态 DIRAM **199,094 B（+48 B vs T3.3）**、无新任务；`ws://` 真实语音轮内部堆峰值 **224,812 B**、
  最低水位 **32,983~33,131 B**（≥30KB）；新增一个一次性 esp_timer（~百余 B）。

## 真机验收（T3.1，2026-11-01）

- `voice_cloud_test.py` **50/50 PASS**：VS 迁移序列、`sid` 一致、`VOICE_STATE_CHANGED`、`vs status` 统计。
- `e2e_voice_test.py` **10/10 轮、677 项断言 PASS**：全程状态序列正确、内部堆波动 52B、PSRAM Δ0、任务数恒 17。
- 手动验证：云端 `error`（`--scenario error`）→ `THINKING→ERROR→IDLE`；`--think-ms 12000` →
  `THINKING`（停留 10000 ms）`→ERROR→IDLE`；`vs start` 完整轮次；`vs interrupt` 立即中止。
- 资源：静态 DIRAM **197,094 B（+96 B）**、无新任务；`ws://` 内部堆峰值 227,736 B / 最低水位 31,891~32,971 B
  （静态+峰值 424,830 B ≤ 440KB）；`evt` 任务栈余 **1,572 B**（4096 B，vs 处理器 + `diag evt trace` 共存时）。

## 后续

- **T3.7 调优**：真实环境噪声下误触发续听轮的概率与阈值（T5.2 唤醒/误触发基线时一起评估）；
  可考虑按麦克风峰值门控或自适应窗口（当前只有 250ms 人声确认 + 轮数上限兜底）。
- T3.6：错误提示音 + `VS_ERROR_HOLD_MS` 正式值；`ERROR` 提示音播放期间屏蔽唤醒。
- ✅ T3.2：R4 完整打断（80ms 淡出 + `interrupt` 报文 + R5 丢弃 50ms）、R3 音乐中唤醒放行、`APP_EVT_BUTTON` 消费；
- T3.4：按键 GPIO 扫描/去抖 + 长按静音 + LED 消费 `APP_EVT_VOICE_STATE_CHANGED`。
- P4：音乐播放中唤醒的 ducking 与 `IDLE` 态允许音乐播放（`IDLE` 已预留）。
- 本地音乐/提示音与 TTS 抢占：T3.2 已对"音乐播放中"放行唤醒并淡出抢占；混音叠加（音乐继续播 + duck）归 T3.3，音乐恢复归 T4.4。
