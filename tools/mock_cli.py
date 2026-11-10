"""mock_cli —— 命令行参数、启动校验、Config 装配与可选后端（ASR / Piper）加载。"""

from __future__ import annotations

import argparse
import os
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
    # ---- 2026-11-04：ASR 文本 → 本地 Qwen3-TTS 复读（回声测试） ----
    parser.add_argument(
        "--tts-qwen",
        action="store_true",
        help="回声测试：把 --asr 识别出的文本用本地 Qwen3-TTS（MLX）合成后下发"
             "（不强制 --asr：没开 ASR 时用 --tts-text 当文本；依赖 mlx-audio/lameenc；"
             "模型约 2.3GB，启动加载约 15s）",
    )
    parser.add_argument("--tts-model", default="",
                        help="Qwen3-TTS 本地 snapshot 目录或 HF repo id"
                             "（空=mlx-community/Qwen3-TTS-12Hz-0.6B-CustomVoice-bf16）")
    parser.add_argument("--tts-voice", default="serena",
                        help="预置音色：serena/vivian/uncle_fu/ryan/aiden/ono_anna/sohee/eric/dylan")
    parser.add_argument("--tts-lang", default="auto",
                        help="语言提示：auto/chinese/english/…")
    parser.add_argument("--tts-instruct", default="用清晰、爽朗、像聊天一样自然的语气说",
                        help="风格指令（自然语言，如 '用温柔的语气说'）")
    parser.add_argument("--tts-stream-interval", type=float, default=0.32,
                        help="流式块时长（秒）：越小首帧越早、块越多")
    parser.add_argument("--no-tts-stream", action="store_true",
                        help="关掉流式：整句合成完再发（排查用；默认流式）")
    # ---- 2026-11-04：ASR 文本 → LLM（OpenAI 兼容流式）→ 回答 → Qwen3-TTS ----
    parser.add_argument(
        "--llm",
        action="store_true",
        help="把 ASR 文本交给大模型（OpenAI 兼容 /chat/completions 流式），回答再交给 TTS；"
             "需 LLM_API_KEY 环境变量（token 不要写进仓库）",
    )
    parser.add_argument("--llm-model", default="", help="模型名（空=LLM_MODEL，必填）")
    parser.add_argument("--llm-base-url", default="", help="接口 base URL（空=LLM_BASE_URL，必填）")
    parser.add_argument("--llm-api-key", default="", help="Bearer token（空=LLM_API_KEY；不建议写进命令行）")
    parser.add_argument("--llm-system", default="", help="系统提示（空=LLM_SYSTEM_PROMPT）")
    parser.add_argument("--llm-max-tokens", type=int, default=0, help="最大输出 token（0=LLM_MAX_TOKENS/256）")
    # ---- 2026-11-10：LLM 流式输出 → 分句 → TTS 流式合成（边出字边播） ----
    parser.add_argument(
        "--llm-tts-stream",
        action="store_true",
        help="启用 LLM→TTS 流水线：LLM 边出字、按标点分句、边合成边下发"
             "（首声更早，但分句会让语音不够连贯；默认关）",
    )
    parser.add_argument(
        "--tts-prebuffer-ms",
        type=int,
        default=800,
        help="流水线起播前预缓冲音频毫秒数（抵消 TTS 首块/段间抖动；0=收到第一帧就起播）",
    )
    parser.add_argument("--tts-seg-first-chars", type=int, default=8,
                        help="首段最少字数（尽早出声，但太短会不自然）")
    parser.add_argument("--tts-seg-min-chars", type=int, default=6,
                        help="后续段最少字数")
    parser.add_argument("--tts-seg-max-chars", type=int, default=48,
                        help="单段硬上限（超过强制在标点/词边界断）")
    parser.add_argument("--tts-seg-first-max-chars", type=int, default=48,
                        help="首段硬上限（比后续段小，保证第一声更早）")
    parser.add_argument("--tts-seg-idle-ms", type=int, default=450,
                        help="LLM 停顿多久后兜底断段（毫秒）")
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
    # ---- 2026-11-10：M1 音乐（mock 侧意图/曲库/内置 HTTP；不需要 Hermes 支持 tools） ----
    parser.add_argument(
        "--music",
        action="store_true",
        help="启用音乐意图：ASR 文本命中控制/点歌规则时下发 music_track / music_control（不进 LLM）",
    )
    parser.add_argument(
        "--music-url",
        default="",
        help="固定曲目 URL（真机联调）；缺省用内置 HTTP 服务下发示例曲目",
    )
    parser.add_argument(
        "--music-title",
        default="",
        help="--music-url 的显示名（电台/直播流用；缺省取 URL 文件名）",
    )
    parser.add_argument(
        "--music-catalog",
        default="",
        help='JSON 曲库：{"tracks":[{"title","artist","url","duration_ms","keywords"}]}',
    )
    parser.add_argument(
        "--music-http-port",
        type=int,
        default=0,
        help="内置音乐 HTTP 端口（0=随机；仅 --music 且无 --music-url 时启用）",
    )
    parser.add_argument(
        "--music-search-url",
        default="",
        help="通用搜索端点：GET 返回 {\"tracks\":[{\"title\",\"url\",\"duration_ms\"}]}；"
             "用 {query} 占位（缺省自动补 ?q=）",
    )
    parser.add_argument(
        "--music-search-timeout-ms",
        type=int,
        default=3000,
        help="搜索超时毫秒（超时/失败回退本地曲库）",
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
    if args.tts_qwen and args.tts_mp3:
        raise SystemExit("--tts-qwen（回声测试）与 --tts-mp3（固定音频）互斥，二选一")
    if args.llm and args.tts_mp3:
        raise SystemExit("--llm（大模型回答→TTS）与 --tts-mp3（固定音频）互斥，二选一")
    if args.llm:
        missing = [
            name
            for name, value in (
                ("LLM_BASE_URL", args.llm_base_url or os.environ.get("LLM_BASE_URL")),
                ("LLM_MODEL", args.llm_model or os.environ.get("LLM_MODEL")),
                ("LLM_API_KEY", args.llm_api_key or os.environ.get("LLM_API_KEY")),
            )
            if not value
        ]
        if missing:
            raise SystemExit(
                "--llm 缺少配置：" + "、".join(missing)
                + "。请先 export（地址/模型/token 不要写进仓库）"
            )
    # --tts-qwen 不强制开 --asr：没有 ASR 时用 --tts-text 当"识别文本"，
    # 便于**不接设备/不说话**地离线验证 TTS 下发链路（2026-11-04）。
    if args.tts_qwen and args.asr == "none":
        LOG.info("--tts-qwen 未开 --asr：用 --tts-text 作为复读文本（离线验证 TTS 链路）")
    if args.tts_stream_interval <= 0:
        raise SystemExit(f"--tts-stream-interval 必须 > 0（当前 {args.tts_stream_interval}）")
    if args.tts_bad_codec and args.scenario == "error":
        raise SystemExit(
            "--tts-bad-codec（设备侧 TTS 失败路径）与 --scenario error（云端 error 事件）互斥，"
            "两者测的是不同反馈来源；需要云端 TTS 错误码用 --scenario error --error-code 3001"
        )
    if args.error_code < 0:
        raise SystemExit(f"--error-code 必须 >= 0（当前 {args.error_code}）")
    if args.tts_prebuffer_ms < 0:
        raise SystemExit(f"--tts-prebuffer-ms 必须 >= 0（当前 {args.tts_prebuffer_ms}）")
    if args.tts_seg_first_chars < 2:
        raise SystemExit(f"--tts-seg-first-chars 必须 >= 2（当前 {args.tts_seg_first_chars}）")
    if args.tts_seg_min_chars < 2:
        raise SystemExit(f"--tts-seg-min-chars 必须 >= 2（当前 {args.tts_seg_min_chars}）")
    if args.tts_seg_max_chars < max(args.tts_seg_first_chars, args.tts_seg_min_chars):
        raise SystemExit(
            f"--tts-seg-max-chars（{args.tts_seg_max_chars}）不能小于首段/后续段最少字数"
            f"（{args.tts_seg_first_chars}/{args.tts_seg_min_chars}）"
        )
    if not args.tts_seg_first_chars <= args.tts_seg_first_max_chars <= args.tts_seg_max_chars:
        raise SystemExit(
            f"--tts-seg-first-max-chars（{args.tts_seg_first_max_chars}）必须在"
            f"首段最少字数（{args.tts_seg_first_chars}）与单段硬上限（{args.tts_seg_max_chars}）之间"
        )
    if args.tts_seg_idle_ms < 50:
        raise SystemExit(f"--tts-seg-idle-ms 必须 >= 50（当前 {args.tts_seg_idle_ms}）")
    max_tts_frames = args.max_tts_frames
    if max_tts_frames is None:
        # 真实音频（固定 MP3 / 回声合成）默认全发；合成帧默认 100 帧 ≈ 2s
        max_tts_frames = 0 if (args.tts_mp3 or args.tts_qwen) else 100
    if (args.music_url or args.music_catalog) and not args.music:
        raise SystemExit("--music-url / --music-catalog 需要配合 --music 一起用")
    if args.music_title and not args.music_url:
        raise SystemExit("--music-title 只对 --music-url 生效（曲库里的标题请写在 JSON 里）")
    if args.music_search_url and not args.music:
        raise SystemExit("--music-search-url 需要配合 --music 一起用")
    if args.music_search_url and not args.music_search_url.startswith(("http://", "https://")):
        raise SystemExit(f"--music-search-url 只支持 http/https（当前 {args.music_search_url!r}）")
    if not 200 <= args.music_search_timeout_ms <= 30000:
        raise SystemExit(f"--music-search-timeout-ms 必须在 200~30000（当前 {args.music_search_timeout_ms}）")
    if args.music_catalog and not Path(args.music_catalog).is_file():
        raise SystemExit(f"--music-catalog 文件不存在：{args.music_catalog}")
    if args.music_url and not args.music_url.startswith(("http://", "https://")):
        raise SystemExit(f"--music-url 只支持 http/https（当前 {args.music_url!r}）")
    if not 0 <= args.music_http_port <= 65535:
        raise SystemExit(f"--music-http-port 必须在 0~65535（当前 {args.music_http_port}）")
    if args.music and args.scenario == "error":
        LOG.warning("--music 与 --scenario error 同开：error 分支在 ASR 之前返回，音乐意图不会触发")
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
        tts_qwen=args.tts_qwen,
        tts_model=args.tts_model,
        tts_voice=args.tts_voice,
        tts_lang=args.tts_lang,
        tts_instruct=args.tts_instruct,
        tts_stream_interval=args.tts_stream_interval,
        tts_stream=not args.no_tts_stream,
        llm=args.llm,
        llm_base_url=args.llm_base_url,
        llm_api_key=args.llm_api_key,
        llm_model=args.llm_model,
        llm_system=args.llm_system,
        llm_max_tokens=args.llm_max_tokens,
        llm_tts_stream=args.llm_tts_stream,
        tts_prebuffer_ms=args.tts_prebuffer_ms,
        tts_seg_first_chars=args.tts_seg_first_chars,
        tts_seg_min_chars=args.tts_seg_min_chars,
        tts_seg_max_chars=args.tts_seg_max_chars,
        tts_seg_first_max_chars=args.tts_seg_first_max_chars,
        tts_seg_idle_ms=args.tts_seg_idle_ms,
        error_code=args.error_code,
        error_stage=args.error_stage,
        tts_bad_codec=args.tts_bad_codec,
        music=args.music,
        music_url=args.music_url,
        music_title=args.music_title,
        music_catalog=args.music_catalog,
        music_http_port=args.music_http_port,
        music_search_url=args.music_search_url,
        music_search_timeout_ms=args.music_search_timeout_ms,
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


def load_tts_qwen(args: argparse.Namespace) -> object | None:
    """--tts-qwen：加载 tools/mock_tts.py 并**预热** Qwen3-TTS（ASR 文本 → 24k MP3 复读）。

    预热是必须的：模型加载约 15s，若等到第一轮 TTS 才加载，设备 THINKING 10s 会先超时。
    """
    if not args.tts_qwen:
        return None
    try:
        import mock_tts  # 同目录模块：自带 PEP 723 依赖（mlx-audio/lameenc/numpy）
    except ImportError as exc:
        raise SystemExit(
            f"--tts-qwen 需要额外依赖（{exc}）。请改用：\n"
            "  uv run --with av --with numpy --with sherpa-onnx --with mlx-audio --with lameenc \\\n"
            "    tools/ws_mock_server.py --asr sensevoice --tts-qwen ...\n"
            "模型缺失时先准备：uv run tools/mock_tts.py --text 你好 --out /tmp/x.mp3（自动查缓存/下载）"
        ) from exc
    try:
        mock_tts.preload(
            model=args.tts_model or mock_tts.DEFAULT_MODEL,
            voice=args.tts_voice or None,
            lang_code=args.tts_lang,
            instruct=args.tts_instruct,
            stream_interval=args.tts_stream_interval,
            log=lambda message: LOG.info("%s", message),
        )
    except BaseException as exc:  # noqa: BLE001 - 模型加载失败要给人话，不要裸露堆栈
        if isinstance(exc, KeyboardInterrupt):
            raise
        raise SystemExit(f"Qwen3-TTS 加载失败：{exc}") from exc
    mode = "流式" if not args.no_tts_stream else "整句"
    if args.llm:
        pipeline = "流水线（边出字边合成/下发）" if args.llm_tts_stream else "整段回答后合成（默认，语音更连贯）"
        LOG.info("TTS 回答已启用：LLM 回答 → Qwen3-TTS %s（%s；%s；模型 %s）",
                 args.tts_voice, mode, pipeline, args.llm_model or os.environ.get("LLM_MODEL", ""))
    else:
        LOG.info("TTS 复读已启用：ASR 文本 → Qwen3-TTS %s（%s下发）", args.tts_voice, mode)
        LOG.info("提示：当前是【复读模式】（回答 = 识别文本）。要大模型回答请加 --llm "
                 "并配置 LLM_BASE_URL/LLM_MODEL/LLM_API_KEY 环境变量")
    return mock_tts
