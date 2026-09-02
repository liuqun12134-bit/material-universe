from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

from ..core import Credential, ImagePreprocessorError, ModelSpec, image_data_uri, normalize_analysis
from .base import VisionAdapter


ANALYSIS_PROMPT = """
你是一个严格的电商产品参考图质量检查器。观察图片中的全部内容，判断它能否直接作为 AI 视频换品的产品外观参考图。

必须逐项检查：
1. clean_studio_background：白色或浅色的干净棚拍背景，没有人物、手、生活杂物、复杂道具或水印。
2. no_chinese_text：包装、背景、角标和水印中均没有可见中文。英文、拉丁字母、品牌名、数字和容量可以存在。
3. multi_view：同一张图中至少有三个有意义的产品角度，例如正面、四分之三侧面、侧面或背面；重复的同角度不计数。
4. product_complete_and_clear：产品主体完整、清晰，没有严重裁切、遮挡、模糊或低分辨率问题。
5. cross_view_consistent：各视图像同一个产品，瓶型、盖子、材质、颜色、比例、标签位置和开合状态没有明显冲突。

只返回 JSON，不要 Markdown，不要解释。格式必须是：
{
  "clean_studio_background": true,
  "no_chinese_text": true,
  "multi_view": true,
  "view_count": 4,
  "product_complete_and_clear": true,
  "cross_view_consistent": true,
  "reasons": [],
  "visible_chinese_text": []
}

reasons 用简短中文列出所有不合格原因。visible_chinese_text 只列出确实看见的中文；看不清时不要猜。
""".strip()


def _content_text(payload: dict[str, Any]) -> str:
    try:
        content = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ImagePreprocessorError("DeepSeek API 响应中没有可识别的消息内容。") from exc
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        text = "".join(
            str(item.get("text", ""))
            for item in content
            if isinstance(item, dict) and item.get("type") in {"text", "output_text"}
        ).strip()
        if text:
            return text
    raise ImagePreprocessorError("DeepSeek API 返回了无法识别的消息内容。")


def _json_object(text: str) -> dict[str, Any]:
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        stripped = "\n".join(lines).strip()
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise ImagePreprocessorError("DeepSeek 没有返回有效的 JSON 检查结果。") from exc
    if not isinstance(payload, dict):
        raise ImagePreprocessorError("DeepSeek 检查结果必须是 JSON 对象。")
    return payload


class DeepSeekVisionAdapter(VisionAdapter):
    name = "deepseek-vision"

    @staticmethod
    def _endpoint(credential: Credential) -> str:
        return urljoin(credential.api_base + "/", credential.submit_path)

    def plan(self, image_path: Path, model: ModelSpec, credential: Credential) -> dict[str, Any]:
        return {
            "model": model.api_model,
            "provider": model.provider,
            "adapter": self.name,
            "endpoint": self._endpoint(credential),
            "credential_source": credential.api_key_source,
            "credential_configured": bool(credential.api_key),
            "request_preview": {
                "model": model.api_model,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "<fixed-product-image-analysis-prompt>"},
                            {"type": "image_url", "image_url": {"url": "<local-image-as-data-uri>"}},
                        ],
                    }
                ],
                "thinking": {"type": "disabled"},
                "response_format": {"type": "json_object"},
            },
            "input": str(image_path),
        }

    def analyze(
        self,
        image_path: Path,
        model: ModelSpec,
        credential: Credential,
        timeout: int,
    ) -> dict[str, Any]:
        if not credential.api_key:
            raise ImagePreprocessorError("DeepSeek 视觉分析缺少 API Key。")
        body = {
            "model": model.api_model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": ANALYSIS_PROMPT},
                        {"type": "image_url", "image_url": {"url": image_data_uri(image_path)}},
                    ],
                }
            ],
            "stream": False,
            "thinking": {"type": "disabled"},
            "response_format": {"type": "json_object"},
        }
        request = urllib.request.Request(
            self._endpoint(credential),
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {credential.api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[-1500:]
            raise ImagePreprocessorError(f"DeepSeek 视觉分析失败（HTTP {exc.code}）：{detail}") from exc
        except urllib.error.URLError as exc:
            raise ImagePreprocessorError(f"无法连接 DeepSeek：{exc.reason}") from exc
        except json.JSONDecodeError as exc:
            raise ImagePreprocessorError("DeepSeek API 没有返回有效 JSON。") from exc
        result = normalize_analysis(_json_object(_content_text(payload)))
        return {"model": model.api_model, "provider": model.provider, **result}
