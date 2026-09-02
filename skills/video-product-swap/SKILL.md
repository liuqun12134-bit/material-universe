---
name: video-product-swap
description: Call supported video-generation models through one interface while preserving each model's own input, upload, API, polling, and download rules. Use when the user selects a video model, prompt, references, and output path; do not use for prompt rewriting, visual analysis, post-processing, or automatic paid retries.
---

# 多模型视频调用

使用一个统一入口接收用户的模型、参考文件、提示词和输出位置，再把任务交给对应的模型适配器。提示词必须原样传递，不得自动换模型、补写提示词或进行付费重试。

## 使用流程

1. 用户明确选择模型、参考文件、提示词和输出位置。
2. 运行 `--list-models` 查看已登记模型的输入要求；只读取当前模型的规则。
3. 需要先核对参数时运行 `--dry-run`。它不得上传文件或提交任务。
4. 正式运行后，由适配器完成验证、上传、提交、轮询和下载。
5. 报告技术执行结果、输出路径和 `video_url`；API 成功不代表视觉质量合格。

## 统一入口

```powershell
python scripts/generate_video.py --prompt "用户原始提示词" --model "模型名称" --reference "D:\素材\参考文件.mp4" --reference "image=https://example.com/ref.png" --output "D:\输出\result.mp4"
```

查看模型：

```powershell
python scripts/generate_video.py --list-models
```

只核对、不提交：

```powershell
python scripts/generate_video.py --dry-run --prompt "用户原始提示词" --model "omniflash" --reference "video=D:\素材\source.mp4" --reference "image=https://example.com/ref.png"
```

URL 没有可识别扩展名时，使用 `image=`、`video=` 或 `audio=` 前缀。

## 图形界面

整个桌面程序名为“素材万象”，AI 视频换品只是当前的第一个功能模块。Windows 用户可双击项目根目录中的 `启动素材万象.cmd`，Skill 根目录也保留同名启动器。

当前包含：

- `设置`：合并管理工作区成片保存目录、提示词分析 API 和视频服务 API；使用 Windows 当前账户加密保存，不覆盖 Skill 原有 `.env`。
- `AI 视频换品`：固定使用 `wan3.0-video`，不向任务用户暴露模型、提示词或输出路径。用户只输入原视频、产品参考图和尺寸关系，确认画面比例、秒数、分辨率后点击一次“一键开始换品”。提示词分析、输出命名和保存目录均由后台处理。

AI 视频换品模块按“素材 → 尺寸关系 → 输出设置 → 一键换品”的顺序呈现。选择原视频后用 ffprobe 自动读取时长、宽高和标准比例；秒数默认跟随原视频，标准比例跟随原视频，非标准比例回退为 9:16。默认分辨率为 480p。界面内提供原视频播放窗口和产品参考图缩略图，不显示素材的完整本地路径。

`omni_video_edit` 仍要求公网 HTTPS 参考图。工作台不会擅自公开上传本地图；本地图仅用于提示词 Skill 的文件存在校验，用户需另外提供已授权使用的公网参考图 URL。

“开始生成”会在提交前再次确认可能产生费用。一次点击只执行一次正式提交，不自动重试；完成状态仍区分技术执行成功与待人工视觉验收。

命令行入口也支持输出规格：

```powershell
python scripts/generate_video.py --prompt "用户原始提示词" --model "wan3" --reference "image=D:\素材\参考图.png" --aspect-ratio "9:16" --duration 5 --resolution "720p" --output "D:\输出\result.mp4"
```

## 已登记适配器

- Wan3：接受本地文件或公网 HTTPS URL；至少一张图片。具体格式、大小、数量和时长由 Wan3 适配器检查。
- Wan 官方 VideoEdit：使用阿里云 Model Studio 官方 DashScope SDK。当前官方文档登记模型为 `wan2.7-videoedit`，调用名为 `wan-official-videoedit`（别名 `wan-official`）。它接受一个本地或公网视频及 1-4 张本地或公网参考图；本地文件只在正式提交时由官方 SDK 上传，`--dry-run` 不上传。
- OmniFlash：接受一个本地 MP4 源视频和至少一个公网 HTTPS 参考图，不接受本地参考图、远程源视频或音频。
- 未登记模型：为兼容旧用法，交给通用中转站适配器；服务端拒绝时原样报告错误。需要针对性优化时，应新增独立适配器和模型登记，不要继续向通用分支堆条件。

模型与服务商登记在 `references/models.json` 和 `references/providers.json`。修改或新增适配器时，只改对应模型文件，并根据该模型实际 API 文档验证参数、提交、轮询和下载逻辑。

## API Key

公共密钥管理器按服务商读取配置。Kaiyuncode 优先读取：

- `KAIYUNCODE_API_BASE`
- `KAIYUNCODE_API_KEY`

为兼容现有安装，缺少以上配置时继续读取 `VIDEO_SWAP_API_BASE` 和 `VIDEO_SWAP_API_KEY`。真实密钥只能放在本机 Skill 的 `.env` 或系统环境变量中，不得写入提示词、源码、注册表或日志。

不同源的轮询地址不得携带原 API Key；不同源的结果下载也不得携带原 API Key。不得擅自把本地参考文件上传为公开链接。

Wan 官方线路单独读取：

- `DASHSCOPE_API_BASE`（中国内地旧版兼容地址默认为 `https://dashscope.aliyuncs.com/api/v1`；工作空间专属地址应完整填写到 `/api/v1`）
- `DASHSCOPE_API_KEY`

DashScope API Key 与服务地域绑定，API 地址必须与 Key 所属地域及工作空间一致。官方线路固定关闭 `prompt_extend`、关闭水印并保留原视频音频，避免服务端改写用户提示词。官方接口当前只支持 2-10 秒以及 720p/1080p。

官方线路示例：

```powershell
python scripts/generate_video.py --dry-run --prompt "用户原始提示词" --model "wan-official" --reference "video=D:\素材\source.mp4" --reference "image=D:\素材\product.png" --output "D:\输出\result.mp4"
```
