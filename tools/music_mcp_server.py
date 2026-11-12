#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp>=2,<3"]
# ///
"""music_mcp_server —— 音乐能力的 MCP 服务（给 Hermes agent 当工具用）。

## 为什么是 MCP

2026-11-12 起音乐意图不再用关键词规则表（实测命中率低），改为 LLM 决策。当前 Hermes
网关的 `/chat/completions` 不返回工具调用，因此把音乐能力做成 **MCP 服务**：

  Hermes agent ──MCP(stdio/HTTP)──▶ 本服务（search/play/pause/resume/next/prev/stop）
                                        │
                                        └─ 只返回“要做什么”（track_id/标题，不含 URL）

真正的下行事件由**持有设备 WS 连接的那一侧**（mock / 真实云端）解析 Responses 的
`mcp_call` 后执行：`music_track`（带 URL）或 `music_control`。这样：
  - 端侧 ESP32 零改动（协议不变）；
  - URL 永远不出现在模型可见的内容里（防念链接 / 防幻觉地址）；
  - MCP 服务可独立跑（生产/调试），也可被 Hermes 拉起（stdio）。

## 启动方式

  # 1) stdio（Hermes 拉起；最常用）
  uv run tools/music_mcp_server.py --catalog tools/music_catalog.json

  # 2) streamable-http（本机调试 / 局域网）
  uv run tools/music_mcp_server.py --catalog tools/music_catalog.json \\
      --transport streamable-http --host 127.0.0.1 --port 8766

  # 3) 不接 Hermes，直接验证工具语义（离线，不需要 token）
  uv run tools/music_mcp_server.py --catalog tools/music_catalog.json --self-test

## 配到 Hermes（示例，按你的配置格式改）

  {
    "mcpServers": {
      "music": {
        "command": "uv",
        "args": ["run", "/abs/path/tools/music_mcp_server.py",
                 "--catalog", "/abs/path/tools/music_catalog.json"]
      }
    }
  }

HTTP 方式则是 `{"url": "http://127.0.0.1:8766/mcp"}`（取决于 Hermes 的 MCP 配置字段）。

## 注意

- **stdout 是 MCP 协议通道**：日志一律走 stderr；`--self-test` 才用 stdout。
- 工具结果只含 `track_id/title/artist/duration_ms`，**没有 url**（单测守着）。
- 搜索源（`--search-url`）可能走网络，工具内部用线程执行，不阻塞 MCP 事件循环。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Any

import anyio
from mcp.server.mcpserver import MCPServer

from mock_music import MusicCatalog, MusicService, MusicTrack
from music_tools import (
    TOOL_NEXT,
    TOOL_PAUSE,
    TOOL_PLAY,
    TOOL_PREV,
    TOOL_RESUME,
    TOOL_SEARCH,
    TOOL_SPECS,
    TOOL_STATUS,
    TOOL_STOP,
    MusicSession,
    execute_tool,
)

LOG = logging.getLogger("music.mcp")

SERVER_NAME = "music"
SERVER_INSTRUCTIONS = (
    "音乐播放工具。用户点歌、暂停/继续、下一首/上一首、停止时调用对应工具；"
    "工具返回 ok=false 时按 message 向用户解释，不要编造播放结果。"
)


# ============================================================================
# 服务装配
# ============================================================================


def build_service(catalog_path: str, music_url: str, music_title: str,
                  search_url: str, search_timeout_ms: int) -> MusicService:
    catalog = MusicCatalog()
    if catalog_path:
        catalog = MusicCatalog.from_json(catalog_path)
    if music_url:
        title = music_title or (Path(music_url.split("?")[0]).stem or "远程曲目")
        catalog.add(MusicTrack(title=title, url=music_url, duration_ms=0, keywords=(title,)))
    search = None
    if search_url:
        from mock_music import MusicSearchHttp
        search = MusicSearchHttp(search_url, timeout_s=max(0.2, search_timeout_ms / 1000))
    return MusicService(catalog=catalog, search=search)


def _json(result: Any) -> str:
    return json.dumps(result, ensure_ascii=False)


def make_server(session: MusicSession) -> MCPServer:
    """注册全部工具；工具函数可以同步也可以异步（MCP SDK 会放线程）。"""
    server = MCPServer(
        name=SERVER_NAME,
        instructions=SERVER_INSTRUCTIONS,
        version="2.0.0",
        log_level="WARNING",
    )
    lock = asyncio.Lock()

    async def run_tool(name: str, arguments: dict[str, Any]) -> str:
        async with lock:  # 单设备状态：并发调用串行化，避免队列/游标交叉
            try:
                command = await anyio.to_thread.run_sync(
                    lambda: execute_tool(session, name, arguments)
                )
            except Exception as exc:  # noqa: BLE001 - 工具内部错误不能拖垮 MCP 会话
                LOG.warning("工具 %s 执行失败：%s", name, exc)
                return _json({"ok": False, "action": name, "message": f"内部错误：{exc}"})
        if command.changed:
            LOG.info("tool=%s args=%s → %s（changed）", name, arguments, command.message)
        else:
            LOG.info("tool=%s args=%s → %s", name, arguments, command.message)
        return _json(command.to_result())

    @server.tool(name=TOOL_SEARCH, description=TOOL_SPECS[TOOL_SEARCH]["description"])
    async def music_search(query: str, limit: int = 5) -> str:
        return await run_tool(TOOL_SEARCH, {"query": query, "limit": limit})

    @server.tool(name=TOOL_PLAY, description=TOOL_SPECS[TOOL_PLAY]["description"])
    async def music_play(query: str = "", track_id: str = "", index: int | None = None) -> str:
        return await run_tool(TOOL_PLAY, {"query": query, "track_id": track_id, "index": index})

    @server.tool(name=TOOL_PAUSE, description=TOOL_SPECS[TOOL_PAUSE]["description"])
    async def music_pause() -> str:
        return await run_tool(TOOL_PAUSE, {})

    @server.tool(name=TOOL_RESUME, description=TOOL_SPECS[TOOL_RESUME]["description"])
    async def music_resume() -> str:
        return await run_tool(TOOL_RESUME, {})

    @server.tool(name=TOOL_NEXT, description=TOOL_SPECS[TOOL_NEXT]["description"])
    async def music_next() -> str:
        return await run_tool(TOOL_NEXT, {})

    @server.tool(name=TOOL_PREV, description=TOOL_SPECS[TOOL_PREV]["description"])
    async def music_prev() -> str:
        return await run_tool(TOOL_PREV, {})

    @server.tool(name=TOOL_STOP, description=TOOL_SPECS[TOOL_STOP]["description"])
    async def music_stop() -> str:
        return await run_tool(TOOL_STOP, {})

    @server.tool(name=TOOL_STATUS, description=TOOL_SPECS[TOOL_STATUS]["description"])
    async def music_status() -> str:
        return await run_tool(TOOL_STATUS, {})

    return server


# ============================================================================
# 自测（离线、不需要 Hermes）
# ============================================================================


def self_test(session: MusicSession) -> int:
    cases: list[tuple[str, dict[str, Any], str]] = [
        (TOOL_STATUS, {}, "idle"),
        (TOOL_PLAY, {"query": ""}, "playing"),
        (TOOL_PAUSE, {}, "paused"),
        (TOOL_PAUSE, {}, "paused"),
        (TOOL_RESUME, {}, "playing"),
        (TOOL_NEXT, {}, "playing"),
        (TOOL_PREV, {}, "playing"),
        (TOOL_STOP, {}, "idle"),
        (TOOL_STOP, {}, "idle"),
        (TOOL_RESUME, {}, "idle"),
    ]
    failed = 0
    print(f"===== music_mcp self-test（{len(cases)} 项）=====")
    for name, args, want_state in cases:
        command = execute_tool(session, name, args)
        payload = command.to_result()
        state_ok = payload["state"] == want_state
        no_url = "url" not in json.dumps(payload, ensure_ascii=False)
        ok = state_ok and no_url
        failed += 0 if ok else 1
        print(f"{'PASS' if ok else 'FAIL'} {name} {args} → state={payload['state']} "
              f"changed={payload['changed']} msg={payload['message']}")
    # track_id 往返：search → play(track_id)
    search = execute_tool(session, TOOL_SEARCH, {"query": ""})
    if search.items:
        tid = search.items[0]["track_id"]
        played = execute_tool(session, TOOL_PLAY, {"track_id": tid, "index": 0})
        ok = played.ok and played.track is not None and played.track.title
        failed += 0 if ok else 1
        print(f"{'PASS' if ok else 'FAIL'} track_id 往返 → {played.message}")
    print(f"\nMUSIC_MCP_SELF_TEST: {'PASS' if failed == 0 else 'FAIL'}（{len(cases) + 1 - failed}"
          f"/{len(cases) + 1} 项）")
    return 0 if failed == 0 else 1


# ============================================================================
# CLI
# ============================================================================


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="音乐 MCP 服务（search/play/pause/resume/next/prev/stop）")
    parser.add_argument("--catalog", default="", help="JSON 曲库（与 mock_ws_server --music-catalog 用同一份）")
    parser.add_argument("--music-url", default="", help="固定曲目 URL（没有曲库时至少给一首）")
    parser.add_argument("--music-title", default="", help="固定曲目的标题")
    parser.add_argument("--search-url", default="", help="搜索端点（{query} 占位），可选")
    parser.add_argument("--search-timeout-ms", type=int, default=3000)
    parser.add_argument("--transport", choices=("stdio", "streamable-http", "sse"),
                        default="stdio", help="stdio=Hermes 拉起；streamable-http=局域网调试")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--path", default="/mcp", help="streamable-http 的路径")
    parser.add_argument("--self-test", action="store_true", help="离线自测工具语义（不启动 MCP）")
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        stream=sys.stderr,  # stdout 留给 MCP 协议
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if not args.catalog and not args.music_url:
        print("需要 --catalog 或 --music-url（MCP 服务不生成示例音频）", file=sys.stderr)
        return 2
    service = build_service(args.catalog, args.music_url, args.music_title,
                            args.search_url, args.search_timeout_ms)
    session = MusicSession(service)
    LOG.info("曲库 %d 首%s", len(service.catalog),
             "、搜索源 1 个" if service.search is not None else "")
    if args.self_test:
        return self_test(session)

    server = make_server(session)
    if args.transport == "streamable-http":
        LOG.info("MCP listening: http://%s:%d%s", args.host, args.port, args.path)
        server.run("streamable-http", host=args.host, port=args.port,
                   streamable_http_path=args.path)
    elif args.transport == "sse":
        LOG.info("MCP listening (sse): http://%s:%d", args.host, args.port)
        server.run("sse", host=args.host, port=args.port)
    else:
        LOG.info("MCP stdio 就绪（等待 Hermes）")
        server.run("stdio")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
