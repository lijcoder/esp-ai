#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["pyserial>=3.5", "lameenc>=1.8", "av>=12", "numpy"]
# ///
"""T2.6 端到端语音真机自测：唤醒 → 上行 → 真实 MP3 下行 → 流式播放 → R2，连续 10 轮。

把 T2.5（`voice_cloud` 端云协议）与 T2.4（`audio_player` 流式播放）拼成**真实用户路径**并
做稳定性验证 —— 与 `tts_stream_test.py` 的 `vc start/stop` 确定性触发不同，本脚本全程走
「`afplay lexin.wav` 唤醒 → 提示音 → 自动起轮 → `afplay 今天天气如何.wav` 模拟用户说话 →
VAD 静音收尾 → mock 下发真实 MP3 → 设备流式播放 → 收音恢复」这条链路，10 轮逐轮断言。

一条命令完成：
  1. PC 侧用 lameenc 把 `tools/今天天气如何.wav`（24k/16bit/mono）转 24k 单声道 MP3
     （拼接 3 份 ≈4.8s，与 `tts_stream_test.py` 同口径：202 帧 / 19392 B / 4848 ms）；
  2. 起本地 mock（`--tts-mp3 <file> --tts-pace-pct 80`，自动拉起/回收）；
  3. 复位设备 → 等 `WS: 握手完成（hello_ack）` → `afplay tools/lexin.wav` 唤醒
     （系统音量正常 31%、上限 50%，**默认 afplay 增益**，脚本不改音量）；
  4. 等 `(APP|VS): prompt done, feed resumed` → 自动轮次 `VC: round start`（= UPLINK）→
     `afplay tools/今天天气如何.wav` 模拟用户说话；
  5. 等 `audio_end（vad-silence）` → mock asr/llm/tts_start → 真实 MP3 0x02 流 →
     设备 `stream open`（R1 feed paused）→ 流式播放 → `tts_end` → `stream end`（排空）→
     `VC: stream done → feed resumed（R2）` → `round done`；
  6. 逐轮断言 + 10 轮汇总（内部堆/PSRAM/任务数/无 ERROR），最后一行机器可读 + 退出码 0/1。

断言（每条都附设备日志/统计证据，口径见 task-8 与 ROADMAP T2.6）：
  - 唤醒链路完整且**有序**：`wake detected` → `feed paused` → `PLAYER: play N samples` →
    `prompt done, feed resumed`；
  - 自动起轮：`round start sid` 与 `audio_end sid`、mock `audio_start sid` 三者一致；
    `audio_start` 由 `vc_start_auto()` 自动发起（无需 `vc start`）；
  - 上行：`audio_end 原因=vad-silence`、上行帧 ≥60、首帧 <200000 us、
    设备帧数 == mock 逐帧计数 == mock `audio_end` 计数、上行 seq 无跳变/无坏帧；
  - 下行：`tts_start codec=mp3 sr=24000`、`tts_end` 帧数 ==202、设备收到 19392 B、
    `fed==decoded==202`、`samples==116352`、`first_sound_us<150000`（日志 first sound <150ms
    双口径）、稳态 `underrun=0`、`bad=0`、`dropped=0`、`ring_ms=0`、`dur≈4848 ms`；
  - 半双工：`stream open ... feed paused (R1)` 出现 → 播放中 `mic stat 状态=收音 暂停`
    → `stream end` 后 `stream done → feed resumed（ok=1）` → 结束后 `mic stat 状态=收音 开启`；
  - 拾音（噪声鲁棒口径）：播放期 `mic stat` 稳态峰值 vs baseline=**min(播放前静音窗口,
    播放后静音窗口)** 峰值。baseline<800 时严格判定 `播放峰 ≥ max(1000, 1.5×baseline)`；
    baseline≥800 说明环境人声/噪声已盖过外放拾音 → 整轮按环境因素重试，重试耗尽记
    WARN/inconclusive（汇总单列计数，不判 FAIL、不静默）；
  - 内存：10 轮内部堆波动 ≤2KB 且不单调下降、PSRAM 首末差 ≈0、任务数不变；
  - 全程无 `E (...)`/panic（`round aborted` 的 `W` 是预期，不算错误）。

限制与环境（与 tts_stream_test 一致）：
  - 脚本**独占串口**（运行前关掉 `idf.py monitor`）；默认复位设备，会真实外放声音（正常现象）；
  - 跑测试时**宿主不要并发重负载**：mock 进程被抢占 >150ms 会造成 1 次真欠载（环境噪声，
    重跑即可，**不要改阈值掩盖**）；
  - 唤醒受环境底噪影响：每轮最多重试 `--wake-retries`（默认 2）次并记录底噪；断言只统计
    成功轮次，最终要求 ≥`--min-rounds`（默认 8）/10 轮成功且所有成功轮次全绿；
  - 需要 `CONFIG_DIAG_ENABLE_PLAYER_TEST` 提供的 `player stat` stream 行 / `mic stat`
    （开发期探针，量产必须关闭）。

用法：
    uv run tools/e2e_voice_test.py                  # 全量 10 轮（默认复位设备）
    uv run tools/e2e_voice_test.py --dry-run        # 只生成素材 + PyAV 参考，不碰设备/mock
    uv run tools/e2e_voice_test.py --rounds 1       # 冒烟 1 轮
    uv run tools/e2e_voice_test.py --no-reset       # 设备已在跑（不动复位）
    uv run tools/e2e_voice_test.py --log /tmp/e2e.log

与另两个脚本的分工（P2 验收三条都要跑）：
  - `voice_cloud_test.py`：测「协议/事件/sid 过滤/收尾」，mock 下发**合成帧**（不可解码），
    唤醒+回放触发但只做一轮；
  - `tts_stream_test.py`：测「音频侧硬指标」，用 `vc start/stop` **确定性触发**，不依赖唤醒；
  - 本脚本：**真实用户路径 + 10 轮稳定性**，同时覆盖上方两条链路的关键断言（端到端验收）。
"""

from __future__ import annotations

import argparse
import glob
import math
import os
import re
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))

BAUDRATE = 115200
WAKE_WAV = "lexin.wav"
SPEECH_WAV = "今天天气如何.wav"
DEFAULT_MOCK_PORT = 8765

# 用户定稿：系统输出正常 31%、上限 50%（不得突破），afplay 用默认增益
SYSTEM_VOLUME_NORMAL = 31
SYSTEM_VOLUME_MAX = 50

# TTS 素材目标（与 tts_stream_test.py / STATUS §1 口径一致）
TTS_RATE = 24000
TTS_SOURCE_REPEATS = 3  # 3×1.6s≈4.8s
TTS_KBPS = 32
TARGET_FRAMES = 202
TARGET_BYTES = 19392
TARGET_SAMPLES = 116352
TARGET_MATERIAL_MS = 4848
DEVICE_DUR_MS = 4850  # 设备实测 dur（素材 4848 + 收尾误差）

# 上行/播放期阈值（口径写进 tools/README.md，不要为跑绿而放宽）
FIRST_FRAME_US_MAX = 200000
FIRST_SOUND_US_MAX = 150000
MIN_UPLINK_FRAMES = 60
PLAYBACK_PEAK_MIN = 1000
# 拾音判据的噪声基线阈值：baseline=min(播放前,播放后静音窗口峰值)。
# baseline≥该值时环境噪声已盖过设备外放拾音，判据转 inconclusive（整轮重试，耗尽记 WARN）。
NOISE_BASELINE_PEAK = 800

# ---- 设备侧日志（逐字匹配现有固件输出，只消费、不猜字段） ----
RE_WS_READY = re.compile(r"WS: 握手完成（hello_ack）")
# T3.1 起唤醒编排归 voice_session（TAG VS）；兼容旧固件的 APP 前缀
RE_WAKE = re.compile(r"(?:APP|VS): wake detected")
RE_FEED_PAUSE = re.compile(r"AFE: feed paused")
RE_PROMPT_PLAY = re.compile(r"PLAYER: play \d+ samples")
RE_PROMPT_DONE = re.compile(r"(?:APP|VS): prompt done, feed resumed")
RE_CLOUD_SKIP = re.compile(r"cloud round skipped")
RE_ROUND_START = re.compile(r"VC: round start sid=(\d+)")
RE_FIRST_FRAME = re.compile(r"VC: 上行首帧 (\d+) us")
RE_AUDIO_END = re.compile(
    r"VC: audio_end sid=(\d+) 原因=([a-z-]+) 上行 (\d+) 帧/(\d+) B 用时 (\d+) ms"
)
RE_TTS_START = re.compile(r"VC: TTS start sid=(\d+) codec=(\w+) sr=(\d+)")
RE_TTS_END = re.compile(r"VC: TTS end sid=(\d+)（本轮下行音频 (\d+) 帧）")
RE_ROUND_DONE = re.compile(r"VC: round done sid=(\d+)")
RE_ROUND_ABORT = re.compile(r"VC: round aborted sid=(\d+)")
RE_RESPONSE_TIMEOUT = re.compile(r"VC: 等待云端响应超时")
RE_STREAM_OPEN_VC = re.compile(r"VC: stream open codec=mp3 sr=(\d+)，feed paused \(R1\)")
RE_STREAM_DONE_R2 = re.compile(r"VC: stream done → feed resumed（ok=(-?\d+)）")

# PLAYER 侧（T2.4-A 常驻输出）
RE_PLAYER_STREAM_OPEN = re.compile(r"PLAYER: stream open codec=mp3 sr=(\d+)")
RE_FIRST_SOUND = re.compile(r"PLAYER: stream first sound after (\d+) ms \(prebuffer (\d+) ms\)")
RE_STREAM_END = re.compile(
    r"PLAYER: stream end frames=(\d+) bytes=(\d+) decoded=(\d+) dur=(\d+) ms "
    r"underrun=(\d+) bad=(\d+)"
)
RE_STREAM_UNDERRUN = re.compile(r"PLAYER: stream underrun #(\d+) \(ring (\d+) ms\)")
RE_STREAM_LINE = re.compile(
    r"stream: active=(\d+) fed=(\d+) bytes=(\d+) decoded=(\d+) samples=(\d+) "
    r"rate=(\d+) ch=(\d+) ring_ms=(\d+) high_ms=(\d+) underrun=(\d+) dropped=(\d+) "
    r"bad=(\d+) first_sound_us=(\d+) dur_ms=(\d+)"
)
STREAM_KEYS = (
    "active", "fed", "bytes", "decoded", "samples", "rate", "ch", "ring_ms",
    "high_ms", "underrun", "dropped", "bad", "first_sound_us", "dur_ms",
)
RE_PLAYERSTAT = re.compile(
    r"PLAYERSTAT jobs=(\d+) pcm=(\d+) mp3=(\d+) errors=(\d+) busy=(\d+) vol=(\d+) "
    r"last_play_ms=(\d+) mp3_ready=(\d+)"
)
RE_VC_AUDIO_DROP = re.compile(r"音频丢弃 (\d+) 帧 字节 (\d+)")

# mock 侧日志
RE_MOCK_HELLO = re.compile(r"<-- hello device_id=(\S+) ver=(\d+)")
RE_MOCK_AUDIO_START = re.compile(r"<-- audio_start sid=(\d+)")
RE_MOCK_AUDIO_END = re.compile(r"<-- audio_end sid=(\d+)（本轮上行 (\d+) 帧）")
RE_MOCK_TTS_DONE = re.compile(r"tts 下发完成 frames=(\d+) bytes=(\d+) mode=(\w+)")
RE_MOCK_TTS_MODE = re.compile(
    r"TTS 模式: 真实 MP3 (.+?) \| 帧=(\d+) 字节=(\d+) 时长=(\d+)ms 采样率=(\d+)Hz 节奏=(\d+)%"
)
MOCK_UP_FRAME = "<-- 上行音频 seq="
MOCK_SEQ_GAP = "seq 跳变"
MOCK_BAD_FRAME = "坏帧"

# console 命令结束标记（esp_console 无统一结束符，用命令最后一行输出当哨兵）
END_VC_STATUS = re.compile(r"说明: 唤醒后由 voice_session 自动起一轮")
END_VC_RESET = re.compile(r"vc 统计已清零")
END_VS_FOLLOWUP = re.compile(r"连续对话续听窗口：(开|关)")
END_VC_CANCEL = re.compile(r"已请求立即放弃")
END_SYS_HEAP = re.compile(r"PSRAM.*(最低水位|未初始化)")
END_SYS_TASKS = re.compile(r"共 \d+ 个任务")
END_PLAYER_STAT = RE_STREAM_LINE
END_PLAYER_RESET = re.compile(r"PLAYERSTAT cleared")
END_MIC_STAT = re.compile(r"参考:")
END_MIC_RESET = re.compile(r"已清零|失败")

ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


# ================================================================ WAV / MP3 素材


def read_wav_info(path: str) -> dict:
    """读 WAV 头（素材是流式 RIFF 头，data 长度是占位值，按文件尾截断，与 afplay 同口径）。"""
    with open(path, "rb") as handle:
        head = handle.read(12)
        if head[:4] != b"RIFF" or head[8:12] != b"WAVE":
            raise ValueError(f"不是 RIFF/WAVE 文件: {path}")
        fmt = None
        data_off = 0
        data_bytes = 0
        while True:
            chunk = handle.read(8)
            if len(chunk) < 8:
                break
            cid, size = chunk[:4], struct.unpack("<I", chunk[4:])[0]
            if cid == b"fmt ":
                body = handle.read(size)
                audio_format, channels, rate, _, _, bits = struct.unpack("<HHIIHH", body[:16])
                fmt = {"format": audio_format, "channels": channels, "rate": rate, "bits": bits}
            elif cid == b"data":
                data_off = handle.tell()
                data_bytes = size
                if data_off + data_bytes > os.path.getsize(path):
                    data_bytes = os.path.getsize(path) - data_off
                break
            else:
                handle.seek(size, os.SEEK_CUR)
    if fmt is None:
        raise ValueError(f"缺 fmt 块: {path}")
    samples = data_bytes // (fmt["bits"] // 8) // max(fmt["channels"], 1)
    duration = samples / fmt["rate"] if fmt["rate"] else 0.0
    return {
        **fmt, "data_offset": data_off, "data_bytes": data_bytes, "samples": samples,
        "duration": duration, "file_bytes": os.path.getsize(path),
    }


def wav_levels(path: str) -> tuple[int, int]:
    """(峰值, RMS)，16 bit 线性值；只用于展示素材电平，不参与断言。"""
    info = read_wav_info(path)
    if info["bits"] != 16:
        return 0, 0
    with open(path, "rb") as handle:
        handle.seek(info["data_offset"])
        raw = handle.read(info["data_bytes"])
    count = len(raw) // 2
    if count == 0:
        return 0, 0
    samples = struct.unpack("<%dh" % count, raw[: count * 2])
    peak = max(max(samples), -min(samples))
    rms = int(math.sqrt(sum(x * x for x in samples) / count))
    return peak, rms


def dbfs(value: int) -> str:
    return "-inf" if value <= 0 else f"{20 * math.log10(value / 32768.0):.1f}"


def make_tts_material(wav_path: str, mp3_path: str, repeats: int, kbps: int) -> dict:
    """把 24k/16bit/mono WAV 拼接 repeats 份，用 lameenc 转 24k 单声道 MP3（素材现生成、不入库）。"""
    import lameenc

    info = read_wav_info(wav_path)
    if (info["format"], info["channels"], info["bits"], info["rate"]) != (1, 1, 16, TTS_RATE):
        raise SystemExit(f"素材格式不符合预期（需 PCM 24k/16bit/mono）：{wav_path} {info}")
    with open(wav_path, "rb") as handle:
        handle.seek(info["data_offset"])
        pcm = handle.read(info["data_bytes"])
    if not pcm:
        raise SystemExit(f"素材没有 PCM 数据：{wav_path}")
    pcm_all = pcm * max(1, repeats)

    encoder = lameenc.Encoder()
    encoder.set_bit_rate(kbps)
    encoder.set_in_sample_rate(TTS_RATE)
    encoder.set_out_sample_rate(TTS_RATE)  # 不设会被 lameenc 默认降到别的采样率
    encoder.set_channels(1)
    encoder.set_quality(5)
    data = encoder.encode(pcm_all) + encoder.flush()
    with open(mp3_path, "wb") as handle:
        handle.write(data)
    return {
        "seconds": len(pcm_all) / 2 / TTS_RATE,
        "pcm_bytes": len(pcm_all),
        "mp3_bytes": len(data),
        "kbps": kbps,
        "repeats": repeats,
    }


def reference_decode(path: str) -> dict:
    """PyAV（FFmpeg）独立解码参考：帧数/样本数/采样率/声道 + 单声道参考 PCM。"""
    import av
    import numpy as np

    container = av.open(path)
    stream = container.streams.audio[0]
    rate = stream.codec_context.sample_rate
    channels = stream.codec_context.channels
    frames = 0
    samples = 0
    chunks = []
    for frame in container.decode(audio=0):
        arr = frame.to_ndarray().astype(np.float64) * 32768.0  # fltp → int16 量纲
        if arr.ndim == 2:
            if arr.shape[0] == channels:  # 平面格式 (ch, n)
                arr = arr.mean(axis=0)
            else:  # 交织格式 (n, ch)
                arr = arr.reshape(-1, channels).mean(axis=1)
        chunks.append(arr)
        frames += 1
        samples += frame.samples
    mono = np.concatenate(chunks) if chunks else np.zeros(0)
    return {"rate": rate, "channels": channels, "frames": frames,
            "samples": samples, "mono": mono}


# ================================================================ 串口设备


class Device:
    """串口封装（独占串口）：读日志 + afplay 回放 + console 命令，逻辑与 voice_test.Device 一致。"""

    def __init__(self, port: str):
        import serial  # 延迟导入：--dry-run 不需要 pyserial

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
        """等待 mark 之后首次出现 pattern，返回该行到达时间（None=超时）。"""
        end = time.time() + timeout
        while True:
            for ts, line in self.lines[mark:]:
                if pattern.search(line):
                    return ts
            if time.time() >= end:
                return None
            self.pump(min(0.05, max(0.005, end - time.time())))

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
        out = self.console("mic stat", END_MIC_STAT)
        stat: dict = {}
        for line in out:
            m = re.search(r"峰值: (\d+)", line)
            if m:
                stat["peak"] = int(m.group(1))
            m = re.search(r"电平: (\d+)", line)
            if m:
                stat["rms"] = int(m.group(1))
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
        out = self.console("mic reset", END_MIC_RESET)
        return any("已清零" in line for line in out)

    def play(self, path: str) -> float:
        """afplay 回放（默认增益，不加 -v），返回墙钟时长（含 afplay 启动开销）。"""
        t0 = time.time()
        proc = subprocess.Popen(
            ["afplay", path], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE
        )
        while proc.poll() is None:
            self.pump(0.02)
        if proc.returncode != 0:
            err = proc.stderr.read().decode("utf-8", "replace").strip()
            raise RuntimeError(f"afplay 失败（{path}）：{err}")
        return time.time() - t0


# ================================================================ mock 进程


def port_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.3)
        return sock.connect_ex(("127.0.0.1", port)) == 0


class Mock:
    """本脚本拉起的 ws_mock_server 子进程（端口被占用时复用并跳过 mock 侧断言）。"""

    def __init__(self, port: int) -> None:
        self.port = port
        self.proc: subprocess.Popen | None = None
        self.owned = False
        self.log_path = os.path.join(tempfile.gettempdir(), f"e2e_mock_{port}.log")
        self._log = None

    def start(self, extra_args: list[str]) -> None:
        if port_open(self.port):
            self.owned = False
            print(f"  mock: 端口 {self.port} 已被占用，复用现有服务（跳过 mock 侧断言）")
            return
        self._log = open(self.log_path, "w", encoding="utf-8", buffering=1)
        cmd = ["uv", "run", MOCK_SCRIPT,
               "--host", "0.0.0.0", "--port", str(self.port), *extra_args]
        self.proc = subprocess.Popen(cmd, stdout=self._log, stderr=subprocess.STDOUT, cwd=HERE)
        self.owned = True
        deadline = time.time() + 20.0
        while time.time() < deadline:
            if port_open(self.port):
                print(f"  mock: 已启动（端口 {self.port}，真实 MP3 下发）")
                return
            if self.proc.poll() is not None:
                raise SystemExit(f"mock 启动失败，日志见 {self.log_path}")
            time.sleep(0.05)
        raise SystemExit(f"mock 端口 {self.port} 20s 内未就绪，日志见 {self.log_path}")

    def stop(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None
        if self._log is not None:
            self._log.close()
            self._log = None

    def mark(self) -> int:
        try:
            return os.path.getsize(self.log_path)
        except OSError:
            return 0

    def text_since(self, mark: int) -> str:
        try:
            with open(self.log_path, encoding="utf-8", errors="replace") as handle:
                handle.seek(mark)
                return handle.read()
        except OSError:
            return ""

    def text(self) -> str:
        return self.text_since(0)


MOCK_SCRIPT = os.path.join(HERE, "ws_mock_server.py")


def wait_mock_text(mock: Mock, needle: str, timeout: float = 10.0) -> str:
    """等 mock 日志出现某段文本（banner 与端口就绪之间有竞态）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        text = mock.text()
        if needle in text:
            return text
        time.sleep(0.05)
    return mock.text()


# ================================================================ 设备统计解析


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


def task_count(dev: Device) -> int | None:
    out = dev.console("sys tasks", END_SYS_TASKS, timeout=4.0)
    for line in out:
        m = re.search(r"共 (\d+) 个任务", line)
        if m:
            return int(m.group(1))
    return None


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
        m = RE_VC_AUDIO_DROP.search(line)
        if m:
            stat["audio_dropped_frames"] = int(m.group(1))
            stat["audio_dropped_bytes"] = int(m.group(2))
    return stat


def player_stat(dev: Device) -> dict:
    """`player stat` → dict；stream 行由 T2.4-A 常驻提供。"""
    out = dev.console("player stat", END_PLAYER_STAT, timeout=4.0)
    stat: dict = {"stream": None}
    for line in out:
        m = RE_PLAYERSTAT.search(line)
        if m:
            stat.update({
                "jobs": int(m.group(1)), "pcm": int(m.group(2)), "mp3": int(m.group(3)),
                "errors": int(m.group(4)), "busy": int(m.group(5)), "vol": int(m.group(6)),
                "last_play_ms": int(m.group(7)), "mp3_ready": int(m.group(8)),
            })
        m = RE_STREAM_LINE.search(line)
        if m:
            stat["stream"] = {key: int(value) for key, value in zip(STREAM_KEYS, m.groups())}
    return stat


def wait_stream_idle(dev: Device, timeout: float = 8.0) -> dict | None:
    """等 PLAYER 流收尾（active=0，含 100ms fade + 300ms 尾部），返回最终 stream 统计。"""
    deadline = time.time() + timeout
    stream = None
    while time.time() < deadline:
        stream = player_stat(dev).get("stream")
        if stream is None or stream["active"] == 0:
            return stream
        dev.pump(0.1)
    return stream


def reset_stats(dev: Device) -> None:
    dev.console("vc reset", END_VC_RESET, timeout=2.0)
    dev.console("player reset", END_PLAYER_RESET, timeout=2.0)
    # T3.7 默认开启连续对话；e2e 按"每轮唤醒+提示音"验收，先关闭续听窗口
    dev.console("vs followup off", END_VS_FOLLOWUP, timeout=2.0)


def wait_idle(dev: Device, timeout: float = 8.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if vc_status(dev).get("state") == "IDLE":
            return True
        dev.pump(0.2)  # 少发 console 命令，避免测 IDLE 本身干扰在途轮次
    return False


def ensure_idle(dev: Device, timeout: float = 8.0) -> bool:
    """轮次开始前确保 IDLE：在途轮次先等它自然收尾，超时才 cancel（会留下预期 WARN）。"""
    if vc_status(dev).get("state") == "IDLE":
        return True
    if wait_idle(dev, timeout):
        return True
    dev.console("vc cancel", END_VC_CANCEL, timeout=2.0)
    return wait_idle(dev, 5.0)


def lines_since(dev: Device, mark: int) -> list[str]:
    return [line for _, line in dev.lines_since(mark)]


def find_line(lines: list[str], pattern: re.Pattern) -> str | None:
    return next((line for line in lines if pattern.search(line)), None)


def first_index(lines: list[str], pattern: re.Pattern) -> int:
    return next((i for i, line in enumerate(lines) if pattern.search(line)), -1)


# ================================================================ 环境检查 / 断言


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
    """macOS 系统输出音量（osascript）；非 macOS 返回 None。"""
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
    infos = []
    for name in (WAKE_WAV, SPEECH_WAV):
        path = os.path.join(HERE, name)
        if not os.path.exists(path):
            raise SystemExit(f"缺少语音素材：{path}")
        info = read_wav_info(path)
        if info["format"] != 1 or info["channels"] != 1 or info["bits"] != 16:
            raise SystemExit(f"{name} 格式不符合预期（需要 PCM/16bit/mono）：{info}")
        peak, rms = wav_levels(path)
        infos.append({"name": name, "path": path, **info, "peak": peak, "rms": rms})
    return infos


def error_lines(lines: list[str]) -> list[str]:
    """筛出与本链路相关的错误：网络/WS 的 ERROR 与本次测试无关，忽略（与 voice_test 一致）。

    T3.6：`E (...) VS: 会话进入 ERROR（原因=…）` 是预期异常反馈语义日志（随后播错误
    提示音并 ERROR→IDLE），按白名单排除；panic/assert/Guru 仍会返回并 FAIL。
    """
    pat = re.compile(r"ERROR|panic|abort|Guru Meditation|stack overflow|StoreProhibited")
    tag_re = re.compile(r"[EW] \(\d+\) ([A-Za-z_0-9-]+):")
    ignored_tags = {
        "NET", "WS", "wifi", "websocket_client", "esp_netif", "esp-tls", "esp_wifi",
        "wpa_supplicant", "phy_init", "net80211", "esp_https_ota", "HTTP_CLIENT",
    }
    ignored_patterns = (
        re.compile(r"VS: 会话进入 ERROR（"),  # T3.6 异常反馈的预期语义日志
    )
    out = []
    for line in lines:
        if not pat.search(line):
            continue
        m = tag_re.search(line)
        if m and m.group(1) in ignored_tags:
            continue
        if any(ignored.search(line) for ignored in ignored_patterns):
            continue
        out.append(line)
    return out


def health_errors(lines: list[str]) -> list[str]:
    """只按 ERROR/panic 判健康（cancel/中止路径的 WARN「round aborted」是预期日志）。"""
    candidates = [
        line for line in lines
        if re.search(r"^\s*E \(\d+\)", line)
        or re.search(r"panic|Guru Meditation|stack overflow|StoreProhibited", line)
    ]
    return [line for line in candidates if error_lines([line])]


class Result:
    """断言收集；按轮分组，summary 打印失败项 + 逐轮结果。"""

    def __init__(self) -> None:
        self.rows: list[tuple[bool, str, int]] = []
        self.round = 0

    def check(self, ok: bool, label: str) -> bool:
        self.rows.append((bool(ok), label, self.round))
        print(f"  {'PASS' if ok else 'FAIL'}  {label}")
        return bool(ok)

    def warn(self, label: str) -> None:
        self.rows.append((True, f"WARN {label}", self.round))
        print(f"  WARN  {label}")

    def info(self, label: str) -> None:
        print(f"  INFO  {label}")

    def round_failures(self, index: int) -> list[str]:
        return [label for ok, label, rnd in self.rows if rnd == index and not ok]

    def summary(self) -> bool:
        failed = [(label, rnd) for ok, label, rnd in self.rows if not ok]
        passed = len(self.rows) - len(failed)
        print("\n===== 失败项 =====")
        if failed:
            for label, rnd in failed:
                where = f"轮 {rnd}: " if rnd else ""
                print(f"  [FAIL] {where}{label}")
        else:
            print("  （无）")
        print(f"\n合计 {passed} 通过 / {len(failed)} 失败（共 {len(self.rows)} 项断言）")
        return not failed


# ================================================================ 一轮


def measure_quiet(dev: Device, seconds: float = 0.8) -> dict:
    """静音段电平：mic reset → 安静 window → mic stat（本轮底噪基线）。"""
    dev.mic_reset()
    dev.pump(seconds)
    return dev.mic_stat()


def attempt_wake(dev: Device, wake_path: str, timeout: float) -> dict:
    """一次唤醒尝试：静音基线 → afplay lexin.wav → 等 prompt done；返回本次尝试的上下文。"""
    base = measure_quiet(dev)
    mark = dev.mark()
    dev.play(wake_path)
    ts = dev.wait_line(RE_PROMPT_DONE, timeout, mark)
    return {
        "ok": ts is not None,
        "mark": mark,
        "base": base,
        "lines": lines_since(dev, mark),
    }


def check_wake_chain(result: Result, lines: list[str], label: str) -> None:
    """唤醒链路：四段日志都在且顺序正确（ARCH §3.3 R1/R2）。"""
    chain = (
        ("wake detected", RE_WAKE),
        ("R1 feed paused", RE_FEED_PAUSE),
        ("提示音播放", RE_PROMPT_PLAY),
        ("prompt done", RE_PROMPT_DONE),
    )
    missing = [name for name, pat in chain if first_index(lines, pat) < 0]
    result.check(not missing, f"{label}: 唤醒链路四段齐全"
                 + ("" if not missing else f"（缺 {', '.join(missing)}）"))
    indices = [first_index(lines, pat) for _, pat in chain]
    result.check(all(a < b for a, b in zip(indices, indices[1:])),
                 f"{label}: 唤醒链路顺序正确（wake→pause→play→done，索引 {indices}）")


def check_round_stream_stats(
    result: Result, label: str, stream: dict, material: dict, ref: dict
) -> None:
    """T2.4 音频侧硬指标（与 tts_stream_test 同口径，端到端再验一次）。"""
    result.check(stream["active"] == 0, f"{label}: 流已收尾 active=0")
    result.check(stream["rate"] == TTS_RATE and stream["ch"] == 1,
                 f"{label}: rate=24000 / ch=1（实际 {stream['rate']}/{stream['ch']}）")
    result.check(stream["bytes"] == material["mp3_bytes"],
                 f"{label}: 设备收到 {stream['bytes']} B == 素材 {material['mp3_bytes']} B")
    result.check(stream["fed"] == TARGET_FRAMES,
                 f"{label}: fed {stream['fed']} 帧 == {TARGET_FRAMES}")
    result.check(stream["decoded"] == TARGET_FRAMES and stream["fed"] == stream["decoded"],
                 f"{label}: decoded==fed=={TARGET_FRAMES}（实际 {stream['decoded']}/{stream['fed']}）")
    result.check(stream["samples"] == TARGET_SAMPLES,
                 f"{label}: samples {stream['samples']} == {TARGET_SAMPLES}")
    result.check(stream["first_sound_us"] < FIRST_SOUND_US_MAX,
                 f"{label}: first_sound_us={stream['first_sound_us']} < {FIRST_SOUND_US_MAX}"
                 "（首字节→I2S 出声）")
    result.check(stream["underrun"] == 0, f"{label}: 稳态 underrun={stream['underrun']} == 0")
    result.check(stream["bad"] == 0, f"{label}: 坏帧 bad={stream['bad']} == 0")
    result.check(stream["dropped"] == 0, f"{label}: ring 未溢出 dropped={stream['dropped']} == 0")
    result.check(stream["ring_ms"] == 0, f"{label}: tts_end 已排空 ring_ms={stream['ring_ms']}")
    dur_ok = abs(stream["dur_ms"] - TARGET_MATERIAL_MS) <= max(100, TARGET_MATERIAL_MS // 10)
    result.check(dur_ok, f"{label}: dur={stream['dur_ms']} ms ≈ 素材 {TARGET_MATERIAL_MS} ms（±10%）")
    result.check(ref["frames"] == TARGET_FRAMES and ref["samples"] == TARGET_SAMPLES,
                 f"{label}: PyAV 参考 frames={ref['frames']} samples={ref['samples']}"
                 f"（期望 {TARGET_FRAMES}/{TARGET_SAMPLES}）")


def evaluate_pickup(result: Result, label: str, play_peak: int, pre_base: int,
                    post_base: int) -> dict:
    """拾音判据（噪声鲁棒）：baseline=min(播放前, 播放后) 静音窗口峰值。

    - baseline < NOISE_BASELINE_PEAK：严格判定 播放峰 ≥ max(1000, 1.5×baseline)（硬断言）；
    - baseline ≥ NOISE_BASELINE_PEAK：环境噪声已盖过外放拾音 → 返回 verdict=noisy，
      由调用方按"整轮重试，耗尽记 WARN/inconclusive（不判 FAIL）"处理；这里只打 WARN 留证。
    返回 {"baseline", "verdict", "threshold", "ratio"}（verdict=strict|noisy）。
    """
    baseline = min(pre_base, post_base)
    ratio = play_peak / baseline if baseline else float("inf")
    if baseline >= NOISE_BASELINE_PEAK:
        result.warn(f"{label}: 拾音判据 inconclusive——环境基线噪声高（baseline 峰值 {baseline} ≥ "
                    f"{NOISE_BASELINE_PEAK}；播放前 {pre_base}/播放后 {post_base}），"
                    f"播放峰 {play_peak}（{ratio:.2f}×）；按环境性处理，不判 FAIL")
        return {"baseline": baseline, "verdict": "noisy", "threshold": 0, "ratio": ratio}
    threshold = max(PLAYBACK_PEAK_MIN, int(baseline * 1.5))
    result.check(play_peak >= threshold,
                 f"{label}: 播放期 mic 峰值 {play_peak} ≥ {threshold}"
                 f"（baseline=min(播放前 {pre_base}, 播放后 {post_base})={baseline} ×1.5"
                 f" 且 ≥{PLAYBACK_PEAK_MIN}）")
    return {"baseline": baseline, "verdict": "strict", "threshold": threshold, "ratio": ratio}


def run_round_attempt(
    dev: Device,
    mock: Mock,
    result: Result,
    args: argparse.Namespace,
    material: dict,
    ref: dict,
    index: int,
    paths: dict,
) -> dict:
    """一次完整尝试：唤醒（单次）→ 自动起轮 → 用户说话 → 上行 → 下行 mp3 → 播放 → R2 + 断言。

    `result` 是本次尝试的**私有** Result：环境因素（唤醒漏触发 / VAD 8s 未静音收尾）
    导致整轮重试时，本次的断言行整体丢弃，不污染总报告；硬失败才并入。
    """
    label = f"第 {index} 轮"
    data: dict = {
        "index": index, "label": label, "ok": False, "env_fail": False,
        "env_retryable": False, "env_reason": "", "failures": [], "attempts": [],
    }
    reset_stats(dev)
    ensure_idle(dev, args.idle_timeout)

    # ---- 唤醒（单次；失败由外层按"每轮最多重试 N 次"重试整轮） ----
    attempt_mock_mark = mock.mark()  # 必须早于本次唤醒（audio_start 紧跟 prompt done 发出）
    att = attempt_wake(dev, paths["wake"], args.wake_timeout)
    data["base_rms"] = att["base"].get("rms")
    data["base_peak"] = att["base"].get("peak")
    if not att["ok"]:
        if find_line(att["lines"], RE_WAKE):
            # 唤醒已触发但提示音链路未在超时内完成：按硬失败处理（不是环境底噪问题）
            result.check(False, f"{label}: 唤醒触发后提示音链路完整（prompt done 未出现）")
            data["failures"] = result.round_failures(index)
            return data
        data["env_retryable"] = True
        data["env_reason"] = (f"唤醒未触发（底噪 RMS {att['base'].get('rms')} / "
                              f"峰值 {att['base'].get('peak')}）")
        print(f"  WARN  唤醒未触发（底噪 RMS {att['base'].get('rms')} / "
              f"峰值 {att['base'].get('peak')} / VAD {att['base'].get('vad')}）")
        return data

    mark = att["mark"]
    mock_mark = attempt_mock_mark
    check_wake_chain(result, lines_since(dev, mark), label)

    # ---- 自动起轮（vc_start_auto，不是脚本发的 vc start） → UPLINK ----
    dev.wait_line(RE_ROUND_START, 5.0, mark)
    lines = lines_since(dev, mark)
    round_start_line = find_line(lines, RE_ROUND_START)
    result.check(round_start_line is not None, f"{label}: 提示音后自动起轮（round start）")
    if round_start_line is None:
        if find_line(lines, RE_CLOUD_SKIP):
            result.check(False, f"{label}: 自动起轮成功（日志出现 cloud round skipped：WS 未 READY？）")
        data["failures"] = result.round_failures(index)
        ensure_idle(dev, args.idle_timeout)
        return data
    sid = int(RE_ROUND_START.search(round_start_line).group(1))
    data["sid"] = sid

    # ---- 用户说话：回放交互语音（模拟人声提问），顺带量拾音电平 ----
    dev.mic_reset()
    dev.play(paths["speech"])
    speech = dev.mic_stat()
    data["speech_peak"] = speech.get("peak")
    data["speech_rms"] = speech.get("rms")
    data["speech_vad"] = speech.get("vad")
    result.info(f"用户语音拾音: 峰值 {speech.get('peak')} / RMS {speech.get('rms')} / "
                f"VAD {speech.get('vad')} / I2S 读失败 {speech.get('mic_read_errors')}")
    if speech.get("vad") != "人声":
        result.warn(f"{label}: 读数时 VAD 非人声（环境相关，非硬失败；上行仍可能正常）")
    if (speech.get("rms") or 0) < 150:
        result.warn(f"{label}: 交互语音拾音偏低（RMS {speech.get('rms')} < 150）："
                    "先确认系统未静音/回放路由，再查 `mic stat` 电平")

    # ---- audio_end：VAD 静音收尾（环境人声持续时会走 8s 上限 = max-duration） ----
    dev.wait_line(RE_AUDIO_END, args.round_timeout, mark)
    audio_end_line = find_line(lines_since(dev, mark), RE_AUDIO_END)
    result.check(audio_end_line is not None, f"{label}: {args.round_timeout}s 内收到 audio_end")
    if audio_end_line is None:
        data["failures"] = result.round_failures(index)
        dev.console("vc cancel", END_VC_CANCEL, timeout=2.0)
        ensure_idle(dev, args.idle_timeout)
        return data
    m = RE_AUDIO_END.search(audio_end_line)
    end_sid, reason = int(m.group(1)), m.group(2)
    up_frames, up_bytes, round_ms = int(m.group(3)), int(m.group(4)), int(m.group(5))
    data.update({"up_frames": up_frames, "up_bytes": up_bytes, "round_ms": round_ms,
                 "reason": reason})
    reason_ok = reason == "vad-silence" or (args.allow_max_duration and reason == "max-duration")
    if not reason_ok:
        # 8s 上限收尾：环境人声/噪声让 VAD 一直不进入 800ms 静音（voice_cloud_test 同口径）
        data["env_retryable"] = True
        data["env_reason"] = f"audio_end 原因={reason}（VAD 未在 {args.max_utter_ms}ms 内静音收尾）"
        print(f"  WARN  audio_end 原因={reason}（环境人声/噪声持续，VAD 未静音收尾）")
    result.check(reason_ok, f"{label}: audio_end 原因={reason}"
                 + ("" if reason_ok else f"（期望 vad-silence；{args.max_utter_ms}ms 上限收尾）"))
    result.check(end_sid == sid, f"{label}: audio_end sid={end_sid} == round start sid={sid}")
    result.check(up_frames >= MIN_UPLINK_FRAMES,
                 f"{label}: 上行 {up_frames} 帧 ≥ {MIN_UPLINK_FRAMES}")

    # ---- 上行首帧 ----
    first_line = find_line(lines_since(dev, mark), RE_FIRST_FRAME)
    first_us = int(RE_FIRST_FRAME.search(first_line).group(1)) if first_line else 10**9
    data["first_frame_us"] = first_us
    result.check(first_us < FIRST_FRAME_US_MAX,
                 f"{label}: 上行首帧 {first_us} us < {FIRST_FRAME_US_MAX} us")

    # ---- 下行：tts_start / R1 / 播放中拾音 / tts_end ----
    dev.wait_line(RE_TTS_START, args.tts_timeout, mark)
    tts_start_line = find_line(lines_since(dev, mark), RE_TTS_START)
    result.check(tts_start_line is not None, f"{label}: 收到 tts_start")
    m = RE_TTS_START.search(tts_start_line) if tts_start_line else None
    result.check(m is not None and int(m.group(1)) == sid and m.group(2) == "mp3"
                 and int(m.group(3)) == TTS_RATE,
                 f"{label}: tts_start sid 一致且 codec=mp3 sr=24000"
                 + (f"（实际 {m.group(0)}）" if m else "（未收到）"))

    # 开流暂停收音（R1）+ 首声日志
    dev.wait_line(RE_STREAM_OPEN_VC, 8.0, mark)
    stream_open_line = find_line(lines_since(dev, mark), RE_STREAM_OPEN_VC)
    result.check(stream_open_line is not None,
                 f"{label}: tts_start 后开流并暂停收音（R1：stream open ... feed paused）")
    if stream_open_line is not None:
        m_open = RE_STREAM_OPEN_VC.search(stream_open_line)
        result.check(int(m_open.group(1)) == TTS_RATE,
                     f"{label}: R1 流采样率 sr={m_open.group(1)} == 24000")
    dev.wait_line(RE_FIRST_SOUND, 8.0, mark)
    first_sound_line = find_line(lines_since(dev, mark), RE_FIRST_SOUND)
    result.check(first_sound_line is not None, f"{label}: 日志出现 PLAYER: stream first sound")
    if first_sound_line is not None:
        m_fs = RE_FIRST_SOUND.search(first_sound_line)
        result.check(int(m_fs.group(1)) < FIRST_SOUND_US_MAX // 1000,
                     f"{label}: 日志 first sound {m_fs.group(1)} ms < "
                     f"{FIRST_SOUND_US_MAX // 1000} ms（prebuffer {m_fs.group(2)} ms）")

    # 播放期：mic stat 必须显示「收音 暂停」；峰值先记录，等播放后再取第二个静音窗口一起判定
    # （噪声鲁棒口径：baseline=min(播放前,播放后)，见 tools/README.md「拾音判据」）。
    # 注意：起播头 ~1s 内拾音偏弱（实测峰值仅数百，稳态 1800~2100），先 warmup 再取窗口，
    # 否则会把「起播瞬间」当成整个播放期，产生与实现无关的假失败。
    dev.pump(args.playback_warmup)
    dev.mic_reset()
    dev.pump(args.playback_window)
    during = dev.mic_stat()
    result.check(during.get("feed") == "暂停",
                 f"{label}: 播放中收音暂停（stream open 的 R1 生效，实际 {during.get('feed')}）")
    data["playback_peak"] = during.get("peak") or 0
    data["playback_rms"] = during.get("rms")
    data["playback_feed"] = during.get("feed")

    dev.wait_line(RE_TTS_END, args.tts_timeout, mark)
    tts_end_line = find_line(lines_since(dev, mark), RE_TTS_END)
    result.check(tts_end_line is not None, f"{label}: 收到 tts_end")
    tts_frames = -1
    if tts_end_line is not None:
        m_te = RE_TTS_END.search(tts_end_line)
        tts_frames = int(m_te.group(2))
        result.check(int(m_te.group(1)) == sid, f"{label}: tts_end sid=={sid}")
    data["tts_frames"] = tts_frames
    result.check(tts_frames == TARGET_FRAMES,
                 f"{label}: tts_end 计数 {tts_frames} == {TARGET_FRAMES} 帧")

    # ---- 排空：stream end / R2 / round done（顺序与出现都断言） ----
    deadline = time.time() + args.drain_timeout
    while time.time() < deadline:
        have = lines_since(dev, mark)
        if (find_line(have, RE_STREAM_END) and find_line(have, RE_STREAM_DONE_R2)
                and find_line(have, RE_ROUND_DONE)):
            break
        dev.pump(0.1)
    lines = lines_since(dev, mark)
    stream_end_line = find_line(lines, RE_STREAM_END)
    r2_line = find_line(lines, RE_STREAM_DONE_R2)
    round_done_line = find_line(lines, RE_ROUND_DONE)
    result.check(stream_end_line is not None, f"{label}: 收到 PLAYER: stream end（排空完成）")
    if stream_end_line is not None:
        m_se = RE_STREAM_END.search(stream_end_line)
        result.check(int(m_se.group(1)) == TARGET_FRAMES,
                     f"{label}: stream end frames={m_se.group(1)} == {TARGET_FRAMES}")
        result.check(int(m_se.group(3)) == TARGET_FRAMES,
                     f"{label}: stream end decoded={m_se.group(3)} == {TARGET_FRAMES}")
        result.check(int(m_se.group(5)) == 0, f"{label}: stream end underrun=0")
        result.check(int(m_se.group(6)) == 0,
                     f"{label}: stream end bad={m_se.group(6)} == 0")
    result.check(r2_line is not None, f"{label}: 收流后恢复收音（R2：stream done → feed resumed）")
    if r2_line is not None:
        m_r2 = RE_STREAM_DONE_R2.search(r2_line)
        result.check(int(m_r2.group(1)) == 1, f"{label}: R2 回调 ok=1（实际 {m_r2.group(1)}）")
    result.check(round_done_line is not None, f"{label}: 本轮正常结束（round done）")
    if round_done_line is not None:
        m_rd = RE_ROUND_DONE.search(round_done_line)
        result.check(int(m_rd.group(1)) == sid, f"{label}: round done sid=={sid}")

    # 顺序：tts_end → stream end；stream end → R2
    idx_tts_end = first_index(lines, RE_TTS_END)
    idx_stream_end = first_index(lines, RE_STREAM_END)
    idx_r2 = first_index(lines, RE_STREAM_DONE_R2)
    result.check(0 <= idx_tts_end < idx_stream_end,
                 f"{label}: 排空顺序 tts_end(#{idx_tts_end}) → stream end(#{idx_stream_end})")
    result.check(0 <= idx_stream_end < idx_r2,
                 f"{label}: R2 顺序 stream end(#{idx_stream_end}) → feed resumed(#{idx_r2})")
    result.check(find_line(lines, RE_STREAM_UNDERRUN) is None,
                 f"{label}: 本轮无欠载日志（stream underrun 未出现）")
    result.check(find_line(lines, RE_ROUND_ABORT) is None, f"{label}: 本轮无 round aborted")
    result.check(find_line(lines, RE_RESPONSE_TIMEOUT) is None,
                 f"{label}: 无「等待云端响应超时」")

    # ---- 流式统计（player stat） ----
    stream = wait_stream_idle(dev, args.drain_timeout)
    if stream is None:
        result.check(False, f"{label}: player stat 有 stream 行")
    else:
        data["stream"] = stream
        check_round_stream_stats(result, label, stream, material, ref)

    # ---- vc 统计自洽 ----
    st = vc_status(dev)
    data["vc"] = {key: st.get(key) for key in (
        "state", "rounds_started", "rounds_done", "rounds_aborted", "up_frames",
        "rx_audio_frames", "dropped_sid", "parse_errors", "cloud_errors",
        "audio_dropped_frames", "first_frame_us")}
    result.check(st.get("state") == "IDLE", f"{label}: 轮次结束后回到 IDLE（实际 {st.get('state')}）")
    result.check(st.get("rounds_started") == 1 and st.get("rounds_done") == 1
                 and st.get("rounds_aborted") == 0,
                 f"{label}: 轮次统计 发起 {st.get('rounds_started')} / 完成 {st.get('rounds_done')}"
                 f" / 中止 {st.get('rounds_aborted')}")
    result.check(st.get("up_frames") == up_frames,
                 f"{label}: vc 上行帧 {st.get('up_frames')} == audio_end 计数 {up_frames}")
    result.check(st.get("rx_audio_frames") == TARGET_FRAMES,
                 f"{label}: vc 下行音频帧 {st.get('rx_audio_frames')} == {TARGET_FRAMES}")
    result.check(st.get("audio_dropped_frames") == 0,
                 f"{label}: 窗口内无丢弃（音频丢弃 {st.get('audio_dropped_frames')} 帧）")
    result.check(st.get("dropped_sid") == 0, f"{label}: 无 sid 丢弃")
    result.check(st.get("parse_errors") == 0, f"{label}: 下行 JSON 解析无失败")
    result.check(st.get("cloud_errors") == 0, f"{label}: 无云端 error 事件")

    # 收尾后 mic stat：收音恢复（R2 可观测口径）
    dev.pump(0.2)
    after = dev.mic_stat()
    data["after_feed"] = after.get("feed")
    result.check(after.get("feed") == "开启",
                 f"{label}: 轮次结束后收音恢复（mic stat 状态=收音 {after.get('feed')}）")

    # ---- 拾音判定（噪声鲁棒）：baseline = min(播放前静音峰, 播放后静音峰) ----
    # 设备已回 IDLE、无外放，再取一个纯静音窗口；与播放前窗口取较小峰值，
    # 避免「某一窗口刚好有人说话」把 baseline 抬到设备外放拾音之上。
    dev.mic_reset()
    dev.pump(args.post_baseline_window)
    post_quiet = dev.mic_stat()
    pre_base = data.get("base_peak") or 0
    post_base = post_quiet.get("peak") or 0
    baseline = min(pre_base, post_base)
    play_peak = data.get("playback_peak") or 0
    data.update({
        "pre_baseline_peak": pre_base,
        "post_baseline_peak": post_base,
        "baseline_peak": baseline,
    })
    result.info(f"拾音判据: 播放峰 {play_peak} / 播放前基线 {pre_base} / 播放后基线 {post_base}"
                f" → baseline {baseline}")
    verdict = evaluate_pickup(result, label, play_peak, pre_base, post_base)
    data["pickup_verdict"] = verdict["verdict"]
    data["pickup_noisy"] = verdict["verdict"] == "noisy"

    # ---- mock 侧（只有自己拉起 mock 时才能读日志） ----
    if mock.owned:
        dev.pump(0.1)
        text = mock.text_since(mock_mark)
        m_start = RE_MOCK_AUDIO_START.search(text)
        m_end = RE_MOCK_AUDIO_END.search(text)
        result.check(m_start is not None and m_end is not None,
                     f"{label}: mock 收到 audio_start / audio_end")
        if m_start is not None and m_end is not None:
            result.check(int(m_start.group(1)) == sid == int(m_end.group(1)),
                         f"{label}: mock 侧 sid 一致（{m_start.group(1)}/{sid}/{m_end.group(1)}）")
            result.check(int(m_end.group(2)) == up_frames,
                         f"{label}: mock 统计上行 {m_end.group(2)} 帧 == 设备 {up_frames} 帧")
        frame_count = text.count(MOCK_UP_FRAME)
        result.check(frame_count == up_frames,
                     f"{label}: mock 逐帧收到 {frame_count} 帧 == 设备 {up_frames} 帧")
        result.check(MOCK_SEQ_GAP not in text, f"{label}: 上行 seq 无跳变")
        result.check(MOCK_BAD_FRAME not in text, f"{label}: mock 未发现坏帧")
        m_tts = RE_MOCK_TTS_DONE.search(text)
        result.check(m_tts is not None, f"{label}: mock 完成一轮真实 MP3 下发")
        if m_tts is not None:
            result.check(int(m_tts.group(1)) == TARGET_FRAMES and int(m_tts.group(2)) == TARGET_BYTES
                         and m_tts.group(3) == "mp3",
                         f"{label}: mock 下发 frames={m_tts.group(1)} bytes={m_tts.group(2)}"
                         f" mode={m_tts.group(3)}（期望 {TARGET_FRAMES}/{TARGET_BYTES}/mp3）")

    # ---- 健康 + 内存采样 ----
    result.check(not health_errors(lines_since(dev, mark)),
                 f"{label}: 本轮无 ERROR/panic")
    heap = parse_heap(dev)
    internal = (heap.get("internal") or {})
    psram = (heap.get("psram") or {})
    tasks = task_count(dev)
    data.update({
        "tasks": tasks,
        "internal_free": internal.get("free"),
        "internal_min_free": internal.get("min_free"),
        "internal_peak": internal.get("peak"),
        "psram_free": psram.get("free"),
        "psram_min_free": psram.get("min_free"),
        "psram_peak": psram.get("peak"),
    })
    print(f"  内部堆剩余 {internal.get('free')} B（最低水位 {internal.get('min_free')} B）/ "
          f"PSRAM 剩余 {psram.get('free')} B / 任务 {tasks} 个")

    # 本轮结束清零统计，下一轮从 0 开始
    reset_stats(dev)
    data["failures"] = result.round_failures(index)
    data["ok"] = not data["failures"] and not data["env_retryable"]
    return data


def run_round(
    dev: Device,
    mock: Mock,
    result: Result,
    args: argparse.Namespace,
    material: dict,
    ref: dict,
    index: int,
    paths: dict,
) -> dict:
    """第 index 轮（1 基）：环境因素（唤醒漏触发 / VAD 8s 未静音 / 拾音基线噪声高）
    最多重试 `--max-retries` 次；拾音重试耗尽记 WARN/inconclusive，不判 FAIL。"""
    label = f"第 {index} 轮"
    result.round = index
    print(f"\n===== {label} =====")
    total = args.max_retries + 1
    records: list[dict] = []
    for attempt in range(1, total + 1):
        if attempt > 1:
            print(f"  ---- {label} 第 {attempt}/{total} 次尝试 ----")
        local = Result()
        local.round = index
        data = run_round_attempt(dev, mock, local, args, material, ref, index, paths)
        records.append({
            "attempt": attempt,
            "ok": data.get("ok"),
            "env_retryable": data.get("env_retryable"),
            "env_reason": data.get("env_reason"),
            "base_rms": data.get("base_rms"),
            "base_peak": data.get("base_peak"),
            "baseline_peak": data.get("baseline_peak"),
            "playback_peak": data.get("playback_peak"),
            "pickup_verdict": data.get("pickup_verdict"),
            "reason": data.get("reason"),
            "up_frames": data.get("up_frames"),
            "failures": data.get("failures"),
        })
        if data["ok"]:
            if data.get("pickup_noisy") and attempt < total:
                result.warn(f"{label}: 第 {attempt} 次尝试拾音判据遇环境噪声（baseline 峰值 "
                            f"{data.get('baseline_peak')} ≥ {NOISE_BASELINE_PEAK}），"
                            f"整轮重试（每轮最多重试 {args.max_retries} 次）")
                ensure_idle(dev, args.idle_timeout)
                dev.pump(args.pause_between)
                continue
            # 成功轮次：硬断言全绿；拾音项若重试用尽仍噪声，记 WARN/inconclusive（不判 FAIL）
            result.rows.extend(local.rows)
            data["attempts"] = records
            data["attempt_count"] = attempt
            data["env_fail"] = False
            data["pickup_warn"] = bool(data.get("pickup_noisy"))
            return data
        if data.get("env_retryable") and attempt < total:
            env = data.get("env_reason", "")
            result.warn(f"{label}: 第 {attempt} 次尝试受环境噪声影响（{env}），"
                        f"整轮重试（每轮最多重试 {args.max_retries} 次）")
            ensure_idle(dev, args.idle_timeout)
            dev.pump(args.pause_between)
            continue
        if data.get("env_retryable"):
            # 环境重试耗尽：本次断言行整体丢弃，记为环境失败（不计入断言）
            data["env_fail"] = True
            data["attempts"] = records
            data["attempt_count"] = attempt
            reason = data.get("env_reason", "")
            result.warn(f"{label}: {total} 次尝试均为环境因素（{reason}），"
                        "本轮记为环境失败、不计入断言（安静环境后复跑）")
            return data
        # 硬失败（真链路/统计不符）：断言行并入总报告，计 FAIL
        result.rows.extend(local.rows)
        data["attempts"] = records
        data["attempt_count"] = attempt
        data["env_fail"] = False
        return data
    # 理论到不了这里
    return data


# ================================================================ 主流程


def prepare_material(args: argparse.Namespace, result: Result) -> tuple[dict, dict, str]:
    """生成/校验真实 TTS 素材，PyAV 独立解出参考值。"""
    wav_path = os.path.join(HERE, SPEECH_WAV)
    if not os.path.exists(wav_path):
        raise SystemExit(f"缺少测试素材：{wav_path}")
    workdir = tempfile.mkdtemp(prefix="e2e_voice_test_")
    mp3_path = os.path.join(workdir, "tts_今天天气如何_24k_4.85s.mp3")
    material = make_tts_material(wav_path, mp3_path, args.material_repeats, args.kbps)
    ref = reference_decode(mp3_path)
    ref["duration_ms"] = int(ref["samples"] * 1000 / max(ref["rate"], 1))

    result.check(material["mp3_bytes"] == TARGET_BYTES,
                 f"素材 MP3 {material['mp3_bytes']} B == {TARGET_BYTES} B")
    result.check(ref["frames"] == TARGET_FRAMES,
                 f"PyAV 参考 frames={ref['frames']} == {TARGET_FRAMES}")
    result.check(ref["samples"] == TARGET_SAMPLES,
                 f"PyAV 参考 samples={ref['samples']} == {TARGET_SAMPLES}")
    result.check(ref["duration_ms"] == TARGET_MATERIAL_MS,
                 f"PyAV 参考时长 {ref['duration_ms']} ms == {TARGET_MATERIAL_MS} ms")
    result.check(ref["rate"] == TTS_RATE and ref["channels"] == 1,
                 f"PyAV 参考参数 rate={ref['rate']} ch={ref['channels']}（期望 24000/1）")
    return material, ref, mp3_path


def check_environment(result: Result, args: argparse.Namespace) -> dict:
    """素材 / 系统音量（用户定稿 31%、上限 50%）/ 串口 预检。"""
    paths = {"wake": os.path.join(HERE, WAKE_WAV), "speech": os.path.join(HERE, SPEECH_WAV)}
    print("\n===== 素材 =====")
    for info in preflight_assets():
        print(f"  {info['name']}: {info['rate']} Hz / {info['bits']} bit / {info['channels']} ch"
              f" / {info['duration']:.2f}s / 峰值 {info['peak']} ({dbfs(info['peak'])} dBFS)"
              f" / RMS {info['rms']} ({dbfs(info['rms'])} dBFS)")

    print("\n===== 回放环境（用户定稿：正常 31%、上限 50%、默认增益） =====")
    volume = get_system_volume()
    original_volume = None
    if volume is None:
        result.warn("未能读取系统音量（非 macOS？）：音量口径请人工确认 ≤50%")
    else:
        print(f"  系统输出音量 {volume['volume']}%（正常 {SYSTEM_VOLUME_NORMAL}%、"
              f"上限 {SYSTEM_VOLUME_MAX}%）")
        result.check(not volume["muted"], "系统输出未静音")
        if args.keep_volume:
            result.check(volume["volume"] <= SYSTEM_VOLUME_MAX,
                         f"系统音量 {volume['volume']}% ≤ 上限 {SYSTEM_VOLUME_MAX}%")
            if volume["volume"] != SYSTEM_VOLUME_NORMAL:
                result.warn(f"系统音量 {volume['volume']}% ≠ 正常值 {SYSTEM_VOLUME_NORMAL}%"
                            "（唤醒率会变化，但不违反上限约束）")
        elif volume["volume"] != args.volume:
            if volume["volume"] > SYSTEM_VOLUME_MAX:
                result.warn(f"当前系统音量 {volume['volume']}% 超过上限 {SYSTEM_VOLUME_MAX}%，"
                            f"测试期间临时设为 {args.volume}%（结束恢复）")
            original_volume = volume["volume"]
            set_system_volume(args.volume)
            print(f"  系统音量 {original_volume}% → {args.volume}%（结束恢复）")
    paths["original_volume"] = original_volume
    return paths


def print_round_table(rounds: list[dict]) -> None:
    print("\n===== 逐轮数据 =====")
    header = (f"{'轮':>2} {'尝试':>4} {'上行帧':>5} {'首帧us':>7} {'TTS帧':>5} "
              f"{'首声us':>7} {'欠载':>4} {'解码':>4} {'dur':>5} {'播放峰':>6} "
              f"{'基线峰':>6} {'拾音':>4} {'内部堆':>8} {'PSRAM':>9} {'任务':>4}  结果")
    print(header)
    for rd in rounds:
        stream = rd.get("stream") or {}
        tries = rd.get("attempt_count", 1)
        if rd.get("env_fail"):
            reason = ""
            attempts = rd.get("attempts") or []
            if attempts:
                reason = attempts[-1].get("env_reason") or ""
            print(f"{rd['index']:>2} {tries:>4}  —— 环境失败（{reason}），不计入断言")
            continue
        print(
            f"{rd['index']:>2} {tries:>4} {rd.get('up_frames', -1):>5} "
            f"{rd.get('first_frame_us', -1):>7} {rd.get('tts_frames', -1):>5} "
            f"{stream.get('first_sound_us', -1):>7} {stream.get('underrun', -1):>4} "
            f"{stream.get('decoded', -1):>4} {stream.get('dur_ms', -1):>5} "
            f"{rd.get('playback_peak', -1):>6} {str(rd.get('baseline_peak')):>6} "
            f"{'WARN' if rd.get('pickup_warn') else ('严格' if rd.get('pickup_verdict') == 'strict' else '-'):>4} "
            f"{str(rd.get('internal_free')):>8} "
            f"{str(rd.get('psram_free')):>9} {str(rd.get('tasks')):>4}  "
            f"{'PASS' if rd.get('ok') else 'FAIL'}"
        )


def run(args: argparse.Namespace) -> int:
    result = Result()
    print("===== T2.6 端到端语音真机自测（e2e_voice_test.py） =====")
    print(f"[1/4] 生成素材：{SPEECH_WAV} ×{args.material_repeats} → 24k/mono MP3"
          f"（lameenc {args.kbps}kbps）")
    material, ref, mp3_path = prepare_material(args, result)
    print(f"  素材: {material['seconds']:.2f}s / PCM {material['pcm_bytes']} B / "
          f"MP3 {material['mp3_bytes']} B → {mp3_path}")

    paths = check_environment(result, args)
    if args.dry_run:
        try:
            port = detect_port(args.port)
            print(f"  串口: {port}")
        except SystemExit as exc:
            print(f"  串口检查跳过: {exc}")
        if paths.get("original_volume") is not None:
            set_system_volume(paths["original_volume"])
            print(f"  系统音量已恢复 {paths['original_volume']}%")
        print("\n--dry-run：素材 / PyAV 参考 / 环境检查完成，未连接设备、未启动 mock。")
        ok = result.summary()
        print("\nE2E_VOICE_TEST: " + ("PASS" if ok else "FAIL") + "（dry-run）")
        return 0 if ok else 1

    port = detect_port(args.port)
    print(f"\n[2/4] 串口 {port} / mock 端口 {args.mock_port}")
    mock = Mock(args.mock_port)
    dev: Device | None = None
    rounds: list[dict] = []
    try:
        if args.no_mock:
            print("  mock: --no-mock，复用已有服务（跳过 mock 侧断言）")
        else:
            mock.start([
                "--tts-mp3", mp3_path,
                "--tts-pace-pct", str(args.pace_pct),
                "--think-ms", str(args.think_ms),
            ])
            if mock.owned:
                text = wait_mock_text(mock, "TTS 模式: 真实 MP3")
                m_mode = RE_MOCK_TTS_MODE.search(text)
                result.check(m_mode is not None, "mock 已加载真实 MP3（--tts-mp3）")
                if m_mode is not None:
                    result.check(int(m_mode.group(2)) == TARGET_FRAMES
                                 and int(m_mode.group(3)) == TARGET_BYTES
                                 and int(m_mode.group(5)) == TTS_RATE,
                                 f"mock 横幅: 帧={m_mode.group(2)} 字节={m_mode.group(3)} "
                                 f"采样率={m_mode.group(5)}Hz 节奏={m_mode.group(6)}%")
                    result.check(int(m_mode.group(6)) == args.pace_pct,
                                 f"mock 节奏 {m_mode.group(6)}% == --tts-pace-pct {args.pace_pct}")
            else:
                result.warn("mock 端口被占用：复用外部服务（mock 侧断言跳过）")

        dev = Device(port)
        mark = dev.mark()
        if not args.no_reset:
            print("  复位设备，等待启动 / 联网 / WS 握手 ...")
            dev.reset()
            ready = dev.wait_line(RE_WS_READY, args.boot_timeout, mark)
        else:
            print("  复用已运行设备（--no-reset），等 WS 就绪 ...")
            ready = dev.wait_line(RE_WS_READY, 1.5, mark)
        if ready is None:
            # --no-reset 时设备可能早就连上了：用 vc status 的 WS 行兜底确认
            st = vc_status(dev)
            ws_ready = any("READY（可发起轮次）" in line for line in st.get("raw", []))
            result.check(ws_ready, "设备已连上 mock（vc status: WS READY）")
            if not ws_ready:
                return 1
        else:
            result.check(True, "设备启动并完成 WS 握手（hello_ack）")
        dev.pump(0.5)
        boot_lines = lines_since(dev, mark)
        result.check(not health_errors(boot_lines), "启动日志无 ERROR/panic")
        if mock.owned:
            text = mock.text()
            m_hello = RE_MOCK_HELLO.search(text)
            result.check(m_hello is not None, "mock 收到 hello 并回 hello_ack")

        # 初始状态：IDLE / 音量 100 / 统计清零 / 任务数基线
        ensure_idle(dev, args.idle_timeout)
        reset_stats(dev)
        pstat = player_stat(dev)
        result.check(pstat.get("vol") == 100, f"设备音量 {pstat.get('vol')} == 100（用户定稿）")
        st = vc_status(dev)
        result.check(st.get("state") == "IDLE", f"初始状态 IDLE（实际 {st.get('state')}）")
        tasks_start = task_count(dev)
        result.check(tasks_start is not None, f"任务数基线可读（{tasks_start} 个）")
        health_mark = dev.mark()

        # ---- 10 轮 ----
        print(f"\n[3/4] 连续 {args.rounds} 轮真实端到端（唤醒 → 上行 → 下行 mp3 → 播放 → R2）")
        for index in range(1, args.rounds + 1):
            rd = run_round(dev, mock, result, args, material, ref, index, paths)
            rounds.append(rd)
            if index < args.rounds:
                dev.pump(args.pause_between)

        # ---- 汇总断言 ----
        print(f"\n[4/4] 汇总（{args.rounds} 轮）")
        print_round_table(rounds)
        result.round = 0
        committed = [rd for rd in rounds if not rd.get("env_fail")]
        env_failed = [rd for rd in rounds if rd.get("env_fail")]
        green = [rd for rd in committed if rd.get("ok")]
        effective_min = min(args.min_rounds, args.rounds)
        if env_failed:
            result.warn(f"环境失败轮次: {[rd['index'] for rd in env_failed]}"
                        f"（唤醒漏触发 / VAD 8s 未静音，不计入断言；见逐轮数据）")
        result.check(len(green) >= effective_min,
                     f"成功轮次 {len(green)}/{args.rounds} ≥ {effective_min}")
        committed_fail = [rd["index"] for rd in committed if not rd.get("ok")]
        result.check(not committed_fail,
                     f"所有成功轮次全部断言通过（失败轮次: {committed_fail or '无'}）")

        internals = [rd["internal_free"] for rd in committed if rd.get("internal_free") is not None]
        if len(internals) >= 2:
            span = max(internals) - min(internals)
            result.check(span <= 2048,
                         f"内部堆 {len(internals)} 轮波动 {span} B ≤ 2048 B"
                         f"（min {min(internals)} / max {max(internals)}）")
            if len(internals) >= 4:
                result.check(
                    not all(later < earlier for earlier, later in zip(internals, internals[1:])),
                    "内部堆不单调下降（无持续泄漏）")
            else:
                result.warn(f"内部堆仅 {len(internals)} 轮样本，暂不判单调性（10 轮时判定）")
        else:
            result.warn(f"内部堆采样仅 {len(internals)} 轮（--rounds 1 冒烟时不做稳定性判定）")

        psrams = [rd["psram_free"] for rd in committed if rd.get("psram_free") is not None]
        if len(psrams) >= 2:
            result.check(abs(psrams[-1] - psrams[0]) <= 8192,
                         f"PSRAM 首末差 {psrams[-1] - psrams[0]} B（≤8192，≈0）")
            if len(psrams) >= 4:
                result.check(
                    not all(later < earlier for earlier, later in zip(psrams, psrams[1:])),
                    "PSRAM 不单调下降")
            else:
                result.warn(f"PSRAM 仅 {len(psrams)} 轮样本，暂不判单调性（10 轮时判定）")
        else:
            result.warn(f"PSRAM 采样仅 {len(psrams)} 轮（--rounds 1 冒烟时不做稳定性判定）")

        tasks_end = task_count(dev)
        result.check(tasks_start is not None and tasks_end == tasks_start,
                     f"任务数不变（{tasks_start} → {tasks_end}）")

        underruns = sum((rd.get("stream") or {}).get("underrun", 0) for rd in committed)
        result.check(underruns == 0, f"全部成功轮次欠载总数 {underruns} == 0")
        firsts = [rd["stream"]["first_sound_us"] for rd in committed if rd.get("stream")]
        if firsts:
            firsts_sorted = sorted(firsts)
            median = firsts_sorted[len(firsts_sorted) // 2]
            print(f"  first_sound_us: min {min(firsts)} / 中位 {median} / max {max(firsts)}"
                  f"（阈值 <{FIRST_SOUND_US_MAX}）")

        # 拾音判据：WARN/inconclusive 单列计数（不判 FAIL，但不得静默）
        pickup_warn = [rd for rd in green if rd.get("pickup_warn")]
        if pickup_warn:
            pct = len(pickup_warn) * 100.0 / max(len(green), 1)
            result.warn(f"拾音判据 inconclusive {len(pickup_warn)}/{len(green)} 轮"
                        f"（{pct:.0f}%；环境基线噪声高，硬断言仍全绿）：轮 "
                        f"{[rd['index'] for rd in pickup_warn]} / baseline "
                        f"{[rd.get('baseline_peak') for rd in pickup_warn]} / 播放峰 "
                        f"{[rd.get('playback_peak') for rd in pickup_warn]}")
        else:
            result.info(f"拾音判据全部严格通过（{len(green)}/{len(green)} 轮，"
                        f"baseline < {NOISE_BASELINE_PEAK}）")

        result.check(not health_errors(lines_since(dev, health_mark)),
                     "全程无 ERROR/panic")

        if args.log:
            with open(args.log, "w", encoding="utf-8") as handle:
                handle.write("\n".join(lines_since(dev, 0)))
            print(f"  串口日志已保存: {args.log}")
        if mock.owned:
            with open(mock.log_path, encoding="utf-8", errors="replace") as handle:
                mock_all = handle.read()
            result.check(MOCK_SEQ_GAP not in mock_all and MOCK_BAD_FRAME not in mock_all,
                         "全程 mock 侧上行无 seq 跳变 / 坏帧")

        ok = result.summary()
        print("\nE2E_VOICE_TEST: " + ("PASS" if ok else "FAIL")
              + f"（成功 {len(green)}/{args.rounds} 轮，拾音 WARN {len(pickup_warn)} 轮，"
              f"断言 {len(result.rows)} 项）")
        return 0 if ok else 1
    finally:
        if dev is not None:
            dev.close()
        mock.stop()
        if paths.get("original_volume") is not None:
            try:
                set_system_volume(paths["original_volume"])
                print(f"  系统音量已恢复 {paths['original_volume']}%")
            except Exception as exc:
                print(f"  恢复系统音量失败：{exc}（请手动设为 {paths['original_volume']}%）")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="T2.6 端到端语音真机自测（唤醒 → 上行 Opus → 下行真实 MP3 流式播放 → R2，10 轮）",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--port", default=None, help="设备串口；缺省自动探测 /dev/cu.usbmodem*")
    parser.add_argument("--mock-port", type=int, default=DEFAULT_MOCK_PORT,
                        help="本地 mock 端口（需与设备 CONFIG_AI_WS_URL 一致）")
    parser.add_argument("--rounds", type=int, default=10, help="端到端轮数")
    parser.add_argument("--min-rounds", type=int, default=8,
                        help="要求成功轮数（唤醒失败只重试/记录，不计入断言）")
    parser.add_argument("--max-retries", type=int, default=2,
                        help="每轮环境因素（唤醒漏触发 / VAD 8s 未静音收尾）的整轮重试次数"
                             "（默认 2，即最多 3 次尝试；断言只统计成功轮次）")
    parser.add_argument("--allow-max-duration", action="store_true",
                        help="接受 audio_end 因 VAD 8s 上限收尾（环境人声持续时放宽；"
                             "voice_cloud_test 同口径）。默认严格按 vad-silence 判定")
    parser.add_argument("--max-utter-ms", type=int, default=8000,
                        help="设备的 VAD 上限收尾时间（CONFIG_VC_MAX_UTTER_MS，只用于提示/日志）")
    parser.add_argument("--volume", type=int, default=SYSTEM_VOLUME_MAX,
                        help=f"回放期间临时系统输出音量（0~{SYSTEM_VOLUME_MAX}，结束恢复）；"
                             f"默认 {SYSTEM_VOLUME_MAX}=用户定稿上限，与 voice_cloud_test 一致，"
                             "保证 10 轮唤醒稳定")
    parser.add_argument("--keep-volume", action="store_true",
                        help="不改系统音量（只校验 ≤ 上限；此时唤醒率取决于当前音量）")
    parser.add_argument("--pace-pct", type=int, default=80,
                        help="mock 真实 MP3 节奏（帧时长百分比；macOS asyncio 每帧超调 ~1.7ms，"
                             "100%% 会产生假欠载，故默认 80%%）")
    parser.add_argument("--think-ms", type=int, default=300, help="mock 的 audio_end→首事件延时")
    parser.add_argument("--material-repeats", type=int, default=TTS_SOURCE_REPEATS,
                        help="TTS 素材拼接份数（3×1.6s≈4.8s）")
    parser.add_argument("--kbps", type=int, default=TTS_KBPS, help="MP3 码率（24k 单声道）")
    parser.add_argument("--boot-timeout", type=float, default=40.0, help="复位后等 WS 握手的超时（秒）")
    parser.add_argument("--wake-timeout", type=float, default=8.0,
                        help="单次唤醒等 prompt done 的超时（秒）")
    parser.add_argument("--round-timeout", type=float, default=15.0,
                        help="等 audio_end 的超时（秒）")
    parser.add_argument("--tts-timeout", type=float, default=20.0,
                        help="等 tts_start / tts_end 的超时（秒）")
    parser.add_argument("--drain-timeout", type=float, default=10.0,
                        help="等 stream end / R2 / round done 的超时（秒）")
    parser.add_argument("--idle-timeout", type=float, default=8.0,
                        help="轮次间隙等 IDLE 的超时（秒）")
    parser.add_argument("--pause-between", type=float, default=1.5, help="每轮之间的静默等待（秒）")
    parser.add_argument("--playback-warmup", type=float, default=1.2,
                        help="起播后等待多少秒再测播放期拾音（避开起播头 ~1s 的弱拾音段）")
    parser.add_argument("--playback-window", type=float, default=1.2,
                        help="播放期 mic stat 的测量窗口（秒）")
    parser.add_argument("--post-baseline-window", type=float, default=0.8,
                        help="播放结束后再取的静音基线窗口（秒）；baseline 取它与播放前窗口的较小峰值")
    parser.add_argument("--no-mock", action="store_true",
                        help="不启动 mock（已有服务在跑时用；mock 侧断言跳过）")
    parser.add_argument("--no-reset", action="store_true", help="不复位设备（设备已在跑时用）")
    parser.add_argument("--log", default="", help="把完整串口日志写到该文件")
    parser.add_argument("--dry-run", action="store_true", help="只生成素材+PyAV 参考/环境检查，不连设备")
    args = parser.parse_args()

    if args.rounds < 1:
        raise SystemExit("--rounds 必须 ≥ 1")
    if args.max_retries < 0:
        raise SystemExit("--max-retries 必须 ≥ 0")
    if args.volume is not None and not 0 <= args.volume <= SYSTEM_VOLUME_MAX:
        raise SystemExit(f"--volume 必须在 0~{SYSTEM_VOLUME_MAX}（用户定稿上限 50%）")
    if not 1 <= args.pace_pct <= 400:
        raise SystemExit("--pace-pct 必须在 1~400")
    if args.material_repeats < 1:
        raise SystemExit("--material-repeats 必须 ≥ 1")

    try:
        return run(args)
    except KeyboardInterrupt:
        print("\n中断（Ctrl-C）")
        return 130


if __name__ == "__main__":
    sys.exit(main())
