"""mock_core —— 协议层核心：二进制帧 / MP3 帧解析 / 运行配置与统计 / 日志。

从 tools/ws_mock_server.py 拆出（2026-11-03，行为不变）：这里只有常量、纯函数与数据类，
不含会话与网络逻辑，便于复用和单测。

契约唯一依据（不要在这里发明字段）：
  - docs/ARCHITECTURE.md §6.1 连接与鉴权、§6.2 二进制帧、§6.3 文本帧
  - docs/PROTOCOL.md §2–§4（帧头/事件/错误码）
"""

from __future__ import annotations

import logging
import socket
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

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

# T3.6：云端错误注入与设备侧 TTS 失败注入（测试工具用，不是协议扩展——error 事件与 tts_start
# 字段都来自 PROTOCOL.md §3.2/§4，这里只是让 mock 能按时机/按错误码下发）
TTS_BAD_CODEC = "pcm"  # 设备不支持的可播放 codec：端侧应上报 VC_EVT_TTS_FAIL 而不是静默失败
ERROR_STAGES = ("after-audio-end", "after-asr", "after-tts-start")
ERROR_TTS_FRAMES = 4   # after-tts-start：报错前下发的真实 MP3 帧数（契约 §5.1 要求 3~5 帧）

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


def valid_sid(value: Any) -> bool:
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
    # ---- T3.1 测试：本地真 ASR（默认关闭，假 ASR 路径保持零额外依赖） ----
    asr: str = "none"            # none / sensevoice（见 tools/mock_asr.py）
    asr_model_dir: str = ""      # SenseVoice 模型目录；空则自动找缓存/DSH 缓存
    asr_download: bool = False   # 模型缺失时下载（约 239MB）
    asr_threads: int = 2         # ONNX 推理线程数
    asr_language: str = "auto"   # SenseVoice 语言提示
    asr_max_seconds: float = 30.0  # 单轮最多送 ASR 的音频时长（超出截断）
    # ---- T3.1 测试：ASR 文本 → 本地 TTS（Piper）复读（回声测试） ----
    tts_piper: bool = False          # True：把 ASR 识别文本用 Piper 合成后下发（需 --asr）
    tts_piper_voice: str = "zh_CN-huayan-medium"
    tts_piper_speed: float = 1.0     # Piper length_scale：>1 更慢
    tts_piper_model_dir: str = ""    # Piper 音色目录（空=自动找缓存）
    tts_piper_download: bool = False # 音色缺失时下载（约 60MB）
    # ---- T3.6：错误提示（云端错误码/报错时机/坏 codec 注入；error_prompt_test.py 用） ----
    error_code: int = 1                   # --scenario error 下发的 error.code（默认 1=通用）
    error_stage: str = "after-audio-end"  # audio_end 后 / asr_final 后 / tts_start+真实 MP3 帧后
    tts_bad_codec: bool = False           # tts_start 用 codec="pcm" 且不发音频帧（设备侧 TTS 失败）
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
    # ---- T3.1：真 ASR 统计（--asr 时才有值） ----
    asr_rounds: int = 0
    asr_errors: int = 0
    asr_audio_ms: int = 0
    asr_infer_ms: int = 0
    tts_piper_errors: int = 0  # 复读合成失败次数（回退合成帧，不中断协议链路）

    def summary(self) -> str:
        duration = time.monotonic() - self.started_at
        extra = f" | TTS 下发 {self.tts_frames} 帧/{self.tts_bytes}B"
        if self.late_bins:
            extra += f" 窗口外注入 {self.late_bins} 帧"
        if self.tts_stalls:
            extra += f" stall {self.tts_stalls} 次"
        if self.tts_piper_errors:
            extra += f" 复读合成失败 {self.tts_piper_errors} 次"
        if self.asr_rounds or self.asr_errors:
            extra += (
                f" | ASR {self.asr_rounds} 轮成功/{self.asr_errors} 失败"
                f"（音频 {self.asr_audio_ms}ms / 推理 {self.asr_infer_ms}ms）"
            )
        return (
            f"时长 {duration:.1f}s | 上行 {self.frames_up} 帧/{self.bytes_up}B | "
            f"下行 {self.frames_down} 帧/{self.bytes_down}B{extra} | "
            f"ping={self.pings} pong={self.pongs} | 语音轮次={self.voice_sessions} "
            f"打断={self.interrupts} 坏帧={self.bad_frames} seq跳变={self.uplink_seq_gaps}"
        )

def lan_ips() -> list[str]:
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

def setup_logging(cfg: Config) -> None:
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


def protocol_logger() -> logging.Logger:
    """websockets 协议层日志独立命名，避免与 mock 自己的收发日志混在一起。"""
    return logging.getLogger("ws_mock.proto")
