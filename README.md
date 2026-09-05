<p align="center">
  <img src="skills/video-product-swap/assets/material-universe-logo.png" alt="素材万象 Logo" width="128">
</p>

# 素材万象

素材万象是一套面向电商产品素材的本地 AI 工具，当前重点是视频换品：参考视频位置分析、固定提示词生成和多服务商视频生成。产品图预处理保留为独立的可选工具。

## 简化后的调用关系

桌面或 Agent → 视频 Skill → 通用提示词 / 内部换品分析 → 对应模型适配器 → 保存任务 ID、查询、下载。

桌面和 Agent 复用同一换品流程；视频 Skill 已自带提示词引擎。独立调用只读自己的 `.env`，桌面显式传入自己的配置。断网或下载失败后，命令行使用 `--resume <输出文件>.video-task.json`，桌面点击“继续已有任务”；不会重新提交生成。

## 当前组件

- `product-image-preprocessor`：检查产品参考图，并在需要时生成干净的棚拍参考图。
- `ai-video-swap-prompt-generator`：从参考视频抽帧识别产品位置，整理用户提供的尺寸关系，并拼装固定提示词。
- `video-product-swap`：通用模式自选提示词、参考文件、模型及输出参数；视频换品模式输入视频、产品图和尺寸关系，自动用 DeepSeek 分析并调用 Wan3。通用接口继续支持 Kaiyun 中转站 Wan3、Wan 官方 VideoEdit 和 OmniFlash。
- `素材万象` 桌面界面：统一选择素材、配置输出规格并管理宿主应用自己的 API Key。

## 使用

安装 Python 依赖后，双击根目录的 `启动素材万象.cmd`，或进入 `skills/video-product-swap` 运行：

```powershell
python -m pip install -r requirements.txt
python scripts/generate_video.py --list-models
```

正式生成前可以使用 `--dry-run` 检查模型线路、输入、上传字段和请求结构。检查模式不会上传文件或提交付费任务。

## Windows 安装版（推荐）

运行 `packaging/build_installer.ps1` 可以在 `发行版` 文件夹生成 `素材万象安装程序.exe`。安装后，素材万象会出现在 Windows 开始菜单和“已安装的应用”中，并带有标准卸载程序；安装时也可以选择创建桌面快捷方式。

安装版使用免重复解压的程序目录，默认安装到当前用户的 `%LOCALAPPDATA%\Programs\MaterialUniverse`。工作区位于 `%USERPROFILE%\Documents\素材万象`，API Key、设置和日志位于 `%LOCALAPPDATA%\MaterialUniverse`。卸载程序不会删除工作区和已经生成的视频。

## 单文件便携版

运行 `packaging/build_release.ps1` 可以在 `发行版` 文件夹生成单文件 `素材万象.exe`。成品已包含 Python、模型适配器、界面资源以及 FFmpeg、FFprobe、FFplay，目标 Windows 电脑不需要安装或配置这些组件。

单文件版适合临时复制使用；日常使用优先选择安装版，避免每次启动时重复释放内置运行文件。

使用者首次打开后只需在“设置”中填写自己的提示词分析和视频服务 API Key。真实 Key 不参与打包，也不会提交到仓库。

## 凭据安全

仓库只提供 `.env.example`，不包含任何真实 API Key。请在本机配置自己的凭据，不要把 `.env`、日志、生成视频或加密凭据文件提交到 Git。

桌面应用的 API Key 与各个 Skill 自己的 `.env` 相互隔离；宿主应用未配置 Key 时会明确停止，不会静默读取 Skill 的 Key。

## 文档

项目设计、决策和路线说明位于 `素材万象_MD文档包`。
