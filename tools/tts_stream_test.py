#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["pyserial>=3.5", "lameenc>=1.8", "av>=12", "numpy"]
# ///
"""T2.4/T3.2 下行流式播放真机自测（真实 MP3 经 WS 0x02 下发 → 端侧流式解码播放）。

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
    uv run tools/tts_stream_test.py --dry-run          # 只生成素材 + PyAV 参考 + 自检，不碰设备
    uv run tools/tts_stream_test.py --no-reset         # 设备已在跑
    uv run tools/tts_stream_test.py --skip-heap        # 跳过 10 轮堆稳定（快速冒烟）
    uv run tools/tts_stream_test.py --log /tmp/tts_stream.log
    uv run tools/tts_stream_test.py --self-test        # 离线自检 T3.2 解析/断言逻辑（不连设备）
    uv run tools/tts_stream_test.py --interrupt        # 附加：播放中按键打断（T3.2 R4）+ 回归轮次
    uv run tools/tts_stream_test.py --r5               # 附加：正常收尾轮不产生 discard（T3.2 R5）
    uv run tools/tts_stream_test.py --r3-duck          # 附加：音乐 ducking 探针（T3.2 R3）
    uv run tools/tts_stream_test.py --prompt-r1        # 附加：提示音 R1 原因位（PROMPT=0x1，需外放）

T3.2（半双工 R1–R5）附加场景（**可选，默认不跑**；依赖 Lead 的 T3.2 固件契约，见 README）：
    --interrupt ：用 `vs start` 起一轮（**不能是 `vc start`**：它不经过 voice_session，VS 会停在
                  IDLE，`evt post BUTTON` 会被当成"按键起一轮"而不是打断），等 TTS 播放中且
                  `vs status == SPEAKING` 后经串口发 `evt post BUTTON`（短按）打断，断言打断链路日志
                  （`AFE: feed paused (holds=0x` / `VS: 打断当前会话` /
                  `PLAYER: fade out <ms> ms` / `VC: interrupt sent sid=` /
                  `AFE: feed resumed (discard 50 ms` / `VS: state SPEAKING -> IDLE（原因=interrupt）`）、
                  mock 收到 interrupt（计数=1）且**不再下发 tts_end**、`player stat` 的
                  `duck: ... last_stop_ms=<n>` <100ms、`mic stat` 的 `门控: ... discard_samples`
                  ≈800（打断前先 `mic reset`，50ms@16k，±1 帧=320）+ hold_mask=0x0 + 收音开启；
                  随后再跑一轮正常播放验证回归未被破坏（并断言 R5：该轮不产生 discard）。
    --r5        ：正常收尾轮不产生 discard（R5）：`mic reset` 后 discard_samples==0、
                  hold_mask=0x0、收音恢复「开启」、R1/R2 的 hold/release 计数各 ≥1。
    --r3-duck   ：R3 音乐 ducking 探针（`player duck on|off`）：恒定幅度 1kHz MP3 播放中，
                  duck 后 `out_peak` ≈0.25× 基线（0.15~0.45），duck off 后恢复 ≥0.7×。
    --prompt-r1 ：T3.2「统一门控」直接证据（R1 原因位）：afplay lexin.wav 唤醒 → 提示音期间
                  `hold_mask=0x1`（仅 PROMPT 位）+ 收音暂停 → `prompt done` 后 `hold_mask=0x0`；
                  与 `--interrupt` 的 `hold_mask=0x2`（仅 TTS 位）对照，证明两路 owner 走同一个
                  `audio_fe` 仲裁器、互不覆盖（旧实现是两套独立开关，会互相顶掉）。
                  **会外放**，且临时把系统音量调到 `--volume`（默认 50，结束恢复）。
    固件未就绪（缺 `mic stat` 的「门控」行 / `player stat` 的 duck+stop 行）时这些场景会记 FAIL，
    基础 6 个场景不受影响；新增解析逻辑可先用 `--self-test`/`--dry-run` 离线验证。

约束：脚本**独占串口**（先关 `idf.py monitor`）；**默认不改系统音量、不外放**（T2.4 场景
不依赖 Mac 扬声器回放）；只有 `--prompt-r1` 例外：它用 `afplay tools/lexin.wav` 外放唤醒，
会临时把系统音量调到 `--volume`（默认 50=用户定稿上限，结束/异常都恢复）。所有 player 探针
依赖 `CONFIG_DIAG_ENABLE_PLAYER_TEST`（`player stat` 的 stream 行由 T2.4-A 常驻提供）。
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from typing import Callable

HERE = os.path.dirname(os.path.abspath(__file__))

# 复用既有工具封装，避免多套实现漂移（这些模块无 import 副作用）
from voice_test import (  # noqa: E402  （PEP 723 脚本按脚本目录入 sys.path）
    Device,
    Result,
    detect_port,
    error_lines,
    get_system_volume,
    read_wav_info,
    set_system_volume,
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
    vs_status,
    wait_line,
)
from mp3_test import (  # noqa: E402  （PyAV 独立解码参考 + T2.3 的上传/播放辅助）
    RE_JOB_DONE,
    RE_PLAY_START,
    RE_PLAYERPLAY,
    gen_tone_mp3,
    reference_decode,
    upload_mp3,
)

MOCK_SCRIPT = os.path.join(HERE, "ws_mock_server.py")
SOURCE_WAV = "今天天气如何.wav"  # 24 kHz / 16 bit / mono，1.6s
LEXIN_WAV = "lexin.wav"        # 唤醒词"嗨，乐鑫"，--prompt-r1 用（Mac 扬声器回放）
DEFAULT_MOCK_PORT = 8765
TTS_RATE = 24000

# T3.2 统一门控原因位（与 components/audio_fe/include/audio_fe.h 的 AUDIO_FE_HOLD_* 一致）
HOLD_PROMPT = 0x1  # AUDIO_FE_HOLD_PROMPT：唤醒应答提示音在播（R1）
HOLD_TTS = 0x2     # AUDIO_FE_HOLD_TTS：云端 TTS 在播（R1）

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

# ---- T3.2（半双工 R1–R5）可观测契约（Lead 2026-11-01 细化版；正则容错，不绑死完整行） ----
# mic stat 追加行（累计值）：门控: holds=<n> releases=<n> hold_mask=0x<hex>
#                              discards=<n> discard_samples=<n> last_discard_ms=<n>
# 注：旧稿前缀是 feed:，两种前缀都认；`mic reset` 会把 holds/releases/discards/
#     discard_samples/last_discard_ms 一起清零（hold_mask 是状态，不清）。
RE_GATE_LINE = re.compile(
    r"(?:门控|feed):\s*holds=(\d+)\s+releases=(\d+)\s+hold_mask=(0x[0-9a-fA-F]+|\d+)\s+"
    r"discards=(\d+)\s+discard_samples=(\d+)\s+last_discard_ms=(\d+)"
)
# 宽松版：固件迭代期尾部字段缺失时只记 WARN（不把「字段还没加」误判成「值为 0」）
RE_GATE_LINE_LOOSE = re.compile(
    r"(?:门控|feed):\s*holds=(\d+)\s+releases=(\d+)\s+hold_mask=(0x[0-9a-fA-F]+|\d+)"
)
# player stat 追加行：duck: on=<0|1> gain_q8=<n> out_peak=<n> stop: last_stop_ms=<n> stops=<n>
#（早期草稿字段名是 last_req_to_tx_off_ms，两个名字都认；last_stop_ms=淡出请求→输出静音，R4 断言 <100）
RE_DUCK_LINE = re.compile(
    r"duck:\s*on=(\d+)\s+gain_q8=(\d+)\s+out_peak=(\d+).*?"
    r"(?:last_stop_ms|last_req_to_tx_off_ms)=(\d+)\s+stops=(\d+)"
)
RE_DUCK_LINE_LOOSE = re.compile(r"duck:\s*on=(\d+)\s+gain_q8=(\d+)\s+out_peak=(\d+)")
# 打断链路日志（R4：AFE 门控 / VS / PLAYER 淡出 / VC interrupt 报文）
RE_AFE_FEED_PAUSED = re.compile(r"AFE: feed paused")
RE_AFE_FEED_PAUSED_HOLDS = re.compile(r"AFE: feed paused[^\n]*holds=0x", re.I)
RE_AFE_FEED_PAUSED_MASK = re.compile(r"AFE: feed paused[^\n]*holds=0x([0-9a-fA-F]+)", re.I)
RE_AFE_FEED_RESUMED = re.compile(r"AFE: feed resumed")
RE_AFE_FEED_RESUMED_DISCARD = re.compile(r"AFE: feed resumed[^\n]*discard (\d+) ms", re.I)
RE_PLAYER_FADE_OUT = re.compile(r"PLAYER: fade out (\d+) ms")
RE_VC_INTERRUPT_SENT = re.compile(r"interrupt sent sid=(\d+)")
RE_VS_INTERRUPT_LOG = re.compile(r"VS: 打断当前会话")
RE_VS_SPEAKING_IDLE = re.compile(r"VS: state\s+SPEAKING\s*->\s*IDLE")
RE_VS_INTERRUPT_REASON = re.compile(
    r"VS: state\s+SPEAKING\s*->\s*IDLE[^\n]*(?:interrupt|button|btn|按键|user)", re.I
)
# `evt post BUTTON` 回显：不同固件措辞可能不同，只把「被拒绝」当失败（见 post_button_event）
# 唤醒提示音播完（voice_session / 旧 APP 前缀）；--prompt-r1 的 R2 证据
RE_PROMPT_DONE = re.compile(r"(?:APP|VS): prompt done, feed resumed")
RE_EVT_POST_ACK = re.compile(r"已投递[^\n]*BUTTON")
RE_EVT_POST_REJECT = re.compile(r"未知事件|投递失败|用法: evt post")
# mock 侧日志：收到 interrupt / 取消下发 / 真的发了 tts_end（断言打断后不得出现）
RE_MOCK_INTERRUPT = re.compile(r"<-- interrupt sid=(\d+)")
RE_MOCK_TTS_CANCELLED = re.compile(r"下发被取消 sid=(\d+)")
RE_MOCK_TTS_END_SEND = re.compile(r"--> tts_end sid=(\d+)")

# R4：恢复收音时丢弃 50ms@16k≈800 样本（容差 ±1 帧=320 样本）
DISCARD_EXPECT_SAMPLES = 800
DISCARD_TOL_SAMPLES = 320
# R4 停声证据：淡出请求 → 输出静音的耗时必须 <100ms（80ms 淡出 + ≤10ms 块）
STOP_MS_MAX = 100.0
# R3 ducking：duck on 后输出峰值应为基线的 0.25 倍量级（实测允许 0.15~0.45）
DUCK_RATIO_MIN = 0.15
DUCK_RATIO_MAX = 0.45
DUCK_RECOVER_RATIO = 0.7

# console 命令结束哨兵（与 voice_cloud_test / diag 现有输出一致）
END_PLAYER_STAT = RE_STREAM_LINE
END_PLAYER_RESET = re.compile(r"PLAYERSTAT cleared")
END_WS_RECONNECT = re.compile(r"已请求重连")
# `vs start`（T3.1）：起一轮并让 voice_session 进入 LISTENING（R4 打断场景必须先有会话状态）
END_VS_START = re.compile(r"已请求开始一轮|开始会话失败")


# ---------------------------------------------------------------- T3.2 解析与判定
# 这些纯函数（不碰串口）同时被真机场景与 --self-test 使用，保证离线自检的就是真机断言逻辑。


GATE_KEYS = (
    "holds", "releases", "hold_mask", "discards", "discard_samples", "last_discard_ms",
)
DUCK_KEYS = ("on", "gain_q8", "out_peak", "last_stop_ms", "stops")


def _to_int(text: str) -> int:
    return int(text, 16) if text.lower().startswith("0x") else int(text)


def parse_gate_line(line: str) -> dict | None:
    """解析 `mic stat` 的 T3.2 feed 行；字段不全时退到宽松版（缺失字段为 None）。"""
    m = RE_GATE_LINE.search(line)
    if m:
        return dict(zip(GATE_KEYS, (_to_int(group) for group in m.groups())))
    m = RE_GATE_LINE_LOOSE.search(line)
    if m:
        out = {key: None for key in GATE_KEYS}
        out.update({
            "holds": int(m.group(1)),
            "releases": int(m.group(2)),
            "hold_mask": _to_int(m.group(3)),
        })
        return out
    return None


def parse_duck_line(line: str) -> dict | None:
    """解析 `player stat` 的 T3.2 duck/stop 行；缺尾部字段时退到宽松版。"""
    m = RE_DUCK_LINE.search(line)
    if m:
        return dict(zip(DUCK_KEYS, (int(group) for group in m.groups())))
    m = RE_DUCK_LINE_LOOSE.search(line)
    if m:
        out = {key: None for key in DUCK_KEYS}
        out.update({
            "on": int(m.group(1)),
            "gain_q8": int(m.group(2)),
            "out_peak": int(m.group(3)),
        })
        return out
    return None


def discard_delta_ok(
    delta: int,
    expect: int = DISCARD_EXPECT_SAMPLES,
    tol: int = DISCARD_TOL_SAMPLES,
) -> bool:
    """R4：恢复收音丢弃的样本数应 ≈800（50ms@16k），容差 ±1 帧（320 样本）。"""
    return abs(delta - expect) <= tol


def no_discard_ok(delta: int) -> bool:
    """R5：正常收尾轮不得产生 discard（增量必须为 0）。"""
    return delta == 0


def stop_ms_ok(value: int | None, limit: float = STOP_MS_MAX) -> bool:
    """R4 停声证据：淡出请求 → 关 TX 的耗时必须严格小于上限（默认 100ms）。"""
    return value is not None and value < limit


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


def console_until(
    dev: Device, cmd: str, done: Callable[[str], bool], timeout: float = 3.0
) -> list[str]:
    """发 console 命令并收集输出，直到 `done(已收到的文本)` 为真或超时。

    为什么不用固定结束行：T3.2 的 feed/duck 追加行可能插在旧结束行（`参考:` / `stream:`）
    之前或之后，固定哨兵会提前返回、漏掉新行。这里按「需要的行都出现了」判定，
    同时不假设字段顺序；旧固件缺行时最多等满 timeout（只影响新增的可选场景）。
    """
    mark = dev.mark()
    dev.send(cmd)
    end = time.time() + timeout
    while time.time() < end:
        text = "\n".join(line for _, line in dev.lines_since(mark))
        if done(text):
            break
        dev.pump(0.05)
    return [line for _, line in dev.lines_since(mark)]


def mic_gate_stat(dev: Device, timeout: float = 3.0) -> dict:
    """`mic stat` + T3.2 的「门控」行（holds/releases/hold_mask/discards/discard_samples）。

    返回 {"line": 门控行 dict|None, "feed": "开启"|"暂停"|None, "raw": [行]}；
    门控行缺失（旧固件）时 line=None，由调用方记 FAIL（契约未就绪）。
    不做固定结束行匹配：门控行可能插在旧的 `参考:` 行之前或之后。
    """
    out = console_until(
        dev,
        "mic stat",
        lambda text: ("门控:" in text or "feed:" in text) and "参考:" in text,
        timeout=timeout,
    )
    stat: dict = {"line": None, "feed": None, "raw": out}
    for line in out:
        m = re.search(r"状态: 收音 (开启|暂停)", line)
        if m:
            stat["feed"] = m.group(1)
        parsed = parse_gate_line(line)
        if parsed is not None:
            stat["line"] = parsed
    return stat


def reset_mic_stats(dev: Device, result: Result, label: str) -> dict:
    """`mic reset` 清零 T3.2 门控统计（holds/releases/discards/discard_samples/last_discard_ms），
    返回清零后的 `mic stat`（用于 R4/R5 的**绝对值**断言，不依赖轮次之间的 delta）。"""
    ok = dev.mic_reset()
    result.check(ok, f"{label}: mic reset 清零门控/丢弃统计")
    stat = mic_gate_stat(dev)
    if stat["line"] is not None:
        print(f"  INFO mic reset 后门控: {stat['line']}")
    return stat


def player_stop_stat(dev: Device, timeout: float = 3.0) -> dict:
    """`player stat` + T3.2 的 duck/stop 行（stream 行顺带解析，不依赖行序）。"""
    out = console_until(
        dev, "player stat", lambda text: "duck:" in text and "PLAYERSTAT" in text, timeout=timeout
    )
    stat: dict = {"duck": None, "stream": None, "raw": out}
    for line in out:
        parsed = parse_duck_line(line)
        if parsed is not None:
            stat["duck"] = parsed
        m = RE_STREAM_LINE.search(line)
        if m:
            stat["stream"] = {key: int(value) for key, value in zip(STREAM_KEYS, m.groups())}
    return stat


def post_button_event(dev: Device, timeout: float = 1.5) -> list[str]:
    """串口投递 BUTTON（短按）事件，返回命令输出 + 期间设备日志。

    不断言回显措辞（固件可能改成 `已注入按键事件（短按）` 等）：只把「未知事件/投递失败」
    当失败；回显缺失只 WARN，真正的判定看后续打断链路日志。
    """
    def done(text: str) -> bool:
        return any(
            key in text for key in ("已投递", "未知事件", "投递失败", "用法: evt post")
        )

    return console_until(dev, "evt post BUTTON", done, timeout=timeout)


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


def begin_vs_round(dev: Device, result: Result, label: str) -> tuple[int, int]:
    """`vs start` 起一轮（voice_session 进 LISTENING → vc 进 UPLINK）。

    为什么 R4 打断必须走这条路：`vc start` 是 T2.4 的确定性播放探针，**不经过
    voice_session**，此时 VS 一直是 IDLE；而 `evt post BUTTON` 在 IDLE 下会被
    vs_on_app_evt 当成「按键开始对话」→ 起新轮（旧轮 new-round abort），根本走不到
    `vs_interrupt()`（R4）。所以打断场景用 `vs start` 让 VS 真的进 SPEAKING。
    """
    mark = dev.mark()
    dev.console("vs start", END_VS_START, timeout=3.0)
    line = wait_line(dev, RE_ROUND_START, 5.0, mark)
    if line is None:
        result.check(False, f"{label}: vs start 被接受（收到 round start）")
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


def check_gate_line(result: Result, label: str, gate: dict) -> bool:
    """断言 `mic stat` 有 T3.2 门控行（没有就说明契约未就绪，后续门控断言无意义）。"""
    ok = gate.get("line") is not None
    result.check(
        ok,
        f"{label}: mic stat 有 T3.2 门控行（holds/releases/hold_mask/discard_samples，"
        "累计值）",
    )
    if not ok:
        print(
            "    ! 契约缺失：`mic stat` 未输出「门控:」行（T3.2 固件未就绪？）"
            "——R4/R5 的 discard 断言无法判定，其余场景照常"
        )
    return ok


def check_no_discard(
    result: Result, label: str, after: dict, *, expect_r1r2: bool = True
) -> None:
    """R5：`mic reset` 后跑一轮正常收尾，不得产生 discard（绝对值口径）。

    Lead 契约：`mic reset` 会把 holds/releases/discards/discard_samples/last_discard_ms
    一起清零，所以这里直接用**绝对值**判定（比轮次间 delta 更硬，也不受前面场景残留影响）：
    - discard_samples == 0（正常收尾不得丢弃 50ms）；
    - hold_mask == 0x0（R2 已释放，没有悬挂的门控持有者）；
    - 收音「开启」；
    - R1/R2 的 hold/release 计数各 ≥1（证明门控真的被计数，不是只打印字段）。
    """
    if after.get("line") is None:
        result.check(False, f"{label}: 能读到 mic stat 的门控行（R5 判定前提）")
        return
    cur = after["line"]
    if cur.get("discard_samples") is None:
        result.check(False, f"{label}: 门控行含 discard_samples（R5 判定前提）")
        return
    result.check(
        no_discard_ok(cur["discard_samples"]),
        f"{label}: R5 正常收尾不丢弃样本（mic reset 后 discard_samples="
        f"{cur['discard_samples']} == 0）",
    )
    result.check(
        cur["hold_mask"] == 0,
        f"{label}: 收尾后 hold_mask=0x0（实际 0x{cur['hold_mask']:x}）",
    )
    result.check(
        after.get("feed") == "开启",
        f"{label}: 收尾后收音恢复（实际 {after.get('feed')}）",
    )
    if expect_r1r2:
        result.check(
            cur["holds"] >= 1 and cur["releases"] >= 1,
            f"{label}: R1/R2 门控计数递增（mic reset 后 holds={cur['holds']} / "
            f"releases={cur['releases']}，均 ≥1）",
        )


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


def scenario_interrupt(
    dev: Device, result: Result, ref: dict, mp3_bytes: int, args: argparse.Namespace, mock: Mock
) -> None:
    """T3.2 R4：真实 MP3 播放中经串口发 `evt post BUTTON`（短按）打断，断言整条打断链路；
    随后再跑一轮正常播放（回归 + R5 不产生 discard）。

    契约（Lead 2026-11-01 细化）：
    - 设备日志：`AFE: feed paused (holds=0x` / `VS: 打断当前会话` / `PLAYER: fade out <ms> ms` /
      `VC: interrupt sent sid=` / `AFE: feed resumed (discard 50 ms` /
      `VS: state SPEAKING -> IDLE（原因=interrupt）`；
    - 停声证据：`player stat` 的 `duck: ... last_stop_ms=<n>` < 100ms（淡出请求→输出静音）；
    - 丢帧证据：**打断前先 `mic reset`**（契约：清零 holds/releases/discards/discard_samples，
      hold_mask 是状态不清），打断后 `mic stat` 的 `discard_samples` ≈800（50ms@16k，±1 帧=320）、
      hold_mask=0x0、收音「开启」；
    - mock：收到 `interrupt sid=`（计数=1）、取消进行中的下发、**不再发 tts_end**；
    - 回归：再跑一轮正常播放（tts_end 排空/解码对拍/无欠载），且该轮 R5 discard == 0。

    注意：`evt post BUTTON` 若在 IDLE 会被 voice_session 当成"按键起一轮"（旧轮 new-round
    abort），所以本场景用 `vs start` 起轮（vc start 不经过 voice_session、VS 一直 IDLE），
    并在发送前断言 tts_start + 已解码帧数 + stream active=1 + `vs status == SPEAKING`。
    """
    label = "interrupt"
    print("\n===== 附加场景 A：播放中按键打断（T3.2 R4：evt post BUTTON）=====")
    reset_stats(dev)
    before = mic_gate_stat(dev)
    have_gate = check_gate_line(result, label, before)
    if have_gate:
        print(f"  INFO 播放前门控: {before['line']}")

    sid, mark = begin_vs_round(dev, result, label)
    if sid < 0:
        return
    dev.console("vc stop", END_VC_STOP, timeout=2.0)
    tts_start = wait_line(dev, RE_TTS_START, 10.0, mark)
    result.check(tts_start is not None, f"{label}: 进入 TTS 播放（tts_start）")
    # 等真的在放再打断（否则可能打在预缓冲阶段，打断链路/停声耗时都不可比）
    decoded = 0
    deadline = time.time() + 8.0
    while time.time() < deadline:
        stream = player_stat(dev).get("stream")
        decoded = int(stream["decoded"]) if stream else 0
        if decoded >= args.interrupt_after_frames:
            break
        dev.pump(0.02)
    result.check(
        decoded >= args.interrupt_after_frames,
        f"{label}: 打断前已解码 {decoded} 帧（≥{args.interrupt_after_frames}，确认真在播放）",
    )
    playing = player_stat(dev).get("stream") or {}
    result.check(playing.get("active") == 1, f"{label}: 打断前 stream active=1（播放中）")
    vs_state = vs_status(dev).get("state")
    result.check(
        vs_state == "SPEAKING",
        f"{label}: 打断前 VS 状态=SPEAKING（实际 {vs_state}；IDLE 下 BUTTON 会被当成起新轮）",
    )

    # ---- T3.2 统一门控直接证据：TTS 播放中「只有 TTS 位」被置起（不是旧的两套独立开关） ----
    if have_gate:
        playing_gate = mic_gate_stat(dev)
        if playing_gate["line"] is None:
            result.check(False, f"{label}: 播放中能读到 mic stat 门控行")
        else:
            result.check(
                playing_gate["line"]["hold_mask"] == HOLD_TTS,
                f"{label}: 播放中 hold_mask=0x{playing_gate['line']['hold_mask']:x} "
                f"== 0x{HOLD_TTS:x}（只有 TTS 位，R1 经 audio_fe 仲裁器生效）",
            )
            result.check(
                playing_gate.get("feed") == "暂停",
                f"{label}: 播放中 mic stat 收音暂停（实际 {playing_gate.get('feed')}）",
            )
    paused_line = wait_line(dev, RE_AFE_FEED_PAUSED, 2.0, mark)
    result.check(paused_line is not None, f"{label}: 日志 AFE: feed paused（R1 暂停收音）")
    if paused_line is not None:
        mask_match = RE_AFE_FEED_PAUSED_MASK.search(paused_line)
        result.check(
            mask_match is not None and int(mask_match.group(1), 16) == HOLD_TTS,
            f"{label}: R1 暂停行 holds=0x{HOLD_TTS:x}（TTS 位，"
            f"{paused_line.strip()}）",
        )

    # 清零：mic reset 只清计数不清 hold_mask，R4 的 discard 用绝对值口径
    gate_base = reset_mic_stats(dev, result, label)
    if have_gate and gate_base["line"] is not None:
        result.check(
            gate_base["line"]["discard_samples"] == 0,
            f"{label}: mic reset 后 discard_samples=0（R4 用绝对口径的前提）",
        )

    # ---- 触发按键打断（记录 mark：之后不允许出现 tts_end） ----
    interrupt_mark = dev.mark()
    mock_round_mark = mock.mark()
    ack_lines = post_button_event(dev)
    ack_text = "\n".join(ack_lines)
    if RE_EVT_POST_REJECT.search(ack_text):
        result.check(False, f"{label}: evt post BUTTON 被控制台拒绝（{ack_text.strip()[:160]}）")
    elif RE_EVT_POST_ACK.search(ack_text):
        result.check(True, f"{label}: evt post BUTTON 已被控制台接受")
    else:
        result.warn(
            f"{label}: evt post BUTTON 无回显（固件可能改了措辞；按后续打断链路日志判定）"
        )

    # ---- 设备侧打断链路日志 ----
    ic_line = wait_line(dev, RE_VC_INTERRUPT_SENT, args.interrupt_timeout, interrupt_mark)
    ic_sid = int(RE_VC_INTERRUPT_SENT.search(ic_line).group(1)) if ic_line else None
    result.check(
        ic_line is not None, f"{label}: 日志 VC: interrupt sent sid=（实际 sid={ic_sid}）"
    )
    if ic_sid is not None:
        result.check(
            ic_sid == sid, f"{label}: interrupt 上行 sid={ic_sid} == 本轮 sid={sid}"
        )

    interrupt_log = wait_line(dev, RE_VS_INTERRUPT_LOG, 3.0, interrupt_mark)
    result.check(interrupt_log is not None, f"{label}: 日志 VS: 打断当前会话（会话层打断）")

    fade_line = wait_line(dev, RE_PLAYER_FADE_OUT, 3.0, interrupt_mark)
    fade_ms = int(RE_PLAYER_FADE_OUT.search(fade_line).group(1)) if fade_line else None
    result.check(fade_line is not None, f"{label}: 日志 PLAYER: fade out <ms> ms（淡出停声请求）")
    if fade_ms is not None:
        result.check(0 < fade_ms <= 500, f"{label}: fade out {fade_ms} ms ∈ (0, 500]")

    vs_line = wait_line(dev, RE_VS_SPEAKING_IDLE, 5.0, interrupt_mark)
    result.check(vs_line is not None, f"{label}: 日志 VS: state SPEAKING -> IDLE（打断收尾）")
    if vs_line is not None:
        if RE_VS_INTERRUPT_REASON.search(vs_line):
            result.check(True, f"{label}: 迁移原因含 interrupt/button（{vs_line.strip()}）")
        else:
            result.warn(
                f"{label}: 迁移行未含 interrupt/button 关键词（契约写的是 原因=interrupt）："
                f"{vs_line.strip()}"
            )

    resumed_line = wait_line(dev, RE_AFE_FEED_RESUMED, 6.0, interrupt_mark)
    result.check(resumed_line is not None, f"{label}: 日志 AFE: feed resumed（R2 恢复收音）")
    if resumed_line is not None:
        discard_ms = RE_AFE_FEED_RESUMED_DISCARD.search(resumed_line)
        if discard_ms is not None:
            result.check(
                20 <= int(discard_ms.group(1)) <= 100,
                f"{label}: 恢复行 R5 丢弃 {discard_ms.group(1)} ms ≈ 50 ms（20~100）",
            )
        else:
            result.warn(
                f"{label}: 恢复行未带 discard <n> ms（契约写的是 discard 50 ms）："
                f"{resumed_line.strip()}"
            )
    result.check(wait_idle(dev, 6.0), f"{label}: 打断后 vc 回到 IDLE")

    stream = wait_stream_idle(dev)
    if stream is None:
        result.check(False, f"{label}: player stat 有 stream 行（T2.4-A 是否就绪？）")
    else:
        result.check(stream["active"] == 0, f"{label}: 播放器已停止（stream active=0）")

    # ---- 停声设备侧证据：淡出请求 → 输出静音的耗时 ----
    stop_stat = player_stop_stat(dev)
    duck = stop_stat["duck"]
    result.check(duck is not None, f"{label}: player stat 有 T3.2 duck/stop 行")
    if duck is not None:
        stop_ms = duck.get("last_stop_ms")
        result.check(
            stop_ms_ok(stop_ms, args.stop_ms_max),
            f"{label}: last_stop_ms={stop_ms} < {args.stop_ms_max:.0f} ms"
            "（淡出请求→输出静音：80ms 淡出=3840 帧，实测 79~95ms，只设上界）",
        )
        result.check(
            (duck.get("stops") or 0) >= 1,
            f"{label}: stops={duck.get('stops')} ≥1（淡出停止真的执行过，"
            "避免 last_stop_ms=0 的假通过）",
        )
        print(
            f"  INFO stop: last_stop_ms={stop_ms} stops={duck.get('stops')} "
            f"duck on={duck.get('on')} gain_q8={duck.get('gain_q8')} "
            f"out_peak={duck.get('out_peak')}"
        )

    # ---- 设备侧：打断后不再收到 tts_end（mock 已取消下发） ----
    dev_lines = lines_since(dev, interrupt_mark)
    late_tts_end = [line for line in dev_lines if RE_TTS_END.search(line)]
    result.check(
        not late_tts_end, f"{label}: 打断后不再收到 tts_end（实际 {len(late_tts_end)} 条）"
    )

    # ---- mock 侧：收到 interrupt（计数=1）/ 取消下发 / 不再发 tts_end ----
    if mock.owned:
        mock_text = mock.text_since(mock_round_mark)
        interrupt_hits = RE_MOCK_INTERRUPT.findall(mock_text)
        result.check(
            len(interrupt_hits) == 1,
            f"{label}: mock 收到 interrupt 且计数=1（实际 {len(interrupt_hits)}）",
        )
        if interrupt_hits:
            result.check(
                int(interrupt_hits[0]) == sid,
                f"{label}: mock 侧 interrupt sid={interrupt_hits[0]} == 本轮 sid={sid}",
            )
        result.check(
            RE_MOCK_TTS_CANCELLED.search(mock_text) is not None,
            f"{label}: mock 确认进行中的下发被取消",
        )
        result.check(
            RE_MOCK_TTS_END_SEND.search(mock_text) is None,
            f"{label}: mock 打断后未下发 tts_end",
        )
    else:
        result.warn(f"{label}: mock 非脚本拉起，跳过 mock 侧 interrupt/tts_end 断言")

    # ---- mic stat：R2 恢复 + R4 丢弃 ≈800 样本（mic reset 后绝对值口径） ----
    dev.pump(0.3)  # Lead 契约：打断后等 300ms 再读
    after = mic_gate_stat(dev)
    if have_gate and after["line"] is not None:
        result.check(
            after.get("feed") == "开启",
            f"{label}: 打断后收音开启（实际 {after.get('feed')}）",
        )
        result.check(
            after["line"]["hold_mask"] == 0,
            f"{label}: 打断后 hold_mask=0x0（实际 0x{after['line']['hold_mask']:x}）",
        )
        if after["line"].get("discard_samples") is not None:
            discard = after["line"]["discard_samples"]
            result.check(
                discard_delta_ok(discard),
                f"{label}: R4 恢复收音丢弃样本 {discard} ≈ {DISCARD_EXPECT_SAMPLES}"
                f"（±{DISCARD_TOL_SAMPLES} = ±1 帧@16k，50ms）",
            )
            result.check(
                after["line"]["releases"] >= 1,
                f"{label}: mic reset 后 releases={after['line']['releases']} ≥1"
                f"（R2 释放有计数；holds 因 reset 在 hold 之后清零，只打印不断言）",
            )
            print(
                f"  INFO 打断后门控: {after['line']}"
                f"（holds={after['line']['holds']} releases={after['line']['releases']}，"
                f"discards={after['line']['discards']}，"
                f"last_discard_ms={after['line'].get('last_discard_ms')}）"
            )
        else:
            result.check(False, f"{label}: 门控行含 discard_samples（R4 判定前提）")
    elif have_gate:
        result.check(False, f"{label}: 打断后能读到 mic stat 的门控行")

    # ---- 回归：打断后再跑一轮正常播放（R5：正常收尾不得产生 discard） ----
    print("\n----- 打断后回归：再跑一轮正常播放（同时断言 R5 无 discard）-----")
    reset_stats(dev)
    reset_mic_stats(dev, result, label)
    regress_mock_mark = mock.mark()
    sid2, mark2, info2 = run_mock_round(dev, result, "interrupt-regress", args.round_timeout)
    if sid2 < 0:
        return
    result.check(
        info2.get("tts_frames") == ref["frames"],
        f"interrupt-regress: tts_end 计数 {info2.get('tts_frames')} == 素材 {ref['frames']} 帧",
    )
    check_stream_round(dev, result, "interrupt-regress", ref, mark2, max_underrun=0)
    # R2 的 feed release 在「流收尾 + 300ms 尾部延时」后的 done 回调里；先等 resumed 行，
    # 否则直接读 mic stat 可能读到 hold_mask 仍置位（不是 R5 缺陷，是读取时机太早）。
    wait_line(dev, RE_AFE_FEED_RESUMED, 3.0, mark2)
    check_no_discard(result, "interrupt-regress", mic_gate_stat(dev))
    if mock.owned:
        check_mock_round(
            result, "interrupt-regress", mock.text_since(regress_mock_mark), ref, mp3_bytes
        )
    check_health(result, label, lines_since(dev, mark))


def scenario_r5(
    dev: Device, result: Result, ref: dict, mp3_bytes: int, args: argparse.Namespace, mock: Mock
) -> None:
    """T3.2 R5：正常收尾轮不产生 discard（独立场景，也可由 --interrupt 的回归轮覆盖）。

    口径：先 `mic reset` 清零门控/丢弃统计 → 跑一轮正常播放 → `mic stat` 断言
    discard_samples == 0、hold_mask=0x0、收音「开启」、hold/release 计数各 ≥1。
    """
    label = "r5"
    print("\n===== 附加场景 B：正常收尾轮不产生 discard（T3.2 R5）=====")
    reset_stats(dev)
    base = reset_mic_stats(dev, result, label)
    if not check_gate_line(result, label, base):
        return
    result.check(
        base["line"]["discard_samples"] == 0,
        f"{label}: mic reset 后 discard_samples=0（R5 绝对口径前提）",
    )
    mock_mark = mock.mark()
    sid, mark, info = run_mock_round(dev, result, label, args.round_timeout)
    if sid < 0:
        return
    result.check(
        info.get("tts_frames") == ref["frames"],
        f"{label}: tts_end 计数 {info.get('tts_frames')} == 素材 {ref['frames']} 帧",
    )
    check_stream_round(dev, result, label, ref, mark, max_underrun=0)
    # 同上：等 R2 的 feed release（300ms 尾部延时后才回调）落地，再读门控累计值。
    wait_line(dev, RE_AFE_FEED_RESUMED, 3.0, mark)
    check_no_discard(result, label, mic_gate_stat(dev))
    if mock.owned:
        check_mock_round(result, label, mock.text_since(mock_mark), ref, mp3_bytes)
    check_health(result, label, lines_since(dev, mark))


def scenario_duck(
    dev: Device, result: Result, args: argparse.Namespace
) -> None:
    """T3.2 R3：音乐 ducking 探针（`player duck on|off`）的自动断言。

    做法（Lead 建议）：PC 侧生成恒定幅度 1kHz 音调 MP3（lameenc）→ `player load` 上传 →
    `player mp3` 播放 → 播放中读 `player stat` 的 out_peak（最近 ~320ms 输出峰值）为基线 →
    `player duck on` 等一个窗口 → out_peak 应为基线 0.25 倍量级（0.15~0.45）→
    `player duck off` 等一个窗口 → out_peak 恢复（≥0.7 倍基线）。
    需要 `CONFIG_DIAG_ENABLE_PLAYER_TEST=y`（player load/mp3/duck 探针，仅开发期）。
    """
    label = "r3-duck"
    print("\n===== 附加场景 C：R3 音乐 ducking（player duck on/off 前后 out_peak 对比）=====")
    workdir = tempfile.mkdtemp(prefix="tts_stream_duck_")
    tone_path = os.path.join(workdir, "duck_tone_24k_mono.mp3")
    gen_tone_mp3(tone_path, TTS_RATE, 1, args.duck_seconds, (1000, 1000), args.kbps)
    with open(tone_path, "rb") as handle:
        data = handle.read()
    got = upload_mp3(dev, data)
    result.check(got == len(data), f"{label}: 音调 MP3 上传完成（{got}/{len(data)} B）")
    if got != len(data):
        return

    dev.send("player duck off")  # 基线前确保 duck 关闭
    dev.pump(0.15)
    mark = dev.mark()
    try:
        dev.send("player mp3")
        result.check(
            wait_line(dev, RE_PLAYERPLAY, 5.0, mark) is not None,
            f"{label}: player mp3 被控制台接受",
        )
        result.check(
            wait_line(dev, RE_PLAY_START, 8.0, mark) is not None,
            f"{label}: 开始播放（PLAYER: play）",
        )
        dev.pump(args.duck_warmup)  # 让 out_peak 的 ~320ms 窗口填满稳态输出
        base = player_stop_stat(dev)["duck"] or {}
        result.check(
            base.get("on") == 0, f"{label}: duck 基线 on=0（实际 {base.get('on')}）"
        )
        base_peak = base.get("out_peak")
        result.check(
            base_peak is not None and base_peak > 0,
            f"{label}: 播放中输出峰值基线 {base_peak} > 0（真的有输出）",
        )

        dev.send("player duck on")
        dev.pump(args.duck_window)
        ducked = player_stop_stat(dev)["duck"] or {}
        result.check(
            ducked.get("on") == 1,
            f"{label}: player duck on 生效（player stat on=1，实际 {ducked.get('on')}）",
        )
        duck_peak = ducked.get("out_peak")
        if base_peak and duck_peak is not None:
            ratio = duck_peak / base_peak
            result.check(
                DUCK_RATIO_MIN <= ratio <= DUCK_RATIO_MAX,
                f"{label}: duck 后 out_peak={duck_peak} = {ratio:.2f}× 基线 {base_peak}"
                f"（{DUCK_RATIO_MIN}~{DUCK_RATIO_MAX}）",
            )
        else:
            result.check(False, f"{label}: duck 后能读到 out_peak（基线 {base_peak} / duck {duck_peak}）")

        dev.send("player duck off")
        dev.pump(args.duck_window)
        recovered = player_stop_stat(dev)["duck"] or {}
        result.check(
            recovered.get("on") == 0,
            f"{label}: player duck off 生效（player stat on=0，实际 {recovered.get('on')}）",
        )
        rec_peak = recovered.get("out_peak")
        if base_peak and rec_peak is not None:
            rec_ratio = rec_peak / base_peak
            result.check(
                rec_ratio >= DUCK_RECOVER_RATIO,
                f"{label}: duck off 后 out_peak={rec_peak} = {rec_ratio:.2f}× 基线 "
                f"{base_peak}（≥{DUCK_RECOVER_RATIO}）",
            )
        else:
            result.check(False, f"{label}: duck off 后能读到 out_peak")

        wait_line(dev, RE_JOB_DONE, 20.0, mark)  # 播放结束
        dev.pump(0.5)  # 等 300ms 尾部 + done 回调把 busy 归位（本地 mp3 无 feed 恢复日志）
        check_health(result, label, lines_since(dev, mark))
    finally:
        dev.send("player duck off")  # 异常退出也把探针复位，避免后续场景被 duck 影响
        dev.pump(0.1)


def _reap_afplay(proc: "subprocess.Popen | None", dev: Device, timeout: float = 8.0) -> None:
    """等 afplay 子进程结束（期间继续 pump 串口，别让串口缓冲堆积）。"""
    if proc is None:
        return
    deadline = time.time() + timeout
    while proc.poll() is None and time.time() < deadline:
        dev.pump(0.05)
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()


def scenario_prompt_r1(dev: Device, result: Result, args: argparse.Namespace) -> None:
    """T3.2 统一门控证据（R1 原因位）：唤醒提示音期间只置 PROMPT 位（0x1），播完归 0。

    与 `--interrupt` 的 TTS 位（0x2）对照，证明「提示音 / TTS」两路 owner 走同一个
    `audio_fe` 原因位仲裁器、互不覆盖（旧实现是两套独立开关，会互相把对方顶掉）。

    做法：`afplay tools/lexin.wav`（Mac 扬声器**外放**，音量临时调到 `--volume`，结束恢复）
    → 等 `AFE: feed paused` 后**紧接着**读 `mic stat`（提示音只有 1.28s，hold_mask 只在
    这期间非 0，不能用固定 sleep）→ 断言 hold_mask=0x1 / 收音暂停 → 等
    `prompt done, feed resumed` → 再读 hold_mask=0x0 / 收音开启。
    """
    label = "prompt-r1"
    print("\n===== 附加场景 D：提示音 R1 原因位（PROMPT=0x1，T3.2 统一门控）=====")
    if not wait_idle(dev, 6.0):
        result.check(False, f"{label}: 场景开始前 vc 空闲（IDLE）")
        return
    lexin = os.path.join(HERE, LEXIN_WAV)
    result.check(os.path.exists(lexin), f"{label}: 唤醒素材 {LEXIN_WAV} 存在")
    if not os.path.exists(lexin):
        return
    if shutil.which("afplay") is None:
        result.warn(f"{label}: 本机没有 afplay（非 macOS？），跳过提示音 R1 证据")
        return

    old_volume = get_system_volume()
    changed = False
    if args.volume:
        if old_volume is None:
            result.warn(f"{label}: 读不到系统音量（非 macOS？），用当前音量回放")
        elif old_volume["muted"]:
            result.warn(f"{label}: 系统输出已静音，唤醒大概率失败（脚本不改静音状态）")
        elif old_volume["volume"] < args.volume:
            try:
                set_system_volume(args.volume)
                changed = True
                print(f"  INFO 系统音量 {old_volume['volume']}% → {args.volume}%（结束恢复）")
            except Exception as exc:  # noqa: BLE001  非致命：音量失败也能跑，只是唤醒率低
                result.warn(f"{label}: 临时调系统音量失败（{exc}），用当前音量")

    proc: subprocess.Popen | None = None
    try:
        # 唤醒前必须空闲：vs_on_wake 在播放器忙时会丢弃唤醒（"播放器忙，忽略本次唤醒"）
        deadline = time.time() + 5.0
        while time.time() < deadline and player_stat(dev).get("busy"):
            dev.pump(0.1)

        paused = None
        paused_mark = 0
        for attempt in range(args.wake_retries + 1):
            mark = dev.mark()
            proc = subprocess.Popen(
                ["afplay", lexin], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
            # 唤醒 → 提示音 R1 只有 ~1.28s 窗口；日志触发后立即读，不用固定 sleep
            paused = wait_line(dev, RE_AFE_FEED_PAUSED, args.wake_timeout, mark)
            if paused is not None:
                paused_mark = mark
                break
            _reap_afplay(proc, dev)
            proc = None
            if attempt < args.wake_retries:
                print(
                    f"  INFO 第 {attempt + 1} 次回放未在 {args.wake_timeout:.1f}s 内唤醒，"
                    "重试（环境噪声/音量/距离所致）..."
                )
        result.check(
            paused is not None, f"{label}: afplay {LEXIN_WAV} 触发唤醒（AFE: feed paused）"
        )
        if paused is None:
            result.warn(
                f"{label}: 唤醒失败属环境问题（系统音量/扬声器距离/底噪），不是门控逻辑；"
                "先按 voice_test.py --volume 50 排查唤醒"
            )
            return

        # 1) 提示音播放期间：hold_mask 必须只有 PROMPT 位（0x1）
        mask_match = RE_AFE_FEED_PAUSED_MASK.search(paused)
        result.check(
            mask_match is not None and int(mask_match.group(1), 16) == HOLD_PROMPT,
            f"{label}: R1 日志 holds=0x{HOLD_PROMPT:x}（PROMPT 位，{paused.strip()}）",
        )
        mid = mic_gate_stat(dev)  # 紧接着读提示音窗口，不 sleep
        if mid["line"] is None:
            result.check(False, f"{label}: 提示音期间能读到 mic stat 门控行")
        else:
            result.check(
                mid["line"]["hold_mask"] == HOLD_PROMPT,
                f"{label}: 提示音期间 hold_mask=0x{mid['line']['hold_mask']:x} "
                f"== 0x{HOLD_PROMPT:x}（只有 PROMPT 位，统一门控生效）",
            )
            result.check(
                mid.get("feed") == "暂停",
                f"{label}: 提示音期间收音暂停（实际 {mid.get('feed')}）",
            )

        # 2) 播完：所有原因位释放，hold_mask 归 0
        _reap_afplay(proc, dev)
        proc = None
        done = wait_line(dev, RE_PROMPT_DONE, args.prompt_timeout, paused_mark)
        result.check(done is not None, f"{label}: 提示音播完 prompt done, feed resumed")
        if done is not None:
            after = mic_gate_stat(dev)
            if after["line"] is None:
                result.check(False, f"{label}: 播完后能读到 mic stat 门控行")
            else:
                result.check(
                    after["line"]["hold_mask"] == 0,
                    f"{label}: 播完 hold_mask=0x0（PROMPT 位已释放，"
                    f"实际 0x{after['line']['hold_mask']:x}）",
                )
                result.check(
                    after.get("feed") == "开启",
                    f"{label}: 播完收音恢复（实际 {after.get('feed')}）",
                )
        check_health(result, label, lines_since(dev, paused_mark))
    finally:
        _reap_afplay(proc, dev)
        if changed and old_volume is not None:
            try:
                set_system_volume(old_volume["volume"])
                print(f"  INFO 系统音量已恢复 {old_volume['volume']}%")
            except Exception as exc:  # noqa: BLE001
                print(f"  WARN 系统音量恢复失败：{exc}")


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


def run_self_test() -> bool:
    """离线自检 T3.2 新契约的解析/断言逻辑（不连串口、不需要硬件）。

    覆盖：门控行（新前缀 `门控:` + 旧草稿前缀 `feed:`）/ duck+stop 行解析、R4 discard≈800
    （±1 帧）判定、R5 discard==0 判定、停声毫秒阈值、打断链路日志正则
    （AFE 门控/VS 打断/PLAYER 淡出/VC interrupt）、mock 侧 interrupt 计数与 tts_end 正负例、
    evt post 回显容错、ducking 比值判定。正负例都断言，避免正则写死导致假 PASS。
    """
    print("\n===== --self-test：T3.2 门控/duck/打断/ducking 解析与断言逻辑自检 =====")
    result = Result()

    gate_sample = (
        "  门控: holds=2 releases=1 hold_mask=0x0 discards=1 "
        "discard_samples=800 last_discard_ms=4192"
    )
    parsed = parse_gate_line(gate_sample)
    result.check(parsed is not None, "S1 门控行可解析（门控: holds/releases/hold_mask/...）")
    if parsed is not None:
        result.check(
            parsed["holds"] == 2 and parsed["releases"] == 1,
            "S2 门控 holds/releases=2/1",
        )
        result.check(parsed["hold_mask"] == 0, "S3 hold_mask=0x0 -> 0")
        result.check(
            parsed["discard_samples"] == 800 and parsed["last_discard_ms"] == 4192,
            "S4 discard_samples/last_discard_ms 解析",
        )
    result.check(
        parse_gate_line(
            "feed: holds=0 releases=0 hold_mask=0x0 discards=0 "
            "discard_samples=0 last_discard_ms=0"
        )
        is not None,
        "S5 旧草稿前缀 feed: 也认（兼容固件早期措辞）",
    )
    loose = parse_gate_line("门控: holds=1 releases=0 hold_mask=0x2")
    result.check(
        loose is not None and loose["holds"] == 1 and loose["discard_samples"] is None,
        "S6 门控行缺尾部字段时宽松解析（不把「缺字段」当 0）",
    )
    result.check(
        parse_gate_line("I (123) AFE: feed resumed") is None,
        "S7 普通 AFE 日志不会被当成门控行（负例）",
    )

    duck = parse_duck_line(
        "duck: on=0 gain_q8=256 out_peak=0 stop: last_stop_ms=71 stops=2"
    )
    result.check(
        duck is not None and duck["last_stop_ms"] == 71 and duck["stops"] == 2,
        "S8 player stat duck/stop 行解析（last_stop_ms=71 / stops=2）",
    )
    result.check(
        duck is not None and duck["on"] == 0 and duck["gain_q8"] == 256,
        "S9 duck on/gain_q8=0/256",
    )
    result.check(
        parse_duck_line("duck: on=1 gain_q8=64 out_peak=1234 stop: last_stop_ms=80 stops=5")
        is not None
        and parse_duck_line("duck: on=1 gain_q8=64 out_peak=1234 stop: last_stop_ms=80 stops=5")
        ["last_stop_ms"] == 80,
        "S10 duck on=1 / last_stop_ms=80 也能解析",
    )
    result.check(
        parse_duck_line("PLAYERSTAT jobs=1 pcm=0 mp3=0") is None,
        "S11 PLAYERSTAT 行不会被当成 duck 行（负例）",
    )

    result.check(
        stop_ms_ok(71, 100.0)
        and not stop_ms_ok(100, 100.0)
        and not stop_ms_ok(120, 100.0)
        and not stop_ms_ok(None, 100.0),
        "S12 last_stop_ms < 100ms 判定（71 PASS / 100、120、缺失 FAIL）",
    )
    result.check(
        discard_delta_ok(800)
        and discard_delta_ok(1120)
        and discard_delta_ok(480)
        and not discard_delta_ok(1121)
        and not discard_delta_ok(0),
        "S13 R4 discard_samples ≈800（±320 = ±1 帧@16k）边界判定",
    )
    result.check(
        no_discard_ok(0) and not no_discard_ok(800),
        "S14 R5 正常轮 discard_samples 必须为 0",
    )
    result.check(
        DUCK_RATIO_MIN <= 0.25 <= DUCK_RATIO_MAX
        and not (DUCK_RATIO_MIN <= 0.6 <= DUCK_RATIO_MAX)
        and 0.8 >= DUCK_RECOVER_RATIO
        and not (0.5 >= DUCK_RECOVER_RATIO),
        "S15 R3 ducking 比值判定（0.25 PASS / 0.6 FAIL；恢复 0.8 PASS / 0.5 FAIL）",
    )

    fade = RE_PLAYER_FADE_OUT.search("I (4230) PLAYER: fade out 80 ms")
    result.check(
        fade is not None and int(fade.group(1)) == 80, "S16 PLAYER: fade out <ms> ms 解析"
    )
    ic = RE_VC_INTERRUPT_SENT.search("I (4231) VC: interrupt sent sid=7")
    result.check(
        ic is not None and int(ic.group(1)) == 7, "S17 VC: interrupt sent sid= 解析"
    )
    vs = "I (4240) VS: state SPEAKING -> IDLE（sid=7，停留 4210 ms，原因=interrupt）"
    result.check(
        RE_VS_SPEAKING_IDLE.search(vs) is not None
        and RE_VS_INTERRUPT_REASON.search(vs) is not None,
        "S18 VS: state SPEAKING -> IDLE（原因=interrupt）",
    )
    result.check(
        RE_VS_INTERRUPT_REASON.search(
            "I (4240) VS: state SPEAKING -> IDLE（sid=7，停留 4210 ms，原因=按键短按）"
        )
        is not None,
        "S19 VS 原因=按键 也认（措辞容错）",
    )
    result.check(
        RE_VS_INTERRUPT_LOG.search("I (4231) VS: 打断当前会话（sid=7）") is not None,
        "S20 VS: 打断当前会话 可检出",
    )
    paused = "I (200) AFE: feed paused (holds=0x1)"
    result.check(
        RE_AFE_FEED_PAUSED.search(paused) is not None
        and RE_AFE_FEED_PAUSED_HOLDS.search(paused) is not None,
        "S21 AFE: feed paused (holds=0x 掩码) 解析",
    )
    resumed = "I (500) AFE: feed resumed (discard 50 ms)"
    m = RE_AFE_FEED_RESUMED_DISCARD.search(resumed)
    result.check(
        RE_AFE_FEED_RESUMED.search(resumed) is not None and m is not None and int(m.group(1)) == 50,
        "S22 AFE: feed resumed (discard 50 ms) 解析",
    )
    result.check(
        RE_AFE_FEED_RESUMED_DISCARD.search("I (500) AFE: feed resumed") is None,
        "S23 恢复行缺 discard 字段时不误判（负例，走 WARN）",
    )

    mock_text = (
        "C1   <-- audio_start sid=7\n"
        "C1   <-- interrupt sid=7：停止下发并作废当前 sid\n"
        "C1   下发被取消 sid=7（interrupt / 新轮次 / 断连）\n"
    )
    hits = RE_MOCK_INTERRUPT.findall(mock_text)
    result.check(
        len(hits) == 1 and int(hits[0]) == 7,
        "S24 mock 侧 interrupt sid= 解析且计数=1",
    )
    result.check(
        RE_MOCK_TTS_END_SEND.search("C1   --> tts_end sid=7") is not None,
        "S25 mock --> tts_end sid= 可检出（打断后必须没有）",
    )
    result.check(
        RE_MOCK_TTS_END_SEND.search("C1   tts 下发完成 frames=202 bytes=19392 mode=mp3")
        is None,
        "S26 mock「tts 下发完成」行不会被当成 tts_end（负例）",
    )
    result.check(
        RE_EVT_POST_ACK.search("已投递 BUTTON（短按 payload）") is not None,
        "S27 evt post BUTTON 回显识别（带 payload 措辞容错）",
    )
    result.check(
        RE_EVT_POST_REJECT.search("未知事件: BUTTON（evt list 查看；支持名字或 0~13 的 ID）")
        is not None,
        "S28 evt post 被拒绝可检出（负例）",
    )

    gate_tts = parse_gate_line(
        "  门控: holds=0 releases=0 hold_mask=0x2 discards=0 "
        "discard_samples=0 last_discard_ms=0"
    )
    result.check(
        gate_tts is not None and gate_tts["hold_mask"] == HOLD_TTS,
        "S29 TTS 播放中 hold_mask=0x2（AUDIO_FE_HOLD_TTS）解析",
    )
    gate_prompt = parse_gate_line(
        "  门控: holds=1 releases=0 hold_mask=0x1 discards=0 "
        "discard_samples=0 last_discard_ms=0"
    )
    result.check(
        gate_prompt is not None and gate_prompt["hold_mask"] == HOLD_PROMPT,
        "S30 提示音 R1 hold_mask=0x1（AUDIO_FE_HOLD_PROMPT）解析",
    )
    result.check(
        HOLD_PROMPT == 0x1 and HOLD_TTS == 0x2,
        "S31 原因位常量与 audio_fe.h 一致（PROMPT=0x1 / TTS=0x2）",
    )
    result.check(
        RE_PROMPT_DONE.search("I (1) VS: prompt done, feed resumed") is not None
        and RE_PROMPT_DONE.search("I (1) APP: prompt done, feed resumed") is not None,
        "S32 prompt done 日志兼容 VS/APP 前缀",
    )
    paused_mask = RE_AFE_FEED_PAUSED_MASK.search("I (1) AFE: feed paused (holds=0x01)")
    result.check(
        paused_mask is not None and int(paused_mask.group(1), 16) == HOLD_PROMPT,
        "S33 AFE: feed paused 的 holds 掩码可按十六进制解析",
    )

    ok = result.summary()
    print("\nTTS_STREAM_TEST: SELF-TEST " + ("PASS" if ok else "FAIL") + f"（{len(result.rows)} 项）")
    return ok


def run(args: argparse.Namespace) -> int:
    result = Result()
    print("===== T2.4/T3.2 下行流式播放真机自测（tts_stream_test.py）=====")
    if args.self_test:
        return 0 if run_self_test() else 1
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
        if not run_self_test():
            return 1
        extras = [
            name
            for name, enabled in (
                ("--interrupt", args.interrupt),
                ("--r5", args.r5),
                ("--r3-duck", args.r3_duck),
                ("--prompt-r1", args.prompt_r1),
            )
            if enabled
        ]
        option_hint = f" + {' + '.join(extras)}" if extras else ""
        print(
            f"用法提示：去掉 --dry-run 后跑 6 个基础场景{option_hint}"
            "（--interrupt 自带「打断后正常轮次」回归）"
        )
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

        # ---- T3.2 附加场景（可选）：R4 按键打断 / R5 正常收尾无 discard ----
        if args.interrupt:
            if mock.owned:
                scenario_interrupt(dev, result, ref, material["mp3_bytes"], args, mock)
            else:
                result.warn(
                    "--interrupt：mock 非脚本拉起（--no-mock/端口被占用），无法断言 mock 侧 "
                    "interrupt/tts_end，跳过打断场景"
                )
        if args.r5:
            scenario_r5(dev, result, ref, material["mp3_bytes"], args, mock)

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

        # ---- T3.2 附加场景（可选）：R3 音乐 ducking（player duck on/off 探针） ----
        if args.r3_duck:
            scenario_duck(dev, result, args)

        # ---- T3.2 附加场景（可选）：提示音 R1 原因位（PROMPT=0x1；放最后，唤醒会让 VS 进 LISTENING） ----
        if args.prompt_r1:
            scenario_prompt_r1(dev, result, args)

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
        description="T2.4/T3.2 下行流式播放真机自测（真实 MP3 经 WS 0x02 → 端侧流式解码播放；"
                    "可选 R4 按键打断 / R5 无 discard）",
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
    parser.add_argument("--interrupt", action="store_true",
                        help="T3.2 R4：真实 MP3 播放中 `evt post BUTTON` 按键打断（打断链路日志/"
                             "停声<100ms/discard≈800/ mock 不再发 tts_end），并跑一轮回归")
    parser.add_argument("--r5", action="store_true",
                        help="T3.2 R5：正常收尾轮不产生 discard（mic reset 后跑一轮，"
                             "discard_samples==0 / hold_mask=0x0 / 收音开启）")
    parser.add_argument("--interrupt-after-frames", type=int, default=8,
                        help="T3.2 R4：起播后解码到多少帧再打断（8 帧 ≈ 0.4s 音频）")
    parser.add_argument("--interrupt-timeout", type=float, default=5.0,
                        help="T3.2 R4：等设备打断链路日志（VC/VS）的超时（秒）")
    parser.add_argument("--stop-ms-max", type=float, default=STOP_MS_MAX,
                        help="T3.2 R4：player stat 的 last_stop_ms 上限（毫秒，"
                             "淡出请求→输出静音，契约 <100）")
    parser.add_argument("--r3-duck", action="store_true",
                        help="T3.2 R3：`player duck on/off` 探针——恒定幅度 1kHz MP3 播放中，"
                             "断言 duck 后 out_peak≈0.25× 基线、duck off 后恢复")
    parser.add_argument("--duck-seconds", type=float, default=4.0,
                        help="T3.2 R3：duck 音调素材时长（秒；需覆盖 基线/duck/恢复 三段窗口）")
    parser.add_argument("--duck-warmup", type=float, default=0.6,
                        help="T3.2 R3：起播后等多久读 out_peak 基线（秒，需 >320ms 窗口）")
    parser.add_argument("--duck-window", type=float, default=0.55,
                        help="T3.2 R3：duck on/off 后各等多久再读 out_peak（秒）。out_peak 是最近 320ms 窗口、duck 斜坡 100ms，故需 >0.42s 才能让窗口里只剩 duck 后增益，默认 0.55")
    parser.add_argument("--prompt-r1", action="store_true",
                        help="T3.2 统一门控证据：afplay lexin.wav 唤醒，断言提示音期间 "
                             "hold_mask=0x1（仅 PROMPT 位）、播完 0x0（会外放！）")
    parser.add_argument("--volume", type=int, default=50,
                        help="--prompt-r1 的回放音量（0=不改系统音量；上限 50，用户定稿）")
    parser.add_argument("--wake-timeout", type=float, default=5.0,
                        help="--prompt-r1：回放后等唤醒（AFE: feed paused）的超时（秒）")
    parser.add_argument("--wake-retries", type=int, default=2,
                        help="--prompt-r1：唤醒失败重试次数（环境噪声/距离导致的漏唤醒）")
    parser.add_argument("--prompt-timeout", type=float, default=8.0,
                        help="--prompt-r1：等提示音播完（prompt done, feed resumed）的超时（秒）")
    parser.add_argument("--self-test", action="store_true",
                        help="离线自检 T3.2 新解析/断言逻辑（不连设备、不需要 mock）")
    parser.add_argument("--log", default="", help="把完整串口日志写到该文件")
    parser.add_argument("--dry-run", action="store_true", help="只生成素材/参考解码 + 自检，不连设备")
    args = parser.parse_args()

    if not 1 <= args.pace_pct <= 400:
        raise SystemExit("--pace-pct 必须在 1~400")
    if args.heap_rounds < 1:
        raise SystemExit("--heap-rounds 必须 ≥ 1")
    if args.material_repeats < 1:
        raise SystemExit("--material-repeats 必须 ≥ 1")
    if args.interrupt_after_frames < 1:
        raise SystemExit("--interrupt-after-frames 必须 ≥ 1")
    if args.stop_ms_max <= 0:
        raise SystemExit("--stop-ms-max 必须 > 0")
    if args.duck_seconds < 2.0:
        raise SystemExit("--duck-seconds 必须 ≥ 2.0（覆盖 基线/duck/恢复 三段窗口）")
    if not 0 <= args.volume <= 50:
        raise SystemExit("--volume 必须在 0~50（用户定稿上限 50；0=不改系统音量）")
    if args.wake_retries < 0:
        raise SystemExit("--wake-retries 必须 ≥ 0")
    if args.wake_timeout <= 0 or args.prompt_timeout <= 0:
        raise SystemExit("--wake-timeout / --prompt-timeout 必须 > 0")
    if args.duck_warmup <= 0.32 or args.duck_window <= 0.42:
        raise SystemExit(
            "--duck-warmup 必须 > 0.32、--duck-window 必须 > 0.42"
            "（out_peak 是 ~320ms 窗口 + duck 斜坡 100ms，窗口太短会把斜坡前的响段算进峰值）"
        )

    try:
        return run(args)
    except KeyboardInterrupt:
        print("\n中断（Ctrl-C）")
        return 130


if __name__ == "__main__":
    sys.exit(main())
