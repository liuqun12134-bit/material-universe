from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .core import VideoGenerationError


@dataclass(frozen=True)
class Credential:
    provider: str
    api_base: str
    api_key: str | None
    api_key_source: str | None
    submit_path: str


def load_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if key:
            values[key] = value
    return values


class CredentialManager:
    def __init__(self, skill_root: Path, providers: dict[str, Any], *, load_env_files: bool = True,
                 values: dict[str, str] | None = None) -> None:
        self.skill_root = skill_root
        self.providers = providers
        # Each invocation owns a snapshot. Never read the caller's working directory
        # or write credentials into process-global state.
        self.values = (dict(values) if values is not None else
                       load_env(skill_root / ".env") if load_env_files else dict(os.environ))
        self._resolved: list[Credential] = []

    def resolve(self, provider: str, require_key: bool = True) -> Credential:
        try:
            spec = self.providers[provider]
        except KeyError as exc:
            raise VideoGenerationError(f"未登记服务商：{provider}") from exc
        base = (
            self.values.get(spec["api_base_env"])
            or self.values.get(spec.get("legacy_api_base_env", ""))
            or spec["default_api_base"]
        ).rstrip("/")
        key_source = None
        key = None
        for name in (spec["api_key_env"], spec.get("legacy_api_key_env")):
            if name and self.values.get(name):
                key_source, key = name, self.values[name]
                break
        if require_key and not key:
            raise VideoGenerationError(
                f"服务商 {provider} 未配置 {spec['api_key_env']}。"
            )
        credential = Credential(provider, base, key, key_source, spec["submit_path"])
        self._resolved.append(credential)
        return credential

    def redact(self, text: str) -> str:
        for name, value in self.values.items():
            if value and any(part in name.upper() for part in ("KEY", "TOKEN", "SECRET")):
                text = text.replace(value, "***")
        return text

    def vision(self) -> tuple[str, str, str]:
        from prompt_engine import DEFAULT_MODEL, DEFAULT_API_BASE
        key = self.values.get("DEEPSEEK_API_KEY", "").strip()
        if not key:
            raise VideoGenerationError("当前配置缺少 DEEPSEEK_API_KEY，请在本 Skill .env 或宿主设置中填写。")
        return (key, self.values.get("DEEPSEEK_API_BASE", "").strip() or DEFAULT_API_BASE,
                self.values.get("DEEPSEEK_PROMPT_MODEL", "").strip() or DEFAULT_MODEL)
