#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["websockets>=14,<19"]
# ///
"""music_demo —— 一条命令看 M1 音乐的事件流（进程内起 mock + 假设备，不接开发板）。

跑法：
  uv run tools/music_demo.py
  uv run tools/music_demo.py --rounds "放首晴天" "暂停"   # 只跑指定句子（可重复 --rounds）

输出就是设备将来会收到的 JSON：`music_track`（含可播 URL）/ `music_control`（暂停/继续/停止）
+ 确认语的 `tts_start`/`tts_end`。默认 `--no-tts`（只发 JSON，不出音频帧），所以不需要
ASR/TTS 模型，也不用连设备。

真正的"出声播放"要等端侧 T4.1（source_http）+ T4.2（music_cloud）；本脚本只验证云端侧协议。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from websockets.asyncio.client import connect

from mock_core import Config
from mock_server import MockServer

DEFAULT_ROUNDS = ["放首周杰伦的晴天", "暂停", "下一首", "今天天气如何", "继续播放", "停止播放"]


async def main(rounds: list[str], search_url: str) -> int:
    cfg = Config(host="127.0.0.1", port=0, music=True, no_tts=True, think_ms=0, quiet=True,
                 music_search_url=search_url)
    server = MockServer(cfg)
    port = await server.start()
    if server.music.demo_url:
        print(f"示例曲目（设备将来拉的 URL）：{server.music.demo_url}\n")
    elif server.music.search is not None:
        print(f"搜索源：{server.music.search.url_template}\n")
    try:
        async with connect(
            f"ws://127.0.0.1:{port}/v1/voice?device_id=demo&token=t&ver=1", max_size=64 * 1024
        ) as ws:
            await ws.send(json.dumps({"t": "hello", "device_id": "demo", "ver": 1}))
            await ws.recv()
            for sid, text in enumerate(rounds, 1):
                cfg.tts_text = text
                print(f"=== 第 {sid} 轮：{text!r} ===")
                await ws.send(json.dumps({"t": "audio_start", "sid": sid}))
                await ws.send(json.dumps({"t": "audio_end", "sid": sid}))
                while True:
                    raw = await ws.recv()
                    if isinstance(raw, (bytes, bytearray)):
                        continue
                    msg = json.loads(str(raw))
                    print("  ->", json.dumps(msg, ensure_ascii=False))
                    if msg.get("t") == "tts_end":
                        break
                print()
    finally:
        await server.stop()
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="M1 音乐事件流演示（不需要开发板）")
    parser.add_argument(
        "--rounds", nargs="+", default=DEFAULT_ROUNDS, help="每轮要\"说\"的 ASR 文本（默认跑 6 句）"
    )
    parser.add_argument(
        "--search-url", default="",
        help="可选：接本机搜索端点，如 'http://127.0.0.1:9009/search?q={query}'",
    )
    args = parser.parse_args()
    sys.exit(asyncio.run(main(args.rounds, args.search_url)))
