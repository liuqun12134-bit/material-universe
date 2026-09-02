from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from ..core import Credential, ModelSpec


class VisionAdapter(ABC):
    @abstractmethod
    def plan(self, image_path: Path, model: ModelSpec, credential: Credential) -> dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    def analyze(
        self, image_path: Path, model: ModelSpec, credential: Credential, timeout: int
    ) -> dict[str, Any]:
        raise NotImplementedError


class ImageGenerationAdapter(ABC):
    @abstractmethod
    def plan(
        self,
        image_path: Path,
        model: ModelSpec,
        credential: Credential,
        output: Path,
        size: str,
    ) -> dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    def generate(
        self,
        image_path: Path,
        model: ModelSpec,
        credential: Credential,
        output: Path,
        size: str,
        timeout: int,
    ) -> dict[str, Any]:
        raise NotImplementedError
