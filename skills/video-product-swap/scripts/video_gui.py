from __future__ import annotations

import json
import os
import queue
import ctypes
import shutil
import subprocess
import sys
import threading
import uuid
from ctypes import wintypes
from datetime import datetime
from pathlib import Path
from tkinter import END, BooleanVar, StringVar, filedialog, messagebox
from urllib.parse import urlparse

import customtkinter as ctk

from gui_support import build_generate_command, build_prompt_command, references_for_swap
from media_inspection import VideoInfo, image_thumbnail, probe_video, video_poster
from model_runner.runner import ModelRunner
from secure_credentials import CredentialStoreError, SecureCredentialStore, read_env_values, redact


SKILL_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = SKILL_ROOT.parents[1]
GENERATE_SCRIPT = SKILL_ROOT / "scripts" / "generate_video.py"
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
PROCESS_SUSPEND_RESUME = 0x0800
DEFAULT_PROMPT_BASE = "https://api.deepseek.com"
DEFAULT_PROMPT_MODEL = "deepseek-v4-flash-vision-exp"
DEFAULT_VIDEO_MODEL = "wan3.0-video"
VIDEO_ROUTE_LABELS = {
    "wan3.0-video": "中转站 · Wan3 视频参考",
    "wan-official-videoedit": "Wan 官方 · Wan2.7 VideoEdit",
}
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

    # Keep retired prompt-service aliases blank so the child cannot use stale credentials.
    env["GEMINI_RELAY_API_KEY"] = ""
    env["GEMINI_API_KEY"] = ""
    for spec in providers.values():
        for field in ("legacy_api_base_env", "legacy_api_key_env"):
            name = str(spec.get(field, "")).strip()
            if name:
                env[name] = ""
    env["PYTHONUTF8"] = "1"
    return env


def set_process_suspended(process_id: int, suspended: bool) -> bool:
    """Suspend or resume every thread in a Windows process."""
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


def locate_prompt_script() -> Path:
    candidates: list[Path] = []
    override = os.environ.get("AI_VIDEO_PROMPT_SKILL_ROOT", "").strip()
    if override:
        candidates.append(Path(override) / "scripts" / "generate_swap_prompt.py")
    candidates.extend(
        [
            SKILL_ROOT.parent / "ai-video-swap-prompt-generator" / "scripts" / "generate_swap_prompt.py",
            Path.home() / ".codex" / "skills" / "ai-video-swap-prompt-generator" / "scripts" / "generate_swap_prompt.py",
        ]
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    raise FileNotFoundError(
        "找不到提示词分析 Skill。请安装 ai-video-swap-prompt-generator，"
        "或设置 AI_VIDEO_PROMPT_SKILL_ROOT。"
    )


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
        was_paused, self.paused = self.paused, False
        if process is not None and process.poll() is None:
            if was_paused:
                set_process_suspended(process.pid, False)
            process.terminate()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
        self.poster.place(relx=0.5, rely=0.5, anchor="center", relwidth=1, relheight=1)
        self.state_callback(False, False)


class MaterialUniverseApp:
    def __init__(self, root: ctk.CTk) -> None:
        self.root = root
        self.root.title("素材万象")
        self.root.geometry("1220x860")
        self.root.minsize(1080, 740)
        self.root.configure(fg_color=BG)
        self.root.option_add("*Font", ("Segoe UI", 10))

        self.runner = ModelRunner(SKILL_ROOT)
        self.models = self.runner.list_models()
        self.model_map = {item["model"]: item for item in self.models}
        self.prompt_script = locate_prompt_script()
        self.prompt_skill_root = self.prompt_script.parents[1]
        self.store = SecureCredentialStore()
        try:
            self.saved_credentials = self.store.load()
            store_error = ""
        except CredentialStoreError as exc:
            self.saved_credentials = {}
            store_error = str(exc)

        preferred = self.saved_credentials.get("VIDEO_MODEL", DEFAULT_VIDEO_MODEL)
        if preferred not in VIDEO_ROUTE_LABELS or preferred not in self.model_map:
            preferred = DEFAULT_VIDEO_MODEL

        self.events: queue.Queue[tuple[str, object]] = queue.Queue()
        self.running = False
        self.generated_context: tuple[str, str, str] | None = None
        self.last_output: Path | None = None
        self.last_video_url: str | None = None
        self.video_info: VideoInfo | None = None
        self.video_preview_ctk_image: ctk.CTkImage | None = None
        self.product_preview_ctk_image: ctk.CTkImage | None = None
        self.key_entries: list[ctk.CTkEntry] = []
        self.provider_vars: dict[str, dict[str, object]] = {}

        self.model_var = StringVar(value=preferred)
        self.video_route_var = StringVar(value=VIDEO_ROUTE_LABELS[preferred])
        self.source_video_var = StringVar()
        self.reference_image_var = StringVar()
        self.public_image_url_var = StringVar()
        self.aspect_ratio_var = StringVar(value="9:16")
        self.duration_var = StringVar(value="")
        self.resolution_var = StringVar(value="480p")
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
        self.public_url_help_var = StringVar()
        self.status_var = StringVar(value="就绪")
        self.api_status_var = StringVar(value=store_error or "API Key 安全存储就绪")
        self.show_keys_var = BooleanVar(value=False)

        self.prompt_base_var = StringVar(
            value=self.saved_credentials.get("DEEPSEEK_API_BASE", DEFAULT_PROMPT_BASE)
        )
        self.prompt_model_var = StringVar(
            value=self.saved_credentials.get("DEEPSEEK_PROMPT_MODEL", DEFAULT_PROMPT_MODEL)
        )
        self.prompt_key_var = StringVar(
            value=self.saved_credentials.get("DEEPSEEK_API_KEY", "")
        )

        self._configure_style()
        self._build_ui()
        self._update_model_help()
        self._update_api_status()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(100, self._drain_events)

    def _configure_style(self) -> None:
        ctk.set_appearance_mode("light")
        ctk.set_default_color_theme("blue")

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
        mark = ctk.CTkLabel(
            brand,
            text="万",
            width=38,
            height=38,
            corner_radius=12,
            fg_color=TEXT,
            text_color="white",
            font=("Segoe UI", 17, "bold"),
        )
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
        self.editor_nav = self._nav_button(
            sidebar, "AI 视频换品", lambda: self._show_page("editor")
        )
        self.editor_nav.pack(fill="x", padx=14, pady=3)
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
            text="固定提示词 · 单次提交\n结果需人工画面验收",
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
        self.api_tab = ctk.CTkFrame(content, fg_color=BG, corner_radius=0)
        self.edit_tab.grid(row=0, column=0, sticky="nsew")
        self.api_tab.grid(row=0, column=0, sticky="nsew")
        self._build_edit_tab()
        self._build_api_tab()
        self._show_page("editor")
        self.root.after(150, self._scroll_pages_to_top)

    def _scroll_pages_to_top(self) -> None:
        for page in (self.editor_page, self.api_page):
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
        active, inactive = (self.editor_nav, self.api_nav) if page == "editor" else (
            self.api_nav,
            self.editor_nav,
        )
        active.configure(fg_color=SOFT_BLUE, text_color=BLUE)
        inactive.configure(fg_color="transparent", text_color=TEXT)
        (self.edit_tab if page == "editor" else self.api_tab).tkraise()

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

    def _build_edit_tab_legacy(self) -> None:
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
            text="导入素材，确认产品尺寸关系，然后一次完成分析与生成。",
            text_color=MUTED,
            font=("Segoe UI", 13),
            anchor="w",
        ).pack(anchor="w", pady=(4, 22))

        _, settings = self._card(page, "输出设置", "选择模型、画面比例、时长与清晰度。")
        settings.grid_columnconfigure((0, 1, 2, 3), weight=1, uniform="spec")
        labels = ("视频模型", "画面比例", "视频秒数", "分辨率")
        for column, label in enumerate(labels):
            self._field_label(settings, label).grid(row=0, column=column, sticky="w", padx=(0, 12))
        self.model_box = ctk.CTkComboBox(
            settings,
            variable=self.model_var,
            values=[item["model"] for item in self.models],
            height=40,
            corner_radius=10,
            border_color=BORDER,
            button_color="#F0F0F2",
            button_hover_color="#E5E5EA",
            fg_color="#FBFBFD",
            text_color=TEXT,
            dropdown_fg_color=CARD,
            command=lambda _value: self._update_model_help(),
        )
        self.model_box.grid(row=1, column=0, sticky="ew", padx=(0, 12), pady=(6, 0))
        self.aspect_box = ctk.CTkOptionMenu(
            settings,
            variable=self.aspect_ratio_var,
            values=["9:16", "16:9", "1:1", "4:3", "3:4"],
            height=40,
            corner_radius=10,
            fg_color="#F0F0F2",
            button_color="#E5E5EA",
            button_hover_color="#D1D1D6",
            text_color=TEXT,
        )
        self.aspect_box.grid(row=1, column=1, sticky="ew", padx=(0, 12), pady=(6, 0))
        self.duration_spin = self._entry(settings, self.duration_var)
        self.duration_spin.grid(row=1, column=2, sticky="ew", padx=(0, 12), pady=(6, 0))
        self.resolution_box = ctk.CTkOptionMenu(
            settings,
            variable=self.resolution_var,
            values=["480p", "720p", "1080p"],
            height=40,
            corner_radius=10,
            fg_color="#F0F0F2",
            button_color="#E5E5EA",
            button_hover_color="#D1D1D6",
            text_color=TEXT,
        )
        self.resolution_box.grid(row=1, column=3, sticky="ew", pady=(6, 0))
        ctk.CTkLabel(
            settings,
            textvariable=self.model_help_var,
            text_color=MUTED,
            font=("Segoe UI", 11),
            anchor="w",
            justify="left",
            wraplength=780,
        ).grid(row=2, column=0, columnspan=4, sticky="w", pady=(10, 0))

        _, inputs = self._card(page, "素材", "本地文件只在当前电脑读取。")
        inputs.grid_columnconfigure(0, weight=1)
        self.source_entry, self.source_button = self._file_row(
            inputs, 0, "原视频", self.source_video_var, "选择视频", self._choose_source_video
        )
        self.reference_entry, self.reference_button = self._file_row(
            inputs, 1, "产品参考图", self.reference_image_var, "选择图片", self._choose_reference_image
        )
        self._field_label(inputs, "参考图公网 URL").grid(row=4, column=0, sticky="w", pady=(14, 0))
        self.public_url_entry = self._entry(inputs, self.public_image_url_var)
        self.public_url_entry.grid(row=5, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        ctk.CTkLabel(
            inputs,
            textvariable=self.public_url_help_var,
            text_color=ORANGE,
            font=("Segoe UI", 11),
            anchor="w",
            justify="left",
            wraplength=790,
        ).grid(row=6, column=0, columnspan=2, sticky="w", pady=(8, 0))

        _, relation = self._card(
            page,
            "尺寸关系",
            "只整理你的原话，不会根据画面自行猜测产品尺寸。",
        )
        self.relation_text = ctk.CTkTextbox(
            relation,
            height=86,
            corner_radius=12,
            border_width=1,
            border_color=BORDER,
            fg_color="#FBFBFD",
            text_color=TEXT,
            font=("Segoe UI", 12),
        )
        self.relation_text.pack(fill="x")
        self.relation_text.insert("1.0", "参考图产品高度约为原视频产品的三分之二，宽度一致")

        _, output = self._card(page, "输出位置")
        output.grid_columnconfigure(0, weight=1)
        self.output_entry = self._entry(output, self.output_var)
        self.output_entry.grid(row=0, column=0, sticky="ew", padx=(0, 10))
        self.output_button = ctk.CTkButton(
            output,
            text="选择位置",
            command=self._choose_output,
            width=104,
            height=40,
            corner_radius=10,
            fg_color="#F0F0F2",
            hover_color="#E5E5EA",
            text_color=TEXT,
        )
        self.output_button.grid(row=0, column=1)

        _, prompt = self._card(page, "固定提示词", "由提示词分析 Skill 生成并原样传递。")
        self.prompt_text = ctk.CTkTextbox(
            prompt,
            height=92,
            corner_radius=12,
            border_width=1,
            border_color=BORDER,
            fg_color="#FBFBFD",
            text_color=TEXT,
            font=("Segoe UI", 12),
            state="disabled",
        )
        self.prompt_text.pack(fill="x")

        actions = ctk.CTkFrame(page, fg_color="transparent")
        actions.pack(fill="x", pady=(2, 12))
        self.analyze_button = self._secondary_button(actions, "只生成提示词", self._start_analysis)
        self.analyze_button.pack(side="left")
        self.check_button = self._secondary_button(actions, "检查模型输入", self._start_check)
        self.check_button.pack(side="left", padx=8)
        self.full_button = ctk.CTkButton(
            actions,
            text="一键开始换品",
            command=self._start_full_pipeline,
            width=148,
            height=44,
            corner_radius=22,
            fg_color=BLUE,
            hover_color=BLUE_HOVER,
            text_color="white",
            font=("Segoe UI", 13, "bold"),
        )
        self.full_button.pack(side="right")
        self.generate_button = self._secondary_button(
            actions, "使用当前提示词生成", self._start_generate
        )
        self.generate_button.pack(side="right", padx=8)

        status_card = ctk.CTkFrame(
            page, fg_color=CARD, corner_radius=16, border_width=1, border_color=BORDER
        )
        status_card.pack(fill="x", pady=(0, 14))
        ctk.CTkLabel(
            status_card,
            textvariable=self.status_var,
            text_color=TEXT,
            font=("Segoe UI", 12, "bold"),
        ).pack(side="left", padx=18, pady=14)
        self.progress = ctk.CTkProgressBar(
            status_card, width=180, height=5, corner_radius=3, progress_color=BLUE
        )
        self.progress.set(0)
        self.progress.pack(side="left", padx=8)
        self.open_button = self._secondary_button(
            status_card, "打开输出文件夹", self._open_output_folder, width=128
        )
        self.open_button.configure(state="disabled")
        self.open_button.pack(side="right", padx=12, pady=8)

        ctk.CTkLabel(
            page,
            text="生成成功仅代表技术执行完成；换品自然度、人物动作和画面质量仍需人工验收。",
            text_color=ORANGE,
            font=("Segoe UI", 11),
            anchor="w",
        ).pack(anchor="w", pady=(0, 8))
        self.log_text = ctk.CTkTextbox(
            page,
            height=100,
            corner_radius=14,
            border_width=1,
            border_color=BORDER,
            fg_color="#FBFBFD",
            text_color=MUTED,
            font=("Consolas", 10),
            state="disabled",
        )
        self.log_text.pack(fill="x", pady=(0, 18))

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

    def _build_edit_tab_guided(self) -> None:
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
            text="先选择素材，系统会自动识别原视频规格，再完成提示词分析与视频换品。",
            text_color=MUTED,
            font=("Segoe UI", 13),
            anchor="w",
        ).pack(anchor="w", pady=(4, 22))

        _, media = self._card(page, "1  素材", "视频可在软件内预览；产品图以缩略图显示。")
        media.grid_columnconfigure((0, 1), weight=1, uniform="preview")

        video_panel = ctk.CTkFrame(media, fg_color="#F7F7F9", corner_radius=14)
        video_panel.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        ctk.CTkLabel(
            video_panel,
            text="原视频",
            text_color=TEXT,
            font=("Segoe UI", 13, "bold"),
        ).pack(anchor="w", padx=14, pady=(13, 8))
        self.video_stage = ctk.CTkFrame(
            video_panel, height=250, fg_color="#111111", corner_radius=12
        )
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
        self.video_player = EmbeddedVideoPlayer(
            self.root, self.video_stage, self.video_poster_label
        )
        self.video_player.state_callback = self._video_player_state
        video_controls = ctk.CTkFrame(video_panel, fg_color="transparent")
        video_controls.pack(fill="x", padx=14, pady=(10, 4))
        self.source_button = self._secondary_button(
            video_controls, "选择视频", self._choose_source_video, width=100
        )
        self.source_button.pack(side="left")
        self.play_button = self._secondary_button(
            video_controls, "播放", self.video_player.toggle, width=78
        )
        self.play_button.configure(textvariable=self.play_button_var, state="disabled")
        self.play_button.pack(side="left", padx=6)
        self.replay_button = self._secondary_button(
            video_controls, "重播", self.video_player.replay, width=70
        )
        self.replay_button.configure(state="disabled")
        self.replay_button.pack(side="left")
        self.system_player_button = self._secondary_button(
            video_controls, "系统播放器", self.video_player.open_system_player, width=108
        )
        self.system_player_button.configure(state="disabled")
        self.system_player_button.pack(side="right")
        ctk.CTkLabel(
            video_panel,
            textvariable=self.source_summary_var,
            text_color=MUTED,
            font=("Segoe UI", 11),
            anchor="w",
        ).pack(fill="x", padx=15, pady=(5, 14))

        image_panel = ctk.CTkFrame(media, fg_color="#F7F7F9", corner_radius=14)
        image_panel.grid(row=0, column=1, sticky="nsew", padx=(8, 0))
        ctk.CTkLabel(
            image_panel,
            text="产品参考图",
            text_color=TEXT,
            font=("Segoe UI", 13, "bold"),
        ).pack(anchor="w", padx=14, pady=(13, 8))
        self.image_stage = ctk.CTkFrame(
            image_panel, height=250, fg_color="#EFEFF4", corner_radius=12
        )
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
        image_controls = ctk.CTkFrame(image_panel, fg_color="transparent")
        image_controls.pack(fill="x", padx=14, pady=(10, 4))
        self.reference_button = self._secondary_button(
            image_controls, "选择图片", self._choose_reference_image, width=100
        )
        self.reference_button.pack(side="left")
        ctk.CTkLabel(
            image_panel,
            textvariable=self.image_summary_var,
            text_color=MUTED,
            font=("Segoe UI", 11),
            anchor="w",
        ).pack(fill="x", padx=15, pady=(5, 14))

        self.public_url_container = ctk.CTkFrame(media, fg_color="transparent")
        self.public_url_container.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(14, 0))
        self.public_url_container.grid_columnconfigure(0, weight=1)
        self._field_label(self.public_url_container, "OmniFlash 公网参考图 URL").grid(
            row=0, column=0, sticky="w"
        )
        self.public_url_entry = self._entry(self.public_url_container, self.public_image_url_var)
        self.public_url_entry.grid(row=1, column=0, sticky="ew", pady=(6, 0))
        ctk.CTkLabel(
            self.public_url_container,
            textvariable=self.public_url_help_var,
            text_color=ORANGE,
            font=("Segoe UI", 11),
            anchor="w",
        ).grid(row=2, column=0, sticky="w", pady=(6, 0))

        _, relation = self._card(
            page,
            "2  尺寸关系",
            "描述参考图产品与原视频产品的体积或外形关系；系统不会自行估算。",
        )
        self.relation_text = ctk.CTkTextbox(
            relation,
            height=88,
            corner_radius=12,
            border_width=1,
            border_color=BORDER,
            fg_color="#FBFBFD",
            text_color=TEXT,
            font=("Segoe UI", 12),
        )
        self.relation_text.pack(fill="x")

        _, settings = self._card(
            page,
            "3  输出设置",
            "默认使用 Wan3 视频参考模型、480p；秒数与比例会跟随原视频自动填写。",
        )
        settings.grid_columnconfigure((0, 1, 2, 3), weight=1, uniform="spec")
        for column, label in enumerate(("视频模型", "画面比例", "视频秒数", "分辨率")):
            self._field_label(settings, label).grid(
                row=0, column=column, sticky="w", padx=(0, 12)
            )
        self.model_box = ctk.CTkComboBox(
            settings,
            variable=self.model_var,
            values=[item["model"] for item in self.models],
            height=40,
            corner_radius=10,
            border_color=BORDER,
            button_color="#F0F0F2",
            button_hover_color="#E5E5EA",
            fg_color="#FBFBFD",
            text_color=TEXT,
            dropdown_fg_color=CARD,
            command=lambda _value: self._update_model_help(),
        )
        self.model_box.grid(row=1, column=0, sticky="ew", padx=(0, 12), pady=(6, 0))
        self.aspect_box = ctk.CTkOptionMenu(
            settings,
            variable=self.aspect_ratio_var,
            values=["9:16", "16:9", "1:1", "4:3", "3:4"],
            height=40,
            corner_radius=10,
            fg_color="#F0F0F2",
            button_color="#E5E5EA",
            button_hover_color="#D1D1D6",
            text_color=TEXT,
        )
        self.aspect_box.grid(row=1, column=1, sticky="ew", padx=(0, 12), pady=(6, 0))
        self.duration_spin = self._entry(settings, self.duration_var)
        self.duration_spin.grid(row=1, column=2, sticky="ew", padx=(0, 12), pady=(6, 0))
        self.resolution_box = ctk.CTkOptionMenu(
            settings,
            variable=self.resolution_var,
            values=["480p", "720p", "1080p"],
            height=40,
            corner_radius=10,
            fg_color="#F0F0F2",
            button_color="#E5E5EA",
            button_hover_color="#D1D1D6",
            text_color=TEXT,
        )
        self.resolution_box.grid(row=1, column=3, sticky="ew", pady=(6, 0))
        ctk.CTkLabel(
            settings,
            textvariable=self.model_help_var,
            text_color=MUTED,
            font=("Segoe UI", 11),
            anchor="w",
            justify="left",
            wraplength=780,
        ).grid(row=2, column=0, columnspan=4, sticky="w", pady=(10, 0))

        _, output = self._card(page, "4  输出位置")
        output.grid_columnconfigure(0, weight=1)
        self.output_entry = self._entry(output, self.output_var)
        self.output_entry.grid(row=0, column=0, sticky="ew", padx=(0, 10))
        self.output_button = self._secondary_button(
            output, "选择位置", self._choose_output, width=104
        )
        self.output_button.grid(row=0, column=1)

        _, prompt = self._card(page, "5  提示词", "由提示词分析 Skill 生成并原样传递。")
        self.prompt_text = ctk.CTkTextbox(
            prompt,
            height=94,
            corner_radius=12,
            border_width=1,
            border_color=BORDER,
            fg_color="#FBFBFD",
            text_color=TEXT,
            font=("Segoe UI", 12),
            state="disabled",
        )
        self.prompt_text.pack(fill="x")

        _, swap = self._card(
            page,
            "6  换品",
            "先生成并检查提示词，或直接运行完整流程；正式生成只提交一次。",
        )
        actions = ctk.CTkFrame(swap, fg_color="transparent")
        actions.pack(fill="x")
        self.analyze_button = self._secondary_button(
            actions, "只生成提示词", self._start_analysis
        )
        self.analyze_button.pack(side="left")
        self.check_button = self._secondary_button(
            actions, "检查模型输入", self._start_check
        )
        self.check_button.pack(side="left", padx=8)
        self.generate_button = self._secondary_button(
            actions, "使用当前提示词生成", self._start_generate, width=164
        )
        self.generate_button.pack(side="right", padx=8)
        self.full_button = ctk.CTkButton(
            actions,
            text="一键开始换品",
            command=self._start_full_pipeline,
            width=148,
            height=44,
            corner_radius=22,
            fg_color=BLUE,
            hover_color=BLUE_HOVER,
            text_color="white",
            font=("Segoe UI", 13, "bold"),
        )
        self.full_button.pack(side="right")

        status = ctk.CTkFrame(swap, fg_color="#F7F7F9", corner_radius=13)
        status.pack(fill="x", pady=(14, 0))
        ctk.CTkLabel(
            status,
            textvariable=self.status_var,
            text_color=TEXT,
            font=("Segoe UI", 12, "bold"),
        ).pack(side="left", padx=16, pady=13)
        self.progress = ctk.CTkProgressBar(
            status, width=170, height=5, corner_radius=3, progress_color=BLUE
        )
        self.progress.set(0)
        self.progress.pack(side="left", padx=8)
        self.open_button = self._secondary_button(
            status, "打开输出文件夹", self._open_output_folder, width=128
        )
        self.open_button.configure(state="disabled")
        self.open_button.pack(side="right", padx=10, pady=7)
        ctk.CTkLabel(
            swap,
            text="生成成功仅代表技术执行完成；换品效果仍需人工验收。",
            text_color=ORANGE,
            font=("Segoe UI", 11),
            anchor="w",
        ).pack(anchor="w", pady=(12, 7))
        self.log_text = ctk.CTkTextbox(
            swap,
            height=96,
            corner_radius=12,
            border_width=1,
            border_color=BORDER,
            fg_color="#FBFBFD",
            text_color=MUTED,
            font=("Consolas", 10),
            state="disabled",
        )
        self.log_text.pack(fill="x")

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
        self.relation_text = ctk.CTkTextbox(
            relation,
            height=88,
            corner_radius=12,
            border_width=1,
            border_color=BORDER,
            fg_color="#FBFBFD",
            text_color=TEXT,
            font=("Segoe UI", 12),
        )
        self.relation_text.pack(fill="x")

        _, settings = self._card(
            page,
            "3  输出设置",
            "选择中转站或 Wan 官方线路；秒数和比例默认跟随原视频。",
        )
        fixed_model = ctk.CTkFrame(settings, fg_color=SOFT_BLUE, corner_radius=12)
        fixed_model.pack(fill="x", pady=(0, 14))
        ctk.CTkLabel(
            fixed_model,
            text="视频线路",
            text_color=MUTED,
            font=("Segoe UI", 10, "bold"),
        ).pack(side="left", padx=(14, 8), pady=10)
        self.model_box = ctk.CTkOptionMenu(
            fixed_model,
            variable=self.video_route_var,
            values=list(VIDEO_ROUTE_LABELS.values()),
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
            "提示词分析、文件命名和保存位置均由系统自动处理。",
        )
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
        status = ctk.CTkFrame(swap, fg_color="#F7F7F9", corner_radius=13)
        status.pack(fill="x", pady=(14, 0))
        ctk.CTkLabel(
            status,
            textvariable=self.status_var,
            text_color=TEXT,
            font=("Segoe UI", 12, "bold"),
        ).pack(side="left", padx=16, pady=13)
        self.progress = ctk.CTkProgressBar(
            status, width=170, height=5, corner_radius=3, progress_color=BLUE
        )
        self.progress.set(0)
        self.progress.pack(side="left", padx=8)
        self.open_button = self._secondary_button(
            status, "打开本地文件夹", self._open_output_folder, width=116
        )
        self.open_button.configure(state="disabled")
        self.open_button.pack(side="right", padx=10, pady=7)
        self.copy_link_button = self._secondary_button(
            status, "复制成片链接", self._copy_video_url, width=116
        )
        self.copy_link_button.configure(state="disabled")
        self.copy_link_button.pack(side="right", pady=7)
        self.log_text = ctk.CTkTextbox(
            swap,
            height=88,
            corner_radius=12,
            border_width=1,
            border_color=BORDER,
            fg_color="#FBFBFD",
            text_color=MUTED,
            font=("Segoe UI", 10),
            state="disabled",
        )
        self.log_text.pack(fill="x", pady=(12, 0))

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
            "所有换品成片自动保存到这里，文件名由原视频名称和时间自动生成。",
        )
        workspace.grid_columnconfigure(0, weight=1)
        self._field_label(workspace, "成片保存目录").grid(row=0, column=0, sticky="w")
        self.workspace_output_entry = self._entry(workspace, self.workspace_output_dir_var)
        self.workspace_output_entry.grid(row=1, column=0, sticky="ew", padx=(0, 10), pady=(6, 0))
        self._secondary_button(
            workspace, "选择文件夹", self._choose_workspace_output_dir, width=108
        ).grid(row=1, column=1, pady=(6, 0))

        _, prompt = self._card(page, "提示词分析服务", "视频位置识别与尺寸关系整理（DeepSeek Vision）")
        prompt.grid_columnconfigure(1, weight=1)
        self._api_row(prompt, 0, "用途", label="视频位置识别与尺寸关系整理（DeepSeek Vision）")
        self._api_row(prompt, 1, "API 地址", variable=self.prompt_base_var)
        self._api_row(prompt, 2, "模型", variable=self.prompt_model_var)
        self._api_row(prompt, 3, "API Key", variable=self.prompt_key_var, secret=True)

        models_by_provider: dict[str, list[str]] = {}
        for model in self.models:
            models_by_provider.setdefault(model["provider"], []).append(model["model"])
        for provider, spec in self.runner.registry.providers.items():
            provider_name = str(spec.get("display_name", provider))
            _, frame = self._card(page, f"视频服务 · {provider_name}")
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
            model_names = "、".join(models_by_provider.get(provider, [])) or "未登记模型"
            self._api_row(frame, 0, "当前功能", label=model_names)
            self._api_row(frame, 1, "API 地址", variable=base_var)
            self._api_row(frame, 2, "API Key", variable=key_var, secret=True)

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

    def _select_video_route(self, label: str) -> None:
        for model, route_label in VIDEO_ROUTE_LABELS.items():
            if label == route_label:
                self.model_var.set(model)
                break
        self._update_model_help()

    def _update_model_help(self) -> None:
        model = self.model_var.get().strip() or DEFAULT_VIDEO_MODEL
        if model not in VIDEO_ROUTE_LABELS:
            model = DEFAULT_VIDEO_MODEL
            self.model_var.set(model)
        self.video_route_var.set(VIDEO_ROUTE_LABELS[model])
        item = self.model_map.get(model, {})
        if item.get("adapter") == "dashscope_videoedit":
            self.model_help_var.set("官方当前公开模型：wan2.7-videoedit")
            resolutions = ["720p", "1080p"]
            if self.resolution_var.get() not in resolutions:
                self.resolution_var.set("720p")
        else:
            self.model_help_var.set("中转站模型：wan3.0-video")
            resolutions = ["480p", "720p", "1080p"]
        if hasattr(self, "resolution_box"):
            self.resolution_box.configure(values=resolutions)

    def _choose_source_video(self) -> None:
        selected = filedialog.askopenfilename(
            parent=self.root,
            title="选择原视频",
            filetypes=[("视频", "*.mp4 *.mov *.webm *.m4v *.avi *.mkv"), ("所有文件", "*.*")],
        )
        if selected:
            self._load_source_video(Path(selected))

    def _load_source_video(self, selected: Path) -> None:
        path = selected.resolve()
        self.status_var.set("正在识别原视频时长与比例…")
        self.root.update_idletasks()
        try:
            info = probe_video(path)
            poster = video_poster(path, (420, 250))
        except Exception as exc:
            messagebox.showerror("无法读取视频", str(exc), parent=self.root)
            self.status_var.set("原视频读取失败")
            return
        self.source_video_var.set(str(path))
        self.video_info = info
        self.duration_var.set(str(info.duration_seconds))
        self.aspect_ratio_var.set(info.aspect_ratio)
        self.source_summary_var.set(f"{path.name} · {info.summary}")
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
        self.status_var.set("已识别原视频规格，输出设置已自动跟随")

    def _automatic_output_path(self, source: Path) -> Path:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        folder = Path(self.workspace_output_dir_var.get().strip()).expanduser().resolve()
        return folder / f"{source.stem}_换品_{stamp}.mp4"

    def _choose_reference_image(self) -> None:
        selected = filedialog.askopenfilename(
            parent=self.root,
            title="选择产品参考图",
            filetypes=[("图片", "*.jpg *.jpeg *.png *.webp *.gif *.bmp"), ("所有文件", "*.*")],
        )
        if selected:
            self._load_reference_image(Path(selected))

    def _load_reference_image(self, selected: Path) -> None:
        path = selected.resolve()
        try:
            thumbnail = image_thumbnail(path, (420, 250))
        except Exception as exc:
            messagebox.showerror("无法读取图片", str(exc), parent=self.root)
            return
        self.reference_image_var.set(str(path))
        self.product_preview_ctk_image = ctk.CTkImage(
            light_image=thumbnail, dark_image=thumbnail, size=(420, 250)
        )
        self.product_image_label.configure(image=self.product_preview_ctk_image, text="")
        self.image_summary_var.set(path.name)

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
        item = self.model_map.get(model, {"adapter": "generic"})
        max_duration = 10 if item.get("adapter") == "dashscope_videoedit" else 30
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
                public_reference_url=self.public_image_url_var.get(),
            )
        except ValueError as exc:
            messagebox.showerror("参考图输入不完整", str(exc), parent=self.root)
            return None
        return {
            "source": str(source.resolve()),
            "image": str(image.resolve()),
            "relation": relation,
            "model": model,
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
        }
        for data in self.provider_vars.values():
            values[str(data["base_env"])] = data["base_var"].get().strip()  # type: ignore[union-attr]
            values[str(data["key_env"])] = data["key_var"].get().strip()  # type: ignore[union-attr]
        return values

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
                missing.append(f"{VIDEO_ROUTE_LABELS.get(selected_model, '视频服务')} API Key")
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

    def _start_analysis(self) -> None:
        if self.running or not self._require_app_credentials(prompt=True):
            return
        values = self._validate_common(require_relation=True, require_generation=False)
        if values is not None:
            self._start_worker("analysis", values)

    def _start_check(self) -> None:
        values = self._validate_common(require_relation=False)
        if values is not None and not self.running:
            self._start_worker("check", values)

    def _start_generate(self) -> None:
        if self.running or not self._require_app_credentials(video=True):
            return
        values = self._validate_common(require_relation=True)
        if values is None:
            return
        prompt = self._current_prompt()
        if not prompt or self.generated_context != self._context():
            messagebox.showerror(
                "请重新生成提示词",
                "当前提示词为空，或原视频、参考图、尺寸关系已经变化。请先点击“只生成提示词”。",
                parent=self.root,
            )
            return
        if self._confirm_paid_run():
            values["prompt"] = prompt
            self._start_worker("generate", values)

    def _start_full_pipeline(self) -> None:
        if self.running or not self._require_app_credentials(prompt=True, video=True):
            return
        source = Path(self.source_video_var.get().strip())
        if source.is_file():
            self.output_var.set(str(self._automatic_output_path(source)))
        values = self._validate_common(require_relation=True)
        if values is not None:
            self._start_worker("full", values)

    def _confirm_paid_run(self) -> bool:
        return messagebox.askokcancel(
            "确认正式生成",
            "工作台会向所选视频模型正式提交一次，可能产生费用。\n\n不会自动重试。是否继续？",
            icon="warning",
            parent=self.root,
        )

    def _start_worker(self, mode: str, values: dict[str, object]) -> None:
        values = dict(values)
        values["_env"] = self._child_env()
        values["_prompt_model"] = self.prompt_model_var.get().strip()
        values["_check_prompt"] = self._current_prompt()
        self._set_running(True)
        labels = {
            "analysis": "正在分析视频并生成提示词…",
            "check": "正在检查模型输入，不会提交…",
            "generate": "正在生成视频，请勿关闭窗口…",
            "full": "正在执行完整换品流程…",
        }
        self.status_var.set(labels[mode])
        self._set_log("任务已开始。正式生成最多提交一次，不会自动重试。")
        threading.Thread(target=self._worker, args=(mode, values), daemon=True).start()

    def _run_command(
        self, command: list[str], cwd: Path, env: dict[str, str]
    ) -> tuple[int, dict[str, object], str]:
        completed = subprocess.run(
            command,
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            creationflags=CREATE_NO_WINDOW,
            check=False,
        )
        stdout, stderr = completed.stdout.strip(), completed.stderr.strip()
        try:
            payload = json.loads(stdout) if stdout else {}
        except json.JSONDecodeError:
            payload = {"success": False, "error": stdout or "程序没有返回有效结果。"}
        if not isinstance(payload, dict):
            payload = {"success": False, "error": "程序返回的数据格式无法识别。"}
        return completed.returncode, payload, stderr

    def _analyze(self, values: dict[str, object]) -> tuple[int, dict[str, object], str]:
        command = build_prompt_command(
            sys.executable,
            self.prompt_script,
            video=str(values["source"]),
            reference_image=str(values["image"]),
            volume_relation=str(values["relation"]),
            model=str(values["_prompt_model"]) or None,
        )
        return self._run_command(command, self.prompt_skill_root, values["_env"])  # type: ignore[arg-type]

    def _generate(
        self, values: dict[str, object], prompt: str, dry_run: bool
    ) -> tuple[int, dict[str, object], str]:
        command = build_generate_command(
            sys.executable,
            GENERATE_SCRIPT,
            prompt=prompt,
            model=str(values["model"]),
            references=values["references"],  # type: ignore[arg-type]
            output=str(values["output"]),
            duration=int(values["duration"]),
            aspect_ratio=str(values["aspect_ratio"]),
            resolution=str(values["resolution"]),
            dry_run=dry_run,
        )
        return self._run_command(command, SKILL_ROOT, values["_env"])  # type: ignore[arg-type]

    def _worker(self, mode: str, values: dict[str, object]) -> None:
        try:
            if mode in {"analysis", "full"}:
                self.events.put(("status", "正在抽取视频画面并调用提示词分析 Skill…"))
                code, prompt_payload, prompt_stderr = self._analyze(values)
                if code != 0 or not prompt_payload.get("prompt"):
                    raise RuntimeError(
                        str(prompt_payload.get("error") or prompt_stderr or f"提示词 Skill 退出代码：{code}")
                    )
                prompt = str(prompt_payload["prompt"])
                self.events.put(
                    (
                        "prompt_ready",
                        {
                            "prompt": prompt,
                            "context": (str(values["source"]), str(values["image"]), str(values["relation"])),
                            "detail": prompt_payload,
                            "stderr": prompt_stderr,
                        },
                    )
                )
                if mode == "analysis":
                    self.events.put(("finished", {"mode": mode, "payload": prompt_payload}))
                    return
                self.events.put(("status", "提示词已生成，正在向视频模型提交一次正式任务…"))
                code, payload, stderr = self._generate(values, prompt, dry_run=False)
            elif mode == "check":
                code, payload, stderr = self._generate(
                    values, str(values["_check_prompt"]) or "输入检查占位提示词", dry_run=True
                )
            else:
                code, payload, stderr = self._generate(values, str(values["prompt"]), dry_run=False)
            if code != 0 or not payload.get("success"):
                raise RuntimeError(str(payload.get("error") or stderr or f"程序退出代码：{code}"))
            self.events.put(
                ("finished", {"mode": mode, "payload": payload, "stderr": stderr, "output": values["output"]})
            )
        except Exception as exc:
            self.events.put(("failed", str(exc)))

    def _drain_events(self) -> None:
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "status":
                    self.status_var.set(str(payload))
                elif kind == "prompt_ready":
                    data = payload  # type: ignore[assignment]
                    self._set_prompt(str(data["prompt"]))
                    self.generated_context = data["context"]
                    detail = json.dumps(data["detail"], ensure_ascii=False, indent=2)
                    self._set_log(self._safe_log("\n\n".join(filter(None, [data["stderr"], detail]))))
                elif kind == "finished":
                    self._handle_finished(payload)  # type: ignore[arg-type]
                elif kind == "failed":
                    self._handle_failed(str(payload))
        except queue.Empty:
            pass
        self.root.after(100, self._drain_events)

    def _handle_finished(self, data: dict[str, object]) -> None:
        self._set_running(False)
        mode, payload = str(data["mode"]), data.get("payload", {})
        if mode == "analysis":
            self.status_var.set("提示词已生成，可检查后正式提交")
            messagebox.showinfo(
                "提示词已生成", "可检查固定提示词，再点击“使用当前提示词生成”。", parent=self.root
            )
            return
        if mode == "check":
            self.status_var.set("模型输入检查通过，没有上传或提交")
            self._set_log(self._safe_log(json.dumps(payload, ensure_ascii=False, indent=2)))
            messagebox.showinfo(
                "检查通过", "输入规则检查通过；没有上传文件，也没有提交任务。", parent=self.root
            )
            return
        result = payload if isinstance(payload, dict) else {}
        video_url = str(result.get("video_url") or "").strip()
        if not video_url:
            self._handle_failed("视频任务已完成，但没有返回成片链接。")
            return
        self.last_video_url = video_url
        local_downloaded = bool(result.get("local_downloaded"))
        output_text = str(result.get("output") or "").strip()
        self.last_output = Path(output_text) if local_downloaded and output_text else None
        self.copy_link_button.configure(state="normal")
        detail = json.dumps(payload, ensure_ascii=False, indent=2)
        self._set_log(self._safe_log("\n\n".join(filter(None, [str(data.get("stderr") or ""), detail]))))
        if self.last_output is not None:
            self.status_var.set("视频已保存本地，也可复制成片链接")
            self.open_button.configure(state="normal")
            messagebox.showinfo(
                "视频已生成",
                f"成片已保存到：\n{self.last_output}\n\n"
                "也可以点击“复制成片链接”发给客户。\n\n"
                "请继续人工检查换品效果和画面质量。",
                parent=self.root,
            )
        else:
            download_error = str(result.get("download_error") or "本地下载失败。")
            self.status_var.set("视频生成成功，本地下载失败，可复制成片链接")
            messagebox.showwarning(
                "视频已生成，本地下载失败",
                f"{download_error}\n\n"
                "生成任务已经完成，不要重新提交。可点击“复制成片链接”发给客户或稍后下载。",
                parent=self.root,
            )

    def _handle_failed(self, error: str) -> None:
        self._set_running(False)
        safe_error = self._safe_log(error)
        self.status_var.set("执行失败，未自动重试")
        self._set_log(safe_error)
        messagebox.showerror("执行失败", safe_error, parent=self.root)

    def _safe_log(self, text: str) -> str:
        values = self._credential_values()
        secret_names = {"DEEPSEEK_API_KEY", "GEMINI_RELAY_API_KEY", "GEMINI_API_KEY"}
        for spec in self.runner.registry.providers.values():
            secret_names.add(str(spec.get("api_key_env", "")))
            secret_names.add(str(spec.get("legacy_api_key_env", "")))
        for name in secret_names:
            if name and os.environ.get(name):
                values[name] = os.environ[name]
        return redact(text, values)

    def _set_running(self, running: bool) -> None:
        self.running = running
        state = "disabled" if running else "normal"
        for name in (
            "aspect_box",
            "duration_spin",
            "resolution_box",
            "source_button",
            "reference_button",
            "relation_text",
            "model_box",
            "full_button",
        ):
            widget = getattr(self, name, None)
            if widget is not None:
                widget.configure(state=state)
        if running:
            self.last_video_url = None
            self.open_button.configure(state="disabled")
            self.copy_link_button.configure(state="disabled")
            self.progress.start()
        else:
            self.progress.stop()
            self.progress.set(0)
            self.aspect_box.configure(state="normal")
            self.resolution_box.configure(state="normal")
            self._update_model_help()

    def _set_log(self, text: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", END)
        self.log_text.insert("1.0", text)
        self.log_text.configure(state="disabled")

    def _toggle_keys(self) -> None:
        show = "" if self.show_keys_var.get() else "●"
        for entry in self.key_entries:
            entry.configure(show=show)

    def _update_api_status(self) -> None:
        values = self._credential_values()
        prompt_ok = bool(values.get("DEEPSEEK_API_KEY"))
        total = len(self.provider_vars)
        configured = sum(bool(values.get(str(data["key_env"]))) for data in self.provider_vars.values())
        self.api_status_var.set(
            f"工作区：已配置；提示词 Key：{'已配置' if prompt_ok else '未配置'}；"
            f"视频服务 Key：{configured}/{total} 已配置"
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
            spec = self.runner.registry.providers[provider]
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
        self.video_route_var.set(VIDEO_ROUTE_LABELS[DEFAULT_VIDEO_MODEL])
        for provider, data in self.provider_vars.items():
            data["key_var"].set("")  # type: ignore[union-attr]
            data["base_var"].set(self.runner.registry.providers[provider]["default_api_base"])  # type: ignore[union-attr]
        self._update_api_status()
        messagebox.showinfo("已清除", "工作台保存的加密配置已清除。", parent=self.root)

    def _open_output_folder(self) -> None:
        target = self.last_output or Path(self.output_var.get())
        folder = target.parent
        if folder.is_dir():
            os.startfile(folder)  # type: ignore[attr-defined]
        else:
            messagebox.showerror("文件夹不存在", str(folder), parent=self.root)

    def _copy_video_url(self) -> None:
        if not self.last_video_url:
            messagebox.showerror("没有成片链接", "当前没有可复制的成片链接。", parent=self.root)
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(self.last_video_url)
        self.root.update()
        self.status_var.set("成片链接已复制")


    def _on_close(self) -> None:
        if self.running:
            messagebox.showwarning(
                "任务仍在运行", "请等待任务完成后再关闭，避免丢失生成或下载结果。", parent=self.root
            )
            return
        self.video_player.stop()
        self.root.destroy()


def main() -> int:
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
