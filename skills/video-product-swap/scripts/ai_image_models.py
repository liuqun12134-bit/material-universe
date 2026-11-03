"""Fish image capability handling. Bundled schemas were checked on 2026-11-03.

Snapshots only populate offline controls. Every quote validates live capabilities;
prices are never taken from these snapshots.
"""
import copy
import json
import math

DEFAULT_MODEL = "gpt-image-2.5-sunburst"
DEFAULT_PARAMETERS = {"aspect_ratio": "9:16", "resolution": "1K", "quality": "medium"}
FOLLOW = "跟随图1"
MODEL_LABELS = {
    DEFAULT_MODEL: "GPT Image 2.5 · Sunburst",
    "gpt-image-2.5-flare": "GPT Image 2.5 · Flare", "gpt-image-2": "GPT Image 2",
    "nano-banana": "Nano Banana", "nano-banana-2-lite": "Nano Banana 2 Lite",
    "seedream-5-0": "Seedream 5.0", "seedream-4-5": "Seedream 4.5",
    "gemini-3-pro-image": "Gemini 3 Pro Image",
}
QUALITY_LABELS = {"low": "低", "medium": "中等", "high": "高", "xhigh": "更高", "max": "最高"}
PARAMETER_LABELS = {"aspect_ratio": "画面比例", "resolution": "分辨率", "quality": "生成精度"}
BUILTIN_MODELS = json.loads(r'''{"gpt-image-2.5-flare":{"model_id":"gpt-image-2.5-flare","prompt":"required","prompt_max_chars":32000,"capabilities":{"t2i":{"parameter_schema":{"type":"object","properties":{"aspect_ratio":{"type":"string","enum":["3:1","21:9","2:1","16:9","3:2","4:3","5:4","1:1","4:5","3:4","2:3","9:16","1:2","1:3"],"default":"16:9"},"resolution":{"type":"string","enum":["1K","2K","4K"],"default":"1K"},"quality":{"type":"string","enum":["low","medium","high","xhigh","max"],"default":"low"}},"additionalProperties":false},"max_count":8},"edit":{"parameter_schema":{"type":"object","properties":{"aspect_ratio":{"type":"string","enum":["3:1","21:9","2:1","16:9","3:2","4:3","5:4","1:1","4:5","3:4","2:3","9:16","1:2","1:3"],"default":"16:9"},"resolution":{"type":"string","enum":["1K","2K","4K"],"default":"1K"},"quality":{"type":"string","enum":["low","medium","high","xhigh","max"],"default":"low"}},"additionalProperties":false},"max_count":1,"required_roles":["ref"],"inputs":{"ref":{"kind":"image","mime_types":["image/jpeg","image/png","image/webp"],"max_bytes":20971520,"max_count":16}},"max_inputs":16}}},"gpt-image-2":{"model_id":"gpt-image-2","prompt":"required","prompt_max_chars":32000,"capabilities":{"t2i":{"parameter_schema":{"type":"object","properties":{"aspect_ratio":{"type":"string","enum":["3:1","21:9","2:1","16:9","3:2","4:3","5:4","1:1","4:5","3:4","2:3","9:16","1:2","1:3"],"default":"16:9"},"resolution":{"type":"string","enum":["1K","2K","4K"],"default":"1K"},"quality":{"type":"string","enum":["low","medium","high"],"default":"low"}},"additionalProperties":false},"max_count":8},"edit":{"parameter_schema":{"type":"object","properties":{"aspect_ratio":{"type":"string","enum":["3:1","21:9","2:1","16:9","3:2","4:3","5:4","1:1","4:5","3:4","2:3","9:16","1:2","1:3"],"default":"16:9"},"resolution":{"type":"string","enum":["1K","2K","4K"],"default":"1K"},"quality":{"type":"string","enum":["low","medium","high"],"default":"low"}},"additionalProperties":false},"max_count":1,"required_roles":["ref"],"inputs":{"ref":{"kind":"image","mime_types":["image/jpeg","image/png","image/webp"],"max_bytes":20971520,"max_count":17}},"max_inputs":17}}},"nano-banana":{"model_id":"nano-banana","prompt":"required","prompt_max_chars":20000,"capabilities":{"t2i":{"parameter_schema":{"type":"object","properties":{"aspect_ratio":{"type":"string","enum":["8:1","4:1","21:9","16:9","3:2","4:3","5:4","1:1","4:5","3:4","2:3","9:16","1:4","1:8"],"default":"16:9"},"resolution":{"type":"string","enum":["0.5K","1K","2K","4K"],"default":"1K"}},"additionalProperties":false},"max_count":8},"edit":{"parameter_schema":{"type":"object","properties":{"aspect_ratio":{"type":"string","enum":["auto","8:1","4:1","21:9","16:9","3:2","4:3","5:4","1:1","4:5","3:4","2:3","9:16","1:4","1:8"],"default":"16:9"},"resolution":{"type":"string","enum":["0.5K","1K","2K","4K"],"default":"1K"}},"additionalProperties":false},"max_count":1,"required_roles":["ref"],"inputs":{"ref":{"kind":"image","mime_types":["image/jpeg","image/png","image/webp"],"max_bytes":20971520,"max_count":14}},"max_inputs":14}}},"gemini-3-pro-image":{"model_id":"gemini-3-pro-image","prompt":"required","prompt_max_chars":20000,"capabilities":{"t2i":{"parameter_schema":{"type":"object","properties":{"aspect_ratio":{"type":"string","enum":["21:9","16:9","3:2","4:3","5:4","1:1","4:5","3:4","2:3","9:16"],"default":"16:9"},"resolution":{"type":"string","enum":["1K","2K","4K"],"default":"2K"}},"additionalProperties":false},"max_count":8},"edit":{"parameter_schema":{"type":"object","properties":{"aspect_ratio":{"type":"string","enum":["auto","21:9","16:9","3:2","4:3","5:4","1:1","4:5","3:4","2:3","9:16"],"default":"16:9"},"resolution":{"type":"string","enum":["1K","2K","4K"],"default":"2K"}},"additionalProperties":false},"max_count":1,"required_roles":["ref"],"inputs":{"ref":{"kind":"image","mime_types":["image/jpeg","image/png","image/webp"],"max_bytes":20971520,"max_count":10}},"max_inputs":10}}},"seedream-4-5":{"model_id":"seedream-4-5","prompt":"required","prompt_max_chars":4000,"capabilities":{"t2i":{"parameter_schema":{"type":"object","properties":{"aspect_ratio":{"type":"string","enum":["16:9","4:3","1:1","3:4","9:16"],"default":"16:9"},"resolution":{"type":"string","enum":["2K","4K"],"default":"2K"}},"additionalProperties":false},"max_count":8},"edit":{"parameter_schema":{"type":"object","properties":{"aspect_ratio":{"type":"string","enum":["16:9","4:3","1:1","3:4","9:16"],"default":"16:9"},"resolution":{"type":"string","enum":["2K","4K"],"default":"2K"}},"additionalProperties":false},"max_count":1,"required_roles":["ref"],"inputs":{"ref":{"kind":"image","mime_types":["image/jpeg","image/png","image/webp"],"max_bytes":20971520,"max_count":14,"limits":{"maxPixelCount":36000000}}},"max_inputs":14}}},"gpt-image-2.5-sunburst":{"model_id":"gpt-image-2.5-sunburst","prompt":"required","prompt_max_chars":32000,"capabilities":{"t2i":{"parameter_schema":{"type":"object","properties":{"aspect_ratio":{"type":"string","enum":["3:1","21:9","2:1","16:9","3:2","4:3","5:4","1:1","4:5","3:4","2:3","9:16","1:2","1:3"],"default":"16:9"},"resolution":{"type":"string","enum":["1K","2K","4K"],"default":"1K"},"quality":{"type":"string","enum":["low","medium","high","xhigh","max"],"default":"low"}},"additionalProperties":false},"max_count":8},"edit":{"parameter_schema":{"type":"object","properties":{"aspect_ratio":{"type":"string","enum":["3:1","21:9","2:1","16:9","3:2","4:3","5:4","1:1","4:5","3:4","2:3","9:16","1:2","1:3"],"default":"16:9"},"resolution":{"type":"string","enum":["1K","2K","4K"],"default":"1K"},"quality":{"type":"string","enum":["low","medium","high","xhigh","max"],"default":"low"}},"additionalProperties":false},"max_count":1,"required_roles":["ref"],"inputs":{"ref":{"kind":"image","mime_types":["image/jpeg","image/png","image/webp"],"max_bytes":20971520,"max_count":16}},"max_inputs":16}}},"seedream-5-0":{"model_id":"seedream-5-0","prompt":"required","prompt_max_chars":4000,"capabilities":{"t2i":{"parameter_schema":{"type":"object","properties":{"aspect_ratio":{"type":"string","enum":["16:9","4:3","1:1","3:4","9:16"],"default":"16:9"},"resolution":{"type":"string","enum":["2K","3K"],"default":"2K"}},"additionalProperties":false},"max_count":8},"edit":{"parameter_schema":{"type":"object","properties":{"aspect_ratio":{"type":"string","enum":["16:9","4:3","1:1","3:4","9:16"],"default":"16:9"},"resolution":{"type":"string","enum":["2K","3K"],"default":"2K"}},"additionalProperties":false},"max_count":1,"required_roles":["ref"],"inputs":{"ref":{"kind":"image","mime_types":["image/jpeg","image/png","image/webp"],"max_bytes":20971520,"max_count":14,"limits":{"maxPixelCount":36000000}}},"max_inputs":14}}},"nano-banana-2-lite":{"model_id":"nano-banana-2-lite","prompt":"required","prompt_max_chars":20000,"capabilities":{"t2i":{"parameter_schema":{"type":"object","properties":{"aspect_ratio":{"type":"string","enum":["16:9","3:2","1:1","2:3","9:16"],"default":"16:9"}},"additionalProperties":false},"max_count":8},"edit":{"parameter_schema":{"type":"object","properties":{"aspect_ratio":{"type":"string","enum":["16:9","3:2","1:1","2:3","9:16"],"default":"16:9"}},"additionalProperties":false},"max_count":1,"required_roles":["ref"],"inputs":{"ref":{"kind":"image","mime_types":["image/jpeg","image/png","image/webp"],"max_bytes":20971520,"max_count":14}},"max_inputs":14}}}}''')


def capability(model, has_references):
    return model.get("capabilities", {}).get("edit" if has_references else "t2i", {})


def properties(model, has_references):
    return capability(model, has_references).get("parameter_schema", {}).get("properties", {})


def reference_limit(model):
    cap = capability(model, True)
    return min(cap.get("max_inputs", 0), cap.get("inputs", {}).get("ref", {}).get("max_count", 0))


def normalize_parameters(model, parameters, has_references):
    """For an explicit model/input change in the UI only; never alter a quote silently."""
    selected, changes = {}, []
    specs = properties(model, has_references)
    for key, label in PARAMETER_LABELS.items():
        options = specs.get(key, {}).get("enum", [])
        old = parameters.get(key)
        if not options:
            if old is not None: changes.append(label + "改为模型默认")
            continue
        value = old
        if not (key == "aspect_ratio" and value == FOLLOW and has_references) and value not in options:
            preferred = DEFAULT_PARAMETERS.get(key)
            value = preferred if preferred in options else specs[key].get("default", options[0])
            if value not in options: value = options[0]
            if old is not None and old != value:
                changes.append(label + "已调整为 " + QUALITY_LABELS.get(value, str(value)))
        selected[key] = value
    return selected, changes


def resolve_parameters(parameters, model, media):
    result = copy.deepcopy(parameters)
    specs = properties(model, bool(media))
    if result.get("aspect_ratio") == FOLLOW:
        if not media: raise ValueError("没有图1，不能跟随参考图比例。")
        options = specs.get("aspect_ratio", {}).get("enum", [])
        if "auto" in options:
            result["aspect_ratio"] = "auto"
        else:
            ratios = []
            for option in options:
                try:
                    width, height = map(float, option.split(":"))
                    if width > 0 and height > 0: ratios.append((option, width / height))
                except (ValueError, AttributeError): pass
            if not ratios: raise ValueError("当前模型没有可跟随的比例选项。")
            ratio = media[0]["width"] / media[0]["height"]
            result["aspect_ratio"] = min(ratios, key=lambda p: abs(math.log(ratio / p[1])))[0]
    for key, value in result.items():
        if value not in specs.get(key, {}).get("enum", []):
            raise ValueError("模型规格已改变，请刷新模型并重新选择" + PARAMETER_LABELS.get(key, key) + "。")
    required = capability(model, bool(media)).get("parameter_schema", {}).get("required", [])
    if any(key not in result for key in required):
        raise ValueError("当前模型需要额外参数，请刷新规格或选择其他模型。")
    return result
