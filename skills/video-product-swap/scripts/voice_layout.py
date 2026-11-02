"""Compact voice-library cards and a two-column listening workspace."""
from pathlib import Path
from tkinter import StringVar

import customtkinter as ctk

BG = "#F4F6FA"
INK = "#18243B"
MUTED = "#778196"
BLUE = "#2864E8"
LINE = "#E3E8F1"


class VoiceLayout:
    def _voice_button(self, parent, text, command, *, primary=False, width=100, height=34):
        return ctk.CTkButton(parent, text=text, command=command, width=width, height=height,
            corner_radius=9, fg_color=BLUE if primary else "#EFF3FA",
            hover_color="#1F52C8" if primary else "#E2E9F5",
            text_color="white" if primary else "#40516D", font=("Microsoft YaHei UI", 12))

    def _voice_surface(self, parent):
        return ctk.CTkFrame(parent, fg_color="white", corner_radius=14, border_width=1, border_color=LINE)

    def _build_voice_page(self):
        from video_gui import AudioPlayer
        self.voice_player = AudioPlayer(self.root)
        self.voice_player.state_callback = self._voice_playback_state
        self.voice_page = ctk.CTkFrame(self.voice_tab, fg_color=BG, corner_radius=0)
        self.voice_page.pack(fill="both", expand=True)
        self.voice_workspace = ctk.CTkFrame(self.voice_page, fg_color="transparent")
        self.voice_workspace.pack(fill="both", expand=True, padx=24, pady=20)
        self.voice_workspace.grid_columnconfigure(0, weight=1)
        self.voice_workspace.grid_rowconfigure(2, weight=1)
        header = ctk.CTkFrame(self.voice_workspace, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", pady=(0, 15))
        ctk.CTkLabel(header, text="声音克隆", text_color=INK, font=("Microsoft YaHei UI", 25, "bold")).pack(side="left")
        self.voice_account_label = StringVar(value="账户设置")
        self._voice_button(header, "账户设置", lambda: self._show_page("api"), width=150).pack(side="right")
        account_button = header.winfo_children()[-1]
        account_button.configure(textvariable=self.voice_account_label)
        def account_changed(*_):
            text = self.fish_account_status.get()
            self.voice_account_label.set("● Fish Audio 已连接" if "已连接" in text else "连接 Fish Audio →")
        self.fish_account_status.trace_add("write", account_changed)
        account_changed()
        tabs = ctk.CTkFrame(self.voice_workspace, fg_color="transparent")
        tabs.grid(row=1, column=0, sticky="ew", pady=(0, 16))
        self.voice_tab_select = ctk.CTkSegmentedButton(tabs, values=["我的声音", "声音合成"], variable=self.voice_view,
            command=self._voice_choose_tab, width=250, height=38, corner_radius=10, border_width=4,
            fg_color="#E7ECF5", selected_color="white", selected_hover_color="white",
            unselected_color="#E7ECF5", unselected_hover_color="#DCE4F1", text_color=INK,
            font=("Microsoft YaHei UI", 13, "bold"), dynamic_resizing=False)
        self.voice_tab_select.pack(side="left")
        ctk.CTkLabel(tabs, text="你的音色 · 你的表达", text_color=MUTED, font=("Microsoft YaHei UI", 12)).pack(side="right")
        stage = ctk.CTkFrame(self.voice_workspace, fg_color="transparent")
        stage.grid(row=2, column=0, sticky="nsew")
        stage.grid_rowconfigure(0, weight=1)
        stage.grid_columnconfigure(0, weight=1)
        self.voice_library_panel = ctk.CTkFrame(stage, fg_color="transparent")
        self.voice_speech_panel = ctk.CTkFrame(stage, fg_color="transparent")
        self._build_voice_library(self.voice_library_panel)
        self._build_voice_speech(self.voice_speech_panel)
        self.voice_status_label = ctk.CTkLabel(self.voice_workspace, textvariable=self.voice_status,
            text_color=MUTED, anchor="w", justify="left", height=25, font=("Microsoft YaHei UI", 11))
        self.voice_status_label.grid(row=3, column=0, sticky="ew", pady=(10, 0))
        self.voice_page.bind("<Configure>", self._voice_layout_changed)
        self._voice_choose_tab("我的声音")

    def _voice_layout_changed(self, event=None):
        if event and event.widget is not self.voice_page:
            return
        width = self.voice_page.winfo_width()
        self.voice_workspace.pack_configure(padx=max(20, (width - 1240) // 2))
        available = self.voice_workspace.winfo_width()
        self.voice_status_label.configure(wraplength=max(500, available - 10))
        columns = 3 if available >= 1100 else 2 if available >= 650 else 1
        if columns != self.voice_card_columns:
            self.voice_card_columns = columns
            self._voice_render_rows()

    def _build_voice_library(self, panel):
        panel.grid_columnconfigure(0, weight=1)
        panel.grid_rowconfigure(1, weight=1)
        controls = ctk.CTkFrame(panel, fg_color="transparent")
        controls.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        self.voice_library_count = StringVar(value="我的音色")
        ctk.CTkLabel(controls, textvariable=self.voice_library_count, text_color=INK,
            font=("Microsoft YaHei UI", 14, "bold")).pack(side="left", padx=(0, 18))
        search = ctk.CTkEntry(controls, textvariable=self.voice_query, placeholder_text="搜索音色名称…", width=200,
            height=34, fg_color="white", border_color=LINE, corner_radius=9)
        search.pack(side="left", fill="x", expand=True, padx=(0, 8))
        search.bind("<Return>", lambda _: self._voice_refresh(1))
        self.voice_refresh_button = self._voice_button(controls, "刷新", lambda: self._voice_refresh(1), width=65)
        self.voice_refresh_button.pack(side="left", padx=(0, 8))
        self.voice_create_button = self._voice_button(controls, "+ 新建音色", self._voice_open_clone, primary=True, width=105)
        self.voice_create_button.pack(side="left")
        self.voice_list_frame = ctk.CTkScrollableFrame(panel, fg_color="transparent", corner_radius=0,
            scrollbar_button_color="#D4DCE9", scrollbar_button_hover_color="#B6C4DB")
        self.voice_list_frame.grid(row=1, column=0, sticky="nsew")
        self.voice_card_columns = 2
        self._voice_render_rows()
        paging = ctk.CTkFrame(panel, fg_color="transparent")
        paging.grid(row=2, column=0, sticky="ew", pady=(12, 0))
        self.voice_previous = self._voice_button(paging, "← 上一页", lambda: self._voice_refresh(self.voice_page_number - 1), width=85)
        self.voice_previous.pack(side="left")
        ctk.CTkLabel(paging, textvariable=self.voice_page_note, text_color=MUTED).pack(side="left", padx=16)
        self.voice_next = self._voice_button(paging, "下一页 →", lambda: self._voice_refresh(self.voice_page_number + 1), width=85)
        self.voice_next.pack(side="left")
        self._voice_button(paging, "停止试听", self._voice_stop_preview, width=90).pack(side="right")
        self.voice_previous.configure(state="disabled")
        self.voice_next.configure(state="disabled")

    def _voice_render_rows(self):
        for widget in self.voice_list_frame.winfo_children():
            widget.destroy()
        cols = self.voice_card_columns
        for col in range(3):
            self.voice_list_frame.grid_columnconfigure(col, weight=1 if col < cols else 0, uniform="voices" if col < cols else "")
        self.voice_library_count.set(f"我的音色  {len(self.voice_rows)}" if self.voice_loaded else "我的音色")
        if not self.voice_rows:
            empty = self._voice_surface(self.voice_list_frame)
            empty.grid(row=0, column=0, columnspan=cols, sticky="ew", pady=8)
            ctk.CTkLabel(empty, text="让熟悉的声音继续创作", text_color=INK, font=("Microsoft YaHei UI", 18, "bold")).pack(pady=(50, 8))
            ctk.CTkLabel(empty, text="刷新已有音色，或上传录音创建一个新音色。", text_color=MUTED).pack(pady=(0, 50))
            return
        colors = [("#EDF2FF", "#526DB0"), ("#EBF5F2", "#518978"), ("#FFF3E8", "#B68550"), ("#F3EEFA", "#8D70B3")]
        self.voice_description_labels = []
        for index, voice in enumerate(self.voice_rows):
            card = self._voice_surface(self.voice_list_frame)
            card.grid(row=index // cols, column=index % cols, sticky="nsew", padx=5, pady=5)
            card.grid_columnconfigure(0, weight=1)
            head = ctk.CTkFrame(card, fg_color="transparent")
            head.grid(row=0, column=0, sticky="ew", padx=16, pady=(16, 10))
            tone, ink = colors[index % len(colors)]
            ctk.CTkLabel(head, text=(voice.get("title") or "声")[:1].upper(), width=38, height=38,
                fg_color=tone, corner_radius=12, text_color=ink, font=("Microsoft YaHei UI", 17, "bold")).pack(side="left", padx=(0, 10))
            ctk.CTkLabel(head, text=voice.get("title") or "未命名音色", text_color=INK, anchor="w",
                wraplength=240, justify="left", font=("Microsoft YaHei UI", 13, "bold")).pack(side="left", fill="x", expand=True)
            description = voice.get("description") or ("备注暂未读取，刷新可重试" if voice.get("description_unavailable") else "尚未填写音色备注")
            label = ctk.CTkLabel(card, text=description, text_color=MUTED, wraplength=285,
                justify="left", anchor="nw", height=48, font=("Microsoft YaHei UI", 12))
            label.grid(row=1, column=0, sticky="nsew", padx=16, pady=(0, 14))
            card.grid_rowconfigure(1, weight=1)
            self.voice_description_labels.append(label)
            actions = ctk.CTkFrame(card, fg_color="transparent")
            actions.grid(row=2, column=0, sticky="ew", padx=16, pady=(0, 16))
            sample = self._voice_button(actions, "▷ 试听", lambda v=voice: self._voice_preview(v), width=78)
            sample.pack(side="left")
            sample.configure(state="normal" if voice.get("sample_url") else "disabled")
            self._voice_button(actions, "使用音色 →", lambda v=voice: self._voice_select(v), primary=True, width=105).pack(side="right")

    def _build_voice_speech(self, panel):
        panel.grid_columnconfigure(0, weight=3, uniform="speech")
        panel.grid_columnconfigure(1, weight=2, uniform="speech")
        panel.grid_rowconfigure(0, weight=1)
        editor = self._voice_surface(panel)
        editor.grid(row=0, column=0, sticky="nsew", padx=(0, 12))
        editor.grid_columnconfigure(0, weight=1)
        editor.grid_rowconfigure(3, weight=1)
        heading = ctk.CTkFrame(editor, fg_color="transparent")
        heading.grid(row=0, column=0, sticky="ew", padx=20, pady=(16, 12))
        ctk.CTkLabel(heading, text="创作一段声音", text_color=INK, font=("Microsoft YaHei UI", 16, "bold")).pack(side="left")
        ctk.CTkLabel(heading, text="Drama 3", text_color="#687997", fg_color="#F1F4F9", corner_radius=6,
            width=80, height=25, font=("Segoe UI", 11)).pack(side="right")
        voice_box = ctk.CTkFrame(editor, fg_color="#F7F9FD", corner_radius=10)
        voice_box.grid(row=1, column=0, sticky="ew", padx=20, pady=(0, 12))
        top = ctk.CTkFrame(voice_box, fg_color="transparent")
        top.pack(fill="x", padx=10, pady=(8, 0))
        self.voice_menu = ctk.CTkOptionMenu(top, variable=self.voice_selection, values=["请选择音色"], dynamic_resizing=False,
            command=lambda _: self._voice_update_selection_detail(), fg_color="#F7F9FD", button_color="#F7F9FD",
            button_hover_color="#E8EEF8", text_color=INK, dropdown_fg_color="white", dropdown_text_color=INK,
            dropdown_hover_color="#EEF3FF", font=("Microsoft YaHei UI", 13, "bold"), corner_radius=7)
        self.voice_menu.pack(side="left", fill="x", expand=True)
        self._voice_button(top, "换音色", lambda: self._voice_choose_tab("我的声音"), width=65, height=28).pack(side="right", padx=(6, 0))
        self.voice_selected_description = StringVar(value="从「我的声音」挑选一个音色")
        self.voice_selected_description_label = ctk.CTkLabel(voice_box, textvariable=self.voice_selected_description,
            text_color=MUTED, wraplength=360, anchor="w", justify="left", font=("Microsoft YaHei UI", 11))
        self.voice_selected_description_label.pack(fill="x", padx=15, pady=(4, 10))
        text_heading = ctk.CTkFrame(editor, fg_color="transparent")
        text_heading.grid(row=2, column=0, sticky="ew", padx=20, pady=(0, 6))
        ctk.CTkLabel(text_heading, text="文案", text_color=INK, font=("Microsoft YaHei UI", 12, "bold")).pack(side="left")
        self.voice_char_count = StringVar(value="0 / 2000")
        ctk.CTkLabel(text_heading, textvariable=self.voice_char_count, text_color=MUTED, font=("Segoe UI", 11)).pack(side="right")
        self.voice_text = ctk.CTkTextbox(editor, height=105, wrap="word", border_width=1, border_color=LINE,
            fg_color="white", corner_radius=9, font=("Microsoft YaHei UI", 13), text_color=INK)
        self.voice_text.grid(row=3, column=0, sticky="nsew", padx=20, pady=(0, 12))
        self.voice_text.bind("<KeyRelease>", lambda _: self._voice_update_char_count(), add="+")
        def text_changed(_event=None):
            if self.voice_text._textbox.edit_modified():
                self._voice_update_char_count()
                self.voice_text._textbox.edit_modified(False)
        self.voice_text._textbox.bind("<<Modified>>", text_changed, add="+")
        settings = ctk.CTkFrame(editor, fg_color="transparent")
        settings.grid(row=4, column=0, sticky="ew", padx=20, pady=(0, 8))
        settings.grid_columnconfigure((0, 1), weight=1, uniform="sound_controls")
        self.voice_control_sliders = {}
        for col, (name, variable, low, high, steps, unit) in enumerate((("语速", self.voice_speed, .7, 1.3, 60, "倍"), ("音量", self.voice_volume, -5., 5., 20, "dB"))):
            group = ctk.CTkFrame(settings, fg_color="#F7F9FD", corner_radius=9)
            group.grid(row=0, column=col, sticky="ew", padx=(0, 6) if col == 0 else (6, 0))
            labelrow = ctk.CTkFrame(group, fg_color="transparent")
            labelrow.pack(fill="x", padx=11, pady=(8, 0))
            ctk.CTkLabel(labelrow, text=name, text_color=MUTED, font=("Microsoft YaHei UI", 11)).pack(side="left")
            value = StringVar()
            def update(*_, var=variable, label=value, suffix=unit):
                label.set(f"{var.get():.2f} 倍" if suffix == "倍" else f"{var.get():+.1f} dB")
            variable.trace_add("write", update)
            update()
            ctk.CTkLabel(labelrow, textvariable=value, text_color=INK, font=("Segoe UI", 12, "bold")).pack(side="right")
            slider = ctk.CTkSlider(group, from_=low, to=high, number_of_steps=steps, variable=variable,
                width=120, height=16, button_length=13, button_color=BLUE, progress_color=BLUE,
                fg_color="#DEE5F0")
            slider.pack(fill="x", padx=10, pady=(5, 12))
            self.voice_control_sliders[name] = slider
        bottom = ctk.CTkFrame(editor, fg_color="transparent")
        bottom.grid(row=5, column=0, sticky="ew", padx=20, pady=(0, 10))
        ctk.CTkLabel(bottom, text="同一文案 · 两种演绎", text_color=MUTED, font=("Microsoft YaHei UI", 11)).pack(side="left")
        self._voice_button(bottom, "恢复默认", self._voice_reset_controls, width=75, height=27).pack(side="right")
        self.voice_submit_button = self._voice_button(editor, "生成两个版本", self._voice_submit, primary=True, height=42)
        self.voice_submit_button.grid(row=6, column=0, sticky="ew", padx=20, pady=(0, 8))
        ctk.CTkLabel(editor, text="预估会在 Fish 创建草稿；确认费用后生成两个版本。", text_color=MUTED, wraplength=390,
            justify="left", font=("Microsoft YaHei UI", 10)).grid(row=7, column=0, sticky="w", padx=20, pady=(0, 15))
        results = ctk.CTkFrame(panel, fg_color="transparent")
        results.grid(row=0, column=1, sticky="nsew")
        results.grid_columnconfigure(0, weight=1)
        results.grid_rowconfigure((1, 2), weight=1, uniform="takes")
        title = ctk.CTkFrame(results, fg_color="transparent")
        title.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        ctk.CTkLabel(title, text="挑选你喜欢的版本", text_color=INK, font=("Microsoft YaHei UI", 15, "bold")).pack(side="left")
        self._voice_button(title, "历史", lambda: self._show_page("history"), width=50, height=28).pack(side="right")
        self.voice_result_widgets = []
        for index in range(2):
            card = self._voice_surface(results)
            card.grid(row=index + 1, column=0, sticky="nsew", pady=(0, 10) if index == 0 else (0, 0))
            card.grid_columnconfigure(0, weight=1)
            card.grid_rowconfigure(2, weight=1)
            top = ctk.CTkFrame(card, fg_color="transparent")
            top.grid(row=0, column=0, sticky="ew", padx=16, pady=(14, 7))
            ctk.CTkLabel(top, text=f"版本 {'AB'[index]}", text_color=INK, font=("Microsoft YaHei UI", 15, "bold")).pack(side="left")
            state = ctk.CTkLabel(top, text="待生成", text_color=MUTED, fg_color="#F1F4F9", corner_radius=6,
                width=66, height=25, font=("Microsoft YaHei UI", 10))
            state.pack(side="right")
            note = ctk.CTkLabel(card, text="生成完成后，在这里试听和保存", text_color=MUTED, wraplength=260,
                justify="left", anchor="w", font=("Microsoft YaHei UI", 11))
            note.grid(row=1, column=0, sticky="ew", padx=16, pady=(0, 8))
            play_area = ctk.CTkFrame(card, fg_color="#F7F9FD", corner_radius=10)
            play_area.grid(row=2, column=0, sticky="nsew", padx=16, pady=(0, 12))
            play_area.grid_columnconfigure(1, weight=1)
            play_area.grid_rowconfigure(0, weight=1)
            play = self._voice_button(play_area, "▶", lambda i=index: self._voice_play_result(i), primary=True, width=42, height=42)
            play.grid(row=0, column=0, padx=12, pady=10)
            duration = ctk.CTkLabel(play_area, text="等待声音就绪", text_color=MUTED, anchor="w", font=("Microsoft YaHei UI", 11))
            duration.grid(row=0, column=1, sticky="ew", padx=(0, 10))
            actions = ctk.CTkFrame(card, fg_color="transparent")
            actions.grid(row=3, column=0, sticky="ew", padx=16, pady=(0, 14))
            save = self._voice_button(actions, "保存", lambda i=index: self._voice_save_result(i), width=60, height=29)
            save.pack(side="left", padx=(0, 6))
            library = self._voice_button(actions, "入库", lambda i=index: self._voice_import_result(i), width=60, height=29)
            library.pack(side="left")
            recover = self._voice_button(actions, "继续获取", lambda i=index: self._voice_resume_result(i), width=80, height=29)
            recover.pack(side="right")
            self.voice_result_widgets.append(dict(state=state, note=note, play=play, duration=duration, save=save, library=library, recover=recover))
        self._voice_render_results()

    def _voice_choose_tab(self, name):
        self.voice_view.set(name)
        self.voice_library_panel.grid_remove()
        self.voice_speech_panel.grid_remove()
        panel = self.voice_library_panel if name == "我的声音" else self.voice_speech_panel
        panel.grid(row=0, column=0, sticky="nsew")
        self._voice_layout_changed()

    def _voice_update_char_count(self):
        self.voice_char_count.set(f"{len(self.voice_text.get('1.0', 'end-1c'))} / 2000")

    def _voice_update_selection_detail(self):
        voice = self.voice_choices.get(self.voice_selection.get(), {})
        self.voice_selected_description.set(voice.get("description") or "尚未填写音色备注" if voice else "从「我的声音」挑选一个音色")

    def _voice_render_results(self, jobs=None):
        from voice_batch import can_resume
        if jobs is not None:
            self.voice_result_jobs = list(jobs)
        for index, widgets in enumerate(self.voice_result_widgets):
            job = next((j for j in self.voice_result_jobs if j.get("variant_index") == index + 1), {})
            result, config = job.get("result", {}), job.get("config", {})
            path = result.get("output")
            ready = bool(path and Path(path).is_file() and result.get("local_downloaded"))
            state = job.get("status", "empty")
            labels = {"empty": "待生成", "queued": "待生成", "running": "生成中", "success": "已完成",
                      "attention": "待处理", "failed": "未完成", "paused": "已暂停", "cancelled": "已取消"}
            widgets["state"].configure(text=labels.get(state, "待处理"), text_color="#43846B" if ready else MUTED,
                fg_color="#EDF7F2" if ready else "#F1F4F9")
            note = (config.get("voice_name") or "已生成") if ready else job.get("error") or job.get("stage") or "生成完成后，在这里试听和保存"
            if ready and result.get("audio_settings_mode") == "fish_original":
                note += " · Fish 原始音频，未应用旧版本地调整"
            widgets["note"].configure(text=note)
            seconds = result.get("duration")
            widgets["duration"].configure(text=f"{float(seconds):.1f} 秒 · WAV" if ready and seconds else "WAV · 已就绪" if ready else "正在生成…" if state == "running" else "等待声音就绪")
            playing = ready and self.voice_playing_result == index and self.voice_player.process is not None
            widgets["play"].configure(state="normal" if ready else "disabled", text="Ⅱ" if playing and not self.voice_player.paused else "▶")
            widgets["save"].configure(state="normal" if ready else "disabled")
            widgets["library"].configure(state="normal" if ready else "disabled")
            resumable = not ready and bool(job) and can_resume(job)
            widgets["recover"].configure(state="normal" if resumable and not self.voice_busy else "disabled")
            if resumable:
                widgets["recover"].pack(side="right")
            else:
                widgets["recover"].pack_forget()
