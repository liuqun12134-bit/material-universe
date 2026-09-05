#!/usr/bin/env python3
"""Prompt-only entrypoint over the video Skill's shared prompt engine."""
from __future__ import annotations
import argparse
import json
import os
import sys
from pathlib import Path

engine_dir = Path(__file__).resolve().parents[2] / "video-product-swap" / "scripts"
sys.path.insert(0, str(engine_dir))
from prompt_engine import (DEFAULT_MODEL, DEFAULT_API_BASE, _volume_relation_input,
                          analyze_video_and_relation, location_phrase, build_prompt)

def _read_env_file(env_path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not env_path.is_file():
        return values
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            values[key] = value
    return values


def _credential_config(
    source: str,
    skill_env_path: Path | None = None,
) -> tuple[str, str, str]:
    if source == "host-app":
        values = {
            "DEEPSEEK_API_KEY": os.environ.get("DEEPSEEK_API_KEY", ""),
            "DEEPSEEK_API_BASE": os.environ.get("DEEPSEEK_API_BASE", ""),
            "DEEPSEEK_PROMPT_MODEL": os.environ.get("DEEPSEEK_PROMPT_MODEL", ""),
        }
        missing_message = "宿主应用没有提供提示词分析 API Key。"
    else:
        env_path = skill_env_path or (Path(__file__).resolve().parent.parent / ".env")
        values = _read_env_file(env_path)
        missing_message = (
            "Skill 目录自己的 .env 缺少 DEEPSEEK_API_KEY；"
            "不会使用 Windows、Codex 或其他程序的环境变量。"
        )

    api_key = values.get("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError(missing_message)
    api_base = values.get("DEEPSEEK_API_BASE", "").strip() or DEFAULT_API_BASE
    model = values.get("DEEPSEEK_PROMPT_MODEL", "").strip() or DEFAULT_MODEL
    return api_key, api_base, model


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="用 DeepSeek 识别产品位置、整理用户给出的体积或外形尺寸关系，并拼装固定换品提示词。"
    )
    parser.add_argument("--video", required=True, help="参考视频本地路径")
    parser.add_argument("--reference-image", required=True, help="产品参考图本地路径；不会上传给 DeepSeek")
    parser.add_argument(
        "--volume-relation",
        required=True,
        help="用户描述的体积或外形尺寸关系，例如：高度为原视频产品的三分之二、宽度一致",
    )
    parser.add_argument(
        "--model",
        default=None,
        help=f"DeepSeek 视觉模型，默认 {DEFAULT_MODEL}",
    )
    parser.add_argument(
        "--credential-source",
        choices=("skill-env", "host-app"),
        default="skill-env",
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--timeout", type=int, default=600, help="等待视频处理的最长秒数")
    parser.add_argument("--output", help="可选：把最终提示词写入 UTF-8 文本文件")
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    return parser


def main() -> int:
    args = _parser().parse_args()
    video_path = Path(args.video).expanduser().resolve()
    reference_image = Path(args.reference_image).expanduser().resolve()
    if not video_path.is_file():
        raise FileNotFoundError(f"找不到参考视频：{video_path}")
    if not reference_image.is_file():
        raise FileNotFoundError(f"找不到产品参考图：{reference_image}")
    if args.timeout <= 0:
        raise ValueError("timeout 必须大于 0。")

    api_key, api_base, configured_model = _credential_config(args.credential_source)
    model = args.model or configured_model
    volume_relation_input = _volume_relation_input(args.volume_relation)
    result = analyze_video_and_relation(
        video_path,
        volume_relation_input,
        model,
        args.timeout,
        api_key,
        api_base,
    )
    location = location_phrase(result)
    prompt = build_prompt(location, result["volume_relation_zh"])

    if args.output:
        output_path = Path(args.output).expanduser().resolve()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(prompt + "\n", encoding="utf-8")

    if args.json:
        print(
            json.dumps(
                {
                    "model": model,
                    "placements": result["placements"],
                    "other_location_zh": result["other_location_zh"],
                    "volume_relation_input": volume_relation_input,
                    "volume_relation_zh": result["volume_relation_zh"],
                    "reference_image_uploaded": False,
                    "prompt": prompt,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        print(prompt)

    if "uncertain" in result["placements"]:
        print("警告：DeepSeek 无法可靠判断产品位置。", file=sys.stderr)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (FileNotFoundError, ValueError, RuntimeError, TimeoutError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        raise SystemExit(2)
