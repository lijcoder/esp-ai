#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "mlx-audio>=0.6",
#   "numpy>=1.24",
#   "sounddevice>=0.5",
#   "huggingface_hub>=0.24",
# ]
# ///
"""tts_console.py —— 控制台连续输入 → Qwen3-TTS → 排队播放

每输入一行文字就加入队列；合成线程和播放线程各一个：
  - 合成线程把文字合成成完整 PCM，放进 `audio_q`（最多缓存 3 条，形成背压）；
  - 播放线程按入队顺序写声卡，播完一条再播下一条，绝不叠音。
因此可以一边听当前这条、一边继续输入下一条，不需要等播放结束。

实现原则与 `tts_test.py` 一致：模型加载/合成/prompt 全部来自 `tools/mock_tts.py`，
本脚本只做输入、队列、播放和统计，不复制 TTS 实现。

用法：
  uv run tools/tts_console.py                          # 交互模式
  uv run tools/tts_console.py --voice ryan             # 换音色（默认 serena）
  uv run tools/tts_console.py --instruct "用温柔的语气说"
  printf '你好。\\n第二句。\\n' | uv run tools/tts_console.py   # 管道/脚本模式
  uv run tools/tts_console.py --no-play                # 只合成不出声（自检）
  uv run tools/tts_console.py --no-warmup              # 跳过预热（首条会慢一点）

命令（交互模式，整行匹配）：
  /help, /h     帮助
  /clear, /c    清空尚未播放的排队（正在合成/播放的那条不受影响）
  /quit, /q     立即退出（正在播放会停止）
快捷键：
  Ctrl+D        读完输入，等队列播完再退出
  Ctrl+C        立即退出

音色/音质环境变量与 mock 共用：MOCK_TTS_LOWPASS_HZ（常试 7000/5500）、
MOCK_TTS_GAIN_DB、MOCK_TTS_PEAK_DBFS。模型解析：--model → QWEN_TTS_MODEL → 本地 HF 缓存。
"""

from __future__ import annotations

import argparse
import queue
import sys
import threading
import time
from dataclasses import dataclass
from itertools import count
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mock_tts  # noqa: E402  —— 与 mock / tts_test 共用同一份 TTS 实现
import tts_test  # noqa: E402  —— 复用声卡打开/重采样/峰值统计，不复制播放代码


# ---------------------------------------------------------------- 队列元素


@dataclass(frozen=True)
class TextItem:
    seq: int
    text: str


@dataclass
class AudioItem:
    seq: int
    text: str
    pcm: np.ndarray
    rate: int
    audio_s: float


# ---------------------------------------------------------------- 控制台输出


class Console:
    """多线程共用的输出：一把锁保证整行不被拆开。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()

    def __call__(self, message: str = "") -> None:
        with self._lock:
            print(message, flush=True)


# ---------------------------------------------------------------- 播放器


class PcmPlayer:
    """常驻一个输出流，队列中的多条语音之间只插一段静音，不重复开关声卡。"""

    def __init__(self, gap_ms: int) -> None:
        self.gap_s = max(0, gap_ms) / 1000.0
        self._stream = None
        self._resampler = None
        self._rate: int | None = None
        self._has_played = False

    def _ensure_stream(self, rate: int) -> None:
        if self._stream is not None and self._rate == rate:
            return
        self.close()
        self._stream, self._resampler = tts_test.open_player(rate)
        self._rate = rate

    def _write(self, pcm: np.ndarray) -> None:
        if pcm.size == 0:
            return
        if self._resampler is not None:
            pcm = self._resampler.push(pcm)
        if pcm.size:
            self._stream.write(np.ascontiguousarray(pcm.reshape(-1, 1)))

    def play(self, pcm: np.ndarray, rate: int) -> None:
        self._ensure_stream(rate)
        if self._has_played and self.gap_s:
            self._write(np.zeros(int(rate * self.gap_s), dtype=np.float32))
        self._write(pcm)
        self._has_played = True

    def close(self) -> None:
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:  # noqa: BLE001 - 关闭阶段不再掩盖原始异常
                pass
        self._stream = None
        self._resampler = None
        self._rate = None
        self._has_played = False


def stop_audio() -> None:
    """Ctrl+C / /quit 时尽量立刻停掉声卡里已缓冲的音频。"""
    try:
        import sounddevice as sd

        sd.stop()
    except Exception:  # noqa: BLE001 - 没有声卡/没装 sounddevice 时忽略
        pass


# ---------------------------------------------------------------- 队列操作


def put_wait(q: queue.Queue, item, stop: threading.Event, timeout: float = 0.2) -> bool:
    """带 stop 检查的 put，避免退出时卡在满队列上。"""
    while not stop.is_set():
        try:
            q.put(item, timeout=timeout)
            return True
        except queue.Full:
            continue
    return False


def drain(q: queue.Queue) -> int:
    n = 0
    while True:
        try:
            q.get_nowait()
        except queue.Empty:
            return n
        n += 1


# ---------------------------------------------------------------- 合成线程


def synth_worker(
    text_q: queue.Queue,
    audio_q: queue.Queue,
    stop: threading.Event,
    opts: argparse.Namespace,
    ui: Console,
    stats: dict[str, int],
    init_error: list[BaseException],
) -> None:
    try:
        engine_kwargs = dict(
            model=opts.model or mock_tts.DEFAULT_MODEL,
            voice=opts.voice or None,
            lang_code=opts.lang,
            instruct=opts.instruct,
            stream_interval=opts.stream_interval,
            log=ui,
        )
        ui("[TTS] 首次启动会加载模型（约 10~15s，本地缓存命中会快一些）")
        ui("[TTS] 加载/预热期间可以先输入，文字会自动排队")
        mock_tts.preload(**engine_kwargs, warmup=not opts.no_warmup)
        engine = mock_tts.get_engine(**engine_kwargs)

        while not stop.is_set():
            item = text_q.get()
            if item is None:
                break
            if not mock_tts.speakable_text(item.text):
                ui(f"[TTS #{item.seq}] 跳过：没有可发音的字符（{item.text!r}）")
                stats["skipped"] += 1
                continue

            started = time.monotonic()
            chunks: list[np.ndarray] = []
            rate: int | None = None
            lowpass: list = []
            try:
                cutoff = mock_tts.env_lowpass_hz()
                peak_dbfs = mock_tts.env_peak_dbfs()
                for pcm, sr in engine.stream(item.text):
                    if rate is None:
                        rate = int(sr)
                    if cutoff > 0:
                        if not lowpass:
                            lowpass = [mock_tts.BiquadLowpass(cutoff, rate=rate),
                                       mock_tts.BiquadLowpass(cutoff, rate=rate)]
                        for stage in lowpass:
                            pcm = stage.process(pcm)
                    pcm = mock_tts.normalize_peak(pcm, peak_dbfs)
                    pcm = mock_tts.apply_gain(pcm)
                    if pcm.size:
                        chunks.append(pcm)
            except Exception as exc:  # noqa: BLE001 - 单条失败不拖垮整个队列
                ui(f"[TTS #{item.seq}] 合成失败：{exc}")
                stats["synth_failed"] += 1
                continue

            if not chunks or not rate:
                ui(f"[TTS #{item.seq}] 合成结果为空，跳过")
                stats["synth_failed"] += 1
                continue

            pcm = np.concatenate(chunks)
            synth_s = time.monotonic() - started
            audio_s = pcm.size / rate
            stats["synth_ok"] += 1
            ui(
                f"[TTS #{item.seq}] 合成完成：{audio_s:.2f}s 音频 / {synth_s:.2f}s"
                f"（{audio_s / max(synth_s, 1e-6):.2f}x，峰值 {tts_test.peak_dbfs(pcm):.1f} dBFS）"
            )
            if not put_wait(audio_q, AudioItem(item.seq, item.text, pcm, rate, audio_s),
                            stop):
                break
    except BaseException as exc:  # noqa: BLE001 - 模型加载失败等，交给主线程收尾
        init_error.append(exc)
        ui(f"[TTS] 初始化失败：{exc}")
    finally:
        # 让播放线程知道不会再有新音频；退出/队列满时不无限等待。
        while not stop.is_set():
            try:
                audio_q.put(None, timeout=0.2)
                break
            except queue.Full:
                continue


# ---------------------------------------------------------------- 播放线程


def player_worker(
    audio_q: queue.Queue,
    stop: threading.Event,
    opts: argparse.Namespace,
    ui: Console,
    stats: dict[str, int],
) -> None:
    player: PcmPlayer | None = None
    try:
        while not stop.is_set():
            item = audio_q.get()
            if item is None:
                break
            if opts.no_play:
                ui(f"[就绪 #{item.seq}] {item.audio_s:.2f}s（--no-play 未播放）")
                continue
            if player is None:
                player = PcmPlayer(opts.gap_ms)
            ui(f"[播放 #{item.seq}] {item.audio_s:.2f}s | {preview(item.text)}")
            player.play(item.pcm, item.rate)
            stats["played"] += 1
            ui(f"[完成 #{item.seq}]")
    except Exception as exc:  # noqa: BLE001 - 声卡错误时收掉整条流水线，避免队列永久卡住
        ui(f"[PLAY] 播放失败：{exc}")
        stats["play_failed"] += 1
        stop.set()
    finally:
        if player is not None:
            player.close()


# ---------------------------------------------------------------- 主循环


def preview(text: str, limit: int = 48) -> str:
    one_line = " ".join(text.split())
    return one_line if len(one_line) <= limit else one_line[: limit - 1] + "…"


def pending_count(text_q: queue.Queue, audio_q: queue.Queue) -> int:
    return text_q.qsize() + audio_q.qsize()


def help_text() -> str:
    return (
        "每行一条文字，回车入队；可以一边播放一边继续输入。\n"
        "  /help, /h     显示本帮助\n"
        "  /clear, /c    清空尚未播放的排队（正在合成/播放的那条不受影响）\n"
        "  /quit, /q     立即退出（正在播放会停止）\n"
        "  Ctrl+D        读完输入，等队列播完再退出\n"
        "  Ctrl+C        立即退出\n"
        "以 / 开头但不是上述命令的文本，按普通文字朗读。"
    )


def submit(
    line: str,
    seqs,
    text_q: queue.Queue,
    audio_q: queue.Queue,
    ui: Console,
    interactive: bool,
) -> str | None:
    """返回 'queued'（已入队）/ 'quit'（立即退出）/ None（空行或命令）。"""
    text = line.strip()
    if not text:
        return None
    command = text.lower()
    if command in ("/quit", "/q", "/exit"):
        return "quit"
    if command in ("/help", "/h"):
        ui(help_text())
        return None
    if command in ("/clear", "/c"):
        dropped = drain(text_q) + drain(audio_q)
        ui(f"[清空] 丢弃 {dropped} 条未播放项（正在合成/播放的不受影响）")
        return None

    seq = next(seqs)
    text_q.put(TextItem(seq, text))
    if interactive:
        ui(f"[入队 #{seq}] 待播 {pending_count(text_q, audio_q)} | {preview(text)}")
    else:
        ui(f"[入队 #{seq}] {preview(text)}")
    return "queued"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="控制台连续输入文字 → Qwen3-TTS → 排队播放",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--voice", default="ryan",
                   help="音色：serena/vivian/uncle_fu/ryan/aiden/ono_anna/sohee/eric/dylan")
    p.add_argument("--model", default="", help="本地 snapshot 或 HF repo id（空=默认 0.6B CustomVoice）")
    p.add_argument("--lang", default="auto", help="语言：auto/chinese/english/…")
    p.add_argument("--instruct", default="爽朗、男大语气说", help="风格指令（自然语言，如 '用温柔的语气说'）")
    p.add_argument("--stream-interval", type=float, default=0.32, help="流式块时长（秒）")
    p.add_argument("--gap-ms", type=int, default=120, help="两条语音之间的静音间隔（毫秒）")
    p.add_argument("--no-warmup", action="store_true",
                   help="跳过启动预热（模型仍会加载；首条合成会多等约 2~3s）")
    p.add_argument("--no-play", action="store_true",
                   help="只合成并排队，不打开声卡（自检/CI 用）")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.gap_ms < 0:
        args.gap_ms = 0

    ui = Console()
    text_q: queue.Queue = queue.Queue()
    audio_q: queue.Queue = queue.Queue(maxsize=3)  # ~3 条音频的合成背压
    stop = threading.Event()
    init_error: list[BaseException] = []
    stats = {"synth_ok": 0, "synth_failed": 0, "skipped": 0, "played": 0, "play_failed": 0}
    seqs = count(1)
    started = time.monotonic()

    synth = threading.Thread(
        target=synth_worker,
        args=(text_q, audio_q, stop, args, ui, stats, init_error),
        name="tts-synth",
        daemon=True,
    )
    player = threading.Thread(
        target=player_worker,
        args=(audio_q, stop, args, ui, stats),
        name="tts-player",
        daemon=True,
    )
    synth.start()
    player.start()

    interactive = sys.stdin.isatty()
    exit_code = 0
    immediate = False
    any_text = False
    try:
        if interactive:
            ui("=" * 62)
            ui("TTS 控制台 · 输入文字 → 自动合成 → 排队播放")
            ui(f"音色 {args.voice} | 间隔 {args.gap_ms}ms | 模型加载中，可先输入")
            ui("输入 /help 看命令；Ctrl+D 播完退出，Ctrl+C 立即退出")
            ui("=" * 62)
            while not stop.is_set():
                if init_error:
                    break
                prompt = f"[待播 {pending_count(text_q, audio_q)}] > "
                try:
                    line = input(prompt)
                except EOFError:
                    ui("")  # Ctrl+D 后换行，避免 shell 提示符粘在输入行
                    break
                action = submit(line, seqs, text_q, audio_q, ui, True)
                if action == "quit":
                    immediate = True
                    break
                any_text = any_text or action == "queued"
        else:
            ui("[TTS] 从 stdin 逐行读取；读完后等队列播完再退出")
            for line in sys.stdin:
                if init_error:
                    break
                action = submit(line, seqs, text_q, audio_q, ui, False)
                if action == "quit":
                    immediate = True
                    break
                any_text = any_text or action == "queued"
    except KeyboardInterrupt:
        ui("\n[退出] 收到 Ctrl+C")
        immediate = True
        exit_code = 130
    finally:
        try:
            if immediate or not any_text:
                stop.set()
                try:
                    text_q.put_nowait(None)
                except queue.Full:
                    pass
                if not args.no_play:
                    stop_audio()
                player.join(timeout=1.0)
                synth.join(timeout=1.0)
            else:
                # 优雅退出：让合成线程处理完 text_q，再让播放线程放完 audio_q。
                text_q.put(None)
                synth.join()
                player.join()
        except KeyboardInterrupt:
            # 等队列播完时又按了 Ctrl+C：立即收掉，不要打出 traceback。
            ui("\n[退出] 收到 Ctrl+C，停止播放")
            stop.set()
            if not args.no_play:
                stop_audio()
            player.join(timeout=1.0)
            synth.join(timeout=1.0)
            exit_code = 130

    if init_error:
        exit_code = 1
        ui(f"[错误] TTS 初始化失败：{init_error[0]}")

    elapsed = time.monotonic() - started
    if args.no_play:
        ui(
            f"[结束] 合成 {stats['synth_ok']} 条（失败 {stats['synth_failed']}，"
            f"跳过 {stats['skipped']}），用时 {elapsed:.1f}s"
        )
    else:
        ui(
            f"[结束] 播放 {stats['played']} 条（合成失败 {stats['synth_failed']}，"
            f"播放失败 {stats['play_failed']}），用时 {elapsed:.1f}s"
        )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
