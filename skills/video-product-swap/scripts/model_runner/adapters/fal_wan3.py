"""fal Wan3 adapters: reference-to-video (换品主力) and text-to-video."""
from __future__ import annotations

from typing import Any

from ..core import GenerationRequest, VideoGenerationError
from .fal_base import FalQueueAdapter

WAN3_LOCAL_RULES = {
    "image": ({".jpg", ".jpeg", ".png", ".webp"}, 10 * 1024 * 1024, 10),
    "video": ({".mp4"}, 90 * 1024 * 1024, 5),
    "audio": ({".mp3", ".wav"}, 15 * 1024 * 1024, 5),
}
WAN3_URL_FIELDS = {
    "image": "reference_image_urls",
    "video": "reference_video_urls",
    "audio": "reference_audio_urls",
}


def check_local_rules(request: GenerationRequest) -> None:
    for kind, (extensions, max_bytes, max_count) in WAN3_LOCAL_RULES.items():
        items = [r for r in request.references if r.kind == kind]
        if len(items) > max_count:
            raise VideoGenerationError(
                f"{request.model.api_model} 最多支持 {max_count} 个{kind}参考，当前为 {len(items)} 个。"
            )
        for reference in items:
            if reference.local_path is None:
                continue
            if reference.local_path.suffix.lower() not in extensions:
                allowed = "/".join(sorted(suffix.lstrip(".").upper() for suffix in extensions))
                raise VideoGenerationError(
                    f"{request.model.api_model} 的本地{kind}参考仅支持 {allowed}：{reference.local_path}"
                )
            if reference.local_path.stat().st_size > max_bytes:
                raise VideoGenerationError(
                    f"{reference.local_path} 超过 {max_bytes // (1024 * 1024)} MB 上限。"
                )


class FalWan3Adapter(FalQueueAdapter):
    """alibaba/wan-3.0/reference-to-video：参考视频 + 参考图生成（换品线路）。"""

    name = "fal_wan3"

    def validate(self, request: GenerationRequest) -> None:
        super().validate(request)
        for key in ("enable_prompt_expansion", "enable_thinking"):
            if key in (request.options or {}) and type(request.options[key]) is not bool:
                raise VideoGenerationError(f"{key} 必须是布尔值。")
        if not request.references:
            raise VideoGenerationError(f"{request.model.api_model} 至少需要一个参考素材。")
        if (request.options or {}).get("strict_media"):
            from media_inspection import probe_audio_duration, probe_frame_rate, probe_video
            video_seconds, audio_seconds = 0.0, 0.0
            try:
                for reference in request.references:
                    if reference.local_path is None:
                        raise VideoGenerationError("画布参考素材需为本地文件。")
                    if reference.kind == "video":
                        video_seconds += probe_video(reference.local_path).duration
                        if probe_frame_rate(reference.local_path) < 16:
                            raise VideoGenerationError("参考视频帧率至少需要 16fps。")
                    elif reference.kind == "audio":
                        audio_seconds += probe_audio_duration(reference.local_path)
                if video_seconds > 15.001:
                    raise VideoGenerationError("参考视频总时长不能超过 15 秒。")
                if audio_seconds > 15.001:
                    raise VideoGenerationError("参考音频总时长不能超过 15 秒。")
            except VideoGenerationError:
                raise
            except (RuntimeError, OSError) as exc:
                raise VideoGenerationError(f"参考素材信息读取失败：{exc}") from exc
        check_local_rules(request)
        request.duration = 5 if request.duration is None else request.duration
        if not 2 <= request.duration <= 30:
            raise VideoGenerationError("Wan3 视频时长必须是 2-30 秒的整数。")

    def fal_input(self, request: GenerationRequest, url_for) -> dict[str, Any]:
        payload: dict[str, Any] = {"prompt": request.prompt}
        for kind, field in WAN3_URL_FIELDS.items():
            values = [url_for(r) for r in request.references if r.kind == kind]
            if values:
                payload[field] = values
        if request.duration is not None:
            payload["duration"] = request.duration
        if request.aspect_ratio is not None:
            payload["aspect_ratio"] = request.aspect_ratio
        if request.resolution is not None:
            payload["resolution"] = request.resolution
        payload["enable_prompt_expansion"] = (request.options or {}).get("enable_prompt_expansion", False)
        if "enable_thinking" in (request.options or {}):
            payload["enable_thinking"] = request.options["enable_thinking"]
        return payload


class FalWan3T2VAdapter(FalQueueAdapter):
    """alibaba/wan-3.0/text-to-video：纯文本生成，不接受参考文件。"""

    name = "fal_wan3_t2v"

    def validate(self, request: GenerationRequest) -> None:
        super().validate(request)
        for key in ("enable_prompt_expansion", "enable_thinking"):
            if key in (request.options or {}) and type(request.options[key]) is not bool:
                raise VideoGenerationError(f"{key} 必须是布尔值。")
        if request.references:
            raise VideoGenerationError(f"{request.model.api_model} 是纯文本模型，不接受参考文件。")
        request.duration = 5 if request.duration is None else request.duration
        if not 2 <= request.duration <= 30:
            raise VideoGenerationError("Wan3 视频时长必须是 2-30 秒的整数。")

    def fal_input(self, request: GenerationRequest, url_for) -> dict[str, Any]:
        payload: dict[str, Any] = {"prompt": request.prompt}
        if request.duration is not None:
            payload["duration"] = request.duration
        if request.aspect_ratio is not None:
            payload["aspect_ratio"] = request.aspect_ratio
        if request.resolution is not None:
            payload["resolution"] = request.resolution
        payload["enable_prompt_expansion"] = (request.options or {}).get("enable_prompt_expansion", False)
        if "enable_thinking" in (request.options or {}):
            payload["enable_thinking"] = request.options["enable_thinking"]
        return payload
