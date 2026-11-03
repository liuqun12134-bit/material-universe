from __future__ import annotations

import json
import copy
import math
import os
import queue
import ctypes
import shutil
import subprocess
import sys
import threading
import time
import uuid
from desktop_tasks import DesktopStore, TaskManager
from desktop_pages import DesktopPages
from image_audio_page import ImageAudioPage
from ai_image_page import AIImagePage
from voice_page import VoicePage
from desktop_tray import DesktopTray
from maintenance_page import MaintenancePage
from assistant_page import AssistantPage
from library_transfer import LibraryTransfer
from canvas_backend import CanvasService
from canvas_window import CanvasWindowManager
from ctypes import wintypes
from datetime import datetime
from pathlib import Path
from tkinter import Frame, END, BooleanVar, StringVar, TclError, Text, filedialog, messagebox
from urllib.parse import urlparse

import customtkinter as ctk
from PIL import Image

from gui_support import references_for_swap
from media_inspection import (
    VideoInfo, image_thumbnail, probe_audio_duration, probe_frame_rate, probe_video, video_poster,
)
from model_runner.runner import ModelRunner
from secure_credentials import CredentialStoreError, SecureCredentialStore, read_env_values, redact


def _configured_path(name: str, fallback: Path) -> Path:
    value = os.environ.get(name, "").strip()
    return Path(value).expanduser().resolve() if value else fallback.resolve()


SKILL_ROOT = _configured_path(
    "MATERIAL_UNIVERSE_VIDEO_SKILL_ROOT",
    Path(__file__).resolve().parents[1],
)
PROJECT_ROOT = _configured_path(
    "MATERIAL_UNIVERSE_WORKSPACE_ROOT",
    SKILL_ROOT.parents[1],
)
GENERATE_SCRIPT = SKILL_ROOT / "scripts" / "generate_video.py"
LOGO_PNG = SKILL_ROOT / "assets" / "material-universe-logo.png"
LOGO_ICO = SKILL_ROOT / "assets" / "material-universe.ico"
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
PROCESS_SUSPEND_RESUME = 0x0800
DEFAULT_PROMPT_BASE = "https://api.deepseek.com"
DEFAULT_PROMPT_MODEL = "deepseek-v4-flash-vision-exp"
DEFAULT_VIDEO_MODEL = "wan3.0-video"
SWAP_ROUTE_ADAPTERS = {"fal_wan3", "dashscope_wan3", "kaiyuncode_wan3"}  # 换品界面可选线路；其他模型走命令行通用模式
SWAP_VIDEO_PRICE_USD = {  # fal 按输出秒数计费（美元/秒）
    "fal_wan3": {"480p": 0.05, "720p": 0.10, "1080p": 0.20},
}
RETIRED_ENV_NAMES = (
    "GEMINI_RELAY_API_KEY",
    "GEMINI_API_KEY",
    "KAIYUNCODE_API_BASE",
    "KAIYUNCODE_API_KEY",
    "VIDEO_SWAP_API_BASE",
    "VIDEO_SWAP_API_KEY",
    "DASHSCOPE_API_BASE",
    "DASHSCOPE_API_KEY",
)
BG = "#F5F5F7"
CARD = "#FFFFFF"
TEXT = "#1D1D1F"
MUTED = "#6E6E73"
BORDER = "#E5E5EA"
BLUE = "#0071E3"
BLUE_HOVER = "#0077ED"
SOFT_BLUE = "#EAF4FF"
ORANGE = "#B25E00"


def build_isolated_child_env(
    parent_env: dict[str, str],
    app_values: dict[str, str],
    providers: dict[str, dict[str, object]],
) -> dict[str, str]:
    """Build a child environment that cannot fall back to either Skill's API keys."""
    env = dict(parent_env)
    env.update(app_values)

    # Only explicit host-app values can populate active provider credentials.
    active_names = {str(spec[field]) for spec in providers.values()
                    for field in ("api_base_env", "api_key_env") if spec.get(field)}
    for name in RETIRED_ENV_NAMES:
        env[name] = app_values.get(name, "") if name in active_names else ""
    for name in active_names:
        env[name] = app_values.get(name, "")
    for spec in providers.values():
        for field in ("legacy_api_base_env", "legacy_api_key_env"):
            name = str(spec.get(field, "")).strip()
            if name:
                env[name] = ""
    env["PYTHONUTF8"] = "1"
    return env


def set_process_suspended(process_id: int, suspended: bool) -> bool:
    """Pause headless audio only. Never suspend a process owning an embedded HWND."""
    if os.name != "nt":
        return False

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    ntdll = ctypes.WinDLL("ntdll")
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    action = ntdll.NtSuspendProcess if suspended else ntdll.NtResumeProcess
    action.argtypes = [wintypes.HANDLE]
    action.restype = wintypes.LONG

    handle = kernel32.OpenProcess(PROCESS_SUSPEND_RESUME, False, process_id)
    if not handle:
        return False
    try:
        return action(handle) == 0
    finally:
        kernel32.CloseHandle(handle)


def toggle_video_pause(window_title: str, hwnd: int = 0) -> bool:
    """Ask ffplay to pause decoding while its SDL window keeps processing messages.

    Suspending the process also suspends the embedded window's message pump.
    Windows then blocks the Tk thread in SetFocus when a file dialog opens.
    PostMessage is deliberately asynchronous; a faulty player cannot block Tk.
    """
    if os.name != "nt" or not window_title:
        return False
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
    user32.FindWindowW.restype = wintypes.HWND
    user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.PostMessageW.restype = wintypes.BOOL
    # FindWindow only finds top-level windows. Once embedded, use the saved HWND.
    hwnd = hwnd or user32.FindWindowW(None, window_title)
    if not hwnd:
        return False
    # ffplay handles SDL_KEYDOWN for 'p'. Include the scan code and release so
    # SDL does not consider later pause requests held-key repeats.
    down = bool(user32.PostMessageW(hwnd, 0x0100, ord("P"), 1 | (0x19 << 16)))
    if down:
        user32.PostMessageW(hwnd, 0x0101, ord("P"), 1 | (0x19 << 16) | (3 << 30))
    return down




class EmbeddedVideoPlayer:
    """Embed ffplay inside a Tk frame on Windows, with a safe external-window fallback."""

    def __init__(self, root: ctk.CTk, host: ctk.CTkFrame, poster: ctk.CTkLabel) -> None:
        self.root = root
        self.host = host
        self.poster = poster
        self.path: Path | None = None
        self.process: subprocess.Popen[bytes] | None = None
        self.hwnd = 0
        self.paused = False
        self.window_title = ""
        self.state_callback = lambda _playing, _paused: None
        self.host.bind("<Configure>", self._resize)

    def load(self, path: Path) -> None:
        self.stop()
        self.path = path

    def toggle(self) -> None:
        if self.process is None or self.process.poll() is not None:
            self.start()
            return
        paused = not self.paused
        if toggle_video_pause(self.window_title, self.hwnd):
            self.paused = paused
            self.state_callback(True, self.paused)

    def start(self) -> None:
        if self.path is None:
            return
        self.stop()
        ffplay = shutil.which("ffplay")
        if not ffplay:
            os.startfile(self.path)  # type: ignore[attr-defined]
            return
        self.window_title = f"素材万象预览-{uuid.uuid4().hex}"
        width = max(480, self.host.winfo_width())
        height = max(270, self.host.winfo_height())
        self.process = subprocess.Popen(
            [
                ffplay,
                "-loglevel",
                "error",
                "-window_title",
                self.window_title,
                "-noborder",
                "-autoexit",
                "-x",
                str(width),
                "-y",
                str(height),
                str(self.path),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=CREATE_NO_WINDOW,
        )
        self.paused = False
        self.state_callback(True, False)
        self.root.after(80, lambda: self._embed(30))
        self.root.after(300, self._watch)

    def replay(self) -> None:
        if self.path is not None:
            self.start()

    def open_system_player(self) -> None:
        if self.path is not None:
            os.startfile(self.path)  # type: ignore[attr-defined]

    def _embed(self, retries: int) -> None:
        if self.process is None or self.process.poll() is not None:
            return
        hwnd = ctypes.windll.user32.FindWindowW(None, self.window_title)
        if not hwnd:
            if retries > 0:
                self.root.after(80, lambda: self._embed(retries - 1))
            return
        self.hwnd = hwnd
        user32 = ctypes.windll.user32
        user32.SetParent(hwnd, self.host.winfo_id())
        style = user32.GetWindowLongW(hwnd, -16)
        style = (style & ~0x00CF0000) | 0x40000000
        user32.SetWindowLongW(hwnd, -16, style)
        self.poster.place_forget()
        self._resize()

    def _resize(self, _event=None) -> None:
        if self.hwnd:
            ctypes.windll.user32.MoveWindow(
                self.hwnd,
                0,
                0,
                max(1, self.host.winfo_width()),
                max(1, self.host.winfo_height()),
                True,
            )

    def _watch(self) -> None:
        if self.process is None:
            return
        if self.process.poll() is None:
            self.root.after(300, self._watch)
            return
        self.process = None
        self.hwnd = 0
        self.paused = False
        self.poster.place(relx=0.5, rely=0.5, anchor="center", relwidth=1, relheight=1)
        self.state_callback(False, False)

    def stop(self) -> None:
        process, self.process = self.process, None
        self.hwnd = 0
        self.paused = False
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
        self.poster.place(relx=0.5, rely=0.5, anchor="center", relwidth=1, relheight=1)
        self.state_callback(False, False)


class AudioPlayer:
    """Headless ffplay audio playback with pause/resume, mirroring EmbeddedVideoPlayer."""

    def __init__(self, root: ctk.CTk) -> None:
        self.root = root
        self.path: Path | None = None
        self.process: subprocess.Popen[bytes] | None = None
        self.paused = False
        self.state_callback = lambda _playing, _paused: None

    def load(self, path: Path) -> None:
        self.stop()
        self.path = path

    def toggle(self) -> None:
        if self.process is None or self.process.poll() is not None:
            self.start()
            return
        paused = not self.paused
        if set_process_suspended(self.process.pid, paused):
            self.paused = paused
            self.state_callback(True, self.paused)

    def start(self) -> None:
        if self.path is None:
            return
        self.stop()
        ffplay = shutil.which("ffplay")
        if not ffplay:
            os.startfile(self.path)  # type: ignore[attr-defined]
            return
        self.process = subprocess.Popen(
            [ffplay, "-loglevel", "error", "-nodisp", "-autoexit", str(self.path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=CREATE_NO_WINDOW,
        )
        self.paused = False
        self.state_callback(True, False)
        self.root.after(300, self._watch)

    def replay(self) -> None:
        if self.path is not None:
            self.start()

    def _watch(self) -> None:
        if self.process is None:
            return
        if self.process.poll() is None:
            self.root.after(300, self._watch)
            return
        self.process = None
        self.paused = False
        self.state_callback(False, False)

    def stop(self) -> None:
        process, self.process = self.process, None
        was_paused, self.paused = self.paused, False
        if process is not None and process.poll() is None:
            if was_paused:
                set_process_suspended(process.pid, False)
            process.terminate()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
        self.state_callback(False, False)


from director_page import DirectorPage


class MaterialUniverseApp(DesktopPages, ImageAudioPage, AIImagePage, DirectorPage, VoicePage, MaintenancePage, AssistantPage):
    def __init__(self, root: ctk.CTk, data_root=None, *, demo=False) -> None:
        self.root = root
        self.demo = demo
        self.root.title("素材万象")
        self.root.geometry("1220x860")
        self.root.minsize(1080, 740)
        self.root.configure(fg_color=BG)
        self.root.option_add("*Font", ("Segoe UI", 10))
        self._configure_window_icon()

        self.runner = ModelRunner(SKILL_ROOT, values={})
        self.models = self.runner.list_models()
        self.model_map = {item["model"]: item for item in self.models}
        self.provider_names = {
            provider: str(spec.get("display_name", provider))
            for provider, spec in self.runner.registry.providers.items()
        }
        self.provider_keys = {display: key for key, display in self.provider_names.items()
                              if any(item["provider"] == key and item["adapter"] in SWAP_ROUTE_ADAPTERS for item in self.models)}
        self.route_labels = {
            item["model"]: f'{self.provider_names.get(item["provider"], item["provider"])} · {item.get("label", item["model"])}'
            for item in self.models
            if item["adapter"] in SWAP_ROUTE_ADAPTERS
        }
        self.store = SecureCredentialStore()
        try:
            self.saved_credentials = {} if demo else self.store.load()
            store_error = ""
        except CredentialStoreError as exc:
            self.saved_credentials = {}
            store_error = str(exc)

        preferred = self.saved_credentials.get("VIDEO_MODEL", DEFAULT_VIDEO_MODEL)
        if preferred not in self.route_labels or preferred not in self.model_map:
            preferred = DEFAULT_VIDEO_MODEL
        if preferred not in self.route_labels and self.route_labels:
            preferred = next(iter(self.route_labels))

        self.events: queue.Queue[tuple[str, object]] = queue.Queue()
        self.tray = None
        self._tray_hide_pending = False
        self._exit_in_progress = False
        self._closed = False
        data_root = data_root or Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "MaterialUniverse" / "任务与素材"
        self.desktop_store = DesktopStore(data_root)
        try:
            concurrency = json.loads((Path(data_root) / "settings.json").read_text(encoding="utf-8")).get("concurrency", 3)
        except (OSError, ValueError):
            concurrency = 3
        if demo:
            from canvas_demo import mock_execute
            self.tasks = TaskManager(self.desktop_store, execute=mock_execute, limit=concurrency,
                                     changed=lambda: self.events.put(("tasks_changed", None)))
        else:
            self.tasks = TaskManager(self.desktop_store, limit=concurrency,
                                     changed=lambda: self.events.put(("tasks_changed", None)))
        self.library_transfer = LibraryTransfer(self.desktop_store, Path(data_root) / "Skill附属库" if demo else None)
        self.library_transfer.recover()
        migration_note = ""
        if not demo:
            # Older installed resources stay at this path; new releases use director-rules.
            try:
                self.library_transfer.migrate_legacy([SKILL_ROOT / "director-skill"])
            except (OSError, ValueError) as exc:
                migration_note = f"旧 Skill 库未完成迁移，原文件已保留，请在数据与迁移中处理后再次导入：{exc}"
        from app_updates import verify_after_update
        self._library_startup_note = migration_note or verify_after_update(self.library_transfer) or "素材、个人案例与经验独立保存，更新程序会保留。"
        self._init_assistants()
        self.extra_reference_images = []
        self.prompt_mode_var = StringVar(value="重新分析视频")
        self.remove_subtitles_var = BooleanVar(value=False)
        self.enable_prompt_expansion_var = BooleanVar(value=True)
        self.enable_thinking_var = BooleanVar(value=True)
        self.running = False
        self._submission_drafts = {mode: self._new_submission_draft()
                                   for mode in ("full", "lipsync", "repair")}
        self._submission_status = {mode: StringVar(value="")
                                   for mode in self._submission_drafts}
        self._submission_buttons = {}
        self._submission_progress = {}
        self.generated_context: tuple[str, str, str] | None = None
        self.last_output: Path | None = None
        self.last_video_url: str | None = None
        self.video_info: VideoInfo | None = None
        self.video_preview_ctk_image: ctk.CTkImage | None = None
        self.product_preview_ctk_image: ctk.CTkImage | None = None
        self.lipsync_poster_image: ctk.CTkImage | None = None
        self.repair_poster_image: ctk.CTkImage | None = None
        self.brand_logo_image: ctk.CTkImage | None = None
        self.key_entries: list[ctk.CTkEntry] = []
        self.provider_vars: dict[str, dict[str, object]] = {}
        self.current_page: str | None = None
        self._source_load_token = ""
        self._reference_load_token = ""
        self._lipsync_video_token = ""
        self._lipsync_audio_token = ""
        self._lipsync_video_seconds: float | None = None
        self._lipsync_audio_seconds: float | None = None
        self._repair_video_token = ""
        self._repair_video_frames: int | None = None
        self._repair_video_info: VideoInfo | None = None

        self.model_var = StringVar(value=preferred)
        self.video_route_var = StringVar(value=self.route_labels[preferred])
        self.provider_var = StringVar(
            value=self.provider_names.get(self.model_map[preferred]["provider"], "")
        )
        self.source_video_var = StringVar()
        self.reference_image_var = StringVar()
        self.aspect_ratio_var = StringVar(value="9:16")
        self.duration_var = StringVar(value="")
        self.resolution_var = StringVar(value="480p")
        self.duration_var.trace_add("write", lambda *_args: self._update_swap_cost())
        self.resolution_var.trace_add("write", lambda *_args: self._update_swap_cost())
        self.source_summary_var = StringVar(value="尚未选择原视频")
        self.image_summary_var = StringVar(value="尚未选择产品参考图")
        self.play_button_var = StringVar(value="播放")
        self.workspace_output_dir_var = StringVar(
            value=self.saved_credentials.get(
                "WORKSPACE_OUTPUT_DIR", str((PROJECT_ROOT / "output").resolve())
            )
        )
        self.output_var = StringVar(value=str(self._new_default_output()))
        self.generated_prompt = ""
        self.model_help_var = StringVar()
        self.status_var = StringVar(value="就绪")
        self.api_status_var = StringVar(value=store_error or "API Key 安全存储就绪")
        self.show_keys_var = BooleanVar(value=False)
        self.lipsync_video_var = StringVar()
        self.lipsync_audio_var = StringVar()
        self.lipsync_video_info_var = StringVar(value="尚未选择视频")
        self.lipsync_audio_info_var = StringVar(value="尚未选择音频")
        self.lipsync_cost_var = StringVar(value="选好视频和音频后，这里显示输出时长和预估费用。")
        self.lipsync_video_play_var = StringVar(value="播放")
        self.lipsync_audio_play_var = StringVar(value="播放")
        self.repair_video_var = StringVar()
        self.repair_video_info_var = StringVar(value="尚未选择视频")
        self.repair_cost_var = StringVar(value="选好视频后，这里显示输出规格和预估费用。")
        self.repair_play_var = StringVar(value="播放")
        self.repair_factor_var = StringVar(value="1.5")
        self.repair_accel_var = StringVar(value="regular 最佳质量")
        self.repair_color_fix_var = BooleanVar(value=True)
        self.repair_audio_var = BooleanVar(value=True)
        self.swap_cost_var = StringVar(value="选择原视频后，这里显示一键换品的预估费用。")
        self.prompt_mode_var.trace_add("write", lambda *_: (self._update_swap_cost(), self._update_reuse_prompt_visibility()))

        self.prompt_base_var = StringVar(
            value=self.saved_credentials.get("DEEPSEEK_API_BASE", DEFAULT_PROMPT_BASE)
        )
        self.prompt_model_var = StringVar(
            value=self.saved_credentials.get("DEEPSEEK_PROMPT_MODEL", DEFAULT_PROMPT_MODEL)
        )
        self.prompt_key_var = StringVar(
            value=self.saved_credentials.get("DEEPSEEK_API_KEY", "")
        )

        self._init_image_audio(demo=demo)
        self._init_ai_image()
        self._init_voice()
        self._configure_style()
        self._build_ui()
        self._update_model_help()
        self._update_api_status()
        self._canvas_credentials_cache = self._credential_values()
        self._bind_canvas_credentials()
        self.canvas_service = CanvasService(
            self.desktop_store, self.tasks, SKILL_ROOT / "canvas" / "dist", self._canvas_credentials,
            models=self.models, demo=demo)
        self.canvas_service.host_action = lambda action: self.events.put(("canvas_host_action", action))
        self.canvas_window = CanvasWindowManager(root, self.canvas_service,
                                                 on_closed=self._end_demo if demo else None)
        self._fullscreen = False
        self.root.bind("<F11>", lambda _event: (self._toggle_fullscreen(), "break")[1])
        self.root.bind("<Escape>", lambda _event: self._leave_fullscreen())
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(100, self._drain_events)
        self.root.after(700, self._restore_history)
        self.root.after(250, self._ensure_tray)

    def _configure_style(self) -> None:
        ctk.set_appearance_mode("light")
        ctk.set_default_color_theme("blue")

    def _configure_window_icon(self) -> None:
        if not LOGO_ICO.is_file():
            return
        try:
            self.root.iconbitmap(str(LOGO_ICO))
        except (OSError, TclError):
            pass

    def _create_brand_mark(self, parent: ctk.CTkFrame) -> ctk.CTkLabel:
        if LOGO_PNG.is_file():
            try:
                with Image.open(LOGO_PNG) as source:
                    logo = source.convert("RGBA").copy()
                self.brand_logo_image = ctk.CTkImage(
                    light_image=logo,
                    dark_image=logo,
                    size=(42, 42),
                )
                return ctk.CTkLabel(
                    parent,
                    text="",
                    image=self.brand_logo_image,
                    width=42,
                    height=42,
                )
            except (OSError, ValueError):
                self.brand_logo_image = None

        return ctk.CTkLabel(
            parent,
            text="万",
            width=38,
            height=38,
            corner_radius=12,
            fg_color=TEXT,
            text_color="white",
            font=("Segoe UI", 17, "bold"),
        )

    def _build_ui(self) -> None:
        shell = ctk.CTkFrame(self.root, fg_color=BG, corner_radius=0)
        shell.pack(fill="both", expand=True)
        shell.grid_rowconfigure(0, weight=1)
        shell.grid_columnconfigure(1, weight=1)

        sidebar = ctk.CTkFrame(
            shell, width=226, fg_color=CARD, corner_radius=0, border_width=0
        )
        sidebar.grid(row=0, column=0, sticky="nsew")
        sidebar.grid_propagate(False)

        brand = ctk.CTkFrame(sidebar, fg_color="transparent")
        brand.pack(fill="x", padx=24, pady=(28, 34))
        mark = self._create_brand_mark(brand)
        mark.pack(side="left")
        ctk.CTkLabel(
            brand,
            text="素材万象",
            text_color=TEXT,
            font=("Segoe UI", 17, "bold"),
        ).pack(side="left", padx=(11, 0))

        ctk.CTkLabel(
            sidebar,
            text="功能模块",
            text_color=MUTED,
            font=("Segoe UI", 10, "bold"),
            anchor="w",
        ).pack(fill="x", padx=27, pady=(0, 6))
        self.image_audio_nav = self._nav_button(sidebar, "图音生视频", lambda: self._show_page("image_audio"))
        self.image_audio_nav.pack(fill="x", padx=14, pady=3)
        self.ai_image_nav = self._nav_button(sidebar, "AI 生图", lambda: self._show_page("ai_image"))
        self.ai_image_nav.pack(fill="x", padx=14, pady=3)
        self.voice_nav = self._nav_button(sidebar, "声音克隆", lambda: self._show_page("voice"))
        self.voice_nav.pack(fill="x", padx=14, pady=3)
        self.canvas_nav = self._nav_button(sidebar, "创作画布", self._open_canvas)
        self.canvas_nav.pack(fill="x", padx=14, pady=3)
        self.history_nav = self._nav_button(sidebar, "使用记录", lambda: self._show_page("history"))
        self.history_nav.pack(fill="x", padx=14, pady=3)
        self.library_nav = self._nav_button(sidebar, "素材库", self._browse_library)
        self.library_nav.pack(fill="x", padx=14, pady=3)
        ctk.CTkLabel(
            sidebar,
            text="系统",
            text_color=MUTED,
            font=("Segoe UI", 10, "bold"),
            anchor="w",
        ).pack(fill="x", padx=27, pady=(22, 6))
        self.api_nav = self._nav_button(
            sidebar, "设置", lambda: self._show_page("api")
        )
        self.api_nav.pack(fill="x", padx=14, pady=3)

        ctk.CTkLabel(
            sidebar,
            text="关闭窗口后在系统托盘运行\n右键托盘图标可退出",
            justify="left",
            anchor="w",
            text_color=MUTED,
            font=("Segoe UI", 11),
        ).pack(side="bottom", fill="x", padx=26, pady=24)

        content = ctk.CTkFrame(shell, fg_color=BG, corner_radius=0)
        content.grid(row=0, column=1, sticky="nsew")
        content.grid_rowconfigure(0, weight=1)
        content.grid_columnconfigure(0, weight=1)
        self.edit_tab = ctk.CTkFrame(content, fg_color=BG, corner_radius=0)
        self.image_audio_tab = ctk.CTkFrame(content, fg_color=BG, corner_radius=0)
        self.ai_image_tab = ctk.CTkFrame(content, fg_color=BG, corner_radius=0)
        self.voice_tab = ctk.CTkFrame(content, fg_color=BG, corner_radius=0)
        self.lipsync_tab = ctk.CTkFrame(content, fg_color=BG, corner_radius=0)
        self.repair_tab = ctk.CTkFrame(content, fg_color=BG, corner_radius=0)
        self.api_tab = ctk.CTkFrame(content, fg_color=BG, corner_radius=0)
        self.history_tab = ctk.CTkFrame(content, fg_color=BG, corner_radius=0)
        self.library_tab = ctk.CTkFrame(content, fg_color=BG, corner_radius=0)
        self.canvas_tab = ctk.CTkFrame(content, fg_color="#191a1f", corner_radius=0)
        self.canvas_host = Frame(self.canvas_tab, background="#191a1f", borderwidth=0, highlightthickness=0)
        self.canvas_host.pack(fill="both", expand=True)
        self.canvas_tab.grid(row=0, column=0, sticky="nsew")
        self.history_tab.grid(row=0, column=0, sticky="nsew")
        self.library_tab.grid(row=0, column=0, sticky="nsew")
        self.edit_tab.grid(row=0, column=0, sticky="nsew")
        self.image_audio_tab.grid(row=0, column=0, sticky="nsew")
        self.ai_image_tab.grid(row=0, column=0, sticky="nsew")
        self.voice_tab.grid(row=0, column=0, sticky="nsew")
        self.lipsync_tab.grid(row=0, column=0, sticky="nsew")
        self.repair_tab.grid(row=0, column=0, sticky="nsew")
        self.api_tab.grid(row=0, column=0, sticky="nsew")
        self._build_image_audio_page()
        self._build_ai_image_page()
        self._build_voice_page()
        self._build_api_tab()
        self._build_task_pages()
        self._show_page("image_audio")
        self.root.after(150, self._scroll_pages_to_top)

    def _scroll_pages_to_top(self) -> None:
        for page in (self.image_audio_page, self.api_page):
            page._parent_canvas.yview_moveto(0)  # customtkinter scroll container

    def _nav_button(self, parent: ctk.CTkFrame, text: str, command) -> ctk.CTkButton:
        return ctk.CTkButton(
            parent,
            text=text,
            command=command,
            height=46,
            corner_radius=12,
            anchor="w",
            font=("Segoe UI", 14, "bold"),
            fg_color="transparent",
            hover_color="#F0F0F2",
            text_color=TEXT,
            border_width=0,
        )

    def _show_page(self, page: str) -> None:
        if page == self.current_page:
            return
        pages = {
            "image_audio": (self.image_audio_nav, self.image_audio_tab),
            "ai_image": (self.ai_image_nav, self.ai_image_tab),
            "voice": (self.voice_nav, self.voice_tab),
            "api": (self.api_nav, self.api_tab),
            "history": (self.history_nav, self.history_tab),
            "library": (self.library_nav, self.library_tab),
            "canvas": (self.canvas_nav, self.canvas_tab),
        }
        if self.current_page == "canvas" and hasattr(self, "canvas_window"):
            self.canvas_window.hide()
        if page not in pages:
            return
        if self.current_page == "ai_image":
            self._ai_save()
        active, visible_tab = pages[page]
        for name, (nav, tab) in pages.items():
            if name == page:
                continue
            nav.configure(fg_color="transparent", text_color=TEXT)
            if name != "canvas":
                tab.grid_remove()
            else:
                # Keep the native HWND host mapped underneath the active Tk page.
                # Only its renderer hides; remapping a cross-process host loses visibility.
                tab.lower()
        active.configure(fg_color=SOFT_BLUE, text_color=BLUE)
        visible_tab.grid(row=0, column=0, sticky="nsew")
        self.root.update_idletasks()  # Map the Tk host before raising/showing native children.
        visible_tab.tkraise()
        self.current_page = page
        if page == "history": self._refresh_history()
        if page == "library": self._refresh_library()
        if page == "voice": self._voice_on_show()
        if page == "ai_image": self._ai_on_show()
        if page in {"image_audio", "ai_image", "api"} and not self.fish_workspaces and not self.demo:
            from fish_mcp import FishAuth
            try:
                if FishAuth(self.fish_auth_path).connected():
                    self._fish_refresh_account()
            except CredentialStoreError as exc:
                self.fish_account_status.set(str(exc))
        if page == "canvas":
            self.root.update_idletasks()
            self.canvas_window.open(self.canvas_host)

    def _card(self, parent, title: str, subtitle: str = "") -> tuple[ctk.CTkFrame, ctk.CTkFrame]:
        card = ctk.CTkFrame(
            parent,
            fg_color=CARD,
            corner_radius=18,
            border_width=1,
            border_color=BORDER,
        )
        card.pack(fill="x", pady=(0, 14))
        header = ctk.CTkFrame(card, fg_color="transparent")
        header.pack(fill="x", padx=22, pady=(18, 12 if subtitle else 16))
        ctk.CTkLabel(
            header,
            text=title,
            text_color=TEXT,
            font=("Segoe UI", 16, "bold"),
            anchor="w",
        ).pack(anchor="w")
        if subtitle:
            ctk.CTkLabel(
                header,
                text=subtitle,
                text_color=MUTED,
                font=("Segoe UI", 11),
                anchor="w",
                justify="left",
            ).pack(anchor="w", pady=(4, 0))
        body = ctk.CTkFrame(card, fg_color="transparent")
        body.pack(fill="x", padx=22, pady=(0, 20))
        return card, body

    @staticmethod
    def _field_label(parent, text: str) -> ctk.CTkLabel:
        return ctk.CTkLabel(
            parent, text=text, text_color=MUTED, font=("Segoe UI", 11, "bold"), anchor="w"
        )

    @staticmethod
    def _entry(parent, variable: StringVar, secret: bool = False) -> ctk.CTkEntry:
        return ctk.CTkEntry(
            parent,
            textvariable=variable,
            height=40,
            corner_radius=10,
            border_width=1,
            border_color=BORDER,
            fg_color="#FBFBFD",
            text_color=TEXT,
            show="●" if secret else "",
        )

    @staticmethod
    def _plain_text_box(parent, *, height: int, read_only: bool = False) -> Text:
        border = ctk.CTkFrame(
            parent,
            fg_color=BORDER,
            corner_radius=12,
            border_width=0,
        )
        border.pack(fill="x")
        text = Text(
            border,
            height=height,
            wrap="word",
            undo=not read_only,
            relief="flat",
            borderwidth=0,
            highlightthickness=0,
            background="#FBFBFD",
            foreground=TEXT if not read_only else MUTED,
            insertbackground=TEXT,
            selectbackground="#B7D9FF",
            selectforeground=TEXT,
            font=("Segoe UI", 12 if not read_only else 10),
            padx=11,
            pady=9,
        )
        text.pack(fill="x", padx=1, pady=1)
        if read_only:
            text.configure(state="disabled")
        return text


    def _file_row(self, parent, row, label, variable, button_text, command):
        self._field_label(parent, label).grid(row=row * 2, column=0, sticky="w", pady=(0 if row == 0 else 14, 0))
        entry = self._entry(parent, variable)
        entry.grid(row=row * 2 + 1, column=0, sticky="ew", padx=(0, 10), pady=(6, 0))
        button = self._secondary_button(parent, button_text, command, width=104)
        button.grid(row=row * 2 + 1, column=1, pady=(6, 0))
        return entry, button

    @staticmethod
    def _secondary_button(parent, text, command, width=126) -> ctk.CTkButton:
        return ctk.CTkButton(
            parent,
            text=text,
            command=command,
            width=width,
            height=40,
            corner_radius=20,
            fg_color="#F0F0F2",
            hover_color="#E5E5EA",
            text_color=TEXT,
            font=("Segoe UI", 12, "bold"),
        )


    def _build_edit_tab(self) -> None:
        page = self.editor_page = ctk.CTkScrollableFrame(
            self.edit_tab, fg_color=BG, corner_radius=0, scrollbar_button_color="#C7C7CC"
        )
        page.pack(fill="both", expand=True, padx=34, pady=24)
        ctk.CTkLabel(
            page,
            text="AI 视频换品",
            text_color=TEXT,
            font=("Segoe UI", 28, "bold"),
            anchor="w",
        ).pack(anchor="w")
        ctk.CTkLabel(
            page,
            text="选择素材并描述尺寸关系，确认输出规格后即可一键换品。",
            text_color=MUTED,
            font=("Segoe UI", 13),
            anchor="w",
        ).pack(anchor="w", pady=(4, 22))

        _, media = self._card(page, "1  素材", "选择视频后自动识别时长和比例。")
        media.grid_columnconfigure((0, 1), weight=1, uniform="preview")
        self._build_video_preview(media)
        self._build_image_preview(media)

        _, relation = self._card(
            page,
            "2  尺寸关系",
            "描述参考图产品与原视频产品的体积或外形关系；系统不会自行估算。",
        )
        self.relation_text = self._plain_text_box(relation, height=4)

        _, settings = self._card(
            page,
            "3  输出设置",
            "先选服务商，再选该服务商下的视频线路；秒数和比例默认跟随原视频。",
        )
        fixed_model = ctk.CTkFrame(settings, fg_color=SOFT_BLUE, corner_radius=12)
        fixed_model.pack(fill="x", pady=(0, 14))
        ctk.CTkLabel(
            fixed_model,
            text="服务商",
            text_color=MUTED,
            font=("Segoe UI", 10, "bold"),
        ).pack(side="left", padx=(14, 8), pady=10)
        self.provider_box = ctk.CTkOptionMenu(
            fixed_model,
            variable=self.provider_var,
            values=list(self.provider_keys.keys()),
            command=self._select_provider,
            height=36,
            corner_radius=10,
            fg_color=BLUE,
            button_color=BLUE_HOVER,
            button_hover_color=BLUE_HOVER,
            text_color="white",
            width=110,
        )
        self.provider_box.pack(side="left", padx=(0, 10), pady=8)
        ctk.CTkLabel(
            fixed_model,
            text="视频线路",
            text_color=MUTED,
            font=("Segoe UI", 10, "bold"),
        ).pack(side="left", padx=(4, 8), pady=10)
        self.model_box = ctk.CTkOptionMenu(
            fixed_model,
            variable=self.video_route_var,
            values=[label for model, label in self.route_labels.items()
                    if self.model_map[model]["provider"] == self.model_map[self.model_var.get()]["provider"]],
            command=self._select_video_route,
            height=36,
            corner_radius=10,
            fg_color=BLUE,
            button_color=BLUE_HOVER,
            button_hover_color=BLUE_HOVER,
            text_color="white",
        )
        self.model_box.pack(side="left", fill="x", expand=True, padx=(0, 10), pady=8)
        ctk.CTkLabel(
            fixed_model,
            textvariable=self.model_help_var,
            text_color=MUTED,
            font=("Segoe UI", 10),
        ).pack(side="left", padx=(0, 14), pady=10)
        settings_row = ctk.CTkFrame(settings, fg_color="transparent")
        settings_row.pack(fill="x")
        settings_row.grid_columnconfigure((0, 1, 2), weight=1, uniform="spec")
        for column, label in enumerate(("画面比例", "视频秒数", "分辨率")):
            self._field_label(settings_row, label).grid(
                row=0, column=column, sticky="w", padx=(0, 12)
            )
        self.aspect_box = ctk.CTkOptionMenu(
            settings_row,
            variable=self.aspect_ratio_var,
            values=["9:16", "16:9", "1:1", "4:3", "3:4"],
            height=42,
            corner_radius=11,
            fg_color="#F0F0F2",
            button_color="#E5E5EA",
            button_hover_color="#D1D1D6",
            text_color=TEXT,
        )
        self.aspect_box.grid(row=1, column=0, sticky="ew", padx=(0, 12), pady=(6, 0))
        self.duration_spin = self._entry(settings_row, self.duration_var)
        self.duration_spin.grid(row=1, column=1, sticky="ew", padx=(0, 12), pady=(6, 0))
        self.resolution_box = ctk.CTkOptionMenu(
            settings_row,
            variable=self.resolution_var,
            values=["480p", "720p", "1080p"],
            height=42,
            corner_radius=11,
            fg_color="#F0F0F2",
            button_color="#E5E5EA",
            button_hover_color="#D1D1D6",
            text_color=TEXT,
        )
        self.resolution_box.grid(row=1, column=2, sticky="ew", pady=(6, 0))

        _, swap = self._card(
            page,
            "4  开始换品",
            "确认素材和参数后开始生成，可在侧栏使用记录中查看任务。",
        )
        cost = ctk.CTkFrame(swap, fg_color=SOFT_BLUE, corner_radius=12)
        cost.pack(fill="x", pady=(0, 14))
        ctk.CTkOptionMenu(swap, variable=self.prompt_mode_var,
                          values=["重新分析视频", "使用已有提示词"]).pack(fill="x", pady=(0, 8))
        self.reuse_prompt_label = ctk.CTkLabel(swap, text="已有提示词（换视频后请检查位置描述）", text_color=MUTED)
        self.reuse_prompt_box = ctk.CTkTextbox(swap, height=100, wrap="word")
        self._update_reuse_prompt_visibility()
        self.remove_subtitles_checkbox = ctk.CTkCheckBox(swap, text="去字幕模式（先去掉参考视频中的字幕）",
                        variable=self.remove_subtitles_var)
        self.remove_subtitles_checkbox.pack(anchor="w", pady=(0, 12))
        ctk.CTkCheckBox(swap, text="提示词扩写（优化生成提示词）",
                        variable=self.enable_prompt_expansion_var).pack(anchor="w", pady=(0, 12))
        self.thinking_checkbox = ctk.CTkCheckBox(swap, text="思考模式（生成前增强推理）",
                                               variable=self.enable_thinking_var)
        self.thinking_checkbox.pack(anchor="w", pady=(0, 12))
        ctk.CTkLabel(
            cost, textvariable=self.swap_cost_var,
            text_color=BLUE, font=("Segoe UI", 12, "bold"), anchor="w",
        ).pack(fill="x", padx=16, pady=12)
        self.full_button = ctk.CTkButton(
            swap,
            text="一键开始换品",
            command=self._start_full_pipeline,
            height=50,
            corner_radius=25,
            fg_color=BLUE,
            hover_color=BLUE_HOVER,
            text_color="white",
            font=("Segoe UI", 14, "bold"),
        )
        self.full_button.pack(fill="x")
        self._build_submission_feedback(swap, "full", self.full_button)

    # ---- AI 换口型 -----------------------------------------------------------

    def _build_lipsync_tab(self) -> None:
        page = self.lipsync_page = ctk.CTkScrollableFrame(
            self.lipsync_tab, fg_color=BG, corner_radius=0, scrollbar_button_color="#C7C7CC"
        )
        page.pack(fill="both", expand=True, padx=34, pady=24)
        ctk.CTkLabel(
            page, text="AI 换口型", text_color=TEXT,
            font=("Segoe UI", 28, "bold"), anchor="w",
        ).pack(anchor="w")
        ctk.CTkLabel(
            page,
            text="一段人物视频 + 一段配音音频，自动生成口型对齐的新视频（fal Sync3）。",
            text_color=MUTED, font=("Segoe UI", 13), anchor="w",
        ).pack(anchor="w", pady=(4, 22))

        _, media_card = self._card(
            page, "1  素材", "视频：MP4 / MOV / WebM，≤10 分钟，480p-4K，人物正脸；音频：MP3 / WAV / M4A / AAC，≤10 分钟。"
        )
        media_card.grid_columnconfigure((0, 1), weight=1, uniform="lipsync_media")

        video_panel = ctk.CTkFrame(media_card, fg_color="#F7F7F9", corner_radius=14)
        video_panel.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        ctk.CTkLabel(
            video_panel, text="视频", text_color=TEXT, font=("Segoe UI", 13, "bold")
        ).pack(anchor="w", padx=14, pady=(13, 8))
        self.lipsync_video_stage = ctk.CTkFrame(video_panel, height=220, fg_color="#111111", corner_radius=12)
        self.lipsync_video_stage.pack(fill="x", padx=14)
        self.lipsync_video_stage.pack_propagate(False)
        self.lipsync_video_poster = ctk.CTkLabel(
            self.lipsync_video_stage,
            text="选择视频后可在这里播放",
            text_color="#A1A1A6",
            fg_color="#111111",
            corner_radius=12,
            font=("Segoe UI", 12),
        )
        self.lipsync_video_poster.place(
            relx=0.5, rely=0.5, anchor="center", relwidth=1, relheight=1
        )
        self.lipsync_video_player = EmbeddedVideoPlayer(
            self.root, self.lipsync_video_stage, self.lipsync_video_poster
        )
        self.lipsync_video_player.state_callback = self._lipsync_video_player_state
        video_controls = ctk.CTkFrame(video_panel, fg_color="transparent")
        video_controls.pack(fill="x", padx=14, pady=(10, 4))
        self.lipsync_video_button = self._secondary_button(
            video_controls, "选择视频", self._choose_lipsync_video, width=100
        )
        self.lipsync_video_button.pack(side="left")
        self._secondary_button(video_panel, "从素材库选择", lambda: self._open_library("换口型视频"), width=130).pack(anchor="w", padx=14, pady=4)
        self.lipsync_play_button = self._secondary_button(
            video_controls, "播放", self.lipsync_video_player.toggle, width=78
        )
        self.lipsync_play_button.configure(textvariable=self.lipsync_video_play_var, state="disabled")
        self.lipsync_play_button.pack(side="left", padx=6)
        self.lipsync_replay_button = self._secondary_button(
            video_controls, "重播", self.lipsync_video_player.replay, width=70
        )
        self.lipsync_replay_button.configure(state="disabled")
        self.lipsync_replay_button.pack(side="left")
        ctk.CTkLabel(
            video_panel, textvariable=self.lipsync_video_info_var,
            text_color=MUTED, font=("Segoe UI", 11), anchor="w",
        ).pack(fill="x", padx=15, pady=(5, 14))

        audio_panel = ctk.CTkFrame(media_card, fg_color="#F7F7F9", corner_radius=14)
        audio_panel.grid(row=0, column=1, sticky="nsew", padx=(8, 0))
        ctk.CTkLabel(
            audio_panel, text="音频", text_color=TEXT, font=("Segoe UI", 13, "bold")
        ).pack(anchor="w", padx=14, pady=(13, 8))
        audio_stage = ctk.CTkFrame(audio_panel, height=220, fg_color="#EFEFF4", corner_radius=12)
        audio_stage.pack(fill="x", padx=14)
        audio_stage.pack_propagate(False)
        ctk.CTkLabel(
            audio_stage,
            text="选择音频后可在这里试听\n（单人清晰语音效果最好）",
            text_color=MUTED, fg_color="#EFEFF4", corner_radius=12,
            font=("Segoe UI", 12), justify="center",
        ).pack(fill="both", expand=True)
        self.lipsync_audio_player = AudioPlayer(self.root)
        self.lipsync_audio_player.state_callback = self._lipsync_audio_player_state
        audio_controls = ctk.CTkFrame(audio_panel, fg_color="transparent")
        audio_controls.pack(fill="x", padx=14, pady=(10, 4))
        self.lipsync_audio_button = self._secondary_button(
            audio_controls, "选择音频", self._choose_lipsync_audio, width=100
        )
        self.lipsync_audio_button.pack(side="left")
        self._secondary_button(audio_panel, "从素材库选择", lambda: self._open_library("换口型音频"), width=130).pack(anchor="w", padx=14, pady=4)
        self.lipsync_audio_play_button = self._secondary_button(
            audio_controls, "播放", self.lipsync_audio_player.toggle, width=78
        )
        self.lipsync_audio_play_button.configure(textvariable=self.lipsync_audio_play_var, state="disabled")
        self.lipsync_audio_play_button.pack(side="left", padx=6)
        self.lipsync_audio_replay_button = self._secondary_button(
            audio_controls, "重播", self.lipsync_audio_player.replay, width=70
        )
        self.lipsync_audio_replay_button.configure(state="disabled")
        self.lipsync_audio_replay_button.pack(side="left")
        ctk.CTkLabel(
            audio_panel, textvariable=self.lipsync_audio_info_var,
            text_color=MUTED, font=("Segoe UI", 11), anchor="w",
        ).pack(fill="x", padx=15, pady=(5, 14))

        _, submit_card = self._card(
            page, "2  开始换口型", "输出时长取视频和音频中较短者；按输出时长计费（约 $8/分钟）。"
        )
        cost = ctk.CTkFrame(submit_card, fg_color=SOFT_BLUE, corner_radius=12)
        cost.pack(fill="x", pady=(0, 14))
        ctk.CTkLabel(
            cost, textvariable=self.lipsync_cost_var,
            text_color=BLUE, font=("Segoe UI", 12, "bold"), anchor="w",
        ).pack(fill="x", padx=16, pady=12)
        self.lipsync_submit_button = ctk.CTkButton(
            submit_card, text="开始换口型", command=self._start_lipsync,
            height=50, corner_radius=25, fg_color=BLUE, hover_color=BLUE_HOVER,
            text_color="white", font=("Segoe UI", 14, "bold"),
        )
        self.lipsync_submit_button.pack(fill="x")
        self._build_submission_feedback(submit_card, "lipsync", self.lipsync_submit_button)

    def _lipsync_video_player_state(self, playing: bool, paused: bool) -> None:
        if not playing:
            self.lipsync_video_play_var.set("播放")
        elif paused:
            self.lipsync_video_play_var.set("继续")
        else:
            self.lipsync_video_play_var.set("暂停")

    def _lipsync_audio_player_state(self, playing: bool, paused: bool) -> None:
        if not playing:
            self.lipsync_audio_play_var.set("播放")
        elif paused:
            self.lipsync_audio_play_var.set("继续")
        else:
            self.lipsync_audio_play_var.set("暂停")

    def _choose_lipsync_video(self) -> None:
        selected = filedialog.askopenfilename(
            parent=self.root, title="选择换口型视频",
            filetypes=[("视频", "*.mp4 *.mov *.webm"), ("所有文件", "*.*")],
        )
        if selected:
            self._load_lipsync_video(Path(selected))

    def _choose_lipsync_audio(self) -> None:
        selected = filedialog.askopenfilename(
            parent=self.root, title="选择配音音频",
            filetypes=[("音频", "*.mp3 *.wav *.m4a *.aac"), ("所有文件", "*.*")],
        )
        if selected:
            self._load_lipsync_audio(Path(selected))

    def _load_lipsync_video(self, selected: Path) -> None:
        path = selected.resolve()
        token = uuid.uuid4().hex
        self._lipsync_video_token = token
        self.lipsync_video_var.set("")
        self._lipsync_video_seconds = None
        self.lipsync_video_info_var.set("正在识别视频时长与分辨率…")
        self.lipsync_video_button.configure(state="disabled")
        threading.Thread(
            target=self._inspect_lipsync_video, args=(token, path), daemon=True
        ).start()

    def _inspect_lipsync_video(self, token: str, path: Path) -> None:
        try:
            path = Path(self.desktop_store.import_asset(path)["path"])
            info = probe_video(path)
            if info.duration > 600:
                raise VideoGenerationError(
                    f"视频为 {info.duration:g} 秒，Sync3 最长支持 600 秒（10 分钟）。"
                )
            if info.width > 4096 or info.height > 2160:
                raise VideoGenerationError(
                    f"视频分辨率为 {info.width}x{info.height}，超过 Sync3 上限 4K（4096x2160）。"
                )
            poster = video_poster(path, (420, 220))
        except Exception as exc:
            self.events.put(("lipsync_video_failed", {"token": token, "error": str(exc)}))
            return
        self.events.put(
            ("lipsync_video_loaded", {"token": token, "path": path, "info": info, "poster": poster})
        )

    def _load_lipsync_audio(self, selected: Path) -> None:
        path = selected.resolve()
        token = uuid.uuid4().hex
        self._lipsync_audio_token = token
        self.lipsync_audio_var.set("")
        self._lipsync_audio_seconds = None
        self.lipsync_audio_info_var.set("正在识别音频时长…")
        self.lipsync_audio_button.configure(state="disabled")
        threading.Thread(
            target=self._inspect_lipsync_audio, args=(token, path), daemon=True
        ).start()

    def _inspect_lipsync_audio(self, token: str, path: Path) -> None:
        try:
            path = Path(self.desktop_store.import_asset(path)["path"])
            seconds = probe_audio_duration(path)
            if seconds > 600:
                raise VideoGenerationError(
                    f"音频为 {seconds:g} 秒，Sync3 最长支持 600 秒（10 分钟）。"
                )
        except Exception as exc:
            self.events.put(("lipsync_audio_failed", {"token": token, "error": str(exc)}))
            return
        self.events.put(("lipsync_audio_loaded", {"token": token, "path": path, "seconds": seconds}))

    def _handle_lipsync_media(self, kind: str, data: dict[str, object]) -> None:
        token_key = f"_lipsync_{kind}_token"
        if str(data.get("token", "")) != getattr(self, token_key):
            return
        button = self.lipsync_video_button if kind == "video" else self.lipsync_audio_button
        info_var = self.lipsync_video_info_var if kind == "video" else self.lipsync_audio_info_var
        error = str(data.get("error", "") or "")
        if error:
            if not self.running:
                button.configure(state="normal")
            info_var.set("读取失败，请重新选择")
            self._update_lipsync_cost()
            messagebox.showerror("无法读取素材", error, parent=self.root)
            return
        path = data["path"]
        if not isinstance(path, Path):
            return
        if kind == "video":
            info = data["info"]
            poster = data.get("poster")
            self.lipsync_video_var.set(str(path))
            self._lipsync_video_seconds = float(info.duration)
            info_var.set(f"{path.name}（{info.duration:g} 秒，{info.width}x{info.height}）")
            if isinstance(poster, Image.Image):
                self.lipsync_poster_image = ctk.CTkImage(
                    light_image=poster, dark_image=poster, size=(420, 220)
                )
                self.lipsync_video_poster.configure(image=self.lipsync_poster_image, text="")
            self.lipsync_video_player.load(path)
            if not self.running:
                self.lipsync_play_button.configure(state="normal")
                self.lipsync_replay_button.configure(state="normal")
        else:
            self.lipsync_audio_var.set(str(path))
            self._lipsync_audio_seconds = float(data["seconds"])
            info_var.set(f"{path.name}（{float(data['seconds']):g} 秒）")
            self.lipsync_audio_player.load(path)
            if not self.running:
                self.lipsync_audio_play_button.configure(state="normal")
                self.lipsync_audio_replay_button.configure(state="normal")
        if not self.running:
            button.configure(state="normal")
        self._update_lipsync_cost()

    def _update_lipsync_cost(self) -> None:
        video_s, audio_s = self._lipsync_video_seconds, self._lipsync_audio_seconds
        if video_s and audio_s:
            output = min(video_s, audio_s)
            cost = output / 60 * 8
            self.lipsync_cost_var.set(
                f"输出约 {output:g} 秒（取两者较短），预估费用约 ${cost:.2f}（$8/分钟）"
            )
        elif video_s or audio_s:
            self.lipsync_cost_var.set("再选择另一个素材，这里显示输出时长和预估费用。")
        else:
            self.lipsync_cost_var.set("选好视频和音频后，这里显示输出时长和预估费用。")

    def _automatic_lipsync_output_path(self, source: Path) -> Path:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        folder = Path(self.workspace_output_dir_var.get().strip()).expanduser().resolve()
        return folder / f"{Path(self._asset_name(source)).stem}_换口型_{stamp}.mp4"

    def _start_lipsync(self) -> None:
        if self._handle_existing_submission("lipsync"):
            return
        if self.running or not self._require_app_credentials(video=True):
            return
        video = Path(self.lipsync_video_var.get().strip()).expanduser()
        audio = Path(self.lipsync_audio_var.get().strip()).expanduser()
        if not video.is_file():
            messagebox.showerror("视频无效", "请先选择一个有效的视频文件。", parent=self.root)
            return
        if not audio.is_file():
            messagebox.showerror("音频无效", "请先选择一个有效的音频文件。", parent=self.root)
            return
        output = self._automatic_lipsync_output_path(video)
        self.lipsync_video_player.stop()
        self.lipsync_audio_player.stop()
        self._start_worker(
            "lipsync",
            {"video": str(video.resolve()), "audio": str(audio.resolve()), "output": str(output)},
        )

    # ---- AI 视频修复画质 ------------------------------------------------------

    def _build_repair_tab(self) -> None:
        page = self.repair_page = ctk.CTkScrollableFrame(
            self.repair_tab, fg_color=BG, corner_radius=0, scrollbar_button_color="#C7C7CC"
        )
        page.pack(fill="both", expand=True, padx=34, pady=24)
        ctk.CTkLabel(
            page, text="AI 视频修复画质", text_color=TEXT,
            font=("Segoe UI", 28, "bold"), anchor="w",
        ).pack(anchor="w")
        ctk.CTkLabel(
            page,
            text="低画质视频一键修复放大（fal FlashVSR，可选放大倍数和处理档位）。",
            text_color=MUTED, font=("Segoe UI", 13), anchor="w",
        ).pack(anchor="w", pady=(4, 22))

        _, media_card = self._card(
            page, "1  素材", "MP4 / MOV / WebM，单文件最大 90MB；输出分辨率 = 原分辨率 × 放大倍数。"
        )
        video_panel = ctk.CTkFrame(media_card, fg_color="#F7F7F9", corner_radius=14)
        video_panel.pack(fill="x")
        ctk.CTkLabel(
            video_panel, text="视频", text_color=TEXT, font=("Segoe UI", 13, "bold")
        ).pack(anchor="w", padx=14, pady=(13, 8))
        self.repair_video_stage = ctk.CTkFrame(video_panel, height=250, fg_color="#111111", corner_radius=12)
        self.repair_video_stage.pack(fill="x", padx=14)
        self.repair_video_stage.pack_propagate(False)
        self.repair_video_poster = ctk.CTkLabel(
            self.repair_video_stage,
            text="选择视频后可在这里播放",
            text_color="#A1A1A6",
            fg_color="#111111",
            corner_radius=12,
            font=("Segoe UI", 12),
        )
        self.repair_video_poster.place(
            relx=0.5, rely=0.5, anchor="center", relwidth=1, relheight=1
        )
        self.repair_video_player = EmbeddedVideoPlayer(
            self.root, self.repair_video_stage, self.repair_video_poster
        )
        self.repair_video_player.state_callback = self._repair_player_state
        controls = ctk.CTkFrame(video_panel, fg_color="transparent")
        controls.pack(fill="x", padx=14, pady=(10, 4))
        self.repair_video_button = self._secondary_button(
            controls, "选择视频", self._choose_repair_video, width=100
        )
        self.repair_video_button.pack(side="left")
        self._secondary_button(video_panel, "从素材库选择", lambda: self._open_library("画质修复视频"), width=130).pack(anchor="w", padx=14, pady=4)
        self.repair_play_button = self._secondary_button(
            controls, "播放", self.repair_video_player.toggle, width=78
        )
        self.repair_play_button.configure(textvariable=self.repair_play_var, state="disabled")
        self.repair_play_button.pack(side="left", padx=6)
        self.repair_replay_button = self._secondary_button(
            controls, "重播", self.repair_video_player.replay, width=70
        )
        self.repair_replay_button.configure(state="disabled")
        self.repair_replay_button.pack(side="left")
        ctk.CTkLabel(
            video_panel, textvariable=self.repair_video_info_var,
            text_color=MUTED, font=("Segoe UI", 11), anchor="w",
        ).pack(fill="x", padx=15, pady=(5, 14))

        _, options_card = self._card(
            page, "2  参数设置", "放大倍数越高费用越高、耗时越长；长视频建议选择更快的处理档位。"
        )
        options_row = ctk.CTkFrame(options_card, fg_color="transparent")
        options_row.pack(fill="x")
        options_row.grid_columnconfigure((0, 1), weight=1, uniform="repair_opt")
        self._field_label(options_row, "放大倍数").grid(row=0, column=0, sticky="w", padx=(0, 12))
        self._field_label(options_row, "处理档位").grid(row=0, column=1, sticky="w")
        self.repair_factor_box = ctk.CTkOptionMenu(
            options_row,
            variable=self.repair_factor_var,
            values=["1", "1.5", "2", "3", "4"],
            command=lambda _value: self._update_repair_cost(),
            height=42, corner_radius=11,
            fg_color="#F0F0F2", button_color="#E5E5EA",
            button_hover_color="#D1D1D6", text_color=TEXT,
        )
        self.repair_factor_box.grid(row=1, column=0, sticky="ew", padx=(0, 12), pady=(6, 0))
        self.repair_accel_box = ctk.CTkOptionMenu(
            options_row,
            variable=self.repair_accel_var,
            values=["regular 最佳质量", "high 均衡", "full 最快"],
            height=42, corner_radius=11,
            fg_color="#F0F0F2", button_color="#E5E5EA",
            button_hover_color="#D1D1D6", text_color=TEXT,
        )
        self.repair_accel_box.grid(row=1, column=1, sticky="ew", pady=(6, 0))
        switches = ctk.CTkFrame(options_card, fg_color="transparent")
        switches.pack(fill="x", pady=(14, 0))
        self.repair_color_fix_switch = ctk.CTkSwitch(
            switches, text="色彩校正", variable=self.repair_color_fix_var,
            font=("Segoe UI", 12), text_color=TEXT, progress_color=BLUE,
        )
        self.repair_color_fix_switch.pack(side="left", padx=(2, 28))
        self.repair_audio_switch = ctk.CTkSwitch(
            switches, text="保留原音轨", variable=self.repair_audio_var,
            font=("Segoe UI", 12), text_color=TEXT, progress_color=BLUE,
        )
        self.repair_audio_switch.pack(side="left")

        _, submit_card = self._card(
            page, "3  开始修复", "按输出视频的像素总量计费（$0.0005/百万像素）；修复时长与视频长度相当。"
        )
        cost = ctk.CTkFrame(submit_card, fg_color=SOFT_BLUE, corner_radius=12)
        cost.pack(fill="x", pady=(0, 14))
        ctk.CTkLabel(
            cost, textvariable=self.repair_cost_var,
            text_color=BLUE, font=("Segoe UI", 12, "bold"), anchor="w",
        ).pack(fill="x", padx=16, pady=12)
        self.repair_submit_button = ctk.CTkButton(
            submit_card, text="开始修复画质", command=self._start_repair,
            height=50, corner_radius=25, fg_color=BLUE, hover_color=BLUE_HOVER,
            text_color="white", font=("Segoe UI", 14, "bold"),
        )
        self.repair_submit_button.pack(fill="x")
        self._build_submission_feedback(submit_card, "repair", self.repair_submit_button)

    def _repair_player_state(self, playing: bool, paused: bool) -> None:
        if not playing:
            self.repair_play_var.set("播放")
        elif paused:
            self.repair_play_var.set("继续")
        else:
            self.repair_play_var.set("暂停")

    def _choose_repair_video(self) -> None:
        selected = filedialog.askopenfilename(
            parent=self.root, title="选择要修复的视频",
            filetypes=[("视频", "*.mp4 *.mov *.webm"), ("所有文件", "*.*")],
        )
        if selected:
            self._load_repair_video(Path(selected))

    def _load_repair_video(self, selected: Path) -> None:
        path = selected.resolve()
        token = uuid.uuid4().hex
        self._repair_video_token = token
        self.repair_video_var.set("")
        self._repair_video_frames = None
        self._repair_video_info = None
        self.repair_video_info_var.set("正在识别视频信息…")
        self.repair_video_button.configure(state="disabled")
        threading.Thread(
            target=self._inspect_repair_video, args=(token, path), daemon=True
        ).start()

    def _inspect_repair_video(self, token: str, path: Path) -> None:
        try:
            path = Path(self.desktop_store.import_asset(path)["path"])
            size_mb = path.stat().st_size / (1024 * 1024)
            if path.stat().st_size > 90 * 1024 * 1024:
                raise VideoGenerationError(
                    f"视频为 {size_mb:.0f}MB，超过 fal 上传上限 90MB；请先压缩或剪短。"
                )
            info = probe_video(path)
            fps = probe_frame_rate(path)
            poster = video_poster(path, (420, 250))
        except Exception as exc:
            self.events.put(("repair_video_failed", {"token": token, "error": str(exc)}))
            return
        self.events.put(
            ("repair_video_loaded",
             {"token": token, "path": path, "info": info, "fps": fps, "poster": poster})
        )

    def _handle_repair_media(self, data: dict[str, object]) -> None:
        if str(data.get("token", "")) != self._repair_video_token:
            return
        error = str(data.get("error", "") or "")
        if error:
            if not self.running:
                self.repair_video_button.configure(state="normal")
            self.repair_video_info_var.set("读取失败，请重新选择")
            self._update_repair_cost()
            messagebox.showerror("无法读取视频", error, parent=self.root)
            return
        path = data["path"]
        info = data["info"]
        poster = data.get("poster")
        if not isinstance(path, Path) or not isinstance(info, VideoInfo):
            return
        fps = float(data["fps"])
        self.repair_video_var.set(str(path))
        self._repair_video_info = info
        self._repair_video_frames = max(1, round(info.duration * fps))
        self.repair_video_info_var.set(
            f"{path.name}（{info.duration:g} 秒，{info.width}x{info.height}，{fps:g}fps）"
        )
        if isinstance(poster, Image.Image):
            self.repair_poster_image = ctk.CTkImage(
                light_image=poster, dark_image=poster, size=(420, 250)
            )
            self.repair_video_poster.configure(image=self.repair_poster_image, text="")
        self.repair_video_player.load(path)
        if not self.running:
            self.repair_video_button.configure(state="normal")
            self.repair_play_button.configure(state="normal")
            self.repair_replay_button.configure(state="normal")
        self._update_repair_cost()

    def _update_repair_cost(self) -> None:
        info = getattr(self, "_repair_video_info", None)
        if not self.repair_video_var.get().strip() or self._repair_video_frames is None or info is None:
            self.repair_cost_var.set("选好视频后，这里显示输出规格和预估费用。")
            return
        try:
            factor = float(self.repair_factor_var.get())
        except ValueError:
            factor = 2.0
        out_w = round(info.width * factor)
        out_h = round(info.height * factor)
        megapixels = out_w * out_h * self._repair_video_frames / 1_000_000
        cost = megapixels * 0.0005
        self.repair_cost_var.set(
            f"输出 {out_w}x{out_h}，约 {self._repair_video_frames} 帧，预估费用约 ${cost:.2f}（{factor:g} 倍放大）"
        )

    def _automatic_repair_output_path(self, source: Path) -> Path:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        folder = Path(self.workspace_output_dir_var.get().strip()).expanduser().resolve()
        return folder / f"{Path(self._asset_name(source)).stem}_画质修复_{stamp}.mp4"

    def _start_repair(self) -> None:
        if self._handle_existing_submission("repair"):
            return
        if self.running or not self._require_app_credentials(video=True):
            return
        video = Path(self.repair_video_var.get().strip()).expanduser()
        if not video.is_file():
            messagebox.showerror("视频无效", "请先选择一个有效的视频文件。", parent=self.root)
            return
        output = self._automatic_repair_output_path(video)
        self.repair_video_player.stop()
        options = {
            "upscale_factor": float(self.repair_factor_var.get()),
            "acceleration": self.repair_accel_var.get().split(" ", 1)[0],
            "color_fix": bool(self.repair_color_fix_var.get()),
            "preserve_audio": bool(self.repair_audio_var.get()),
        }
        self._start_worker(
            "repair",
            {"video": str(video.resolve()), "output": str(output), "options": options},
        )

    def _build_video_preview(self, media) -> None:
        panel = ctk.CTkFrame(media, fg_color="#F7F7F9", corner_radius=14)
        panel.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        ctk.CTkLabel(
            panel, text="原视频", text_color=TEXT, font=("Segoe UI", 13, "bold")
        ).pack(anchor="w", padx=14, pady=(13, 8))
        self.video_stage = ctk.CTkFrame(panel, height=250, fg_color="#111111", corner_radius=12)
        self.video_stage.pack(fill="x", padx=14)
        self.video_stage.pack_propagate(False)
        self.video_poster_label = ctk.CTkLabel(
            self.video_stage,
            text="选择原视频后可在这里播放",
            text_color="#A1A1A6",
            fg_color="#111111",
            corner_radius=12,
            font=("Segoe UI", 12),
        )
        self.video_poster_label.place(
            relx=0.5, rely=0.5, anchor="center", relwidth=1, relheight=1
        )
        self.video_player = EmbeddedVideoPlayer(self.root, self.video_stage, self.video_poster_label)
        self.video_player.state_callback = self._video_player_state
        controls = ctk.CTkFrame(panel, fg_color="transparent")
        controls.pack(fill="x", padx=14, pady=(10, 4))
        self.source_button = self._secondary_button(
            controls, "选择视频", self._choose_source_video, width=100
        )
        self.source_button.pack(side="left")
        self._secondary_button(panel, "从素材库选择", lambda: self._open_library("换品原视频"), width=130).pack(anchor="w", padx=14, pady=4)
        self.play_button = self._secondary_button(controls, "播放", self.video_player.toggle, width=78)
        self.play_button.configure(textvariable=self.play_button_var, state="disabled")
        self.play_button.pack(side="left", padx=6)
        self.replay_button = self._secondary_button(controls, "重播", self.video_player.replay, width=70)
        self.replay_button.configure(state="disabled")
        self.replay_button.pack(side="left")
        ctk.CTkLabel(
            panel,
            textvariable=self.source_summary_var,
            text_color=MUTED,
            font=("Segoe UI", 11),
            anchor="w",
        ).pack(fill="x", padx=15, pady=(5, 14))

    def _build_image_preview(self, media) -> None:
        panel = ctk.CTkFrame(media, fg_color="#F7F7F9", corner_radius=14)
        panel.grid(row=0, column=1, sticky="nsew", padx=(8, 0))
        ctk.CTkLabel(
            panel, text="产品参考图", text_color=TEXT, font=("Segoe UI", 13, "bold")
        ).pack(anchor="w", padx=14, pady=(13, 8))
        self.image_stage = ctk.CTkFrame(panel, height=250, fg_color="#EFEFF4", corner_radius=12)
        self.image_stage.pack(fill="x", padx=14)
        self.image_stage.pack_propagate(False)
        self.product_image_label = ctk.CTkLabel(
            self.image_stage,
            text="选择图片后显示缩略图",
            text_color=MUTED,
            fg_color="#EFEFF4",
            corner_radius=12,
            font=("Segoe UI", 12),
        )
        self.product_image_label.pack(fill="both", expand=True)
        controls = ctk.CTkFrame(panel, fg_color="transparent")
        controls.pack(fill="x", padx=14, pady=(10, 4))
        self.reference_button = self._secondary_button(
            controls, "选择图片", self._choose_reference_image, width=100
        )
        self.reference_button.pack(side="left")
        self._secondary_button(panel, "从素材库选择", lambda: self._open_library("换品产品图"), width=130).pack(anchor="w", padx=14, pady=4)
        ctk.CTkLabel(
            panel,
            textvariable=self.image_summary_var,
            text_color=MUTED,
            font=("Segoe UI", 11),
            anchor="w",
        ).pack(fill="x", padx=15, pady=(5, 14))

    def _build_api_tab(self) -> None:
        page = self.api_page = ctk.CTkScrollableFrame(
            self.api_tab, fg_color=BG, corner_radius=0, scrollbar_button_color="#C7C7CC"
        )
        page.pack(fill="both", expand=True, padx=34, pady=24)
        ctk.CTkLabel(
            page,
            text="设置",
            text_color=TEXT,
            font=("Segoe UI", 28, "bold"),
            anchor="w",
        ).pack(anchor="w")
        ctk.CTkLabel(
            page,
            text="统一管理工作区和 API Key；任务过程中不再要求选择保存位置。",
            text_color=MUTED,
            font=("Segoe UI", 13),
            anchor="w",
        ).pack(anchor="w", pady=(4, 22))

        _, workspace = self._card(
            page,
            "工作区",
            "生成结果自动保存到这里，已有任务继续使用原保存目录。",
        )
        workspace.grid_columnconfigure(0, weight=1)
        self._field_label(workspace, "成片保存目录").grid(row=0, column=0, sticky="w")
        self.workspace_output_entry = self._entry(workspace, self.workspace_output_dir_var)
        self.workspace_output_entry.grid(row=1, column=0, sticky="ew", padx=(0, 10), pady=(6, 0))
        self._secondary_button(
            workspace, "选择文件夹", self._choose_workspace_output_dir, width=108
        ).grid(row=1, column=1, pady=(6, 0))

        self._build_assistant_settings(page)
        self._build_maintenance_settings(page)
        self._build_fish_settings(page)
        _, queue = self._card(page, "任务队列", "所有创作功能共享并发上限。")
        self.concurrency_var = StringVar(value=str(self.tasks.limit))
        ctk.CTkLabel(queue, text="最多同时执行").pack(side="left", padx=(0, 14))
        ctk.CTkOptionMenu(queue, variable=self.concurrency_var, values=[str(i) for i in range(1, 11)], width=90, command=self._set_concurrency).pack(side="left")

        _, prompt = self._card(page, "AI 助手 · Kaiyun GPT-6.1 Sol", "AI 美工、AI 导演与学习分析共用下方 KaiyunCode 账户，按调用用量计费。")
        prompt.grid_columnconfigure(1, weight=1)
        self._api_row(prompt, 0, "用途", label="AI 美工、AI 导演与学习建议")
        self._api_row(prompt, 1, "模型", label="gpt-6.1-sol")
        self._api_row(prompt, 2, "账户配置", label="在下方 KaiyunCode 服务中填写 API 地址与 API Key")

        models_by_provider: dict[str, list[str]] = {}
        from feature_policy import RETIRED_MODELS
        for model in self.models:
            if model["model"] in RETIRED_MODELS:
                continue
            models_by_provider.setdefault(model["provider"], []).append(model["model"])
        for provider, spec in self.runner.credentials.providers.items():
            provider_name = str(spec.get("display_name", provider))
            recovery_only = provider not in self.runner.registry.providers
            title = f"旧任务恢复 · {provider_name}（选填）" if recovery_only else f"生成服务 · {provider_name}"
            _, frame = self._card(page, title)
            frame.grid_columnconfigure(1, weight=1)
            base_env, key_env = str(spec["api_base_env"]), str(spec["api_key_env"])
            base_var = StringVar(value=self.saved_credentials.get(base_env, str(spec["default_api_base"])))
            key_var = StringVar(value=self.saved_credentials.get(key_env, ""))
            self.provider_vars[provider] = {
                "base_env": base_env,
                "key_env": key_env,
                "base_var": base_var,
                "key_var": key_var,
            }
            model_names = "仅查询、下载已有任务；不用于新生成" if recovery_only else "、".join(models_by_provider.get(provider, []))
            if provider == "kaiyuncode":
                model_names = "AI 助手（gpt-6.1-sol）、" + model_names
            self._api_row(frame, 0, "当前功能", label=model_names)
            self._api_row(frame, 1, "API 地址", variable=base_var)
            self._api_row(frame, 2, "API Key", variable=key_var, secret=True)
            if provider == "dashscope":
                self._api_row(frame, 3, "地址说明", label="北京公共地址：https://dashscope.aliyuncs.com/api/v1；如使用专属业务空间，请填写该空间的地域 API 地址（以 /api/v1 结尾）。Key 与 fal 分别保存。")

        controls = ctk.CTkFrame(page, fg_color="transparent")
        controls.pack(fill="x", pady=(2, 14))
        ctk.CTkSwitch(
            controls,
            text="显示 Key",
            variable=self.show_keys_var,
            command=self._toggle_keys,
            progress_color=BLUE,
            button_color="white",
            text_color=TEXT,
        ).pack(side="left")
        self._secondary_button(controls, "从现有配置导入", self._import_existing, width=142).pack(
            side="left", padx=10
        )
        ctk.CTkButton(
            controls,
            text="安全保存",
            command=self._save_credentials,
            width=112,
            height=42,
            corner_radius=21,
            fg_color=BLUE,
            hover_color=BLUE_HOVER,
            font=("Segoe UI", 12, "bold"),
        ).pack(side="right")
        self._secondary_button(
            controls, "清除工作台配置", self._clear_credentials, width=142
        ).pack(side="right", padx=10)
        ctk.CTkLabel(
            page,
            textvariable=self.api_status_var,
            text_color=TEXT,
            fg_color=CARD,
            corner_radius=14,
            height=48,
            font=("Segoe UI", 12, "bold"),
            anchor="w",
        ).pack(fill="x", pady=(0, 20))

    def _choose_workspace_output_dir(self) -> None:
        selected = filedialog.askdirectory(
            parent=self.root,
            title="选择素材万象成片保存目录",
            initialdir=self.workspace_output_dir_var.get() or str(PROJECT_ROOT),
        )
        if selected:
            self.workspace_output_dir_var.set(str(Path(selected).resolve()))

    def _api_row(
        self,
        frame,
        row: int,
        title: str,
        *,
        variable: StringVar | None = None,
        label: str | None = None,
        secret: bool = False,
    ) -> None:
        self._field_label(frame, title).grid(row=row, column=0, sticky="w", padx=(0, 16), pady=7)
        if variable is not None:
            entry = self._entry(frame, variable, secret=secret)
            entry.grid(row=row, column=1, sticky="ew", pady=7)
            if secret:
                self.key_entries.append(entry)
        else:
            ctk.CTkLabel(
                frame,
                text=label or "",
                text_color=TEXT,
                font=("Segoe UI", 12),
                anchor="w",
                justify="left",
                wraplength=650,
            ).grid(row=row, column=1, sticky="w", pady=7)

    def _new_default_output(self) -> Path:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        if hasattr(self, "workspace_output_dir_var"):
            folder = Path(self.workspace_output_dir_var.get().strip())
        else:
            folder = Path(self.saved_credentials.get("WORKSPACE_OUTPUT_DIR", PROJECT_ROOT / "output"))
        return folder / f"swap_{stamp}.mp4"

    def _select_provider(self, display: str) -> None:
        provider = self.provider_keys.get(display, "")
        models = [
            model
            for model in self.route_labels
            if self.model_map.get(model, {}).get("provider") == provider
        ]
        if not models:
            return
        default_model = str(
            self.runner.registry.providers.get(provider, {}).get("default_model", "")
        )
        target = default_model if default_model in models else models[0]
        self.model_box.configure(values=[self.route_labels[m] for m in models])
        self.model_var.set(target)
        self.video_route_var.set(self.route_labels[target])
        self._update_model_help()

    def _select_video_route(self, label: str) -> None:
        for model, route_label in self.route_labels.items():
            if label == route_label:
                self.model_var.set(model)
                break
        self._update_model_help()

    def _update_model_help(self) -> None:
        model = self.model_var.get().strip() or DEFAULT_VIDEO_MODEL
        if model not in self.route_labels:
            model = DEFAULT_VIDEO_MODEL if DEFAULT_VIDEO_MODEL in self.route_labels else next(iter(self.route_labels))
            self.model_var.set(model)
        self.video_route_var.set(self.route_labels[model])
        item = self.model_map.get(model, {})
        self.model_help_var.set(f'模型：{item.get("api_model", model)}')
        self.provider_var.set(self.provider_names.get(item.get("provider", "fal"), ""))
        if hasattr(self, "model_box"):
            self.model_box.configure(values=[label for route, label in self.route_labels.items()
                                             if self.model_map[route]["provider"] == item.get("provider")])
        provider = item.get("provider", "fal")
        previous_provider = getattr(self, "_feature_provider", provider)
        saved_features = getattr(self, "_provider_feature_values", {})
        if previous_provider != provider:
            saved_features[previous_provider] = (self.enable_prompt_expansion_var.get(), self.enable_thinking_var.get())
            if provider in saved_features:
                expansion, thinking = saved_features[provider]
                self.enable_prompt_expansion_var.set(expansion)
                self.enable_thinking_var.set(thinking)
        self._feature_provider, self._provider_feature_values = provider, saved_features
        if hasattr(self, "thinking_checkbox"):
            if provider in {"dashscope", "kaiyuncode"}:
                self.thinking_checkbox.pack_forget()
            elif not self.thinking_checkbox.winfo_manager():
                self.thinking_checkbox.pack(anchor="w", pady=(0, 12), before=self.full_button)
        resolutions = ["480p", "720p", "1080p"]
        fallback = "480p"
        if hasattr(self, "resolution_box"):
            self.resolution_box.configure(values=resolutions)
            if self.resolution_var.get() not in resolutions:
                self.resolution_var.set(fallback)
        self._update_swap_cost()

    def _update_swap_cost(self) -> None:
        """一键换品预估费用：视觉分析按实际用量计费，视频按秒估算。"""
        info = getattr(self, "video_info", None)
        if info is None or not self.source_video_var.get().strip():
            self.swap_cost_var.set("选择原视频后，这里显示一键换品的预估费用。")
            return
        model = self.model_var.get().strip() or DEFAULT_VIDEO_MODEL
        adapter = str(self.model_map.get(model, {}).get("adapter", "fal_wan3"))
        if adapter == "kaiyuncode_wan3":
            self.swap_cost_var.set("KaiyunCode 费用以平台当前模型及账户分组计费为准。")
            return
        price_table = SWAP_VIDEO_PRICE_USD.get(adapter)
        if price_table is None:
            settings = self.provider_vars.get("dashscope", {})
            base_var = settings.get("base_var")
            base = str((base_var.get() if base_var else "") or self.runner.registry.providers.get("dashscope", {}).get("default_api_base", ""))
            from urllib.parse import urlsplit
            host = urlsplit(base).hostname or ""
            if host != "dashscope.aliyuncs.com" and not host.endswith(".cn-beijing.maas.aliyuncs.com"):
                self.swap_cost_var.set("当前阿里云地域预估价格暂未核实；可继续生成，以实际计费为准。")
                return
            rates = {"480p": 0.45, "720p": 0.90, "1080p": 1.80} if model.endswith("-prime") else {"480p": 0.30, "720p": 0.60, "1080p": 1.20}
            resolution = self.resolution_var.get().strip() or "480p"
            try:
                seconds = int(self.duration_var.get().strip())
            except (ValueError, AttributeError):
                seconds = max(2, math.ceil(info.duration))
            rate = rates[resolution]
            cost = (seconds + info.duration) * rate
            self.swap_cost_var.set(f"视频预估约 ¥{cost:.2f}（北京原价 · 输出 {seconds} 秒 + 参考 {info.duration:g} 秒 × ¥{rate}/秒；以实际计费为准）")
            return
        resolution = self.resolution_var.get().strip()
        if resolution not in price_table:
            resolution = next(iter(price_table))
        price = price_table[resolution]
        try:
            seconds = max(2, int(self.duration_var.get().strip()))
        except (ValueError, AttributeError):
            seconds = max(2, math.ceil(info.duration))
        cost = price * seconds
        self.swap_cost_var.set(
            ("预估费用：沿用提示词，无分析费用 + " if self.prompt_mode_var.get() == "使用已有提示词" else "预估费用：视觉分析按实际用量计费 + ")
            +
            f"视频生成约 ${cost:.2f}（{resolution} × {seconds:g} 秒 @ ${price}/秒）"
        )

    def _choose_source_video(self) -> None:
        selected = filedialog.askopenfilename(
            parent=self.root,
            title="选择原视频",
            filetypes=[("视频", "*.mp4 *.mov *.webm *.m4v *.avi *.mkv"), ("所有文件", "*.*")],
        )
        if selected:
            self._load_source_video(Path(selected))

    def _load_source_video(self, selected: Path) -> None:
        self._pending_reuse_specs = None
        path = selected.resolve()
        token = uuid.uuid4().hex
        self._source_load_token = token
        self.source_video_var.set("")
        self.status_var.set("正在识别原视频时长与比例…")
        self.source_button.configure(state="disabled")
        threading.Thread(
            target=self._inspect_source_video,
            args=(token, path),
            daemon=True,
        ).start()

    def _inspect_source_video(self, token: str, path: Path) -> None:
        try:
            path = Path(self.desktop_store.import_asset(path)["path"])
            info = probe_video(path)
            poster = video_poster(path, (420, 250))
        except Exception as exc:
            self.events.put(
                ("source_load_failed", {"token": token, "error": str(exc)})
            )
            return
        self.events.put(
            (
                "source_loaded",
                {"token": token, "path": path, "info": info, "poster": poster},
            )
        )

    def _handle_source_loaded(self, data: dict[str, object]) -> None:
        if str(data.get("token", "")) != self._source_load_token:
            return
        path = data["path"]
        info = data["info"]
        poster = data["poster"]
        if (
            not isinstance(path, Path)
            or not isinstance(info, VideoInfo)
            or not isinstance(poster, Image.Image)
        ):
            self._handle_source_load_failed(
                {"token": self._source_load_token, "error": "视频识别结果格式无效。"}
            )
            return
        self.source_video_var.set(str(path))
        self.video_info = info
        self.duration_var.set(str(info.duration_seconds))
        self.aspect_ratio_var.set(info.aspect_ratio)
        if getattr(self, "_pending_reuse_specs", None):
            duration, ratio = self._pending_reuse_specs
            self._pending_reuse_specs = None
            if duration: self.duration_var.set(str(duration))
            if ratio: self.aspect_ratio_var.set(ratio)
        self.source_summary_var.set(f"{self._asset_name(path)} · {info.summary}")
        self.video_preview_ctk_image = ctk.CTkImage(
            light_image=poster, dark_image=poster, size=(420, 250)
        )
        self.video_poster_label.configure(image=self.video_preview_ctk_image, text="")
        self.video_player.load(path)
        self.play_button.configure(state="normal")
        self.replay_button.configure(state="normal")
        if hasattr(self, "system_player_button"):
            self.system_player_button.configure(state="normal")
        self.output_var.set(str(self._automatic_output_path(path)))
        self._update_swap_cost()
        if not self.running:
            self.source_button.configure(state="normal")
        self.status_var.set("已识别原视频规格，输出设置已自动跟随")

    def _handle_source_load_failed(self, data: dict[str, object]) -> None:
        if str(data.get("token", "")) != self._source_load_token:
            return
        if not self.running:
            self.source_button.configure(state="normal")
        self.status_var.set("原视频读取失败")
        messagebox.showerror(
            "无法读取视频",
            str(data.get("error", "未知错误")),
            parent=self.root,
        )

    def _automatic_output_path(self, source: Path) -> Path:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        folder = Path(self.workspace_output_dir_var.get().strip()).expanduser().resolve()
        return folder / f"{Path(self._asset_name(source)).stem}_换品_{stamp}.mp4"

    def _choose_reference_image(self) -> None:
        selected = filedialog.askopenfilename(
            parent=self.root,
            title="选择产品参考图",
            filetypes=[("图片", "*.jpg *.jpeg *.png *.webp *.gif *.bmp"), ("所有文件", "*.*")],
        )
        if selected:
            self._load_reference_image(Path(selected))

    def _load_reference_image(self, selected: Path) -> None:
        self.extra_reference_images = []
        path = selected.resolve()
        token = uuid.uuid4().hex
        self._reference_load_token = token
        self.reference_image_var.set("")
        self.image_summary_var.set("正在读取产品参考图…")
        self.reference_button.configure(state="disabled")
        threading.Thread(
            target=self._inspect_reference_image,
            args=(token, path),
            daemon=True,
        ).start()

    def _inspect_reference_image(self, token: str, path: Path) -> None:
        try:
            path = Path(self.desktop_store.import_asset(path)["path"])
            thumbnail = image_thumbnail(path, (420, 250))
        except Exception as exc:
            self.events.put(
                ("reference_load_failed", {"token": token, "error": str(exc)})
            )
            return
        self.events.put(
            (
                "reference_loaded",
                {"token": token, "path": path, "thumbnail": thumbnail},
            )
        )

    def _handle_reference_loaded(self, data: dict[str, object]) -> None:
        if str(data.get("token", "")) != self._reference_load_token:
            return
        path = data["path"]
        thumbnail = data["thumbnail"]
        if not isinstance(path, Path) or not isinstance(thumbnail, Image.Image):
            self._handle_reference_load_failed(
                {"token": self._reference_load_token, "error": "图片读取结果格式无效。"}
            )
            return
        self.reference_image_var.set(str(path))
        self.product_preview_ctk_image = ctk.CTkImage(
            light_image=thumbnail, dark_image=thumbnail, size=(420, 250)
        )
        self.product_image_label.configure(image=self.product_preview_ctk_image, text="")
        self.image_summary_var.set(f"{self._asset_name(path)} · 共 {1 + len(self.extra_reference_images)} 张参考图")
        if not self.running:
            self.reference_button.configure(state="normal")

    def _handle_reference_load_failed(self, data: dict[str, object]) -> None:
        if str(data.get("token", "")) != self._reference_load_token:
            return
        if not self.running:
            self.reference_button.configure(state="normal")
        self.image_summary_var.set("产品参考图读取失败")
        messagebox.showerror(
            "无法读取图片",
            str(data.get("error", "未知错误")),
            parent=self.root,
        )

    def _video_player_state(self, playing: bool, paused: bool) -> None:
        if not playing:
            self.play_button_var.set("播放")
        elif paused:
            self.play_button_var.set("继续")
        else:
            self.play_button_var.set("暂停")

    def _choose_output(self) -> None:
        current = Path(self.output_var.get().strip() or self._new_default_output())
        selected = filedialog.asksaveasfilename(
            parent=self.root,
            title="保存输出视频",
            initialdir=str(current.parent),
            initialfile=current.name,
            defaultextension=".mp4",
            filetypes=[("MP4 视频", "*.mp4")],
        )
        if selected:
            self.output_var.set(selected)

    def _relation(self) -> str:
        return " ".join(self.relation_text.get("1.0", END).strip().split())

    def _context(self) -> tuple[str, str, str]:
        return (
            str(Path(self.source_video_var.get().strip()).expanduser().resolve()),
            str(Path(self.reference_image_var.get().strip()).expanduser().resolve()),
            self._relation(),
        )

    def _validate_common(
        self, require_relation: bool = True, require_generation: bool = True
    ) -> dict[str, object] | None:
        source = Path(self.source_video_var.get().strip()).expanduser()
        image = Path(self.reference_image_var.get().strip()).expanduser()
        model, output, relation = self.model_var.get().strip(), self.output_var.get().strip(), self._relation()
        if not source.is_file():
            messagebox.showerror("原视频无效", f"找不到原视频：\n{source}", parent=self.root)
            return None
        if not image.is_file():
            messagebox.showerror("参考图无效", f"找不到本地参考图：\n{image}", parent=self.root)
            return None
        if require_relation and not relation:
            messagebox.showerror(
                "缺少尺寸关系", "请亲自描述参考图产品与原视频产品的体积或外形尺寸关系。", parent=self.root
            )
            return None
        if len(relation) > 300:
            messagebox.showerror("尺寸关系过长", "请把尺寸关系压缩到 300 字以内。", parent=self.root)
            return None
        if not require_generation:
            return {
                "source": str(source.resolve()),
                "image": str(image.resolve()),
                "relation": relation,
            }
        if not model:
            messagebox.showerror("缺少模型", "请选择或输入视频模型。", parent=self.root)
            return None
        if not output or Path(output).suffix.lower() != ".mp4":
            messagebox.showerror("输出位置无效", "请选择以 .mp4 结尾的输出位置。", parent=self.root)
            return None
        try:
            duration = int(self.duration_var.get())
        except ValueError:
            messagebox.showerror("秒数无效", "视频秒数必须是整数。", parent=self.root)
            return None
        item = self.model_map.get(model, {"adapter": "fal_generic"})
        max_duration = 30
        if not 2 <= duration <= max_duration:
            messagebox.showerror(
                "秒数无效",
                f"当前线路支持 2-{max_duration} 秒。",
                parent=self.root,
            )
            return None
        try:
            references = references_for_swap(
                str(item["adapter"]),
                source_video=str(source.resolve()),
                local_reference_image=str(image.resolve()),
            )
        except ValueError as exc:
            messagebox.showerror("参考图输入不完整", str(exc), parent=self.root)
            return None
        return {
            "source": str(source.resolve()),
            "image": str(image.resolve()),
            "relation": relation,
            "model": model,
            "provider": item.get("provider", "fal"),
            "output": str(Path(output).expanduser().resolve()),
            "references": references,
            "duration": duration,
            "aspect_ratio": self.aspect_ratio_var.get(),
            "resolution": self.resolution_var.get(),
        }

    def _current_prompt(self) -> str:
        return self.generated_prompt.strip()

    def _set_prompt(self, prompt: str) -> None:
        self.generated_prompt = prompt

    def _credential_values(self) -> dict[str, str]:
        values = {
            "DEEPSEEK_API_KEY": self.prompt_key_var.get().strip(),
            "DEEPSEEK_API_BASE": self.prompt_base_var.get().strip(),
            "DEEPSEEK_PROMPT_MODEL": self.prompt_model_var.get().strip(),
            "WORKSPACE_OUTPUT_DIR": self.workspace_output_dir_var.get().strip(),
            "VIDEO_MODEL": self.model_var.get().strip(),
            "FISH_AUTH_PATH": str(self.fish_auth_path),
        }
        for data in self.provider_vars.values():
            values[str(data["base_env"])] = data["base_var"].get().strip()  # type: ignore[union-attr]
            values[str(data["key_env"])] = data["key_var"].get().strip()  # type: ignore[union-attr]
        return values

    def _bind_canvas_credentials(self) -> None:
        def refresh(*_):
            self._canvas_credentials_cache = self._credential_values()
            self._update_swap_cost()
        for variable in (self.prompt_key_var, self.prompt_base_var, self.prompt_model_var,
                         self.workspace_output_dir_var, self.model_var):
            variable.trace_add("write", refresh)
        for data in self.provider_vars.values():
            data["key_var"].trace_add("write", refresh)
            data["base_var"].trace_add("write", refresh)

    def _canvas_credentials(self) -> dict[str, str]:
        # The HTTP worker reads a main-thread snapshot; it never calls Tk.
        return dict(self._canvas_credentials_cache)

    def _toggle_fullscreen(self) -> None:
        self._fullscreen = not self._fullscreen
        self.root.attributes("-fullscreen", self._fullscreen)
        self.root.after(80, self.canvas_window.resize)

    def _leave_fullscreen(self):
        if self._fullscreen:
            self._toggle_fullscreen()
            return "break"
        return None

    def _open_canvas(self) -> None:
        try:
            if self.current_page == "canvas":
                self.canvas_window.open(self.canvas_host)
            else:
                self._show_page("canvas")
        except Exception as exc:
            messagebox.showerror("画布无法打开", str(exc), parent=self.root)

    def _end_demo(self) -> None:
        if not self.canvas_window.stop():
            return
        self.tasks.stop()
        self._closed = True
        self._ai_close_display()
        if self.tray:
            self.tray.stop()
        self.root.destroy()

    def _child_env(self) -> dict[str, str]:
        return build_isolated_child_env(
            dict(os.environ),
            self._credential_values(),
            self.runner.registry.providers,
        )

    def _require_app_credentials(self, *, prompt: bool = False, video: bool = False) -> bool:
        missing: list[str] = []
        if prompt and not self.prompt_key_var.get().strip():
            missing.append("提示词分析 API Key")
        if video:
            selected_model = self.model_var.get().strip()
            model = self.model_map.get(selected_model, {})
            provider = str(model.get("provider", ""))
            provider_values = self.provider_vars.get(provider)
            key_var = provider_values.get("key_var") if provider_values else None
            key = key_var.get().strip() if key_var is not None else ""  # type: ignore[union-attr]
            if not key:
                missing.append(f"{self.route_labels.get(selected_model, '视频服务')} API Key")
        if not missing:
            return True
        messagebox.showerror(
            "缺少素材万象 API Key",
            "请先在“设置”中填写素材万象自己的：\n"
            + "\n".join(f"• {name}" for name in missing)
            + "\n\n素材万象不会自动使用 Skill 的 .env Key。",
            parent=self.root,
        )
        return False




    def _update_reuse_prompt_visibility(self) -> None:
        if not hasattr(self, "reuse_prompt_box"):
            return
        if self.prompt_mode_var.get() == "使用已有提示词":
            before = getattr(self, "remove_subtitles_checkbox", None)
            kwargs = {"before": before} if before is not None else {}
            self.reuse_prompt_label.pack(anchor="w", **kwargs)
            self.reuse_prompt_box.pack(fill="x", pady=(4, 12), **kwargs)
        else:
            self.reuse_prompt_label.pack_forget()
            self.reuse_prompt_box.pack_forget()

    def _start_full_pipeline(self) -> None:
        if self._handle_existing_submission("full"):
            return
        reuse = self.prompt_mode_var.get() == "使用已有提示词"
        if not self._require_app_credentials(prompt=not reuse, video=True):
            return
        source = Path(self.source_video_var.get().strip())
        if source.is_file():
            self.output_var.set(str(self._automatic_output_path(source)))
        values = self._validate_common(require_relation=not reuse)
        if values is not None:
            prompt = self.reuse_prompt_box.get("1.0", "end-1c")
            if reuse and not prompt.strip():
                messagebox.showerror("缺少提示词", "请填写已有提示词，或从使用记录复制配置。", parent=self.root)
                return
            values.update(prompt_mode="reuse" if reuse else "analyze", prompt=prompt,
                          base_prompt=prompt if reuse else "",
                          extra_images=list(self.extra_reference_images),
                          remove_subtitles=self.remove_subtitles_var.get(),
                          enable_prompt_expansion=self.enable_prompt_expansion_var.get(),
                          enable_thinking=False if values.get("provider") in {"dashscope", "kaiyuncode"} else self.enable_thinking_var.get())
            self._start_worker("full", values)


    @staticmethod
    def _new_submission_draft():
        return {"request_id": uuid.uuid4().hex, "lock": threading.Lock(),
                "config": None, "job_id": None, "error": "", "submitted_at": 0,
                "cooldown_pending": False, "not_before": 0}

    def _build_submission_feedback(self, parent, mode, button):
        self._submission_buttons[mode] = button
        feedback = ctk.CTkFrame(parent, fg_color="transparent")
        feedback.pack(fill="x", pady=(8, 0))
        progress = ctk.CTkProgressBar(feedback, height=4, mode="indeterminate", progress_color=BLUE)
        self._submission_progress[mode] = progress
        ctk.CTkLabel(feedback, textvariable=self._submission_status[mode], text_color=MUTED,
                     font=("Segoe UI", 12), wraplength=650, justify="left",
                     anchor="w").pack(fill="x")

    def _set_submission_progress(self, mode, active):
        progress = self._submission_progress[mode]
        if active and not progress.winfo_manager():
            progress.pack(fill="x", pady=(0, 6), before=progress.master.winfo_children()[-1])
            progress.start()
        elif not active and progress.winfo_manager():
            progress.stop()
            progress.pack_forget()

    def _prepare_next_submission(self, mode, *, reused=False):
        if self._submission_drafts[mode]["lock"].locked():
            return False
        self._submission_drafts[mode] = self._new_submission_draft()
        self._submission_status[mode].set("已载入历史配置" if reused else "")
        self._set_submission_progress(mode, False)
        labels = {"full": "一键开始换品", "lipsync": "开始换口型", "repair": "开始修复画质"}
        self._submission_buttons[mode].configure(text=labels[mode], state="normal")
        return True

    def _view_submission(self, mode):
        identity = self._submission_drafts[mode]["job_id"]
        self.history_filter.set("全部功能")
        self.history_search.set("")
        self._show_page("history")
        self._refresh_history()
        if identity and self.history_tree.exists(identity):
            self.history_tree.selection_set(identity)
            self.history_tree.see(identity)

    def _handle_existing_submission(self, mode):
        draft = self._submission_drafts[mode]
        if draft["lock"].locked() or time.monotonic() < draft.get("not_before", 0):
            return True
        if draft["job_id"]:
            try:
                job = self.desktop_store.job(draft["job_id"])
            except ValueError:
                job = None
            if job and job["status"] in {"queued", "running"}:
                return True
            if job and job["status"] == "paused":
                try:
                    self.tasks.recover(job["id"], self._credential_values())
                except Exception as exc:
                    messagebox.showerror("无法继续任务", str(exc), parent=self.root)
                self._refresh_submission_feedback()
                return True
            if job and job["status"] not in {"success", "failed", "cancelled"}:
                self._view_submission(mode)
                return True
            # Even an instantly completed task must not turn a double click into a new paid job.
            if time.monotonic() - draft["submitted_at"] < 1.0:
                return True
            self._prepare_next_submission(mode)
            return False
        if draft["config"] is not None:
            self._start_worker(mode, draft["config"])
            return True
        return False

    def _refresh_submission_feedback(self):
        for mode, draft in self._submission_drafts.items():
            identity = draft["job_id"]
            if not identity:
                continue
            try:
                job = self.desktop_store.job(identity)
            except ValueError:
                job = None
            status = job["status"] if job else "deleted"
            active = status in {"queued", "running"}
            self._set_submission_progress(mode, active)
            labels = {"queued": "排队中…", "running": "生成中…", "success": "再次生成",
                      "failed": "重新生成", "cancelled": "开始生成", "paused": "继续任务",
                      "attention": "查看任务", "deleted": "开始生成"}
            note = ("已完成 · 成片已保存" if status == "success" else
                    "生成失败，可重新生成；详情见使用记录" if status == "failed" else
                    "任务需要处理，请查看原任务" if status == "attention" else
                    job["stage"] if job else "本次任务记录已删除")
            self._submission_status[mode].set(note)
            cooling = not active and time.monotonic() - draft["submitted_at"] < 1.0
            self._submission_buttons[mode].configure(text=labels.get(status, "查看任务"),
                                                     state="disabled" if active or cooling else "normal")
            if cooling and not draft["cooldown_pending"]:
                draft["cooldown_pending"] = True
                self.root.after(1050, self._refresh_submission_feedback)

    def _start_worker(self, mode: str, values: dict[str, object]) -> None:
        draft = self._submission_drafts[mode]
        if draft["job_id"]:
            return
        if not draft["lock"].acquire(blocking=False):
            return
        try:
            if draft["config"] is None:
                config = copy.deepcopy(values)
                config["vision_model"] = self.prompt_model_var.get().strip() or DEFAULT_PROMPT_MODEL
                draft["config"] = config
            self._submission_buttons[mode].configure(text="正在提交…", state="disabled")
            self._submission_status[mode].set("正在提交任务")
            self._set_submission_progress(mode, True)
            draft["submitted_at"] = time.monotonic()
            self.root.update_idletasks()
            identity = self.tasks.submit(mode, draft["config"], self._credential_values(),
                                         request_id=draft["request_id"])
            draft["job_id"] = identity
            draft["error"] = ""
        except Exception as exc:
            draft["error"] = str(exc)
            self._set_submission_progress(mode, False)
            self._submission_status[mode].set("提交尚未确认，重试会核对原任务")
            self._submission_buttons[mode].configure(text="重试提交", state="normal")
            messagebox.showerror("创建任务失败", str(exc), parent=self.root)
            return
        finally:
            draft["lock"].release()
        self.status_var.set("任务已提交")
        self._refresh_history()
        # Keep this draft locked through the modal loop: queued clicks and task
        # notifications must not turn this submission into another paid task.
        draft["lock"].acquire()
        try:
            messagebox.showinfo("任务已提交", "任务已加入使用记录，请进入使用记录查看进度。\n\n点击确定后可以继续生成其他视频。", parent=self.root)
        finally:
            draft["lock"].release()
        if self._submission_drafts[mode] is draft:
            self._prepare_next_submission(mode)
            self._submission_drafts[mode]["not_before"] = draft["submitted_at"] + 1.0
            self._submission_status[mode].set("任务已提交，请在使用记录查看进度")

    def _resume_task(self) -> None:
        self._show_page("history")
        selected = filedialog.askopenfilename(parent=self.root, title="导入已有任务记录",
            initialdir=self.workspace_output_dir_var.get(), filetypes=[("视频任务记录", "*.video-task.json")])
        if selected:
            try:
                identity = self.desktop_store.import_record(selected, explicit=True)
                self._refresh_history()
                self.history_tree.selection_set(identity)
            except Exception as exc:
                messagebox.showerror("导入失败", str(exc), parent=self.root)

    def _drain_events(self) -> None:
        if self._closed:
            return
        try:
            while True:
                if self._closed:
                    return
                kind, payload = self.events.get_nowait()
                if kind == "tasks_changed":
                    self._refresh_history()
                    self._refresh_submission_feedback()
                    self._ai_jobs_changed()
                    if self.current_page == "library": self._refresh_library()
                elif kind == "maintenance_event":
                    self._handle_maintenance_event(payload)
                elif kind == "director_event":
                    self._handle_director_event(payload)
                elif kind == "history_sync":
                    self._finish_history_sync(payload)
                elif kind == "fish_event":
                    self._handle_fish_event(payload)
                elif kind == "ai_image_event":
                    self._handle_ai_image_event(payload)
                elif kind == "assistant_event":
                    self._handle_assistant_event(payload)
                elif kind == "voice_event":
                    self._handle_voice_event(payload)
                elif kind == "assets_changed":
                    self._refresh_library()
                    errors = payload.get("errors", []) if isinstance(payload, dict) else payload
                    duplicates = payload.get("duplicates", 0) if isinstance(payload, dict) else 0
                    imported = payload.get("imported", 0) if isinstance(payload, dict) else 0
                    note = f"新增 {imported} 个素材"
                    if duplicates: note += f"，{duplicates} 个已存在并保留原分组，可选中后批量移动"
                    if errors: note += f"，{len(errors)} 个导入失败"
                    self.asset_target_var.set(note)
                    if errors: messagebox.showerror("导入结果", "\n".join(errors), parent=self.root)
                elif kind == "canvas_host_action":
                    if payload == "fullscreen": self._toggle_fullscreen()
                    elif payload == "leave_fullscreen": self._leave_fullscreen()
                elif kind == "status":
                    self.status_var.set(str(payload))
                elif kind == "tray_ready":
                    if payload is self.tray and not self._closed and not self._exit_in_progress:
                        if self._tray_hide_pending and self.tray.is_ready:
                            self._tray_hide_pending = False
                            self.root.withdraw()
                elif kind == "tray_failed":
                    self._tray_failed(*payload)
                elif kind == "tray_open":
                    if payload is self.tray and not self._closed and not self._exit_in_progress:
                        self._restore_window()
                elif kind == "tray_exit":
                    if payload is self.tray and not self._closed and not self._exit_in_progress:
                        self._request_exit()
                elif kind == "source_loaded":
                    self._handle_source_loaded(payload)  # type: ignore[arg-type]
                elif kind == "source_load_failed":
                    self._handle_source_load_failed(payload)  # type: ignore[arg-type]
                elif kind == "reference_loaded":
                    self._handle_reference_loaded(payload)  # type: ignore[arg-type]
                elif kind == "reference_load_failed":
                    self._handle_reference_load_failed(payload)  # type: ignore[arg-type]
                elif kind == "lipsync_video_loaded":
                    self._handle_lipsync_media("video", payload)  # type: ignore[arg-type]
                elif kind == "lipsync_video_failed":
                    self._handle_lipsync_media("video", payload)  # type: ignore[arg-type]
                elif kind == "lipsync_audio_loaded":
                    self._handle_lipsync_media("audio", payload)  # type: ignore[arg-type]
                elif kind == "lipsync_audio_failed":
                    self._handle_lipsync_media("audio", payload)  # type: ignore[arg-type]
                elif kind == "repair_video_loaded":
                    self._handle_repair_media(payload)  # type: ignore[arg-type]
                elif kind == "repair_video_failed":
                    self._handle_repair_media(payload)  # type: ignore[arg-type]
        except queue.Empty:
            pass
        if not self._closed:
            self.root.after(100, self._drain_events)

    def _toggle_keys(self) -> None:
        show = "" if self.show_keys_var.get() else "●"
        for entry in self.key_entries:
            entry.configure(show=show)

    def _update_api_status(self) -> None:
        values = self._credential_values()
        prompt_ok = bool(values.get("DEEPSEEK_API_KEY"))
        active = [self.provider_vars[p] for p in self.runner.registry.providers]
        total = len(active)
        configured = sum(bool(values.get(str(data["key_env"]))) for data in active)
        self.api_status_var.set(
            f"工作区：已配置；提示词 Key：{'已配置' if prompt_ok else '未配置'}；"
            f"生成服务 Key：{configured}/{total} 已配置"
        )

    def _save_credentials(self) -> None:
        values = self._credential_values()
        workspace_text = values.get("WORKSPACE_OUTPUT_DIR", "").strip()
        if not workspace_text:
            messagebox.showerror("工作区无效", "请选择成片保存目录。", parent=self.root)
            return
        workspace = Path(workspace_text).expanduser()
        for name, value in values.items():
            if "BASE" in name and value:
                parsed = urlparse(value)
                if parsed.scheme != "https" or not parsed.netloc:
                    messagebox.showerror("API 地址无效", f"{name} 必须是 HTTPS 地址。", parent=self.root)
                    return
        try:
            workspace.mkdir(parents=True, exist_ok=True)
            self.store.save(values)
            self.saved_credentials = self.store.load()
        except (OSError, CredentialStoreError) as exc:
            messagebox.showerror("保存失败", str(exc), parent=self.root)
            return
        self._update_api_status()
        messagebox.showinfo("保存成功", "工作区和 API Key 设置已保存。", parent=self.root)

    def _import_existing(self) -> None:
        # Import only this product's own configuration. Prompt-Skill and Windows
        # environment credentials belong to different credential domains.
        merged = read_env_values(SKILL_ROOT / ".env")
        prompt_key = merged.get("DEEPSEEK_API_KEY")
        if prompt_key:
            self.prompt_key_var.set(prompt_key)
        if merged.get("DEEPSEEK_API_BASE"):
            self.prompt_base_var.set(merged["DEEPSEEK_API_BASE"])
        if merged.get("DEEPSEEK_PROMPT_MODEL"):
            self.prompt_model_var.set(merged["DEEPSEEK_PROMPT_MODEL"])
        for provider, data in self.provider_vars.items():
            spec = self.runner.credentials.providers[provider]
            base_env, key_env = str(data["base_env"]), str(data["key_env"])
            base = merged.get(base_env) or merged.get(str(spec.get("legacy_api_base_env", "")))
            key = merged.get(key_env) or merged.get(str(spec.get("legacy_api_key_env", "")))
            if base:
                data["base_var"].set(base)  # type: ignore[union-attr]
            if key:
                data["key_var"].set(key)  # type: ignore[union-attr]
        self._update_api_status()
        messagebox.showinfo(
            "导入完成", "已读入界面；点击“安全保存”后才会写入工作台的加密存储。", parent=self.root
        )

    def _clear_credentials(self) -> None:
        if not messagebox.askyesno(
            "确认清除",
            "只清除工作台的加密配置，不删除两个 Skill 原有的 .env。是否继续？",
            icon="warning",
            parent=self.root,
        ):
            return
        try:
            self.store.clear()
        except OSError as exc:
            messagebox.showerror("清除失败", str(exc), parent=self.root)
            return
        self.prompt_key_var.set("")
        self.prompt_base_var.set(DEFAULT_PROMPT_BASE)
        self.prompt_model_var.set(DEFAULT_PROMPT_MODEL)
        self.workspace_output_dir_var.set(str((PROJECT_ROOT / "output").resolve()))
        self.model_var.set(DEFAULT_VIDEO_MODEL)
        self.video_route_var.set(self.route_labels[DEFAULT_VIDEO_MODEL])
        for provider, data in self.provider_vars.items():
            data["key_var"].set("")  # type: ignore[union-attr]
            data["base_var"].set(self.runner.credentials.providers[provider]["default_api_base"])  # type: ignore[union-attr]
        self._update_api_status()
        messagebox.showinfo("已清除", "工作台保存的加密配置已清除。", parent=self.root)

    def _on_close(self) -> None:
        if self._closed or self._exit_in_progress:
            return
        self._tray_hide_pending = True
        if self.tray and self.tray.is_ready:
            self._tray_hide_pending = False
            self.root.withdraw()
        else:
            self.status_var.set("正在建立系统托盘，准备好后窗口将在后台运行")
            self._ensure_tray()

    def _ensure_tray(self) -> None:
        if self._closed or self._exit_in_progress or self.tray is not None:
            return
        tray = self.tray = DesktopTray(self.events, LOGO_ICO)
        try:
            tray.start()
        except Exception as exc:
            self._tray_failed(tray, str(exc))
            return
        self.root.after(5000, lambda: self._tray_start_timeout(tray))

    def _tray_start_timeout(self, tray) -> None:
        if tray is self.tray and not tray.is_ready and not self._closed:
            self._tray_failed(tray, "系统托盘启动超时。")

    def _restore_window(self) -> None:
        self._tray_hide_pending = False
        self.root.deiconify()
        self.root.lift()

    def _tray_failed(self, tray, error) -> None:
        if tray is not self.tray or self._closed or self._exit_in_progress:
            return
        # Each controller is one attempt. Clearing it also makes late callbacks
        # from a failed/timed-out attempt harmless after the next retry.
        self.tray = None
        tray.stop()
        self._restore_window()
        self.status_var.set("系统托盘不可用，窗口保持打开；再次关闭可重试。")
        if messagebox.askyesno("系统托盘无法启动",
                               f"{error}\n\n窗口保持打开，任务继续运行。是否退出软件？\n选择否可继续使用，再次关闭窗口可重试托盘。",
                               parent=self.root):
            self._request_exit()

    def _request_exit(self) -> None:
        if self._closed or self._exit_in_progress:
            return
        if getattr(self, "_assistant_busy", False):
            messagebox.showinfo("正在处理学习资料", "请等学习或反馈保存完成再退出；关闭窗口可继续在后台处理。", parent=self.root)
            return
        if getattr(self, "_maintenance_busy", False):
            messagebox.showinfo("正在处理数据", "请等当前迁移或下载完成后再退出。", parent=self.root)
            return
        if getattr(self, "ai_image_busy", False) or getattr(self, "ai_model_busy", False):
            messagebox.showinfo("图片正在处理", "请等当前看图或上传完成再退出；关闭窗口可继续在后台处理。", parent=self.root)
            return
        self._ai_save()
        if getattr(self, "voice_busy", False) or getattr(self, "voice_clone_busy", False):
            self._restore_window()
            messagebox.showinfo("声音正在处理", "请等当前声音处理完成后退出，以便保存生成结果。关闭窗口可继续在后台处理。", parent=self.root)
            return
        self._exit_in_progress = True
        self._restore_window()
        if self.tasks.active or self.tasks.pending:
            if not messagebox.askyesno("退出素材万象", "还有任务在执行或排队。退出后本地工作暂停，服务商已受理的任务可能继续。重新打开后可在使用记录恢复，是否退出？", parent=self.root):
                self._exit_in_progress = False
                return
        def after_canvas(saved):
            if not saved:
                self._exit_in_progress = False
                self._update_on_exit = None
                self._show_page("canvas")
                messagebox.showerror("画布尚未保存", "请在创作画布处理保存失败的问题后再退出。", parent=self.root)
                return
            if getattr(self, "_update_on_exit", None):
                from app_updates import backup_for_update, start_installer
                try:
                    if not self._maintenance_idle():
                        self._exit_in_progress = False
                        self._update_on_exit = None
                        return
                    installer, manifest = self._update_on_exit
                    backup_for_update(self.library_transfer, manifest["version"])
                    start_installer(installer, manifest, self.desktop_store.root)
                except Exception as exc:
                    self._exit_in_progress = False
                    self._update_on_exit = None
                    messagebox.showerror("更新未启动", str(exc), parent=self.root)
                    return
            self._closed = True
            self._shutdown_errors = []
            cleanup = ([self.tray.stop] if self.tray else []) + [
                self.tasks.stop, self._ai_close_display, self.fish_audio_player.stop, self.voice_player.stop, self.root.destroy]
            for close in cleanup:
                try:
                    close()
                except Exception as exc:
                    self._shutdown_errors.append(str(exc))
        try:
            self.canvas_window.request_stop(after_canvas)
        except Exception as exc:
            self._exit_in_progress = False
            self._update_on_exit = None
            messagebox.showerror("画布尚未保存", f"画布关闭失败：{exc}", parent=self.root)


def main() -> int:
    if "--desktop-demo" in sys.argv:
        from canvas_demo import run_desktop_demo
        data_root = sys.argv[sys.argv.index("--desktop-demo-data-root") + 1] if "--desktop-demo-data-root" in sys.argv else None
        return run_desktop_demo(data_root)
    if len(sys.argv) >= 3 and sys.argv[1] == "--canvas-child":
        from canvas_window import run_child
        return run_child(sys.argv[2], embedded="--canvas-embedded" in sys.argv)
    if "--canvas-demo" in sys.argv:
        from canvas_demo import run_demo
        data_root = sys.argv[sys.argv.index("--canvas-demo-data-root") + 1] if "--canvas-demo-data-root" in sys.argv else None
        return run_demo(data_root, browser_only="--canvas-demo-browser-only" in sys.argv)
    root = ctk.CTk()
    try:
        MaterialUniverseApp(root)
    except Exception as exc:
        messagebox.showerror("工作台启动失败", str(exc), parent=root)
        root.destroy()
        return 1
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
