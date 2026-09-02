from pathlib import Path
import os
import shutil

from PyInstaller.utils.hooks import collect_all


project_root = Path(SPECPATH).parent
video_skill = project_root / "skills" / "video-product-swap"
prompt_skill = project_root / "skills" / "ai-video-swap-prompt-generator"
packaging_root = project_root / "packaging"


def required_tool(name):
    override = os.environ.get(f"MATERIAL_UNIVERSE_{name.upper()}", "").strip()
    executable = override or shutil.which(name)
    if not executable:
        raise SystemExit(f"缺少 {name}，无法制作完整单文件版。")
    return str(Path(executable).resolve())


customtkinter_datas, customtkinter_binaries, customtkinter_hidden = collect_all("customtkinter")
dashscope_datas, dashscope_binaries, dashscope_hidden = collect_all("dashscope")

datas = [
    (str(video_skill / "references"), "skills/video-product-swap/references"),
    (str(video_skill / "assets"), "skills/video-product-swap/assets"),
    (str(video_skill / "scripts" / "generate_video.py"), "skills/video-product-swap/scripts"),
    (
        str(prompt_skill / "scripts" / "generate_swap_prompt.py"),
        "skills/ai-video-swap-prompt-generator/scripts",
    ),
] + customtkinter_datas + dashscope_datas

binaries = [
    (required_tool("ffmpeg"), "ffmpeg"),
    (required_tool("ffprobe"), "ffmpeg"),
    (required_tool("ffplay"), "ffmpeg"),
] + customtkinter_binaries + dashscope_binaries

hiddenimports = [
    "video_gui",
    "portable_runtime",
    "generate_video",
    "generate_swap_prompt",
] + customtkinter_hidden + dashscope_hidden

a = Analysis(
    [str(packaging_root / "material_universe_launcher.py")],
    pathex=[
        str(video_skill / "scripts"),
        str(prompt_skill / "scripts"),
    ],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=1,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="素材万象",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=[str(video_skill / "assets" / "material-universe.ico")],
    version=str(packaging_root / "version_info.txt"),
    uac_admin=False,
)
