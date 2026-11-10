#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""mock_pipeline_test.py —— LLM→TTS 流水线的离线自测（纯标准库，不占设备/不加载模型）

覆盖两件事：
  1. `SpeechSegmenter` 的分句是否自然（句末标点优先、首段尽早、超长兜底、标点补齐）；
  2. `LlmTtsPipeline` 的线程编排是否真的"边出字边合成"（首帧早于 LLM 完成）、
     失败回退、stop 不漏事件。

运行：
  uv run tools/mock_pipeline_test.py
退出码 0 = 全部 PASS，1 = 有 FAIL。
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mock_pipeline import (  # noqa: E402
    LlmTtsPipeline,
    PipelineConfig,
    SegmenterConfig,
    SpeechSegmenter,
    sanitize_for_speech,
)

ANSWER = (
    "今天天气不错，适合出门散步。"
    "傍晚可能会有点风，记得带件外套。"
    "要不要我帮你查一下具体的温度？"
)

_CHECKS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    _CHECKS.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  {detail}" if detail and not ok else ""), flush=True)


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, ms: float) -> None:
        self.now += ms / 1000.0


def test_segmenter() -> None:
    seg = SpeechSegmenter(SegmenterConfig())
    segments: list[str] = []
    for ch in ANSWER:
        segments.extend(seg.feed(ch))
    segments.extend(seg.flush())
    check("分句：3 个完整句（句末标点优先）", len(segments) == 3, repr(segments))
    check("分句：保留句末标点", all(s.endswith(("。", "？", "！")) for s in segments), repr(segments))
    check("分句：没有把句子切成碎块", all(len(s) >= 8 for s in segments), repr(segments))

    # 首段尽早：逗号后够 first_min_chars 就先出一段（模拟逐字 SSE，而不是一次给整句）
    eager = SpeechSegmenter(SegmenterConfig(first_min_chars=6))
    out: list[str] = []
    for ch in "今天天气不错，适合出门散步。":
        out.extend(eager.feed(ch))
    out.extend(eager.flush())
    check("首段：逗号处尽早断（>= first_min_chars）", out and out[0] == "今天天气不错，", repr(out))

    # 太短不断：等下一个标点
    short = SpeechSegmenter(SegmenterConfig(first_min_chars=8, min_chars=6))
    check("短句：少于最少字数不急着断", short.feed("好的。") == [], repr(short.feed("")))

    # idle 兜底：停顿超时补逗号
    clock = FakeClock()
    idle = SpeechSegmenter(SegmenterConfig(first_min_chars=6, first_idle_ms=300), clock=clock)
    idle.feed("今天天气不错适合出门散步")
    clock.advance(400)
    out = idle.poll_idle()
    check("空闲兜底：补逗号表示未说完", out == ["今天天气不错适合出门散步，"], repr(out))

    # flush 补句号
    final = SpeechSegmenter(SegmenterConfig())
    final.feed("今天天气不错")
    out = final.flush()
    check("收尾：补句号", out == ["今天天气不错。"], repr(out))

    # 超长硬断：在标点处断，且补逗号
    long = SpeechSegmenter(SegmenterConfig(first_min_chars=6, min_chars=6, max_chars=12, first_max_chars=12))
    out = long.feed("一二三四五，六七八九十十一十二")
    check("超长：在标点/边界断，不超 max_chars", out and len(out[0]) <= 13, repr(out))

    # 小数点不拆
    decimal = SpeechSegmenter(SegmenterConfig(first_min_chars=6, max_chars=10, first_max_chars=10))
    out = decimal.feed("今天是 3.14 度左右")
    check("小数点：不把 3.14 拆开", all("3." not in s[-2:] for s in out), repr(out))

    # 右引号并入本段（逐字 SSE：句号刚到先等一下，等引号到了再断）
    quoted = SpeechSegmenter(SegmenterConfig(first_min_chars=6))
    out = []
    for ch in "他说：“你好。”然后走了。":
        out.extend(quoted.feed(ch))
    out.extend(quoted.flush())
    check("引号收尾：右引号并入本段", out and out[0].endswith("”"), repr(out))

    # 首段优先上限：句号在上限之后、但在 lookback 窗口内 → 仍等整句，不硬断
    long_first = SpeechSegmenter(
        SegmenterConfig(first_min_chars=6, first_max_chars=12, max_chars=24, lookback=12)
    )
    out = []
    for ch in "一二三四五六七八九十一二三四五六七八九十。":
        out.extend(long_first.feed(ch))
    out.extend(long_first.flush())
    check("首段：优先上限后仍在 lookback 内 → 等整句", out and out[0].endswith("。"), repr(out))

    # 完全没有标点、且超过 优先上限 + lookback → 才硬断（补逗号）
    hard = SpeechSegmenter(
        SegmenterConfig(first_min_chars=6, first_max_chars=12, max_chars=24, lookback=2)
    )
    out = []
    for ch in "一二三四五六七八九十一二三四五六七八九十一二三四五六七八九十":
        out.extend(hard.feed(ch))
    out.extend(hard.flush())
    check("首段：无标点超过上限才硬断", out and len(out[0]) <= 15, repr(out))

    # 文本清理
    cleaned = sanitize_for_speech("**你好** 😀 https://x.com/a 今天 不错\n")
    check("清理：去 markdown/emoji/URL/换行", "**" not in cleaned and "😀" not in cleaned
          and "https" not in cleaned and "\n" not in cleaned, repr(cleaned))


class FakeSink:
    """按 3x 实时速度吐 24ms 帧的假 TTS。"""

    def __init__(self) -> None:
        self.frames = 0

    def add(self, text: str):
        for _ in range(max(1, len(text) * 2)):
            time.sleep(0.004)
            self.frames += 1
            yield b"FRAME", 24.0

    def flush(self):
        return iter(())


def _fake_stream(text: str, *, delay_s: float = 0.02, chunk: int = 2, fail_at: int | None = None):
    for index in range(0, len(text), chunk):
        if fail_at is not None and index >= fail_at:
            raise RuntimeError("fake llm failure")
        time.sleep(delay_s)
        yield text[index : index + chunk]


async def _collect(pipeline: LlmTtsPipeline, timeout: float = 8.0) -> list[tuple]:
    events: list[tuple] = []
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AssertionError("等待 tts_done 超时")
        event = await asyncio.wait_for(pipeline.events.get(), remaining)
        events.append(event)
        if event[0] == "tts_done":
            return events


def test_pipeline() -> None:
    async def run_ok() -> None:
        pipeline = LlmTtsPipeline(
            text=ANSWER,
            stream_llm=lambda: _fake_stream(ANSWER, delay_s=0.03),
            open_tts=FakeSink,
            config=PipelineConfig(prebuffer_ms=100),
        )
        started = time.monotonic()
        pipeline.start(asyncio.get_running_loop())
        events = await _collect(pipeline)
        pipeline.stop()
        first_frame = next((i for i, e in enumerate(events) if e[0] == "frame"), None)
        llm_done = next((i for i, e in enumerate(events) if e[0] == "llm_done"), None)
        check("流水线：首帧在 LLM 完成之前（边出字边合成）",
              first_frame is not None and llm_done is not None and first_frame < llm_done,
              f"first_frame={first_frame} llm_done={llm_done}")
        check("流水线：至少切成 2 段", sum(1 for e in events if e[0] == "segment") >= 2)
        check("流水线：LLM 成功 ok=True",
              any(e[0] == "llm_done" and e[3] is True for e in events))
        check("流水线：tts_done 在所有 frame 之后",
              events[-1][0] == "tts_done" and any(e[0] == "frame" for e in events))
        check("流水线：整轮 < 3s（不阻塞主循环）", time.monotonic() - started < 3.0,
              f"{time.monotonic() - started:.2f}s")

    async def run_fallback() -> None:
        pipeline = LlmTtsPipeline(
            text="回退文本。",
            stream_llm=lambda: _fake_stream("不回退", fail_at=0),
            open_tts=FakeSink,
            config=PipelineConfig(prebuffer_ms=0),
        )
        pipeline.start(asyncio.get_running_loop())
        events = await _collect(pipeline)
        pipeline.stop()
        deltas = "".join(e[1] for e in events if e[0] == "delta")
        check("失败回退：LLM 没吐内容时复读 ASR 文本", deltas == "回退文本。", repr(deltas))
        check("失败回退：llm_done ok=False",
              any(e[0] == "llm_done" and e[3] is False for e in events))
        check("失败回退：仍有 tts_done", events[-1][0] == "tts_done")

    async def run_stop() -> None:
        pipeline = LlmTtsPipeline(
            text=ANSWER,
            stream_llm=lambda: _fake_stream(ANSWER, delay_s=0.2),
            open_tts=FakeSink,
            config=PipelineConfig(prebuffer_ms=0),
        )
        pipeline.start(asyncio.get_running_loop())
        # 等到第一帧就取消
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            event = await asyncio.wait_for(pipeline.events.get(), 3)
            if event[0] == "frame":
                break
        pipeline.stop()
        events = await _collect(pipeline, timeout=3)
        check("stop：取消后仍能收到 tts_done（不卡死）", events[-1][0] == "tts_done")

    asyncio.run(run_ok())
    asyncio.run(run_fallback())
    asyncio.run(run_stop())


def main() -> int:
    test_segmenter()
    test_pipeline()
    failed = [name for name, ok, _ in _CHECKS if not ok]
    print(f"\nmock_pipeline_test: {len(_CHECKS) - len(failed)}/{len(_CHECKS)} PASS" +
          ("" if not failed else f"（FAIL：{'；'.join(failed)}）"), flush=True)
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
