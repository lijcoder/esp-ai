#!/usr/bin/env python3
"""music_tools —— 音乐工具层（LLM tool calling / MCP 共用的唯一执行入口）。

## 定位（2026-11-12 架构调整）

旧实现是「ASR 文本 → 关键词规则表 → 事件」，命中率低且越加越脆。现在改成：

    ASR 文本 ──▶ LLM/Hermes（决定调用哪个工具）──▶ tool_calls / mcp_call
                                                        │
                                     本文件 execute_tool()（唯一执行入口）
                                                        │
                                    play/next/prev → music_track（带 URL）
                                    pause/resume/stop → music_control

**端侧（ESP32）仍然零改动**：它只认 `music_track` / `music_control`，不解析文本、
不解析工具调用。工具执行在云端（mock 或真实网关），URL 由曲库给，永远不出现在
模型可见的文本/工具结果里。

## 两条接入路径共用同一份语义

1. **MCP**（生产/Hermes）：`tools/music_mcp_server.py` 把 8 个工具暴露给 agent，
   工具结果只含 `track_id/title/artist/duration_ms`（**不含 URL**）；
2. **Responses API inline tools**（本地 mock 测试）：`tools/responses_client.py`
   把 `tool_definitions()` 作为 `tools` 发给网关，拿回 `function_call` 后本地执行。

两条路径的入参、返回结构、状态机完全一致，所以 mock 上验过的行为可以原样搬到
Hermes（只换“谁决定调用”，不换“怎么执行”）。

## 约定

- 工具名统一 `music_*`；也接受去掉前缀的 `play/pause/...`（模型偶尔会这么写）。
- 返回 JSON 里**禁止出现 url 字段**（有单测守着）；客户端用 `track_id` 回到曲库取 URL。
- 幂等操作（已暂停时再 pause、空闲时 stop）返回 `ok=true, changed=false`，
  调用方只在 `changed=true` 时下发事件，避免重复报文。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from mock_music import (
    ACTION_NEXT,
    ACTION_PAUSE,
    ACTION_PLAY,
    ACTION_PREV,
    ACTION_RESUME,
    ACTION_STOP,
    MusicService,
    MusicTrack,
    track_id_of,
)

# 播放器状态（mock 侧口径；端侧还有 STOPPED/ERROR，由设备自己维护）
STATE_IDLE = "idle"
STATE_PLAYING = "playing"
STATE_PAUSED = "paused"

TOOL_SEARCH = "music_search"
TOOL_PLAY = "music_play"
TOOL_PAUSE = "music_pause"
TOOL_RESUME = "music_resume"
TOOL_NEXT = "music_next"
TOOL_PREV = "music_prev"
TOOL_STOP = "music_stop"
TOOL_STATUS = "music_status"

TOOL_NAMES = (
    TOOL_SEARCH,
    TOOL_PLAY,
    TOOL_PAUSE,
    TOOL_RESUME,
    TOOL_NEXT,
    TOOL_PREV,
    TOOL_STOP,
    TOOL_STATUS,
)

# 一轮只允许执行一个“会改播放状态”的工具：模型偶尔会同时调 pause+stop，
# 多下发一条会让设备状态跳变（见 ARCH §6.3 的音乐事件语义）。
MUTATING_TOOLS = (TOOL_PLAY, TOOL_PAUSE, TOOL_RESUME, TOOL_NEXT, TOOL_PREV, TOOL_STOP)


# ============================================================================
# 状态 + 执行器
# ============================================================================


@dataclass
class Command:
    """一次工具调用的结果；`to_result()` 是给模型看的 JSON（不含 URL）。"""

    ok: bool
    action: str
    changed: bool = False
    state: str = STATE_IDLE
    track: MusicTrack | None = None
    matched: bool = True
    message: str = ""
    items: list[dict[str, Any]] = field(default_factory=list)

    def to_result(self, include_url: bool = False) -> dict[str, Any]:
        result: dict[str, Any] = {
            "ok": self.ok,
            "action": self.action,
            "changed": self.changed,
            "state": self.state,
            "matched": self.matched,
            "message": self.message,
        }
        if self.track is not None:
            result["track"] = track_public(self.track, include_url=include_url)
        if self.items:
            result["tracks"] = self.items
        return result


def is_playable_url(url: str) -> bool:
    """设备侧只接受 http(s) 且长度 < 320（端侧 MUSIC_URL_MAX 含 NUL）。"""
    return (
        isinstance(url, str)
        and 0 < len(url) < 320
        and url.startswith(("http://", "https://"))
    )


def track_public(track: MusicTrack, include_url: bool = False) -> dict[str, Any]:
    """给模型/客户端看的曲目信息。

    `include_url=False`（默认）只给 track_id，客户端回曲库取 URL；
    `include_url=True` 时额外带 url——MCP 结果走这条路，客户端可不再依赖曲库。
    是否带 URL 由调用方决定（MCP 服务带，内部执行器不带）。
    """
    data: dict[str, Any] = {
        "track_id": track_id_of(track.url),
        "title": track.title,
        "artist": track.artist,
        "duration_ms": int(track.duration_ms),
    }
    if include_url:
        data["url"] = track.url
    return data


class MusicSession:
    """服务端权威播放状态（每台设备一份）；不产生任何 I/O。

    `MusicService` 负责曲库/搜索/HTTP，本类只负责“点歌 → 当前曲目/队列/暂停态”的状态机。
    """

    def __init__(self, service: MusicService) -> None:
        self.service = service
        self.state = STATE_IDLE
        self.current: MusicTrack | None = None
        self.queue: list[MusicTrack] = []
        self.index = -1
        self.last_error = ""

    # ---- 查询 ----

    def describe(self) -> dict[str, Any]:
        """注入 system prompt 的一小段权威状态（避免模型凭空猜“有没有在播”）。"""
        return {
            "state": self.state,
            "current": track_public(self.current) if self.current is not None else None,
            "queue_len": len(self.queue),
        }

    # ---- 工具动作 ----

    def search(self, query: str, limit: int = 5) -> Command:
        limit = max(1, min(int(limit or 5), 10))
        tracks = self.service.search_many(query, limit=limit)
        if not tracks:
            self.queue = []
            self.index = -1
            return Command(ok=False, action="search", state=self.state,
                           matched=False, message=f"没有找到与 {query!r} 相关的曲目")
        self.queue = tracks
        self.index = 0
        return Command(ok=True, action="search", state=self.state, matched=True,
                       items=[track_public(t) for t in tracks],
                       message=f"找到 {len(tracks)} 首候选")

    def play(self, query: str = "", track_id: str = "", index: int | None = None) -> Command:
        track: MusicTrack | None = None
        matched = True
        from_queue = False
        if track_id:
            track, matched, from_queue = self._by_id(track_id)
            if track is None:
                return Command(ok=False, action=ACTION_PLAY, state=self.state,
                               message=f"未知的 track_id={track_id!r}，请先 music_search")
        elif index is not None:
            track, from_queue = self._by_index(int(index))
            if track is None:
                return Command(ok=False, action=ACTION_PLAY, state=self.state,
                               message=f"index={index} 超出候选范围（候选 {len(self.queue)} 首）")
        elif query:
            # 点歌：搜索源优先（多结果时把队列留给“下一首”），失败回退本地曲库
            if self.service.search is not None:
                tracks = self.service.search_many(query, limit=5)
                if tracks:
                    self.queue, self.index = tracks, 0
                    track, from_queue = tracks[0], True
                else:
                    track, matched = self.service.catalog.resolve(query)
            else:
                track, matched = self.service.resolve_query(query, search_first=False)
        else:
            track, from_queue = self._current_or_first()

        if track is None:
            return Command(ok=False, action=ACTION_PLAY, state=self.state,
                           matched=False, message="曲库为空，没有可播放的曲目")

        if not from_queue:
            # 新查询可能和上一次搜索结果无关：清掉旧队列，避免“下一首”跳到旧结果
            self.queue = []
            self.index = -1
        changed = self.current is None or track.url != self.current.url
        self.current = track
        self.state = STATE_PLAYING
        self._align_cursor(track)
        self.service.last_url = track.url
        note = "" if matched else "（曲库未匹配，回退到当前曲目）"
        return Command(ok=True, action=ACTION_PLAY, changed=changed, state=self.state,
                       track=track, matched=matched,
                       message=f"开始播放《{track.title}》{note}")

    def apply_status(self, state: str) -> None:
        """把设备上报的 music_status 同步到本会话（设备可能因语音/按键自行暂停）。

        mock 的 MusicSession 是“云端视角”；设备 auto-pause / stop 不会经过工具调用，
        不同步的话 system prompt 会告诉模型“还在播放”，用户说“继续”时模型会去 play。
        """
        if state == "playing":
            self.state = STATE_PLAYING
        elif state == "paused":
            self.state = STATE_PAUSED
        elif state in ("stopped", "error", "idle"):
            self.state = STATE_IDLE
            self.current = None
            self.queue = []
            self.index = -1

    def pause(self, lenient: bool = False) -> Command:
        if self.state == STATE_PAUSED:
            return Command(ok=True, action=ACTION_PAUSE, changed=False,
                           state=self.state, track=self.current, message="已经是暂停状态")
        if self.state != STATE_PLAYING or self.current is None:
            if lenient:
                # MCP 服务只表达“用户要暂停”；真实状态由持有设备的客户端决定
                self.state = STATE_PAUSED
                return Command(ok=True, action=ACTION_PAUSE, changed=True,
                               state=self.state, track=self.current,
                               message="已请求暂停")
            return Command(ok=False, action=ACTION_PAUSE, state=self.state,
                           message="当前没有在播放，无法暂停")
        self.state = STATE_PAUSED
        return Command(ok=True, action=ACTION_PAUSE, changed=True, state=self.state,
                       track=self.current, message=f"已暂停《{self.current.title}》")

    def resume(self, lenient: bool = False) -> Command:
        if self.state == STATE_PLAYING:
            return Command(ok=True, action=ACTION_RESUME, changed=False,
                           state=self.state, track=self.current, message="已经在播放")
        if self.state != STATE_PAUSED or self.current is None:
            if lenient:
                self.state = STATE_PLAYING
                return Command(ok=True, action=ACTION_RESUME, changed=True,
                               state=self.state, track=self.current,
                               message="已请求继续")
            return Command(ok=False, action=ACTION_RESUME, state=self.state,
                           message="没有暂停中的曲目；要听歌请调用 music_play")
        self.state = STATE_PLAYING
        return Command(ok=True, action=ACTION_RESUME, changed=True, state=self.state,
                       track=self.current, message=f"继续播放《{self.current.title}》")

    def next(self) -> Command:
        return self._step(+1)

    def prev(self) -> Command:
        return self._step(-1)

    def stop(self, lenient: bool = False) -> Command:
        if self.state == STATE_IDLE and self.current is None:
            return Command(ok=True, action=ACTION_STOP, changed=lenient,
                           state=self.state, message="当前没有在播放")
        old = self.current
        self.state = STATE_IDLE
        self.current = None
        self.queue = []
        self.index = -1
        title = f"《{old.title}》" if old is not None else ""
        return Command(ok=True, action=ACTION_STOP, changed=True, state=self.state,
                       message=f"已停止播放{title}")

    def status(self) -> Command:
        return Command(ok=True, action="status", state=self.state, track=self.current,
                       items=[track_public(t) for t in self.queue],
                       message=f"当前状态 {self.state}")

    # ---- 远程结果（MCP/Responses 的 function_call_output） ----

    def apply_remote(self, payload: dict[str, Any]) -> Command | None:
        """用工具返回的结果更新本地状态；返回 None 表示无法识别、由调用方本地执行。

        URL 只来自**工具结果**（MCP 服务产出），不接受模型在 arguments 里塞的 URL：
        模型可以看到工具结果，但无法伪造服务端返回。
        `changed` 一律按**本地**状态算（MCP 进程的状态可能来自别的会话/更早的测试）。
        """
        if not isinstance(payload, dict):
            return None
        action = str(payload.get("action") or "")
        message = str(payload.get("message") or "")
        track = _track_from_public(payload.get("track"))

        if action == "search":
            tracks = payload.get("tracks")
            parsed = [t for t in (_track_from_public(item) for item in tracks or [])
                      if t is not None]
            if not parsed:
                return None
            self.queue, self.index = parsed, 0
            return Command(ok=True, action="search", state=self.state, matched=True,
                           items=[track_public(t) for t in parsed],
                           message=message or f"找到 {len(parsed)} 首候选")

        if action == "status":
            return self.status()

        if action in (ACTION_PLAY, ACTION_NEXT, ACTION_PREV):
            if track is None:
                return None
            changed = self.current is None or self.current.url != track.url
            self.current = track
            self.state = STATE_PLAYING
            self.queue = []
            self.index = -1
            self.service.last_url = track.url
            return Command(ok=True, action=action, changed=changed, state=self.state,
                           track=track, matched=bool(payload.get("matched", True)),
                           message=message or f"开始播放《{track.title}》")

        if action == ACTION_PAUSE:
            if self.state == STATE_PAUSED:
                return Command(ok=True, action=action, changed=False, state=self.state,
                               track=self.current, message=message or "已经是暂停状态")
            if self.state != STATE_PLAYING:
                return None
            self.state = STATE_PAUSED
            return Command(ok=True, action=action, changed=True, state=self.state,
                           track=self.current, message=message or "已暂停")

        if action == ACTION_RESUME:
            if self.state != STATE_PAUSED:
                return None
            self.state = STATE_PLAYING
            return Command(ok=True, action=action, changed=True, state=self.state,
                           track=self.current, message=message or "继续播放")

        if action == ACTION_STOP:
            if self.state == STATE_IDLE and self.current is None:
                return Command(ok=True, action=action, changed=False, state=self.state,
                               message=message or "当前没有在播放")
            self.state = STATE_IDLE
            self.current = None
            self.queue = []
            self.index = -1
            return Command(ok=True, action=action, changed=True, state=self.state,
                           message=message or "已停止播放")
        return None

    # ---- 内部 ----

    def _step(self, direction: int) -> Command:
        action = ACTION_NEXT if direction > 0 else ACTION_PREV
        track: MusicTrack | None = None
        if len(self.queue) > 1:
            self.index = (self.index + direction) % len(self.queue)
            track = self.queue[self.index]
        else:
            if self.current is not None:
                self._align_cursor(self.current)
            track = (self.service.catalog.next_track() if direction > 0
                     else self.service.catalog.prev_track())
            if track is not None:
                self.queue = []
                self.index = -1
        if track is None:
            return Command(ok=False, action=action, state=self.state,
                           message="没有可切换的曲目（曲库为空）")
        self.current = track
        self.state = STATE_PLAYING
        self.service.last_url = track.url
        return Command(ok=True, action=action, changed=True, state=self.state,
                       track=track, message=f"切换到《{track.title}》")

    def _current_or_first(self) -> tuple[MusicTrack | None, bool]:
        if self.queue and 0 <= self.index < len(self.queue):
            return self.queue[self.index], True
        track, _ = self.service.catalog.resolve("")
        return track, False

    def _by_id(self, track_id: str) -> tuple[MusicTrack | None, bool, bool]:
        """返回 (曲目, matched, 是否来自候选队列)。"""
        for pos, track in enumerate(self.queue):
            if track_id_of(track.url) == track_id:
                self.index = pos
                return track, True, True
        track = self.service.resolve_id(track_id)
        return track, track is not None, False

    def _by_index(self, index: int) -> tuple[MusicTrack | None, bool]:
        if 0 <= index < len(self.queue):
            self.index = index
            return self.queue[index], True
        tracks = self.service.catalog.tracks
        if 0 <= index < len(tracks):
            return tracks[index], False
        return None, False

    def _align_cursor(self, track: MusicTrack) -> None:
        self.service.catalog.maybe_reload()
        self.service.catalog.seek(track.url)


# ============================================================================
# 参数校验 + 工具分发
# ============================================================================


def _track_from_public(data: Any) -> MusicTrack | None:
    """工具结果里的 track 对象 → MusicTrack；URL 非法则丢弃。"""
    if not isinstance(data, dict):
        return None
    url = str(data.get("url") or "")
    if not is_playable_url(url):
        return None
    return MusicTrack(
        title=str(data.get("title") or "远程曲目"),
        url=url,
        duration_ms=int(data.get("duration_ms") or 0),
        artist=str(data.get("artist") or ""),
    )


def _as_str(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _as_optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_int(value: Any, default: int) -> int:
    parsed = _as_optional_int(value)
    return default if parsed is None else parsed


def canonical_tool_name(name: str) -> str:
    """`play` / `music.play` / `mcp__music__music_play` 统一成 `music_play`。"""
    raw = (name or "").strip()
    if raw.startswith("mcp__"):  # Hermes/MCP 命名空间：mcp__<server>__<tool>
        parts = raw.split("__", 2)
        if len(parts) == 3 and parts[2]:
            raw = parts[2]
    if raw.startswith("music."):
        raw = raw.replace("music.", "music_", 1)
    if raw in TOOL_NAMES:
        return raw
    prefixed = "music_" + raw.lstrip("_")
    return prefixed if prefixed in TOOL_NAMES else raw


def execute_tool(session: MusicSession, name: str, arguments: dict[str, Any] | None,
                 lenient: bool = False) -> Command:
    """唯一的工具执行入口（MCP / Responses 共用）；arg 容错为 dict。"""
    args = arguments if isinstance(arguments, dict) else {}
    tool = canonical_tool_name(name)
    if tool == TOOL_SEARCH:
        return session.search(_as_str(args.get("query")), _as_int(args.get("limit"), 5))
    if tool == TOOL_PLAY:
        return session.play(query=_as_str(args.get("query")),
                            track_id=_as_str(args.get("track_id")),
                            index=_as_optional_int(args.get("index")))
    if tool == TOOL_PAUSE:
        return session.pause(lenient)
    if tool == TOOL_RESUME:
        return session.resume(lenient)
    if tool == TOOL_NEXT:
        return session.next()
    if tool == TOOL_PREV:
        return session.prev()
    if tool == TOOL_STOP:
        return session.stop(lenient)
    if tool == TOOL_STATUS:
        return session.status()
    return Command(ok=False, action=tool or "unknown", state=session.state,
                   message=f"未知工具 {name!r}（可用：{', '.join(TOOL_NAMES)}）")


# ============================================================================
# 工具定义（Responses API inline tools / MCP schema 共用）
# ============================================================================

_EMPTY_PARAMS: dict[str, Any] = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
}

TOOL_SPECS: dict[str, dict[str, Any]] = {
    TOOL_SEARCH: {
        "description": (
            "在曲库里搜索歌曲，返回候选（track_id/标题/歌手/时长）。"
            "只在用户点歌但你不确定曲名、或需要让用户/自己挑选时使用；"
            "已经知道歌名时直接用 music_play{query}。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "歌名或歌手关键词"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 10,
                          "description": "返回候选数，默认 5"},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    TOOL_PLAY: {
        "description": (
            "点歌并开始播放。三选一：query（歌名/歌手）、track_id（music_search 返回的 id）、"
            "index（候选序号）。都不传表示“随便放一首/接着放当前队列”。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "歌名或歌手关键词，例如“晴天”"},
                "track_id": {"type": "string", "description": "music_search 返回的 track_id"},
                "index": {"type": "integer", "minimum": 0,
                          "description": "候选列表下标（0 起）"},
            },
            "additionalProperties": False,
        },
    },
    TOOL_PAUSE: {
        "description": "暂停当前播放（保留进度，之后可以 music_resume 从原位置继续）。",
        "parameters": _EMPTY_PARAMS,
    },
    TOOL_RESUME: {
        "description": "从暂停点继续播放。用户说“继续/接着放”时必须调用它（即使不确定设备是否在暂停态，由设备决定）。",
        "parameters": _EMPTY_PARAMS,
    },
    TOOL_NEXT: {
        "description": "切到下一首。用户说“下一首/换一首/切歌/换个带劲的”时使用。",
        "parameters": _EMPTY_PARAMS,
    },
    TOOL_PREV: {
        "description": "切回上一首。",
        "parameters": _EMPTY_PARAMS,
    },
    TOOL_STOP: {
        "description": "停止播放并丢弃当前曲目（不是暂停）。用户说“关掉/别放了/停止播放”时使用。",
        "parameters": _EMPTY_PARAMS,
    },
    TOOL_STATUS: {
        "description": "查询当前播放状态/曲目/进度（用户问“现在放的是什么/在播吗”时使用）。",
        "parameters": _EMPTY_PARAMS,
    },
}


def tool_definitions() -> list[dict[str, Any]]:
    """OpenAI Responses API 的 inline function tools（无 MCP 时的本地测试路径）。"""
    return [
        {
            "type": "function",
            "name": name,
            "description": spec["description"],
            "parameters": spec["parameters"],
            "strict": False,
        }
        for name, spec in TOOL_SPECS.items()
    ]


def tool_names() -> list[str]:
    return list(TOOL_NAMES)


# ============================================================================
# 给 mock/Hermes 的系统提示（状态注入）
# ============================================================================

MUSIC_SYSTEM_PROMPT = """你是语音助手的音乐控制决策层。
当用户的话与音乐有关（点歌、暂停、继续、下一首、上一首、停止、换一首、问正在放什么）时，
必须调用对应的音乐工具，不要只口头答应。

规则：
- 点歌用 music_play{query=歌名或歌手}；不确定曲目时先 music_search。
- “继续/接着放”用 music_resume；“暂停”用 music_pause；“关掉/别放了”用 music_stop。
- “下一首/换一首”用 music_next；“上一首”用 music_prev。
- 当前没有曲目（state=idle）时不要调用 music_next/music_prev/music_pause/music_resume。
- state=paused 时，用户说“继续/接着放”必须调用 music_resume，不要用 music_play 重新点歌。
- 用户只是闲聊、问天气、问知识时，**不要调用任何音乐工具**，按普通语音助手正常回答。
- 工具返回 ok=false 时按 message 向用户解释，不要编造播放结果。

当前播放状态（服务端权威，不要自行猜测）：
{state}
"""


def system_prompt_for(session: MusicSession) -> str:
    return MUSIC_SYSTEM_PROMPT.replace(
        "{state}", json.dumps(session.describe(), ensure_ascii=False)
    )
