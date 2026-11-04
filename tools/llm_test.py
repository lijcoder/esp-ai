#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""llm_test.py —— 离线 LLM 模块测试：文字 → 大模型 → 文字（可流式打印）

**不依赖开发板、不依赖 mock 服务**：直接打 OpenAI 兼容的 `/chat/completions`（SSE 流式），
验证模型连通性、首包时延、吞吐与回答风格，用于和 ASR/TTS 模块测试配套（后续接进 mock）。

配置走环境变量（`tools/llm_client.py` 读取），**地址/模型/token 都不写进文件**：

  export LLM_BASE_URL=https://<你的网关>/v1
  export LLM_MODEL=<模型名>
  export LLM_API_KEY=sk-...

用法：
  uv run tools/llm_test.py --text "今天天气怎么样？"
  uv run tools/llm_test.py --text "用一句话介绍你自己" --model <另一个模型名>
  uv run tools/llm_test.py --text "..." --no-stream            # 只看完整答案
  uv run tools/llm_test.py --text "..." --system "回答要像朋友聊天" --max-tokens 120
  printf '明天适合跑步吗？' | uv run tools/llm_test.py

输出：首包时延 / 总耗时 / 字数 / 吞吐，以及流式打印的回答。
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import llm_client  # noqa: E402  —— 与 mock 的 --llm 共用同一份 HTTP/SSE 实现


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="离线 LLM 模块测试：文字 → OpenAI 兼容接口 → 文字（流式）",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--text", default="", help="用户输入（也可 --text-file / stdin）")
    p.add_argument("--text-file", default="", help="从文件读用户输入（UTF-8）")
    p.add_argument("--model", default=None, help="模型名（空=取 LLM_MODEL，必填）")
    p.add_argument("--base-url", default=None, help="接口 base URL（空=取 LLM_BASE_URL，必填）")
    p.add_argument("--api-key", default=None, help="Bearer token（默认取 LLM_API_KEY；不建议写在命令行历史里）")
    p.add_argument("--system", default=None, help="系统提示（默认取 LLM_SYSTEM_PROMPT）")
    p.add_argument("--max-tokens", type=int, default=None, help="最大输出 token（默认 256）")
    p.add_argument("--temperature", type=float, default=None, help="采样温度（默认 0.6）")
    p.add_argument("--no-stream", action="store_true", help="不用流式（一次性取完整答案）")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    text = args.text.strip()
    if not text and args.text_file:
        text = Path(args.text_file).read_text(encoding="utf-8").strip()
    if not text and not sys.stdin.isatty():
        text = sys.stdin.read().strip()
    if not text:
        build_parser().print_help()
        return 2

    cfg = llm_client.LlmConfig.from_env(
        model=args.model,
        base_url=args.base_url,
        api_key=args.api_key,
        system=args.system,
        max_tokens=args.max_tokens,
        temperature=args.temperature,
    )
    try:
        cfg.require_ready()
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc

    print(f"[LLM] {cfg.model} @ {cfg.endpoint}", flush=True)
    print(f"[LLM] 用户：{text}", flush=True)
    print("[LLM] 回答：", end="", flush=True)

    started = time.monotonic()
    first_at: float | None = None
    answer: list[str] = []
    try:
        if args.no_stream:
            answer_text = llm_client.chat(text, cfg)
            first_at = time.monotonic() - started
            answer.append(answer_text)
            print(answer_text, flush=True)
        else:
            for chunk in llm_client.stream_chat(text, cfg):
                if first_at is None:
                    first_at = time.monotonic() - started
                answer.append(chunk)
                print(chunk, end="", flush=True)
            print(flush=True)
    except RuntimeError as exc:
        print(f"\n[LLM] 失败：{exc}", flush=True)
        return 1

    total = time.monotonic() - started
    full = "".join(answer)
    chars = len(full.strip())
    print(
        f"[LLM] 首包 {first_at or 0:.2f}s / 总 {total:.2f}s / {chars} 字"
        f"（{chars / total:.1f} 字/秒，{('流式' if not args.no_stream else '非流式')}）",
        flush=True,
    )
    return 0 if full.strip() else 1


if __name__ == "__main__":
    raise SystemExit(main())
