#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "mlx-audio>=0.6",
#   "numpy>=1.24",
#   "sounddevice>=0.5",
#   "soundfile>=0.13",
#   "lameenc>=1.7",
#   "huggingface_hub>=0.24",
#   "av>=12",
# ]
# ///
"""tts_test.py —— 离线 TTS 模块测试：**直接调用 mock 用的那一套 `mock_tts`**，不复制实现

设计原则（2026-11-04 修正）：测试脚本只做"驱动 + 播放 + 断言"，**模型加载、prompt、
流式生成、MP3 编码全部来自 `tools/mock_tts.py`** —— 和 `ws_mock_server.py --tts-qwen`
下发时走的是同一份代码，所以这里通过就代表 mock 那条路通过（除了 WS 传输本身）。

两种口径（覆盖不同层）：

  --via-pcm（默认）
      mock_tts.get_engine(...).stream(text) → float32 PCM
      → **先合成完再播**（默认，避免生成慢于实时时"一卡一卡"）+ 可选存 WAV
      覆盖：模型解析/加载、prompt、流式分块
      加 --stream-play 才"边生成边播"（生成 RTF < 1 时会卡，仅用于听流式行为）

  --via-mp3
      mock_tts.stream_frames(text) → MP3 帧 (payload, duration_ms)
      → PyAV 解码回 PCM → 播放/存盘
      **与 mock 服务调用的是同一个函数**，额外覆盖：lameenc 增量编码、MPEG 帧边界、
      24kHz/64kbps 契约、帧时长统计

  --check
      不发声音，只跑 --via-mp3 的入口并断言（帧数/字节/时长误差 ≤1 帧/可解码/非静音），
      退出码 0/1 —— 适合 CI 或不方便出声时验证。

用法：
  uv run tools/tts_test.py --text "今天天气不错，适合出门散步。"          # 默认 PCM 流式播放
  uv run tools/tts_test.py --text "..." --via-mp3                       # 走 mock 的 MP3 帧路径并播放
  uv run tools/tts_test.py --text "..." --check                         # 只断言，不出声
  uv run tools/tts_test.py --text "..." --voice vivian --instruct "用温柔的语气说" \
      --out /tmp/reply.wav --mp3 /tmp/reply.mp3
  uv run tools/tts_test.py --play-wav /tmp/reply.wav                    # 只播已有 WAV

模型解析顺序与 mock 一致（`mock_tts.resolve_model`）：`--model` → `QWEN_TTS_MODEL` →
本地 HF 缓存 → 默认 `mlx-community/Qwen3-TTS-12Hz-0.6B-CustomVoice-bf16`。
"""

from __future__ import annotations

import argparse
import io
import os
import sys
import time
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mock_tts  # noqa: E402  —— 与 mock 服务共用同一份实现

DEVICE_RATE = 48000  # Mac 扬声器默认；24k 打不开时用它 + 线性重采样


# ---------------------------------------------------------------- 播放（测试壳独有）


class LinearResampler:
    """跨块连续的有状态线性重采样（24k → 声卡 48k；块边界不产生台阶）。"""

    def __init__(self, src_rate: int, dst_rate: int) -> None:
        self.step = src_rate / dst_rate
        self.pos = 0.0
        self.prev: float | None = None

    def push(self, chunk: np.ndarray) -> np.ndarray:
        if chunk.size == 0:
            return chunk
        if self.prev is None:
            self.prev = float(chunk[0])
        buf = np.concatenate(([self.prev], chunk.astype(np.float32)))
        n = buf.size
        out = []
        pos = self.pos
        while pos + 1.0 < n:
            i = int(pos)
            frac = pos - i
            out.append(buf[i] * (1.0 - frac) + buf[i + 1] * frac)
            pos += self.step
        self.prev = float(buf[-1])
        self.pos = pos - (n - 1)
        return np.asarray(out, dtype=np.float32)


def open_player(src_rate: int):
    """打开播放流：优先按源采样率；打不开就退到 48k + 重采样。返回 (stream, resampler)。"""
    import sounddevice as sd

    try:
        stream = sd.OutputStream(samplerate=src_rate, channels=1, dtype="float32")
        stream.start()
        return stream, None
    except Exception as exc:  # noqa: BLE001 - 设备不支持该采样率
        print(f"[PLAY] 声卡不支持 {src_rate} Hz（{exc}），改用 {DEVICE_RATE} Hz + 重采样", flush=True)
        stream = sd.OutputStream(samplerate=DEVICE_RATE, channels=1, dtype="float32")
        stream.start()
        return stream, LinearResampler(src_rate, DEVICE_RATE)


def play_chunks(chunks, src_rate: int) -> None:
    """边收边播（chunks 是 (float32 PCM, rate) 的迭代器）。"""
    stream, resampler = open_player(src_rate)
    try:
        for chunk, sr in chunks:
            if resampler is not None:
                chunk = resampler.push(chunk)
            stream.write(np.ascontiguousarray(chunk.reshape(-1, 1)))
    finally:
        stream.stop()
        stream.close()


def play_wav_file(path: str) -> None:
    import soundfile as sf

    data, rate = sf.read(path, dtype="float32", always_2d=False)
    if data.ndim > 1:
        data = data.mean(axis=1)
    play_chunks(iter([(data, rate)]), rate)


def write_wav(path: str, pcm: np.ndarray, rate: int) -> None:
    path = str(Path(path).expanduser())
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes((np.clip(pcm, -1.0, 1.0) * 32767.0).astype(np.int16).tobytes())


def peak_dbfs(pcm: np.ndarray) -> float:
    peak = float(np.abs(pcm).max()) if pcm.size else 0.0
    return 20.0 * np.log10(max(peak, 1e-9))


# ---------------------------------------------------------------- MP3 帧解码（校验用）


def decode_mp3_frames(frames: list[tuple[bytes, float]]) -> np.ndarray:
    """把 mock 产出的 MP3 帧拼起来，用 PyAV 解码成 float32 PCM（独立于设备解码器）。"""
    import av

    data = b"".join(payload for payload, _ in frames)
    with av.open(io.BytesIO(data), format="mp3") as container:
        stream = container.streams.audio[0]
        chunks = [
            frame.to_ndarray().reshape(-1)
            for frame in container.decode(stream)
        ]
    if not chunks:
        return np.zeros(0, dtype=np.float32)
    return np.concatenate(chunks).astype(np.float32)


# ---------------------------------------------------------------- 两条测试口径


def run_via_pcm(args) -> int:
    """默认口径：mock_tts 的引擎直接给 PCM → 播放/存 WAV。"""
    engine = mock_tts.get_engine(
        model=args.model or mock_tts.DEFAULT_MODEL,
        voice=args.voice or None,
        lang_code=args.lang,
        instruct=args.instruct,
        stream_interval=args.stream_interval,
        log=lambda message: print(message, flush=True),
    )
    t_start = time.monotonic()
    engine.load()
    t_loaded = time.monotonic() - t_start
    started = time.monotonic()
    first_chunk_s: float | None = None
    first_chunk_audio_s = 0.0
    chunks: list[np.ndarray] = []
    rate = mock_tts.TARGET_RATE
    # 默认"先合成完再播"。--stream-play 走"边生成边播"：生成放独立线程、经队列交给
    # 主线程写声卡 —— 绝不能在同一线程里 write() 完再生成（write 会阻塞到放完，生成被
    # 串行化，块间必然空档；2026-11-04 实测这样稳态只有 0.76x，听感一卡一卡）。
    stream = None
    resampler = None
    # (块就绪时刻, 该块音频秒数, 入队等待秒数=被播放限流的时间)
    gen_marks: list[tuple[float, float, float]] = []
    stream_play = args.stream_play and not args.no_play
    if stream_play:
        import queue
        import threading

        q: queue.Queue = queue.Queue(maxsize=4)  # ~2s 背压
        errors: list[BaseException] = []

        def producer() -> None:
            try:
                for audio, sr in engine.stream(args.text):
                    t_ready = time.monotonic()
                    q.put((audio, sr))          # 队列满时在这里等播放，属于"背压"不是生成慢
                    t_put = time.monotonic()
                    gen_marks.append(
                        (t_ready - started, audio.size / sr if sr else 0.0, t_put - t_ready)
                    )
            except BaseException as exc:  # noqa: BLE001 - 异常回传主线程
                errors.append(exc)
            finally:
                q.put(None)

        thread = threading.Thread(target=producer, name="tts-producer", daemon=True)
        thread.start()
        stream, resampler = open_player(rate)
        try:
            while True:
                item = q.get()
                if item is None:
                    break
                audio, sr = item
                if sr and sr != rate:
                    stream.stop()
                    stream.close()
                    rate = sr
                    stream, resampler = open_player(rate)
                chunks.append(audio)
                out = resampler.push(audio) if resampler is not None else audio
                stream.write(np.ascontiguousarray(out.reshape(-1, 1)))
        finally:
            if stream is not None:
                stream.stop()
                stream.close()
        if errors:
            raise errors[0]
    else:
        for audio, sr in engine.stream(args.text):
            if first_chunk_s is None:
                first_chunk_s = time.monotonic() - started
                rate = sr
                first_chunk_audio_s = audio.size / sr if sr else 0.0
            chunks.append(audio)

    pcm = np.concatenate(chunks) if chunks else np.zeros(0, dtype=np.float32)
    duration = pcm.size / rate if rate else 0.0
    if gen_marks:
        first_t, first_d, _ = gen_marks[0]
        steady_audio = max(duration - first_d, 0.0)
        # 纯生成时间 = 块就绪间隔扣除入队等待（等待播放=背压）；背压时间单独统计
        pure_gen = 0.0
        backpressure = 0.0
        prev_put = first_t + gen_marks[0][2]
        for t_ready, _dur, wait in gen_marks[1:]:
            pure_gen += max(t_ready - prev_put, 0.0)
            backpressure += wait
            prev_put = t_ready + wait
        print(
            f"[TTS] 合成完成：{duration:.2f}s 音频；首块 {first_t:.2f}s（含 prefill）；"
            f"其后 {steady_audio:.2f}s 音频纯生成 {pure_gen:.2f}s（稳态 {steady_audio / max(pure_gen, 1e-6):.2f}x），"
            f"被播放限流（队列背压）{backpressure:.2f}s；加载 {t_loaded:.2f}s 另计；"
            f"峰值 {peak_dbfs(pcm):.1f} dBFS，sr={rate}",
            flush=True,
        )
    else:
        gen_wall = time.monotonic() - started
        steady_audio = max(duration - first_chunk_audio_s, 0.0)
        steady_wall = max(gen_wall - (first_chunk_s or 0.0), 1e-6)
        print(
            f"[TTS] 合成完成：{duration:.2f}s 音频 / 生成 {gen_wall:.2f}s（加载 {t_loaded:.2f}s 另计）；"
            f"首块 {first_chunk_s or 0:.2f}s（含 prefill），其后 {steady_audio:.2f}s/{steady_wall:.2f}s"
            f"（稳态 {steady_audio / steady_wall:.2f}x）；峰值 {peak_dbfs(pcm):.1f} dBFS，sr={rate}",
            flush=True,
        )
    if args.out:
        write_wav(args.out, pcm, rate)
        print(f"[TTS] WAV → {args.out}", flush=True)
    if args.mp3:
        Path(args.mp3).expanduser().parent.mkdir(parents=True, exist_ok=True)
        Path(args.mp3).expanduser().write_bytes(mock_tts.encode_pcm_to_mp3(pcm))
        print(f"[TTS] MP3 → {args.mp3}（24k/64kbps，与 mock 下发同一编码器）", flush=True)
    if not args.no_play and not stream_play and pcm.size:
        print(f"[PLAY] 播放 {duration:.2f}s（整段；--stream-play 可听边生成边播）", flush=True)
        play_chunks(iter([(pcm, rate)]), rate)
    return 0


def run_via_mp3(args) -> int:
    """mock 口径：stream_frames → MP3 帧（与 ws_mock_server 同一个函数）→ 解码/播放/断言。"""
    started = time.monotonic()
    frames: list[tuple[bytes, float]] = []
    first_frame_s: float | None = None
    for payload, duration_ms in mock_tts.stream_frames(
        args.text,
        model=args.model or mock_tts.DEFAULT_MODEL,
        voice=args.voice or None,
        lang_code=args.lang,
        instruct=args.instruct,
        stream_interval=args.stream_interval,
        log=lambda message: print(message, flush=True),
    ):
        frames.append((payload, duration_ms))
        if first_frame_s is None:
            first_frame_s = time.monotonic() - started

    total_bytes = sum(len(p) for p, _ in frames)
    total_ms = sum(ms for _, ms in frames)
    pcm = decode_mp3_frames(frames)
    wall = time.monotonic() - started
    decoded_s = pcm.size / mock_tts.TARGET_RATE if pcm.size else 0.0
    print(
        f"[TTS] 帧下发口径：{len(frames)} 帧 / {total_bytes} B / 统计时长 {total_ms / 1000:.2f}s"
        f"（解码 {decoded_s:.2f}s），首帧 {first_frame_s or 0:.2f}s，"
        f"峰值 {peak_dbfs(pcm):.1f} dBFS，墙钟 {wall:.2f}s",
        flush=True,
    )

    failures: list[str] = []
    if not frames:
        failures.append("没有产出任何 MP3 帧")
    if any(len(p) != len(frames[0][0]) for p, _ in frames):
        failures.append("MP3 帧长度不一致（帧边界切分有问题）")
    if abs(decoded_s - total_ms / 1000.0) > 0.05:
        failures.append(f"解码时长 {decoded_s:.2f}s 与帧统计 {total_ms / 1000:.2f}s 差超过 50ms")
    if pcm.size == 0 or peak_dbfs(pcm) < -60:
        failures.append("解码结果为空或接近静音")

    if args.out:
        write_wav(args.out, pcm, mock_tts.TARGET_RATE)
        print(f"[TTS] WAV → {args.out}", flush=True)
    if args.mp3:
        Path(args.mp3).expanduser().parent.mkdir(parents=True, exist_ok=True)
        Path(args.mp3).expanduser().write_bytes(b"".join(p for p, _ in frames))
        print(f"[TTS] MP3 → {args.mp3}（与 mock 下发内容一致）", flush=True)
    if not args.no_play and not args.check:
        play_chunks(iter([(pcm, mock_tts.TARGET_RATE)]), mock_tts.TARGET_RATE)

    if args.check:
        if failures:
            print("[CHECK] FAIL：" + "；".join(failures), flush=True)
            return 1
        print("[CHECK] PASS：帧连续、可解码、时长一致、非静音", flush=True)
    return 0


# ---------------------------------------------------------------- CLI


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="离线 TTS 模块测试：直接调用 mock_tts（Qwen3-TTS）验证合成/编码/播放",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--text", default="", help="要合成的文字（也可 --text-file / stdin）")
    p.add_argument("--text-file", default="", help="从文件读文字（UTF-8）")
    p.add_argument("--play-wav", default="", help="只播放这个 WAV（不合成）")
    p.add_argument("--model", default="", help="本地 snapshot 或 HF repo id（空=默认 0.6B CustomVoice）")
    p.add_argument("--voice", default="serena", help="预置音色（serena/vivian/uncle_fu/ryan/…）")
    p.add_argument("--lang", default="auto", help="语言：auto/chinese/english/…")
    p.add_argument("--instruct", default="", help="风格指令（自然语言，如 '用温柔的语气说'）")
    p.add_argument("--stream-interval", type=float, default=0.5, help="流式块时长（秒）")
    p.add_argument("--via-mp3", action="store_true",
                   help="走 mock 的 MP3 帧路径（mock_tts.stream_frames）而不是 PCM 路径")
    p.add_argument("--check", action="store_true",
                   help="只跑 --via-mp3 的入口并断言（不出声），退出码 0/1")
    p.add_argument("--stream-play", action="store_true",
                   help="边生成边播（生成慢于实时时块间会卡；默认整段合成后播）")
    p.add_argument("--no-play", action="store_true", help="只合成/存盘，不播放")
    p.add_argument("--out", default="", help="保存 24k 单声道 WAV")
    p.add_argument("--mp3", default="", help="保存 MP3（--via-mp3 时就是 mock 下发的那串帧）")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.play_wav:
        print(f"[PLAY] {args.play_wav}", flush=True)
        play_wav_file(args.play_wav)
        return 0

    text = args.text.strip()
    if not text and args.text_file:
        text = Path(args.text_file).read_text(encoding="utf-8").strip()
    if not text and not sys.stdin.isatty():
        text = sys.stdin.read().strip()
    if not text:
        build_parser().print_help()
        return 2
    args.text = text

    if args.via_mp3 or args.check:
        return run_via_mp3(args)
    return run_via_pcm(args)


if __name__ == "__main__":
    raise SystemExit(main())
