#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "mlx-audio>=0.6",
#   "numpy>=1.24",
#   "lameenc>=1.7",
#   "huggingface_hub>=0.24",
# ]
# ///
"""mock_tts.py —— mock 的本地 TTS 后端（Qwen3-TTS / MLX）：文字 → 24kHz MP3（可流式）

**2026-11-04 起只保留 Qwen3-TTS**（Piper/say 已移除）：Piper 停顿处有 -44 dBFS 合成噪声、
峰值 0 dBFS（端侧会撞软限幅 → 听感"嚓嚓"），Qwen3-TTS-0.6B 实测峰值约 -5~-12 dBFS（句子/语气不同有波动）、
停顿干净，且 12Hz tokenizer 支持流式（首块约 2.7s，之后约 3× 实时）。

两个入口：
  - `stream_frames(text)`：**流式**，边合成边吐 MP3 帧（mock 的默认路径，首帧不等整句）；
  - `synthesize_mp3(text)`：一次性合成整句再编码（给 `--tts-mp3` 类离线用法与单测）。

模型解析顺序：`--model` → 环境变量 `QWEN_TTS_MODEL` → 默认
`mlx-community/Qwen3-TTS-12Hz-0.6B-CustomVoice-bf16`；先查本地 HF 缓存（离线可用），
未命中才交给 mlx_audio 下载（国内建议 `HF_ENDPOINT=https://hf-mirror.com`）。

单独运行（不接设备，只生成 MP3，便于试听）：
  uv run tools/mock_tts.py --text "今天天气不错，适合出门散步。" --out /tmp/reply.mp3
  uv run tools/mock_tts.py --text "..." --voice vivian --instruct "用温柔的语气说" --out /tmp/reply.mp3

接入 mock（ASR 文本 → Qwen3-TTS 复读）：
  uv run --with av --with numpy --with sherpa-onnx --with mlx-audio --with lameenc \\
    tools/ws_mock_server.py --asr sensevoice --tts-qwen
"""

from __future__ import annotations

import argparse
import os
import sys
import time
import wave
from pathlib import Path

import numpy as np

TARGET_RATE = 24000  # 设备 TTS 契约：24kHz 单声道
MP3_BITRATE = 96     # kbps；24k 单声道。实测 MP3 往返 SNR：64k=16.4dB / 96k=20.2dB / 128k=22.8dB
                     # （环境变量 MOCK_TTS_BITRATE 可覆盖；追求音质用 128）
DEFAULT_MODEL = "mlx-community/Qwen3-TTS-12Hz-0.6B-CustomVoice-bf16"
DEFAULT_VOICE = "serena"

# 音频电平：Qwen3-TTS 输出峰值约 -5~-12 dBFS（句子/语气不同有波动），默认**不再归一**（不动动态）。
# `MOCK_TTS_PEAK_DBFS=<dBFS>` 可把峰值缩到指定档（例如 -7.8 与提示音同档），`off` = 不处理。
TTS_PEAK_DBFS: float | None = None


# ---------------------------------------------------------------- 文本过滤


def speakable_text(text: str) -> str:
    """挑出真正可发音的字符（去掉标点/空白）。

    ASR 在"没听到语音"时常返回 "." / "。" 这类纯标点文本；直接喂 TTS 会生成 0 帧。
    """
    return "".join(ch for ch in text if ch.isalnum() or "\u4e00" <= ch <= "\u9fff")


# ---------------------------------------------------------------- 模型解析/加载


def resolve_model(ref: str) -> str:
    """本地目录直接用；否则先查 HF 缓存（离线命中），最后才交给 mlx_audio 按 repo id 下载。"""
    path = Path(ref).expanduser()
    if path.is_dir():
        return str(path)
    try:
        from huggingface_hub import snapshot_download

        return snapshot_download(ref, local_files_only=True)
    except Exception:  # noqa: BLE001 - 没缓存就走在线解析
        return ref


class Qwen3Tts:
    """Qwen3-TTS 极简封装：load 一次，之后按句流式合成（yield float32 PCM）。"""

    def __init__(self, model: str = DEFAULT_MODEL, *, voice: str | None = DEFAULT_VOICE,
                 lang_code: str = "auto", instruct: str = "",
                 stream_interval: float = 0.5, log=print) -> None:
        self.model_ref = resolve_model(model)
        self.voice = voice or None
        self.lang_code = lang_code
        self.instruct = instruct or None
        self.stream_interval = stream_interval
        self._log = log
        self._model = None

    def load(self) -> None:
        if self._model is not None:
            return
        started = time.monotonic()
        self._log(f"[TTS] 加载 Qwen3-TTS：{self.model_ref} ...")
        from mlx_audio.tts.utils import load_model  # 延迟 import：不装 mlx 也能 --help

        self._model = load_model(self.model_ref)
        self._log(f"[TTS] 就绪（加载 {time.monotonic() - started:.2f}s）")

    def stream(self, text: str):
        """yield (float32 PCM [-1,1], sample_rate)；`stream=True` 时块约 stream_interval 秒。"""
        self.load()
        kwargs = {
            "voice": self.voice,
            "lang_code": self.lang_code,
            "stream": True,
            "streaming_interval": self.stream_interval,
        }
        if self.instruct:
            kwargs["instruct"] = self.instruct
        for result in self._model.generate(text, **kwargs):
            audio = np.asarray(result.audio, dtype=np.float32).reshape(-1)
            if audio.size:
                yield audio, int(result.sample_rate)


# ---------------------------------------------------------------- 电平/编码

_ENGINE: Qwen3Tts | None = None
_ENGINE_KEY: tuple | None = None


def get_engine(*, model: str = DEFAULT_MODEL, voice: str | None = DEFAULT_VOICE,
               lang_code: str = "auto", instruct: str = "",
               stream_interval: float = 0.5, log=print) -> Qwen3Tts:
    """进程级单例：mock 只应加载一次模型（13~15s、约 1.2GB 内存）。"""
    global _ENGINE, _ENGINE_KEY
    key = (model, voice, lang_code, instruct, stream_interval)
    if _ENGINE is None or _ENGINE_KEY != key:
        _ENGINE = Qwen3Tts(model, voice=voice, lang_code=lang_code, instruct=instruct,
                           stream_interval=stream_interval, log=log)
        _ENGINE_KEY = key
    return _ENGINE


def preload(*, model: str = DEFAULT_MODEL, voice: str | None = DEFAULT_VOICE,
            lang_code: str = "auto", instruct: str = "",
            stream_interval: float = 0.5, warmup: bool = True, log=print) -> None:
    """启动时预热（mock 用）：加载模型 + **跑一次短句把首调用预热掉**。

    实测（2026-11-04）：模型 `load()` 只是把权重读进来，**第一次 `generate()` 还要付
    2.8s 的 MLX kernel 编译/首次 eval**；预热后首块只要 ~0.2s。不预热的话，设备第一轮
    `tts_start → 首帧` 会多等 2~3s（仍在 10s 超时内，但体验差）。
    """
    engine = get_engine(model=model, voice=voice, lang_code=lang_code, instruct=instruct,
                        stream_interval=stream_interval, log=log)
    engine.load()
    if not warmup:
        return
    started = time.monotonic()
    got = 0
    for pcm, _sr in engine.stream("嗯。"):
        got += pcm.size
        break  # 只取首块，够把 kernel 预热掉
    log(f"[TTS] 预热完成（首块 {time.monotonic() - started:.2f}s，{got} 样本；"
        f"之后首块约 0.2s）")


class BiquadLowpass:
    """二阶 RBJ 低通（可级联）；用于压 Qwen3-TTS 的"特别亮/颗粒感"。

    环境变量 `MOCK_TTS_LOWPASS_HZ`（默认 0=关闭；试音时常用 7000~8000）。
    跨块保持滤波器状态，流式合成时块边界不会重新起步。
    """

    def __init__(self, cutoff_hz: float, rate: int = TARGET_RATE, q: float = 0.7071) -> None:
        import math

        w0 = 2.0 * math.pi * cutoff_hz / rate
        cos_w0, alpha = math.cos(w0), math.sin(w0) / (2.0 * q)
        b0, b1, b2 = (1 - cos_w0) / 2, 1 - cos_w0, (1 - cos_w0) / 2
        a0, a1, a2 = 1 + alpha, -2 * cos_w0, 1 - alpha
        self.b = (b0 / a0, b1 / a0, b2 / a0)
        self.a = (a1 / a0, a2 / a0)
        self.x1 = self.x2 = self.y1 = self.y2 = 0.0

    def process(self, x: np.ndarray) -> np.ndarray:
        out = np.empty_like(x, dtype=np.float32)
        b0, b1, b2 = self.b
        a1, a2 = self.a
        x1, x2, y1, y2 = self.x1, self.x2, self.y1, self.y2
        for i, sample in enumerate(x):
            y = b0 * sample + b1 * x1 + b2 * x2 - a1 * y1 - a2 * y2
            x2, x1 = x1, sample
            y2, y1 = y1, y
            out[i] = y
        self.x1, self.x2, self.y1, self.y2 = x1, x2, y1, y2
        return out


def env_gain_db() -> float:
    """合成侧整体增益（可正可负；默认 0）。用于把 Qwen 输出抬到接近提示音电平。"""
    try:
        return float(os.environ.get("MOCK_TTS_GAIN_DB", "0"))
    except ValueError:
        return 0.0


def apply_gain(pcm: np.ndarray, gain_db: float | None = None) -> np.ndarray:
    db = env_gain_db() if gain_db is None else gain_db
    if not db:
        return pcm
    return np.clip(pcm * (10.0 ** (db / 20.0)), -1.0, 1.0).astype(np.float32)


def env_bitrate() -> int:
    try:
        return max(32, min(320, int(os.environ.get("MOCK_TTS_BITRATE", str(MP3_BITRATE)))))
    except ValueError:
        return MP3_BITRATE


def env_lowpass_hz() -> float:
    raw = os.environ.get("MOCK_TTS_LOWPASS_HZ", "0")
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 0.0


def env_peak_dbfs() -> float | None:
    raw = os.environ.get("MOCK_TTS_PEAK_DBFS")
    if raw is None:
        return TTS_PEAK_DBFS
    if raw.strip().lower() in ("", "off", "none"):
        return None
    return float(raw)


def normalize_peak(pcm: np.ndarray, peak_dbfs: float | None = None) -> np.ndarray:
    """把 PCM 峰值缩放到 peak_dbfs（None 表示不动）；只缩不放。"""
    if peak_dbfs is None:
        return pcm
    if pcm.size == 0:
        return pcm
    peak = float(np.abs(pcm).max())
    if peak <= 1e-6:
        return pcm
    target = 10.0 ** (peak_dbfs / 20.0)
    if peak <= target:
        return pcm
    out = pcm * (target / peak)
    return np.clip(out, -1.0, 1.0).astype(np.float32)


class Mp3StreamEncoder:
    """增量 MP3 编码 + 帧切分：喂 PCM 块，吐出完整帧 (payload, duration_ms)。"""

    _BR_MPEG1_L3 = (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 0)
    _BR_MPEG2_L3 = (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160, 0)
    _SR = {
        3: (44100, 48000, 32000),  # MPEG1
        2: (22050, 24000, 16000),  # MPEG2
        0: (11025, 12000, 8000),   # MPEG2.5
    }

    def __init__(self, rate: int = TARGET_RATE, bitrate: int = MP3_BITRATE) -> None:
        import lameenc

        self.rate = rate
        self._enc = lameenc.Encoder()
        self._enc.set_bit_rate(bitrate)
        self._enc.set_in_sample_rate(rate)
        self._enc.set_channels(1)
        self._enc.set_quality(2)
        self._buf = b""

    def _frame_meta(self, data: bytes, pos: int):
        """解析 pos 处帧头；返回 (frame_len, duration_ms) 或 None（还没凑齐/不是帧头）。"""
        if pos + 4 > len(data):
            return None
        b1, b2, b3 = data[pos + 1], data[pos + 2], data[pos + 3]
        if data[pos] != 0xFF or (b1 & 0xE0) != 0xE0:
            return None
        version_bits = (b1 >> 3) & 0x3      # 3=MPEG1, 2=MPEG2, 0=MPEG2.5
        layer_bits = (b1 >> 1) & 0x3        # 1=Layer III
        if version_bits == 1 or layer_bits != 1:
            return None
        br_idx = (b2 >> 4) & 0xF
        sr_idx = (b2 >> 2) & 0x3
        if br_idx in (0, 15) or sr_idx == 3:
            return None
        table = self._BR_MPEG1_L3 if version_bits == 3 else self._BR_MPEG2_L3
        bitrate = table[br_idx] * 1000
        sample_rate = self._SR[version_bits][sr_idx]
        padding = (b2 >> 1) & 0x1
        if version_bits == 3:
            frame_len = 144 * bitrate // sample_rate + padding
            samples = 1152
        else:
            frame_len = 72 * bitrate // sample_rate + padding
            samples = 576
        return frame_len, samples * 1000.0 / sample_rate

    def feed(self, pcm: np.ndarray) -> list[tuple[bytes, float]]:
        int16 = (np.clip(pcm, -1.0, 1.0) * 32767.0).astype(np.int16)
        self._buf += self._enc.encode(int16.tobytes())
        return self._drain(final=False)

    def flush(self) -> list[tuple[bytes, float]]:
        self._buf += self._enc.flush()
        return self._drain(final=True)

    def _drain(self, *, final: bool) -> list[tuple[bytes, float]]:
        out: list[tuple[bytes, float]] = []
        while True:
            meta = self._frame_meta(self._buf, 0)
            if meta is None:
                if final:
                    self._buf = b""
                break
            frame_len, duration_ms = meta
            if len(self._buf) < frame_len:
                break
            out.append((self._buf[:frame_len], duration_ms))
            self._buf = self._buf[frame_len:]
        return out


# ---------------------------------------------------------------- 对外 API


def encode_pcm_to_mp3(pcm: np.ndarray, *, bitrate: int | None = None) -> bytes:
    """把 24k 单声道 float32 PCM 编成 MP3 字节（复用 mock 下发的同一个编码器）。"""
    encoder = Mp3StreamEncoder(rate=TARGET_RATE, bitrate=bitrate or env_bitrate())
    payloads = [payload for payload, _ms in encoder.feed(normalize_peak(pcm, env_peak_dbfs()))]
    payloads += [payload for payload, _ms in encoder.flush()]
    return b"".join(payloads)


def stream_pcm(text: str, *, model: str = DEFAULT_MODEL, voice: str | None = DEFAULT_VOICE,
               lang_code: str = "auto", instruct: str = "",
               stream_interval: float = 0.5, log=print):
    """流式：yield **float32 PCM 块**（已做低通/峰值归一/增益，未编码）。

    `stream_frames()` 与 `Mp3SpeechStream` 共用这一层；想自己控制 MP3 编码器
    （例如多段文本共用一条连续 MP3 流）时用它，避免重复实现电平处理。
    """
    if not speakable_text(text):
        raise RuntimeError(f"没有可合成的文字（{text!r}）")
    engine = get_engine(model=model, voice=voice, lang_code=lang_code, instruct=instruct,
                        stream_interval=stream_interval, log=log)
    peak_dbfs = env_peak_dbfs()
    fc = env_lowpass_hz()
    lowpass = [BiquadLowpass(fc), BiquadLowpass(fc)] if fc > 0 else []
    for pcm, _sr in engine.stream(text):
        for stage in lowpass:
            pcm = stage.process(pcm)
        pcm = normalize_peak(pcm, peak_dbfs)
        pcm = apply_gain(pcm)
        yield pcm


def stream_frames(text: str, *, model: str = DEFAULT_MODEL, voice: str | None = DEFAULT_VOICE,
                  lang_code: str = "auto", instruct: str = "",
                  stream_interval: float = 0.5, bitrate: int | None = None, log=print):
    """流式：yield (mp3 帧 payload, 帧时长 ms)。首帧只在第一块解码完成后就吐出来。"""
    bitrate = bitrate or env_bitrate()
    encoder = Mp3StreamEncoder(rate=TARGET_RATE, bitrate=bitrate)
    total = 0.0
    first_at: float | None = None
    started = time.monotonic()
    for pcm in stream_pcm(text, model=model, voice=voice, lang_code=lang_code,
                          instruct=instruct, stream_interval=stream_interval, log=log):
        for payload, duration_ms in encoder.feed(pcm):
            total += duration_ms
            if first_at is None:
                first_at = time.monotonic() - started
                log(f"[TTS] 首帧 {first_at:.2f}s（之后按实时节奏下发）")
            yield payload, duration_ms
    for payload, duration_ms in encoder.flush():
        total += duration_ms
        yield payload, duration_ms
    log(f"[TTS] 合成完成：{total / 1000:.2f}s 音频 / {time.monotonic() - started:.2f}s 墙钟")


class Mp3SpeechStream:
    """多段文本 → **一条连续 MP3 流**（同一个编码器，段间没有重编码接缝）。

    LLM→TTS 流水线用：每一小段合成完就 `encoder.feed()`，主循环可以立刻下发；
    段与段之间不会重新初始化 lame/比特池，句末停顿由 Qwen3-TTS 自己生成。
    """

    def __init__(self, *, model: str = DEFAULT_MODEL, voice: str | None = DEFAULT_VOICE,
                 lang_code: str = "auto", instruct: str = "",
                 stream_interval: float = 0.5, bitrate: int | None = None, log=print) -> None:
        self._encoder = Mp3StreamEncoder(rate=TARGET_RATE, bitrate=bitrate or env_bitrate())
        self._synth = {
            "model": model,
            "voice": voice,
            "lang_code": lang_code,
            "instruct": instruct,
            "stream_interval": stream_interval,
        }
        self._log = log
        self._created = time.monotonic()
        self._last_done_at: float | None = None
        self._first_at: float | None = None
        self.segments = 0
        self.total_ms = 0.0

    def add(self, text: str):
        """合成一段并 yield 完整 MP3 帧；段间共用同一个编码器。"""
        if not speakable_text(text):
            return
        self.segments += 1
        started = time.monotonic()
        # 这一段是不是让 TTS 线程等了一会儿？把"等 LLM 出字"与"本段合成"分开报，
        # 否则首帧日志会把 LLM 的首包/首句延迟算进 TTS，看起来像 TTS 很慢。
        if self._last_done_at is None:
            wait_s = started - self._created
            if wait_s > 0.2:
                self._log(f"[TTS] 首段到达 +{wait_s:.2f}s（等 LLM 出完第一段，TTS 还没开始合成）")
        else:
            wait_s = started - self._last_done_at
            if wait_s > 0.2:
                self._log(f"[TTS] 段 {self.segments} 开始：距上段结束 {wait_s:.2f}s（TTS 在等 LLM）")
        segment_ms = 0.0
        for pcm in stream_pcm(text, **self._synth, log=self._log):
            for payload, duration_ms in self._encoder.feed(pcm):
                segment_ms += duration_ms
                self.total_ms += duration_ms
                if self._first_at is None:
                    # 从**本段开始**算，不含等 LLM 的时间；这才是 TTS 首帧时延
                    self._first_at = time.monotonic() - started
                    self._log(f"[TTS] 首帧 {self._first_at:.2f}s（本段合成，从段开始计）")
                    if self._first_at > 2.0:
                        self._log(
                            f"[TTS] 首帧偏慢（正常预热后约 0.2~0.7s）：检查 mock 启动时是否打印"
                            "「预热完成」、是否多个 mock 进程抢同一块 GPU/CPU，或首段是否过长"
                        )
                yield payload, duration_ms
        self._last_done_at = time.monotonic()
        self._log(
            f"[TTS] 段 {self.segments}：{len(text)} 字 → {segment_ms / 1000:.2f}s 音频 / "
            f"{self._last_done_at - started:.2f}s 合成（累计 {self.total_ms / 1000:.2f}s）"
        )

    def flush(self):
        for payload, duration_ms in self._encoder.flush():
            self.total_ms += duration_ms
            yield payload, duration_ms
        self._log(
            f"[TTS] 流水线合成完成：{self.segments} 段 / {self.total_ms / 1000:.2f}s 音频"
            + (f" / 首帧 {self._first_at:.2f}s" if self._first_at is not None else "")
        )


def synthesize_mp3(text: str, *, model: str = DEFAULT_MODEL, voice: str | None = DEFAULT_VOICE,
                   lang_code: str = "auto", instruct: str = "",
                   stream_interval: float = 0.5, bitrate: int | None = None,
                   log=print) -> tuple[bytes, float]:
    """一次性合成整句 → (mp3 bytes, 音频秒数)。"""
    payloads: list[bytes] = []
    total_ms = 0.0
    for payload, duration_ms in stream_frames(text, model=model, voice=voice,
                                              lang_code=lang_code, instruct=instruct,
                                              stream_interval=stream_interval,
                                              bitrate=bitrate, log=log):
        payloads.append(payload)
        total_ms += duration_ms
    return b"".join(payloads), total_ms / 1000.0


# ---------------------------------------------------------------- CLI


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="离线 TTS：文字 → 24kHz 单声道 MP3（Qwen3-TTS / MLX）",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--text", default="", help="要合成的文字（缺省从 stdin 读）")
    parser.add_argument("--text-file", default="", help="从文件读文字（UTF-8）")
    parser.add_argument("--out", default="/tmp/mock_tts.mp3", help="输出 MP3 路径")
    parser.add_argument("--model", default=os.environ.get("QWEN_TTS_MODEL", DEFAULT_MODEL),
                        help="本地 snapshot 目录或 HF repo id")
    parser.add_argument("--voice", default=DEFAULT_VOICE,
                        help="serena/vivian/uncle_fu/ryan/aiden/ono_anna/sohee/eric/dylan")
    parser.add_argument("--lang", default="auto", help="语言：auto/chinese/english/…")
    parser.add_argument("--instruct", default="", help="风格指令（自然语言）")
    parser.add_argument("--bitrate", type=int, default=MP3_BITRATE, help="MP3 码率（kbps）")
    args = parser.parse_args(argv)

    text = args.text.strip()
    if not text and args.text_file:
        text = Path(args.text_file).read_text(encoding="utf-8").strip()
    if not text and not sys.stdin.isatty():
        text = sys.stdin.read().strip()
    if not text:
        parser.print_help()
        return 2

    started = time.monotonic()
    audio, duration = synthesize_mp3(
        text, model=args.model, voice=args.voice, lang_code=args.lang,
        instruct=args.instruct, bitrate=args.bitrate,
    )
    out = Path(args.out).expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(audio)
    print(
        f"[TTS] {out}（{duration:.2f}s，{TARGET_RATE}Hz 单声道，{len(audio)}B，"
        f"总耗时 {time.monotonic() - started:.2f}s）\n"
        f"      文本：{text}\n"
        f"      下发：uv run tools/ws_mock_server.py --tts-mp3 {out} --tts-pace-pct 80",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
