from __future__ import annotations

import json
import sys
import time
from abc import ABC
from typing import Any, BinaryIO
from urllib.parse import urljoin, urlparse

from ..core import (
    GenerationRequest,
    Reference,
    VideoGenerationError,
    nested,
    result_url,
)
from ..credentials import Credential
from ..task_state import TaskFailed


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
            "submit_endpoint": urljoin(credential.api_base + "/", credential.submit_path),
            "request_payload": payload,
            "local_uploads": uploads,
            "output": str(request.output),
        }

    def remote_payload(self, request: GenerationRequest) -> dict[str, Any]:
        return self.build_payload(request)

    def execute(self, request: GenerationRequest, credential: Credential) -> dict[str, Any]:
        raise NotImplementedError


class KaiyunRelayAdapter(ModelAdapter):
    def remote_payload(self, request: GenerationRequest) -> dict[str, Any]:
        payload = self.build_payload(request)
        remote = [reference for reference in request.references if reference.local_path is None]
        if not remote:
            return payload
        content: list[dict[str, Any]] = [{"type": "text", "text": request.prompt}]
        first_by_kind: dict[str, str] = {}
        for reference in remote:
            first_by_kind.setdefault(reference.kind, reference.value)
            key = f"{reference.kind}_url"
            content.append({"type": key, key: {"url": reference.value}})
        payload["messages"] = [{"role": "user", "content": content}]
        for kind, url in first_by_kind.items():
            payload[f"{kind}_url"] = url
        return payload

    def open_uploads(
        self, references: list[Reference]
    ) -> tuple[list[tuple[str, tuple[str, BinaryIO, str]]], list[BinaryIO]]:
        uploads: list[tuple[str, tuple[str, BinaryIO, str]]] = []
        handles: list[BinaryIO] = []
        for reference in references:
            if reference.local_path is None:
                continue
            handle = reference.local_path.open("rb")
            handles.append(handle)
            uploads.append(
                (
                    self.upload_field(reference),
                    (reference.local_path.name, handle, reference.mime_type),
                )
            )
        return uploads, handles

    @staticmethod
    def multipart_data(payload: dict[str, Any]) -> list[tuple[str, str]]:
        data: list[tuple[str, str]] = []
        for key, value in payload.items():
            if isinstance(value, list) and all(isinstance(item, str) for item in value):
                data.extend((key, item) for item in value)
            elif isinstance(value, (dict, list)):
                data.append((key, json.dumps(value, ensure_ascii=False)))
            else:
                data.append((key, str(value)))
        return data

    def submit(
        self,
        session: Any,
        endpoint: str,
        headers: dict[str, str],
        payload: dict[str, Any],
        request: GenerationRequest,
    ) -> dict[str, Any]:
        uploads, handles = self.open_uploads(request.references)
        try:
            if uploads:
                response = session.post(
                    endpoint,
                    headers=headers,
                    data=self.multipart_data(payload),
                    files=uploads,
                    timeout=300,
                )
            else:
                response = session.post(endpoint, headers=headers, json=payload, timeout=300)
        except Exception as exc:
            raise VideoGenerationError(f"提交视频任务失败：{exc}") from exc
        finally:
            for handle in handles:
                handle.close()
        return response_json(response, "提交视频任务")

    @staticmethod
    def poll(
        session: Any,
        url: str,
        headers: dict[str, str],
        timeout: float = 1800,
        interval: float = 5,
        result_timeout: float = 120,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        success_seen_at: float | None = None
        while time.monotonic() < deadline:
            try:
                response = session.get(url, headers=headers, timeout=60)
            except Exception as exc:
                raise VideoGenerationError(f"查询视频任务失败：{exc}") from exc
            payload = response_json(response, "查询视频任务")
            status = str(
                nested(payload, ("status",), ("data", "status"), ("result", "status"))
                or ""
            ).lower()
            if result_url(payload):
                return payload
            if status in FAILURE_STATES:
                detail = nested(payload, ("error",), ("message",), ("data", "error"))
                raise TaskFailed(f"视频任务失败：{detail or status}")
            if status in SUCCESS_STATES:
                now = time.monotonic()
                success_seen_at = success_seen_at or now
                if now - success_seen_at >= result_timeout:
                    raise VideoGenerationError(
                        f"任务已完成，但等待 {result_timeout:g} 秒仍未返回视频 URL。"
                    )
                print("视频任务已完成，正在等待成片地址……", file=sys.stderr, flush=True)
            else:
                print(f"视频任务状态：{status or '未知'}", file=sys.stderr, flush=True)
            time.sleep(interval)
        if success_seen_at is not None:
            raise VideoGenerationError("任务已完成，但等待超时仍未返回视频 URL。")
        raise VideoGenerationError(f"等待视频任务完成超时（{timeout:g} 秒）。")

    def execute(self, request: GenerationRequest, credential: Credential) -> dict[str, Any]:
        try:
            import requests
        except ImportError as exc:
            raise VideoGenerationError("缺少 requests，请安装 requirements.txt。") from exc
        endpoint = urljoin(credential.api_base + "/", credential.submit_path)
        headers = {"Authorization": f"Bearer {credential.api_key}"}
        session = requests.Session()
        submitted = self.submit(
            session, endpoint, headers, self.remote_payload(request), request
        )
        task_id = nested(
            submitted, ("id",), ("task_id",), ("data", "id"), ("data", "task_id")
        )
        video_url = result_url(submitted)
        poll_url = nested(submitted, ("poll_url",), ("data", "poll_url"))
        if poll_url:
            poll_url = urljoin(endpoint + "/", str(poll_url))
        elif task_id:
            poll_url = f"{endpoint}/{task_id}"
        if request.task_record is not None:
            request.task_record.update(task_id=str(task_id) if task_id else None,
                                       poll_url=poll_url, video_url=video_url, status="submitted")
        return self._continue(request, credential, session, task_id, video_url, poll_url)

    def resume(self, request: GenerationRequest, credential: Credential) -> dict[str, Any]:
        import requests
        state = request.task_record.data
        return self._continue(request, credential, requests.Session(), state.get("task_id"),
                              state.get("video_url"), state.get("poll_url"))

    def _continue(self, request, credential, session, task_id, video_url, poll_url):
        headers = {"Authorization": f"Bearer {credential.api_key}"}
        if not video_url:
            if not poll_url:
                raise VideoGenerationError("提交响应中没有任务 ID、poll_url 或结果 URL。")
            if not same_origin(poll_url, credential.api_base):
                raise VideoGenerationError("轮询地址与 API 不同源，已停止以避免泄露密钥。")
            video_url = result_url(self.poll(session, poll_url, headers))
        if not video_url:
            raise VideoGenerationError("任务已完成，但没有返回视频 URL。")
        if request.task_record is not None:
            request.task_record.update(video_url=video_url, status="generated")
        common_result = {
            "success": True,
            "model": request.model.api_model,
            "provider": request.model.provider,
            "adapter": self.name,
            "reference_count": len(request.references),
            "video_url": video_url,
            "task_id": str(task_id) if task_id else None,
            "technical_completed": True,
            "quality_passed": None,
        }
        download_headers = headers if same_origin(video_url, credential.api_base) else {}
        try:
            response = session.get(video_url, headers=download_headers, timeout=300)
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


def save_download(request: GenerationRequest, content: bytes) -> None:
    request.output.parent.mkdir(parents=True, exist_ok=True)
    if request.output.exists():
        raise FileExistsError(f"输出文件已存在：{request.output}")
    temporary = request.output.with_name(request.output.name + ".part")
    temporary.write_bytes(content)
    if request.output.exists():
        raise FileExistsError(f"输出文件已存在：{request.output}")
    temporary.replace(request.output)
