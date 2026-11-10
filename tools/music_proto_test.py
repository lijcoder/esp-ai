#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["pyserial>=3.5", "websockets>=14,<19"]
# ///
"""music_proto_test —— T4.2（music_cloud 纯协议）真机回归：**可控下行注入**。

为什么单独一个脚本：`music_e2e_test.py` 走真实 mock（ASR → 音乐意图），验证得了
「说一句话能出声/暂停/继续」，但**验证不了端侧协议边界**——重复 mid、过期 mid、
非法 URL、非 mp3 codec、music_request 往返。这些必须由测试端**主动下发**构造。

做法：脚本自己起一个最小 WS 服务（hello/hello_ack/ping-pong + 按需下发 music_track /
music_control），让设备 `ws reconnect` 连过来，然后用 `mc status` / `ms status` 断言。

覆盖（PV-013 ⑥⑧ + PV-014 ②③⑧ 的协议部分）：
  A hello/hello_ack 握手、music_status 上行可见
  B music_track 起播（mid=10）
  C 重复 mid 丢弃（dup+1，不重复起播）
  D 过期 mid 丢弃（dropped_mid+1）
  E 非法 URL（ftp://）丢弃（bad+1）
  F 非 mp3 codec 丢弃（bad+1；端侧只实现 MP3）
  G music_control pause → ≤1.5s 暂停、progress 冻结、上报 paused
  H music_control resume → 从暂停点续播、progress 继续涨
  I 端侧 music_request → 服务端回新曲目（mid=13）并起播
  J music_control stop → ≤1.5s 停、上报 stopped

用法（先停掉 mock，端口 8765 必须空闲；设备 CONFIG_AI_WS_URL 指向同一地址）：
    uv run tools/music_proto_test.py --music-url 'http://192.168.1.15:9009/stream/test41.mp3'
    uv run tools/music_proto_test.py --music-url ... --ws-port 8765 --log /tmp/proto_dev.log
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import socket
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))

from voice_test import Device, Result, detect_port  # noqa: E402

END_MS = re.compile(r"^MSSTAT ")
END_MC = re.compile(r"^MCSTAT ")
END_CMD = re.compile(r"MSSTOP|MSPAUSE|MSRESUME|MSPLAY|MSREQUEST|MCSTAT|MSSTAT|WS 重连")
RE_MS_INFO = re.compile(
    r"MS state=(\w+) paused=(\d) reason=(\d+) mid=(\d+) have_mid=(\d) duration=(-?\d+)ms "
    r"progress=(\d+)ms")
RE_MCSTAT = re.compile(r"MCSTAT (.*)")


def parse_ms(lines: list[str]) -> dict | None:
    for line in lines:
        match = RE_MS_INFO.search(line)
        if match:
            return {"state": match.group(1), "paused": int(match.group(2)),
                    "reason": int(match.group(3)), "mid": int(match.group(4)),
                    "progress": int(match.group(7))}
    return None


def parse_mc(lines: list[str]) -> dict[str, int] | None:
    for line in lines:
        match = RE_MCSTAT.search(line)
        if match:
            return {k: int(v) for k, v in re.findall(r"(\w+)=(\d+)", match.group(1))}
    return None


def port_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.3)
        return sock.connect_ex(("127.0.0.1", port)) == 0


class FakeCloud(threading.Thread):
    """最小服务端：只做 hello_ack / pong / 按需下发 + 记录上行。"""

    def __init__(self, port: int) -> None:
        super().__init__(daemon=True, name="fake-cloud")
        self.port = port
        self.loop = asyncio.new_event_loop()
        self.ready = threading.Event()
        self.connected = threading.Event()
        self.events: list[tuple[float, dict]] = []
        self.lock = threading.Lock()
        self._q: asyncio.Queue | None = None

    def run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(self._serve())

    async def _serve(self) -> None:
        import websockets

        async with websockets.serve(self._handler, "0.0.0.0", self.port,
                                    ping_interval=None, max_size=1 << 20):
            self.ready.set()
            await asyncio.Future()

    async def _handler(self, ws) -> None:
        self._q = asyncio.Queue()
        self.connected.set()

        async def sender() -> None:
            while True:
                msg = await self._q.get()
                if msg is None:
                    return
                await ws.send(json.dumps(msg, ensure_ascii=False))

        task = asyncio.create_task(sender())
        try:
            async for raw in ws:
                if isinstance(raw, (bytes, bytearray)):
                    continue
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                with self.lock:
                    self.events.append((time.time(), msg))
                evt = msg.get("t")
                if evt == "hello":
                    await ws.send(json.dumps({"t": "hello_ack", "ver": 1}))
                elif evt == "ping":
                    await ws.send(json.dumps({"t": "pong"}))
        finally:
            task.cancel()
            self.connected.clear()

    # ---- 主线程 API ----
    def wait_ready(self, timeout: float = 5.0) -> bool:
        return self.ready.wait(timeout)

    def wait_connected(self, timeout: float = 20.0) -> bool:
        return self.connected.wait(timeout)

    def send(self, msg: dict) -> None:
        assert self._q is not None
        self.loop.call_soon_threadsafe(self._q.put_nowait, msg)

    def uplinks(self, kind: str | None = None) -> list[dict]:
        with self.lock:
            items = [m for _, m in self.events]
        return [m for m in items if kind is None or m.get("t") == kind]

    def wait_uplink(self, kind: str, pred=None, timeout: float = 6.0) -> dict | None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            for msg in self.uplinks(kind):
                if pred is None or pred(msg):
                    return msg
            time.sleep(0.05)
        return None


TRACK_EVENT = {"t": "music_track", "codec": "mp3", "duration_ms": 0}


def track(mid: int, url: str, **extra) -> dict:
    msg = dict(TRACK_EVENT)
    msg.update({"mid": mid, "url": url, "title": f"proto-{mid}"})
    msg.update(extra)
    return msg


def wait_ms(dev: Device, want: str, timeout: float = 6.0) -> dict | None:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = parse_ms(dev.console("ms status", END_MS, 2.0))
        if last and last["state"] == want:
            return last
        time.sleep(0.2)
    return last


def wait_terminal_state(dev: Device, timeout: float = 6.0) -> dict | None:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = parse_ms(dev.console("ms status", END_MS, 2.0))
        if last and last["state"] in ("stopped", "error", "idle"):
            return last
        time.sleep(0.2)
    return last


def mc_stat(dev: Device) -> dict[str, int] | None:
    return parse_mc(dev.console("mc status", END_MC, 2.0))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="music_cloud 协议真机回归（下行注入）")
    parser.add_argument("--port", default="", help="串口")
    parser.add_argument("--ws-port", type=int, default=8765, help="本脚本监听的 WS 端口（设备配置需一致）")
    parser.add_argument("--music-url", required=True, help="可长期播放的 MP3/直播流 URL（设备要能访问）")
    parser.add_argument("--log", default="", help="设备日志写入文件")
    args = parser.parse_args(argv)

    if port_open(args.ws_port):
        raise SystemExit(f"端口 {args.ws_port} 被占用：先停掉 mock（本脚本要自己当云端）")
    if not args.music_url.startswith(("http://", "https://")):
        raise SystemExit("--music-url 必须是 http(s)")

    port = detect_port(args.port)
    print(f"[TEST] 串口 {port} | 假云端 0.0.0.0:{args.ws_port} | 音乐 {args.music_url}")
    r = Result()
    cloud = FakeCloud(args.ws_port)
    cloud.start()
    r.check(cloud.wait_ready(), "假云端监听就绪")

    dev = Device(port)
    try:
        # 让设备立刻重连到本脚本（否则要等 1/2/5/10/30s 退避）
        mark = dev.mark()
        dev.console("ws reconnect", END_CMD, 2.0)
        connected = cloud.wait_connected(20.0) and \
            dev.wait_line(re.compile(r"WS: 握手完成（hello_ack）"), 15.0, mark) is not None
        r.check(connected, "A1 设备连上假云端并完成 hello/hello_ack")
        if not connected:
            return 1
        r.check(any(m.get("t") == "hello" for m in cloud.uplinks()),
                "A2 收到 hello")
        dev.console("mc reset", END_MC, 1.5)
        dev.console("ms reset", END_MS, 1.5)
        dev.console("ms stop", END_CMD, 1.5)
        time.sleep(0.5)

        # B: 起播
        cloud.send(track(10, args.music_url))
        playing = wait_ms(dev, "playing", 8.0)
        r.check(playing is not None and playing["state"] == "playing",
                f"B1 music_track mid=10 起播（{playing}）")
        r.check(bool(wait_ms(dev, "playing")) and mc_stat(dev) is not None
                and mc_stat(dev).get("track") == 1,
                f"B2 mc.track=1（{mc_stat(dev)}）")
        time.sleep(2.5)
        up = cloud.wait_uplink("music_status", lambda m: m.get("state") == "playing", 7.0)
        r.check(up is not None, "B3 上行 music_status state=playing")

        # C: 重复 mid
        before = mc_stat(dev)
        cloud.send(track(10, args.music_url))
        time.sleep(1.5)
        after = mc_stat(dev)
        r.check(after is not None and before is not None and after.get("dup", 0) >= 1,
                f"C1 重复 mid 被丢弃（dup {before.get('dup') if before else '?'} → "
                f"{after.get('dup') if after else '?'}）")
        r.check(parse_ms(dev.console("ms status", END_MS, 2.0))["mid"] == 10,
                "C2 重复 mid 没有重新起播（mid 仍=10）")

        # D: 过期 mid
        before = mc_stat(dev)
        cloud.send(track(5, args.music_url))
        time.sleep(1.5)
        after = mc_stat(dev)
        r.check(after is not None and before is not None
                and after.get("dropped_mid", 0) > before.get("dropped_mid", 0),
                f"D1 过期 mid 被丢弃（dropped_mid {before.get('dropped_mid') if before else '?'} → "
                f"{after.get('dropped_mid') if after else '?'}）")

        # E/F: 非法 URL / 非 mp3 codec
        before = mc_stat(dev)
        cloud.send(track(11, "ftp://example/x.mp3"))
        cloud.send(track(12, args.music_url, codec="pcm"))
        time.sleep(1.5)
        after = mc_stat(dev)
        r.check(after is not None and before is not None
                and after.get("bad", 0) >= before.get("bad", 0) + 2,
                f"E1 非法 URL + 非 mp3 各丢弃一次（bad {before.get('bad') if before else '?'} → "
                f"{after.get('bad') if after else '?'}）")
        r.check(parse_ms(dev.console("ms status", END_MS, 2.0))["mid"] == 10,
                "E2 坏事件没有改变当前曲目")

        # G: pause（时延 + 冻结 + 上报）
        frozen = parse_ms(dev.console("ms status", END_MS, 2.0))["progress"]
        t0 = time.time()
        cloud.send({"t": "music_control", "mid": 10, "action": "pause"})
        paused = wait_ms(dev, "paused", 3.0)
        took_ms = int((time.time() - t0) * 1000)
        r.check(paused is not None and paused["state"] == "paused",
                f"G1 pause 生效（{took_ms} ms，{paused}）")
        r.check(took_ms <= 1500, f"G2 pause ≤1.5s（{took_ms} ms）")
        time.sleep(1.5)
        frozen2 = parse_ms(dev.console("ms status", END_MS, 2.0))["progress"]
        r.check(paused is not None and paused["progress"] == frozen2,
                f"G3 暂停中 progress 冻结（{paused['progress'] if paused else '?'} / {frozen2}）")
        r.check(cloud.wait_uplink("music_status", lambda m: m.get("state") == "paused", 7.0) is not None,
                "G4 上报 state=paused")

        # H: resume（从暂停点）
        cloud.send({"t": "music_control", "mid": 10, "action": "resume"})
        resumed = wait_ms(dev, "playing", 4.0)
        time.sleep(2.0)
        later = parse_ms(dev.console("ms status", END_MS, 2.0))
        r.check(resumed is not None and resumed["state"] == "playing",
                f"H1 resume 生效（{resumed}）")
        r.check(later is not None and later["progress"] > frozen2,
                f"H2 从暂停点续播且进度增长（{frozen2} → {later['progress'] if later else '?'}）")

        # I: music_request → 服务端回新曲目
        dev.console("ms request play 0", END_CMD, 1.5)
        req = cloud.wait_uplink("music_request", None, 5.0)
        r.check(req is not None, f"I1 设备上行 music_request（{req}）")
        cloud.send(track(13, args.music_url))
        new = None
        deadline = time.time() + 10.0
        while time.time() < deadline:
            new = parse_ms(dev.console("ms status", END_MS, 2.0))
            if new and new["mid"] == 13 and new["state"] == "playing":
                break
            time.sleep(0.3)
        r.check(new is not None and new["mid"] == 13 and new["state"] == "playing",
                f"I2 服务端回 track 后起播 mid=13（{new}）")

        # J: stop
        t0 = time.time()
        cloud.send({"t": "music_control", "mid": 13, "action": "stop"})
        stopped = wait_terminal_state(dev, 3.0)
        r.check(stopped is not None and stopped["state"] in ("stopped", "idle"),
                f"J1 stop 生效（{int((time.time() - t0) * 1000)} ms，{stopped}）")
        r.check(cloud.wait_uplink("music_status", lambda m: m.get("state") == "stopped", 7.0) is not None,
                "J2 上报 state=stopped")

        bad = [line for _, line in dev.lines
               if re.search(r"Guru Meditation|panic|Backtrace:", line)]
        r.check(not bad, "全程无 panic/backtrace")
    finally:
        if args.log:
            try:
                with open(args.log, "w", encoding="utf-8") as handle:
                    for ts, line in dev.lines:
                        handle.write(f"{ts:.3f} {line}\n")
            except OSError:
                pass
        dev.close()

    return 0 if r.summary() else 1


if __name__ == "__main__":
    sys.exit(main())
