#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["pyserial>=3.5", "av>=12"]
# ///
"""T2.2 上行 Opus 编码真机自测：codec_enc 输出帧的独立解码与数值断言。

验证内容（对应 docs/ROADMAP.md T2.2 验收项）：
  A. tone 源数值基准：`enc dump on` → `enc test <ms> tone`，抓 ENCDUMP 帧后用
     PyAV **独立解码**（不信任设备自报数值），断言：解码失败 0、解码帧率 ∈ {16000, 48000}
     （Opus 原生输出，设备实测 48000/960 样本/帧，不是 320/16k）、Opus TOC 单声道 + 20ms 帧、
     总时长 ≈ 抓帧数×20ms、1kHz 正弦 FFT 峰值 1000±20Hz
     且该频段能量占比 >50%，以及 frames/bytes_per_frame/bitrate/encode 耗时/
     errors/carry 等结构性门槛；
  B. uplink 源真实链路：`afe uplink on` → 静音基线 → `afplay` 回放交互语音 →
     `enc test <ms> uplink`，数值门槛同 A（B 用例改用三条 OR 自适应"有语音"判据，见 speech_presence_ok），
     并检查无 I2S 读失败 / ERROR / panic；结束务必 `enc dump off`、`afe uplink off`；
  C. 内存/泄漏：连续 3 次 `enc test 1000 tone`，对比 `sys heap` 前后差值与 done 行的
     internal_free_delta / psram_free_delta，断言内部 RAM 增量 ≤8KB、重复调用不持续
     下降（后续 Δ ≤4KB）。

离线自检（不连串口/设备，用 PyAV libopus 造真实 Opus 帧跑完整断言逻辑）：
    uv run tools/opus_enc_test.py --self-test

真机（**独占串口**，先关掉 idf.py monitor；固件由 T2.2 实现后才有 enc 命令）：
    uv run tools/opus_enc_test.py --port /dev/cu.usbmodemXXXX
    uv run tools/opus_enc_test.py --no-reset --window-ms 2000 --log /tmp/opus_enc.log
    uv run tools/opus_enc_test.py --keep-wav /tmp/opus_wavs   # 保留解码 WAV（试听 A_tone/B_speech）

设备侧 console 契约（由 components/codec_enc 提供，本工具只消费输出）：
    enc dump on|off                 → `ENC dump on` / `ENC dump off`
    enc test <ms> [tone|uplink]     → `ENC TEST start: ...` + N 行 `ENCDUMP ...` + `ENC TEST done: ...`
    enc stat                        → `ENC stat: open=.. frames=.. ...`
    enc reset                       → `ENC stats reset`
字段含义见 tools/README.md「T2.2 上行 Opus 编码自测」一节。
"""

from __future__ import annotations

import argparse
import glob
import math
import os
import re
import struct
import subprocess
import sys
import tempfile
import time
import wave

HERE = os.path.dirname(os.path.abspath(__file__))
BAUDRATE = 115200
DECODE_RATE = 16000          # 上行/分析采样率（CONVENTIONS §3）；Opus 解码器原生输出 48k，见 decode_opus_to_wav
FRAME_MS = 20                # Opus 20ms 帧
FRAME_SAMPLES = DECODE_RATE * FRAME_MS // 1000  # 320 样本
SPEECH_WAV = "今天天气如何.wav"
TONE_FREQ_HZ = 1000.0        # 设备 tone 源约定的 1kHz 正弦

# 设备就绪标记（main/ 启动编排，与 afe_uplink_test.py 同口径）
APP_READY_RE = re.compile(r"APP: ready, say")

# 设备 console 输出格式（T2.2 契约，见任务/README）
START_RE = re.compile(r"ENC TEST start: source=(\w+) window_ms=(\d+) dump=(on|off)")
DUMP_RE = re.compile(r"ENCDUMP (\d+) (\d+) ([0-9a-fA-F]+)")
DONE_RE = re.compile(r"ENC TEST done:\s*(.+)$")
DUMP_MISSED_RE = re.compile(r"ENC dump missed: (\d+) 帧")
DUMP_DONE_RE = re.compile(r"ENC dump done: n=(\d+)")
HEAP_INT_RE = re.compile(r"内部 RAM: 总量 (\d+) B, 剩余 (\d+) B, 最大块 (\d+) B, 最低水位 (\d+) B")
HEAP_PSRAM_RE = re.compile(r"PSRAM\s*: 总量 (\d+) B, 剩余 (\d+) B")

ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
ERROR_RE = re.compile(r"\bE \(|ERROR|panic|abort\(\)|Guru Meditation|stack overflow")
# 与本次测试无关的持续重连噪音（mock/线上服务未起时的 net/ws/tls 错误），同 voice_test.py / afe_uplink_test.py 口径
IGNORED_TAGS = {
    "NET", "WS", "wifi", "websocket_client", "esp_netif", "esp-tls", "esp_wifi",
    "wpa_supplicant", "phy_init", "net80211", "esp_https_ota", "HTTP_CLIENT",
    "transport_base", "transport_ws",
}
TAG_RE = re.compile(r"[EW] \(\d+\) ([A-Za-z_0-9-]+):")
I2S_FAIL_RE = re.compile(r"(读失败|读取失败|read fail(?:ed)?|read error|读超时)", re.I)
I2S_FAIL_COUNT_RE = re.compile(
    r"(?:读失败|读取失败|read fail(?:ed)?|read error|读超时)[^0-9]{0,8}(\d+)", re.I)

# Opus TOC（RFC 6716 §3.1）：config(5b) | stereo(1b) | frame-count code(2b)。
# config 0..15 是 SILK/hybrid（帧长 10/20/40/60ms），16..31 是 CELT（2.5/5/10/20ms）。
OPUS_FRAME_MS = [
    10, 20, 40, 60, 10, 20, 40, 60, 10, 20, 40, 60, 10, 20, 10, 20,
    2.5, 5, 10, 20, 2.5, 5, 10, 20, 2.5, 5, 10, 20, 2.5, 5, 10, 20,
]


def error_lines(lines: list[str]) -> list[str]:
    """ERROR/panic 行，忽略网络/WS 重连噪音（同 afe_uplink_test.py）。"""
    out = []
    for line in lines:
        if not ERROR_RE.search(line):
            continue
        m = TAG_RE.search(line)
        if m and m.group(1) in IGNORED_TAGS:
            continue
        out.append(line)
    return out


def i2s_failure_lines(lines: list[str]) -> list[str]:
    """I2S 读失败日志行；"读失败 0 次" 这类统计行（0 次）不算失败。"""
    out = []
    for line in lines:
        if "i2s" not in line.lower() or not I2S_FAIL_RE.search(line):
            continue
        m = I2S_FAIL_COUNT_RE.search(line)
        if m and int(m.group(1)) == 0:
            continue
        out.append(line)
    return out


class Device:
    """串口设备封装（复位/提示符同步/命令下发），与 afe_uplink_test.py 同风格。"""

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


def safe_cmd(dev: Device, line: str, timeout: float = 5.0) -> list[str]:
    """收尾/清理用：命令失败不抛异常，返回已收到的输出。"""
    try:
        return dev.cmd(line, timeout=timeout)
    except Exception:
        return []


# ---------------------------------------------------------------- 解析


def _num(value: str):
    try:
        return int(value)
    except ValueError:
        try:
            return float(value)
        except ValueError:
            return value


def parse_enc_output(lines: list[str]) -> dict:
    """解析一次 `enc test` 输出（start / ENCDUMP 帧 / done）。"""
    parsed: dict = {"start": None, "done": {}, "dumps": [], "dump_missed": None, "dump_done": None}
    for line in lines:
        m = START_RE.search(line)
        if m:
            parsed["start"] = {
                "source": m.group(1),
                "window_ms": int(m.group(2)),
                "dump": m.group(3),
            }
            continue
        m = DUMP_RE.search(line)
        if m:
            hex_str = m.group(3)
            try:
                payload = bytes.fromhex(hex_str)
            except ValueError:
                payload = b""
            parsed["dumps"].append({
                "seq": int(m.group(1)),
                "len": int(m.group(2)),
                "hex": hex_str,
                "payload": payload,
            })
            continue
        m = DONE_RE.search(line)
        if m:
            kv = dict(re.findall(r"([A-Za-z_][A-Za-z0-9_]*)=(-?[\w.]+)", m.group(1)))
            parsed["done"] = {k: _num(v) for k, v in kv.items()}
            continue
        m = DUMP_MISSED_RE.search(line)
        if m:
            parsed["dump_missed"] = int(m.group(1))
            continue
        m = DUMP_DONE_RE.search(line)
        if m:
            parsed["dump_done"] = int(m.group(1))
    return parsed


def dump_payloads(parsed: dict) -> list[bytes]:
    return [d["payload"] for d in parsed["dumps"]]


def parse_heap(out: list[str]) -> dict:
    heap: dict = {}
    for line in out:
        m = HEAP_INT_RE.search(line)
        if m:
            heap["int_total"] = int(m.group(1))
            heap["int_free"] = int(m.group(2))
            heap["int_largest"] = int(m.group(3))
            heap["int_min_free"] = int(m.group(4))
        m = HEAP_PSRAM_RE.search(line)
        if m:
            heap["psram_total"] = int(m.group(1))
            heap["psram_free"] = int(m.group(2))
    return heap


def opus_toc(payload: bytes) -> dict:
    """解析 Opus 包 TOC 字节，得到帧长与声道数（独立于解码器配置）。"""
    if not payload:
        return {"config": None, "stereo": None, "code": None, "frame_ms": None}
    toc = payload[0]
    config = toc >> 3
    return {
        "config": config,
        "stereo": (toc >> 2) & 1,
        "code": toc & 0x03,
        "frame_ms": OPUS_FRAME_MS[config] if config < len(OPUS_FRAME_MS) else None,
    }


# ---------------------------------------------------------------- 解码 / DSP
# 本机 av 19 的 wheel 未声明 numpy 依赖，因此这里不用 numpy：
# 解码 → av.AudioResampler(16k/mono/s16) → 写临时 WAV → 标准库 wave 读样本，
# FFT 用纯 Python 迭代式 Cooley-Tukey（2s@16k 约 0.05s，足够快，且不多装依赖）。


KEEP_WAV_DIR: str | None = None


class _WavDir:
    """解码 WAV 的落地目录：默认临时目录（用完即删）；--keep-wav 时保留给用户试听。"""

    def __init__(self, prefix: str):
        self.prefix = prefix
        self._tmp: tempfile.TemporaryDirectory | None = None

    def __enter__(self) -> str:
        if KEEP_WAV_DIR:
            os.makedirs(KEEP_WAV_DIR, exist_ok=True)
            return KEEP_WAV_DIR
        self._tmp = tempfile.TemporaryDirectory(prefix=self.prefix)
        return self._tmp.name

    def __exit__(self, *_exc) -> bool:
        if self._tmp is not None:
            self._tmp.cleanup()
        return False


def decode_opus_to_wav(payloads: list[bytes], wav_path: str) -> dict:
    """把一组 Opus 包逐个独立解码并写成 16k/mono/s16 WAV，同时收集解码信息。"""
    import av

    info: dict = {
        "errors": [],
        "decoded_frames": 0,
        "corrupt": 0,
        "native_rates": set(),
        "native_layouts": set(),
        # Opus 解码器原生输出（设备实测 48kHz，960 样本/帧），时长必须按 frame.sample_rate 折算，
        # 不能写死 16kHz：16k 只是我们重采样后的分析采样率。
        # 注意：裸 Opus 包无容器 extradata 时 libopus 默认按 2ch 输出（native_layouts=stereo），
        # 单声道以 TOC 为准；重采样 downmix 到 mono 后 RMS/FFT 不受影响。
        "native_samples": 0,
        "native_ms": 0.0,
    }
    if not payloads:
        info["errors"].append("无 Opus 包可解码")
        return info

    dec = av.CodecContext.create("opus", "r")
    rs = av.AudioResampler(format="s16", layout="mono", rate=DECODE_RATE)
    out = av.open(wav_path, "w", format="wav")
    stream = out.add_stream("pcm_s16le", rate=DECODE_RATE, layout="mono")

    def feed(frames) -> None:
        for frame in frames:
            info["decoded_frames"] += 1
            info["native_rates"].add(frame.sample_rate)
            info["native_layouts"].add(frame.layout.name)
            info["native_samples"] += frame.samples
            if frame.sample_rate:
                info["native_ms"] += frame.samples * 1000.0 / frame.sample_rate
            if getattr(frame, "is_corrupt", False):
                info["corrupt"] += 1
            for resampled in rs.resample(frame):
                for pkt in stream.encode(resampled):
                    out.mux(pkt)

    try:
        for idx, payload in enumerate(payloads):
            try:
                feed(dec.decode(av.Packet(payload)))
            except Exception as exc:  # 单帧解码失败只记账，继续解后续帧
                info["errors"].append(f"#{idx}: {type(exc).__name__}: {exc}")
        try:
            feed(dec.decode(None))
        except Exception as exc:
            info["errors"].append(f"flush: {type(exc).__name__}: {exc}")
        for resampled in rs.resample(None):
            for pkt in stream.encode(resampled):
                out.mux(pkt)
        for pkt in stream.encode(None):
            out.mux(pkt)
    finally:
        out.close()
    return info


def decode_opus_libopus_wav(payloads: list[bytes], wav_path: str) -> dict:
    """量纲交叉校验用：libopus 解码器原生 s16 输出 → 48k/stereo WAV（不做 16k 重采样）。

    用来验证分析 PCM（16k/mono/s16）与"解码 float×32768 / libopus s16"同量纲：
    设备 A tone（ENC_TONE_AMPLITUDE=8000）解码后分析 PCM RMS≈5556，与 libopus s16 5555 一致。
    """
    import av

    info: dict = {"errors": [], "frames": 0}
    if not payloads:
        info["errors"].append("无 Opus 包可解码")
        return info
    dec = av.CodecContext.create("libopus", "r")
    rs = av.AudioResampler(format="s16", layout="stereo", rate=48000)
    out = av.open(wav_path, "w", format="wav")
    stream = out.add_stream("pcm_s16le", rate=48000, layout="stereo")

    def feed(frames) -> None:
        for frame in frames:
            info["frames"] += 1
            for resampled in rs.resample(frame):
                for pkt in stream.encode(resampled):
                    out.mux(pkt)

    try:
        for idx, payload in enumerate(payloads):
            try:
                feed(dec.decode(av.Packet(payload)))
            except Exception as exc:
                info["errors"].append(f"#{idx}: {type(exc).__name__}: {exc}")
        try:
            feed(dec.decode(None))
        except Exception as exc:
            info["errors"].append(f"flush: {type(exc).__name__}: {exc}")
        for resampled in rs.resample(None):
            for pkt in stream.encode(resampled):
                out.mux(pkt)
        for pkt in stream.encode(None):
            out.mux(pkt)
    finally:
        out.close()
    return info


def read_wav(path: str) -> dict:
    with wave.open(path, "rb") as w:
        channels = w.getnchannels()
        sampwidth = w.getsampwidth()
        rate = w.getframerate()
        nframes = w.getnframes()
        raw = w.readframes(nframes)
    if sampwidth != 2:
        raise RuntimeError(f"解码 WAV 位宽非 16bit: {sampwidth}")
    samples = list(struct.unpack(f"<{len(raw) // 2}h", raw)) if raw else []
    return {"channels": channels, "rate": rate, "frames": nframes, "samples": samples}


def decode_and_read(payloads: list[bytes], wav_path: str) -> tuple[dict, dict]:
    info = decode_opus_to_wav(payloads, wav_path)
    if not info["decoded_frames"]:
        return info, {"channels": 0, "rate": 0, "frames": 0, "samples": []}
    return info, read_wav(wav_path)


def rms_of(samples: list) -> float:
    if not samples:
        return 0.0
    total = 0.0
    for value in samples:
        total += float(value) * float(value)
    return math.sqrt(total / len(samples))


def segment_rms(samples: list, seg_ms: int = 100) -> list[float]:
    seg = max(1, DECODE_RATE * seg_ms // 1000)
    return [rms_of(samples[i:i + seg]) for i in range(0, len(samples), seg)]


def fft_power(samples: list) -> tuple[list[float], int]:
    """Hann 窗 + 补零到 2 的幂的迭代 FFT，返回 (各 bin 功率, FFT 点数)。"""
    n = 1
    while n < len(samples):
        n <<= 1
    re_part = [0.0] * n
    im_part = [0.0] * n
    mean = sum(samples) / len(samples) if samples else 0.0
    span = max(1, len(samples) - 1)
    for i, value in enumerate(samples):
        window = 0.5 - 0.5 * math.cos(2 * math.pi * i / span)
        re_part[i] = (float(value) - mean) * window

    # 位反转置换
    j = 0
    for i in range(1, n):
        bit = n >> 1
        while j & bit:
            j ^= bit
            bit >>= 1
        j |= bit
        if i < j:
            re_part[i], re_part[j] = re_part[j], re_part[i]
            im_part[i], im_part[j] = im_part[j], im_part[i]

    # 蝶形运算
    length = 2
    while length <= n:
        angle = -2 * math.pi / length
        wlen_re, wlen_im = math.cos(angle), math.sin(angle)
        half = length >> 1
        for start in range(0, n, length):
            wr, wi = 1.0, 0.0
            for k in range(start, start + half):
                ur, ui = re_part[k], im_part[k]
                xr, xi = re_part[k + half], im_part[k + half]
                vr = xr * wr - xi * wi
                vi = xr * wi + xi * wr
                re_part[k], im_part[k] = ur + vr, ui + vi
                re_part[k + half], im_part[k + half] = ur - vr, ui - vi
                wr, wi = wr * wlen_re - wi * wlen_im, wr * wlen_im + wi * wlen_re
        length <<= 1

    powers = [re_part[k] * re_part[k] + im_part[k] * im_part[k] for k in range(n // 2 + 1)]
    return powers, n


def spectrum_stats(samples: list, rate: int = DECODE_RATE, band_lo: float = 980.0,
                   band_hi: float = 1020.0) -> dict:
    """FFT 主峰频率 + [band_lo, band_hi] 频段能量占比（去 DC）。"""
    if not samples:
        return {"peak_hz": 0.0, "band_ratio": 0.0, "total_power": 0.0}
    powers, n = fft_power(samples)
    total = sum(powers[1:])
    best_k, best_p = 0, -1.0
    for k in range(1, len(powers)):
        if powers[k] > best_p:
            best_k, best_p = k, powers[k]
    peak_hz = best_k * rate / n
    band = 0.0
    for k in range(1, len(powers)):
        freq = k * rate / n
        if band_lo <= freq <= band_hi:
            band += powers[k]
    return {
        "peak_hz": peak_hz,
        "band_ratio": band / total if total > 0 else 0.0,
        "total_power": total,
    }


def band_energy_ratio(samples: list, rate: int = DECODE_RATE,
                      band_lo: float = 300.0, band_hi: float = 3400.0) -> float:
    """FFT 频带（默认语音 300~3400Hz）能量占比；B 用例的"有语音"定性判据之一。"""
    if not samples:
        return 0.0
    powers, n = fft_power(samples)
    total = sum(powers[1:])
    if total <= 0:
        return 0.0
    band = 0.0
    for k in range(1, len(powers)):
        freq = k * rate / n
        if band_lo <= freq <= band_hi:
            band += powers[k]
    return band / total


def speech_presence_ok(seg_rms: list, baseline_rms: float,
                       voice_band_ratio: float) -> tuple[bool, dict]:
    """B 用例"有语音"自适应判据（三条 OR，均与绝对音量无关）：

    ① 最高 100ms 分段 RMS ≥ max(静音基线×3, 30)；
    ② 最高段 > 全段中位数×1.3；
    ③ 300~3400Hz（语音频带）能量占比 > 0.6。
    真机实测（系统音量低、脚本不改音量）：B dump 最高段 79~108、中位 54~57、静音基线 7~10，
    绝对 RMS 只有几十；因此不能再设 150 这种按"满量程 PCM"设计的绝对地板。
    每条判据里的静音基线都来自同一条解码链路（同一量纲）。
    """
    max_seg = max(seg_rms) if seg_rms else 0.0
    median = sorted(seg_rms)[len(seg_rms) // 2] if seg_rms else 0.0
    amp_floor = max(baseline_rms * 3.0, 30.0)
    by_amp = max_seg >= amp_floor
    by_median = median > 0 and max_seg > median * 1.3
    by_band = voice_band_ratio > 0.6
    return (by_amp or by_median or by_band), {
        "max_seg": max_seg,
        "median": median,
        "amp_floor": amp_floor,
        "band_ratio": voice_band_ratio,
        "by_amp": by_amp,
        "by_median": by_median,
        "by_band": by_band,
    }


def encode_tone_opus_packets(window_ms: int, freq: float = TONE_FREQ_HZ,
                             amp: float = 0.5) -> list[bytes]:
    """--self-test 用：PyAV libopus 造 16k/单声道/20ms 正弦 Opus 帧（真实可解码帧）。"""
    import av
    from fractions import Fraction

    frames = max(1, window_ms // FRAME_MS)
    enc = av.CodecContext.create("libopus", "w")
    enc.sample_rate = DECODE_RATE
    enc.format = "s16"
    enc.layout = "mono"
    enc.bit_rate = 16000
    enc.time_base = Fraction(1, DECODE_RATE)
    packets: list[bytes] = []
    for i in range(frames):
        base = i * FRAME_SAMPLES
        sig = [int(amp * 32767 * math.sin(2 * math.pi * freq * (base + j) / DECODE_RATE))
               for j in range(FRAME_SAMPLES)]
        frame = av.AudioFrame(format="s16", layout="mono", samples=FRAME_SAMPLES)
        frame.sample_rate = DECODE_RATE
        frame.pts = base
        frame.time_base = Fraction(1, DECODE_RATE)
        frame.planes[0].update(struct.pack(f"<{FRAME_SAMPLES}h", *sig))
        packets.extend(bytes(p) for p in enc.encode(frame))
    packets.extend(bytes(p) for p in enc.encode(None))
    return packets


# ---------------------------------------------------------------- 结果汇总


class Result:
    def __init__(self) -> None:
        self.rows: list[tuple[bool, str, str]] = []

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        self.rows.append((bool(ok), name, detail))
        print(f"[{'PASS' if ok else 'FAIL'}] {name}{(' — ' + detail) if detail else ''}")
        return bool(ok)

    def summary(self) -> int:
        bad = [r for r in self.rows if not r[0]]
        print(f"\n汇总: {len(self.rows) - len(bad)}/{len(self.rows)} PASS")
        for _, name, detail in bad:
            print(f"  未通过: {name} {detail}")
        return 0 if not bad else 1


# ---------------------------------------------------------------- 公共断言


def _is_num(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def check_stream(res: Result, tag: str, expected_source: str, parsed: dict,
                 window_ms: int, wav: dict, info: dict, start: int = 1,
                 frame_tol: int = 2) -> dict:
    """A/B 共用的结构性 + 数值断言；返回统计值供调用方继续断言。"""
    done = parsed["done"]
    declared = parsed["start"] or {}
    dumps = parsed["dumps"]
    payloads = [d["payload"] for d in dumps]
    n = len(dumps)
    idx = start

    ok_start = (declared.get("source") == expected_source
                and declared.get("window_ms") == window_ms
                and declared.get("dump") == "on")
    res.check(f"{tag}{idx} 起始行正确（source={expected_source}, window_ms={window_ms}, dump=on）",
              ok_start,
              f"source={declared.get('source')} window_ms={declared.get('window_ms')} dump={declared.get('dump')}")
    idx += 1

    seqs = [d["seq"] for d in dumps]
    seq_detail = (f"dump={n} seq={seqs[0]}..{seqs[-1]}") if n else "未抓到 ENCDUMP"
    res.check(f"{tag}{idx} ENCDUMP seq 从 0 连续（每帧一行）",
              n > 0 and seqs == list(range(n)), seq_detail)
    idx += 1

    # 设备 dump 缓冲有上限（当前固件 50 帧=1s）：2000ms 窗口只会 dump 前 1s，
    # 剩余帧打 `ENC dump missed: N 帧`。对账口径：dump + missed == done.frames。
    frames = done.get("frames")
    missed = parsed.get("dump_missed") or 0
    dump_done = parsed.get("dump_done")
    ok_recon = _is_num(frames) and n > 0 and frames == n + missed
    if dump_done is not None and dump_done != n:
        ok_recon = False
    recon_detail = f"dump={n} + missed={missed} / done.frames={frames}"
    if missed:
        recon_detail += f"（dump 缓冲上限 {n} 帧，窗口内其余帧未记录）"
    if dump_done is not None and dump_done != n:
        recon_detail += f"；ENC dump done:n={dump_done} 与行数不符"
    res.check(f"{tag}{idx} ENCDUMP 帧数与 done.frames 对账（兼容 dump 缓冲截断）",
              ok_recon, recon_detail)
    idx += 1

    bad_len = [d["seq"] for d in dumps if d["len"] != len(d["payload"])]
    bad_hex = [d["seq"] for d in dumps if d["hex"] != d["hex"].lower()]
    res.check(f"{tag}{idx} ENCDUMP len=hex 字节数且 hex 小写",
              n > 0 and not bad_len and not bad_hex,
              f"len 不符={bad_len[:5]} 非小写={bad_hex[:5]}" if (bad_len or bad_hex) else f"{n} 帧")
    idx += 1

    res.check(f"{tag}{idx} PyAV 独立解码无失败帧",
              not info["errors"] and info["decoded_frames"] > 0 and info["corrupt"] == 0,
              info["errors"][0] if info["errors"]
              else f"解码 {info['decoded_frames']} 帧 / corrupt={info['corrupt']}")
    idx += 1

    native_rates = sorted(r for r in info["native_rates"] if r)
    res.check(f"{tag}{idx} 解码帧率 ∈ {{16000, 48000}}（Opus 原生输出；设备实测 48000、960 样本/帧）",
              bool(native_rates) and all(rate in (16000, 48000) for rate in native_rates),
              f"native_rates={native_rates} 帧数={info['decoded_frames']} "
              f"总样本={info['native_samples']} 布局={sorted(info['native_layouts'])}")
    idx += 1

    tocs = [opus_toc(p) for p in payloads]
    bad_toc = [i for i, t in enumerate(tocs)
               if t["stereo"] != 0 or t["frame_ms"] is None or abs(t["frame_ms"] - FRAME_MS) > 1e-6]
    res.check(f"{tag}{idx} Opus TOC：单声道 + 20ms 帧",
              n > 0 and not bad_toc,
              f"bad={bad_toc[:5]} 帧长集合={sorted({t['frame_ms'] for t in tocs})} "
              f"stereo集合={sorted({t['stereo'] for t in tocs})}" if bad_toc
              else f"{n} 帧全部 mono/20ms")
    idx += 1

    # 时长按解码帧原生 sample_rate 折算（设备 48k），不依赖我们重采样后的 16k 分析 PCM
    total_ms = info["native_ms"]
    expect_ms = n * FRAME_MS
    dur_ok = n > 0 and expect_ms > 0 and abs(total_ms - expect_ms) <= 0.05 * expect_ms
    res.check(f"{tag}{idx} 总时长 ≈ 抓帧数×20ms（±5%，按解码帧原生采样率折算）", dur_ok,
              f"解码 {total_ms:.1f} ms vs {expect_ms} ms"
              f"（偏差 {((total_ms - expect_ms) / expect_ms * 100 if expect_ms else 0):+.1f}%）")
    idx += 1

    expect_frames = window_ms // FRAME_MS
    # tone 源压固定帧数，严格 ±2；uplink 源固件在测试前用 timeout=0 循环排空 ring 积压
    # （并打印 `ENC TEST: 已丢弃 N 帧积压`，start 行之前、解析器忽略），实测 2000ms → 100 帧，
    # ±3 只吸收调度边界。
    res.check(f"{tag}{idx} 帧数 ≈ window_ms/20 ±{frame_tol}"
              + ("（uplink：固件测试前排空 ring 积压）" if expected_source == "uplink" else ""),
              _is_num(frames) and abs(frames - expect_frames) <= frame_tol,
              f"frames={frames} 期望 {expect_frames}±{frame_tol}")
    idx += 1

    sizes = [len(p) for p in payloads]
    avg_bpf = sum(sizes) / n if n else 0.0
    reported_bpf = done.get("bytes_per_frame")
    res.check(f"{tag}{idx} avg bytes/frame 26~46 B（VBR 实测约 33~35 B）",
              n > 0 and 26 <= avg_bpf <= 46 and _is_num(reported_bpf) and 26 <= reported_bpf <= 46,
              f"实测 {avg_bpf:.2f} B（{min(sizes) if sizes else 0}~{max(sizes) if sizes else 0}）"
              f" / 设备 {reported_bpf}")
    idx += 1

    bitrate = done.get("bitrate_kbps")
    computed_kbps = (sum(sizes) * 8 / (n * FRAME_MS / 1000) / 1000) if n else 0.0
    res.check(f"{tag}{idx} bitrate 11~18 kbps（VBR 实测约 13~14 kbps）",
              _is_num(bitrate) and 11 <= bitrate <= 18,
              f"设备 {bitrate} kbps / 抓帧折算 {computed_kbps:.2f} kbps")
    idx += 1

    avg_ms = done.get("encode_avg_ms")
    res.check(f"{tag}{idx} encode_avg_ms ≤ 10", _is_num(avg_ms) and avg_ms <= 10,
              f"{avg_ms} ms")
    idx += 1

    max_ms = done.get("encode_max_ms")
    res.check(f"{tag}{idx} encode_max_ms ≤ 15", _is_num(max_ms) and max_ms <= 15,
              f"{max_ms} ms")
    idx += 1

    res.check(f"{tag}{idx} errors=0 且 carry=0",
              done.get("errors") == 0 and done.get("carry") == 0,
              f"errors={done.get('errors')} carry={done.get('carry')} "
              f"max_packet={done.get('max_packet')} elapsed_ms={done.get('elapsed_ms')}")
    idx += 1

    return {
        "n": n,
        "sizes": sizes,
        "avg_bpf": avg_bpf,
        "total_ms": total_ms,
        "frames": frames,
        "next_idx": idx,  # 调用方续编号用
    }


# ---------------------------------------------------------------- 自检


def run_self_test() -> int:
    """不连串口/设备：用 PyAV libopus 造真实 Opus 帧，把解码+频谱+解析断言跑一遍。"""
    res = Result()
    print("== self-test：PyAV libopus 编码 → 独立解码 → 频谱/解析断言（不连串口） ==")
    window_ms = 2000
    try:
        packets = encode_tone_opus_packets(window_ms, TONE_FREQ_HZ)
    except Exception as exc:
        res.check("S1 PyAV libopus 编码器可用（16k/单声道/20ms）", False,
                  f"{type(exc).__name__}: {exc}")
        return res.summary()

    res.check("S1 PyAV libopus 编码器可用（16k/单声道/20ms，16kbps）",
              len(packets) > 0, f"{len(packets)} 包 / {window_ms} ms 输入")

    tocs = [opus_toc(p) for p in packets]
    bad_toc = [i for i, t in enumerate(tocs)
               if t["stereo"] != 0 or t["frame_ms"] is None or abs(t["frame_ms"] - FRAME_MS) > 1e-6]
    res.check("S2 编码帧 TOC：单声道 + 20ms", not bad_toc,
              f"bad={bad_toc[:5]}" if bad_toc else f"{len(tocs)} 帧全部 mono/20ms")

    # 用自造帧反推 console 输出，验证 ENCDUMP/start/done 解析链路
    fake_lines = ["ENC TEST: 已丢弃 8 帧积压（start 前采集的旧数据）",  # 固件新增行：应被忽略
                  "ENC TEST start: source=tone window_ms=2000 dump=on"]
    fake_lines += [f"ENCDUMP {i} {len(p)} {p.hex()}" for i, p in enumerate(packets)]
    fake_done = (f"ENC TEST done: source=tone frames={len(packets)} samples={len(packets) * FRAME_SAMPLES} "
                 f"bytes={sum(len(p) for p in packets)} bytes_per_frame={sum(len(p) for p in packets) / len(packets):.2f} "
                 f"bitrate_kbps=16.0 encode_avg_ms=1.50 encode_max_ms=3.00 max_packet={max(len(p) for p in packets)} "
                 f"errors=0 carry=0 elapsed_ms=2000 internal_free_delta=0 psram_free_delta=0 "
                 f"read_timeouts=0 stack_free=8192")  # 固件额外字段：解析器应忽略、不判失败
    fake_lines.append(fake_done)
    parsed = parse_enc_output(fake_lines)
    payloads = dump_payloads(parsed)
    seqs = [d["seq"] for d in parsed["dumps"]]
    res.check("S3 ENCDUMP/start/done 解析：帧数、seq、done 字段",
              len(payloads) == len(packets) and seqs == list(range(len(packets)))
              and parsed["done"].get("frames") == len(packets)
              and parsed["start"]["source"] == "tone",
              f"解析 {len(payloads)}/{len(packets)} 帧, done.frames={parsed['done'].get('frames')}")

    with tempfile.TemporaryDirectory(prefix="opus_enc_selftest_") as tmp:
        wav_path = os.path.join(tmp, "tone_1k.wav")
        info, wav = decode_and_read(payloads, wav_path)
        res.check("S4 独立解码无失败帧",
                  not info["errors"] and info["decoded_frames"] > 0 and info["corrupt"] == 0,
                  info["errors"][0] if info["errors"]
                  else f"解码 {info['decoded_frames']} 帧 / corrupt={info['corrupt']}")
        native_rates = sorted(r for r in info["native_rates"] if r)
        res.check("S5 解码帧率 ∈ {16000, 48000}（Opus 原生输出，设备实测 48000）",
                  bool(native_rates) and all(r in (16000, 48000) for r in native_rates),
                  f"native_rates={native_rates} 总样本={info['native_samples']} "
                  f"布局={sorted(info['native_layouts'])}")

        total_ms = info["native_ms"]
        expect_ms = len(packets) * FRAME_MS
        res.check("S6 总时长 ≈ 抓帧数×20ms（±5%，按原生采样率折算）",
                  abs(total_ms - expect_ms) <= 0.05 * expect_ms,
                  f"解码 {total_ms:.1f} ms vs {expect_ms} ms")

        res.check("S7 帧数 ≈ window_ms/20 ±2",
                  abs(len(packets) - window_ms // FRAME_MS) <= 2,
                  f"{len(packets)} 包 vs 期望 {window_ms // FRAME_MS}±2")

        spec = spectrum_stats(wav["samples"], rate=wav["rate"] or DECODE_RATE)
        res.check("S8 FFT 主峰 1000±20 Hz", abs(spec["peak_hz"] - TONE_FREQ_HZ) <= 20,
                  f"peak={spec['peak_hz']:.1f} Hz")
        res.check("S9 1kHz 频段能量占比 > 50%", spec["band_ratio"] > 0.5,
                  f"{spec['band_ratio'] * 100:.2f}%")

        # 负例：500Hz 正弦必须不被判成 1kHz（证明断言不是写死的）
        pkts500 = encode_tone_opus_packets(500, 500.0)
        info500, wav500 = decode_and_read(pkts500, os.path.join(tmp, "tone_500.wav"))
        spec500 = spectrum_stats(wav500["samples"], rate=wav500["rate"] or DECODE_RATE)
        res.check("S10 负例：500Hz 正弦不被误判为 1kHz",
                  not info500["errors"] and abs(spec500["peak_hz"] - 500) <= 20
                  and spec500["band_ratio"] < 0.5,
                  f"peak={spec500['peak_hz']:.1f} Hz / 1kHz 占比 {spec500['band_ratio'] * 100:.2f}%")

    # 兼容设备 dump 缓冲上限（当前固件 50 帧=1s）：dump + missed == done.frames 才判对账一致
    trunc = ["ENC TEST: 已丢弃 8 帧积压（start 前采集的旧数据）",
             "ENC TEST start: source=tone window_ms=2000 dump=on"]
    trunc += [f"ENCDUMP {i} {len(p)} {p.hex()}" for i, p in enumerate(packets[:50])]
    trunc.append("ENC dump missed: 51 帧（超出缓冲未记录）")
    trunc.append("ENC dump done: n=50")
    trunc.append(fake_done)
    tp = parse_enc_output(trunc)
    res.check("S11 兼容 dump 缓冲截断（dump 50 + missed 51 = frames 101）",
              len(tp["dumps"]) == 50 and tp["dump_missed"] == 51
              and tp["dump_done"] == 50 and tp["done"].get("frames") == len(packets),
              f"dump={len(tp['dumps'])} missed={tp['dump_missed']} "
              f"done.n={tp['dump_done']} frames={tp['done'].get('frames')}")

    # S12 量纲交叉校验：分析 PCM(16k/mono/s16) RMS 应 ≈ libopus 解码器原生 s16 RMS（±10%）。
    # 设备 A tone 实测：分析 PCM 5556 vs libopus s16 5555（说明没有 downmix 0.5 或再次除 32768）。
    with tempfile.TemporaryDirectory(prefix="opus_enc_scale_") as tmp2:
        libopus_wav = os.path.join(tmp2, "tone_libopus48k.wav")
        libopus_info = decode_opus_libopus_wav(payloads, libopus_wav)
        libopus_pcm = read_wav(libopus_wav)
    analysis_rms = rms_of(wav["samples"])
    libopus_rms = rms_of(libopus_pcm["samples"])
    ratio = analysis_rms / libopus_rms if libopus_rms else 0.0
    res.check("S12 解码量纲一致（分析 PCM s16 ≈ libopus 原生 s16，±10%）",
              not libopus_info["errors"] and libopus_info["frames"] > 0
              and 0.9 <= ratio <= 1.1,
              f"analysis={analysis_rms:.0f} / libopus={libopus_rms:.0f}"
              f"（比值 {ratio:.3f}，帧 {libopus_info['frames']}）")

    return res.summary()


# ---------------------------------------------------------------- 真机


def detect_port(explicit: str | None) -> str:
    if explicit:
        return explicit
    ports = sorted(glob.glob("/dev/cu.usbmodem*"))
    if len(ports) == 1:
        return ports[0]
    if not ports:
        raise SystemExit("未找到 /dev/cu.usbmodem*：确认开发板已插上，或用 --port 指定")
    raise SystemExit(f"找到多个串口，请用 --port 指定：{', '.join(ports)}")


def write_log(dev: Device, path: str) -> None:
    try:
        with open(path, "w", encoding="utf-8") as f:
            for ts, line in dev.lines:
                f.write(f"{ts:.3f} {line}\n")
        print(f"\n原始日志: {path}")
    except OSError as exc:
        print(f"\n写日志失败: {exc}")


def run(args: argparse.Namespace) -> int:
    if args.self_test:
        return run_self_test()

    window = args.window_ms
    if window < 200:
        raise SystemExit("--window-ms 至少 200（至少 10 个 20ms 帧）")
    wav_speech = os.path.join(HERE, SPEECH_WAV)
    if not os.path.exists(wav_speech):
        raise SystemExit(f"缺少素材: {wav_speech}")

    port = detect_port(args.port)
    print(f"T2.2 上行 Opus 编码自测 | 串口: {port} | 窗口: {window} ms")
    dev = Device(port)
    res = Result()
    dump_on = False
    afe_on = False
    log_written = False
    try:
        if not args.no_reset:
            dev.reset()
            dev.wait_prompt(args.boot_timeout)
            if dev.wait_line(APP_READY_RE, args.boot_timeout) is None:
                raise SystemExit("设备未就绪（未见 'APP: ready, say'）")
            dev.wait_prompt(2.0)
        print("设备就绪\n")

        # ---------- 0. enc console 可用 ----------
        print("== 0. enc console 可用性 ==")
        stat_out = dev.cmd("enc stat")
        res.check("0.1 enc stat 有输出（console 命令已实现）",
                  any("ENC stat:" in line for line in stat_out),
                  next((line for line in stat_out if "ENC stat:" in line), "未见到 'ENC stat:'"))
        reset_out = dev.cmd("enc reset")
        res.check("0.2 enc reset 生效",
                  any("ENC stats reset" in line for line in reset_out),
                  next((line for line in reset_out if "ENC stats reset" in line), "未见到 'ENC stats reset'"))

        # ---------- A. tone 源数值基准 ----------
        print("\n== A. tone 源数值基准（enc dump on → enc test tone → PyAV 独立解码） ==")
        on_out = dev.cmd("enc dump on")
        dump_on = True
        res.check("A1 enc dump on 生效",
                  any("ENC dump on" in line for line in on_out),
                  next((line for line in on_out if "ENC dump on" in line), "未见到 'ENC dump on'"))
        a_out = dev.cmd(f"enc test {window} tone", timeout=window / 1000 + 20)
        off_out = dev.cmd("enc dump off")
        dump_on = False
        a_parsed = parse_enc_output(a_out)
        a_payloads = dump_payloads(a_parsed)
        with _WavDir("opus_enc_A_") as tmp:
            a_info, a_wav = decode_and_read(a_payloads, os.path.join(tmp, "A_tone.wav"))
        a_stats = check_stream(res, "A", "tone", a_parsed, window, a_wav, a_info, start=2)
        spec = spectrum_stats(a_wav["samples"], rate=a_wav["rate"] or DECODE_RATE)
        a_idx = a_stats["next_idx"]
        res.check(f"A{a_idx} 解码后 FFT 主峰 1000±20 Hz",
                  abs(spec["peak_hz"] - TONE_FREQ_HZ) <= 20,
                  f"peak={spec['peak_hz']:.1f} Hz（帧数 {a_stats['n']}，"
                  f"avg {a_stats['avg_bpf']:.2f} B/frame）")
        res.check(f"A{a_idx + 1} 1kHz 频段能量占比 > 50%",
                  spec["band_ratio"] > 0.5,
                  f"{spec['band_ratio'] * 100:.2f}%")
        res.check(f"A{a_idx + 2} enc dump off 生效",
                  any("ENC dump off" in line for line in off_out),
                  next((line for line in off_out if "ENC dump off" in line), "未见到 'ENC dump off'"))

        # ---------- B. uplink 源真实链路 ----------
        print("\n== B. uplink 源真实链路（afe uplink on → afplay 回放 → enc test uplink） ==")
        b_mark = len(dev.lines)
        dev.cmd("afe uplink on", timeout=8)
        afe_on = True
        b_on = dev.cmd("enc dump on")
        dump_on = True
        res.check("B1 enc dump on 生效",
                  any("ENC dump on" in line for line in b_on),
                  next((line for line in b_on if "ENC dump on" in line), "未见到 'ENC dump on'"))

        # 静音基线：不放音，取同一链路的本底 RMS，作为"人声段明显高于静音"的参照
        base_out = dev.cmd("enc test 500 uplink", timeout=20)
        base_parsed = parse_enc_output(base_out)
        base_payloads = dump_payloads(base_parsed)
        with _WavDir("opus_enc_B_base_") as tmp:
            base_info, base_wav = decode_and_read(base_payloads, os.path.join(tmp, "B_base.wav"))
        quiet_rms = rms_of(base_wav["samples"])
        res.check("B2 静音基线可编码/解码（source=uplink、解码无失败）",
                  bool(base_parsed["start"]) and base_parsed["start"].get("source") == "uplink"
                  and not base_info["errors"] and len(base_wav["samples"]) > 0,
                  f"{len(base_payloads)} 帧, 本底 RMS={quiet_rms:.0f}")

        player = dev.play_async(wav_speech)
        try:
            b_out = dev.cmd(f"enc test {window} uplink", timeout=window / 1000 + 20)
        finally:
            while player.poll() is None:
                dev.pump(0.02)
        b_parsed = parse_enc_output(b_out)
        b_payloads = dump_payloads(b_parsed)
        with _WavDir("opus_enc_B_") as tmp:
            b_info, b_wav = decode_and_read(b_payloads, os.path.join(tmp, "B_speech.wav"))
        # 固件在 uplink 测试前会排空 ring 积压（打 `ENC TEST: 已丢弃 N 帧积压`，start 行前），
        # 实测 2000ms → 100 帧；±3 吸收调度边界（tone 固定帧数仍 ±2）。
        b_stats = check_stream(res, "B", "uplink", b_parsed, window, b_wav, b_info,
                               start=3, frame_tol=3)
        b_idx = b_stats["next_idx"]

        segs = segment_rms(b_wav["samples"], seg_ms=100)
        voice_band = band_energy_ratio(b_wav["samples"], rate=b_wav["rate"] or DECODE_RATE)
        overall_rms = rms_of(b_wav["samples"])
        # 三条 OR 自适应判据（不设绝对音量地板，见 speech_presence_ok）：真机音量低时绝对 RMS 只有几十，
        # 但帧长/频带仍是正常语音；B12 的 avg bytes/frame 已作为"有声"的独立证据。
        ok_speech, sp = speech_presence_ok(segs, quiet_rms, voice_band)
        res.check(f"B{b_idx} 有语音（最高段≥max(静音×3,30) 或 >中位×1.3 或 语音频带>0.6）",
                  ok_speech,
                  f"最高段 {sp['max_seg']:.0f}（地板 {sp['amp_floor']:.0f}）{'✓' if sp['by_amp'] else '✗'}；"
                  f">中位 {sp['median']:.0f}×1.3 {'✓' if sp['by_median'] else '✗'}；"
                  f"300~3400Hz 占比 {sp['band_ratio'] * 100:.1f}% {'✓' if sp['by_band'] else '✗'}"
                  f"（静音基线 {quiet_rms:.0f}，同一解码量纲）")
        res.check(f"B{b_idx + 1} 整段解码 RMS 高于静音基线×1.5",
                  overall_rms > quiet_rms * 1.5,
                  f"{overall_rms:.0f} vs {quiet_rms:.0f}")

        b_off = dev.cmd("enc dump off")
        dump_on = False
        res.check(f"B{b_idx + 2} enc dump off 生效",
                  any("ENC dump off" in line for line in b_off),
                  next((line for line in b_off if "ENC dump off" in line), "未见到 'ENC dump off'"))
        safe_cmd(dev, "afe uplink off", timeout=5)
        afe_on = False
        dev.pump(0.3)
        b_text = [text for _, text in dev.lines[b_mark:]]
        errs = error_lines(b_text)
        i2s_errs = i2s_failure_lines(b_text)
        res.check(f"B{b_idx + 3} 无 I2S 读失败 / ERROR / panic",
                  not errs and not i2s_errs,
                  (errs + i2s_errs)[0] if (errs or i2s_errs) else "日志干净（网络噪音已忽略）")

        # ---------- C. 内存/泄漏 ----------
        print("\n== C. 内存/泄漏（连续 3 次 enc test 1000 tone） ==")
        heap0 = parse_heap(dev.cmd("sys heap"))
        runs: list[dict] = []
        heaps: list[dict] = [heap0]
        for i in range(3):
            c_out = dev.cmd("enc test 1000 tone", timeout=25)
            c_parsed = parse_enc_output(c_out)
            runs.append(c_parsed["done"])
            print(f"  第 {i + 1} 次: internal_free_delta={runs[-1].get('internal_free_delta')} "
                  f"psram_free_delta={runs[-1].get('psram_free_delta')} "
                  f"frames={runs[-1].get('frames')}")
            heaps.append(parse_heap(dev.cmd("sys heap")))

        d_int = heap0.get("int_free", 0) - heaps[-1].get("int_free", 0)
        res.check("C1 内部 RAM 增量 ≤ 8KB", d_int <= 8192,
                  f"int_free {heap0.get('int_free')} → {heaps[-1].get('int_free')} (Δ={d_int})")
        int_drops = [heaps[i].get("int_free", 0) - heaps[i + 1].get("int_free", 0)
                     for i in range(len(heaps) - 1)]
        res.check("C2 内部 RAM 重复调用不持续下降（第 2/3 次 Δ ≤ 4KB）",
                  all(drop <= 4096 for drop in int_drops[1:]),
                  f"逐次 Δ={int_drops}")
        d_psram = heap0.get("psram_free", 0) - heaps[-1].get("psram_free", 0)
        psram_first = runs[0].get("psram_free_delta")
        res.check("C3 PSRAM 首次分配后持平（总 Δ ≤ 64KB）",
                  _is_num(psram_first) and abs(psram_first) <= 65536 and abs(d_psram) <= 65536,
                  f"首次 done.psram_free_delta={psram_first} / sys heap Δ={d_psram}")
        later = [runs[1].get("psram_free_delta"), runs[2].get("psram_free_delta")]
        res.check("C4 PSRAM 后续调用 Δ ≤ 4KB（无持续下降）",
                  all(_is_num(v) and abs(v) <= 4096 for v in later), f"后续 Δ={later}")
        res.check("C5 3 次均 errors=0/carry=0 且帧数正常",
                  all(r.get("errors") == 0 and r.get("carry") == 0
                      and _is_num(r.get("frames")) and abs(r.get("frames") - 50) <= 2 for r in runs),
                  "; ".join(f"#{i + 1} frames={r.get('frames')} errors={r.get('errors')} "
                            f"carry={r.get('carry')}" for i, r in enumerate(runs)))

        if KEEP_WAV_DIR:
            wavs = sorted(glob.glob(os.path.join(KEEP_WAV_DIR, "*.wav")))
            print(f"\n解码 WAV 已保留（{KEEP_WAV_DIR}），可直接 afplay 试听：")
            for w in wavs:
                print(f"  {w}")
        if args.log:
            write_log(dev, args.log)
            log_written = True
        return res.summary()
    finally:
        if dump_on:
            safe_cmd(dev, "enc dump off", timeout=4)
        if afe_on:
            safe_cmd(dev, "afe uplink off", timeout=4)
        if args.log and not log_written and dev.lines:
            write_log(dev, args.log)
        dev.close()


def main() -> int:
    ap = argparse.ArgumentParser(
        description="T2.2 codec_enc 上行 Opus 编码真机自测（串口抓帧 + PyAV 独立解码断言）",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    ap.add_argument("--port", help="串口设备（默认自动探测唯一的 /dev/cu.usbmodem*）")
    ap.add_argument("--window-ms", type=int, default=2000, help="enc test 编码窗口毫秒数")
    ap.add_argument("--no-reset", action="store_true", help="不复位设备（已在运行时用）")
    ap.add_argument("--boot-timeout", type=float, default=25.0, help="等待启动/就绪的秒数")
    ap.add_argument("--log", help="把串口原始日志写到该文件")
    ap.add_argument("--keep-wav", metavar="DIR",
                    help="把解码后的 WAV（A_tone / B_base / B_speech）保存到该目录，默认用完即删")
    ap.add_argument("--self-test", action="store_true",
                    help="离线自检：不需要串口/设备，用 PyAV libopus 造帧跑解码/频谱/解析断言")
    args = ap.parse_args()
    global KEEP_WAV_DIR
    KEEP_WAV_DIR = os.path.abspath(args.keep_wav) if args.keep_wav else None
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
