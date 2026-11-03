"""Image fission workspace. All Tk access happens on the main thread."""
from __future__ import annotations

import copy
import shutil
import threading
from pathlib import Path
from tkinter import StringVar, Toplevel, filedialog, messagebox

import customtkinter as ctk
from PIL import Image, ImageOps

from director_prompt import configuration
from fish_mcp import FishClient
from fission_chat import chat
from fission_service import MODEL, RATIOS, image_inputs, inspect_image, prepare_quote
from fission_store import FissionStore, MODES, ROLES, branch_turns

QUALITY_LABELS = {"low": "快速", "medium": "标准", "high": "精细", "xhigh": "更精细", "max": "最高"}

class FissionPage:
    def _init_fission(self):
        self.fission_store = FissionStore(self.desktop_store)
        sessions = self.fission_store.sessions()
        self.fission_id = sessions[0]["id"] if sessions else None
        self.fission_busy = False
        self._fission_loading = False
        self._fission_save_timer = None
        self._fission_preview_timer = None
        self._fission_job_signature = None
        self._fission_preview_image = None
        self._fission_preview_path = None
        self.fission_status = StringVar(value="先添加母图，再聊聊你想改哪里。")
        self.fission_mode = StringVar(value=MODES["refine"])
        self.fission_ratio = StringVar(value="跟随底图")
        self.fission_resolution = StringVar(value="1K")
        self.fission_quality = StringVar(value="high")

    def _build_fission_page(self):
        page = self.fission_tab
        top = ctk.CTkFrame(page, fg_color="transparent")
        top.pack(fill="x", padx=18, pady=(18, 10))
        ctk.CTkLabel(top, text="图片裂变", font=("Microsoft YaHei UI", 25, "bold")).pack(side="left")
        self._secondary_button(top, "账户设置", lambda: self._show_page("api"), width=86).pack(side="right")
        ctk.CTkLabel(top, text="保存每轮修改，随时回到满意的一版", text_color="#6E6E73").pack(side="left", padx=14)
        body = ctk.CTkFrame(page, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=16, pady=(0, 14))
        body.grid_columnconfigure(0, weight=0, minsize=154)
        body.grid_columnconfigure(1, weight=1, minsize=265)
        body.grid_columnconfigure(2, weight=1, minsize=318)
        body.grid_rowconfigure(0, weight=1)

        left = ctk.CTkFrame(body, width=154, fg_color="white", corner_radius=12)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 8)); left.grid_propagate(False)
        left.grid_columnconfigure(0, weight=1); left.grid_rowconfigure(3, weight=1)
        ctk.CTkButton(left, text="＋ 新建会话", command=self._fission_new, width=132).grid(row=0, column=0, padx=10, pady=10, sticky="ew")
        self.fission_sessions = ctk.CTkScrollableFrame(left, height=160, fg_color="white", width=128)
        self.fission_sessions.grid(row=1, column=0, sticky="ew", padx=4)
        ctk.CTkLabel(left, text="原始参考", font=("Microsoft YaHei UI", 13, "bold")).grid(row=2, column=0, sticky="w", padx=12, pady=(12, 4))
        self.fission_refs = ctk.CTkScrollableFrame(left, fg_color="white", width=128)
        self.fission_refs.grid(row=3, column=0, sticky="nsew", padx=4, pady=(0, 6))
        self._secondary_button(left, "重命名会话", self._fission_rename, width=128).grid(row=4, column=0, padx=10, pady=8, sticky="ew")

        middle = ctk.CTkFrame(body, fg_color="white", corner_radius=12)
        middle.grid(row=0, column=1, sticky="nsew", padx=(0, 8))
        middle.grid_columnconfigure(0, weight=1); middle.grid_rowconfigure(1, weight=1)
        self.fission_image_title = ctk.CTkLabel(middle, text="母图与版本", font=("Microsoft YaHei UI", 14, "bold"))
        self.fission_image_title.grid(row=0, column=0, pady=(12, 4))
        self.fission_preview = ctk.CTkLabel(middle, text="添加母图开始创作", text_color="#86868B", width=1, height=1)
        self.fission_preview.grid(row=1, column=0, sticky="nsew", padx=10, pady=8)
        self.fission_preview.bind("<Configure>", self._fission_resize_preview)
        self.fission_preview.bind("<Double-1>", lambda _: self._fission_compare())
        ctk.CTkLabel(middle, text="选择某一版，再在右侧继续聊", text_color="#86868B").grid(row=2, column=0)
        self.fission_versions = ctk.CTkScrollableFrame(middle, height=112, fg_color="#F6F7F9")
        self.fission_versions.grid(row=3, column=0, sticky="ew", padx=10, pady=6)
        actions = ctk.CTkFrame(middle, fg_color="transparent")
        actions.grid(row=4, column=0, sticky="ew", padx=10, pady=(4, 6)); actions.grid_columnconfigure((0, 1), weight=1)
        self._secondary_button(actions, "放大 / 对比", self._fission_compare, width=95).grid(row=0, column=0, sticky="ew", padx=(0, 3))
        self._secondary_button(actions, "另存图片", self._fission_export, width=90).grid(row=0, column=1, sticky="ew", padx=(3, 0))
        self.fission_use_button = ctk.CTkButton(middle, text="用作图音视频首帧", command=self._fission_use, height=34)
        self.fission_use_button.grid(row=5, column=0, sticky="ew", padx=10, pady=(0, 6))
        self.fission_recover_button = self._secondary_button(middle, "继续原任务 / 查看详情", self._fission_recover, width=160)
        self.fission_recover_button.grid(row=6, column=0, sticky="ew", padx=10, pady=(0, 10))

        right = ctk.CTkFrame(body, fg_color="white", corner_radius=12)
        right.grid(row=0, column=2, sticky="nsew")
        right.grid_columnconfigure(0, weight=1); right.grid_rowconfigure(1, weight=1)
        chat_header = ctk.CTkFrame(right, fg_color="transparent")
        chat_header.grid(row=0, column=0, sticky="ew", padx=12, pady=(10, 4))
        ctk.CTkLabel(chat_header, text="多轮聊天", font=("Microsoft YaHei UI", 14, "bold")).pack(side="left")
        history_button = self._secondary_button(chat_header, "全部记录", self._fission_history, width=76)
        history_button.configure(height=26)
        history_button.pack(side="right")
        self.fission_chat_log = ctk.CTkTextbox(right, wrap="word", height=145, fg_color="#F7F8FA", state="disabled")
        self.fission_chat_log.grid(row=1, column=0, sticky="nsew", padx=10, pady=(0, 6))
        self.fission_input = ctk.CTkTextbox(right, wrap="word", height=70, border_width=1, border_color="#E5E5EA")
        self.fission_input.grid(row=2, column=0, sticky="ew", padx=10)
        row = ctk.CTkFrame(right, fg_color="transparent"); row.grid(row=3, column=0, sticky="ew", padx=10, pady=6)
        self._secondary_button(row, "保留要求 / 参数", self._fission_settings, width=126).pack(side="left")
        self.fission_chat_button = ctk.CTkButton(row, text="发送", command=self._fission_send, width=74)
        self.fission_chat_button.pack(side="right")
        ctk.CTkLabel(right, text="聊天看图使用 Kaiyun，按用量计费", text_color="#86868B", font=("Microsoft YaHei UI", 10)).grid(row=4, column=0, sticky="w", padx=12)
        row = ctk.CTkFrame(right, fg_color="transparent"); row.grid(row=5, column=0, sticky="ew", padx=10, pady=(8, 4))
        ctk.CTkLabel(row, text="本轮修改稿", font=("Microsoft YaHei UI", 12, "bold")).pack(side="left")
        ctk.CTkOptionMenu(row, variable=self.fission_mode, values=list(MODES.values()), command=self._fission_mode_changed, width=130).pack(side="right")
        self.fission_prompt = ctk.CTkTextbox(right, wrap="word", height=88, border_width=1, border_color="#E5E5EA")
        self.fission_prompt.grid(row=6, column=0, sticky="ew", padx=10)
        self.fission_generate_button = ctk.CTkButton(right, text="预估费用并生成一张", command=self._fission_quote, height=38)
        self.fission_generate_button.grid(row=7, column=0, sticky="ew", padx=10, pady=(8, 4))
        ctk.CTkLabel(right, text="Fish · Sunburst · 预估会上传参考\n确认费用后生成一张", justify="left", font=("Microsoft YaHei UI", 10), text_color="#86868B").grid(row=8, column=0, sticky="w", padx=12)
        self.fission_status_label = ctk.CTkLabel(right, textvariable=self.fission_status, wraplength=290,
                                              justify="left", anchor="w", text_color="#0071E3")
        self.fission_status_label.grid(row=9, column=0, sticky="ew", padx=12, pady=(4, 10))
        for widget in (self.fission_input, self.fission_prompt):
            widget._textbox.bind("<<Modified>>", self._fission_edited, add=True)
        self._fission_render()

    def _fission_edited(self, event):
        widget = event.widget
        if not widget.edit_modified():
            return
        widget.edit_modified(False)
        if self._fission_loading:
            return
        if self._fission_save_timer:
            self.root.after_cancel(self._fission_save_timer)
        self._fission_save_timer = self.root.after(400, self._fission_save)

    def _fission_save(self):
        if not getattr(self, "fission_id", None) or not hasattr(self, "fission_input"):
            return
        if self._fission_save_timer:
            self.root.after_cancel(self._fission_save_timer)
            self._fission_save_timer = None
        if self._fission_loading:
            return
        s = self.fission_store.get(self.fission_id)
        fields = {"input": self.fission_input.get("1.0", "end-1c"), "prompt": self.fission_prompt.get("1.0", "end-1c"),
                  "mode": next(k for k, v in MODES.items() if v == self.fission_mode.get()),
                  "parameters": {"aspect_ratio": self.fission_ratio.get(), "resolution": self.fission_resolution.get(), "quality": self.fission_quality.get()}}
        if any(s.get(k) != v for k, v in fields.items()):
            self.fission_store.fields(s["id"], **fields)

    def _fission_on_show(self):
        if not self.fission_id:
            self.fission_id = self.fission_store.create()["id"]
        self._fission_render()

    def _fission_new(self):
        self._fission_save()
        self.fission_id = self.fission_store.create()["id"]
        self.fission_status.set("添加母图后，说说你想改哪里。")
        self._fission_render()

    def _fission_select_session(self, identity):
        self._fission_save()
        self.fission_id = identity
        self.fission_status.set("已恢复会话、原始参考和图片版本。")
        self._fission_render()

    def _fission_rename(self):
        if not self.fission_id:
            return
        dialog = ctk.CTkInputDialog(text="输入会话名称", title="重命名图片会话")
        name = dialog.get_input()
        if name and name.strip():
            self.fission_store.fields(self.fission_id, title=name.strip()[:80])
            self._fission_render(drafts=False)

    def _fission_render(self, drafts=True):
        for child in self.fission_sessions.winfo_children(): child.destroy()
        for session in self.fission_store.sessions():
            ctk.CTkButton(self.fission_sessions, text=session["title"][:10], width=120, anchor="w", height=32,
                          fg_color="#EAF4FF" if session["id"] == self.fission_id else "transparent",
                          text_color="#0071E3" if session["id"] == self.fission_id else "#303035", hover_color="#F1F3F6",
                          command=lambda i=session["id"]: self._fission_select_session(i)).pack(fill="x", pady=2)
        for child in self.fission_refs.winfo_children(): child.destroy()
        if not self.fission_id:
            return
        s = self.fission_store.get(self.fission_id)
        for role, title in ROLES.items():
            ref = next((r for r in s["references"] if r["role"] == role), None)
            ctk.CTkButton(self.fission_refs, text=("✓ " if ref else "＋ ") + title, width=120, height=30,
                          fg_color="#F3F5F8", text_color="#303035", hover_color="#EAF4FF",
                          command=lambda r=role: self._fission_reference_dialog(r)).pack(fill="x", pady=(5, 0))
            if ref:
                ctk.CTkLabel(self.fission_refs, text=ref.get("name", "图片")[:22], wraplength=112,
                             text_color="#86868B", font=("Microsoft YaHei UI", 10)).pack(fill="x")
        if drafts:
            self._fission_loading = True
            for widget, text in ((self.fission_input, s["input"]), (self.fission_prompt, s["prompt"])):
                widget.delete("1.0", "end"); widget.insert("1.0", text); widget._textbox.edit_modified(False)
            self.fission_mode.set(MODES[s["mode"]])
            self.fission_ratio.set(s["parameters"]["aspect_ratio"])
            self.fission_resolution.set(s["parameters"]["resolution"])
            self.fission_quality.set(s["parameters"]["quality"])
            self._fission_loading = False
        history = []
        for t in branch_turns(s):
            history.append("你：" + t["user"])
            if t.get("reply"): history.append("助手：" + t["reply"])
            if t["status"] == "pending": history.append("正在整理…" if self.fission_busy else "此轮未完成，可重新发送；未生成图片。")
            if t.get("error"): history.append("此轮未完成：" + t["error"])
        if not history:
            history = ["上传母图，说说想保留和修改的地方。\n\n例如：人物和构图保持，只把手里的产品换成参考图里的产品。\n\n选中旧版本再发送，会从那一版另开修改方向。"]
        self.fission_chat_log.configure(state="normal")
        self.fission_chat_log.delete("1.0", "end"); self.fission_chat_log.insert("1.0", "\n\n".join(history))
        self.fission_chat_log.configure(state="disabled"); self.fission_chat_log.see("end")
        self._fission_render_versions(s)

    def _fission_render_versions(self, s=None):
        if not self.fission_id:
            return
        s = s or self.fission_store.get(self.fission_id)
        for child in self.fission_versions.winfo_children(): child.destroy()
        ctk.CTkButton(self.fission_versions, text="母图 · 原始构图", anchor="w", height=27,
                      fg_color="#DDEEFF" if not s["selected"] else "transparent", text_color="#303035",
                      hover_color="#EAF4FF", command=lambda: self._fission_select_version(None)).pack(fill="x", pady=1)
        from desktop_tasks import LABELS
        for v in s["versions"]:
            job = self.fission_store.job_for(v)
            parent = next((f"V{x['number']}" for x in s["versions"] if x["id"] == v["parent"]), "母图")
            status = LABELS.get(job["status"], job["status"]) if job else "未提交"
            if job and job["status"] == "success": status = "已生成"
            text = f"V{v['number']} ← {parent} · {status}"
            ctk.CTkButton(self.fission_versions, text=text, anchor="w", height=27,
                          fg_color="#DDEEFF" if s["selected"] == v["id"] else "transparent", text_color="#303035",
                          hover_color="#EAF4FF", command=lambda i=v["id"]: self._fission_select_version(i)).pack(fill="x", pady=1)
        path, title, job = self._fission_selected(s)
        self.fission_image_title.configure(text=title)
        self.fission_use_button.configure(state="normal" if path else "disabled")
        self.fission_recover_button.configure(state="normal" if job else "disabled")
        if job and not self.fission_busy:
            self.fission_status.set(job.get("error") or job["stage"])
        self._fission_preview_path = path
        self._fission_draw_preview()

    def _fission_selected(self, s=None):
        s = s or self.fission_store.get(self.fission_id)
        if s["selected"]:
            v = next(v for v in s["versions"] if v["id"] == s["selected"])
            job = self.fission_store.job_for(v)
            result = job.get("result", {}) if job else {}
            path = result.get("asset_path") or result.get("output")
            return path if path and Path(path).is_file() else None, f"V{v['number']} · {MODES[v['mode']]}", job
        ref = next((r for r in s["references"] if r["role"] == "original"), {})
        return ref.get("path"), "母图 · 原始构图", None

    def _fission_select_version(self, identity):
        self._fission_save()
        if self.fission_store.get(self.fission_id)["selected"] == identity:
            return  # Clicking the selected picture must not discard ongoing feedback.
        self.fission_store.select(self.fission_id, identity)
        self._fission_render()
        self.fission_status.set("已恢复这版的参考与保留要求；请继续描述修改。")

    def _fission_mode_changed(self, _=None):
        if not self.fission_id:
            return
        self.fission_prompt.delete("1.0", "end")
        self._fission_save()
        self.fission_status.set("方式已切换，请重新整理本轮修改稿；聊天反馈仍保留。")

    def _fission_resize_preview(self, _=None):
        if self._fission_preview_timer:
            self.root.after_cancel(self._fission_preview_timer)
        self._fission_preview_timer = self.root.after(150, self._fission_draw_preview)

    def _fission_draw_preview(self):
        self._fission_preview_timer = None
        path = self._fission_preview_path
        if not path or not Path(path).is_file():
            self.fission_preview.configure(image=None, text="添加母图或等待本版图片完成")
            self.fission_preview._label.configure(image="")
            self._fission_preview_image = None
            return
        try:
            with Image.open(path) as source:
                thumb = ImageOps.exif_transpose(source).convert("RGB")
                thumb.thumbnail((max(100, self.fission_preview.winfo_width() - 8), max(100, self.fission_preview.winfo_height() - 8)))
            image = ctk.CTkImage(thumb, size=thumb.size)
            self.fission_preview.configure(image=image, text="")
            self._fission_preview_image = image
        except OSError:
            self.fission_preview.configure(image=None, text="图片文件暂不可读")
            self.fission_preview._label.configure(image="")

    def _fission_reference_dialog(self, role):
        if not self.fission_id or self.fission_busy:
            return
        self._fission_save()
        window = Toplevel(self.root, bg="#F5F5F7"); window.title(ROLES[role]); window.geometry("330x240")
        window.transient(self.root); window.grab_set()
        ref = next((r for r in self.fission_store.get(self.fission_id)["references"] if r["role"] == role), None)
        ctk.CTkLabel(window, text=ROLES[role] + ("（必填）" if role == "original" else "（可选）"), font=("Microsoft YaHei UI", 17, "bold")).pack(pady=14)
        def local():
            window.destroy()
            path = filedialog.askopenfilename(parent=self.root, title="选择" + ROLES[role], filetypes=[("图片", "*.jpg *.jpeg *.png *.webp")])
            if path: self._fission_load_reference(role, path)
        def library():
            window.destroy(); self._open_library("fission:" + role)
        ctk.CTkButton(window, text="选择本地图片", command=local).pack(pady=6)
        self._secondary_button(window, "从素材库选择", library, width=140).pack(pady=6)
        if ref:
            def remove():
                self.fission_store.reference(self.fission_id, role, None); window.destroy(); self._fission_render()
            self._secondary_button(window, "移除当前参考", remove, width=140).pack(pady=6)

    def _fission_load_reference(self, role, path):
        if self.fission_busy or not self.fission_id:
            return
        self._fission_save()
        sid = self.fission_id
        self._fission_set_busy(True, "检查并保存图片…")
        def work():
            info = inspect_image(path)
            asset = self.desktop_store.import_asset(path)
            saved = inspect_image(asset["path"])
            if info["sha256"] != saved["sha256"]: raise ValueError("导入期间图片发生变化，请重新选择。")
            return {**saved, "asset_id": asset["id"], "name": asset["name"]}
        self._fission_work("reference", (sid, role), work)

    def _fission_set_busy(self, busy, status=None):
        self.fission_busy = busy
        for b in (self.fission_chat_button, self.fission_generate_button):
            b.configure(state="disabled" if busy else "normal")
        if status: self.fission_status.set(status)

    def _fission_work(self, kind, context, work):
        def run():
            try: result, error = work(), None
            except Exception as exc: result, error = None, str(exc)
            self.events.put(("fission_event", (kind, context, result, error)))
        threading.Thread(target=run, daemon=True).start()

    def _fission_send(self):
        if self.fission_busy or not self.fission_id or getattr(self, "_maintenance_busy", False):
            return
        self._fission_save()
        try:
            s = self.fission_store.get(self.fission_id)
            credentials = self._credential_values()
            configuration(credentials)
            refs = image_inputs(s, self.fission_store)
            # Compare the bytes that will be sent with the retained reference evidence.
            for r in refs:
                if inspect_image(r["path"])["sha256"] != r["sha256"]: raise ValueError("参考文件已改变，请重新添加。")
            s = self.fission_store.begin_turn(s["id"], s["input"])
            self._fission_set_busy(True, "正在看图并整理这一轮修改…")
            self._fission_render()
            self._fission_work("chat", (s["id"], s["cursor"], s["revision"]), lambda: chat(s, refs, credentials))
        except Exception as exc:
            self.fission_status.set(str(exc))

    def _fission_quote(self):
        if self.fission_busy or self.fish_login_busy or not self.fission_id or getattr(self, "_maintenance_busy", False):
            return
        self._fission_save()
        try:
            s = self.fission_store.get(self.fission_id)
            refs = image_inputs(s, self.fission_store)
            if not s["prompt"].strip(): raise ValueError("先聊天整理修改要求，或直接填写本轮修改稿。")
            workspace = self.fish_workspaces.get(self.fish_workspace.get())
            if not workspace: raise ValueError("请到设置连接 Fish Audio 并选择工作区。")
            config = {"session_id": s["id"], "model": MODEL, "prompt": s["prompt"].strip(), "locks": s["locks"],
                      "mode": s["mode"], "image_inputs": refs, "parameters": copy.deepcopy(s["parameters"]),
                      "workspace_id": workspace, "workspace_name": self.fish_workspace.get(),
                      "output": str(Path(self.workspace_output_dir_var.get()).expanduser().resolve() / "图片裂变.png")}
            auth = self.fish_auth_path
            def work():
                with FishClient(auth) as client:
                    return prepare_quote(config, self.desktop_store, client)
            self._fission_set_busy(True, "准备参考并读取费用…")
            self._fission_work("quote", (s["id"], s["revision"], workspace), work)
        except Exception as exc:
            self.fission_status.set(str(exc))

    def _handle_fission_event(self, payload):
        kind, context, result, error = payload
        self._fission_save()
        self._fission_set_busy(False)
        sid = context[0]
        if kind == "chat":
            _, tid, revision = context
            current = self.fission_store.get(sid)
            apply = current["revision"] == revision and current["cursor"] == tid
            self.fission_store.finish_turn(sid, tid, result, error, expected=revision)
            if sid == self.fission_id:
                self._fission_render(drafts=apply)
                self.fission_status.set(error or ("修改稿已填入，可先调整，再预估生成。" if apply else "答复已保存在原分支；当前输入已改变，未覆盖修改稿。"))
            return
        if error:
            self.fission_status.set(error)
            return
        if kind == "reference":
            self.fission_store.reference(sid, context[1], result)
            if sid == self.fission_id:
                self._fission_render()
                self.fission_status.set("参考已保存；请描述本轮修改要求。")
            return
        if kind == "quote":
            _, revision, workspace = context
            if sid != self.fission_id or self.fission_store.get(sid)["revision"] != revision or self.fish_workspaces.get(self.fish_workspace.get()) != workspace:
                self.fission_status.set("预估期间会话或工作区已改变，请重新预估。")
                return
            quote, params = result["fish_quote"], result["parameters"]
            text = (f"Fish · Sunburst\n{result['workspace_name']}\n"
                    f"一张图片 · {params['aspect_ratio']} · {params['resolution']} · {QUALITY_LABELS[params['quality']]}\n\n"
                    f"本次费用：{quote['credits']:,} 积分\n可用积分：{quote.get('balance', '以平台为准')}\n\n确认生成这一版？")
            if not messagebox.askyesno("确认图片生成费用", text, parent=self.root):
                self.fission_status.set("已取消，未提交生图；修改稿和已上传参考保留。")
                return
            self._fission_save()
            if self.fission_store.get(sid)["revision"] != revision or self.fish_workspaces.get(self.fish_workspace.get()) != workspace:
                self.fission_status.set("会话已改变，请重新预估。")
                return
            result["cost_approved"] = True
            self._fission_enqueue(sid, revision, result)

    def _fission_enqueue(self, sid, revision, config):
        version = None
        try:
            s = self.fission_store.reserve(sid, config, revision)
            version = s["versions"][-1]
            config = {**config, "version_id": version["id"]}
            identity = self.tasks.submit("image_fission", config, self._credential_values(), request_id=version["request_id"])
            self.fission_store.attach_job(sid, version["id"], identity)
            self.fission_store.select(sid, version["id"])
            self._fission_render()
            self.fission_status.set("已加入生成队列，本页会更新图片；也可在使用记录查看。")
        except Exception as exc:
            # submit can have committed before a local callback failed. Resolve its
            # durable request ID before allowing another generation of this draft.
            if version:
                job = self.fission_store.job_for(version)
                if job:
                    self.fission_store.attach_job(sid, version["id"], job["id"])
                    self.fission_store.select(sid, version["id"])
                    self._fission_render()
                    self.fission_status.set("已找回本次任务，继续显示原任务进度。")
                    return
            self.fission_status.set("本地提交需要核对：" + str(exc))
            self._fission_render_versions()

    def _fission_jobs_changed(self):
        if not self.fission_id or self.current_page != "image_fission":
            return
        s = self.fission_store.get(self.fission_id)
        signature = [(v["id"], (self.fission_store.job_for(v) or {}).get("updated")) for v in s["versions"]]
        if signature != self._fission_job_signature:
            self._fission_job_signature = signature
            self._fission_render_versions(s)

    def _fission_settings(self):
        if not self.fission_id:
            return
        self._fission_save()
        sid = self.fission_id
        s = self.fission_store.get(sid)
        window = Toplevel(self.root, bg="#F5F5F7"); window.title("保留要求与输出参数"); window.geometry("530x500")
        window.transient(self.root); window.grab_set()
        ctk.CTkLabel(window, text="明确要保留的内容", font=("Microsoft YaHei UI", 16, "bold")).pack(anchor="w", padx=20, pady=(18, 6))
        ctk.CTkLabel(window, text="例如：人物、发型、妆容和构图已满意，只调整产品。\n每轮都会带上这些要求；模型不能保证像素级不变。", justify="left", text_color="#6E6E73").pack(anchor="w", padx=20)
        locks = ctk.CTkTextbox(window, height=135, wrap="word"); locks.pack(fill="x", padx=20, pady=12); locks.insert("1.0", s["locks"])
        variables = []
        for title, value, options in (("比例", s["parameters"]["aspect_ratio"], ["跟随底图", *RATIOS]),
                                      ("清晰度", s["parameters"]["resolution"], ["1K", "2K", "4K"]),
                                      ("质量", QUALITY_LABELS[s["parameters"]["quality"]], list(QUALITY_LABELS.values()))):
            row = ctk.CTkFrame(window, fg_color="transparent"); row.pack(fill="x", padx=20, pady=5)
            ctk.CTkLabel(row, text=title, width=90, anchor="w").pack(side="left")
            var = StringVar(value=value); variables.append(var)
            ctk.CTkOptionMenu(row, variable=var, values=options, width=180).pack(side="left")
        ctk.CTkLabel(window, text="跟随底图会选择最接近的可用比例，生成前显示实际规格。", text_color="#6E6E73").pack(pady=6)
        def save():
            text = locks.get("1.0", "end-1c").strip()
            if len(text) > 12000:
                messagebox.showerror("要求过长", "保留要求最多 12000 字符。", parent=window); return
            self._fission_save()
            parameters = dict(zip(("aspect_ratio", "resolution", "quality"), [v.get() for v in variables]))
            parameters["quality"] = next(k for k, v in QUALITY_LABELS.items() if v == parameters["quality"])
            self.fission_store.fields(sid, locks=text, parameters=parameters)
            window.destroy(); self._fission_render()
        ctk.CTkButton(window, text="保存", command=save).pack(pady=16)

    def _fission_compare(self):
        if not self.fission_id:
            return
        s = self.fission_store.get(self.fission_id)
        choices = {ROLES[r["role"]]: r["path"] for r in s["references"] if Path(r["path"]).is_file()}
        for v in s["versions"]:
            job = self.fission_store.job_for(v)
            result = job.get("result", {}) if job else {}
            path = result.get("asset_path") or result.get("output")
            if path and Path(path).is_file(): choices[f"V{v['number']}"] = path
        if not choices:
            return
        path, _, _ = self._fission_selected(s)
        selected = next((label for label, value in choices.items() if value == path), next(iter(choices)))
        from fission_viewer import open_comparison
        return open_comparison(self.root, choices, selected)

    def _fission_history(self):
        if not self.fission_id:
            return
        self._fission_save()
        session = self.fission_store.get(self.fission_id)
        names = {v["id"]: f"V{v['number']}" for v in session["versions"]}
        window = Toplevel(self.root, bg="#F5F5F7")
        window.title("全部会话记录 · " + session["title"]); window.geometry("750x600")
        window.transient(self.root)
        ctk.CTkLabel(window, text="包含各分支和未应用的答复；可复制历史修改稿。", text_color="#6E6E73").pack(pady=12)
        box = ctk.CTkTextbox(window, wrap="word"); box.pack(fill="both", expand=True, padx=14, pady=(0, 14))
        for i, turn in enumerate(session["turns"], 1):
            text = (f"第 {i} 轮 · 基于 {names.get(turn.get('base_version'), '母图')}\n"
                    f"你：{turn['user']}\n\n助手：{turn.get('reply') or turn.get('error') or '此轮未完成'}\n")
            if turn.get("prompt"): text += "\n当时的修改稿：\n" + turn["prompt"] + "\n"
            box.insert("end", text + "\n" + "─" * 30 + "\n\n")
        if not session["turns"]: box.insert("end", "此会话还没有聊天记录。")
        box.configure(state="disabled")
        return window

    def _fission_export(self):
        if not self.fission_id:
            return
        path, title, _ = self._fission_selected()
        if not path:
            return
        suffix = Path(path).suffix
        dest = filedialog.asksaveasfilename(parent=self.root, title="另存图片", initialfile=title.split(" · ")[0] + suffix,
                                          defaultextension=suffix, filetypes=[("图片", "*" + suffix)])
        if dest:
            try:
                if Path(dest).resolve() != Path(path).resolve(): shutil.copy2(path, dest)
                self.fission_status.set("图片已另存。")
            except OSError as exc: messagebox.showerror("另存失败", str(exc), parent=self.root)

    def _fission_use(self):
        if not self.fission_id:
            return
        path, _, _ = self._fission_selected()
        if path:
            if self.fish_busy or self.director_busy:
                self.fission_status.set("请等待图音页面当前操作结束后回填。")
                return
            self._fission_save()
            self._fish_load("first_frame", path)
            self._show_page("image_audio")

    def _fission_recover(self):
        if not self.fission_id:
            return
        _, _, job = self._fission_selected()
        if not job:
            return
        if job["status"] in {"attention", "paused"}:
            try: self.tasks.recover(job["id"], self._credential_values())
            except Exception as exc: self.fission_status.set(str(exc))
        else:
            self._show_page("history")
            self.history_tree.selection_set(job["id"])
            self._task_detail()

    def _fission_open_job(self, config):
        sid = config.get("session_id")
        if sid and any(s["id"] == sid for s in self.fission_store.sessions()):
            self._fission_select_session(sid)
            version = config.get("version_id")
            if version: self._fission_select_version(version)
            self._show_page("image_fission")
        else:
            messagebox.showinfo("会话未找到", "此历史图片仍可查看和下载；新建会话并把图片作为母图即可继续。", parent=self.root)
