"""One-shot status refresh. Never submits, uploads, downloads or retries."""
import time
from pathlib import Path
from urllib.parse import quote

import requests

from fish_mcp import FishClient
from image_audio_video import _terminal_media
from model_runner.runner import ModelRunner
from model_runner.task_state import TaskRecord
from model_runner.adapters.base import same_origin, extract_video_url


def fetch_status(state, credentials):
    provider = state["model"]["provider"]
    if provider == "fish_audio":
        with FishClient(credentials.get("FISH_AUTH_PATH")) as client:
            payload = client.call("get_generation_status", {"generation_id": state["task_id"]})
        media = _terminal_media(payload)
        result = (str(payload.get("status", "")).lower(), media.get("url") if media else None,
                  payload.get("view_url"), max(10, float(payload.get("poll_after_seconds") or 60)))
        if state["model"].get("adapter") == "fish_image":
            return (*result, {"media_object_key": media.get("object_key") if media else None})
        return result
    runner = ModelRunner(credential_source="host-app", values=credentials)
    credential = runner.credentials.resolve(provider)
    if credential.api_base.rstrip("/") != state["api_base"].rstrip("/"):
        raise ValueError("服务地址与原任务不同，请在设置中恢复原线路后刷新。")
    headers = {"Authorization": ("Key " if provider == "fal" else "Bearer ") + credential.api_key}
    def get(url):
        if not same_origin(url, credential.api_base) or not url.startswith("https://"):
            raise ValueError("原任务查询地址与服务不一致，已停止。")
        response = requests.get(url, headers=headers, timeout=(15, 45), allow_redirects=False)
        if response.status_code != 200:
            raise ValueError(f"任务同步失败（HTTP {response.status_code}），未重试。")
        return response.json()
    task_id = quote(str(state["task_id"]), safe="")
    if provider == "fal":
        from model_runner.adapters.fal_base import status_url, response_url
        payload = get(state.get("status_url") or status_url(credential, state["model"]["api_model"], task_id))
        status = str(payload.get("status", "")).lower()
        if status == "completed":
            result = get(state.get("response_url") or response_url(credential, state["model"]["api_model"], task_id))
            return status, extract_video_url(result), None, 5
        return status, None, None, 5
    if provider == "dashscope":
        output = get(credential.api_base.rstrip("/") + "/tasks/" + task_id).get("output", {})
        return str(output.get("task_status", "")).lower(), output.get("video_url"), None, 10
    if provider == "kaiyuncode":
        from model_runner.adapters.kaiyuncode import api_root
        payload = get(state.get("poll_url") or api_root(credential) + "/videos/" + task_id)
        data = payload.get("data", payload)
        return str(data.get("status", "")).lower(), extract_video_url(payload) or data.get("url"), None, 10
    raise ValueError("此历史服务暂不支持即时刷新，请使用继续原任务。")


def refresh(manager, credentials):
    counts = {"synced": 0, "active": 0, "cooldown": 0, "errors": []}
    for job in manager.store.jobs():
        identity = job["id"]
        if job["status"] in {"success", "failed", "cancelled", "paused"}:
            continue
        with manager.guard:
            if identity in manager.active or job["status"] == "queued":
                counts["active"] += 1
                continue  # The existing worker owns polling and the provider interval.
            if identity in manager.refreshing:
                continue
            if job["kind"] == "speech":
                continue  # Speech is synchronous; recovery only downloads its saved audio URL.
            manager.refreshing.add(identity)
        try:
            path = Path(job.get("task_state_file") or job["config"]["output"] + ".video-task.json")
            if not path.is_file():
                continue
            record = TaskRecord(path)
            with record.lock():
                record.read()
                state = record.data
                if state.get("video_url") or state.get("audio_url") or state.get("model", {}).get("adapter") == "fish_speech" or not state.get("task_id") or state.get("status") in {"failed", "completed"}:
                    continue
                if time.time() < state.get("next_manual_check", 0):
                    counts["cooldown"] += 1
                    continue
                record.update(next_manual_check=time.time() + 60)
                fetched = fetch_status(state, credentials)
                status, url, view_url, interval = fetched[:4]
                fields = {"last_status_check": time.time(), "next_manual_check": time.time() + interval}
                if len(fetched) > 4 and fetched[4].get("media_object_key"):
                    fields["media_object_key"] = fetched[4]["media_object_key"]
                if view_url:
                    fields["view_url"] = view_url
                if status in {"failed", "failure", "error", "cancelled", "canceled", "rejected"}:
                    fields.update(status="failed", error="服务商已报告任务失败。", failure_kind="terminal_task")
                    local = {"status": "failed", "stage": "服务商已报告失败", "error": "请查看服务商任务详情。"}
                elif status in {"completed", "succeeded", "success", "finished"} and url:
                    fields.update(status="generated", video_url=url)
                    local = {"status": "attention", "stage": "生成完成，待下载", "error": ""}
                elif status in {"pending", "queued", "running", "processing", "submitted", "in_progress", "in_queue", "created"}:
                    fields.update(status="submitted")
                    local = {"status": "attention", "stage": "服务商处理中 · 可继续跟踪", "error": ""}
                else:
                    raise ValueError("未识别到可靠任务状态，请稍后刷新或查看服务商任务。")
                record.update(**fields)
                manager.store.update(identity, **local, remote_status=fields["status"], last_synced=time.time())
                counts["synced"] += 1
        except Exception as exc:
            message = str(exc) if isinstance(exc, ValueError) else "网络或任务记录暂不可用，请稍后刷新。"
            for key, value in credentials.items():
                if value and any(word in key.upper() for word in ("KEY", "TOKEN", "SECRET")):
                    message = message.replace(str(value), "***")
            counts["errors"].append(job["name"] + "：" + message)
        finally:
            with manager.guard:
                manager.refreshing.discard(identity)
    return counts
