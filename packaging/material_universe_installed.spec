from pathlib import Path
import os
import shutil

from PyInstaller.utils.hooks import collect_all


project_root = Path(SPECPATH).parent
video_skill = project_root / "skills" / "video-product-swap"
packaging_root = project_root / "packaging"


def required_tool(name):
    override = os.environ.get(f"MATERIAL_UNIVERSE_{name.upper()}", "").strip()
    executable = override or shutil.which(name)
    if not executable:
        raise SystemExit(f"缺少 {name}，无法制作完整安装版。")
    return str(Path(executable).resolve())


customtkinter_datas, customtkinter_binaries, customtkinter_hidden = collect_all("customtkinter")
dashscope_datas, dashscope_binaries, dashscope_hidden = collect_all("dashscope")

datas = [
    (str(video_skill / "references"), "skills/video-product-swap/references"),
    (str(video_skill / "assets"), "skills/video-product-swap/assets"),
    (str(video_skill / "scripts" / "generate_video.py"), "skills/video-product-swap/scripts"),
] + customtkinter_datas + dashscope_datas

binaries = [
    (required_tool("ffmpeg"), "ffmpeg"),
    (required_tool("ffprobe"), "ffmpeg"),
    (required_tool("ffplay"), "ffmpeg"),
] + customtkinter_binaries + dashscope_binaries

hiddenimports = [
    "video_gui",
    "generate_video",
    "prompt_engine",
    "product_swap",
] + customtkinter_hidden + dashscope_hidden

a = Analysis(
    [str(packaging_root / "material_universe_launcher.py")],
    pathex=[
        str(video_skill / "scripts"),
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
    [],
    exclude_binaries=True,
    name="素材万象",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
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

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="素材万象",
)
