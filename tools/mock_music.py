#!/usr/bin/env python3
"""mock_music —— 云端音乐意图与曲库（M1「双通道」方案的 mock 侧）。

## 为什么需要它

端侧（ESP32）不做任何 NLU：它只会「唤醒 → 上行音频」，以及按**结构化事件**
播放/暂停。所以「用户说暂停」怎么变成设备动作，是云端的事。两条通道：

  1. 控制通道（本文件）：ASR 文本先过一张确定性规则表（暂停/继续/下一首/停止…），
     命中就直接下发 `music_control`，**不进 LLM**——否则说「暂停」要等一次 LLM 往返；
  2. 内容通道：没命中控制词的句子照旧交给 LLM/Hermes 回答，URL 由曲库检索给出。

LLM/Hermes **不参与传 URL**：URL 只出现在下行 `music_track` 里（契约见 ARCH §6.3、
PROTOCOL §3）。这样既不需要网关支持 tool calling，也不会出现「TTS 把 URL 念出来」。

## 职责

  - `MusicIntentParser`：ASR 文本 → 意图（play/pause/resume/next/prev/stop + query）
  - `MusicCatalog`：query → 曲目（JSON 曲库 / `--music-url` 固定曲目 / 内置示例曲目）
  - `MusicHttpServer`：内置 HTTP 静态服务，给示例曲目一个**真机可播**的 URL
  - `MusicService`：把上面三件事装在一起，供 mock_server 的会话调用

纯标准库（不依赖 websockets），可单独单测；HTTP 服务用守护线程，mock 退出即结束。

## 契约（2026-11-10 提议，T4.2 端侧已实现）

下行：
  {"t":"music_track","mid":1,"url":"http://…/x.mp3","codec":"mp3","duration_ms":…,"title":"…"}
  {"t":"music_control","mid":1,"action":"pause|resume|stop"}

音乐事件用独立的 `mid`（本连接内自增），**不复用语音轮次的 `sid`**：音乐可以在没有
语音轮次时播放（按键/云端推荐），也会跨越多轮会话，用语音 `sid` 过滤会把音乐事件误杀。
`next`/`prev` 由服务端直接下发**新曲目的 `music_track`**（端侧没有播放列表，只有服务端有）。
"""

from __future__ import annotations

import json
import logging
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass, field
import socketserver
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote, unquote, urlsplit

LOG = logging.getLogger("ws_mock.music")

# 动作常量：与文档/端侧 T4.2 保持一致，禁止私改
ACTION_PLAY = "play"
ACTION_PAUSE = "pause"
ACTION_RESUME = "resume"
ACTION_NEXT = "next"
ACTION_PREV = "prev"
ACTION_STOP = "stop"
MUSIC_ACTIONS = (ACTION_PLAY, ACTION_PAUSE, ACTION_RESUME, ACTION_NEXT, ACTION_PREV, ACTION_STOP)
TRACK_ACTIONS = (ACTION_PLAY, ACTION_NEXT, ACTION_PREV)   # 需要曲目 URL 的动作
CONTROL_ACTIONS = (ACTION_PAUSE, ACTION_RESUME, ACTION_STOP)  # 只改播放状态的动作
MUSIC_CODEC = "mp3"


# ============================================================================
# 1. 意图解析：一张确定性规则表（控制指令必须快且确定）
# ============================================================================

# 归一化时丢掉的标点/空白（保留中英文数字）
_STRIP_CHARS = set(
    " \t\r\n，。！？、；：,.!?;:~…\"'“”‘’（）()【】[]《》<>·-—_/\\|+*=&%$#@^`~"
)

# 口语前缀：先剥掉再判断「放/播/来/听…」，这样「你能放首歌吗」也能命中。
# 长词在前，避免「我想听」被「我」截断。
_LEADS = (
    "帮我", "麻烦你", "麻烦", "请", "给我", "我想", "能不能", "可不可以", "可以", "能",
    "你", "再", "那就", "就", "现在", "我",
)

# 「我想听稻香」这类：先把「想听」并成播放入口「听」，否则会被前缀吃掉后找不到入口词
_INTENT_PREFIX_MAP = (
    ("我想听", "听"), ("我要听", "听"), ("想听", "听"),
    ("我想放", "放"), ("我要放", "放"), ("想放", "放"),
)

# 播放入口词（长词在前：「播放」要先于「放」，「来一首」要先于「来」）
_PLAY_HEADS = (
    "播放", "放一首", "放首", "放个", "放点", "放",
    "来一首", "来首", "来一曲", "来点", "来",
    "听一首", "听首", "听点", "听",
    "点一首", "点歌", "点个",
    "唱一首", "唱首", "唱",
)

# 只有「整句就是这些泛称」时才清空 query；不做「去尾字」——否则「情歌」会被削成「情」
_EMPTY_QUERIES = ("这首歌", "一首歌", "首歌", "歌曲", "音乐", "曲子", "歌")
# 尾部语气词反复剥离（「放首歌吧」→「放首歌」）
_EXPLETIVES = ("好吗", "可以吗", "行吗", "吧", "吗", "谢谢", "呀", "啊", "呢")

# 控制类规则：顺序即优先级（stop → pause → resume → next → prev）
_CONTROL_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (ACTION_STOP, ("停止播放", "停止音乐", "关掉音乐", "关闭音乐", "把音乐关掉",
                   "别放了", "别放", "不要放了", "别唱了", "退出音乐", "停止")),
    (ACTION_PAUSE, ("暂停播放", "暂停音乐", "暂停", "先停一下", "停一下", "停一停", "别播了")),
    (ACTION_RESUME, ("继续播放", "继续放", "接着放", "接着听", "恢复播放", "继续")),
    (ACTION_NEXT, ("下一首歌", "下一首", "下一曲", "换一首", "换一曲", "换首歌",
                   "换个歌", "再换一首", "切歌", "下一首吧")),
    (ACTION_PREV, ("上一首歌", "上一首", "上一曲", "前一首", "返回上一首")),
)

_BAD_QUERY_PREFIX = ("不", "没", "别")   # 「听不懂」这类误命中：query 以否定词开头就丢弃


def _normalize(text: str) -> str:
    """去空白/标点并转小写；保留中文、字母、数字。"""
    return "".join(ch.lower() for ch in text if ch not in _STRIP_CHARS)


@dataclass(frozen=True)
class MusicIntent:
    """一条音乐意图；`rule` 是命中的关键词，日志与断言用。"""

    action: str
    query: str = ""
    rule: str = ""


class MusicIntentParser:
    """ASR 文本 → 音乐意图；非音乐语句返回 None（继续走 LLM/Hermes）。"""

    def parse(self, text: str | None) -> MusicIntent | None:
        raw = (text or "").strip()
        norm = _normalize(raw)
        if len(norm) < 2:
            return None
        # 音量/声音类指令（T4.3 才定契约）：这里不猜，交给 LLM 通道，避免「放大音量」被当点歌
        if "音量" in norm or "声音" in norm:
            return None

        # 1) 控制词优先：「别放了」含「放」，必须先判 stop
        for action, rules in _CONTROL_RULES:
            for rule in rules:
                if rule in norm:
                    return MusicIntent(action=action, query="", rule=rule)

        # 2) 「想听/想放」并入播放入口，再剥口语前缀，最后必须以播放入口词开头
        stripped = norm
        for phrase, head_word in _INTENT_PREFIX_MAP:
            if stripped.startswith(phrase):
                stripped = head_word + stripped[len(phrase):]
                break
        changed = True
        while changed:
            changed = False
            for lead in _LEADS:
                if stripped.startswith(lead) and len(stripped) > len(lead):
                    stripped = stripped[len(lead):]
                    changed = True
                    break

        head = next((w for w in _PLAY_HEADS if stripped.startswith(w)), None)
        if head is None:
            return None
        query = stripped[len(head):]

        # 3) 剥尾部语气词；只有整句是「歌/歌曲/音乐…」这类泛称才清空 query
        changed = True
        while changed and query:
            changed = False
            for suffix in _EXPLETIVES:
                if query.endswith(suffix) and len(query) > len(suffix):
                    query = query[: -len(suffix)]
                    changed = True
                    break
        # "播放一首主角" / "播放首晴天"：head 只吃掉"播放"，这里再吃掉量词
        for prefix in ("一首", "首", "个", "支"):
            if query.startswith(prefix) and len(query) > len(prefix):
                query = query[len(prefix):]
                break
        if query in _EMPTY_QUERIES:
            query = ""

        if query.startswith(_BAD_QUERY_PREFIX):
            return None
        if query and len(query) <= 1 and len(norm) <= 2:
            return None      # 「放屁」这类两个字的口语：不点歌
        if len(query) > 40:  # 明显不是歌名：交给 LLM，不要瞎点歌
            return None
        return MusicIntent(action=ACTION_PLAY, query=query, rule=head)


# ============================================================================
# 2. 曲库：query → 曲目
# ============================================================================


@dataclass(frozen=True)
class MusicTrack:
    title: str
    url: str
    duration_ms: int = 0
    artist: str = ""
    keywords: tuple[str, ...] = field(default_factory=tuple)

    def as_event(self, mid: int) -> dict[str, Any]:
        event: dict[str, Any] = {
            "t": "music_track",
            "mid": mid & 0xFFFF,
            "url": self.url,
            "codec": MUSIC_CODEC,
            "duration_ms": int(self.duration_ms),
            "title": self.title,
        }
        if self.artist:
            event["artist"] = self.artist
        return event


def _redact(url: str) -> str:
    """日志里只留 scheme://host/path（不打印 query，避免把 token/签名写进日志）。"""
    try:
        parts = urlsplit(url)
    except ValueError:
        return "<非法 URL>"
    return f"{parts.scheme}://{parts.netloc}{parts.path}"


def _valid_url(url: str) -> bool:
    """只允许 http(s)：设备侧也按这个口径校验（禁止 file://、ftp://）。"""
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    return parts.scheme in ("http", "https") and bool(parts.netloc)


class MusicCatalog:
    """曲目列表 + 检索 + 顺序游标（next/prev 用）。

    匹配优先级：标题完全相等 > 关键词 > 标题包含 > 歌手包含；都没命中时回退到
    当前游标曲目（`matched=False`，调用方记 WARN）——演示时「放首不存在的歌」也能出声。
    """

    def __init__(self, tracks: Iterable[MusicTrack] = ()) -> None:
        self._tracks: list[MusicTrack] = []
        for track in tracks:
            self.add(track)
        self._cursor = -1

    def add(self, track: MusicTrack) -> None:
        if not _valid_url(track.url):
            raise ValueError(f"曲目 {track.title!r} 的 url 非法（只允许 http/https）：{track.url!r}")
        self._tracks.append(track)

    def __len__(self) -> int:
        return len(self._tracks)

    @property
    def tracks(self) -> tuple[MusicTrack, ...]:
        return tuple(self._tracks)

    def resolve(self, query: str) -> tuple[MusicTrack | None, bool]:
        """返回 (曲目, 是否精确命中)；曲库为空返回 (None, False)。"""
        if not self._tracks:
            return None, False
        norm = _normalize(query or "")
        if not norm:
            self._cursor = 0 if self._cursor < 0 else self._cursor
            return self._tracks[self._cursor], True

        for index, track in enumerate(self._tracks):
            if _normalize(track.title) == norm:
                self._cursor = index
                return track, True
        # 标题出现在 query 里优先于关键词：「周杰伦的稻香」应命中《稻香》而不是同为周杰伦的《晴天》
        for index, track in enumerate(self._tracks):
            title = _normalize(track.title)
            if title and (norm in title or title in norm):
                self._cursor = index
                return track, True
        for index, track in enumerate(self._tracks):
            if any(norm in _normalize(kw) or _normalize(kw) in norm for kw in track.keywords if kw):
                self._cursor = index
                return track, True
        for index, track in enumerate(self._tracks):
            artist = _normalize(track.artist)
            if artist and (norm in artist or artist in norm):
                self._cursor = index
                return track, True

        if self._cursor < 0:
            self._cursor = 0
        return self._tracks[self._cursor], False

    def next_track(self) -> MusicTrack | None:
        if not self._tracks:
            return None
        self._cursor = 0 if self._cursor < 0 else (self._cursor + 1) % len(self._tracks)
        return self._tracks[self._cursor]

    def prev_track(self) -> MusicTrack | None:
        if not self._tracks:
            return None
        self._cursor = 0 if self._cursor < 0 else (self._cursor - 1) % len(self._tracks)
        return self._tracks[self._cursor]

    @classmethod
    def from_json(cls, path: str | Path) -> "MusicCatalog":
        """读 JSON 曲库：{"tracks":[{"title","url","duration_ms","artist","keywords"}]}。"""
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("tracks"), list):
            raise ValueError('曲库格式错误：顶层应为 {"tracks": [...]}')
        catalog = cls()
        for item in data["tracks"]:
            if not isinstance(item, dict) or not item.get("url"):
                raise ValueError(f"曲库条目缺少 url：{item!r}")
            keywords = item.get("keywords") or ()
            if isinstance(keywords, str):
                keywords = (keywords,)
            catalog.add(
                MusicTrack(
                    title=str(item.get("title") or "未命名曲目"),
                    url=str(item["url"]),
                    duration_ms=int(item.get("duration_ms") or 0),
                    artist=str(item.get("artist") or ""),
                    keywords=tuple(str(k) for k in keywords),
                )
            )
        return catalog


# ============================================================================
# 3. 内置 HTTP 服务：给示例曲目一个真机可播 URL
# ============================================================================

_DEMO_REPEATS = 12   # 内嵌 MP3 只有 ~528ms；重复拼帧（MP3 帧自包含）到 ~6.3s，够真机起播验证


class _MusicRequestHandler(BaseHTTPRequestHandler):
    """只读静态文件服务；支持 Range（播放器/T4.5 断点续播会用）。"""

    server_version = "MockMusic/1.0"
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 约定
        self._serve(head_only=False)

    def do_HEAD(self) -> None:  # noqa: N802
        self._serve(head_only=True)

    def _serve(self, head_only: bool) -> None:
        files: dict[str, bytes] = getattr(self.server, "music_files", {})
        path = _recover_utf8(unquote(urlsplit(self.path).path))
        body = files.get(path)
        if body is None:
            self.send_error(404, "not found")
            return

        size = len(body)
        content_type = "audio/mpeg" if path.lower().endswith(".mp3") else "application/octet-stream"
        start, end = 0, size - 1
        status = 200
        range_header = self.headers.get("Range")
        if range_header:
            parsed = _parse_range(range_header, size)
            if parsed is None:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            start, end = parsed
            status = 206

        chunk = body[start : end + 1]
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(chunk)))
        self.send_header("Accept-Ranges", "bytes")
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if not head_only:
            self.wfile.write(chunk)

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        LOG.debug("%s - %s", self.address_string(), fmt % args)


def _recover_utf8(text: str) -> str:
    """http.server 按 latin-1 解的原始 UTF-8 字节 → 中文（百分号编码不受影响）。"""
    try:
        return text.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return text


def _parse_range(header: str, size: int) -> tuple[int, int] | None:
    """解析单段 Range；不合法返回 None（调用方回 416）。"""
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
            start = max(0, size - length)
            end = size - 1
        else:
            start = int(start_s)
            end = int(end_s) if end_s else size - 1
    except ValueError:
        return None
    if start < 0 or start >= size or end < start:
        return None
    return start, min(end, size - 1)


class _ThreadingHttpServer(ThreadingHTTPServer):
    """跳过 `HTTPServer.server_bind()` 里的 `socket.getfqdn()`。

    该方法会对监听地址做反向 DNS，某些网络环境下要等 5~10s（实测 0.0.0.0 就卡了 10s），
    会让 `--music` 启动变慢、横幅迟迟不出来；这里只保留 server_name/port 的本地赋值。
    """

    daemon_threads = True
    allow_reuse_address = True

    def server_bind(self) -> None:
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = host
        self.server_port = port


class MusicHttpServer:
    """守护线程上的只读 HTTP 服务（mock 退出即随进程结束）。"""

    def __init__(self, files: dict[str, bytes], host: str = "0.0.0.0", port: int = 0) -> None:
        self._files = dict(files)
        self._host = host or "0.0.0.0"
        self._port = int(port)
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> int:
        httpd = _ThreadingHttpServer((self._host, self._port), _MusicRequestHandler)
        httpd.music_files = self._files  # type: ignore[attr-defined]
        self._httpd = httpd
        self._port = httpd.server_address[1]
        self._thread = threading.Thread(
            target=httpd.serve_forever, name="mock-music-http", daemon=True
        )
        self._thread.start()
        LOG.info("音乐 HTTP 服务已启动：http://%s:%d（%d 个文件）", self._host, self._port, len(self._files))
        return self._port

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    @property
    def port(self) -> int:
        return self._port


# ============================================================================
# 4. MusicService：装配 + 面向会话的便捷方法
# ============================================================================


def _demo_mp3() -> tuple[bytes, int]:
    """内嵌真实 MP3（mock_fixtures）+ 重复拼帧，返回 (字节, 时长 ms)。

    MP3 帧自包含，整帧重复拼接仍是可解码的合法流，因此不引入 lameenc/av 依赖。
    """
    from mock_fixtures import embedded_real_mp3_frames

    frames = embedded_real_mp3_frames()
    body = b"".join(frame.payload for frame in frames) * _DEMO_REPEATS
    duration_ms = int(sum(frame.duration_ms for frame in frames) * _DEMO_REPEATS)
    return body, duration_ms


class MusicSearchHttp:
    """通用搜索适配器：把 mock 接到**任意**一个返回 JSON 的搜索服务上。

    契约（`--music-search-url`，`{query}` 会被 URL 编码后替换；没写占位符时自动补 `?q=`）：

        GET http://127.0.0.1:9009/search?q=晴天
        → {"tracks":[{"title":"晴天","artist":"周杰伦","url":"http://.../x.mp3",
                      "duration_ms":269000,"keywords":["周杰伦","晴天"]}]}

    也接受裸数组 `[...]` 或 `{"data":[...]}`；取第一条 URL 合法的记录。
    失败（超时/非 2xx/JSON 坏/没有合法 URL）只 WARN 并返回 None，
    由 `MusicService.track_for()` 回退到本地曲库——不打断本轮对话。

    注意：这是**通用适配器**，不含任何站点专用的签名/反爬实现；接哪个源由部署方决定。
    """

    def __init__(self, url_template: str, timeout_s: float = 3.0) -> None:
        if not _valid_url(url_template):
            raise ValueError(f"--music-search-url 必须是 http(s)：{url_template!r}")
        self.url_template = url_template
        self.timeout_s = timeout_s

    def _url(self, query: str) -> str:
        encoded = quote(query, safe="")
        if "{query}" in self.url_template:
            return self.url_template.replace("{query}", encoded)
        sep = "&" if "?" in self.url_template else "?"
        return f"{self.url_template}{sep}q={encoded}"

    def search_tracks(self, query: str, limit: int = 5) -> list[MusicTrack]:
        """返回最多 `limit` 条可用结果；失败/无结果返回空列表（调用方回退曲库）。"""
        url = self._url(query)
        try:
            req = urllib.request.Request(
                url,
                headers={"Accept": "application/json", "User-Agent": "esp-ai-mock/1.0"},
            )
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            LOG.warning("音乐搜索失败（%s）：%s", _redact(url), exc)
            return []

        # 注意：{"tracks": []} 里的空数组是 falsy，不能写 `data.get("tracks") or ...`，
        # 否则空结果会被误判成"格式不对"（2026-11-10 真机日志踩到）。
        if isinstance(data, dict):
            items = data["tracks"] if "tracks" in data else data.get("data")
        else:
            items = data
        if not isinstance(items, list):
            LOG.warning("音乐搜索返回格式不对（需要 tracks/data 数组）：%s", _redact(url))
            return []
        tracks: list[MusicTrack] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            raw_url = str(item.get("url") or "")
            if not _valid_url(raw_url):
                continue
            keywords = item.get("keywords") or ()
            if isinstance(keywords, str):
                keywords = (keywords,)
            tracks.append(MusicTrack(
                title=str(item.get("title") or query),
                url=raw_url,
                duration_ms=int(item.get("duration_ms") or 0),
                artist=str(item.get("artist") or ""),
                keywords=tuple(str(k) for k in keywords) or (query,),
            ))
            if len(tracks) >= max(1, limit):
                break
        if not tracks:
            LOG.warning("音乐搜索无可用结果（query=%r）：%s", query, _redact(url))
        return tracks

    def search(self, query: str) -> MusicTrack | None:
        tracks = self.search_tracks(query, limit=1)
        return tracks[0] if tracks else None


class MusicService:
    """mock 侧音乐能力；`MockServer` 在 `--music` 时创建一个实例。"""

    DEMO_PATH = "/media/demo.mp3"

    def __init__(
        self,
        parser: MusicIntentParser | None = None,
        catalog: MusicCatalog | None = None,
        http_port: int = 0,
        need_demo: bool = False,
        search: MusicSearchHttp | None = None,
    ) -> None:
        self.parser = parser or MusicIntentParser()
        self.catalog = catalog or MusicCatalog()
        self.search = search
        self._http_port_req = int(http_port)
        self._need_demo = need_demo
        self.http: MusicHttpServer | None = None
        self.demo_url = ""

    # ---- 构造 ----

    @classmethod
    def from_config(cls, cfg: Any) -> "MusicService | None":
        """按 Config 建服务；`--music` 与 `--scenario music` 都会启用。"""
        if not getattr(cfg, "music", False) and getattr(cfg, "scenario", "normal") != "music":
            return None

        catalog = MusicCatalog()
        if getattr(cfg, "music_catalog", ""):
            catalog = MusicCatalog.from_json(cfg.music_catalog)
        if getattr(cfg, "music_url", ""):
            title = (getattr(cfg, "music_title", "") or
                     Path(urlsplit(cfg.music_url).path).stem or "远程曲目")
            catalog.add(MusicTrack(title=title, url=cfg.music_url, duration_ms=0,
                                   keywords=(title,)))
            LOG.info("固定曲目：%s → %s（duration_ms=0 表示直播流/时长未知）",
                     title, _redact(cfg.music_url))
        search = None
        if getattr(cfg, "music_search_url", ""):
            timeout_s = max(0.2, getattr(cfg, "music_search_timeout_ms", 3000) / 1000)
            search = MusicSearchHttp(cfg.music_search_url, timeout_s=timeout_s)
            LOG.info("音乐搜索源已启用：%s（超时 %.1fs，失败回退曲库）",
                     _redact(cfg.music_search_url), timeout_s)

        # 有搜索源时不必再起内置示例曲目（搜不到会回退曲库；曲库空则本轮给"没找到"）
        need_demo = len(catalog) == 0 and search is None
        service = cls(catalog=catalog, http_port=getattr(cfg, "music_http_port", 0),
                      need_demo=need_demo, search=search)
        LOG.info(
            "音乐(M1)已启用：曲库 %d 首%s%s",
            len(catalog),
            "、搜索源 1 个" if search is not None else "",
            "（无曲目 → 启动内置 HTTP 示例曲目）" if need_demo else "",
        )
        return service

    # ---- 生命周期 ----

    def start(self, url_host: str, bind_host: str = "0.0.0.0") -> None:
        """启动内置 HTTP（仅在需要示例曲目时）并补上示例曲目的 URL。"""
        if not self._need_demo or self.http is not None:
            return
        body, duration_ms = _demo_mp3()
        self.http = MusicHttpServer({self.DEMO_PATH: body}, host=bind_host, port=self._http_port_req)
        try:
            port = self.http.start()
        except OSError as exc:
            # 端口被占用等：mock 本身继续跑（只是没有示例曲目 URL），不拖垮联调
            LOG.error("音乐 HTTP 服务启动失败（%s）；示例曲目不可播，请改用 --music-url", exc)
            self.http = None
            self._need_demo = False
            return
        self.demo_url = f"http://{url_host}:{port}{self.DEMO_PATH}"
        self.catalog.add(
            MusicTrack(
                title="mock 示例曲目",
                url=self.demo_url,
                duration_ms=duration_ms,
                artist="mock",
                keywords=("示例", "demo", "测试"),
            )
        )
        LOG.info("示例曲目可播 URL：%s（%d B / %.1fs）", self.demo_url, len(body), duration_ms / 1000)

    def stop(self) -> None:
        if self.http is not None:
            self.http.stop()
            self.http = None

    # ---- 会话调用 ----

    def search_tracks(self, query: str, limit: int = 5) -> list[MusicTrack]:
        if self.search is None:
            return []
        return self.search.search_tracks(query, limit=limit)

    def track_for(self, intent: MusicIntent) -> tuple[MusicTrack | None, bool]:
        # 点歌：先问搜索源（可能在网络上，调用方用 to_thread 包住），失败/无结果回退曲库
        if intent.action == ACTION_PLAY and intent.query and self.search is not None:
            track = self.search.search(intent.query)
            if track is not None:
                return track, True
        if intent.action == ACTION_NEXT:
            track = self.catalog.next_track()
            return track, track is not None
        if intent.action == ACTION_PREV:
            track = self.catalog.prev_track()
            return track, track is not None
        return self.catalog.resolve(intent.query)

    def confirm_text(self, action: str, track: MusicTrack | None) -> str:
        """固定确认语：控制指令不进 LLM，保证「说暂停 0.3s 内响应」的口径。"""
        title = track.title if track is not None else ""
        if action == ACTION_PLAY:
            return f"好的，为你播放《{title}》。" if title else "好的，马上为你播放。"
        if action == ACTION_NEXT:
            return f"好的，下一首《{title}》。" if title else "好的，已切到下一首。"
        if action == ACTION_PREV:
            return f"好的，上一首《{title}》。" if title else "好的，已回到上一首。"
        if action == ACTION_PAUSE:
            return "好的，已暂停。"
        if action == ACTION_RESUME:
            return "好的，继续播放。"
        if action == ACTION_STOP:
            return "好的，已停止播放。"
        return "好的。"

    def banner_line(self) -> str:
        parts = [f"曲库 {len(self.catalog)} 首"]
        if self.demo_url:
            parts.append(f"示例 {self.demo_url}")
        elif self.catalog.tracks:
            parts.append("、".join(track.url for track in self.catalog.tracks[:2]))
        if self.search is not None:
            parts.append(f"搜索 {_redact(self.search.url_template)}")
        return "音乐(M1): 开 | " + " | ".join(parts)


def build_control_event(mid: int, action: str) -> dict[str, Any]:
    """下行控制事件：暂停/继续/停止（下一首/上一首走新的 music_track）。"""
    if action not in CONTROL_ACTIONS:
        raise ValueError(f"music_control 不支持 action={action!r}（只允许 {CONTROL_ACTIONS}）")
    return {"t": "music_control", "mid": mid & 0xFFFF, "action": action}
