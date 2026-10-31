#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["websockets>=14,<19"]
# ///
"""
ws_mock_server.py —— 本地「云端」WebSocket 模拟服务（T1.5）

为什么需要：P1 起端侧依赖云端 WS 契约，但后端未就绪。先做本地 mock，让 T1.4/T1.6 以及
P2–P4 都能脱机联调，不占端侧排期（ROADMAP §2.0 P1 顺序 0）。

契约唯一依据（本脚本不发明字段）：
  - docs/ARCHITECTURE.md §6.1 连接与鉴权、§6.2 二进制帧、§6.3 文本帧
  - docs/ARCHITECTURE.md §7 会话状态机（sid 由端侧每轮自增，mock 原样回填）
  - docs/ARCHITECTURE.md §3.7 下行 TTS：MP3 24kHz 单声道
发现契约缺口时只记录（docs/PROTOCOL.md「实现中发现的契约待确认项」），不私自扩展。

用法：
  uv run tools/ws_mock_server.py                 # 起服务并打印可直连 URL
  uv run tools/ws_mock_server.py --self-test     # 进程内自测（不需要硬件、不需要手敲命令）
  uv run tools/ws_mock_server.py --no-tts        # 只发 JSON 事件（P1 握手/心跳联调）
  uv run tools/ws_mock_server.py --scenario error --drop-pong   # 异常/心跳超时路径
  # T2.4 下行流式播放（契约 §6）：
  uv run tools/ws_mock_server.py --tts-mp3 tts.mp3            # 真实 MP3，按帧切分、实时节奏
  uv run tools/ws_mock_server.py --tts-mp3 tts.mp3 --tts-stall-ms 600 --tts-stall-at 30
  uv run tools/ws_mock_server.py --tts-mp3 tts.mp3 --tts-late-bin  # 窗口外帧（端侧应丢弃）

传输说明：本地 mock 是 ws://（不加密）；线上契约是 wss://（ARCH §6.1）。
TLS/证书策略在 T1.4 决定，见 docs/PRODUCT_IDEAS.md §8 X2。
每条 accept 的连接都开 TCP_NODELAY（关 Nagle）：Nagle + 端侧 delayed ACK 会把均匀发送的
TTS 帧攒成 ~200ms 一批，造成「mock 均匀发、端侧却 underrun」的假欠载（T2.4-C2）。
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import logging
import os
import socket
import sys
import tempfile
import time
import websockets
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from websockets.asyncio.client import connect
from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed

LOG = logging.getLogger("ws_mock")

# ---------------- 协议常量（ARCHITECTURE §6.2，禁止私改） ----------------

MAGIC = b"\x56\x4F"          # magic "VO"
FRAME_VER = 1                # 与 hello 的 ver=1 对齐
TYPE_AUDIO_UP = 0x01         # ↑ 上行麦克风音频（Opus 16k/16kbps/20ms）
TYPE_AUDIO_DOWN = 0x02       # ↓ 下行 TTS 音频（MP3 24k 单声道）
FRAME_HEADER_LEN = 8
MAX_UPLINK_PAYLOAD = 4096    # 20ms Opus 帧约 40B；4KB 足够，同时挡住明显异常帧

TTS_CODEC = "mp3"
TTS_SAMPLE_RATE = 24000

# 鉴权失败关闭码：契约未定义，暂用 4001/4002 应用码（已登记为待确认项）
CLOSE_BAD_DEVICE = 4001
CLOSE_BAD_TOKEN = 4002


class FrameError(ValueError):
    """二进制帧不合法（magic/ver/len 不符）。"""


def build_frame(frame_type: int, seq: int, payload: bytes) -> bytes:
    """按 ARCHITECTURE §6.2 组装 8 字节头 + payload。"""
    if not 0 <= seq <= 0xFFFF:
        raise FrameError(f"seq 越界：{seq}")
    if len(payload) > 0xFFFF:
        raise FrameError(f"payload 过长：{len(payload)}")
    return (
        MAGIC
        + bytes((FRAME_VER, frame_type))
        + seq.to_bytes(2, "big")
        + len(payload).to_bytes(2, "big")
        + payload
    )


def parse_frame(data: bytes) -> tuple[int, int, bytes]:
    """解析并严格校验二进制帧，返回 (type, seq, payload)。

    校验比端侧更严：联调时宁可 mock 报错，也不要让"端侧能解析、mock 放行"的坏帧
    在 T1.6 之后才暴露出来。
    """
    if len(data) < FRAME_HEADER_LEN:
        raise FrameError(f"帧长 {len(data)} < {FRAME_HEADER_LEN}")
    if data[:2] != MAGIC:
        raise FrameError(f"magic 错误：{data[:2].hex()}")
    ver = data[2]
    frame_type = data[3]
    if ver != FRAME_VER:
        raise FrameError(f"ver={ver}，契约只支持 {FRAME_VER}")
    seq = int.from_bytes(data[4:6], "big")
    length = int.from_bytes(data[6:8], "big")
    payload = data[FRAME_HEADER_LEN:]
    if length != len(payload):
        raise FrameError(f"len 字段={length} 与实际 payload {len(payload)}B 不符")
    return frame_type, seq, payload


def _valid_sid(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 0xFFFF


# ---------------- MP3 帧解析（--tts-mp3 用，纯标准库，不依赖 av/lame） ----------------
#
# 为什么自己做帧解析：`--self-test` 要求零硬件、无 av 依赖；而 `--tts-mp3` 必须按
# MP3 帧边界切分（一个 0x02 帧 = 一个 MP3 帧），否则端侧解码器会拿到跨帧数据。
# 只实现 MPEG Layer III：TTS 契约就是 MP3 24k 单声道，lameenc/PyAV 产出的也是 Layer III。

_MP3_BITRATE_V1_L3 = (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320)
_MP3_BITRATE_V2_L3 = (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160)
_MP3_RATES = {3: (44100, 48000, 32000), 2: (22050, 24000, 16000), 0: (11025, 12000, 8000)}
_MP3_SAMPLES_PER_FRAME = {3: 1152, 2: 576, 0: 576}  # Layer III 每帧样本数（MPEG1 / MPEG2·2.5）


@dataclass(frozen=True)
class Mp3Frame:
    """一个完整 MP3 帧（含 4 字节帧头）；下发时整帧作为一个 0x02 payload。"""

    payload: bytes
    sample_rate: int
    bitrate_kbps: int
    duration_ms: float  # 该帧音频时长 = 每帧样本数 / 采样率（24k 时 24 ms）


def _mp3_frame_at(data: bytes, offset: int) -> Mp3Frame | None:
    """尝试把 data[offset:] 解析成一个 MPEG Layer III 帧；不合法/不完整返回 None。"""
    if offset + 4 > len(data):
        return None
    b0, b1, b2, _b3 = data[offset : offset + 4]
    if b0 != 0xFF or (b1 & 0xE0) != 0xE0:
        return None  # 帧同步字不匹配
    version = (b1 >> 3) & 0x03  # 3=MPEG1 / 2=MPEG2 / 0=MPEG2.5 / 1=保留
    layer = (b1 >> 1) & 0x03  # 1=Layer III
    if version == 1 or layer != 1:
        return None
    bitrate_index = (b2 >> 4) & 0x0F
    rate_index = (b2 >> 2) & 0x03
    padding = (b2 >> 1) & 0x01
    if bitrate_index in (0, 0x0F) or rate_index == 0x03:
        return None  # free-format / 非法码率 / 保留采样率
    bitrate = (_MP3_BITRATE_V1_L3 if version == 3 else _MP3_BITRATE_V2_L3)[bitrate_index]
    sample_rate = _MP3_RATES[version][rate_index]
    per_frame_divisor = 144 if version == 3 else 72
    length = per_frame_divisor * bitrate * 1000 // sample_rate + padding
    if length < 4 or offset + length > len(data):
        return None  # 末尾截断帧
    return Mp3Frame(
        payload=data[offset : offset + length],
        sample_rate=sample_rate,
        bitrate_kbps=bitrate,
        duration_ms=_MP3_SAMPLES_PER_FRAME[version] * 1000.0 / sample_rate,
    )


def _skip_id3v2(data: bytes) -> int:
    """跳过文件头 ID3v2 标签（没有则返回 0）。size 字段是 4×7bit synchsafe。"""
    if len(data) < 10 or data[:3] != b"ID3":
        return 0
    size = (
        ((data[6] & 0x7F) << 21)
        | ((data[7] & 0x7F) << 14)
        | ((data[8] & 0x7F) << 7)
        | (data[9] & 0x7F)
    )
    footer = 10 if (data[5] & 0x10) else 0
    return min(10 + size + footer, len(data))


def parse_mp3_frames(data: bytes, source: str = "<mp3>") -> list[Mp3Frame]:
    """把 MP3 字节流按帧切开（Layer III）。

    - 跳过文件头 ID3v2；尾部 ID3v1(`TAG`)/APE/全零填充等不被解析为帧（有帧后自然停在
      最后一个完整帧边界）；
    - 帧头同步字不匹配或末尾不完整帧时停止；一帧都没有则抛 FrameError（宁可联调时报错，
      也不要静默下发空流）。
    """
    frames: list[Mp3Frame] = []
    offset = _skip_id3v2(data)
    while offset + 4 <= len(data):
        frame = _mp3_frame_at(data, offset)
        if frame is None:
            if not frames:
                raise FrameError(f"{source}: 偏移 {offset} 处不是合法 MPEG Layer III 帧")
            break
        frames.append(frame)
        offset += len(frame.payload)
    if not frames:
        raise FrameError(f"{source}: 未解析出任何 MP3 帧")
    return frames


def load_mp3_frames(path: str) -> tuple[list[Mp3Frame], int]:
    """读取并解析 MP3 文件；返回 (帧列表, 未被解析的尾部字节数)。"""
    data = Path(path).read_bytes()
    frames = parse_mp3_frames(data, source=path)
    used = sum(len(frame.payload) for frame in frames)
    return frames, len(data) - used


# ---------------- 配置与统计 ----------------


@dataclass
class Config:
    host: str = "0.0.0.0"
    port: int = 8765
    device_id: str = ""          # 非空则校验 query.device_id
    token: str = ""              # 非空则校验 query.token
    scenario: str = "normal"     # normal / error / music
    think_ms: int = 300          # audio_end → asr 的"云端思考"延时
    chunk_ms: int = 20           # 二进制帧发送间隔，20ms 对应一帧 TTS
    chunk_bytes: int = 192       # 合成 payload 长度（24k/32kbps 20ms ≈ 192B）
    max_tts_frames: int = 100    # 一轮 TTS 帧数上限，100 帧 ≈ 2s
    tts_text: str = "今天天气不错，我们来试试端云联调。"
    no_tts: bool = False         # 只发 JSON，不发二进制帧
    inject_late: bool = False    # 每轮先发一个 sid+1 的迟到事件，测端侧 sid 过滤
    drop_pong: bool = False      # 故意不回 pong，测端侧 10s 心跳超时
    close_after: float = 0.0     # >0 时到点主动断开，测端侧重连
    save_upload: str = ""        # 非空则把上行 payload 落盘到该目录
    # ---- T2.4：真实 MP3 下发 / 欠载注入 / 窗口外帧 ----
    tts_mp3: str = ""            # 非空：下发真实 MP3（按帧切分；缺省仍发合成帧）
    tts_stall_ms: int = 0        # >0：在 tts_stall_at 帧后停发 n ms（制造欠载）
    tts_stall_at: int = 30       # stall 帧序号（0 基）
    tts_late_bin: bool = False   # 每轮 tts_start 前 / tts_end 后各发 1 帧 0x02
    tts_pace_pct: int = 100      # 真实 MP3 发送节奏：帧时长的百分比（100=实时）
    quiet: bool = False          # 自测用：只打 WARN 及以上
    verbose: bool = False        # 打印上行 payload 前 16 字节


@dataclass
class Stats:
    started_at: float = field(default_factory=time.monotonic)
    hello_seen: bool = False
    pings: int = 0
    pongs: int = 0
    voice_sessions: int = 0
    interrupts: int = 0
    music_requests: int = 0
    errors: int = 0
    frames_up: int = 0
    bytes_up: int = 0
    frames_down: int = 0
    bytes_down: int = 0
    bad_frames: int = 0
    uplink_seq_gaps: int = 0
    tts_cancelled: int = 0
    tts_frames: int = 0
    tts_bytes: int = 0
    tts_stalls: int = 0
    late_bins: int = 0

    def summary(self) -> str:
        duration = time.monotonic() - self.started_at
        extra = f" | TTS 下发 {self.tts_frames} 帧/{self.tts_bytes}B"
        if self.late_bins:
            extra += f" 窗口外注入 {self.late_bins} 帧"
        if self.tts_stalls:
            extra += f" stall {self.tts_stalls} 次"
        return (
            f"时长 {duration:.1f}s | 上行 {self.frames_up} 帧/{self.bytes_up}B | "
            f"下行 {self.frames_down} 帧/{self.bytes_down}B{extra} | "
            f"ping={self.pings} pong={self.pongs} | 语音轮次={self.voice_sessions} "
            f"打断={self.interrupts} 坏帧={self.bad_frames} seq跳变={self.uplink_seq_gaps}"
        )


# ---------------- 单个设备会话 ----------------


class Session:
    """一条 WS 连接 = 一台设备；状态只属于该连接，不跨连接共享。"""

    def __init__(self, server: "MockServer", ws: Any, conn_id: int) -> None:
        self.server = server
        self.cfg = server.cfg
        self.ws = ws
        self.conn_id = conn_id
        self.stats = Stats()
        self.sid: int | None = None          # 当前轮 sid（端侧生成，mock 原样回填）
        self.music_sid = 0                    # 音乐事件独立计数（契约未定义归属）
        self.seq_out = 0                      # 下行二进制帧 seq
        self.up_seq_expected: int | None = None
        self.up_round_frames = 0              # 本轮（audio_start 起）上行帧数
        self.tts_task: asyncio.Task | None = None
        self.close_task: asyncio.Task | None = None
        self.hello_info: dict[str, Any] = {}
        self._upload_dir_ready = False

    # ---- 日志统一带连接号：多设备联调时能分清是谁说的话 ----

    def _log(self, level: int, msg: str) -> None:
        LOG.log(level, "C%-2d %s", self.conn_id, msg)

    async def run(self) -> None:
        peer = self.ws.remote_address
        self._log(logging.INFO, f"连接建立 peer={peer[0]}:{peer[1]} path={self.ws.request.path}")
        if self.cfg.close_after > 0:
            self.close_task = asyncio.create_task(self._close_later())
        try:
            async for message in self.ws:
                if isinstance(message, (bytes, bytearray, memoryview)):
                    self._on_binary(bytes(message))
                else:
                    await self._on_text(str(message))
        except ConnectionClosed:
            pass
        finally:
            if self.tts_task is not None and not self.tts_task.done():
                self.tts_task.cancel()
                await asyncio.gather(self.tts_task, return_exceptions=True)
            if self.close_task is not None and not self.close_task.done():
                self.close_task.cancel()
            self._log(logging.INFO, f"连接关闭 | {self.stats.summary()}")
            self.server.on_session_closed(self)

    # ---- 上行 ----

    def _on_binary(self, data: bytes) -> None:
        try:
            frame_type, seq, payload = parse_frame(data)
        except FrameError as exc:
            self.stats.bad_frames += 1
            self._log(logging.WARNING, f"<-- 坏帧({len(data)}B) 已丢弃：{exc}")
            return
        if frame_type != TYPE_AUDIO_UP:
            self.stats.bad_frames += 1
            self._log(logging.WARNING, f"<-- 未知帧类型 0x{frame_type:02X}（上行只接受 0x01），已丢弃")
            return
        if self.up_seq_expected is not None and seq != self.up_seq_expected:
            self.stats.uplink_seq_gaps += 1
            self._log(logging.WARNING, f"<-- seq 跳变：期望 {self.up_seq_expected} 实收 {seq}")
        self.up_seq_expected = (seq + 1) & 0xFFFF
        self.up_round_frames += 1
        self.stats.frames_up += 1
        self.stats.bytes_up += len(payload)
        self._log(logging.INFO, f"<-- 上行音频 seq={seq} len={len(payload)} sid={self.sid}")
        if self.cfg.verbose:
            self._log(logging.DEBUG, f"    hex={payload[:16].hex(' ')}")
        if self.cfg.save_upload:
            self._save_uplink(seq, payload)

    def _save_uplink(self, seq: int, payload: bytes) -> None:
        try:
            directory = Path(self.cfg.save_upload)
            if not self._upload_dir_ready:
                directory.mkdir(parents=True, exist_ok=True)
                self._upload_dir_ready = True
            sid = "nosid" if self.sid is None else f"{self.sid:05d}"
            (directory / f"c{self.conn_id}-sid{sid}-seq{seq:05d}.bin").write_bytes(payload)
        except OSError as exc:
            self._log(logging.WARNING, f"上行 payload 落盘失败：{exc}")
            self.cfg.save_upload = ""  # 只报一次，避免刷屏

    async def _on_text(self, raw: str) -> None:
        try:
            msg = json.loads(raw)
            if not isinstance(msg, dict):
                raise ValueError("顶层不是 JSON 对象")
        except (json.JSONDecodeError, ValueError) as exc:
            self.stats.bad_frames += 1
            self._log(logging.WARNING, f"<-- 非法文本帧({len(raw)}B) 已丢弃：{exc}")
            return

        event = msg.get("t")
        if event == "hello":
            self.stats.hello_seen = True
            self.hello_info = msg
            ver = msg.get("ver")
            self._log(logging.INFO, f"<-- hello device_id={msg.get('device_id')} ver={ver}")
            if ver != 1:
                self._log(logging.WARNING, f"hello ver={ver}，mock 只实现 ver=1，继续服务")
            # ARCH §6.1（T1.4 定稿）：服务端必须回 hello_ack，端侧 5s 内没收到按建连失败处理。
            # 这里是 mock 对契约的最小实现，不额外发明字段。
            await self._send_json({"t": "hello_ack", "ver": FRAME_VER})

        elif event == "ping":
            self.stats.pings += 1
            if self.cfg.drop_pong:
                self._log(logging.WARNING, "<-- ping（--drop-pong：故意不回，测端侧 10s 心跳超时）")
                return
            await self._send_json({"t": "pong"})
            self.stats.pongs += 1

        elif event == "audio_start":
            sid = msg.get("sid")
            await self._stop_tts("新的 audio_start")
            if not _valid_sid(sid):
                self._log(logging.WARNING, f"audio_start 的 sid 非 uint16：{sid!r}，下发时会缺 sid")
            self.sid = sid if _valid_sid(sid) else None
            self.up_seq_expected = None          # 新的一轮重新累计上行 seq
            self.up_round_frames = 0
            self.stats.voice_sessions += 1
            self._log(logging.INFO, f"<-- audio_start sid={self.sid}")

        elif event == "audio_end":
            sid = msg.get("sid", self.sid)
            self._log(logging.INFO, f"<-- audio_end sid={sid}（本轮上行 {self.up_round_frames} 帧）")
            if not _valid_sid(sid):
                self._log(logging.WARNING, f"audio_end 的 sid 非 uint16：{sid!r}")
            else:
                self.sid = sid
            await self._stop_tts("重复的 audio_end")
            self.tts_task = asyncio.create_task(self._tts_flow())

        elif event == "interrupt":
            self.stats.interrupts += 1
            await self._stop_tts("设备 interrupt")
            self._log(logging.INFO, f"<-- interrupt sid={msg.get('sid')}：停止下发并作废当前 sid")
            self.sid = None
            # §6.3 未定义服务端对 interrupt 的应答，这里不发帧。

        elif event == "music_request":
            self.stats.music_requests += 1
            self._log(
                logging.INFO,
                f"<-- music_request action={msg.get('action')} id={msg.get('id')} index={msg.get('index')}",
            )
            if self.cfg.scenario == "music":
                self.music_sid = (self.music_sid + 1) & 0xFFFF
                await self._send_json(
                    {
                        "t": "music_track",
                        "sid": self.music_sid,
                        "url": self._music_url(),
                        "codec": "mp3",
                        "duration_ms": 30000,
                        "title": "mock track",
                    }
                )
                self._log(
                    logging.WARNING,
                    "music_track 的 url 不可播放：T1.5 不提供 HTTP 音频服务（P4 前补），"
                    "且音乐事件 sid 归属契约未定义（见 PROTOCOL.md 待确认项）",
                )
            else:
                self._log(logging.WARNING, "scenario 非 music，不回 music_track（--scenario music 可测）")

        elif event == "music_status":
            self._log(
                logging.INFO,
                f"<-- music_status state={msg.get('state')} progress_ms={msg.get('progress_ms')}",
            )

        else:
            self._log(logging.WARNING, f"<-- 未知事件 t={event!r}，已忽略（§6.3 未定义）")

    # ---- 下行：文本 ----

    async def _send_json(self, obj: dict[str, Any]) -> None:
        raw = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
        await self.ws.send(raw)
        extra = ""
        if "sid" in obj:
            extra += f" sid={obj['sid']}"
        if "text" in obj:
            extra += f" text={obj['text']!r}"
        if obj.get("t") == "pong":
            self._log(logging.INFO, "--> pong")
        else:
            self._log(logging.INFO, f"--> {obj.get('t')}{extra}")

    # ---- 下行：二进制 ----

    async def _send_bin(self, payload: bytes, note: str = "") -> None:
        seq = self.seq_out
        frame = build_frame(TYPE_AUDIO_DOWN, seq, payload)
        await self.ws.send(frame)
        self.seq_out = (self.seq_out + 1) & 0xFFFF
        self.stats.frames_down += 1
        self.stats.bytes_down += len(payload)
        if note:
            self.stats.late_bins += 1
        suffix = f" {note}" if note else ""
        self._log(
            logging.INFO,
            f"--> bin type=0x02 seq={seq} len={len(payload)} sid={self.sid}{suffix}",
        )

    def _synth_tts_payload(self, index: int) -> bytes:
        """合成 TTS payload，仅用于帧级联调，**不是可解码的 MP3**。

        前 4 字节写成 MP3 帧同步头样式，便于 hexdump 肉眼核对；T1.5 不依赖真实
        MP3 素材（本机无 ffmpeg），端侧 MP3 解码在 T2.3 实现时再补真实样例。
        """
        size = max(FRAME_HEADER_LEN, self.cfg.chunk_bytes)
        head = b"\xff\xfb\x90\x64" + (index & 0xFFFF).to_bytes(2, "big")
        return (head + b"\x00" * size)[:size]

    def _music_url(self) -> str:
        return f"http://{self.server.banner_host()}:{self.server.port}/media/mock.mp3"

    # ---- 一轮语音下发（等价于"云端 ASR + LLM + TTS 流"） ----

    def _late_bin_payload(self) -> bytes:
        """窗口外注入用的 0x02 payload：优先用真实 MP3 首帧，否则合成帧。"""
        frames = self.server.tts_mp3_frames
        if frames:
            return frames[0].payload
        return self._synth_tts_payload(0)

    async def _pace_after_frame(self, index: int, frame_ms: float) -> None:
        """一帧发完后的节奏：默认按帧时长（真实 MP3 秒级实时）；stall 点改为停发。"""
        cfg = self.cfg
        if index == cfg.tts_stall_at and cfg.tts_stall_ms > 0:
            self.stats.tts_stalls += 1
            self._log(
                logging.INFO,
                f"stall {cfg.tts_stall_ms} ms（第 {index} 帧后停发，制造端侧欠载）",
            )
            await asyncio.sleep(cfg.tts_stall_ms / 1000)
            return
        gap_ms = frame_ms * cfg.tts_pace_pct / 100.0 if cfg.tts_pace_pct > 0 else 0.0
        if gap_ms > 0:
            await asyncio.sleep(gap_ms / 1000)

    async def _tts_flow(self) -> None:
        sid = self.sid
        try:
            if self.cfg.think_ms > 0:
                await asyncio.sleep(self.cfg.think_ms / 1000)

            # 迟到帧注入：端侧必须丢弃 sid 不匹配的事件（ARCH §6.3 / PROTOCOL P-2）
            if self.cfg.inject_late:
                late = 1 if sid is None else (sid + 1) & 0xFFFF
                await self._send_json(
                    {"t": "asr_final", "sid": late, "text": "迟到帧（端侧应丢弃）"}
                )
                self._log(
                    logging.INFO,
                    f"已注入迟到事件 asr_final sid={late}（当前轮 sid={sid}，端侧应丢弃并计数）",
                )

            if self.cfg.scenario == "error":
                self.stats.errors += 1
                await self._send_json(
                    {"t": "error", "sid": sid, "code": 1, "msg": "mock: 模拟 TTS 失败"}
                )
                self._log(
                    logging.WARNING,
                    "error 码表已由 PROTOCOL.md 定稿：1=通用/未知（T2.5）",
                )
                return

            text = self.cfg.tts_text
            await self._send_json({"t": "asr_partial", "sid": sid, "text": text[: max(1, len(text) // 2)]})
            await self._send_json({"t": "asr_final", "sid": sid, "text": text})
            for offset in range(0, len(text), 5):
                await self._send_json({"t": "llm_delta", "sid": sid, "text": text[offset : offset + 5]})

            sent_frames = 0
            sent_bytes = 0
            if self.cfg.no_tts:
                await self._send_json(
                    {"t": "tts_start", "sid": sid, "codec": TTS_CODEC, "sr": TTS_SAMPLE_RATE}
                )
                self._log(logging.INFO, "（--no-tts：跳过二进制帧，仅验证 JSON 链路）")
            else:
                if self.cfg.tts_late_bin:
                    # tts_start 之前：端侧还没开 0x02 接收窗口，必须丢弃（T2.4 契约 §2.3）
                    await self._send_bin(self._late_bin_payload(), note="late-pre")
                await self._send_json(
                    {"t": "tts_start", "sid": sid, "codec": TTS_CODEC, "sr": TTS_SAMPLE_RATE}
                )
                frames = self.server.tts_mp3_frames
                if frames is None:
                    total = max(1, self.cfg.max_tts_frames)
                    for index in range(total):
                        payload = self._synth_tts_payload(index)
                        await self._send_bin(payload)
                        sent_frames += 1
                        sent_bytes += len(payload)
                        await self._pace_after_frame(index, self.cfg.chunk_ms)
                else:
                    total = len(frames) if self.cfg.max_tts_frames <= 0 else min(
                        len(frames), self.cfg.max_tts_frames
                    )
                    for index in range(total):
                        frame = frames[index]
                        await self._send_bin(frame.payload)
                        sent_frames += 1
                        sent_bytes += len(frame.payload)
                        await self._pace_after_frame(index, frame.duration_ms)
                    if self.cfg.tts_stall_ms > 0 and self.cfg.tts_stall_at >= total:
                        self._log(
                            logging.WARNING,
                            f"--tts-stall-at={self.cfg.tts_stall_at} 超出本轮帧数 {total}，stall 未触发",
                        )
            await self._send_json({"t": "tts_end", "sid": sid})
            if not self.cfg.no_tts and self.cfg.tts_late_bin:
                # tts_end 之后：端侧窗口已关闭，必须丢弃（不计数、不播放）
                await self._send_bin(self._late_bin_payload(), note="late-post")
            mode = "mp3" if self.server.tts_mp3_frames is not None else "synth"
            if not self.cfg.no_tts:
                self.stats.tts_frames += sent_frames
                self.stats.tts_bytes += sent_bytes
                self._log(
                    logging.INFO,
                    f"tts 下发完成 frames={sent_frames} bytes={sent_bytes} mode={mode}",
                )

        except asyncio.CancelledError:
            self.stats.tts_cancelled += 1
            self._log(logging.INFO, f"下发被取消 sid={sid}（interrupt / 新轮次 / 断连）")
            raise
        except ConnectionClosed:
            self._log(logging.INFO, "连接已断开，结束本轮下发")
        finally:
            self.tts_task = None

    async def _stop_tts(self, reason: str) -> None:
        task = self.tts_task
        if task is None or task.done():
            self.tts_task = None
            return
        self._log(logging.INFO, f"取消进行中的下发（{reason}）")
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        self.tts_task = None

    async def _close_later(self) -> None:
        await asyncio.sleep(self.cfg.close_after)
        self._log(logging.WARNING, f"--close-after={self.cfg.close_after}s 到点，主动断开（测端侧重连）")
        await self.ws.close(code=1000, reason="mock: close-after")


# ---------------- mock 服务 ----------------


def _enable_tcp_nodelay(connection: Any) -> bool:
    """给一条 WS 连接开 TCP_NODELAY（关 Nagle）。

    为什么必须开：mock 每 ~20ms 发一个 0x02 帧，Nagle 会攒到 ACK / 满 MSS 才发，
    叠加端侧 delayed ACK 后设备变成每 ~200–250ms 才集中收到一批 → ring 耗尽，真机上
    表现为「mock 明明均匀发、稳态却 underrun=1」的**端侧假欠载**（T2.4-C2 实测）。
    真实云端实现同样应关 Nagle；这是测试环境/服务端修复，不放宽契约 underrun=0 阈值。

    `connection` 可以是 websockets 的 ServerConnection（handler 参数）或 process_request
    钩子里的 connection；transport 在握手前（process_request）就已由 connection_made 建好。
    """
    try:
        transport = getattr(connection, "transport", None)
        if transport is None:
            return False
        sock = transport.get_extra_info("socket")
        if sock is None:
            return False
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        return True
    except (AttributeError, OSError):
        return False


def _process_request(connection: Any, request: Any) -> None:
    """握手前给每条 accept 的连接设 TCP_NODELAY（返回 None = 继续正常握手）。"""
    if not _enable_tcp_nodelay(connection):
        LOG.warning(
            "握手前无法设置 TCP_NODELAY：连接可能被 Nagle 攒批，端侧会出现假欠载"
        )
    return None


class MockServer:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.port = cfg.port
        self._server: Any = None
        self._next_conn = 1
        self.active: dict[int, Session] = {}
        self.history: list[Session] = []
        # --tts-mp3：启动时解析一次，所有连接共享（只读）；None 表示合成帧模式
        self.tts_mp3_frames: list[Mp3Frame] | None = None

    async def start(self) -> int:
        if self.cfg.tts_mp3 and self.tts_mp3_frames is None:
            self.tts_mp3_frames = self._load_tts_mp3()
        # ping_interval=None：关掉 WS 协议层心跳，避免与契约里的应用层 {"t":"ping"} 混淆
        # process_request：握手前对每条连接设 TCP_NODELAY（T2.4-C2，防 Nagle 假欠载）
        self._server = await serve(
            self._handler,
            self.cfg.host,
            self.cfg.port,
            ping_interval=None,
            max_size=64 * 1024,
            logger=_protocol_logger(),
            process_request=_process_request,
        )
        self.port = self._server.sockets[0].getsockname()[1]
        return self.port

    def _load_tts_mp3(self) -> list[Mp3Frame]:
        path = self.cfg.tts_mp3
        try:
            frames, leftover = load_mp3_frames(path)
        except OSError as exc:
            raise RuntimeError(f"--tts-mp3 读取失败：{path}（{exc}）") from exc
        except FrameError as exc:
            raise RuntimeError(f"--tts-mp3 解析失败：{exc}") from exc
        duration_ms = sum(frame.duration_ms for frame in frames)
        rates = sorted({frame.sample_rate for frame in frames})
        bitrates = sorted({frame.bitrate_kbps for frame in frames})
        LOG.info(
            "真实 MP3: %s | 帧=%d 字节=%d 时长=%.0f ms 采样率=%s 码率=%s kbps%s",
            path,
            len(frames),
            sum(len(frame.payload) for frame in frames),
            duration_ms,
            "/".join(str(rate) for rate in rates),
            "/".join(str(bitrate) for bitrate in bitrates),
            f" | 尾部未解析 {leftover} B（ID3v1/APE 等标签，已忽略）" if leftover else "",
        )
        if TTS_SAMPLE_RATE not in rates:
            LOG.warning(
                "真实 MP3 采样率 %s 与 TTS 契约 %d 不一致；端侧可能按「流中帧参数变化」中止本轮",
                rates,
                TTS_SAMPLE_RATE,
            )
        return frames

    async def stop(self) -> None:
        if self._server is None:
            return
        self._server.close()
        await self._server.wait_closed()
        self._server = None

    def banner_host(self) -> str:
        if self.cfg.host not in ("0.0.0.0", "", "::"):
            return self.cfg.host
        ips = _lan_ips()
        return ips[0] if ips else "127.0.0.1"

    def on_session_closed(self, session: Session) -> None:
        self.active.pop(session.conn_id, None)
        self.history.append(session)

    async def _handler(self, ws: Any) -> None:
        conn_id = self._next_conn
        self._next_conn += 1
        # 双保险：process_request 已设过；这里再设一次（兼容不同 websockets 版本/钩子差异）
        _enable_tcp_nodelay(ws)
        query = parse_qs(urlsplit(ws.request.path).query)

        ok, reason, close_code = self._validate(query)
        if not ok:
            LOG.warning("C%-2d 拒绝连接：%s", conn_id, reason)
            await ws.close(code=close_code, reason=reason[:100])
            return

        session = Session(self, ws, conn_id)
        self.active[conn_id] = session
        try:
            await session.run()
        except Exception:  # 单连接异常不能拖垮 mock 服务
            LOG.exception("C%-2d 会话异常退出", conn_id)
            self.active.pop(conn_id, None)

    def _validate(self, query: dict[str, list[str]]) -> tuple[bool, str, int]:
        """校验 §6.1 的 query 参数；只在 CLI 显式给了期望值时才强制匹配。"""
        device_id = (query.get("device_id") or [""])[0]
        token = (query.get("token") or [""])[0]
        ver = (query.get("ver") or [""])[0]
        if not device_id:
            LOG.warning("缺少 query 参数 device_id（§6.1），仍继续，便于端侧早期联调")
        if self.cfg.device_id and device_id != self.cfg.device_id:
            return False, f"device_id 不匹配：收 {device_id!r} 期望 {self.cfg.device_id!r}", CLOSE_BAD_DEVICE
        if self.cfg.token and token != self.cfg.token:
            return False, "token 不匹配", CLOSE_BAD_TOKEN
        if ver != "1":
            LOG.warning("query ver=%r，mock 只实现 ver=1（继续服务）", ver)
        return True, "", 0


# ---------------- 输出与环境 ----------------


def _lan_ips() -> list[str]:
    ips: set[str] = set()
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.connect(("8.8.8.8", 80))  # 不发包，只为拿到出口网卡地址
            ips.add(sock.getsockname()[0])
        finally:
            sock.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except OSError:
        pass
    return sorted(ip for ip in ips if not ip.startswith("127."))


def _setup_logging(cfg: Config) -> None:
    level = logging.DEBUG if cfg.verbose else (logging.WARNING if cfg.quiet else logging.INFO)
    logging.basicConfig(
        level=level,
        format="[%(asctime)s.%(msecs)03d] %(levelname)-5s %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
        force=True,
    )
    # websockets 库自身的握手帧/协议 DEBUG 会淹没联调日志（--verbose 时尤其严重），
    # 统一压到 WARNING：mock 只输出"设备视角"的收发事件。
    logging.getLogger("websockets").setLevel(logging.WARNING)
    logging.getLogger("ws_mock.proto").setLevel(logging.WARNING)
    logging.getLogger("asyncio").setLevel(logging.WARNING)


def _protocol_logger() -> logging.Logger:
    """websockets 协议层日志独立命名，避免与 mock 自己的收发日志混在一起。"""
    return logging.getLogger("ws_mock.proto")


def _print_banner(server: MockServer) -> None:
    cfg = server.cfg
    device_id = cfg.device_id or "dev1"
    token = cfg.token or "dev-token"
    hosts = ["127.0.0.1"]
    if cfg.host in ("0.0.0.0", "", "::"):
        hosts += _lan_ips()
    else:
        hosts = [cfg.host]

    lines = [
        "=" * 78,
        "ws_mock_server —— 本地云端模拟（T1.5 / T2.4）",
        f"python {sys.version.split()[0]} / websockets {websockets.__version__}",
        f"scenario={cfg.scenario} think={cfg.think_ms}ms chunk={cfg.chunk_bytes}B/{cfg.chunk_ms}ms "
        f"max_tts_frames={cfg.max_tts_frames} no_tts={cfg.no_tts} drop_pong={cfg.drop_pong}"
        + (f" close_after={cfg.close_after}s" if cfg.close_after else ""),
        _tts_mode_line(server),
        "传输: 每条 accept 连接 TCP_NODELAY=1（关 Nagle；避免 delayed ACK 攒批造成端侧假欠载）",
        "直连 URL（设备端填 CONFIG_AI_WS_URL）：",
    ]
    for host in hosts:
        lines.append(f"  ws://{host}:{server.port}/v1/voice?device_id={device_id}&token={token}&ver=1")
    lines += [
        '手工验证：uv run --with websockets python -m websockets "<上面的 URL>"',
        "本地 mock 用 ws://；线上契约是 wss://（ARCH §6.1），TLS/证书策略在 T1.4 决定（PRODUCT_IDEAS §8 X2）",
        "=" * 78,
    ]
    print("\n".join(lines), flush=True)


def _tts_mode_line(server: MockServer) -> str:
    """横幅里的 TTS 下发模式摘要：真实 MP3 的帧数/时长，或合成帧提示。"""
    cfg = server.cfg
    frames = server.tts_mp3_frames
    if frames is None:
        mode = f"TTS 模式: 合成帧（协议测试用，非可解码 MP3）chunk={cfg.chunk_bytes}B/{cfg.chunk_ms}ms"
    else:
        total_ms = sum(frame.duration_ms for frame in frames)
        audio_bytes = sum(len(frame.payload) for frame in frames)
        mode = (
            f"TTS 模式: 真实 MP3 {cfg.tts_mp3} | 帧={len(frames)} "
            f"字节={audio_bytes} 时长={total_ms:.0f}ms 采样率={frames[0].sample_rate}Hz "
            f"节奏={cfg.tts_pace_pct}%"
        )
    extras = []
    if cfg.tts_stall_ms > 0:
        extras.append(f"stall={cfg.tts_stall_ms}ms@帧{cfg.tts_stall_at}")
    if cfg.tts_late_bin:
        extras.append("late-bin（tts_start 前/tts_end 后各 1 帧）")
    if extras:
        mode += " | " + " | ".join(extras)
    return mode


async def _serve_forever(server: MockServer) -> int:
    await server.start()
    _print_banner(server)
    try:
        await asyncio.Event().wait()  # 只靠 Ctrl-C 退出
    finally:
        await server.stop()
        LOG.info("mock 服务已停止")
    return 0


# ---------------- 自测（不需要硬件，进程内起客户端） ----------------


async def _start(cfg: Config) -> tuple[MockServer, str]:
    server = MockServer(cfg)
    port = await server.start()
    url = f"ws://127.0.0.1:{port}/v1/voice?device_id=dev1&token=t&ver=1"
    return server, url


async def _wait_closed(server: MockServer, count: int, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while len(server.history) < count:
        if time.monotonic() > deadline:
            raise AssertionError(f"等待会话关闭超时：已关闭 {len(server.history)}，期望 ≥{count}")
        await asyncio.sleep(0.01)


async def _recv_json(ws: Any, timeout: float = 3.0) -> dict[str, Any]:
    raw = await asyncio.wait_for(ws.recv(), timeout)
    if isinstance(raw, (bytes, bytearray, memoryview)):
        raise AssertionError(f"期望 JSON 文本帧，却收到二进制 {len(raw)}B")
    return json.loads(raw)


async def _recv_until(ws: Any, stop_event: str, timeout: float = 3.0) -> list[tuple[str, Any]]:
    collected: list[tuple[str, Any]] = []
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AssertionError(f"等待 {stop_event} 超时，已收到 {[m for _, m in collected]}")
        raw = await asyncio.wait_for(ws.recv(), remaining)
        if isinstance(raw, (bytes, bytearray, memoryview)):
            collected.append(("bin", bytes(raw)))
            continue
        msg = json.loads(raw)
        collected.append(("json", msg))
        if msg.get("t") == stop_event:
            return collected


# 自测用的真实 MP3：lameenc 24k/单声道/32kbps，取自 tools/今天天气如何.wav 前 0.48s
# （22 帧 × 24ms = 528ms / 2112 B）。嵌入脚本是为了 `--self-test` 不依赖 lameenc/av：
# 帧数/字节/帧头/节奏断言直接跑在真实码流上，而不是脚本自己造的"假 MP3"。
_SELFTEST_MP3_FRAMES = 22
_SELFTEST_MP3_BYTES = 2112
_SELFTEST_MP3_B64 = (
    "//NExAASKSYIAEmGcAYC5O3SAgFAoJ0ZGAye//+7tMmnZAgQQiAgAXeFudT6/6Oe533KmhfwWKvPuUGMvW8+GFAMgXWfLvlxAjKJ"
    "iBw45bh/o+i7XLora2tJbGd2/xi0//NExAoR4YIsAHjGlDZ6uWpiDkJ87XPNFMABhWKIciVqh8qErHTR5moaxP27/oT5iNpcWWoW"
    "S4EAWBBChIJPVFnt2FHruR6q0K6aFDtoXRXZEhE/XkP/Pj48K1GJ4XLt//NExBUQ2Z4sAGjGlKOo6kGZcdqgReK5Fn9vA+2RV5uf"
    "CYVINqWNCwVErCpsdFhjB6XAVlWM6nf5BH11CLGcvjZundy1rrdKlVUWzZs0W3/Ojh830mRcRCBhAYIw8hsu//NExCQSidYsAUsY"
    "APtW76+RuYqd/WY4akaKdIkmRkCJvLFVvPh8/PXQmwK2Rb/ejauKKkABAIBQMNv+NvzeBQAF/xF6/3M/3Op96IxGiKXMMnboxIrl"
    "xXJxXEILH8wx//NExCwdOyqOX4FQALkhiDQeDxRZ9fJDZVxbdhEOYeYYYcaKn+/EQLAtk7DgkAEBDhfoIRjxbLsSj8Wx+Ri2f//8"
    "8nQkG8+p4tnEm0ukWx+555xAPCv/////+lTv//56//NExAoSmxKoAcAoAPIjIIN8irISsTOJiYvEyDUExIOAAHHQjEZWoYVcEixp"
    "hwHExBBMyGE0F0IRA4ZxRjOJnQUSKOcjoQ+x9hjmtFFK2////11amhl4UGQmSBdFDF5t//NExBIW8XraXEjeluQNg5My6il1Nlrc"
    "FwgQUuxTIPiXOqzbxqJGZYLEwvnAWMeJ0K6udatr4pb59N119anlzH0nbqdWogBVjgaDR3V3Yiv/amrUMo+u+xympWKIFmqm//NE"
    "xAkTsXqcysBGlE3TCcMW7EAPxj3n8Y35f///uajyFTrsp1aA4gHSy///NSdFArAqkeSjAyg2dFCSg8HUJHxMeARK0SjHuf6LTGPc"
    "BmoYb6775SfOl5jxl2pzJpox//NExA0VoaKQytKQlHqpXqKDEtyhecVSdSEFeLDSMkrdGSud+nW5JAhNh0IlEj9Rc+RCgZZ/F173"
    "KPwmTNyH8vCanyj3JCHOHf///8wLydxce0wGzVSapUtttrclFoEV//NExAkU4vrWXnqEunQO0ulcBnGptdFthSp1z/TdQJMQQ5UR"
    "dRJecLZ8po305k40vOXMzTt6lbbTpR2KVDlBclQdbui/////////r2Yxr110LV2zQbLcqkCqKCIIObjg//NExAgSOQqRlMpMcFM6"
    "SyMhj1M/WSuo7LNdMg3Ase5Zv0zvTY9kGcr4yf7Z2fslnYdvOhik+6LiUOqeqi1Z4RTx1l/////+oO4ihq3VQBkuYluSQAxcwAjZ"
    "KGTgwE4K//NExBIR4RqiPpvKciNCLLOXoyd+VNYlL98tU/7bqMXfG8uj4C1UWztI2+NOIsuW4tZOJGiFTL/2f//76hBnWd8yhQAp"
    "Gm43JAIBSAXwAWyvw2zx4+FVt6S+JAcDiowI//NExB0R0QKuX08YApcaN8XOxR2NgApov0uLH2hBgJwENLDyFEBWhMb79C49pWe/"
    "//////0C5lUlkCKxWA0yQtQHVkxbmolF2EIoTPT0AxqpaQgXAMAuaNhsPPaHIrs8//NExCga0nKMAZk4AP1SyOYzJY9r8bk43Ecm"
    "XEwkhwlBN/MMY8fJscNTY1IC7/7o0zmHHEi43HD//8+3Ma6FZJlJLUo//puLvjjoCCIsBwRVcY4CMCLJiykjYlzQzQ2+//NExA8R"
    "CXKcAdI4AJ///T/bM57n5rsxjMSGwXHhcUFRU4g6LrOOms5pYXg6beuF3wRP0nAqUu2fX65pX9hTclzkqs8UnAkyq6CIlBGUb4Hq"
    "1bfYiZUnev/fbq0icj2Q//NExB0RIW6cAMiMlPTKpF0WPAhe2JiTUmdFtz/9+iW8fUxxu8KJkR6GuRiL////5alS2rrlVJ4Rpl1Q"
    "BSY5ZSVRUnGQBFGnL+uL7bbtc/8mevu++o7H3qOlA4OSTIE8//NExCsQQQ6gAMMMcU1iYhwgrDnG1Y/fKP84PBzC6PYa5vLE//h7"
    "DC2GII9XILlNbqQnEFmGpGIVsA58kROCT7h/q9YrmMYKQVQzITyqQRw6KLQzGxrjPm5XZ47tdXjt//NExD0SkXK5lHoMlIm8aWce"
    "bPP//////4Eq39CYqxjAqpJgX42YQUm+OTj5ecCWFjajmr/m1HWceFy5OEC8kgMncgkXHQSUFypOd7Pb7r+/n+OIXttXVnmoer//"
    "//////NExEUScYqcAMHWlM7MDzpWgL+kOwwopdSmbk5LOSxOLAUz+qQvPISaOg69o1j87ctwRti3JTTg08m5btVCiYeGKiWMMOoS"
    "PMcAl09ygqo8RDQLMb///////JJqACx+//NExE4SkPqRlMsGcL8MFqNICwQ7kNQggAGuYpCxhlzBczg9NUDh8ByiLkuQDVXUdgIW"
    "yHDhxK9ItXrZgxkDA6dH2kKtd4WIChQkee1P//////2rOwkyGSiU15CZ4gGO//NExFYSgO6BlNIGcBgDGmBBHRDS4k5OlDUNULKD"
    "AIkAgEAgqjpNIkUZQgwUFBSYFBTegw3/CgX8QX8go74oK777yCn/8WpMQU1FNC4wqqqqqqqqqqqqqqqqqqqqqqqq//NExF8RUIZg"
    "CMvMS6qqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq"
    "qqqqqqqqqqqqqqqq"
)


def _write_selftest_mp3() -> str:
    directory = tempfile.mkdtemp(prefix="ws_mock_selftest_")
    path = os.path.join(directory, "selftest_24k_mono.mp3")
    with open(path, "wb") as handle:
        handle.write(base64.b64decode(_SELFTEST_MP3_B64))
    return path


async def _recv_one(ws: Any, deadline: float) -> tuple[float, str, Any]:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise AssertionError("等待下行超时")
    raw = await asyncio.wait_for(ws.recv(), remaining)
    now = time.monotonic()
    if isinstance(raw, (bytes, bytearray, memoryview)):
        return now, "bin", bytes(raw)
    return now, "json", json.loads(raw)


async def _collect_round(
    ws: Any, sid: int, trailing_bins: int = 0, timeout: float = 10.0
) -> list[tuple[float, str, Any]]:
    """客户端侧跑一轮：hello / audio_start / 上行 3 帧 / audio_end，收到 tts_end。

    返回 [(接收时刻, "bin"|"json", 内容)]；`trailing_bins>0` 时继续收 tts_end 之后到达的
    二进制帧（--tts-late-bin 用来验证"收尾后仍有窗口外帧"）。
    """
    await ws.send(json.dumps({"t": "hello", "device_id": "dev1", "ver": 1}))
    await ws.send(json.dumps({"t": "audio_start", "sid": sid}))
    for index in range(3):
        await ws.send(build_frame(TYPE_AUDIO_UP, index, b"\x01" * 40))
    await ws.send(json.dumps({"t": "audio_end", "sid": sid}))
    collected: list[tuple[float, str, Any]] = []
    deadline = time.monotonic() + timeout
    while True:
        item = await _recv_one(ws, deadline)
        collected.append(item)
        if item[1] == "json" and item[2].get("t") == "tts_end":
            break
    for _ in range(trailing_bins):
        item = await _recv_one(ws, deadline)
        assert item[1] == "bin", f"tts_end 后期望窗口外二进制帧，实际收到 {item[2]!r}"
        collected.append(item)
    return collected


async def _t_hello(cfg: Config) -> None:
    server, url = await _start(cfg)
    try:
        async with connect(url) as ws:
            await ws.send(json.dumps({"t": "hello", "device_id": "dev1", "ver": 1}))
            # ARCH §6.1（T1.4 定稿）：hello 之后必须回 hello_ack，端侧靠它判断握手成功
            ack = await _recv_json(ws, timeout=1.0)
            assert ack.get("t") == "hello_ack", f"期望 hello_ack，收到 {ack}"
            assert ack.get("ver") == 1, f"hello_ack.ver 错误：{ack}"
        await _wait_closed(server, 1)
        session = server.history[-1]
        assert session.stats.hello_seen, "服务端没看到 hello"
        assert session.hello_info.get("device_id") == "dev1", "hello.device_id 解析错误"
        assert session.hello_info.get("ver") == 1, "hello.ver 解析错误"
    finally:
        await server.stop()


async def _t_tcp_nodelay(cfg: Config) -> None:
    """每条 accept 的连接必须开 TCP_NODELAY（否则 Nagle/delayed ACK 攒批→端侧假欠载）。"""
    server, url = await _start(cfg)
    try:
        async with connect(url) as ws:
            # 等 handler 建好 Session（connect 返回时握手已完成，通常立刻就有）
            deadline = time.monotonic() + 1.0
            session = None
            while time.monotonic() < deadline:
                session = next(iter(server.active.values()), None)
                if session is not None:
                    break
                await asyncio.sleep(0.01)
            assert session is not None, "服务端未建立会话（拿不到连接）"
            transport = getattr(session.ws, "transport", None)
            assert transport is not None, "服务端连接没有 transport"
            sock = transport.get_extra_info("socket")
            assert sock is not None, "服务端连接拿不到 socket"
            enabled = sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY)
            # Linux 返回 1；macOS 实测返回非 0（4）即已开启，故只校验「非 0」。
            assert enabled != 0, f"服务端 TCP_NODELAY 未开启（getsockopt={enabled}）"
            # 开启后连接仍可正常收发
            await ws.send(json.dumps({"t": "ping"}))
            msg = await _recv_json(ws, timeout=1.0)
            assert msg.get("t") == "pong", f"期望 pong，收到 {msg}"
    finally:
        await server.stop()


async def _t_ping(cfg: Config) -> None:
    server, url = await _start(cfg)
    try:
        async with connect(url) as ws:
            await ws.send(json.dumps({"t": "ping"}))
            msg = await _recv_json(ws, timeout=1.0)
            assert msg.get("t") == "pong", f"期望 pong，收到 {msg}"
    finally:
        await server.stop()


async def _t_voice(cfg: Config) -> None:
    server, url = await _start(cfg)
    try:
        async with connect(url) as ws:
            await ws.send(json.dumps({"t": "hello", "device_id": "dev1", "ver": 1}))
            await ws.send(json.dumps({"t": "audio_start", "sid": 42}))
            for index in range(3):
                await ws.send(build_frame(TYPE_AUDIO_UP, index, b"\x01" * 40))
            await ws.send(json.dumps({"t": "audio_end", "sid": 42}))
            frames = await _recv_until(ws, "tts_end")
        # hello_ack（T1.4 定稿）不属于语音事件，做 sid/顺序断言前先剔除
        json_msgs = [msg for kind, msg in frames if kind == "json" and msg.get("t") != "hello_ack"]
        assert any(kind == "json" and msg.get("t") == "hello_ack" for kind, msg in frames), "缺少 hello_ack"
        bins = [msg for kind, msg in frames if kind == "bin"]
        types = [msg["t"] for msg in json_msgs]
        assert "asr_partial" in types and "asr_final" in types and "llm_delta" in types, types
        assert types.index("asr_final") < types.index("tts_start") < types.index("tts_end"), types
        assert all(msg.get("sid") == 42 for msg in json_msgs), "存在 sid 未回填的下行事件"
        tts_start = next(msg for msg in json_msgs if msg["t"] == "tts_start")
        assert tts_start["codec"] == TTS_CODEC and tts_start["sr"] == TTS_SAMPLE_RATE, tts_start
        assert len(bins) == cfg.max_tts_frames, f"二进制帧数 {len(bins)} != {cfg.max_tts_frames}"
        for expected_seq, raw in enumerate(bins):
            frame_type, seq, payload = parse_frame(raw)
            assert frame_type == TYPE_AUDIO_DOWN, f"下行帧类型错误 {frame_type}"
            assert seq == expected_seq, f"seq 应为 {expected_seq}，实为 {seq}"
            assert len(payload) == cfg.chunk_bytes, f"payload 长度 {len(payload)} != {cfg.chunk_bytes}"
        await _wait_closed(server, 1)
        assert server.history[-1].stats.frames_up == 3, "上行帧计数错误"
    finally:
        await server.stop()


async def _t_tts_mp3(cfg: Config) -> None:
    path = _write_selftest_mp3()
    data = Path(path).read_bytes()
    frames = parse_mp3_frames(data, source="self-test.mp3")
    assert len(frames) == _SELFTEST_MP3_FRAMES, f"帧数 {len(frames)} != {_SELFTEST_MP3_FRAMES}"
    assert sum(len(frame.payload) for frame in frames) == _SELFTEST_MP3_BYTES, "帧字节总和不符"
    assert all(
        frame.payload[0] == 0xFF and (frame.payload[1] & 0xE0) == 0xE0 for frame in frames
    ), "帧头不同步"
    assert all(frame.sample_rate == TTS_SAMPLE_RATE for frame in frames), "采样率应为 24000"
    assert abs(sum(frame.duration_ms for frame in frames) - 528.0) < 0.5, "时长应为 22 帧 × 24ms"
    # 真实 MP3 常带 ID3v2 标签：必须跳过标签后仍解析出同样的帧
    id3 = b"ID3\x04\x00\x00\x00\x00\x00\x64" + b"\x00" * 100 + data
    tagged = parse_mp3_frames(id3, source="self-test+id3")
    assert len(tagged) == len(frames) and tagged[0].payload == frames[0].payload, "ID3v2 跳过失败"

    server, url = await _start(replace(cfg, tts_mp3=path, max_tts_frames=0, tts_pace_pct=100))
    try:
        assert server.tts_mp3_frames is not None, "mock 未加载真实 MP3"
        async with connect(url) as ws:
            messages = await _collect_round(ws, sid=11)
        bins = [(ts, payload) for ts, kind, payload in messages if kind == "bin"]
        assert len(bins) == len(frames), f"下发帧数 {len(bins)} != 素材 {len(frames)}"
        for index, ((_, raw), expected) in enumerate(zip(bins, frames)):
            frame_type, seq, payload = parse_frame(raw)
            assert frame_type == TYPE_AUDIO_DOWN, f"第 {index} 帧类型 {frame_type}"
            assert seq == index, f"seq 应为 {index}，实为 {seq}"
            assert payload == expected.payload, f"第 {index} 帧 payload 与素材帧不一致"
        span = bins[-1][0] - bins[0][0]
        expected_span = sum(frame.duration_ms for frame in frames[:-1]) / 1000.0
        # 本地回环下按帧时长实时发送：允许 0.5~2 倍 + 0.3s 抖动容差
        assert expected_span * 0.5 <= span <= expected_span * 2.0 + 0.3, (
            f"节奏异常：首末帧间隔 {span:.3f}s，期望约 {expected_span:.3f}s（实时节奏）"
        )
        await _wait_closed(server, 1)
        stats = server.history[-1].stats
        assert stats.tts_frames == len(frames), f"tts_frames={stats.tts_frames}"
        assert stats.tts_bytes == sum(len(frame.payload) for frame in frames), (
            f"tts_bytes={stats.tts_bytes}"
        )
        assert stats.late_bins == 0 and stats.tts_stalls == 0
    finally:
        await server.stop()


async def _t_tts_stall(cfg: Config) -> None:
    path = _write_selftest_mp3()
    server, url = await _start(
        replace(
            cfg,
            tts_mp3=path,
            max_tts_frames=0,
            tts_pace_pct=100,
            tts_stall_ms=250,
            tts_stall_at=2,
        )
    )
    try:
        frames = server.tts_mp3_frames
        assert frames is not None
        async with connect(url) as ws:
            messages = await _collect_round(ws, sid=12)
        bins = [ts for ts, kind, _ in messages if kind == "bin"]
        assert len(bins) == len(frames), f"stall 不应丢帧：{len(bins)} != {len(frames)}"
        max_gap = max(bins[i + 1] - bins[i] for i in range(len(bins) - 1))
        assert max_gap >= 0.20, f"未观察到停发间隙（最大帧间隔 {max_gap:.3f}s < 0.20s）"
        await _wait_closed(server, 1)
        stats = server.history[-1].stats
        assert stats.tts_stalls == 1, f"stall 计数 {stats.tts_stalls} != 1"
        assert stats.late_bins == 0
    finally:
        await server.stop()


async def _t_tts_late(cfg: Config) -> None:
    path = _write_selftest_mp3()
    server, url = await _start(
        replace(cfg, tts_mp3=path, max_tts_frames=5, tts_pace_pct=0, tts_late_bin=True)
    )
    try:
        frames = server.tts_mp3_frames
        assert frames is not None
        async with connect(url) as ws:
            messages = await _collect_round(ws, sid=13, trailing_bins=1)
        bin_indexes = [i for i, (_, kind, _) in enumerate(messages) if kind == "bin"]
        tts_start_index = next(
            i
            for i, (_, kind, obj) in enumerate(messages)
            if kind == "json" and obj.get("t") == "tts_start"
        )
        tts_end_index = next(
            i
            for i, (_, kind, obj) in enumerate(messages)
            if kind == "json" and obj.get("t") == "tts_end"
        )
        assert len(bin_indexes) == 2 + 5, f"窗口外 2 帧 + 正常 5 帧，实收 {len(bin_indexes)}"
        assert bin_indexes[0] < tts_start_index, "tts_start 之前没有窗口外帧"
        assert bin_indexes[-1] > tts_end_index, "tts_end 之后没有窗口外帧"
        for expected_seq, index in enumerate(bin_indexes):
            frame_type, seq, _ = parse_frame(messages[index][2])
            assert frame_type == TYPE_AUDIO_DOWN, f"帧类型 {frame_type}"
            assert seq == expected_seq, f"seq 应为 {expected_seq}，实为 {seq}"
        await _wait_closed(server, 1)
        stats = server.history[-1].stats
        assert stats.late_bins == 2, f"late_bins={stats.late_bins} != 2"
        assert stats.tts_frames == 5, f"tts_frames={stats.tts_frames} != 5"
    finally:
        await server.stop()


async def _t_bad_frames(cfg: Config) -> None:
    server, url = await _start(cfg)
    try:
        async with connect(url) as ws:
            await ws.send(json.dumps({"t": "hello", "device_id": "dev1", "ver": 1}))
            await ws.send(b"\x00\x01\x02\x03\x04\x05\x06\x07")            # magic 错
            await ws.send(MAGIC + bytes((FRAME_VER, TYPE_AUDIO_UP)) + b"\x00\x00\x00\x05")  # len 不符
            await ws.send(build_frame(0x7F, 0, b""))                      # 未知类型
            await ws.send(json.dumps({"t": "ping"}))
            # 先收到 hello_ack（T1.4 定稿），再等 pong
            deadline = time.monotonic() + 1.0
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AssertionError("坏帧之后未在 1s 内收到 pong")
                msg = await _recv_json(ws, timeout=remaining)
                if msg.get("t") == "pong":
                    break
            assert msg.get("t") == "pong", "坏帧之后连接被误关闭"
        await _wait_closed(server, 1)
        session = server.history[-1]
        assert session.stats.bad_frames == 3, f"坏帧计数 {session.stats.bad_frames} != 3"
    finally:
        await server.stop()


async def _t_interrupt(cfg: Config) -> None:
    server, url = await _start(cfg)
    try:
        async with connect(url) as ws:
            await ws.send(json.dumps({"t": "hello", "device_id": "dev1", "ver": 1}))
            await ws.send(json.dumps({"t": "audio_start", "sid": 7}))
            await ws.send(json.dumps({"t": "audio_end", "sid": 7}))
            while True:
                msg = await _recv_json(ws, timeout=3.0)
                if msg["t"] == "tts_start":
                    break
            await asyncio.sleep(0.05)  # 让几帧下行真的发出去
            await ws.send(json.dumps({"t": "interrupt", "sid": 7}))
            # 已在 socket 缓冲里的帧允许收完，但绝不能再出现 tts_end
            deadline = time.monotonic() + 0.5
            leftovers: list[Any] = []
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    leftover = await asyncio.wait_for(ws.recv(), remaining)
                except asyncio.TimeoutError:
                    break
                leftovers.append(leftover)
            for item in leftovers:
                if isinstance(item, str):
                    assert json.loads(item).get("t") != "tts_end", "interrupt 后仍收到 tts_end"
        await _wait_closed(server, 1)
        session = server.history[-1]
        assert session.stats.interrupts == 1, f"interrupt 计数 {session.stats.interrupts} != 1"
        assert session.stats.tts_cancelled == 1, f"取消失败：tts_cancelled={session.stats.tts_cancelled}"
    finally:
        await server.stop()


async def _t_reconnect(cfg: Config) -> None:
    server, url = await _start(cfg)
    try:
        async with connect(url) as ws:
            await ws.send(json.dumps({"t": "hello", "device_id": "dev1", "ver": 1}))
            await ws.send(json.dumps({"t": "ping"}))
            # hello_ack 先到，跳过它再等 pong（T1.4 定稿）
            deadline = time.monotonic() + 1.0
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AssertionError("首连 ping 未收到 pong")
                msg = await _recv_json(ws, timeout=remaining)
                if msg.get("t") == "pong":
                    break
            assert msg.get("t") == "pong", "首连 ping 未收到 pong"
        await _wait_closed(server, 1)
        async with connect(url) as ws:
            await ws.send(json.dumps({"t": "ping"}))
            assert (await _recv_json(ws, 1.0)).get("t") == "pong", "重连后 ping 未收到 pong"
        await _wait_closed(server, 2)
        assert server.history[0].conn_id != server.history[1].conn_id, "重连未产生新会话"
    finally:
        await server.stop()


async def _t_drop_pong(cfg: Config) -> None:
    server, url = await _start(cfg)
    try:
        async with connect(url) as ws:
            await ws.send(json.dumps({"t": "ping"}))
            try:
                raw = await asyncio.wait_for(ws.recv(), 0.5)
            except asyncio.TimeoutError:
                return
            raise AssertionError(f"--drop-pong 却收到了响应：{raw!r}")
    finally:
        await server.stop()


async def _t_ping_during_tts(cfg: Config) -> None:
    server, url = await _start(cfg)
    try:
        async with connect(url) as ws:
            await ws.send(json.dumps({"t": "hello", "device_id": "dev1", "ver": 1}))
            await ws.send(json.dumps({"t": "audio_start", "sid": 3}))
            await ws.send(json.dumps({"t": "audio_end", "sid": 3}))
            while True:
                msg = await _recv_json(ws, timeout=3.0)
                if msg["t"] == "tts_start":
                    break
            # 端侧 20s 心跳会落在 TTS 流中间：mock 必须能在下发帧的同时回 pong
            await ws.send(json.dumps({"t": "ping"}))
            deadline = time.monotonic() + 1.0
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AssertionError("TTS 流中 ping 未在 1s 内收到 pong")
                try:
                    raw = await asyncio.wait_for(ws.recv(), remaining)
                except asyncio.TimeoutError:
                    raise AssertionError("TTS 流中 ping 未在 1s 内收到 pong") from None
                if isinstance(raw, str) and json.loads(raw).get("t") == "pong":
                    return
    finally:
        await server.stop()


async def _t_auth_rejected(cfg: Config) -> None:
    server, url = await _start(replace(cfg, token="secret"))
    try:
        async with connect(url) as ws:
            code = None
            try:
                await asyncio.wait_for(ws.recv(), 1.0)
            except ConnectionClosed as exc:
                code = exc.rcvd.code if exc.rcvd is not None else None
            assert code == CLOSE_BAD_TOKEN, f"token 不匹配时期望关闭码 {CLOSE_BAD_TOKEN}，实为 {code}"
    finally:
        await server.stop()


async def run_self_test() -> int:
    base = Config(
        host="127.0.0.1",
        port=0,
        think_ms=10,
        chunk_ms=1,
        max_tts_frames=5,
        tts_text="今天天气不错",
        quiet=True,
    )
    cases = [
        ("hello / hello_ack 握手与参数解析", _t_hello(base)),
        ("accept 连接 TCP_NODELAY=1（防 Nagle 假欠载）", _t_tcp_nodelay(base)),
        ("ping → pong 心跳", _t_ping(base)),
        ("一轮语音：事件顺序 / sid 回填 / 二进制帧头", _t_voice(base)),
        ("tts-mp3：真实 MP3 按帧切分 / 帧头连续 / 实时节奏", _t_tts_mp3(base)),
        ("tts-stall：中途停发注入欠载且不丢帧", _t_tts_stall(base)),
        ("tts-late-bin：tts_start 前 / tts_end 后各 1 帧窗口外注入", _t_tts_late(base)),
        ("坏帧容错（magic / len / type）后连接仍存活", _t_bad_frames(base)),
        ("interrupt 取消下发且不再发 tts_end", _t_interrupt(replace(base, chunk_ms=20, max_tts_frames=100))),
        ("断线重连后新会话可用", _t_reconnect(base)),
        ("TTS 流进行中 ping 仍能收到 pong", _t_ping_during_tts(replace(base, chunk_ms=20, max_tts_frames=100))),
        ("token 不匹配时按约定关闭码拒绝", _t_auth_rejected(base)),
        ("--drop-pong 不回 pong（端侧超时路径）", _t_drop_pong(replace(base, drop_pong=True))),
    ]

    passed = 0
    for index, (name, coro) in enumerate(cases, 1):
        try:
            await coro
        except Exception as exc:  # 自测里用 assert，失败即 FAIL，不中断其它用例
            print(f"[{index}/{len(cases)}] FAIL  {name}: {exc}", flush=True)
        else:
            print(f"[{index}/{len(cases)}] PASS  {name}", flush=True)
            passed += 1
    failed = len(cases) - passed
    print(f"self-test: {passed}/{len(cases)} PASS" + ("" if failed == 0 else f"（{failed} FAIL）"), flush=True)
    return 0 if failed == 0 else 1


# ---------------- CLI ----------------


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="本地云端 WebSocket 模拟服务（T1.5 / T2.4）",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--host", default="0.0.0.0", help="监听地址；0.0.0.0 允许局域网设备直连")
    parser.add_argument("--port", type=int, default=8765, help="监听端口；0 表示由系统分配（自测用）")
    parser.add_argument("--device-id", default="", help="非空则校验 query.device_id")
    parser.add_argument("--token", default="", help="非空则校验 query.token")
    parser.add_argument("--scenario", choices=("normal", "error", "music"), default="normal")
    parser.add_argument("--think-ms", type=int, default=300, help="audio_end 到首个下行事件的延时")
    parser.add_argument("--chunk-ms", type=int, default=20, help="合成帧发送间隔（--tts-mp3 时按帧时长）")
    parser.add_argument("--chunk-bytes", type=int, default=192, help="合成 TTS payload 长度")
    parser.add_argument(
        "--max-tts-frames",
        type=int,
        default=None,
        help="一轮 TTS 的二进制帧上限；缺省合成帧 100、--tts-mp3 时全发；"
             "MP3 模式下 0 表示不限",
    )
    parser.add_argument("--tts-text", default=Config.tts_text, help="模拟识别的文本")
    parser.add_argument("--no-tts", action="store_true", help="只发 JSON 事件，不发二进制帧")
    parser.add_argument("--inject-late", action="store_true",
                        help="每轮先发一个 sid+1 的 asr_final（测端侧迟到帧过滤）")
    parser.add_argument("--drop-pong", action="store_true", help="收到 ping 不回 pong（测端侧超时）")
    parser.add_argument("--close-after", type=float, default=0.0, help=">0 时到点主动断开（测重连）")
    parser.add_argument("--save-upload", default="", help="把上行 payload 落盘到该目录")
    # ---- T2.4：真实 MP3 下发（契约 §6） ----
    parser.add_argument(
        "--tts-mp3",
        default="",
        help="下发真实 MP3 文件（按 MP3 帧边界切分，缺省按帧时长实时节奏）；缺省仍发合成帧",
    )
    parser.add_argument(
        "--tts-stall-ms",
        type=int,
        default=0,
        help="在 --tts-stall-at 帧后停发 n ms，制造端侧欠载（0=不停发）",
    )
    parser.add_argument("--tts-stall-at", type=int, default=30, help="stall 帧序号（0 基，默认 30）")
    parser.add_argument(
        "--tts-late-bin",
        action="store_true",
        help="每轮 tts_start 前 / tts_end 后各发 1 帧 0x02（验证端侧窗口外丢弃）",
    )
    parser.add_argument(
        "--tts-pace-pct",
        type=int,
        default=100,
        help="真实 MP3 发送节奏：帧时长的百分比（100=实时；测试可略快，如 90）",
    )
    parser.add_argument("--verbose", action="store_true", help="打印上行 payload 前 16 字节")
    parser.add_argument("--self-test", action="store_true", help="进程内自测，不需要硬件")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.tts_mp3 and not Path(args.tts_mp3).is_file():
        raise SystemExit(f"--tts-mp3 文件不存在：{args.tts_mp3}")
    if not 0 <= args.tts_stall_ms:
        raise SystemExit(f"--tts-stall-ms 必须 >= 0（当前 {args.tts_stall_ms}）")
    if args.tts_stall_at < 0:
        raise SystemExit(f"--tts-stall-at 必须 >= 0（当前 {args.tts_stall_at}）")
    if not 1 <= args.tts_pace_pct <= 400:
        raise SystemExit(f"--tts-pace-pct 必须在 1~400（当前 {args.tts_pace_pct}）")
    max_tts_frames = args.max_tts_frames
    if max_tts_frames is None:
        max_tts_frames = 0 if args.tts_mp3 else 100
    cfg = Config(
        host=args.host,
        port=args.port,
        device_id=args.device_id,
        token=args.token,
        scenario=args.scenario,
        think_ms=args.think_ms,
        chunk_ms=args.chunk_ms,
        chunk_bytes=max(FRAME_HEADER_LEN, args.chunk_bytes),
        max_tts_frames=max(0, max_tts_frames),
        tts_text=args.tts_text,
        no_tts=args.no_tts,
        inject_late=args.inject_late,
        drop_pong=args.drop_pong,
        close_after=args.close_after,
        save_upload=args.save_upload,
        tts_mp3=args.tts_mp3,
        tts_stall_ms=args.tts_stall_ms,
        tts_stall_at=args.tts_stall_at,
        tts_late_bin=args.tts_late_bin,
        tts_pace_pct=args.tts_pace_pct,
        quiet=args.self_test,
        verbose=args.verbose,
    )
    _setup_logging(cfg)

    if args.self_test:
        return asyncio.run(run_self_test())

    server = MockServer(cfg)
    try:
        return asyncio.run(_serve_forever(server))
    except KeyboardInterrupt:
        print(flush=True)  # 换行，避免 ^C 与最后一行日志粘连
        LOG.info("收到 Ctrl-C，退出")
        return 0
    except RuntimeError as exc:  # --tts-mp3 解析失败等启动期错误
        print(f"mock 启动失败：{exc}", file=sys.stderr, flush=True)
        return 2


if __name__ == "__main__":
    sys.exit(main())
