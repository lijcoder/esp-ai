#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""fake_llm_server.py —— 本地假的 OpenAI 兼容流式 LLM（测试 mock 的流水线，不需要真 token）

用途：没有 `LLM_API_KEY` 时也能验证 `ws_mock_server.py --llm` 的
「SSE 增量 → 分句 → TTS 流水线 → 设备边出字边播」整条链路，并且可以精确控制
首包延迟/出字速度/标点，复现"LLM 很慢、回答很长"的场景。

实现 mock 用到的三块：`/v1/chat/completions`（stream=true 的 SSE）、
`/v1/responses`（非流式；**可返回工具调用**，用来离线验证音乐工具链路），以及 `/health`。
回答固定由 `--answer` 指定，按 `--chunk-chars` 切块、每块间隔 `--chunk-delay-ms` 吐出，
首块前等 `--first-delay-ms`。

音乐工具（2026-11-12）：
  --tool-mode mcp        → output 里给一条 mcp_call（Hermes 已配 MCP 时的形态）
  --tool-mode function   → output 里给一条 function_call（本地 inline tools 形态）
  --tool-name / --tool-args 指定工具与参数；--tool-match 只在用户文本包含该子串时触发

用法：
  # 终端 A：假 LLM（打印 FAKE_LLM_LISTENING <base_url>）
  uv run tools/fake_llm_server.py --port 8799

  # 终端 B：mock 指向假 LLM（token/model 随便填）
  uv run --with av --with numpy --with sherpa-onnx --with mlx-audio --with lameenc \\
    tools/ws_mock_server.py --llm --llm-base-url http://127.0.0.1:8799/v1 \\
    --llm-model fake --llm-api-key fake --tts-qwen --asr none \\
    --tts-text "今天天气怎么样？"

  # 终端 C：假设备验证（--expect-pipeline 断言首声早于 LLM 完成）
  uv run tools/mock_offline_test.py --wav tools/今天天气如何.wav \\
    --expect-llm --expect-pipeline
"""

from __future__ import annotations

import argparse
import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DEFAULT_ANSWER = (
    "今天天气不错，适合出门散步。"
    "傍晚可能会有点风，记得带件外套。"
    "要不要我帮你查一下具体的温度？"
)


def _responses_user_text(items: object) -> str:
    """从 Responses 的 input（字符串或 item 数组）里取最后一条 user 文本。"""
    if isinstance(items, str):
        return items
    text = ""
    for item in items or []:
        if not isinstance(item, dict) or item.get("role") != "user":
            continue
        content = item.get("content", "")
        if isinstance(content, str):
            text = content
        else:
            text = "".join(
                str(block.get("text", "")) for block in content if isinstance(block, dict)
            )
    return text


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "fake-llm/1.0"

    # ---- 静默 http.server 的默认访问日志，只打自己的摘要 ----
    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        return

    def _json(self, obj: dict, status: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.path in ("/health", "/"):
            self._json({"ok": True, "service": "fake-llm"})
            return
        self._json({"error": "not found"}, 404)

    def do_POST(self) -> None:  # noqa: N802
        if self.path.rstrip("/").endswith("/responses"):
            self._responses()
            return
        if not self.path.rstrip("/").endswith("/chat/completions"):
            self._json({"error": "not found"}, 404)
            return
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            request = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._json({"error": {"message": "bad json"}}, 400)
            return

        messages = request.get("messages") or []
        user_text = ""
        for message in reversed(messages):
            if message.get("role") == "user":
                user_text = str(message.get("content") or "")
                break
        stream = bool(request.get("stream"))

        cfg = self.server.cfg  # type: ignore[attr-defined]
        answer = cfg.answer
        if cfg.echo_user and user_text:
            answer = f"你刚才说的是：{user_text}。{answer}"
        print(
            f"[fake-llm] {'stream' if stream else 'json'} 请求：user={user_text!r} "
            f"→ 回答 {len(answer)} 字 / 首块 {cfg.first_delay_ms}ms / "
            f"每 {cfg.chunk_delay_ms}ms 一块",
            flush=True,
        )

        if not stream:
            self._json(
                {
                    "id": "fake-1",
                    "object": "chat.completion",
                    "model": request.get("model", "fake"),
                    "choices": [{"index": 0, "message": {"role": "assistant", "content": answer}}],
                }
            )
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()

        try:
            self._sleep(cfg.first_delay_ms)
            for index in range(0, len(answer), max(1, cfg.chunk_chars)):
                chunk = answer[index : index + max(1, cfg.chunk_chars)]
                self._sse({"choices": [{"index": 0, "delta": {"content": chunk}}]})
                self._sleep(cfg.chunk_delay_ms)
            self._sse({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            print("[fake-llm] 客户端断开，停止本轮", flush=True)

    # ---- Responses API：可返回工具调用（音乐工具链路离线测试） ----

    def _responses(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            request = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._json({"error": {"message": "bad json"}}, 400)
            return
        cfg = self.server.cfg  # type: ignore[attr-defined]
        user_text = _responses_user_text(request.get("input"))
        answer = cfg.answer
        if cfg.echo_user and user_text:
            answer = f"你刚才说的是：{user_text}。{answer}"

        output: list[dict] = []
        mode = cfg.tool_mode
        name = cfg.tool_name
        args_text = cfg.tool_args
        triggered = mode != "none" and (not cfg.tool_match or cfg.tool_match in user_text)
        # --tool-map：按子串映射到不同工具（真机回归脚本用；顺序即优先级）
        if not triggered and cfg.tool_map:
            for keyword, mapped in cfg.tool_map.items():
                if keyword in user_text:
                    mode, name, args_text, triggered = "mcp", mapped, "{}", True
                    break
        if triggered:
            try:
                arguments = json.loads(args_text or "{}")
            except json.JSONDecodeError:
                arguments = {}
            arg_text = json.dumps(arguments, ensure_ascii=False)
            if mode == "mcp":
                output.append({
                    "type": "mcp_call",
                    "id": "mcp_1",
                    "server_label": "music",
                    "name": name,
                    "arguments": arg_text,
                    "output": json.dumps({"ok": True}, ensure_ascii=False),
                })
            else:
                output.append({
                    "type": "function_call",
                    "id": "fc_1",
                    "call_id": "call_1",
                    "name": name,
                    "arguments": arg_text,
                })
        output.append({
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": answer}],
        })
        print(
            f"[fake-llm] responses 请求：user={user_text!r} → tool={name if triggered else '-'} "
            f"args={args_text if triggered else '-'}",
            flush=True,
        )
        self._json({
            "id": "resp_fake_1",
            "object": "response",
            "status": "completed",
            "model": request.get("model", "fake"),
            "output": output,
            "output_text": answer,
        })

    def _sse(self, obj: dict) -> None:
        payload = json.dumps(obj, ensure_ascii=False)
        self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
        self.wfile.flush()

    @staticmethod
    def _sleep(ms: int) -> None:
        if ms > 0:
            time.sleep(ms / 1000.0)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="本地假 OpenAI 兼容流式 LLM（测试 mock 的 LLM→TTS 流水线）",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--host", default="127.0.0.1", help="监听地址")
    parser.add_argument("--port", type=int, default=8799, help="监听端口（0=系统分配）")
    parser.add_argument("--answer", default=DEFAULT_ANSWER, help="固定回答文本（含标点，便于验证分句）")
    parser.add_argument("--chunk-chars", type=int, default=3, help="每块字符数")
    parser.add_argument("--chunk-delay-ms", type=int, default=70, help="块间隔毫秒（模拟 LLM 出字速度）")
    parser.add_argument("--first-delay-ms", type=int, default=600, help="首块前的首包延迟")
    parser.add_argument("--echo-user", action="store_true", help="回答前先复述用户问题（便于确认请求内容）")
    parser.add_argument("--tool-mode", choices=("none", "mcp", "function"), default="none",
                        help="Responses 返回的工具调用形态（none=只回答）")
    parser.add_argument("--tool-name", default="music_play", help="工具名")
    parser.add_argument("--tool-args", default='{"query":""}',
                        help='工具参数 JSON（如 {"query":"晴天"}）')
    parser.add_argument("--tool-match", default="",
                        help="仅在用户文本包含该子串时返回工具调用（空=总是）")
    parser.add_argument("--tool-map", default="",
                        help='JSON：{"子串":"工具名"}，按子串顺序匹配（真机回归用，如 {"暂停":"music_pause"}）')
    args = parser.parse_args(argv)
    if args.tool_map:
        try:
            parsed = json.loads(args.tool_map)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"--tool-map 不是合法 JSON：{exc}") from exc
        if not isinstance(parsed, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in parsed.items()
        ):
            raise SystemExit("--tool-map 必须是 {子串: 工具名} 的对象")
        args.tool_map = parsed
    else:
        args.tool_map = {}

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.daemon_threads = True
    server.cfg = args  # type: ignore[attr-defined]
    host, port = server.server_address[:2]
    base = f"http://{'127.0.0.1' if host in ('0.0.0.0', '') else host}:{port}/v1"
    print(f"FAKE_LLM_LISTENING {base}", flush=True)
    print(
        f"[fake-llm] 回答 {len(args.answer)} 字 / 首块 {args.first_delay_ms}ms / "
        f"每 {args.chunk_delay_ms}ms {args.chunk_chars} 字 → 约 "
        f"{len(args.answer) / max(1, args.chunk_chars) * args.chunk_delay_ms / 1000:.1f}s 出完",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[fake-llm] 退出", flush=True)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
