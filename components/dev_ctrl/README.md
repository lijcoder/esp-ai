# dev_ctrl —— 按键 / 静音（L3 能力，T3.4）

## 作用

物理交互入口：GPIO0 按键扫描（短按/长按、去抖）+ 长按静音切换。

**边界**：本组件只负责"采按键、改音量"。会话语义（短按 = 打断/起轮）归 `voice_session`。
两者通过 `APP_EVT_BUTTON` 事件解耦，符合 CONVENTIONS §2「同层走 event_bus」。

> **LED 状态指示已移除（2026-11-02，用户决定）**：板载 WS2812（GPIO48）实测数据解码不可靠
> —— 任何帧（含全黑帧）都显示满白，满白电流（~60mA）叠加 WiFi 启动峰值会让 5V 掉到 BOD 阈值、
> 设备反复复位。已排除寄存器写法/引脚/时序数学/固件版本/RGBW 变体（证据见 `docs/PENDING.md` PD-003）。
> 现在本组件只把 GPIO48 **拉低并保持**（防止上电噪声锁存亮色），不再驱动任何帧。
> 历史实现（bit-bang WS2812 驱动、亮度/时序调试旋钮）见 git `4f5d152` 与 `084ef4f`。

## 对外接口

| 接口 | 说明 |
|---|---|
| `dev_init()` | 初始化按键扫描（须在 `board_init` / `audio_player_init` 之后）；同时把 LED 引脚拉低 |
| `dev_register_cb(cb, arg)` | 可选按键回调（附加观测，与事件并存；在 esp_timer 任务上下文执行） |
| `dev_is_muted()` | 当前是否静音（运行期状态，不持久化） |
| `dev_set_mute(bool)` | 静音切换；`true` 音量→0，`false` 恢复原音量（长按与 `dev mute` 共用入口） |
| `dev_stats_read/ reset()` | 短按/长按/投递失败计数 |

## 依赖

`board`（引脚）、`driver`（GPIO 读）、`esp_timer`（按键扫描）、`event_bus`（投递按键）、`audio_player`（静音改音量）。

## Kconfig

| 项 | 默认 | 说明 |
|---|---|---|
| `CONFIG_DEV_ENABLE` | y | 总开关；关闭后全部接口空实现 |
| `CONFIG_DEV_BUTTON_DEBOUNCE_MS` | 30 | 去抖窗口（10ms 周期采样，连续一致才认边沿） |
| `CONFIG_DEV_BUTTON_LONG_PRESS_MS` | 600 | 长按阈值（达到即触发，释放不再报短按） |

## 关键约束

1. **不新建任务**（CONVENTIONS §4：内部 RAM 水位余量小）。按键扫描用 10ms 周期 `esp_timer`，
   回调只做 GPIO 读 + 整数状态机，不阻塞、不分配内存。ARCH §9.3 原计划的 `dev` 3KB 任务未创建。
2. **短按/长按都投 `APP_EVT_BUTTON`**（payload 为 T3.2 定稿的 `app_evt_button_t`）。
   长按的静音语义**在本组件内完成**（ARCH §4.2 分工：`voice_session` 忽略长按）。
3. **静音不写 NVS**：只改运行时音量，`s_saved_volume` 保存原值供恢复；重启按 NVS 配置恢复。
4. **静音与 LED 完全解耦**：LED 移除后长按静音仍照常工作（静音只改 `audio_player` 音量）。
5. **GPIO48 必须保持低电平**：不受驱动时 WS2812 可能因上电噪声锁存亮色 → 满白电流 → BOD 复位。
   `dev_init()` 里 `gpio_config(OUTPUT)` + `gpio_set_level(0)` 是安全措施，不要删。

## 后续

- 若将来硬件侧解决 LED 电平问题（先量 VDD：3.3V 还是 5V；5V 供电 + 3.3V 数据属 VIH 不足，需电平转换），
  可从 git `4f5d152` 取回 WS2812 bit-bang 驱动（当时静态只 +276 B）并重新评估；
- 产品化换独立按键引脚（当前用板载 BOOT，见 HARDWARE §2）；
- T3.5 提示音迁 `assets` 分区时，按键音（"叮"）可复用 `CH_PROMPT`。
