"""mock_server —— Session（一条设备连接）与 MockServer（WS 服务）。

从 tools/ws_mock_server.py 拆出（2026-11-03，行为不变）。Session 负责握手、心跳、
上行帧校验、事件分派与 music 骨架；下行 TTS 流见 tools/mock_downlink.py。
"""

from __future__ import annotations

import asyncio
import json
import logging
import socket
import sys
from typing import Any
from urllib.parse import parse_qs, urlsplit

import websockets
from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed

from mock_core import (
    CLOSE_BAD_DEVICE,
    CLOSE_BAD_TOKEN,
    ERROR_TTS_FRAMES,
    FRAME_VER,
    LOG,
    TTS_SAMPLE_RATE,
    TYPE_AUDIO_DOWN,
    TYPE_AUDIO_UP,
    Config,
    FrameError,
    Mp3Frame,
    Stats,
    build_frame,
    lan_ips,
    load_mp3_frames,
    parse_frame,
    protocol_logger,
    valid_sid,
)
from mock_downlink import DownlinkMixin
from mock_fixtures import embedded_real_mp3_frames


class Session(DownlinkMixin):
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
        # --asr：本轮上行 Opus payload（audio_start 清空、audio_end 取走送识别）
        self.up_packets: list[bytes] = []
        self.up_packet_bytes = 0
        self.asr_overflow = False

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
        if self.server.asr is not None:
            self._collect_asr_payload(payload)

    def _collect_asr_payload(self, payload: bytes) -> None:
        """缓存本轮上行 Opus payload，供 audio_end 后送 ASR（有上限，避免内存失控）。"""
        max_frames = max(1, int(self.cfg.asr_max_seconds * 1000 / 20))
        if len(self.up_packets) >= max_frames:
            if not self.asr_overflow:
                self.asr_overflow = True
                self._log(
                    logging.WARNING,
                    f"本轮上行超过 ASR 上限（{self.cfg.asr_max_seconds:.0f}s / {max_frames} 帧），"
                    "超出部分不参与识别",
                )
            return
        self.up_packets.append(payload)
        self.up_packet_bytes += len(payload)

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
            if not valid_sid(sid):
                self._log(logging.WARNING, f"audio_start 的 sid 非 uint16：{sid!r}，下发时会缺 sid")
            self.sid = sid if valid_sid(sid) else None
            self.up_seq_expected = None          # 新的一轮重新累计上行 seq
            self.up_round_frames = 0
            self.up_packets = []
            self.up_packet_bytes = 0
            self.asr_overflow = False
            self.stats.voice_sessions += 1
            self._log(logging.INFO, f"<-- audio_start sid={self.sid}")

        elif event == "audio_end":
            sid = msg.get("sid", self.sid)
            self._log(logging.INFO, f"<-- audio_end sid={sid}（本轮上行 {self.up_round_frames} 帧）")
            if not valid_sid(sid):
                self._log(logging.WARNING, f"audio_end 的 sid 非 uint16：{sid!r}")
            else:
                self.sid = sid
            await self._stop_tts("重复的 audio_end")
            # 取走本轮上行帧（识别在 tts_flow 任务里做，不阻塞 WS 收包/心跳）
            asr_packets, self.up_packets = self.up_packets, []
            self.up_packet_bytes = 0
            self.tts_task = asyncio.create_task(self._tts_flow(asr_packets))

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
        # T3.6：tts_start 的 codec/sr 与 error 的 code 是测试断言依据，日志里带上
        if obj.get("t") == "tts_start":
            extra += f" codec={obj.get('codec')} sr={obj.get('sr')}"
        if obj.get("t") == "error":
            extra += f" code={obj.get('code')} msg={obj.get('msg')!r}"
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

    def _music_url(self) -> str:
        return f"http://{self.server.banner_host()}:{self.server.port}/media/mock.mp3"

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
    def __init__(self, cfg: Config, asr: Any = None, tts_qwen_module: Any = None) -> None:
        self.cfg = cfg
        self.port = cfg.port
        self.asr = asr  # mock_asr.MockAsr 实例（--asr）；None = 固定文案假 ASR
        self.tts_qwen_module = tts_qwen_module  # tools/mock_tts.py 模块（--tts-qwen，Qwen3-TTS）
        self._server: Any = None
        self._next_conn = 1
        self.active: dict[int, Session] = {}
        self.history: list[Session] = []
        # --tts-mp3：启动时解析一次，所有连接共享（只读）；None 表示合成帧模式
        self.tts_mp3_frames: list[Mp3Frame] | None = None
        # T3.6：--error-stage after-tts-start 用的真实 MP3 帧（--tts-mp3 优先，否则用内嵌段）
        self._embedded_mp3_frames: list[Mp3Frame] | None = None

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
            logger=protocol_logger(),
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
        ips = lan_ips()
        return ips[0] if ips else "127.0.0.1"

    def error_audio_frames(self) -> list[Mp3Frame]:
        """--error-stage after-tts-start 下发用的真实 24k/单声道 MP3 帧（T3.6）。

        优先用 --tts-mp3 的素材；没给时用脚本内嵌的真实 MP3（与 --self-test 同一段，
        22 帧/528ms）——这样「SPEAKING 中云端报错」场景即使不配素材，设备也能真的解码
        出声音，而不是收到不可解码的合成帧。
        """
        if self.tts_mp3_frames is not None:
            return self.tts_mp3_frames
        if self._embedded_mp3_frames is None:
            self._embedded_mp3_frames = embedded_real_mp3_frames()
        return self._embedded_mp3_frames

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

def _print_banner(server: MockServer) -> None:
    cfg = server.cfg
    device_id = cfg.device_id or "dev1"
    token = cfg.token or "dev-token"
    hosts = ["127.0.0.1"]
    if cfg.host in ("0.0.0.0", "", "::"):
        hosts += lan_ips()
    else:
        hosts = [cfg.host]

    lines = [
        "=" * 78,
        "ws_mock_server —— 本地云端模拟（T1.5 / T2.4）",
        f"python {sys.version.split()[0]} / websockets {websockets.__version__}",
        f"scenario={cfg.scenario} think={cfg.think_ms}ms chunk={cfg.chunk_bytes}B/{cfg.chunk_ms}ms "
        f"max_tts_frames={cfg.max_tts_frames} no_tts={cfg.no_tts} drop_pong={cfg.drop_pong}"
        + (f" error_code={cfg.error_code} error_stage={cfg.error_stage}"
           if cfg.scenario == "error" else "")
        + (" tts_bad_codec" if cfg.tts_bad_codec else "")
        + (f" close_after={cfg.close_after}s" if cfg.close_after else ""),
        _tts_mode_line(server),
        (
            f"ASR 模式: {cfg.asr}（本地 SenseVoice，模型 {server.asr.model_dir}；失败回退 --tts-text）"
            if server.asr is not None
            else "ASR 模式: 关闭（asr_final 固定用 --tts-text 文案）"
        ),
        (
            f"TTS {'回答' if cfg.llm else '复读'}: 开（{'LLM 回答' if cfg.llm else 'ASR 文本'} → Qwen3-TTS {cfg.tts_voice} 合成；"
            f"{'流式' if cfg.tts_stream else '整句'}下发）"
            if server.tts_qwen_module is not None
            else "TTS 复读: 关"
        ),
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
    if cfg.tts_bad_codec:
        return (
            'TTS 模式: --tts-bad-codec（tts_start codec="pcm" → tts_end、不发音频帧；'
            "测设备侧 TTS 无法播放 → tts-fail 提示音）"
        )
    if cfg.scenario == "error":
        return (
            f"错误注入: code={cfg.error_code} stage={cfg.error_stage}"
            + ("（先发 asr_final）" if cfg.error_stage == "after-asr" else "")
            + (
                f"（先发 tts_start + {ERROR_TTS_FRAMES} 帧真实 MP3）"
                if cfg.error_stage == "after-tts-start"
                else ""
            )
        )
    if server.tts_qwen_module is not None:
        mode = (
            f"TTS 模式: 回声（ASR 识别文本 → Qwen3-TTS {cfg.tts_voice} → 24k MP3，"
            f"按帧下发，节奏={cfg.tts_pace_pct}%）"
        )
    elif frames is None:
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

async def serve_forever(server: MockServer) -> int:
    await server.start()
    _print_banner(server)
    try:
        await asyncio.Event().wait()  # 只靠 Ctrl-C 退出
    finally:
        await server.stop()
        LOG.info("mock 服务已停止")
    return 0
