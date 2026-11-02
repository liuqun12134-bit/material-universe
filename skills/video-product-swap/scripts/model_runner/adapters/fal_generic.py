"""fal generic fallback adapter for unregistered model IDs."""
from __future__ import annotations

from typing import Any

from ..core import GenerationRequest, VideoGenerationError
from .fal_base import FalQueueAdapter


class FalGenericAdapter(FalQueueAdapter):
    """未登记 fal 模型的兼容提交：prompt + 规格 + 常见参考字段。"""

    name = "fal_generic"

    def fal_input(self, request: GenerationRequest, url_for) -> dict[str, Any]:
        payload: dict[str, Any] = {"prompt": request.prompt}
        for kind in ("image", "video", "audio"):
            values = [url_for(r) for r in request.references if r.kind == kind]
            if not values:
                continue
            payload[f"{kind}_url"] = values[0]
            if len(values) > 1:
                payload[f"{kind}_urls"] = values
        if request.duration is not None:
            payload["duration"] = request.duration
        if request.aspect_ratio is not None:
            payload["aspect_ratio"] = request.aspect_ratio
        if request.resolution is not None:
            payload["resolution"] = request.resolution
        return payload
