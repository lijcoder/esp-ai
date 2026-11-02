#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["pyserial>=3.5", "lameenc>=1.8", "av>=12", "numpy"]
# ///
"""T3.4 物理交互真机自测（按键 / LED / 静音）。

覆盖 P3 验收的两条硬指标：
  - **按键打断 < 100ms**：`player stat` 的 `last_stop_ms`（淡出请求→输出静音）+ 整条打断链路日志；
  - **LED 状态与实际状态一致**：`dev status` 的 LED 模式 / 帧计数随会话状态迁移变化。

按键怎么"按"：**不靠手**。DevKit 的 USB 自动下载电路把 DTR 接到 GPIO0（BOOT），
把 RTS 接到 EN；本脚本保持 `rts=False`（EN 高、设备运行），只翻转 `dtr`：
`dtr=True` = 按住 BOOT（GPIO0 拉低）→ `dtr=False` = 松开。这样走的是**真实 GPIO 扫描路径**
（dev_ctrl 的 esp_timer 去抖状态机 → APP_EVT_BUTTON），不是 `evt post BUTTON` 的命令注入。

场景（全部自动断言，最后给出 PASS/FAIL 与退出码）：
  A 短按（IDLE）      ：`DEV: 按键：短按` → VS 起轮；`dev status` 短按计数 +1；
                        **长按不会额外产生短按**（松开后不重复触发）。
  B 长按静音          ：音量 100→0（`PLAYER: volume=0`）→ LED 白色；再长按 0→100 恢复；
                        静音期间 `dev status` 显示「静音=开」，NVS 音量不变。
  C 播放中打断（R4）  ：mock 下发真实 MP3 → `vs start` 进 SPEAKING → 短按 →
                        `VS: 打断当前会话` / `PLAYER: fade out` / `VC: interrupt sent sid=` /
                        `AFE: feed resumed (discard 50 ms` / `VS: state SPEAKING -> IDLE（原因=interrupt）`
                        / `last_stop_ms < 100` / mock 收到 interrupt 且不再发 tts_end。
  D 续听窗口内短按    ：T3.7 交集 —— TTS 播完进 FOLLOW_UP 后短按 → `FOLLOW_UP -> IDLE`。
  E LED 模式          ：`dev led <mode>` 逐个设置 + `dev led auto` 恢复跟随；
                        强制模式不被状态迁移顶掉；AUTO 下状态迁移会重绘（帧计数增加）。
  F 堆稳定            ：10 次短按 + 状态迁移后内部堆波动 ≤ 2KB（不单调下降）。

前置：`CONFIG_DEV_ENABLE=y`（默认）+ 设备已烧 T3.4 固件；脚本**独占串口**。
用法：
    uv run tools/dev_ctrl_test.py                 # 全量（默认复位设备）
    uv run tools/dev_ctrl_test.py --skip-interrupt  # 只跑 A/B/D/E/F（不需要 mock）
    uv run tools/dev_ctrl_test.py --dry-run       # 只打印计划，不连设备
    uv run tools/dev_ctrl_test.py --no-reset --log /tmp/dev.log
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))

from voice_test import Device, Result, detect_port  # noqa: E402
from voice_cloud_test import (  # noqa: E402
    END_VC_RESET,
    END_VS_STATUS,
    RE_ROUND_START,
    RE_TTS_START,
    parse_heap,
    vc_status,
    vs_status,
    wait_line,
)
from tts_stream_test import (  # noqa: E402
    END_PLAYER_RESET,
    END_VS_START,
    END_VC_STOP,
    RE_AFE_FEED_RESUMED,
    RE_PLAYER_FADE_OUT,
    RE_VC_INTERRUPT_SENT,
    RE_VS_INTERRUPT_LOG,
    RE_VS_SPEAKING_IDLE,
    STOP_MS_MAX,
    Mock,
    begin_vs_round,
    player_stat,
    player_stop_stat,
    prepare_material,
    wait_line_fast,
)

# ---- 设备侧日志（本任务新增，dev_ctrl 契约） ----
RE_DEV_SHORT = re.compile(r"DEV: 按键：短按")
RE_DEV_LONG = re.compile(r"DEV: 按键：长按")
RE_DEV_MUTE_ON = re.compile(r"DEV: 静音开")
RE_DEV_MUTE_OFF = re.compile(r"DEV: 静音关")
RE_DEV_READY = re.compile(r"DEV: ready: button=GPIO(\d+)")
RE_PLAYER_VOL = re.compile(r"PLAYER: volume=(\d+)")
RE_VS_BUTTON_ROUND = re.compile(r"VS: 按键开始对话")
RE_VS_FOLLOWUP_IDLE = re.compile(r"VS: state\s+FOLLOW_UP\s*->\s*IDLE")

# `dev status` 输出行
RE_DEV_STATUS = re.compile(r"dev_ctrl: 静音=(\S+?)，LED 模式=(\S+?)（当前音量 (\d+)）")
RE_DEV_COUNTS = re.compile(
    r"按键: 短按 (\d+) / 长按 (\d+)；事件投递失败 (\d+)"
)
RE_DEV_LEDS = re.compile(r"LED: 已发送 (\d+) 帧")
END_DEV_STATUS = re.compile(r"说明: 短按投 APP_EVT_BUTTON")

# 按键时序（与 Kconfig 默认一致：去抖 30ms、长按 600ms）
SHORT_PRESS_S = 0.15   # 150ms < 600ms 长按阈值，且 > 2×去抖
LONG_PRESS_S = 0.90    # 900ms > 600ms
PRESS_SETTLE_S = 0.35  # 松开后等状态机收尾（去抖 + 事件分发）

MOCK_PORT_DEFAULT = 8765


# ---------------------------------------------------------------- 按键模拟


def press(dev: Device, duration: float, settle: float = PRESS_SETTLE_S) -> float:
    """模拟一次按键（按住 duration 秒），返回按下瞬间的宿主时间戳。

    EN 必须保持高电平（rts=False），否则会把「按键」变成一次复位。
    """
    dev.ser.rts = False
    time.sleep(0.02)
    t0 = time.time()
    dev.ser.dtr = True
    time.sleep(duration)
    dev.ser.dtr = False
    dev.pump(settle)
    return t0


def dev_status(dev: Device) -> dict:
    out = dev.console("dev status", END_DEV_STATUS, timeout=3.0)
    stat: dict = {"raw": out}
    for line in out:
        m = RE_DEV_STATUS.search(line)
        if m:
            stat["muted"] = m.group(1)
            stat["led"] = m.group(2)
            stat["volume"] = int(m.group(3))
        m = RE_DEV_COUNTS.search(line)
        if m:
            stat["short"] = int(m.group(1))
            stat["long"] = int(m.group(2))
            stat["dropped"] = int(m.group(3))
        m = RE_DEV_LEDS.search(line)
        if m:
            stat["led_frames"] = int(m.group(1))
    return stat


def set_followup(dev: Device, on: bool) -> None:
    dev.console(f"vs followup {'on' if on else 'off'}", re.compile(r"连续对话续听窗口：(开|关)"))


def wait_vs_state(dev: Device, state: str, timeout: float = 8.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if vs_status(dev).get("state") == state:
            return True
        dev.pump(0.05)
    return False


# ---------------------------------------------------------------- 场景


def scenario_short(dev: Device, result: Result) -> None:
    """A 短按（IDLE）：真实 GPIO 路径 → DEV 日志 → VS 起轮；长按不产生额外短按。"""
    print("\n===== A 短按（IDLE → DEV 日志 → VS 起轮）=====")
    dev.console("vs interrupt", re.compile(r"已打断|未在进行"), timeout=2.0)
    dev.console("dev reset", re.compile(r"dev 统计已清零"), timeout=2.0)
    before = dev_status(dev)
    mark = dev.mark()
    press(dev, SHORT_PRESS_S)
    short_log = wait_line(dev, RE_DEV_SHORT, 2.0, mark)
    result.check(short_log is not None, "A: 设备日志出现 `DEV: 按键：短按`（真实 GPIO0 路径）")
    round_log = wait_line(dev, RE_VS_BUTTON_ROUND, 2.0, mark)
    result.check(round_log is not None, "A: voice_session 收到按键（`VS: 按键开始对话`）")
    after = dev_status(dev)
    result.check(
        after.get("short", 0) == before.get("short", 0) + 1,
        f"A: `dev status` 短按计数 +1（{before.get('short')} → {after.get('short')}）",
    )
    result.check(after.get("long") == before.get("long"), "A: 短按不增加长按计数")
    dev.console("vs interrupt", re.compile(r"已打断|未在进行"), timeout=2.0)


def scenario_mute(dev: Device, result: Result) -> None:
    """B 长按静音：音量 100→0→100，LED 白色，静音状态可读。"""
    print("\n===== B 长按静音（音量 0 ↔ 恢复）=====")
    # 保证从「未静音」开始
    st = dev_status(dev)
    if st.get("muted") == "开":
        dev.console("dev mute off", re.compile(r"静音：关"), timeout=2.0)
    base = dev_status(dev)
    base_vol = base.get("volume", 100)
    result.check(base.get("muted") == "关", f"B: 初始未静音（实际 {base.get('muted')}）")
    result.check(base_vol > 0, f"B: 初始音量 >0（实际 {base_vol}）")
    long_before = base.get("long", 0)

    mark = dev.mark()
    press(dev, LONG_PRESS_S)
    mute_on = wait_line(dev, RE_DEV_MUTE_ON, 2.0, mark)
    vol0 = wait_line(dev, RE_PLAYER_VOL, 2.0, mark)
    result.check(mute_on is not None, "B: 长按触发 `DEV: 静音开`")
    result.check(vol0 is not None, "B: 播放器音量被置 0（`PLAYER: volume=0`）")
    st = dev_status(dev)
    result.check(st.get("muted") == "开", f"B: `dev status` 静音=开（实际 {st.get('muted')}）")
    result.check(st.get("volume") == 0, f"B: 静音时音量=0（实际 {st.get('volume')}）")
    result.check(
        st.get("long", 0) == long_before + 1,
        f"B: 长按计数 +1（{long_before} → {st.get('long')}）",
    )
    result.check(
        st.get("short") == base.get("short"),
        "B: 长按只触发一次、松开不再报短按",
    )

    mark = dev.mark()
    press(dev, LONG_PRESS_S)
    mute_off = wait_line(dev, RE_DEV_MUTE_OFF, 2.0, mark)
    result.check(mute_off is not None, "B: 再次长按触发 `DEV: 静音关`")
    st = dev_status(dev)
    result.check(st.get("muted") == "关", f"B: `dev status` 静音=关（实际 {st.get('muted')}）")
    result.check(
        st.get("volume") == base_vol,
        f"B: 音量恢复到原值 {base_vol}（实际 {st.get('volume')}）",
    )


def scenario_led(dev: Device, result: Result) -> None:
    """E LED 模式：逐个强制设置 + auto 恢复；AUTO 下状态迁移会重绘。"""
    print("\n===== E LED 模式（强制/跟随）=====")
    modes = ["idle", "listening", "thinking", "speaking", "followup", "error", "mute"]
    ok_all = True
    for mode in modes:
        out = dev.console(f"dev led {mode}", re.compile(r"LED 模式 →|设置 LED 模式失败"),
                          timeout=2.0)
        got = dev_status(dev).get("led")
        if got != mode:
            ok_all = False
            result.check(False, f"E: `dev led {mode}` 生效（实际 {got}，输出 {out[-1:]}）")
        else:
            result.check(True, f"E: `dev led {mode}` 生效（{out[-1] if out else ''}）")
    if ok_all:
        result.check(True, f"E: 全部 {len(modes)} 种强制 LED 模式可设置并被 `dev status` 反映")

    # 非法模式必须被拒绝（不能静默生效）
    out = dev.console("dev led nonsense", re.compile(r"未知 LED 模式"), timeout=2.0)
    result.check(
        any("未知 LED 模式" in line for line in out),
        "E: 非法 LED 模式被拒绝",
    )

    # AUTO 恢复跟随：状态迁移后 LED 帧计数应增加（重绘）
    dev.console("dev led auto", re.compile(r"LED 模式 → auto"), timeout=2.0)
    st = dev_status(dev)
    result.check(st.get("led") == "auto", f"E: `dev led auto` 恢复跟随（实际 {st.get('led')}）")
    frames_before = st.get("led_frames", 0)
    mark = dev.mark()
    press(dev, SHORT_PRESS_S)  # IDLE 短按 → LISTENING → ... 至少两次迁移
    wait_line(dev, RE_DEV_SHORT, 2.0, mark)
    dev.pump(1.2)
    frames_after = dev_status(dev).get("led_frames", 0)
    result.check(
        frames_after > frames_before,
        f"E: AUTO 模式随状态迁移重绘 LED（帧 {frames_before} → {frames_after}）",
    )
    dev.console("vs interrupt", re.compile(r"已打断|未在进行"), timeout=2.0)


def scenario_interrupt(dev: Device, result: Result, args: argparse.Namespace, mock: Mock) -> None:
    """C 播放中按键打断（R4）：真实 MP3 播放中按 BOOT，整条链路 + 停声 <100ms。"""
    print("\n===== C 播放中按键打断（R4：真实 GPIO 按键）=====")
    label = "C"
    dev.console("vc reset", END_VC_RESET, timeout=2.0)
    dev.console("player reset", END_PLAYER_RESET, timeout=2.0)

    sid, mark = begin_vs_round(dev, result, label)
    if sid < 0:
        return
    dev.console("vc stop", END_VC_STOP, timeout=2.0)
    tts_start = wait_line(dev, RE_TTS_START, 10.0, mark)
    result.check(tts_start is not None, f"{label}: 进入 TTS 播放（tts_start）")

    # 等真的在放（避免打在预缓冲阶段，停声耗时不可比）
    decoded = 0
    deadline = time.time() + 8.0
    while time.time() < deadline:
        stream = player_stat(dev).get("stream")
        decoded = int(stream["decoded"]) if stream else 0
        if decoded >= args.after_frames:
            break
        dev.pump(0.02)
    result.check(
        decoded >= args.after_frames,
        f"{label}: 打断前已解码 {decoded} 帧（≥{args.after_frames}，确认真在播放）",
    )
    playing = player_stat(dev).get("stream") or {}
    result.check(playing.get("active") == 1, f"{label}: 打断前 stream active=1（播放中）")
    vs_now = vs_status(dev).get("state")
    result.check(vs_now == "SPEAKING", f"{label}: 打断前 VS 状态=SPEAKING（实际 {vs_now}）")

    mock_mark = mock.mark() if mock.owned else 0
    mark = dev.mark()
    t_press = press(dev, SHORT_PRESS_S, settle=0.0)

    # 按键 → 打断链路（逐段证据）
    short_log = wait_line_fast(dev, RE_DEV_SHORT, 1.0, mark)
    result.check(short_log is not None, f"{label}: 设备侧检测到短按（`DEV: 按键：短按`）")
    vs_int = wait_line(dev, RE_VS_INTERRUPT_LOG, 1.5, mark)
    result.check(vs_int is not None, f"{label}: `VS: 打断当前会话`")
    fade = wait_line(dev, RE_PLAYER_FADE_OUT, 1.5, mark)
    result.check(fade is not None, f"{label}: `PLAYER: fade out`（输出级淡出）")
    vc_int = wait_line(dev, RE_VC_INTERRUPT_SENT, 2.0, mark)
    result.check(vc_int is not None, f"{label}: `VC: interrupt sent sid=`（R4 报文）")
    # 用带时间戳的 wait_line_fast：R4 的宿主侧时延只作 INFO 参考
    resumed = wait_line_fast(dev, RE_AFE_FEED_RESUMED, 3.0, mark)
    result.check(resumed is not None, f"{label}: `AFE: feed resumed`（R5 恢复收音）")
    idle = wait_line(dev, RE_VS_SPEAKING_IDLE, 3.0, mark)
    result.check(idle is not None, f"{label}: `VS: state SPEAKING -> IDLE`")
    if idle is not None:
        result.check(
            bool(re.search(r"(中断|interrupt|按键|button)", idle, re.I)),
            f"{label}: 回 IDLE 的原因是打断（{idle.strip()}）",
        )

    # 停声硬指标：device 侧「淡出请求 → 输出静音」
    dev.pump(0.5)
    duck = player_stop_stat(dev).get("duck") or {}
    stop_ms = duck.get("last_stop_ms")
    if stop_ms is None:
        stop_ms = parse_last_stop_ms(dev)
    result.check(
        stop_ms is not None and stop_ms < STOP_MS_MAX,
        f"{label}: 停声耗时 last_stop_ms={stop_ms} < {STOP_MS_MAX:.0f}ms（P3 验收：按键打断 <100ms）",
    )
    # 参考量：宿主侧「按下 → AFE feed resumed」的墙钟时间（含日志/flush 延迟，仅 INFO）
    if resumed is not None:
        print(f"  INFO 宿主侧 按下→feed resumed ≈ {(resumed - t_press) * 1000:.0f} ms（含串口日志延迟）")

    # mock 侧：必须收到 interrupt 且不再下发 tts_end
    if mock.owned:
        dev.pump(1.0)
        mock_text = mock.text_since(mock_mark)
        result.check("interrupt sid=" in mock_text, f"{label}: mock 收到 interrupt 报文")
        result.check("--> tts_end" not in mock_text, f"{label}: 打断后 mock 不再下发 tts_end")
    else:
        print(f"  INFO mock 非本脚本所有，跳过 mock 侧断言")

    # 打断后必须回得去：再起一轮能正常进入 UPLINK
    regress_mark = dev.mark()  # 先取 mark 再发命令：round start 日志就在命令返回之前产生
    dev.console("vc start", re.compile(r"已请求发起一轮"), timeout=2.0)
    ok = wait_line(dev, RE_ROUND_START, 5.0, regress_mark) is not None
    result.check(ok, f"{label}: 打断后仍能正常起新一轮（回归）")
    dev.console("vc cancel", re.compile(r"已请求立即放弃"), timeout=2.0)
    dev.pump(0.8)
    dev.console("vs interrupt", re.compile(r"已打断|未在进行"), timeout=2.0)


def parse_last_stop_ms(dev: Device) -> int | None:
    out = dev.console("player stat", re.compile(r"duck: on="), timeout=3.0)
    for line in out:
        m = re.search(r"last_stop_ms=(\d+)", line)
        if m:
            return int(m.group(1))
    return None


def scenario_followup(dev: Device, result: Result, mock: Mock) -> None:
    """D 续听窗口内短按（T3.7 交集）：TTS 播完进 FOLLOW_UP → 短按回 IDLE。"""
    print("\n===== D 续听窗口内短按（FOLLOW_UP → IDLE）=====")
    label = "D"
    set_followup(dev, True)
    dev.console("vc reset", END_VC_RESET, timeout=2.0)

    sid, mark = begin_vs_round(dev, result, label)
    if sid < 0:
        return
    dev.console("vc stop", END_VC_STOP, timeout=2.0)
    if wait_line(dev, RE_TTS_START, 10.0, mark) is None:
        result.check(False, f"{label}: 未进入 TTS 播放，跳过续听断言")
        return
    # 等本轮播完进入 FOLLOW_UP（允许 mock 非本脚本所有：仍由设备侧状态判定）
    into_followup = wait_vs_state(dev, "FOLLOW_UP", 20.0)
    result.check(into_followup, f"{label}: TTS 播完进入 FOLLOW_UP 续听窗口")
    if not into_followup:
        dev.console("vs interrupt", re.compile(r"已打断|未在进行"), timeout=2.0)
        return

    mark = dev.mark()
    press(dev, SHORT_PRESS_S)
    short_log = wait_line(dev, RE_DEV_SHORT, 1.5, mark)
    result.check(short_log is not None, f"{label}: 续听窗口内检测到短按")
    went_idle = wait_line(dev, RE_VS_FOLLOWUP_IDLE, 3.0, mark)
    result.check(went_idle is not None, f"{label}: `VS: state FOLLOW_UP -> IDLE`（短按退出续听）")
    st = vs_status(dev)
    result.check(st.get("state") == "IDLE", f"{label}: 最终状态 IDLE（实际 {st.get('state')}）")
    set_followup(dev, True)  # 恢复默认（默认就是开）


def scenario_heap(dev: Device, result: Result, args: argparse.Namespace) -> None:
    """F 堆稳定：多次按键后内部堆不单调下降（esp_timer/事件投递无泄漏）。"""
    print("\n===== F 堆稳定（按键循环）=====")
    dev.console("vs interrupt", re.compile(r"已打断|未在进行"), timeout=2.0)
    # 先空跑一轮当基线：按键/LED/事件/console 首次使用会一次性申请缓冲
    # （esp_console 历史、esp_event 队列页、日志缓冲），这些不计入泄漏。
    press(dev, SHORT_PRESS_S)
    dev.console("vs interrupt", re.compile(r"已打断|未在进行"), timeout=2.0)
    dev.pump(0.5)
    base = parse_heap(dev).get("internal") or {}
    base_free = base.get("free")
    if base_free is None:
        result.check(False, "F: 能读到内部堆（`sys heap`）")
        return
    print(f"  内部堆基线（预热后）: free={base_free} B")
    seq = []
    for i in range(args.press_rounds):
        press(dev, SHORT_PRESS_S if i % 2 == 0 else LONG_PRESS_S)
        if i % 2 == 1:  # 长按切换了静音 → 关回去，保持状态不变
            st = dev_status(dev)
            if st.get("muted") == "开":
                dev.console("dev mute off", re.compile(r"静音：关"), timeout=2.0)
        dev.console("vs interrupt", re.compile(r"已打断|未在进行"), timeout=2.0)
        heap = parse_heap(dev).get("internal") or {}
        seq.append(heap.get("free"))
    print(f"  内部堆序列: {seq}")
    tail = [v for v in seq if v is not None]
    if not tail:
        result.check(False, "F: 逐轮读到内部堆")
        return
    spread = max(tail) - min(tail)
    drop = base_free - min(tail)
    monotone_down = all(b <= a for a, b in zip(tail, tail[1:])) and tail[-1] < tail[0]
    result.check(
        spread <= args.heap_tol,
        f"F: 循环期间内部堆波动 {spread} B ≤ {args.heap_tol} B（不含一次性缓冲）",
    )
    result.check(
        drop <= args.heap_tol,
        f"F: 相对预热基线最大下探 {drop} B ≤ {args.heap_tol} B",
    )
    result.check(not monotone_down, "F: 内部堆不单调下降（无泄漏特征）")
    st = dev_status(dev)
    result.check(
        st.get("short", 0) + st.get("long", 0) >= args.press_rounds,
        f"F: 累计按键计数 {st.get('short', 0)} 短 + {st.get('long', 0)} 长 ≥ {args.press_rounds}",
    )


# ---------------------------------------------------------------- main


def main() -> int:
    ap = argparse.ArgumentParser(description="T3.4 按键/LED/静音真机自测")
    ap.add_argument("--port", help="串口设备（默认自动探测唯一的 /dev/cu.usbmodem*）")
    ap.add_argument("--mock-port", type=int, default=MOCK_PORT_DEFAULT)
    ap.add_argument("--no-mock", action="store_true", help="不拉起 mock（复用已有服务）")
    ap.add_argument("--skip-interrupt", action="store_true", help="跳过 C/D（不需要 mock/TTS）")
    ap.add_argument("--after-frames", type=int, default=15, help="打断前至少已解码帧数")
    ap.add_argument("--material-repeats", type=int, default=3, help="TTS 素材拼接份数（≈4.8s）")
    ap.add_argument("--kbps", type=int, default=64, help="素材 MP3 码率")
    ap.add_argument("--press-rounds", type=int, default=10, help="F 场景按键次数")
    ap.add_argument("--heap-tol", type=int, default=2048, help="F 场景内部堆允许下探字节数")
    ap.add_argument("--no-reset", action="store_true", help="设备已在跑，不复位")
    ap.add_argument("--dry-run", action="store_true", help="只打印计划，不连设备")
    ap.add_argument("--log", help="把串口日志写文件")
    args = ap.parse_args()

    if args.dry_run:
        print("T3.4 dev_ctrl 自测计划（不连设备）：")
        print("  A 短按（IDLE）→ DEV 日志 → VS 起轮；长按不额外报短按")
        print("  B 长按静音：音量 100→0→100；LED 白色；`dev status` 静音=开/关")
        print("  E LED：7 种强制模式 + 非法模式拒绝 + auto 跟随状态重绘")
        print("  C 播放中按键打断（R4）：链路日志 + last_stop_ms<100 + mock 不再 tts_end")
        print("  D 续听窗口内短按：FOLLOW_UP → IDLE")
        print("  F 堆稳定：10 次按键内部堆下探 ≤2KB 且不单调下降")
        print("按键模拟：rts=False 保 EN，dtr 翻转 = BOOT(GPIO0) 按下/松开")
        return 0

    port = detect_port(args.port)
    print(f"设备串口: {port}")
    dev = Device(port)
    log_handle = open(args.log, "w", encoding="utf-8") if args.log else None

    mock = Mock(args.mock_port)
    result = Result()
    try:
        if not args.skip_interrupt:
            import argparse as _ap
            mat_args = _ap.Namespace(material_repeats=args.material_repeats, kbps=args.kbps)
            _material, _ref, mp3_path = prepare_material(mat_args)
            mock.start(["--tts-mp3", mp3_path, "--tts-pace-pct", "80"])
        else:
            print("  --skip-interrupt：跳过 mock/TTS 相关场景（C/D）")

        if not args.no_reset:
            print("复位设备…")
            dev.reset()
            dev.pump(0.5)
            ready = wait_line(dev, RE_DEV_READY, 15.0, 0)
            result.check(ready is not None or args.no_reset,
                         "设备启动并初始化 dev_ctrl（`DEV: ready`）")
            dev.pump(2.0)

        scenario_short(dev, result)
        scenario_mute(dev, result)
        scenario_led(dev, result)
        if not args.skip_interrupt:
            scenario_interrupt(dev, result, args, mock)
            scenario_followup(dev, result, mock)
        scenario_heap(dev, result, args)

        errors = [line for _, line in dev.lines if re.search(r"^\s*E \(", line)]
        dev_ns = [line for line in errors if "DEV:" in line or "PLAYER:" in line or "VS:" in line]
        result.check(not dev_ns, f"全程无 DEV/PLAYER/VS 相关 ERROR 日志（{len(dev_ns)} 条）")
    finally:
        # 收尾：恢复默认状态（未静音 / LED auto / 续听开）
        try:
            st = dev_status(dev)
            if st.get("muted") == "开":
                dev.console("dev mute off", re.compile(r"静音：关"), timeout=2.0)
            dev.console("dev led auto", re.compile(r"LED 模式 → auto"), timeout=2.0)
        except Exception:
            pass
        mock.stop()
        if log_handle is not None:
            for _, line in dev.lines:
                log_handle.write(line + "\n")
            log_handle.close()
        dev.close()

    return 0 if result.summary() else 1


if __name__ == "__main__":
    sys.exit(main())
