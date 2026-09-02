from __future__ import annotations

from ..core import GenerationRequest, Reference, VideoGenerationError
from .base import KaiyunRelayAdapter


WAN3_UPLOAD_FIELDS = {"image": "images", "video": "videos", "audio": "audio"}
WAN3_REMOTE_FIELDS = {
    "image": "images",
    "video": "reference_video_urls",
    "audio": "audio_urls",
}
WAN3_LOCAL_RULES = {
    "image": ({".jpg", ".jpeg", ".png", ".webp"}, 10 * 1024 * 1024, 10),
    "video": ({".mp4"}, 100 * 1024 * 1024, 5),
    "audio": ({".mp3"}, 15 * 1024 * 1024, 5),
}


class Wan3Adapter(KaiyunRelayAdapter):
    name = "wan3"

    def validate(self, request: GenerationRequest) -> None:
        super().validate(request)
        grouped = {
            kind: [reference for reference in request.references if reference.kind == kind]
            for kind in WAN3_LOCAL_RULES
        }
        if not grouped["image"]:
            raise VideoGenerationError(f"{request.model.api_model} 至少需要一张参考图片。")
        for kind, items in grouped.items():
            extensions, max_bytes, max_count = WAN3_LOCAL_RULES[kind]
            if len(items) > max_count:
                raise VideoGenerationError(
                    f"{request.model.api_model} 最多支持 {max_count} 个{kind}参考，当前为 {len(items)} 个。"
                )
            for reference in items:
                if reference.local_path is None:
                    continue
                if reference.local_path.suffix.lower() not in extensions:
                    allowed = "/".join(
                        sorted(suffix.lstrip(".").upper() for suffix in extensions)
                    )
                    raise VideoGenerationError(
                        f"{request.model.api_model} 的本地{kind}参考仅支持 {allowed}：{reference.local_path}"
                    )
                if reference.local_path.stat().st_size > max_bytes:
                    raise VideoGenerationError(
                        f"{reference.local_path} 超过 {max_bytes // (1024 * 1024)} MB 上限。"
                    )
        request.duration = 5 if request.duration is None else request.duration
        if not 2 <= request.duration <= 30:
            raise VideoGenerationError("Wan3 视频时长必须是 2-30 秒的整数。")

    def upload_field(self, reference: Reference) -> str:
        return WAN3_UPLOAD_FIELDS[reference.kind]

    def remote_payload(self, request: GenerationRequest):
        payload = self.build_payload(request)
        for kind, field in WAN3_REMOTE_FIELDS.items():
            values = [
                reference.value
                for reference in request.references
                if reference.kind == kind and reference.local_path is None
            ]
            if values:
                payload[field] = values
        return payload
