"""Two assistant settings panels, explicit feedback, and approval-gated learning."""
from __future__ import annotations

import copy
import threading
import time
from pathlib import Path
from tkinter import BooleanVar, StringVar, Toplevel, filedialog, messagebox

import customtkinter as ctk

from assistant_learning import add_feedback, add_material, analyze_source
from assistant_store import AssistantStore, CONTENT_FIELDS, KINDS, ROLES
from personal_library import relative_file

TABS = ("待确认", "学习资料", "工作规则", "变更记录")
FIELD_LABELS = {"title": "名称", "category": "产品品类 / 主题", "content": "具体做法 / 演示过程",
    "applicability": "适用条件", "exceptions": "例外情况", "evidence": "来源与依据", "limitations": "尚未验证 / 看不清的内容"}
MODE_LABELS = {"两种模式": ["talk", "use"], "纯口播": ["talk"], "边讲边用": ["use"]}


class AssistantPage:
    def _init_assistants(self):
        self.assistant_store = AssistantStore(self.library_transfer.root)
        self._assistant_busy = False
        self._assistant_window = None
        self._assistant_role = "artist"
        self._assistant_badges = {}
        self.assistant_status = StringVar(value="学习建议确认后生效，可随时编辑或停用。")

    def _build_assistant_settings(self, page):
        for role, name in ROLES.items():
            description = ("图片构思与细节要求；根据明确反馈整理经验。" if role == "artist" else "电商口播与动作编排；从参考视频和任务反馈积累演示。")
            _, card = self._card(page, name, description)
            row = ctk.CTkFrame(card, fg_color="transparent"); row.pack(fill="x")
            button = self._secondary_button(row, "学习、规则与待确认", lambda r=role: self._open_assistant(r), width=180)
            button.pack(side="left")
            badge = ctk.CTkLabel(row, text="", text_color="#6E6E73")
            badge.pack(side="left", padx=14); self._assistant_badges[role] = badge
        self._assistant_refresh_badges()

    def _assistant_refresh_badges(self):
        try:
            state = self.assistant_store.read()
            for role, badge in self._assistant_badges.items():
                entries = [e for e in state["entries"].values() if e["role"] == role]
                pending = sum(e.get("draft") is not None for e in entries)
                active = sum(bool(e.get("active") and e["enabled"]) for e in entries)
                badge.configure(text=f"待确认 {pending} · 已启用 {active} · 自进化{'已开' if state['settings'][role]['enabled'] else '已关'}")
        except ValueError as exc:
            for badge in self._assistant_badges.values(): badge.configure(text=str(exc))

    def _assistant_new_window(self, title, geometry="980x720"):
        window = Toplevel(self.root, bg="#F5F5F7")
        window.title(title); window.geometry(geometry); window.transient(self.root)
        return window

    def _open_assistant(self, role):
        if role not in ROLES: return
        if self._assistant_window and self._assistant_window.winfo_exists():
            self._assistant_window.destroy()
        self._assistant_role = role
        window = self._assistant_window = self._assistant_new_window(ROLES[role] + " · 学习与规则", "1040x760")
        window.minsize(900, 620)
        top = ctk.CTkFrame(window, fg_color="transparent"); top.pack(fill="x", padx=22, pady=(18, 8))
        ctk.CTkLabel(top, text=ROLES[role], font=("Microsoft YaHei UI", 24, "bold")).pack(side="left")
        self._assistant_auto_var = BooleanVar(value=False)
        self._assistant_auto_switch = ctk.CTkSwitch(top, text="自进化：根据明确反馈整理建议", variable=self._assistant_auto_var,
                                                  command=self._assistant_toggle_auto)
        self._assistant_auto_switch.pack(side="right")
        ctk.CTkLabel(window, text="学习与反馈 → 待确认 → 确认并启用。未确认内容不参与创作；关闭自进化仍使用已启用内容。",
                     text_color="#6E6E73", anchor="w").pack(fill="x", padx=22)
        ctk.CTkLabel(window, text="整理建议使用现有 Kaiyun GPT-6.1 Sol，按用量计费；查看、编辑和确认不调用模型。",
                     text_color="#86868B", anchor="w", font=("Microsoft YaHei UI", 11)).pack(fill="x", padx=22, pady=(0, 8))
        bottom = ctk.CTkFrame(window, fg_color="transparent"); bottom.pack(side="bottom", fill="x", padx=22, pady=12)
        ctk.CTkLabel(bottom, textvariable=self.assistant_status, text_color="#0071E3", wraplength=850,
                     justify="left", anchor="w").pack(fill="x")
        self._assistant_tabs = ctk.CTkTabview(window, fg_color="white")
        self._assistant_tabs.pack(fill="both", expand=True, padx=18, pady=(0, 6))
        self._assistant_lists = {}
        self._assistant_pages = {title: 0 for title in TABS}
        self._assistant_page_labels = {}
        for title in TABS:
            panel = self._assistant_tabs.add(title)
            if title in {"学习资料", "工作规则"}:
                row = ctk.CTkFrame(panel, fg_color="transparent"); row.pack(fill="x", padx=10, pady=8)
                if title == "学习资料":
                    self._secondary_button(row, "添加学习素材", lambda: self._assistant_add_dialog(role), width=140).pack(side="left")
                    if role == "director":
                        self._secondary_button(row, "已有演示库", self._assistant_existing_library, width=115).pack(side="left", padx=8)
                    ctk.CTkLabel(row, text="视频仅用于 AI 导演；图片可说明每张图的用途。", text_color="#86868B").pack(side="left", padx=12)
                else:
                    self._secondary_button(row, "新增个人规则", lambda: self._assistant_edit(role), width=140).pack(side="left")
                    ctk.CTkLabel(row, text="确认后的规则与案例；本次明确要求优先。", text_color="#86868B").pack(side="left", padx=12)
            navigation = ctk.CTkFrame(panel, fg_color="transparent"); navigation.pack(side="bottom", fill="x", padx=12, pady=4)
            self._assistant_page_labels[title] = ctk.CTkLabel(navigation, text="")
            self._assistant_page_labels[title].pack(side="left")
            self._secondary_button(navigation, "下一页", lambda t=title: self._assistant_turn_page(t, 1), width=80).pack(side="right", padx=4)
            self._secondary_button(navigation, "上一页", lambda t=title: self._assistant_turn_page(t, -1), width=80).pack(side="right")
            box = ctk.CTkScrollableFrame(panel, fg_color="white")
            box.pack(fill="both", expand=True, padx=4, pady=4); self._assistant_lists[title] = box
        self._assistant_reload()
        return window

    def _assistant_toggle_auto(self):
        if self._assistant_busy:
            self._assistant_auto_var.set(self.assistant_store.enabled(self._assistant_role)); return
        try:
            self.assistant_store.enable(self._assistant_role, self._assistant_auto_var.get())
            self.assistant_status.set("自进化已开启：保存明确反馈后会整理建议，确认前不生效。" if self._assistant_auto_var.get() else "自进化已关闭；已启用规则保留，仍可手动学习。")
            self._assistant_reload()
        except Exception as exc: messagebox.showerror("设置未保存", str(exc), parent=self._assistant_window)

    def _assistant_reload(self):
        self._assistant_refresh_badges()
        if not self._assistant_window or not self._assistant_window.winfo_exists(): return
        try: state = self.assistant_store.read()
        except ValueError as exc: self.assistant_status.set(str(exc)); return
        role = self._assistant_role
        self._assistant_auto_var.set(state["settings"][role]["enabled"])
        self._assistant_auto_switch.configure(state="disabled" if self._assistant_busy else "normal")
        for box in self._assistant_lists.values():
            for widget in box.winfo_children(): widget.destroy()
        counts = {t: 0 for t in TABS}
        totals = {t: 0 for t in TABS}
        def visible(title):
            index = totals[title]; totals[title] += 1
            return self._assistant_pages[title] * 20 <= index < (self._assistant_pages[title] + 1) * 20
        for entry in sorted(state["entries"].values(), key=lambda e: e["updated"], reverse=True):
            if entry["role"] != role: continue
            if entry.get("draft") and visible("待确认"):
                content = entry["draft"]; counts["待确认"] += 1
                card = self._assistant_card("待确认", content["title"], f"{KINDS[entry['kind']]} · 确认后才生效\n适用：{content['applicability']}")
                self._secondary_button(card, "查看 / 确认", lambda e=entry: self._assistant_edit(role, e), width=120).pack(anchor="e", pady=6)
            if entry.get("active") and visible("工作规则"):
                content = entry["active"]; counts["工作规则"] += 1
                card = self._assistant_card("工作规则", content["title"], f"{KINDS[entry['kind']]} · {'已启用' if entry['enabled'] else '已停用'} · 版本 {entry['revision']}\n适用：{content['applicability']}")
                row = ctk.CTkFrame(card, fg_color="transparent"); row.pack(fill="x", pady=6)
                self._secondary_button(row, "查看 / 编辑", lambda e=entry: self._assistant_edit(role, e), width=110).pack(side="left")
                self._secondary_button(row, "停用" if entry["enabled"] else "启用", lambda e=entry: self._assistant_decide(e, "disable" if e["enabled"] else "enable"), width=80).pack(side="left", padx=8)
        for source in sorted(state["sources"].values(), key=lambda s: s["created"], reverse=True):
            if source["role"] != role: continue
            if not visible("学习资料"): continue
            counts["学习资料"] += 1
            status = {"new": "尚未整理", "working": "处理中 / 可重新检查", "done": "已整理", "error": "分析未完成"}.get(source["analysis"], "待检查")
            card = self._assistant_card("学习资料", source["title"], status + " · " + source["request"][:220])
            self._secondary_button(card, "查看资料与反馈", lambda s=source: self._assistant_view_source(s), width=150).pack(anchor="e", pady=6)
        for event in reversed(state["history"]):
            if event["role"] != role: continue
            if not visible("变更记录"): continue
            counts["变更记录"] += 1
            entry = event.get("after") or {}
            content = entry.get("draft") or entry.get("active") or {}
            card = self._assistant_card("变更记录", event["action"] + (" · " + content.get("title", "") if content else ""),
                time.strftime("%Y-%m-%d %H:%M", time.localtime(event["time"])))
            if event.get("entry_id"):
                self._secondary_button(card, "查看变更", lambda e=event: self._assistant_history(e), width=110).pack(anchor="e", pady=4)
        for title, count in counts.items():
            last = max(0, (totals[title] - 1) // 20)
            if self._assistant_pages[title] > last:
                self._assistant_pages[title] = last
                self._assistant_reload(); return
            self._assistant_page_labels[title].configure(text=f"共 {totals[title]} 条 · 第 {self._assistant_pages[title] + 1} / {last + 1} 页")
            if not count:
                label = "还没有待确认建议。可添加学习素材，或在生成结果旁提交具体反馈。" if title == "待确认" else "这里会保留你的资料与记录。"
                ctk.CTkLabel(self._assistant_lists[title], text=label, text_color="#86868B", wraplength=720).pack(pady=50)

    def _assistant_turn_page(self, title, delta):
        self._assistant_pages[title] = max(0, self._assistant_pages[title] + delta)
        self._assistant_reload()

    def _assistant_card(self, tab, title, text):
        card = ctk.CTkFrame(self._assistant_lists[tab], fg_color="#F6F7F9", corner_radius=12)
        card.pack(fill="x", padx=8, pady=6)
        inside = ctk.CTkFrame(card, fg_color="transparent"); inside.pack(fill="x", padx=14, pady=10)
        ctk.CTkLabel(inside, text=title, font=("Microsoft YaHei UI", 14, "bold"), anchor="w", wraplength=780, justify="left").pack(fill="x")
        ctk.CTkLabel(inside, text=text, text_color="#6E6E73", anchor="w", justify="left", wraplength=780).pack(fill="x", pady=4)
        return inside

    def _assistant_work(self, work, done):
        if self._assistant_busy or getattr(self, "_maintenance_busy", False) or self._exit_in_progress:
            self.assistant_status.set("请等待当前资料处理完成。"); return False
        self._assistant_busy = True
        self._assistant_done = done
        self.assistant_status.set("正在处理资料…")
        self._assistant_reload()
        def worker():
            try: result, error = work(), None
            except Exception as exc: result, error = None, str(exc)
            self.events.put(("assistant_event", ("done", result, error)))
        threading.Thread(target=worker, daemon=True).start()
        return True

    def _handle_assistant_event(self, payload):
        if self._closed: return
        if payload[0] == "progress": self.assistant_status.set(payload[1]); return
        _, result, error = payload
        self._assistant_busy = False
        if error: self.assistant_status.set("未完成：" + error)
        else: self._assistant_done(result)
        self._assistant_reload()

    def _assistant_analyze(self, identity, automatic=False):
        credentials = self._credential_values()
        def work():
            return analyze_source(self.assistant_store, identity, credentials,
                lambda note: self.events.put(("assistant_event", ("progress", note))), automatic=automatic)
        self._assistant_work(work, lambda r: self.assistant_status.set(f"新增 {r['count']} 条待确认建议。" + r["note"]))

    def _assistant_add_dialog(self, role):
        window = self._assistant_new_window(ROLES[role] + " · 添加学习素材", "650x470")
        window.resizable(False, False)
        paths = []
        selected = StringVar(value="尚未选择素材")
        ctk.CTkLabel(window, text="添加产品演示视频或参考图片" if role == "director" else "添加参考图片、原图或生成结果", font=("Microsoft YaHei UI", 17, "bold")).pack(anchor="w", padx=22, pady=(20, 12))
        def choose():
            types = [("图片与视频", "*.png *.jpg *.jpeg *.webp *.mp4 *.mov *.mkv *.webm *.avi")] if role == "director" else [("图片", "*.png *.jpg *.jpeg *.webp")]
            files = filedialog.askopenfilenames(parent=window, title="选择学习素材", filetypes=types)
            if files: paths[:] = files; selected.set("、".join(Path(p).name for p in files)[:120])
        self._secondary_button(window, "选择素材", choose, width=120).pack(anchor="w", padx=22)
        ctk.CTkLabel(window, textvariable=selected, wraplength=600, justify="left").pack(anchor="w", padx=22, pady=8)
        category = ctk.CTkEntry(window, placeholder_text="产品品类 / 主题（导演必填，例如乳霜）")
        category.pack(fill="x", padx=22, pady=5)
        ctk.CTkLabel(window, text="想学习什么？多张图请说明各自用途。", anchor="w").pack(fill="x", padx=22, pady=(8, 4))
        request = ctk.CTkTextbox(window, height=105, wrap="word"); request.pack(fill="x", padx=22)
        ctk.CTkLabel(window, text="视频最长 3 分钟，按时间抽帧学习动作，暂不分析音频。\n先保存在个人库；点击资料中的「整理建议」才调用模型。", text_color="#86868B", justify="left").pack(anchor="w", padx=22, pady=10)
        def save():
            selected_paths, note, group = list(paths), request.get("1.0", "end-1c"), category.get()
            def done(result):
                source, added = result
                self.assistant_status.set("学习资料已保存，可查看后整理建议。" if added else "相同资料和学习要求已存在，可继续查看。")
                if self._assistant_window and self._assistant_window.winfo_exists(): self._assistant_tabs.set("学习资料")
                self._assistant_view_source(source)
            if self._assistant_work(lambda: add_material(self.assistant_store, role, selected_paths, note, group), done): window.destroy()
        ctk.CTkButton(window, text="保存学习资料", command=save, height=36).pack(anchor="e", padx=22, pady=(0, 16))
        return window

    def _assistant_view_source(self, source):
        window = self._assistant_new_window(ROLES[source["role"]] + " · 学习资料", "900x730")
        window.minsize(800, 650)
        ctk.CTkLabel(window, text=source["title"], font=("Microsoft YaHei UI", 18, "bold"), wraplength=840).pack(anchor="w", padx=20, pady=(16, 4))
        text = "学习要求 / 用户反馈：\n" + source["request"]
        if source["context"].get("prompt"): text += "\n\n本次提示词：\n" + source["context"]["prompt"]
        if source["context"].get("observation"): text += "\n\n观察方式：" + source["context"]["observation"]
        if source["context"].get("limitations"): text += "\n\n资料限制：" + "；".join(source["context"]["limitations"])
        box = ctk.CTkTextbox(window, height=150, wrap="word"); box.pack(fill="x", padx=20, pady=8)
        box.insert("1.0", text); box.configure(state="disabled")
        controls = ctk.CTkFrame(window, fg_color="transparent"); controls.pack(side="bottom", fill="x", padx=20, pady=16)
        ctk.CTkLabel(controls, text="整理建议按 Kaiyun 用量计费；确认后才用于后续提示词。", text_color="#86868B").pack(side="left")
        ctk.CTkButton(controls, text="整理建议", width=110,
                      state="disabled" if source["analysis"] == "done" else "normal",
                      command=lambda: self._assistant_analyze(source["id"])).pack(side="right")
        page, page_label = [0], StringVar()
        nav = ctk.CTkFrame(window, fg_color="transparent"); nav.pack(side="bottom", fill="x", padx=20)
        ctk.CTkLabel(nav, textvariable=page_label).pack(side="left")
        gallery = ctk.CTkFrame(window, fg_color="white"); gallery.pack(fill="both", expand=True, padx=20, pady=8)
        gallery.grid_columnconfigure((0, 1, 2), weight=1, uniform="evidence")
        def render(change=0):
            page[0] = min(max(0, page[0] + change), max(0, (len(source["media"]) - 1) // 6))
            for widget in gallery.winfo_children(): widget.destroy()
            for i, media in enumerate(source["media"][page[0] * 6:page[0] * 6 + 6]):
                frame = ctk.CTkFrame(gallery, fg_color="transparent"); frame.grid(row=i // 3, column=i % 3, padx=8, pady=5, sticky="nsew")
                path = relative_file(self.assistant_store.root, media["path"])
                picture = ctk.CTkLabel(frame, text="加载中", width=190, height=120); picture.pack()
                self._ai_bind_thumbnail(picture, str(path), (190, 120))
                picture.bind("<Button-1>", lambda _, p=str(path): self._open_task_result(p))
                ctk.CTkLabel(frame, text=media["label"], wraplength=220, height=22).pack()
            page_label.set(f"证据图片 {len(source['media'])} 张 · 第 {page[0] + 1} 页 · 点击图片查看原图")
        self._secondary_button(nav, "下一页", lambda: render(1), width=80).pack(side="right", padx=4)
        self._secondary_button(nav, "上一页", lambda: render(-1), width=80).pack(side="right")
        render()
        return window

    def _assistant_edit(self, role, entry=None):
        if self._assistant_busy: self.assistant_status.set("请等待当前资料处理完成。"); return
        entry = copy.deepcopy(entry)
        content = (entry.get("draft") or entry.get("active")) if entry else {"evidence": "用户手动添加", "modes": ["talk", "use"]}
        window = self._assistant_new_window(ROLES[role] + " · 检查与编辑", "900x740")
        window.minsize(780, 620)
        ctk.CTkLabel(window, text="确认后用于后续创作；采用做法不等于生成效果已经验证。", text_color="#6E6E73").pack(anchor="w", padx=20, pady=(16, 8))
        buttons = ctk.CTkFrame(window, fg_color="transparent"); buttons.pack(side="bottom", fill="x", padx=20, pady=14)
        form = ctk.CTkScrollableFrame(window, fg_color="white"); form.pack(fill="both", expand=True, padx=18, pady=6)
        fields = {}
        if entry and entry.get("active") and entry.get("draft"):
            ctk.CTkLabel(form, text="修改前（当前仍在使用）", text_color="#86868B", anchor="w").pack(fill="x", padx=12, pady=(10, 3))
            old = ctk.CTkTextbox(form, height=110, wrap="word"); old.pack(fill="x", padx=12)
            old.insert("1.0", self._assistant_content_text(entry["active"])); old.configure(state="disabled")
        for key, label in FIELD_LABELS.items():
            ctk.CTkLabel(form, text=label, anchor="w").pack(fill="x", padx=12, pady=(10, 3))
            if key in {"title", "category"}:
                widget = ctk.CTkEntry(form); widget.insert(0, content.get(key, ""))
            else:
                widget = ctk.CTkTextbox(form, height=130 if key == "content" else 65, wrap="word")
                widget.insert("1.0", content.get(key, ""))
            widget.pack(fill="x", padx=12); fields[key] = widget
        mode = StringVar(value=next((k for k, v in MODE_LABELS.items() if sorted(v) == sorted(content.get("modes", ["talk", "use"]))), "两种模式"))
        if role == "director":
            ctk.CTkLabel(form, text="适用导演模式", anchor="w").pack(fill="x", padx=12, pady=(10, 4))
            ctk.CTkOptionMenu(form, values=list(MODE_LABELS), variable=mode).pack(anchor="w", padx=12, pady=(0, 12))
        if entry and entry.get("source_id"):
            self._secondary_button(buttons, "查看来源", lambda: self._assistant_view_source(self.assistant_store.read()["sources"][entry["source_id"]]), width=96).pack(side="left")
        def save(approve=False):
            if self._assistant_busy: return
            try:
                edited = {k: w.get() if k in {"title", "category"} else w.get("1.0", "end-1c") for k, w in fields.items()}
                edited["modes"] = MODE_LABELS[mode.get()]
                current = self.assistant_store.propose(role, entry["kind"] if entry else "rule", edited,
                    entry.get("source_id") if entry else None, entry["id"] if entry else None, entry["revision"] if entry else None)
                if approve: self.assistant_store.decide(current["id"], current["revision"], "approve")
                self.assistant_status.set("已确认并启用，下次使用助手整理提示词时生效。" if approve else "已保存到待确认，当前生效内容保持原版本。")
                window.destroy(); self._assistant_reload()
            except Exception as exc: messagebox.showerror("未保存", str(exc), parent=window)
        if entry and entry.get("draft"):
            self._secondary_button(buttons, "忽略建议", lambda: self._assistant_decide(entry, "ignore", window), width=96).pack(side="left", padx=8)
        ctk.CTkButton(buttons, text="确认并启用", command=lambda: save(True), width=125, height=36).pack(side="right")
        self._secondary_button(buttons, "保存为待确认", lambda: save(False), width=125).pack(side="right", padx=10)
        # Expose the visible editor for offline UI acceptance, without a JSON editor.
        window.assistant_fields, window.assistant_save = fields, save
        return window

    @staticmethod
    def _assistant_content_text(content):
        if not content: return "无"
        return "\n\n".join(label + "：\n" + content.get(key, "") for key, label in FIELD_LABELS.items())

    def _assistant_decide(self, entry, action, window=None):
        if self._assistant_busy: self.assistant_status.set("请等待当前资料处理完成。"); return
        try:
            self.assistant_store.decide(entry["id"], entry["revision"], action)
            if window: window.destroy()
            self.assistant_status.set("已保存，后续提示词会使用当前启用的内容。")
            self._assistant_reload()
        except Exception as exc: messagebox.showerror("未保存", str(exc), parent=self._assistant_window or self.root)

    def _assistant_history(self, event):
        window = self._assistant_new_window("变更记录", "900x660")
        ctk.CTkLabel(window, text=event["action"], font=("Microsoft YaHei UI", 18, "bold")).pack(anchor="w", padx=20, pady=16)
        body = ctk.CTkFrame(window, fg_color="transparent"); body.pack(fill="both", expand=True, padx=16)
        for key, label in (("before", "修改前"), ("after", "修改后")):
            panel = ctk.CTkFrame(body, fg_color="white"); panel.pack(side="left", fill="both", expand=True, padx=4)
            ctk.CTkLabel(panel, text=label).pack(pady=8)
            entry = event.get(key) or {}
            content = entry.get("active") if event["action"] == "确认并启用" else entry.get("draft") or entry.get("active")
            box = ctk.CTkTextbox(panel, wrap="word"); box.pack(fill="both", expand=True, padx=8, pady=(0, 8))
            box.insert("1.0", self._assistant_content_text(content)); box.configure(state="disabled")
        def restore():
            if self._assistant_busy: return
            try:
                self.assistant_store.restore(event["id"])
                self.assistant_status.set("旧版本已恢复为待确认，检查后可重新启用。")
                window.destroy(); self._assistant_reload()
            except Exception as exc: messagebox.showerror("无法恢复", str(exc), parent=window)
        before = event.get("before") or {}
        restorable = before.get("active") if event["action"] == "确认并启用" else before.get("draft") or before.get("active")
        ctk.CTkButton(window, text="将修改前内容恢复为待确认", command=restore,
                      state="normal" if restorable else "disabled").pack(anchor="e", padx=20, pady=16)
        return window

    def _assistant_feedback(self, job):
        if not job: return
        job = copy.deepcopy(job)
        role = "artist" if job["kind"] in {"ai_image", "image_fission"} else "director"
        window = self._assistant_new_window(ROLES[role] + " · 结果反馈", "640x390")
        window.resizable(False, False)
        ctk.CTkLabel(window, text="哪里满意、哪里需要改进，或下次希望记住什么？", font=("Microsoft YaHei UI", 16, "bold")).pack(anchor="w", padx=20, pady=(20, 8))
        ctk.CTkLabel(window, text="自动关联这次任务的素材、提示词和结果，不需要多轮聊天。", text_color="#86868B").pack(anchor="w", padx=20)
        box = ctk.CTkTextbox(window, height=150, wrap="word"); box.pack(fill="x", padx=20, pady=12)
        automatic = self.assistant_store.enabled(role)
        ctk.CTkLabel(window, text="自进化已开启：保存后按 Kaiyun 用量计费整理建议，确认前不生效。" if automatic else "自进化已关闭：只保存反馈，可在设置中手动整理建议。",
                     text_color="#6E6E73", wraplength=590, justify="left").pack(anchor="w", padx=20)
        def save():
            feedback = box.get("1.0", "end-1c").strip()
            if not feedback: messagebox.showinfo("请补充反馈", "请写明具体问题或认可的改进。", parent=window); return
            credentials = self._credential_values()
            def work():
                source, added = add_feedback(self.assistant_store, job, feedback)
                result = {"count": 0, "note": "反馈已保存，可在设置中查看。"}
                if automatic and added:
                    try:
                        result = analyze_source(self.assistant_store, source["id"], credentials,
                            lambda note: self.events.put(("assistant_event", ("progress", note))), automatic=True)
                    except Exception as exc: result["note"] = "反馈已保存，建议整理未完成：" + str(exc)
                return result
            def done(result):
                self.assistant_status.set(f"反馈已保存，新增 {result['count']} 条待确认。" + result["note"])
                if role == "artist": self.ai_status.set(self.assistant_status.get())
                else: self.director_note.set(self.assistant_status.get())
            if self._assistant_work(work, done): window.destroy()
        ctk.CTkButton(window, text="保存并整理建议" if automatic else "保存反馈", command=save).pack(anchor="e", padx=20, pady=14)
        window.feedback_box, window.feedback_save = box, save
        return window

    def _assistant_existing_library(self):
        from personal_library import catalog
        import json
        try: rows = catalog(self.library_transfer.root)
        except ValueError as exc:
            messagebox.showerror("资料无法读取", str(exc), parent=self._assistant_window); return
        window = self._assistant_new_window("AI 导演 · 已有演示与经验", "920x730")
        ctk.CTkLabel(window, text="现有 Skill 正在使用的内置与个人资料；新学习内容在待确认中检查。",
                     text_color="#6E6E73").pack(anchor="w", padx=20, pady=14)
        choices = {f"{e['origin']} · {e['title']} · {e['id'][-8:]}": p for e, p in rows}
        selected = StringVar(value=next(iter(choices), "暂无已有资料"))
        box = ctk.CTkTextbox(window, wrap="word", height=350)
        gallery = ctk.CTkFrame(window, fg_color="white")
        def show(_=None):
            path = choices.get(selected.get())
            if path is None: return
            record = json.loads(path.read_text(encoding="utf-8-sig"))
            lines = [record["title"], "品类：" + record["category"], "摘要：" + record["summary"]]
            for unit in record.get("units", []):
                lines.append("\n" + unit["name"] + " · " + (unit.get("source_time") or "未记录时间"))
                for key, label in (("start_conditions", "起始条件"), ("action", "动作"), ("transition", "过程变化"),
                                   ("end_state", "结束状态"), ("expected_effect", "可见效果"), ("avoid", "避免"), ("prompt_fragment", "写法参考")):
                    value = unit.get(key, "")
                    lines.append(label + "：" + ("；".join(value) if isinstance(value, list) else str(value)))
            for attempt in record.get("attempts", []):
                lines.append("\n尝试：" + attempt["id"])
                for key, label in (("observations", "观察"), ("hypotheses", "假设"), ("changes", "改动")):
                    lines.append(label + "：" + "；".join(attempt.get(key, [])))
            provenance = record.get("provenance", {})
            lines.extend(["\n观察方式：" + provenance.get("observation_method", "用户维护的历史经验"),
                          "限制：" + "；".join(provenance.get("limitations", record.get("unknowns", [])))])
            box.configure(state="normal"); box.delete("1.0", "end"); box.insert("1.0", "\n".join(lines)); box.configure(state="disabled")
            for widget in gallery.winfo_children(): widget.destroy()
            for media in record.get("keyframes", [])[:4]:
                image = ctk.CTkLabel(gallery, text="加载中", width=190, height=125); image.pack(side="left", padx=8, pady=8)
                location = relative_file(path.parent, media["path"])
                self._ai_bind_thumbnail(image, str(location), (190, 125))
                image.bind("<Button-1>", lambda _, p=str(location): self._open_task_result(p))
        ctk.CTkOptionMenu(window, values=list(choices) or ["暂无已有资料"], variable=selected, command=show, dynamic_resizing=False, width=850).pack(fill="x", padx=20, pady=8)
        box.pack(fill="both", expand=True, padx=20, pady=8); gallery.pack(fill="x", padx=20, pady=8)
        def supplement():
            editor = self._assistant_edit("director")
            if editor:
                field = editor.assistant_fields["evidence"]
                field.delete("1.0", "end"); field.insert("1.0", "基于已有资料：" + selected.get() + "；用户补充的要求。")
        self._secondary_button(window, "为此补充个人规则", supplement, width=170).pack(anchor="e", padx=20, pady=14)
        show()
        return window
