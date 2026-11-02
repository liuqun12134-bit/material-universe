"""fal sync-3 lipsync adapter: 视频 + 音频 -> 换口型视频。"""
from __future__ import annotations

import math
from typing import Any

from ..core import GenerationRequest, VideoGenerationError
from .fal_base import FalQueueAdapter

SYNC3_MAX_SECONDS = 600.0  # sync-3 视频/音频时长上限（10 分钟）
SYNC3_MAX_WIDTH = 4096  # sync-3 输入分辨率上限 4K（4096x2160）
SYNC3_MAX_HEIGHT = 2160
SYNC3_VIDEO_EXTENSIONS = {".mp4", ".mov", ".webm"}
SYNC3_AUDIO_EXTENSIONS = {".mp3", ".wav", ".m4a", ".aac"}


class FalSync3Adapter(FalQueueAdapter):
    """fal-ai/sync-lipsync/v3：一个视频 + 一段音频换口型。

    sync_mode 固定使用 fal 默认 cut_off：输出时长取视频和音频中较短者。
    """

    name = "fal_sync3"

    def validate(self, request: GenerationRequest) -> None:
        # 不调用 super().validate()：sync-3 不需要提示词
        videos = [r for r in request.references if r.kind == "video"]
        audios = [r for r in request.references if r.kind == "audio"]
        images = [r for r in request.references if r.kind == "image"]
        if len(videos) != 1 or len(audios) != 1:
            raise VideoGenerationError("Sync3 换口型需要且只接受一个视频和一段音频。")
        if images:
            raise VideoGenerationError("Sync3 换口型不接受图片参考。")
        video, audio = videos[0], audios[0]
        if video.local_path is not None:
            if video.local_path.suffix.lower() not in SYNC3_VIDEO_EXTENSIONS:
                raise VideoGenerationError("Sync3 的本地视频仅支持 MP4 / MOV / WebM。")
            try:
                from media_inspection import probe_video
                info = probe_video(video.local_path)
            except Exception as exc:
                if (request.options or {}).get("strict_media"):
                    raise VideoGenerationError(f"Sync3 视频无法读取，尚未提交：{exc}") from exc
                info = None  # 旧业务入口仍交给 fal 侧校验
            if info is not None and (not math.isfinite(info.duration) or info.duration <= 0):
                if (request.options or {}).get("strict_media"):
                    raise VideoGenerationError("Sync3 视频时长无效，尚未提交。")
            if info is not None:
                if math.isfinite(info.duration) and info.duration > SYNC3_MAX_SECONDS:
                    raise VideoGenerationError(
                        f"Sync3 视频最长 {SYNC3_MAX_SECONDS:g} 秒，当前为 {info.duration:g} 秒，未提交。"
                    )
                if info.width > SYNC3_MAX_WIDTH or info.height > SYNC3_MAX_HEIGHT:
                    raise VideoGenerationError(
                        f"Sync3 视频分辨率最高 4K（{SYNC3_MAX_WIDTH}x{SYNC3_MAX_HEIGHT}），"
                        f"当前为 {info.width}x{info.height}，未提交。"
                    )
        if audio.local_path is not None:
            if audio.local_path.suffix.lower() not in SYNC3_AUDIO_EXTENSIONS:
                raise VideoGenerationError("Sync3 的本地音频仅支持 MP3 / WAV / M4A / AAC。")
            try:
                from media_inspection import probe_audio_duration
                seconds = probe_audio_duration(audio.local_path)
            except Exception as exc:
                if (request.options or {}).get("strict_media"):
                    raise VideoGenerationError(f"Sync3 音频无法读取，尚未提交：{exc}") from exc
                seconds = None
            if seconds is not None and (not math.isfinite(seconds) or seconds <= 0):
                if (request.options or {}).get("strict_media"):
                    raise VideoGenerationError("Sync3 音频时长无效，尚未提交。")
            if seconds is not None and math.isfinite(seconds) and seconds > SYNC3_MAX_SECONDS:
                raise VideoGenerationError(
                    f"Sync3 音频最长 {SYNC3_MAX_SECONDS:g} 秒，当前为 {seconds:g} 秒，未提交。"
                )
        if request.duration is not None or request.aspect_ratio is not None or request.resolution is not None:
            raise VideoGenerationError("Sync3 输出跟随输入，不能指定时长、比例或分辨率。")

    def fal_input(self, request: GenerationRequest, url_for) -> dict[str, Any]:
        video = next(r for r in request.references if r.kind == "video")
        audio = next(r for r in request.references if r.kind == "audio")
        return {"video_url": url_for(video), "audio_url": url_for(audio)}
