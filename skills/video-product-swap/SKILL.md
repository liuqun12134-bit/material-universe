---
name: video-product-swap
description: 通用视频生成与专用视频换品。通用模式使用用户提示词、参考文件、模型和输出参数；换品模式使用原视频、产品图与用户尺寸关系，内部调用 DeepSeek 分析后用 Wan3 生成。支持 fal、阿里官方及 KaiyunCode 视频/图片服务。支持继续已有任务的查询与下载。
---

# 视频生成与视频换品

统一入口为 `scripts/generate_video.py`。安装包包含全部换品代码，无需另装提示词 Skill。首次安装、配置密钥时读 [references/installation.md](references/installation.md)。命令在本 Skill 根目录运行，使用依赖齐全的 Python 3.10+。

## 选择模式

- `--mode general`（默认）：用户提供提示词、模型、参考素材和输出规格；提示词不改写，不调用视觉分析。
- `--mode product-swap`：用户提供一个本地原视频、一张本地产品图、用户本人描述的体积或尺寸关系；一次调用完成分析、提示词拼装、视频生成和下载。
- `--resume <任务记录>`：只继续原任务查询或下载。不重新分析、不上传原素材、不提交新任务。不能同时传入新的生成参数。
- 只需要提示词时，不提交视频。若已安装独立提示词 Skill，可使用其提示词入口。

## 服务商与模型选择

先选服务商，再选模型；不选服务商时默认 `fal`。

- `--provider <名称>`：指定服务商；未登记的名字直接报错。
- `--model <名称或别名>`：指定模型；不填时使用服务商在 `providers.json` 配置的 `default_model`（fal 默认为 `wan3.0-video`）。
- 同时指定时校验模型归属：模型不属于所选服务商则报错，不静默改线路。
- `--list-models`：列出全部已登记模型及其服务商、适配器和输入要求。

## KaiyunCode 通用创作

使用 `--provider kaiyuncode`，API Key 为 `KAIYUNCODE_API_KEY`，地址默认 `https://kaiyuncode.com`。准确型号及参数见 [KaiyunCode 接入说明](references/kaiyuncode.md)。六个模型在无限画布提供，两个 Wan3 同时加入桌面换品。Nano Banana 输出 PNG；`--option image_size=2K` 控制尺寸。

Omni 视频编辑支持本地 MP4 与最多 5 张本地图。本地参考图会经 Litterbox 临时公开上传 24 小时，原视频直接提交 KaiyunCode；调用前须明确使用者接受临时公开参考图，不能将内部图片静默公开。`--dry-run` 不上传；已有任务恢复不重新上传。

## 视频换品

```text
python scripts/generate_video.py --mode product-swap --video "/path/source.mp4" --reference-image "/path/product.png" --volume-relation "用户本人描述的尺寸关系" --output "/path/result.mp4" --dry-run
```

可选 `--remove-subtitles` 开启“去字幕模式”，默认关闭。开启后，仅在最终换品提示词前添加固定文字 `先把参考视频中的字幕去掉，再`；原有换品提示词内容保持原样。桌面与画布提供同一开关。历史复用保存原始提示词与实际提交提示词，切换开关后不会叠加重复前缀。`--mode general` 和 `--resume` 不接受此参数。此模式仅通过 Wan3 提示词请求去字幕，生成画面仍需人工查看。

1. 本地读取原视频时长和显示比例；原始时长超过 15 秒立即停止，不调用模型、不裁剪。
2. 时长跟随原视频，小数向上取整，至少 2 秒。标准比例 `9:16`、`16:9`、`1:1`、`4:3`、`3:4` 跟随原视频；其他比例采用 `9:16`。分辨率固定 `480p`。
3. 检查素材、输出冲突及模型参数。`--dry-run` 到此返回，不调用 DeepSeek、不上传、不生成；返回 `prompt: null`、`prompt_pending: true`，不视为分析完成。
4. 正式执行前，检查当前配置中的 DeepSeek 与 fal 两份 Key。
5. 内部 `prompt_engine.py` 抽取视频 12 帧，调用固定模型 `deepseek-v4-flash-vision-exp` 判断产品位置、整理用户尺寸关系。产品参考图不发送给视觉模型，不根据画面猜尺寸。
6. 本地拼装：`把参考视频中{位置}的产品，替换成参考图的产品，注意：{整理后的体积关系}。` 位置不确定时用“位置不明确处”并报告警告，继续生成；接口或格式错误则停止。
7. 固定调用 fal 的 `wan3.0-video`（`alibaba/wan-3.0/reference-to-video`）：本地素材先上传 fal 存储取得 URL，再与提示词、输出规格一起提交一次；关闭 fal 的提示词扩写以保持拼好的提示词原样。

换品模式不能用 `--prompt`、`--model`、`--provider`、`--reference`、`--duration`、`--aspect-ratio`、`--resolution` 覆盖固定规格；需要用户自行控制时使用通用模式。缺少用户尺寸关系时索取，不套用默认比例。

桌面程序共用 `ProductSwapWorkflow.generate()`，显式传入界面所选视频/分析模型与输出规格；桌面的可调规格和 Agent 的固定预设保留为入口参数，业务步骤不再重复维护。

## 通用生成

```text
python scripts/generate_video.py --list-models
python scripts/generate_video.py --model wan3 --prompt "用户原始提示词" --reference "image=/path/product.png" --duration 5 --aspect-ratio 9:16 --resolution 480p --output "/path/result.mp4" --dry-run
```

查看已登记模型的当前输入规则，然后只检查所选模型。URL 没有可识别后缀时使用 `image=`、`video=`、`audio=` 前缀。正式生成按用户授权去掉 `--dry-run`；安装和检查任务不发起付费请求。

- Wan3 视频参考（换品）：调用名 `wan3` / `wan3.0-video`，实际模型 `alibaba/wan-3.0/reference-to-video`；本地或公网参考（图 ≤10、视频 ≤5、音频 ≤5），至少一张图，2–30 秒，480p/720p/1080p。提交时自动关闭提示词扩写。
- Wan3 文生视频：调用名 `wan3.0-t2v`，实际模型 `alibaba/wan-3.0/text-to-video`；纯提示词，不接受参考文件，2–30 秒。
- Sync3 换口型：调用名 `sync3` / `lipsync`，实际模型 `fal-ai/sync-lipsync/v3`；需要且只接受 1 个视频（MP4/MOV/WebM，≤600 秒，≤4K 4096x2160）+ 1 段音频（MP3/WAV/M4A/AAC，≤600 秒），本地文件会先探测时长和分辨率拦截；不需要提示词（--prompt 可省略）；不能指定时长/比例/分辨率；sync_mode 固定 cut_off（输出时长取视频和音频中较短者）；按输出时长计费约 $8/分钟，提交前先向用户确认费用。
- FlashVSR 画质修复：调用名 `flashvsr` / `repair`，实际模型 `fal-ai/flashvsr/upscale/video`；需要且只接受 1 个视频（MP4/MOV/WebM，≤90MB 上传上限）；不需要提示词；不能指定时长/比例/分辨率；可选参数用 `--option key=value` 传入：`upscale_factor`（1-4，默认 2）、`acceleration`（regular/high/full，默认 regular 质量最高，长视频建议更快档位）、`color_fix`（默认 true）、`preserve_audio`（默认 true 保留原音轨）；按输出像素计费 $0.0005/百万像素（宽×高×帧数），提交前先给用户看预估费用。
- 未登记名称走 fal 兼容提交（`fal_generic`）：本地文件上传 fal 存储后按常见字段名提交；服务端拒绝时报告错误，不自动改模型或改线路。

登记位于 `references/models.json`、`references/providers.json`；模型协议只维护在对应适配器中。

## 配置归属

独立调用默认只读当前视频 Skill 根目录 `.env`，不读工作目录、同级 Skill 或进程里的 Key。明确使用进程提供的配置时，可传 `--credential-source host-app`；桌面直接传入自身设置的配置快照。任何方式均不把 Key 写入全局环境。

- fal：`FAL_API_BASE`（默认 `https://queue.fal.run`）、`FAL_KEY`。
- 换品分析：`DEEPSEEK_API_BASE`、`DEEPSEEK_API_KEY`。

真实 Key 不进入源码、安装包、聊天或日志。不同来源的查询地址不得携带原 API Key；跨源成片下载不携带原 API Key。

## 任务恢复与结果

正式提交前，在输出文件旁创建 `<输出文件>.video-task.json`；取得任务 ID 后立即保存，再查询状态。记录不含真实 Key；本地文件锁防止同一记录并发执行。

```text
python scripts/generate_video.py --resume "/path/result.mp4.video-task.json"
```

- 查询断网或超时：报告任务记录路径，继续时使用 `--resume`。
- 已有成片地址但下载失败：返回地址，恢复时只下载。
- 成片已保存且校验一致：恢复直接返回已有结果。
- 提交结果不明确、未取得任务 ID：停止，先到服务商核实是否受理，不盲目重新提交。
- 服务商明确失败：保留失败原因，恢复不会重新生成。
- 已存在任务记录或输出文件：不通过再次普通生成覆盖它；需要一笔新任务时由用户明确指定新的输出位置。

桌面的“继续已有任务”可选择该记录进行恢复。报告输出路径、成片地址、实际规格和警告。技术完成不代表画面合格；不自动验收、不后期修片、不自动付费重试。

## 旧任务恢复

旧 fal OmniFlash 新生成入口仍关闭；KaiyunCode 的 `omni` 与 `omni_video_edit` 为独立的新路线。一键换品仅支持 Wan3；已有任务仍可 `--resume`。
KaiyunCode、DashScope 支持新生成与旧任务查询、下载；在本 Skill `.env` 显式配置原服务的 `KAIYUNCODE_API_KEY` / `KAIYUNCODE_API_BASE` 或 `DASHSCOPE_API_KEY` / `DASHSCOPE_API_BASE`。桌面在设置的对应服务商中填写自己的凭据。不会借用 fal Key 或其他应用配置。已有公网成片链接可直接恢复下载。
临时 HTTP 错误不会判定生成永久失败；旧版误标的 408/429/5xx 任务可继续查询原任务，不重新提交。
