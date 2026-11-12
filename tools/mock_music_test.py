#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["websockets>=14,<19"]
# ///
"""mock_music_test —— M1 音乐（mock 侧）离线自测，不接开发板、不需要 ASR/TTS 模型。

覆盖四层：
  A. 音乐工具：MusicSession 状态机 / 幂等 / track_id 往返 / 工具 schema
  B. 曲库：检索优先级、next/prev 游标、JSON 加载、URL 校验
  C. 内置 HTTP：200/206/404、Content-Type、示例曲目可解码（MP3 帧同步）
  D. 假设备 e2e（假 Responses 网关模拟模型决策）：真实 WS 协议跑一轮，断言 asr_final → music_track / music_control →
     固定确认语 TTS，并验证「未启用 --music 时不产生音乐事件」

跑法：
  uv run tools/mock_music_test.py
  uv run tools/mock_music_test.py --verbose
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlsplit
from pathlib import Path
from typing import Any

from websockets.asyncio.client import connect

from mock_cli import config_from_args, parse_args
from mock_core import Config, llm_env_ready
import llm_client
import music_tools
from fake_responses import FakeResponsesServer
import responses_client
from mock_music import (
    ACTION_NEXT,
    ACTION_PAUSE,
    ACTION_PLAY,
    ACTION_PREV,
    ACTION_RESUME,
    ACTION_STOP,
    MusicCatalog,
    MusicHttpServer,
    MusicSearchHttp,
    MusicService,
    MusicTrack,
    build_control_event,
)
from mock_server import MockServer


class Checker:
    """极简断言器：逐条计数，最后统一汇总（与仓库其它脚本口径一致）。"""

    def __init__(self, verbose: bool = False) -> None:
        self.verbose = verbose
        self.passed = 0
        self.failures: list[str] = []
        self.section_name = ""

    def section(self, title: str) -> None:
        self.section_name = title
        print(f"\n--- {title} ---", flush=True)

    def ok(self, cond: bool, name: str, detail: str = "") -> bool:
        if cond:
            self.passed += 1
            if self.verbose:
                print(f"  PASS {name}", flush=True)
        else:
            label = f"[{self.section_name}] {name}" + (f" | {detail}" if detail else "")
            self.failures.append(label)
            print(f"  FAIL {label}", flush=True)
        return cond

    def eq(self, got: Any, want: Any, name: str) -> bool:
        return self.ok(got == want, name, f"got={got!r} want={want!r}")

    def summary(self) -> bool:
        total = self.passed + len(self.failures)
        print(f"\n{'=' * 60}")
        if self.failures:
            print(f"FAIL：{len(self.failures)}/{total} 项失败")
            for item in self.failures:
                print(f"  - {item}")
        else:
            print(f"PASS：{self.passed}/{total} 项全部通过")
        print("=" * 60, flush=True)
        return not self.failures


# ============================================================================
# A. 音乐工具（LLM tool calling 的唯一执行层）
# ============================================================================


def test_tools(check: Checker) -> None:
    check.section("A. 音乐工具")
    service = MusicService(catalog=MusicCatalog(_tracks()))
    session = music_tools.MusicSession(service)

    check.eq(music_tools.execute_tool(session, "music_status", {}).state, "idle", "初始状态 idle")
    check.ok(not music_tools.execute_tool(session, "music_pause", {}).ok,
             "空闲时 pause → ok=false（不编造事件）")

    command = music_tools.execute_tool(session, "music_play", {"query": "晴天"})
    check.ok(command.ok and command.changed, "play 命中并改变状态")
    check.eq(command.state, "playing", "play 后 playing")
    check.eq(command.track.title if command.track else None, "晴天", "play 选中《晴天》")
    payload = json.dumps(command.to_result(), ensure_ascii=False)
    check.ok("url" not in payload, "工具结果不含 URL（模型看不到链接）")
    check.ok("track_id" in payload, "工具结果含 track_id")

    command = music_tools.execute_tool(session, "music_play", {"query": "晴天"})
    check.ok(command.ok and not command.changed, "同一首重复 play → changed=false")

    command = music_tools.execute_tool(session, "music_pause", {})
    check.ok(command.changed and command.state == "paused", "pause → paused")
    command = music_tools.execute_tool(session, "music_pause", {})
    check.ok(command.ok and not command.changed, "重复 pause 幂等（不下发事件）")
    command = music_tools.execute_tool(session, "music_resume", {})
    check.ok(command.changed and command.state == "playing", "resume → playing")

    command = music_tools.execute_tool(session, "music_next", {})
    check.ok(command.changed and command.track is not None, "next 切歌")
    next_title = command.track.title if command.track else ""
    command = music_tools.execute_tool(session, "music_prev", {})
    check.eq(command.track.title if command.track else None, "晴天", "prev 回到上一首")
    check.ok(next_title != "晴天", "next 确实换了曲目")

    command = music_tools.execute_tool(session, "music_stop", {})
    check.ok(command.changed and command.state == "idle", "stop → idle")
    command = music_tools.execute_tool(session, "music_stop", {})
    check.ok(command.ok and not command.changed, "重复 stop 幂等")
    check.ok(not music_tools.execute_tool(session, "music_resume", {}).ok,
             "空闲时 resume → ok=false（应改用 play）")

    command = music_tools.execute_tool(session, "play", {"query": "孤勇者"})
    check.ok(command.ok and command.track.title == "孤勇者", "play 短名兼容")
    command = music_tools.execute_tool(session, "music.play", {"query": "稻香"})
    check.ok(command.ok and command.track.title == "稻香", "music.play 兼容")
    check.ok(not music_tools.execute_tool(session, "music_nope", {}).ok, "未知工具 → ok=false")

    # search → track_id → play
    session = music_tools.MusicSession(service)
    command = music_tools.execute_tool(session, "music_search", {"query": "周杰伦", "limit": 5})
    check.ok(command.ok and len(command.items) >= 1, "search 返回候选")
    track_id = command.items[0]["track_id"] if command.items else ""
    command = music_tools.execute_tool(session, "music_play", {"track_id": track_id})
    check.ok(command.ok and command.track is not None, "track_id 可回放")
    check.ok(not music_tools.execute_tool(session, "music_play", {"track_id": "deadbeef"}).ok,
             "未知 track_id → ok=false")

    # 旧搜索结果不能污染后续点歌：新查询清空队列，“下一首”回到曲库游标
    session = music_tools.MusicSession(service)
    session.queue = list(service.catalog.tracks[:2])
    session.index = 0
    command = music_tools.execute_tool(session, "music_play", {"query": "孤勇者"})
    check.ok(command.ok and not session.queue, "新查询清空旧候选队列")

    prompt = music_tools.system_prompt_for(session)
    check.ok('"state"' in prompt and "music_play" in prompt, "system prompt 注入状态/规则")
    check.ok("http" not in prompt, "system prompt 不含 URL")

    # 设备上报的 music_status 同步到 mock 会话（语音 auto-pause 不经过工具调用）
    session = music_tools.MusicSession(service)
    music_tools.execute_tool(session, "music_play", {"query": "晴天"})
    session.apply_status("paused")
    check.eq(session.state, "paused", "apply_status(paused) 同步设备暂停态")
    command = music_tools.execute_tool(session, "music_resume", {})
    check.ok(command.ok and command.changed and session.state == "playing",
             "同步后 resume 立即生效")
    session.apply_status("stopped")
    check.ok(session.state == "idle" and session.current is None,
             "apply_status(stopped) 清空当前曲目")
    command = music_tools.execute_tool(music_tools.MusicSession(MusicService(catalog=MusicCatalog())),
                                       "music_resume", {}, lenient=True)
    check.ok(command.ok and command.changed, "lenient resume（MCP 无状态）返回已请求")

    # 工具结果自带 URL：空曲库也能起播（mock 不再依赖 --music-catalog）
    url = "http://192.168.1.10:9009/media/remote.mp3"
    payload = {
        "ok": True, "action": "play", "changed": False, "state": "playing",
        "matched": True, "message": "远程工具已执行",
        "track": {"track_id": "remote01", "title": "远程曲目", "artist": "测试",
                  "duration_ms": 0, "url": url},
    }
    bare = music_tools.MusicSession(MusicService(catalog=MusicCatalog()))
    command = bare.apply_remote(payload)
    check.ok(command is not None and command.ok and command.changed,
             "空曲库也能按工具结果 URL 起播")
    check.eq(command.track.url if command and command.track else None, url, "URL 来自工具结果")
    again = bare.apply_remote(payload)
    check.ok(again is not None and again.ok and not again.changed,
             "同 URL 重复 apply → changed=false")
    bad = dict(payload, track=dict(payload["track"], url="file:///etc/passwd"))
    check.ok(bare.apply_remote(bad) is None, "非 http(s) URL 被拒绝")

    definitions = music_tools.tool_definitions()
    names = {item["name"] for item in definitions}
    check.ok({"music_search", "music_play", "music_pause", "music_resume",
              "music_next", "music_prev", "music_stop"} <= names,
             "inline tools 定义齐全")
    check.ok(all(item["type"] == "function" for item in definitions), "tool 定义形态正确")


# ============================================================================
# B. 曲库
# ============================================================================


def _tracks() -> list[MusicTrack]:
    return [
        MusicTrack("晴天", "http://192.168.1.10:9000/qingtian.mp3", 269000, "周杰伦",
                   ("周杰伦", "晴天")),
        MusicTrack("稻香", "http://192.168.1.10:9000/daoxiang.mp3", 223000, "周杰伦",
                   ("周杰伦", "稻香")),
        MusicTrack("孤勇者", "http://192.168.1.10:9000/guyongzhe.mp3", 243000, "陈奕迅",
                   ("陈奕迅", "孤勇者")),
    ]


def test_catalog(check: Checker, tmpdir: Path) -> None:
    check.section("B. 曲库")
    catalog = MusicCatalog(_tracks())
    check.eq(len(catalog), 3, "曲库加载 3 首")

    track, matched = catalog.resolve("晴天")
    check.eq(track.title if track else None, "晴天", "标题精确命中")
    check.ok(matched, "精确命中标记 matched=True")

    track, _ = catalog.resolve("周杰伦的稻香")
    check.eq(track.title if track else None, "稻香", "关键词命中（歌手+歌名）")

    track, _ = catalog.resolve("孤勇")
    check.eq(track.title if track else None, "孤勇者", "标题部分命中")

    track, matched = catalog.resolve("完全不存在的歌")
    check.ok(track is not None and not matched, "未命中 → 回退当前曲目且 matched=False")

    first = catalog.next_track()
    second = catalog.next_track()
    check.ok(first is not None and second is not None and first.title != second.title,
             "next 切到下一首")
    back = catalog.prev_track()
    check.eq(back.title if back else None, first.title if first else None, "prev 回到上一首")

    empty = MusicCatalog()
    check.eq(empty.resolve("晴天"), (None, False), "空曲库 resolve → (None, False)")
    check.ok(empty.next_track() is None, "空曲库 next → None")

    path = tmpdir / "catalog.json"
    path.write_text(json.dumps({"tracks": [
        {"title": "晴天", "artist": "周杰伦", "url": "http://x/y.mp3", "duration_ms": 1000,
         "keywords": ["周杰伦"]},
        {"title": "稻香", "url": "http://x/z.mp3"},
    ]}, ensure_ascii=False), encoding="utf-8")
    loaded = MusicCatalog.from_json(path)
    check.eq(len(loaded), 2, "JSON 曲库加载")
    check.eq(loaded.tracks[1].artist, "", "JSON 缺省 artist 为空")

    bad = tmpdir / "bad.json"
    bad.write_text(json.dumps({"tracks": [{"title": "x", "url": "file:///x.mp3"}]}),
                   encoding="utf-8")
    try:
        MusicCatalog.from_json(bad)
        check.ok(False, "file:// URL 必须被拒绝")
    except ValueError:
        check.ok(True, "file:// URL 被拒绝")

    radio = MusicTrack("清听电台", "http://lazy.example/live/64k.mp3", 0, "电台", ("电台",))
    radio_catalog = MusicCatalog([radio])
    track, matched = radio_catalog.resolve("放电台")
    check.eq(track.title if track else None, "清听电台", "直播流按关键词命中")
    check.ok(matched, "直播流 matched=True")
    event = radio.as_event(9)
    check.eq(event.get("duration_ms"), 0, "直播流 duration_ms=0（端侧按未知/无线处理）")
    check.eq(event.get("mid"), 9, "直播流事件带 mid")

    err = build_control_event(3, ACTION_PAUSE)
    check.eq(err, {"t": "music_control", "mid": 3, "action": "pause"}, "music_control 事件结构")
    try:
        build_control_event(3, ACTION_NEXT)
        check.ok(False, "music_control 不允许 next（next 应发新 music_track）")
    except ValueError:
        check.ok(True, "music_control 拒绝 next")


# ============================================================================
# B2. 通用搜索适配器（--music-search-url）
# ============================================================================


class _SearchHandler(BaseHTTPRequestHandler):
    """假的"本机搜索服务"：按 q 返回不同结果，覆盖正常/空/坏 URL/500/慢。"""

    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlsplit(self.path)
        query = (parse_qs(parsed.query).get("q") or [""])[0]
        if parsed.path == "/search":
            body = {"tracks": [
                {
                    "title": f"搜索结果:{query}",
                    "artist": "测试歌手",
                    "url": f"http://music.example/{quote(query)}.mp3",
                    "duration_ms": 123000,
                    "keywords": [query],
                },
                {
                    "title": f"另一首:{query}",
                    "artist": "测试歌手",
                    "url": f"http://music.example/{quote(query)}-2.mp3",
                    "duration_ms": 123000,
                    "keywords": [query],
                },
            ]}
        elif parsed.path == "/empty":
            body = {"tracks": []}
        elif parsed.path == "/nourl":
            body = {"tracks": [{"title": "x", "url": "file:///x.mp3"}]}
        elif parsed.path == "/slow":
            time.sleep(1.0)
            body = {"tracks": []}
        else:
            self.send_response(500)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        pass


class _SearchServer:
    def __init__(self) -> None:
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _SearchHandler)
        self.httpd.daemon_threads = True
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.httpd.server_address[1]

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


def test_search_http(check: Checker) -> None:
    check.section("B2. 通用搜索适配器")
    server = _SearchServer()
    try:
        search = MusicSearchHttp(server.base + "/search?q={query}", timeout_s=2)
        track = search.search("晴天")
        check.eq(track.title if track else None, "搜索结果:晴天", "搜索命中标题")
        check.ok(track.url.startswith("http://") if track else False, "搜索命中 URL 合法")
        check.eq(track.duration_ms if track else None, 123000, "搜索命中时长")
        check.eq(track.artist if track else None, "测试歌手", "搜索命中歌手")
        all_tracks = search.search_tracks("晴天", limit=5)
        check.eq(len(all_tracks), 2, "search_tracks 返回多条（next/prev 用）")

        # 中文/特殊字符必须 URL 编码后交给服务端
        track = search.search("a b&c")
        check.eq(track.title if track else None, "搜索结果:a b&c", "query 正确 URL 编码")

        # 缺省占位符时自动补 ?q=
        auto = MusicSearchHttp(server.base + "/search", timeout_s=2)
        check.ok(auto._url("晴天").endswith("q=%E6%99%B4%E5%A4%A9"), "缺省补 ?q= 并编码")

        check.ok(search.search("x").url.startswith("http://music.example/"), "搜索结果 URL 透传")
        empty = MusicSearchHttp(server.base + "/empty?q={query}")
        check.eq(empty.search_tracks("x"), [], "空 results 数组 → 空列表（不误判格式）")
        check.ok(empty.search("x") is None, "空结果 → None")
        check.ok(MusicSearchHttp(server.base + "/nourl?q={query}").search("x") is None,
                 "只有 file:// 结果 → None")
        check.ok(MusicSearchHttp(server.base + "/fail?q={query}").search("x") is None,
                 "500 → None")
        check.ok(MusicSearchHttp(server.base + "/slow?q={query}", timeout_s=0.2).search("x") is None,
                 "超时 → None")

        # 服务层：先搜、失败回退曲库
        cfg = Config(music=True, music_search_url=server.base + "/search?q={query}")
        service = MusicService.from_config(cfg)
        check.ok(service is not None and service.search is not None, "Config 搜索源 → MusicService")
        check.ok(service is not None and not service._need_demo, "有搜索源时不再起示例 HTTP")
        track, matched = service.resolve_query("晴天") if service else (None, False)
        check.eq(track.title if track else None, "搜索结果:晴天", "resolve_query 走搜索源")
        check.ok(matched, "搜索命中 matched=True")

        fallback = MusicService.from_config(Config(
            music=True,
            music_search_url=server.base + "/fail?q={query}",
            music_url="http://music.example/fallback.mp3",
        ))
        track, _ = fallback.resolve_query("晴天") if fallback else (None, False)
        check.eq(track.url if track else None, "http://music.example/fallback.mp3",
                 "搜索失败回退本地曲库")

        for argv in (
            ["--music-search-url", server.base + "/search"],       # 缺 --music
            ["--music", "--music-search-url", "ftp://x/y"],         # 非 http(s)
            ["--music", "--music-search-timeout-ms", "50"],         # 超时越界
        ):
            try:
                config_from_args(parse_args(argv))
                check.ok(False, f"应拒绝参数 {argv}")
            except SystemExit:
                check.ok(True, f"拒绝参数 {argv}")
    finally:
        server.stop()


async def test_e2e_search(check: Checker) -> None:
    """搜索源接入后，WS 上仍只下发普通 music_track（端侧无感知）。"""
    check.section("D2. 搜索源 e2e")
    search_server = _SearchServer()
    responses = FakeResponsesServer(_decide_search)
    cfg = Config(host="127.0.0.1", port=0, music=True, no_tts=True, think_ms=0, quiet=True,
                 tts_text="放首晴天", music_search_url=search_server.base + "/search?q={query}",
                 llm_base_url=responses.base, llm_api_key="test", llm_model="fake")
    server = MockServer(cfg)
    ws = None
    try:
        ws, _ = await _open(server)
        msgs = await _collect_round(ws, 1)
        track_event = _find(msgs, "music_track")
        check.ok(track_event is not None, "搜索源命中 → music_track")
        if track_event:
            check.eq(track_event.get("title"), "搜索结果:晴天", "music_track.title 来自搜索源")
            check.ok(str(track_event.get("url", "")).startswith("http://music.example/"),
                     "music_track.url 来自搜索源")

        cfg.tts_text = "下一首"
        msgs = await _collect_round(ws, 2)
        track_event = _find(msgs, "music_track")
        check.eq(track_event.get("title") if track_event else None, "另一首:晴天",
                 "下一首 → 搜索结果第二条")
        check.eq(track_event.get("mid") if track_event else None, 2, "下一首 mid=2")

        cfg.tts_text = "上一首"
        msgs = await _collect_round(ws, 3)
        track_event = _find(msgs, "music_track")
        check.eq(track_event.get("title") if track_event else None, "搜索结果:晴天",
                 "上一首 → 回到第一条")
        check.eq(track_event.get("mid") if track_event else None, 3, "上一首 mid=3")
        check.ok(_find(msgs, "error") is None, "搜索链路无 error")
    finally:
        if ws is not None:
            await ws.close()
        await server.stop()
        search_server.stop()
        responses.stop()


# ============================================================================
# B3. 假 Responses 网关（模拟“模型决定调哪个音乐工具”；实现见 fake_responses.py）
# ============================================================================


def _decide_e2e(text: str) -> tuple[str, dict[str, Any]] | None:
    """测试用“模型”：把整句映射成工具调用（模拟 LLM 决策，不参与产品路径）。"""
    if "暂停" in text:
        return "music_pause", {}
    if "继续" in text:
        return "music_resume", {}
    if "下一首" in text:
        return "music_next", {}
    if "上一首" in text:
        return "music_prev", {}
    if "停止" in text or "别放" in text:
        return "music_stop", {}
    if "晴天" in text:
        return "music_play", {"query": "晴天"}
    return None


def _decide_search(text: str) -> tuple[str, dict[str, Any]] | None:
    if "下一首" in text:
        return "music_next", {}
    if "上一首" in text:
        return "music_prev", {}
    if "晴天" in text:
        return "music_play", {"query": "晴天"}
    return None


# ============================================================================
# B4. Responses 解析 / inline 工具链
# ============================================================================


def test_responses_parse(check: Checker) -> None:
    check.section("B4. Responses 解析")
    cfg = responses_client.ResponsesConfig(base_url="https://gw.example/v1", api_key="k",
                                           model="m")
    check.eq(cfg.endpoint, "https://gw.example/v1/responses", "endpoint 自动补 /responses")

    result = responses_client.parse_response({
        "id": "resp_1",
        "output": [
            {"type": "function_call", "name": "music_play", "call_id": "c1",
             "arguments": '{"query":"晴天"}'},
            {"type": "mcp_call", "id": "mcp1", "name": "music_next",
             "arguments": "{}", "output": '{"ok":true}'},
            {"type": "message", "role": "assistant",
             "content": [{"type": "output_text", "text": "好的"}]},
        ],
    })
    check.eq(len(result.tool_calls), 2, "解析出两条工具调用")
    check.eq(result.tool_calls[0].name, "music_play", "function_call 名字")
    check.eq(result.tool_calls[0].arguments, {"query": "晴天"}, "function_call 参数 JSON")
    check.eq(result.tool_calls[0].source, "function", "来源 function")
    check.eq(result.tool_calls[1].source, "mcp", "来源 mcp")
    check.eq(result.text, "好的", "message 文本")

    # 有些网关只在 output 里回工具结果：从 output 还原参数（优先 track_id）
    result = responses_client.parse_response({
        "output": [{"type": "mcp_call", "name": "music_play",
                    "output": '{"ok":true,"action":"play","track":{"track_id":"abcd1234","title":"晴天"}}'}],
    })
    check.eq(result.tool_calls[0].arguments.get("track_id"), "abcd1234",
             "output 兜底还原 track_id")

    # Hermes 的 function_call_output：工具结果（含 URL）单独一项，按 call_id 回填
    inner = {"ok": True, "action": "play", "changed": False, "state": "playing",
             "matched": True, "message": "开始播放《主角》",
             "track": {"track_id": "6cec2605", "title": "主角", "artist": "王菲",
                       "duration_ms": 0, "url": "http://192.168.1.10:9009/z.mp3"}}
    wrapped = ('<untrusted_tool_result source="mcp__music__music_play">\n'
               + json.dumps({"result": json.dumps(inner, ensure_ascii=False)},
                            ensure_ascii=False)
               + '\n</untrusted_tool_result>')
    parsed = responses_client.parse_tool_output(wrapped)
    check.eq(parsed.get("track", {}).get("url") if parsed else None,
             "http://192.168.1.10:9009/z.mp3", "解析 Hermes 工具结果里的 URL")

    result = responses_client.parse_response({"output": [
        {"type": "function_call", "call_id": "c9", "name": "tool_call",
         "arguments": '{"calls":[{"name":"mcp__music__music_play","arguments":{}}]}'},
        {"type": "function_call_output", "call_id": "c9", "output": wrapped},
    ]})
    check.ok(result.tool_calls and result.tool_calls[0].output == wrapped,
             "function_call_output 按 call_id 贴回调用")

    # Hermes 真机形态：tool_describe + tool_call{calls:[mcp__music__music_play]}
    result = responses_client.parse_response({
        "output": [
            {"type": "mcp_call", "name": "tool_describe",
             "arguments": '{"names":["mcp__music__music_search","mcp__music__music_play"]}'},
            {"type": "mcp_call", "name": "tool_call",
             "arguments": '{"calls":[{"name":"mcp__music__music_search",'
                          '"arguments":{"query":"7686","limit":5}}]}'},
            {"type": "mcp_call", "name": "tool_call",
             "arguments": '{"calls":[{"name":"mcp__music__music_play",'
                          '"arguments":{"track_id":"6cec2605"}}]}'},
            {"type": "message", "content": [{"type": "output_text", "text": "好了"}]},
        ],
    })
    check.eq([c.name for c in result.tool_calls], ["music_search", "music_play"],
             "Hermes 元工具展开 + 剥 mcp__ 前缀")
    check.eq(result.tool_calls[1].arguments.get("track_id"), "6cec2605",
             "Hermes 内层参数解析")
    check.eq(result.text, "好了", "Hermes 最终回答")

    # chat 风格兜底
    result = responses_client.parse_response({
        "choices": [{"message": {"content": "天气不错",
                                 "tool_calls": [{"id": "c9", "function": {
                                     "name": "music_stop", "arguments": "{}"}}]}}],
    })
    check.eq(result.text, "天气不错", "chat 兜底文本")
    check.eq(result.tool_calls[0].name, "music_stop", "chat 兜底 tool_calls")

    missing = responses_client.ResponsesConfig(base_url="", api_key="", model="")
    try:
        missing.require_ready()
        check.ok(False, "缺配置必须报错")
    except responses_client.ResponsesError:
        check.ok(True, "缺配置报 ResponsesError")


async def test_inline_tools(check: Checker) -> None:
    """inline 模式：把 function tools 发给网关，解析 function_call 后本地执行。"""
    check.section("B5. inline function tools")
    responses = FakeResponsesServer(_decide_e2e, style="function")
    cfg = Config(host="127.0.0.1", port=0, music=True, music_tool_mode="inline",
                 no_tts=True, think_ms=0, quiet=True, tts_text="放首晴天",
                 llm_base_url=responses.base, llm_api_key="test", llm_model="fake")
    server = MockServer(cfg)
    ws = None
    try:
        ws, _ = await _open(server)
        msgs = await _collect_round(ws, 1)
        check.ok(_find(msgs, "music_track") is not None, "inline function_call → music_track")
        check.ok(len(responses.requests) == 1, "调用一次 Responses")
        request = responses.requests[0]
        check.ok(isinstance(request.get("tools"), list), "inline 模式带 tools 字段")
        names = {item.get("name") for item in request.get("tools", [])}
        check.ok("music_play" in names, "tools 里包含 music_play")
        check.eq(request.get("tool_choice"), "auto", "tool_choice=auto")
    finally:
        if ws is not None:
            await ws.close()
        await server.stop()
        responses.stop()


def test_session_tracking(check: Checker) -> None:
    """LLM_SESSION_ID → 请求头 X-Hermes-Session-Id + body.conversation（两者并存）。"""
    check.section("F. 会话追踪")
    cfg = responses_client.ResponsesConfig(base_url="https://gw.example/v1", api_key="k",
                                           model="m", session_id="sess-123")
    body = json.loads(responses_client._request_body(
        "你好", cfg, tools=None, system="", history=None, max_output_tokens=32))
    check.eq(body.get("conversation"), "sess-123", "Responses body 带 conversation")
    check.eq(body.get("model"), "m", "body 仍带 model")

    empty = responses_client.ResponsesConfig(base_url="https://gw.example/v1", api_key="k",
                                            model="m")
    body = json.loads(responses_client._request_body(
        "你好", empty, tools=None, system="", history=None, max_output_tokens=32))
    check.ok("conversation" not in body, "未配置 LLM_SESSION_ID 时不带 conversation")

    lcfg = llm_client.LlmConfig(base_url="https://gw.example/v1", api_key="k", model="m",
                                session_id="sess-123")
    body = json.loads(llm_client._request_body(
        [{"role": "user", "content": "你好"}], lcfg, False))
    check.eq(body.get("conversation"), "sess-123", "chat body 也带 conversation")


# ============================================================================
# C. 内置 HTTP
# ============================================================================


def test_http(check: Checker) -> None:
    check.section("C. 内置 HTTP")
    body = b"\xff\xfb\x90\x64" + bytes(range(256)) * 4
    server = MusicHttpServer({"/media/test.mp3": body}, host="127.0.0.1", port=0)
    port = server.start()
    try:
        url = f"http://127.0.0.1:{port}/media/test.mp3"
        with urllib.request.urlopen(url, timeout=5) as resp:
            data = resp.read()
            check.eq(resp.status, 200, "GET 200")
            check.eq(resp.headers.get("Content-Type"), "audio/mpeg", "Content-Type=audio/mpeg")
            check.eq(data, body, "GET 内容一致")
            check.eq(resp.headers.get("Accept-Ranges"), "bytes", "Accept-Ranges=bytes")

        req = urllib.request.Request(url, headers={"Range": "bytes=0-99"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            chunk = resp.read()
            check.eq(resp.status, 206, "Range 206")
            check.eq(len(chunk), 100, "Range 长度 100")
            check.eq(resp.headers.get("Content-Range"),
                     f"bytes 0-99/{len(body)}", "Content-Range 正确")

        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/nope.mp3", timeout=5)
            check.ok(False, "未知路径 404")
        except urllib.error.HTTPError as exc:
            check.eq(exc.code, 404, "未知路径 404")
    finally:
        server.stop()

    # 示例曲目：真机可播（MP3 帧同步）+ 时长足够起播验证
    demo = MusicService(catalog=MusicCatalog(), need_demo=True)
    demo.start("127.0.0.1", bind_host="127.0.0.1")
    try:
        check.ok(demo.demo_url.startswith("http://127.0.0.1:"), "示例曲目 URL 可访问")
        with urllib.request.urlopen(demo.demo_url, timeout=5) as resp:
            data = resp.read()
        check.ok(data[:2] == b"\xff\xfb" or (data[0] == 0xFF and data[1] & 0xE0 == 0xE0),
                 "示例曲目以 MP3 帧同步开头")
        track = demo.catalog.tracks[0]
        check.ok(track.duration_ms >= 5000, "示例曲目时长 ≥ 5s（够真机起播）")
        check.eq(track.url, demo.demo_url, "示例曲目 URL 写入曲库")
    finally:
        demo.stop()


# ============================================================================
# D. 假设备 e2e（真实 WS 协议）
# ============================================================================


async def _collect_round(ws: Any, sid: int, timeout: float = 5.0) -> list[dict[str, Any]]:
    """发一轮 audio_start/audio_end，收到 tts_end 为止的所有 JSON（二进制帧忽略）。"""
    await ws.send(json.dumps({"t": "audio_start", "sid": sid}))
    await ws.send(json.dumps({"t": "audio_end", "sid": sid}))
    messages: list[dict[str, Any]] = []
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"sid={sid} 等待 tts_end 超时（已收 {len(messages)} 条）")
        raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
        if isinstance(raw, (bytes, bytearray)):
            continue
        msg = json.loads(str(raw))
        messages.append(msg)
        if msg.get("t") == "tts_end":
            return messages


def _find(messages: list[dict[str, Any]], event: str) -> dict[str, Any] | None:
    return next((m for m in messages if m.get("t") == event), None)


async def _open(server: MockServer) -> tuple[Any, list[dict[str, Any]]]:
    port = await server.start()
    ws = await connect(
        f"ws://127.0.0.1:{port}/v1/voice?device_id=dev1&token=t&ver=1",
        max_size=64 * 1024,
    )
    await ws.send(json.dumps({"t": "hello", "device_id": "dev1", "ver": 1}))
    ack = json.loads(str(await asyncio.wait_for(ws.recv(), timeout=5)))
    return ws, [ack]


async def test_e2e(check: Checker) -> None:
    check.section("D. 假设备 e2e")
    responses = FakeResponsesServer(_decide_e2e)
    cfg = Config(host="127.0.0.1", port=0, music=True, no_tts=True, think_ms=0,
                 quiet=True, max_tts_frames=0,
                 llm_base_url=responses.base, llm_api_key="test", llm_model="fake")
    server = MockServer(cfg)
    ws = None
    try:
        ws, first = await _open(server)
        check.eq(first[0].get("t"), "hello_ack", "hello_ack")

        # 第 1 轮：点歌 → music_track（含真实可播 URL）
        cfg.tts_text = "放首周杰伦的晴天"
        msgs = await _collect_round(ws, 1)
        check.eq(_find(msgs, "asr_final")["text"], "放首周杰伦的晴天", "asr_final 回填识别文本")
        track_event = _find(msgs, "music_track")
        check.ok(track_event is not None, "点歌 → 下发 music_track")
        if track_event:
            check.eq(track_event.get("mid"), 1, "music_track mid=1")
            check.eq(track_event.get("codec"), "mp3", "music_track codec=mp3")
            check.ok(str(track_event.get("url", "")).startswith("http://"), "music_track url 可播")
            check.eq(track_event.get("title"), "mock 示例曲目", "music_track 带标题")
            check.ok(track_event.get("duration_ms", 0) >= 5000, "music_track duration_ms 合理")
        check.ok(_find(msgs, "tts_start") is not None, "音乐轮仍有 tts_start（确认语）")
        check.ok(_find(msgs, "tts_end") is not None, "音乐轮 tts_end 收尾")

        # 第 2 轮：暂停 → music_control（不带 URL）
        cfg.tts_text = "暂停"
        msgs = await _collect_round(ws, 2)
        control = _find(msgs, "music_control")
        check.ok(control is not None, "暂停 → 下发 music_control")
        if control:
            check.eq(control.get("action"), "pause", "music_control action=pause")
            check.eq(control.get("mid"), 1, "控制事件沿用当前 mid（不加 1）")
            check.eq(control.get("url"), None, "music_control 不带 url")
        check.ok(_find(msgs, "music_track") is None, "暂停不重复下发 music_track")

        # 第 3 轮：继续 → music_control resume
        cfg.tts_text = "继续播放"
        msgs = await _collect_round(ws, 3)
        control = _find(msgs, "music_control")
        check.eq(control.get("action") if control else None, "resume", "继续 → action=resume")

        # 第 4 轮：下一首 → 新的 music_track（端侧无播放列表）
        cfg.tts_text = "下一首"
        msgs = await _collect_round(ws, 4)
        track_event = _find(msgs, "music_track")
        check.ok(track_event is not None, "下一首 → 新 music_track")
        if track_event:
            check.eq(track_event.get("mid"), 2, "下一首 mid=2")

        # 第 5 轮：普通聊天 → 不得产生音乐事件
        cfg.tts_text = "今天天气如何"
        msgs = await _collect_round(ws, 5)
        check.ok(_find(msgs, "music_track") is None, "普通聊天不下发 music_track")
        check.ok(_find(msgs, "music_control") is None, "普通聊天不下发 music_control")
        check.ok(_find(msgs, "llm_delta") is not None, "普通聊天仍有 llm_delta（模型回答）")

        # 第 6 轮：停止 → music_control stop
        cfg.tts_text = "停止播放"
        msgs = await _collect_round(ws, 6)
        control = _find(msgs, "music_control")
        check.eq(control.get("action") if control else None, "stop", "停止 → action=stop")

        # 端侧主动 music_request（T4.3 起用）→ 也回真实曲目
        await ws.send(json.dumps({"t": "music_request", "action": "play", "index": 0}))
        deadline = time.monotonic() + 3
        got_track = None
        while time.monotonic() < deadline:
            raw = await asyncio.wait_for(ws.recv(), timeout=deadline - time.monotonic())
            if isinstance(raw, (bytes, bytearray)):
                continue
            msg = json.loads(str(raw))
            if msg.get("t") == "music_track":
                got_track = msg
                break
        check.ok(got_track is not None, "music_request → music_track")
        if got_track:
            check.eq(got_track.get("mid"), 3, "music_request 的 mid 继续自增")

        session = next(iter(server.active.values()))
        check.eq(session.stats.music_intents, 5,
                 "统计：5 次工具调用（点歌/暂停/继续/下一首/停止）")
        check.eq(session.stats.music_tracks, 3, "统计：3 条 music_track（点歌/下一首/music_request）")
        check.eq(session.stats.music_controls, 3, "统计：3 条 music_control（暂停/继续/停止）")
        check.ok(len(responses.requests) >= 5, "每轮都走了 Responses API")
        check.ok(all("tools" not in req for req in responses.requests),
                 "mcp 模式不向网关传 tools 字段")
    finally:
        if ws is not None:
            await ws.close()
        await server.stop()
        responses.stop()


async def test_hermes_tool_shape(check: Checker) -> None:
    """Hermes 实际返回形态（tool_describe + tool_call + mcp__music__ 前缀）也要能驱动设备。"""
    check.section("D4. Hermes 工具形态")
    responses = FakeResponsesServer(_decide_e2e, style="hermes")
    cfg = Config(host="127.0.0.1", port=0, music=True, no_tts=True, think_ms=0, quiet=True,
                 tts_text="放首晴天",
                 llm_base_url=responses.base, llm_api_key="test", llm_model="fake")
    server = MockServer(cfg)
    ws = None
    try:
        ws, _ = await _open(server)
        msgs = await _collect_round(ws, 1)
        track_event = _find(msgs, "music_track")
        check.ok(track_event is not None, "元工具包装 → 仍下发 music_track")
        check.eq(track_event.get("mid") if track_event else None, 1, "music_track mid=1")
        session = next(iter(server.active.values()))
        check.eq(session.stats.music_intents, 1, "tool_describe 不计数，只算 music_play")
        check.ok(_find(msgs, "error") is None, "无 error")
    finally:
        if ws is not None:
            await ws.close()
        await server.stop()
        responses.stop()


async def test_remote_url_e2e(check: Checker) -> None:
    """MCP 结果里直接带 URL：mock 不配置曲库也能下发 music_track。"""
    check.section("D5. 工具结果自带 URL")
    url = "http://127.0.0.1:9009/media/remote.mp3"
    responses = FakeResponsesServer(_decide_e2e, style="hermes_output", tool_url=url)
    cfg = Config(host="127.0.0.1", port=0, music=True, no_tts=True, think_ms=0, quiet=True,
                 tts_text="放首晴天",
                 llm_base_url=responses.base, llm_api_key="test", llm_model="fake")
    server = MockServer(cfg)
    ws = None
    try:
        ws, _ = await _open(server)
        msgs = await _collect_round(ws, 1)
        track_event = _find(msgs, "music_track")
        check.ok(track_event is not None, "工具结果 URL → 下发 music_track")
        check.eq(track_event.get("url") if track_event else None, url, "music_track.url 来自工具结果")
        check.eq(track_event.get("title") if track_event else None, "远程曲目",
                 "music_track.title 来自工具结果")
        session = next(iter(server.active.values()))
        check.eq(session.stats.music_intents, 1, "统计 1 次工具调用")
    finally:
        if ws is not None:
            await ws.close()
        await server.stop()
        responses.stop()


async def test_music_with_llm_pipeline(check: Checker) -> None:
    """`--llm --llm-tts-stream` 同开时，音乐意图必须短路 LLM 流水线（不能重复回答/报 LLM 错）。"""
    check.section("D. --llm-tts-stream 同开")
    responses = FakeResponsesServer(_decide_e2e)
    cfg = Config(host="127.0.0.1", port=0, music=True, no_tts=True, think_ms=0, quiet=True,
                 tts_text="放首晴天", llm=True, llm_tts_stream=True,
                 llm_base_url=responses.base, llm_api_key="test", llm_model="fake")
    server = MockServer(cfg)
    ws = None
    try:
        ws, _ = await _open(server)
        msgs = await _collect_round(ws, 1)
        check.ok(_find(msgs, "music_track") is not None, "命中音乐工具仍下发 music_track")
        check.eq(sum(1 for m in msgs if m.get("t") == "music_track"), 1,
                 "只下发一次 music_track（不重复）")
        check.ok(_find(msgs, "error") is None, "不因 LLM 流水线配置而报 error")
        check.ok(len(responses.requests) == 1, "音乐轮只调 1 次 Responses（模板确认语）")
    finally:
        if ws is not None:
            await ws.close()
        await server.stop()
        responses.stop()


async def test_music_without_llm(check: Checker) -> None:
    """未配置 Responses API：音乐不执行，但语音链路必须照常走完（不能报 error/静默丢轮）。"""
    check.section("D3. 未配置 Responses API")
    saved = {key: os.environ.get(key) for key in ("LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL")}
    try:
        for key in saved:
            os.environ.pop(key, None)
        cfg = Config(host="127.0.0.1", port=0, music=True, no_tts=True, think_ms=0,
                     quiet=True, tts_text="放首晴天")
        server = MockServer(cfg)
        ws = None
        try:
            ws, _ = await _open(server)
            msgs = await _collect_round(ws, 1)
            check.ok(_find(msgs, "music_track") is None, "缺配置时不下发 music_track")
            check.ok(_find(msgs, "tts_end") is not None, "缺配置时语音链路仍收尾")
            session = next(iter(server.active.values()))
            check.eq(session.stats.music_llm_errors, 1, "统计：Responses 失败 1 次")
        finally:
            if ws is not None:
                await ws.close()
            await server.stop()
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


async def test_music_disabled(check: Checker) -> None:
    """未开 --music：同样说「暂停」，不能凭空产生音乐事件（保持旧行为）。"""
    check.section("D. 未启用 --music")
    cfg = Config(host="127.0.0.1", port=0, music=False, no_tts=True, think_ms=0,
                 quiet=True, tts_text="暂停")
    server = MockServer(cfg)
    ws = None
    try:
        ws, _ = await _open(server)
        msgs = await _collect_round(ws, 1)
        check.ok(_find(msgs, "music_control") is None, "未启用时不下发 music_control")
        check.ok(_find(msgs, "music_track") is None, "未启用时不下发 music_track")
        session = next(iter(server.active.values()))
        check.eq(session.stats.music_intents, 0, "未启用时意图统计为 0")
    finally:
        if ws is not None:
            await ws.close()
        await server.stop()


# ============================================================================
# E. CLI 接线
# ============================================================================


def test_cli(check: Checker) -> None:
    check.section("E. CLI")
    args = parse_args(["--music"])
    cfg = config_from_args(args)
    check.ok(cfg.music, "--music → Config.music")
    check.eq(cfg.music_tool_mode, "mcp", "默认工具模式 mcp（工具配在 Hermes 侧）")
    cfg_inline = config_from_args(parse_args(["--music", "--music-tools", "inline"]))
    check.eq(cfg_inline.music_tool_mode, "inline", "--music-tools inline 透传")

    args = parse_args(["--music", "--music-url", "http://192.168.1.5:9000/x.mp3"])
    cfg = config_from_args(args)
    check.eq(cfg.music_url, "http://192.168.1.5:9000/x.mp3", "--music-url 透传")

    radio_url = "http://lhttp.qingting.fm/live/20210755/64k.mp3"
    args = parse_args(["--music", "--music-url", radio_url, "--music-title", "清听电台"])
    cfg = config_from_args(args)
    check.eq(cfg.music_title, "清听电台", "--music-title 透传")
    service = MusicService.from_config(cfg)
    track = service.catalog.tracks[0] if service else None
    check.eq(track.title if track else None, "清听电台", "电台用 --music-title 作为曲目名")
    check.eq(track.duration_ms if track else None, 0, "电台 duration_ms=0")

    catalog_path = Path(__file__).resolve().parent / "music_catalog.example.json"
    example = MusicCatalog.from_json(catalog_path)
    check.ok(len(example) >= 1, "music_catalog.example.json 可加载")
    check.ok(all(t.url.startswith(("http://", "https://")) for t in example.tracks),
             "示例曲库 URL 均为 http(s)")

    for argv in (
        ["--music-url", "http://x/y.mp3"],              # 缺 --music
        ["--music-tools", "inline"],                    # 缺 --music
        ["--music", "--music-url", "ftp://x/y.mp3"],     # 非 http(s)
        ["--music", "--music-catalog", "/no/such.json"],  # 文件不存在
        ["--music", "--music-title", "电台"],             # 缺 --music-url
    ):
        try:
            config_from_args(parse_args(argv))
            check.ok(False, f"应拒绝参数 {argv}")
        except SystemExit:
            check.ok(True, f"拒绝参数 {argv}")

    service = MusicService.from_config(Config(music=True, music_url="http://x/y.mp3"))
    check.ok(service is not None and len(service.catalog) == 1, "单曲 URL 建曲库（1 首）")
    check.ok(MusicService.from_config(Config(music=False)) is None, "未启用 → 不创建服务")

    # 横幅/提示判断：--music 不要求 --llm，只要 LLM_* 齐全就按 Responses 回答显示
    check.ok(llm_env_ready("http://gw/v1", "m", "k"), "LLM 配置齐全 → 走 Responses")
    saved = {key: os.environ.pop(key, None)
             for key in ("LLM_BASE_URL", "LLM_MODEL", "LLM_API_KEY")}
    try:
        check.ok(not llm_env_ready(), "无 LLM_* → 不走 Responses")
        check.ok(not llm_env_ready("http://gw/v1", "", "k"), "缺 model → 不走 Responses")
    finally:
        for key, value in saved.items():
            if value is not None:
                os.environ[key] = value


# ============================================================================


def main(argv: list[str] | None = None) -> int:
    verbose = "--verbose" in (argv or sys.argv[1:])
    check = Checker(verbose=verbose)
    with tempfile.TemporaryDirectory(prefix="mock_music_test_") as tmp:
        test_tools(check)
        test_catalog(check, Path(tmp))
        test_search_http(check)
        test_http(check)
        test_responses_parse(check)
        test_session_tracking(check)
        asyncio.run(test_inline_tools(check))
        asyncio.run(test_hermes_tool_shape(check))
        asyncio.run(test_remote_url_e2e(check))
        asyncio.run(test_e2e(check))
        asyncio.run(test_e2e_search(check))
        asyncio.run(test_music_with_llm_pipeline(check))
        asyncio.run(test_music_without_llm(check))
        asyncio.run(test_music_disabled(check))
        test_cli(check)
    return 0 if check.summary() else 1


if __name__ == "__main__":
    sys.exit(main())
