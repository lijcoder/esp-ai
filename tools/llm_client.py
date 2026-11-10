#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""llm_client.py —— LLM（OpenAI 兼容 /chat/completions）的极简流式客户端（纯标准库）

给 `tools/llm_test.py` 与 mock 的 `--llm` 共用，避免两套 HTTP 代码。
**不依赖 openai SDK**：只用 `urllib` 读 SSE（`data: {...}` 行），流式产出 `delta.content`。

配置（**地址、模型、token 全部走环境变量，不写进代码/仓库**）：
  LLM_BASE_URL    **必填**：OpenAI 兼容 base URL（形如 https://<网关>/v1）
  LLM_API_KEY     **必填**：Bearer token（sk-…）
  LLM_MODEL       **必填**：模型名
  LLM_SYSTEM_PROMPT  可选：系统提示（默认：语音助手、口语化、简短、不要思考过程）
  LLM_MAX_TOKENS  默认 256
  LLM_TEMPERATURE 默认 0.6
  LLM_TIMEOUT_S   默认 20（首包超时/整体读超时）
  LLM_SESSION_ID  可选：网关会话追踪头 `X-Hermes-Session-Id`（空=不带该头），
                  便于在网关侧把 mock/真机的一轮轮请求串成同一个会话

接口：
  cfg = LlmConfig.from_env()
  for chunk in stream_chat("今天天气怎么样", cfg): print(chunk, end="")
  answer = chat("今天天气怎么样", cfg)

注意：
  - 该网关的部分模型会在 `delta.reasoning_content` 里先吐思考过程，**这里只取 `content`**；
  - 出错（非 200 / 连接失败 / 超时）抛 `RuntimeError`，调用方决定降级（mock 回退复读）。
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

DEFAULT_SYSTEM = (
    "你是桌面语音助手，回答会被直接朗读出来。"
    "要求：像跟朋友聊天一样自然、口语化，用完整的短句和正常标点；"
    "一般不超过 60 字；不要用列表、代码、表情符号、markdown，也不要输出思考过程，只给最终回答。"
)

SESSION_HEADER = "X-Hermes-Session-Id"


@dataclass
class LlmConfig:
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    system: str = DEFAULT_SYSTEM
    max_tokens: int = 256
    temperature: float = 0.6
    timeout_s: float = 20.0
    session_id: str = ""  # 非空 → 请求头带 X-Hermes-Session-Id
    extra: dict = field(default_factory=dict)

    @classmethod
    def from_env(cls, **overrides) -> "LlmConfig":
        cfg = cls(
            base_url=os.environ.get("LLM_BASE_URL", ""),
            api_key=os.environ.get("LLM_API_KEY", ""),
            model=os.environ.get("LLM_MODEL", ""),
            system=os.environ.get("LLM_SYSTEM_PROMPT", DEFAULT_SYSTEM),
            max_tokens=int(os.environ.get("LLM_MAX_TOKENS", "256")),
            temperature=float(os.environ.get("LLM_TEMPERATURE", "0.6")),
            timeout_s=float(os.environ.get("LLM_TIMEOUT_S", "20")),
            session_id=os.environ.get("LLM_SESSION_ID", "").strip(),
        )
        for key, value in overrides.items():
            if value is not None:
                setattr(cfg, key, value)
        return cfg

    def require_ready(self) -> None:
        """起请求前校验必需配置（地址/模型/token 都不落在文件里）。"""
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
            raise RuntimeError(
                "缺少 LLM 配置：" + "、".join(missing)
                + "。请先 export（地址/模型/token 不要提交进仓库），例如：\n"
                "  export LLM_BASE_URL=https://<你的网关>/v1\n"
                "  export LLM_MODEL=<模型名>\n"
                "  export LLM_API_KEY=sk-..."
            )

    @property
    def endpoint(self) -> str:
        base = self.base_url.rstrip("/")
        if base.endswith("/chat/completions"):
            return base
        return base + "/chat/completions"


def _request_body(messages: list[dict], cfg: LlmConfig, stream: bool) -> bytes:
    return json.dumps(
        {
            "model": cfg.model,
            "messages": messages,
            "stream": stream,
            "max_tokens": cfg.max_tokens,
            "temperature": cfg.temperature,
        },
        ensure_ascii=False,
    ).encode("utf-8")


def _build_request(messages: list[dict], cfg: LlmConfig, stream: bool) -> urllib.request.Request:
    cfg.require_ready()
    headers = {
        "Authorization": "Bearer " + cfg.api_key,
        "Content-Type": "application/json",
        "Accept": "text/event-stream" if stream else "application/json",
    }
    if cfg.session_id:  # 网关会话追踪（LLM_SESSION_ID，空则不带头）
        headers[SESSION_HEADER] = cfg.session_id
    return urllib.request.Request(
        cfg.endpoint,
        data=_request_body(messages, cfg, stream),
        headers=headers,
        method="POST",
    )


def _with_system(user_text: str, cfg: LlmConfig, history: list[dict] | None) -> list[dict]:
    messages: list[dict] = []
    if cfg.system:
        messages.append({"role": "system", "content": cfg.system})
    messages.extend(history or [])
    messages.append({"role": "user", "content": user_text})
    return messages


def stream_chat(user_text: str, cfg: LlmConfig, *, history: list[dict] | None = None):
    """流式对话：yield 文本增量（只含 delta.content，不含 reasoning_content）。

    阻塞式生成器（urllib 同步读），调用方应在独立线程里消费（mock 就是这么做的）。
    网关偶发 5xx（实测遇到 502）：**还没吐过任何内容**时自动重试（`LLM_RETRIES`，默认 2 次）；
    已经吐过内容就不再重试，避免回答重复。
    """
    messages = _with_system(user_text, cfg, history)
    attempts = max(1, int(os.environ.get("LLM_RETRIES", "2")))
    for attempt in range(1, attempts + 1):
        emitted = False
        try:
            for chunk in _stream_once(messages, cfg):
                emitted = True
                yield chunk
            return
        except RuntimeError as exc:
            if emitted or attempt >= attempts:
                raise
            time.sleep(0.4 * attempt)


def _stream_once(messages: list[dict], cfg: LlmConfig):
    request = _build_request(messages, cfg, stream=True)
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=cfg.timeout_s) as response:
            for raw in response:
                line = raw.decode("utf-8", "replace").strip()
                if not line or line.startswith(":"):
                    continue  # 空行 / SSE 注释（keep-alive）
                if not line.startswith("data:"):
                    continue
                payload = line[len("data:"):].strip()
                if payload in ("[DONE]", ""):
                    break
                try:
                    obj = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                if "error" in obj:
                    raise RuntimeError(f"LLM 返回错误：{obj['error']}")
                choices = obj.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta") or {}
                content = delta.get("content")
                if content:
                    yield content
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        raise RuntimeError(f"LLM HTTP {exc.code}：{detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"LLM 连接失败（{cfg.endpoint}）：{exc.reason}") from exc
    except TimeoutError as exc:
        raise RuntimeError(f"LLM 超时（{cfg.timeout_s}s，已等 {time.monotonic() - started:.1f}s）") from exc


def chat(user_text: str, cfg: LlmConfig, *, history: list[dict] | None = None) -> str:
    """非流式：拼完整答案返回。"""
    return "".join(stream_chat(user_text, cfg, history=history))
