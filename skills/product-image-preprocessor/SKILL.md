---
name: product-image-preprocessor
description: 分析产品图片是否为干净、无中文且多视角一致的棚拍参考图；不合格时使用已注册生图模型生成一次并复检。用于视频换品前的产品参考图预处理，不执行视频生成或提示词改写。
---

# 产品图片分析与预处理

把一张本地产品图转换为可供视频换品使用的最终产品参考图。

## 默认模型

- 视觉分析：`deepseek-v4-flash-vision-exp`
- 图片生成：`seedream-5.0-pro`

用户可通过 `--vision-model` 和 `--image-model` 选择其他已注册模型；先用 `--list-models` 查看可选项。未知模型必须明确失败，不能猜测兼容协议。

## 固定流程

1. 用视觉模型检查输入图是否同时满足：背景干净、产品完整清晰、没有中文、包含至少三个有意义角度、不同视图保持同一产品结构。
2. 合格时直接返回原图，不调用生图模型。
3. 不合格时，以原图为唯一产品身份参考，调用生图模型生成一次 1:1 无中文多视角棚拍图。
4. 用同一个视觉模型复检生成图；合格才返回 `final_image`，不合格则保留候选图并停止。

不得自动付费重试或自动切换模型。模型技术失败时报告原因，由用户选择其他模型后重新运行。由单张正面图推导出的侧面和背面只是模型重建，不代表真实包装背面。

Seedream 提交成功后必须立即把 `task_id` 保存到输出图片旁的 `.seedream-task.json`。轮询临时断线或可重试的服务端错误时，只继续查询同一任务，不得重新提交；再次使用相同输入和输出运行时应恢复该任务。

## 凭据边界

直接调用 Skill 时只读取本 Skill 目录下的 `.env`，不读取 Windows、Codex 进程或其他 Skill 的 Key。复制 `.env.example` 为 `.env` 并填写需要的服务：

```text
DEEPSEEK_API_KEY=
KAIYUNCODE_API_KEY=
```

宿主应用调用时传入 `--credential-source host-app`，并在子进程环境中显式提供自己的 Key。宿主配置为空时必须失败，不得回退到 Skill 的 `.env`。

## 执行

```powershell
python -X utf8 scripts/preprocess_product_image.py --input "<产品原图>" --output "<输出图片>" --json
```

只分析，不生成：

```powershell
python -X utf8 scripts/preprocess_product_image.py --input "<产品原图>" --analyze-only --json
```

查看模型和检查请求计划：

```powershell
python -X utf8 scripts/preprocess_product_image.py --list-models
python -X utf8 scripts/preprocess_product_image.py --input "<产品原图>" --dry-run --json
```

机器调用应读取 JSON 中的 `success`、`action`、`quality_passed`、`final_image`、`candidate_image` 和 `error`。`technical_completed` 只表示接口和下载完成，不能替代视觉合格。

Seedream 已确认的异步端点和当前参考图字段边界见 [references/seedream-api.md](references/seedream-api.md)。
