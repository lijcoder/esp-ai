#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["pyserial>=3.5"]
# ///
"""语音链路真机自测：唤醒 + 交互语音回放（不需要人说话）。

用 Mac 扬声器把 tools/ 里的 WAV 回放给设备麦克风，替代人工喊话/说话：
  - lexin.wav               唤醒词"嗨，乐鑫"（WakeNet9 wn9_hilexin）
  - 今天天气如何.wav         交互语音（一句话，模拟用户提问）
  - 今天吃了什么.wav         交互语音（同上）

脚本自动完成：串口复位/就绪等待 → `mic reset`/`mic stat` 读数 → 逐轮回放断言 →
汇总 PASS/FAIL（退出码 0/1）。断言口径见 tools/README.md「语音测试素材」章节。

用法：
    uv run tools/voice_test.py --port /dev/cu.usbmodemXXXX
    uv run tools/voice_test.py --wakes 3 --log /tmp/voice_test.log
    python3 tools/voice_test.py --no-reset        # 本机已装 pyserial 时可直接跑

为什么用录像回放而不是人工喊话：agent 无法说话；回放可重复、可计时、可断言，
唤醒率/误唤醒的对比才有意义。音量遵守用户定稿（系统输出 31% 正常、50% 上限）。
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
import time

HERE = os.path.dirname(os.path.abspath(__file__))
BAUDRATE = 115200
WAKE_WAV = "lexin.wav"
INTERACTION_WAVS = ("今天天气如何.wav", "今天吃了什么.wav")

# 允许的最大系统输出音量（用户定稿：正常 31%、上限 50%，不得突破）
SYSTEM_VOLUME_MAX = 50

# 唤醒链路的完整日志顺序（ARCHITECTURE §3.3 R1/R2）。T3.1 起唤醒编排归 voice_session
# （TAG VS），兼容旧固件的 APP 前缀。
WAKE_CHAIN = (
    ("wake", re.compile(r"(?:APP|VS): wake detected")),
    ("pause", re.compile(r"AFE: feed paused")),
    ("play", re.compile(r"PLAYER: play \d+ samples")),
    ("done", re.compile(r"(?:APP|VS): prompt done, feed resumed")),
)

ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


# ---------------------------------------------------------------- WAV 素材

def read_wav_info(path: str) -> dict:
    """读 WAV 头（不依赖 wave 模块：素材是流式头，RIFF/data 长度字段是占位值）。"""
    with open(path, "rb") as f:
        head = f.read(12)
        if head[:4] != b"RIFF" or head[8:12] != b"WAVE":
            raise ValueError(f"不是 RIFF/WAVE 文件: {path}")
        fmt = None
        while True:
            chunk = f.read(8)
            if len(chunk) < 8:
                break
            cid, size = chunk[:4], struct.unpack("<I", chunk[4:])[0]
            if cid == b"fmt ":
                body = f.read(size)
                audio_format, channels, rate, _, _, bits = struct.unpack("<HHIIHH", body[:16])
                fmt = {"format": audio_format, "channels": channels, "rate": rate, "bits": bits}
            elif cid == b"data":
                # 占位长度（0x7FFFFFxx）时按文件尾截断，afplay 同口径
                data_off = f.tell()
                data_bytes = size
                if data_off + data_bytes > os.path.getsize(path):
                    data_bytes = os.path.getsize(path) - data_off
                f.seek(0, os.SEEK_END)
                break
            else:
                f.seek(size, os.SEEK_CUR)
    if fmt is None:
        raise ValueError(f"缺 fmt 块: {path}")
    n = data_bytes // (fmt["bits"] // 8) // max(fmt["channels"], 1)
    duration = n / fmt["rate"] if fmt["rate"] else 0.0
    return {**fmt, "data_offset": data_off, "data_bytes": data_bytes,
            "samples": n, "duration": duration, "file_bytes": os.path.getsize(path)}


def wav_levels(path: str) -> tuple[int, int]:
    """返回 (峰值, RMS)，16 bit 线性值；只用于展示素材电平，不参与断言。"""
    info = read_wav_info(path)
    if info["bits"] != 16:
        return 0, 0
    with open(path, "rb") as f:
        f.seek(info["data_offset"])
        raw = f.read(info["data_bytes"])
    n = len(raw) // 2
    if n == 0:
        return 0, 0
    samples = struct.unpack("<%dh" % n, raw[: n * 2])
    peak = max(max(samples), -min(samples))
    rms = int(math.sqrt(sum(x * x for x in samples) / n))
    return peak, rms


def dbfs(v: int) -> str:
    return "-inf" if v <= 0 else f"{20 * math.log10(v / 32768.0):.1f}"


# ---------------------------------------------------------------- 串口设备

class Device:
    def __init__(self, port: str):
        import serial  # 延迟导入：--dry-run 不依赖 pyserial

        self.ser = serial.Serial(port, BAUDRATE, timeout=0.05)
        self.buf = ""
        self.lines: list[tuple[float, str]] = []

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
            self.lines.append((time.time(), line))

    def pump(self, duration: float) -> None:
        end = time.time() + duration
        while time.time() < end:
            data = self.ser.read(8192)
            if data:
                self._feed(data.decode("utf-8", "replace"))
            else:
                time.sleep(0.005)

    def mark(self) -> int:
        return len(self.lines)

    def lines_since(self, mark: int) -> list[tuple[float, str]]:
        return self.lines[mark:]

    def wait_line(self, pattern: re.Pattern, timeout: float, mark: int = 0) -> float | None:
        """等待 mark 之后首次出现 pattern，返回该行到达时间。"""
        end = time.time() + timeout
        while time.time() < end:
            for ts, line in self.lines[mark:]:
                if pattern.search(line):
                    return ts
            self.pump(0.05)
        return None

    def send(self, cmd: str) -> None:
        self.ser.write((cmd + "\r").encode())
        self.ser.flush()

    def console(self, cmd: str, end_pattern: re.Pattern, timeout: float = 2.0) -> list[str]:
        """发一条 console 命令，返回从下发到结束标记之间的输出行。"""
        mark = self.mark()
        self.send(cmd)
        end = time.time() + timeout
        while time.time() < end:
            for _, line in self.lines[mark:]:
                if end_pattern.search(line):
                    return [l for _, l in self.lines[mark:]]
            self.pump(0.05)
        return [l for _, l in self.lines[mark:]]

    def mic_stat(self) -> dict:
        out = self.console("mic stat", re.compile(r"参考:"))
        stat: dict = {}
        for line in out:
            m = re.search(r"窗口 (\d+) ms / (\d+) 采样", line)
            if m:
                stat["window_ms"] = int(m.group(1))
            m = re.search(r"电平: (\d+)", line)
            if m:
                stat["rms"] = int(m.group(1))
            m = re.search(r"峰值: (\d+)", line)
            if m:
                stat["peak"] = int(m.group(1))
            m = re.search(r"I2S 读失败 (\d+) 次", line)
            if m:
                stat["mic_read_errors"] = int(m.group(1))
            m = re.search(r"状态: 收音 (开启|暂停)", line)
            if m:
                stat["feed"] = m.group(1)
            m = re.search(r"VAD (人声|静音)", line)
            if m:
                stat["vad"] = m.group(1)
            m = re.search(r"累计唤醒 (\d+) 次", line)
            if m:
                stat["wake_count"] = int(m.group(1))
        return stat

    def mic_reset(self) -> bool:
        out = self.console("mic reset", re.compile(r"已清零|失败"))
        return any("已清零" in line for line in out)

    def play(self, path: str, gain: float | None = None) -> float:
        """afplay 回放，返回从启动到结束的墙钟时长（含 afplay 启动开销）。"""
        cmd = ["afplay"]
        if gain:
            cmd += ["-v", str(gain)]
        cmd.append(path)
        t0 = time.time()
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        while proc.poll() is None:
            self.pump(0.02)
        if proc.returncode != 0:
            err = proc.stderr.read().decode("utf-8", "replace").strip()
            raise RuntimeError(f"afplay 失败（{path}）：{err}")
        return time.time() - t0


# ---------------------------------------------------------------- 环境检查

def detect_port(explicit: str | None) -> str:
    if explicit:
        return explicit
    ports = sorted(glob.glob("/dev/cu.usbmodem*"))
    if len(ports) == 1:
        return ports[0]
    if not ports:
        raise SystemExit("未找到 /dev/cu.usbmodem*：确认开发板已插上，或用 --port 指定")
    raise SystemExit(f"找到多个串口，请用 --port 指定：{', '.join(ports)}")


def get_system_volume() -> dict | None:
    """macOS 系统输出音量（osascript），非 macOS 返回 None。"""
    if not shutil.which("osascript"):
        return None
    try:
        out = subprocess.run(
            ["osascript", "-e", "get volume settings"],
            capture_output=True, text=True, timeout=5,
        ).stdout
    except Exception:
        return None
    m = re.search(r"output volume:(\d+).*output muted:(\w+)", out)
    if not m:
        return None
    return {"volume": int(m.group(1)), "muted": m.group(2) == "true"}


def set_system_volume(volume: int) -> None:
    subprocess.run(
        ["osascript", "-e", f"set volume output volume {volume}"],
        capture_output=True, text=True, timeout=5, check=True,
    )


def preflight_assets() -> list[dict]:
    assets = [WAKE_WAV, *INTERACTION_WAVS]
    infos = []
    for name in assets:
        path = os.path.join(HERE, name)
        if not os.path.exists(path):
            raise SystemExit(f"缺少语音素材：{path}")
        info = read_wav_info(path)
        if info["format"] != 1 or info["channels"] != 1 or info["bits"] != 16:
            raise SystemExit(f"{name} 格式不符合预期（需要 PCM/16bit/mono）：{info}")
        peak, rms = wav_levels(path)
        infos.append({"name": name, "path": path, **info, "peak": peak, "rms": rms})
    return infos


# ---------------------------------------------------------------- 断言与报告

class Result:
    def __init__(self) -> None:
        self.rows: list[tuple[bool, str]] = []

    def check(self, ok: bool, label: str) -> bool:
        self.rows.append((ok, label))
        print(f"  {'PASS' if ok else 'FAIL'}  {label}")
        return ok

    def warn(self, label: str) -> None:
        self.rows.append((True, f"WARN {label}"))
        print(f"  WARN  {label}")

    def summary(self) -> bool:
        failed = [r for r in self.rows if not r[0]]
        print("\n===== 结果汇总 =====")
        for ok, label in self.rows:
            print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
        print(f"  合计 {len(self.rows) - len(failed)} 通过 / {len(failed)} 失败")
        return not failed


def error_lines(lines: list[str]) -> list[str]:
    """筛出与语音链路相关的错误：网络/WS 的 ERROR 与本次测试无关，忽略。"""
    pat = re.compile(r"ERROR|panic|abort|Guru Meditation|stack overflow|StoreProhibited")
    tag_re = re.compile(r"[EW] \(\d+\) ([A-Za-z_0-9-]+):")
    ignored_tags = {
        "NET", "WS", "wifi", "websocket_client", "esp_netif", "esp-tls", "esp_wifi",
        "wpa_supplicant", "phy_init", "net80211", "esp_https_ota", "HTTP_CLIENT",
    }
    out = []
    for line in lines:
        if not pat.search(line):
            continue
        m = tag_re.search(line)
        if m and m.group(1) in ignored_tags:
            continue
        out.append(line)
    return out


# ---------------------------------------------------------------- 主流程

def run(args: argparse.Namespace) -> int:
    res = Result()
    assets = preflight_assets()
    print("===== 素材 =====")
    for a in assets:
        print(
            f"  {a['name']}: {a['rate']} Hz / {a['bits']} bit / {a['channels']} ch / "
            f"{a['duration']:.2f}s / {a['file_bytes']} B / 峰值 {a['peak']} ({dbfs(a['peak'])} dBFS)"
            f" / RMS {a['rms']} ({dbfs(a['rms'])} dBFS)"
        )

    vol = get_system_volume()
    print("\n===== 回放环境 =====")
    original_volume = None
    if args.volume is not None:
        if vol is None:
            raise SystemExit("--volume 需要 macOS 的 osascript 支持")
        if not 0 <= args.volume <= SYSTEM_VOLUME_MAX:
            raise SystemExit(f"--volume 取值必须在 0~{SYSTEM_VOLUME_MAX}%（用户定稿上限，不得突破）")
        original_volume = vol["volume"]
        set_system_volume(args.volume)
        vol = dict(vol, volume=args.volume)
        print(f"  临时把系统输出音量从 {original_volume}% 调到 {args.volume}%（结束后恢复）")
    if vol is None:
        res.warn("未读到系统输出音量（非 macOS / osascript 不可用），音量口径请人工确认")
    else:
        print(f"  系统输出音量 {vol['volume']}%（正常 31%、上限 {SYSTEM_VOLUME_MAX}%）")
        res.check(not vol["muted"], "系统输出未静音")
        res.check(vol["volume"] <= SYSTEM_VOLUME_MAX,
                  f"系统输出音量 {vol['volume']}% ≤ 上限 {SYSTEM_VOLUME_MAX}%")

    dev = None
    try:
        port = detect_port(args.port)
        print(f"\n===== 串口 {port} =====")
        dev = Device(port)
        if not args.no_reset:
            dev.reset()
            dev.pump(0.3)
        mark = dev.mark()
        ready = None
        if not args.no_reset:
            ready = dev.wait_line(re.compile(r'ready, say "Hi Lexin"'), args.boot_timeout, mark)
        if ready is None:
            # --no-reset（设备已在跑）或复位后没等到启动行：用一条无副作用命令确认控制台
            print("  未捕获启动行，改用 console 探测（设备可能已在运行）")
            ready = time.time() if dev.mic_reset() else None
        boot = [l for _, l in dev.lines_since(mark)]
        res.check(ready is not None or any("esp-ai>" in l for l in boot), "设备启动/控制台就绪")
        for line in boot:
            if "DIAG: app" in line or "rst:" in line:
                print(f"  {line.strip()}")
        res.check(not error_lines(boot), "启动日志无 ERROR/panic")
        dev.pump(1.0)

        # 基线电平（安静窗口）
        print("\n===== 基线（安静 2s）=====")
        dev.mic_reset()
        dev.pump(2.0)
        base = dev.mic_stat()
        print(f"  RMS {base.get('rms')} / 峰值 {base.get('peak')} / I2S 读失败 {base.get('mic_read_errors')}")
        res.check(base.get("mic_read_errors") == 0, "基线 I2S 读失败 0 次")
        if (base.get("rms") or 0) > 800:
            res.warn(f"基线 RMS {base.get('rms')} 偏高（安静室内参考 40~270）：环境噪声会拉低唤醒率，"
                     f"可安静后复跑或用 --volume {SYSTEM_VOLUME_MAX}")

        # 唤醒测试
        print(f"\n===== 唤醒测试（{args.wakes} 轮，{WAKE_WAV}）=====")
        wake_pass = 0
        for i in range(args.wakes):
            dev.pump(args.pause_between)
            dev.mic_reset()
            mark = dev.mark()
            t_play = time.time()
            dev.play(os.path.join(HERE, WAKE_WAV))
            chain_hits = []
            for label, pat in WAKE_CHAIN:
                hit = dev.wait_line(pat, args.wake_timeout, mark)
                chain_hits.append(hit is not None)
            lines = [l for _, l in dev.lines_since(mark)]
            ok = all(chain_hits) and not error_lines(lines)
            if ok:
                wake_pass += 1
                wake_ts = next((ts for ts, l in dev.lines_since(mark) if "wake detected" in l), None)
                latency = (wake_ts - t_play) * 1000 if wake_ts else float("nan")
                print(f"  第 {i + 1} 轮 PASS  唤醒响应 {latency:.0f} ms（含 afplay 启动开销）")
            else:
                print(f"  第 {i + 1} 轮 FAIL  链路: "
                      + " → ".join(f"{n}={'ok' if h else 'miss'}" for (n, _), h in zip(WAKE_CHAIN, chain_hits)))
                for line in lines:
                    if "APP:" in line or "VS:" in line or "AFE:" in line or "PLAYER:" in line:
                        print(f"    {line.strip()}")
            dev.pump(0.3)
        res.check(wake_pass >= args.min_wake_pass,
                  f"唤醒链路完整 {wake_pass}/{args.wakes}（要求 ≥{args.min_wake_pass}）")
        if wake_pass < args.wakes:
            print("  提示：31% 是日常听感音量，环境噪声/距离/朝向都会拉低唤醒率；"
                  f"先确认系统未静音，再用 --volume {SYSTEM_VOLUME_MAX}（上限内）复测。")

        # 交互语音回放：设备麦克风应拾到人声（VAD 人声）、误唤醒 0、健康
        print(f"\n===== 交互语音回放（{len(INTERACTION_WAVS)} 个素材）=====")
        for name in INTERACTION_WAVS:
            path = os.path.join(HERE, name)
            dev.pump(args.pause_between)
            dev.mic_reset()
            mark = dev.mark()
            dev.play(path)
            dev.pump(0.2)
            stat = dev.mic_stat()
            lines = [l for _, l in dev.lines_since(mark)]
            false_wake = [l for l in lines if re.search(r"(?:APP|VS): wake detected", l)]
            print(f"  {name}: RMS {stat.get('rms')} / 峰值 {stat.get('peak')} / "
                  f"VAD {stat.get('vad')} / 收音 {stat.get('feed')} / "
                  f"误唤醒 {len(false_wake)} / I2S 读失败 {stat.get('mic_read_errors')}")
            res.check(not false_wake, f"{name} 未误唤醒（误唤醒 0）")
            res.check(stat.get("feed") == "开启", f"{name} 回放后收音已恢复（R2）")
            res.check(stat.get("mic_read_errors") == 0, f"{name} I2S 读失败 0 次")
            res.check(not error_lines(lines), f"{name} 回放期间无 ERROR/panic")
            if stat.get("vad") != "人声":
                res.warn(f"{name} 读数时 VAD 非人声（环境/音量影响，非硬失败，可复跑或提到 50%）")

        # 结束检查：设备仍健康
        print("\n===== 结束健康检查 =====")
        tail_mark = dev.mark()
        dev.console("sys info", re.compile(r"内存水位"))
        tail = [l for _, l in dev.lines_since(tail_mark)]
        for line in tail:
            if re.search(r"运行时长|内存水位", line):
                print(f"  {line.strip()}")
        res.check(not error_lines(tail), "结束时无 ERROR/panic")

        if args.log:
            with open(args.log, "w", encoding="utf-8") as f:
                for ts, line in dev.lines:
                    f.write(f"{ts:.3f} {line}\n")
            print(f"\n串口原始日志已保存：{args.log}")
    finally:
        if dev is not None:
            dev.close()
        if original_volume is not None:
            try:
                set_system_volume(original_volume)
                print(f"\n系统输出音量已恢复 {original_volume}%")
            except Exception as exc:  # 恢复失败不能静默，否则用户音量被留在测试档
                print(f"\n恢复系统输出音量失败：{exc}（请手动设为 {original_volume}%）")

    return 0 if res.summary() else 1


def main() -> int:
    ap = argparse.ArgumentParser(
        description="语音链路真机自测：回放 lexin.wav 唤醒 + 两个交互语音素材，自动断言串口日志。",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    ap.add_argument("--port", help="串口设备（默认自动探测唯一的 /dev/cu.usbmodem*）")
    ap.add_argument("--wakes", type=int, default=3, help="唤醒测试轮数")
    ap.add_argument("--min-wake-pass", type=int, default=None, help="最少唤醒成功轮数（默认=--wakes）")
    ap.add_argument("--volume", type=int, default=None,
                    help=f"测试期间临时把系统输出音量设为该值（0~{SYSTEM_VOLUME_MAX}，结束后恢复；默认不改）")
    ap.add_argument("--no-reset", action="store_true", help="不复位设备（设备已在运行时用）")
    ap.add_argument("--boot-timeout", type=float, default=25.0, help="等待启动/控制台的超时秒数")
    ap.add_argument("--wake-timeout", type=float, default=6.0, help="单轮唤醒链路超时秒数")
    ap.add_argument("--pause-between", type=float, default=2.0, help="每轮回放前静默等待秒数")
    ap.add_argument("--log", help="把串口原始日志写到该文件")
    ap.add_argument("--dry-run", action="store_true", help="只检查素材/音量/串口，不连设备")
    args = ap.parse_args()
    if args.min_wake_pass is None:
        args.min_wake_pass = args.wakes

    if args.dry_run:
        for a in preflight_assets():
            print(f"{a['name']}: {a['duration']:.2f}s {a['rate']}Hz")
        print("volume:", get_system_volume())
        print("port:", detect_port(args.port))
        return 0
    try:
        return run(args)
    except KeyboardInterrupt:
        print("\n中断（Ctrl-C）")
        return 130


if __name__ == "__main__":
    sys.exit(main())
