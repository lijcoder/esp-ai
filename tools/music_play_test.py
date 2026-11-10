#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["pyserial>=3.5"]
# ///
"""music_play_test —— T4.1 真机验证：`music play <url>` → 出声/统计/停止/内存。

设备侧探针（diag）：
  music play <http(s)://...>   启动拉流（music_http 任务按需创建）
  music stat                   active/url_active/ring/underruns/first_sound/http 统计
  music stop                   丢弃 ring + 淡出，任务自行退出

跑法：
  uv run tools/music_play_test.py --url 'http://192.168.1.23:9009/media/x.mp3'
  uv run tools/music_play_test.py --port /dev/tty.usbmodemXXXX --url ... --seconds 5

断言：MUSICPLAY ok → first_sound>0 且 <1500ms → 稳态 underruns==0 →
music stop 后 3s 内 active=0 且 music_http 任务消失 → 内部堆不持续下降 → 无 ERROR/panic。
"""

from __future__ import annotations

import argparse
import glob
import re
import sys
import time

import serial

FAILS: list[str] = []
PASSES = 0


def check(cond: bool, name: str, detail: str = "") -> bool:
    global PASSES
    if cond:
        PASSES += 1
        print(f"  PASS {name}", flush=True)
    else:
        FAILS.append(f"{name}" + (f" | {detail}" if detail else ""))
        print(f"  FAIL {name}" + (f" | {detail}" if detail else ""), flush=True)
    return cond


def pick_port(arg: str) -> str:
    if arg:
        return arg
    for pattern in ("/dev/tty.usbmodem*", "/dev/tty.usbserial*", "/dev/ttyACM*"):
        found = sorted(glob.glob(pattern))
        if found:
            return found[0]
    raise SystemExit("找不到串口：请用 --port 指定（macOS: /dev/tty.usbmodemXXXX）")


class Dev:
    def __init__(self, port: str, baud: int = 115200) -> None:
        self.ser = serial.Serial(port, baud, timeout=0.2)
        self.buf = ""
        time.sleep(0.3)
        self.drain()

    def drain(self) -> str:
        data = self.ser.read(self.ser.in_waiting or 1).decode("utf-8", "replace")
        self.buf += data
        return data

    def send(self, cmd: str, wait: float = 0.5) -> str:
        self.ser.reset_input_buffer()
        self.ser.write((cmd + "\r\n").encode())
        deadline = time.monotonic() + wait
        out = ""
        while time.monotonic() < deadline or self.ser.in_waiting:
            chunk = self.drain()
            if chunk:
                out += chunk
            if time.monotonic() >= deadline and not self.ser.in_waiting:
                break
            time.sleep(0.02)
        self.buf += out
        return out

    def wait_for(self, pattern: str, timeout: float = 5.0) -> str | None:
        regex = re.compile(pattern)
        deadline = time.monotonic() + timeout
        acc = ""
        while time.monotonic() < deadline:
            acc += self.drain()
            match = regex.search(acc)
            if match:
                return acc
            time.sleep(0.05)
        return None

    def close(self) -> None:
        self.ser.close()


def parse_heap(text: str) -> tuple[int | None, int | None]:
    """解析 `sys heap` 的 `内部 RAM: ..., 剩余 X B, ..., 最低水位 Y B, ...`。

    兼容两种输出：`sys info` 的 `内存水位 : 内部 RAM 剩余 X B / 最低水位 Y B`
    与 `sys heap` 的分行明细（T4.1 真机首跑发现只匹配前者）。
    """
    match = re.search(r"内部 RAM.*?剩余 (\d+) B.*?最低水位 (\d+) B", text)
    if not match:
        return None, None
    return int(match.group(1)), int(match.group(2))


def parse_musicstat(text: str) -> dict[str, str]:
    """合并 `MUSICSTAT`（播放器统计）与 `MUSICHTTP`（HTTP 统计）两行。"""
    out: dict[str, str] = {}
    for line in text.splitlines():
        if line.startswith("MUSICSTAT") or line.startswith("MUSICHTTP"):
            out.update(dict(re.findall(r"(\w+)=(-?[\w.]+)", line)))
    return out


def stat_num(stat: dict[str, str], key: str) -> int:
    """取数值字段：固件带单位（如 `first_sound=10ms`），这里剥掉非数字后缀。"""
    raw = stat.get(key, "") or ""
    digits = re.match(r"-?\d+", raw)
    return int(digits.group(0)) if digits else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="T4.1 音乐播放真机验证")
    parser.add_argument("--port", default="", help="串口（缺省自动找 /dev/tty.usbmodem*）")
    parser.add_argument("--url", required=True, help="可播的 http(s) MP3/电台 URL")
    parser.add_argument("--seconds", type=float, default=3.0, help="稳态观察时长")
    parser.add_argument("--max-underruns", type=int, default=0, help="允许的欠载次数")
    parser.add_argument("--skip-invalid", action="store_true", help="跳过非法 URL 拒绝断言")
    parser.add_argument("--dry-run", action="store_true", help="只打印命令，不连设备")
    args = parser.parse_args(argv)

    if args.dry_run:
        print(f"music play {args.url}")
        print(f"sleep {args.seconds}s；期间轮询 music stat")
        print("music stop")
        return 0

    port = pick_port(args.port)
    print(f"[TEST] 串口 {port} | URL {args.url}", flush=True)
    dev = Dev(port)
    try:
        dev.send("", 0.5)
        # 刚复位时 WiFi 可能还没拿到 IP（T4.1 真机首跑：抢在联网前 play → sock<0）
        net_ok = False
        for _ in range(30):
            if "WiFi 状态: UP" in dev.send("wifi status", 0.25):
                net_ok = True
                break
            time.sleep(0.5)
        check(net_ok, "设备已联网（WiFi UP）")
        # 上一次运行可能留下正在播的音乐：先停干净，保证统计从 0 起算
        for cmd in ("ms stop", "music stop"):
            dev.send(cmd, 0.5)
        for _ in range(10):
            st = parse_musicstat(dev.send("music stat", 0.3))
            if st.get("active") == "0" and st.get("url_active") == "0":
                break
            time.sleep(0.2)
        heap = dev.send("sys heap", 0.6)
        free0, min0 = parse_heap(heap)
        check(free0 is not None, "读取启动后内部堆", heap[-200:])

        if not args.skip_invalid:
            out = dev.send("music play ftp://example/x.mp3", 0.8)
            check("MUSICPLAY err=ESP_ERR_INVALID_ARG" in out, "非法 scheme 被拒绝", out[-200:])

        out = dev.send(f"music play {args.url}", 1.0)
        check("MUSICPLAY err=ESP_OK" in out, "music play 启动", out[-300:])

        stat: dict[str, str] = {}
        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline:
            stat = parse_musicstat(dev.send("music stat", 0.35))
            if stat and stat.get("url_active") == "1" and stat_num(stat, "first_sound") > 0:
                break
        check(bool(stat), "能读到 MUSICSTAT", stat)
        check(stat.get("url_active") == "1", "拉流任务在跑", stat)
        first_sound = stat_num(stat, "first_sound")
        check(0 < first_sound < 1500, "首声 <1500ms", f"{first_sound}ms")
        check(stat.get("status") in ("200", "206"), "HTTP 200/206", stat.get("status", "?"))

        time.sleep(args.seconds)
        stat = parse_musicstat(dev.send("music stat", 0.5))
        played = stat_num(stat, "played")
        check(played > 0, "混音已消费音乐帧", stat)
        underruns = stat_num(stat, "underruns")
        check(underruns <= args.max_underruns,
              f"稳态欠载 ≤{args.max_underruns}", f"{underruns} 次")

        out = dev.send("music stop", 0.8)
        check("MUSICSTOP err=ESP_OK" in out or "MUSICSTOP err=ESP_ERR_INVALID_STATE" in out,
              "music stop 接受", out[-200:])

        deadline = time.monotonic() + 3.0
        stopped = False
        while time.monotonic() < deadline:
            stat = parse_musicstat(dev.send("music stat", 0.35))
            if stat.get("active") == "0" and stat.get("url_active") == "0":
                stopped = True
                break
        check(stopped, "停止后 active=0 / url_active=0", stat)

        tasks = dev.send("sys tasks", 0.8)
        check("music_http" not in tasks, "停止后 music_http 任务已销毁",
              "\n".join(line for line in tasks.splitlines() if "music" in line))

        time.sleep(0.5)
        heap = dev.send("sys heap", 0.8)
        free1, min1 = parse_heap(heap)
        check(free1 is not None, "读取停止后内部堆", heap[-200:])
        if free0 is not None and free1 is not None:
            check(free1 >= free0 - 4096, "停止后内部堆回落（≤4KB 残留）", f"{free0} → {free1}")

        log = dev.buf
        # WS/TLS 重连噪声不是播放路径错误（mock 未起时必然出现），只过滤这些
        benign = re.compile(r"transport_base|transport_ws|esp-tls|websocket_client|WS: WS 错误|WS: 连接断开")
        bad = [line for line in log.splitlines()
               if re.search(r"^E \(|panic|Guru Meditation|abort\(\)", line)
               and not benign.search(line)]
        check(not bad, "无 ERROR/panic", "\n".join(bad[:3]))
    finally:
        dev.close()

    total = PASSES + len(FAILS)
    print(f"\n{'=' * 60}")
    if FAILS:
        print(f"FAIL：{len(FAILS)}/{total} 项失败")
        for item in FAILS:
            print(f"  - {item}")
    else:
        print(f"PASS：{PASSES}/{total} 项全部通过")
    print("=" * 60, flush=True)
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(main())
