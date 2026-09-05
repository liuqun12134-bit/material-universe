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
        description="通用视频调用，或 DeepSeek 分析后自动调用 Wan3 视频换品。"
    )
    parser.add_argument("--mode", choices=("general", "product-swap"), default="general",
                        help="general：通用调用（默认）；product-swap：固定视频换品流程")
    parser.add_argument("--video", help="换品模式：一个本地原视频，最长 15 秒")
    parser.add_argument("--reference-image", help="换品模式：一张本地产品参考图")
    parser.add_argument("--volume-relation", help="换品模式：用户提供的体积或外形尺寸关系")
    parser.add_argument("--credential-source", choices=("skill-env", "host-app"),
                        default="skill-env", help=argparse.SUPPRESS)
    parser.add_argument("--prompt", help="原始提示词；不会被改写")
    parser.add_argument("--model", help="模型名称或模型别名")
    parser.add_argument(
        "--reference",
        action="append",
        default=[],
        help="本地路径或公网 HTTPS URL；必要时可加 image=、video=、audio= 前缀",
    )
    parser.add_argument("--output", help="输出视频路径；默认保存到当前目录 output 文件夹")
    parser.add_argument("--resume", help="读取 .video-task.json，继续原任务查询或下载；不重新提交")
    parser.add_argument("--duration", type=int, help="模型支持时指定视频时长")
    parser.add_argument(
        "--aspect-ratio",
        choices=("9:16", "16:9", "1:1", "4:3", "3:4"),
        help="输出画面比例",
    )
    parser.add_argument(
        "--resolution",
        choices=("480p", "720p", "1080p"),
        help="输出分辨率档位",
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
            if args.list_models or args.dry_run or args.mode != "general" or args.reference or any(
                getattr(args, name) is not None for name in
                ("prompt", "model", "video", "reference_image", "volume_relation", "output", "duration", "aspect_ratio", "resolution")
            ):
                raise VideoGenerationError("--resume 只接收原任务记录和凭据来源，不能同时指定新的生成参数。")
            result = runner.resume(Path(args.resume))
        elif args.list_models:
            result = {"success": True, "models": runner.list_models()}
        elif args.mode == "product-swap":
            from product_swap import ProductSwapWorkflow

            result = ProductSwapWorkflow(runner).run(args)
        else:
            if any(value is not None for value in (args.video, args.reference_image, args.volume_relation)):
                raise VideoGenerationError("--video、--reference-image、--volume-relation 仅用于 --mode product-swap。")
            if not args.prompt:
                raise VideoGenerationError("缺少 --prompt。")
            if not args.model:
                raise VideoGenerationError("缺少 --model。")
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
