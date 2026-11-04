#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["websockets>=14,<19"]
# ///
"""
ws_mock_server.py —— 本地「云端」WebSocket 模拟服务（T1.5）

为什么需要：P1 起端侧依赖云端 WS 契约，但后端未就绪。先做本地 mock，让 T1.4/T1.6 以及
P2–P4 都能脱机联调，不占端侧排期（ROADMAP §2.0 P1 顺序 0）。

契约唯一依据（本脚本不发明字段）：
  - docs/ARCHITECTURE.md §6.1 连接与鉴权、§6.2 二进制帧、§6.3 文本帧
  - docs/ARCHITECTURE.md §7 会话状态机（sid 由端侧每轮自增，mock 原样回填）
  - docs/ARCHITECTURE.md §3.7 下行 TTS：MP3 24kHz 单声道
发现契约缺口时只记录（docs/PROTOCOL.md「实现中发现的契约待确认项」），不私自扩展。

用法：
  uv run tools/ws_mock_server.py                 # 起服务并打印可直连 URL
  uv run tools/ws_mock_server.py --self-test     # 进程内自测（不需要硬件、不需要手敲命令）
  uv run tools/ws_mock_server.py --no-tts        # 只发 JSON 事件（P1 握手/心跳联调）
  uv run tools/ws_mock_server.py --scenario error --drop-pong   # 异常/心跳超时路径
  # T2.4 下行流式播放（契约 §6）：
  uv run tools/ws_mock_server.py --tts-mp3 tts.mp3            # 真实 MP3，按帧切分、实时节奏
  uv run tools/ws_mock_server.py --tts-mp3 tts.mp3 --tts-stall-ms 600 --tts-stall-at 30
  uv run tools/ws_mock_server.py --tts-mp3 tts.mp3 --tts-late-bin  # 窗口外帧（端侧应丢弃）

T3.6 错误提示（tools/error_prompt_test.py 会自动用这些参数起 mock，手工联调同理）：
  uv run tools/ws_mock_server.py --scenario error                        # audio_end 后 error code=1
  uv run tools/ws_mock_server.py --scenario error --error-code 3001      # TTS 失败类错误码
  uv run tools/ws_mock_server.py --scenario error --error-code 3001 --error-stage after-asr
  uv run tools/ws_mock_server.py --scenario error --error-code 3001 \
      --error-stage after-tts-start     # 先 tts_start + 3~5 帧真实 MP3 再报错（SPEAKING 中报错）
  uv run tools/ws_mock_server.py --tts-bad-codec   # tts_start codec="pcm" + tts_end、不发帧
  # ↑ 触发设备侧 TTS 无法播放（VC_EVT_TTS_FAIL）→ feedback 播「语音播放失败」
  # --error-stage / --error-code 只对 --scenario error 生效；--tts-bad-codec 与它互斥。

T3.1 测试用的「真 ASR」（可选，独立实现见 tools/mock_asr.py，不依赖 DSH）：
  # 依赖通过 --with 临时装进 uv 环境（脚本自身的 PEP 723 依赖仍只有 websockets）
  uv run --with av --with numpy --with sherpa-onnx tools/ws_mock_server.py --asr sensevoice
  # 模型缺失时自动下载（约 239MB；也可先 `uv run tools/mock_asr.py --download`）
  uv run --with av --with numpy --with sherpa-onnx tools/ws_mock_server.py --asr sensevoice --asr-download
  开启后：audio_start~audio_end 的上行 Opus 帧会被解码 + 本地 SenseVoice 识别，asr_final.text
  用真实识别结果；识别失败/无声时回退 `--tts-text`（只 WARN，不打断协议链路）。
  离线验证识别效果：uv run tools/mock_asr.py --test tools/今天天气如何.wav

T3.1 回声测试（ASR 识别文本 → 本地 Piper 复读，不要固定文案）：
  uv run --with av --with numpy --with sherpa-onnx --with mlx-audio --with lameenc \
    tools/ws_mock_server.py --asr sensevoice --tts-qwen
  设备说的话先被识别，再用 Piper 把识别文本合成 24k MP3 下发给设备复读；
  识别文本会走 asr_partial/asr_final，合成失败只 WARN 并回退合成帧。
  （--tts-qwen 与 --tts-mp3 互斥；模型默认 mlx-community/Qwen3-TTS-12Hz-0.6B-CustomVoice-bf16）

传输说明：本地 mock 是 ws://（不加密）；线上契约是 wss://（ARCH §6.1）。
TLS/证书策略在 T1.4 决定，见 docs/PRODUCT_IDEAS.md §8 X2。
每条 accept 的连接都开 TCP_NODELAY（关 Nagle）：Nagle + 端侧 delayed ACK 会把均匀发送的
TTS 帧攒成 ~200ms 一批，造成「mock 均匀发、端侧却 underrun」的假欠载（T2.4-C2）。


实现位置（2026-11-03 拆分，行为不变；本文件只保留入口）：
  mock_core.py      协议帧 / MP3 帧解析 / Config / Stats / 日志
  mock_downlink.py  下行 TTS 流（合成帧 / 真实 MP3 / Piper 复读）、ASR 文本、错误注入
  mock_server.py    Session + MockServer + TCP_NODELAY + 启动横幅
  mock_cli.py       命令行参数、校验、Config 装配、ASR/Piper 后端加载
  mock_self_test.py 19 项进程内自测（`--self-test`）
  mock_fixtures.py  自测/错误注入共用的内嵌真实 MP3
"""

from __future__ import annotations

import asyncio
import sys

from mock_cli import config_from_args, load_asr_backend, load_tts_qwen, parse_args
from mock_core import LOG, TTS_BAD_CODEC, setup_logging
from mock_self_test import run_self_test
from mock_server import MockServer, serve_forever


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    cfg = config_from_args(args)
    setup_logging(cfg)
    if args.scenario != "error" and (args.error_code != 1 or args.error_stage != "after-audio-end"):
        LOG.warning("--error-code/--error-stage 只在 --scenario error 下生效，本次启动忽略")
    if args.tts_bad_codec:
        LOG.info("已启用 --tts-bad-codec：下一轮 tts_start 用 codec=%s 且不发音频帧", TTS_BAD_CODEC)

    if args.self_test:
        return asyncio.run(run_self_test())

    asr_backend = load_asr_backend(args)
    tts_qwen_module = load_tts_qwen(args)

    server = MockServer(cfg, asr_backend, tts_qwen_module)
    try:
        return asyncio.run(serve_forever(server))
    except KeyboardInterrupt:
        print(flush=True)  # 换行，避免 ^C 与最后一行日志粘连
        LOG.info("收到 Ctrl-C，退出")
        return 0
    except RuntimeError as exc:  # --tts-mp3 解析失败等启动期错误
        print(f"mock 启动失败：{exc}", file=sys.stderr, flush=True)
        return 2


if __name__ == "__main__":
    sys.exit(main())
