"""Canvas model families, routes and local cost descriptors (no provider calls)."""
from copy import deepcopy

ZERO_REFS = {kind: {"min": 0, "max": 0} for kind in ("image", "video", "audio")}
VIDEO_PARAMETERS = [
    {"key": "duration", "type": "integer", "label": "时长（秒）", "default": 5, "min": 2, "max": 30},
    {"key": "aspect_ratio", "type": "enum", "label": "比例", "default": "9:16", "enum": ["9:16", "16:9", "1:1", "4:3", "3:4"]},
    {"key": "resolution", "type": "enum", "label": "清晰度", "default": "480p", "enum": ["480p", "720p", "1080p"]},
    {"key": "enable_thinking", "type": "boolean", "label": "思考模式", "default": True, "placement": "direct", "runnerOption": "enable_thinking"},
    {"key": "enable_prompt_expansion", "type": "boolean", "label": "提示词扩写", "default": True, "placement": "direct", "runnerOption": "enable_prompt_expansion"},
]
WAN_REFS = {"image": {"min": 0, "max": 10}, "video": {"min": 0, "max": 5}, "audio": {"min": 0, "max": 5}}
SYNC_REFS = {"image": {"min": 0, "max": 0}, "video": {"min": 1, "max": 1}, "audio": {"min": 1, "max": 1}}
SEEDANCE_REFS = {"image": {"min": 0, "max": 30}, "video": {"min": 0, "max": 10}, "audio": {"min": 0, "max": 10}}


def video_price(path):
    return {"currency": "USD", "unit": "second", "durationBasis": "output", "rateByResolution": {"480p": 0.05, "720p": 0.10, "1080p": 0.20},
            "sourceUrl": f"https://fal.ai/models/alibaba/wan-3.0/{path}", "verifiedAt": "2026-10-02"}


def official_video_price(prime=False):
    return {"currency": "CNY", "unit": "second", "durationBasis": "output_plus_reference_video",
            "rateByResolution": {"480p": 0.45 if prime else 0.30, "720p": 0.90 if prime else 0.60, "1080p": 1.80 if prime else 1.20},
            "region": "cn-beijing", "note": "北京原价，活动优惠未计入；以实际计费为准。", "sourceUrl": "https://help.aliyun.com/zh/model-studio/model-pricing", "verifiedAt": "2026-10-05"}


MODEL_FAMILIES = [
    {"id": "wan3", "label": "Wan3", "provider": "fal", "providerLabel": "fal", "nodeType": "video", "promptRequired": True, "referenceLimits": WAN_REFS, "parameters": VIDEO_PARAMETERS,
     "routes": [
         {"modelId": "wan3.0-t2v", "when": "no_references", "label": "Wan3 文生视频", "adapter": "fal_wan3_t2v", "referenceLimits": ZERO_REFS, "price": video_price("text-to-video")},
         {"modelId": "wan3.0-video", "when": "has_references", "label": "Wan3 参考生视频", "adapter": "fal_wan3", "referenceLimits": WAN_REFS, "staticOptions": {"strict_media": True}, "mediaConstraints": {"videoTotalMaxSeconds": 15, "videoMinFps": 16, "audioTotalMaxSeconds": 15}, "price": {**video_price("reference-to-video"), "durationBasis": "output_plus_reference_video"}},
     ]},
    {"id": "wan3-dashscope", "label": "Wan3 官方", "provider": "dashscope", "providerLabel": "阿里云官方", "nodeType": "video", "promptRequired": True,
     "referenceLimits": WAN_REFS, "parameters": [item for item in VIDEO_PARAMETERS if item["key"] != "enable_thinking"],
     "routes": [
         {"modelId": "dashscope-wan3.0-video", "when": "always", "label": "Wan3 官方标准版", "adapter": "dashscope_wan3", "referenceLimits": WAN_REFS, "pricingUnverified": True, "price": official_video_price(), "mediaConstraints": {"videoTotalMaxSeconds": 15, "videoMinFps": 16, "audioTotalMaxSeconds": 15}},
         {"modelId": "dashscope-wan3.0-video-prime", "when": "always", "label": "Wan3 官方 Prime", "adapter": "dashscope_wan3", "referenceLimits": WAN_REFS, "pricingUnverified": True, "price": official_video_price(prime=True), "mediaConstraints": {"videoTotalMaxSeconds": 15, "videoMinFps": 16, "audioTotalMaxSeconds": 15}},
     ]},
    {"id": "sync3", "label": "Sync3", "provider": "fal", "providerLabel": "fal", "nodeType": "lipsync", "promptRequired": False, "referenceLimits": SYNC_REFS, "parameters": [],
     "routes": [{"modelId": "sync_lipsync", "when": "always", "label": "Sync3 对口型", "adapter": "fal_sync3", "referenceLimits": SYNC_REFS,
                 "price": {"currency": "USD", "unit": "minute", "rate": 8, "durationBasis": "shorter_media", "sourceUrl": "https://fal.ai/models/fal-ai/sync-lipsync/v3", "verifiedAt": "2026-10-02"}}]},
]
def enum_parameter(key, label, default, values):
    return {"key": key, "type": "enum", "label": label, "default": default, "enum": values}


MODEL_FAMILIES.extend([
    {"id": "wan3-kaiyuncode", "label": "Wan3", "provider": "kaiyuncode", "providerLabel": "KaiyunCode",
     "nodeType": "video", "promptRequired": True, "referenceLimits": WAN_REFS,
     "parameters": [item for item in VIDEO_PARAMETERS if item["key"] != "enable_thinking"],
     "routes": [
         {"modelId": "kaiyuncode-wan3.0-video", "when": "always", "label": "Wan3 标准版", "adapter": "kaiyuncode_wan3", "referenceLimits": WAN_REFS, "pricingUnverified": True},
         {"modelId": "kaiyuncode-wan3.0-video-prime", "when": "always", "label": "Wan3 Prime", "adapter": "kaiyuncode_wan3", "referenceLimits": WAN_REFS, "pricingUnverified": True},
     ]},
    {"id": "seedance-kaiyuncode", "label": "Seedance 2.5 TJ", "provider": "kaiyuncode", "providerLabel": "KaiyunCode",
     "nodeType": "video", "promptRequired": True, "referenceLimits": SEEDANCE_REFS,
     "parameters": [
         enum_parameter("duration", "时长（秒）", 4, [4, 5, 6, 8, 10, 12, 15, 20, 25, 30]),
         enum_parameter("aspect_ratio", "比例", "9:16", ["16:9", "9:16", "1:1"]),
         enum_parameter("resolution", "清晰度", "720p", ["720p"]),
     ],
     "routes": [{"modelId": "kaiyuncode-seedance-2.5-tj", "when": "always", "label": "Seedance 2.5 TJ", "adapter": "kaiyuncode_seedance", "referenceLimits": SEEDANCE_REFS, "pricingUnverified": True}]},
    {"id": "omni-kaiyuncode", "label": "Omni", "provider": "kaiyuncode", "providerLabel": "KaiyunCode",
     "nodeType": "video", "promptRequired": True, "referenceLimits": ZERO_REFS,
     "parameters": [
         enum_parameter("duration", "时长（秒）", 8, [4, 6, 8, 10]),
         enum_parameter("aspect_ratio", "比例", "9:16", ["16:9", "9:16"]),
     ],
     "routes": [{"modelId": "kaiyuncode-omni", "when": "always", "label": "Omni", "adapter": "kaiyuncode_omni", "referenceLimits": ZERO_REFS, "pricingUnverified": True}]},
    {"id": "nano-banana-kaiyuncode", "label": "Nano Banana 2.1", "provider": "kaiyuncode", "providerLabel": "KaiyunCode",
     "nodeType": "image", "promptRequired": True,
     "referenceLimits": {**ZERO_REFS, "image": {"min": 0, "max": 16}},
     "parameters": [
         {**enum_parameter("image_size", "尺寸", "1K", ["1K", "2K", "4K"]), "runnerOption": "image_size"},
         enum_parameter("aspect_ratio", "比例", "1:1", ["1:1", "16:9", "9:16", "3:2", "2:3", "4:3", "3:4", "4:5", "5:4", "21:9", "4:1", "1:4", "8:1", "1:8"]),
     ],
     "routes": [{"modelId": "kaiyuncode-gemini-nano-banana-2.1", "when": "always", "label": "Nano Banana 2.1", "adapter": "kaiyuncode_image",
                 "referenceLimits": {**ZERO_REFS, "image": {"min": 0, "max": 16}}, "pricingUnverified": True}]},
    {"id": "omni-edit-kaiyuncode", "label": "Omni 视频编辑", "provider": "kaiyuncode", "providerLabel": "KaiyunCode",
     "nodeType": "video", "promptRequired": True,
     "referenceLimits": {**ZERO_REFS, "video": {"min": 1, "max": 1}, "image": {"min": 0, "max": 5}},
     "parameters": [
         enum_parameter("aspect_ratio", "比例", "9:16", ["16:9", "9:16"]),
     ],
     "routes": [{"modelId": "kaiyuncode-omni_video_edit", "when": "always", "label": "Omni 视频编辑", "adapter": "kaiyuncode_omni_edit",
                 "referenceLimits": {**ZERO_REFS, "video": {"min": 1, "max": 1}, "image": {"min": 0, "max": 5}}, "pricingUnverified": True}]},
])

MODEL_FAMILIES = [family for family in MODEL_FAMILIES if family["id"] not in {"sync3", "omni-edit-kaiyuncode"}]

GENERAL_MODELS = [{"id": route["modelId"], "label": route["label"], "outputKind": "image" if family["nodeType"] == "image" else "video", "adapter": route["adapter"],
                   "provider": family["provider"], "providerLabel": family["providerLabel"], "pricingUnverified": route.get("pricingUnverified", False),
                   "promptRequired": family["promptRequired"], "referenceLimits": route["referenceLimits"], "parameters": family["parameters"],
                   "staticOptions": route.get("staticOptions", {}), "familyId": family["id"], "nodeType": family["nodeType"], "familyReferenceLimits": family["referenceLimits"], "routeWhen": route["when"]}
                  for family in MODEL_FAMILIES for route in family["routes"]]


def available_families(models):
    available = {item["id"] for item in models}
    result = []
    for family in MODEL_FAMILIES:
        item = deepcopy(family)
        item["routes"] = [route for route in item["routes"] if route["modelId"] in available]
        if item["routes"]:
            result.append(item)
    return result


def resolve_route(family_id, reference_ids, families=MODEL_FAMILIES, model_id=None):
    family = next((item for item in families if item["id"] == family_id), None)
    if family is None:
        raise ValueError("创作模型家族已不可用。")
    condition = "has_references" if reference_ids else "no_references"
    candidates = [item for item in family["routes"] if item["when"] in (condition, "always")]
    route = next((item for item in candidates if item["modelId"] == model_id), None) if model_id else None
    # Reference-driven fal routes keep their automatic text/reference selection.
    if route is None and model_id and all(item["when"] == "always" for item in family["routes"]):
        raise ValueError("所选型号不属于当前创作模型家族。")
    route = route or next(iter(candidates), None)
    if route is None:
        raise ValueError("当前素材组合没有可用生成路线。")
    return family, route


def runner_options(model_id, parameters):
    model = next((item for item in GENERAL_MODELS if item["id"] == model_id), None)
    if not model:
        return None
    if model["nodeType"] == "lipsync":
        return {"strict_media": True}
    # Missing flags on historical submissions retain their old false behavior.
    return {**model["staticOptions"], **{spec["runnerOption"]: parameters.get(spec["key"], False if spec["type"] == "boolean" else spec["default"])
            for spec in model["parameters"] if spec.get("runnerOption")}}
