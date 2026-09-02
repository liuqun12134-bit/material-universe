from __future__ import annotations

from ..core import ImagePreprocessorError
from .deepseek import DeepSeekVisionAdapter
from .seedream import SeedreamAsyncAdapter


VISION_ADAPTERS = {"deepseek-vision": DeepSeekVisionAdapter}
IMAGE_ADAPTERS = {"seedream-async": SeedreamAsyncAdapter}


def create_vision_adapter(name: str):
    try:
        return VISION_ADAPTERS[name]()
    except KeyError as exc:
        raise ImagePreprocessorError(f"未实现视觉模型适配器：{name}") from exc


def create_image_adapter(name: str):
    try:
        return IMAGE_ADAPTERS[name]()
    except KeyError as exc:
        raise ImagePreprocessorError(f"未实现生图模型适配器：{name}") from exc
