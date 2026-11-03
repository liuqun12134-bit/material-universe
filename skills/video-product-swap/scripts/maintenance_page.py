"""Library migration and update controls. Worker results return through the Tk queue."""
from __future__ import annotations

import threading
from datetime import datetime
from pathlib import Path
from tkinter import StringVar, Toplevel, filedialog, messagebox

import customtkinter as ctk

from app_version import VERSION
from app_updates import check_update, download_update
from library_transfer import PreparedBundle


class MaintenancePage:
    def _build_maintenance_settings(self, page):
        self._maintenance_busy = False
        self._maintenance_window = None
        self._update_on_exit = None
        self.library_scope = StringVar(value="全部库")
        self.library_status = StringVar(value=getattr(self, "_library_startup_note", "素材、个人案例与经验独立保存，更新程序会保留。"))
        self.update_status = StringVar(value=f"当前版本 {VERSION} · 手动检查正式更新")
        _, library = self._card(page, "数据与迁移", "迁移包包含实际文件、分组和个人案例；账号授权在新电脑重新配置。")
        row = ctk.CTkFrame(library, fg_color="transparent"); row.pack(fill="x")
        ctk.CTkOptionMenu(row, variable=self.library_scope, values=["全部库", "仅素材库", "仅 Skill 附属库"], width=170).pack(side="left", padx=(0, 10))
        self._secondary_button(row, "导出库", self._export_libraries, width=92).pack(side="left", padx=4)
        self._secondary_button(row, "导入迁移包", self._import_libraries, width=110).pack(side="left", padx=4)
        row = ctk.CTkFrame(library, fg_color="transparent"); row.pack(fill="x", pady=(10, 0))
        self._secondary_button(row, "导入已有 Skill 库", self._import_skill_library, width=145).pack(side="left", padx=(0, 8))
        self._secondary_button(row, "打开个人库", lambda: self._open_maintenance_folder(self.library_transfer.root), width=110).pack(side="left", padx=(0, 8))
        self._secondary_button(row, "打开备份", lambda: self._open_maintenance_folder(self.library_transfer.backups), width=100).pack(side="left")
        ctk.CTkLabel(library, textvariable=self.library_status, wraplength=650, justify="left", text_color="#6E6E73").pack(anchor="w", pady=(10, 0))
        _, updates = self._card(page, "软件更新", "下载 GitHub 正式版本，校验并备份后覆盖升级，无需先卸载。")
        self._secondary_button(updates, "检查更新", self._check_application_update, width=120).pack(anchor="w")
        ctk.CTkLabel(updates, textvariable=self.update_status, wraplength=650, justify="left", text_color="#6E6E73").pack(anchor="w", pady=(10, 0))

    def _open_maintenance_folder(self, path):
        import os
        path.mkdir(parents=True, exist_ok=True)
        os.startfile(str(path))

    def _maintenance_idle(self):
        busy = (self.tasks.active or self.tasks.pending or getattr(self.tasks, "refreshing", set())
                or any(getattr(self, name, False) for name in ("_assistant_busy", "voice_busy", "voice_clone_busy", "director_busy", "fish_busy", "fish_login_busy", "ai_image_busy", "ai_model_busy", "running")))
        if busy:
            messagebox.showinfo("请等待当前任务", "请等生成、上传或查询结束后再迁移或安装更新。素材和任务记录会保留。", parent=self.root)
            return False
        return True

    def _maintenance_work(self, title, work, done):
        if self._maintenance_busy or self._exit_in_progress:
            return
        self._maintenance_busy = True
        # Native Toplevel has no delayed titlebar/focus callbacks that can outlive
        # a very fast operation and target an already destroyed progress window.
        window = self._maintenance_window = Toplevel(self.root, bg="#F5F5F7")
        window.title(title); window.geometry("460x150"); window.resizable(False, False)
        window.transient(self.root); window.grab_set()
        window.protocol("WM_DELETE_WINDOW", lambda: None)
        self._maintenance_note = StringVar(value=title + "…")
        ctk.CTkLabel(window, textvariable=self._maintenance_note, wraplength=410).pack(expand=True, padx=20, pady=20)
        self._maintenance_done = done
        def worker():
            try:
                result = work()
                self.events.put(("maintenance_event", ("done", result, None)))
            except Exception as exc:
                self.events.put(("maintenance_event", ("done", None, str(exc))))
        threading.Thread(target=worker, daemon=True).start()

    def _handle_maintenance_event(self, payload):
        if payload[0] == "progress":
            self._maintenance_note.set(payload[1]); return
        _, result, error = payload
        self._maintenance_busy = False
        window, self._maintenance_window = self._maintenance_window, None
        if window:
            window.grab_release(); window.destroy()
        if error:
            messagebox.showerror("操作未完成", error, parent=self.root)
            return
        self._maintenance_done(result)

    def _export_libraries(self):
        if self._maintenance_busy or not self._maintenance_idle():
            return
        selected = filedialog.asksaveasfilename(parent=self.root, title="导出库", defaultextension=".mu-library.zip",
            initialfile="素材万象-库备份-" + datetime.now().strftime("%Y%m%d-%H%M%S") + ".mu-library.zip",
            filetypes=[("素材万象迁移包", "*.zip")])
        if not selected:
            return
        scope = self.library_scope.get()
        self._maintenance_work("正在导出库", lambda: self.library_transfer.export(selected, assets=scope != "仅 Skill 附属库", skills=scope != "仅素材库"),
            lambda result: self.library_status.set(f"已导出 {result['assets']} 个素材、{result['files']} 个文件：{result['path']}"))

    def _import_libraries(self):
        if self._maintenance_busy or not self._maintenance_idle():
            return
        selected = filedialog.askopenfilename(parent=self.root, title="选择素材万象迁移包", filetypes=[("素材万象迁移包", "*.zip")])
        if selected:
            self._prepare_library_import(lambda: PreparedBundle(selected))

    def _import_skill_library(self):
        if self._maintenance_busy or not self._maintenance_idle():
            return
        selected = filedialog.askdirectory(parent=self.root, title="选择含 demo-library / experience-library 的 Skill 目录")
        if not selected:
            return
        if not any((Path(selected) / name).is_dir() for name in ("demo-library", "experience-library", "learning", "assistant-knowledge")):
            messagebox.showerror("没有找到库", "请选择含案例库或经验库的 Skill 目录。", parent=self.root); return
        include_learning = messagebox.askyesno("个人学习记录", "是否同时复制此 Skill 在本机保存的自优化记录？\n原 Skill 和原记录都会保留。", parent=self.root)
        self._prepare_library_import(lambda: self.library_transfer.import_skill_directory(selected, include_learning=include_learning))

    def _prepare_library_import(self, prepare):
        def work():
            prepared = prepare()
            try:
                return prepared, self.library_transfer.preview(prepared)
            except BaseException:
                prepared.close(); raise
        def preview(result):
            prepared, stats = result
            skills = stats["skills"]
            message = (f"素材：新增 {stats['assets_added']}，已存在 {stats['assets_duplicate']}，可恢复缺失副本 {stats['assets_repaired']}。\n"
                       f"Skill 案例：新增 {skills['added']}，重复 {skills['duplicates']}，冲突保留双方 {skills['conflicts']}。\n"
                       f"个人学习文件：新增 {skills['learning_added']}。\n\n"
                       "导入前自动备份。已有素材的名称、分组和案例内容保持原样。是否合并导入？")
            if not messagebox.askyesno("检查完成", message, parent=self.root) or not self._maintenance_idle():
                prepared.close(); return
            def apply():
                try:
                    return self.library_transfer.import_bundle(prepared)
                finally:
                    prepared.close()
            def completed(value):
                self._refresh_library()
                self.library_status.set(f"导入完成，新增 {value['assets_added']} 个素材，恢复 {value['assets_repaired']} 个副本。备份：{value['backup']}")
            self._maintenance_work("正在合并库", apply, completed)
        self._maintenance_work("正在检查迁移包", work, preview)

    def _check_application_update(self):
        def checked(update):
            if not update["available"]:
                self.update_status.set(update["message"]); return
            self.update_status.set("发现新版本 " + update["version"])
            if not messagebox.askyesno("发现新版本", f"当前 {VERSION} → {update['version']}\n\n{update['notes'][:1400]}\n\n是否下载更新？", parent=self.root):
                return
            def work():
                path = download_update(update, self.desktop_store.root / "updates", progress=lambda message: self.events.put(("maintenance_event", ("progress", message))))
                return update, path
            self._maintenance_work("正在下载更新", work, self._downloaded_update)
        self._maintenance_work("正在检查更新", check_update, checked)

    def _downloaded_update(self, result):
        import sys
        update, path = result
        self.update_status.set(f"{update['version']} 已下载并校验：{path}")
        installed = getattr(sys, "frozen", False) and (Path(sys.executable).parent / "unins000.exe").is_file()
        if not installed:
            messagebox.showinfo("更新已下载", f"当前为源码或便携运行。请退出软件后运行安装程序：\n{path}\n\n以后从安装版中可直接覆盖升级。", parent=self.root)
            return
        if not self._maintenance_idle():
            return
        if messagebox.askyesno("安装更新", "已校验更新文件。将保存画布、备份库并退出软件，然后覆盖升级并重新打开。现在安装？", parent=self.root):
            self._update_on_exit = (path, update["manifest"])
            self._request_exit()
