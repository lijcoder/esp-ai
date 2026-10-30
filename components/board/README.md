# board —— 板级硬件抽象（L0 板级/驱动层）

**作用**：集中定义 v1 样机的引脚映射，并初始化/读取板载按键。上层组件（`audio_io`、后续 `dev_ctrl`）只通过这里拿引脚与按键状态，不直接写 GPIO 数字。

## 对外接口

| 接口 | 说明 |
|---|---|
| `board_init()` | 初始化按键 GPIO（GPIO0 上拉输入），打印引脚表 |
| `board_button_pressed()` | 按键是否按下（低电平有效） |

## 引脚

| 功能 | GPIO |
|---|---|
| INMP441 SCK / WS / SD | 5 / 6 / 4 |
| MAX98357A BCLK / LRC / DIN | 41 / 42 / 40 |
| WS2812 状态灯 | 48（定义保留，驱动待 `dev_ctrl`） |
| 用户按键 | 0（板载 BOOT） |

详细接线、电源与 EMI 要求见 [docs/HARDWARE.md](../../docs/HARDWARE.md)。

## 依赖与约定

- 依赖：`driver`（GPIO）。
- 本组件**不做业务逻辑**，不碰 I2S/音频。
- 新增硬件（LED 驱动、电池 ADC）时在此组件扩展，并同步更新 HARDWARE.md。

对应架构：[docs/ARCHITECTURE.md](../../docs/ARCHITECTURE.md) §2（硬件约束）、§4（L0 板级）。
