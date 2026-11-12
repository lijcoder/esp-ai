#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["pyserial>=3.5"]
# ///
"""music_e2e_test —— T4.2（music_cloud）/ T4.3（music_session）真机回归。

一条命令完成：
  起 mock（`--music --asr sensevoice`）→ 复位设备 → 回放唤醒词 + 语音指令 →
  断言「点歌出声 / music_status 周期上报 / 暂停冻结 / 继续不从头 / 唤醒自动暂停与会后保持暂停 /
  用户暂停 latch / 切歌 / 停止 / 资源不泄漏 / 错误路径不崩」→ 汇总 PASS/FAIL。

为什么需要本脚本：T4.2/T4.3 只有 host 单测（`test/host/music_policy_test` 76/76）与构建证据，
没有任何真机证据；PV-013/PV-014 列出的 17 项验收全部回板才能证明。

语音指令来源：macOS `say -v Tingting` 现生成（放首歌 / 暂停播放 / 继续播放 / 下一首 / 停止播放），
默认放 `<tmp>/music_e2e_cmds/`，可用 `--commands-dir` 指定现成素材。

依赖：`tools/ws_mock_server.py`（`--music` + `--asr sensevoice`；`--with av numpy sherpa-onnx`），
`tools/lexin.wav`，`tools/今天天气如何.wav`。脚本独占串口（先关掉 `idf.py monitor`）。

用法：
    uv run tools/music_e2e_test.py --music-url 'http://192.168.1.15:9009/stream/test41.mp3'
    uv run tools/music_e2e_test.py --music-url ... --volume 45      # 系统音量（31~50）
    uv run tools/music_e2e_test.py --music-url ... --dry-run        # 只检查素材/串口/指令

注意：**音乐播放时唤醒会被音乐掩蔽**（实测设备混音音量 100/15/10 均无法唤醒，0 才能稳定唤醒）。
这是 T4.4「语音打断 + 音乐暂停保留」要解决的问题；本脚本默认 `--player-volume 0`
（音乐管线照常跑、状态机行为不变），只验证 T4.2/T4.3/T4.4 的协议与控制逻辑。
"""

from __future__ import annotations

import argparse
import glob
import os
import re
import socket
import subprocess
import threading
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))

from voice_test import (  # noqa: E402  （PEP 723 脚本按脚本目录入 sys.path）
    Device,
    Result,
    detect_port,
    get_system_volume,
    set_system_volume,
)

MOCK_SCRIPT = os.path.join(HERE, "ws_mock_server.py")
WAKE_WAV = os.path.join(HERE, "lexin.wav")
QUERY_WAV = os.path.join(HERE, "今天天气如何.wav")

COMMAND_TEXT = {
    "play": "放首歌",
    "pause": "暂停播放",
    "resume": "继续播放",
    "next": "下一首",
    "stop": "停止播放",
}

# ---------------- 设备侧日志 ----------------
RE_WAKE = re.compile(r"VS: wake detected")
RE_VS_STATE = re.compile(r"VS: state (\w+) -> (\w+)")
RE_PROMPT_DONE = re.compile(r"VS: prompt done, feed resumed")
RE_ROUND_DONE = re.compile(r"VC: round done sid=")
RE_TTS_FAIL = re.compile(r"VS: state \w+ -> ERROR（.*原因=tts-fail")
RE_MS_STATE = re.compile(r"MS: state (\w+) -> (\w+)")
RE_MS_INFO = re.compile(
    r"MS state=(\w+) paused=(\d) reason=(\d+) mid=(\d+) have_mid=(\d) duration=(-?\d+)ms "
    r"progress=(\d+)ms")
RE_MS_STAT = re.compile(
    r"MSSTAT transitions=(\d+) tracks=(\d+) play_err=(\d+) pauses=(\d+) resumes=(\d+) "
    r"steals=(\d+) stops=(\d+) eof=(\d+) controls=(\d+) dropped=(\d+) resume_fail=(\d+) "
    r"fallback=(\d+)")
RE_MC_TRACK = re.compile(r"MC: music_track mid=(\d+)")
RE_MC_CONTROL = re.compile(r"MC: music_control mid=(\d+) action=(\w+)")
RE_MUSIC_START = re.compile(r"PLAYER: music start")
RE_MUSIC_PAUSED = re.compile(r"PLAYER: music paused")

END_MS = re.compile(r"^MSSTAT ")
END_MC = re.compile(r"^MCSTAT ")
END_HEAP = re.compile(r"PSRAM\s*:")
END_TASKS = re.compile(r"共 \d+ 个任务")
RE_HEAP_RAM = re.compile(r"内部 RAM:.*?剩余 (\d+) B.*?最大块 (\d+) B.*?最低水位 (\d+) B.*?峰值占用 (\d+) B")

# ---------------- mock 侧日志 ----------------
RE_MOCK_INTENT = re.compile(r"\[MUSIC\] (?:意图=|工具 )(\w+).*?mid=(\d+)")
RE_MOCK_STATUS = re.compile(r"\[(\d\d):(\d\d):(\d\d\.\d+)\].*<-- music_status state=(\w+) progress_ms=(\d+)")
RE_MOCK_TRACK_SENT = re.compile(r"music_track mid=(\d+)")
RE_MOCK_REQUEST = re.compile(r"<-- music_request action=(\w+)")

END_CMD = re.compile(r"MSSTOP|MSPAUSE|MSRESUME|MSPLAY|MSREQUEST|MCSTAT|MSSTAT|MIXVOL")


# ---------------------------------------------------------------- 工具

def port_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.3)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def ensure_commands(cache_dir: str) -> dict[str, str]:
    """用 macOS `say` 生成中文指令 WAV/AIFF（幂等；已有文件跳过）。"""
    os.makedirs(cache_dir, exist_ok=True)
    out: dict[str, str] = {}
    for key, text in COMMAND_TEXT.items():
        path = os.path.join(cache_dir, f"{key}.aiff")
        if not os.path.isfile(path) or os.path.getsize(path) < 4096:
            try:
                subprocess.run(["say", "-v", "Tingting", "-r", "180", "-o", path, text],
                               check=True, capture_output=True)
            except (subprocess.CalledProcessError, FileNotFoundError) as exc:
                raise SystemExit(
                    f"生成语音指令失败（{text} → {path}）：{exc}\n"
                    "需要 macOS `say`；可改用 --commands-dir 提供现成 aiff/wav（文件名 play/pause/...）")
        out[key] = path
    return out


def parse_ms_info(lines: list[str]) -> dict | None:
    for line in lines:
        match = RE_MS_INFO.search(line)
        if match:
            return {
                "state": match.group(1),
                "paused": int(match.group(2)),
                "reason": int(match.group(3)),
                "mid": int(match.group(4)),
                "duration": int(match.group(6)),
                "progress": int(match.group(7)),
            }
    return None


def parse_ms_stat(lines: list[str]) -> dict | None:
    for line in lines:
        match = RE_MS_STAT.search(line)
        if match:
            keys = ("transitions", "tracks", "play_err", "pauses", "resumes", "steals", "stops",
                    "eof", "controls", "dropped", "resume_fail", "fallback")
            return {k: int(v) for k, v in zip(keys, match.groups())}
    return None


def parse_mc_stat(lines: list[str]) -> dict | None:
    for line in lines:
        match = re.search(r"MCSTAT (.*)", line)
        if match:
            return dict(re.findall(r"(\w+)=(\d+)", match.group(1)))
    return None


def parse_heap(lines: list[str]) -> dict | None:
    for line in lines:
        match = RE_HEAP_RAM.search(line)
        if match:
            return {"free": int(match.group(1)), "block": int(match.group(2)),
                    "min": int(match.group(3)), "peak": int(match.group(4))}
    return None


# ---------------------------------------------------------------- mock

class FakeTools:
    """进程内假 Responses 网关：把常见中文指令映射成音乐工具（无需 token/Hermes）。

    只用于「手边没有真实模型」时跑通链路；它模拟的是**模型决策**，不是产品规则表。
    """

    TOOL_MAP = {
        "停止": "music_stop", "别放": "music_stop", "关掉": "music_stop",
        "暂停": "music_pause", "继续": "music_resume",
        "下一首": "music_next", "上一首": "music_prev",
        "放": "music_play", "播放": "music_play",
    }

    def __init__(self) -> None:
        from types import SimpleNamespace
        from http.server import ThreadingHTTPServer
        from fake_llm_server import Handler

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.httpd.cfg = SimpleNamespace(  # type: ignore[attr-defined]
            answer="好的。", chunk_chars=3, chunk_delay_ms=0, first_delay_ms=0,
            echo_user=False, tool_mode="none", tool_name="music_play",
            tool_args="{}", tool_match="", tool_map=self.TOOL_MAP,
        )
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.httpd.server_address[1]
        print(f"[TEST] 假 Responses 网关：http://127.0.0.1:{self.port}/v1（模拟模型决策）")

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


class Mock:
    def __init__(self, port: int, music_url: str, music_title: str, tts_qwen: bool,
                 llm_base_url: str = "") -> None:
        self.port = port
        self.music_url = music_url
        self.music_title = music_title
        self.tts_qwen = tts_qwen
        self.llm_base_url = llm_base_url
        self.proc: subprocess.Popen | None = None
        self.log_path = os.path.join(tempfile.gettempdir(), f"music_e2e_mock_{port}.log")

    def start(self) -> None:
        if port_open(self.port):
            raise SystemExit(f"mock 端口 {self.port} 已被占用：请先停掉现有 mock（本脚本要拿 mock 日志做断言）")
        cmd = ["uv", "run", "--with", "av", "--with", "numpy", "--with", "sherpa-onnx"]
        if self.tts_qwen:
            cmd += ["--with", "mlx-audio", "--with", "lameenc"]
        cmd += [MOCK_SCRIPT, "--host", "0.0.0.0", "--port", str(self.port), "--music",
                "--asr", "sensevoice"]
        if self.music_url:
            cmd += ["--music-url", self.music_url, "--music-title", self.music_title]
        if self.tts_qwen:
            cmd.append("--tts-qwen")
        if self.llm_base_url:
            cmd += ["--llm-base-url", self.llm_base_url,
                    "--llm-model", "fake", "--llm-api-key", "fake"]
        log = open(self.log_path, "w", encoding="utf-8", buffering=1)
        self.proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, cwd=HERE)
        deadline = time.time() + 45
        while time.time() < deadline:
            if port_open(self.port) and "音乐(M1): 开" in self.text():
                return
            if self.proc.poll() is not None:
                raise SystemExit(f"mock 启动失败，日志见 {self.log_path}")
            time.sleep(0.2)
        raise SystemExit(f"mock 45s 内未就绪，日志见 {self.log_path}")

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

    def statuses(self) -> list[tuple[float, str, int]]:
        """解析 mock 侧收到的 music_status（相对时间只用于间隔计算）。"""
        out = []
        for match in RE_MOCK_STATUS.finditer(self.text()):
            hh, mm, ss = int(match.group(1)), int(match.group(2)), float(match.group(3))
            out.append((hh * 3600 + mm * 60 + ss, match.group(4), int(match.group(5))))
        return out

    def intents(self) -> list[tuple[str, int]]:
        return [(m.group(1), int(m.group(2))) for m in RE_MOCK_INTENT.finditer(self.text())]


# ---------------------------------------------------------------- 设备编排

class Harness:
    def __init__(self, dev: Device, result: Result, ms_state_wait: float = 10.0) -> None:
        self.dev = dev
        self.r = result
        self.ms_state_wait = ms_state_wait

    # ---- console ----
    def ms_info(self) -> dict | None:
        return parse_ms_info(self.dev.console("ms status", END_MS, 2.0))

    def ms_stat(self) -> dict | None:
        return parse_ms_stat(self.dev.console("ms status", END_MS, 2.0))

    def mc_stat(self) -> dict | None:
        return parse_mc_stat(self.dev.console("mc status", END_MC, 2.0))

    def heap(self) -> dict | None:
        return parse_heap(self.dev.console("sys heap", END_HEAP, 2.0))

    def task_count(self) -> int | None:
        lines = self.dev.console("sys tasks", END_TASKS, 2.5)
        for line in lines:
            match = re.search(r"共 (\d+) 个任务", line)
            if match:
                return int(match.group(1))
        return None

    # ---- 等待 ----
    def wait_ms_state(self, want: str, timeout: float | None = None) -> dict | None:
        deadline = time.time() + (timeout if timeout is not None else self.ms_state_wait)
        last = None
        while time.time() < deadline:
            last = self.ms_info()
            if last and last["state"] == want:
                return last
            time.sleep(0.35)
        return last

    def wait_music_playing(self, timeout: float = 12.0) -> dict | None:
        return self.wait_ms_state("playing", timeout)

    # ---- 语音轮 ----
    def voice_round(self, command: str | None, timeout: float = 40.0) -> dict:
        """唤醒 →（可选）说指令 → 等 round done 与 VS 回 IDLE。返回观测信息。"""
        info: dict = {"wake": False, "round_done": False, "tts_fail": False, "idle": False}
        mark = self.dev.mark()
        self.dev.play(WAKE_WAV, gain=2.0)
        info["wake"] = self.dev.wait_line(RE_WAKE, 6.0, mark) is not None
        if not info["wake"]:
            # 唤醒偶发漏检（环境噪声/音乐掩蔽）：再试一次，仍失败才判失败
            mark = self.dev.mark()
            self.dev.play(WAKE_WAV, gain=2.0)
            info["wake"] = self.dev.wait_line(RE_WAKE, 6.0, mark) is not None
        if not info["wake"]:
            return info
        # 唤醒应答提示音播完（~1s）；不等它就说话会被 R1 丢掉
        self.dev.wait_line(RE_PROMPT_DONE, 6.0, mark)
        if command:
            self.dev.play(command)
        info["round_done"] = self.dev.wait_line(RE_ROUND_DONE, timeout, mark) is not None
        self.dev.wait_line(re.compile(r"VS: state \w+ -> IDLE"), 20.0, mark)
        lines = [line for _, line in self.dev.lines_since(mark)]
        info["tts_fail"] = any(RE_TTS_FAIL.search(line) for line in lines)
        info["idle"] = any("-> IDLE" in line for line in lines)
        info["lines"] = lines
        return info

    def wake_only(self, timeout: float = 40.0) -> dict:
        """只唤醒不说话：用于「唤醒自动暂停 → 会后保持暂停（T4.4）」。"""
        return self.voice_round(None, timeout)

    def ms_state_ts(self) -> dict[str, float]:
        """从日志时间戳里找最近一次 MS 状态迁移（用于时延断言）。"""
        out: dict[str, float] = {}
        for ts, line in self.dev.lines:
            match = RE_MS_STATE.search(line)
            if match:
                out[f"{match.group(1)}->{match.group(2)}"] = ts
        return out


# ---------------------------------------------------------------- 场景

def scenario_point_song(h: Harness, cmds: dict[str, str], mock: Mock) -> None:
    print("\n[A] 点歌：唤醒 →「放首歌」→ 出声 + music_status")
    info = h.voice_round(cmds["play"])
    h.r.check(info["wake"], "A1 唤醒")
    h.r.check(info["round_done"], "A2 语音轮完成")
    h.r.check(not info["tts_fail"], "A3 本轮无 tts-fail")
    h.r.check(any(RE_MC_TRACK.search(line) for line in info["lines"]),
              "A4 设备收到 music_track")
    playing = h.wait_music_playing(15.0)
    h.r.check(playing is not None and playing["state"] == "playing",
              f"A5 进入 playing（{playing}）")
    if playing:
        h.r.check(playing["mid"] >= 1, f"A6 mid={playing['mid']}")

    # music_status 周期：状态变化会立即上报，只有"同状态相邻两条"才应约 5s
    time.sleep(11.0)
    statuses = mock.statuses()
    h.r.check(len(statuses) >= 2, f"A7 mock 收到 music_status ≥2 条（{len(statuses)}）")
    if len(statuses) >= 2:
        playing_status = [s for s in statuses if s[1] == "playing"]
        h.r.check(bool(playing_status), "A8 上报 state=playing")
        h.r.check(all(s[2] >= 0 for s in statuses), "A9 progress_ms 字段可解析")
    # 5s 周期（同状态相邻两条的间隔）
    playing_ts = [s[0] for s in statuses if s[1] == "playing"]
    if len(playing_ts) >= 3:
        gaps = [b - a for a, b in zip(playing_ts, playing_ts[1:])]
        gaps = [g for g in gaps if g > 3.0]  # 过滤状态变化立即上报造成的密集条目
        h.r.check(bool(gaps) and all(4.0 <= gap <= 6.5 for gap in gaps),
                  f"A10 music_status 约 5s 一条（{['%.1f' % g for g in gaps]}）")
    else:
        h.r.warn("A10 上报条数不足，跳过 5s 周期断言")

    ms = h.ms_info()
    mc = h.mc_stat()
    h.r.check(ms is not None and mc is not None, "A11 能读 ms/mc 统计")
    if ms and mc:
        h.r.check(ms["mid"] == int(mc.get("track", "0")),
                  f"A12 ms.mid 与 mc.track 一致（{ms['mid']} / {mc.get('track')}）")


def scenario_auto_pause(h: Harness) -> dict:
    print("\n[B] 唤醒自动暂停 + 会后保持暂停（T4.4 pause 模式）")
    before = h.ms_info()
    h.r.check(before is not None and before["state"] == "playing", "B0 前置：音乐在播")
    mark = h.dev.mark()
    h.dev.play(WAKE_WAV, gain=2.0)
    wake = h.dev.wait_line(RE_WAKE, 6.0, mark) is not None
    if not wake:  # 同 voice_round：偶发漏检再试一次
        mark = h.dev.mark()
        h.dev.play(WAKE_WAV, gain=2.0)
        wake = h.dev.wait_line(RE_WAKE, 6.0, mark) is not None
    h.r.check(wake, "B1 唤醒（音乐播放中）")
    if not wake:
        return {}
    # 关键：暂停发生在"轮次进行中"，必须现在轮询（不能等 round done 之后）
    paused = h.wait_ms_state("paused", 5.0)
    h.r.check(paused is not None and paused["state"] == "paused", f"B2 唤醒后自动暂停（{paused}）")
    frozen = paused["progress"] if paused else 0
    if paused:
        h.r.check(paused["reason"] == 2, f"B3 暂停原因=语音抢占（reason={paused['reason']}）")
        h.r.check(paused["paused"] == 1, "B4 paused 标志置位")
    # 不说话 → 轮次由静音收尾 → 回 IDLE；T4.4：不再自动恢复，保持暂停
    h.dev.wait_line(RE_ROUND_DONE, 30.0, mark)
    time.sleep(2.0)
    still = h.ms_info()
    h.r.check(still is not None and still["state"] == "paused",
              f"B5 语音回 IDLE 后保持暂停（{still}）")
    if still and paused:
        h.r.check(still["progress"] == frozen,
                  f"B6 暂停期间 progress 冻结（{frozen} → {still['progress']} ms）")
    # 收尾：console 恢复，供后续场景从 playing 开始
    h.dev.console("ms resume", END_CMD, 1.5)
    playing = h.wait_music_playing(10.0)
    h.r.check(playing is not None, f"B7 console 恢复播放（{playing}）")
    return playing or {}


def scenario_pause_resume(h: Harness, cmds: dict[str, str], mock: Mock) -> None:
    print("\n[C] 语音「暂停 / 继续」：暂停冻结 + 继续不从头")
    info = h.voice_round(cmds["pause"])
    h.r.check(info["wake"] and info["round_done"], "C1 暂停指令轮完成")
    paused = h.wait_ms_state("paused", 6.0)
    h.r.check(paused is not None and paused["state"] == "paused", f"C2 已暂停（{paused}）")
    if not paused:
        return
    # 暂停时延：MC 控制 → MS 迁移
    ts = h.ms_state_ts()
    h.r.check("playing->paused" in ts, "C3 日志有 playing->paused 迁移")
    frozen = paused["progress"]
    time.sleep(2.5)
    still = h.ms_info()
    h.r.check(still is not None and still["progress"] == frozen,
              f"C4 暂停中 progress 冻结（{frozen} → {still['progress'] if still else '?'}）")
    h.r.check(still is not None and still["state"] == "paused", "C5 会话结束后仍保持暂停（用户 latch）")
    if still and still["state"] == "paused":
        h.r.check(still["reason"] == 1, f"C6 暂停原因=用户（reason={still['reason']}）")
    h.r.check(any(s[1] == "paused" for s in mock.statuses()), "C7 mock 收到 state=paused")

    # 继续
    info = h.voice_round(cmds["resume"])
    h.r.check(info["wake"] and info["round_done"], "C8 继续指令轮完成")
    h.r.check(not info["tts_fail"], "C9 继续时确认语 TTS 未被音乐抢占（无 tts-fail）")
    playing = h.wait_music_playing(10.0)
    h.r.check(playing is not None and playing["state"] == "playing", f"C10 恢复 playing（{playing}）")
    if playing:
        h.r.check(playing["progress"] >= frozen,
                  f"C11 从暂停点续播（暂停 {frozen} → 恢复 {playing['progress']}）")
        h.r.check(playing["progress"] <= frozen + 6000,
                  f"C12 不是从头播（进度增量 {playing['progress'] - frozen} ms）")


def scenario_user_latch(h: Harness, cmds: dict[str, str]) -> None:
    print("\n[D] 用户暂停 latch：普通会话结束不得自动恢复")
    info = h.voice_round(cmds["pause"])
    h.r.check(info["wake"] and info["round_done"], "D1 暂停轮完成")
    paused = h.wait_ms_state("paused", 6.0)
    h.r.check(paused is not None, "D2 已暂停")
    if paused is None:
        return
    info = h.voice_round(QUERY_WAV, timeout=45.0)
    h.r.check(info["wake"] and info["round_done"], "D3 普通问答轮完成")
    after = h.wait_ms_state("paused", 3.0)
    h.r.check(after is not None and after["state"] == "paused",
              f"D4 问答结束后仍暂停（{after}）")
    # 收尾：恢复
    info = h.voice_round(cmds["resume"])
    h.r.check(info["wake"] and info["round_done"], "D5 恢复轮完成")
    playing = h.wait_music_playing(10.0)
    h.r.check(playing is not None and playing["state"] == "playing", "D6 恢复播放")


def scenario_next_stop(h: Harness, cmds: dict[str, str], mock: Mock) -> None:
    print("\n[E] 切歌 / 停止")
    before = h.ms_info()
    h.r.check(before is not None, "E0 读当前曲目")
    old_mid = before["mid"] if before else 0
    info = h.voice_round(cmds["next"])
    h.r.check(info["wake"] and info["round_done"], "E1 下一首轮完成")
    time.sleep(3.0)
    after = h.ms_info()
    h.r.check(after is not None and after["state"] == "playing", f"E2 切歌后 playing（{after}）")
    if after:
        h.r.check(after["mid"] == old_mid + 1, f"E3 mid 递增（{old_mid} → {after['mid']}）")
    h.r.check(any(action == "next" and mid == old_mid + 1 for action, mid in mock.intents()),
              "E4 mock 下发新 music_track（意图=next）")

    info = h.voice_round(cmds["stop"])
    h.r.check(info["wake"] and info["round_done"], "E5 停止轮完成")
    stopped = h.wait_ms_state("stopped", 6.0)
    h.r.check(stopped is not None and stopped["state"] == "stopped", f"E6 状态 stopped（{stopped}）")
    if stopped is not None:
        h.r.check(any(s[1] == "stopped" for s in mock.statuses()), "E7 mock 收到 state=stopped")


def scenario_resources(h: Harness, cmds: dict[str, str]) -> None:
    print("\n[F] 资源：暂停/继续 ×10，任务数与内部堆不泄漏")
    playing = h.wait_music_playing(6.0)
    if playing is None or playing["state"] != "playing":
        info = h.voice_round(cmds["play"])
        h.r.check(info["wake"] and info["round_done"], "F0 前置点歌")
        playing = h.wait_music_playing(15.0)
    h.r.check(playing is not None and playing["state"] == "playing", f"F1 前置：音乐在播（{playing}）")
    if playing is None or playing["state"] != "playing":
        return
    heap0 = h.heap()
    tasks0 = h.task_count()
    stat0 = h.ms_stat()
    h.r.check(heap0 is not None and tasks0 is not None and stat0 is not None, "F2 读到基线堆/任务数/统计")
    cycles = 0
    for _ in range(10):
        h.dev.console("ms pause", END_CMD, 1.2)
        if h.wait_ms_state("paused", 2.0) is None:
            break
        if h.ms_info()["state"] != "paused":
            break
        h.dev.console("ms resume", END_CMD, 1.2)
        info = h.wait_ms_state("playing", 2.5)
        if info is None or info["state"] != "playing":
            break
        cycles += 1
    h.r.check(cycles == 10, f"F3 暂停/继续 10 次均生效（实际 {cycles}）")
    time.sleep(1.0)
    heap1 = h.heap()
    tasks1 = h.task_count()
    stat1 = h.ms_stat()
    h.r.check(heap1 is not None and tasks1 is not None, "F4 读到收尾堆/任务数")
    if heap0 and heap1:
        h.r.check(heap1["free"] >= heap0["free"] - 4096,
                  f"F5 内部堆无单调下降（{heap0['free']} → {heap1['free']} B）")
    if tasks0 and tasks1:
        h.r.check(tasks1 == tasks0, f"F6 任务数不变（{tasks0} → {tasks1}）")
    if stat0 and stat1:
        h.r.check(stat1["pauses"] - stat0["pauses"] >= 10,
                  f"F7 统计 pauses +{stat1['pauses'] - stat0['pauses']}")
        h.r.check(stat1["resumes"] - stat0["resumes"] >= 10,
                  f"F8 统计 resumes +{stat1['resumes'] - stat0['resumes']}")
        h.r.check(stat1["play_err"] == stat0["play_err"], "F9 无起播错误")
    after = h.wait_music_playing(6.0)
    h.r.check(after is not None and after["state"] == "playing", f"F10 循环后仍在播放（{after}）")


def scenario_errors(h: Harness, mock: Mock, http_base: str) -> None:
    print("\n[G] 错误路径：非法 scheme / 404 / 不崩")
    if not http_base:
        h.r.warn("G0 未提供 --http-base，跳过 404 路径（可用 --http-base http://<MacIP>:9009）")
        return
    out = h.dev.console("ms play ftp://example/x.mp3", END_CMD, 1.5)
    h.r.check(any("MSPLAY err=ESP_ERR_INVALID_ARG" in line for line in out),
              "G1 非法 scheme 被拒绝")
    h.dev.console("ms stop", END_CMD, 1.5)
    out = h.dev.console(f"ms play {http_base}/media/__no_such_file__.mp3", END_CMD, 2.0)
    h.r.check(any("MSPLAY err=ESP_OK" in line for line in out), "G2 404 URL 被接受（异步失败）")
    info = None
    for _ in range(20):
        info = h.ms_info()
        if info and info["state"] in ("error", "stopped", "idle"):
            break
        time.sleep(0.5)
    h.r.check(info is not None and info["state"] in ("error", "stopped", "idle"),
              f"G3 404 后收敛到终态（{info}）")
    stat = parse_ms_stat(h.dev.console("ms status", END_MS, 2.0))
    h.r.check(stat is not None and (stat["play_err"] + stat["dropped"]) >= 0, "G4 统计可读")
    h.dev.console("ms stop", END_CMD, 1.5)


# ---------------------------------------------------------------- main

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="music_cloud/music_session 真机回归（PV-013/PV-014）")
    parser.add_argument("--port", default="", help="串口（缺省自动找 /dev/tty.usbmodem*）")
    parser.add_argument("--mock-port", type=int, default=8765, help="mock 端口（需与设备 CONFIG_AI_WS_URL 一致）")
    parser.add_argument("--music-url", required=True,
                        help="音乐 URL（长曲目/直播流更稳：本地 `tools/local_music_server.py` 的 /stream/）")
    parser.add_argument("--music-title", default="回归测试曲")
    parser.add_argument("--http-base", default="", help="404 错误路径用的 HTTP 基址，如 http://192.168.1.15:9009")
    parser.add_argument("--commands-dir", default="", help="现成语音指令目录（缺省用 say 生成）")
    parser.add_argument("--volume", type=int, default=45, help="系统输出音量 31~50（唤醒回放）")
    parser.add_argument("--player-volume", type=int, default=0,
                        help="设备混音音量（默认 0：音乐出声会掩蔽唤醒词，属 T4.4 范畴）")
    parser.add_argument("--tts-qwen", action="store_true", help="mock 用 Qwen3-TTS 真实语音（较慢）")
    parser.add_argument("--skip-reset", action="store_true", help="不复位设备")
    parser.add_argument("--dry-run", action="store_true", help="只做素材/串口检查")
    parser.add_argument("--fake-tools", action="store_true",
                        help="用进程内假 Responses 网关模拟音乐工具调用（无需 LLM token/Hermes）")
    parser.add_argument("--log", default="", help="把设备日志写入文件（排障用）")
    parser.add_argument("--only", default="", help="只跑指定场景，逗号分隔：a,b,c,d,e,f,g")
    args = parser.parse_args(argv)

    if not 31 <= args.volume <= 50:
        raise SystemExit("--volume 必须在 31~50（用户定稿：正常 31%，上限 50%）")
    if not 0 <= args.player_volume <= 100:
        raise SystemExit("--player-volume 必须在 0~100")

    result = Result()
    cmds_dir = args.commands_dir or os.path.join(tempfile.gettempdir(), "music_e2e_cmds")
    cmds = ensure_commands(cmds_dir)
    print(f"[TEST] 指令素材 {cmds_dir}")

    port = detect_port(args.port)
    print(f"[TEST] 串口 {port} | mock 127.0.0.1:{args.mock_port} | 音乐 {args.music_url}")

    if args.dry_run:
        for key, path in cmds.items():
            print(f"  {key}: {path} ({os.path.getsize(path)} B)")
        print(f"  wake: {WAKE_WAV} | query: {QUERY_WAV}")
        return 0

    dev = Device(port)
    fake_tools = FakeTools() if args.fake_tools else None
    mock = Mock(args.mock_port, args.music_url, args.music_title, args.tts_qwen,
                llm_base_url=fake_tools.base if fake_tools else "")
    old_volume = get_system_volume()
    try:
        mock.start()
        set_system_volume(args.volume)
        if not args.skip_reset:
            dev.reset()
            time.sleep(0.5)
        # 等联网 + WS 握手（max 30s）
        mark = dev.mark()
        ready = dev.wait_line(re.compile(r"WS: 握手完成（hello_ack）"), 35.0, mark) is not None
        result.check(ready, "设备 WS 已连接 mock")
        if not ready:
            return 1
        # 关闭续听窗口：让每轮都回到 IDLE，断言确定
        dev.console("vs followup off", re.compile(r"连续对话续听窗口：(开|关)"), 2.0)
        dev.console(f"player mix vol {args.player_volume}",
                    re.compile(r"MIXVOL vol=\d+"), 2.0)
        # 清场：停掉上一轮残留
        dev.console("ms stop", END_MS, 1.5)
        dev.console("mc reset", re.compile(r"MCSTAT cleared|MC 不可用"), 1.5)
        dev.console("ms reset", re.compile(r"MSSTAT cleared|MS 不可用"), 1.5)
        h = Harness(dev, result)

        only = {x.strip().lower() for x in args.only.split(",") if x.strip()}
        def want(key: str) -> bool:
            return not only or key in only
        if want("a"):
            scenario_point_song(h, cmds, mock)
        if want("b"):
            scenario_auto_pause(h)
        if want("c"):
            scenario_pause_resume(h, cmds, mock)
        if want("d"):
            scenario_user_latch(h, cmds)
        if want("e"):
            scenario_next_stop(h, cmds, mock)
        if want("f"):
            scenario_resources(h, cmds)
        if want("g"):
            scenario_errors(h, mock, args.http_base)

        # 收尾检查：无 panic
        bad = [line for _, line in dev.lines
               if re.search(r"Guru Meditation|panic|Backtrace:", line)]
        result.check(not bad, "全程无 panic/backtrace")
    finally:
        if args.log:
            try:
                with open(args.log, "w", encoding="utf-8") as handle:
                    for ts, line in dev.lines:
                        handle.write(f"{ts:.3f} {line}\n")
                print(f"[TEST] 设备日志已写入 {args.log}")
            except OSError as exc:
                print(f"[TEST] 写日志失败：{exc}")
        mock.stop()
        if fake_tools is not None:
            fake_tools.stop()
        dev.close()
        if old_volume is not None:
            try:
                set_system_volume(int(old_volume.get("volume", 31)))
            except Exception:
                pass

    ok = result.summary()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
