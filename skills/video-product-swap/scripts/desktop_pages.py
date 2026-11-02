"""Desktop history/library views; UI work stays on Tk's main thread."""
import json
import os
import shutil
import threading
import time
from pathlib import Path
from tkinter import Menu, StringVar, filedialog, messagebox, ttk

import customtkinter as ctk
from PIL import Image

from desktop_tasks import KINDS, LABELS, ACTIVE


class DesktopPages:
    def _asset_name(self, path):
        return next((a["name"] for a in self.desktop_store.assets() if a["path"] == str(path)), Path(path).name)

    def _restore_history(self):
        # Import old provider records without generating anything or scanning unrelated folders.
        output = Path(self.workspace_output_dir_var.get()).expanduser()
        if output.is_dir():
            for path in output.glob("*.video-task.json"):
                try: self.desktop_store.import_record(path)
                except (OSError, ValueError, KeyError): pass
        credentials = self._credential_values()
        from model_runner.runner import ModelRunner
        runner = ModelRunner(credential_source="host-app", values=credentials)
        for job in self.desktop_store.jobs():
            if job["status"] != "attention": continue
            path = job.get("task_state_file") or job["config"]["output"] + ".video-task.json"
            try:
                state = json.loads(Path(path).read_text(encoding="utf-8"))
                if state.get("status") == "failed" or not any(state.get(k) for k in ("task_id", "video_url", "audio_url", "poll_url")): continue
                if job["kind"] == "speech":
                    if state.get("audio_url"):
                        self.tasks.recover(job["id"], credentials)
                    continue
                if job["kind"] == "image_audio":
                    from fish_mcp import FishAuth
                    if state.get("video_url") or FishAuth(credentials.get("FISH_AUTH_PATH")).connected():
                        self.tasks.recover(job["id"], credentials)
                    continue
                runner.credentials.resolve(state["model"]["provider"], require_key=not bool(state.get("video_url")))
                self.tasks.recover(job["id"], credentials)
            except Exception:
                # Leave the record visible for manual recovery when credentials/network are available.
                pass
        self._refresh_history()

    def _build_task_pages(self):
        self.history_filter = StringVar(value="全部功能")
        self.history_state = StringVar(value="全部状态")
        self.history_search = StringVar()
        self.history_sync_note = StringVar(value="后台自动同步任务进度")
        self.history_sync_busy = False
        self.asset_search = StringVar()
        self.asset_filter = StringVar(value="全部素材")
        self.asset_group_filter = StringVar(value="全部分组")
        self._asset_group_id, self._asset_target = "all", None
        self._asset_group_labels, self._asset_group_options = {}, []
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("Tasks.Treeview", rowheight=48, font=("Microsoft YaHei UI", 10), background="white", fieldbackground="white", borderwidth=0)
        style.configure("Tasks.Treeview.Heading", font=("Microsoft YaHei UI", 10, "bold"))
        style.map("Tasks.Treeview", background=[("selected", "#EAF4FF")], foreground=[("selected", "#0071E3")])
        for tab, title, note in ((self.history_tab, "使用记录", "查看创作进度与结果"), (self.library_tab, "素材库", "整理参考素材，随时用于新的创作")):
            ctk.CTkLabel(tab, text=title, font=("Microsoft YaHei UI", 26, "bold")).pack(anchor="w", padx=28, pady=(24, 4))
            ctk.CTkLabel(tab, text=note, text_color="#86868B").pack(anchor="w", padx=28, pady=(0, 18))
        controls = ctk.CTkFrame(self.history_tab, fg_color="transparent")
        controls.pack(fill="x", padx=28)
        for variable, values, width in ((self.history_state, ["全部状态", "进行中", "已完成", "需要处理", "已取消"], 110), (self.history_filter, ["全部功能", *KINDS.values()], 115)):
            ctk.CTkOptionMenu(controls, variable=variable, values=values, width=width, command=lambda _: self._refresh_history(), fg_color="#EDEDF0", button_color="#E2E2E7", text_color="#303035").pack(side="left", padx=(0, 8))
        ctk.CTkLabel(controls, text="搜索", text_color="#86868B", width=34).pack(side="left")
        ctk.CTkEntry(controls, textvariable=self.history_search, placeholder_text="搜索任务", width=180).pack(side="left", fill="x", expand=True, padx=(0, 8))
        self.history_refresh_button = self._secondary_button(controls, "刷新", self._sync_history, width=70)
        self.history_refresh_button.pack(side="right")
        self.history_more_button = self._secondary_button(controls, "···", self._history_more, width=42)
        self.history_more_button.pack(side="right", padx=6)
        self.task_count_var = StringVar()
        ctk.CTkLabel(self.history_tab, textvariable=self.task_count_var, text_color="#86868B").pack(anchor="w", padx=28, pady=(10, 0))
        ctk.CTkLabel(self.history_tab, textvariable=self.history_sync_note, text_color="#86868B", wraplength=720, justify="left").pack(anchor="w", padx=28, pady=(0, 8))
        self.history_body = ctk.CTkFrame(self.history_tab, fg_color="transparent")
        self.history_body.pack(fill="both", expand=True)
        self.history_tree = self._table(self.history_body, ("name", "kind", "status", "stage", "created"),
            ("任务", "功能", "状态", "进度", "创建时间"), (210, 90, 75, 180, 110))
        self.history_drawer = ctk.CTkFrame(self.history_body, width=350, fg_color="white", corner_radius=12)
        self.history_drawer.pack_propagate(False)
        self.history_actions = ctk.CTkFrame(self.history_tab, fg_color="transparent")
        self.history_actions.pack(side="bottom", fill="x", padx=28, pady=14, before=self.history_body)
        self.history_primary = ctk.CTkButton(self.history_actions, text="查看详情", width=115, command=self._task_detail)
        self.history_primary.pack(side="left")
        self.history_action_more = self._secondary_button(self.history_actions, "更多操作 ···", self._history_selected_more, width=110)
        self.history_action_more.pack(side="left", padx=8)
        self.history_tree.bind("<<TreeviewSelect>>", lambda _: self._history_selection())
        self.history_tree.bind("<Double-1>", lambda _: self._task_detail())

        controls = ctk.CTkFrame(self.library_tab, fg_color="transparent")
        controls.pack(fill="x", padx=28, pady=(0, 8))
        self.asset_type_menu = ctk.CTkOptionMenu(controls, variable=self.asset_filter, values=["全部素材", "视频", "图片", "音频"], width=105,
            command=lambda _: self._refresh_library(), fg_color="#EDEDF0", button_color="#E2E2E7", text_color="#303035")
        self.asset_type_menu.pack(side="left", padx=(0, 10))
        ctk.CTkLabel(controls, text="搜索", text_color="#86868B", width=34).pack(side="left")
        self.asset_search_entry = ctk.CTkEntry(controls, textvariable=self.asset_search, placeholder_text="搜索素材名称")
        self.asset_search_entry.pack(side="left", fill="x", expand=True, padx=(0, 10))
        self.asset_import_button = ctk.CTkButton(controls, text="导入素材", width=110, command=self._import_assets)
        self.asset_import_button.pack(side="right")
        self.asset_target_var, self.asset_import_target_var = StringVar(), StringVar()
        self.asset_selection_var, self.asset_count_var, self.asset_help_var = StringVar(), StringVar(), StringVar()
        self.asset_use_var = StringVar(value="")
        self.asset_import_status_label = ctk.CTkLabel(self.library_tab, textvariable=self.asset_target_var, text_color="#0071E3", wraplength=720, justify="left")
        self.asset_target_var.trace_add("write", lambda *_: self.asset_import_status_label.pack(anchor="w", padx=28, before=self.library_body) if self.asset_target_var.get() else self.asset_import_status_label.pack_forget())
        self.library_body = ctk.CTkFrame(self.library_tab, fg_color="transparent")
        self.library_body.pack(fill="both", expand=True, padx=23)
        groups = ctk.CTkFrame(self.library_body, width=145, fg_color="#EDEDF0", corner_radius=12)
        groups.pack(side="left", fill="y", padx=(5, 10))
        groups.pack_propagate(False)
        row = ctk.CTkFrame(groups, fg_color="transparent")
        row.pack(fill="x", padx=8, pady=8)
        ctk.CTkLabel(row, text="分组", font=("Microsoft YaHei UI", 12, "bold")).pack(side="left")
        self.asset_group_manage_button = self._secondary_button(row, "···", self._manage_asset_groups, width=34)
        self.asset_group_manage_button.pack(side="right")
        self.asset_groups_list = ctk.CTkScrollableFrame(groups, fg_color="transparent")
        self.asset_groups_list.pack(fill="both", expand=True, padx=2)
        from asset_grid import AssetGrid
        self.asset_tree = AssetGrid(self.library_body, self.desktop_store, self._refresh_asset_selection, self._preview_asset)
        self.asset_tree.pack(side="left", fill="both", expand=True)
        self.asset_empty_var = StringVar()
        self.asset_empty_label = ctk.CTkLabel(self.asset_tree.body, textvariable=self.asset_empty_var, text_color="#86868B")
        self.asset_action_card = ctk.CTkFrame(self.library_tab, fg_color="white", corner_radius=12)
        buttons = ctk.CTkFrame(self.asset_action_card, fg_color="transparent")
        buttons.pack(fill="x", padx=14, pady=12)
        ctk.CTkLabel(buttons, textvariable=self.asset_selection_var).pack(side="left", padx=(0, 15))
        self.asset_preview_button = self._secondary_button(buttons, "预览", self._preview_asset, width=65)
        self.asset_preview_button.pack(side="left")
        self.asset_move_button = self._secondary_button(buttons, "移动分组", self._move_selected_assets, width=90)
        self.asset_move_button.pack(side="left", padx=6)
        self.asset_more_button = self._secondary_button(buttons, "···", self._show_asset_more, width=38)
        self.asset_more_button.pack(side="left")
        self.asset_use_button = ctk.CTkButton(buttons, text="使用所选素材", width=155, command=self._use_assets)
        self.asset_cancel_pick = self._secondary_button(buttons, "取消选择", self._cancel_asset_pick, width=80)
        ctk.CTkLabel(self.asset_action_card, textvariable=self.asset_help_var, text_color="#86868B", wraplength=680, justify="left").pack(anchor="w", padx=14, pady=(0, 8))
        self.asset_more_menu = Menu(self.root, tearoff=False)
        self.asset_more_menu.add_command(label="重命名", command=self._rename_asset)
        self.asset_more_menu.add_separator()
        self.asset_more_menu.add_command(label="删除素材副本…", command=self._delete_asset)
        ctk.CTkLabel(self.library_tab, textvariable=self.asset_import_target_var, text_color="#86868B").pack(anchor="w", padx=28, pady=(4, 8))
        self.history_search.trace_add("write", lambda *_: self._refresh_history())
        self.asset_search.trace_add("write", lambda *_: self._refresh_library())
        self._refresh_history()
        self._refresh_library()
        self.root.after(1000, self._tick_tasks)

    def _table(self, parent, columns, headings, widths):
        frame = ctk.CTkFrame(parent, fg_color="white")
        frame.pack(fill="both", expand=True, padx=28)
        tree = ttk.Treeview(frame, columns=columns, show="headings", style="Tasks.Treeview", selectmode="extended")
        scroll = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y"); tree.pack(side="left", fill="both", expand=True)
        for key, heading, width in zip(columns, headings, widths):
            tree.heading(key, text=heading); tree.column(key, width=width, minwidth=55)
        return tree

    def _set_concurrency(self, value):
        self.tasks.set_limit(int(value))
        (self.desktop_store.root / "settings.json").write_text(json.dumps({"concurrency": int(value)}), encoding="utf-8")

    def _tick_tasks(self):
        if self.current_page == "history":
            self._refresh_history()
        elif self.current_page == "library":
            self._refresh_library()
        self.root.after(1000, self._tick_tasks)

    @staticmethod
    def _sync_table(tree, rows):
        ids = {identity for identity, _ in rows}
        for identity in tree.get_children():
            if identity not in ids:
                tree.delete(identity)
        for index, (identity, values) in enumerate(rows):
            if tree.exists(identity):
                tree.item(identity, values=values); tree.move(identity, "", index)
            else:
                tree.insert("", index, iid=identity, values=values)

    def _refresh_history(self):
        jobs = self.desktop_store.jobs()
        active = sum(j["status"] == "running" for j in jobs)
        waiting = sum(j["status"] == "queued" for j in jobs)
        self.task_count_var.set(f"执行中 {active} · 排队 {waiting} · 共 {len(jobs)} 条记录")
        rows = []
        for job in jobs:
            kind = KINDS.get(job["kind"], "旧任务")
            if self.history_filter.get() not in ("全部功能", kind): continue
            if self.history_search.get().casefold() not in (job["name"] + job["id"]).casefold(): continue
            groups = {"进行中": {"running", "queued"}, "已完成": {"success"}, "需要处理": {"attention", "failed", "paused"}, "已取消": {"cancelled"}}
            if self.history_state.get() in groups and job["status"] not in groups[self.history_state.get()]: continue
            rows.append((job["id"], (job["name"], kind, LABELS.get(job["status"], job["status"]), job["stage"],
                                     time.strftime("%m-%d %H:%M:%S", time.localtime(job["created"])))))
        self._sync_table(self.history_tree, rows)
        self._history_selection()

    def _refresh_library(self):
        rows, jobs = [], self.desktop_store.jobs()
        groups = self.desktop_store.groups()
        names = {group["id"]: group["name"] for group in groups}
        self._asset_group_labels = {"全部分组": "all", "未分组": None,
                                    **{group["name"]: group["id"] for group in groups}}
        options = list(self._asset_group_labels)
        if options != self._asset_group_options:
            for widget in self.asset_groups_list.winfo_children(): widget.destroy()
            self.asset_group_buttons = {}
            for name in options:
                button = ctk.CTkButton(self.asset_groups_list, text=name, anchor="w", width=105, height=34,
                    fg_color="transparent", text_color="#303035", hover_color="#DFE5EE", command=lambda value=name: self._choose_group(value))
                button.pack(fill="x", pady=2)
                self.asset_group_buttons[name] = button
            self._asset_group_options = options
        if self._asset_group_id not in ("all", None) and self._asset_group_id not in names:
            self._asset_group_id = None
        label = next((label for label, identity in self._asset_group_labels.items()
                      if identity == self._asset_group_id), "未分组")
        if self.asset_group_filter.get() != label:
            self.asset_group_filter.set(label)
        self.asset_import_target_var.set(f"新素材导入到：{names.get(self._asset_group_id, '未分组')}")
        labels = {"video": "视频", "image": "图片", "audio": "音频"}
        assets = self.desktop_store.assets()
        for asset in assets:
            kind = labels[asset["kind"]]
            if self._asset_group_id != "all" and asset.get("group_id") != self._asset_group_id: continue
            if self.asset_filter.get() not in ("全部素材", kind): continue
            if self.asset_search.get().casefold() not in asset["name"].casefold(): continue
            used = sum(asset["id"] in j.get("asset_ids", []) for j in jobs)
            rows.append((asset["id"], (asset["name"], names.get(asset.get("group_id"), "未分组"),
                                         kind, f"{asset['size']/1024/1024:.2f} MB", used)))
        self.asset_tree.set_rows(rows)
        for name, button in self.asset_group_buttons.items():
            button.configure(fg_color="#D9E9FC" if name == label else "transparent")
        self.asset_count_var.set(f"当前显示 {len(rows)} 项")
        self._refresh_asset_selection()

    def _choose_group(self, name):
        self.asset_group_filter.set(name)
        self._select_asset_group(name)

    def _browse_library(self):
        self._asset_target = None
        self.asset_use_var.set("")
        self._show_page("library")
        self._refresh_asset_selection()

    def _cancel_asset_pick(self):
        self._asset_target = None
        self.asset_use_var.set("")
        self._show_page("image_audio")

    def _asset_use_changed(self, target):
        self._asset_target = target
        self._refresh_asset_selection()

    def _asset_selection_problem(self, assets):
        if not self._asset_target: return ""
        kind = "audio" if self._asset_target == "audio" else "image"
        if len(assets) != 1 or assets[0]["kind"] != kind:
            return "请选择一条音频。" if kind == "audio" else "请选择一张图片。"
        return ""

    def _refresh_asset_selection(self):
        assets = self._selected_assets()
        count = len(assets)
        problem = self._asset_selection_problem(assets)
        self.asset_selection_var.set(f"已选 {count} 项")
        if count or self._asset_target:
            self.asset_action_card.pack(side="bottom", fill="x", padx=28, pady=8, before=self.library_body)
        else:
            self.asset_action_card.pack_forget()
        if self._asset_target:
            from image_audio_video import FIELD_LABELS
            self.asset_help_var.set(problem or "填入后请返回创作页确认。")
            self.asset_use_button.configure(state="disabled" if problem else "normal", text="用作" + FIELD_LABELS[self._asset_target])
            self.asset_use_button.pack(side="right")
            self.asset_cancel_pick.pack(side="right", padx=6)
        else:
            self.asset_use_button.pack_forget()
            self.asset_cancel_pick.pack_forget()
            self.asset_help_var.set("双击预览；在创作页面选择素材用途。")
        self.asset_preview_button.configure(state="normal" if count == 1 else "disabled")
        self.asset_move_button.configure(state="normal" if count else "disabled")
        self.asset_more_button.configure(state="normal" if count else "disabled")
        self.asset_more_menu.entryconfigure(0, state="normal" if count == 1 else "disabled")
        self.asset_more_menu.entryconfigure(2, state="normal" if count else "disabled")

    def _show_asset_more(self):
        self._refresh_asset_selection()
        if not self._selected_assets(): return
        try:
            self.asset_more_menu.tk_popup(self.asset_more_button.winfo_rootx(),
                self.asset_more_button.winfo_rooty() + self.asset_more_button.winfo_height())
        finally:
            self.asset_more_menu.grab_release()

    def _select_asset_group(self, label):
        self._asset_group_id = self._asset_group_labels.get(label, "all")
        self._refresh_library()

    def _manage_asset_groups(self):
        win = ctk.CTkToplevel(self.root)
        win.title("管理素材分组")
        win.geometry("420x210")
        ctk.CTkLabel(win, text="选择分组后可重命名或删除；删除只将素材移到未分组。",
                     wraplength=380).pack(padx=16, pady=(18, 12))
        choice = StringVar()
        labels = {}
        menu = ctk.CTkOptionMenu(win, variable=choice, width=300)
        menu.pack(pady=4)
        def reload_groups(preferred_id=None):
            nonlocal labels
            labels = {f"{group['name']}": group["id"] for group in self.desktop_store.groups()}
            menu.configure(values=list(labels) or ["暂无自定义分组"])
            selected = next((name for name, identity in labels.items()
                             if identity == (preferred_id if preferred_id is not None else self._asset_group_id)), None)
            choice.set(selected or next(iter(labels), "暂无自定义分组"))
        def create():
            value = ctk.CTkInputDialog(text="输入新的分组名称", title="新建分组").get_input()
            if value is None: return
            try:
                group = self.desktop_store.create_group(value)
                self._asset_group_id = group["id"]
                self._refresh_library(); reload_groups(group["id"])
            except ValueError as exc: messagebox.showerror("无法新建", str(exc), parent=win)
        def rename():
            identity = labels.get(choice.get())
            if not identity: return
            value = ctk.CTkInputDialog(text="输入新的分组名称", title="重命名分组").get_input()
            if value is None: return
            try:
                self.desktop_store.rename_group(identity, value)
                self._refresh_library(); reload_groups(identity)
            except ValueError as exc: messagebox.showerror("无法重命名", str(exc), parent=win)
        def delete():
            identity = labels.get(choice.get())
            if not identity: return
            if not messagebox.askyesno("删除分组", f"删除「{choice.get()}」？组内素材会移到未分组。", parent=win): return
            try:
                self.desktop_store.delete_group(identity)
                if self._asset_group_id == identity: self._asset_group_id = None
                self._refresh_library(); reload_groups()
            except ValueError as exc: messagebox.showerror("无法删除", str(exc), parent=win)
        bar = ctk.CTkFrame(win, fg_color="transparent"); bar.pack(pady=14)
        for title, callback in (("新建", create), ("重命名", rename), ("删除", delete)):
            ctk.CTkButton(bar, text=title, width=95, command=callback).pack(side="left", padx=5)
        reload_groups()
        win.after(100, win.lift)

    def _move_selected_assets(self):
        selected = [identity for identity in self.asset_tree.selection() if self.asset_tree.exists(identity)]
        if not selected:
            messagebox.showinfo("移动素材", "请先选择要移动的素材。", parent=self.root); return
        win = ctk.CTkToplevel(self.root); win.title("移动素材到分组"); win.geometry("390x170")
        ctk.CTkLabel(win, text=f"将 {len(selected)} 个素材移动到：").pack(pady=(18, 8))
        labels = {"未分组": None, **{g["name"]: g["id"] for g in self.desktop_store.groups()}}
        choice = StringVar(value=next(iter(labels)))
        ctk.CTkOptionMenu(win, variable=choice, values=list(labels), width=260).pack()
        def apply():
            try:
                self.desktop_store.move_assets(selected, labels[choice.get()])
                win.destroy(); self._refresh_library()
            except ValueError as exc: messagebox.showerror("无法移动", str(exc), parent=win)
        ctk.CTkButton(win, text="移动", command=apply).pack(pady=14)
        win.after(100, win.lift)

    def _selected_job(self):
        selected = self.history_tree.selection()
        return self.desktop_store.job(selected[0]) if selected else None

    def _task_detail(self):
        job = self._selected_job()
        if not job: return
        for widget in self.history_drawer.winfo_children(): widget.destroy()
        self.history_tree.configure(displaycolumns=("name", "status"))
        self.history_drawer.pack(side="right", fill="y", padx=(0, 20), before=self.history_tree.master)
        def close_details():
            self.history_drawer.pack_forget()
            self.history_tree.configure(displaycolumns="#all")
        self._secondary_button(self.history_drawer, "关闭详情 ×", close_details, width=100).pack(anchor="e", padx=12, pady=10)
        body = ctk.CTkScrollableFrame(self.history_drawer, fg_color="white")
        body.pack(fill="both", expand=True, padx=10)
        for text, color in ((job["name"], "#1D1D1F"), (LABELS[job["status"]] + " · " + job["stage"], "#0071E3")):
            ctk.CTkLabel(body, text=text, text_color=color, wraplength=285, justify="left", anchor="w").pack(fill="x", pady=6)
        if job.get("error"):
            ctk.CTkLabel(body, text=job["error"], text_color="#B94A48", wraplength=285, justify="left").pack(fill="x", pady=8)
        config = job["config"]
        spec = " · ".join(str(config[k]) for k in ("model", "duration", "resolution", "aspect_ratio") if config.get(k))
        if job["kind"] == "speech":
            settings = {**config, **job.get("result", {})}
            mode = settings.get("audio_settings_mode")
            volume = (f"{settings.get('volume_db', 0):+.1f} dB" if mode in {"fish", "fish_original"}
                      else f"{settings.get('volume', 100)}%（旧版本地调整）")
            spec = (f"音色：{config.get('voice_name') or config.get('voice_id', '')}\n"
                    f"Drama 3 · 语速 {settings.get('speed', 1)}× · 音量 {volume}")
            if mode == "fish_original":
                spec += "\nFish 原始音频，未应用旧版本地调整。"
        ctk.CTkLabel(body, text=spec, text_color="#86868B", wraplength=285, justify="left").pack(fill="x", pady=8)
        for asset in self.desktop_store.assets():
            if asset["id"] in job.get("asset_ids", []):
                ctk.CTkLabel(body, text="素材 · " + asset["name"], wraplength=285, justify="left").pack(anchor="w")
        prompt = ctk.CTkTextbox(body, height=170, wrap="word")
        prompt.pack(fill="x", pady=10)
        prompt.insert("1.0", (config.get("text") or "") if job["kind"] == "speech" else job.get("actual_prompt") or config.get("prompt") or "此任务没有提示词")
        prompt.configure(state="disabled")
        result = job.get("result", {})
        path = result.get("output")
        if path and Path(path).is_file():
            ctk.CTkButton(body, text="播放音频" if job["kind"] == "speech" else "查看结果", command=lambda: self._open_task_result(path)).pack(fill="x", pady=5)
            self._secondary_button(body, "另存为…", lambda: self._save_task_result(path), width=120).pack(anchor="w", pady=5)
            self._secondary_button(body, "加入素材库", lambda: self._import_paths([path]), width=120).pack(anchor="w", pady=5)
        view_url = result.get("view_url")
        state = {}
        try:
            state = json.loads(Path(job.get("task_state_file") or config["output"] + ".video-task.json").read_text(encoding="utf-8"))
            view_url = view_url or state.get("view_url")
        except (OSError, ValueError, KeyError): pass
        if view_url:
            from urllib.parse import urlparse
            import webbrowser
            parsed = urlparse(view_url)
            if parsed.scheme == "https" and (parsed.hostname == "fish.audio" or (parsed.hostname or "").endswith(".fish.audio")):
                self._secondary_button(body, "Fish 任务详情 ↗", lambda: webbrowser.open(view_url), width=145).pack(anchor="w", pady=6)
        logs = ctk.CTkTextbox(body, height=220, wrap="word")
        logs.insert("1.0", json.dumps({"task_id": job.get("task_id"), "config": config, "events": job.get("events", [])}, ensure_ascii=False, indent=2))
        logs.configure(state="disabled")
        def toggle():
            logs.pack_forget() if logs.winfo_manager() else logs.pack(fill="x", pady=8)
        self._secondary_button(body, "查看技术详情", toggle, width=120).pack(anchor="w", pady=6)

    def _history_selection(self):
        job = self._selected_job()
        if not job:
            self.history_primary.configure(state="disabled")
            self.history_action_more.configure(state="disabled")
            return
        title, command = "查看详情", self._task_detail
        path = job.get("result", {}).get("output")
        if job["status"] == "success" and path and Path(path).is_file():
            title, command = ("播放音频" if job["kind"] == "speech" else "查看成片"), lambda: self._open_task_result(path)
        elif job["status"] == "queued":
            title, command = "取消排队", self._cancel_selected
        elif job["status"] == "attention":
            try:
                state = json.loads(Path(job.get("task_state_file") or job["config"]["output"] + ".video-task.json").read_text(encoding="utf-8"))
                if any(state.get(k) for k in ("task_id", "video_url", "audio_url", "poll_url")):
                    title, command = ("下载结果" if state.get("video_url") or state.get("audio_url") else "继续查询"), self._recover_selected
                elif job.get("config", {}).get("variant_backend") == "studio":
                    from voice_batch import can_resume
                    if can_resume(job):
                        title, command = "继续获取音频", self._recover_selected
            except (OSError, ValueError): pass
        self.history_primary.configure(text=title, command=command, state="normal")
        self.history_action_more.configure(state="normal")

    def _history_more(self):
        menu = Menu(self.root, tearoff=False)
        menu.add_command(label="导入旧记录", command=self._resume_task)
        menu.add_command(label="队列设置", command=lambda: self._show_page("api"))
        self._popup_menu(menu, self.history_more_button)

    def _history_selected_more(self):
        job = self._selected_job()
        if not job: return
        from feature_policy import retired
        menu = Menu(self.root, tearoff=False)
        menu.add_command(label="查看详情", command=self._task_detail)
        path = job.get("result", {}).get("output")
        if path and Path(path).is_file():
            menu.add_command(label="另存为…", command=lambda: self._save_task_result(path))
            menu.add_command(label="加入素材库", command=lambda: self._import_paths([path]))
        if not retired(job["kind"], job["config"]):
            menu.add_command(label="复用为新草稿", command=self._reuse_selected)
        if job["status"] not in ACTIVE:
            menu.add_separator()
            menu.add_command(label="删除记录…", command=self._delete_selected)
        self._popup_menu(menu, self.history_action_more)

    def _popup_menu(self, menu, button):
        try: menu.tk_popup(button.winfo_rootx(), button.winfo_rooty() + button.winfo_height())
        finally: menu.grab_release()

    def _open_task_result(self, path):
        try:
            os.startfile(path)
        except OSError as exc:
            messagebox.showerror("无法打开结果", str(exc), parent=self.root)

    def _save_task_result(self, path):
        source = Path(path)
        selected = filedialog.asksaveasfilename(parent=self.root, title="保存生成结果", initialfile=source.name,
            defaultextension=source.suffix, filetypes=[("生成结果", "*" + source.suffix), ("所有文件", "*.*")])
        if not selected:
            return
        target = Path(selected)
        if target.resolve() == source.resolve():
            return
        try:
            shutil.copy2(source, target)
        except OSError as exc:
            messagebox.showerror("无法保存结果", str(exc), parent=self.root)

    def _sync_history(self):
        if self.history_sync_busy: return
        self.history_sync_busy = True
        self.history_refresh_button.configure(state="disabled", text="刷新中")
        self.history_sync_note.set("正在同步任务状态…")
        self._refresh_history()
        credentials = self._credential_values()
        def run():
            from history_sync import refresh
            try: result = refresh(self.tasks, credentials)
            except Exception: result = {"synced": 0, "active": 0, "cooldown": 0, "errors": ["刷新未完成，请稍后重试。"]}
            self.events.put(("history_sync", result))
        threading.Thread(target=run, daemon=True).start()

    def _finish_history_sync(self, result):
        self.history_sync_busy = False
        self.history_refresh_button.configure(state="normal", text="刷新")
        note = time.strftime("%H:%M:%S") + f" 已刷新列表 · 同步 {result['synced']} 条"
        if result["active"]: note += f" · {result['active']} 条由后台持续同步"
        if result["cooldown"]: note += " · 部分任务等待服务查询间隔"
        if result["errors"]: note += " · " + "；".join(result["errors"][:2])
        self.history_sync_note.set(note)
        self._refresh_history()

    def _recover_selected(self):
        job = self._selected_job()
        if job:
            if job["kind"] == "speech" and job.get("config", {}).get("variant_backend") == "studio":
                self._show_page("voice")
                self._voice_resume_job_id(job["id"])
                return
            try: self.tasks.recover(job["id"], self._credential_values())
            except Exception as exc: messagebox.showerror("无法继续", str(exc), parent=self.root)

    def _cancel_selected(self):
        job = self._selected_job()
        if job:
            try: self.tasks.cancel(job["id"])
            except Exception as exc: messagebox.showinfo("取消排队", str(exc), parent=self.root)

    def _delete_selected(self):
        job = self._selected_job()
        if job and messagebox.askyesno("删除记录", "仅删除这条使用记录，保留素材和成片。", parent=self.root):
            try:
                self.desktop_store.delete_job(job["id"])
                self._refresh_history()
                self._refresh_submission_feedback()
            except Exception as exc: messagebox.showerror("无法删除", str(exc), parent=self.root)

    def _reuse_selected(self):
        job = self._selected_job()
        if not job: return
        from feature_policy import retired
        if retired(job["kind"], job["config"]):
            messagebox.showinfo("功能已下线", "历史任务保留查看、查询与下载，不再创建同类新任务。", parent=self.root)
        elif job["kind"] == "image_audio":
            self._fish_reuse(job["config"])
        elif job["kind"] == "speech":
            self._voice_reuse(job["config"])
            self._show_page("voice")
        else:
            self._open_canvas()

    def _open_library(self, target):
        from image_audio_video import MEDIA_FIELDS
        if target not in MEDIA_FIELDS: return
        self._asset_target = target
        self.asset_use_var.set(target)
        self.asset_tree.selection_remove(*self.asset_tree.selection())
        self.asset_search.set("")
        self.asset_filter.set("音频" if target == "audio" else "图片")
        self._refresh_library()
        self._show_page("library")

    def _import_assets(self):
        group_id = self._asset_group_id if self._asset_group_id != "all" else None
        paths = filedialog.askopenfilenames(parent=self.root, title="导入视频、图片或音频")
        if paths: self._import_paths(paths, group_id=group_id)

    def _import_paths(self, paths, group_id=None):
        def run():
            errors, duplicates, imported = [], 0, 0
            known = {asset["id"] for asset in self.desktop_store.assets()}
            for path in paths:
                try:
                    asset = self.desktop_store.import_asset(path, group_id=group_id)
                    if asset["id"] in known: duplicates += 1
                    else: imported += 1; known.add(asset["id"])
                except Exception as exc: errors.append(f"{Path(path).name}：{exc}")
            self.events.put(("assets_changed", {"errors": errors, "duplicates": duplicates, "imported": imported}))
        threading.Thread(target=run, daemon=True).start()
        self.asset_target_var.set("正在导入素材…")

    def _selected_assets(self):
        selected = {identity for identity in self.asset_tree.selection() if self.asset_tree.exists(identity)}
        return [a for a in self.desktop_store.assets() if a["id"] in selected]

    def _use_assets(self):
        assets = self._selected_assets()
        if not self._asset_target or self._asset_selection_problem(assets): return
        self._fish_load(self._asset_target, assets[0]["path"])
        self._asset_target = None
        self.asset_use_var.set("")
        self._show_page("image_audio")

    def _preview_asset(self):
        assets = self._selected_assets()
        if len(assets) == 1:
            try: os.startfile(assets[0]["path"])
            except OSError as exc: messagebox.showerror("无法预览", str(exc), parent=self.root)

    def _rename_asset(self):
        assets = self._selected_assets()
        if len(assets) == 1:
            value = ctk.CTkInputDialog(text="输入新的素材名称", title="重命名").get_input()
            if value:
                try: self.desktop_store.rename_asset(assets[0]["id"], value)
                except ValueError as exc: messagebox.showerror("无法重命名", str(exc), parent=self.root)
                self._refresh_library()

    def _delete_asset(self):
        assets = self._selected_assets()
        if assets and messagebox.askyesno("删除素材", f"删除选中的 {len(assets)} 个素材库副本？原始文件不受影响，正在被任务引用的素材会保留。", parent=self.root):
            deleted, failures = 0, []
            for asset in assets:
                try: self.desktop_store.delete_asset(asset["id"]); deleted += 1
                except Exception as exc: failures.append(f"{asset['name']}：{exc}")
            if failures:
                messagebox.showerror("删除结果", f"已删除 {deleted} 个，未删除 {len(failures)} 个。\n" + "\n".join(failures), parent=self.root)
            self._refresh_library()
