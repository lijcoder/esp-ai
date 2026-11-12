#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""responses_client —— OpenAI **Responses API** 极简客户端（纯标准库）。

为什么不用 `/chat/completions`：当前对接的 Hermes 网关在 chat 接口上不返回工具调用
（2026-11-12 用户实测），Responses 接口的 `output` 数组能明确给出 `function_call` /
`mcp_call`，解析更稳定。本文件只做三件事：

  1. 发请求（`POST {base}/responses`，`stream=false`）；
  2. 把 `output` 归一化成 `ResponsesResult`（文本 + 工具调用）；
  3. 工具调用只暴露“名字 + 已解析的 arguments”，执行交给 `music_tools.execute_tool()`。

## 配置（全部走环境变量 / CLI，不写进仓库）

  LLM_BASE_URL   必填，OpenAI 兼容 base URL（形如 https://<网关>/v1）
  LLM_API_KEY    必填，Bearer token
  LLM_MODEL      必填，模型名
  LLM_TIMEOUT_S  默认 20
  LLM_SESSION_ID 可选，**同时**用作请求头 `X-Hermes-Session-Id` 和 body 的
                 `conversation` 字段（两个都保留，网关侧可任选其一追踪）

## 支持的工具调用形态

  - `{"type":"function_call","name":"music_play","arguments":"{...}","call_id":"..."}`
  - `{"type":"mcp_call","name":"music_play","arguments":"{...}","output":"{...}"}`
  - 有些网关把参数放在 `output` 里（arguments 为空）——两种都解析。
  - **Hermes 实际形态（2026-11-12 真机）**：网关把 MCP 工具再包一层元工具——
    `tool_describe{names:[...]}` 用于列工具，`tool_call{calls:[{name:"mcp__music__music_play",
    arguments:{...}}]}` 用于调用。这里会自动展开成真正的 `music_play`，并剥掉
    `mcp__<server>__` 命名空间；`tool_describe` 直接忽略。
  - 传 `tools=...` 时是“客户端执行”模式；不传则假定工具已配在网关（MCP）侧。

## 用法

  cfg = ResponsesConfig.from_env()
  res = responses_call("放一首晴天", cfg, tools=tool_definitions(), system=...)
  for call in res.tool_calls:
      cmd = execute_tool(session, call.name, call.arguments)
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

SESSION_HEADER = "X-Hermes-Session-Id"


class ResponsesError(RuntimeError):
    """请求/解析失败；调用方决定降级（mock 里回退成普通回答）。"""


@dataclass
class ResponsesConfig:
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    timeout_s: float = 20.0
    session_id: str = ""
    retries: int = 1

    @classmethod
    def from_env(cls, **overrides: Any) -> "ResponsesConfig":
        cfg = cls(
            base_url=os.environ.get("LLM_BASE_URL", ""),
            api_key=os.environ.get("LLM_API_KEY", ""),
            model=os.environ.get("LLM_MODEL", ""),
            timeout_s=float(os.environ.get("LLM_TIMEOUT_S", "20")),
            session_id=os.environ.get("LLM_SESSION_ID", "").strip(),
            retries=max(0, int(os.environ.get("LLM_RETRIES", "1"))),
        )
        for key, value in overrides.items():
            if value is not None and value != "":
                setattr(cfg, key, value)
        return cfg

    @property
    def endpoint(self) -> str:
        base = self.base_url.rstrip("/")
        if base.endswith("/responses"):
            return base
        return base + "/responses"

    def require_ready(self) -> None:
        missing = [
            name
            for name, value in (
                ("LLM_BASE_URL", self.base_url),
                ("LLM_MODEL", self.model),
                ("LLM_API_KEY", self.api_key),
            )
            if not value
        ]
        if missing:
            raise ResponsesError(
                "缺少 Responses API 配置：" + "、".join(missing)
                + "。先 export（地址/模型/token 不要提交进仓库）"
            )


@dataclass
class ToolCall:
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    call_id: str = ""
    source: str = "function"  # function / mcp
    output: str = ""           # MCP 工具返回（原始文本，调试用）

    def __str__(self) -> str:  # 日志友好
        return f"{self.name}({json.dumps(self.arguments, ensure_ascii=False)})"


@dataclass
class ResponsesResult:
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)
    request_id: str = ""

    @property
    def has_tool_calls(self) -> bool:
        return bool(self.tool_calls)


# ============================================================================
# 请求
# ============================================================================


def _content_blocks(system: str, user: str, history: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Responses API 的 `input` 数组；system 用 role=system 的 input_text 下发。"""
    items: list[dict[str, Any]] = []

    def text_item(role: str, text: str) -> dict[str, Any]:
        return {"role": role, "content": [{"type": "input_text", "text": text}]}

    if system:
        items.append(text_item("system", system))
    for turn in history or []:
        role = str(turn.get("role", "user"))
        content = turn.get("content", "")
        if isinstance(content, list):
            items.append({"role": role, "content": content})
        else:
            items.append(text_item(role, str(content)))
    items.append(text_item("user", user))
    return items


def _request_body(user: str, cfg: ResponsesConfig, *, tools: list[dict[str, Any]] | None,
                  system: str, history: list[dict[str, Any]] | None,
                  max_output_tokens: int) -> bytes:
    body: dict[str, Any] = {
        "model": cfg.model,
        "input": _content_blocks(system, user, history),
        "stream": False,
        "max_output_tokens": max_output_tokens,
    }
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"
    if cfg.session_id:
        # 会话追踪：与请求头 X-Hermes-Session-Id 并存（2026-11-12 用户要求）。
        # 值来自 LLM_SESSION_ID；沿用 chat 接口时期的会话 id，便于网关把两种接口串起来。
        body["conversation"] = cfg.session_id
    return json.dumps(body, ensure_ascii=False).encode("utf-8")


def _post(body: bytes, cfg: ResponsesConfig) -> dict[str, Any]:
    headers = {
        "Authorization": "Bearer " + cfg.api_key,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    if cfg.session_id:
        headers[SESSION_HEADER] = cfg.session_id
    request = urllib.request.Request(cfg.endpoint, data=body, headers=headers, method="POST")

    last_error: Exception | None = None
    for attempt in range(cfg.retries + 1):
        try:
            with urllib.request.urlopen(request, timeout=cfg.timeout_s) as response:
                payload = response.read().decode("utf-8", "replace")
            try:
                data = json.loads(payload)
            except json.JSONDecodeError as exc:
                raise ResponsesError(f"Responses 返回不是 JSON：{payload[:200]!r}") from exc
            if not isinstance(data, dict):
                raise ResponsesError(f"Responses 返回结构异常：{type(data).__name__}")
            if "error" in data:
                raise ResponsesError(f"Responses 返回错误：{data['error']}")
            return data
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            last_error = ResponsesError(f"Responses HTTP {exc.code}：{detail}")
            # 502/503/504：网关抖动，重试一次；4xx 直接失败（多半是路径/模型/鉴权错）
            if exc.code not in (502, 503, 504) or attempt >= cfg.retries:
                raise last_error from exc
        except urllib.error.URLError as exc:
            last_error = ResponsesError(f"Responses 连接失败（{cfg.endpoint}）：{exc.reason}")
            if attempt >= cfg.retries:
                raise last_error from exc
        except TimeoutError as exc:
            last_error = ResponsesError(f"Responses 超时（{cfg.timeout_s}s）")
            if attempt >= cfg.retries:
                raise last_error from exc
        time.sleep(0.4 * (attempt + 1))
    raise last_error or ResponsesError("Responses 请求失败")


# ============================================================================
# 解析
# ============================================================================


def parse_tool_output(text: str) -> dict[str, Any] | None:
    """把工具结果文本还原成 dict（我们的 MCP 工具返回 JSON 对象）。

    兼容三种形态：
      1. 纯 JSON：`{"ok":true,...}`（标准 `mcp_call.output`）；
      2. Hermes 包装：`<untrusted_tool_result ...>\n{...}\n</untrusted_tool_result>`，
         外层 JSON 形如 `{"result":"<内层 JSON 字符串>","structuredContent":{...}}`；
      3. 解析失败返回 None（调用方回退到本地执行器/曲库）。
    """
    if not isinstance(text, str):
        return None
    raw = text.strip()
    if not raw:
        return None

    if raw.startswith("<untrusted_tool_result"):
        start = raw.find("{")
        if start < 0:
            return None
        try:
            wrapper, _ = json.JSONDecoder().raw_decode(raw[start:])
        except json.JSONDecodeError:
            return None
        return _unwrap_tool_result(wrapper)

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if isinstance(data, dict) and ("result" in data or "structuredContent" in data):
        return _unwrap_tool_result(data)
    return data if isinstance(data, dict) else None


def _unwrap_tool_result(wrapper: Any) -> dict[str, Any] | None:
    if not isinstance(wrapper, dict):
        return None
    inner = wrapper.get("result")
    if inner is None:
        structured = wrapper.get("structuredContent")
        if isinstance(structured, dict):
            inner = structured.get("result")
    if isinstance(inner, str):
        try:
            inner = json.loads(inner)
        except json.JSONDecodeError:
            return None
    return inner if isinstance(inner, dict) else None




def _parse_args(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


_META_DESCRIBE = {"tool_describe", "describe_tools", "tool_list", "tools_list",
                  "list_tools"}
_META_CALL = {"tool_call", "tool_calls", "call_tool", "call_tools",
              "tool_invoke", "invoke_tool"}


def _strip_mcp_prefix(name: str) -> str:
    """`mcp__music__music_play` → `music_play`（Hermes/MCP 命名空间）。"""
    if name.startswith("mcp__"):
        parts = name.split("__", 2)
        if len(parts) == 3 and parts[2]:
            return parts[2]
    return name


def _append_tool_call(sink: list[ToolCall], name: str, arguments: dict[str, Any],
                      *, source: str, call_id: str = "", output: str = "",
                      depth: int = 0) -> None:
    """把一条（可能被 Hermes 元工具包了一层的）调用展开进 sink。"""
    if name in _META_DESCRIBE:
        return  # tool_describe 只是列工具，没有业务动作
    if name in _META_CALL and isinstance(arguments.get("calls"), list) and depth < 2:
        for call in arguments["calls"]:
            if not isinstance(call, dict):
                continue
            inner = _strip_mcp_prefix(str(call.get("name", "")))
            _append_tool_call(sink, inner, _parse_args(call.get("arguments")),
                              source=source, call_id=call_id, depth=depth + 1)
        return
    sink.append(ToolCall(name=_strip_mcp_prefix(name), arguments=arguments,
                         call_id=call_id, source=source, output=output))


def _args_from_output(payload: dict[str, Any]) -> dict[str, Any]:
    """从工具返回 JSON 反推调用参数（网关只回 output、不回 arguments 时用）。

    我们的工具结果里 track 带 `track_id`，足够让 `music_play` 回放；
    pause/resume/stop 本身不需要参数。搜索/状态结果无法反推 query，按空参数处理。
    """
    track = payload.get("track")
    if isinstance(track, dict) and track.get("track_id"):
        return {"track_id": str(track["track_id"])}
    return {}


def _message_text(item: dict[str, Any]) -> str:
    content = item.get("content")
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for block in content or []:
        if not isinstance(block, dict):
            continue
        text = block.get("text")
        if isinstance(text, str) and text:
            parts.append(text)
    return "".join(parts)


def parse_response(data: dict[str, Any]) -> ResponsesResult:
    """把 Responses 的 `output` 归一化；兼容个别网关的 chat 风格返回。"""
    result = ResponsesResult(raw=data, request_id=str(data.get("id", "")))

    output = data.get("output")
    call_outputs: dict[str, str] = {}
    if isinstance(output, list):
        for item in output:
            if (isinstance(item, dict) and item.get("type") == "function_call_output"
                    and item.get("call_id")):
                call_outputs[str(item["call_id"])] = str(item.get("output") or "")
        for item in output:
            if not isinstance(item, dict):
                continue
            kind = str(item.get("type", ""))
            if kind in ("function_call", "tool_call"):
                _append_tool_call(
                    result.tool_calls,
                    str(item.get("name", "")),
                    _parse_args(item.get("arguments")),
                    source="function",
                    call_id=str(item.get("call_id") or item.get("id") or ""),
                )
            elif kind in ("mcp_call", "mcp_tool_call"):
                arguments = _parse_args(item.get("arguments"))
                output_text = item.get("output")
                if not isinstance(output_text, str):
                    output_text = json.dumps(output_text, ensure_ascii=False) if output_text else ""
                if not arguments:
                    # 有些网关只回工具结果，不回入参：从 output 里兜底还原
                    arguments = _args_from_output(_parse_args(output_text))
                _append_tool_call(
                    result.tool_calls,
                    str(item.get("name", "")),
                    arguments,
                    source="mcp",
                    call_id=str(item.get("id") or item.get("call_id") or ""),
                    output=output_text,
                )
            elif kind == "message":
                result.text += _message_text(item)

    # Hermes 把 MCP 结果单独放在 function_call_output 里；按 call_id 贴回调用，
    # 供上层（music_tools.apply_remote）直接拿 URL，不再依赖本地曲库。
    for call in result.tool_calls:
        if not call.output and call.call_id in call_outputs:
            call.output = call_outputs[call.call_id]

    # output_text 是汇总字段（可能和 message 重复）
    top_text = data.get("output_text")
    if isinstance(top_text, str) and top_text.strip():
        result.text = result.text or top_text

    # 兜底：网关把 /responses 映射成 chat 风格（choices）
    if not output and isinstance(data.get("choices"), list):
        choice = data["choices"][0] if data["choices"] else {}
        message = (choice or {}).get("message") or {}
        if isinstance(message.get("content"), str):
            result.text = message["content"]
        for call in message.get("tool_calls") or []:
            function = (call or {}).get("function") or {}
            _append_tool_call(
                result.tool_calls,
                str(function.get("name", "")),
                _parse_args(function.get("arguments")),
                source="function",
                call_id=str((call or {}).get("id", "")),
            )

    return result


# ============================================================================
# 对外 API
# ============================================================================


def responses_call(
    user_text: str,
    cfg: ResponsesConfig,
    *,
    system: str = "",
    tools: list[dict[str, Any]] | None = None,
    history: list[dict[str, Any]] | None = None,
    max_output_tokens: int = 512,
) -> ResponsesResult:
    """一轮 Responses 调用（同步阻塞，调用方自己放线程里）。"""
    cfg.require_ready()
    body = _request_body(user_text, cfg, tools=tools, system=system, history=history,
                         max_output_tokens=max_output_tokens)
    return parse_response(_post(body, cfg))


def parse_tool_calls(result: ResponsesResult) -> list[ToolCall]:
    """按出现顺序返回工具调用；空名字的条目丢弃。"""
    return [call for call in result.tool_calls if call.name]
