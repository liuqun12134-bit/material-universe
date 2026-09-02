# 素材万象发行版

运行 `packaging/build_release.ps1` 后，这里会生成单文件 `素材万象.exe`。

这个 EXE 已包含 Python 运行环境、模型适配器、界面资源以及 FFmpeg、FFprobe、FFplay。发给其他 Windows 电脑时，只需要发送 `素材万象.exe`；使用者首次打开后在“设置”里填写自己的 API Key 即可。

API Key 使用 Windows 当前账户加密，并保存在使用者自己的 `%LOCALAPPDATA%\MaterialUniverse\credentials.dat`，不会写进 EXE。
