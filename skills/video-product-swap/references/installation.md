# 安装与配置

面向能读取 `SKILL.md`、管理本地文件并执行 Python 的 Agent。纯聊天产品或禁止执行程序的环境无法仅靠粘贴指令运行本 Skill。按目标产品自身的 Skill 安装规范选择目录，不假定所有产品都使用 Codex 路径。

## 安装

1. 下载用户给出的版本 ZIP；有 SHA-256 时先核对。解压得到唯一顶层目录 `video-product-swap`，将整个目录放入目标产品认可的 Skills 目录，保证 `video-product-swap/SKILL.md` 可被发现。已有同名安装时先备份，保留其 `.env`，不要直接覆盖用户配置。
2. 检查 Python 3.10+。在此 Skill 目录创建独立虚拟环境：`python -m venv .venv`。之后用该环境的 Python 安装 `requirements-agent.txt`：`<python> -m pip install -r requirements-agent.txt`。Windows 的 `<python>` 为 `.venv/Scripts/python.exe`，macOS/Linux 为 `.venv/bin/python`，也可使用目标 Agent 已管理且依赖齐全的环境。
3. 视频换品需要 `ffmpeg`、`ffprobe` 都能从 PATH 执行。按操作系统使用可信的软件包来源安装 FFmpeg；然后执行 `ffmpeg -version` 和 `ffprobe -version` 验证。通用模型的具体媒体检查也可能需要 ffprobe。
4. 本地不存在 `.env` 时，从 `.env.example` 复制创建，并让用户在本地或目标产品的安全配置界面填写实际需要的 Key。不要把真实 Key 写入聊天、安装指令、共享链接或日志。不要读取或复用其他项目的 Key。
5. 在 Skill 根目录使用选定的 Python 执行 `scripts/generate_video.py --list-models`。无需 Key，无上传和生成费用。安装阶段不得发起正式视频生成或付费视觉分析。
6. 按目标产品规则重新加载 Skills；必要时重启会话。报告安装路径、使用的 Python、依赖检查结果及仍缺少的配置；未完成依赖配置时，不要声称已经能够生成视频。

分发包不包含桌面 EXE、GUI、用户素材或真实密钥。`scripts/prompt_engine.py` 是视频 Skill 的内部模块，源码安装与分发包使用同一结构，不需要第二个 Skill，也不在打包时改写说明文件。

## 服务与密钥

| 用途 | 服务 | 本地配置 |
| --- | --- | --- |
| 视频换品的视觉分析 | DeepSeek，固定 `deepseek-v4-flash-vision-exp` | `DEEPSEEK_API_BASE`、`DEEPSEEK_API_KEY` |
| 视频换品或通用 Wan3 | Kaiyuncode 中转，`wan3.0-video` | `KAIYUNCODE_API_BASE`、`KAIYUNCODE_API_KEY` |
| 通用 Wan 官方视频编辑 | 阿里云 DashScope，`wan2.7-videoedit` | `DASHSCOPE_API_BASE`、`DASHSCOPE_API_KEY` |

视频换品需要分别配置 DeepSeek 与 Kaiyuncode；仅使用通用模式时配置所选服务即可。不同服务的 Key 独立，不凭 Key 猜线路、不自动切换服务。接口可用性和额度取决于使用者账号，安装成功不代表服务账号已获授权。

独立调用时，全部模型配置只读本 Skill 根目录 `.env`，不读当前工作目录、其他 Skill 或系统环境里的 Key。桌面通过参数传入自身配置快照。若 Agent 宿主明确管理环境变量，可使用 `--credential-source host-app`，此时只使用进程提供的值，不读 `.env`。

从 1.0.0 升级：将原提示词 Skill 的 `DEEPSEEK_API_BASE` / `DEEPSEEK_API_KEY` 配置到视频 Skill 自己的 `.env`；不要覆盖已有不同配置。仅在旧系统环境中配置了视频 Key 的使用者，也需要明确选择迁入本 Skill `.env` 或使用 `host-app` 模式。旧版没有保存的任务 ID 无法凭空恢复。

恢复任务：`python scripts/generate_video.py --resume "/path/result.mp4.video-task.json"`。桌面可点击“继续已有任务”。恢复不会重新提交，服务地址必须与原任务一致。

## 安装后使用

切换到 Skill 根目录；下面的 `python` 均指安装时选定的 Python。路径替换为用户实际提供的文件路径，支持 Windows、macOS、Linux 的本地路径。

```text
python scripts/generate_video.py --mode product-swap --video "/path/source.mp4" --reference-image "/path/product.png" --volume-relation "参考图产品高度约为原视频产品的三分之二，宽度一致" --output "/path/result.mp4" --dry-run
```

预检查不分析画面、不上传、不提交视频。只有用户要求正式生成时，才去掉 `--dry-run`。使用用户本人提供的体积或尺寸关系，不能擅自套用示例中的比例。

通用模式示例：

```text
python scripts/generate_video.py --mode general --model wan3 --prompt "用户原始提示词" --reference "image=/path/product.png" --duration 5 --aspect-ratio 9:16 --resolution 480p --output "/path/result.mp4" --dry-run
```
