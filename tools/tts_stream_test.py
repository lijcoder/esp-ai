#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["pyserial>=3.5", "lameenc>=1.8", "av>=12", "numpy"]
# ///
"""T2.4 下行流式播放真机自测（真实 MP3 经 WS 0x02 下发 → 端侧流式解码播放）。

契约依据（**不要改契约**）：`.t2.4-contract.md` §6（mock 真实 MP3 参数）、§7（本脚本）、
§8（阈值）。设备侧 console 输出格式以契约 §4 为准，脚本只消费、不猜字段：

    stream: active=<0|1> fed=<n> bytes=<n> decoded=<n> samples=<n> rate=<n> ch=<n>
            ring_ms=<n> high_ms=<n> underrun=<n> dropped=<n> bad=<n> first_sound_us=<n> dur_ms=<n>

    PLAYER: stream open codec=mp3 sr=24000
    PLAYER: stream first sound after <n> ms (prebuffer <n> ms)
    PLAYER: stream end frames=<n> bytes=<n> decoded=<n> dur=<n> ms underrun=<n> bad=<n>
    PLAYER: stream abort (frames=<n>, drop <n> B)

一条命令完成：
  1. PC 侧把 tools/今天天气如何.wav（24k/16bit/mono，1.6s）经 lameenc 转 24k 单声道 MP3，
     拼接 3 份 ≈4.8s（**新增音频不入库**）；
  2. 起本地 mock（`--tts-mp3` 真实 MP3、按帧切分、实时节奏）→ 复位设备 → `vc start`
     → 轮询 `vc status` 到 UPLINK → `vc stop` 确定性触发一轮（不依赖唤醒/人声）；
  3. 断言（对应 T2.4-D 验收表）：first_sound_us<150ms、稳态 underrun=0、decoded 帧/样本
     与 PyAV 参考一致（±2 帧）、stall 场景 underrun>=1 且恢复、abort 停声（≤150ms，目标
     100ms）、tts_end 排空（ring_ms=0、decoded==fed、先 tts_end 后 stream end）、窗口外帧
     丢弃（`--tts-late-bin`，vc「音频丢弃」≥2 帧）、合成帧降级无 ERROR、连续 10 轮内部堆
     波动 ≤2KB 且不单调下降、PSRAM 稳定；
  4. 汇总 PASS/FAIL（机器可读的最后一行 + 退出码 0/1），供 P2 验收使用。

与 `voice_cloud_test.py` 的分工：那个脚本测「端云链路/协议/sid 过滤/事件」，mock 下发的是
**合成帧**（不可解码）；本脚本测「真实 MP3 流式播放」的音频侧硬指标（首帧延迟/欠载/解码
对拍/排空/abort/堆稳定），mock 下发**真实 MP3**。两者互补，P2 验收都要跑。

用法：
    uv run tools/tts_stream_test.py                    # 全量（默认复位设备，独占串口）
    uv run tools/tts_stream_test.py --dry-run          # 只生成素材 + PyAV 参考，不碰设备
    uv run tools/tts_stream_test.py --no-reset         # 设备已在跑
    uv run tools/tts_stream_test.py --skip-heap        # 跳过 10 轮堆稳定（快速冒烟）
    uv run tools/tts_stream_test.py --log /tmp/tts_stream.log

约束：脚本**独占串口**（先关 `idf.py monitor`）、**不改系统音量**（本测试不依赖 Mac 扬声器
回放，音量口径与 `voice_test.py` 无关）；需要 `CONFIG_DIAG_ENABLE_PLAYER_TEST` 的 console
探针（`player stat` 的 stream 行由 T2.4-A 常驻提供，不依赖该探针）。
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

# 复用既有工具封装，避免多套实现漂移（这些模块无 import 副作用）
from voice_test import (  # noqa: E402  （PEP 723 脚本按脚本目录入 sys.path）
    Device,
    Result,
    detect_port,
    error_lines,
    read_wav_info,
)
from voice_cloud_test import (  # noqa: E402
    END_VC_CANCEL,
    END_VC_RESET,
    END_VC_START,
    END_VC_STOP,
    RE_ROUND_ABORT,
    RE_ROUND_DONE,
    RE_ROUND_START,
    RE_TTS_END,
    RE_TTS_START,
    RE_WS_READY,
    lines_since,
    parse_heap,
    vc_status,
    wait_line,
)
from mp3_test import reference_decode  # noqa: E402  （PyAV 独立解码参考）

MOCK_SCRIPT = os.path.join(HERE, "ws_mock_server.py")
SOURCE_WAV = "今天天气如何.wav"  # 24 kHz / 16 bit / mono，1.6s
DEFAULT_MOCK_PORT = 8765
TTS_RATE = 24000

# ---- 设备侧日志（契约 §4，逐字匹配；不得按猜测改） ----
RE_STREAM_OPEN = re.compile(r"PLAYER: stream open codec=mp3 sr=(\d+)")
RE_FIRST_SOUND = re.compile(r"PLAYER: stream first sound after (\d+) ms \(prebuffer (\d+) ms\)")
RE_STREAM_END = re.compile(
    r"PLAYER: stream end frames=(\d+) bytes=(\d+) decoded=(\d+) dur=(\d+) ms "
    r"underrun=(\d+) bad=(\d+)"
)
RE_STREAM_ABORT = re.compile(r"PLAYER: stream abort \(frames=(\d+), drop (\d+) B\)")
RE_STREAM_UNDERRUN = re.compile(r"PLAYER: stream underrun #(\d+) \(ring (\d+) ms\)")
# 契约 §4 的 player stat 追加行（字段顺序/拼写逐字一致）
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
# 契约 §2.7：`vc stat` 的「下行」行追加 `；音频丢弃 <n> 帧 字节 <n>`
RE_VC_AUDIO_DROP = re.compile(r"音频丢弃 (\d+) 帧 字节 (\d+)")
# mock 日志
RE_MOCK_TTS_DONE = re.compile(r"tts 下发完成 frames=(\d+) bytes=(\d+) mode=(\w+)")
RE_MOCK_MODE = re.compile(r"TTS 模式: 真实 MP3")

# console 命令结束哨兵（与 voice_cloud_test / diag 现有输出一致）
END_PLAYER_STAT = RE_STREAM_LINE
END_PLAYER_RESET = re.compile(r"PLAYERSTAT cleared")
END_WS_RECONNECT = re.compile(r"已请求重连")


def wait_mock_text(mock: "Mock", needle: str, timeout: float = 8.0) -> str:
    """等 mock 日志里出现某段文本（banner/启动日志与端口就绪之间有竞态）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        text = mock.text()
        if needle in text:
            return text
        time.sleep(0.05)
    return mock.text()


# ---------------------------------------------------------------- 素材与参考


def make_tts_material(wav_path: str, mp3_path: str, repeats: int, kbps: int) -> dict:
    """把 24k/16bit/mono WAV 拼接 repeats 份，用 lameenc 转成 24k 单声道 MP3。

    契约 §7 要求「可拼接成 ~5s 长素材」；默认 3×1.6s≈4.8s。素材现生成、不入库。
    """
    import lameenc

    info = read_wav_info(wav_path)
    if (info["format"], info["channels"], info["bits"], info["rate"]) != (1, 1, 16, 24000):
        raise SystemExit(
            f"素材格式不符合预期（需 PCM 24k/16bit/mono）：{wav_path} {info}"
        )
    with open(wav_path, "rb") as handle:
        handle.seek(info["data_offset"])
        pcm = handle.read(info["data_bytes"])
    if not pcm:
        raise SystemExit(f"素材没有 PCM 数据：{wav_path}")
    pcm_all = pcm * max(1, repeats)

    encoder = lameenc.Encoder()
    encoder.set_bit_rate(kbps)
    encoder.set_in_sample_rate(24000)
    encoder.set_out_sample_rate(24000)  # 不设会被 lameenc 默认降到 24k 以外
    encoder.set_channels(1)
    encoder.set_quality(5)
    data = encoder.encode(pcm_all) + encoder.flush()
    with open(mp3_path, "wb") as handle:
        handle.write(data)
    return {
        "seconds": len(pcm_all) / 2 / 24000,
        "pcm_bytes": len(pcm_all),
        "mp3_bytes": len(data),
        "kbps": kbps,
        "repeats": repeats,
    }


def prepare_material(args: argparse.Namespace) -> tuple[dict, dict, str]:
    wav_path = os.path.join(HERE, SOURCE_WAV)
    if not os.path.exists(wav_path):
        raise SystemExit(f"缺少测试素材：{wav_path}")
    workdir = tempfile.mkdtemp(prefix="tts_stream_test_")
    mp3_path = os.path.join(workdir, "tts_24k_mono_5s.mp3")
    material = make_tts_material(wav_path, mp3_path, args.material_repeats, args.kbps)
    ref = reference_decode(mp3_path)
    ref["duration_ms"] = int(ref["samples"] * 1000 / max(ref["rate"], 1))
    if ref["rate"] != TTS_RATE or ref["channels"] != 1:
        raise SystemExit(
            f"MP3 参考参数不符（期望 24000/1ch）：rate={ref['rate']} ch={ref['channels']}"
        )
    return material, ref, mp3_path


# ---------------------------------------------------------------- mock 进程


def port_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.3)
        return sock.connect_ex(("127.0.0.1", port)) == 0


class Mock:
    """本脚本拉起的 ws_mock_server 子进程（--no-mock 时只做端口探测，不再拥有）。"""

    def __init__(self, port: int) -> None:
        self.port = port
        self.proc: subprocess.Popen | None = None
        self.owned = False
        self.log_path = os.path.join(tempfile.gettempdir(), f"tts_stream_mock_{port}.log")
        self._log = None

    def start(self, extra_args: list[str]) -> None:
        if port_open(self.port):
            self.owned = False
            print(f"  mock: 端口 {self.port} 已被占用，复用现有服务（跳过 mock 侧断言）")
            return
        self._log = open(self.log_path, "w", encoding="utf-8", buffering=1)
        cmd = [
            "uv", "run", MOCK_SCRIPT,
            "--host", "0.0.0.0", "--port", str(self.port),
            *extra_args,
        ]
        self.proc = subprocess.Popen(
            cmd, stdout=self._log, stderr=subprocess.STDOUT, cwd=HERE,
        )
        self.owned = True
        deadline = time.time() + 20.0
        while time.time() < deadline:
            if port_open(self.port):
                print(f"  mock: 已启动（端口 {self.port}）")
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


# ---------------------------------------------------------------- 设备侧读取


def wait_line_fast(dev: Device, pattern: re.Pattern, timeout: float, mark: int = 0) -> float | None:
    """同 voice_test.Device.wait_line，但 10ms 轮询（abort 延迟测量需要更细粒度）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        for ts, line in dev.lines_since(mark):
            if pattern.search(line):
                return ts
        dev.pump(0.01)
    return None


def player_stat(dev: Device) -> dict:
    """`player stat` → dict；含契约 §4 的 stream 行（stream=None 表示未输出）。"""
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


def vc_counts(dev: Device) -> dict:
    """`vc status`：状态/轮次/上行/下行 + T2.4 追加的「音频丢弃」。"""
    st = vc_status(dev)
    out = {
        "state": st.get("state"),
        "raw": st.get("raw", []),
        "audio_dropped_frames": None,
        "audio_dropped_bytes": None,
    }
    for key in ("rounds_started", "rounds_done", "rounds_aborted", "up_frames",
                "up_bytes", "rx_audio_frames", "dropped_sid", "parse_errors",
                "cloud_errors"):
        if st.get(key) is not None:
            out[key] = st[key]
    for line in st.get("raw", []):
        m = RE_VC_AUDIO_DROP.search(line)
        if m:
            out["audio_dropped_frames"] = int(m.group(1))
            out["audio_dropped_bytes"] = int(m.group(2))
    return out


def reset_stats(dev: Device) -> None:
    dev.console("vc reset", END_VC_RESET, timeout=2.0)
    dev.console("player reset", END_PLAYER_RESET, timeout=2.0)


def wait_idle(dev: Device, timeout: float = 6.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if vc_counts(dev).get("state") == "IDLE":
            return True
        dev.pump(0.05)
    return False


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


def health_errors(lines: list[str]) -> list[str]:
    """只按 ERROR/panic 判健康：取消/中止路径的 WARN「round aborted」是预期日志。"""
    candidates = [
        line for line in lines
        if re.search(r"^\s*E \(\d+\)", line)
        or re.search(r"panic|Guru Meditation|stack overflow|StoreProhibited", line)
    ]
    return [line for line in candidates if error_lines([line])]


# ---------------------------------------------------------------- 一轮的触发与收尾


def begin_round(dev: Device, result: Result, label: str) -> tuple[int, int]:
    """`vc start` → 等 round start → 轮询 vc status 到 UPLINK（确定性触发，不靠唤醒）。"""
    mark = dev.mark()
    dev.console("vc start", END_VC_START, timeout=2.0)
    line = wait_line(dev, RE_ROUND_START, 5.0, mark)
    if line is None:
        result.check(False, f"{label}: vc start 被接受（收到 round start）")
        return -1, mark
    sid = int(RE_ROUND_START.search(line).group(1))
    state = None
    deadline = time.time() + 3.0
    while time.time() < deadline:
        state = vc_counts(dev).get("state")
        if state in ("UPLINK", "WAIT_RESPONSE"):
            break
        dev.pump(0.02)
    result.check(state == "UPLINK", f"{label}: 轮询 vc status 到 UPLINK（实际 {state}）")
    return sid, mark


def stop_and_wait_round(
    dev: Device, result: Result, label: str, sid: int, mark: int, timeout: float
) -> dict:
    """`vc stop` → mock 下发 TTS → 等 tts_start / tts_end / round done。"""
    dev.console("vc stop", END_VC_STOP, timeout=2.0)
    info: dict = {"sid": sid, "tts_frames": -1, "round_done": False}

    line = wait_line(dev, RE_TTS_START, 10.0, mark)
    m = RE_TTS_START.search(line) if line else None
    tts_ok = (
        m is not None and int(m.group(1)) == sid and m.group(2) == "mp3"
        and int(m.group(3)) == TTS_RATE
    )
    result.check(tts_ok, f"{label}: tts_start codec=mp3 sr=24000 sid={sid}")

    end_line = wait_line(dev, RE_TTS_END, timeout, mark)
    m = RE_TTS_END.search(end_line) if end_line else None
    if m is not None:
        info["tts_frames"] = int(m.group(2))
    result.check(
        m is not None and int(m.group(1)) == sid,
        f"{label}: 收到 tts_end（本轮下行音频 {info['tts_frames']} 帧）",
    )

    done = wait_line(dev, RE_ROUND_DONE, 15.0, mark)
    info["round_done"] = done is not None
    result.check(done is not None, f"{label}: round done")
    return info


# ---------------------------------------------------------------- 断言


def check_duration(
    result: Result, label: str, dur_ms: int, material_ms: int, stall_ms: int | None
) -> None:
    """stream dur 的断言口径（T2.4-C3）。

    - normal / late-bin（无 stall）：播放时长应 ≈ 素材时长，±10%（至少 ±100ms）。
    - stall：按设计会在欠载期间补静音，多出「stall_ms - 起播缓冲余量」≈ 460~500ms，
      dur 的物理范围是 [素材时长, 素材时长 + stall_ms + 300ms 余量]；**不能用 ±10%**
      （±484ms 会被 stall 命中时的缓冲相位随机顶破，与实现无关）。
    """
    if stall_ms is None:
        result.check(
            abs(dur_ms - material_ms) <= max(100, material_ms // 10),
            f"{label}: dur={dur_ms} ms ≈ 素材 {material_ms} ms（±10%）",
        )
        return
    upper = material_ms + stall_ms + 300
    result.check(
        material_ms <= dur_ms <= upper,
        f"{label}: dur={dur_ms} ms ∈ [{material_ms}, {upper}]"
        f"（素材 {material_ms} + stall {stall_ms} + 余量 300；stall 按设计补静音，不用 ±10%）",
    )


def check_stream_round(
    dev: Device,
    result: Result,
    label: str,
    ref: dict,
    mark: int,
    *,
    min_underrun: int | None = None,
    max_underrun: int | None = None,
    stall_ms: int | None = None,
) -> dict | None:
    """正常/stall 轮次的硬指标：首帧延迟、欠载、解码对拍、排空、日志一致性。

    `stall_ms` 非 None 表示这是 stall 注入轮，dur 用物理范围口径（见 check_duration）。
    """
    # round done 可能早于 PLAYER 的 fade/尾部延时结束：等 active 归 0 再读统计
    stream = wait_stream_idle(dev)
    if stream is None:
        result.check(False, f"{label}: player stat 有契约 §4 的 stream 行（T2.4-A 是否就绪？）")
        return None

    dur_ref = int(ref["samples"] * 1000 / max(ref["rate"], 1))
    result.check(stream["active"] == 0, f"{label}: 流已收尾 active=0")
    result.check(
        stream["rate"] == TTS_RATE and stream["ch"] == 1,
        f"{label}: rate=24000 / ch=1（实际 {stream['rate']}/{stream['ch']}）",
    )
    result.check(
        abs(stream["fed"] - ref["frames"]) <= 2,
        f"{label}: fed {stream['fed']} 帧 ≈ 素材 {ref['frames']} 帧（±2）",
    )
    result.check(
        abs(stream["decoded"] - ref["frames"]) <= 2,
        f"{label}: decoded {stream['decoded']} 帧 ≈ PyAV 参考 {ref['frames']} 帧（±2）",
    )
    result.check(
        abs(stream["samples"] - ref["samples"]) <= 2 * 1152,
        f"{label}: samples {stream['samples']} ≈ 参考 {ref['samples']}（±2 帧）",
    )
    result.check(
        stream["first_sound_us"] < 150000,
        f"{label}: first_sound_us={stream['first_sound_us']} < 150000（首字节→I2S 出声）",
    )
    result.check(stream["ring_ms"] == 0, f"{label}: tts_end 已排空 ring_ms={stream['ring_ms']}")
    result.check(
        stream["decoded"] == stream["fed"],
        f"{label}: 排空 decoded==fed（{stream['decoded']}/{stream['fed']}）",
    )
    result.check(stream["dropped"] == 0, f"{label}: ring 未溢出 dropped=0")
    result.check(stream["bad"] == 0, f"{label}: 坏帧 bad=0")
    if max_underrun is not None:
        result.check(
            stream["underrun"] <= max_underrun,
            f"{label}: 稳态 underrun={stream['underrun']} ≤ {max_underrun}",
        )
    if min_underrun is not None:
        result.check(
            stream["underrun"] >= min_underrun,
            f"{label}: stall 欠载 underrun={stream['underrun']} ≥ {min_underrun}",
        )
    check_duration(result, label, stream["dur_ms"], dur_ref, stall_ms)

    # 日志侧（契约 §4）：open / first sound / end，顺序证明 tts_end 后真的排空
    lines = lines_since(dev, mark)
    open_match = next(
        (RE_STREAM_OPEN.search(line) for line in lines if RE_STREAM_OPEN.search(line)), None
    )
    result.check(
        open_match is not None and int(open_match.group(1)) == TTS_RATE,
        f"{label}: 日志 stream open codec=mp3 sr=24000",
    )
    first_match = next(
        (RE_FIRST_SOUND.search(line) for line in lines if RE_FIRST_SOUND.search(line)), None
    )
    result.check(
        first_match is not None and int(first_match.group(1)) < 150,
        f"{label}: 日志 first sound "
        + (f"{first_match.group(1)} ms < 150 ms" if first_match else "未出现"),
    )
    end_match = next(
        (RE_STREAM_END.search(line) for line in lines if RE_STREAM_END.search(line)), None
    )
    result.check(end_match is not None, f"{label}: 日志 stream end（收尾完成）")
    if end_match is not None:
        result.check(
            abs(int(end_match.group(3)) - ref["frames"]) <= 2,
            f"{label}: stream end decoded={end_match.group(3)} ≈ 参考 {ref['frames']}",
        )
        result.check(
            int(end_match.group(6)) == 0,
            f"{label}: stream end bad={end_match.group(6)}（0）",
        )
    tts_end_index = next(
        (i for i, line in enumerate(lines) if RE_TTS_END.search(line)), -1
    )
    stream_end_index = next(
        (i for i, line in enumerate(lines) if RE_STREAM_END.search(line)), -1
    )
    result.check(
        0 <= tts_end_index < stream_end_index,
        f"{label}: 排空顺序 tts_end(#{tts_end_index}) → stream end(#{stream_end_index})",
    )
    return stream


def check_mock_round(
    result: Result,
    label: str,
    text: str,
    ref: dict,
    mp3_bytes: int,
    *,
    expect_late: bool = False,
    expect_stall: bool = False,
) -> None:
    """mock 侧：本轮真实 MP3 帧数/字节与素材一致（切帧正确）。"""
    if not text:
        result.warn(f"{label}: mock 日志为空（复用外部 mock？跳过 mock 侧断言）")
        return
    matches = RE_MOCK_TTS_DONE.findall(text)
    result.check(bool(matches), f"{label}: mock 日志有本轮 TTS 下发统计")
    if matches:
        frames, nbytes, mode = matches[-1]
        result.check(
            int(frames) == ref["frames"],
            f"{label}: mock 发送 {frames} 帧 == 素材 {ref['frames']} 帧",
        )
        result.check(
            int(nbytes) == mp3_bytes,
            f"{label}: mock 发送 {nbytes} B == 素材 {mp3_bytes} B",
        )
        result.check(mode == "mp3", f"{label}: mock 模式 mp3（实际 {mode}）")
    if expect_stall:
        result.check("stall" in text, f"{label}: mock 日志确认执行了 stall")
    if expect_late:
        result.check("late-pre" in text, f"{label}: mock 在 tts_start 前注入 1 帧")
        result.check("late-post" in text, f"{label}: mock 在 tts_end 后注入 1 帧")


def check_health(result: Result, label: str, lines: list[str]) -> None:
    errors = health_errors(lines)
    result.check(len(errors) == 0, f"{label}: 无 ERROR/panic（{len(errors)} 条）")
    for line in errors[:5]:
        print(f"    ! {line.strip()}")


# ---------------------------------------------------------------- 场景


def run_mock_round(
    dev: Device,
    result: Result,
    label: str,
    timeout: float,
) -> tuple[int, int, dict]:
    sid, mark = begin_round(dev, result, label)
    if sid < 0:
        return sid, mark, {}
    info = stop_and_wait_round(dev, result, label, sid, mark, timeout)
    return sid, mark, info


def scenario_normal(
    dev: Device, result: Result, ref: dict, mp3_bytes: int, args: argparse.Namespace, mock: Mock
) -> None:
    print("\n===== 场景 1/6：真实 MP3 正常一轮（首帧延迟 / 稳态欠载 / 排空 / 解码对拍） =====")
    reset_stats(dev)
    mock_mark = mock.mark()
    sid, mark, info = run_mock_round(dev, result, "normal", args.round_timeout)
    if sid < 0:
        return
    result.check(
        info.get("tts_frames") == ref["frames"],
        f"normal: tts_end 计数 {info.get('tts_frames')} == 素材 {ref['frames']} 帧",
    )
    stream = check_stream_round(
        dev, result, "normal", ref, mark, max_underrun=0
    )
    if stream is not None:
        result.check(
            stream["bytes"] == mp3_bytes,
            f"normal: 设备收到 {stream['bytes']} B == 素材 {mp3_bytes} B",
        )
    vc = vc_counts(dev)
    result.check(
        vc.get("rx_audio_frames") == ref["frames"],
        f"normal: vc 下行音频帧 {vc.get('rx_audio_frames')} == 素材 {ref['frames']}",
    )
    result.check(
        vc.get("audio_dropped_frames") == 0,
        f"normal: 窗口内无丢弃（音频丢弃 {vc.get('audio_dropped_frames')} 帧）",
    )
    result.check(
        vc.get("parse_errors") == 0 and vc.get("cloud_errors") == 0,
        "normal: 无解析失败 / 云端 error",
    )
    dev.pump(0.2)  # 等 done 回调（vc 侧 audio_fe_set_feed_enabled(true)）落地
    feed = dev.mic_stat().get("feed")
    result.check(feed == "开启", f"normal: 结束后收音恢复 R2（实际 {feed}）")
    if mock.owned:
        check_mock_round(result, "normal", mock.text_since(mock_mark), ref, mp3_bytes)


def scenario_abort(
    dev: Device, result: Result, ref: dict, args: argparse.Namespace
) -> None:
    print("\n===== 场景 2/6：abort 停声（vc cancel → stream_abort）=====")
    reset_stats(dev)
    sid, mark = begin_round(dev, result, "abort")
    if sid < 0:
        return
    dev.console("vc stop", END_VC_STOP, timeout=2.0)
    tts_start = wait_line(dev, RE_TTS_START, 10.0, mark)
    result.check(tts_start is not None, "abort: 进入 TTS 播放（tts_start）")
    decoded = 0
    deadline = time.time() + 6.0
    while time.time() < deadline:
        stream = player_stat(dev).get("stream")
        decoded = int(stream["decoded"]) if stream else 0
        if decoded >= 8:
            break
        dev.pump(0.02)
    result.check(decoded >= 8, f"abort: 打断前已解码 {decoded} 帧（≥8，确认真的在放）")

    dev.pump(0.1)
    cancel_at = time.time()
    dev.console("vc cancel", END_VC_CANCEL, timeout=2.0)
    abort_ts = wait_line_fast(dev, RE_STREAM_ABORT, 3.0, mark)
    latency_ms = (abort_ts - cancel_at) * 1000.0 if abort_ts is not None else None
    result.check(abort_ts is not None, "abort: 收到 PLAYER: stream abort 日志")
    if latency_ms is not None:
        result.check(
            latency_ms <= 150.0,
            f"abort: 停声延迟 {latency_ms:.0f} ms ≤ 150 ms（目标 100，含串口轮询误差）",
        )
        print(f"  INFO abort 延迟 {latency_ms:.0f} ms（串口读取 10ms 轮询）")

    stream1 = wait_stream_idle(dev) or {}
    result.check(stream1.get("active") == 0, "abort: 流已停止 active=0")
    dev.pump(0.5)
    stream2 = player_stat(dev).get("stream") or {}
    result.check(
        stream2.get("decoded", 0) - stream1.get("decoded", 0) <= 1,
        f"abort: 停声后不再解码（{stream1.get('decoded')} → {stream2.get('decoded')}）",
    )
    aborted = wait_line(dev, RE_ROUND_ABORT, 5.0, mark)
    result.check(aborted is not None, "abort: 日志 round aborted（取消路径）")
    result.check(wait_idle(dev, 6.0), "abort: 回到 IDLE")


def scenario_heap(
    dev: Device, result: Result, ref: dict, args: argparse.Namespace
) -> None:
    rounds = args.heap_rounds
    print(f"\n===== 场景 3/6：连续 {rounds} 轮堆 / PSRAM 稳定性 =====")
    reset_stats(dev)
    # 预热一轮：首轮会分配解码器状态等一次性缓冲，不计入稳定窗口
    warm_sid, _warm_mark, _ = run_mock_round(dev, result, "heap-warmup", args.round_timeout)
    if warm_sid < 0:
        return
    dev.pump(0.3)

    internal: list[int] = []
    psram: list[int] = []
    for index in range(rounds):
        label = f"heap#{index + 1}"
        sid, mark, _info = run_mock_round(dev, result, label, args.round_timeout)
        if sid < 0:
            return
        stream = wait_stream_idle(dev) or {}
        result.check(
            stream.get("active") == 0 and stream.get("underrun") == 0,
            f"{label}: 收尾正常（active=0 underrun=0，实际 "
            f"{stream.get('active')}/{stream.get('underrun')}）",
        )
        result.check(
            abs(stream.get("decoded", -1) - ref["frames"]) <= 2,
            f"{label}: decoded {stream.get('decoded')} ≈ 参考 {ref['frames']}",
        )
        heap = parse_heap(dev)
        internal_free = (heap.get("internal") or {}).get("free")
        psram_free = (heap.get("psram") or {}).get("free")
        if internal_free is not None:
            internal.append(internal_free)
        if psram_free is not None:
            psram.append(psram_free)
        print(
            f"  #{index + 1}: 内部剩余 {internal_free} B / PSRAM 剩余 {psram_free} B"
        )

    if len(internal) == rounds:
        span = max(internal) - min(internal)
        result.check(
            span <= 2048,
            f"heap: {rounds} 轮内部堆波动 {span} B ≤ 2048 B"
            f"（min {min(internal)} / max {max(internal)}）",
        )
        result.check(
            not all(later < earlier for earlier, later in zip(internal, internal[1:])),
            "heap: 内部堆不单调下降（无持续泄漏）",
        )
    else:
        result.check(False, f"heap: sys heap 内部 RAM 解析失败（{len(internal)}/{rounds}）")

    if len(psram) == rounds:
        span = max(psram) - min(psram)
        result.check(
            abs(psram[-1] - psram[0]) <= 8192 and span <= 65536,
            f"PSRAM 稳定：首末差 {psram[-1] - psram[0]} B（≤8192）、波动 {span} B（≤65536）",
        )
        result.check(
            not all(later < earlier for earlier, later in zip(psram, psram[1:])),
            "PSRAM 不单调下降",
        )
    else:
        result.check(False, f"PSRAM: 解析失败（{len(psram)}/{rounds}）")


def restart_mock(
    mock: Mock,
    args: argparse.Namespace,
    mp3_path: str,
    dev: Device,
    result: Result,
    label: str,
    extra: list[str],
) -> bool:
    """重启 mock（切换场景参数）→ 让设备立刻重连 → 等 hello_ack / IDLE。"""
    mock.stop()
    deadline = time.time() + 5.0
    while port_open(mock.port) and time.time() < deadline:
        time.sleep(0.05)
    base = ["--tts-mp3", mp3_path, "--tts-pace-pct", str(args.pace_pct),
            "--think-ms", str(args.think_ms)]
    mock.start(base + extra)
    if not mock.owned:
        return False
    mark = dev.mark()
    dev.console("ws reconnect", END_WS_RECONNECT, timeout=2.0)
    ready = wait_line(dev, RE_WS_READY, 15.0, mark)
    result.check(ready is not None, f"{label}: mock 重启后设备重连成功（hello_ack）")
    if ready is None:
        return False
    return wait_idle(dev, 6.0)


def scenario_stall(
    dev: Device, result: Result, ref: dict, mp3_bytes: int, args: argparse.Namespace, mock: Mock,
    mp3_path: str,
) -> None:
    print(f"\n===== 场景 4/6：stall 欠载（第 {args.stall_at} 帧后停发 {args.stall_ms} ms）=====")
    if not restart_mock(
        mock, args, mp3_path, dev, result, "stall",
        ["--tts-stall-ms", str(args.stall_ms), "--tts-stall-at", str(args.stall_at)],
    ):
        return
    reset_stats(dev)
    mock_mark = mock.mark()
    sid, mark, info = run_mock_round(dev, result, "stall", args.round_timeout)
    if sid < 0:
        return
    result.check(
        info.get("tts_frames") == ref["frames"],
        f"stall: tts_end 计数 {info.get('tts_frames')} == 素材 {ref['frames']} 帧",
    )
    check_stream_round(
        dev, result, "stall", ref, mark, min_underrun=1, max_underrun=None,
        stall_ms=args.stall_ms,
    )
    lines = lines_since(dev, mark)
    result.check(
        any(RE_STREAM_UNDERRUN.search(line) for line in lines),
        "stall: 日志出现 PLAYER: stream underrun",
    )
    # 恢复：欠载后仍把整段解完（上面 check_stream_round 已断言 decoded≈参考），这里补统计
    vc = vc_counts(dev)
    result.check(
        vc.get("rx_audio_frames") == ref["frames"],
        f"stall: vc 下行帧 {vc.get('rx_audio_frames')} == 素材 {ref['frames']}（停发不丢帧）",
    )
    result.check(
        vc.get("rounds_done") == 1,
        f"stall: 本轮正常完成（rounds_done={vc.get('rounds_done')}）",
    )
    check_mock_round(
        result, "stall", mock.text_since(mock_mark), ref, mp3_bytes, expect_stall=True
    )


def scenario_late(
    dev: Device, result: Result, ref: dict, mp3_bytes: int, args: argparse.Namespace, mock: Mock,
    mp3_path: str,
) -> None:
    print("\n===== 场景 5/6：窗口外 0x02 帧丢弃（tts_start 前 / tts_end 后各 1 帧）=====")
    if not restart_mock(
        mock, args, mp3_path, dev, result, "late-bin", ["--tts-late-bin"]
    ):
        return
    reset_stats(dev)
    mock_mark = mock.mark()
    sid, mark, info = run_mock_round(dev, result, "late-bin", args.round_timeout)
    if sid < 0:
        return
    result.check(
        info.get("tts_frames") == ref["frames"],
        f"late-bin: tts_end 计数 {info.get('tts_frames')} == 素材 {ref['frames']} 帧"
        "（窗口外 2 帧不计入）",
    )
    stream = check_stream_round(
        dev, result, "late-bin", ref, mark, max_underrun=0
    )
    if stream is not None:
        result.check(
            abs(stream["decoded"] - ref["frames"]) <= 2,
            f"late-bin: 窗口外帧未播放（decoded {stream['decoded']} ≈ {ref['frames']}）",
        )
    vc = vc_counts(dev)
    dropped = vc.get("audio_dropped_frames")
    result.check(
        dropped is not None and dropped >= 2,
        f"late-bin: vc 统计「音频丢弃 {dropped} 帧」（≥2，窗口外帧被丢弃）",
    )
    result.check(
        (vc.get("audio_dropped_bytes") or 0) > 0,
        f"late-bin: 丢弃字节数 {vc.get('audio_dropped_bytes')} > 0",
    )
    result.check(
        vc.get("rx_audio_frames") == ref["frames"],
        f"late-bin: 窗口内计数 {vc.get('rx_audio_frames')} == 素材 {ref['frames']}"
        "（窗口外不计数）",
    )
    check_health(result, "late-bin", lines_since(dev, mark))
    check_mock_round(
        result, "late-bin", mock.text_since(mock_mark), ref, mp3_bytes, expect_late=True
    )


def scenario_synthetic(
    dev: Device, result: Result, args: argparse.Namespace, mock: Mock, mp3_path: str
) -> None:
    print("\n===== 场景 6/6：合成帧降级（不可解码 MP3 → 不得 ERROR/panic）=====")
    mock.stop()
    deadline = time.time() + 5.0
    while port_open(mock.port) and time.time() < deadline:
        time.sleep(0.05)
    mock.start([
        "--think-ms", str(args.think_ms),
        "--chunk-ms", "20",
        "--max-tts-frames", "30",
    ])
    if not mock.owned:
        result.warn("synthetic: mock 复用中，跳过")
        return
    mark = dev.mark()
    dev.console("ws reconnect", END_WS_RECONNECT, timeout=2.0)
    ready = wait_line(dev, RE_WS_READY, 15.0, mark)
    result.check(ready is not None, "synthetic: mock 重启后设备重连成功")
    if ready is None:
        return
    result.check(wait_idle(dev, 6.0), "synthetic: 重连后 vc 空闲")
    reset_stats(dev)
    mock_mark = mock.mark()
    sid, mark, info = run_mock_round(dev, result, "synthetic", args.round_timeout)
    if sid < 0:
        return
    result.check(info.get("round_done", False), "synthetic: 本轮仍能正常收尾（round done）")
    stream = wait_stream_idle(dev)
    if stream is None:
        result.check(False, "synthetic: player stat 有 stream 行")
    else:
        result.check(stream["active"] == 0, "synthetic: 流已停止 active=0（降级不卡死）")
        # 契约没有规定合成帧能/不能解出多少帧（minimp3 可能把伪帧当静音解出）；
        # 这里只记录数值：硬要求是「无 ERROR/panic + 本轮收尾 + 收音恢复」。
        print(
            f"  INFO synthetic: fed={stream['fed']} decoded={stream['decoded']} "
            f"bad={stream['bad']} underrun={stream['underrun']}（仅记录，不作阈值）"
        )
    vc_synth = vc_counts(dev)
    result.check(
        vc_synth.get("rounds_done") == 1,
        f"synthetic: 本轮正常完成（rounds_done={vc_synth.get('rounds_done')}）",
    )
    dev.pump(0.2)  # 等 done 回调恢复收音
    feed = dev.mic_stat().get("feed")
    result.check(feed == "开启", f"synthetic: 降级后收音恢复 R2（实际 {feed}）")
    check_health(result, "synthetic", lines_since(dev, mark))
    if mock.owned:
        text = mock.text_since(mock_mark)
        result.check("mode=synth" in text, "synthetic: mock 本轮为合成帧模式")
        result.check("tts 下发完成" in text, "synthetic: mock 完成合成帧下发")


# ---------------------------------------------------------------- 主流程


def run(args: argparse.Namespace) -> int:
    result = Result()
    print("===== T2.4 下行流式播放真机自测（tts_stream_test.py）=====")
    print(f"[1/6] 生成素材：{SOURCE_WAV} ×{args.material_repeats} → 24k/mono MP3（lameenc {args.kbps}kbps）")
    material, ref, mp3_path = prepare_material(args)
    print(
        f"  素材: {material['seconds']:.2f}s / PCM {material['pcm_bytes']} B / "
        f"MP3 {material['mp3_bytes']} B"
    )
    print(
        f"  参考(PyAV): frames={ref['frames']} samples={ref['samples']} "
        f"rate={ref['rate']} ch={ref['channels']} dur={ref['duration_ms']} ms"
    )
    print(f"  MP3 文件: {mp3_path}")

    if args.dry_run:
        print("\n--dry-run：素材与 PyAV 参考就绪，未连接设备/未启动 mock。")
        print(f"用法提示：去掉 --dry-run 后全量断言（{len(['normal','abort','heap','stall','late','synthetic'])} 个场景）")
        return 0

    port = detect_port(args.port)
    print(f"[2/6] 串口 {port} / mock 端口 {args.mock_port}")
    mock = Mock(args.mock_port)
    dev: Device | None = None
    health_mark = 0
    try:
        if args.no_mock:
            print("  mock: --no-mock，复用已有服务（跳过 mock 侧断言与 stall/late/合成场景）")
            mock.owned = False
        else:
            base = ["--tts-mp3", mp3_path, "--tts-pace-pct", str(args.pace_pct),
                    "--think-ms", str(args.think_ms)]
            mock.start(base)
            if mock.owned:
                text = wait_mock_text(mock, "真实 MP3")
                result.check(RE_MOCK_MODE.search(text) is not None, "mock 已加载真实 MP3（--tts-mp3）")
                result.check(mp3_path in text, "mock 横幅显示素材路径")
            else:
                result.warn("mock 端口被占用：复用外部服务（mock 侧断言跳过；外部 mock 需自行用 --tts-mp3 启动）")

        dev = Device(port)
        mark = dev.mark()
        if not args.no_reset:
            print("  复位设备，等待启动/联网/WS 握手 ...")
            dev.reset()
            ready = wait_line(dev, RE_WS_READY, args.boot_timeout, mark)
        else:
            print("  复用已运行设备（--no-reset），等 WS 就绪 ...")
            ready = wait_line(dev, RE_WS_READY, 1.0, mark)
        if ready is not None:
            result.check(True, "设备完成 WS 握手（hello_ack）")
        else:
            # --no-reset 时设备可能早就连上了：用 vc status 的 WS 行兜底确认
            vc = vc_counts(dev)
            ws_ready = any("READY" in line and "WS:" in line for line in vc.get("raw", []))
            result.check(ws_ready, "设备已连上 mock（vc status: WS READY）")
            if not ws_ready:
                return 1
        dev.pump(0.3)
        health_mark = dev.mark()

        # ---- 场景 1：正常一轮 ----
        scenario_normal(dev, result, ref, material["mp3_bytes"], args, mock)

        if not args.skip_abort:
            scenario_abort(dev, result, ref, args)
        else:
            result.warn("--skip-abort：跳过 abort 停声场景")

        if not args.skip_heap:
            scenario_heap(dev, result, ref, args)
        else:
            result.warn("--skip-heap：跳过 10 轮堆稳定场景")

        # ---- 场景 4~6 需要脚本自己控制 mock 参数（重启 mock 并让设备重连） ----
        if mock.owned and not args.skip_scenarios:
            if not args.skip_stall:
                scenario_stall(dev, result, ref, material["mp3_bytes"], args, mock, mp3_path)
            else:
                result.warn("--skip-stall：跳过 stall 欠载场景")
            if not args.skip_late:
                scenario_late(dev, result, ref, material["mp3_bytes"], args, mock, mp3_path)
            else:
                result.warn("--skip-late：跳过窗口外帧场景")
            if not args.skip_synthetic:
                scenario_synthetic(dev, result, args, mock, mp3_path)
            else:
                result.warn("--skip-synthetic：跳过合成帧降级场景")
        else:
            result.warn("mock 复用中（--no-mock）：stall / late-bin / 合成帧场景需要脚本自己拉起 mock，已跳过")

        print("\n===== 结束健康检查 =====")
        all_lines = lines_since(dev, health_mark)
        check_health(result, "全程", all_lines)
        if args.log:
            with open(args.log, "w", encoding="utf-8") as handle:
                handle.write("\n".join(lines_since(dev, 0)))
            print(f"  串口日志已保存: {args.log}")
    finally:
        if dev is not None:
            dev.close()
        mock.stop()

    ok = result.summary()
    print("\nTTS_STREAM_TEST: " + ("PASS" if ok else "FAIL") + f"（{len(result.rows)} 项）")
    return 0 if ok else 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description="T2.4 下行流式播放真机自测（真实 MP3 经 WS 0x02 → 端侧流式解码播放）",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--port", default=None, help="设备串口；缺省自动探测 /dev/cu.usbmodem*")
    parser.add_argument("--mock-port", type=int, default=DEFAULT_MOCK_PORT,
                        help="本地 mock 端口（需与设备 CONFIG_AI_WS_URL 一致）")
    parser.add_argument("--no-mock", action="store_true",
                        help="复用已在跑的 mock（跳过 mock 侧断言与 stall/late/合成场景）")
    parser.add_argument("--no-reset", action="store_true", help="不复位设备（设备已在跑时用）")
    parser.add_argument("--boot-timeout", type=float, default=40.0,
                        help="复位后等 WS 握手的超时（秒）")
    parser.add_argument("--round-timeout", type=float, default=30.0,
                        help="等 tts_end 的超时（秒；素材 ~4.8s）")
    parser.add_argument("--think-ms", type=int, default=100,
                        help="mock 的 audio_end→首事件延时")
    parser.add_argument("--pace-pct", type=int, default=80,
                        help="mock 真实 MP3 节奏（帧时长百分比）。mock 缺省 100=实时；本脚本用 80 "
                             "让发送略快于播放、给端侧留小缓冲——实测 macOS asyncio.sleep 每帧超调 "
                             "约 1.7ms（≈7%%），100%% 会产生假欠载")
    parser.add_argument("--material-repeats", type=int, default=3,
                        help="素材拼接份数（3×1.6s≈4.8s）")
    parser.add_argument("--kbps", type=int, default=32, help="MP3 码率（24k 单声道；32kbps≈96B/帧）")
    parser.add_argument("--heap-rounds", type=int, default=10, help="堆稳定场景的轮数")
    parser.add_argument("--stall-ms", type=int, default=600, help="stall 停发毫秒数（> 端侧预缓冲）")
    parser.add_argument("--stall-at", type=int, default=30, help="stall 帧序号（0 基）")
    parser.add_argument("--skip-heap", action="store_true", help="跳过 10 轮堆稳定场景")
    parser.add_argument("--skip-abort", action="store_true", help="跳过 abort 停声场景")
    parser.add_argument("--skip-stall", action="store_true", help="跳过 stall 欠载场景")
    parser.add_argument("--skip-late", action="store_true", help="跳过窗口外帧场景")
    parser.add_argument("--skip-synthetic", action="store_true", help="跳过合成帧降级场景")
    parser.add_argument("--skip-scenarios", action="store_true",
                        help="只跑正常轮 + abort + 堆稳定（不重启 mock）")
    parser.add_argument("--log", default="", help="把完整串口日志写到该文件")
    parser.add_argument("--dry-run", action="store_true", help="只生成素材/参考解码，不连设备")
    args = parser.parse_args()

    if not 1 <= args.pace_pct <= 400:
        raise SystemExit("--pace-pct 必须在 1~400")
    if args.heap_rounds < 1:
        raise SystemExit("--heap-rounds 必须 ≥ 1")
    if args.material_repeats < 1:
        raise SystemExit("--material-repeats 必须 ≥ 1")

    try:
        return run(args)
    except KeyboardInterrupt:
        print("\n中断（Ctrl-C）")
        return 130


if __name__ == "__main__":
    sys.exit(main())
