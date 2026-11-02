#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["pyserial>=3.5"]
# ///
"""T3.6 错误提示真机自测：mock 注入云端错误码/错误时机 → 串口断言异常反馈链路。

一条命令完成：按场景起/重启本地 mock（错误码、报错时机、坏 codec）→ 复位设备并等 WS READY
→ 逐场景触发异常（唤醒/中途断网/云端 error/THINKING 超时/设备侧 TTS 失败/打断/正常轮/
SPEAKING 中报错）→ 断言设备日志（`VS:`/`FB:`/`PLAYER:`）与 `fb status`/`vs status`/`mic stat`
字段 → 逐场景 PASS/FAIL + 机器可读汇总 + 退出码 0/1。

契约依据（冻结）：`build/t36_contract.md` §3.2（日志正则）、§3.3（diag 输出）、§5.2（场景 A~J）。
断言只消费固件日志/命令输出，不猜字段；日志格式以契约 §3.2 为准（缺字段即 FAIL，便于固件迭代）。

场景（默认全跑，`--scenarios A,C,J` 或 `A-C` 选择）：
  A 起轮前网络未连接：不启 mock，回放 lexin.wav 唤醒 → T3.6 离线唤醒语义
    （不播"我在呢"，直接播「网络未连接」→ ERROR → IDLE；无 `prompt done` 日志）；
  B 会话中途断网：mock 正常 + `vs start` 起轮 → 1s 后 `wifi reconnect` → 中途断网反馈；
  C 云端通用 error：mock `--scenario error --error-code 1` → cloud-error 提示音；
  D 云端 TTS 错误：`--error-code 3001` → tts-fail 提示音（「语音播放失败」）；
  E THINKING 超时：mock `--think-ms 12000` → 约 10s 后 timeout 提示音（音频结束→提示音 9.5~13s）；
  F 设备侧 TTS 失败：mock `--tts-bad-codec` → tts-fail 提示音 + 「非 mp3/无法播放」WARN；
  G 打断不误播：正常轮 SPEAKING 中 `vs interrupt` → 窗口内无错误反馈、回 IDLE；
  H 正常轮不误播：正常一轮结束无错误反馈；
  I SPEAKING 中云端报错：`--error-code 3001 --error-stage after-tts-start` → 先停 TTS 流再播提示音；
  J 稳定性：连续 10 次 C 场景 → 堆/PSRAM 波动阈值内、无 panic、`fb` 总播放 +10。

触发方式为什么用 `vs start` + `vc stop`（而不是真唤醒+说话）：错误码/时机要可复现、可定时；
`vs start` 让 voice_session 真的拥有本轮（VS 进 LISTENING），`vc stop` 确定性结束上行
（audio_end 收尾原因 stop-request），云端错误/超时才能走到 ERROR 状态机；场景 A/B 走真实唤
醒与真实 `wifi reconnect` 事件（这两条是产品路径，必须真触发）。

依赖与素材：`tools/ws_mock_server.py`（T3.6 扩展）、`tools/lexin.wav`。脚本**独占串口**
（运行前关掉 `idf.py monitor`）；mock 端口被外部占用时复用外部服务并跳过 mock 侧断言（WARN）。

用法：
    uv run tools/error_prompt_test.py                       # 全场景
    uv run tools/error_prompt_test.py --scenarios A          # 只测离线唤醒反馈（不需要 mock）
    uv run tools/error_prompt_test.py --scenarios C,D,J --log /tmp/error_prompt.log
    uv run tools/error_prompt_test.py --self-test            # 离线自检解析/判定（≥20 项，不连设备）
    uv run tools/error_prompt_test.py --dry-run              # 只检查素材/音量/串口/场景计划（不开串口）
"""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))

# 复用既有串口/素材/断言封装，避免多套实现漂移（模块无 import 副作用）
from voice_test import (  # noqa: E402  （PEP 723 脚本按脚本目录入 sys.path）
    Device,
    Result,
    detect_port,
    get_system_volume,
    preflight_assets,
    set_system_volume,
)

MOCK_SCRIPT = os.path.join(HERE, "ws_mock_server.py")
WAKE_WAV = "lexin.wav"
DEFAULT_MOCK_PORT = 8765
SYSTEM_VOLUME_MAX = 50
HOLD_PROMPT = 0x1  # AUDIO_FE_HOLD_PROMPT：提示音在播（R1）；与 audio_fe.h 一致

SCENARIO_ORDER = ("A", "B", "C", "D", "E", "F", "G", "H", "I", "J")
SCENARIO_TITLES = {
    "A": "起轮前网络未连接（离线唤醒 → 网络未连接反馈）",
    "B": "会话中途断网（wifi reconnect → 网络未连接反馈）",
    "C": "云端通用 error（code=1 → cloud-error 提示音）",
    "D": "云端 TTS 错误（code=3001 → tts-fail 提示音）",
    "E": "THINKING 超时（约 10s → timeout 提示音）",
    "F": "设备侧 TTS 无法播放（坏 codec → tts-fail 提示音）",
    "G": "打断不误播（SPEAKING 中 vs interrupt）",
    "H": "正常轮不误播（一轮正常结束）",
    "I": "SPEAKING 中云端报错（先停 TTS 流再播提示音）",
    "J": "稳定性（连续 10 轮云端 error 反馈）",
}

# 每个场景需要的 mock 参数（A 不启 mock；其余按场景注入错误码/时机/坏 codec）。
MOCK_SPECS: dict[str, list[str]] = {
    "B": [],
    "C": ["--scenario", "error", "--error-code", "1"],
    "D": ["--scenario", "error", "--error-code", "3001"],
    "E": ["--think-ms", "12000"],
    "F": ["--tts-bad-codec"],
    "G": [],
    "H": [],
    "I": ["--scenario", "error", "--error-code", "3001", "--error-stage", "after-tts-start"],
    "J": ["--scenario", "error", "--error-code", "1"],
}

# ---------------------------------------------------------------- 设备日志（契约 §3.2，逐字）
RE_WAKE = re.compile(r"(?:APP|VS): wake detected")
RE_PROMPT_DONE = re.compile(r"(?:APP|VS): prompt done, feed resumed")
RE_VS_STATE = re.compile(
    r"VS: state (\w+) -> (\w+)（sid=(\d+)，停留 (\d+) ms，原因=([^）]+)）")
RE_VS_ENTER_ERROR = re.compile(r"VS: 会话进入 ERROR（([a-z-]+)，code=(-?\d+)）")
RE_FB_START_ANY = re.compile(
    r"VS: 错误反馈（类别=([a-z-]+)，code=(-?\d+)，sid=(\d+)）→ 提示音（R1）")
RE_FB_DONE_ANY = re.compile(
    r"VS: 错误反馈完成（类别=([a-z-]+)，ok=([01])，时长=(\d+) ms）→ IDLE")
RE_FB_WATCHDOG = re.compile(r"VS: 错误反馈看门狗超时（(\d+) ms）→ 强制回 IDLE")
RE_FB_PLAY_ANY = re.compile(
    r"FB: 反馈 kind=([a-z-]+) code=(-?\d+) sid=(\d+) → 播放提示音（([a-z-]+)=(\d+) 样本）")
RE_FB_PROMPT_DONE = re.compile(r"FB: 提示音完成 kind=([a-z-]+) ok=([01]) dur=(\d+) ms")
RE_FB_DEFER = re.compile(r"FB: .*延迟播放（kind=([a-z-]+)")  # 契约/实现两种措辞都认
RE_FB_DROP = re.compile(r"FB: 反馈被丢弃（kind=([a-z-]+)，reason=(\w+)）")
RE_PLAYER_PLAY = re.compile(r"PLAYER: play \d+ samples")
RE_PLAYER_STREAM_STOP = re.compile(r"PLAYER: stream (?:end frames=|abort \()")
RE_VS_SPEAKING_DONE = re.compile(r"VS: speaking done（ok=([01])）")
RE_VS_TO_IDLE = re.compile(
    r"VS: state (\w+) -> IDLE（sid=(\d+)，停留 (\d+) ms，原因=([^）]+)）")

# VC 侧证据（T2.4/T2.6 既有日志 + T3.6 新增）
RE_ROUND_START = re.compile(r"VC: round start sid=(\d+)")
RE_AUDIO_END = re.compile(
    r"VC: audio_end sid=(\d+) 原因=([a-z-]+) 上行 (\d+) 帧/(\d+) B 用时 (\d+) ms")
RE_TTS_START = re.compile(r"VC: TTS start sid=(\d+) codec=(\w+) sr=(\d+)")
RE_TTS_END = re.compile(r"VC: TTS end sid=(\d+)（本轮下行音频 (\d+) 帧）")
RE_VC_CLOUD_ERROR = re.compile(r"VC: 云端 error sid=(\d+) code=(-?\d+) msg=")
RE_VC_TTS_FAIL = re.compile(r"VC: 设备侧 TTS 无法播放（原因=(\d+)）")
RE_VC_BAD_CODEC = re.compile(r"VC: tts_start codec=(\w+) 非 ?mp3")
RE_VC_TIMEOUT = re.compile(r"VC: 等待云端响应超时（(\d+) ms），sid=(\d+) 结束")
RE_VS_NET_DOWN = re.compile(r"VS: 网络断开（reason=\d+，重试 \d+ ms）→ 结束当前会话")
RE_WS_READY = re.compile(r"WS: 握手完成（hello_ack）")

# ---------------------------------------------------------------- 健康检查
RE_ANY_E = re.compile(r"^\s*E \(\d+\)")
RE_PANIC = re.compile(r"panic|Guru Meditation|stack overflow|StoreProhibited|assert failed")
RE_IGNORED_E_TAG = re.compile(
    r"[EW] \(\d+\) (NET|WS|wifi|websocket_client|esp_netif|esp-tls|esp_wifi|wpa_supplicant|"
    r"phy_init|net80211|esp_https_ota|HTTP_CLIENT):")
# T3.6 异常反馈是**预期**的 E 级语义日志（不是崩溃），健康检查按白名单排除
RE_EXPECTED_E = re.compile(
    r"VS: 会话进入 ERROR（|"
    r"VC: 云端 error sid=|"
    r"VS: 网络断开（|"
    r"VC: 设备侧 TTS 无法播放（|"
    r"VC: tts_start codec=\w+ 非 ?mp3|"
    r"VC: 等待云端响应超时（")

# ---------------------------------------------------------------- console 结束哨兵
END_VS_START = re.compile(r"已请求开始一轮|开始会话失败|已有会话进行中")
END_VC_STOP = re.compile(r"已请求优雅收尾|ESP_ERR|失败")
END_VC_STATUS = re.compile(r"说明: 唤醒后由 voice_session 自动起一轮")
END_VS_INTERRUPT = re.compile(r"已打断|未在进行|ERROR 反馈被按键中止|打断")
END_VS_FOLLOWUP = re.compile(r"连续对话续听窗口：(开|关)")
END_WS_RECONNECT = re.compile(r"已请求重连|无法重连|重连失败")
END_WIFI_RECONNECT = re.compile(r"已请求断开并重连|重连失败|未初始化|无法")
END_MIC_RESET = re.compile(r"已清零|失败")
END_SYS_HEAP = re.compile(r"PSRAM.*(最低水位|未初始化)")
RE_HEAP_LINE = re.compile(
    r"(内部 RAM|PSRAM   ): 总量 (\d+) B, 剩余 (\d+) B, 最大块 (\d+) B, "
    r"最低水位 (\d+) B, 峰值占用 (\d+) B")

# mic stat 门控行（T3.2 契约，hold_mask 是当前 R1 原因位图）
RE_GATE_LINE = re.compile(
    r"(?:门控|feed):\s*holds=(\d+)\s+releases=(\d+)\s+hold_mask=(0x[0-9a-fA-F]+|\d+)\s+"
    r"discards=(\d+)\s+discard_samples=(\d+)\s+last_discard_ms=(\d+)")
GATE_KEYS = ("holds", "releases", "hold_mask", "discards", "discard_samples", "last_discard_ms")

# fb status（T3.6）：契约 §3.3 与固件实际措辞都兼容解析
RE_FB_ACTIVE = re.compile(r"feedback: 活动=([^\s（]+)")
RE_FB_TOTALS_V1 = re.compile(
    r"总播放 (\d+) / 完成 (\d+) / 失败 (\d+)；延迟 (\d+) 丢弃 (\d+) 中止 (\d+)")
RE_FB_TOTALS_V2 = re.compile(
    r"总播放 (\d+) 完成 (\d+) 失败 (\d+) 延迟 (\d+) 丢弃 (\d+) 中止 (\d+)")
RE_FB_KIND_LINE_V1 = re.compile(
    r"分类: 网络 (\d+) / 云端 (\d+) / 超时 (\d+) / TTS (\d+)；最近时长 (\d+) ms")
RE_FB_KIND_LINE_V2 = re.compile(
    r"网络 (\d+) / 云端 (\d+) / 超时 (\d+) / TTS (\d+)；.*?最近时长 (\d+) ms")
RE_FB_LAST_DUR = re.compile(r"最近时长 (\d+) ms")

# vs status 的 T3.6 追加行（契约 §3.3）
RE_VS_HEAD = re.compile(r"voice_session: (\w+)")
RE_VS_ROUNDS = re.compile(
    r"轮次: 开始 (\d+) / 完成 (\d+) / 异常 (\d+) / 中止 (\d+)；打断 (\d+) 唤醒丢弃 (\d+)；"
    r"超时 (\d+) 云端 error (\d+)")
RE_VS_FB_LINE = re.compile(
    r"错误反馈: 网络 (\d+) / 云端 (\d+) / 超时 (\d+) / TTS (\d+)；提示音完成 (\d+) 失败 (\d+)；"
    r"最近 kind=([a-z-]+|\?) code=(-?\d+) 时长 (\d+) ms")

# kind ↔ 中文/短名（fb status「分类」行与 vs status「错误反馈」行）
KIND_ALIASES = {
    "net-offline": ("net-offline", "网络"),
    "cloud-error": ("cloud-error", "云端"),
    "timeout": ("timeout", "超时"),
    "tts-fail": ("tts-fail", "TTS"),
}
VS_FB_KEYS = ("net", "cloud", "timeout", "tts")
VS_FB_KIND_KEY = {"net-offline": "net", "cloud-error": "cloud", "timeout": "timeout",
                  "tts-fail": "tts"}

BOOT_TIMEOUT_DEFAULT = 40.0
MOCK_TIMEOUT_DEFAULT = 25.0


# ================================================================ 纯解析/判定（真机与 --self-test 共用）
# 这些函数不碰串口：--self-test 校验的就是真机断言用的解析逻辑，避免两套漂移。

def _to_int(text: str) -> int:
    return int(text, 16) if text.lower().startswith("0x") else int(text)


def parse_vs_states(lines: list[str]) -> list[dict]:
    """解析 `VS: state A -> B（sid=…，停留 … ms，原因=…）` 迁移（保留行号，用于顺序断言）。"""
    out = []
    for idx, line in enumerate(lines):
        m = RE_VS_STATE.search(line)
        if m:
            out.append({
                "idx": idx,
                "from": m.group(1),
                "to": m.group(2),
                "sid": int(m.group(3)),
                "dur_ms": int(m.group(4)),
                "reason": m.group(5),
            })
    return out


def parse_fb_start(line: str) -> dict | None:
    m = RE_FB_START_ANY.search(line)
    if not m:
        return None
    return {"kind": m.group(1), "code": int(m.group(2)), "sid": int(m.group(3))}


def parse_fb_play(line: str) -> dict | None:
    m = RE_FB_PLAY_ANY.search(line)
    if not m:
        return None
    return {
        "kind": m.group(1),
        "code": int(m.group(2)),
        "sid": int(m.group(3)),
        "prompt": m.group(4),
        "samples": int(m.group(5)),
    }


def parse_fb_done(line: str) -> dict | None:
    m = RE_FB_DONE_ANY.search(line)
    if not m:
        return None
    return {"kind": m.group(1), "ok": int(m.group(2)), "dur_ms": int(m.group(3))}


def parse_fb_prompt_done(line: str) -> dict | None:
    m = RE_FB_PROMPT_DONE.search(line)
    if not m:
        return None
    return {"kind": m.group(1), "ok": int(m.group(2)), "dur_ms": int(m.group(3))}


def find_first(lines: list[str], pattern: re.Pattern) -> tuple[int, re.Match] | tuple[None, None]:
    for idx, line in enumerate(lines):
        m = pattern.search(line)
        if m:
            return idx, m
    return None, None


def find_all(lines: list[str], pattern: re.Pattern) -> list[tuple[int, re.Match]]:
    return [(idx, m) for idx, line in enumerate(lines) for m in [pattern.search(line)] if m]


def parse_gate_line(line: str) -> dict | None:
    m = RE_GATE_LINE.search(line)
    if not m:
        return None
    return dict(zip(GATE_KEYS, (_to_int(group) for group in m.groups())))


def parse_fb_status(lines: list[str]) -> dict:
    """解析 `fb status`：活动/通道统计/4 类计数（兼容契约措辞与固件实际措辞）。"""
    st: dict = {
        "valid": False, "active": None, "plays": None, "done_ok": None, "done_fail": None,
        "deferred": None, "rejected": None, "aborts": None, "by_kind": {},
        "last_dur_ms": None, "raw": lines,
    }
    for line in lines:
        m = RE_FB_ACTIVE.search(line)
        if m and st["active"] is None:
            st["active"] = m.group(1)
        m = RE_FB_TOTALS_V1.search(line) or RE_FB_TOTALS_V2.search(line)
        if m:
            st["plays"] = int(m.group(1))
            st["done_ok"] = int(m.group(2))
            st["done_fail"] = int(m.group(3))
            st["deferred"] = int(m.group(4))
            st["rejected"] = int(m.group(5))
            st["aborts"] = int(m.group(6))
        m = RE_FB_KIND_LINE_V1.search(line) or RE_FB_KIND_LINE_V2.search(line)
        if m:
            for kind, value in zip(("net-offline", "cloud-error", "timeout", "tts-fail"),
                                   m.groups()[:4]):
                st["by_kind"][kind] = int(value)
            st["last_dur_ms"] = int(m.group(5))
        if st["last_dur_ms"] is None:
            m = RE_FB_LAST_DUR.search(line)
            if m:
                st["last_dur_ms"] = int(m.group(1))
    st["valid"] = st["plays"] is not None or bool(st["by_kind"])
    return st


def parse_vs_status(lines: list[str]) -> dict:
    """解析 `vs status`：状态/轮次 + T3.6「错误反馈」追加行。"""
    st: dict = {"valid": False, "raw": lines, "feedback": {}}
    for line in lines:
        m = RE_VS_HEAD.search(line)
        if m:
            st["state"] = m.group(1)
        m = RE_VS_ROUNDS.search(line)
        if m:
            st["rounds_started"] = int(m.group(1))
            st["rounds_completed"] = int(m.group(2))
            st["rounds_errored"] = int(m.group(3))
            st["round_aborts"] = int(m.group(4))
            st["interrupts"] = int(m.group(5))
            st["wakes_dropped"] = int(m.group(6))
            st["timeouts"] = int(m.group(7))
            st["cloud_errors"] = int(m.group(8))
        m = RE_VS_FB_LINE.search(line)
        if m:
            for key, value in zip(VS_FB_KEYS, m.groups()[:4]):
                st["feedback"][key] = int(value)
            st["feedback"]["ok"] = int(m.group(5))
            st["feedback"]["fail"] = int(m.group(6))
            st["feedback"]["recent_kind"] = m.group(7)
            st["feedback"]["recent_code"] = int(m.group(8))
            st["feedback"]["recent_ms"] = int(m.group(9))
    st["valid"] = ("state" in st and "rounds_started" in st)
    return st


def delta(after: dict, before: dict, key: str) -> int | None:
    a, b = after.get(key), before.get(key)
    return None if a is None or b is None else int(a) - int(b)


def kind_delta(after: dict, before: dict, kind: str) -> int | None:
    """fb status「分类」行某类别的增量（解析不到时返回 None）。"""
    a = (after.get("by_kind") or {}).get(kind)
    b = (before.get("by_kind") or {}).get(kind)
    return None if a is None or b is None else int(a) - int(b)


def vs_fb_delta(after: dict, before: dict, key: str) -> int | None:
    a = (after.get("feedback") or {}).get(key)
    b = (before.get("feedback") or {}).get(key)
    return None if a is None or b is None else int(a) - int(b)


def unexpected_errors(lines: list[str], allow_feedback: bool = True) -> list[str]:
    """健康检查：E 级/panic 行中，排除网络/WS 噪声与 T3.6 预期的异常反馈日志。

    `allow_feedback=True` 用于错误场景（云端 error/断网/超时/TTS 失败会有预期 E 级日志）；
    正常轮/打断场景传 False，任何 E 级（含错误反馈）都算失败。
    """
    out = []
    for line in lines:
        if RE_PANIC.search(line):
            out.append(line)
            continue
        if not RE_ANY_E.search(line):
            continue
        if allow_feedback and RE_EXPECTED_E.search(line):
            continue
        if RE_IGNORED_E_TAG.search(line):
            continue
        out.append(line)
    return out


def timeout_duration_ok(seconds: float) -> bool:
    """场景 E：audio_end → 错误提示音开始的耗时应在 9.5~13.0s（10s 超时 + 反馈启动）。"""
    return 9.5 <= seconds <= 13.0


def order_ok(lines: list[str], before: re.Pattern, after: re.Pattern) -> bool:
    """`before` 的首次出现必须早于 `after` 的首次出现（同一条日志算同点，不做先后）。"""
    i_before, _ = find_first(lines, before)
    i_after, _ = find_first(lines, after)
    return i_before is not None and i_after is not None and i_before < i_after


def feedback_present(lines: list[str]) -> bool:
    """窗口内是否出现任何错误反馈痕迹（用于「不误播」负向断言）。"""
    return any(RE_FB_START_ANY.search(line) or RE_FB_PLAY_ANY.search(line) for line in lines)


def parse_scenarios(text: str) -> list[str]:
    """解析 `--scenarios`：`A,C,J` / `A-C` / 空=全部；返回按 A~J 顺序去重列表。"""
    if not text or not text.strip():
        return list(SCENARIO_ORDER)
    selected: set[str] = set()
    for token in re.split(r"[,\s]+", text.strip()):
        if not token:
            continue
        m = re.fullmatch(r"([A-Ja-j])[-~]([A-Ja-j])", token)
        if m:
            start, end = m.group(1).upper(), m.group(2).upper()
            if SCENARIO_ORDER.index(start) > SCENARIO_ORDER.index(end):
                raise SystemExit(f"--scenarios 范围反向: {token}")
            selected.update(SCENARIO_ORDER[SCENARIO_ORDER.index(start):
                                           SCENARIO_ORDER.index(end) + 1])
            continue
        name = token.upper()
        if name not in SCENARIO_ORDER:
            raise SystemExit(f"未知场景: {token}（可用 A~J，如 --scenarios A,C,J 或 A-C）")
        selected.add(name)
    return [name for name in SCENARIO_ORDER if name in selected]


def describe_spec(spec: list[str] | None) -> str:
    if spec is None:
        return "不启 mock"
    if not spec:
        return "mock 默认 normal"
    return "mock " + " ".join(spec)


# ================================================================ 串口/console 辅助

def wait_line(dev: Device, pattern: re.Pattern, timeout: float, mark: int = 0) -> str | None:
    """等 mark 之后首次出现 pattern，返回该行文本（超时返回 None）。"""
    if dev.wait_line(pattern, timeout, mark) is None:
        return None
    for _, line in dev.lines_since(mark):
        if pattern.search(line):
            return line
    return None


def wait_line_ts(dev: Device, pattern: re.Pattern, timeout: float,
                 mark: int = 0) -> tuple[float, str] | None:
    """等 mark 之后首次出现 pattern，返回 (到达时间戳, 行文本)。"""
    end = time.time() + timeout
    while time.time() < end:
        for ts, line in dev.lines_since(mark):
            if pattern.search(line):
                return ts, line
        dev.pump(0.05)
    return None


def lines_since(dev: Device, mark: int) -> list[str]:
    return [line for _, line in dev.lines_since(mark)]


def console_until(dev: Device, cmd: str, done, timeout: float = 2.0) -> list[str]:
    """发 console 命令并收集输出，直到 done(已收到的文本) 为真或超时（不假设固定结束行顺序）。"""
    mark = dev.mark()
    dev.send(cmd)
    end = time.time() + timeout
    while time.time() < end:
        text = "\n".join(line for _, line in dev.lines_since(mark))
        if done(text):
            break
        dev.pump(0.05)
    return [line for _, line in dev.lines_since(mark)]


def fb_status(dev: Device, timeout: float = 2.0) -> dict:
    lines = console_until(
        dev, "fb status", lambda text: "feedback:" in text and "总播放" in text, timeout)
    return parse_fb_status(lines)


def vs_status(dev: Device, timeout: float = 2.0) -> dict:
    lines = console_until(
        dev, "vs status", lambda text: "说明: 唤醒自动进 LISTENING" in text, timeout)
    return parse_vs_status(lines)


def mic_gate_stat(dev: Device, timeout: float = 2.0) -> dict:
    """`mic stat`：门控行（holds/releases/hold_mask）+ 收音状态。"""
    lines = console_until(
        dev, "mic stat",
        lambda text: ("门控:" in text or "feed:" in text) and "参考:" in text, timeout)
    stat: dict = {"line": None, "feed": None, "raw": lines}
    for line in lines:
        m = re.search(r"状态: 收音 (开启|暂停)", line)
        if m:
            stat["feed"] = m.group(1)
        parsed = parse_gate_line(line)
        if parsed is not None:
            stat["line"] = parsed
    return stat


def sys_heap(dev: Device, timeout: float = 3.0) -> dict:
    lines = console_until(dev, "sys heap", lambda text: "PSRAM" in text and "B," in text, timeout)
    heap: dict = {}
    for line in lines:
        m = RE_HEAP_LINE.search(line)
        if m:
            key = "internal" if m.group(1) == "内部 RAM" else "psram"
            heap[key] = {
                "total": int(m.group(2)),
                "free": int(m.group(3)),
                "largest": int(m.group(4)),
                "min_free": int(m.group(5)),
                "peak": int(m.group(6)),
            }
    return heap


# ================================================================ mock 云端

def port_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.3)
        return sock.connect_ex(("127.0.0.1", port)) == 0


class Mock:
    """按场景参数拉起/重启 ws_mock_server（设备固定连同一端口，所以一次只跑一个配置）。"""

    def __init__(self, port: int) -> None:
        self.port = port
        self.proc: subprocess.Popen | None = None
        self.owned = False
        self.spec: list[str] | None = None
        self.log_path = os.path.join(tempfile.gettempdir(), f"error_prompt_mock_{port}.log")

    def start(self, spec: list[str], label: str) -> bool:
        if port_open(self.port):
            print(f"  mock: 端口 {self.port} 已被占用，复用外部服务（{label} 期望配置: "
                  f"{describe_spec(spec)}；mock 侧断言跳过）")
            self.spec = spec
            self.owned = False
            return False
        log = open(self.log_path, "w", encoding="utf-8", buffering=1)
        cmd = ["uv", "run", MOCK_SCRIPT, "--host", "0.0.0.0", "--port", str(self.port), *spec]
        self.proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, cwd=HERE)
        self.owned = True
        self.spec = spec
        deadline = time.time() + 15
        while time.time() < deadline:
            if port_open(self.port):
                print(f"  mock: 已启动（端口 {self.port}，{describe_spec(spec)}）")
                return True
            if self.proc.poll() is not None:
                raise SystemExit(f"mock 启动失败，日志见 {self.log_path}")
            time.sleep(0.1)
        raise SystemExit(f"mock 端口 {self.port} 15s 内未就绪，日志见 {self.log_path}")

    def stop(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None
        self.owned = False

    def text(self) -> str:
        try:
            with open(self.log_path, encoding="utf-8", errors="replace") as handle:
                return handle.read()
        except OSError:
            return ""


# ================================================================ 场景执行器

class Runner:
    def __init__(self, args: argparse.Namespace, dev: Device, result: Result, mock: Mock) -> None:
        self.args = args
        self.dev = dev
        self.result = result
        self.mock = mock

    # ---- 小工具 ----

    def section(self, name: str) -> None:
        print(f"\n===== [{name}] {SCENARIO_TITLES[name]} =====")

    def wait_line(self, pattern: re.Pattern, timeout: float, mark: int = 0) -> str | None:
        return wait_line(self.dev, pattern, timeout, mark)

    def lines_since(self, mark: int) -> list[str]:
        return lines_since(self.dev, mark)

    def fb_status(self, timeout: float = 2.0) -> dict:
        return fb_status(self.dev, timeout)

    def vs_status(self, timeout: float = 2.0) -> dict:
        return vs_status(self.dev, timeout)

    def mic_stat(self, timeout: float = 2.0) -> dict:
        return mic_gate_stat(self.dev, timeout)

    def vc_ws_ready(self) -> bool:
        out = self.dev.console("vc status", END_VC_STATUS, timeout=2.0)
        return any("WS: READY" in line for line in out)

    def wait_ws_ready(self, label: str, timeout: float, reconnect: bool = True) -> bool:
        if reconnect:
            self.dev.console("ws reconnect", END_WS_RECONNECT, timeout=2.0)
        end = time.time() + timeout
        while time.time() < end:
            if self.vc_ws_ready():
                return True
            self.dev.pump(0.3)
        return False

    def wait_idle(self, timeout: float = 6.0) -> bool:
        end = time.time() + timeout
        while time.time() < end:
            if self.vs_status().get("state") == "IDLE":
                return True
            self.dev.pump(0.1)
        return False

    def ensure_idle(self, label: str) -> bool:
        ok = self.wait_idle()
        self.result.check(ok, f"[{label}] 场景开始前状态机 IDLE（当前 "
                              f"{self.vs_status().get('state')}）")
        return ok

    def ensure_mock(self, label: str) -> bool:
        spec = MOCK_SPECS[label]
        if self.args.no_mock:
            self.result.warn(f"[{label}] --no-mock：复用外部 mock（期望配置: {describe_spec(spec)}），"
                             "mock 侧断言跳过")
            return True
        if self.mock.spec != spec or self.mock.proc is None:
            if self.mock.owned or self.mock.proc is not None:
                self.mock.stop()
                self.dev.pump(0.3)
            self.mock.start(spec, label)
            ok = self.wait_ws_ready(label, self.args.mock_timeout)
            self.result.check(ok, f"[{label}] mock 就绪（{describe_spec(spec)}）且设备 WS READY")
            return ok
        return True

    def start_round(self, label: str) -> tuple[int, int]:
        mark = self.dev.mark()
        self.dev.console("vs start", END_VS_START, timeout=3.0)
        line = self.wait_line(RE_ROUND_START, 5.0, mark)
        self.result.check(line is not None, f"[{label}] vs start 起轮（收到 VC round start）")
        if line is None:
            return -1, mark
        sid = int(RE_ROUND_START.search(line).group(1))
        return sid, mark

    def end_utterance(self, label: str) -> bool:
        self.dev.pump(0.3)  # 让上行真的跑起来，再优雅收尾（audio_end 原因 stop-request）
        out = self.dev.console("vc stop", END_VC_STOP, timeout=2.0)
        ok = any("已请求优雅收尾" in line for line in out)
        self.result.check(ok, f"[{label}] vc stop 结束本轮（audio_end）")
        return ok

    def wait_error_feedback_start(self, label: str, timeout: float, mark: int) -> str | None:
        line = self.wait_line(RE_FB_START_ANY, timeout, mark)
        self.result.check(line is not None, f"[{label}] 收到 `VS: 错误反馈（…）→ 提示音（R1）`")
        return line

    def assert_error_chain(self, label: str, lines: list[str], kind: str, reason: str,
                           code: int | None = None) -> bool:
        """错误反馈公共链断言：VS 错误反馈行 / FB 反馈行 / PLAYER play / 状态迁移到 ERROR。"""
        r = self.result
        starts = find_all(lines, RE_FB_START_ANY)
        if starts:
            m = starts[0][1]
            start_ok = m.group(1) == kind and (code is None or int(m.group(2)) == code)
            start_detail = f"实际 {m.group(1)}/code={m.group(2)}"
        else:
            start_ok = False
            start_detail = "未收到"
        r.check(start_ok, f"[{label}] VS 错误反馈 kind={kind}"
                + (f" code={code}" if code is not None else "（code 任意）")
                + f"（{start_detail}）")

        plays = find_all(lines, RE_FB_PLAY_ANY)
        if plays:
            m = plays[0][1]
            play_ok = m.group(1) == kind and (code is None or int(m.group(2)) == code)
            play_detail = f"{m.group(1)}/{m.group(4)}={m.group(5)} 样本"
        else:
            play_ok = False
            play_detail = "未收到"
        r.check(play_ok, f"[{label}] FB: 反馈 kind={kind} → 播放提示音（{play_detail}）")

        r.check(any(RE_PLAYER_PLAY.search(line) for line in lines),
                f"[{label}] PLAYER: play 提示音（R1 暂收麦克风）")

        transitions = parse_vs_states(lines)
        enter_error = [t for t in transitions if t["to"] == "ERROR"]
        if enter_error:
            error_ok = enter_error[0]["reason"] == reason
            transit_detail = (f"实际 {enter_error[0]['from']} -> ERROR"
                              f"（原因={enter_error[0]['reason']}）")
        else:
            error_ok = False
            transit_detail = "未收到"
        r.check(error_ok, f"[{label}] VS: state * -> ERROR（原因={reason}；{transit_detail}）")

        enter_lines = find_all(lines, RE_VS_ENTER_ERROR)
        r.check(bool(enter_lines) and enter_lines[0][1].group(1) == kind,
                f"[{label}] VS: 会话进入 ERROR（{kind}，code=…）")
        return bool(start_ok and play_ok and error_ok)

    def wait_feedback_done_idle(self, label: str, kind: str, mark: int,
                                timeout: float = 8.0) -> bool:
        """等 VS 错误反馈完成 + FB 提示音完成 + ERROR→IDLE。"""
        r = self.result
        vs_done = self.wait_line(RE_FB_DONE_ANY, timeout, mark)
        vs_done_ok = bool(vs_done) and RE_FB_DONE_ANY.search(vs_done).group(1) == kind
        r.check(vs_done_ok, f"[{label}] VS: 错误反馈完成（类别={kind}，ok=1）→ IDLE")
        fb_done = self.wait_line(RE_FB_PROMPT_DONE, timeout, mark)
        fb_done_ok = bool(fb_done) and RE_FB_PROMPT_DONE.search(fb_done).group(1) == kind
        r.check(fb_done_ok, f"[{label}] FB: 提示音完成 kind={kind} ok=1")
        idle = self.wait_line(RE_VS_TO_IDLE, 4.0, mark)
        idle_ok = bool(idle) and RE_VS_TO_IDLE.search(idle).group(1) == "ERROR"
        r.check(idle_ok, f"[{label}] VS: state ERROR -> IDLE（原因=error-done）")
        return bool(vs_done_ok and fb_done_ok and idle_ok)

    def assert_fb_stats(self, label: str, before: dict, after: dict, kind: str,
                        plays: int = 1) -> None:
        r = self.result
        d_plays = delta(after, before, "plays")
        r.check(d_plays == plays, f"[{label}] fb status 总播放 +{plays}（实际 +{d_plays}）")
        d_kind = kind_delta(after, before, kind)
        if d_kind is not None:
            r.check(d_kind == plays, f"[{label}] fb status 分类 {kind} +{plays}（实际 +{d_kind}）")
        else:
            r.warn(f"[{label}] fb status「分类」行未解析到（格式未冻结，仅 WARN）")
        d_ok = delta(after, before, "done_ok")
        r.check(d_ok is not None and d_ok >= plays,
                f"[{label}] fb status 完成 +{d_ok}（≥{plays}）")
        last_dur = after.get("last_dur_ms")
        r.check(isinstance(last_dur, int) and last_dur > 0,
                f"[{label}] fb status 最近时长 {last_dur} ms > 0")

    def assert_vs_fb_stats(self, label: str, before: dict, after: dict, kind: str,
                           plays: int = 1) -> None:
        r = self.result
        key = VS_FB_KIND_KEY[kind]
        d_kind = vs_fb_delta(after, before, key)
        r.check(d_kind == plays, f"[{label}] vs status 错误反馈 {key} +{plays}（实际 +{d_kind}）")
        d_ok = vs_fb_delta(after, before, "ok")
        r.check(d_ok is not None and d_ok >= plays,
                f"[{label}] vs status 提示音完成 +{d_ok}（≥{plays}）")
        recent_kind = (after.get("feedback") or {}).get("recent_kind")
        r.check(recent_kind == kind, f"[{label}] vs status 最近 kind={kind}（实际 {recent_kind}）")

    def assert_health(self, label: str, lines: list[str], allow_feedback: bool) -> None:
        errs = unexpected_errors(lines, allow_feedback=allow_feedback)
        self.result.check(not errs, f"[{label}] 无非预期 ERROR/panic（{len(errs)} 条）")
        for line in errs[:5]:
            print(f"    {line.strip()}")

    def assert_no_feedback(self, label: str, lines: list[str], before: dict, after: dict) -> None:
        r = self.result
        bad = [line for line in lines
               if RE_FB_START_ANY.search(line) or RE_FB_PLAY_ANY.search(line)]
        r.check(not bad, f"[{label}] 窗口内无错误反馈日志（{len(bad)} 条）")
        for line in bad[:3]:
            print(f"    {line.strip()}")
        d_plays = delta(after, before, "plays")
        r.check(d_plays == 0, f"[{label}] fb status 总播放未增加（实际 +{d_plays}）")

    def assert_mock_owned(self, label: str) -> bool:
        if self.mock.owned:
            return True
        self.result.warn(f"[{label}] mock 非本脚本所有：跳过 mock 侧断言")
        return False

    # ---- A：起轮前网络未连接（T3.6 离线唤醒语义） ----

    def run_a(self) -> None:
        name = "A"
        self.section(name)
        r = self.result
        self.dev.console("vs followup off", END_VS_FOLLOWUP, timeout=2.0)
        r.check(not self.vc_ws_ready(), f"[{name}] 前置：云端未 READY（本场景不启 mock）")
        before_fb = self.fb_status()
        before_vs = self.vs_status()
        self.dev.console("mic reset", END_MIC_RESET, timeout=2.0)

        mark = self.dev.mark()
        wake_hit = None
        hold_during = None
        for attempt in range(1, self.args.wake_attempts + 1):
            self.dev.play(os.path.join(HERE, WAKE_WAV))
            # 回放结束立刻采一次（离线唤醒在 lexin.wav 播放中触发，反馈音 1.07s 可能还在播）
            hold_during = hold_during or self.wait_hold_mask(HOLD_PROMPT, timeout=0.8)
            # T3.6 离线唤醒：不播"我在呢"，直接进 ERROR + net-offline 反馈
            wake_hit = self.wait_line(RE_FB_START_ANY, 8.0, mark)
            if wake_hit is not None:
                break
            if attempt < self.args.wake_attempts:
                print(f"  唤醒未触发错误反馈（第 {attempt} 次），重试（环境噪声口径）")
                self.dev.pump(0.5)
        r.check(wake_hit is not None, f"[{name}] 唤醒触发 net-offline 错误反馈")
        if wake_hit is None:
            return

        hold_during = hold_during or self.wait_hold_mask(HOLD_PROMPT, timeout=2.0)
        hold_mask = hold_during["line"]["hold_mask"] if hold_during else None
        r.check(hold_mask == HOLD_PROMPT,
                f"[{name}] 提示音期间 mic stat hold_mask=0x1（实际 "
                f"{hex(hold_mask) if hold_mask is not None else '未采到'}）")

        done_ok = self.wait_feedback_done_idle(name, "net-offline", mark)
        lines = self.lines_since(mark)
        self.assert_error_chain(name, lines, "net-offline", "net-offline", code=0)

        # 离线唤醒语义：不播"我在呢"（无 prompt done），直接 ERROR + 提示音
        r.check(any(RE_WAKE.search(line) for line in lines), f"[{name}] VS: wake detected")
        r.check(not any(RE_PROMPT_DONE.search(line) for line in lines),
                f"[{name}] 不播唤醒应答「我在呢」（无 `prompt done, feed resumed`）")
        r.check(any(RE_PLAYER_PLAY.search(line) for line in lines),
                f"[{name}] PLAYER: play 提示音（R1）")

        after_mic = self.mic_stat()
        gate = after_mic.get("line")
        r.check(gate is not None and gate.get("hold_mask") == 0,
                f"[{name}] 结束后 mic stat hold_mask=0x0（R1 已释放）")
        r.check(after_mic.get("feed") == "开启", f"[{name}] 结束后收音恢复（R2）")

        after_fb = self.fb_status()
        after_vs = self.vs_status()
        self.assert_fb_stats(name, before_fb, after_fb, "net-offline")
        self.assert_vs_fb_stats(name, before_vs, after_vs, "net-offline")
        self.assert_health(name, lines, allow_feedback=True)
        if not done_ok:
            r.check(False, f"[{name}] 反馈收尾链完整（完成/IDLE 均收到）")

    def wait_hold_mask(self, want: int, timeout: float) -> dict | None:
        """轮询 `mic stat` 直到 hold_mask 恰好等于 want（在提示音播放窗口内采样 hold=0x1）。"""
        end = time.time() + timeout
        best = None
        while time.time() < end:
            stat = self.mic_stat()
            line = stat.get("line")
            if line is not None and line.get("hold_mask") is not None:
                best = stat
                if line["hold_mask"] == want:
                    return stat
            self.dev.pump(0.05)
        return best if best is not None else None

    # ---- B：会话中途断网 ----

    def run_b(self) -> None:
        name = "B"
        self.section(name)
        r = self.result
        if not self.ensure_mock(name) or not self.ensure_idle(name):
            return
        before_fb = self.fb_status()
        before_vs = self.vs_status()
        sid, mark = self.start_round(name)
        if sid < 0:
            return
        self.dev.pump(1.0)  # 契约 §5.2-B：起轮 1s 后中途断网
        self.dev.console("wifi reconnect", END_WIFI_RECONNECT, timeout=3.0)
        r.check(self.wait_line(RE_VS_NET_DOWN, 10.0, mark) is not None,
                f"[{name}] VS: 网络断开 → 结束当前会话")
        fb_line = self.wait_error_feedback_start(name, 10.0, mark)
        if fb_line is None:
            return
        self.wait_feedback_done_idle(name, "net-offline", mark, timeout=8.0)
        lines = self.lines_since(mark)
        self.assert_error_chain(name, lines, "net-offline", "net-offline")
        after_fb = self.fb_status()
        after_vs = self.vs_status()
        self.assert_fb_stats(name, before_fb, after_fb, "net-offline")
        self.assert_vs_fb_stats(name, before_vs, after_vs, "net-offline")
        # 无残留轮次：VC 已回 IDLE，且会话轮次已中止（不是中途挂住）
        vc_lines = self.dev.console("vc status", END_VC_STATUS, timeout=2.0)
        vc_state = None
        for line in vc_lines:
            m = re.search(r"voice_cloud: (\w+)", line)
            if m:
                vc_state = m.group(1)
        r.check(vc_state == "IDLE", f"[{name}] 无残留轮次（vc status={vc_state}）")
        self.assert_health(name, lines, allow_feedback=True)
        # 恢复联网，供后续场景重启 mock 后重连
        self.wait_ws_ready(name, self.args.mock_timeout)

    # ---- C/D：云端 error（通用 / TTS 错误码） ----

    def run_cloud_error(self, name: str, code: int, kind: str, reason: str) -> None:
        self.section(name)
        r = self.result
        if not self.ensure_mock(name) or not self.ensure_idle(name):
            return
        before_fb = self.fb_status()
        before_vs = self.vs_status()
        sid, mark = self.start_round(name)
        if sid < 0:
            return
        if not self.end_utterance(name):
            return
        fb_line = self.wait_error_feedback_start(name, 15.0, mark)
        if fb_line is None:
            return
        self.wait_feedback_done_idle(name, kind, mark)
        lines = self.lines_since(mark)
        self.assert_error_chain(name, lines, kind, reason, code=code)
        r.check(any(RE_VC_CLOUD_ERROR.search(line) and int(
            RE_VC_CLOUD_ERROR.search(line).group(2)) == code for line in lines),
            f"[{name}] VC: 云端 error code={code}")
        after_fb = self.fb_status()
        after_vs = self.vs_status()
        self.assert_fb_stats(name, before_fb, after_fb, kind)
        self.assert_vs_fb_stats(name, before_vs, after_vs, kind)
        if kind == "cloud-error":
            cloud_delta = vs_fb_delta(after_vs, before_vs, "cloud")
            r.check(cloud_delta is not None and cloud_delta >= 1,
                    f"[{name}] vs status 云端 error 计数 +{cloud_delta}")
        self.assert_health(name, lines, allow_feedback=True)
        if self.assert_mock_owned(name):
            mock_text = self.mock.text()
            r.check(f"code={code}" in mock_text,
                    f"[{name}] mock 按配置下发 error（code={code}）")

    # ---- E：THINKING 超时 ----

    def run_e(self) -> None:
        name = "E"
        self.section(name)
        r = self.result
        if not self.ensure_mock(name) or not self.ensure_idle(name):
            return
        before_fb = self.fb_status()
        before_vs = self.vs_status()
        sid, mark = self.start_round(name)
        if sid < 0:
            return
        if not self.end_utterance(name):
            return
        audio_end = wait_line_ts(self.dev, RE_AUDIO_END, 5.0, mark)
        r.check(audio_end is not None, f"[{name}] 收到 audio_end（进入 THINKING）")
        fb_hit = wait_line_ts(self.dev, RE_FB_START_ANY, 15.0, mark)
        r.check(fb_hit is not None, f"[{name}] 收到 timeout 错误反馈")
        if audio_end is not None and fb_hit is not None:
            elapsed = fb_hit[0] - audio_end[0]
            r.check(timeout_duration_ok(elapsed),
                    f"[{name}] audio_end → 错误提示音耗时 {elapsed:.2f}s 在 9.5~13.0s")
        if fb_hit is None:
            return
        self.wait_feedback_done_idle(name, "timeout", mark, timeout=8.0)
        lines = self.lines_since(mark)
        self.assert_error_chain(name, lines, "timeout", "timeout")
        r.check(any(RE_VC_TIMEOUT.search(line) for line in lines),
                f"[{name}] VC: 等待云端响应超时")
        after_fb = self.fb_status()
        after_vs = self.vs_status()
        self.assert_fb_stats(name, before_fb, after_fb, "timeout")
        self.assert_vs_fb_stats(name, before_vs, after_vs, "timeout")
        r.check((after_vs.get("timeouts") or 0) - (before_vs.get("timeouts") or 0) >= 1,
                f"[{name}] vs status 超时计数 +1")
        self.assert_health(name, lines, allow_feedback=True)

    # ---- F：设备侧 TTS 失败（坏 codec） ----

    def run_f(self) -> None:
        name = "F"
        self.section(name)
        r = self.result
        if not self.ensure_mock(name) or not self.ensure_idle(name):
            return
        before_fb = self.fb_status()
        before_vs = self.vs_status()
        sid, mark = self.start_round(name)
        if sid < 0:
            return
        if not self.end_utterance(name):
            return
        fb_line = self.wait_error_feedback_start(name, 10.0, mark)
        if fb_line is None:
            return
        self.wait_feedback_done_idle(name, "tts-fail", mark)
        lines = self.lines_since(mark)
        self.assert_error_chain(name, lines, "tts-fail", "tts-fail")
        bad_codec = [line for line in lines
                     if (RE_VC_BAD_CODEC.search(line) or RE_VC_TTS_FAIL.search(line)
                         or "无法播放" in line or "非 mp3" in line)]
        r.check(bool(bad_codec), f"[{name}] 设备日志有「非 mp3/无法播放」WARN（{len(bad_codec)} 条）")
        for line in bad_codec[:3]:
            print(f"    {line.strip()}")
        after_fb = self.fb_status()
        after_vs = self.vs_status()
        self.assert_fb_stats(name, before_fb, after_fb, "tts-fail")
        self.assert_vs_fb_stats(name, before_vs, after_vs, "tts-fail")
        self.assert_health(name, lines, allow_feedback=True)
        if self.assert_mock_owned(name):
            r.check("codec=pcm" in self.mock.text(),
                    f"[{name}] mock tts_start 使用 codec=pcm（--tts-bad-codec）")

    # ---- G：打断不误播 ----

    def run_g(self) -> None:
        name = "G"
        self.section(name)
        r = self.result
        if not self.ensure_mock(name) or not self.ensure_idle(name):
            return
        before_fb = self.fb_status()
        sid, mark = self.start_round(name)
        if sid < 0:
            return
        if not self.end_utterance(name):
            return
        r.check(self.wait_line(RE_TTS_START, 12.0, mark) is not None,
                f"[{name}] 正常轮进入 TTS 播放（SPEAKING）")
        self.dev.pump(0.2)
        self.dev.console("vs interrupt", END_VS_INTERRUPT, timeout=2.0)
        idle = self.wait_line(RE_VS_TO_IDLE, 5.0, mark)
        r.check(idle is not None, f"[{name}] 打断后回 IDLE")
        self.dev.pump(1.5)  # 越过可能的 feedback 启动窗口再检查
        lines = self.lines_since(mark)
        after_fb = self.fb_status()
        self.assert_no_feedback(name, lines, before_fb, after_fb)
        r.check(not any(RE_VS_ENTER_ERROR.search(line) for line in lines),
                f"[{name}] 打断不进入 ERROR")
        self.assert_health(name, lines, allow_feedback=False)
        if self.assert_mock_owned(name):
            r.check("interrupt sid=" in self.mock.text(), f"[{name}] mock 收到 interrupt 报文")

    # ---- H：正常轮不误播 ----

    def run_h(self) -> None:
        name = "H"
        self.section(name)
        r = self.result
        if not self.ensure_mock(name) or not self.ensure_idle(name):
            return
        before_fb = self.fb_status()
        before_vs = self.vs_status()
        sid, mark = self.start_round(name)
        if sid < 0:
            return
        if not self.end_utterance(name):
            return
        r.check(self.wait_line(RE_TTS_START, 12.0, mark) is not None,
                f"[{name}] 正常轮收到 tts_start")
        r.check(self.wait_line(RE_TTS_END, 12.0, mark) is not None,
                f"[{name}] 正常轮收到 tts_end")
        idle = self.wait_line(RE_VS_TO_IDLE, 15.0, mark)
        r.check(idle is not None and RE_VS_TO_IDLE.search(idle).group(1) == "SPEAKING",
                f"[{name}] 正常轮 SPEAKING -> IDLE")
        self.dev.pump(0.5)
        lines = self.lines_since(mark)
        after_fb = self.fb_status()
        after_vs = self.vs_status()
        self.assert_no_feedback(name, lines, before_fb, after_fb)
        r.check((after_vs.get("rounds_completed") or 0) - (before_vs.get("rounds_completed") or 0)
                >= 1, f"[{name}] vs status 完成轮次 +1")
        self.assert_health(name, lines, allow_feedback=False)

    # ---- I：SPEAKING 中云端报错 ----

    def run_i(self) -> None:
        name = "I"
        self.section(name)
        r = self.result
        if not self.ensure_mock(name) or not self.ensure_idle(name):
            return
        before_fb = self.fb_status()
        before_vs = self.vs_status()
        sid, mark = self.start_round(name)
        if sid < 0:
            return
        if not self.end_utterance(name):
            return
        r.check(self.wait_line(re.compile(r"VS: state THINKING -> SPEAKING"), 12.0, mark)
                is not None, f"[{name}] 先进入 SPEAKING（TTS 已开播）")
        fb_line = self.wait_error_feedback_start(name, 12.0, mark)
        if fb_line is None:
            return
        self.wait_feedback_done_idle(name, "tts-fail", mark)
        lines = self.lines_since(mark)
        self.assert_error_chain(name, lines, "tts-fail", "tts-fail", code=3001)
        # 先停 TTS 流、再播提示音（无叠音）
        stream_stop = [idx for idx, line in enumerate(lines)
                       if RE_PLAYER_STREAM_STOP.search(line)]
        fb_play = [idx for idx, line in enumerate(lines) if RE_FB_PLAY_ANY.search(line)]
        order = bool(stream_stop and fb_play and stream_stop[0] < fb_play[0])
        r.check(order, f"[{name}] 先停 TTS 流（PLAYER stream end/abort）再播错误提示音")
        r.check(not any(RE_FB_DROP.search(line) for line in lines),
                f"[{name}] 无「反馈被丢弃（reason=busy）」（未叠音/未丢反馈）")
        r.check(any(RE_VS_SPEAKING_DONE.search(line) for line in lines),
                f"[{name}] VS: speaking done（TTS 流收尾）")
        after_fb = self.fb_status()
        after_vs = self.vs_status()
        self.assert_fb_stats(name, before_fb, after_fb, "tts-fail")
        self.assert_vs_fb_stats(name, before_vs, after_vs, "tts-fail")
        self.assert_health(name, lines, allow_feedback=True)
        if self.assert_mock_owned(name):
            mock_text = self.mock.text()
            r.check("after-tts-start" in mock_text,
                    f"[{name}] mock 按 --error-stage after-tts-start 下发 error")
            r.check("error_stage=after-tts-start" in mock_text or "真实 MP3" in mock_text,
                    f"[{name}] mock 报错前已下发真实 MP3 帧")

    # ---- J：稳定性（连续 N 轮云端 error） ----

    def run_j(self) -> None:
        name = "J"
        self.section(name)
        r = self.result
        rounds = self.args.stability_rounds
        if not self.ensure_mock(name) or not self.ensure_idle(name):
            return
        before_fb = self.fb_status()
        before_vs = self.vs_status()
        heap_before = sys_heap(self.dev)
        if "internal" in heap_before:
            print(f"  堆基线: 内部 free={heap_before['internal']['free']} B / "
                  f"最低水位={heap_before['internal']['min_free']} B / "
                  f"PSRAM free={heap_before.get('psram', {}).get('free')} B")
        ok_rounds = 0
        all_lines: list[str] = []
        for i in range(1, rounds + 1):
            sid, mark = self.start_round(f"{name}#{i}")
            if sid < 0:
                break
            if not self.end_utterance(f"{name}#{i}"):
                break
            if self.wait_error_feedback_start(f"{name}#{i}", 15.0, mark) is None:
                break
            if not self.wait_feedback_done_idle(f"{name}#{i}", "cloud-error", mark):
                break
            if not self.wait_idle():
                r.check(False, f"[{name}#{i}] 反馈后回 IDLE")
                break
            ok_rounds += 1
            all_lines.extend(self.lines_since(mark))
            print(f"  第 {i}/{rounds} 轮 PASS（cloud-error → IDLE）")
        r.check(ok_rounds == rounds, f"[{name}] 连续 {rounds} 轮错误反馈均完成（实际 {ok_rounds}）")

        after_fb = self.fb_status()
        after_vs = self.vs_status()
        heap_after = sys_heap(self.dev)
        d_plays = delta(after_fb, before_fb, "plays")
        r.check(d_plays == rounds, f"[{name}] fb status 总播放 +{rounds}（实际 +{d_plays}）")
        d_kind = kind_delta(after_fb, before_fb, "cloud-error")
        r.check(d_kind == rounds, f"[{name}] fb status 分类 cloud-error +{rounds}（实际 +{d_kind}）")
        d_cloud = vs_fb_delta(after_vs, before_vs, "cloud")
        r.check(d_cloud == rounds, f"[{name}] vs status 错误反馈云端 +{rounds}（实际 +{d_cloud}）")
        d_errored = (after_vs.get("rounds_errored") or 0) - (before_vs.get("rounds_errored") or 0)
        r.check(d_errored == rounds, f"[{name}] vs status 异常轮次 +{rounds}（实际 +{d_errored}）")
        if "internal" in heap_before and "internal" in heap_after:
            before_int = heap_before["internal"]
            after_int = heap_after["internal"]
            print(f"  堆收尾: 内部 free={after_int['free']} B（基线 {before_int['free']}）/"
                  f"最低水位 {after_int['min_free']} B")
            r.check(after_int["min_free"] >= 30 * 1024,
                    f"[{name}] 内部堆最低水位 {after_int['min_free']} B ≥ 30KB")
            free_delta = after_int["free"] - before_int["free"]
            r.check(free_delta >= -16 * 1024,
                    f"[{name}] 内部堆 free 波动 {free_delta:+d} B ≥ -16KB")
        if "psram" in heap_before and "psram" in heap_after:
            psram_delta = heap_after["psram"]["free"] - heap_before["psram"]["free"]
            print(f"  PSRAM free 收尾={heap_after['psram']['free']} B（基线 "
                  f"{heap_before['psram']['free']}，波动 {psram_delta:+d} B）")
            r.check(psram_delta >= -64 * 1024,
                    f"[{name}] PSRAM free 波动 {psram_delta:+d} B ≥ -64KB")
        self.assert_health(name, all_lines, allow_feedback=True)
        r.check(not any(RE_FB_WATCHDOG.search(line) for line in all_lines),
                f"[{name}] 无看门狗超时（正常完成不靠兜底）")

    # ---- 契约探测（§3.3 diag 输出） ----

    def preflight(self) -> None:
        name = "P"
        print("\n===== [P] 固件契约探测（fb status / evt list / log / vs status）=====")
        r = self.result
        fb = self.fb_status()
        r.check(fb["valid"], "[P] `fb status` 可解析（feedback: 活动=… 总播放 …）")
        r.check(fb.get("active") is not None, "[P] `fb status` 含活动字段（活动=…）")

        evt_lines = console_until(
            self.dev, "evt list", lambda text: "说明: evt post" in text, timeout=2.0)
        evt_text = "\n".join(evt_lines)
        m = re.search(r"共 (\d+) 个", evt_text)
        r.check(m is not None and int(m.group(1)) == 14,
                f"[P] `evt list` 事件数 14（实际 {m.group(1) if m else '未解析'}）")
        r.check("FEEDBACK" in evt_text, "[P] `evt list` 含 FEEDBACK 事件（ID=13）")

        log_lines = console_until(
            self.dev, "log", lambda text: re.search(r"(?m)^  \*\s", text) is not None,
            timeout=2.0)
        log_text = "\n".join(log_lines)
        m = re.search(r"已知 TAG（(\d+) 个）", log_text)
        r.check(m is not None and int(m.group(1)) == 17,
                f"[P] `log` TAG 数 17（实际 {m.group(1) if m else '未解析'}）")
        r.check(re.search(r"(?m)^  FB\s", log_text) is not None,
                "[P] `log` TAG 表含 FB（异常反馈）")

        vs = self.vs_status()
        r.check(bool(vs.get("feedback")), "[P] `vs status` 含「错误反馈: 网络 … / 云端 …」行")
        self.dev.console("vs followup off", END_VS_FOLLOWUP, timeout=2.0)

    # ---- 调度 ----

    def run_scenarios(self, selected: list[str]) -> dict[str, dict]:
        """按选定场景跑；返回 {场景: {checks,failed,result}} 供机器可读汇总。"""
        scenario_stats: dict[str, dict] = {}
        started: set[str] = set()
        # A 必须在起 mock 之前：它验证"云端未就绪"的离线唤醒语义。
        # 若设备已连上 mock（外部服务在跑），A 的离线分支不成立 → 记 WARN 跳过，不误判固件。
        if "A" in selected:
            started.add("A")
            if self.vc_ws_ready():
                self.result.warn("[A] 设备已连上 mock（WS READY），离线唤醒场景不适用；"
                                 "跳过 A（先停 mock，或 --scenarios 排除 A）")
                scenario_stats["A"] = {"checks": 0, "failed": 0, "result": "SKIP"}
            else:
                scenario_stats["A"] = self._run_one("A", self.run_a)
        for name in selected:
            if name == "A":
                continue
            started.add(name)
            scenario_stats[name] = self._run_one(
                name, lambda n=name: self._run_mock_scenario(n))
        for name in SCENARIO_ORDER:
            if name not in started:
                scenario_stats[name] = {"checks": 0, "failed": 0, "result": "SKIP"}
        return scenario_stats

    def _run_mock_scenario(self, name: str) -> None:
        if name in ("C", "D"):
            code = 1 if name == "C" else 3001
            kind = "cloud-error" if name == "C" else "tts-fail"
            reason = "cloud-error" if name == "C" else "tts-fail"
            self.run_cloud_error(name, code, kind, reason)
            return
        method = getattr(self, f"run_{name.lower()}")
        method()

    def _run_one(self, name: str, fn) -> dict:
        rows_before = len(self.result.rows)
        try:
            fn()
        except KeyboardInterrupt:
            raise
        except Exception as exc:  # 单场景异常不拖垮其它场景；记为 FAIL
            self.result.check(False, f"[{name}] 场景执行异常: {exc!r}")
        rows = self.result.rows[rows_before:]
        failed = sum(1 for ok, _ in rows if not ok)
        status = "PASS" if failed == 0 else "FAIL"
        print(f"  [{name}] SCENARIO {status}（{len(rows) - failed}/{len(rows)} 条断言）")
        return {"checks": len(rows), "failed": failed, "result": status}


# ================================================================ 主流程

def restore_volume(original: int | None) -> None:
    if original is not None:
        try:
            set_system_volume(original)
            print(f"  系统音量已恢复 {original}%")
        except Exception as exc:  # 恢复失败不能静默，否则用户音量被留在测试档
            print(f"  恢复系统音量失败：{exc}（请手动设为 {original}%）")


def run(args: argparse.Namespace) -> int:
    result = Result()
    selected = parse_scenarios(args.scenarios)

    assets = preflight_assets()
    for info in assets:
        print(f"  素材 {info['name']}: {info['rate']} Hz/{info['bits']} bit/"
              f"{info['channels']} ch, {info['duration']:.2f}s")

    original_volume = None
    volume = get_system_volume()
    if volume is None:
        result.warn("未能读取系统音量（非 macOS？）")
    else:
        result.check(not volume["muted"], "系统输出未静音")
        result.check(volume["volume"] <= SYSTEM_VOLUME_MAX,
                     f"系统音量 {volume['volume']}% ≤ {SYSTEM_VOLUME_MAX}%")
        if args.volume and volume["volume"] != args.volume:
            original_volume = volume["volume"]
            set_system_volume(args.volume)
            print(f"  系统音量 {original_volume}% → {args.volume}%（结束时恢复）")

    if args.dry_run:
        try:
            print(f"  串口: {detect_port(args.port)}（dry-run 不打开串口）")
        except SystemExit as exc:
            print(f"  串口检查跳过: {exc}")
        print("  场景计划（mock 配置 / 断言重点）：")
        for name in selected:
            spec = MOCK_SPECS.get(name)
            print(f"    [{name}] {SCENARIO_TITLES[name]}")
            print(f"          {describe_spec(spec)}")
        print("\n--dry-run：只检查素材/音量/串口/场景计划，未连接设备、未开串口。")
        print("离线自检：uv run tools/error_prompt_test.py --self-test")
        restore_volume(original_volume)
        return 0

    port = detect_port(args.port)
    print(f"  串口: {port}")
    mock = Mock(args.mock_port or DEFAULT_MOCK_PORT)
    dev: Device | None = None
    scenario_stats: dict[str, dict] = {}
    try:
        try:
            dev = Device(port)
        except Exception as exc:  # pyserial 打不开：常见于 monitor 占用/端口权限
            raise SystemExit(f"打开串口失败：{port}（{exc}）\n"
                             "  串口是否被 idf.py monitor 占用？先关掉再跑（脚本独占串口）") from exc
        if not args.no_reset:
            dev.reset()
            print("  已复位设备，等待启动 ...")
            boot_mark = dev.mark()
            dev_ready = dev.wait_line(re.compile(r"APP: ready, say"), args.boot_timeout,
                                      boot_mark) is not None
            result.check(dev_ready, "设备启动就绪（APP: ready）")
            if not dev_ready:
                print("  未等到启动日志：确认串口未被 monitor 占用/设备已插好；"
                      "跳过场景（避免白等一串超时）")
        else:
            dev_ready = bool(dev.console("vc status", END_VC_STATUS, timeout=2.0))
            result.check(dev_ready, "控制台可用（--no-reset 复用已运行设备）")
        dev.pump(0.3)

        if dev_ready:
            runner = Runner(args, dev, result, mock)
            runner.preflight()
            scenario_stats = runner.run_scenarios(selected)
        else:
            print("  设备未就绪：跳过契约探测与 A~J 场景")

        if args.log:
            with open(args.log, "w", encoding="utf-8") as handle:
                handle.write("\n".join(lines_since(dev, 0)))
            print(f"  串口日志已保存: {args.log}")
    finally:
        if dev is not None:
            dev.close()
        mock.stop()
        restore_volume(original_volume)

    # ---- 汇总（逐场景 + 机器可读 + 退出码） ----
    ok = result.summary()
    failed_total = sum(1 for ok_row, _ in result.rows if not ok_row)
    for name in SCENARIO_ORDER:
        stats = scenario_stats.get(name)
        if stats and stats["result"] != "SKIP":
            print(f"  [{name}] {stats['result']}（{stats['checks'] - stats['failed']}/"
                  f"{stats['checks']} 条）")
    summary = {
        "script": "error_prompt_test.py",
        "task": "T3.6",
        "total": len(result.rows),
        "passed": len(result.rows) - failed_total,
        "failed": failed_total,
        "result": "PASS" if ok else "FAIL",
        "scenarios": scenario_stats,
    }
    print("\nSUMMARY " + json.dumps(summary, ensure_ascii=False, sort_keys=True))
    print("ERROR_PROMPT_TEST: " + ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


# ================================================================ 离线自检（--self-test）

def run_self_test() -> bool:
    """离线自检解析/判定逻辑（不连串口、不需要硬件、不需要 mock）。"""
    print("\n===== --self-test：错误提示日志/统计解析与判定逻辑自检（不连设备）=====")
    result = Result()
    r = result

    # ---- VS 契约日志（§3.2） ----
    start_line = ("I (100) VS: 错误反馈（类别=net-offline，code=0，sid=7）→ 提示音（R1）")
    parsed = parse_fb_start(start_line)
    r.check(parsed is not None and parsed["kind"] == "net-offline" and parsed["code"] == 0
            and parsed["sid"] == 7, "S1 解析 VS: 错误反馈（kind/code/sid）")
    r.check(parse_fb_start("I (100) VS: prompt done, feed resumed") is None,
            "S2 负例：普通唤醒提示音日志不会被当成错误反馈")

    done_line = "I (1100) VS: 错误反馈完成（类别=tts-fail，ok=1，时长=1236 ms）→ IDLE"
    parsed = parse_fb_done(done_line)
    r.check(parsed is not None and parsed["kind"] == "tts-fail" and parsed["ok"] == 1
            and parsed["dur_ms"] == 1236, "S3 解析 VS: 错误反馈完成（kind/ok/时长）")

    m = RE_FB_WATCHDOG.search("W (4000) VS: 错误反馈看门狗超时（3000 ms）→ 强制回 IDLE")
    r.check(m is not None and int(m.group(1)) == 3000, "S4 解析错误反馈看门狗超时（3000 ms）")

    states = parse_vs_states([
        "I (1) VS: state LISTENING -> ERROR（sid=7，停留 12 ms，原因=net-offline）",
        "I (2) VS: state ERROR -> IDLE（sid=7，停留 1075 ms，原因=error-done）",
    ])
    r.check(len(states) == 2 and states[0]["reason"] == "net-offline"
            and states[1]["from"] == "ERROR" and states[1]["to"] == "IDLE",
            "S5 解析状态迁移（原因 net-offline / error-done）")

    # ---- FB 契约日志（§3.2） ----
    parsed = parse_fb_play(
        "I (300) FB: 反馈 kind=cloud-error code=1 sid=7 → 播放提示音（cloud-error=23487 样本）")
    r.check(parsed is not None and parsed["kind"] == "cloud-error" and parsed["code"] == 1
            and parsed["samples"] == 23487 and parsed["prompt"] == "cloud-error",
            "S6 解析 FB: 反馈 → 播放提示音（kind/code/样本数）")
    parsed = parse_fb_prompt_done("I (1500) FB: 提示音完成 kind=timeout ok=1 dur=1082 ms")
    r.check(parsed is not None and parsed["kind"] == "timeout" and parsed["dur_ms"] == 1082,
            "S7 解析 FB: 提示音完成（kind/ok/dur）")
    m = RE_FB_DEFER.search("W (900) FB: 播放器被 TTS 流占用，延迟播放（kind=tts-fail，code=3001，sid=7）")
    r.check(m is not None and m.group(1) == "tts-fail", "S8 解析 FB 延迟播放（kind）")
    m = RE_FB_DROP.search("W (900) FB: 反馈被丢弃（kind=cloud-error，reason=busy）")
    r.check(m is not None and m.group(1) == "cloud-error" and m.group(2) == "busy",
            "S9 解析 FB 反馈被丢弃（kind/reason）")

    # ---- fb status（§3.3）：两种措辞都要能解析 ----
    fb_lines = [
        "feedback: 活动=?",
        "  通道统计: 总播放 3 / 完成 2 / 失败 1；延迟 0 丢弃 1 中止 0",
        "  分类: 网络 1 / 云端 2 / 超时 0 / TTS 0；最近时长 1075 ms",
        "  说明: 语音只是当前反馈通道；APP_EVT_FEEDBACK 事件供未来屏幕/灯光通道订阅（T3.6）",
    ]
    st = parse_fb_status(fb_lines)
    r.check(st["valid"] and st["plays"] == 3 and st["done_ok"] == 2 and st["done_fail"] == 1,
            "S10 解析 fb status 通道统计（总播放/完成/失败/延迟/丢弃/中止）")
    r.check(st["by_kind"] == {"net-offline": 1, "cloud-error": 2, "timeout": 0, "tts-fail": 0}
            and st["last_dur_ms"] == 1075, "S11 解析 fb status 分类计数与最近时长")
    st2 = parse_fb_status(["feedback: 活动=net-offline 总播放 1 完成 1 失败 0 延迟 0 丢弃 0 中止 0；"
                           "最近时长 1020 ms"])
    r.check(st2["plays"] == 1 and st2["last_dur_ms"] == 1020,
            "S12 兼容契约式 fb status 一行式措辞")
    after = parse_fb_status(["  通道统计: 总播放 4 / 完成 3 / 失败 1；延迟 0 丢弃 1 中止 0",
                             "  分类: 网络 2 / 云端 2 / 超时 0 / TTS 0；最近时长 1100 ms"])
    r.check(delta(after, st, "plays") == 1 and kind_delta(after, st, "net-offline") == 1,
            "S13 fb status 增量计算（总播放 +1 / 网络 +1）")

    # ---- vs status 错误反馈行（§3.3） ----
    vs = parse_vs_status([
        "voice_session: IDLE（当前 sid=7，VC=IDLE）",
        "  轮次: 开始 3 / 完成 2 / 异常 1 / 中止 0；打断 0 唤醒丢弃 0；超时 0 云端 error 1",
        "  错误反馈: 网络 1 / 云端 2 / 超时 0 / TTS 0；提示音完成 2 失败 1；"
        "最近 kind=cloud-error code=1 时长 1468 ms",
    ])
    r.check(vs["valid"] and vs["state"] == "IDLE" and vs["rounds_errored"] == 1,
            "S14 解析 vs status 状态与轮次行")
    r.check(vs["feedback"]["cloud"] == 2 and vs["feedback"]["ok"] == 2
            and vs["feedback"]["recent_kind"] == "cloud-error"
            and vs["feedback"]["recent_code"] == 1, "S15 解析 vs status 错误反馈行")
    r.check(vs_fb_delta(vs, {"feedback": {"cloud": 1, "ok": 1}}, "cloud") == 1,
            "S16 vs status 错误反馈增量计算（云端 +1）")

    # ---- mic stat 门控行与 hold_mask ----
    gate = parse_gate_line(
        "  门控: holds=2 releases=1 hold_mask=0x01 discards=1 discard_samples=800 "
        "last_discard_ms=4192")
    r.check(gate is not None and gate["hold_mask"] == HOLD_PROMPT and gate["holds"] == 2,
            "S17 解析 mic stat 门控行（hold_mask=0x01）")
    r.check(parse_gate_line("I (1) AFE: feed resumed") is None,
            "S18 负例：普通 AFE 日志不会被当成门控行")
    r.check(gate["hold_mask"] == HOLD_PROMPT and HOLD_PROMPT == 0x1,
            "S19 hold_mask 0x1 == AUDIO_FE_HOLD_PROMPT（R1 唯一原因位）")

    # ---- 顺序/负向/超时/白名单判定 ----
    ordered = ["I (1) PLAYER: stream abort (frames=5, drop 900 B)",
               "I (2) FB: 反馈 kind=tts-fail code=3001 sid=7 → 播放提示音（tts-fail=19769 样本）"]
    r.check(order_ok(ordered, RE_PLAYER_STREAM_STOP, RE_FB_PLAY_ANY),
            "S20 先停 TTS 流再播提示音（顺序正确）")
    reversed_lines = list(reversed(ordered))
    r.check(not order_ok(reversed_lines, RE_PLAYER_STREAM_STOP, RE_FB_PLAY_ANY),
            "S21 负例：提示音先于停流判定为顺序错误")
    r.check(feedback_present(["I (1) VS: 错误反馈（类别=timeout，code=0，sid=1）→ 提示音（R1）"]),
            "S22 能检出错误反馈痕迹（误播判定）")
    r.check(not feedback_present(["I (1) VC: round start sid=1",
                                  "I (2) VS: state SPEAKING -> IDLE（sid=1，停留 100 ms，"
                                  "原因=interrupt）"]),
            "S23 正常轮日志无错误反馈痕迹")
    r.check(not unexpected_errors(["E (1) VS: 会话进入 ERROR（cloud-error，code=1），"
                                   "本轮不自动重试"], allow_feedback=True),
            "S24 预期异常反馈 E 日志不判失败（allow_feedback=True）")
    r.check(bool(unexpected_errors(["E (1) VS: 会话进入 ERROR（cloud-error，code=1），"
                                    "本轮不自动重试"], allow_feedback=False)),
            "S25 正常轮出现错误反馈 E 日志判失败（allow_feedback=False）")
    r.check(bool(unexpected_errors(["E (1) PLAYER: assert failed"], allow_feedback=True)),
            "S26 panic/assert 始终判失败")
    r.check(timeout_duration_ok(10.1) and timeout_duration_ok(12.9)
            and not timeout_duration_ok(8.0) and not timeout_duration_ok(13.5),
            "S27 超时耗时判定（10.1/12.9 PASS，8.0/13.5 FAIL）")
    r.check(parse_scenarios("") == list(SCENARIO_ORDER), "S28 --scenarios 缺省=全部 A~J")
    r.check(parse_scenarios("C,A,J") == ["A", "C", "J"], "S29 --scenarios 按 A~J 顺序去重")
    r.check(parse_scenarios("B-D") == ["B", "C", "D"], "S30 --scenarios 支持范围 B-D")
    try:
        parse_scenarios("K")
        rejected = False
    except SystemExit:
        rejected = True
    r.check(rejected, "S31 --scenarios 非法场景名被拒绝")
    r.check(MOCK_SPECS["D"][-1] == "3001" and "--error-code" in MOCK_SPECS["D"],
            "S32 场景 D mock 配置下发 error-code 3001")
    r.check("after-tts-start" in MOCK_SPECS["I"],
            "S33 场景 I mock 配置 after-tts-start（SPEAKING 中报错）")
    r.check(MOCK_SPECS["F"] == ["--tts-bad-codec"],
            "S34 场景 F mock 配置 --tts-bad-codec")
    r.check("A" not in MOCK_SPECS,
            "S35 场景 A 不启 mock（离线唤醒语义）")
    r.check(VS_FB_KIND_KEY["net-offline"] == "net"
            and VS_FB_KIND_KEY["cloud-error"] == "cloud"
            and VS_FB_KIND_KEY["timeout"] == "timeout"
            and VS_FB_KIND_KEY["tts-fail"] == "tts", "S36 kind → vs status 字段映射")
    r.check(RE_PROMPT_DONE.search("I (1) VS: prompt done, feed resumed") is not None
            and parse_fb_start("I (1) VS: prompt done, feed resumed") is None,
            "S37 离线唤醒负例：`prompt done` 不计入错误反馈")
    r.check(RE_VS_SPEAKING_DONE.search("I (1) VS: speaking done（ok=0）") is not None,
            "S38 解析 VS: speaking done（ok=0）")

    ok = result.summary()
    print("\nERROR_PROMPT_TEST: SELF-TEST " + ("PASS" if ok else "FAIL")
          + f"（{len(result.rows)} 项）")
    return ok


# ================================================================ CLI

def main() -> int:
    parser = argparse.ArgumentParser(
        description="T3.6 错误提示真机自测（场景 A~J；mock 注入错误码/时机，串口断言）",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--port", default=None, help="设备串口；缺省自动探测 /dev/cu.usbmodem*")
    parser.add_argument("--volume", type=int, default=50,
                        help=f"回放期间临时系统音量（0~{SYSTEM_VOLUME_MAX}；结束恢复）")
    parser.add_argument("--mock-port", type=int, default=DEFAULT_MOCK_PORT,
                        help="本地 mock 端口（需与设备 CONFIG_AI_WS_URL 一致）")
    parser.add_argument("--no-mock", action="store_true",
                        help="不拉起 mock（复用已有服务；mock 侧断言跳过，需自行按场景配置 mock）")
    parser.add_argument("--no-reset", action="store_true", help="不复位设备（设备已在跑时用）")
    parser.add_argument("--scenarios", default="",
                        help="选择场景（逗号/范围，如 A,C,J 或 A-C；缺省全部 A~J）")
    parser.add_argument("--wake-attempts", type=int, default=3,
                        help="场景 A 唤醒回放重试次数（环境噪声下唤醒率会下降）")
    parser.add_argument("--stability-rounds", type=int, default=10,
                        help="场景 J 连续错误反馈轮数（契约默认 10）")
    parser.add_argument("--boot-timeout", type=float, default=BOOT_TIMEOUT_DEFAULT,
                        help="复位后等启动就绪的超时（秒）")
    parser.add_argument("--mock-timeout", type=float, default=MOCK_TIMEOUT_DEFAULT,
                        help="mock 重启后等设备 WS READY 的超时（秒）")
    parser.add_argument("--log", default="", help="把完整串口日志写到该文件")
    parser.add_argument("--dry-run", action="store_true",
                        help="只检查素材/音量/串口/场景计划（不打开串口、不连设备）")
    parser.add_argument("--self-test", action="store_true",
                        help="离线自检日志解析/判定逻辑（不连设备、不需要硬件）")
    args = parser.parse_args()

    if args.self_test:
        return 0 if run_self_test() else 1
    if not 0 <= args.volume <= SYSTEM_VOLUME_MAX:
        raise SystemExit(f"--volume 必须在 0~{SYSTEM_VOLUME_MAX}（用户定稿上限 50%）")
    if args.stability_rounds < 1:
        raise SystemExit(f"--stability-rounds 必须 >= 1（当前 {args.stability_rounds}）")
    if args.wake_attempts < 1:
        raise SystemExit(f"--wake-attempts 必须 >= 1（当前 {args.wake_attempts}）")
    try:
        parse_scenarios(args.scenarios)
    except SystemExit as exc:
        raise SystemExit(str(exc)) from exc

    print("===== T3.6 错误提示真机自测（error_prompt_test.py）=====")
    try:
        return run(args)
    except KeyboardInterrupt:
        print("\n中断")
        return 130


if __name__ == "__main__":
    sys.exit(main())
