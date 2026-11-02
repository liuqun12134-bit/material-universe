"""fal FLUX.1 Schnell: one PNG image from the exact supplied prompt."""
from __future__ import annotations

from typing import Any

from ..core import GenerationRequest, VideoGenerationError, nested
from .fal_base import FalQueueAdapter


IMAGE_SIZES = {
    "square_hd", "square", "portrait_4_3", "portrait_16_9",
    "landscape_4_3", "landscape_16_9",
}


class FalFluxSchnellAdapter(FalQueueAdapter):
    name = "fal_flux_schnell"

    def validate(self, request: GenerationRequest) -> None:
        super().validate(request)
        if request.references:
            raise VideoGenerationError("FLUX Schnell 文生图不接受参考素材。")
        if request.duration is not None or request.aspect_ratio is not None or request.resolution is not None:
            raise VideoGenerationError("FLUX Schnell 不接受视频输出参数。")
        options = request.options or {}
        if set(options) != {"image_size"} or options["image_size"] not in IMAGE_SIZES:
            raise VideoGenerationError("FLUX Schnell 仅接受已列出的图片尺寸。")
        if request.output.suffix.lower() != ".png":
            raise VideoGenerationError("FLUX Schnell 输出必须为 PNG。")

    def fal_input(self, request: GenerationRequest, url_for) -> dict[str, Any]:
        return {"prompt": request.prompt, "image_size": request.options["image_size"],
                "num_images": 1, "output_format": "png"}

    def result_url(self, payload: dict[str, Any]) -> str | None:
        value = nested(payload, ("images", 0, "url"))
        return str(value) if value else None
