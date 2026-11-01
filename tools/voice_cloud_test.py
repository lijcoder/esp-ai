#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["pyserial>=3.5"]
# ///
"""voice_cloud（T2.5）真机端到端自测：唤醒 → 上行 Opus → 下行事件 → 统计断言。

一条命令完成：起本地 mock 云端 → 复位设备 → 回放唤醒词 → 回放交互语音 →
断言"设备把音频发上去了、云端事件按 sid 收对了、统计数据自洽"→ 再测 vc stop / cancel
两条收尾路径 → 汇总 PASS/FAIL（退出码 0/1）。

为什么不用 `idf.py monitor` 手工看：断言要能复跑。本脚本与 `voice_test.py` 同样用
Mac 扬声器回放 `tools/` 里的 WAV（agent 不能说话），音量遵守用户定稿（系统输出正常 31%、
上限 50%；默认 `--volume 50` 临时调到上限后恢复）。

依赖与素材：`tools/ws_mock_server.py`（T1.5）、`tools/lexin.wav`、`tools/今天天气如何.wav`。
脚本**独占串口**（运行前关掉 `idf.py monitor`）；mock 端口（默认 8765）若已被占用则复用，
但此时跳过 mock 侧断言（会打印 WARN）。

用法：
    uv run tools/voice_cloud_test.py
    uv run tools/voice_cloud_test.py --volume 31 --log /tmp/vc_test.log
    uv run tools/voice_cloud_test.py --no-mock          # 已有 mock 在跑（跳过 mock 侧断言）
    uv run tools/voice_cloud_test.py --dry-run          # 只检查素材/音量/串口
"""

from __future__ import annotations

import argparse
import os
import re
import socket
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))

# 与 voice_test.py 共用串口封装/音量/素材/断言工具，避免两套实现漂移
from voice_test import (  # noqa: E402  （PEP 723 脚本按脚本目录入 sys.path）
    Device,
    Result,
    detect_port,
    error_lines,
    get_system_volume,
    preflight_assets,
    set_system_volume,
)

MOCK_SCRIPT = os.path.join(HERE, "ws_mock_server.py")
WAKE_WAV = "lexin.wav"
SPEECH_WAV = "今天天气如何.wav"
DEFAULT_MOCK_PORT = 8765
SYSTEM_VOLUME_MAX = 50

# 设备侧日志断言（VC 组件与 diag 输出）
RE_WS_READY = re.compile(r"WS: 握手完成（hello_ack）")
# T3.1 起唤醒编排归 voice_session（TAG VS）；兼容旧固件的 APP 前缀
RE_WAKE = re.compile(r"(?:APP|VS): wake detected")
RE_FEED_PAUSE = re.compile(r"AFE: feed paused")
RE_PROMPT_PLAY = re.compile(r"PLAYER: play \d+ samples")
RE_PROMPT_DONE = re.compile(r"(?:APP|VS): prompt done, feed resumed")
RE_ROUND_START = re.compile(r"VC: round start sid=(\d+)")
RE_FIRST_FRAME = re.compile(r"VC: 上行首帧 (\d+) us")
RE_AUDIO_END = re.compile(
    r"VC: audio_end sid=(\d+) 原因=([a-z-]+) 上行 (\d+) 帧/(\d+) B 用时 (\d+) ms"
)
RE_TTS_START = re.compile(r"VC: TTS start sid=(\d+) codec=(\w+) sr=(\d+)")
RE_TTS_END = re.compile(r"VC: TTS end sid=(\d+)（本轮下行音频 (\d+) 帧）")
RE_ROUND_DONE = re.compile(r"VC: round done sid=(\d+)")
RE_ROUND_ABORT = re.compile(r"VC: round aborted sid=(\d+)（([^）]+)）")
RE_RESPONSE_TIMEOUT = re.compile(r"VC: 等待云端响应超时")

RE_EVT_ASR_FINAL = re.compile(r"evt trace: ASR_FINAL")
RE_EVT_TTS_START = re.compile(r"evt trace: TTS_START")
RE_EVT_TTS_END = re.compile(r"evt trace: TTS_END")
RE_EVT_VS_STATE = re.compile(r"evt trace: VOICE_STATE_CHANGED")

# voice_session（T3.1）：状态迁移日志与命令输出
RE_VS_STATE = re.compile(
    r"VS: state (\w+) -> (\w+)（sid=(\d+)，停留 (\d+) ms，原因=([a-z-]+)）")

# Mock 侧日志断言
RE_MOCK_HELLO = re.compile(r"<-- hello device_id=(\S+) ver=(\d+)")
RE_MOCK_AUDIO_START = re.compile(r"<-- audio_start sid=(\d+)")
RE_MOCK_AUDIO_END = re.compile(r"<-- audio_end sid=(\d+)（本轮上行 (\d+) 帧）")
RE_MOCK_TTS_END = re.compile(r"--> tts_end sid=(\d+)")
MOCK_UP_FRAME = "<-- 上行音频 seq="
MOCK_SEQ_GAP = "seq 跳变"
MOCK_BAD_FRAME = "坏帧"

# console 命令结束标记（esp_console 无统一结束符，用命令最后一行输出当哨兵）
END_VC_STATUS = re.compile(r"说明: 唤醒后由 voice_session 自动起一轮")
END_VC_RESET = re.compile(r"vc 统计已清零")
END_VC_START = re.compile(r"已请求发起一轮")
END_VC_STOP = re.compile(r"已请求优雅收尾")
END_VC_CANCEL = re.compile(r"已请求立即放弃")
END_VS_STATUS = re.compile(r"说明: 唤醒自动进 LISTENING")
END_VS_RESET = re.compile(r"vs 统计已清零")
END_EVT_TRACE_ON = re.compile(r"事件 trace 已开启|事件 trace 已经开启")
END_SYS_HEAP = re.compile(r"PSRAM.*(最低水位|未初始化)")
END_SYS_TASKS = re.compile(r"共 \d+ 个任务")


# ---------------------------------------------------------------- mock 云端

def port_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.3)
        return sock.connect_ex(("127.0.0.1", port)) == 0


class MockServer:
    def __init__(self, port: int, inject_late: bool = False) -> None:
        self.port = port
        self.inject_late = inject_late
        self.proc: subprocess.Popen | None = None
        self.owned = False
        self.log_path = os.path.join(tempfile.gettempdir(), f"vc_mock_{port}.log")

    def start(self) -> None:
        if port_open(self.port):
            print(f"  mock: 端口 {self.port} 已被占用，复用现有服务（跳过 mock 侧断言）")
            return
        log = open(self.log_path, "w", encoding="utf-8", buffering=1)
        cmd = ["uv", "run", MOCK_SCRIPT,
               "--host", "0.0.0.0", "--port", str(self.port)]
        if self.inject_late:
            cmd.append("--inject-late")
        self.proc = subprocess.Popen(
            cmd, stdout=log, stderr=subprocess.STDOUT, cwd=HERE,
        )
        self.owned = True
        deadline = time.time() + 15
        while time.time() < deadline:
            if port_open(self.port):
                print(f"  mock: 已启动（端口 {self.port}）")
                return
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

    def text(self) -> str:
        try:
            with open(self.log_path, encoding="utf-8", errors="replace") as handle:
                return handle.read()
        except OSError:
            return ""


# ---------------------------------------------------------------- console 解析

def vc_status(dev: Device) -> dict:
    out = dev.console("vc status", END_VC_STATUS, timeout=3.0)
    stat: dict = {"raw": out}
    for line in out:
        m = re.search(r"voice_cloud: (\w+)（当前 sid=(\d+)）", line)
        if m:
            stat["state"] = m.group(1)
            stat["sid"] = int(m.group(2))
        m = re.search(
            r"轮次: 发起 (\d+) / 完成 (\d+) / 中止 (\d+)；最近 sid=(\d+) 用时 (\d+) ms", line)
        if m:
            stat["rounds_started"] = int(m.group(1))
            stat["rounds_done"] = int(m.group(2))
            stat["rounds_aborted"] = int(m.group(3))
            stat["last_round_ms"] = int(m.group(5))
        m = re.search(
            r"上行: (\d+) 帧 / (\d+) 字节（([\d.]+) B/帧）；编码失败 (\d+) 入队失败 (\d+) "
            r"读超时 (\d+)；首帧 (\d+) us", line)
        if m:
            stat["up_frames"] = int(m.group(1))
            stat["up_bytes"] = int(m.group(2))
            stat["bytes_per_frame"] = float(m.group(3))
            stat["encode_errors"] = int(m.group(4))
            stat["send_errors"] = int(m.group(5))
            stat["read_timeouts"] = int(m.group(6))
            stat["first_frame_us"] = int(m.group(7))
        m = re.search(
            r"下行: 文本 (\d+) / 音频帧 (\d+)；sid 丢弃 (\d+) 解析失败 (\d+) 云端 error (\d+)", line)
        if m:
            stat["rx_text"] = int(m.group(1))
            stat["rx_audio_frames"] = int(m.group(2))
            stat["dropped_sid"] = int(m.group(3))
            stat["parse_errors"] = int(m.group(4))
            stat["cloud_errors"] = int(m.group(5))
    return stat


def vs_status(dev: Device) -> dict:
    """解析 `vs status`（T3.1 会话状态机）。"""
    out = dev.console("vs status", END_VS_STATUS, timeout=3.0)
    stat: dict = {"raw": out}
    for line in out:
        m = re.search(r"voice_session: (\w+)", line)
        if m:
            stat["state"] = m.group(1)
        m = re.search(
            r"轮次: 开始 (\d+) / 完成 (\d+) / 异常 (\d+) / 中止 (\d+)；打断 (\d+) 唤醒丢弃 (\d+)；"
            r"超时 (\d+) 云端 error (\d+)", line)
        if m:
            stat["rounds_started"] = int(m.group(1))
            stat["rounds_completed"] = int(m.group(2))
            stat["rounds_errored"] = int(m.group(3))
            stat["round_aborts"] = int(m.group(4))
            stat["interrupts"] = int(m.group(5))
            stat["wakes_dropped"] = int(m.group(6))
            stat["timeouts"] = int(m.group(7))
            stat["cloud_errors"] = int(m.group(8))
        m = re.search(r"迁移: 共 (\d+) 次；最近 sid=(\d+)", line)
        if m:
            stat["transitions"] = int(m.group(1))
            stat["sid"] = int(m.group(2))
    return stat


def wait_line(dev: Device, pattern: re.Pattern, timeout: float, mark: int) -> str | None:
    if dev.wait_line(pattern, timeout, mark) is None:
        return None
    for _, line in dev.lines_since(mark):
        if pattern.search(line):
            return line
    return None


def lines_since(dev: Device, mark: int) -> list[str]:
    return [line for _, line in dev.lines_since(mark)]


def parse_heap(dev: Device) -> dict:
    out = dev.console("sys heap", END_SYS_HEAP, timeout=3.0)
    heap: dict = {}
    for line in out:
        m = re.search(
            r"(内部 RAM|PSRAM   ): 总量 (\d+) B, 剩余 (\d+) B, 最大块 (\d+) B, "
            r"最低水位 (\d+) B, 峰值占用 (\d+) B", line)
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


def vc_task_stack_free(dev: Device) -> int | None:
    out = dev.console("sys tasks", END_SYS_TASKS, timeout=3.0)
    for line in out:
        m = re.search(r"^vc\s+\S\s+\d+\s+(\d+) B", line.strip())
        if m:
            return int(m.group(1))
    return None


# ---------------------------------------------------------------- 主流程

def run(args: argparse.Namespace) -> int:
    result = Result()
    wake_path = os.path.join(HERE, WAKE_WAV)
    speech_path = os.path.join(HERE, SPEECH_WAV)
    original_volume = None

    assets = preflight_assets()
    for info in assets:
        print(f"  素材 {info['name']}: {info['rate']} Hz/{info['bits']} bit/"
              f"{info['channels']} ch, {info['duration']:.2f}s, 峰值 {info['peak']}")

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
            port = detect_port(args.port)
            print(f"  串口: {port}")
            Device(port).close()
        except SystemExit as exc:
            print(f"  串口检查跳过: {exc}")
        if original_volume is not None:  # dry-run 也要把临时调高的音量恢复
            set_system_volume(original_volume)
            print(f"  系统音量已恢复 {original_volume}%")
        print("\n--dry-run：只做环境检查，未连接设备。")
        return 0

    port = detect_port(args.port)
    print(f"  串口: {port}")
    mock = MockServer(args.mock_port or DEFAULT_MOCK_PORT, inject_late=args.inject_late)
    dev: Device | None = None
    mark = 0
    try:
        if args.no_mock:
            print("  mock: --no-mock，不启动本地 mock（mock 侧断言跳过）")
        else:
            mock.start()
        dev = Device(port)
        if not args.no_reset:
            dev.reset()
            print("  已复位设备，等待启动/联网/WS 握手 ...")
        if wait_line(dev, RE_WS_READY, args.boot_timeout, 0) is None:
            result.check(False, "设备启动并完成 WS 握手（hello_ack）")
            return 1
        result.check(True, "设备启动并完成 WS 握手（hello_ack）")
        dev.pump(0.3)

        # 基线：统计清零 + 打开事件 trace（验证 voice_cloud 真的往 event_bus 投了事件）
        dev.console("vc reset", END_VC_RESET, timeout=2.0)
        dev.console("vs reset", END_VS_RESET, timeout=2.0)
        dev.console("evt trace on", END_EVT_TRACE_ON, timeout=2.0)
        base = vc_status(dev)
        result.check(base.get("state") == "IDLE", f"初始状态 IDLE（实际 {base.get('state')}）")
        base_vs = vs_status(dev)
        result.check(base_vs.get("state") == "IDLE",
                     f"VS 初始状态 IDLE（实际 {base_vs.get('state')}）")

        # ---- 一轮真实语音：唤醒 → 提示音 → 交互语音 → 上行 → 下行 ----
        mark = dev.mark()
        dev.play(wake_path)
        wake_seen = wait_line(dev, RE_PROMPT_DONE, 8.0, mark)
        round_lines = lines_since(dev, mark)
        result.check(wake_seen is not None, "唤醒提示音播放完成（prompt done, feed resumed）")
        result.check(any(RE_WAKE.search(l) for l in round_lines), "日志出现唤醒事件")
        result.check(any(RE_FEED_PAUSE.search(l) for l in round_lines), "R1：播放前暂停收音")
        result.check(any(RE_PROMPT_PLAY.search(l) for l in round_lines), "提示音开始播放")

        dev.play(speech_path)  # 交互语音，模拟用户提问（1.6s）
        audio_end_line = wait_line(dev, RE_AUDIO_END, args.round_timeout, mark)
        if audio_end_line is None:
            result.check(False, f"{args.round_timeout}s 内收到 audio_end")
            audio_end_line = ""
        m = RE_AUDIO_END.search(audio_end_line) if audio_end_line else None
        if m is None:
            result.check(False, "解析 audio_end 行")
            return 1
        sid_dev = int(m.group(1))
        reason = m.group(2)
        up_frames = int(m.group(3))
        up_bytes = int(m.group(4))
        allow_max = args.allow_max_duration
        result.check(reason == "vad-silence" or allow_max,
                     f"audio_end 原因={reason}（期望 vad-silence）")

        round_start_line = wait_line(dev, RE_ROUND_START, 0.1, mark) or ""
        m = RE_ROUND_START.search(round_start_line)
        result.check(m is not None and int(m.group(1)) == sid_dev, "round start 与 audio_end 的 sid 一致")
        first_line = wait_line(dev, RE_FIRST_FRAME, 0.1, mark) or ""
        m = RE_FIRST_FRAME.search(first_line)
        first_us = int(m.group(1)) if m else 10**9
        result.check(first_us < 200000, f"上行首帧 {first_us} us < 200000 us")

        tts_start_line = wait_line(dev, RE_TTS_START, 10.0, mark) or ""
        m = RE_TTS_START.search(tts_start_line)
        result.check(m is not None and m.group(2) == "mp3" and m.group(3) == "24000",
                     f"tts_start 解析（mp3/24000）：{m.group(0) if m else '未收到'}")
        tts_end_line = wait_line(dev, RE_TTS_END, 10.0, mark) or ""
        m = RE_TTS_END.search(tts_end_line)
        tts_frames = int(m.group(2)) if m else -1
        result.check(tts_end_line != "", "收到 tts_end")
        round_done_line = wait_line(dev, RE_ROUND_DONE, 5.0, mark) or ""
        result.check(round_done_line != "", "本轮正常结束（round done）")

        all_lines = lines_since(dev, 0)
        result.check(any(RE_EVT_ASR_FINAL.search(l) for l in all_lines),
                     "event_bus 收到 ASR_FINAL")
        result.check(any(RE_EVT_TTS_START.search(l) for l in all_lines),
                     "event_bus 收到 TTS_START")
        result.check(any(RE_EVT_TTS_END.search(l) for l in all_lines),
                     "event_bus 收到 TTS_END")

        # ---- T3.1：会话状态机（voice_session） ----
        # vc 的 round done 早于 TTS 排空：先等播放完成回调把 SPEAKING 收回 IDLE（R2 之后）
        wait_line(dev, re.compile(r"VS: state SPEAKING -> IDLE"), 12.0, mark)
        vs_lines = lines_since(dev, 0)
        vs_seq = [(m.group(1), m.group(2), int(m.group(3)))
                  for l in vs_lines for m in [RE_VS_STATE.search(l)] if m]
        got = [(a, b) for a, b, _ in vs_seq]
        expect = [("IDLE", "LISTENING"), ("LISTENING", "THINKING"),
                  ("THINKING", "SPEAKING"), ("SPEAKING", "IDLE")]
        result.check(got[:4] == expect,
                     f"VS 状态迁移 IDLE→LISTENING→THINKING→SPEAKING→IDLE（实际 {got[:6]}）")
        result.check(bool(vs_seq) and all(sid == sid_dev for _, _, sid in vs_seq),
                     "VS 迁移日志的 sid 与本轮一致")
        result.check(any(RE_EVT_VS_STATE.search(l) for l in vs_lines),
                     "event_bus 收到 VOICE_STATE_CHANGED")
        vst = vs_status(dev)
        result.check(vst.get("state") == "IDLE", f"VS 轮次结束后 IDLE（实际 {vst.get('state')}）")
        result.check(vst.get("rounds_started") == 1 and vst.get("rounds_completed") == 1,
                     f"VS 轮次统计：开始 {vst.get('rounds_started')} / 完成 {vst.get('rounds_completed')}")
        result.check(vst.get("rounds_errored") == 0 and vst.get("timeouts") == 0,
                     "VS 无异常/超时轮次")

        # ---- 统计自洽 ----
        st = vc_status(dev)
        d = {key: st.get(key, 0) - base.get(key, 0) for key in (
            "rounds_started", "rounds_done", "rounds_aborted", "up_frames", "up_bytes",
            "rx_audio_frames", "dropped_sid", "parse_errors", "cloud_errors")}
        result.check(d["rounds_started"] == 1 and d["rounds_done"] == 1,
                     f"轮次统计：发起 {d['rounds_started']} / 完成 {d['rounds_done']}")
        result.check(st.get("state") == "IDLE", "轮次结束后回到 IDLE")
        result.check(st.get("sid") == sid_dev, f"vc 当前 sid={st.get('sid')} 与日志一致")
        result.check(d["up_frames"] == up_frames and up_frames >= 60,
                     f"上行帧数 {up_frames}（≥60 且与统计一致）")
        bpf = st.get("bytes_per_frame", 0.0)
        result.check(20.0 <= bpf <= 60.0, f"平均帧长 {bpf:.1f} B/帧（VBR 预期 20~60）")
        result.check(first_us < 200000 and st.get("first_frame_us", 10**9) < 200000,
                     f"统计里的首帧耗时 {st.get('first_frame_us')} us < 200000 us")
        result.check(tts_frames == d["rx_audio_frames"],
                     f"下行音频帧 {d['rx_audio_frames']} 与 tts_end 计数一致")
        result.check(d["rx_audio_frames"] >= 90, f"下行音频帧 {d['rx_audio_frames']} ≥ 90")
        if args.inject_late:
            result.check(d["dropped_sid"] >= 1,
                         f"注入的迟到事件被丢弃（dropped_sid={d['dropped_sid']}）")
        else:
            result.check(d["dropped_sid"] == 0, "无 sid 不匹配丢弃")
        result.check(d["parse_errors"] == 0, "下行 JSON 解析无失败")
        result.check(d["cloud_errors"] == 0, "无云端 error 事件")

        # ---- mock 侧（只有自己拉起 mock 时才能读日志） ----
        if mock.owned:
            dev.pump(0.3)
            log = mock.text()
            result.check(RE_MOCK_HELLO.search(log) is not None, "mock 收到 hello 并回 hello_ack")
            m_start = RE_MOCK_AUDIO_START.search(log)
            m_end = RE_MOCK_AUDIO_END.search(log)
            result.check(m_start is not None and m_end is not None,
                         "mock 收到 audio_start / audio_end")
            if m_start is not None and m_end is not None:
                result.check(int(m_start.group(1)) == sid_dev == int(m_end.group(1)),
                             f"mock 侧 sid 一致（{m_start.group(1)}/{m_end.group(1)}/{sid_dev}）")
                result.check(int(m_end.group(2)) == up_frames,
                             f"mock 统计上行 {m_end.group(2)} 帧 == 设备 {up_frames} 帧")
            frame_count = log.count(MOCK_UP_FRAME)
            result.check(frame_count == up_frames, f"mock 逐帧收到 {frame_count} 帧")
            result.check(MOCK_SEQ_GAP not in log, "上行 seq 无跳变")
            result.check(MOCK_BAD_FRAME not in log, "mock 未发现坏帧")
            result.check(RE_MOCK_TTS_END.search(log) is not None, "mock 已完成一轮 TTS 下发")
        else:
            result.warn("mock 端口被复用：跳过 mock 侧断言")

        # ---- vc stop：优雅收尾（stop-request） ----
        mark = dev.mark()
        dev.console("vc start", END_VC_START, timeout=2.0)
        wait_line(dev, RE_ROUND_START, 5.0, mark)
        dev.pump(0.8)
        dev.console("vc stop", END_VC_STOP, timeout=2.0)
        m = RE_AUDIO_END.search(wait_line(dev, RE_AUDIO_END, 5.0, mark) or "")
        result.check(m is not None and m.group(2) == "stop-request",
                     f"vc stop 收尾原因={m.group(2) if m else '未收到 audio_end'}")
        wait_line(dev, RE_ROUND_DONE, 10.0, mark)

        # ---- vc cancel：立即放弃（round aborted / cancel） ----
        mark = dev.mark()
        dev.console("vc start", END_VC_START, timeout=2.0)
        wait_line(dev, RE_ROUND_START, 5.0, mark)
        dev.pump(0.6)
        dev.console("vc cancel", END_VC_CANCEL, timeout=2.0)
        m = RE_ROUND_ABORT.search(wait_line(dev, RE_ROUND_ABORT, 5.0, mark) or "")
        result.check(m is not None and "cancel" in m.group(2),
                     f"vc cancel 生效（{m.group(2) if m else '未收到 round aborted'}）")

        st_after = vc_status(dev)
        result.check(st_after.get("rounds_aborted", 0) - st.get("rounds_aborted", 0) == 1,
                     "中止计数 +1")
        result.check(st_after.get("rounds_done", 0) - st.get("rounds_done", 0) == 1,
                     "stop 后仍正常收到 tts_end（完成计数 +1）")

        # ---- 内存 / 任务栈 ----
        heap = parse_heap(dev)
        if "internal" in heap:
            internal = heap["internal"]
            print(f"  内部 RAM: 峰值占用 {internal['peak']} B，最低水位 {internal['min_free']} B，"
                  f"最大块 {internal['largest']} B")
            result.check(internal["min_free"] >= 30 * 1024,
                         f"内部堆最低水位 {internal['min_free']} B ≥ 30KB")
        if "psram" in heap:
            print(f"  PSRAM: 峰值占用 {heap['psram']['peak']} B，剩余 {heap['psram']['free']} B")
        stack_free = vc_task_stack_free(dev)
        result.check(stack_free is not None and stack_free >= 1024,
                     f"vc 任务栈剩余 {stack_free} B ≥ 1024 B")

        # ---- 健康检查 ----
        # 只按 ERROR/panic 判定；`W` 级的 "round aborted"（cancel 用例的预期日志）不算错误
        candidates = [
            line for line in lines_since(dev, 0)
            if re.search(r"^\s*E \(\d+\)", line)
            or re.search(r"panic|Guru Meditation|stack overflow|StoreProhibited", line)
        ]
        errors = [line for line in candidates if error_lines([line])]
        result.check(len(errors) == 0, f"无语音链路 ERROR/panic（{len(errors)} 条）")
        for line in errors[:5]:
            print(f"    {line}")

        if args.log:
            with open(args.log, "w", encoding="utf-8") as handle:
                handle.write("\n".join(lines_since(dev, 0)))
            print(f"  串口日志已保存: {args.log}")
        return 0 if result.summary() else 1
    finally:
        if dev is not None:
            dev.close()
        mock.stop()
        if original_volume is not None:
            set_system_volume(original_volume)
            print(f"  系统音量已恢复 {original_volume}%")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="voice_cloud（T2.5）真机端到端自测",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--port", default=None, help="设备串口；缺省自动探测 /dev/cu.usbmodem*")
    parser.add_argument("--volume", type=int, default=50,
                        help=f"回放期间临时系统音量（0~{SYSTEM_VOLUME_MAX}；结束恢复）")
    parser.add_argument("--mock-port", type=int, default=DEFAULT_MOCK_PORT,
                        help="本地 mock 端口（需与设备 CONFIG_AI_WS_URL 一致）")
    parser.add_argument("--inject-late", action="store_true",
                        help="让 mock 每轮注入一个 sid+1 的迟到事件，验证端侧 sid 过滤")
    parser.add_argument("--no-mock", action="store_true",
                        help="不启动 mock（已有服务在跑时用；mock 侧断言会跳过）")
    parser.add_argument("--no-reset", action="store_true", help="不复位设备（设备已在跑时用）")
    parser.add_argument("--boot-timeout", type=float, default=40.0, help="复位后等 WS 握手的超时（秒）")
    parser.add_argument("--round-timeout", type=float, default=12.0, help="等 audio_end 的超时（秒）")
    parser.add_argument("--allow-max-duration", action="store_true",
                        help="允许 audio_end 因 8s 上限收尾（环境收不到人声时放宽）")
    parser.add_argument("--log", default="", help="把完整串口日志写到该文件")
    parser.add_argument("--dry-run", action="store_true", help="只检查素材/音量/串口")
    args = parser.parse_args()

    if not 0 <= args.volume <= SYSTEM_VOLUME_MAX:
        raise SystemExit(f"--volume 必须在 0~{SYSTEM_VOLUME_MAX}（用户定稿上限 50%）")

    print("===== voice_cloud 端到端自测（T2.5） =====")
    try:
        return run(args)
    except KeyboardInterrupt:
        print("\n中断")
        return 130


if __name__ == "__main__":
    sys.exit(main())
