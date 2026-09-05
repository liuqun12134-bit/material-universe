"""One local record per output, for resuming a paid task without resubmission."""
from __future__ import annotations

import hashlib
import json
import os
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path

from .core import GenerationRequest, ModelSpec, Reference, VideoGenerationError


class TaskFailed(VideoGenerationError):
    """The provider explicitly declared the task terminally failed."""


class TaskRecord:
    def __init__(self, path: Path):
        self.path = path.expanduser().resolve()
        self.data: dict = {}

    @staticmethod
    def path_for(output: Path) -> Path:
        return Path(str(output) + ".video-task.json")

    @contextmanager
    def lock(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with Path(str(self.path) + ".lock").open("a+b") as handle:
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise VideoGenerationError(f"该任务正在另一个调用中执行：{self.path}") from None
            try:
                yield
            finally:
                handle.seek(0)
                if os.name == "nt":
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def start(self, request: GenerationRequest, api_base: str):
        if self.path.exists():
            raise VideoGenerationError(f"已有任务记录，请使用 --resume 继续原任务：{self.path}")
        self.data = {
            "version": 1, "status": "submitting", "api_base": api_base.rstrip("/"),
            "model": asdict(request.model), "requested_model": request.requested_model,
            "output": str(request.output), "reference_count": len(request.references),
            "duration": request.duration, "aspect_ratio": request.aspect_ratio,
            "resolution": request.resolution,
        }
        self.update()

    def read(self):
        try:
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
            if self.data.get("version") != 1 or not isinstance(self.data.get("model"), dict):
                raise ValueError("invalid record")
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            raise VideoGenerationError(f"无法读取任务记录：{self.path}（{exc}）") from None
        return self

    def update(self, **values):
        self.data.update(values)
        temporary = Path(str(self.path) + ".tmp")
        try:
            temporary.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(self.path)
        except OSError as exc:
            raise VideoGenerationError(
                f"任务记录保存失败；请保留任务 ID {self.data.get('task_id', '尚未取得')}，"
                f"不要重新提交：{self.path}（{exc}）"
            ) from None

    def request(self):
        model = ModelSpec(**self.data["model"])
        # Query/download no longer depend on the original media files being present.
        references = [Reference("image", "", None) for _ in range(self.data.get("reference_count", 0))]
        return GenerationRequest("", self.data["requested_model"], model, references,
                                 Path(self.data["output"]), self.data.get("duration"),
                                 self.data.get("aspect_ratio"), self.data.get("resolution"), self)

    def finish(self, result: dict) -> dict:
        result = {**result, "task_state_file": str(self.path)}
        downloaded = bool(result.get("local_downloaded"))
        digest = hashlib.sha256(Path(result["output"]).read_bytes()).hexdigest() if downloaded else None
        self.update(status="completed" if downloaded else "generated", result=result, output_sha256=digest)
        return result

    def completed_result(self):
        output = Path(self.data["output"])
        if self.data.get("status") == "completed" and output.is_file():
            if hashlib.sha256(output.read_bytes()).hexdigest() != self.data.get("output_sha256"):
                raise VideoGenerationError("本地成片已被修改，已停止，避免覆盖。")
            return {**self.data["result"], "resumed_existing_task": True}
        return None
