#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""local_music_server —— 本机音乐服务器：静态 MP3 + 搜索端点 + 曲库 JSON。

用途：给 mock 的 `--music-search-url` / `--music-catalog` 提供一个**可控、合法**的源
（目录里放你自己的 MP3）。零依赖、零固件改动。

```bash
# 起服务（默认扫描目录下所有 .mp3，打印可用的两条 mock 命令）
uv run tools/local_music_server.py --dir ~/Music --port 9009

# 方式 A：mock 走搜索（说歌名 → 搜索 → music_track）
uv run tools/ws_mock_server.py --music \
  --music-search-url 'http://127.0.0.1:9009/search?q={query}'

# 方式 B：导出一份曲库 JSON，走本地匹配（不需要搜索端点）
curl -s http://127.0.0.1:9009/catalog > /tmp/music_catalog.json
uv run tools/ws_mock_server.py --music --music-catalog /tmp/music_catalog.json
```

端点：
  GET /search?q=晴天 → {"tracks":[{title,url,duration_ms,keywords}]}（最多 --limit 条）
  GET /media/<rel>    → 音频文件（支持 Range/206、HEAD）
  GET /catalog        → 整库 JSON（可直接给 --music-catalog）

URL 里的主机名默认自动取本机局域网 IP（设备要能访问；跨网段用 --public-host 指定）。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import socketserver
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlsplit

AUDIO_SUFFIXES = {".mp3"}


# ---------------------------------------------------------------------------
# 曲库索引
# ---------------------------------------------------------------------------


def _recover_utf8(text: str) -> str:
    """把 http.server 按 latin-1 解出来的原始 UTF-8 字节还原成中文。

    `curl '.../search?q=主角'` 会把中文按 UTF-8 原始字节放进请求行；`BaseHTTPRequestHandler`
    用 iso-8859-1 解码，`parse_qs` 不会二次还原 → 查询串变成乱码、搜索永远空。
    百分号编码（浏览器/mock）不受影响：unquote 已按 UTF-8 解好，这里 encode 会失败、原样返回。
    """
    try:
        return text.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return text


def _norm(text: str) -> str:
    """小写、去标点/多余空白（中文按原样保留）。"""
    return re.sub(r"[\s._\-—()（）\[\]【】]+", " ", text.lower()).strip()


# 文件名里常见的站点水印/推广标签（下载站会往文件名里塞）：整段丢掉
_PROMO_TAG = re.compile(
    r"[\[\(【]\s*[^\]\)】]*?(?:myfreemp3|www\.|https?://|\.(?:com|net|ink|cc|cn|org|me|top|vip))"
    r"[^\]\)】]*?\s*[\]\)】]",
    re.IGNORECASE,
)


def _parse_name(stem: str) -> tuple[str, str]:
    """文件名 → (title, artist)。

    - 去掉 `[myfreemp3.ink]` 这类站点标签、前导音轨号、多余下划线；
    - `王菲 - 主角` → title=主角、artist=王菲（确认语只念歌名，不念歌手/站点）。
    """
    text = _PROMO_TAG.sub(" ", stem)
    text = re.sub(r"[_]+", " ", text)
    text = re.sub(r"^[\s\d.\-]+", "", text)          # "01. " / "03 - "
    text = re.sub(r"\s+", " ", text).strip(" -—")
    artist, title = "", text
    if " - " in text:
        left, right = text.split(" - ", 1)
        if left.strip() and right.strip():
            artist, title = left.strip(), right.strip()
    return title or stem, artist


def _keywords(text: str) -> list[str]:
    tokens: list[str] = []
    for token in _norm(text).split(" "):
        if token and token not in tokens:
            tokens.append(token)
    return tokens[:8]


class Library:
    """扫描目录 → 曲目表 + 关键词检索。"""

    def __init__(self, root: Path, public_host: str, port: int, limit: int = 5) -> None:
        self.root = root.resolve()
        self.public_host = public_host
        self.port = port
        self.limit = max(1, limit)
        self._tracks: list[dict[str, Any]] = []

    def refresh(self) -> int:
        tracks: list[dict[str, Any]] = []
        for path in sorted(self.root.rglob("*")):
            if not path.is_file() or path.suffix.lower() not in AUDIO_SUFFIXES:
                continue
            rel = path.relative_to(self.root).as_posix()
            title, artist = _parse_name(path.stem)
            keywords = _keywords(f"{artist} {title}")
            if title.lower() not in keywords:
                keywords.insert(0, title.lower())
            tracks.append({
                "title": title,
                "artist": artist,
                "url": self.media_url(rel),
                "duration_ms": 0,       # 未知：端侧按"时长未知/持续流"处理
                "keywords": keywords,
                "_rel": rel,
                "_norm": _norm(f"{artist} {title}"),
            })
        self._tracks = tracks
        return len(tracks)

    @property
    def tracks(self) -> list[dict[str, Any]]:
        return self._tracks

    def media_url(self, rel: str) -> str:
        return f"http://{self.public_host}:{self.port}/media/{quote(rel, safe='/')}"

    def search(self, query: str) -> list[dict[str, Any]]:
        norm = _norm(query)
        if not norm:
            return []
        hits = []
        for track in self._tracks:
            name = track["_norm"]
            if norm == name:
                score = 3
            elif norm in name:
                score = 2
            elif all(token in name for token in norm.split(" ") if token):
                score = 1
            else:
                continue
            hits.append((score, len(name), track))
        hits.sort(key=lambda item: (-item[0], item[1]))
        return [dict(item[2]) for item in hits[: self.limit]]

    def catalog(self) -> dict[str, Any]:
        # 内部字段（_rel/_norm）不下发：--music-catalog 只认 title/url/...
        return {"tracks": [{k: v for k, v in t.items() if not k.startswith("_")}
                           for t in self._tracks]}


def _public_payload(track: dict[str, Any]) -> dict[str, Any]:
    """去掉内部字段（_rel/_norm）再下发给 mock。"""
    return {k: v for k, v in track.items() if not k.startswith("_")}


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


_NON_ASCII_BYTES = re.compile(rb"[\x80-\xff]")


class _Handler(BaseHTTPRequestHandler):
    server_version = "LocalMusic/1.0"
    protocol_version = "HTTP/1.1"

    def parse_request(self) -> bool:
        """把请求行里的原始非 ASCII 字节先转成 %XX，再交给基类解析。

        为什么需要：`curl '.../search?q=八'` 会把 UTF-8 原始字节塞进请求行，其中 0x85
        在 latin-1 下是 U+0085（NEL）——`str.split()` 把它当空白，请求行被切成 4 段，
        基类直接回 400。先把非 ASCII 字节百分号编码成 ASCII，就绕开这个（HTTP 本也要求编码）。
        """
        raw = getattr(self, "raw_requestline", b"") or b""
        if raw and _NON_ASCII_BYTES.search(raw):
            self.raw_requestline = _NON_ASCII_BYTES.sub(
                lambda match: b"%%%02X" % match.group(0)[0], raw
            )
        return super().parse_request()

    @property
    def library(self) -> Library:
        return self.server.library  # type: ignore[attr-defined]

    def do_GET(self) -> None:  # noqa: N802
        self._route(head_only=False)

    def do_HEAD(self) -> None:  # noqa: N802
        self._route(head_only=True)

    def _route(self, head_only: bool) -> None:
        parsed = urlsplit(self.path)
        path = _recover_utf8(unquote(parsed.path))
        if path == "/search":
            query = _recover_utf8((parse_qs(parsed.query).get("q") or [""])[0])
            tracks = self.library.search(query)
            self._json({"tracks": [_public_payload(t) for t in tracks]}, head_only)
            return
        if path == "/catalog":
            self.library.refresh()
            self._json(self.library.catalog(), head_only)
            return
        if path.startswith("/media/"):
            self._file(path[len("/media/"):], head_only)
            return
        if path.startswith("/stream/"):
            self._stream(path[len("/stream/"):], head_only)
            return
        self._json({"error": "not found",
                    "hint": "/search?q= /media/<path> /stream/<path> /catalog"}, head_only,
                   status=404)

    def _json(self, body: dict[str, Any], head_only: bool, status: int = 200) -> None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if not head_only:
            self.wfile.write(data)

    def _file(self, rel: str, head_only: bool) -> None:
        rel = rel.lstrip("/")
        target = (self.library.root / rel).resolve()
        try:
            target.relative_to(self.library.root)
        except ValueError:
            self._json({"error": "forbidden"}, head_only, status=403)
            return
        if not target.is_file() or target.suffix.lower() not in AUDIO_SUFFIXES:
            self._json({"error": "not found"}, head_only, status=404)
            return

        size = target.stat().st_size
        start, end, status = 0, size - 1, 200
        range_header = self.headers.get("Range")
        if range_header:
            parsed_range = _parse_range(range_header, size)
            if parsed_range is None:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            start, end = parsed_range
            status = 206

        self.send_response(status)
        self.send_header("Content-Type", "audio/mpeg")
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Accept-Ranges", "bytes")
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if head_only:
            return
        with target.open("rb") as handle:
            handle.seek(start)
            remaining = end - start + 1
            while remaining > 0:
                chunk = handle.read(min(64 * 1024, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def _stream(self, rel: str, head_only: bool) -> None:
        """无限循环直播流（T4.1 电台路径验证）：无 Content-Length、connection close 定界。

        目的：把"直播流（时长未知）"的代码路径从公网 CDN 抖动里剥离出来，让真机
        60s 连续播放可复现（PV-012 第 ⑥ 项）。客户端断开后写失败即结束。
        """
        rel = rel.lstrip("/")
        target = (self.library.root / rel).resolve()
        try:
            target.relative_to(self.library.root)
        except ValueError:
            self._json({"error": "forbidden"}, head_only, status=403)
            return
        if not target.is_file() or target.suffix.lower() not in AUDIO_SUFFIXES:
            self._json({"error": "not found"}, head_only, status=404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "audio/mpeg")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        if head_only:
            return
        try:
            with target.open("rb") as handle:
                while True:
                    chunk = handle.read(4096)
                    if not chunk:
                        handle.seek(0)
                        continue
                    self.wfile.write(chunk)
                    # 按"直播节奏"限速（~100KB/s，高于常见网络电台码率即可）：
                    # 不限速会以磁盘读速猛灌，客户端一旦读满就只留下巨大的临时残留。
                    time.sleep(0.04)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        if os.environ.get("LOCAL_MUSIC_VERBOSE"):
            print(f"[{self.address_string()}] {fmt % args}", flush=True)


def _parse_range(header: str, size: int) -> tuple[int, int] | None:
    if not header.startswith("bytes="):
        return None
    spec = header[len("bytes="):].split(",")[0].strip()
    start_s, _, end_s = spec.partition("-")
    try:
        if not start_s:
            if not end_s:
                return None
            length = int(end_s)
            if length <= 0:
                return None
            start, end = max(0, size - length), size - 1
        else:
            start = int(start_s)
            end = int(end_s) if end_s else size - 1
    except ValueError:
        return None
    if start < 0 or start >= size or end < start:
        return None
    return start, min(end, size - 1)


class _ThreadingServer(ThreadingHTTPServer):
    """跳过 getfqdn 反向解析（0.0.0.0 时某些网络会卡 5s）。"""

    daemon_threads = True
    allow_reuse_address = True

    def server_bind(self) -> None:
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = host
        self.server_port = port


class MusicServer:
    def __init__(self, directory: Path, host: str, port: int, public_host: str,
                 limit: int = 5, rescan: bool = False) -> None:
        self.library = Library(directory, public_host, port, limit=limit)
        self.host = host
        self.port = port
        self.rescan = rescan
        self.httpd: _ThreadingServer | None = None
        self.thread: threading.Thread | None = None

    def start(self) -> int:
        count = self.library.refresh()
        httpd = _ThreadingServer((self.host, self.port), _Handler)
        httpd.library = self.library  # type: ignore[attr-defined]
        self.port = httpd.server_address[1]
        # 端口确定后重建 URL（--port 0 时库里的 URL 要带真实端口）
        self.library.port = self.port
        self.library.refresh()
        self.httpd = httpd
        self.thread = threading.Thread(target=httpd.serve_forever, name="local-music", daemon=True)
        self.thread.start()
        return count

    def stop(self) -> None:
        if self.httpd is not None:
            self.httpd.shutdown()
            self.httpd.server_close()
            self.httpd = None
        if self.thread is not None:
            self.thread.join(timeout=2.0)
            self.thread = None


def detect_lan_ip() -> str:
    """UDP 出口探测（瞬时）；失败回退 127.0.0.1。"""
    import socket

    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.connect(("8.8.8.8", 80))
            ip = sock.getsockname()[0]
        finally:
            sock.close()
        if ip and not ip.startswith("127."):
            return ip
    except OSError:
        pass
    return "127.0.0.1"


# ---------------------------------------------------------------------------
# 自测
# ---------------------------------------------------------------------------


def run_self_test() -> int:
    import tempfile
    import urllib.error
    import urllib.request

    failures: list[str] = []
    passed = 0

    def check(cond: bool, name: str) -> None:
        nonlocal passed
        if cond:
            passed += 1
            print(f"  PASS {name}", flush=True)
        else:
            failures.append(name)
            print(f"  FAIL {name}", flush=True)

    with tempfile.TemporaryDirectory(prefix="local_music_selftest_") as tmp:
        root = Path(tmp)
        (root / "01. 晴天.mp3").write_bytes(b"\xff\xfb\x90\x64" + b"\x00" * 400)
        (root / "稻香.mp3").write_bytes(b"\xff\xfb\x90\x64" + b"\x00" * 200)
        (root / "王菲 - 主角 [myfreemp3.ink].mp3").write_bytes(
            b"\xff\xfb\x90\x64" + b"\x00" * 100)
        (root / "readme.txt").write_text("not audio", encoding="utf-8")

        server = MusicServer(root, "127.0.0.1", 0, "127.0.0.1", limit=5)
        count = server.start()
        try:
            check(count == 3, "只索引 .mp3（忽略 txt）")
            base = f"http://127.0.0.1:{server.port}"

            with urllib.request.urlopen(base + "/search?q=" + quote("晴天"), timeout=5) as resp:
                body = json.loads(resp.read())
            tracks = body.get("tracks") or []
            check(len(tracks) == 1 and tracks[0]["title"] == "晴天", "搜索命中（去音轨号）")
            check(tracks[0]["url"].startswith("http://127.0.0.1:"), "返回可播 URL")
            check(tracks[0]["duration_ms"] == 0, "duration_ms=0（时长未知）")

            with urllib.request.urlopen(base + "/search?q=" + quote("王菲"), timeout=5) as resp:
                hit = json.loads(resp.read())["tracks"][0]
            check(hit["title"] == "主角" and hit["artist"] == "王菲",
                  "去站点标签并按「歌手 - 歌名」拆分")

            # curl 直接贴中文（请求行里是原始 UTF-8 字节，不是 %XX）
            # 注意：每个请求开新连接（Connection: close），否则复用已关闭的 socket
            import socket

            def raw_search(text: str) -> list:
                sock = socket.create_connection(("127.0.0.1", server.port), timeout=5)
                try:
                    sock.sendall(b"GET /search?q=" + text.encode("utf-8")
                                 + b" HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
                    chunks = []
                    while True:
                        piece = sock.recv(4096)
                        if not piece:
                            break
                        chunks.append(piece)
                finally:
                    sock.close()
                head, _, body = b"".join(chunks).partition(b"\r\n\r\n")
                if b" 200 " not in head.split(b"\r\n", 1)[0]:
                    return [{"_status": head.split(b"\r\n", 1)[0].decode("latin-1")}]
                return json.loads(body).get("tracks") or []

            hits = raw_search("主角")
            check(len(hits) == 1 and hits[0]["title"] == "主角",
                  "raw UTF-8 query（curl 直接贴中文）")
            # 0x85（NEL）坑：curl 'q=八' 曾被基类当成空白 → 400
            # 自测目录里没有"七溜八溜"，这里只验证不再 400（旧实现会回 400 HTML）
            hits = raw_search("八")
            check(isinstance(hits, list) and all("_status" not in h for h in hits),
                  "raw query '八'（含 0x85）不 400")
            hits = raw_search("七溜八溜")
            check(isinstance(hits, list) and all("_status" not in h for h in hits),
                  "raw query '七溜八溜' 不 400")

            with urllib.request.urlopen(base + "/search?q=" + quote("不存在"), timeout=5) as resp:
                check(json.loads(resp.read()).get("tracks") == [], "搜索未命中 → 空数组")

            with urllib.request.urlopen(base + "/catalog", timeout=5) as resp:
                catalog = json.loads(resp.read())
            check(len(catalog.get("tracks") or []) == 3, "catalog 导出整库")
            check(all("_rel" not in t for t in catalog["tracks"]), "catalog 不含内部字段")

            url = tracks[0]["url"]
            with urllib.request.urlopen(url, timeout=5) as resp:
                data = resp.read()
            check(resp.status == 200 and len(data) == 404, "media 200 + 完整字节")

            req = urllib.request.Request(url, headers={"Range": "bytes=0-9"})
            with urllib.request.urlopen(req, timeout=5) as resp:
                check(resp.status == 206 and len(resp.read()) == 10, "Range 206")
                check(resp.headers.get("Content-Range", "").startswith("bytes 0-9/"),
                      "Content-Range 正确")

            for bad in ("/media/../etc/passwd", "/media/readme.txt", "/nope"):
                try:
                    urllib.request.urlopen(base + bad, timeout=5)
                    check(False, f"{bad} 应 4xx")
                except urllib.error.HTTPError as exc:
                    check(exc.code in (403, 404), f"{bad} → {exc.code}")
        finally:
            server.stop()

    print(f"\nself-test: {passed}/{passed + len(failures)} PASS" if not failures
          else f"\nself-test: {len(failures)} FAIL")
    return 0 if not failures else 1


# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="本机音乐服务器（静态 MP3 + 搜索 + 曲库）")
    parser.add_argument("--dir", default=str(Path.home() / "Music"), help="音乐目录（递归扫描 .mp3）")
    parser.add_argument("--host", default="0.0.0.0", help="监听地址")
    parser.add_argument("--port", type=int, default=9009, help="端口（0=随机）")
    parser.add_argument("--public-host", default="", help="写进 url 的主机名（设备要能访问；缺省自动探测局域网 IP）")
    parser.add_argument("--limit", type=int, default=5, help="单次搜索最多返回条数")
    parser.add_argument("--rescan", action="store_true", help="每次 /search 前重新扫描目录")
    parser.add_argument("--self-test", action="store_true", help="进程内自测（不需要音乐文件）")
    args = parser.parse_args(argv)

    if args.self_test:
        return run_self_test()

    root = Path(args.dir).expanduser()
    if not root.is_dir():
        print(f"目录不存在：{root}", file=sys.stderr)
        return 2
    public_host = args.public_host or detect_lan_ip()
    server = MusicServer(root, args.host, args.port, public_host,
                         limit=args.limit, rescan=args.rescan)
    count = server.start()
    base = f"http://{public_host}:{server.port}"
    print("=" * 72)
    print(f"本机音乐服务器：{root}（{count} 首 .mp3）")
    print(f"监听 {args.host}:{server.port} | 设备侧 URL 用 {public_host}")
    print("mock 接法（二选一）：")
    print(f"  --music-search-url 'http://127.0.0.1:{server.port}/search?q={{query}}'")
    print(f"  curl -s {base}/catalog > /tmp/music_catalog.json  # 再 --music-catalog")
    print("=" * 72, flush=True)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n退出", flush=True)
    finally:
        server.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
