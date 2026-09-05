from __future__ import annotations

import sys
import time
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from ..core import GenerationRequest, Reference, VideoGenerationError, nested
from ..credentials import Credential
from .base import ModelAdapter, same_origin, save_download
from ..task_state import TaskFailed


_SDK_LOCK = threading.RLock()


@contextmanager
def sdk_base(sdk, api_base):
    # DashScope exposes a global base URL; serialize its use, never mutate Key globals.
    with _SDK_LOCK:
        previous = getattr(sdk, "base_http_api_url", None)
        sdk.base_http_api_url = api_base.rstrip("/")
        try:
            yield
        finally:
            sdk.base_http_api_url = previous


VIDEO_EXTENSIONS = {".mp4", ".mov"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}
VIDEO_MAX_BYTES = 100 * 1024 * 1024
IMAGE_MAX_BYTES = 20 * 1024 * 1024
SUCCESS_STATES = {"SUCCEEDED"}
FAILURE_STATES = {"FAILED", "CANCELED", "UNKNOWN"}


def _sdk_media_url(reference: Reference) -> str:
    if reference.local_path is None:
        return reference.value
    return reference.local_path.as_uri()


def _response_payload(response: Any) -> dict[str, Any]:
    if isinstance(response, dict):
        return dict(response)
    try:
        return dict(response)
    except (TypeError, ValueError):
        return {}


def _response_error(response: Any, action: str) -> VideoGenerationError:
    payload = _response_payload(response)
    output = payload.get("output") if isinstance(payload.get("output"), dict) else {}
    code = payload.get("code") or output.get("code") or getattr(response, "code", None)
    message = (
        payload.get("message")
        or output.get("message")
        or getattr(response, "message", None)
        or "无错误详情"
    )
    status_code = payload.get("status_code") or getattr(response, "status_code", None)
    status_text = f"（HTTP {status_code}）" if status_code else ""
    code_text = f"{code}：" if code else ""
    return VideoGenerationError(f"{action}失败{status_text}：{code_text}{message}")


class DashScopeVideoEditAdapter(ModelAdapter):
    """Official Alibaba Cloud Model Studio Wan VideoEdit adapter."""

    name = "dashscope_videoedit"

    def validate(self, request: GenerationRequest) -> None:
        super().validate(request)
        videos = [item for item in request.references if item.kind == "video"]
        images = [item for item in request.references if item.kind == "image"]
        audio = [item for item in request.references if item.kind == "audio"]
        if len(videos) != 1:
            raise VideoGenerationError("Wan 官方 VideoEdit 必须提供且只能提供一个原视频。")
        if not images:
            raise VideoGenerationError("Wan 官方 VideoEdit 至少需要一张产品参考图。")
        if len(images) > 4:
            raise VideoGenerationError("Wan 官方 VideoEdit 最多支持四张参考图。")
        if audio:
            raise VideoGenerationError("Wan 官方 VideoEdit 不接受独立音频参考。")

        self._validate_local(videos[0], VIDEO_EXTENSIONS, VIDEO_MAX_BYTES, "原视频")
        for image in images:
            self._validate_local(image, IMAGE_EXTENSIONS, IMAGE_MAX_BYTES, "参考图")

        if request.duration is not None and not 2 <= request.duration <= 10:
            raise VideoGenerationError("Wan 官方 VideoEdit 视频时长必须是 2-10 秒的整数。")
        if request.resolution not in {None, "720p", "1080p"}:
            raise VideoGenerationError("Wan 官方 VideoEdit 分辨率仅支持 720p 或 1080p。")

    @staticmethod
    def _validate_local(
        reference: Reference,
        extensions: set[str],
        max_bytes: int,
        label: str,
    ) -> None:
        path = reference.local_path
        if path is None:
            return
        if path.suffix.lower() not in extensions:
            allowed = "/".join(sorted(item.lstrip(".").upper() for item in extensions))
            raise VideoGenerationError(f"Wan 官方 VideoEdit 的{label}仅支持 {allowed}：{path}")
        if path.stat().st_size > max_bytes:
            raise VideoGenerationError(
                f"{path} 超过 Wan 官方 {max_bytes // (1024 * 1024)} MB 上限。"
            )

    def remote_payload(self, request: GenerationRequest) -> dict[str, Any]:
        media = [
            {
                "type": "video" if item.kind == "video" else "reference_image",
                "url": _sdk_media_url(item),
            }
            for item in request.references
        ]
        parameters: dict[str, Any] = {
            "prompt_extend": False,
            "watermark": False,
            "audio_setting": "origin",
        }
        if request.duration is not None:
            parameters["duration"] = request.duration
        if request.aspect_ratio is not None:
            parameters["ratio"] = request.aspect_ratio
        if request.resolution is not None:
            parameters["resolution"] = request.resolution.upper()
        return {
            "model": request.model.api_model,
            "input": {"prompt": request.prompt, "media": media},
            "parameters": parameters,
        }

    def execute(self, request: GenerationRequest, credential: Credential) -> dict[str, Any]:
        try:
            import dashscope
            from dashscope import VideoSynthesis
        except ImportError as exc:
            raise VideoGenerationError(
                "缺少 DashScope 官方 SDK，请安装 requirements.txt。"
            ) from exc

        payload = self.remote_payload(request)
        parameters = payload["parameters"]
        with sdk_base(dashscope, credential.api_base):
            try:
                submitted = VideoSynthesis.async_call(
                    api_key=credential.api_key,
                    model=payload["model"],
                    prompt=payload["input"]["prompt"],
                    media=payload["input"]["media"],
                    extend_prompt=False,
                    prompt_extend=parameters["prompt_extend"],
                    watermark=parameters["watermark"],
                    audio_setting=parameters["audio_setting"],
                    duration=parameters.get("duration"),
                    ratio=parameters.get("ratio"),
                    resolution=parameters.get("resolution"),
                )
            except Exception as exc:
                raise VideoGenerationError(f"提交 Wan 官方视频任务失败：{exc}") from exc

            submitted_payload = _response_payload(submitted)
            status_code = submitted_payload.get("status_code") or getattr(
                submitted, "status_code", None
            )
            if status_code not in {None, 200}:
                raise _response_error(submitted, "提交 Wan 官方视频任务")
            task_id = nested(submitted_payload, ("output", "task_id"), ("task_id",))
            if not task_id:
                raise _response_error(submitted, "提交 Wan 官方视频任务")
            if request.task_record is not None:
                request.task_record.update(task_id=str(task_id), status="submitted")
            result = self._poll(VideoSynthesis, str(task_id), credential.api_key)

        video_url = nested(result, ("output", "video_url"), ("video_url",))
        if not video_url:
            raise VideoGenerationError("Wan 官方任务已完成，但没有返回视频 URL。")
        if request.task_record is not None:
            request.task_record.update(video_url=str(video_url), status="generated")
        return self._download(request, credential, str(task_id), str(video_url))

    def resume(self, request, credential):
        state = request.task_record.data
        video_url = state.get("video_url")
        if not video_url:
            import dashscope
            from dashscope import VideoSynthesis
            with sdk_base(dashscope, credential.api_base):
                result = self._poll(VideoSynthesis, state["task_id"], credential.api_key)
            video_url = nested(result, ("output", "video_url"), ("video_url",))
            if not video_url:
                raise VideoGenerationError("Wan 官方任务尚未返回视频 URL。")
            request.task_record.update(video_url=str(video_url), status="generated")
        return self._download(request, credential, state.get("task_id"), str(video_url))

    @staticmethod
    def _poll(
        video_synthesis: Any,
        task_id: str,
        api_key: str | None,
        timeout: float = 1800,
        interval: float = 15,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                response = video_synthesis.fetch(task_id, api_key=api_key)
            except Exception as exc:
                raise VideoGenerationError(f"查询 Wan 官方视频任务失败：{exc}") from exc
            payload = _response_payload(response)
            status_code = payload.get("status_code") or getattr(response, "status_code", None)
            if status_code not in {None, 200}:
                raise _response_error(response, "查询 Wan 官方视频任务")
            status = str(nested(payload, ("output", "task_status"), ("task_status",)) or "").upper()
            if status in SUCCESS_STATES:
                return payload
            if status in FAILURE_STATES:
                raise TaskFailed(str(_response_error(response, "Wan 官方视频任务")))
            print(f"Wan 官方视频任务状态：{status or '未知'}", file=sys.stderr, flush=True)
            time.sleep(interval)
        raise VideoGenerationError(f"等待 Wan 官方视频任务完成超时（{timeout:g} 秒）。")

    def _download(
        self,
        request: GenerationRequest,
        credential: Credential,
        task_id: str,
        video_url: str,
    ) -> dict[str, Any]:
        try:
            import requests
        except ImportError as exc:
            raise VideoGenerationError("缺少 requests，请安装 requirements.txt。") from exc

        common_result = {
            "success": True,
            "model": request.model.api_model,
            "provider": request.model.provider,
            "adapter": self.name,
            "reference_count": len(request.references),
            "video_url": video_url,
            "task_id": task_id,
            "technical_completed": True,
            "quality_passed": None,
        }
        headers = (
            {"Authorization": f"Bearer {credential.api_key}"}
            if same_origin(video_url, credential.api_base)
            else {}
        )
        try:
            response = requests.get(video_url, headers=headers, timeout=300)
        except Exception as exc:
            return {
                **common_result,
                "output": None,
                "delivery": "url_fallback",
                "local_downloaded": False,
                "download_error": f"下载视频失败：{exc}",
            }
        if not response.ok or not response.content:
            detail = (
                f"HTTP {response.status_code}" if not response.ok else "返回内容为空"
            )
            return {
                **common_result,
                "output": None,
                "delivery": "url_fallback",
                "local_downloaded": False,
                "download_error": f"下载视频失败（{detail}）。",
            }
        try:
            save_download(request, response.content)
        except OSError as exc:
            return {**common_result, "output": None, "delivery": "url_fallback",
                    "local_downloaded": False, "download_error": f"保存视频失败：{exc}"}
        return {
            **common_result,
            "output": str(request.output),
            "delivery": "local_file",
            "local_downloaded": True,
        }
