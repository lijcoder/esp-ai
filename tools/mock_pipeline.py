#!/usr/bin/env python3
"""mock_pipeline —— LLM 流式输出 → 自然分句 → TTS 流式合成（可单测，不依赖 MLX/网络）。

**为什么需要它**：旧路径 `_llm_answer()` 要等 LLM **整段**回答写完，才把整段文本交给
`mock_tts.stream_frames()`；设备在 THINKING 里干等，回答越长首声越晚。这里把 LLM 增量按
标点切成"语义段"，第一段一到就交给 TTS，音频边合成边下发——LLM 还在出字，设备已经开始说话。

**自然度优先的分句规则**（不是简单按长度切）：
  - 句末标点（。！？；）立即断，**标点保留**给 TTS 做语调；
  - 逗号只在"首段要尽早出声"或长句时才断，避免把一句话切成碎块；
  - LLM 停顿（idle）或超长无标点时兜底断，并补逗号表示"话没说完"，避免每段都用句号收尾；
  - 段间共用**同一个 MP3 编码器**（见 `mock_tts.Mp3SpeechStream`），不是拼接多段 MP3，
    因此没有比特池重置/接缝爆音。

**线程模型**（mock 跑在 asyncio 里，不能让阻塞推理占事件循环）：
  llm 线程    : 阻塞读 SSE → 增量写 delta_q，并 emit("delta")
  coordinator : delta_q → SpeechSegmenter → seg_q（空闲超时也会切段）
  tts 线程    : seg_q → 同一个 MP3 编码器 → emit("frame")

所有事件经 `asyncio.Queue` 回主循环；Session 负责发 WS 帧、预缓冲和实时节奏。

单测：`uv run tools/mock_pipeline_test.py`（纯标准库，不需要设备/模型）。
"""

from __future__ import annotations

import asyncio
import queue
import re
import threading
import time
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

# 句末标点：看到就断（`;`/`\n` 也按句末处理，列表项之间能自然停顿）
STRONG_PUNCT = "。！？!?；;…\n"
# 句中标点：只在首段/长句时断
WEAK_PUNCT = "，,、：:"
# 断句后紧跟的收尾符号：一起并入这一段，避免下一段以引号/括号开头
_CLOSERS = "”’」』】）)》〉"
# 用在"这段没有句末标点"时补的标点（表示还要继续）
_CONTINUE_PUNCT = "，"
_FINAL_PUNCT = "。"

_URL_RE = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)
# markdown 装饰符：星号/下划线/反引号/井号/引用/表格/花括号
_MD_RE = re.compile(r"[*_`#>\[\]{}|]+")
_SPACE_RE = re.compile(r"\s+")


def has_speakable(text: str) -> bool:
    """是否包含真正能念的字符（字母/数字/汉字）。"""
    return any(ch.isalnum() or "\u4e00" <= ch <= "\u9fff" for ch in text or "")


def sanitize_for_speech(text: str) -> str:
    """把 LLM/markdown 文本清成"适合朗读"的文本。

    去掉 URL、markdown 装饰符、emoji/控制字符，压缩空白；保留中英文标点与数字，
    让 Qwen3-TTS 自己按标点处理停顿和语调。**不**在这里做分句，分句见 SpeechSegmenter。
    """
    if not text:
        return ""
    text = _URL_RE.sub("", text)
    text = _MD_RE.sub("", text)
    chars: list[str] = []
    for ch in text:
        if ch in "\r\n\t" or ch.isspace():
            chars.append(" ")
            continue
        category = unicodedata.category(ch)
        # So=emoji/符号，Cs=代理，Co=私用区，Cn=未分配，Cf=零宽/格式控制
        if category in ("So", "Cs", "Co", "Cn", "Cf"):
            continue
        chars.append(ch)
    return _SPACE_RE.sub(" ", "".join(chars)).strip()


def _is_cjk(ch: str) -> bool:
    return "\u4e00" <= ch <= "\u9fff"


def _find_strong(text: str, start: int = 0) -> int:
    """从 start 起找句末标点位置（ASCII 句点只在词边界算，避免切断 3.14/网址）。"""
    for index in range(start, len(text)):
        ch = text[index]
        if ch in STRONG_PUNCT:
            return index
        if ch == ".":
            nxt = text[index + 1] if index + 1 < len(text) else ""
            if not nxt or nxt.isspace() or _is_cjk(nxt):
                return index
    return -1


def _find_any(text: str, chars: str, start: int = 0) -> int:
    for index in range(start, len(text)):
        if text[index] in chars:
            return index
    return -1


def _extend_cut(text: str, cut: int) -> int:
    """断句点后紧跟的引号/括号/省略号并入本段，避免下一段以符号开头。"""
    while cut < len(text) and (text[cut] in _CLOSERS or text[cut] in "…"):
        cut += 1
    return cut


@dataclass
class SegmenterConfig:
    """分句阈值（默认值按"中文口语、首段尽早出声"调过）。"""

    first_min_chars: int = 8   # 首段最少字数（含标点），低于此不断
    min_chars: int = 6         # 后续段最少字数
    max_chars: int = 48        # 单段硬上限，超过必须断
    first_max_chars: int = 48  # 首段优先上限（与 max_chars 同档；调小可更早，但可能断在句中）
    idle_ms: int = 450         # LLM 停顿多久后兜底断段
    first_idle_ms: int = 600   # 首段的停顿阈值可以稍长，等一个自然标点
    lookback: int = 12         # 超长硬断时向前找标点的窗口


class SpeechSegmenter:
    """流式文本 → 语义段。`feed()` 立即返回已就绪的段，`poll_idle()` 处理 LLM 停顿。

    设计成纯同步类（不碰网络/线程），方便单测；coordinator 线程负责按 50ms 轮询 idle。
    """

    def __init__(self, config: SegmenterConfig | None = None, clock: Callable[[], float] = time.monotonic) -> None:
        self.cfg = config or SegmenterConfig()
        self._clock = clock
        self._buf = ""
        self._first = True
        self._last_update = clock()

    @property
    def pending(self) -> str:
        return self._buf

    def feed(self, delta: str) -> list[str]:
        """喂一个 LLM 增量，返回可以立刻交给 TTS 的段（0~N 个）。"""
        if not delta:
            return []
        self._buf += delta
        self._last_update = self._clock()
        ready: list[str] = []
        while True:
            segment = self._take(force=False)
            if not segment:
                break
            ready.append(segment)
        return ready

    def poll_idle(self) -> list[str]:
        """LLM 停顿超时：把当前缓冲当成一段交出去（补逗号，语调保持"未说完"）。"""
        if not self._buf:
            return []
        threshold = self.cfg.first_idle_ms if self._first else self.cfg.idle_ms
        if (self._clock() - self._last_update) * 1000.0 < threshold:
            return []
        minimum = self.cfg.first_min_chars if self._first else self.cfg.min_chars
        if len(sanitize_for_speech(self._buf)) < minimum:
            return []
        segment = self._take(force=True)
        return [segment] if segment else []

    def flush(self) -> list[str]:
        """LLM 结束：把剩余文本作为最后一段（补句号，给 TTS 收尾语调）。"""
        if not self._buf:
            return []
        segment = self._take(force=True, final=True)
        if not segment:
            self._buf = ""
            return []
        return [segment]

    # ---- 内部 ----

    def _take(self, *, force: bool, final: bool = False) -> str | None:
        raw = self._buf
        if not raw:
            return None
        minimum = self.cfg.first_min_chars if self._first else self.cfg.min_chars

        hard_limit = self.cfg.first_max_chars if self._first else self.cfg.max_chars
        # 允许在硬上限之后再等一个 lookback 窗口的标点，尽量把完整句子留给同一段；
        # 只有连这个窗口都没有标点时才硬断（并补逗号），避免在词中间切开。
        slack = self.cfg.lookback
        cut: int | None = None

        # 找"第一个够长、且没有远到超过硬上限"的句末标点；太早的标点（如 "好的。"）
        # 跳过继续找下一个，避免为了一个 3 字短句把整句拖到后面。
        search = 0
        strong = -1
        while True:
            pos = _find_strong(raw, search)
            if pos < 0:
                break
            end = pos + 1
            if end < minimum:
                search = end
                continue
            if end <= hard_limit + slack:
                strong = pos
            break
        if strong >= 0:
            cut = _extend_cut(raw, strong + 1)
        elif self._first:
            # 首段：看到逗号就可以先出一小段，让设备尽早开口
            search = 0
            weak = -1
            while True:
                pos = _find_any(raw, WEAK_PUNCT, search)
                if pos < 0:
                    break
                end = pos + 1
                if end < minimum:
                    search = end
                    continue
                if end <= hard_limit:
                    weak = pos
                break
            if weak >= 0:
                cut = _extend_cut(raw, weak + 1)
        if cut is None and len(raw) >= hard_limit + slack:
            cut = _extend_cut(raw, self._boundary(raw, hard_limit + slack))
        elif cut is None and force:
            cut = len(raw)
        if cut is None:
            return None
        if cut >= len(raw) and not force and raw[-1] in STRONG_PUNCT:
            # 句末标点刚到：再等一个增量，看后面是否跟着右引号/括号；
            # 只多等一个 delta（几十 ms），换来段首不出现孤立符号。
            return None

        text, rest = raw[:cut], raw[cut:]
        self._buf = rest
        speech = sanitize_for_speech(text)
        if not has_speakable(speech):
            # 纯标点/装饰（例如 LLM 先吐一个 "**"）不是一句话，丢掉继续等
            return None
        speech = self._apply_punct(speech, final=final)
        self._first = False
        self._last_update = self._clock()
        return speech

    def _boundary(self, raw: str, limit: int) -> int:
        """超长硬断：优先在向前 lookback 窗口里的标点/空格处断。"""
        low = max(1, limit - self.cfg.lookback)
        for index in range(limit - 1, low - 1, -1):
            ch = raw[index]
            if ch in STRONG_PUNCT or ch in WEAK_PUNCT or ch.isspace():
                return index + 1
        cut = limit
        # 不要把英文单词/数字从中间切开（中文一个字一个词，不适用此规则）
        while (
            cut < len(raw)
            and raw[cut - 1].isascii()
            and raw[cut - 1].isalnum()
            and raw[cut].isascii()
            and raw[cut].isalnum()
        ):
            cut += 1
        return cut

    @staticmethod
    def _apply_punct(speech: str, *, final: bool) -> str:
        """给兜底切出来的段补标点：非末段补逗号，末段补句号；已有标点不动。

        断句点可能落在右引号/右括号**之前**（`_extend_cut` 已把这些符号并进本段），
        所以要先越过收尾符号看"真正最后一个字"，否则 `他说：“你好。”` 会被误补成
        `他说：“你好。”，`。
        """
        text = speech.strip()
        if not text:
            return text
        index = len(text) - 1
        while index >= 0 and text[index] in _CLOSERS:
            index -= 1
        if index < 0:
            return text
        last, tail = text[index], text[index + 1 :]
        if final:
            if last in STRONG_PUNCT or last in "。！？!?….":
                return text
            if last in WEAK_PUNCT:
                return text[:index] + _FINAL_PUNCT + tail
            return text[: index + 1] + _FINAL_PUNCT + tail
        if last in STRONG_PUNCT or last in WEAK_PUNCT or last in "….":
            return text
        return text[: index + 1] + _CONTINUE_PUNCT + tail


@dataclass
class PipelineConfig:
    prebuffer_ms: int = 800  # 攒够这么多音频再 tts_start，抵消 TTS 首块/段间抖动
    segmenter: SegmenterConfig = field(default_factory=SegmenterConfig)
    poll_ms: int = 50        # coordinator 的空闲轮询间隔


class LlmTtsPipeline:
    """LLM 流 + TTS 流 的线程编排；主协程消费 `events`。

    事件（tuple，第一项是 kind）：
      ("delta", text)                  LLM 文本增量（要原样转发 llm_delta）
      ("segment", text, age_s)         切出来的一段（age_s = 从流水线启动到切出这段的秒数）
      ("frame", payload, duration_ms)  一帧 24k MP3
      ("llm_done", answer, first_ms, ok)  LLM 结束（ok=False 表示失败/回退）
      ("llm_error", exc)               LLM 失败（可能仍有部分回答/回退文本）
      ("tts_done",)                    TTS 线程结束（所有 frame 已经 emit）
      ("tts_error", exc)               TTS 失败（随后仍会 emit tts_done）
    """

    def __init__(
        self,
        *,
        text: str,
        stream_llm: Callable[[], Iterable[str]],
        open_tts: Callable[[], Any],
        config: PipelineConfig | None = None,
        log: Callable[[str], None] = print,
    ) -> None:
        self.text = text
        self._stream_llm = stream_llm
        self._open_tts = open_tts
        self.cfg = config or PipelineConfig()
        self._log = log
        self.segmenter = SpeechSegmenter(self.cfg.segmenter)
        self.events: asyncio.Queue | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop = threading.Event()
        self._delta_q: queue.Queue = queue.Queue()
        self._seg_q: queue.Queue = queue.Queue()
        self._threads: list[threading.Thread] = []
        self._started = False
        self._t0 = 0.0

    # ---- 生命周期 ----

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        if self._started:
            return
        self._started = True
        self._loop = loop
        self._t0 = time.monotonic()
        self.events = asyncio.Queue()
        for name, target in (
            ("llm", self._llm_worker),
            ("llm-seg", self._coordinator),
            ("tts", self._tts_worker),
        ):
            thread = threading.Thread(target=target, name=name, daemon=True)
            thread.start()
            self._threads.append(thread)

    def stop(self) -> None:
        """取消：让三个线程尽快退出（MLX 正在生成的那一块无法强杀，最多跑完当前块）。"""
        self._stop.set()
        self._delta_q.put(None)
        self._seg_q.put(None)

    # ---- 线程 ----

    def _emit(self, *event: Any) -> None:
        loop, events = self._loop, self.events
        if loop is None or events is None or loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(events.put_nowait, event)
        except RuntimeError:  # 事件循环已关闭（断连/进程退出）
            pass

    def _llm_worker(self) -> None:
        started = time.monotonic()
        parts: list[str] = []
        first_ms = 0
        try:
            for delta in self._stream_llm():
                if self._stop.is_set():
                    return
                if not delta:
                    continue
                if not first_ms:
                    first_ms = int((time.monotonic() - started) * 1000)
                parts.append(delta)
                self._emit("delta", delta)
                self._delta_q.put(("delta", delta))
            answer = "".join(parts).strip()
            self._emit("llm_done", answer, first_ms, True)
            self._delta_q.put(("done", answer))
        except BaseException as exc:  # noqa: BLE001 - 线程内异常必须回传，不能静默死掉
            if self._stop.is_set():
                return
            self._emit("llm_error", exc)
            answer = "".join(parts).strip()
            if not answer:
                # 与旧行为一致：LLM 失败且没吐过内容 → 回退复读 ASR 文本
                answer = self.text
                self._emit("delta", answer)
                self._delta_q.put(("delta", answer))
            self._emit("llm_done", answer, first_ms, False)
            self._delta_q.put(("done", answer))

    def _coordinator(self) -> None:
        try:
            while not self._stop.is_set():
                try:
                    item = self._delta_q.get(timeout=self.cfg.poll_ms / 1000.0)
                except queue.Empty:
                    self._enqueue(self.segmenter.poll_idle())
                    continue
                if item is None or self._stop.is_set():
                    break
                kind = item[0]
                if kind == "delta":
                    self._enqueue(self.segmenter.feed(item[1]))
                elif kind == "done":
                    self._enqueue(self.segmenter.flush())
                    break
        finally:
            self._seg_q.put(None)

    def _enqueue(self, segments: Iterable[str]) -> None:
        for segment in segments:
            if not segment or self._stop.is_set():
                continue
            self._emit("segment", segment, time.monotonic() - self._t0)
            self._seg_q.put(segment)

    def _tts_worker(self) -> None:
        sink = None
        try:
            sink = self._open_tts()
            while not self._stop.is_set():
                text = self._seg_q.get()
                if text is None or self._stop.is_set():
                    break
                for payload, duration_ms in sink.add(text):
                    if self._stop.is_set():
                        break
                    self._emit("frame", payload, duration_ms)
                if self._stop.is_set():
                    break
            if sink is not None and not self._stop.is_set():
                for payload, duration_ms in sink.flush():
                    self._emit("frame", payload, duration_ms)
        except BaseException as exc:  # noqa: BLE001
            if not self._stop.is_set():
                self._emit("tts_error", exc)
        finally:
            self._emit("tts_done")
