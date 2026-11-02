from __future__ import annotations

from abc import ABC
from typing import Any
from urllib.parse import urljoin, urlparse

from ..core import (
    GenerationRequest,
    Reference,
    VideoGenerationError,
    nested,
    result_url,
)
from ..credentials import Credential


SUCCESS_STATES = {"completed", "succeeded", "success"}
FAILURE_STATES = {"failed", "failure", "error", "cancelled", "canceled"}


def same_origin(a: str, b: str) -> bool:
    left, right = urlparse(a), urlparse(b)
    return (left.scheme.lower(), left.netloc.lower()) == (
        right.scheme.lower(),
        right.netloc.lower(),
    )


def response_json(response: Any, action: str) -> dict[str, Any]:
    if not response.ok:
        detail = (response.text or "").strip()[:1000]
        raise VideoGenerationError(
            f"{action}失败（HTTP {response.status_code}）：{detail or '无错误详情'}"
        )
    try:
        payload = response.json()
    except Exception as exc:
        raise VideoGenerationError(f"{action}返回的不是有效 JSON。") from exc
    if not isinstance(payload, dict):
        raise VideoGenerationError(f"{action}返回格式不正确。")
    return payload


class ModelAdapter(ABC):
    name = "base"

    def validate(self, request: GenerationRequest) -> None:
        if not request.prompt.strip():
            raise VideoGenerationError("提示词不能为空。")
        if request.duration is not None and request.duration <= 0:
            raise VideoGenerationError("视频秒数必须大于 0。")
        if request.aspect_ratio not in {None, "9:16", "16:9", "1:1", "4:3", "3:4"}:
            raise VideoGenerationError(f"不支持的视频比例：{request.aspect_ratio}")
        if request.resolution not in {None, "480p", "720p", "1080p"}:
            raise VideoGenerationError(f"不支持的分辨率：{request.resolution}")

    def build_payload(self, request: GenerationRequest) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": request.model.api_model,
            "prompt": request.prompt,
        }
        if request.duration is not None:
            payload["duration"] = request.duration
        if request.aspect_ratio is not None:
            payload["aspect_ratio"] = request.aspect_ratio
        if request.resolution is not None:
            payload["resolution"] = request.resolution
        return payload

    def upload_field(self, reference: Reference) -> str:
        return reference.kind

    def submit_endpoint(self, request: GenerationRequest, credential: Credential) -> str:
        return urljoin(credential.api_base + "/", credential.submit_path)

    def plan(self, request: GenerationRequest, credential: Credential) -> dict[str, Any]:
        payload = self.remote_payload(request)
        uploads = [
            {
                "kind": reference.kind,
                "field": self.upload_field(reference),
                "path": str(reference.local_path),
            }
            for reference in request.references
            if reference.local_path is not None
        ]
        return {
            "success": True,
            "dry_run": True,
            "requested_model": request.requested_model,
            "resolved_model": request.model.api_model,
            "registered_model": request.model.registered,
            "provider": request.model.provider,
            "adapter": self.name,
            "credential_profile": credential.provider,
            "credential_source": credential.api_key_source,
            "credential_configured": bool(credential.api_key),
            "submit_endpoint": self.submit_endpoint(request, credential),
            "request_payload": payload,
            "local_uploads": uploads,
            "output": str(request.output),
        }

    def remote_payload(self, request: GenerationRequest) -> dict[str, Any]:
        return self.build_payload(request)

    def execute(self, request: GenerationRequest, credential: Credential) -> dict[str, Any]:
        raise NotImplementedError

    def deliver(
        self,
        request: GenerationRequest,
        session: Any,
        video_url: str,
        task_id: str | None,
        download_headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Shared result assembly: download the finished video or fall back to its URL."""
        if request.progress:
            request.progress("下载成片")
        common_result = {
            "success": True,
            "model": request.model.api_model,
            "provider": request.model.provider,
            "adapter": self.name,
            "reference_count": len(request.references),
            "video_url": video_url,
            "output_kind": "image" if request.output.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"} else "video",
            "task_id": str(task_id) if task_id else None,
            "technical_completed": True,
            "quality_passed": None,
        }
        try:
            response = session.get(video_url, headers=download_headers or {}, timeout=300)
        except Exception as exc:
            return {
                **common_result,
                "output": None,
                "delivery": "url_fallback",
                "local_downloaded": False,
                "download_error": f"下载视频失败：{exc}",
            }
        if not response.ok:
            return {
                **common_result,
                "output": None,
                "delivery": "url_fallback",
                "local_downloaded": False,
                "download_error": f"下载视频失败（HTTP {response.status_code}）。",
            }
        if not response.content:
            return {
                **common_result,
                "output": None,
                "delivery": "url_fallback",
                "local_downloaded": False,
                "download_error": "下载结果为空。",
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


def extract_video_url(payload: dict[str, Any]) -> str | None:
    """Find a video URL in a provider response, fal shape first."""
    value = nested(payload, ("video", "url"))
    if value:
        return str(value)
    return result_url(payload)


def save_download(request: GenerationRequest, content: bytes) -> None:
    request.output.parent.mkdir(parents=True, exist_ok=True)
    if request.output.exists():
        raise FileExistsError(f"输出文件已存在：{request.output}")
    temporary = request.output.with_name(request.output.name + ".part")
    temporary.write_bytes(content)
    if request.output.exists():
        raise FileExistsError(f"输出文件已存在：{request.output}")
    temporary.replace(request.output)
