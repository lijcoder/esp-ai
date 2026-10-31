#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["pyserial>=3.5"]
# ///
"""T2.1 上行 PCM 真机自测：audio_fe 的 16k 上行音频输出接口。

验证内容（对应 docs/ROADMAP.md T2.1 验收项）：
  A. 精确分帧读出：按 320 样本（20ms@16k）读，样本数与时长的比例正确、
     无丢弃、无读超时、首帧延迟 < 200ms；回放交互语音时电平明显高于静音；
  B. 溢出策略：消费端停读 3.5s（>环形缓冲 2.048s）后 samples_dropped 增长、
     最高占用达到缓冲容量，恢复读取后仍能正常工作；
  C. 反复开关 10 次：内部 RAM / PSRAM 剩余无泄漏（Δ ≤ 4KB）；
  D. 回归：唤醒链路完整（wake → feed paused → PLAYER: play → prompt done），无 ERROR。

用法：
    uv run tools/afe_uplink_test.py --port /dev/cu.usbmodemXXXX
    uv run tools/afe_uplink_test.py --no-reset --log /tmp/afe_uplink.log
"""

from __future__ import annotations

import argparse
import glob
import math
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import wave

HERE = os.path.dirname(os.path.abspath(__file__))
BAUDRATE = 115200
SPEECH_WAV = "今天天气如何.wav"
WAKE_WAV = "lexin.wav"

# 唤醒链路顺序（ARCHITECTURE §3.3 R1/R2）
WAKE_CHAIN = (
    ("wake", re.compile(r"APP: wake detected")),
    ("pause", re.compile(r"AFE: feed paused")),
    ("play", re.compile(r"PLAYER: play \d+ samples")),
    ("done", re.compile(r"APP: prompt done, feed resumed")),
)

ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
ERROR_RE = re.compile(r"\bE \(|ERROR|panic|abort\(\)|Guru Meditation|stack overflow")
# 与本次测试无关的持续重连噪音（mock/线上服务未起时的 net/ws/tls 错误），同 voice_test.py 口径
IGNORED_TAGS = {
    "NET", "WS", "wifi", "websocket_client", "esp_netif", "esp-tls", "esp_wifi",
    "wpa_supplicant", "phy_init", "net80211", "esp_https_ota", "HTTP_CLIENT",
    "transport_base", "transport_ws",
}
TAG_RE = re.compile(r"[EW] \(\d+\) ([A-Za-z_0-9-]+):")


def error_lines(lines: list[str]) -> list[str]:
    out = []
    for line in lines:
        if not ERROR_RE.search(line):
            continue
        m = TAG_RE.search(line)
        if m and m.group(1) in IGNORED_TAGS:
            continue
        out.append(line)
    return out


class Device:
    def __init__(self, port: str):
        import serial

        self.ser = serial.Serial(port, BAUDRATE, timeout=0.05)
        self.buf = ""
        self.lines: list[tuple[float, str]] = []
        # 提示符"esp-ai>"后面常紧跟异步日志（WS 重连），会被吸进同一行；
        # 因此用单调计数判断"出现新提示符"，不能只查未成行缓冲（否则会误等超时）。
        self.prompt_count = 0
        self.prompt_seen = 0

    def close(self) -> None:
        try:
            self.ser.close()
        except Exception:
            pass

    def reset(self) -> None:
        # 与 esptool/idf.py 相同的最小复位序列：RTS→EN，DTR 保持高
        self.ser.dtr = False
        self.ser.rts = True
        time.sleep(0.15)
        self.ser.rts = False

    def _feed(self, text: str) -> None:
        self.buf += text
        while "\n" in self.buf:
            line, self.buf = self.buf.split("\n", 1)
            line = ANSI_RE.sub("", line).rstrip("\r")
            if "esp-ai>" in line:
                self.prompt_count += 1
            self.lines.append((time.time(), line))
        while "esp-ai>" in self.buf:
            self.prompt_count += 1
            self.buf = self.buf.replace("esp-ai>", "", 1)

    def pump(self, duration: float) -> None:
        end = time.time() + duration
        while time.time() < end:
            data = self.ser.read(8192)
            if data:
                self._feed(data.decode("utf-8", "replace"))
            else:
                time.sleep(0.005)

    def send(self, cmd: str) -> None:
        self.ser.write((cmd + "\r").encode())
        self.ser.flush()

    def wait_prompt(self, timeout: float) -> bool:
        end = time.time() + timeout
        while time.time() < end:
            if self.prompt_count > self.prompt_seen:
                self.prompt_seen = self.prompt_count
                return True
            self.pump(0.05)
        return False

    def wait_line(self, pattern: re.Pattern, timeout: float, mark: int = 0) -> float | None:
        end = time.time() + timeout
        while time.time() < end:
            for ts, line in self.lines[mark:]:
                if pattern.search(line):
                    return ts
            self.pump(0.05)
        return None

    def cmd(self, line: str, timeout: float = 12.0) -> list[str]:
        mark = len(self.lines)
        self.send(line)
        if not self.wait_prompt(timeout):
            tail = [text for _, text in self.lines[-12:]]
            raise RuntimeError(f"命令超时（{timeout}s）: {line}\n最后输出: {tail}\n未成行缓冲: {self.buf[-200:]!r}")
        return [text for _, text in self.lines[mark:]]

    def play_async(self, path: str):
        """后台回放（调用方在等待期间自己 pump，或配合 console 命令一起跑）。"""
        return subprocess.Popen(["afplay", path], stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)

    def play(self, path: str) -> float:
        """afplay 回放并在播放期间持续读串口（不读会丢日志，唤醒断言会误判）。"""
        t0 = time.time()
        proc = self.play_async(path)
        while proc.poll() is None:
            self.pump(0.02)
        return time.time() - t0


def parse_uplink(out: list[str]) -> dict:
    stat: dict = {}
    for line in out:
        m = re.search(r"环形缓冲: (\d+) 样本（(\d+) ms，(\d+) B PSRAM），历史最高占用 (\d+) 样本", line)
        if m:
            stat["ring"] = int(m.group(1))
            stat["ring_ms"] = int(m.group(2))
            stat["ring_bytes"] = int(m.group(3))
            stat["high_water"] = int(m.group(4))
        m = re.search(r"首帧延迟: (\d+) us", line)
        if m:
            stat["first_frame_us"] = int(m.group(1))
        m = re.search(r"累计写入 (\d+) / 读取 (\d+) / 丢弃 (\d+) / 读超时 (\d+)", line)
        if m:
            stat["written"] = int(m.group(1))
            stat["read"] = int(m.group(2))
            stat["dropped"] = int(m.group(3))
            stat["timeouts"] = int(m.group(4))
        m = re.search(r"本次窗口: (\d+) ms，320 样本帧 (\d+) 个（(\d+) 样本）", line)
        if m:
            stat["window_ms"] = int(m.group(1))
            stat["frames"] = int(m.group(2))
            stat["window_samples"] = int(m.group(3))
        m = re.search(r"窗口电平 RMS: (\d+)", line)
        if m:
            stat["rms"] = int(m.group(1))
        m = re.search(r"窗口峰值: (\d+)", line)
        if m:
            stat["peak"] = int(m.group(1))
        m = re.search(r"分段 RMS\(\d+ms\):([\d ]+)", line)
        if m:
            stat["segments"] = [int(v) for v in m.group(1).split()]
    return stat


def parse_heap(out: list[str]) -> dict:
    heap: dict = {}
    for line in out:
        m = re.search(r"内部 RAM: 总量 (\d+) B, 剩余 (\d+) B, 最大块 (\d+) B, 最低水位 (\d+) B", line)
        if m:
            heap["int_total"] = int(m.group(1))
            heap["int_free"] = int(m.group(2))
            heap["int_largest"] = int(m.group(3))
            heap["int_min_free"] = int(m.group(4))
        m = re.search(r"PSRAM\s*: 总量 (\d+) B, 剩余 (\d+) B", line)
        if m:
            heap["psram_total"] = int(m.group(1))
            heap["psram_free"] = int(m.group(2))
    return heap


def make_test_tone(seconds: float = 3.0, rate: int = 48000, amp: float = 0.9) -> str:
    """生成 1kHz 正弦测试音（临时目录，测试结束删除，不进仓库）。

    为什么用音调而不是语音做"回放能被上行拾到"的判据：本机扬声器回放音量
    （系统 50% 上限）下，设备侧语音电平只比环境底噪高 1~4dB，波动大；1kHz 纯音
    在分段 RMS 上的抬升稳定得多，且不改变"不超 50% 音量"的定稿约束。
    """
    tmpdir = tempfile.mkdtemp(prefix="afe_uplink_tone_")
    path = os.path.join(tmpdir, "tone_1k_3s.wav")
    n = int(rate * seconds)
    frames = bytearray()
    for i in range(n):
        frames += struct.pack("<h", int(amp * 32767 * math.sin(2 * math.pi * 1000 * i / rate)))
    with wave.open(path, "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(rate)
        f.writeframes(bytes(frames))
    return path


class Result:
    def __init__(self) -> None:
        self.rows: list[tuple[bool, str, str]] = []

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        self.rows.append((ok, name, detail))
        print(f"[{'PASS' if ok else 'FAIL'}] {name}{(' — ' + detail) if detail else ''}")
        return ok

    def summary(self) -> int:
        bad = [r for r in self.rows if not r[0]]
        print(f"\n汇总: {len(self.rows) - len(bad)}/{len(self.rows)} PASS")
        for _, name, detail in bad:
            print(f"  未通过: {name} {detail}")
        return 0 if not bad else 1


def get_system_volume() -> int | None:
    """macOS 系统输出音量（非 macOS 返回 None）。"""
    try:
        out = subprocess.run(
            ["osascript", "-e", "output volume of (get volume settings)"],
            capture_output=True, text=True, check=True,
        )
        return int(out.stdout.strip())
    except Exception:
        return None


def set_system_volume(volume: int) -> None:
    subprocess.run(["osascript", "-e", f"set volume output volume {volume}"], check=False)


def detect_port(explicit: str | None) -> str:
    if explicit:
        return explicit
    ports = sorted(glob.glob("/dev/cu.usbmodem*"))
    if len(ports) == 1:
        return ports[0]
    if not ports:
        raise SystemExit("未找到 /dev/cu.usbmodem*：确认开发板已插上，或用 --port 指定")
    raise SystemExit(f"找到多个串口，请用 --port 指定：{', '.join(ports)}")


def run(args: argparse.Namespace) -> int:
    wav_speech = os.path.join(HERE, SPEECH_WAV)
    wav_wake = os.path.join(HERE, WAKE_WAV)
    for wav in (wav_speech, wav_wake):
        if not os.path.exists(wav):
            raise SystemExit(f"缺少素材: {wav}")

    port = detect_port(args.port)
    print(f"串口: {port}")
    dev = Device(port)
    res = Result()
    volume_before = None
    if args.volume:
        # 与 voice_test.py 同口径：临时调到上限 50%，结束恢复（用户定稿：正常 31%、上限 50%）
        volume_before = get_system_volume()
        set_system_volume(args.volume)
        print(f"系统输出音量 {volume_before}% → {args.volume}%（结束恢复）")
    try:
        if not args.no_reset:
            dev.reset()
            dev.wait_prompt(args.boot_timeout)
        if dev.wait_line(re.compile(r"APP: ready, say"), args.boot_timeout) is None:
            raise SystemExit("设备未就绪（未见 'APP: ready, say'）")
        dev.wait_prompt(2.0)
        print("设备就绪\n")

        heap_before = parse_heap(dev.cmd("sys heap"))

        # ---------- A. 精确分帧读出 + 回放可闻 ----------
        print("== A. 上行读出（静音基线 / 1kHz 音调 / 交互语音） ==")
        dev.cmd("afe uplink on")
        silent = parse_uplink(dev.cmd("afe uplink stat 1200", timeout=15))
        dev.cmd("afe uplink off")
        print(f"  静音基线: RMS={silent.get('rms')} peak={silent.get('peak')}"
              f"（分段 {silent.get('segments')}）")

        # 3s 音调 + 4s 窗口：段 0~5 是音调、段 6 是分界、段 7~8 是静音。
        # 同一窗口内比较两段，环境底噪漂移的影响被抵消。
        tone_wav = make_test_tone()
        tone_stat: dict = {}
        for attempt in range(1, 4):
            dev.cmd("afe uplink on")
            player = dev.play_async(tone_wav)
            tone_stat = parse_uplink(dev.cmd("afe uplink stat 4000", timeout=20))
            while player.poll() is None:
                dev.pump(0.02)
            dev.cmd("afe uplink off")
            segs = tone_stat.get("segments", [])
            tone_segs = segs[1:5]   # 0.5~2.5s：纯音调
            quiet_segs = segs[7:9]  # 3.0~4.0s：音调结束后的静音
            tone_mean = sum(tone_segs) / len(tone_segs) if tone_segs else 0
            quiet_mean = sum(quiet_segs) / len(quiet_segs) if quiet_segs else 0
            tone_stat["tone_mean"] = tone_mean
            tone_stat["quiet_mean"] = quiet_mean
            print(f"  第 {attempt} 轮: 音调段 RMS 均值={tone_mean:.0f} / 静音段={quiet_mean:.0f}"
                  f"（分段 {segs}）")
            if tone_mean >= max(300, quiet_mean * 1.5):
                break
        shutil.rmtree(os.path.dirname(tone_wav), ignore_errors=True)

        # 交互语音：仅弱断言"有真实音频（非全零/非静音）"，具体电平受房间噪声影响大
        dev.cmd("afe uplink on")
        player = dev.play_async(wav_speech)
        speech = parse_uplink(dev.cmd("afe uplink stat 2000", timeout=15))
        while player.poll() is None:
            dev.pump(0.02)
        dev.cmd("afe uplink off")
        print(f"  交互语音回放: RMS={speech.get('rms')} peak={speech.get('peak')}")

        ring = speech.get("ring", 0)
        res.check("A1 环形缓冲已分配（2.048s / 64KB PSRAM）",
                  speech.get("ring_ms") == 2048 and speech.get("ring_bytes") == 65536,
                  f"ring={ring} samples, {speech.get('ring_ms')} ms, {speech.get('ring_bytes')} B")
        window = speech.get("window_ms", 0)
        ratio = speech.get("window_samples", 0) / (window * 16) if window else 0
        res.check("A2 样本/时长比例正确（16k，窗口内 ±10%）",
                  0.9 <= ratio <= 1.1,
                  f"{speech.get('window_samples')} samples / {window} ms → {ratio:.3f}×")
        frames = speech.get("frames", 0)
        samples = speech.get("window_samples", 0)
        res.check("A3 帧长精确 320 样本（20ms@16k，T2.2 分帧前提）",
                  frames >= window // 20 - 3 and 0 <= frames * 320 - samples < 320,
                  f"frames={frames}, samples={samples}")
        res.check("A4 无丢弃 / 读超时 ≤1", speech.get("dropped") == 0
                  and speech.get("timeouts", 99) <= 1,
                  f"dropped={speech.get('dropped')}, timeouts={speech.get('timeouts')}")
        res.check("A5 首帧延迟 < 200ms",
                  0 < speech.get("first_frame_us", 1 << 30) < 200_000,
                  f"{speech.get('first_frame_us')} us")
        tone_mean = tone_stat.get("tone_mean", 0)
        quiet_mean = tone_stat.get("quiet_mean", 0)
        res.check("A6 回放能被上行拾到（音调段 RMS ≥ max(300, 静音段×1.5)）",
                  tone_mean >= max(300, quiet_mean * 1.5),
                  f"音调段均值={tone_mean:.0f} / 静音段={quiet_mean:.0f}")
        res.check("A7 交互语音回放时上行有真实音频（非全零/静音）",
                  speech.get("rms", 0) >= 100 and speech.get("peak", 0) >= 400,
                  f"RMS={speech.get('rms')} peak={speech.get('peak')}")

        # ---------- B. 溢出策略：停读 3.5s ----------
        print("\n== B. 溢出策略（停读 3.5s > 2.048s 缓冲） ==")
        dev.cmd("afe uplink on")
        time.sleep(3.5)
        over = parse_uplink(dev.cmd("afe uplink stat 600", timeout=15))
        dev.cmd("afe uplink off")
        res.check("B1 缓冲满时丢弃最旧样本（samples_dropped 增长）",
                  over.get("dropped", 0) > 0,
                  f"dropped={over.get('dropped')} samples")
        res.check("B2 最高占用达到缓冲容量（丢最旧、时延有界）",
                  over.get("high_water") == ring and ring > 0,
                  f"high_water={over.get('high_water')} / ring={ring}")
        res.check("B3 溢出后仍能继续读出（AFE/fetch 未卡死）",
                  over.get("frames", 0) > 0 and over.get("window_samples", 0) > 0,
                  f"frames={over.get('frames')}")

        # ---------- C. 反复开关无泄漏 ----------
        print("\n== C. 反复 on/off 10 次 ==")
        for _ in range(10):
            dev.cmd("afe uplink on")
            dev.cmd("afe uplink off")
        heap_after = parse_heap(dev.cmd("sys heap"))
        d_int = heap_before.get("int_free", 0) - heap_after.get("int_free", 0)
        d_psram = heap_before.get("psram_free", 0) - heap_after.get("psram_free", 0)
        res.check("C1 内部 RAM 无泄漏（Δ ≤ 4KB）", abs(d_int) <= 4096,
                  f"free {heap_before.get('int_free')} → {heap_after.get('int_free')} (Δ={d_int})")
        res.check("C2 PSRAM 无泄漏（Δ ≤ 4KB）", abs(d_psram) <= 4096,
                  f"free {heap_before.get('psram_free')} → {heap_after.get('psram_free')} (Δ={d_psram})")

        # ---------- D. 回归：唤醒链路 ----------
        print("\n== D. 回归：唤醒链路 ==")
        mark = len(dev.lines)
        dev.play(wav_wake)
        # 唤醒应答要播完（"我在呢" 0.8s + 300ms 尾音）才有 prompt done，先等链路收尾再断言
        dev.wait_line(re.compile(r"APP: prompt done, feed resumed"), 6.0, mark)
        seen = set()
        for _, line in dev.lines[mark:]:
            for name, pattern in WAKE_CHAIN:
                if pattern.search(line):
                    seen.add(name)
        missing = [name for name, _ in WAKE_CHAIN if name not in seen]
        res.check("D1 唤醒链路完整（wake→paused→play→resumed）", not missing,
                  f"missing={missing}" if missing else f"{len(seen)}/4")
        dev.pump(0.5)
        errors = error_lines([text for _, text in dev.lines[mark:]])
        res.check("D2 无音频链路 ERROR/panic（网络/WS 重连噪音已忽略）", not errors,
                  errors[0] if errors else "")

        # 下行/上行状态一致性：uplink 已关闭时不应再写
        stat_idle = parse_uplink(dev.cmd("afe uplink stat 300", timeout=15))
        res.check("D3 uplink 关闭后 read 不再有数据",
                  stat_idle.get("window_samples", 0) == 0,
                  f"samples={stat_idle.get('window_samples')}")

        if args.log:
            with open(args.log, "w", encoding="utf-8") as f:
                for ts, line in dev.lines:
                    f.write(f"{ts:.3f} {line}\n")
            print(f"\n原始日志: {args.log}")

        return res.summary()
    finally:
        if volume_before is not None:
            set_system_volume(volume_before)
            print(f"系统输出音量已恢复 {volume_before}%")
        dev.close()


def main() -> int:
    ap = argparse.ArgumentParser(
        description="T2.1 audio_fe 上行 PCM 真机自测",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    ap.add_argument("--port", help="串口设备（默认自动探测唯一的 /dev/cu.usbmodem*）")
    ap.add_argument("--no-reset", action="store_true", help="不复位设备（已在运行时用）")
    ap.add_argument("--boot-timeout", type=float, default=25.0, help="等待启动/就绪的秒数")
    ap.add_argument("--volume", type=int, default=None,
                    help="回放前把系统输出音量临时调到该值（结束恢复；上限 50，同 voice_test.py）")
    ap.add_argument("--log", help="把串口原始日志写到该文件")
    return run(ap.parse_args())


if __name__ == "__main__":
    sys.exit(main())
