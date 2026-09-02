from __future__ import annotations

from .generic import GenericRelayAdapter
from .omniflash import OmniFlashAdapter
from .dashscope_videoedit import DashScopeVideoEditAdapter
from .wan3 import Wan3Adapter


ADAPTERS = {
    "generic": GenericRelayAdapter,
    "omniflash": OmniFlashAdapter,
    "dashscope_videoedit": DashScopeVideoEditAdapter,
    "wan3": Wan3Adapter,
}


def create_adapter(name: str):
    try:
        return ADAPTERS[name]()
    except KeyError as exc:
        raise ValueError(f"未实现模型适配器：{name}") from exc
