"""mock_cli —— 命令行参数、启动校验、Config 装配与可选后端（ASR / Piper）加载。"""

from __future__ import annotations

import argparse
from pathlib import Path

from mock_core import (
    ERROR_STAGES,
    FRAME_HEADER_LEN,
    LOG,
    Config,
)

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="本地云端 WebSocket 模拟服务（T1.5 / T2.4）",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--host", default="0.0.0.0", help="监听地址；0.0.0.0 允许局域网设备直连")
    parser.add_argument("--port", type=int, default=8765, help="监听端口；0 表示由系统分配（自测用）")
    parser.add_argument("--device-id", default="", help="非空则校验 query.device_id")
    parser.add_argument("--token", default="", help="非空则校验 query.token")
    parser.add_argument("--scenario", choices=("normal", "error", "music"), default="normal")
    parser.add_argument("--think-ms", type=int, default=300, help="audio_end 到首个下行事件的延时")
    parser.add_argument("--chunk-ms", type=int, default=20, help="合成帧发送间隔（--tts-mp3 时按帧时长）")
    parser.add_argument("--chunk-bytes", type=int, default=192, help="合成 TTS payload 长度")
    parser.add_argument(
        "--max-tts-frames",
        type=int,
        default=None,
        help="一轮 TTS 的二进制帧上限；缺省合成帧 100、--tts-mp3 时全发；"
             "MP3 模式下 0 表示不限",
    )
    parser.add_argument("--tts-text", default=Config.tts_text, help="模拟识别的文本")
    parser.add_argument("--no-tts", action="store_true", help="只发 JSON 事件，不发二进制帧")
    parser.add_argument("--inject-late", action="store_true",
                        help="每轮先发一个 sid+1 的 asr_final（测端侧迟到帧过滤）")
    parser.add_argument("--drop-pong", action="store_true", help="收到 ping 不回 pong（测端侧超时）")
    parser.add_argument("--close-after", type=float, default=0.0, help=">0 时到点主动断开（测重连）")
    parser.add_argument("--save-upload", default="", help="把上行 payload 落盘到该目录")
    # ---- T3.1 测试：本地真 ASR（tools/mock_asr.py，独立于 DSH） ----
    parser.add_argument(
        "--asr",
        choices=("none", "sensevoice"),
        default="none",
        help="用本地 SenseVoice 识别上行音频（需额外依赖，见模块文档；默认固定文案）",
    )
    parser.add_argument("--asr-model-dir", default="", help="SenseVoice 模型目录（含 model.int8.onnx + tokens.txt）")
    parser.add_argument("--asr-download", action="store_true", help="模型缺失时自动下载（约 239MB）")
    parser.add_argument("--asr-threads", type=int, default=2, help="ONNX 推理线程数")
    parser.add_argument(
        "--asr-language",
        default="auto",
        choices=("auto", "zh", "en", "yue", "ja", "ko"),
        help="SenseVoice 语言提示",
    )
    parser.add_argument("--asr-max-seconds", type=float, default=30.0, help="单轮最多送 ASR 的音频秒数")
    # ---- T3.1 测试：ASR 文本 → 本地 Piper 复读（回声测试） ----
    parser.add_argument(
        "--tts-piper",
        action="store_true",
        help="回声测试：把 --asr 识别出的文本用本地 Piper 合成后下发（需 --asr，依赖 piper-tts/lameenc）",
    )
    parser.add_argument("--tts-piper-voice", default="zh_CN-huayan-medium", help="Piper 音色名")
    parser.add_argument("--tts-piper-speed", type=float, default=1.0,
                        help="Piper 语速（length_scale：>1 更慢）")
    parser.add_argument("--tts-piper-model-dir", default="", help="Piper 音色目录（空=自动找缓存）")
    parser.add_argument("--tts-piper-download", action="store_true", help="Piper 音色缺失时下载（约 60MB）")
    # ---- T2.4：真实 MP3 下发（契约 §6） ----
    parser.add_argument(
        "--tts-mp3",
        default="",
        help="下发真实 MP3 文件（按 MP3 帧边界切分，缺省按帧时长实时节奏）；缺省仍发合成帧",
    )
    parser.add_argument(
        "--tts-stall-ms",
        type=int,
        default=0,
        help="在 --tts-stall-at 帧后停发 n ms，制造端侧欠载（0=不停发）",
    )
    parser.add_argument("--tts-stall-at", type=int, default=30, help="stall 帧序号（0 基，默认 30）")
    parser.add_argument(
        "--tts-late-bin",
        action="store_true",
        help="每轮 tts_start 前 / tts_end 后各发 1 帧 0x02（验证端侧窗口外丢弃）",
    )
    parser.add_argument(
        "--tts-pace-pct",
        type=int,
        default=100,
        help="真实 MP3 发送节奏：帧时长的百分比（100=实时；测试可略快，如 90）",
    )
    parser.add_argument("--verbose", action="store_true", help="打印上行 payload 前 16 字节")
    # ---- T3.6：错误提示（云端错误码/报错时机/坏 codec 注入；error_prompt_test.py 用） ----
    parser.add_argument(
        "--error-code",
        type=int,
        default=1,
        help="--scenario error 下发的 error.code（默认 1=通用；3001/3002=TTS 失败，端侧应播 tts-fail 提示音）",
    )
    parser.add_argument(
        "--error-stage",
        choices=ERROR_STAGES,
        default="after-audio-end",
        help="云端报错时机：audio_end 后（现状）/ asr_final 后 / tts_start+3~5 帧真实 MP3 后",
    )
    parser.add_argument(
        "--tts-bad-codec",
        action="store_true",
        help='tts_start 用 codec="pcm" 后发 tts_end、不发音频帧（触发设备侧 TTS 无法播放 → 失败提示音）',
    )
    parser.add_argument("--self-test", action="store_true", help="进程内自测，不需要硬件")
    return parser.parse_args(argv)


def _validate_args(args: argparse.Namespace) -> int:
    """启动期校验（错误一律 SystemExit，信息与拆分前逐字一致）；返回归一化后的帧上限。"""
    if args.tts_mp3 and not Path(args.tts_mp3).is_file():
        raise SystemExit(f"--tts-mp3 文件不存在：{args.tts_mp3}")
    if not 0 <= args.tts_stall_ms:
        raise SystemExit(f"--tts-stall-ms 必须 >= 0（当前 {args.tts_stall_ms}）")
    if args.tts_stall_at < 0:
        raise SystemExit(f"--tts-stall-at 必须 >= 0（当前 {args.tts_stall_at}）")
    if not 1 <= args.tts_pace_pct <= 400:
        raise SystemExit(f"--tts-pace-pct 必须在 1~400（当前 {args.tts_pace_pct}）")
    if args.tts_piper and args.tts_mp3:
        raise SystemExit("--tts-piper（回声测试）与 --tts-mp3（固定音频）互斥，二选一")
    if args.tts_piper and args.asr == "none":
        raise SystemExit("--tts-piper 是回声测试：需要同时开 --asr sensevoice 才有识别文本可复读")
    if args.tts_bad_codec and args.scenario == "error":
        raise SystemExit(
            "--tts-bad-codec（设备侧 TTS 失败路径）与 --scenario error（云端 error 事件）互斥，"
            "两者测的是不同反馈来源；需要云端 TTS 错误码用 --scenario error --error-code 3001"
        )
    if args.error_code < 0:
        raise SystemExit(f"--error-code 必须 >= 0（当前 {args.error_code}）")
    max_tts_frames = args.max_tts_frames
    if max_tts_frames is None:
        # 真实音频（固定 MP3 / 回声合成）默认全发；合成帧默认 100 帧 ≈ 2s
        max_tts_frames = 0 if (args.tts_mp3 or args.tts_piper) else 100
    if args.asr_max_seconds <= 0:
        raise SystemExit(f"--asr-max-seconds 必须 > 0（当前 {args.asr_max_seconds}）")
    if args.asr_threads < 1:
        raise SystemExit(f"--asr-threads 必须 >= 1（当前 {args.asr_threads}）")
    return max_tts_frames


def config_from_args(args: argparse.Namespace) -> Config:
    """CLI 参数 → Config（含校验）；顺序与拆分前一致，便于错误信息复现。"""
    max_tts_frames = _validate_args(args)
    return Config(
        host=args.host,
        port=args.port,
        device_id=args.device_id,
        token=args.token,
        scenario=args.scenario,
        think_ms=args.think_ms,
        chunk_ms=args.chunk_ms,
        chunk_bytes=max(FRAME_HEADER_LEN, args.chunk_bytes),
        max_tts_frames=max(0, max_tts_frames),
        tts_text=args.tts_text,
        no_tts=args.no_tts,
        inject_late=args.inject_late,
        drop_pong=args.drop_pong,
        close_after=args.close_after,
        save_upload=args.save_upload,
        tts_mp3=args.tts_mp3,
        tts_stall_ms=args.tts_stall_ms,
        tts_stall_at=args.tts_stall_at,
        tts_late_bin=args.tts_late_bin,
        tts_pace_pct=args.tts_pace_pct,
        asr=args.asr,
        asr_model_dir=args.asr_model_dir,
        asr_download=args.asr_download,
        asr_threads=args.asr_threads,
        asr_language=args.asr_language,
        asr_max_seconds=args.asr_max_seconds,
        tts_piper=args.tts_piper,
        tts_piper_voice=args.tts_piper_voice,
        tts_piper_speed=args.tts_piper_speed,
        tts_piper_model_dir=args.tts_piper_model_dir,
        tts_piper_download=args.tts_piper_download,
        error_code=args.error_code,
        error_stage=args.error_stage,
        tts_bad_codec=args.tts_bad_codec,
        quiet=args.self_test,
        verbose=args.verbose,
    )


def load_asr_backend(args: argparse.Namespace) -> object | None:
    """--asr：加载 tools/mock_asr.py（同目录模块，默认假 ASR 路径零额外依赖）。"""
    if args.asr == "none":
        return None
    try:
        import mock_asr  # 同目录模块：自带 PEP 723 依赖，mock 只在 --asr 时依赖它
    except ImportError as exc:
        raise SystemExit(
            f"--asr={args.asr} 需要额外依赖（{exc}）。请改用：\n"
            "  uv run --with av --with numpy --with sherpa-onnx tools/ws_mock_server.py --asr sensevoice ...\n"
            "首次使用可先准备模型：uv run tools/mock_asr.py --download（约 239MB，"
            "或复用 ~/.dsh 下的缓存）"
        ) from exc
    try:
        backend = mock_asr.MockAsr(
            args.asr_model_dir or None,
            download=args.asr_download,
            threads=args.asr_threads,
            language=args.asr_language,
            log=lambda message: LOG.info("%s", message),
        )
    except RuntimeError as exc:
        raise SystemExit(f"mock ASR 初始化失败：{exc}") from exc
    LOG.info("ASR 已启用：asr_final 使用本地 SenseVoice 识别结果")
    return backend


def load_tts_piper(args: argparse.Namespace) -> object | None:
    """--tts-piper：加载 tools/mock_tts.py（ASR 文本 → Piper 合成 24k MP3 复读）。"""
    if not args.tts_piper:
        return None
    try:
        import mock_tts  # 同目录模块：自带 PEP 723 依赖（piper-tts/lameenc/numpy）
    except ImportError as exc:
        raise SystemExit(
            f"--tts-piper 需要额外依赖（{exc}）。请改用：\n"
            "  uv run --with av --with numpy --with sherpa-onnx --with piper-tts --with lameenc \\\n"
            "    tools/ws_mock_server.py --asr sensevoice --tts-piper ...\n"
            "首次使用可先准备音色：uv run tools/mock_tts.py --download（约 60MB）"
        ) from exc
    try:
        mock_tts.ensure_voice(
            args.tts_piper_voice,
            args.tts_piper_model_dir or None,
            download=args.tts_piper_download,
            log=lambda message: LOG.info("%s", message),
        )
    except SystemExit as exc:
        raise SystemExit(f"Piper 音色未就绪：{exc}") from exc
    LOG.info("TTS 复读已启用：ASR 文本 → Piper %s 合成后下发", args.tts_piper_voice)
    return mock_tts
