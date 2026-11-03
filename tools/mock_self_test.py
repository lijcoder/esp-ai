#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["websockets>=14,<19"]
# ///
"""mock_self_test —— ws_mock_server 的进程内自测（19 项，不需要硬件、不改设备）。

入口仍是 `uv run tools/ws_mock_server.py --self-test`；也可 `uv run tools/mock_self_test.py`
单独跑。每个用例在进程内起 MockServer + 假客户端，只断言 mock 自己的协议行为。
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import socket
import sys
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from mock_cli import parse_args
from mock_core import (
    CLOSE_BAD_TOKEN,
    ERROR_STAGES,
    ERROR_TTS_FRAMES,
    FRAME_VER,
    MAGIC,
    TTS_BAD_CODEC,
    TTS_CODEC,
    TTS_SAMPLE_RATE,
    TYPE_AUDIO_DOWN,
    TYPE_AUDIO_UP,
    Config,
    build_frame,
    parse_frame,
    parse_mp3_frames,
)
from mock_fixtures import (
    SELFTEST_MP3_BYTES,
    SELFTEST_MP3_FRAMES,
    write_selftest_mp3,
)
from mock_server import MockServer


async def _start(cfg: Config) -> tuple[MockServer, str]:
    server = MockServer(cfg)
    port = await server.start()
    url = f"ws://127.0.0.1:{port}/v1/voice?device_id=dev1&token=t&ver=1"
    return server, url


async def _wait_closed(server: MockServer, count: int, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while len(server.history) < count:
        if time.monotonic() > deadline:
            raise AssertionError(f"等待会话关闭超时：已关闭 {len(server.history)}，期望 ≥{count}")
        await asyncio.sleep(0.01)


async def _recv_json(ws: Any, timeout: float = 3.0) -> dict[str, Any]:
    raw = await asyncio.wait_for(ws.recv(), timeout)
    if isinstance(raw, (bytes, bytearray, memoryview)):
        raise AssertionError(f"期望 JSON 文本帧，却收到二进制 {len(raw)}B")
    return json.loads(raw)


async def _recv_until(ws: Any, stop_event: str, timeout: float = 3.0) -> list[tuple[str, Any]]:
    collected: list[tuple[str, Any]] = []
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AssertionError(f"等待 {stop_event} 超时，已收到 {[m for _, m in collected]}")
        raw = await asyncio.wait_for(ws.recv(), remaining)
        if isinstance(raw, (bytes, bytearray, memoryview)):
            collected.append(("bin", bytes(raw)))
            continue
        msg = json.loads(raw)
        collected.append(("json", msg))
        if msg.get("t") == stop_event:
            return collected

async def _recv_one(ws: Any, deadline: float) -> tuple[float, str, Any]:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise AssertionError("等待下行超时")
    raw = await asyncio.wait_for(ws.recv(), remaining)
    now = time.monotonic()
    if isinstance(raw, (bytes, bytearray, memoryview)):
        return now, "bin", bytes(raw)
    return now, "json", json.loads(raw)


async def _collect_round(
    ws: Any, sid: int, trailing_bins: int = 0, timeout: float = 10.0
) -> list[tuple[float, str, Any]]:
    """客户端侧跑一轮：hello / audio_start / 上行 3 帧 / audio_end，收到 tts_end。

    返回 [(接收时刻, "bin"|"json", 内容)]；`trailing_bins>0` 时继续收 tts_end 之后到达的
    二进制帧（--tts-late-bin 用来验证"收尾后仍有窗口外帧"）。
    """
    await ws.send(json.dumps({"t": "hello", "device_id": "dev1", "ver": 1}))
    await ws.send(json.dumps({"t": "audio_start", "sid": sid}))
    for index in range(3):
        await ws.send(build_frame(TYPE_AUDIO_UP, index, b"\x01" * 40))
    await ws.send(json.dumps({"t": "audio_end", "sid": sid}))
    collected: list[tuple[float, str, Any]] = []
    deadline = time.monotonic() + timeout
    while True:
        item = await _recv_one(ws, deadline)
        collected.append(item)
        if item[1] == "json" and item[2].get("t") == "tts_end":
            break
    for _ in range(trailing_bins):
        item = await _recv_one(ws, deadline)
        assert item[1] == "bin", f"tts_end 后期望窗口外二进制帧，实际收到 {item[2]!r}"
        collected.append(item)
    return collected


async def _collect_until(
    ws: Any, sid: int, stop_event: str, timeout: float = 10.0
) -> list[tuple[float, str, Any]]:
    """跑一轮上行后收集下行，直到出现 JSON `t == stop_event`（error 路径没有 tts_end）。

    `sid` 由假客户端给出（真实端侧由 voice_session 分配，mock 原样回填）。
    """
    await ws.send(json.dumps({"t": "hello", "device_id": "dev1", "ver": 1}))
    await ws.send(json.dumps({"t": "audio_start", "sid": sid}))
    for index in range(3):
        await ws.send(build_frame(TYPE_AUDIO_UP, index, b"\x01" * 40))
    await ws.send(json.dumps({"t": "audio_end", "sid": sid}))
    collected: list[tuple[float, str, Any]] = []
    deadline = time.monotonic() + timeout
    while True:
        item = await _recv_one(ws, deadline)
        collected.append(item)
        if item[1] == "json" and item[2].get("t") == stop_event:
            return collected

async def _t_hello(cfg: Config) -> None:
    server, url = await _start(cfg)
    try:
        async with connect(url) as ws:
            await ws.send(json.dumps({"t": "hello", "device_id": "dev1", "ver": 1}))
            # ARCH §6.1（T1.4 定稿）：hello 之后必须回 hello_ack，端侧靠它判断握手成功
            ack = await _recv_json(ws, timeout=1.0)
            assert ack.get("t") == "hello_ack", f"期望 hello_ack，收到 {ack}"
            assert ack.get("ver") == 1, f"hello_ack.ver 错误：{ack}"
        await _wait_closed(server, 1)
        session = server.history[-1]
        assert session.stats.hello_seen, "服务端没看到 hello"
        assert session.hello_info.get("device_id") == "dev1", "hello.device_id 解析错误"
        assert session.hello_info.get("ver") == 1, "hello.ver 解析错误"
    finally:
        await server.stop()


async def _t_tcp_nodelay(cfg: Config) -> None:
    """每条 accept 的连接必须开 TCP_NODELAY（否则 Nagle/delayed ACK 攒批→端侧假欠载）。"""
    server, url = await _start(cfg)
    try:
        async with connect(url) as ws:
            # 等 handler 建好 Session（connect 返回时握手已完成，通常立刻就有）
            deadline = time.monotonic() + 1.0
            session = None
            while time.monotonic() < deadline:
                session = next(iter(server.active.values()), None)
                if session is not None:
                    break
                await asyncio.sleep(0.01)
            assert session is not None, "服务端未建立会话（拿不到连接）"
            transport = getattr(session.ws, "transport", None)
            assert transport is not None, "服务端连接没有 transport"
            sock = transport.get_extra_info("socket")
            assert sock is not None, "服务端连接拿不到 socket"
            enabled = sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY)
            # Linux 返回 1；macOS 实测返回非 0（4）即已开启，故只校验「非 0」。
            assert enabled != 0, f"服务端 TCP_NODELAY 未开启（getsockopt={enabled}）"
            # 开启后连接仍可正常收发
            await ws.send(json.dumps({"t": "ping"}))
            msg = await _recv_json(ws, timeout=1.0)
            assert msg.get("t") == "pong", f"期望 pong，收到 {msg}"
    finally:
        await server.stop()


async def _t_ping(cfg: Config) -> None:
    server, url = await _start(cfg)
    try:
        async with connect(url) as ws:
            await ws.send(json.dumps({"t": "ping"}))
            msg = await _recv_json(ws, timeout=1.0)
            assert msg.get("t") == "pong", f"期望 pong，收到 {msg}"
    finally:
        await server.stop()


async def _t_voice(cfg: Config) -> None:
    server, url = await _start(cfg)
    try:
        async with connect(url) as ws:
            await ws.send(json.dumps({"t": "hello", "device_id": "dev1", "ver": 1}))
            await ws.send(json.dumps({"t": "audio_start", "sid": 42}))
            for index in range(3):
                await ws.send(build_frame(TYPE_AUDIO_UP, index, b"\x01" * 40))
            await ws.send(json.dumps({"t": "audio_end", "sid": 42}))
            frames = await _recv_until(ws, "tts_end")
        # hello_ack（T1.4 定稿）不属于语音事件，做 sid/顺序断言前先剔除
        json_msgs = [msg for kind, msg in frames if kind == "json" and msg.get("t") != "hello_ack"]
        assert any(kind == "json" and msg.get("t") == "hello_ack" for kind, msg in frames), "缺少 hello_ack"
        bins = [msg for kind, msg in frames if kind == "bin"]
        types = [msg["t"] for msg in json_msgs]
        assert "asr_partial" in types and "asr_final" in types and "llm_delta" in types, types
        assert types.index("asr_final") < types.index("tts_start") < types.index("tts_end"), types
        assert all(msg.get("sid") == 42 for msg in json_msgs), "存在 sid 未回填的下行事件"
        tts_start = next(msg for msg in json_msgs if msg["t"] == "tts_start")
        assert tts_start["codec"] == TTS_CODEC and tts_start["sr"] == TTS_SAMPLE_RATE, tts_start
        assert len(bins) == cfg.max_tts_frames, f"二进制帧数 {len(bins)} != {cfg.max_tts_frames}"
        for expected_seq, raw in enumerate(bins):
            frame_type, seq, payload = parse_frame(raw)
            assert frame_type == TYPE_AUDIO_DOWN, f"下行帧类型错误 {frame_type}"
            assert seq == expected_seq, f"seq 应为 {expected_seq}，实为 {seq}"
            assert len(payload) == cfg.chunk_bytes, f"payload 长度 {len(payload)} != {cfg.chunk_bytes}"
        await _wait_closed(server, 1)
        assert server.history[-1].stats.frames_up == 3, "上行帧计数错误"
    finally:
        await server.stop()


async def _t_tts_mp3(cfg: Config) -> None:
    path = write_selftest_mp3()
    data = Path(path).read_bytes()
    frames = parse_mp3_frames(data, source="self-test.mp3")
    assert len(frames) == SELFTEST_MP3_FRAMES, f"帧数 {len(frames)} != {SELFTEST_MP3_FRAMES}"
    assert sum(len(frame.payload) for frame in frames) == SELFTEST_MP3_BYTES, "帧字节总和不符"
    assert all(
        frame.payload[0] == 0xFF and (frame.payload[1] & 0xE0) == 0xE0 for frame in frames
    ), "帧头不同步"
    assert all(frame.sample_rate == TTS_SAMPLE_RATE for frame in frames), "采样率应为 24000"
    assert abs(sum(frame.duration_ms for frame in frames) - 528.0) < 0.5, "时长应为 22 帧 × 24ms"
    # 真实 MP3 常带 ID3v2 标签：必须跳过标签后仍解析出同样的帧
    id3 = b"ID3\x04\x00\x00\x00\x00\x00\x64" + b"\x00" * 100 + data
    tagged = parse_mp3_frames(id3, source="self-test+id3")
    assert len(tagged) == len(frames) and tagged[0].payload == frames[0].payload, "ID3v2 跳过失败"

    server, url = await _start(replace(cfg, tts_mp3=path, max_tts_frames=0, tts_pace_pct=100))
    try:
        assert server.tts_mp3_frames is not None, "mock 未加载真实 MP3"
        async with connect(url) as ws:
            messages = await _collect_round(ws, sid=11)
        bins = [(ts, payload) for ts, kind, payload in messages if kind == "bin"]
        assert len(bins) == len(frames), f"下发帧数 {len(bins)} != 素材 {len(frames)}"
        for index, ((_, raw), expected) in enumerate(zip(bins, frames)):
            frame_type, seq, payload = parse_frame(raw)
            assert frame_type == TYPE_AUDIO_DOWN, f"第 {index} 帧类型 {frame_type}"
            assert seq == index, f"seq 应为 {index}，实为 {seq}"
            assert payload == expected.payload, f"第 {index} 帧 payload 与素材帧不一致"
        span = bins[-1][0] - bins[0][0]
        expected_span = sum(frame.duration_ms for frame in frames[:-1]) / 1000.0
        # 本地回环下按帧时长实时发送：允许 0.5~2 倍 + 0.3s 抖动容差
        assert expected_span * 0.5 <= span <= expected_span * 2.0 + 0.3, (
            f"节奏异常：首末帧间隔 {span:.3f}s，期望约 {expected_span:.3f}s（实时节奏）"
        )
        await _wait_closed(server, 1)
        stats = server.history[-1].stats
        assert stats.tts_frames == len(frames), f"tts_frames={stats.tts_frames}"
        assert stats.tts_bytes == sum(len(frame.payload) for frame in frames), (
            f"tts_bytes={stats.tts_bytes}"
        )
        assert stats.late_bins == 0 and stats.tts_stalls == 0
    finally:
        await server.stop()


async def _t_tts_stall(cfg: Config) -> None:
    path = write_selftest_mp3()
    server, url = await _start(
        replace(
            cfg,
            tts_mp3=path,
            max_tts_frames=0,
            tts_pace_pct=100,
            tts_stall_ms=250,
            tts_stall_at=2,
        )
    )
    try:
        frames = server.tts_mp3_frames
        assert frames is not None
        async with connect(url) as ws:
            messages = await _collect_round(ws, sid=12)
        bins = [ts for ts, kind, _ in messages if kind == "bin"]
        assert len(bins) == len(frames), f"stall 不应丢帧：{len(bins)} != {len(frames)}"
        max_gap = max(bins[i + 1] - bins[i] for i in range(len(bins) - 1))
        assert max_gap >= 0.20, f"未观察到停发间隙（最大帧间隔 {max_gap:.3f}s < 0.20s）"
        await _wait_closed(server, 1)
        stats = server.history[-1].stats
        assert stats.tts_stalls == 1, f"stall 计数 {stats.tts_stalls} != 1"
        assert stats.late_bins == 0
    finally:
        await server.stop()


async def _t_tts_late(cfg: Config) -> None:
    path = write_selftest_mp3()
    server, url = await _start(
        replace(cfg, tts_mp3=path, max_tts_frames=5, tts_pace_pct=0, tts_late_bin=True)
    )
    try:
        frames = server.tts_mp3_frames
        assert frames is not None
        async with connect(url) as ws:
            messages = await _collect_round(ws, sid=13, trailing_bins=1)
        bin_indexes = [i for i, (_, kind, _) in enumerate(messages) if kind == "bin"]
        tts_start_index = next(
            i
            for i, (_, kind, obj) in enumerate(messages)
            if kind == "json" and obj.get("t") == "tts_start"
        )
        tts_end_index = next(
            i
            for i, (_, kind, obj) in enumerate(messages)
            if kind == "json" and obj.get("t") == "tts_end"
        )
        assert len(bin_indexes) == 2 + 5, f"窗口外 2 帧 + 正常 5 帧，实收 {len(bin_indexes)}"
        assert bin_indexes[0] < tts_start_index, "tts_start 之前没有窗口外帧"
        assert bin_indexes[-1] > tts_end_index, "tts_end 之后没有窗口外帧"
        for expected_seq, index in enumerate(bin_indexes):
            frame_type, seq, _ = parse_frame(messages[index][2])
            assert frame_type == TYPE_AUDIO_DOWN, f"帧类型 {frame_type}"
            assert seq == expected_seq, f"seq 应为 {expected_seq}，实为 {seq}"
        await _wait_closed(server, 1)
        stats = server.history[-1].stats
        assert stats.late_bins == 2, f"late_bins={stats.late_bins} != 2"
        assert stats.tts_frames == 5, f"tts_frames={stats.tts_frames} != 5"
    finally:
        await server.stop()


async def _t_bad_frames(cfg: Config) -> None:
    server, url = await _start(cfg)
    try:
        async with connect(url) as ws:
            await ws.send(json.dumps({"t": "hello", "device_id": "dev1", "ver": 1}))
            await ws.send(b"\x00\x01\x02\x03\x04\x05\x06\x07")            # magic 错
            await ws.send(MAGIC + bytes((FRAME_VER, TYPE_AUDIO_UP)) + b"\x00\x00\x00\x05")  # len 不符
            await ws.send(build_frame(0x7F, 0, b""))                      # 未知类型
            await ws.send(json.dumps({"t": "ping"}))
            # 先收到 hello_ack（T1.4 定稿），再等 pong
            deadline = time.monotonic() + 1.0
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AssertionError("坏帧之后未在 1s 内收到 pong")
                msg = await _recv_json(ws, timeout=remaining)
                if msg.get("t") == "pong":
                    break
            assert msg.get("t") == "pong", "坏帧之后连接被误关闭"
        await _wait_closed(server, 1)
        session = server.history[-1]
        assert session.stats.bad_frames == 3, f"坏帧计数 {session.stats.bad_frames} != 3"
    finally:
        await server.stop()


async def _t_interrupt(cfg: Config) -> None:
    server, url = await _start(cfg)
    try:
        async with connect(url) as ws:
            await ws.send(json.dumps({"t": "hello", "device_id": "dev1", "ver": 1}))
            await ws.send(json.dumps({"t": "audio_start", "sid": 7}))
            await ws.send(json.dumps({"t": "audio_end", "sid": 7}))
            while True:
                msg = await _recv_json(ws, timeout=3.0)
                if msg["t"] == "tts_start":
                    break
            await asyncio.sleep(0.05)  # 让几帧下行真的发出去
            await ws.send(json.dumps({"t": "interrupt", "sid": 7}))
            # 已在 socket 缓冲里的帧允许收完，但绝不能再出现 tts_end
            deadline = time.monotonic() + 0.5
            leftovers: list[Any] = []
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    leftover = await asyncio.wait_for(ws.recv(), remaining)
                except asyncio.TimeoutError:
                    break
                leftovers.append(leftover)
            for item in leftovers:
                if isinstance(item, str):
                    assert json.loads(item).get("t") != "tts_end", "interrupt 后仍收到 tts_end"
        await _wait_closed(server, 1)
        session = server.history[-1]
        assert session.stats.interrupts == 1, f"interrupt 计数 {session.stats.interrupts} != 1"
        assert session.stats.tts_cancelled == 1, f"取消失败：tts_cancelled={session.stats.tts_cancelled}"
    finally:
        await server.stop()


async def _t_reconnect(cfg: Config) -> None:
    server, url = await _start(cfg)
    try:
        async with connect(url) as ws:
            await ws.send(json.dumps({"t": "hello", "device_id": "dev1", "ver": 1}))
            await ws.send(json.dumps({"t": "ping"}))
            # hello_ack 先到，跳过它再等 pong（T1.4 定稿）
            deadline = time.monotonic() + 1.0
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AssertionError("首连 ping 未收到 pong")
                msg = await _recv_json(ws, timeout=remaining)
                if msg.get("t") == "pong":
                    break
            assert msg.get("t") == "pong", "首连 ping 未收到 pong"
        await _wait_closed(server, 1)
        async with connect(url) as ws:
            await ws.send(json.dumps({"t": "ping"}))
            assert (await _recv_json(ws, 1.0)).get("t") == "pong", "重连后 ping 未收到 pong"
        await _wait_closed(server, 2)
        assert server.history[0].conn_id != server.history[1].conn_id, "重连未产生新会话"
    finally:
        await server.stop()


async def _t_drop_pong(cfg: Config) -> None:
    server, url = await _start(cfg)
    try:
        async with connect(url) as ws:
            await ws.send(json.dumps({"t": "ping"}))
            try:
                raw = await asyncio.wait_for(ws.recv(), 0.5)
            except asyncio.TimeoutError:
                return
            raise AssertionError(f"--drop-pong 却收到了响应：{raw!r}")
    finally:
        await server.stop()


async def _t_ping_during_tts(cfg: Config) -> None:
    server, url = await _start(cfg)
    try:
        async with connect(url) as ws:
            await ws.send(json.dumps({"t": "hello", "device_id": "dev1", "ver": 1}))
            await ws.send(json.dumps({"t": "audio_start", "sid": 3}))
            await ws.send(json.dumps({"t": "audio_end", "sid": 3}))
            while True:
                msg = await _recv_json(ws, timeout=3.0)
                if msg["t"] == "tts_start":
                    break
            # 端侧 20s 心跳会落在 TTS 流中间：mock 必须能在下发帧的同时回 pong
            await ws.send(json.dumps({"t": "ping"}))
            deadline = time.monotonic() + 1.0
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AssertionError("TTS 流中 ping 未在 1s 内收到 pong")
                try:
                    raw = await asyncio.wait_for(ws.recv(), remaining)
                except asyncio.TimeoutError:
                    raise AssertionError("TTS 流中 ping 未在 1s 内收到 pong") from None
                if isinstance(raw, str) and json.loads(raw).get("t") == "pong":
                    return
    finally:
        await server.stop()


async def _t_auth_rejected(cfg: Config) -> None:
    server, url = await _start(replace(cfg, token="secret"))
    try:
        async with connect(url) as ws:
            code = None
            try:
                await asyncio.wait_for(ws.recv(), 1.0)
            except ConnectionClosed as exc:
                code = exc.rcvd.code if exc.rcvd is not None else None
            assert code == CLOSE_BAD_TOKEN, f"token 不匹配时期望关闭码 {CLOSE_BAD_TOKEN}，实为 {code}"
    finally:
        await server.stop()

# ---- T3.6：错误提示/坏 codec 注入（error_prompt_test.py 的 mock 侧契约） ----


def _json_of(messages: list[tuple[float, str, Any]], event: str) -> dict[str, Any] | None:
    for _, kind, msg in messages:
        if kind == "json" and msg.get("t") == event:
            return msg
    return None


def _types_of(messages: list[tuple[float, str, Any]]) -> list[str]:
    return [msg["t"] for _, kind, msg in messages if kind == "json"]

async def _t_error_default(cfg: Config) -> None:
    """--scenario error 默认行为（T2.5 兼容）：audio_end 后直接 error，code=1，无 asr/tts。"""
    server, url = await _start(replace(cfg, scenario="error"))
    try:
        async with connect(url) as ws:
            messages = await _collect_until(ws, sid=31, stop_event="error")
        types = _types_of(messages)
        err = _json_of(messages, "error")
        assert err is not None, f"未收到 error：{types}"
        assert err.get("sid") == 31, f"error.sid 未回填：{err}"
        assert err.get("code") == 1, f"默认 error.code 应为 1，实为 {err.get('code')}"
        assert "asr_final" not in types and "tts_start" not in types, (
            f"after-audio-end 不应先发 asr/tts：{types}"
        )
        bins = [msg for _, kind, msg in messages if kind == "bin"]
        assert bins == [], f"error 路径不应下发二进制帧：{len(bins)} 帧"
        await _wait_closed(server, 1)
        stats = server.history[-1].stats
        assert stats.errors == 1 and stats.tts_frames == 0, (
            f"统计异常：errors={stats.errors} tts_frames={stats.tts_frames}"
        )
    finally:
        await server.stop()


async def _t_error_code(cfg: Config) -> None:
    """--error-code：下发 JSON 里的 code 必须原样等于参数（3001 用于 TTS 失败类）。"""
    server, url = await _start(replace(cfg, scenario="error", error_code=3001))
    try:
        async with connect(url) as ws:
            messages = await _collect_until(ws, sid=32, stop_event="error")
        err = _json_of(messages, "error")
        assert err is not None and err.get("code") == 3001, (
            f"--error-code 3001 未生效：{err}"
        )
    finally:
        await server.stop()


async def _t_error_after_asr(cfg: Config) -> None:
    """--error-stage after-asr：先发 asr_final 再 error（ASR 之后服务失败）。"""
    server, url = await _start(replace(cfg, scenario="error", error_stage="after-asr"))
    try:
        async with connect(url) as ws:
            messages = await _collect_until(ws, sid=33, stop_event="error")
        types = _types_of(messages)
        assert "asr_final" in types, f"after-asr 应先发 asr_final：{types}"
        assert types.index("asr_final") < types.index("error"), f"事件顺序错误：{types}"
        assert "tts_start" not in types, f"after-asr 不应下发 tts_start：{types}"
        await _wait_closed(server, 1)
        assert server.history[-1].stats.tts_frames == 0
    finally:
        await server.stop()


async def _t_error_after_tts_start(cfg: Config) -> None:
    """--error-stage after-tts-start：tts_start + 3~5 帧真实 MP3 之后再 error（SPEAKING 中报错）。"""
    server, url = await _start(
        replace(cfg, scenario="error", error_code=3001, error_stage="after-tts-start")
    )
    try:
        async with connect(url) as ws:
            messages = await _collect_until(ws, sid=34, stop_event="error")
        types = _types_of(messages)
        assert "tts_start" in types and "error" in types, f"缺 tts_start/error：{types}"
        assert types.index("tts_start") < types.index("error"), f"事件顺序错误：{types}"
        assert "tts_end" not in types, f"报错轮不应发 tts_end：{types}"
        tts_start = _json_of(messages, "tts_start")
        assert tts_start is not None and tts_start.get("codec") == TTS_CODEC, (
            f"tts_start.codec 应为 {TTS_CODEC}：{tts_start}"
        )
        bins = [msg for _, kind, msg in messages if kind == "bin"]
        assert ERROR_TTS_FRAMES <= len(bins) <= 5, (
            f"报错前应发 3~5 帧真实 MP3，实际 {len(bins)} 帧"
        )
        for raw in bins:
            frame_type, _, payload = parse_frame(raw)
            assert frame_type == TYPE_AUDIO_DOWN, f"下行帧类型错误 0x{frame_type:02X}"
            assert payload[:2] == b"\xff\xfb" or (payload[1] & 0xE0) == 0xE0, (
                "报错前下发的不是真实 MP3 帧（帧同步字不符）"
            )
        await _wait_closed(server, 1)
        assert server.history[-1].stats.errors == 1
    finally:
        await server.stop()


async def _t_tts_bad_codec(cfg: Config) -> None:
    """--tts-bad-codec：tts_start codec="pcm" + tts_end，且一帧音频都不发。"""
    server, url = await _start(replace(cfg, tts_bad_codec=True))
    try:
        async with connect(url) as ws:
            messages = await _collect_round(ws, sid=35)
        tts_start = _json_of(messages, "tts_start")
        assert tts_start is not None and tts_start.get("codec") == TTS_BAD_CODEC, (
            f"tts_start.codec 应为 {TTS_BAD_CODEC}：{tts_start}"
        )
        assert tts_start.get("sr") == TTS_SAMPLE_RATE, f"tts_start.sr 异常：{tts_start}"
        bins = [msg for _, kind, msg in messages if kind == "bin"]
        assert bins == [], f"--tts-bad-codec 不应发音频帧：{len(bins)} 帧"
        types = _types_of(messages)
        assert "tts_end" in types, f"缺 tts_end：{types}"
        assert "asr_final" in types, f"正常轮仍应先发 asr_final：{types}"
        await _wait_closed(server, 1)
        stats = server.history[-1].stats
        assert stats.frames_down == 0 and stats.tts_frames == 0, (
            f"统计异常：frames_down={stats.frames_down} tts_frames={stats.tts_frames}"
        )
    finally:
        await server.stop()


async def _t_cli_config() -> None:
    """CLI：--error-code/--error-stage/--tts-bad-codec 默认值与解析（含非法 stage 拒绝）。"""
    defaults = parse_args([])
    assert defaults.error_code == 1, f"--error-code 默认应为 1：{defaults.error_code}"
    assert defaults.error_stage == "after-audio-end", (
        f"--error-stage 默认应为 after-audio-end：{defaults.error_stage}"
    )
    assert defaults.tts_bad_codec is False, "--tts-bad-codec 默认应关闭"

    explicit = parse_args(
        ["--error-code", "3001", "--error-stage", "after-tts-start", "--tts-bad-codec"]
    )
    assert explicit.error_code == 3001, f"--error-code 解析错误：{explicit.error_code}"
    assert explicit.error_stage == "after-tts-start", (
        f"--error-stage 解析错误：{explicit.error_stage}"
    )
    assert explicit.tts_bad_codec is True, "--tts-bad-codec 解析错误"

    for stage in ERROR_STAGES:
        parsed = parse_args(["--error-stage", stage])
        assert parsed.error_stage == stage, f"--error-stage {stage} 解析错误"

    rejected = False
    with contextlib.redirect_stderr(io.StringIO()):
        try:
            parse_args(["--error-stage", "after-everything"])
        except SystemExit:
            rejected = True
    assert rejected, "非法 --error-stage 应被 argparse 拒绝"


async def run_self_test() -> int:
    base = Config(
        host="127.0.0.1",
        port=0,
        think_ms=10,
        chunk_ms=1,
        max_tts_frames=5,
        tts_text="今天天气不错",
        quiet=True,
    )
    cases = [
        ("hello / hello_ack 握手与参数解析", _t_hello(base)),
        ("accept 连接 TCP_NODELAY=1（防 Nagle 假欠载）", _t_tcp_nodelay(base)),
        ("ping → pong 心跳", _t_ping(base)),
        ("一轮语音：事件顺序 / sid 回填 / 二进制帧头", _t_voice(base)),
        ("tts-mp3：真实 MP3 按帧切分 / 帧头连续 / 实时节奏", _t_tts_mp3(base)),
        ("tts-stall：中途停发注入欠载且不丢帧", _t_tts_stall(base)),
        ("tts-late-bin：tts_start 前 / tts_end 后各 1 帧窗口外注入", _t_tts_late(base)),
        ("坏帧容错（magic / len / type）后连接仍存活", _t_bad_frames(base)),
        ("interrupt 取消下发且不再发 tts_end", _t_interrupt(replace(base, chunk_ms=20, max_tts_frames=100))),
        ("断线重连后新会话可用", _t_reconnect(base)),
        ("TTS 流进行中 ping 仍能收到 pong", _t_ping_during_tts(replace(base, chunk_ms=20, max_tts_frames=100))),
        ("token 不匹配时按约定关闭码拒绝", _t_auth_rejected(base)),
        ("--drop-pong 不回 pong（端侧超时路径）", _t_drop_pong(replace(base, drop_pong=True))),
        # ---- T3.6：错误提示 mock 扩展（error_prompt_test.py 依赖） ----
        (
            "error 默认：audio_end 后直接报错（code=1、无 asr/tts）",
            _t_error_default(replace(base, scenario="error")),
        ),
        (
            "error --error-code 3001：下发 JSON code 原样",
            _t_error_code(replace(base, scenario="error", error_code=3001)),
        ),
        (
            "error --error-stage after-asr：先 asr_final 再 error",
            _t_error_after_asr(replace(base, scenario="error", error_stage="after-asr")),
        ),
        (
            "error --error-stage after-tts-start：先真实 MP3 帧再 error",
            _t_error_after_tts_start(replace(base, scenario="error")),
        ),
        (
            "--tts-bad-codec：tts_start codec=pcm + tts_end、不发音频帧",
            _t_tts_bad_codec(replace(base, tts_bad_codec=True)),
        ),
        (
            "CLI：--error-code/--error-stage/--tts-bad-codec 解析与默认值",
            _t_cli_config(),
        ),
    ]

    passed = 0
    for index, (name, coro) in enumerate(cases, 1):
        try:
            await coro
        except Exception as exc:  # 自测里用 assert，失败即 FAIL，不中断其它用例
            print(f"[{index}/{len(cases)}] FAIL  {name}: {exc}", flush=True)
        else:
            print(f"[{index}/{len(cases)}] PASS  {name}", flush=True)
            passed += 1
    failed = len(cases) - passed
    print(f"self-test: {passed}/{len(cases)} PASS" + ("" if failed == 0 else f"（{failed} FAIL）"), flush=True)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(run_self_test()))
