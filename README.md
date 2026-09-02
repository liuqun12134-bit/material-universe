# 素材万象

素材万象是一套面向电商产品素材的本地 AI 工具，当前重点是视频换品工作流：产品图预处理、参考视频位置分析、固定提示词生成以及多服务商视频生成。

## 当前组件

- `product-image-preprocessor`：检查产品参考图，并在需要时生成干净的棚拍参考图。
- `ai-video-swap-prompt-generator`：从参考视频抽帧识别产品位置，整理用户提供的尺寸关系，并拼装固定提示词。
- `video-product-swap`：通过统一入口调用已登记的视频模型，支持 Kaiyun 中转站 Wan3、Wan 官方 VideoEdit 和 OmniFlash。
- `素材万象` 桌面界面：统一选择素材、配置输出规格并管理宿主应用自己的 API Key。

## 使用

安装 Python 依赖后，双击根目录的 `启动素材万象.cmd`，或进入 `skills/video-product-swap` 运行：

```powershell
python -m pip install -r requirements.txt
python scripts/generate_video.py --list-models
```

正式生成前可以使用 `--dry-run` 检查模型线路、输入、上传字段和请求结构。检查模式不会上传文件或提交付费任务。

## 凭据安全

仓库只提供 `.env.example`，不包含任何真实 API Key。请在本机配置自己的凭据，不要把 `.env`、日志、生成视频或加密凭据文件提交到 Git。

桌面应用的 API Key 与各个 Skill 自己的 `.env` 相互隔离；宿主应用未配置 Key 时会明确停止，不会静默读取 Skill 的 Key。

## 文档

项目设计、决策和路线说明位于 `素材万象_MD文档包`。

