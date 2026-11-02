"""One product-swap workflow shared by the Agent entrypoint and desktop UI."""
from __future__ import annotations

import math
from argparse import Namespace
from pathlib import Path

import prompt_engine
from media_inspection import probe_video
from model_runner.core import VideoGenerationError, default_output
from model_runner.runner import ModelRunner
from model_runner.task_state import TaskRecord

VIDEO_MODEL = "wan3.0-video"
VISION_MODEL = prompt_engine.DEFAULT_MODEL
MAX_SOURCE_SECONDS = 15
SUBTITLE_PREFIX = "先把参考视频中的字幕去掉，再"


def prompt_without_system_prefix(prompt: str, remove_subtitles: bool) -> str:
    """Recover the editable prompt only for a task that enabled this option."""
    return prompt[len(SUBTITLE_PREFIX):] if remove_subtitles and prompt.startswith(SUBTITLE_PREFIX) else prompt


def final_swap_prompt(prompt: str, remove_subtitles: bool) -> str:
    return (SUBTITLE_PREFIX + prompt_without_system_prefix(prompt, True)) if remove_subtitles else prompt


class ProductSwapWorkflow:
    def __init__(self, video_runner: ModelRunner):
        self.video_runner = video_runner

    def run(self, args: Namespace) -> dict:
        """Agent preset: fixed models/specifications, preserving the existing contract."""
        for name in ("video", "reference_image", "volume_relation"):
            if not getattr(args, name, None) or not getattr(args, name).strip():
                raise VideoGenerationError(f"换品模式缺少 --{name.replace('_', '-')}。")
        if any(getattr(args, name, None) is not None for name in
               ("prompt", "model", "provider", "duration", "aspect_ratio", "resolution")) or args.reference:
            raise VideoGenerationError("换品模式自动确定提示词、模型和输出规格；需要自行指定时请使用 --mode general。")
        return self.generate(args.video, args.reference_image, args.volume_relation,
                             args.output, dry_run=args.dry_run,
                             remove_subtitles=getattr(args, "remove_subtitles", False),
                             enable_prompt_expansion=True if getattr(args, "enable_prompt_expansion", None) is None else args.enable_prompt_expansion,
                             enable_thinking=True if getattr(args, "enable_thinking", None) is None else args.enable_thinking)

    def generate(self, video, reference_image, volume_relation, output=None, *,
                 model=VIDEO_MODEL, duration=None, aspect_ratio=None, resolution="480p",
                 vision_model=VISION_MODEL, max_source_seconds=MAX_SOURCE_SECONDS,
                 dry_run=False, progress=None, prompt_override=None, prepared_callback=None,
                 extra_images=None, remove_subtitles=False,
                 enable_prompt_expansion=True, enable_thinking=True) -> dict:
        """Desktop supplies its explicit settings here; the steps are identical."""
        report = progress or (lambda message: None)
        model_spec = self.video_runner.registry.resolve(model)
        if model_spec.adapter not in {"fal_wan3", "dashscope_wan3", "kaiyuncode_wan3"}:
            raise VideoGenerationError("一键换品仅支持 Wan3 视频参考模型。")
        if type(enable_prompt_expansion) is not bool or type(enable_thinking) is not bool:
            raise VideoGenerationError("提示词扩写和思考模式开关必须是布尔值。")
        if model_spec.provider == "kaiyuncode":
            enable_thinking = False
        source = Path(video).expanduser().resolve()
        product = Path(reference_image).expanduser().resolve()
        if not product.is_file():
            raise VideoGenerationError(f"找不到产品参考图：{product}")
        info = probe_video(source)
        if not math.isfinite(info.duration) or info.duration <= 0:
            raise VideoGenerationError("原视频时长无效。")
        if max_source_seconds is not None and info.duration > max_source_seconds:
            raise VideoGenerationError(f"原视频为 {info.duration:g} 秒，超过 {max_source_seconds:g} 秒，未调用任何模型。")
        duration = max(2, math.ceil(info.duration)) if duration is None else duration
        aspect_ratio = aspect_ratio or info.aspect_ratio
        references = [f"video={source}", f"image={product}"] + [f"image={p}" for p in (extra_images or [])]
        output_path = Path(output).expanduser().resolve() if output else default_output()
        relation = prompt_engine._volume_relation_input(volume_relation) if prompt_override is None else str(volume_relation)
        prepared = Namespace(model=model, provider=model_spec.provider, prompt="待分析后拼装", duration=duration,
                             aspect_ratio=aspect_ratio, resolution=resolution,
                             reference=references,
                             options={"enable_prompt_expansion": enable_prompt_expansion,
                                      "enable_thinking": enable_thinking},
                             output=str(output_path), dry_run=True)
        plan = self.video_runner.run(prepared)
        if not dry_run and TaskRecord.path_for(output_path).exists():
            raise VideoGenerationError(f"已有任务记录，请使用 --resume 继续，不再分析或生成：{TaskRecord.path_for(output_path)}")
        metadata = {
            "mode": "product-swap", "vision_model": vision_model,
            "source_video": str(source), "reference_image": str(product),
            "source_duration": info.duration, "source_width": info.width, "source_height": info.height,
            "output_parameters": {"model": model, "duration": duration,
                                  "aspect_ratio": aspect_ratio, "resolution": resolution},
            "volume_relation_input": relation, "remove_subtitles": bool(remove_subtitles),
            "enable_prompt_expansion": enable_prompt_expansion, "enable_thinking": enable_thinking,
        }
        warnings = []
        if duration is not None and duration != info.duration:
            warnings.append(f"原视频 {info.duration:g} 秒，本次提交 {duration} 秒。")
        if dry_run:
            if model_spec.adapter == "dashscope_wan3":
                plan["request_payload"]["input"]["prompt"] = None
            else:
                plan["request_payload"]["prompt"] = None
            return {**plan, **metadata, "prompt": None, "prompt_pending": True,
                    "analysis_performed": False, "warnings": warnings}

        self.video_runner.credentials.resolve(plan["provider"], require_key=True)
        if prompt_override is not None:
            if not prompt_override.strip():
                raise VideoGenerationError("使用已有提示词时，提示词不能为空。")
            base_prompt = prompt_without_system_prefix(prompt_override, remove_subtitles)
            prompt = final_swap_prompt(prompt_override, remove_subtitles)
            if prepared_callback:
                prepared_callback(prompt, base_prompt)
            report("使用已有提示词，准备上传素材…")
            prepared.prompt, prepared.dry_run = prompt, False
            result = self.video_runner.run(prepared)
            return {**result, **metadata, "prompt": prompt, "base_prompt": base_prompt,
                    "analysis_performed": False, "warnings": warnings}
        key, base, _ = self.video_runner.credentials.vision()
        report("正在抽取视频画面并分析产品位置…")
        try:
            analysis = prompt_engine.analyze_video_and_relation(source, relation, vision_model, 600, key, base)
        except Exception as exc:
            raise VideoGenerationError(self.video_runner.redact(str(exc)).replace(key, "***")) from None
        base_prompt = prompt_engine.build_prompt(prompt_engine.location_phrase(analysis), analysis["volume_relation_zh"])
        prompt = final_swap_prompt(base_prompt, remove_subtitles)
        if prepared_callback:
            prepared_callback(prompt, base_prompt)
        if "uncertain" in analysis["placements"]:
            warnings.append("产品位置识别不确定，继续生成。")
        report("提示词已生成，正在提交一次视频任务…")
        prepared.prompt, prepared.dry_run = prompt, False
        result = self.video_runner.run(prepared)
        return {**result, **metadata, "prompt": prompt, "base_prompt": base_prompt, "analysis": analysis,
                "analysis_performed": True, "warnings": warnings}
