#!/usr/bin/env python3
"""Unified entry point for registered video-model adapters."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from model_runner.runner import ModelRunner, VideoGenerationError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="通用图片/视频调用，或 DeepSeek 分析后自动调用 Wan3 视频换品。"
    )
    parser.add_argument("--mode", choices=("general", "product-swap"), default="general",
                        help="general：通用调用（默认）；product-swap：固定视频换品流程")
    parser.add_argument("--video", help="换品模式：一个本地原视频，最长 15 秒")
    parser.add_argument("--reference-image", help="换品模式：一张本地产品参考图")
    parser.add_argument("--volume-relation", help="换品模式：用户提供的体积或外形尺寸关系")
    parser.add_argument("--remove-subtitles", action="store_true",
                        help="仅换品模式：在最终提示词前加入去字幕指令（默认关闭）")
    parser.add_argument("--enable-prompt-expansion", action=argparse.BooleanOptionalAction,
                        default=None, help="仅换品模式：fal 提示词扩写（默认开启）")
    parser.add_argument("--enable-thinking", action=argparse.BooleanOptionalAction,
                        default=None, help="仅换品模式：fal 思考模式（默认开启）")
    parser.add_argument("--credential-source", choices=("skill-env", "host-app"),
                        default="skill-env", help=argparse.SUPPRESS)
    parser.add_argument("--prompt", help="原始提示词；不会被改写（换口型等不需要提示词的模型可省略）")
    parser.add_argument("--provider", help="服务商名称（默认 fal）；选了模型时校验模型归属")
    parser.add_argument("--model", help="模型名称或模型别名；不填使用服务商默认模型")
    parser.add_argument(
        "--reference",
        action="append",
        default=[],
        help="本地路径或公网 HTTPS URL；必要时可加 image=、video=、audio= 前缀",
    )
    parser.add_argument("--output", help="输出图片或视频路径；默认保存到当前目录 output 文件夹")
    parser.add_argument("--resume", help="读取 .video-task.json，继续原任务查询或下载；不重新提交")
    parser.add_argument("--duration", type=int, help="模型支持时指定视频时长")
    parser.add_argument(
        "--aspect-ratio",
        help="输出画面比例，支持范围由所选模型校验",
    )
    parser.add_argument(
        "--resolution",
        choices=("480p", "720p", "1080p"),
        help="输出分辨率档位",
    )
    parser.add_argument(
        "--option",
        action="append",
        default=[],
        help="模型自定义参数，格式 key=value（可多次指定），如 --option upscale_factor=4 --option acceleration=full",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只检查模型、输入和密钥配置，不提交或上传",
    )
    parser.add_argument(
        "--list-models",
        action="store_true",
        help="列出已登记模型和输入要求",
    )
    return parser


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    runner: ModelRunner | None = None
    try:
        args = build_parser().parse_args()
        runner = ModelRunner(credential_source=args.credential_source)
        if args.resume:
            if args.list_models or args.dry_run or args.mode != "general" or args.reference or args.option or any(
                getattr(args, name) is not None for name in
                ("prompt", "model", "provider", "video", "reference_image", "volume_relation", "output", "duration", "aspect_ratio", "resolution")
            ) or args.remove_subtitles or args.enable_prompt_expansion is not None or args.enable_thinking is not None:
                raise VideoGenerationError("--resume 只接收原任务记录和凭据来源，不能同时指定新的生成参数。")
            result = runner.resume(Path(args.resume))
        elif args.list_models:
            result = {"success": True, "models": runner.list_models()}
        elif args.mode == "product-swap":
            from product_swap import ProductSwapWorkflow

            args.enable_prompt_expansion = True if args.enable_prompt_expansion is None else args.enable_prompt_expansion
            args.enable_thinking = True if args.enable_thinking is None else args.enable_thinking
            result = ProductSwapWorkflow(runner).run(args)
        else:
            if args.remove_subtitles or args.enable_prompt_expansion is not None or args.enable_thinking is not None:
                raise VideoGenerationError("--remove-subtitles、--enable-prompt-expansion、--enable-thinking 仅用于 --mode product-swap。")
            if any(value is not None for value in (args.video, args.reference_image, args.volume_relation)):
                raise VideoGenerationError("--video、--reference-image、--volume-relation 仅用于 --mode product-swap。")
            if args.option:
                args.options = {}
                for item in args.option:
                    if "=" not in item:
                        raise VideoGenerationError(f"--option 格式应为 key=value：{item}")
                    key, raw = item.split("=", 1)
                    try:
                        args.options[key.strip()] = json.loads(raw)
                    except ValueError:
                        args.options[key.strip()] = raw.strip()
            result = runner.run(args)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except KeyboardInterrupt:
        message, code = "用户已中止。", 130
    except Exception as exc:
        message, code = str(exc), 1
    if runner is not None:
        message = runner.redact(message)
    print(json.dumps({"success": False, "error": message}, ensure_ascii=False, indent=2))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
