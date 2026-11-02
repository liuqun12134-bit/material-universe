from __future__ import annotations

from .fal_flashvsr import FalFlashVsrAdapter
from .fal_flux_schnell import FalFluxSchnellAdapter
from .fal_generic import FalGenericAdapter
from .fal_sync3 import FalSync3Adapter
from .fal_wan3 import FalWan3Adapter, FalWan3T2VAdapter
from .dashscope_wan3 import DashScopeWan3Adapter
from .kaiyuncode import KaiyunWan3Adapter, KaiyunOmniAdapter, KaiyunSeedanceAdapter, KaiyunImageAdapter, KaiyunOmniEditAdapter


ADAPTERS = {
    "kaiyuncode_wan3": KaiyunWan3Adapter,
    "kaiyuncode_seedance": KaiyunSeedanceAdapter,
    "kaiyuncode_omni": KaiyunOmniAdapter,
    "kaiyuncode_image": KaiyunImageAdapter,
    "kaiyuncode_omni_edit": KaiyunOmniEditAdapter,
    "dashscope_wan3": DashScopeWan3Adapter,
    "fal_wan3": FalWan3Adapter,
    "fal_wan3_t2v": FalWan3T2VAdapter,
    "fal_sync3": FalSync3Adapter,
    "fal_flashvsr": FalFlashVsrAdapter,
    "fal_flux_schnell": FalFluxSchnellAdapter,
    "fal_generic": FalGenericAdapter,
}


def create_adapter(name: str):
    try:
        return ADAPTERS[name]()
    except KeyError as exc:
        raise ValueError(f"未实现模型适配器：{name}") from exc
