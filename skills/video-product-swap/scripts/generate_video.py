#!/usr/bin/env python3
"""Unified entry point for registered video-model adapters."""

from __future__ import annotations

import argparse
import json
import sys

from model_runner.runner import ModelRunner, VideoGenerationError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="选择视频模型、参考文件和提示词，然后由对应适配器完成调用。"
    )
    parser.add_argument("--prompt", help="原始提示词；不会被改写")
    parser.add_argument("--model", help="模型名称或模型别名")
    parser.add_argument(
        "--reference",
        action="append",
        default=[],
        help="本地路径或公网 HTTPS URL；必要时可加 image=、video=、audio= 前缀",
    )
    parser.add_argument("--output", help="输出视频路径；默认保存到当前目录 output 文件夹")
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
        runner = ModelRunner()
        args = build_parser().parse_args()
        if args.list_models:
            result = {"success": True, "models": runner.list_models()}
        else:
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
