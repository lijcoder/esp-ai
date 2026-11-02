# dev_ctrl —— 按键 / LED / 静音（L3 能力，T3.4）

## 作用

物理交互入口：GPIO0 按键扫描（短按/长按、去抖）、WS2812 LED 会话状态指示、长按静音切换。

**边界**：本组件只负责"采按键、亮灯、改音量"。会话语义（短按 = 打断/起轮）归 `voice_session`。
两者通过 `APP_EVT_BUTTON` 事件解耦，符合 CONVENTIONS §2「同层走 event_bus」。

## 对外接口

| 接口 | 说明 |
|---|---|
| `dev_init()` | 初始化按键扫描 + LED；须在 `board_init` / `audio_player_init` / `vs_init` 之后 |
| `dev_register_cb(cb, arg)` | 可选按键回调（附加观测，与事件并存；在 esp_timer 任务上下文执行） |
| `dev_is_muted()` | 当前是否静音（运行期状态，不持久化） |
| `dev_set_mute(bool)` | 静音切换；`true` 音量→0，`false` 恢复原音量（长按与 `dev mute` 共用入口） |
| `dev_led_set_mode(mode)` | 强制 LED 模式（`DEV_LED_AUTO` 恢复跟随会话状态；console 探针用） |
| `dev_led_get_mode()` | 当前 LED 模式 |
| `dev_stats_read/ reset()` | 短按/长按/投递失败/LED 帧计数 |

## 依赖

`board`（引脚）、`driver`（GPIO 读写/配置）、`esp_timer`（扫描与闪烁计时）、`event_bus`（投递按键、订阅状态）、`audio_player`（静音改音量）。
**不依赖 `esp_driver_rmt`**（原因见「关键约束」第 4 条）。

## Kconfig

| 项 | 默认 | 说明 |
|---|---|---|
| `CONFIG_DEV_ENABLE` | y | 总开关；关闭后全部接口空实现 |
| `CONFIG_DEV_BUTTON_DEBOUNCE_MS` | 30 | 去抖窗口（10ms 周期采样，连续一致才认边沿） |
| `CONFIG_DEV_BUTTON_LONG_PRESS_MS` | 600 | 长按阈值（达到即触发，释放不再报短按） |
| `CONFIG_DEV_LED_ENABLE` | y | LED 与静音能力开关 |
| `CONFIG_DEV_LED_BRIGHTNESS` | 24 | LED 亮度 0~255（室内取小值避免刺眼） |

## 关键约束

1. **不新建任务**（CONVENTIONS §4：内部 RAM 水位余量极小）。
   - 按键扫描：10ms 周期 `esp_timer`，回调只做 GPIO 读 + 整数状态机；
   - LED 闪烁：25ms 周期 `esp_timer`，**静态色时回调一次比较后立即返回**（不碰 GPIO）；
   - 状态订阅回调在 `evt` 任务上下文，只做赋值 + 一次非阻塞 LED 帧发送。
   - ARCH §9.3 原计划的 `dev` 3KB 任务**未创建**，按本约束改为定时器方案。
2. **短按/长按都投 `APP_EVT_BUTTON`**（payload 为 T3.2 定稿的 `app_evt_button_t`）。
   长按的静音语义**在本组件内完成**（ARCH §4.2 分工：`voice_session` 忽略长按）。
3. **静音不写 NVS**：只改运行时音量，`s_saved_volume` 保存原值供恢复；重启按 NVS 配置恢复。
4. **LED 驱动用周期精确 bit-bang，不用 RMT**（`ws2812_led.c`）。这不是偏好，是被内存预算逼出来的：
   先用 `esp_driver_rmt` + copy encoder 实现并编译验证，实测静态 DIRAM 从 199,094 → **201,978 B（+2,884 B）**
   —— RMT 的 ISR/事务函数必须 `IRAM_ATTR`（实占 2,244 B），运行时还要分配通道/symbol 缓冲（~0.5KB），
   合计吃掉 ~3.4KB 内部池，会把 `ws://` 堆水位从 32,983 B 压到 ~29.6KB、**击穿「wss 握手期 ≥30KB」硬约束**。
   现方案：关中断 + 读 CCOUNT 周期计数精确延时，24bit ≈ 30µs 关中断（240MHz 下 1 cycle = 4.17ns，
   容差 ±150ns 充裕），**静态只 +276 B**，且不引入第三方组件（CONVENTIONS §9）。
   时序取值刻意落在 datasheet 窗口偏低端（`wait_cycles` 的读 CCOUNT 开销会叠加到高电平段）：
   T0H 350ns / T0L 900ns / T1H 750ns / T1L 500ns / 复位 60µs。
5. **帧发送是同步阻塞的**（~30µs 关中断）：只在状态迁移或闪烁边沿调用（最快 4Hz，即 8 次/秒），
   对 WiFi/I2S（DMA 缓冲 ms 级）无影响。**不要把它改到高频路径**（如逐帧呼吸灯），
   那会让关中断变得频繁；需要渐变效果时必须重新评估（见「后续」）。
6. **调用方上下文**：`evt` 任务（状态订阅）、`esp_timer` 任务（扫描/闪烁）、console REPL 任务（`dev mute`/`dev led`）
   都会调 `ws2812_set_rgb()`。它是自包含的（无锁、无队列、无共享缓冲），这些上下文互不干扰。

## LED 状态映射（`AUTO` 模式）

| 会话状态 | 颜色 |
|---|---|
| `IDLE` | 绿 常亮 |
| `LISTENING` | 青 常亮 |
| `THINKING` | 黄 2Hz 闪烁 |
| `SPEAKING` | 蓝 常亮 |
| `FOLLOW_UP` | 绿 2Hz 闪烁（续听窗口） |
| `ERROR` | 红 4Hz 闪烁 |
| 静音中 | 白 常亮（优先级最高，覆盖以上全部） |

## 后续

- T3.5 提示音迁 `assets` 分区时，按键音（"叮"）可复用 `CH_PROMPT`；
- 产品化换独立按键引脚（当前用板载 BOOT，见 HARDWARE §2）；
- LED 做呼吸/渐变效果**必须重新评估关中断代价**（现方案每帧 ~30µs 关中断，只适配 ≤4Hz 的状态指示），
  不建议在本组件里加刷新频率；需要渐变时可考虑「一帧内插值多个 24bit 帧」的批量方案，或回到 RMT + 更严的内存裁剪。
- **亮度旋钮待定**：当前只有编译期 `CONFIG_DEV_LED_BRIGHTNESS`（默认 24/255）。用户 2026-11-02 反馈「太亮晃眼」，
  计划（等开发板回手边再做，见 `docs/PENDING.md` PD-001）：默认降到 8 左右 + 白灯单独降权 + 运行期 `dev bright <0~255>` + 写 NVS。
