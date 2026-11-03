"""Voice library, cloning and fixed Drama 3 synthesis in the desktop app."""
from __future__ import annotations

import copy
import hashlib
import threading
import time
import uuid
from pathlib import Path
from tkinter import BooleanVar, DoubleVar, StringVar, filedialog, messagebox

import customtkinter as ctk

from fish_mcp import FishAuth, FishClient
from voice_service import create_clone, download_sample, list_voices, prepare_speech, verify_clone
from voice_batch import run_batch, latest_batch, resume_job, can_resume
from voice_layout import VoiceLayout


class VoicePage(VoiceLayout):
    def _init_voice(self):
        self.voice_view = StringVar(value="我的声音")
        self.voice_query = StringVar()
        self.voice_selection = StringVar(value="请选择音色")
        self.voice_speed = DoubleVar(value=1.0)
        self.voice_volume = DoubleVar(value=0.0)
        self.voice_status = StringVar(value="连接 Fish Audio 后刷新，即可查看已有音色。")
        self.voice_page_note = StringVar(value="第 1 页")
        self.voice_rows, self.voice_choices = [], {}
        self.voice_page_number = 1
        self.voice_has_next = False
        self.voice_busy = self.voice_list_busy = self.voice_clone_busy = False
        self.voice_pending_config = None
        self.voice_submit_id = uuid.uuid4().hex
        self.voice_not_before = 0
        self.voice_connection_id = None
        self.voice_loaded = False
        self.voice_list_token = ""
        self.voice_sample_token = ""
        self.voice_clone_dialog = None
        self.voice_result_jobs = []
        self.voice_playing_result = None
        self.voice_stop_requested = threading.Event()
        self.voice_active_request = None


    def _voice_reset_controls(self):
        self.voice_speed.set(1.)
        self.voice_volume.set(0.)


    def _voice_on_show(self):
        if not self.demo and not self.voice_loaded and not self.voice_list_busy:
            self._voice_refresh(1)

    def _voice_connection(self):
        if self.fish_login_busy:
            raise ValueError("请先完成 Fish Audio 账户连接。")
        saved = FishAuth(self.fish_auth_path).load()
        if not saved.get("access_token") or not saved.get("connection_id"):
            raise ValueError("请先到「设置」连接 Fish Audio 账户。")
        return saved["connection_id"]

    def _voice_reset_account(self):
        self.voice_connection_id = None
        self.voice_pending_config = None
        self.voice_active_request = None
        self.voice_submit_id = uuid.uuid4().hex
        self.voice_loaded = False
        self.voice_list_token = uuid.uuid4().hex
        self.voice_sample_token = uuid.uuid4().hex
        self.voice_list_busy = False
        self.voice_rows, self.voice_choices = [], {}
        self.voice_selection.set("请选择音色")
        self.voice_menu.configure(values=["请选择音色"])
        self.voice_submit_button.configure(state="normal", text="生成音频")
        self.voice_result_jobs = []
        self.voice_playing_result = None
        self.voice_player.stop()
        self._voice_render_rows()
        self._voice_update_selection_detail()
        self._voice_render_results()
        self.voice_previous.configure(state="disabled")
        self.voice_next.configure(state="disabled")
        self.voice_status.set("账户已切换，请刷新音色列表。")
        if self.voice_clone_dialog and self.voice_clone_dialog.winfo_exists():
            self.voice_clone_dialog.destroy()
            self.voice_clone_dialog = None

    def _voice_worker(self, kind, action, *metadata):
        def run():
            try:
                result, error = action(), None
            except Exception as exc:
                result, error = None, str(exc)
            self.events.put(("voice_event", (kind, metadata, result, error)))
        threading.Thread(target=run, daemon=True).start()

    def _voice_refresh(self, page=1):
        if self.voice_list_busy or self.fish_login_busy:
            return
        try:
            connection = self._voice_connection()
        except Exception as exc:
            self.voice_status.set(str(exc))
            return
        if connection != self.voice_connection_id:
            self._voice_reset_account()
            self.voice_connection_id = connection
            self._voice_render_results(latest_batch(self.desktop_store, connection))
        page, query = max(1, page), self.voice_query.get().strip()
        self.voice_list_busy = True
        token = self.voice_list_token = uuid.uuid4().hex
        self.voice_status.set("正在读取我的音色…")
        def action():
            with FishClient(self.fish_auth_path, connection_id=connection) as client:
                return list_voices(client, page=page, query=query)
        self._voice_worker("list", action, token, page)


    def _voice_label(self, voice):
        title = voice.get("title") or "未命名音色"
        same = self.voice_choices.get(title)
        return title if not same or same["id"] == voice["id"] else title + " · " + voice["id"][-6:]

    def _voice_store_choice(self, voice):
        selected_id = self.voice_choices.get(self.voice_selection.get(), {}).get("id")
        for label, previous in list(self.voice_choices.items()):
            if previous["id"] == voice["id"]:
                del self.voice_choices[label]
        label = self._voice_label(voice)
        self.voice_choices[label] = voice
        if selected_id == voice["id"]:
            self.voice_selection.set(label)
        return label

    def _voice_select(self, voice):
        label = self._voice_store_choice(voice)
        self.voice_menu.configure(values=list(self.voice_choices))
        self.voice_selection.set(label)
        self._voice_update_selection_detail()
        self._voice_choose_tab("声音合成")

    def _voice_preview(self, voice):
        token = self.voice_sample_token = uuid.uuid4().hex
        cache_key = hashlib.sha256(f"{self.voice_connection_id}|{voice['id']}|{voice['sample_url']}".encode()).hexdigest()
        destination = self.desktop_store.root / "voice-previews" / (cache_key + ".mp3")
        self.voice_playing_result = None
        self.voice_player.stop()
        self.voice_status.set("正在准备音色试听…")
        self._voice_worker("sample", lambda: download_sample(voice["sample_url"], destination), token)

    def _voice_stop_preview(self):
        self.voice_sample_token = uuid.uuid4().hex
        self.voice_playing_result = None
        self.voice_player.stop()
        self.voice_status.set("试听已停止。")

    def _voice_form(self):
        selected = self.voice_choices.get(self.voice_selection.get(), {})
        return {"text": self.voice_text.get("1.0", "end-1c").strip(), "voice_id": selected.get("id", ""),
                "voice_name": selected.get("title", ""), "voice_description": selected.get("description", ""), "model": "drama-3",
                "speed": 1., "volume_db": 0.,
                "audio_settings_mode": "fish"}

    def _voice_submit(self):
        if self.voice_busy or self.fish_login_busy or time.monotonic() < self.voice_not_before:
            return
        if self.voice_pending_config:
            self._voice_enqueue(self.voice_pending_config)
            return
        config = self._voice_form()
        try:
            connection = self._voice_connection()
            if connection != self.voice_connection_id:
                self._voice_reset_account()
                raise ValueError("账户已切换，请刷新并重新选择音色。")
            if not config["voice_id"]:
                raise ValueError("请先在「我的声音」选择音色。")
            if not config["text"] or len(config["text"]) > 2000:
                raise ValueError("请填写 1–2000 字符的文案。")
        except Exception as exc:
            messagebox.showerror("无法合成", str(exc), parent=self.root)
            return
        self.voice_busy = True
        self.voice_submit_button.configure(state="disabled", text="正在预估积分…")
        snapshot = copy.deepcopy(config)
        config["output"] = str(Path(self.workspace_output_dir_var.get()).expanduser().resolve() / "声音合成.wav")
        def action():
            with FishClient(self.fish_auth_path, connection_id=connection) as client:
                return prepare_speech(config, client)
        self._voice_worker("quote", action, snapshot)

    def _voice_enqueue(self, config):
        self.voice_pending_config = copy.deepcopy(config)
        self.voice_busy = True
        self.voice_active_request = request_id = self.voice_submit_id
        self.voice_stop_requested.clear()
        self._voice_stop_preview()
        self.voice_submit_button.configure(state="disabled", text="正在生成音频…")
        self.voice_status.set("正在生成一条音频，完成后可以直接试听。")
        self._voice_render_results([])
        credentials = self._credential_values()
        frozen = copy.deepcopy(config)
        def progress(jobs):
            self.events.put(("voice_event", ("batch_progress", (request_id,), jobs, None)))
        def action():
            return run_batch(frozen, self.desktop_store, credentials, progress, request_id=request_id,
                             stop_requested=self.voice_stop_requested.is_set)
        self._voice_worker("batch_done", action, request_id)

    def _voice_result(self, index):
        return next((job for job in self.voice_result_jobs if job.get("variant_index") == index + 1), {})

    def _voice_playback_state(self, playing, paused):
        if not playing:
            self.voice_playing_result = None
        if hasattr(self, "voice_result_widgets"):
            self._voice_render_results()

    def _voice_play_result(self, index):
        job = self._voice_result(index)
        path = job.get("result", {}).get("output")
        if not path or not Path(path).is_file():
            self.voice_status.set("音频尚未就绪，请等待完成或继续获取结果。")
            return
        self.voice_sample_token = uuid.uuid4().hex
        try:
            if self.voice_playing_result == index and self.voice_player.path == Path(path):
                self.voice_player.toggle()
            else:
                self.voice_player.load(Path(path))
                self.voice_player.start()
            self.voice_playing_result = index if self.voice_player.process is not None else None
            self._voice_render_results()
        except (OSError, ValueError):
            self.voice_status.set("无法播放音频，请检查播放组件后重试。")

    def _voice_save_result(self, index):
        path = self._voice_result(index).get("result", {}).get("output")
        if path and Path(path).is_file():
            self._save_task_result(path)

    def _voice_import_result(self, index):
        path = self._voice_result(index).get("result", {}).get("output")
        if path and Path(path).is_file():
            try:
                self.desktop_store.import_asset(path)
                self.voice_status.set("音频已加入素材库。")
            except (OSError, ValueError) as exc:
                self.voice_status.set(str(exc))

    def _voice_resume_result(self, index):
        identity = self._voice_result(index).get("id")
        if identity:
            self._voice_resume_job_id(identity)

    def _voice_resume_job_id(self, identity):
        if self.voice_busy or self.fish_login_busy:
            return
        job = self.desktop_store.job(identity)
        if not can_resume(job):
            self.voice_status.set("此版本尚无可继续获取的结果，不会再次提交合成。")
            return
        self._show_page("voice")
        self._voice_choose_tab("声音合成")
        self.voice_busy = True
        request_id = self.voice_active_request = uuid.uuid4().hex
        self.voice_submit_button.configure(state="disabled", text="正在获取原结果…")
        credentials = self._credential_values()
        def progress(jobs):
            self.events.put(("voice_event", ("batch_progress", (request_id,), jobs, None)))
        self._voice_worker("batch_done", lambda: resume_job(identity, self.desktop_store, credentials, progress), request_id)

    def _voice_reuse(self, config):
        if self.voice_busy or self.voice_pending_config:
            messagebox.showinfo("请先完成提交", "请先完成当前声音提交。", parent=self.root)
            return
        self.voice_text.delete("1.0", "end")
        self.voice_text.insert("1.0", config.get("text", ""))
        self._voice_reset_controls()
        self._show_page("voice")
        self._voice_select(next((v for v in self.voice_choices.values() if v["id"] == config["voice_id"]),
            {"id": config["voice_id"], "title": config.get("voice_name", "历史音色"), "description": config.get("voice_description", "")}))
        self._voice_update_char_count()
        self.voice_status.set("已复制文案和音色；新任务生成一条音频，使用原速、原音量，生成前重新确认费用。")

    def _voice_open_clone(self):
        if self.voice_clone_dialog and self.voice_clone_dialog.winfo_exists():
            self.voice_clone_dialog.lift()
            return
        try:
            self.voice_clone_connection = self._voice_connection()
        except Exception as exc:
            messagebox.showerror("尚未连接", str(exc), parent=self.root)
            return
        dialog = self.voice_clone_dialog = ctk.CTkToplevel(self.root)
        dialog.title("创建新音色")
        dialog.geometry("600x670")
        dialog.minsize(540, 560)
        dialog.transient(self.root)
        dialog.protocol("WM_DELETE_WINDOW", self._voice_close_clone)
        body = ctk.CTkScrollableFrame(dialog, fg_color="#F5F5F7")
        body.pack(fill="both", expand=True, padx=18, pady=18)
        ctk.CTkLabel(body, text="创建新音色", font=("Microsoft YaHei UI", 23, "bold")).pack(anchor="w")
        ctk.CTkLabel(body, text="上传同一人的 1–5 段清晰录音，建议合计 10–60 秒，最长 270 秒。", wraplength=500, justify="left", text_color="#6E6E73").pack(anchor="w", pady=(6, 16))
        self.voice_clone_title = StringVar()
        self.voice_clone_rights = BooleanVar(value=False)
        self.voice_clone_files = []
        self.voice_clone_preflight = None
        self.voice_clone_phase = "samples"
        self.voice_clone_info = StringVar(value="尚未选择录音")
        ctk.CTkEntry(body, textvariable=self.voice_clone_title, placeholder_text="音色名称", height=38).pack(fill="x", pady=(0, 12))
        self.voice_clone_pick = self._secondary_button(body, "选择录音", self._voice_pick_clone, width=120)
        self.voice_clone_pick.pack(anchor="w")
        ctk.CTkLabel(body, textvariable=self.voice_clone_info, wraplength=500, justify="left").pack(anchor="w", pady=(8, 12))
        self.voice_clone_rights_check = ctk.CTkCheckBox(body, text="这是我的声音，或我已获得说话人的克隆授权", variable=self.voice_clone_rights)
        self.voice_clone_rights_check.pack(anchor="w", pady=(4, 14))
        ctk.CTkLabel(body, text="音色默认设为私有。创建免费，录音将上传至 Fish Audio。", text_color="#6E6E73", wraplength=500, justify="left").pack(anchor="w", pady=(0, 12))
        self.voice_clone_submit = ctk.CTkButton(body, text="创建音色", command=self._voice_create_clone, height=42, fg_color="#0071E3")
        self.voice_clone_submit.pack(fill="x")
        self.voice_clone_note = StringVar(value="")
        ctk.CTkLabel(body, textvariable=self.voice_clone_note, wraplength=500, justify="left").pack(anchor="w", pady=12)
        self.voice_verification = ctk.CTkFrame(body, fg_color="transparent")
        self.voice_verify_prompt = ctk.CTkTextbox(self.voice_verification, height=95, wrap="word")
        self.voice_verify_prompt.pack(fill="x", pady=(0, 8))
        self._secondary_button(self.voice_verification, "上传本人验证录音", self._voice_verify_recording, width=180).pack(anchor="w")
        self._secondary_button(self.voice_verification, "刷新验证要求", self._voice_refresh_verification, width=140).pack(anchor="w", pady=(8, 0))
        self._secondary_button(body, "关闭", self._voice_close_clone, width=80).pack(anchor="e", pady=(14, 0))

    def _voice_close_clone(self):
        if self.voice_clone_busy:
            self.voice_clone_note.set("正在处理，请等待结果后关闭。")
            return
        if self.voice_clone_dialog:
            self.voice_clone_dialog.destroy()
            self.voice_clone_dialog = None

    def _voice_pick_clone(self):
        if self.voice_clone_busy:
            return
        paths = filedialog.askopenfilenames(parent=self.voice_clone_dialog, title="选择同一人的录音", filetypes=[("音频", "*.mp3 *.wav *.m4a *.flac *.ogg *.aac"), ("所有文件", "*.*")])
        if not paths:
            return
        if len(paths) > 5:
            messagebox.showerror("录音数量过多", "每次最多选择 5 段录音。", parent=self.voice_clone_dialog)
            return
        self.voice_clone_files = list(paths)
        self.voice_clone_preflight = None
        self.voice_clone_phase = "samples"
        self.voice_clone_info.set("\n".join(Path(p).name for p in paths))
        self.voice_verification.pack_forget()
        self.voice_clone_submit.configure(text="创建音色", state="normal")

    def _voice_create_clone(self, proceed=False):
        if self.voice_clone_busy or self.voice_clone_phase == "verification":
            return
        if not self.voice_clone_rights.get():
            messagebox.showerror("请确认声音授权", "请确认这是你的声音或你已获得说话人的克隆授权。", parent=self.voice_clone_dialog)
            return
        title = self.voice_clone_title.get().strip()
        if not title or not self.voice_clone_files:
            messagebox.showerror("信息不完整", "请填写音色名称并选择录音。", parent=self.voice_clone_dialog)
            return
        self.voice_clone_busy = True
        self.voice_clone_submit.configure(state="disabled")
        self.voice_clone_note.set("正在检查录音并创建音色…")
        paths = list(self.voice_clone_files)
        preflight = self.voice_clone_preflight if proceed else None
        connection = self.voice_clone_connection
        def action():
            with FishClient(self.fish_auth_path, connection_id=connection) as client:
                return create_clone(client, paths, title, True, preflight_id=preflight, proceed_despite_warnings=proceed)
        self._voice_worker("clone", action)

    def _voice_verify_recording(self):
        if self.voice_clone_busy or not self.voice_clone_preflight:
            return
        selected = filedialog.askopenfilename(parent=self.voice_clone_dialog, title="选择本人朗读验证句的录音", filetypes=[("音频", "*.mp3 *.wav *.m4a *.flac *.ogg *.aac")])
        if not selected:
            return
        self.voice_clone_busy = True
        self.voice_clone_note.set("正在验证本人录音…")
        connection, preflight = self.voice_clone_connection, self.voice_clone_preflight
        title = self.voice_clone_title.get().strip()
        def action():
            with FishClient(self.fish_auth_path, connection_id=connection) as client:
                return verify_clone(client, preflight, selected, title=title)
        self._voice_worker("clone", action)

    def _voice_refresh_verification(self):
        if self.voice_clone_busy or not self.voice_clone_preflight:
            return
        self.voice_clone_busy = True
        self.voice_clone_note.set("正在读取验证要求…")
        connection, preflight = self.voice_clone_connection, self.voice_clone_preflight
        title = self.voice_clone_title.get().strip()
        def action():
            with FishClient(self.fish_auth_path, connection_id=connection) as client:
                return verify_clone(client, preflight, title=title)
        self._voice_worker("clone", action)

    def _handle_voice_event(self, payload):
        kind, metadata, result, error = payload
        if kind == "list":
            token, page = metadata
            if token != self.voice_list_token:
                return
            self.voice_list_busy = False
            if error:
                self.voice_status.set(error)
                return
            self.voice_loaded = True
            self.voice_rows = result.get("voices", [])
            self.voice_page_number = page
            self.voice_has_next = result.get("has_next", False)
            for voice in self.voice_rows:
                self._voice_store_choice(voice)
            self.voice_menu.configure(values=list(self.voice_choices) or ["请选择音色"])
            self._voice_update_selection_detail()
            self.voice_previous.configure(state="normal" if page > 1 else "disabled")
            self.voice_next.configure(state="normal" if self.voice_has_next else "disabled")
            self.voice_page_note.set(f"第 {page} 页")
            self._voice_render_rows()
            self.voice_status.set(f"本页 {len(self.voice_rows)} 个音色。" if self.voice_rows else "没有找到音色，可更换关键词或创建新音色。")
        elif kind == "sample":
            if metadata[0] != self.voice_sample_token:
                return
            if error:
                self.voice_status.set(error)
                return
            try:
                self.voice_player.load(Path(result))
                self.voice_player.start()
            except (OSError, ValueError):
                self.voice_status.set("无法播放样音，请检查音频播放组件后重试。")
            else:
                self.voice_status.set("正在试听音色样音。")
        elif kind == "quote":
            accepted = False
            try:
                if error:
                    self.voice_status.set(error)
                    messagebox.showerror("无法合成", error, parent=self.root)
                    return
                if self._voice_form() != metadata[0]:
                    self.voice_status.set("文案或参数已改变，请重新预估。")
                    return
                quote = result["fish_quote"]
                message = (f"音色：{result['voice_name']}\n模型：Drama 3\n生成一条音频 · 原速、原音量\n\n"
                           f"本次费用：{quote['credits']:,} 积分\n确认生成？")
                if messagebox.askyesno("确认合成费用", message, parent=self.root):
                    result["cost_approved"] = True
                    self._voice_enqueue(result)
                    accepted = True
                else:
                    self.voice_status.set("已取消合成，未扣费。")
            finally:
                if not accepted:
                    self.voice_busy = False
                    self.voice_submit_button.configure(state="normal", text="生成音频")
        elif kind in {"batch_progress", "batch_done"}:
            if metadata[0] != self.voice_active_request:
                return
            if result is not None:
                self._voice_render_results(result)
            self._refresh_history()
            if kind == "batch_done":
                self.voice_busy = False
                self.voice_active_request = None
                if error:
                    self.voice_status.set(error)
                    self.voice_submit_button.configure(state="normal", text="核对本次结果" if self.voice_pending_config else "生成音频")
                else:
                    self.voice_pending_config = None
                    self.voice_submit_id = uuid.uuid4().hex
                    self.voice_not_before = time.monotonic() + 1
                    completed = sum(job.get("result", {}).get("local_downloaded") is True for job in (result or []))
                    total = len(result or [])
                    self.voice_status.set("音频已就绪，可以直接试听。" if completed == total and total else
                                          f"已完成 {completed} / {total} 条音频；未完成的原因显示在结果卡片中。")
                    self.voice_submit_button.configure(state="normal", text="再生成一条")
                self._voice_render_results()
        elif kind == "clone":
            self.voice_clone_busy = False
            if not self.voice_clone_dialog or not self.voice_clone_dialog.winfo_exists():
                return
            self.voice_clone_submit.configure(state="disabled" if self.voice_clone_phase == "verification" else "normal")
            if error:
                self.voice_clone_note.set(error + "\n若创建结果不明确，请先刷新我的声音核实。")
                return
            status = result.get("status")
            self.voice_clone_preflight = result.get("preflight_id") or self.voice_clone_preflight
            if status == "created" and result.get("voice_id"):
                self._voice_close_clone()
                self.voice_status.set("新音色已创建，正在刷新列表。")
                self._voice_refresh(1)
            elif status == "quality_warning":
                self.voice_clone_phase = "quality"
                note = result.get("instructions") or str(result.get("warnings", "录音质量可能影响克隆效果。"))
                self.voice_clone_note.set(note)
                if self.voice_clone_preflight and messagebox.askyesno("录音质量提示", note + "\n\n仍要使用这些录音继续创建？", parent=self.voice_clone_dialog):
                    self._voice_create_clone(proceed=True)
            elif status == "verification_required":
                self.voice_clone_phase = "verification"
                self.voice_clone_note.set(result.get("instructions") or "请让声音本人朗读下面的验证句，并上传录音。")
                self.voice_clone_submit.configure(state="disabled")
                self.voice_verify_prompt.configure(state="normal")
                self.voice_verify_prompt.delete("1.0", "end")
                self.voice_verify_prompt.insert("1.0", result.get("verification_prompt") or "尚未取得验证句，请点击「刷新验证要求」。")
                self.voice_verify_prompt.configure(state="disabled")
                self.voice_verification.pack(fill="x", pady=8)
            else:
                self.voice_clone_note.set(result.get("instructions") or "尚未完成创建，请检查 Fish Audio 的音色状态。")
