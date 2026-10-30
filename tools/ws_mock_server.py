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

传输说明：本地 mock 是 ws://（不加密）；线上契约是 wss://（ARCH §6.1）。
TLS/证书策略在 T1.4 决定，见 docs/PRODUCT_IDEAS.md §8 X2。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import socket
import sys
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
    drop_pong: bool = False      # 故意不回 pong，测端侧 10s 心跳超时
    close_after: float = 0.0     # >0 时到点主动断开，测端侧重连
    save_upload: str = ""        # 非空则把上行 payload 落盘到该目录
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

    def summary(self) -> str:
        duration = time.monotonic() - self.started_at
        return (
            f"时长 {duration:.1f}s | 上行 {self.frames_up} 帧/{self.bytes_up}B | "
            f"下行 {self.frames_down} 帧/{self.bytes_down}B | "
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
            # §6.1 未定义服务端 hello 应答：这里刻意不发额外帧。
            # 若 T1.4 发现端侧必须有 ack，先改 ARCHITECTURE §6 再改本脚本。

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

    async def _send_bin(self, payload: bytes) -> None:
        seq = self.seq_out
        frame = build_frame(TYPE_AUDIO_DOWN, seq, payload)
        await self.ws.send(frame)
        self.seq_out = (self.seq_out + 1) & 0xFFFF
        self.stats.frames_down += 1
        self.stats.bytes_down += len(payload)
        self._log(logging.INFO, f"--> bin type=0x02 seq={seq} len={len(payload)} sid={self.sid}")

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

    async def _tts_flow(self) -> None:
        sid = self.sid
        try:
            if self.cfg.think_ms > 0:
                await asyncio.sleep(self.cfg.think_ms / 1000)

            if self.cfg.scenario == "error":
                self.stats.errors += 1
                await self._send_json(
                    {"t": "error", "sid": sid, "code": 1, "msg": "mock: 模拟 TTS 失败"}
                )
                self._log(
                    logging.WARNING,
                    "error 码表契约未定义（§6.3 只有 '错误码与描述'），P2/T2.5 补 PROTOCOL 后再统一",
                )
                return

            text = self.cfg.tts_text
            await self._send_json({"t": "asr_partial", "sid": sid, "text": text[: max(1, len(text) // 2)]})
            await self._send_json({"t": "asr_final", "sid": sid, "text": text})
            for offset in range(0, len(text), 5):
                await self._send_json({"t": "llm_delta", "sid": sid, "text": text[offset : offset + 5]})

            await self._send_json(
                {"t": "tts_start", "sid": sid, "codec": TTS_CODEC, "sr": TTS_SAMPLE_RATE}
            )
            if self.cfg.no_tts:
                self._log(logging.INFO, "（--no-tts：跳过二进制帧，仅验证 JSON 链路）")
            else:
                for index in range(self.cfg.max_tts_frames):
                    await self._send_bin(self._synth_tts_payload(index))
                    if self.cfg.chunk_ms > 0:
                        await asyncio.sleep(self.cfg.chunk_ms / 1000)
            await self._send_json({"t": "tts_end", "sid": sid})

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


class MockServer:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.port = cfg.port
        self._server: Any = None
        self._next_conn = 1
        self.active: dict[int, Session] = {}
        self.history: list[Session] = []

    async def start(self) -> int:
        # ping_interval=None：关掉 WS 协议层心跳，避免与契约里的应用层 {"t":"ping"} 混淆
        self._server = await serve(
            self._handler,
            self.cfg.host,
            self.cfg.port,
            ping_interval=None,
            max_size=64 * 1024,
            logger=_protocol_logger(),
        )
        self.port = self._server.sockets[0].getsockname()[1]
        return self.port

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
        "ws_mock_server —— 本地云端模拟（T1.5）",
        f"python {sys.version.split()[0]} / websockets {websockets.__version__}",
        f"scenario={cfg.scenario} think={cfg.think_ms}ms chunk={cfg.chunk_bytes}B/{cfg.chunk_ms}ms "
        f"max_tts_frames={cfg.max_tts_frames} no_tts={cfg.no_tts} drop_pong={cfg.drop_pong}"
        + (f" close_after={cfg.close_after}s" if cfg.close_after else ""),
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


async def _t_hello(cfg: Config) -> None:
    server, url = await _start(cfg)
    try:
        async with connect(url) as ws:
            await ws.send(json.dumps({"t": "hello", "device_id": "dev1", "ver": 1}))
        await _wait_closed(server, 1)
        session = server.history[-1]
        assert session.stats.hello_seen, "服务端没看到 hello"
        assert session.hello_info.get("device_id") == "dev1", "hello.device_id 解析错误"
        assert session.hello_info.get("ver") == 1, "hello.ver 解析错误"
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
        json_msgs = [msg for kind, msg in frames if kind == "json"]
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


async def _t_bad_frames(cfg: Config) -> None:
    server, url = await _start(cfg)
    try:
        async with connect(url) as ws:
            await ws.send(json.dumps({"t": "hello", "device_id": "dev1", "ver": 1}))
            await ws.send(b"\x00\x01\x02\x03\x04\x05\x06\x07")            # magic 错
            await ws.send(MAGIC + bytes((FRAME_VER, TYPE_AUDIO_UP)) + b"\x00\x00\x00\x05")  # len 不符
            await ws.send(build_frame(0x7F, 0, b""))                      # 未知类型
            await ws.send(json.dumps({"t": "ping"}))
            msg = await _recv_json(ws, timeout=1.0)
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
            assert (await _recv_json(ws, 1.0)).get("t") == "pong", "首连 ping 未收到 pong"
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
        ("hello 握手与参数解析", _t_hello(base)),
        ("ping → pong 心跳", _t_ping(base)),
        ("一轮语音：事件顺序 / sid 回填 / 二进制帧头", _t_voice(base)),
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
        description="本地云端 WebSocket 模拟服务（T1.5）",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--host", default="0.0.0.0", help="监听地址；0.0.0.0 允许局域网设备直连")
    parser.add_argument("--port", type=int, default=8765, help="监听端口；0 表示由系统分配（自测用）")
    parser.add_argument("--device-id", default="", help="非空则校验 query.device_id")
    parser.add_argument("--token", default="", help="非空则校验 query.token")
    parser.add_argument("--scenario", choices=("normal", "error", "music"), default="normal")
    parser.add_argument("--think-ms", type=int, default=300, help="audio_end 到首个下行事件的延时")
    parser.add_argument("--chunk-ms", type=int, default=20, help="二进制帧发送间隔")
    parser.add_argument("--chunk-bytes", type=int, default=192, help="合成 TTS payload 长度")
    parser.add_argument("--max-tts-frames", type=int, default=100, help="一轮 TTS 的二进制帧上限")
    parser.add_argument("--tts-text", default=Config.tts_text, help="模拟识别的文本")
    parser.add_argument("--no-tts", action="store_true", help="只发 JSON 事件，不发二进制帧")
    parser.add_argument("--drop-pong", action="store_true", help="收到 ping 不回 pong（测端侧超时）")
    parser.add_argument("--close-after", type=float, default=0.0, help=">0 时到点主动断开（测重连）")
    parser.add_argument("--save-upload", default="", help="把上行 payload 落盘到该目录")
    parser.add_argument("--verbose", action="store_true", help="打印上行 payload 前 16 字节")
    parser.add_argument("--self-test", action="store_true", help="进程内自测，不需要硬件")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    cfg = Config(
        host=args.host,
        port=args.port,
        device_id=args.device_id,
        token=args.token,
        scenario=args.scenario,
        think_ms=args.think_ms,
        chunk_ms=args.chunk_ms,
        chunk_bytes=max(FRAME_HEADER_LEN, args.chunk_bytes),
        max_tts_frames=max(1, args.max_tts_frames),
        tts_text=args.tts_text,
        no_tts=args.no_tts,
        drop_pong=args.drop_pong,
        close_after=args.close_after,
        save_upload=args.save_upload,
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


if __name__ == "__main__":
    sys.exit(main())
