---
name: ai-video-swap-prompt-generator
description: 根据参考视频识别产品所在位置，整理用户提供的体积或外形尺寸关系，并拼装固定的 AI 视频换品提示词。仅用于生成提示词，不执行视频换品或自行估算尺寸。
---

# AI 视频换品提示词生成器

使用 `scripts/generate_swap_prompt.py` 通过 DeepSeek 官方 OpenAI 兼容接口调用 `deepseek-v4-flash-vision-exp`。脚本从参考视频本地抽取 12 帧时间序列图，供 DeepSeek 识别产品位置；同时把用户提供的体积或外形尺寸关系整理成清楚、无歧义的一句话，最后由本地脚本拼装固定换品提示词。

## 必需输入

- 参考视频的本地路径。
- 产品参考图的本地路径。脚本只验证该文件存在，不把参考图发送给 DeepSeek。
- 用户亲自描述的体积或外形尺寸关系，例如“参考图产品大概是原视频产品的 1.5 倍”，或“高度为原视频产品的三分之二、宽度一致”。

缺少关系描述时必须向用户索取。不得猜测、估算、补默认值或让 DeepSeek 根据视频、参考图自行计算关系。

## 固定边界

- 默认模型必须是 `deepseek-v4-flash-vision-exp`；仅当用户明确指定其他兼容视觉模型时才通过 `--model` 修改。
- 默认接口为 DeepSeek 官方 `https://api.deepseek.com/chat/completions`。
- DeepSeek 只做两件事：把产品位置分类为人物手中、桌面上、其他位置或不确定；把用户给出的体积或外形尺寸关系整理为简短、明确的中文关系句。
- 关系中的方向、倍数、数字、维度、程度和“大概/约/略微”等不确定性必须来自用户原话。DeepSeek 可以去掉口头语和重复，但不得添加用户没有表达的比例、尺寸或结论。
- 如果用户只给出高度和宽度，不得推导整体体积、深度或长度。
- 不让 DeepSeek 分析参考图、识别品牌或外观、根据画面估算尺寸、规划编辑、生成视频或评价换品效果。
- 参考图不上传给 DeepSeek；只验证文件存在。发送给 DeepSeek 的是参考视频抽取的时间序列图，不是视频文件本身；它们只用于位置分类，不作为尺寸关系的计算依据。
- 请求使用非思考模式和 JSON Output，避免额外推理内容干扰固定 JSON 结果。
- DeepSeek API 若拒绝多模态 `image_url` 输入，停止并报告接口能力不足，不自动改用其他模型或付费重试。
- 最终换品提示词由脚本使用固定模板拼装，不让 DeepSeek 自由改写整个提示词。

## 执行

首次使用时，如果缺少依赖，在本 Skill 目录运行：

```powershell
python -m pip install -r requirements.txt
```

复制 `.env.example` 为本 Skill 目录下的 `.env`，并由用户在这个 `.env` 中填写 DeepSeek 官方 API Key。直接调用 Skill 时只读取此文件，必须忽略 Windows、Codex 进程和其他程序环境中的 `DEEPSEEK_API_KEY`；Skill 自己的 `.env` 未配置时立即失败。不得回显密钥。

“素材万象”等宿主应用必须使用自己的凭据存储，并在调用脚本时显式传入 `--credential-source host-app`。宿主应用未配置 Key 时必须在调用前阻止；宿主 Key 不得写入或回退到 Skill 的 `.env`。

运行：

```powershell
python scripts/generate_swap_prompt.py --video "<参考视频>" --reference-image "<参考图>" --volume-relation "<用户描述的体积或外形尺寸关系>"
```

将脚本标准输出中的最终提示词原样交付。只有用户明确需要机器可读结果时才增加 `--json`；只有用户指定保存路径时才增加 `--output`。

若模型返回无法识别，提示词使用“位置不明确处”，同时明确告诉用户位置识别不确定，不自行改成手中或桌面。

## 固定模板

```text
把参考视频中{位置}的产品，替换成参考图的产品，注意：{整理后的体积关系}。
```
