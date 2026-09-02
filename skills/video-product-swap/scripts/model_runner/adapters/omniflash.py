from __future__ import annotations

from ..core import GenerationRequest, VideoGenerationError
from .base import KaiyunRelayAdapter


class OmniFlashAdapter(KaiyunRelayAdapter):
    name = "omniflash"

    def validate(self, request: GenerationRequest) -> None:
        super().validate(request)
        videos = [reference for reference in request.references if reference.kind == "video"]
        images = [reference for reference in request.references if reference.kind == "image"]
        audio = [reference for reference in request.references if reference.kind == "audio"]
        if len(videos) != 1 or videos[0].local_path is None:
            raise VideoGenerationError("OmniFlash 需要且只接受一个本地源视频。")
        if videos[0].local_path.suffix.lower() != ".mp4":
            raise VideoGenerationError("OmniFlash 的本地源视频必须是 MP4。")
        if not images or any(reference.local_path is not None for reference in images):
            raise VideoGenerationError("OmniFlash 至少需要一个公网 HTTPS 参考图，不接受本地参考图。")
        if audio:
            raise VideoGenerationError("OmniFlash 当前适配器不接受音频参考。")

    def remote_payload(self, request: GenerationRequest):
        payload = super().remote_payload(request)
        payload.pop("video_url", None)
        return payload
