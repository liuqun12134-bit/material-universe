"""fal FlashVSR upscale adapter: 视频画质修复/超分。"""
from __future__ import annotations

from typing import Any

from ..core import GenerationRequest, VideoGenerationError
from .fal_base import FalQueueAdapter

FLASHVSR_VIDEO_EXTENSIONS = {".mp4", ".mov", ".webm"}
FLASHVSR_ACCELERATIONS = {"regular", "high", "full"}
FLASHVSR_DEFAULTS = {
    "upscale_factor": 2,
    "acceleration": "regular",
    "color_fix": True,
    "preserve_audio": True,
}


class FalFlashVsrAdapter(FalQueueAdapter):
    """fal-ai/flashvsr/upscale/video：一个视频 -> 画质修复放大。

    可选参数（request.options）：upscale_factor 1-4、acceleration regular/high/full、
    color_fix、preserve_audio；未提供时使用 FLASHVSR_DEFAULTS。
    """

    name = "fal_flashvsr"

    def _options(self, request: GenerationRequest) -> dict[str, Any]:
        raw = dict(request.options or {})
        unknown = sorted(set(raw) - set(FLASHVSR_DEFAULTS))
        if unknown:
            raise VideoGenerationError(
                f"FlashVSR 不支持的参数：{', '.join(unknown)}；"
                f"可选：{', '.join(sorted(FLASHVSR_DEFAULTS))}。"
            )
        merged = {**FLASHVSR_DEFAULTS, **raw}
        try:
            factor = float(merged["upscale_factor"])
        except (TypeError, ValueError) as exc:
            raise VideoGenerationError("upscale_factor 必须是 1-4 的数字。") from exc
        if not 1 <= factor <= 4:
            raise VideoGenerationError("upscale_factor 必须在 1-4 之间。")
        merged["upscale_factor"] = int(factor) if factor.is_integer() else factor
        if str(merged["acceleration"]) not in FLASHVSR_ACCELERATIONS:
            raise VideoGenerationError("acceleration 仅支持 regular / high / full。")
        for flag in ("color_fix", "preserve_audio"):
            if not isinstance(merged[flag], bool):
                raise VideoGenerationError(f"{flag} 必须是 true 或 false。")
        return merged

    def validate(self, request: GenerationRequest) -> None:
        # 不调用 super().validate()：FlashVSR 不需要提示词
        videos = [r for r in request.references if r.kind == "video"]
        audios = [r for r in request.references if r.kind == "audio"]
        images = [r for r in request.references if r.kind == "image"]
        if len(videos) != 1:
            raise VideoGenerationError("FlashVSR 画质修复需要且只接受一个视频。")
        if audios or images:
            raise VideoGenerationError("FlashVSR 画质修复只接受视频，不接受音频或图片参考。")
        video = videos[0]
        if video.local_path is not None and video.local_path.suffix.lower() not in FLASHVSR_VIDEO_EXTENSIONS:
            raise VideoGenerationError("FlashVSR 的本地视频仅支持 MP4 / MOV / WebM。")
        if request.duration is not None or request.aspect_ratio is not None or request.resolution is not None:
            raise VideoGenerationError("FlashVSR 输出跟随输入，不能指定时长、比例或分辨率档位。")
        self._options(request)

    def fal_input(self, request: GenerationRequest, url_for) -> dict[str, Any]:
        video = next(r for r in request.references if r.kind == "video")
        return {"video_url": url_for(video), **self._options(request)}
