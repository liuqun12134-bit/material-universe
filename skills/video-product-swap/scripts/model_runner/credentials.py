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


def load_env(path: Path) -> None:
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value


class CredentialManager:
    def __init__(self, skill_root: Path, providers: dict[str, Any]) -> None:
        self.skill_root = skill_root
        self.providers = providers
        load_env(Path.cwd() / ".env")
        load_env(skill_root / ".env")
        self._resolved: list[Credential] = []

    def resolve(self, provider: str, require_key: bool = True) -> Credential:
        try:
            spec = self.providers[provider]
        except KeyError as exc:
            raise VideoGenerationError(f"未登记服务商：{provider}") from exc
        base = (
            os.environ.get(spec["api_base_env"])
            or os.environ.get(spec.get("legacy_api_base_env", ""))
            or spec["default_api_base"]
        ).rstrip("/")
        key_source = None
        key = None
        for name in (spec["api_key_env"], spec.get("legacy_api_key_env")):
            if name and os.environ.get(name):
                key_source, key = name, os.environ[name]
                break
        if require_key and not key:
            raise VideoGenerationError(
                f"服务商 {provider} 未配置 {spec['api_key_env']}。"
            )
        credential = Credential(provider, base, key, key_source, spec["submit_path"])
        self._resolved.append(credential)
        return credential

    def redact(self, text: str) -> str:
        for credential in self._resolved:
            if credential.api_key:
                text = text.replace(credential.api_key, "***")
        return text
