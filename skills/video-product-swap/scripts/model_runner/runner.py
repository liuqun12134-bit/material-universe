from __future__ import annotations

import os
import json
from pathlib import Path
from typing import Any

from .adapters import create_adapter
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
        self.registry = ModelRegistry(skill_root / "references")
        self.credentials = CredentialManager(
            skill_root, self.registry.providers, load_env_files=credential_source != "host-app", values=values
        )

    def list_models(self) -> list[dict[str, Any]]:
        return self.registry.list_models()

    def redact(self, text: str) -> str:
        return self.credentials.redact(text)

    def run(self, args: Any) -> dict[str, Any]:
        prompt = args.prompt.strip()
        requested_model = args.model.strip()
        model = self.registry.resolve(requested_model)
        references = [parse_reference(value) for value in args.reference]
        output = (
            Path(args.output).expanduser().resolve() if args.output else default_output()
        )
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
        )
        adapter = create_adapter(model.adapter)
        adapter.validate(request)
        credential = self.credentials.resolve(model.provider, require_key=not args.dry_run)
        if args.dry_run:
            return adapter.plan(request, credential)
        record = TaskRecord(TaskRecord.path_for(output))
        with record.lock():
            record.start(request, credential.api_base)
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
            status = "failed" if isinstance(exc, TaskFailed) else (
                "interrupted" if record.data.get("task_id") or record.data.get("video_url") or record.data.get("poll_url")
                else "submission_unknown")
            record.update(status=status, error=message)
            raise VideoGenerationError(f"{message}\n任务记录：{record.path}\n使用 --resume 继续查询或下载，不会重新提交。") from None

    def resume(self, path: Path) -> dict[str, Any]:
        record = TaskRecord(path)
        with record.lock():
            record.read()
            completed = record.completed_result()
            if completed is not None:
                return completed
            if record.data.get("status") == "failed":
                raise VideoGenerationError(f"该任务已明确失败：{record.data.get('error', '')}；不会重新提交。")
            if not any(record.data.get(key) for key in ("task_id", "video_url", "poll_url")):
                raise VideoGenerationError("提交结果不明确且没有任务 ID，请先到服务商核实是否受理；不会重新提交。")
            request = record.request()
            if request.output.exists():
                raise VideoGenerationError(f"输出文件已存在且未确认属于该任务，已停止：{request.output}")
            credential = self.credentials.resolve(request.model.provider)
            if credential.api_base.rstrip("/") != record.data["api_base"]:
                raise VideoGenerationError("当前服务地址与原任务不一致，请恢复原线路配置。")
            result = self._execute(create_adapter(request.model.adapter), request, credential, resume=True)
            return {**result, "resumed_existing_task": True}
