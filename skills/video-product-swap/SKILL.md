---
name: video-product-swap
description: 通用视频生成与专用视频换品。通用模式使用用户提示词、参考文件、模型和输出参数；换品模式使用原视频、产品图与用户尺寸关系，内部调用 DeepSeek 分析后用 Wan3 生成。支持继续已有任务的查询与下载。
---

# 视频生成与视频换品

统一入口为 `scripts/generate_video.py`。安装包包含全部换品代码，无需另装提示词 Skill。首次安装、配置密钥时读 [references/installation.md](references/installation.md)。命令在本 Skill 根目录运行，使用依赖齐全的 Python 3.10+。

## 选择模式

- `--mode general`（默认）：用户提供提示词、模型、参考素材和输出规格；提示词不改写，不调用视觉分析。
- `--mode product-swap`：用户提供一个本地原视频、一张本地产品图、用户本人描述的体积或尺寸关系；一次调用完成分析、提示词拼装、视频生成和下载。
- `--resume <任务记录>`：只继续原任务查询或下载。不重新分析、不上传原素材、不提交新任务。不能同时传入新的生成参数。
- 只需要提示词时，不提交视频。若已安装独立提示词 Skill，可使用其提示词入口。

## 视频换品

```text
python scripts/generate_video.py --mode product-swap --video "/path/source.mp4" --reference-image "/path/product.png" --volume-relation "用户本人描述的尺寸关系" --output "/path/result.mp4" --dry-run
```

1. 本地读取原视频时长和显示比例；原始时长超过 15 秒立即停止，不调用模型、不裁剪。
2. 时长跟随原视频，小数向上取整，至少 2 秒。标准比例 `9:16`、`16:9`、`1:1`、`4:3`、`3:4` 跟随原视频；其他比例采用 `9:16`。分辨率固定 `480p`。
3. 检查素材、输出冲突及模型参数。`--dry-run` 到此返回，不调用 DeepSeek、不上传、不生成；返回 `prompt: null`、`prompt_pending: true`，不视为分析完成。
4. 正式执行前，检查当前配置中的 DeepSeek 与 Kaiyuncode 两份 Key。
5. 内部 `prompt_engine.py` 抽取视频 12 帧，调用固定模型 `deepseek-v4-flash-vision-exp` 判断产品位置、整理用户尺寸关系。产品参考图不发送给视觉模型，不根据画面猜尺寸。
6. 本地拼装：`把参考视频中{位置}的产品，替换成参考图的产品，注意：{整理后的体积关系}。` 位置不确定时用“位置不明确处”并报告警告，继续生成；接口或格式错误则停止。
7. 固定调用 Kaiyuncode 的 `wan3.0-video`，提交视频、产品图、提示词和确定的输出规格一次。

换品模式不能用 `--prompt`、`--model`、`--reference`、`--duration`、`--aspect-ratio`、`--resolution` 覆盖固定规格；需要用户自行控制时使用通用模式。缺少用户尺寸关系时索取，不套用默认比例。

桌面程序共用 `ProductSwapWorkflow.generate()`，显式传入界面所选视频/分析模型与输出规格；桌面的可调规格和 Agent 的固定预设保留为入口参数，业务步骤不再重复维护。

## 通用生成

```text
python scripts/generate_video.py --list-models
python scripts/generate_video.py --model wan3 --prompt "用户原始提示词" --reference "image=/path/product.png" --duration 5 --aspect-ratio 9:16 --resolution 480p --output "/path/result.mp4" --dry-run
```

查看已登记模型的当前输入规则，然后只检查所选模型。URL 没有可识别后缀时使用 `image=`、`video=`、`audio=` 前缀。正式生成按用户授权去掉 `--dry-run`；安装和检查任务不发起付费请求。

- Wan3：支持本地文件和公网 HTTPS 参考，至少一张图片；时长等限制由适配器验证。
- Wan 官方：调用名 `wan-official`，实际模型 `wan2.7-videoedit`，使用 DashScope 独立线路；一个视频加 1–4 张图片，2–10 秒，720p/1080p。关闭提示词扩写和水印，保留原视频音频设置。
- OmniFlash：调用名 `omniflash`，一个本地 MP4 和至少一个公网 HTTPS 参考图。不得擅自上传本地图为公开链接。
- 未登记名称保留通用 Kaiyuncode 兼容调用；服务端拒绝时报告错误，不自动改模型或改线路。

登记位于 `references/models.json`、`references/providers.json`；模型协议只维护在对应适配器中。

## 配置归属

独立调用默认只读当前视频 Skill 根目录 `.env`，不读工作目录、同级 Skill 或进程里的 Key。明确使用进程提供的配置时，可传 `--credential-source host-app`；桌面直接传入自身设置的配置快照。任何方式均不把 Key 写入全局环境。

- Kaiyuncode：`KAIYUNCODE_API_BASE`、`KAIYUNCODE_API_KEY`；同一份配置中保留 `VIDEO_SWAP_API_BASE`、`VIDEO_SWAP_API_KEY` 旧名称兼容。
- 换品分析：`DEEPSEEK_API_BASE`、`DEEPSEEK_API_KEY`。
- Wan 官方：`DASHSCOPE_API_BASE`、`DASHSCOPE_API_KEY`，地址须匹配 Key 的地域和工作空间。

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
