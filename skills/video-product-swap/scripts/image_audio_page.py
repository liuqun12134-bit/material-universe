"""图音生视频 page. Background workers communicate with Tk only via events."""
from __future__ import annotations

import copy
import threading
import time
import uuid
from pathlib import Path
from tkinter import StringVar, filedialog, messagebox

import customtkinter as ctk
from PIL import Image

from fish_mcp import FishAuth, FishClient, auth_path
from image_audio_video import (FIELD_LABELS, IMAGE_FIELDS, MEDIA_FIELDS, RATIOS, RESOLUTIONS,
                               inspect_audio, inspect_image, prepare_quote)


class ImageAudioPage:
    def _init_image_audio(self, demo=False):
        self.fish_auth_path = self.desktop_store.root / "fish-demo.dat" if demo else auth_path()
        self.fish_media = {}
        self.fish_load_tokens = {}
        self.fish_previews = {}
        self.fish_workspaces = {}
        self.fish_busy = False
        self.fish_login_busy = False
        self.fish_submit_id = uuid.uuid4().hex
        self.fish_pending_config = None
        self.fish_not_before = 0
        self.fish_ratio = StringVar(value="9:16")
        self.fish_resolution = StringVar(value="480p")
        self.fish_duration = StringVar(value="")
        self.fish_workspace = StringVar(value="请连接账户")
        self.fish_status = StringVar(value="三张图片、音频和提示词全部必填。")
        self.fish_account_status = StringVar(value="未连接 Fish Audio")
        self.fish_media_labels = {field: StringVar(value="尚未选择") for field in MEDIA_FIELDS}
        self.fish_audio_play_label = StringVar(value="试听")
        self._init_director()
        self.fish_workspace.trace_add("write", self._fish_workspace_changed)

    def _build_image_audio_page(self):
        page = self.image_audio_page = ctk.CTkScrollableFrame(self.image_audio_tab, fg_color="#F5F5F7", corner_radius=0)
        page.pack(fill="both", expand=True, padx=30, pady=22)
        ctk.CTkLabel(page, text="图音生视频", font=("Microsoft YaHei UI", 28, "bold"), text_color="#1D1D1F").pack(anchor="w")
        ctk.CTkLabel(page, text="三张参考图 + 一段音频，生成讲解或使用演示视频 · Seedance 2.5",
                     text_color="#6E6E73").pack(anchor="w", pady=(4, 16))
        self.fish_service_status = ctk.CTkButton(page, textvariable=self.fish_account_status, command=lambda: self._show_page("api"),
            fg_color="transparent", hover_color="#EAF4FF", text_color="#6E6E73", anchor="w", height=28)
        self.fish_service_status.pack(fill="x", pady=(0, 10))

        _, materials = self._card(page, "1  参考素材", "每项恰好一个文件，全部必填；多视角图请合成在一张静态图片内。")
        images = ctk.CTkFrame(materials, fg_color="transparent")
        images.pack(fill="x")
        images.grid_columnconfigure((0, 1, 2), weight=1, uniform="fish_images")
        self.fish_image_widgets = {}
        for index, field in enumerate(IMAGE_FIELDS):
            panel = ctk.CTkFrame(images, fg_color="#F7F7F9", corner_radius=12)
            panel.grid(row=0, column=index, sticky="nsew", padx=(0 if index == 0 else 5, 0 if index == 2 else 5))
            ctk.CTkLabel(panel, text=f"{FIELD_LABELS[field]} *  ·  @图{index + 1}",
                         font=("Microsoft YaHei UI", 12, "bold")).pack(anchor="w", padx=12, pady=(10, 6))
            preview = ctk.CTkLabel(panel, text="JPG / PNG / WebP", height=120, text_color="#6E6E73")
            preview.pack(fill="x", padx=12)
            self.fish_image_widgets[field] = preview
            self._secondary_button(panel, "选择图片", lambda f=field: self._fish_choose(f), width=115).pack(pady=(8, 4))
            self._secondary_button(panel, "从素材库选择", lambda f=field: self._open_library(f), width=115).pack(pady=(0, 4))
            ctk.CTkLabel(panel, textvariable=self.fish_media_labels[field], text_color="#6E6E73",
                         wraplength=195, height=40).pack(fill="x", padx=10, pady=(0, 8))
        ctk.CTkLabel(materials, text="图片每张 ≤20 MB；宽高 300–6000 像素，宽高比 0.4–2.5。",
                     text_color="#6E6E73").pack(anchor="w", pady=(8, 4))
        ctk.CTkLabel(materials, text="首帧图用于开场构图参考；多参考模式不能保证逐像素锁定首帧。",
                     text_color="#6E6E73", wraplength=710, justify="left").pack(anchor="w")
        audio = ctk.CTkFrame(materials, fg_color="#F7F7F9", corner_radius=12)
        audio.pack(fill="x", pady=(12, 0))
        row = ctk.CTkFrame(audio, fg_color="transparent")
        row.pack(fill="x", padx=14, pady=10)
        ctk.CTkLabel(row, text="音频 *  ·  @音频1", font=("Microsoft YaHei UI", 12, "bold")).pack(side="left", padx=(0, 16))
        self._secondary_button(row, "选择音频", lambda: self._fish_choose("audio"), width=100).pack(side="left")
        self._secondary_button(row, "素材库", lambda: self._open_library("audio"), width=75).pack(side="left", padx=(8, 0))
        from video_gui import AudioPlayer
        self.fish_audio_player = AudioPlayer(self.root)
        self.fish_audio_player.state_callback = lambda playing, paused: self.fish_audio_play_label.set("暂停" if playing and not paused else "试听")
        self.fish_play_button = self._secondary_button(row, "试听", self.fish_audio_player.toggle, width=75)
        self.fish_play_button.configure(textvariable=self.fish_audio_play_label, state="disabled")
        self.fish_play_button.pack(side="left", padx=8)
        ctk.CTkLabel(audio, textvariable=self.fish_media_labels["audio"], text_color="#6E6E73",
                     wraplength=700, justify="left").pack(anchor="w", padx=14)
        ctk.CTkLabel(audio, text="真实 MP3 / PCM WAV · 1.8–30 秒 · ≤15 MB；不接受改后缀伪装的文件。",
                     text_color="#6E6E73", wraplength=710, justify="left").pack(anchor="w", padx=14, pady=(4, 10))

        prompt_card, content = self._card(page, "2  提示词与输出", "提示词可使用 @图1、@图2、@图3、@音频1 引用对应素材。")
        self.fish_prompt = ctk.CTkTextbox(content, height=150, wrap="word", border_width=1, border_color="#E5E5EA")
        self._build_director_controls(prompt_card, content)
        self.fish_prompt.pack(fill="x", pady=(0, 14))
        settings = ctk.CTkFrame(content, fg_color="transparent")
        settings.pack(fill="x")
        settings.grid_columnconfigure((0, 1, 2), weight=1, uniform="fish_specs")
        for col, title in enumerate(("画面比例", "分辨率", "生成秒数")):
            ctk.CTkLabel(settings, text=title).grid(row=0, column=col, sticky="w", padx=(0, 12))
        ctk.CTkOptionMenu(settings, variable=self.fish_ratio, values=list(RATIOS)).grid(row=1, column=0, sticky="ew", padx=(0, 12))
        ctk.CTkOptionMenu(settings, variable=self.fish_resolution, values=list(RESOLUTIONS)).grid(row=1, column=1, sticky="ew", padx=(0, 12))
        ctk.CTkEntry(settings, textvariable=self.fish_duration, placeholder_text="根据音频自动填写").grid(row=1, column=2, sticky="ew")
        ctk.CTkLabel(content, text="音频时长向上取整，最短生成 4 秒，最长 30 秒；可调长，不能短于音频。",
                     text_color="#6E6E73", wraplength=710, justify="left").pack(anchor="w", pady=(8, 4))
        self._secondary_button(content, "恢复音频默认秒数", self._fish_reset_duration, width=160).pack(anchor="w", pady=(0, 8))
        self.fish_submit_button = ctk.CTkButton(content, text="提交并生成", command=self._fish_submit,
                                               height=46, corner_radius=23, fg_color="#0071E3")
        self.fish_submit_button.pack(fill="x", pady=(8, 6))
        ctk.CTkLabel(content, text="先校验并上传至 Fish 工作区，再显示积分报价；确认后生成。进度与结果在「使用记录」查看。",
                     text_color="#6E6E73", wraplength=710, justify="left").pack(anchor="w")
        ctk.CTkLabel(content, textvariable=self.fish_status, text_color="#0071E3", wraplength=710,
                     justify="left").pack(anchor="w", pady=(6, 0))

    def _fish_choose(self, field):
        types = [("音频", "*.mp3 *.wav"), ("所有文件", "*.*")] if field == "audio" else [
            ("图片", "*.jpg *.jpeg *.png *.webp"), ("所有文件", "*.*")]
        selected = filedialog.askopenfilename(parent=self.root, title="选择" + FIELD_LABELS[field], filetypes=types)
        if selected:
            self._fish_load(field, selected)

    def _fish_load(self, field, path):
        identity = uuid.uuid4().hex
        self.fish_load_tokens[field] = identity
        self.fish_media.pop(field, None)
        self.fish_media_labels[field].set("正在检查并保存…")
        if field == "audio":
            self.fish_audio_player.stop()
            self.fish_play_button.configure(state="disabled")
            self.fish_duration.set("")
        else:
            self.fish_image_widgets[field].configure(image=None, text="正在检查…")
            # CTkLabel leaves the Tcl image set when image=None; clear it before
            # replacing the retained CTkImage to avoid a dangling photo handle.
            self.fish_image_widgets[field]._label.configure(image="")

        def run():
            try:
                info = inspect_audio(path) if field == "audio" else inspect_image(path, FIELD_LABELS[field])
                asset = self.desktop_store.import_asset(path)
                from image_audio_video import digest_file
                if digest_file(asset["path"]) != info["sha256"]:
                    raise ValueError("文件在检查期间发生变化，请重新选择。")
                info.update(path=asset["path"], display_name=asset["name"])
                self.events.put(("fish_event", ("media", field, identity, info, None)))
            except Exception as exc:
                self.events.put(("fish_event", ("media", field, identity, None, str(exc))))
        threading.Thread(target=run, daemon=True).start()

    def _fish_reset_duration(self):
        if "audio" in self.fish_media:
            self.fish_duration.set(str(self.fish_media["audio"]["default_duration"]))

    def _fish_connect(self):
        self._fish_account_worker(login=True)

    def _fish_refresh_account(self):
        self._fish_account_worker(login=False)

    def _fish_account_worker(self, login):
        if self.fish_login_busy or self.fish_busy or self.director_busy or self.voice_busy or self.voice_clone_busy:
            return
        if login and any(job["kind"] in {"image_audio", "speech"} and job["status"] in {"queued", "running"} for job in self.desktop_store.jobs()):
            messagebox.showinfo("任务执行中", "请等待 Fish Audio 任务结束后再切换授权账户。", parent=self.root)
            return
        self.fish_login_busy = True
        if login:
            self._voice_reset_account()
            self.fish_workspaces = {}
            self.fish_workspace.set("请连接账户")
        self.fish_connect_button.configure(state="disabled")
        self.fish_account_status.set("请在浏览器完成 Fish Audio 登录授权…" if login else "正在读取工作区…")
        path = self.fish_auth_path

        def run():
            try:
                auth = FishAuth(path)
                if login:
                    auth.login()
                with FishClient(path) as client:
                    result = client.call("list_my_workspaces", {})
                self.events.put(("fish_event", ("account", result, None)))
            except Exception as exc:
                self.events.put(("fish_event", ("account", None, str(exc))))
        threading.Thread(target=run, daemon=True).start()

    def _fish_form(self):
        return {**{field: self.fish_media.get(field, {}).get("path", "") for field in MEDIA_FIELDS},
                "prompt": self.fish_prompt.get("1.0", "end-1c").strip(), "duration": self.fish_duration.get().strip(),
                "aspect_ratio": self.fish_ratio.get(), "resolution": self.fish_resolution.get(),
                "workspace_id": self.fish_workspaces.get(self.fish_workspace.get(), ""),
                "workspace_name": self.fish_workspace.get()}

    def _fish_submit(self):
        if self.fish_busy or self.fish_login_busy or self.director_busy or time.monotonic() < self.fish_not_before:
            return
        if self.fish_pending_config:
            self._fish_enqueue(self.fish_pending_config)
            return
        config = self._fish_form()
        for field in MEDIA_FIELDS:
            if not config[field]:
                messagebox.showerror("素材不完整", f"请上传并检查{FIELD_LABELS[field]}，此项必填。", parent=self.root)
                return
        if not config["prompt"]:
            messagebox.showerror("提示词必填", "请填写视频提示词。", parent=self.root)
            return
        if not config["workspace_id"]:
            messagebox.showerror("尚未连接", "请到设置中连接 Fish Audio 并选择工作区。", parent=self.root)
            return
        self.fish_busy = True
        self.fish_submit_button.configure(state="disabled", text="正在校验并预估…")
        snapshot = copy.deepcopy(config)
        config["output"] = str(Path(self.workspace_output_dir_var.get()).expanduser().resolve() / "图音生视频.mp4")
        path = self.fish_auth_path

        def run():
            try:
                with FishClient(path) as client:
                    prepared = prepare_quote(config, self.desktop_store, client,
                        lambda text: self.events.put(("fish_event", ("progress", text))))
                self.events.put(("fish_event", ("quote", snapshot, prepared, None)))
            except Exception as exc:
                self.events.put(("fish_event", ("quote", snapshot, None, str(exc))))
        threading.Thread(target=run, daemon=True).start()

    def _fish_enqueue(self, config):
        self.fish_busy = True
        self.fish_pending_config = copy.deepcopy(config)
        self.fish_submit_button.configure(state="disabled", text="正在提交…")
        try:
            identity = self.tasks.submit("image_audio", config, self._credential_values(), request_id=self.fish_submit_id)
        except Exception as exc:
            self.fish_busy = False
            self.fish_submit_button.configure(state="normal", text="核对原提交")
            self.fish_status.set("本地提交尚未确认，点击会核对原任务，不会重复提交。")
            messagebox.showerror("创建任务失败", str(exc), parent=self.root)
            return
        self.fish_status.set("已加入使用记录，可继续生成其他视频。")
        self._refresh_history()
        try:
            messagebox.showinfo("任务已提交", "图音生视频已加入队列，请在「使用记录」查看进度。\n\n确认后可继续提交其他视频，各功能共享并发上限。", parent=self.root)
        finally:
            self.fish_submit_id = uuid.uuid4().hex
            self.fish_pending_config = None
            self.fish_busy = False
            self.fish_not_before = time.monotonic() + 1
            self.fish_submit_button.configure(state="normal", text="提交并生成")
        return identity

    def _handle_fish_event(self, payload):
        kind = payload[0]
        if kind == "progress":
            self.fish_status.set(payload[1])
        elif kind == "account":
            _, result, error = payload
            self.fish_login_busy = False
            self.fish_connect_button.configure(state="normal")
            if error:
                self.fish_account_status.set("连接检查未完成 · " + error)
                return
            workspaces = result.get("workspaces", [])
            self.fish_workspaces = {f"{w.get('team_name', '')} / {w['workspace_name']} ({w['workspace_id'][:6]})": w["workspace_id"] for w in workspaces}
            if not self.fish_workspaces:
                self.fish_account_status.set("此授权没有可用工作区，请检查账户。")
                return
            self.fish_workspace_menu.configure(values=list(self.fish_workspaces))
            if self.fish_workspace.get() not in self.fish_workspaces:
                saved = self._saved_fish_workspace()
                default = saved if saved in self.fish_workspaces.values() else next((w["workspace_id"] for w in workspaces if w.get("is_default")), workspaces[0]["workspace_id"])
                self.fish_workspace.set(next(k for k, v in self.fish_workspaces.items() if v == default))
            self._fish_workspace_changed()
            if getattr(self, "current_page", None) == "voice":
                self._voice_on_show()
        elif kind == "media":
            _, field, identity, info, error = payload
            if self.fish_load_tokens.get(field) != identity:
                return
            if error:
                self.fish_media_labels[field].set("校验失败，请重新选择")
                if field != "audio":
                    self.fish_image_widgets[field].configure(text="未通过校验", image=None)
                messagebox.showerror(FIELD_LABELS[field] + "不符合要求", error, parent=self.root)
                return
            self.fish_media[field] = info
            if field == "audio":
                self.fish_media_labels[field].set(f"{info['display_name']} · {info['duration']:.3f} 秒 · 默认生成 {info['default_duration']} 秒")
                self._fish_reset_duration()
                self.fish_audio_player.load(Path(info["path"]))
                self.fish_play_button.configure(state="normal")
            else:
                with Image.open(info["path"]) as source:
                    thumb = source.convert("RGB")
                    thumb.thumbnail((180, 120))
                image = ctk.CTkImage(light_image=thumb, dark_image=thumb, size=thumb.size)
                self.fish_image_widgets[field].configure(image=image, text="")
                self.fish_previews[field] = image
                self.fish_media_labels[field].set(f"{info['display_name']}\n{info['width']} × {info['height']}")
        elif kind == "quote":
            _, snapshot, config, error = payload
            try:
                if error:
                    self.fish_status.set(error)
                    messagebox.showerror("无法提交", error, parent=self.root)
                    return
                if self._fish_form() != snapshot:
                    self.fish_status.set("预估期间素材或参数发生变化，请重新提交以取得对应报价。")
                    return
                quote = config["fish_quote"]
                message = (f"Fish Audio · Seedance 2.5\n{config['workspace_name']}\n"
                           f"{config['duration']} 秒 · {config['aspect_ratio']} · {config['resolution']}\n\n"
                           f"本次消耗：{quote['credits']:,} 积分\n可用积分：{quote.get('balance', '以平台为准')}\n\n确认生成？")
                if messagebox.askyesno("确认生成费用", message, parent=self.root):
                    config["cost_approved"] = True
                    self._fish_enqueue(config)
                else:
                    self.fish_status.set("已取消生成，未提交付费任务；已上传的素材保留在 Fish 工作区。")
            finally:
                self.fish_busy = False
                if not self.fish_pending_config:
                    self.fish_submit_button.configure(state="normal", text="提交并生成")

    def _fish_reuse(self, config):
        if self.fish_busy or self.fish_pending_config:
            messagebox.showinfo("请先完成当前提交", "当前正在准备或核对一次提交，请先完成。", parent=self.root)
            return
        self.fish_prompt.delete("1.0", "end")
        self.fish_prompt.insert("1.0", config.get("prompt", ""))
        self.fish_ratio.set(config.get("aspect_ratio", "9:16"))
        self.fish_resolution.set(config.get("resolution", "480p"))
        for field in MEDIA_FIELDS:
            self._fish_load(field, config.get(field, ""))
        # Audio is revalidated asynchronously and restores its rounded default.
        self.fish_submit_id = uuid.uuid4().hex
        self.fish_status.set("已复制素材与提示词；音频重新检查后填写默认秒数，提交时重新报价。")
        self._show_page("image_audio")
