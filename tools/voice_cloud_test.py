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

# 真 ASR（--asr，tools/mock_asr.py）：设备侧 asr_final 文本 + mock 侧识别日志
RE_ASR_FINAL_TEXT = re.compile(r"VC: ASR final sid=(\d+): (.+)")
RE_MOCK_ASR_TEXT = re.compile(r"\[ASR\] 识别结果: '([^']*)'")
ASR_FALLBACK_TEXT = "今天天气不错，我们来试试端云联调。"

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
END_VS_FOLLOWUP = re.compile(r"连续对话续听窗口：(开|关)")
END_VS_INTERRUPT = re.compile(r"已打断当前会话")
END_EVT_TRACE_ON = re.compile(r"事件 trace 已开启|事件 trace 已经开启")
END_SYS_HEAP = re.compile(r"PSRAM.*(最低水位|未初始化)")
END_SYS_TASKS = re.compile(r"共 \d+ 个任务")


# ---------------------------------------------------------------- mock 云端

def port_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.3)
        return sock.connect_ex(("127.0.0.1", port)) == 0


class MockServer:
    def __init__(self, port: int, inject_late: bool = False, asr: bool = False,
                 asr_model_dir: str = "") -> None:
        self.port = port
        self.inject_late = inject_late
        self.asr = asr
        self.asr_model_dir = asr_model_dir
        self.proc: subprocess.Popen | None = None
        self.owned = False
        self.log_path = os.path.join(tempfile.gettempdir(), f"vc_mock_{port}.log")

    def start(self) -> None:
        if port_open(self.port):
            print(f"  mock: 端口 {self.port} 已被占用，复用现有服务（跳过 mock 侧断言）")
            return
        log = open(self.log_path, "w", encoding="utf-8", buffering=1)
        cmd = ["uv", "run"]
        if self.asr:
            # mock 的 ASR 依赖不在本脚本的 PEP 723 里：由 uv --with 临时装进 mock 子进程环境
            cmd += ["--with", "av", "--with", "numpy", "--with", "sherpa-onnx"]
        cmd += [MOCK_SCRIPT, "--host", "0.0.0.0", "--port", str(self.port)]
        if self.inject_late:
            cmd.append("--inject-late")
        if self.asr:
            cmd += ["--asr", "sensevoice"]
            if self.asr_model_dir:
                cmd += ["--asr-model-dir", self.asr_model_dir]
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
        m = re.search(r"续听: 续听轮 (\d+) / 命中 (\d+)；超时退出 (\d+) / 上限退出 (\d+)", line)
        if m:
            stat["followup_rounds"] = int(m.group(1))
            stat["followup_hits"] = int(m.group(2))
            stat["followup_timeouts"] = int(m.group(3))
            stat["followup_max_exits"] = int(m.group(4))
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
    mock = MockServer(args.mock_port or DEFAULT_MOCK_PORT, inject_late=args.inject_late,
                      asr=args.asr, asr_model_dir=args.asr_model_dir)
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
        # T3.7：单轮用例关续听窗口；--followup 用例显式打开
        dev.console("vs followup on" if args.followup else "vs followup off",
                    END_VS_FOLLOWUP, timeout=2.0)
        dev.console("evt trace on", END_EVT_TRACE_ON, timeout=2.0)
        base = vc_status(dev)
        result.check(base.get("state") == "IDLE", f"初始状态 IDLE（实际 {base.get('state')}）")
        base_vs = vs_status(dev)
        result.check(base_vs.get("state") == "IDLE",
                     f"VS 初始状态 IDLE（实际 {base_vs.get('state')}）")

        # ---- 一轮真实语音：唤醒 → 提示音 → 交互语音 → 上行 → 下行 ----
        mark = dev.mark()
        wake_seen = None
        for attempt in range(1, args.wake_attempts + 1):
            dev.play(wake_path)
            wake_seen = wait_line(dev, RE_PROMPT_DONE, 8.0, mark)
            if wake_seen is not None:
                break
            if attempt < args.wake_attempts:
                print(f"  唤醒未触发（第 {attempt} 次），重试（环境噪声口径）")
                dev.pump(0.5)
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

        # ---- T3.1/T3.7：会话状态机（voice_session） ----
        # 单轮用例在基线已 `vs followup off`；--followup 用例为 on
        follow_sids: list[int] = []
        forced_idle = False
        if args.followup:
            fp_line = wait_line(dev, re.compile(r"VS: state SPEAKING -> FOLLOW_UP"), 12.0, mark)
            result.check(fp_line is not None, "TTS 播完进入 FOLLOW_UP 续听窗口（T3.7）")
            if fp_line is not None:
                for idx in range(args.followup_rounds):
                    mark2 = dev.mark()
                    dev.play(speech_path)
                    start_line = wait_line(dev, RE_ROUND_START, 12.0, mark2)
                    result.check(start_line is not None,
                                 f"续听第 {idx + 1} 轮：窗口内直接起轮（无需唤醒）")
                    if start_line is None:
                        break
                    m_start = RE_ROUND_START.search(start_line)
                    if m_start is not None:
                        follow_sids.append(int(m_start.group(1)))
                    result.check(not any(RE_WAKE.search(l) for l in lines_since(dev, mark2)),
                                 f"续听第 {idx + 1} 轮：未触发唤醒词")
                    end = wait_line(dev, RE_AUDIO_END, args.round_timeout, mark2)
                    result.check(end is not None, f"续听第 {idx + 1} 轮：audio_end")
                    done = wait_line(dev, RE_ROUND_DONE, 15.0, mark2)
                    result.check(done is not None, f"续听第 {idx + 1} 轮：round done")
                    fp_again = wait_line(dev, re.compile(r"VS: state SPEAKING -> FOLLOW_UP"),
                                         12.0, mark2)
                    result.check(fp_again is not None,
                                 f"续听第 {idx + 1} 轮：播完再次进入 FOLLOW_UP")
                    if fp_again is None:
                        break
                sids_ok = bool(follow_sids) and all(
                    b > a for a, b in zip([sid_dev] + follow_sids, follow_sids))
                result.check(sids_ok, f"续听轮 sid 递增（首轮 {sid_dev}，续听 {follow_sids}）")
                mark_idle = dev.mark()
            else:
                mark_idle = mark
            # 环境噪声可能额外触发续听轮（有确认窗口仍可能被持续人声触发），等窗口/轮数上限收尾
            if wait_line(dev, re.compile(r"VS: state FOLLOW_UP -> IDLE"), 25.0, mark_idle) is None:
                result.warn("续听窗口未在 25s 内超时（环境人声持续触发续听轮）；发 vs interrupt 收尾")
                dev.console("vs interrupt", END_VS_INTERRUPT, timeout=2.0)
                wait_line(dev, re.compile(r"VS: state (?:LISTENING|THINKING|SPEAKING|FOLLOW_UP) -> IDLE"),
                          6.0, mark_idle)
                forced_idle = True
        else:
            # 单轮用例：续听窗口已关闭，SPEAKING 播完直接回 IDLE
            result.check(wait_line(dev, re.compile(r"VS: state SPEAKING -> IDLE"), 12.0, mark) is not None,
                         "TTS 播完回 IDLE（续听窗口已关闭）")

        vs_lines = lines_since(dev, 0)
        vs_seq = [(m.group(1), m.group(2), int(m.group(3)))
                  for l in vs_lines for m in [RE_VS_STATE.search(l)] if m]
        got = [(a, b) for a, b, _ in vs_seq]

        def contains_in_order(pairs: list[tuple[str, str]]) -> bool:
            it = iter(got)
            return all(item in it for item in pairs)

        tail = ("SPEAKING", "FOLLOW_UP") if args.followup else ("SPEAKING", "IDLE")
        result.check(contains_in_order([("IDLE", "LISTENING"), ("LISTENING", "THINKING"),
                                        ("THINKING", "SPEAKING"), tail]),
                     f"VS 首轮迁移 IDLE→LISTENING→THINKING→{tail[0]}→{tail[1]}（实际 {got[:8]}）")
        if args.followup:
            if forced_idle:
                result.warn("续听窗口由环境噪声额外触发轮/强制打断收尾，跳过严格窗口超时断言")
            else:
                result.check(any(pair == ("FOLLOW_UP", "IDLE") for pair in got),
                             "VS 续听窗口超时后回 IDLE")
        result.check(all(sid == sid_dev for _, _, sid in vs_seq[:4]),
                     "VS 首轮迁移日志的 sid 与本轮一致")
        result.check(any(RE_EVT_VS_STATE.search(l) for l in vs_lines),
                     "event_bus 收到 VOICE_STATE_CHANGED")
        vst = vs_status(dev)
        expected_rounds = 1 + len(follow_sids)
        result.check(vst.get("state") == "IDLE", f"VS 窗口结束后 IDLE（实际 {vst.get('state')}）")
        result.check((vst.get("rounds_started") or 0) >= expected_rounds and
                     (vst.get("rounds_completed") or 0) >= expected_rounds,
                     f"VS 轮次统计：开始 {vst.get('rounds_started')} / 完成 {vst.get('rounds_completed')}"
                     f"（至少 {expected_rounds}）")
        if args.followup:
            result.check((vst.get("followup_rounds") or 0) >= len(follow_sids),
                         f"VS 续听轮计数 {vst.get('followup_rounds')} ≥ {len(follow_sids)}")
            result.check((vst.get("followup_hits") or 0) >= len(follow_sids),
                         f"VS 续听命中 {vst.get('followup_hits')} ≥ {len(follow_sids)}")
            if not forced_idle:
                result.check((vst.get("followup_timeouts") or 0) >= 1, "VS 记录窗口超时退出")
        result.check(vst.get("rounds_errored") == 0 and vst.get("timeouts") == 0,
                     "VS 无异常/超时轮次")

        # ---- 真 ASR（--asr）：设备 asr_final 的文本应来自本地 SenseVoice 识别 ----
        if args.asr:
            if not mock.owned:
                result.warn("--asr 需要脚本自己拉起 mock（端口被复用），跳过真 ASR 文本断言")
            else:
                asr_text = None
                for line in vs_lines:
                    m = RE_ASR_FINAL_TEXT.search(line)
                    if m and int(m.group(1)) == sid_dev:
                        asr_text = m.group(2).strip()
                result.check(asr_text is not None and len(asr_text) >= 2,
                             f"设备收到真 ASR 文本（{asr_text!r}）")
                mock_match = RE_MOCK_ASR_TEXT.search(mock.text())
                mock_text = mock_match.group(1).strip() if mock_match else None
                result.check(mock_text is not None, "mock 侧打印了 SenseVoice 识别结果")
                result.check(asr_text == mock_text,
                             f"设备 asr_final 与 mock 识别一致（设备 {asr_text!r} / mock {mock_text!r}）")
                result.check(asr_text != ASR_FALLBACK_TEXT, "识别文本不是 --tts-text 固定文案")
                if args.asr_expect:
                    result.check(args.asr_expect in (asr_text or ""),
                                 f"识别文本包含期望子串 {args.asr_expect!r}")

        # ---- 统计自洽 ----
        st = vc_status(dev)
        d = {key: st.get(key, 0) - base.get(key, 0) for key in (
            "rounds_started", "rounds_done", "rounds_aborted", "up_frames", "up_bytes",
            "rx_audio_frames", "dropped_sid", "parse_errors", "cloud_errors")}
        result.check(d["rounds_started"] >= expected_rounds and d["rounds_done"] >= expected_rounds,
                     f"轮次统计：发起 {d['rounds_started']} / 完成 {d['rounds_done']}"
                     f"（至少 {expected_rounds}）")
        result.check(st.get("state") == "IDLE", "轮次结束后回到 IDLE")
        min_last_sid = follow_sids[-1] if follow_sids else sid_dev
        result.check((st.get("sid") or 0) >= min_last_sid,
                     f"vc 当前 sid={st.get('sid')} ≥ 最近计划轮（{min_last_sid}）")
        result.check(d["up_frames"] >= up_frames and up_frames >= 60,
                     f"首轮上行 {up_frames} 帧，累计 {d['up_frames']} 帧（≥60）")
        bpf = st.get("bytes_per_frame", 0.0)
        result.check(20.0 <= bpf <= 60.0, f"平均帧长 {bpf:.1f} B/帧（VBR 预期 20~60）")
        result.check(first_us < 200000 and st.get("first_frame_us", 10**9) < 200000,
                     f"统计里的首帧耗时 {st.get('first_frame_us')} us < 200000 us")
        result.check(d["rx_audio_frames"] >= tts_frames,
                     f"首轮 tts_end {tts_frames} 帧，累计下行 {d['rx_audio_frames']} 帧")
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
            result.check(frame_count >= d["up_frames"],
                         f"mock 逐帧收到 {frame_count} 帧 ≥ 设备快照 {d['up_frames']} 帧"
                         "（环境噪声额外轮可能滞后于快照）")
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
    parser.add_argument("--asr", action="store_true",
                        help="让 mock 用本地 SenseVoice 做真 ASR：asr_final 文本来自上行音频识别"
                             "（mock 由脚本以 uv --with 启动，本脚本自身不需要额外依赖）")
    parser.add_argument("--asr-model-dir", default="",
                        help="SenseVoice 模型目录（缺省自动找本机缓存）")
    parser.add_argument("--asr-expect", default="",
                        help="期望识别文本包含的子串（如 天气；缺省只断言非空且非固定文案）")
    parser.add_argument("--followup", action="store_true",
                        help="连续对话（T3.7）：首轮 TTS 后进入 FOLLOW_UP，窗口内直接说话连续跑 N 轮")
    parser.add_argument("--followup-rounds", type=int, default=2,
                        help="续听轮数（1~5，默认 2；受 Kconfig VS_FOLLOWUP_MAX_ROUNDS 限制）")
    parser.add_argument("--wake-attempts", type=int, default=3,
                        help="唤醒回放重试次数（环境噪声下唤醒率会下降，不改变端侧阈值）")
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
    if args.asr and args.no_mock:
        raise SystemExit("--asr 需要脚本自己拉起 mock，不能与 --no-mock 同时使用")
    if not 1 <= args.followup_rounds <= 5:
        raise SystemExit(f"--followup-rounds 必须在 1~5（当前 {args.followup_rounds}）")

    print("===== voice_cloud 端到端自测（T2.5） =====")
    try:
        return run(args)
    except KeyboardInterrupt:
        print("\n中断")
        return 130


if __name__ == "__main__":
    sys.exit(main())
