from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .core import ImagePreprocessorError, ModelSpec


def _load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ImagePreprocessorError(f"缺少注册表：{path}") from exc
    except json.JSONDecodeError as exc:
        raise ImagePreprocessorError(f"注册表 JSON 无效：{path}") from exc
    if not isinstance(payload, dict):
        raise ImagePreprocessorError(f"注册表格式错误：{path}")
    return payload


class ModelRegistry:
    def __init__(self, references_dir: Path) -> None:
        model_data = _load_json(references_dir / "models.json")
        provider_data = _load_json(references_dir / "providers.json")
        self.defaults = model_data.get("defaults", {})
        self.raw_models = model_data.get("models", [])
        self.providers = provider_data.get("providers", {})
        if not isinstance(self.raw_models, list) or not isinstance(self.providers, dict):
            raise ImagePreprocessorError("模型或 Provider 注册表格式错误。")
        self._lookup: dict[str, dict[str, Any]] = {}
        for item in self.raw_models:
            if not isinstance(item, dict) or "id" not in item:
                raise ImagePreprocessorError("模型注册项缺少 id。")
            for name in [item["id"], *item.get("aliases", [])]:
                key = str(name).strip().lower()
                if not key or key in self._lookup:
                    raise ImagePreprocessorError(f"模型名称或别名无效或重复：{name}")
                self._lookup[key] = item

    def default(self, capability: str) -> str:
        value = str(self.defaults.get(capability, "")).strip()
        if not value:
            raise ImagePreprocessorError(f"没有配置默认 {capability} 模型。")
        return value

    def resolve(self, requested: str, capability: str) -> ModelSpec:
        item = self._lookup.get(requested.strip().lower())
        if item is None:
            raise ImagePreprocessorError(f"未注册模型：{requested}")
        actual_capability = str(item.get("capability", ""))
        if actual_capability != capability:
            raise ImagePreprocessorError(
                f"模型 {requested} 的能力是 {actual_capability}，不能用于 {capability}。"
            )
        provider = str(item.get("provider", ""))
        if provider not in self.providers:
            raise ImagePreprocessorError(f"模型 {requested} 使用了未登记 Provider：{provider}")
        return ModelSpec(
            id=str(item["id"]),
            api_model=str(item.get("api_model", item["id"])),
            aliases=tuple(str(value) for value in item.get("aliases", [])),
            capability=actual_capability,
            provider=provider,
            adapter=str(item.get("adapter", "")),
            input_summary=str(item.get("input_summary", "")),
            options=dict(item.get("options", {})),
        )

    def list_models(self) -> dict[str, Any]:
        return {
            "defaults": self.defaults,
            "models": [
                {
                    "model": item["id"],
                    "aliases": item.get("aliases", []),
                    "capability": item.get("capability"),
                    "provider": item.get("provider"),
                    "adapter": item.get("adapter"),
                    "inputs": item.get("input_summary", ""),
                }
                for item in self.raw_models
            ],
        }
