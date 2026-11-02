from __future__ import annotations

import os
import json
import re
from pathlib import Path
from typing import Any

from .adapters import create_adapter
from .adapters.recovery import recovery_adapter, RecoveryAdapter
from .adapters.base import same_origin
from .core import GenerationRequest, VideoGenerationError, default_output, parse_reference
from .credentials import CredentialManager
from .registry import ModelRegistry
from .task_state import TaskRecord, TaskFailed


SKILL_ROOT = Path(
    os.environ.get("MATERIAL_UNIVERSE_VIDEO_SKILL_ROOT", "")
    or Path(__file__).resolve().parents[2]
).expanduser().resolve()


class ModelRunner:
    def __init__(self, skill_root: Path = SKILL_ROOT, *, credential_source: str = "skill-env",
                 values: dict[str, str] | None = None) -> None:
        self.skill_root = skill_root
        self.progress = None
        self.task_update = None
        self.record_metadata = None
        self.registry = ModelRegistry(skill_root / "references")
        self.credentials = CredentialManager(
            skill_root, self.registry.providers, load_env_files=credential_source != "host-app", values=values
        )

    def list_models(self) -> list[dict[str, Any]]:
        return self.registry.list_models()

    def redact(self, text: str) -> str:
        return self.credentials.redact(text)

    def run(self, args: Any) -> dict[str, Any]:
        prompt = args.prompt or ""
        provider = str(getattr(args, "provider", None) or self.registry.default_provider)
        requested_model = (args.model or "").strip() or self.registry.default_model(provider)
        model = self.registry.resolve_for_provider(provider, requested_model)
        references = [parse_reference(value) for value in args.reference]
        output = (
            Path(args.output).expanduser().resolve() if args.output else default_output()
        )
        if not args.output and model.adapter == "kaiyuncode_image":
            output = output.with_name(output.stem.replace("video_", "image_", 1) + ".png")
        if output.exists():
            raise VideoGenerationError(f"输出文件已存在：{output}")
        request = GenerationRequest(
            prompt=prompt,
            requested_model=requested_model,
            model=model,
            references=references,
            output=output,
            duration=args.duration,
            aspect_ratio=getattr(args, "aspect_ratio", None),
            resolution=getattr(args, "resolution", None),
            options=getattr(args, "options", None),
        )
        adapter = create_adapter(model.adapter)
        request.progress = self.progress
        adapter.validate(request)
        credential = self.credentials.resolve(model.provider, require_key=not args.dry_run)
        if hasattr(adapter, "validate_credential"):
            adapter.validate_credential(credential)
        if args.dry_run:
            return adapter.plan(request, credential)
        record = TaskRecord(TaskRecord.path_for(output))
        record.on_update = self.task_update
        with record.lock():
            record.start(request, credential.api_base)
            if self.record_metadata:
                record.update(**self.record_metadata)
            request.task_record = record
            return self._execute(adapter, request, credential)

    def _execute(self, adapter, request, credential, *, resume=False):
        record = request.task_record
        try:
            result = adapter.resume(request, credential) if resume else adapter.execute(request, credential)
            result = json.loads(self.redact(json.dumps(result, ensure_ascii=False)))
            return record.finish(result)
        except Exception as exc:
            message = self.redact(str(exc))
            status = "not_submitted" if record.data.get("status") == "preparing" else "failed" if isinstance(exc, TaskFailed) else (
                "interrupted" if record.data.get("task_id") or record.data.get("video_url") or record.data.get("poll_url") or record.data.get("image_base64")
                else "submission_unknown")
            record.update(status=status, error=message,
                          failure_kind="terminal_task" if isinstance(exc, TaskFailed) else None)
            if status == "not_submitted":
                hint = "尚未提交视频生成，可修复网络后点击“一键开始换品”生成新任务。"
            elif status == "submission_unknown":
                hint = "没有任务编号，无法使用“继续已有任务”。请先在服务商控制台核实是否已受理，确认没有任务后再重新开始；程序不会自动重复提交。"
            elif status == "failed":
                hint = "服务商已明确判定任务失败，不会重新提交。"
            else:
                hint = "可点击“继续已有任务”选择此记录（命令行使用 --resume），只查询或下载，不会重新提交。"
            raise VideoGenerationError(f"{message}\n任务记录：{record.path}\n{hint}") from None

    def resume(self, path: Path) -> dict[str, Any]:
        record = TaskRecord(path)
        record.on_update = self.task_update
        with record.lock():
            record.read()
            completed = record.completed_result()
            if completed is not None:
                return completed
            if record.data.get("status") == "not_submitted":
                raise VideoGenerationError("此记录尚未提交视频生成，不能继续查询；请修复网络后重新开始。")
            if record.data.get("status") == "failed":
                # Versions before this fix marked transient result GET failures as final.
                legacy_http_error = (
                    not record.data.get("failure_kind")
                    and record.data["model"].get("provider") == "fal"
                    and record.data.get("task_id")
                    and re.match(r"^视频任务失败（HTTP (?:408|429|5\d\d)）：", record.data.get("error", ""))
                )
                if not legacy_http_error:
                    raise VideoGenerationError(f"该任务已明确失败：{record.data.get('error', '')}；不会重新提交。")
                record.update(status="interrupted", recovery_note="旧版将临时 HTTP 错误误判为任务失败，继续查询原任务。")
            if not any(record.data.get(key) for key in ("task_id", "video_url", "poll_url", "image_base64")):
                raise VideoGenerationError("提交结果不明确且没有任务 ID，请先到服务商核实是否受理；不会重新提交。")
            request = record.request()
            request.progress = self.progress
            if request.output.exists():
                raise VideoGenerationError(f"输出文件已存在且未确认属于该任务，已停止：{request.output}")
            download_only = bool(record.data.get("video_url") or record.data.get("image_base64"))
            credential = self.credentials.resolve(request.model.provider, require_key=not download_only)
            if record.data.get("video_url") and same_origin(str(record.data["video_url"]), credential.api_base):
                credential = self.credentials.resolve(request.model.provider)
            if credential.api_base.rstrip("/") != record.data["api_base"]:
                raise VideoGenerationError("当前服务地址与原任务不一致，请恢复原线路配置。")
            current_kaiyun = request.model.provider == "kaiyuncode" and request.model.adapter.startswith("kaiyuncode_")
            adapter = recovery_adapter(request.model) if current_kaiyun else RecoveryAdapter() if download_only else recovery_adapter(request.model)
            result = self._execute(adapter, request, credential, resume=True)
            return {**result, "resumed_existing_task": True}
