from pathlib import Path
import os
import re
import shutil
import sys
import uuid

from PyInstaller.utils.hooks import collect_all


project_root = Path(SPECPATH).parent
video_skill = project_root / "skills" / "video-product-swap"
packaging_root = project_root / "packaging"
sys.path.insert(0, str(packaging_root))
from director_resources import stage_director
director_bundle = stage_director(video_skill / "director-skill", project_root / ".build" / ("director-public-" + uuid.uuid4().hex))


def required_tool(name):
    override = os.environ.get(f"MATERIAL_UNIVERSE_{name.upper()}", "").strip()
    executable = override or shutil.which(name)
    if not executable:
        raise SystemExit(f"缺少 {name}，无法制作完整安装版。")
    return str(Path(executable).resolve())


customtkinter_datas, customtkinter_binaries, customtkinter_hidden = collect_all("customtkinter")
webview_datas, webview_binaries, webview_hidden = collect_all("webview")
canvas_dist = video_skill / "canvas" / "dist"
canvas_html = canvas_dist / "index.html"
canvas_assets = [canvas_html]
for asset in re.findall(r'(?:src|href)="/([^"]+)"', canvas_html.read_text(encoding="utf-8")):
    source = (canvas_dist / asset).resolve()
    if not source.is_file() or not source.is_relative_to(canvas_dist.resolve()):
        raise SystemExit(f"画布资源缺失或越界：{asset}")
    canvas_assets.append(source)
canvas_datas = [(str(path), f"skills/video-product-swap/canvas/dist/{path.relative_to(canvas_dist).parent.as_posix()}")
                for path in canvas_assets]

datas = [
    (str(video_skill / "references"), "skills/video-product-swap/references"),
    (str(director_bundle), "skills/video-product-swap/director-rules"),
    (str(video_skill / "update-helper.ps1"), "skills/video-product-swap"),
    (str(video_skill / "assets"), "skills/video-product-swap/assets"),
    (str(video_skill / "scripts" / "generate_video.py"), "skills/video-product-swap/scripts"),
    (str(video_skill / "canvas" / "THIRD_PARTY_NOTICES.txt"), "skills/video-product-swap/canvas"),
    (str(packaging_root / "THIRD_PARTY_NOTICES.txt"), "."),
] + canvas_datas + customtkinter_datas + webview_datas

binaries = [
    (required_tool("ffmpeg"), "ffmpeg"),
    (required_tool("ffprobe"), "ffmpeg"),
    (required_tool("ffplay"), "ffmpeg"),
] + customtkinter_binaries + webview_binaries

hiddenimports = [
    "video_gui",
    "generate_video",
    "prompt_engine",
    "product_swap",
    "desktop_tasks", "desktop_pages", "canvas_backend", "canvas_window", "canvas_demo",
    "fish_mcp", "image_audio_video", "image_audio_page", "voice_service", "voice_pair_service", "voice_batch", "voice_layout", "voice_page", "voice_acceptance",
    "app_version", "personal_library", "library_transfer", "app_updates", "maintenance", "maintenance_page",
    "director_prompt", "director_page", "feature_policy", "history_sync", "asset_grid", "redesign_acceptance",
    "webview", "desktop_tray", "pystray._win32",
] + customtkinter_hidden + webview_hidden

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
    optimize=0,
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
