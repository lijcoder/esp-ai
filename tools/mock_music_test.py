#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["websockets>=14,<19"]
# ///
"""mock_music_test —— M1 音乐（mock 侧）离线自测，不接开发板、不需要 ASR/TTS 模型。

覆盖四层：
  A. 意图规则表：控制词/点歌/误命中防护（「今天天气如何」不能被当成点歌）
  B. 曲库：检索优先级、next/prev 游标、JSON 加载、URL 校验
  C. 内置 HTTP：200/206/404、Content-Type、示例曲目可解码（MP3 帧同步）
  D. 假设备 e2e：真实 WS 协议跑一轮，断言 asr_final → music_track / music_control →
     固定确认语 TTS，并验证「未启用 --music 时不产生音乐事件」

跑法：
  uv run tools/mock_music_test.py
  uv run tools/mock_music_test.py --verbose
"""

from __future__ import annotations

import asyncio
import json
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
from mock_core import Config
from mock_music import (
    ACTION_NEXT,
    ACTION_PAUSE,
    ACTION_PLAY,
    ACTION_PREV,
    ACTION_RESUME,
    ACTION_STOP,
    MusicCatalog,
    MusicHttpServer,
    MusicIntent,
    MusicIntentParser,
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
# A. 意图规则表
# ============================================================================

_INTENT_CASES: tuple[tuple[str, str | None, str], ...] = (
    ("放首周杰伦的晴天", ACTION_PLAY, "周杰伦的晴天"),
    ("播放孤勇者", ACTION_PLAY, "孤勇者"),
    ("来首歌", ACTION_PLAY, ""),
    ("放首歌", ACTION_PLAY, ""),
    ("放首歌吧", ACTION_PLAY, ""),
    ("你能放首歌吗", ACTION_PLAY, ""),
    ("我想听稻香", ACTION_PLAY, "稻香"),
    ("我要听晴天", ACTION_PLAY, "晴天"),
    ("放首情歌", ACTION_PLAY, "情歌"),
    ("放首晴天吧", ACTION_PLAY, "晴天"),
    ("播放一首主角", ACTION_PLAY, "主角"),
    ("播放首晴天", ACTION_PLAY, "晴天"),
    ("放一首王菲", ACTION_PLAY, "王菲"),
    ("放电台", ACTION_PLAY, "电台"),
    ("听广播", ACTION_PLAY, "广播"),
    ("播放", ACTION_PLAY, ""),
    ("暂停", ACTION_PAUSE, ""),
    ("暂停一下", ACTION_PAUSE, ""),
    ("先停一下", ACTION_PAUSE, ""),
    ("暂停播放这首歌", ACTION_PAUSE, ""),
    ("继续播放", ACTION_RESUME, ""),
    ("接着放", ACTION_RESUME, ""),
    ("继续", ACTION_RESUME, ""),
    ("下一首", ACTION_NEXT, ""),
    ("换一首", ACTION_NEXT, ""),
    ("换一首歌", ACTION_NEXT, ""),
    ("上一首", ACTION_PREV, ""),
    ("前一首", ACTION_PREV, ""),
    ("停止播放", ACTION_STOP, ""),
    ("别放了", ACTION_STOP, ""),
    ("关掉音乐", ACTION_STOP, ""),
    # 误命中防护
    ("今天天气如何", None, ""),
    ("你好", None, ""),
    ("我听不懂", None, ""),
    ("放", None, ""),
    ("放屁", None, ""),
    ("", None, ""),
    ("放大音量", None, ""),
    ("把音量调大", None, ""),
)


def test_parser(check: Checker) -> None:
    check.section("A. 意图规则")
    parser = MusicIntentParser()
    for text, want_action, want_query in _INTENT_CASES:
        intent = parser.parse(text)
        got_action = intent.action if intent else None
        got_query = intent.query if intent else ""
        check.eq(got_action, want_action, f"action({text!r})")
        if want_action is not None:
            check.eq(got_query, want_query, f"query({text!r})")

    check.ok(parser.parse(None) is None, "None 输入 → None")
    # 控制词优先于播放词：「别放了」含「放」，必须是 stop 而不是 play
    intent = parser.parse("这首歌别放了")
    check.eq(intent.action if intent else None, ACTION_STOP, "控制词优先（别放 > 放）")


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
        intent = MusicIntent(action=ACTION_PLAY, query="晴天")
        track, matched = service.track_for(intent) if service else (None, False)
        check.eq(track.title if track else None, "搜索结果:晴天", "track_for 走搜索源")
        check.ok(matched, "搜索命中 matched=True")

        fallback = MusicService.from_config(Config(
            music=True,
            music_search_url=server.base + "/fail?q={query}",
            music_url="http://music.example/fallback.mp3",
        ))
        track, _ = fallback.track_for(intent) if fallback else (None, False)
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
    cfg = Config(host="127.0.0.1", port=0, music=True, no_tts=True, think_ms=0, quiet=True,
                 tts_text="放首晴天", music_search_url=search_server.base + "/search?q={query}")
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
    cfg = Config(host="127.0.0.1", port=0, music=True, no_tts=True, think_ms=0,
                 quiet=True, max_tts_frames=0)
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

        # 第 3 轮：下一首 → 新的 music_track（end 侧无播放列表）
        cfg.tts_text = "下一首"
        msgs = await _collect_round(ws, 3)
        track_event = _find(msgs, "music_track")
        check.ok(track_event is not None, "下一首 → 新 music_track")
        if track_event:
            check.eq(track_event.get("mid"), 2, "下一首 mid=2")

        # 第 4 轮：普通聊天 → 不得产生音乐事件
        cfg.tts_text = "今天天气如何"
        msgs = await _collect_round(ws, 4)
        check.ok(_find(msgs, "music_track") is None, "普通聊天不下发 music_track")
        check.ok(_find(msgs, "music_control") is None, "普通聊天不下发 music_control")
        check.ok(_find(msgs, "llm_delta") is not None, "普通聊天仍有 llm_delta（复读路径）")

        # 第 5 轮：停止 → music_control stop
        cfg.tts_text = "停止播放"
        msgs = await _collect_round(ws, 5)
        control = _find(msgs, "music_control")
        check.eq(control.get("action") if control else None, "stop", "停止 → action=stop")

        # 第 6 轮：继续 → music_control resume
        cfg.tts_text = "继续播放"
        msgs = await _collect_round(ws, 6)
        control = _find(msgs, "music_control")
        check.eq(control.get("action") if control else None, "resume", "继续 → action=resume")

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
        check.eq(session.stats.music_intents, 5, "统计：5 条音乐意图（点歌/暂停/下一首/停止/继续）")
        check.eq(session.stats.music_tracks, 3, "统计：3 条 music_track（点歌/下一首/music_request）")
        check.eq(session.stats.music_controls, 3, "统计：3 条 music_control（暂停/停止/继续）")
    finally:
        if ws is not None:
            await ws.close()
        await server.stop()


async def test_music_with_llm_pipeline(check: Checker) -> None:
    """`--llm --llm-tts-stream` 同开时，音乐意图必须短路 LLM 流水线（不能重复回答/报 LLM 错）。"""
    check.section("D. --llm-tts-stream 同开")
    cfg = Config(host="127.0.0.1", port=0, music=True, no_tts=True, think_ms=0, quiet=True,
                 tts_text="暂停", llm=True, llm_tts_stream=True)
    server = MockServer(cfg)
    ws = None
    try:
        ws, _ = await _open(server)
        msgs = await _collect_round(ws, 1)
        check.ok(_find(msgs, "music_control") is not None, "命中音乐意图仍下发 music_control")
        check.eq(sum(1 for m in msgs if m.get("t") == "music_control"), 1,
                 "只下发一次 music_control")
        check.ok(_find(msgs, "error") is None, "不因 LLM 未配置而报 error")
    finally:
        if ws is not None:
            await ws.close()
        await server.stop()


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


# ============================================================================


def main(argv: list[str] | None = None) -> int:
    verbose = "--verbose" in (argv or sys.argv[1:])
    check = Checker(verbose=verbose)
    with tempfile.TemporaryDirectory(prefix="mock_music_test_") as tmp:
        test_parser(check)
        test_catalog(check, Path(tmp))
        test_search_http(check)
        test_http(check)
        asyncio.run(test_e2e(check))
        asyncio.run(test_e2e_search(check))
        asyncio.run(test_music_with_llm_pipeline(check))
        asyncio.run(test_music_disabled(check))
        test_cli(check)
    return 0 if check.summary() else 1


if __name__ == "__main__":
    sys.exit(main())
