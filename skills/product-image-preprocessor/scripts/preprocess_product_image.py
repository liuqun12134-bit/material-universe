#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from image_preprocessor import ImagePreprocessor, ImagePreprocessorError


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="检查产品图，必要时生成一次无中文多视角棚拍参考图并复检。"
    )
    parser.add_argument("--input", help="产品原图本地路径")
    parser.add_argument("--output", help="不合格时生成图片的保存路径")
    parser.add_argument("--vision-model", help="已注册视觉分析模型")
    parser.add_argument("--image-model", help="已注册图片生成模型")
    parser.add_argument("--size", help="生图尺寸；默认使用模型注册值 2048x2048")
    parser.add_argument("--timeout", type=int, default=900, help="单次接口阶段最长等待秒数")
    parser.add_argument("--analyze-only", action="store_true", help="只分析，不调用生图模型")
    parser.add_argument("--dry-run", action="store_true", help="检查模型、凭据和请求结构，不调用 API")
    parser.add_argument("--list-models", action="store_true", help="列出视觉和生图模型")
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    parser.add_argument(
        "--credential-source",
        choices=("skill-env", "host-app"),
        default="skill-env",
        help=argparse.SUPPRESS,
    )
    return parser


def _emit(payload: dict, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    if payload.get("final_image"):
        print(payload["final_image"])
    elif payload.get("candidate_image"):
        print(payload["candidate_image"])
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2))


def main() -> int:
    args = _parser().parse_args()
    runner = ImagePreprocessor(credential_source=args.credential_source)
    if args.list_models:
        _emit(runner.list_models(), True)
        return 0
    if not args.input:
        raise ImagePreprocessorError("必须提供 --input 产品图片。")
    input_path = Path(args.input)
    output = Path(args.output) if args.output else None
    if args.dry_run:
        result = runner.dry_run(input_path, output, args.vision_model, args.image_model, args.size)
    else:
        result = runner.run(
            input_path=input_path,
            output=output,
            vision_model=args.vision_model,
            image_model=args.image_model,
            size=args.size,
            timeout=args.timeout,
            analyze_only=args.analyze_only,
        )
    _emit(result, args.json)
    return 0 if result.get("success") else 3


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ImagePreprocessorError, FileNotFoundError, ValueError, TimeoutError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        raise SystemExit(2)
