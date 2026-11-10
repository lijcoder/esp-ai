#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "av>=12",
#   "numpy>=1.24",
#   "websockets>=14",
#   "sounddevice>=0.5",
#   "mlx-audio>=0.6",
#   "lameenc>=1.7",
#   "huggingface_hub>=0.24",
# ]
# ///
"""mock_offline_test.py —— **不接开发板**，用假设备把 mock 的一整轮跑通并验证

模拟设备端行为：hello → audio_start → 上行 Opus 帧（从 WAV 编码）→ audio_end，
然后收集下发的 asr_final / llm_delta / tts_start / 0x02 音频帧 / tts_end，
把收到的音频存成 MP3（可选本机播放）并打印报告。

用来验证 mock 的 **ASR → LLM → TTS** 整条链路（以及 `--asr sensevoice`/`--tts-qwen`/`--llm`
三项开关是否真的接上了），全程不需要 ESP32。

前提：mock 已经在跑（另开一个终端），例如：
  export LLM_BASE_URL=... LLM_MODEL=... LLM_API_KEY=...
  uv run --with av --with numpy --with sherpa-onnx --with mlx-audio --with lameenc \\
    tools/ws_mock_server.py --asr sensevoice --tts-qwen --llm

两种"用户说话"的来源：
  --text "今天天气怎么样？"   **本地 Qwen3-TTS 合成** → 16k → Opus 上行（想说什么就说什么，不用录音/开发板）
  --wav FILE                 用现成 WAV（默认 tools/今天天气如何.wav）

用法：
  uv run tools/mock_offline_test.py --text "今天天气怎么样？" --expect-llm --play   # 实时播放（边收边播）
  uv run tools/mock_offline_test.py --text "帮我讲个笑话" --voice vivian
  uv run tools/mock_offline_test.py --wav tools/今天吃了什么.wav
  uv run tools/mock_offline_test.py --out /tmp/reply.mp3 --expect-llm

判定（退出码 0/1）：
  - 收到 asr_final 且非空；
  - 收到 tts_start、至少 1 个 0x02 帧、tts_end；
  - `--expect-llm`：回答文本（llm_delta 拼接）**不等于** ASR 文本（证明是 LLM 回答，不是复读）；
  - `--expect-pipeline`：首声早于 LLM 文本结束（要求 mock 启动时加 `--llm-tts-stream`）；
  - `--expect-no-pipeline`：反过来断言（默认整段回答后合成）。

超时口径：`--timeout` 是**空闲超时**（默认 60s，每收到一条下行消息就重置；0=不因空闲失败），
`--max-total` 是总等待上限（默认 900s，0=不限）。读长文时音频按实时节奏下发，几十秒到几分钟
都算正常——旧版"固定 60s 总时长"会在长文上误判成"没收到 tts_end"并掐断播放（2026-11-05 修）；
长音频期间脚本每 10s 打印一行 `已收 N 帧 / 已等 Xs`。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import wave
from pathlib import Path

import numpy as np

DEFAULT_URL = "ws://127.0.0.1:8765/v1/voice?device_id=offline-test&token=dev-token&ver=1"
DEFAULT_WAV = "tools/今天天气如何.wav"
MAGIC = b"VO"
FRAME_VER = 1
TYPE_AUDIO_UP = 0x01
TYPE_AUDIO_DOWN = 0x02
SAMPLE_RATE = 16000


def synthesize_16k(text: str, voice: str | None) -> np.ndarray:
    """本地 Qwen3-TTS 合成"用户说的话" → 16 kHz 单声道 float32（给 Opus 编码用）。"""
    import mock_tts  # 同目录；--wav 模式不会 import

    engine = mock_tts.get_engine(voice=voice or None, log=print)
    chunks = [pcm for pcm, _sr in engine.stream(text)]
    if not chunks:
        raise RuntimeError("TTS 合成结果为空")
    pcm24 = np.concatenate(chunks).astype(np.float32)
    n_out = int(round(pcm24.size * SAMPLE_RATE / mock_tts.TARGET_RATE))
    return np.interp(
        np.arange(n_out) * (mock_tts.TARGET_RATE / SAMPLE_RATE), np.arange(pcm24.size), pcm24
    ).astype(np.float32)


def load_wav_16k(path: str) -> np.ndarray:
    """读 WAV → 16 kHz 单声道 float32（[-1,1]）；24k/48k 用线性插值重采样。"""
    with wave.open(path, "rb") as w:
        rate = w.getframerate()
        channels = w.getnchannels()
        frames = w.readframes(w.getnframes())
    data = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
    if channels > 1:
        data = data.reshape(-1, channels).mean(axis=1)
    if rate != SAMPLE_RATE:
        n_out = int(round(data.size * SAMPLE_RATE / rate))
        data = np.interp(
            np.arange(n_out) * (rate / SAMPLE_RATE), np.arange(data.size), data
        ).astype(np.float32)
    return data


def encode_opus_packets(pcm: np.ndarray, bitrate: int = 16000) -> list[bytes]:
    """16k 单声道 float32 → 20ms 裸 Opus 包（与设备上行 0x01 payload 一致）。"""
    import av

    int16 = (np.clip(pcm, -1.0, 1.0) * 32767.0).astype(np.int16)
    enc = av.CodecContext.create("libopus", "w")
    enc.sample_rate = SAMPLE_RATE
    enc.layout = "mono"
    enc.format = "s16"
    enc.bit_rate = bitrate
    step = SAMPLE_RATE // 50
    packets: list[bytes] = []
    for start in range(0, int16.size, step):
        chunk = int16[start : start + step]
        if chunk.size < step:
            chunk = np.pad(chunk, (0, step - chunk.size))
        frame = av.AudioFrame.from_ndarray(chunk.reshape(1, -1), format="s16", layout="mono")
        frame.sample_rate = SAMPLE_RATE
        packets.extend(bytes(p) for p in enc.encode(frame))
    packets.extend(bytes(p) for p in enc.encode(None))
    return packets


def build_bin_frame(frame_type: int, seq: int, payload: bytes) -> bytes:
    return (
        MAGIC
        + bytes((FRAME_VER, frame_type))
        + (seq & 0xFFFF).to_bytes(2, "big")
        + len(payload).to_bytes(2, "big")
        + payload
    )


def decode_mp3(payloads: list[bytes]) -> np.ndarray:
    """把下行 0x02 帧拼起来解码成 float32 PCM（24k 单声道）。

    只用于统计/试听：mock 的 `--tts-mp3` 关闭时下行的是**合成帧**（不是真实 MP3），
    解码失败不算链路错误（否则会盖掉真正的失败原因，例如空闲超时），返回空 PCM 即可。
    """
    import av
    import io

    data = b"".join(payloads)
    if not data:
        return np.zeros(0, dtype=np.float32)
    try:
        with av.open(io.BytesIO(data), format="mp3") as container:
            stream = container.streams.audio[0]
            chunks = [f.to_ndarray().reshape(-1) for f in container.decode(stream)]
    except Exception:  # noqa: BLE001 - 合成帧/半帧：解不出来不影响断言
        return np.zeros(0, dtype=np.float32)
    return np.concatenate(chunks).astype(np.float32) if chunks else np.zeros(0, dtype=np.float32)


class RealtimePlayer:
    """边收边播：WS 收到 0x02 帧 → MP3 增量解码 → 队列 → 声卡线程（不在收包协程里阻塞）。"""

    def __init__(self, rate: int = 24000, preroll_s: float = 0.3, gain_db: float = 0.0) -> None:
        import queue
        import threading

        import sounddevice as sd

        from av import CodecContext

        self.rate = rate
        self.preroll_s = max(0.0, preroll_s)
        self.gain = 10.0 ** (gain_db / 20.0)
        self._q: queue.Queue = queue.Queue(maxsize=64)
        self._dec = CodecContext.create("mp3", "r")
        # 统一转成 float [-1,1] 单声道（codec 默认可能给 s16p，直接写 float 流会爆音）
        self._res = __import__("av").AudioResampler(format="flt", layout="mono", rate=rate)
        self._stream = sd.OutputStream(samplerate=rate, channels=1, dtype="float32")
        self._started = False
        self._thread = threading.Thread(target=self._consumer, name="play", daemon=True)
        self._thread.start()

    def _write(self, chunk: np.ndarray) -> None:
        self._stream.write(np.ascontiguousarray(chunk.reshape(-1, 1)))

    def _consumer(self) -> None:
        """先攒 preroll 再开播：0x02 帧是按 24ms 到达的，声卡空跑会欠载 → 听感"断断续续/偏小"。"""
        need = int(self.rate * self.preroll_s)
        pending: list[np.ndarray] = []
        pending_n = 0
        while True:
            item = self._q.get()
            if item is None:
                if pending:
                    if not self._started:
                        self._stream.start()
                        self._started = True
                    self._write(np.concatenate(pending))
                if self._started:
                    # 收尾：补一小段静音并等一下设备缓冲排空，否则 stop() 会把尾巴切掉（听感"最后一下嚓"）
                    self._write(np.zeros(int(self.rate * 0.2), dtype=np.float32))
                    time.sleep(0.25)
                break
            pending.append(item)
            pending_n += item.size
            if not self._started and pending_n >= need:
                self._stream.start()
                self._started = True
            if self._started:
                self._write(np.concatenate(pending))
                pending = []
                pending_n = 0
        if self._started:
            self._stream.stop()

    def feed(self, payload: bytes) -> None:
        """喂一个完整的 MP3 帧；解出来的 PCM 直接排队播放。

        注意：`av.CodecContext("mp3")` 默认输出 **s16p**（int16 存成 float32，量级 32768），
        而容器解码（`--play-after` 用的 `decode_mp3`）会给 float [-1,1]。两者混用会让
        实时播放"特别大/爆音"——这里统一归一化到 [-1,1]（2026-11-04 修）。
        """
        import av

        try:
            for frame in self._dec.decode(av.Packet(payload)):
                for out in self._res.resample(frame):
                    pcm = out.to_ndarray().reshape(-1).astype(np.float32)
                    if pcm.size:
                        self._q.put(np.clip(pcm * self.gain, -1.0, 1.0))
        except Exception:  # noqa: BLE001 - 单帧解码失败不影响整轮
            return

    def close(self) -> None:
        self._q.put(None)
        self._thread.join(timeout=5)
        try:
            self._stream.close()
        except Exception:  # noqa: BLE001
            pass


def play(pcm: np.ndarray, rate: int) -> None:
    import sounddevice as sd

    sd.play(pcm, rate)
    sd.wait()


async def run(args) -> int:
    import websockets

    if args.text:
        print(f"[TEST] 本地合成「用户说的话」：{args.text!r}（voice={args.voice}）...", flush=True)
        pcm = synthesize_16k(args.text, args.voice or None)
        source = f"--text 合成（{pcm.size / SAMPLE_RATE:.2f}s）"
    else:
        pcm = load_wav_16k(args.wav)
        source = args.wav
    packets = encode_opus_packets(pcm)
    print(f"[TEST] 输入 {source} → 16k {pcm.size / SAMPLE_RATE:.2f}s → Opus {len(packets)} 包", flush=True)
    print(f"[TEST] 连接 {args.url}", flush=True)

    asr_partial = ""
    asr_final = ""
    answer_parts: list[str] = []
    down: list[bytes] = []
    t_tts_start = None
    t_first_frame = None
    t_end = None
    error = None
    player = None
    # 各阶段时刻（用来证明流水线顺序：ASR 完成 → LLM 完成 → 才开始 TTS 下发/播放）
    t_send_done = None
    t_asr = None
    t_llm_first = None
    t_llm_last = None

    async with websockets.connect(args.url) as ws:
        t0 = time.monotonic()
        await ws.send(json.dumps({"t": "hello", "device_id": "offline-test", "ver": 1,
                                  "token": "dev-token"}))
        while True:
            msg = await asyncio.wait_for(ws.recv(), timeout=10)
            if isinstance(msg, str) and json.loads(msg).get("t") == "hello_ack":
                break

        if args.play:
            player = RealtimePlayer(24000, preroll_s=args.preroll, gain_db=args.gain_db)
            print("[TEST] 实时播放已开启（边收 0x02 帧边播）", flush=True)
        await ws.send(json.dumps({"t": "audio_start", "sid": 1}))
        for seq, payload in enumerate(packets):
            await ws.send(build_bin_frame(TYPE_AUDIO_UP, seq, payload))
        await ws.send(json.dumps({"t": "audio_end", "sid": 1}))
        t_send_done = time.monotonic()

        # 超时口径 = **空闲超时**（每收到一条下行消息就重置），不是总时长：
        # 长文本（几百字 → 几十秒~几分钟音频）按实时节奏下发，固定 60s 总超时会把它拦腰砍断。
        last_rx = time.monotonic()
        hard_deadline = last_rx + args.max_total if args.max_total > 0 else None
        next_progress = last_rx + 10.0
        timeout_reason = ""
        while True:
            now = time.monotonic()
            idle_left = args.timeout - (now - last_rx) if args.timeout > 0 else float("inf")
            if idle_left <= 0:
                timeout_reason = f"连续 {args.timeout:.0f}s 没有下行数据"
                break
            if hard_deadline is not None and now >= hard_deadline:
                timeout_reason = f"总等待超过 --max-total {args.max_total:.0f}s"
                break
            wait = min(idle_left, 1.0)
            if hard_deadline is not None:
                wait = max(0.1, min(wait, hard_deadline - now))
            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=wait)
            except asyncio.TimeoutError:
                continue
            now = time.monotonic()
            gap = now - last_rx
            last_rx = now
            if now >= next_progress:
                next_progress = now + 10.0
                print(
                    f"[TEST] ... 已收 {len(down)} 帧 / 已等 {now - t0:.0f}s"
                    f"（上一条数据 {gap:.2f}s 前，仍在实时下发）",
                    flush=True,
                )
            if isinstance(msg, (bytes, bytearray)):
                raw = bytes(msg)
                if raw[:2] != MAGIC:
                    continue
                ftype = raw[3]
                if ftype == TYPE_AUDIO_DOWN:
                    if t_first_frame is None:
                        t_first_frame = now
                    payload = raw[8:]
                    down.append(payload)
                    if player is not None:
                        player.feed(payload)
                continue
            evt = json.loads(msg)
            kind = evt.get("t")
            if kind == "asr_partial":
                asr_partial = evt.get("text", "")
                print(f"[TEST] <- asr_partial {asr_partial!r}", flush=True)
            elif kind == "asr_final":
                asr_final = evt.get("text", "")
                t_asr = now
                print(f"[TEST] <- asr_final   {asr_final!r}", flush=True)
            elif kind == "llm_delta":
                if t_llm_first is None:
                    t_llm_first = now
                t_llm_last = now
                answer_parts.append(evt.get("text", ""))
            elif kind == "tts_start":
                t_tts_start = now
                print(f"[TEST] <- tts_start  codec={evt.get('codec')} sr={evt.get('sr')}", flush=True)
            elif kind == "tts_end":
                t_end = now
                break
            elif kind == "error":
                error = evt
                break

    if player is not None:
        player.close()

    answer = "".join(answer_parts).strip()
    audio = decode_mp3(down)
    duration = audio.size / 24000 if audio.size else 0.0
    total = (t_end or time.monotonic()) - t0

    print(f"[TEST] 回答（llm_delta 拼接）：{answer!r}", flush=True)
    print(
        f"[TEST] 下行：{len(down)} 帧 / {sum(len(p) for p in down)} B / 解码 {duration:.2f}s 音频；"
        f"tts_start→首帧 {(t_first_frame - t_tts_start) * 1000 if t_tts_start and t_first_frame else -1:.0f}ms",
        flush=True,
    )
    print(f"[TEST] 总耗时 {total:.2f}s" + (f"；error={error}" if error else ""), flush=True)

    def at(t: float | None) -> str:
        return "—" if t is None else f"+{t - t0:.2f}s"

    print(
        "[TEST] 阶段时间线：上行结束 {} → asr_final {} → LLM 首包 {} → LLM 完成 {} → "
        "tts_start {} → 首个音频帧/开始播放 {}".format(
            at(t_send_done), at(t_asr), at(t_llm_first), at(t_llm_last), at(t_tts_start), at(t_first_frame)
        ),
        flush=True,
    )
    if t_llm_last and t_first_frame:
        overlap_ms = (t_llm_last - t_first_frame) * 1000
        if overlap_ms > 50:
            print(
                f"[TEST] 流水线：首声比 LLM 完成早 {overlap_ms:.0f}ms"
                "（LLM 还在出字，设备已经开始播放）",
                flush=True,
            )
        else:
            print(
                f"[TEST] 首声比 LLM 完成晚 {-overlap_ms:.0f}ms"
                "（回答较短/LLM 很快；用 --expect-pipeline 可强制要求流水线）",
                flush=True,
            )

    failures: list[str] = []
    if not asr_final:
        failures.append("没收到 asr_final（ASR 没识别出文本）")
    if not down:
        failures.append("没收到任何 0x02 音频帧")
    if not answer:
        failures.append("没收到 llm_delta（回答为空）")
    if t_tts_start is None:
        failures.append("没收到 tts_start")
    if t_end is None and not error:
        why = f"：{timeout_reason}" if timeout_reason else ""
        failures.append(
            f"没收到 tts_end（{len(down)} 帧 / {duration:.1f}s 音频{why}）"
            "；回答很长时属于等待不够，放大 --timeout（空闲超时）即可"
        )
    if error is not None:
        failures.append(f"mock 返回 error：{error}")
    if args.expect_llm and answer and asr_final and answer == asr_final:
        failures.append("回答与 ASR 文本完全相同 → 这是复读，不是 LLM 回答（检查 mock 是否带 --llm）")
    if args.expect_pipeline:
        if not (t_first_frame and t_llm_last and t_first_frame < t_llm_last):
            failures.append(
                "--expect-pipeline：首声没有早于 LLM 完成"
                "（mock 未开流水线，或回答太短/LLM 太快；看上面时间线）"
            )
    if args.expect_no_pipeline:
        if t_first_frame and t_llm_last and t_first_frame < t_llm_last:
            failures.append("--expect-no-pipeline：首声早于 LLM 完成（mock 意外开启了流水线）")

    if args.out and down:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_bytes(b"".join(down))
        print(f"[TEST] 音频已保存：{args.out}（{duration:.2f}s，可 afplay 试听）", flush=True)
    if args.play_after and audio.size:
        print(f"[TEST] 播放已保存音频 {duration:.2f}s ...", flush=True)
        play(np.clip(audio * (10.0 ** (args.gain_db / 20.0)), -1.0, 1.0), 24000)

    if failures:
        print("[TEST] FAIL：" + "；".join(failures), flush=True)
        return 1
    print("[TEST] PASS：ASR → LLM → TTS 链路完整", flush=True)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="不接开发板，用假设备验证 mock 的 ASR→LLM→TTS 整轮链路",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--url", default=DEFAULT_URL, help="mock 的 WS 地址")
    src = p.add_mutually_exclusive_group()
    src.add_argument("--text", default="", help="想说的文字：本地 Qwen3-TTS 合成成语音再上行（推荐）")
    src.add_argument("--wav", default="", help="用现成 WAV（任意采样率，自动转 16k）；不给 --text 时默认 "
                                               + DEFAULT_WAV)
    p.add_argument("--voice", default="serena", help="--text 合成用的音色（serena/vivian/…）")
    p.add_argument("--out", default="/tmp/mock_offline_reply.mp3", help="把收到的 TTS 音频存成 MP3")
    p.add_argument("--play", action="store_true",
                   help="**实时播放**：收到 0x02 音频帧就解码、边收边播（不打断收包）")
    p.add_argument("--play-after", action="store_true",
                   help="收完再整段播放（排查用；与 --play 可同时给）")
    p.add_argument("--preroll", type=float, default=0.3,
                   help="实时播放先攒多少秒再开播（防声卡欠载；0=立即）")
    p.add_argument("--gain-db", type=float, default=6.0,
                   help="播放增益（dB，默认 +6：TTS 峰值约 -10~-13 dBFS，比提示音轻）")
    p.add_argument("--timeout", type=float, default=60.0,
                   help="等 tts_end 的**空闲**超时（秒）：每收到一条下行消息就重置，长音频不会被打断；"
                        "0=不因空闲失败")
    p.add_argument("--max-total", type=float, default=900.0,
                   help="总等待上限（秒）：防「一直有数据但永不 tts_end」；0=不限")
    p.add_argument("--expect-llm", action="store_true", help="断言回答 != ASR 文本（证明走的是 LLM）")
    p.add_argument(
        "--expect-pipeline",
        action="store_true",
        help="断言首声早于 LLM 文本结束（证明 mock 的 LLM→TTS 流水线生效）",
    )
    p.add_argument(
        "--expect-no-pipeline",
        action="store_true",
        help="断言首声不早于 LLM 完成（默认整段合成路径；mock 未加 --llm-tts-stream 时用）",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.text and not args.wav:
        args.wav = DEFAULT_WAV
    try:
        return asyncio.run(run(args))
    except Exception as exc:  # noqa: BLE001 - 连不上 mock 时给一句人话
        print(f"[TEST] 失败：{type(exc).__name__}: {exc}", flush=True)
        print("[TEST] 先确认 mock 已在跑（另一个终端），且 --url 与 mock 的端口一致", flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
