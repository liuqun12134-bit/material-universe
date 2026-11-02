"""GET-only recovery for retired providers and models; no generation entry point."""
from __future__ import annotations

import time
from urllib.parse import quote, urlparse

from ..core import VideoGenerationError, nested, result_url
from ..task_state import TaskFailed
from .base import ModelAdapter, FAILURE_STATES, response_json, same_origin


class RecoveryAdapter(ModelAdapter):
    name = "recovery_only"

    def resume(self, request, credential):
        import requests

        state = request.task_record.data
        session = requests.Session()
        scheme = "Key" if credential.provider == "fal" else "Bearer"
        headers = {"Authorization": f"{scheme} {credential.api_key}"} if credential.api_key else {}
        video_url = state.get("video_url")
        if not video_url:
            task_id = quote(str(state.get("task_id") or ""), safe="")
            if request.model.provider == "kaiyuncode":
                poll_url = state.get("poll_url") or (
                    f"{credential.api_base}/v1/videos/{task_id}" if task_id else None
                )
            elif request.model.provider == "dashscope":
                poll_url = f"{credential.api_base}/tasks/{task_id}" if task_id else None
            else:
                raise VideoGenerationError("该服务商仅支持使用已保存的成片链接下载。")
            if not poll_url or urlparse(poll_url).scheme != "https" or not same_origin(poll_url, credential.api_base):
                raise VideoGenerationError("旧任务查询地址必须与原服务地址同源且使用 HTTPS。")
            deadline = time.monotonic() + 1800
            while time.monotonic() < deadline:
                response = session.get(poll_url, headers=headers, timeout=60, allow_redirects=False)
                if 300 <= response.status_code < 400:
                    raise VideoGenerationError("旧任务查询地址发生跳转，请核对原服务地址。")
                payload = response_json(response, "查询旧任务")
                status = str(nested(payload, ("output", "task_status"), ("status",),
                                    ("data", "status"), ("result", "status")) or "").lower()
                if status in FAILURE_STATES:
                    detail = nested(payload, ("output", "message"), ("error",), ("message",), ("data", "error"))
                    raise TaskFailed(f"旧任务已失败：{detail or status}")
                video_url = nested(payload, ("output", "video_url")) or result_url(payload)
                if video_url:
                    request.task_record.update(video_url=video_url, status="generated")
                    break
                time.sleep(5)
            else:
                raise VideoGenerationError("旧任务查询超时，可稍后继续原任务。")
        if urlparse(video_url).scheme != "https":
            raise VideoGenerationError("成片下载地址必须使用 HTTPS。")
        download_headers = headers if same_origin(video_url, credential.api_base) else {}
        return self.deliver(request, session, video_url, state.get("task_id"), download_headers)


def recovery_adapter(model):
    """Select by the original record, never translate an old task to a new model."""
    if model.provider == "kaiyuncode" and model.adapter in {
        "kaiyuncode_wan3", "kaiyuncode_seedance", "kaiyuncode_omni", "kaiyuncode_image", "kaiyuncode_omni_edit"
    }:
        from . import create_adapter
        return create_adapter(model.adapter)
    if model.provider == "dashscope" and model.adapter == "dashscope_wan3":
        from .dashscope_wan3 import DashScopeWan3Adapter
        return DashScopeWan3Adapter()
    if model.provider in {"kaiyuncode", "dashscope"}:
        return RecoveryAdapter()
    if model.provider == "fal" and model.adapter == "fal_omniflash":
        from .fal_base import FalQueueAdapter
        return FalQueueAdapter()
    from . import create_adapter
    return create_adapter(model.adapter)
