from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path
from typing import Any

from .adapters import create_image_adapter, create_vision_adapter
from .core import ImagePreprocessorError, validate_input_image
from .credentials import CredentialManager
from .registry import ModelRegistry


SKILL_ROOT = Path(__file__).resolve().parents[2]


def default_output() -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return (Path.cwd() / "output" / f"product_reference_{stamp}.png").resolve()


class ImagePreprocessor:
    def __init__(self, skill_root: Path = SKILL_ROOT, credential_source: str = "skill-env") -> None:
        self.skill_root = skill_root
        self.registry = ModelRegistry(skill_root / "references")
        self.credentials = CredentialManager(skill_root, self.registry.providers, credential_source)

    def list_models(self) -> dict[str, Any]:
        return self.registry.list_models()

    def _models(self, vision_model: str | None, image_model: str | None):
        vision_name = vision_model or self.registry.default("vision_analysis")
        image_name = image_model or self.registry.default("image_generation")
        return (
            self.registry.resolve(vision_name, "vision_analysis"),
            self.registry.resolve(image_name, "image_generation"),
        )

    def dry_run(
        self,
        input_path: Path,
        output: Path | None,
        vision_model: str | None,
        image_model: str | None,
        size: str | None,
    ) -> dict[str, Any]:
        source = validate_input_image(input_path)
        target = (output or default_output()).expanduser().resolve()
        vision, image = self._models(vision_model, image_model)
        vision_credential = self.credentials.resolve(vision.provider, require_key=False)
        image_credential = self.credentials.resolve(image.provider, require_key=False)
        vision_adapter = create_vision_adapter(vision.adapter)
        image_adapter = create_image_adapter(image.adapter)
        chosen_size = size or str(image.options.get("default_size", "2048x2048"))
        return {
            "success": True,
            "dry_run": True,
            "input": str(source),
            "workflow": "analyze -> pass-through OR generate once -> re-analyze",
            "vision": vision_adapter.plan(source, vision, vision_credential),
            "image_generation": image_adapter.plan(source, image, image_credential, target, chosen_size),
            "automatic_retry": False,
        }

    def run(
        self,
        input_path: Path,
        output: Path | None,
        vision_model: str | None,
        image_model: str | None,
        size: str | None,
        timeout: int,
        analyze_only: bool = False,
    ) -> dict[str, Any]:
        if timeout <= 0:
            raise ImagePreprocessorError("timeout 必须大于 0。")
        started = time.monotonic()
        source = validate_input_image(input_path)
        target = (output or default_output()).expanduser().resolve()
        vision, image = self._models(vision_model, image_model)
        vision_credential = self.credentials.resolve(vision.provider, require_key=True)
        vision_adapter = create_vision_adapter(vision.adapter)

        initial = vision_adapter.analyze(source, vision, vision_credential, timeout)
        if initial["passed"]:
            return {
                "success": True,
                "action": "passed_through",
                "technical_completed": True,
                "quality_passed": True,
                "input": str(source),
                "final_image": str(source),
                "candidate_image": None,
                "initial_analysis": initial,
                "final_analysis": initial,
                "vision_model": vision.api_model,
                "image_model": None,
                "generation_calls": 0,
                "error": None,
                "elapsed_seconds": round(time.monotonic() - started, 3),
            }

        if analyze_only:
            return {
                "success": True,
                "action": "analysis_only",
                "technical_completed": True,
                "quality_passed": False,
                "input": str(source),
                "final_image": None,
                "candidate_image": None,
                "initial_analysis": initial,
                "final_analysis": None,
                "vision_model": vision.api_model,
                "image_model": None,
                "generation_calls": 0,
                "error": None,
                "elapsed_seconds": round(time.monotonic() - started, 3),
            }

        image_credential = self.credentials.resolve(image.provider, require_key=True)
        image_adapter = create_image_adapter(image.adapter)
        chosen_size = size or str(image.options.get("default_size", "2048x2048"))
        generated = image_adapter.generate(source, image, image_credential, target, chosen_size, timeout)
        candidate = Path(str(generated["output"])).resolve()
        final = vision_adapter.analyze(candidate, vision, vision_credential, timeout)
        passed = bool(final["passed"])
        return {
            "success": passed,
            "action": "generated" if passed else "generated_but_rejected",
            "technical_completed": True,
            "quality_passed": passed,
            "input": str(source),
            "final_image": str(candidate) if passed else None,
            "candidate_image": str(candidate),
            "initial_analysis": initial,
            "final_analysis": final,
            "generation": generated,
            "vision_model": vision.api_model,
            "image_model": image.api_model,
            "generation_calls": 1,
            "error": None if passed else "生成图未通过视觉复检，已停止后续流程。",
            "elapsed_seconds": round(time.monotonic() - started, 3),
        }
