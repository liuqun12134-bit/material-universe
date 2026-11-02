"""Optional intelligent prompt UI and centralized Fish account settings."""
import copy
import json
import threading
from tkinter import StringVar, messagebox

import customtkinter as ctk

from director_prompt import MODES, configuration, generate
from image_audio_video import MEDIA_FIELDS


class DirectorPage:
    def _init_director(self):
        self.director_busy = False
        self.director_mode = StringVar(value="请选择导演模式")
        self.director_note = StringVar(value="先选择模式；音频由 Fish 自动转写，图片与台词交给 Kaiyun GPT-6.1 Sol 编排。")
        self.director_result = None
        self.director_undo = None

    def _build_director_controls(self, card, content):
        header = card.winfo_children()[0]
        self._secondary_button(header, "✦ 智能提示词", self._toggle_director, width=125).place(relx=1, x=0, y=0, anchor="ne")
        self.director_panel = ctk.CTkFrame(content, fg_color="#F5F7FA", corner_radius=12)
        row = ctk.CTkFrame(self.director_panel, fg_color="transparent")
        row.pack(fill="x", padx=12, pady=(12, 8))
        ctk.CTkOptionMenu(row, variable=self.director_mode, values=["请选择导演模式", *MODES],
            width=155, command=lambda _: self.director_generate_button.configure(state="normal" if self.director_mode.get() in MODES and not self.director_busy else "disabled")).pack(side="left")
        self.director_generate_button = ctk.CTkButton(row, text="生成提示词", width=110, state="disabled", command=self._generate_director)
        self.director_generate_button.pack(side="right")
        self.director_requirements = ctk.CTkEntry(self.director_panel, placeholder_text="补充要求（可选）：例如只展示首帧产品、只涂画面左侧面颊")
        self.director_requirements.pack(fill="x", padx=12, pady=(0, 8))
        ctk.CTkLabel(self.director_panel, text="Fish Flash 转写约 500 积分/分钟；相同音频复用转写。GPT-6.1 Sol 按 Kaiyun 账户用量计费。\n点击仅生成提示词，不会提交视频；机器转写及生成稿请核对。",
            justify="left", anchor="w", wraplength=630, text_color="#6E6E73", font=("Microsoft YaHei UI", 11)).pack(fill="x", padx=12)
        ctk.CTkLabel(self.director_panel, text="画面默认围绕首帧主产品；口播提及其他商品、优惠或功效，不要求逐项展示。",
            justify="left", anchor="w", wraplength=630, text_color="#6E6E73", font=("Microsoft YaHei UI", 11)).pack(fill="x", padx=12)
        ctk.CTkLabel(self.director_panel, textvariable=self.director_note, wraplength=630, justify="left", anchor="w", text_color="#0071E3").pack(fill="x", padx=12, pady=6)
        actions = ctk.CTkFrame(self.director_panel, fg_color="transparent")
        actions.pack(fill="x", padx=12, pady=(0, 10))
        self.director_undo_button = self._secondary_button(actions, "撤销填入", self._undo_director, width=90)
        self.director_undo_button.configure(state="disabled")
        self.director_undo_button.pack(side="left")
        self.director_view_button = self._secondary_button(actions, "查看台词与生成稿", self._view_director, width=145)
        self.director_view_button.configure(state="disabled")
        self.director_view_button.pack(side="left", padx=8)

    def _toggle_director(self):
        if self.director_panel.winfo_manager():
            self.director_panel.pack_forget()
        else:
            self.director_panel.pack(fill="x", pady=(0, 12), before=self.fish_prompt)

    def _director_snapshot(self):
        return {"mode": self.director_mode.get(), "requirements": self.director_requirements.get().strip(),
                "prompt": self.fish_prompt.get("1.0", "end").strip(), "media": copy.deepcopy(self.fish_media),
                "load_tokens": dict(self.fish_load_tokens)}

    def _generate_director(self):
        if self.director_busy or self.fish_busy or self.fish_login_busy:
            return
        snapshot = self._director_snapshot()
        if snapshot["mode"] not in MODES:
            self.director_note.set("请先选择纯口播或边讲边用。")
            return
        if any(f not in snapshot["media"] for f in MEDIA_FIELDS):
            self.director_note.set("请先选好并完成检查三张图片和音频。")
            return
        credentials = self._credential_values()
        try:
            configuration(credentials)
        except ValueError as exc:
            self.director_note.set(str(exc))
            return
        from fish_mcp import FishAuth
        if not FishAuth(self.fish_auth_path).connected():
            self.director_note.set("请先在设置中连接 Fish Audio，用于音频转写。")
            return
        self.director_busy = True
        self.director_generate_button.configure(state="disabled", text="正在编排…")
        self.fish_submit_button.configure(state="disabled")
        self.fish_connect_button.configure(state="disabled")
        self.director_note.set("正在检查素材和导演规则…")
        auth_path, cache = self.fish_auth_path, self.desktop_store.root / "transcripts"
        def run():
            try:
                result = generate(snapshot, credentials, auth_path, cache,
                    lambda note: self.events.put(("director_event", ("progress", note))),
                    personal_root=self.library_transfer.root)
                self.events.put(("director_event", ("done", snapshot, result, None)))
            except Exception as exc:
                message = str(exc)
                for key, value in credentials.items():
                    if value and any(word in key for word in ("KEY", "TOKEN", "SECRET")):
                        message = message.replace(str(value), "***")
                self.events.put(("director_event", ("done", snapshot, None, message)))
        threading.Thread(target=run, daemon=True).start()

    def _handle_director_event(self, payload):
        if payload[0] == "progress":
            self.director_note.set(payload[1])
            return
        _, snapshot, result, error = payload
        self.director_busy = False
        self.director_generate_button.configure(text="生成提示词", state="normal" if self.director_mode.get() in MODES else "disabled")
        self.fish_submit_button.configure(state="normal")
        self.fish_connect_button.configure(state="normal")
        if error:
            self.director_note.set(error)
            return
        self.director_result = result
        self.director_view_button.configure(state="normal")
        if snapshot != self._director_snapshot():
            self.director_note.set("生成期间输入已改变，未覆盖当前内容；可查看生成稿。")
            return
        self.director_undo = (snapshot["prompt"], result["prompt"])
        self.fish_prompt.delete("1.0", "end")
        self.fish_prompt.insert("1.0", result["prompt"])
        self.director_undo_button.configure(state="normal")
        note = "已填入完整提示词，可继续修改。请核对台词、动作与产品信息。"
        if result.get("issues"):
            note += f" 有 {len(result['issues'])} 条编排说明，可在“查看台词与生成稿”查看。"
        self.director_note.set(note)

    def _undo_director(self):
        if not self.director_undo:
            return
        before, generated = self.director_undo
        if self.fish_prompt.get("1.0", "end").strip() != generated:
            if not messagebox.askyesno("撤销智能填入", "你已继续编辑，撤销会恢复生成前的原文。是否恢复？", parent=self.root):
                return
        self.fish_prompt.delete("1.0", "end")
        self.fish_prompt.insert("1.0", before)
        self.director_undo = None
        self.director_undo_button.configure(state="disabled")

    def _view_director(self):
        if not self.director_result:
            return
        win = ctk.CTkToplevel(self.root)
        win.title("台词与导演稿 · 请人工核对")
        win.geometry("760x650")
        box = ctk.CTkTextbox(win, wrap="word")
        box.pack(fill="both", expand=True, padx=20, pady=20)
        result = self.director_result
        details = ""
        if result.get("visual_plan"):
            details += "\n\n画面编排范围\n\n" + result["visual_plan"]
        if result.get("issues"):
            details += "\n\n编排说明（不影响生成）\n\n" + "\n".join(f"• {item}" for item in result["issues"])
        box.insert("1.0", "机器转写（未经人工核对，无逐句时间戳）\n\n" + result["transcript"] + details + "\n\n完整提示词\n\n" + result["prompt"])
        box.configure(state="disabled")
        win.after(100, win.lift)

    def _build_fish_settings(self, page):
        _, account = self._card(page, "Fish Audio", "图音生视频与音频转写共用此授权；使用所授权团队的积分。")
        row = ctk.CTkFrame(account, fg_color="transparent")
        row.pack(fill="x")
        self.fish_connect_button = self._secondary_button(row, "连接 / 切换账户", self._fish_connect, width=135)
        self.fish_connect_button.pack(side="left")
        self._secondary_button(row, "刷新账户", self._fish_refresh_account, width=100).pack(side="left", padx=8)
        self.fish_workspace_menu = ctk.CTkOptionMenu(row, variable=self.fish_workspace, values=["请连接账户"], width=280)
        self.fish_workspace_menu.pack(side="left")
        ctk.CTkLabel(account, textvariable=self.fish_account_status, text_color="#6E6E73", wraplength=680, justify="left").pack(anchor="w", pady=(8, 0))

    def _saved_fish_workspace(self):
        try:
            return json.loads((self.desktop_store.root / "fish-workspace.json").read_text(encoding="utf-8")).get("workspace_id")
        except (OSError, ValueError):
            return None

    def _fish_workspace_changed(self, *_):
        identity = self.fish_workspaces.get(self.fish_workspace.get())
        if identity:
            self.fish_account_status.set("● Fish Audio 已连接 · " + self.fish_workspace.get())
            (self.desktop_store.root / "fish-workspace.json").write_text(json.dumps({"workspace_id": identity}), encoding="utf-8")
