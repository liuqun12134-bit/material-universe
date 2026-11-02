"""fal.ai queue adapter: storage upload, submit, poll, result, resume."""
from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any

from ..core import GenerationRequest, Reference, VideoGenerationError, nested
from ..credentials import Credential
from ..task_state import TaskFailed
from .base import (
    FAILURE_STATES,
    ModelAdapter,
    extract_video_url,
    response_json,
)

FAL_STORAGE_INITIATE = "https://rest.alpha.fal.ai/storage/upload/initiate"
FAL_SINGLE_PUT_LIMIT = 90 * 1024 * 1024  # fal 单 PUT 上限；更大文件需分段上传，暂不适用
POLL_TIMEOUT = 1800.0
POLL_INTERVAL = 5.0
STORAGE_ATTEMPTS = 3
TRANSIENT_HTTP = {408, 429, 500, 502, 503, 504}


def storage_request(send, action: str):
    """Storage only: retry allocation or PUT to the same object, never generation."""
    import requests

    reason = ""
    for attempt in range(STORAGE_ATTEMPTS):
        try:
            response = send()
        except (requests.ConnectionError, requests.Timeout, ConnectionResetError) as exc:
            # Do not expose signed upload URLs contained in requests exceptions.
            reason = f"网络连接中断（{type(exc).__name__}）"
        else:
            if response.ok:
                return response
            reason = f"HTTP {response.status_code}"
            retryable = response.status_code in TRANSIENT_HTTP
            response.close()
            if not retryable:
                raise VideoGenerationError(f"{action}失败（{reason}），尚未提交视频生成。")
        if attempt + 1 < STORAGE_ATTEMPTS:
            print(f"{action}时{reason}，正在重试上传环节（{attempt + 2}/{STORAGE_ATTEMPTS}）……",
                  file=sys.stderr, flush=True)
            time.sleep(2 ** attempt)
    raise VideoGenerationError(
        f"{action}失败：{reason}，已尝试 {STORAGE_ATTEMPTS} 次。\n"
        "素材尚未上传完成，未提交视频生成。请检查网络；如开启了 VPN/代理，可关闭后再试。"
    )


def check_connection(session: Any, endpoint: str) -> None:
    """Warm up TLS using a non-billable HEAD; never retry a generation POST."""
    import requests

    for attempt in range(3):
        try:
            response = session.head(endpoint, timeout=15, allow_redirects=False)
            response.close()
            # Even 404/405 proves the TLS connection reached an HTTP server.
            return
        except (requests.ConnectionError, requests.Timeout) as exc:
            if attempt == 2:
                raise VideoGenerationError(
                    "连接 fal 视频服务失败，尚未提交生成任务。请检查网络或代理后重新开始。"
                    f"\n连接详情：{exc}"
                ) from exc
            time.sleep(1)


def queue_get(session: Any, url: str, **kwargs):
    """Retry transient read-only queue requests without resubmitting generation."""
    import requests

    for attempt in range(3):
        try:
            response = session.get(url, **kwargs)
        except (requests.ConnectionError, requests.Timeout):
            if attempt == 2:
                raise
        else:
            if response.status_code not in (408, 429, 500, 502, 503, 504) or attempt == 2:
                return response
            response.close()
        time.sleep(1 + attempt)


def auth_headers(credential: Credential) -> dict[str, str]:
    return {"Authorization": f"Key {credential.api_key}"}


def queue_base(api_model: str) -> str:
    """fal 队列状态/结果 URL 挂在应用（owner/app 两段）下，不含子路径。

    例如 alibaba/wan-3.0/reference-to-video 的队列 URL 前缀是 alibaba/wan-3.0。
    """
    parts = [part for part in str(api_model).split("/") if part]
    return "/".join(parts[:2])


def status_url(credential: Credential, api_model: str, request_id: str) -> str:
    return f"{credential.api_base}/{queue_base(api_model)}/requests/{request_id}/status"


def response_url(credential: Credential, api_model: str, request_id: str) -> str:
    return f"{credential.api_base}/{queue_base(api_model)}/requests/{request_id}"


class FalQueueAdapter(ModelAdapter):
    name = "fal_base"

    def result_url(self, payload: dict[str, Any]) -> str | None:
        return extract_video_url(payload)

    def submit_endpoint(self, request: GenerationRequest, credential: Credential) -> str:
        return f"{credential.api_base}/{request.model.api_model}"

    # ---- payload construction -------------------------------------------------

    def fal_input(self, request: GenerationRequest, url_for) -> dict[str, Any]:
        """Build fal input arguments; url_for(reference) resolves a reference to a URL."""
        raise NotImplementedError

    def remote_payload(self, request: GenerationRequest) -> dict[str, Any]:
        def dry_url(reference: Reference) -> str:
            if reference.local_path is None:
                return reference.value
            return f"fal-storage://{reference.local_path.name}"

        return self.fal_input(request, dry_url)

    # ---- fal storage upload ----------------------------------------------------

    def upload_file(self, session: Any, credential: Credential, path: Path, mime: str) -> str:
        size = path.stat().st_size
        if size > FAL_SINGLE_PUT_LIMIT:
            raise VideoGenerationError(
                f"{path} 为 {size // (1024 * 1024)} MB，超过 fal 单次上传 90 MB 上限。"
            )
        initiate = storage_request(
            lambda: session.post(
                FAL_STORAGE_INITIATE,
                headers=auth_headers(credential),
                json={"content_type": mime, "file_name": path.name},
                timeout=60,
            ), "申请 fal 上传地址",
        )
        try:
            payload = response_json(initiate, "申请 fal 上传地址")
        finally:
            initiate.close()
        upload_url = nested(payload, ("upload_url",))
        file_url = nested(payload, ("file_url",))
        if not upload_url or not file_url:
            raise VideoGenerationError("fal 上传地址响应缺少 upload_url 或 file_url。")
        def put_file():
            # Reopen for each attempt: a disconnected upload may have consumed part
            # or all of the stream. Always replace the SAME storage object in full.
            with path.open("rb") as data:
                return session.put(
                    str(upload_url), data=data,
                    headers={"Content-Type": mime}, timeout=(30, 300),
                )

        uploaded = storage_request(put_file, f"上传 {path.name} 到 fal 存储")
        uploaded.close()
        return str(file_url)

    def upload_references(
        self, session: Any, credential: Credential, request: GenerationRequest
    ) -> dict[int, str]:
        uploaded: dict[int, str] = {}
        for reference in request.references:
            if reference.local_path is None:
                continue
            if request.progress:
                request.progress(f"上传素材（{reference.kind}）")
            print(f"正在上传 {reference.local_path.name} 到 fal 存储……", file=sys.stderr, flush=True)
            uploaded[id(reference)] = self.upload_file(
                session, credential, reference.local_path, reference.mime_type
            )
        return uploaded

    # ---- execution --------------------------------------------------------------

    def execute(self, request: GenerationRequest, credential: Credential) -> dict[str, Any]:
        try:
            import requests
        except ImportError as exc:
            raise VideoGenerationError("缺少 requests，请安装 requirements.txt。") from exc
        session = requests.Session()
        if request.task_record is not None:
            request.task_record.update(status="preparing")
        check_connection(session, self.submit_endpoint(request, credential))
        uploaded = self.upload_references(session, credential, request)

        def final_url(reference: Reference) -> str:
            if reference.local_path is None:
                return reference.value
            return uploaded[id(reference)]

        payload = self.fal_input(request, final_url)
        endpoint = self.submit_endpoint(request, credential)
        if request.progress:
            request.progress("提交视频任务")
        if request.task_record is not None:
            request.task_record.update(status="submitting")
        try:
            response = session.post(endpoint, headers=auth_headers(credential), json=payload, timeout=300)
        except Exception as exc:
            detail = "与 fal 视频服务的安全连接中断" if isinstance(exc, requests.exceptions.SSLError) else "网络请求失败"
            raise VideoGenerationError(f"提交视频任务时{detail}，未收到任务编号。\n连接详情：{exc}") from exc
        submitted = response_json(response, "提交视频任务")
        request_id = nested(submitted, ("request_id",), ("id",), ("gateway_request_id",))
        if not request_id:
            raise VideoGenerationError("fal 提交响应中没有 request_id。")
        request_id = str(request_id)
        # 提交响应里的 status_url/response_url 是权威地址，保存下来供查询与 --resume 使用
        queue_urls = {
            key: str(submitted[key])
            for key in ("status_url", "response_url")
            if submitted.get(key)
        }
        if request.task_record is not None:
            request.task_record.update(task_id=request_id, status="submitted", **queue_urls)
        return self._wait_and_deliver(request, credential, session, request_id)

    def resume(self, request: GenerationRequest, credential: Credential) -> dict[str, Any]:
        import requests

        state = request.task_record.data
        session = requests.Session()
        video_url = state.get("video_url")
        if video_url:
            return self.deliver(request, session, str(video_url), state.get("task_id"))
        request_id = state.get("task_id")
        if not request_id:
            raise VideoGenerationError("任务记录中没有 fal request_id，无法继续查询。")
        return self._wait_and_deliver(request, credential, session, str(request_id))

    def _wait_and_deliver(
        self,
        request: GenerationRequest,
        credential: Credential,
        session: Any,
        request_id: str,
    ) -> dict[str, Any]:
        stored = request.task_record.data if request.task_record is not None else {}
        video_url = self._poll(
            session, credential, request.model.api_model, request_id,
            stored_status_url=stored.get("status_url"),
            stored_response_url=stored.get("response_url"),
            progress=request.progress,
        )
        if request.task_record is not None:
            request.task_record.update(video_url=video_url, status="generated")
        return self.deliver(request, session, video_url, request_id)

    def _poll(
        self,
        session: Any,
        credential: Credential,
        api_model: str,
        request_id: str,
        stored_status_url: str | None = None,
        stored_response_url: str | None = None,
        progress=None,
    ) -> str:
        headers = auth_headers(credential)
        status = stored_status_url or status_url(credential, api_model, request_id)
        deadline = time.monotonic() + POLL_TIMEOUT
        while time.monotonic() < deadline:
            try:
                response = queue_get(session, status, headers=headers, timeout=60)
            except Exception as exc:
                raise VideoGenerationError(f"查询视频任务失败：{exc}") from exc
            payload = response_json(response, "查询视频任务")
            state = str(payload.get("status", "")).strip()
            if state.lower() in FAILURE_STATES:
                detail = nested(payload, ("error",), ("message",), ("detail",))
                raise TaskFailed(f"视频任务失败：{detail or state}")
            if state == "COMPLETED":
                return self._fetch_result(
                    session, credential, api_model, request_id,
                    stored_response_url=stored_response_url,
                )
            if state in ("IN_QUEUE", "IN_PROGRESS"):
                position = payload.get("queue_position")
                suffix = f"（排队第 {position} 位）" if state == "IN_QUEUE" and position else ""
                if progress:
                    progress(("服务商排队中" if state == "IN_QUEUE" else "视频生成中") + suffix)
                print(f"视频任务状态：{state}{suffix}", file=sys.stderr, flush=True)
            else:
                print(f"视频任务状态：{state or '未知'}", file=sys.stderr, flush=True)
            time.sleep(POLL_INTERVAL)
        raise VideoGenerationError(f"等待视频任务完成超时（{POLL_TIMEOUT:g} 秒）。")

    def _fetch_result(
        self,
        session: Any,
        credential: Credential,
        api_model: str,
        request_id: str,
        stored_response_url: str | None = None,
    ) -> str:
        try:
            response = queue_get(session,
                stored_response_url or response_url(credential, api_model, request_id),
                headers=auth_headers(credential),
                timeout=120,
            )
        except Exception as exc:
            raise VideoGenerationError(f"获取视频任务结果失败：{exc}") from exc
        # An HTTP failure describes this GET, not the paid generation task.
        # Keep it resumable; only an explicit terminal task status is final.
        payload = response_json(response, "获取视频任务结果")
        if str(payload.get("status", "")).lower() in FAILURE_STATES:
            detail = nested(payload, ("error",), ("message",), ("detail",))
            raise TaskFailed(f"视频任务失败：{detail or payload['status']}")
        video_url = self.result_url(payload)
        if not video_url:
            raise VideoGenerationError("任务已完成，但结果中没有媒体 URL。")
        return video_url
