from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .core import ModelSpec, VideoGenerationError


def load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise VideoGenerationError(f"缺少注册表：{path}") from exc
    except json.JSONDecodeError as exc:
        raise VideoGenerationError(f"注册表 JSON 无效：{path}") from exc
    if not isinstance(payload, dict):
        raise VideoGenerationError(f"注册表格式错误：{path}")
    return payload


class ModelRegistry:
    def __init__(self, references_dir: Path) -> None:
        data = load_json(references_dir / "models.json")
        provider_data = load_json(references_dir / "providers.json")
        self.raw_models = data.get("models", [])
        self.fallback = data.get("fallback", {})
        self.providers = provider_data.get("providers", {})
        self._lookup: dict[str, dict[str, Any]] = {}
        for item in self.raw_models:
            for name in [item["id"], *item.get("aliases", [])]:
                lowered = name.lower()
                if lowered in self._lookup:
                    raise VideoGenerationError(f"模型名称或别名重复：{name}")
                self._lookup[lowered] = item

    def resolve(self, requested: str) -> ModelSpec:
        key = requested.strip().lower()
        if not key:
            raise VideoGenerationError("模型名称不能为空。")
        item = self._lookup.get(key)
        if item is None:
            return ModelSpec(
                id=requested.strip(),
                api_model=requested.strip(),
                aliases=(),
                provider=self.fallback["provider"],
                adapter=self.fallback["adapter"],
                input_summary=self.fallback["input_summary"],
                registered=False,
            )
        return ModelSpec(
            id=item["id"],
            api_model=item.get("api_model", item["id"]),
            aliases=tuple(item.get("aliases", [])),
            provider=item["provider"],
            adapter=item["adapter"],
            input_summary=item["input_summary"],
        )

    def list_models(self) -> list[dict[str, Any]]:
        return [
            {
                "model": item["id"],
                "aliases": item.get("aliases", []),
                "provider": item["provider"],
                "adapter": item["adapter"],
                "inputs": item["input_summary"],
            }
            for item in self.raw_models
        ]
