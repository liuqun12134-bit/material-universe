from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from .adapters import create_adapter
from .core import GenerationRequest, VideoGenerationError, default_output, parse_reference
from .credentials import CredentialManager
from .registry import ModelRegistry


SKILL_ROOT = Path(
    os.environ.get("MATERIAL_UNIVERSE_VIDEO_SKILL_ROOT", "")
    or Path(__file__).resolve().parents[2]
).expanduser().resolve()


class ModelRunner:
    def __init__(self, skill_root: Path = SKILL_ROOT) -> None:
        self.skill_root = skill_root
        self.registry = ModelRegistry(skill_root / "references")
        self.credentials = CredentialManager(skill_root, self.registry.providers)

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
        return adapter.execute(request, credential)
