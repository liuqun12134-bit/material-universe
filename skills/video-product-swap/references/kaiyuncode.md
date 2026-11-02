# KaiyunCode 接入（2026-10-20）

凭据：`KAIYUNCODE_API_KEY`，`KAIYUNCODE_API_BASE=https://kaiyuncode.com`（也接受 `/v1` 结尾）。桌面与 Skill 凭据隔离。以下均为一次提交；不做付费自动重试，不更换模型或服务商。

来源：[平台文档](https://kaiyuncode.com/docs)、[公开模型目录](https://kaiyuncode.com/models)及本次用户提供的 Seedance 2.5 TJ / Omni 精确调用教程。文档与目录按模型各自核对，不能用相近型号替代。未做真实收费调用。

| API 型号 | 输入与参数 | 提交方式 |
| --- | --- | --- |
| wan3.0-video / wan3.0-video-prime | 文字，最多10图/5视频/5音频；duration整数2–30，resolution大写480P/720P/1080P，ratio，prompt_extend | `/v1/videos`；本地字段 images/videos/audio，公网字段 images/reference_video_urls/audio_urls |
| seedance-2.5-tj | 多模态参考：最多30图/10视频/10音频；每个参考视频/音频2–14秒；seconds字符串4/5/6/8/10/12/15/20/25/30；特价模型仅720p；16:9/9:16/1:1 | `/v1/videos`；multipart 纯文本字段 images/videos/audios，各字段为 JSON 数组字符串，本地素材编码为 data URL，公网 URL 原样保留；不发送文件部件 |
| omni | 本次明确接入文生：duration字符串4/6/8/10；16:9/9:16；不传resolution | `/v1/videos` JSON |
| omni_video_edit | 一个MP4（最长30秒），提示词，最多5张参考图；不传duration/resolution；可选generationConfig.videoConfig.aspectRatio | `/v1/videos`；本地原视频字段video；编辑文字和公网图片写入messages[0].content |
| gemini-nano-banana-2.1 | 提示词，可选最多16张参考图，image_size=1K/2K/4K，aspect_ratio，n=1 | `/v1/images/generations`；本地图字段image；公网图image_urls |

本地参考文件采用应用预检限制：图片 JPG/PNG/WEBP ≤10MB，视频 MP4/MOV ≤100MB，音频 MP3/WAV ≤15MB，并验证能读取；Omni Edit 原视频只接受 MP4。这些本地边界不代表服务商完整能力上限。

2026-10-22 进一步核对真实站内画布公开脚本：`https://canvas.kaiyuncode.com/canvas` 的 `95-48ba7d70e0a024bf.js` 将 Seedance 2.5 TJ 的图片、视频和音频设置为 dataUrl 输入；`256-7043047b2a101acf.js` 使用 FileReader.readAsDataURL；`app/(user)/canvas/[id]/page-bf73b56bc91c9aab.js` 将数组 JSON.stringify 后写入 FormData 纯文本字段 images/videos/audios。1.7.2 按此实际调用结构修复，替代教程的 image/videos/audio 二进制文件字段，避免 Unexpected field。所有文本部件无 filename，不外部托管、不自动重投。预览和任务诊断不保存 base64 内容。数量仍为30图/10视频/10音频，仅720p，本地视频和音频各2–14秒。未做真实收费调用。

Nano Banana 比例：1:1、16:9、9:16、3:2、2:3、4:3、3:4、4:5、5:4、21:9、4:1、1:4、8:1、1:8。同步结果读取 `data[0].url` 或 `data[0].b64_json`，解码后保存 PNG。超时无任务ID时必须由使用者核实平台记录，不能自动重提。

Omni Edit 的本地参考图使用 [Litterbox 官方 API](https://litterbox.catbox.moe/tools.php)：
`POST https://litterbox.catbox.moe/resources/internals/api.php`，multipart `reqtype=fileupload`、`time=24h`、`fileToUpload`。只上传所选图片，不上传原视频；不携带 KaiyunCode Key。返回 URL 需为 HTTPS 的 `litter.catbox.moe`。24h 为服务商的保留期，并非访问一次即销毁；有效期内持有链接者可以访问。界面在生成按钮前说明；独立 Agent 调用应先明确使用者接受临时公开。

图片上传发生在付费提交之前；失败保持 `not_submitted`。上传成功记录 URL 和预计过期时间，之后提交一次 Omni；恢复只处理原任务，不重新上传或提交。

视频保存 id/task_id 及同源 poll_url，按五秒间隔 GET 查询原任务，仅成功状态下载，明确失败停止；第三方下载链接不携带平台 Key。过期的视频结果 URL 可通过原任务只读查询刷新一次。下载结果经 ffprobe 校验，不将 HTML 错误页保存为 MP4。

内部模型 ID 采用 `kaiyuncode-` 前缀，避免与原 fal/阿里型号冲突；CLI 也允许在 `--provider kaiyuncode` 下使用精确 API 型号。

```text
python scripts/generate_video.py --provider kaiyuncode --model gemini-nano-banana-2.1 --prompt "用户原文" --reference image=/path/product.jpg --aspect-ratio 1:1 --option image_size=2K --output /path/result.png --dry-run
python scripts/generate_video.py --provider kaiyuncode --model omni_video_edit --prompt "用户编辑指令" --reference video=/path/source.mp4 --reference image=/path/product.jpg --aspect-ratio 9:16 --output /path/edited.mp4 --dry-run
```

## 智能提示词（1.9.2）

2026-11-02 按用户要求从 `gpt-6-sol` 升级为 `gpt-6.1-sol`。核对公开 `/api/developer/relay-catalog`：支持 text/image 输入、text 输出和 chat_completions / responses。智能提示词两阶段均使用 `POST /v1/chat/completions`，共用桌面 KaiyunCode 凭据，不回退旧模型或 DeepSeek。根地址与 `/v1` 地址均可。目录中旧型号默认 medium，新型号默认 low，因此显式发送 `reasoning_effort=medium`，保留原有思考档位。Fish 转写和导演规则保持不变。未做真实收费调用；目录信息不代表账户已获模型权限。
