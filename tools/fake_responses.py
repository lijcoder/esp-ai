#!/usr/bin/env python3
"""fake_responses —— 进程内假 Responses API 网关（测试/演示专用，不是产品代码）。

它只做一件事：收到一轮用户文本，调用 `decide(text)` 决定“模型要调哪个音乐工具”，
然后按 Responses 接口的 `output` 数组返回（`mcp_call` 或 `function_call`）+ 一条
assistant 消息。这样可以在**没有 token、没有 Hermes** 的情况下验证：

    mock_ws_server → responses_client → parse tool_call → music_tools.execute
                   → music_track / music_control → 假设备

注意：`decide` 是**测试脚本里的模拟 LLM**，不是音乐意图规则表；产品路径里这个决策
完全由 Hermes/LLM 做（见 docs/ARCHITECTURE.md §6.3）。
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

Decide = Callable[[str], "tuple[str, dict[str, Any]] | None"]


def last_user_text(items: Any) -> str:
    """从 Responses 的 `input`（字符串或 item 数组）里取最后一条 user 文本。"""
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
            text = "".join(str(block.get("text", "")) for block in content
                           if isinstance(block, dict))
    return text


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self) -> None:  # noqa: N802
        if not self.path.rstrip("/").endswith("/responses"):
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            request = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self.send_response(400)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        server = self.server
        server.requests.append(request)  # type: ignore[attr-defined]
        text = last_user_text(request.get("input"))
        decision = server.decide(text)  # type: ignore[attr-defined]
        output: list[dict[str, Any]] = []
        if decision is not None:
            name, arguments = decision
            arg_text = json.dumps(arguments, ensure_ascii=False)
            if server.style == "hermes":  # type: ignore[attr-defined]
                # Hermes 真机形态：元工具 tool_describe + tool_call，工具名带 mcp__<server>__ 前缀
                output.append({
                    "type": "mcp_call",
                    "id": "mcp_describe_1",
                    "name": "tool_describe",
                    "arguments": json.dumps({"names": [
                        "mcp__music__music_search", "mcp__music__music_play",
                    ]}, ensure_ascii=False),
                })
                output.append({
                    "type": "mcp_call",
                    "id": "mcp_call_1",
                    "name": "tool_call",
                    "arguments": json.dumps({"calls": [{
                        "name": f"mcp__music__{name}",
                        "arguments": arguments,
                    }]}, ensure_ascii=False),
                })
            elif server.style == "function":
                output.append({
                    "type": "function_call",
                    "id": "fc_fake_1",
                    "call_id": "call_fake_1",
                    "name": name,
                    "arguments": arg_text,
                })
            else:
                output.append({
                    "type": "mcp_call",
                    "id": "mcp_fake_1",
                    "server_label": "music",
                    "name": name,
                    "arguments": arg_text,
                    "output": json.dumps({"ok": True}, ensure_ascii=False),
                })
        answer = server.answer  # type: ignore[attr-defined]
        output.append({
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": answer}],
        })
        body = json.dumps({
            "id": "resp_fake_1",
            "object": "response",
            "status": "completed",
            "output": output,
            "output_text": answer,
        }, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        pass


class FakeResponsesServer:
    """进程内假网关：`decide(text) -> (工具名, 参数) | None`。"""

    def __init__(self, decide: Decide, answer: str = "好的，没问题。",
                 style: str = "mcp", host: str = "127.0.0.1", port: int = 0) -> None:
        """style: mcp（标准 mcp_call）/ function（function_call）/ hermes（元工具包装）。"""
        self.httpd = ThreadingHTTPServer((host, port), _Handler)
        self.httpd.daemon_threads = True
        self.httpd.decide = decide  # type: ignore[attr-defined]
        self.httpd.answer = answer  # type: ignore[attr-defined]
        self.httpd.style = style  # type: ignore[attr-defined]
        self.httpd.requests = []  # type: ignore[attr-defined]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base(self) -> str:
        """直接给 `LLM_BASE_URL` / Config.llm_base_url 用（客户端会补 /responses）。"""
        return f"http://127.0.0.1:{self.httpd.server_address[1]}/v1"

    @property
    def requests(self) -> list[dict[str, Any]]:
        return self.httpd.requests  # type: ignore[attr-defined]

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
