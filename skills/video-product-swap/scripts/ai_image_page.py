"""A single AI image form with optional fission assistance and flat job results."""
from __future__ import annotations

import copy
import shutil
import threading
import time
import uuid
from collections import OrderedDict
from pathlib import Path
from tkinter import Canvas, StringVar, Toplevel, filedialog, messagebox

import customtkinter as ctk
from PIL import ImageTk

from ai_image_assistant import generate_prompt
from ai_image_display import ImageDisplay
from ai_image_models import (BUILTIN_MODELS, DEFAULT_MODEL, DEFAULT_PARAMETERS, FOLLOW, MODEL_LABELS, capability,
                             PARAMETER_LABELS, QUALITY_LABELS, normalize_parameters, properties, reference_limit)
from ai_image_store import (AIImageStore, fingerprint, generation_snapshot,
                            helper_snapshot, validate_references)
from director_prompt import configuration
from fish_mcp import FishClient
from fission_service import inspect_image, prepare_quote, actual_prompt


class AIImagePage:
    def _init_ai_image(self):
        self.ai_image_store = AIImageStore(self.desktop_store)
        self.ai_image_busy = False
        self.ai_model_busy = False
        self._ai_loading = False
        self._ai_form_generation = 0
        self._ai_save_timer = None
        self._ai_preview_timer = None
        self._ai_preview_path = None
        self._ai_preview_ref = None
        self._ai_preview_image = None
        self._ai_preview_available = False
        self._ai_preview_zoom = 1.0
        self._ai_preview_token = 0
        self._ai_preview_requested = None
        self._ai_preview_pending = False
        self._ai_thumbnails = OrderedDict()
        self._ai_thumb_waiters = {}
        self._ai_reference_signature = None
        self._ai_result_cards = {}
        self._ai_empty_result = None
        self.ai_display = ImageDisplay(lambda *payload: self.events.put(("ai_image_event", payload)))
        self._ai_results_signature = None
        self._ai_models = copy.deepcopy(BUILTIN_MODELS)
        self._ai_models.update(self.ai_image_store.model_cache())
        self._ai_catalog_ids = list(MODEL_LABELS)
        self._ai_live_models = set()
        self._ai_model_request = None
        self._ai_catalog_loaded = False
        self._ai_not_before = 0
        self.ai_status = StringVar(value="输入提示词即可生图，也可以添加参考图片。")
        self.ai_model_var = StringVar(value=MODEL_LABELS[DEFAULT_MODEL])
        self.ai_parameter_vars = {key: StringVar() for key in PARAMETER_LABELS}
        self.ai_reference_count = StringVar(value="参考图 · 可选")

    def _build_ai_image_page(self):
        top = ctk.CTkFrame(self.ai_image_tab, fg_color="transparent")
        top.pack(fill="x", padx=22, pady=(18, 12))
        ctk.CTkLabel(top, text="AI 生图", font=("Microsoft YaHei UI", 26, "bold")).pack(side="left")
        ctk.CTkLabel(top, text="文字创作 · 多图参考 · 细节对比", text_color="#6E6E73").pack(side="left", padx=16)
        self._secondary_button(top, "账户设置", lambda: self._show_page("api"), width=86).pack(side="right")
        body = ctk.CTkFrame(self.ai_image_tab, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=18, pady=(0, 16))
        body.grid_columnconfigure(0, weight=1, minsize=380)
        body.grid_columnconfigure(1, weight=2, minsize=370)
        body.grid_rowconfigure(0, weight=1)

        left = ctk.CTkFrame(body, width=380, height=1, fg_color="white", corner_radius=14)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 12))
        left.grid_propagate(False)
        left.grid_columnconfigure(0, weight=1); left.grid_rowconfigure(0, weight=1)
        form = self.ai_form_panel = ctk.CTkScrollableFrame(left, fg_color="white", corner_radius=12)
        form.grid(row=0, column=0, sticky="nsew", padx=6, pady=(6, 0))
        self.ai_ref_section = ctk.CTkFrame(form, fg_color="transparent")
        self.ai_ref_section.pack(fill="x", padx=6, pady=(6, 4))
        ctk.CTkLabel(self.ai_ref_section, textvariable=self.ai_reference_count, font=("Microsoft YaHei UI", 13, "bold")).pack(anchor="w")
        ref_actions = ctk.CTkFrame(self.ai_ref_section, fg_color="transparent"); ref_actions.pack(fill="x", pady=6)
        add = self._secondary_button(ref_actions, "添加图片", self._ai_choose_images, width=96)
        add.configure(height=30); add.pack(side="left")
        library = self._secondary_button(ref_actions, "从素材库选择", lambda: self._open_library("ai_image"), width=120)
        library.configure(height=30); library.pack(side="left", padx=8)
        self.ai_reference_box = ctk.CTkFrame(self.ai_ref_section, height=128, fg_color="transparent")
        self.ai_reference_box.pack(fill="x"); self.ai_reference_box.pack_propagate(False)
        self.ai_reference_grid = ctk.CTkScrollableFrame(self.ai_reference_box, fg_color="#F6F7F9", corner_radius=10)
        self.ai_reference_grid.pack(fill="both", expand=True)
        self.ai_reference_grid.grid_columnconfigure((0, 1, 2), weight=1, uniform="ai_refs")
        ctk.CTkLabel(self.ai_ref_section, text="点击编号插入提示词；不加图为文生图。", text_color="#86868B",
                     font=("Microsoft YaHei UI", 10)).pack(anchor="w", pady=(4, 0))

        header = ctk.CTkFrame(form, fg_color="transparent"); header.pack(fill="x", padx=6, pady=(10, 8))
        ctk.CTkLabel(header, text="提示词", font=("Microsoft YaHei UI", 15, "bold")).pack(side="left")
        self.ai_helper_toggle = self._secondary_button(header, "AI 美工", self._ai_toggle_helper, width=105)
        self.ai_helper_toggle.configure(height=30); self.ai_helper_toggle.pack(side="right")
        self.ai_helper_panel = ctk.CTkFrame(form, fg_color="#F2F6FB", corner_radius=10)
        self._build_ai_helper(self.ai_helper_panel)
        self.ai_prompt = ctk.CTkTextbox(form, height=112, wrap="word", border_width=1, border_color="#E2E5EA")
        self.ai_prompt.pack(fill="x", padx=6)

        settings = ctk.CTkFrame(left, fg_color="transparent")
        settings.grid(row=1, column=0, sticky="ew", padx=14, pady=(10, 0))
        row = ctk.CTkFrame(settings, fg_color="transparent"); row.pack(fill="x", pady=(0, 7))
        ctk.CTkLabel(row, text="生成设置", font=("Microsoft YaHei UI", 13, "bold")).pack(side="left")
        reset = self._secondary_button(row, "恢复默认", self._ai_reset_parameters, width=84)
        reset.configure(height=28); reset.pack(side="right")
        self.ai_model_refresh = self._secondary_button(row, "刷新", lambda: self._ai_refresh_models(catalog=True, force=True), width=54)
        self.ai_model_refresh.configure(height=28); self.ai_model_refresh.pack(side="right", padx=6)
        self.ai_model_menu = ctk.CTkOptionMenu(settings, variable=self.ai_model_var, values=list(MODEL_LABELS.values()),
                                             command=self._ai_model_changed, dynamic_resizing=False)
        self.ai_model_menu.pack(fill="x", pady=(0, 8))
        self.ai_parameter_menus = {}
        parameters = ctk.CTkFrame(settings, fg_color="transparent"); parameters.pack(fill="x")
        parameters.grid_columnconfigure((0, 1, 2), weight=1, uniform="ai_parameters")
        for i, (key, label) in enumerate(PARAMETER_LABELS.items()):
            row = ctk.CTkFrame(parameters, fg_color="transparent")
            row.grid(row=0, column=i, sticky="ew", padx=(0 if i == 0 else 5, 0))
            ctk.CTkLabel(row, text=label, height=20, anchor="w", font=("Microsoft YaHei UI", 10)).pack(fill="x", pady=(0, 3))
            menu = ctk.CTkOptionMenu(row, variable=self.ai_parameter_vars[key], values=["模型默认"],
                                   command=lambda _: self._ai_save(), dynamic_resizing=False, width=80)
            menu.pack(fill="x")
            self.ai_parameter_menus[key] = menu

        footer = ctk.CTkFrame(left, fg_color="white"); footer.grid(row=2, column=0, sticky="ew", padx=14, pady=(12, 12))
        self.ai_generate_button = ctk.CTkButton(footer, text="预估费用并生成一张", command=self._ai_quote, height=42)
        self.ai_generate_button.pack(fill="x")
        ctk.CTkLabel(footer, text="参考图预估时上传至 Fish；确认费用后生成。", text_color="#86868B",
                     font=("Microsoft YaHei UI", 10)).pack(anchor="w", pady=(4, 0))
        self.ai_status_label = ctk.CTkLabel(footer, textvariable=self.ai_status, text_color="#0071E3", wraplength=335,
                                         justify="left", anchor="w")
        self.ai_status_label.pack(fill="x", pady=(4, 0))

        right = ctk.CTkFrame(body, width=370, height=1, fg_color="white", corner_radius=14)
        right.grid(row=0, column=1, sticky="nsew"); right.grid_columnconfigure(0, weight=1); right.grid_rowconfigure(1, weight=1)
        right.grid_propagate(False)
        preview_header = ctk.CTkFrame(right, height=92, fg_color="transparent")
        preview_header.grid(row=0, column=0, padx=14, pady=(12, 4), sticky="ew")
        preview_header.grid_propagate(False)
        preview_header.grid_columnconfigure(0, weight=1)
        preview_header.grid_rowconfigure(1, weight=1)
        self.ai_preview_title = ctk.CTkLabel(preview_header, text="结果预览", font=("Microsoft YaHei UI", 15, "bold"))
        self.ai_preview_title.grid(row=0, column=0, sticky="w")
        self.ai_result_status = ctk.CTkLabel(preview_header, text="", text_color="#6E6E73", wraplength=330, justify="left", anchor="w")
        self.ai_result_status.grid(row=1, column=0, sticky="nsew")
        zoom_bar = ctk.CTkFrame(preview_header, fg_color="transparent")
        zoom_bar.grid(row=2, column=0, sticky="ew", pady=(4, 0))
        self.ai_zoom_label = ctk.CTkLabel(zoom_bar, text="适应窗口", width=78, anchor="w", text_color="#86868B")
        self.ai_zoom_label.pack(side="left")
        self.ai_zoom_buttons = {}
        for key, label, command, width in (("out", "−", lambda: self._ai_zoom_preview(1 / 1.25), 32),
                                          ("in", "＋", lambda: self._ai_zoom_preview(1.25), 32),
                                          ("fit", "适应窗口", self._ai_fit_preview, 86)):
            button = self._secondary_button(zoom_bar, label, command, width=width)
            button.configure(height=28, corner_radius=8); button.pack(side="left", padx=(0, 6))
            self.ai_zoom_buttons[key] = button
        # The canvas owns its viewport; image dimensions never become layout requests.
        self.ai_preview = Canvas(right, width=1, height=1, bg="white", highlightthickness=0)
        self.ai_preview.grid(row=1, column=0, sticky="nsew", padx=12, pady=8)
        self.ai_preview.bind("<Configure>", self._ai_resize_preview)
        self.ai_preview.bind("<Double-1>", lambda _: self._ai_compare())
        self.ai_preview.bind("<MouseWheel>", self._ai_preview_wheel)
        self.ai_preview.bind("<ButtonPress-1>", lambda e: self.ai_preview.scan_mark(e.x, e.y))
        self.ai_preview.bind("<B1-Motion>", lambda e: self.ai_preview.scan_dragto(e.x, e.y, gain=1))
        actions = ctk.CTkFrame(right, fg_color="transparent"); actions.grid(row=2, column=0, sticky="ew", padx=12, pady=(4, 8))
        actions.grid_columnconfigure((0, 1, 2), weight=1, uniform="ai_actions")
        self.ai_result_buttons = {}
        for i, (key, label, command) in enumerate((
                ("compare", "放大 / 对比", self._ai_compare), ("save", "另存图片", self._ai_export),
                ("modify", "作为参考图", self._ai_use_as_reference), ("reuse", "复用参数", self._ai_reuse_selected),
                ("frame", "用作视频首帧", self._ai_use_as_frame), ("recover", "任务详情 / 恢复", self._ai_recover))):
            button = self._secondary_button(actions, label, command, width=92)
            button.grid(row=i // 3, column=i % 3, padx=3, pady=3, sticky="ew")
            self.ai_result_buttons[key] = button
        recent_header = ctk.CTkFrame(right, fg_color="transparent"); recent_header.grid(row=3, column=0, sticky="ew", padx=14, pady=(0, 4))
        ctk.CTkLabel(recent_header, text="最近生成", text_color="#6E6E73").pack(side="left")
        self._secondary_button(recent_header, "使用记录", self._ai_show_history, width=84).pack(side="right")
        self.ai_feedback_button = self._secondary_button(recent_header, "结果反馈", lambda: self._assistant_feedback(self._ai_selected_job()), width=84)
        self.ai_feedback_button.pack(side="right", padx=6)
        self.ai_results = ctk.CTkScrollableFrame(right, orientation="horizontal", height=112, fg_color="#F6F7F9")
        self.ai_results.grid(row=4, column=0, sticky="ew", padx=12, pady=(0, 12))
        for box in (self.ai_prompt, self.ai_requirements):
            box._textbox.bind("<<Modified>>", self._ai_edited, add=True)
        self._ai_render_form()
        self._ai_render_results()

    def _build_ai_helper(self, panel):
        ctk.CTkLabel(panel, text="描述你想要的效果", font=("Microsoft YaHei UI", 12, "bold")).pack(anchor="w", padx=10, pady=(10, 4))
        ctk.CTkLabel(panel, text="例如：把手里的产品换成图2，保留人物和动作，\n光线自然，瓶身和手指接触真实。", justify="left", anchor="w",
                     text_color="#6E6E73", font=("Microsoft YaHei UI", 11)).pack(fill="x", padx=10, pady=(0, 6))
        self.ai_requirements = ctk.CTkTextbox(panel, height=88, wrap="word")
        self.ai_requirements.pack(fill="x", padx=10)
        row = ctk.CTkFrame(panel, fg_color="transparent"); row.pack(fill="x", padx=10, pady=8)
        self.ai_helper_generate = ctk.CTkButton(row, text="生成提示词", command=self._ai_generate_helper, width=108)
        self.ai_helper_generate.pack(side="left")
        self.ai_helper_undo = self._secondary_button(row, "撤销填入", self._ai_undo_helper, width=80)
        self.ai_helper_undo.pack(side="left", padx=6)
        self.ai_helper_view = self._secondary_button(row, "查看稿件", self._ai_view_helper, width=80)
        self.ai_helper_view.pack(side="left")
        ctk.CTkLabel(panel, text="AI 根据需求与参考图整理提示词，可继续编辑。\n使用现有 Kaiyun 设置，按用量计费。", justify="left",
                     text_color="#6E6E73", font=("Microsoft YaHei UI", 10)).pack(anchor="w", padx=10, pady=(0, 10))

    def _ai_toggle_helper(self):
        if self.ai_helper_panel.winfo_manager(): self.ai_helper_panel.pack_forget()
        else:
            self.ai_helper_panel.pack(fill="x", padx=6, pady=(0, 10), before=self.ai_prompt)
            self.ai_requirements.focus_set()
            self.root.after_idle(self._ai_reveal_helper)

    def _ai_reveal_helper(self):
        if not self.ai_helper_panel.winfo_manager(): return
        self.ai_form_panel.update_idletasks()
        offset = max(0, self.ai_helper_panel.winfo_y() - 40)
        self.ai_form_panel._parent_canvas.yview_moveto(offset / max(1, self.ai_form_panel.winfo_height()))

    def _ai_model_id(self):
        return self._ai_model_labels.get(self.ai_model_var.get(), DEFAULT_MODEL)

    def _ai_model(self):
        return self._ai_models.get(self._ai_model_id(), {})

    def _ai_edited(self, event):
        if not event.widget.edit_modified(): return
        event.widget.edit_modified(False)
        if self._ai_loading: return
        if self._ai_save_timer: self.root.after_cancel(self._ai_save_timer)
        self._ai_save_timer = self.root.after(400, self._ai_save)

    def _ai_save(self):
        if not hasattr(self, "ai_prompt") or self._ai_loading: return
        if self._ai_save_timer:
            self.root.after_cancel(self._ai_save_timer); self._ai_save_timer = None
        params = {}
        for key, var in self.ai_parameter_vars.items():
            value = var.get()
            if value == "模型默认" or not value: continue
            if key == "quality": value = next((k for k, v in QUALITY_LABELS.items() if v == value), value)
            if value == "自动": value = "auto"
            params[key] = value
        fields = {"model": self._ai_model_id(), "parameters": params, "prompt": self.ai_prompt.get("1.0", "end-1c"),
                  "assistant": {"request": self.ai_requirements.get("1.0", "end-1c")}}
        current = self.ai_image_store.get()
        if any(current[k] != value for k, value in fields.items()): self.ai_image_store.fields(**fields)

    def _ai_render_form(self):
        form = self.ai_image_store.get()
        self._ai_loading = True
        try:
            ids = list(dict.fromkeys([*self._ai_catalog_ids, form["model"]]))
            self._ai_model_labels = {MODEL_LABELS.get(i, i): i for i in ids}
            self.ai_model_menu.configure(values=list(self._ai_model_labels))
            self.ai_model_var.set(MODEL_LABELS.get(form["model"], form["model"]))
            for widget, text in ((self.ai_prompt, form["prompt"]), (self.ai_requirements, form["assistant"]["request"])):
                widget.delete("1.0", "end"); widget.insert("1.0", text); widget._textbox.edit_modified(False)
            self._ai_apply_options(form)
            self._ai_render_references(form)
            self.ai_helper_undo.configure(state="normal" if form.get("undo_prompt") else "disabled")
            self.ai_helper_view.configure(state="normal" if form.get("helper_result") else "disabled")
            self._ai_controls()
        finally: self._ai_loading = False

    def _ai_apply_options(self, form=None):
        form = form or self.ai_image_store.get()
        model = self._ai_models.get(form["model"], {})
        if not model:
            for key, menu in self.ai_parameter_menus.items():
                value = form["parameters"].get(key, "模型默认")
                if key == "quality": value = QUALITY_LABELS.get(value, value)
                menu.configure(values=[value], state="disabled")
                self.ai_parameter_vars[key].set(value)
            self.ai_status.set("请刷新当前模型规格后再生成。")
            self.ai_reference_count.set(f"参考图 {len(form['references'])} · 等待规格")
            return
        specs = properties(model, bool(form["references"]))
        normalized, changes = normalize_parameters(model, form["parameters"], bool(form["references"]))
        if model and normalized != form["parameters"]:
            form = self.ai_image_store.fields(parameters=normalized)
        for key, menu in self.ai_parameter_menus.items():
            options = list(specs.get(key, {}).get("enum", []))
            if options and key == "aspect_ratio" and form["references"]: options.insert(0, FOLLOW)
            displayed = [QUALITY_LABELS.get(o, o) if key == "quality" else "自动" if o == "auto" else o for o in options]
            value = form["parameters"].get(key)
            if key == "quality": value = QUALITY_LABELS.get(value, value)
            if value == "auto": value = "自动"
            menu.configure(values=displayed or ["模型默认"], state="normal" if options else "disabled")
            self.ai_parameter_vars[key].set(value if value in displayed else "模型默认")
        if changes: self.ai_status.set("；".join(changes) + "。参考图和提示词已保留。")
        limit = reference_limit(model)
        count = len(form["references"])
        self.ai_reference_count.set(f"参考图 {count} / {limit}" if model else f"参考图 {count} · 等待规格")
        if count > limit and model:
            self.ai_status.set(f"当前模型最多支持 {limit} 张参考，已保留全部 {count} 张，请减少参考或切换模型。")

    def _ai_controls(self):
        busy = self.ai_image_busy or self.ai_model_busy
        form = self.ai_image_store.get()
        supported = bool(capability(self._ai_model(), bool(form["references"])))
        self.ai_generate_button.configure(state="disabled" if busy or (not supported and not form.get("pending_submission")) else "normal",
            text="核对上次提交" if self.ai_image_store.get().get("pending_submission") else "预估费用并生成一张")
        self.ai_helper_generate.configure(state="disabled" if busy else "normal")
        self.ai_model_refresh.configure(state="disabled" if self.ai_model_busy else "normal")

    def _ai_set_busy(self, busy, note=None):
        self.ai_image_busy = busy
        self._ai_controls()
        if note: self.ai_status.set(note)

    def _ai_work(self, kind, context, work):
        generation = self._ai_form_generation
        def run():
            try: result, error = work(), None
            except Exception as exc: result, error = None, str(exc)
            self.events.put(("ai_image_event", (kind, context, result, error, generation)))
        threading.Thread(target=run, daemon=True).start()

    def _ai_on_show(self):
        pending = self.ai_image_store.pending_job()
        if pending: self._ai_reset_after_submission(pending["id"])
        else: self._ai_render_form(); self._ai_render_results()
        if not self.demo and self.fish_workspaces and not self._ai_catalog_loaded:
            self._ai_refresh_models(catalog=True)

    def _ai_model_changed(self, _=None):
        self._ai_save()
        self._ai_apply_options(); self._ai_save(); self._ai_controls()
        if not self.demo and self.fish_workspaces and self._ai_model_id() not in self._ai_live_models:
            self._ai_refresh_models()

    def _ai_reset_parameters(self):
        self._ai_save()
        self.ai_image_store.fields(model=DEFAULT_MODEL, parameters=copy.deepcopy(DEFAULT_PARAMETERS))
        self._ai_render_form()
        self.ai_status.set("已恢复 Sunburst · 1K · 中等精度；提示词和参考图保留。")

    def _ai_refresh_models(self, catalog=False, force=False):
        if self.ai_model_busy or self.ai_image_busy or self.fish_login_busy: return
        model_id = self._ai_model_id()
        if not force and model_id in self._ai_live_models and not catalog: return
        self._ai_save()
        self.ai_model_busy = True; self._ai_controls()
        token = self._ai_model_request = uuid.uuid4().hex
        auth = self.fish_auth_path
        def work():
            with FishClient(auth) as client:
                listing = client.call("list_media_models", {"media_type": "image"}) if catalog else None
                detail = client.call("get_media_model", {"model_id": model_id})
                return {"listing": listing, "detail": detail}
        self._ai_work("models", (token, model_id), work)

    def _ai_choose_images(self):
        if self.ai_image_busy: return
        paths = filedialog.askopenfilenames(parent=self.root, title="添加参考图片（可多选）", filetypes=[("图片", "*.jpg *.jpeg *.png *.webp")])
        if paths: self._ai_add_paths(paths)

    def _ai_add_paths(self, paths, remote=None):
        if self.ai_image_busy or getattr(self, "_maintenance_busy", False): return
        self._ai_save()
        self._ai_set_busy(True, "正在检查并保存参考图片…")
        paths = list(paths)
        def work():
            imported, errors = [], []
            for path in paths:
                try:
                    info = inspect_image(path)
                    asset = self.desktop_store.import_asset(path)
                    saved = inspect_image(asset["path"])
                    if saved["sha256"] != info["sha256"]: raise ValueError("导入期间图片发生变化")
                    imported.append({**saved, "asset_id": asset["id"], "name": asset["name"], "remote": copy.deepcopy(remote) if remote else {}})
                except Exception as exc: errors.append(Path(path).name + "：" + str(exc))
            return {"references": imported, "errors": errors}
        self._ai_work("references", None, work)

    def _ai_render_references(self, form):
        signature = [(r["asset_id"], r["path"], r["name"]) for r in form["references"]]
        if signature == self._ai_reference_signature: return
        self._ai_reference_signature = signature
        for widget in self.ai_reference_grid.winfo_children(): widget.destroy()
        self.ai_reference_box.configure(height=128 if form["references"] else 80)
        if not form["references"]:
            ctk.CTkLabel(self.ai_reference_grid, text="可一次添加多张图片\n也可以只写文字直接生成", text_color="#86868B", height=56).grid(row=0, column=0, columnspan=3, sticky="ew")
        for index, ref in enumerate(form["references"]):
            panel = ctk.CTkFrame(self.ai_reference_grid, fg_color="white", corner_radius=8)
            panel.grid(row=index // 3, column=index % 3, sticky="nsew", padx=3, pady=3)
            top = ctk.CTkFrame(panel, fg_color="transparent"); top.pack(fill="x", padx=3, pady=(2, 0))
            ctk.CTkButton(top, text=f"@图{index + 1}", width=55, height=22, fg_color="transparent", text_color="#0071E3",
                          command=lambda n=index+1: self._ai_insert_reference(n)).pack(side="left")
            ctk.CTkButton(top, text="×", width=22, height=22, fg_color="transparent", text_color="#86868B",
                          command=lambda i=index: self._ai_remove_reference(i)).pack(side="right")
            preview = ctk.CTkLabel(panel, text="加载中", width=76, height=70)
            preview.pack(padx=3, pady=(2, 4)); preview.bind("<Button-1>", lambda _, i=index: self._ai_preview_reference(i))
            self._ai_bind_thumbnail(preview, ref["path"], (76, 66))

    def _ai_bind_thumbnail(self, widget, path, size):
        key = (path, size)
        widget._ai_thumb_key = key
        if key in self._ai_thumbnails:
            self._ai_thumbnails.move_to_end(key)
            widget.configure(image=self._ai_thumbnails[key], text="")
            return
        widget.configure(image=None, text="加载中")
        pending = key in self._ai_thumb_waiters
        self._ai_thumb_waiters.setdefault(key, []).append(widget)
        if not pending: self.ai_display.thumbnail(key)

    def _ai_insert_reference(self, number):
        self.ai_prompt.insert("insert", f"@图{number} "); self.ai_prompt.focus_set(); self._ai_save()

    def _ai_remove_reference(self, index):
        if self.ai_image_busy: return
        self._ai_save()
        form = self.ai_image_store.get()
        if index >= len(form["references"]): return
        self.ai_image_store.remove_reference(index)
        self._ai_preview_ref = None
        self._ai_render_form(); self._ai_render_preview()
        self.ai_status.set("参考已移除，后续编号已同步；提示词中对应引用会标为【参考图已移除】，请检查。")

    def _ai_generate_helper(self):
        if self.ai_image_busy or self.ai_model_busy or getattr(self, "_maintenance_busy", False): return
        self._ai_save()
        try:
            credentials = self._credential_values(); configuration(credentials)
            form = self.ai_image_store.get()
            if not form["assistant"]["request"].strip(): raise ValueError("请直接描述你想要的效果。")
            if len(form["references"]) > reference_limit(self._ai_model()): raise ValueError("参考数量超过当前模型上限，请先调整。")
            snapshot = helper_snapshot(form)
            limit = min(20000, max(100, self._ai_model().get("prompt_max_chars", 20000) - 600))
            self._ai_set_busy(True, "正在看图并整理提示词…")
            self._ai_work("helper", snapshot, lambda: generate_prompt(snapshot, credentials, limit, personal_root=self.library_transfer.root))
        except Exception as exc: self.ai_status.set(str(exc))

    def _ai_undo_helper(self):
        self._ai_save()
        try:
            self.ai_image_store.undo(); self._ai_render_form(); self.ai_status.set("已撤销辅助填入。")
        except Exception as exc: self.ai_status.set(str(exc))

    def _ai_view_helper(self):
        form = self.ai_image_store.get()
        result = form.get("helper_result")
        if not result: return
        window = Toplevel(self.root, bg="#F5F5F7"); window.title("AI 美工 · 生成稿"); window.geometry("700x520"); window.transient(self.root)
        ctk.CTkLabel(window, text="本次辅助生成稿，可选中文字复制", text_color="#6E6E73").pack(pady=12)
        box = ctk.CTkTextbox(window, wrap="word"); box.pack(fill="both", expand=True, padx=16, pady=(0, 16))
        notice = ""
        if fingerprint(result.get("snapshot", {}).get("references", [])) != fingerprint(form["references"]):
            notice = "参考图已发生变化，以下编号对应生成稿当时的图片，请核对后再使用。\n\n"
        box.insert("1.0", notice + result.get("note", "") + "\n\n" + result["prompt"]); box.configure(state="disabled")
        return window

    def _ai_quote_snapshot(self):
        return {**generation_snapshot(self.ai_image_store.get()), "workspace_id": self.fish_workspaces.get(self.fish_workspace.get()),
                "output_dir": str(Path(self.workspace_output_dir_var.get()).expanduser().resolve())}

    def _ai_quote(self):
        if self.ai_image_busy or self.ai_model_busy or self.fish_login_busy or time.monotonic() < self._ai_not_before or getattr(self, "_maintenance_busy", False): return
        self._ai_save()
        if self.ai_image_store.get().get("pending_submission"):
            self._ai_submit_pending(); return
        try:
            snapshot = self._ai_quote_snapshot()
            if not snapshot["prompt"].strip(): raise ValueError("请填写提示词。")
            if not snapshot["workspace_id"]: raise ValueError("请到设置连接 Fish Audio 并选择工作区。")
            if not capability(self._ai_model(), bool(snapshot["references"])):
                raise ValueError("当前模型不支持此生图方式或尚未取得规格，请刷新规格或切换模型。")
            validate_references(snapshot["prompt"], len(snapshot["references"]))
            if len(snapshot["references"]) > reference_limit(self._ai_model()): raise ValueError("参考数量超过所选模型上限，请减少参考或切换模型。")
            refs = [{**copy.deepcopy(r), "label": f"@图{i}"} for i, r in enumerate(snapshot["references"], 1)]
            config = {"workflow": "ai_image", "model": snapshot["model"], "parameters": copy.deepcopy(snapshot["parameters"]),
                      "prompt": snapshot["prompt"].strip(), "image_inputs": refs, "workspace_id": snapshot["workspace_id"],
                      "workspace_name": self.fish_workspace.get(), "output": str(Path(snapshot["output_dir"]) / "AI生图.png")}
            auth = self.fish_auth_path
            def work():
                with FishClient(auth) as client: return prepare_quote(config, self.desktop_store, client)
            self._ai_set_busy(True, "正在检查参数并读取本次费用…")
            self._ai_work("quote", snapshot, work)
        except Exception as exc: self.ai_status.set(str(exc))

    def _handle_ai_image_event(self, payload):
        kind, context, result, error, *generation = payload
        if kind.startswith("display_"):
            self._ai_handle_display(kind, context, result, error)
            return
        if kind != "models" and generation and generation[0] != self._ai_form_generation:
            return  # A completed submission has retired this form's background work.
        self._ai_save()
        if kind == "models":
            if context[0] != self._ai_model_request: return
            self.ai_model_busy = False; self._ai_controls()
            if error:
                self.ai_status.set("模型规格未刷新：" + error); return
            detail = result["detail"]
            self.ai_image_store.save_model(detail)
            self._ai_models[detail["model_id"]] = detail; self._ai_live_models.add(detail["model_id"])
            if result["listing"] is not None:
                self._ai_catalog_ids = [m["model_id"] for m in result["listing"].get("models", []) if set(m.get("capabilities", [])) & {"t2i", "edit"}]
                self._ai_catalog_loaded = True
            self._ai_render_form()
            return
        self._ai_set_busy(False)
        if error:
            self.ai_status.set(error); return
        if kind == "references":
            before = len(self.ai_image_store.get()["references"])
            form = self.ai_image_store.add_references(result["references"])
            self._ai_render_form(); self._ai_render_preview()
            added = len(form["references"]) - before
            self.ai_status.set(f"新增 {added} 张参考，重复图片已保留原编号。" if not result["errors"] else "部分图片未添加，请查看提示。")
            if len(form["references"]) > reference_limit(self._ai_model()): self._ai_apply_options()
            if result["errors"]: messagebox.showerror("参考图片检查", "\n".join(result["errors"]), parent=self.root)
        elif kind == "helper":
            _, applied = self.ai_image_store.helper_finished(context, result)
            self._ai_render_form()
            self.ai_status.set(("提示词已填入，可编辑或撤销。" if applied else "生成稿已保存到「查看稿件」，当前输入已改变或需要补充，未覆盖提示词。") + ("\n" + result["note"][:100] if result["note"] else ""))
        elif kind == "quote":
            if fingerprint(context) != fingerprint(self._ai_quote_snapshot()):
                self.ai_status.set("预估期间提示词、参考、模型或工作区已改变，请重新预估。"); return
            q, p = result["fish_quote"], result["parameters"]
            specs = " · ".join(QUALITY_LABELS.get(v, v) if k == "quality" else "自动比例" if v == "auto" else str(v) for k, v in p.items())
            mode = f"参考生图 · {len(result['image_inputs'])} 张参考" if result["image_inputs"] else "文生图"
            title = MODEL_LABELS.get(result["model"], result["model"])
            message = f"{title}\n{result['workspace_name']}\n{mode} · 输出一张\n{specs}\n\n本次费用：{q['credits']:,} 积分\n可用积分：{q.get('balance', '以平台为准')}\n\n确认生成？"
            if not messagebox.askyesno("确认生图费用", message, parent=self.root):
                self.ai_status.set("已取消生成，未提交付费任务；提示词与参考保留。"); return
            self._ai_save()
            if fingerprint(context) != fingerprint(self._ai_quote_snapshot()):
                self.ai_status.set("生成配置已改变，请重新预估。 "); return
            result["cost_approved"] = True
            self.ai_image_store.fields(pending_submission={"request_id": uuid.uuid4().hex, "config": result})
            self._ai_submit_pending()

    def _ai_submit_pending(self):
        pending = self.ai_image_store.get().get("pending_submission")
        if not pending: return
        self._ai_set_busy(True, "正在提交本次生成…")
        try:
            job = self.ai_image_store.pending_job()
            identity = job["id"] if job else self.tasks.submit("ai_image", pending["config"], self._credential_values(), request_id=pending["request_id"])
        except Exception as exc:
            job = self.ai_image_store.pending_job()
            if not job:
                self._ai_set_busy(False, "本地提交尚未确认。点击会核对原请求，不会另建重复任务。\n" + str(exc)); return
            identity = job["id"]
        self._ai_reset_after_submission(identity)

    def _ai_reset_after_submission(self, identity):
        if self._ai_save_timer:
            self.root.after_cancel(self._ai_save_timer); self._ai_save_timer = None
        self.ai_image_store.reset_after_submission(identity)
        self._ai_form_generation += 1
        self._ai_preview_ref = None
        self._ai_preview_zoom = 1.0
        self._ai_preview_requested = None
        self.ai_helper_panel.pack_forget()
        self._ai_not_before = time.monotonic() + 1
        self._ai_render_form()
        self._ai_set_busy(False, "已提交，参考图与提示词已清空，生成设置已恢复默认。进度与结果可在右侧和使用记录查看。")
        self._ai_render_results()

    def _ai_jobs(self):
        return [j for j in self.desktop_store.jobs() if j.get("kind") in {"ai_image", "image_fission"}]

    def _ai_selected_job(self):
        identity = self.ai_image_store.get()["selected_job"]
        try: return self.desktop_store.job(identity) if identity else None
        except ValueError: return None

    @staticmethod
    def _ai_result_path(job):
        result = job.get("result", {}) if job else {}
        return next((str(p) for p in (result.get("asset_path"), result.get("output")) if p and Path(p).is_file()), None)

    def _ai_selected_path(self):
        form = self.ai_image_store.get()
        if self._ai_preview_ref:
            return next((r["path"] for r in form["references"] if r["asset_id"] == self._ai_preview_ref and Path(r["path"]).is_file()), None)
        job = self._ai_selected_job()
        if job: return self._ai_result_path(job)
        return form["references"][0]["path"] if form["references"] else None

    def _ai_select_result(self, identity):
        self._ai_save(); self.ai_image_store.fields(selected_job=identity); self._ai_preview_ref = None
        self._ai_preview_zoom = 1.0
        self._ai_preview_requested = None
        self._ai_paint_selection(); self._ai_render_preview()
        self.ai_preview.xview_moveto(0); self.ai_preview.yview_moveto(0)

    def _ai_preview_reference(self, index):
        refs = self.ai_image_store.get()["references"]
        if index < len(refs):
            self._ai_preview_zoom = 1.0
            self._ai_preview_requested = None
            self._ai_preview_ref = refs[index]["asset_id"]
            self._ai_paint_selection(); self._ai_render_preview()

    def _ai_render_results(self):
        from desktop_tasks import LABELS
        jobs = self._ai_jobs()[:30]
        identities = {j["id"] for j in jobs}
        for identity in list(self._ai_result_cards):
            if identity not in identities: self._ai_result_cards.pop(identity)["panel"].destroy()
        if jobs and self._ai_empty_result:
            self._ai_empty_result.destroy(); self._ai_empty_result = None
        if not jobs and self._ai_empty_result is None:
            self._ai_empty_result = ctk.CTkLabel(self.ai_results, text="生成记录会保留在这里", text_color="#86868B", height=84)
            self._ai_empty_result.grid(row=0, column=0, padx=16)
        for index, job in enumerate(jobs):
            card = self._ai_result_cards.get(job["id"])
            if card is None:
                panel = ctk.CTkFrame(self.ai_results, width=116, height=106, fg_color="white", corner_radius=8)
                panel.pack_propagate(False)
                btn = ctk.CTkButton(panel, text="加载中", width=100, height=70, fg_color="transparent", text_color="#6E6E73",
                                    command=lambda i=job["id"]: self._ai_select_result(i))
                btn.pack(padx=4, pady=(4, 0))
                label = ctk.CTkLabel(panel, text="", text_color="#6E6E73", font=("Microsoft YaHei UI", 10))
                label.pack(padx=5)
                card = self._ai_result_cards[job["id"]] = {"panel": panel, "button": btn, "label": label, "signature": None, "selected": False}
            card["panel"].grid(row=0, column=index, padx=4, pady=4)
            path = self._ai_result_path(job)
            status = "已生成" if job["status"] == "success" else LABELS.get(job["status"], job["status"])
            signature = (path, status, job["created"])
            if signature != card["signature"]:
                card["signature"] = signature
                card["label"].configure(text=time.strftime("%m-%d %H:%M", time.localtime(job["created"])) + " · " + status)
                if path: self._ai_bind_thumbnail(card["button"], path, (84, 65))
                else:
                    card["button"]._ai_thumb_key = None
                    card["button"].configure(image=None, text=status)
        self._ai_paint_selection()
        self._ai_render_preview()

    def _ai_paint_selection(self):
        selected = None if self._ai_preview_ref else self.ai_image_store.get()["selected_job"]
        for identity, card in self._ai_result_cards.items():
            active = identity == selected
            if card["selected"] != active:
                card["panel"].configure(fg_color="#EAF4FF" if active else "white")
                card["selected"] = active

    def _ai_render_preview(self):
        job = self._ai_selected_job()
        path = self._ai_selected_path()
        if path != self._ai_preview_path:
            self._ai_preview_path = path
            self._ai_preview_zoom = 1.0
            self._ai_preview_available = bool(path)
            self._ai_preview_requested = None
            self._ai_preview_image = None
            self.ai_preview.delete("all")
        if self._ai_preview_requested is None:
            self._ai_preview_available = bool(path)
        if self._ai_preview_ref or not job:
            form = self.ai_image_store.get()
            index = next((i for i, r in enumerate(form["references"], 1) if r["path"] == path), None)
            title = f"参考 @图{index}" if index else "结果预览"
            self.ai_result_status.configure(text="")
        else:
            title = MODEL_LABELS.get(job["config"].get("model"), "历史图片")
            self.ai_result_status.configure(text=(job.get("error") or job["stage"])[:180])
        self.ai_preview_title.configure(text=title)
        self.ai_feedback_button.configure(state="normal" if job and job["status"] == "success" and not self._ai_preview_ref else "disabled")
        for key in ("save", "modify", "frame"):
            self.ai_result_buttons[key].configure(state="normal" if path else "disabled")
        for key in ("reuse", "recover"):
            self.ai_result_buttons[key].configure(state="normal" if job and not self._ai_preview_ref else "disabled")
        self.ai_result_buttons["compare"].configure(state="normal" if path or self.ai_image_store.get()["references"] else "disabled")
        for button in self.ai_zoom_buttons.values():
            button.configure(state="normal" if self._ai_preview_available else "disabled")
        self._ai_draw_preview()

    def _ai_resize_preview(self, _=None):
        if self._ai_preview_timer: self.root.after_cancel(self._ai_preview_timer)
        self._ai_preview_timer = self.root.after(150, self._ai_draw_preview)

    def _ai_draw_preview(self):
        if self._ai_preview_timer: self.root.after_cancel(self._ai_preview_timer)
        self._ai_preview_timer = None
        canvas = self.ai_preview
        w, h = max(1, canvas.winfo_width()), max(1, canvas.winfo_height())
        signature = (self._ai_preview_path, (w, h), self._ai_preview_zoom)
        if signature == self._ai_preview_requested: return
        self._ai_preview_requested = signature
        self._ai_preview_token += 1
        self._ai_preview_pending = bool(self._ai_preview_path)
        if self._ai_preview_path:
            if self._ai_preview_image is None:
                canvas.delete("all")
                canvas.configure(scrollregion=(0, 0, w, h)); canvas.xview_moveto(0); canvas.yview_moveto(0)
                canvas.create_text(w / 2, h / 2, text="正在加载图片…", fill="#86868B")
            self.ai_display.preview({"token": self._ai_preview_token, "path": self._ai_preview_path, "size": (w, h), "zoom": self._ai_preview_zoom})
        else:
            canvas.delete("all")
            canvas.configure(scrollregion=(0, 0, w, h))
            canvas.xview_moveto(0); canvas.yview_moveto(0)
            canvas.create_text(w / 2, h / 2, text="描述你想要的画面\n生成的图片会显示在这里", fill="#86868B", justify="center", width=max(1, w - 20))
            self._ai_preview_image = None
            self.ai_zoom_label.configure(text="适应窗口")

    def _ai_zoom_preview(self, factor):
        if not self._ai_preview_available: return
        self._ai_preview_zoom = max(.25, min(8.0, self._ai_preview_zoom * factor))
        self._ai_draw_preview()

    def _ai_fit_preview(self):
        self._ai_preview_zoom = 1.0
        self._ai_preview_requested = None
        self._ai_draw_preview()

    def _ai_preview_wheel(self, event):
        if event.delta: self._ai_zoom_preview(1.25 if event.delta > 0 else 1 / 1.25)
        return "break"

    def _ai_handle_display(self, kind, context, result, error):
        if getattr(self, "_closed", False): return
        if kind == "display_thumbnail":
            widgets = self._ai_thumb_waiters.pop(context, [])
            thumb = ctk.CTkImage(result, size=result.size) if result else None
            if thumb:
                self._ai_thumbnails[context] = thumb
                while len(self._ai_thumbnails) > 128: self._ai_thumbnails.popitem(last=False)
            for widget in widgets:
                if widget.winfo_exists() and getattr(widget, "_ai_thumb_key", None) == context:
                    widget.configure(image=thumb, text="" if thumb else "无法读取")
            return
        if context["token"] != self._ai_preview_token: return
        self._ai_preview_pending = False
        canvas = self.ai_preview
        w, h = context["size"]
        if (w, h) != (canvas.winfo_width(), canvas.winfo_height()):
            self._ai_draw_preview(); return
        canvas.delete("all")
        if error:
            self._ai_preview_available = False
            self._ai_preview_image = None
            canvas.configure(scrollregion=(0, 0, w, h)); canvas.xview_moveto(0); canvas.yview_moveto(0)
            self.ai_zoom_label.configure(text="无法读取")
            for button in self.ai_zoom_buttons.values(): button.configure(state="disabled")
            canvas.create_text(w / 2, h / 2, text=error, fill="#86868B", width=max(1, w - 20))
            return
        self._ai_preview_available = True
        for button in self.ai_zoom_buttons.values(): button.configure(state="normal")
        rendered = result["image"]
        size = rendered.size
        self._ai_preview_image = ImageTk.PhotoImage(rendered, master=canvas)
        canvas.create_image(max(w, size[0]) / 2, max(h, size[1]) / 2, image=self._ai_preview_image, anchor="center")
        canvas.configure(scrollregion=(0, 0, max(w, size[0]), max(h, size[1])))
        self.ai_zoom_label.configure(text=f"{result['scale']:.0%}")
        canvas.xview_moveto(max(0, (size[0] - w) / (2 * size[0])))
        canvas.yview_moveto(max(0, (size[1] - h) / (2 * size[1])))

    def _ai_close_display(self):
        display = getattr(self, "ai_display", None)
        if display: display.close()

    def _ai_jobs_changed(self):
        if getattr(self, "current_page", None) != "ai_image": return
        signature = [(j["id"], j.get("updated")) for j in self._ai_jobs()[:30]]
        if signature != self._ai_results_signature:
            self._ai_results_signature = signature; self._ai_render_results()

    def _ai_compare(self):
        form = self.ai_image_store.get()
        choices = {f"@图{i} · {r['name'][:20]}": r["path"] for i, r in enumerate(form["references"], 1) if Path(r["path"]).is_file()}
        for job in self._ai_jobs():
            path = self._ai_result_path(job)
            if path: choices["结果 " + time.strftime("%m-%d %H:%M", time.localtime(job["created"])) + " · " + job["id"][:5]] = path
        if not choices: return
        path = self._ai_selected_path()
        selected = next((label for label, value in choices.items() if value == path), next(iter(choices)))
        from fission_viewer import open_comparison
        return open_comparison(self.root, choices, selected)

    def _ai_export(self):
        path = self._ai_selected_path()
        if not path: return
        suffix = Path(path).suffix
        dest = filedialog.asksaveasfilename(parent=self.root, title="另存图片", initialfile="AI生图" + suffix,
                                          defaultextension=suffix, filetypes=[("图片", "*" + suffix)])
        if dest:
            try:
                if Path(dest).resolve() != Path(path).resolve(): shutil.copy2(path, dest)
                self.ai_status.set("图片已另存。")
            except OSError as exc: messagebox.showerror("另存失败", str(exc), parent=self.root)

    def _ai_use_as_reference(self):
        path = self._ai_selected_path()
        if not path: return
        job = None if self._ai_preview_ref else self._ai_selected_job()
        result = job.get("result", {}) if job else {}
        remote = {"object_key": result.get("media_object_key"), "generation_id": result.get("task_id"),
                  "workspace_id": result.get("workspace_id"), "connection_id": result.get("connection_id")}
        self._ai_add_paths([path], remote=remote)
        self.ai_prompt.focus_set()

    def _ai_reuse_selected(self):
        job = self._ai_selected_job()
        if job: self._ai_reuse_config(job["config"])

    def _ai_reuse_config(self, config):
        if self.ai_image_busy or self.ai_image_store.get().get("pending_submission"):
            self.ai_status.set("请先完成当前准备或核对提交，再复用参数。"); return
        self._ai_save()
        source = copy.deepcopy(config)
        def mutate(form):
            refs = source.get("image_inputs", [])
            assets = {a["path"]: a for a in self.desktop_store.assets()}
            form["references"] = [{**r, "asset_id": r.get("asset_id") or assets.get(r["path"], {}).get("id", r.get("sha256")),
                                    "name": r.get("name") or assets.get(r["path"], {}).get("name", Path(r["path"]).name)} for r in refs]
            form["model"] = source.get("model", DEFAULT_MODEL)
            form["parameters"] = source.get("parameters", copy.deepcopy(DEFAULT_PARAMETERS))
            form["parameters"] = {k: FOLLOW if v == "跟随底图" else v for k, v in form["parameters"].items()}
            form["prompt"] = source.get("prompt", "") if source.get("workflow") == "ai_image" else actual_prompt(source)
            form["assistant"] = {"request": ""}
            form["helper_result"] = form["undo_prompt"] = None
        self.ai_image_store.change(mutate)
        self._ai_preview_ref = None
        self._ai_render_form(); self._ai_render_results(); self._show_page("ai_image")
        self.ai_status.set("已复用提示词、参考图和参数；生成前会重新报价。")
        if not self.demo and self.fish_workspaces and self._ai_model_id() not in self._ai_live_models: self._ai_refresh_models()

    def _ai_use_as_frame(self):
        path = self._ai_selected_path()
        if not path: return
        if self.fish_busy or self.director_busy:
            self.ai_status.set("请等待图音页面当前操作结束后回填。"); return
        self._ai_save(); self._fish_load("first_frame", path); self._show_page("image_audio")

    def _ai_show_history(self):
        self._show_page("history"); self.history_filter.set("AI 生图")
        self.history_state.set("全部状态"); self.history_search.set(""); self._refresh_history()

    def _ai_recover(self):
        job = self._ai_selected_job()
        if not job: return
        if job["status"] in {"attention", "paused"}:
            try: self.tasks.recover(job["id"], self._credential_values())
            except Exception as exc: self.ai_status.set(str(exc))
        else:
            self._show_page("history"); self.history_filter.set("全部功能"); self.history_state.set("全部状态"); self.history_search.set("")
            self._refresh_history(); self.history_tree.selection_set(job["id"]); self._task_detail()
