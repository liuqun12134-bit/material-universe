"""Paged thumbnail browser; media work stays off Tk's thread."""
import queue
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import customtkinter as ctk


class AssetGrid(ctk.CTkFrame):
    PAGE_SIZE = 24

    def __init__(self, parent, store, selected, preview):
        super().__init__(parent, fg_color="transparent")
        self.store, self.on_selected, self.on_preview = store, selected, preview
        self.rows, self.selected, self.cards, self.images = {}, set(), {}, {}
        self.page, self.signature = 0, None
        self.results, self.pending = queue.Queue(), set()
        self.pool = ThreadPoolExecutor(max_workers=2)
        self.closed = False
        self.body = ctk.CTkScrollableFrame(self, fg_color="transparent")
        self.body.pack(fill="both", expand=True)
        self.body.grid_columnconfigure((0, 1, 2), weight=1, uniform="assets")
        bar = ctk.CTkFrame(self, fg_color="transparent")
        bar.pack(fill="x", pady=5)
        self.previous = ctk.CTkButton(bar, text="上一页", width=68, command=lambda: self.turn(-1), fg_color="#EDEDF0", text_color="#303035")
        self.previous.pack(side="left")
        self.page_label = ctk.CTkLabel(bar, text="")
        self.page_label.pack(side="left", padx=12)
        self.next = ctk.CTkButton(bar, text="下一页", width=68, command=lambda: self.turn(1), fg_color="#EDEDF0", text_color="#303035")
        self.next.pack(side="left")
        self.after(100, self._drain)

    def set_rows(self, rows):
        signature = tuple((identity, tuple(values)) for identity, values in rows)
        if signature == self.signature:
            return
        self.signature = signature
        self.rows = dict(rows)
        self.selected.intersection_update(self.rows)
        self.page = min(self.page, max(0, (len(rows) - 1) // self.PAGE_SIZE))
        self.render()

    def turn(self, delta):
        self.page = max(0, min(self.page + delta, max(0, (len(self.rows) - 1) // self.PAGE_SIZE)))
        self.render()

    def render(self):
        for widget in self.body.winfo_children():
            widget.destroy()
        self.cards.clear()
        assets = {a["id"]: a for a in self.store.assets()}
        visible = list(self.rows.items())[self.page * self.PAGE_SIZE:(self.page + 1) * self.PAGE_SIZE]
        if not visible:
            ctk.CTkLabel(self.body, text="暂无匹配素材，导入文件或调整筛选。", text_color="#86868B").grid(row=0, column=0, columnspan=3, pady=65)
        for index, (identity, values) in enumerate(visible):
            card = ctk.CTkFrame(self.body, fg_color="white", border_width=2, corner_radius=12)
            card.grid(row=index // 3, column=index % 3, sticky="nsew", padx=5, pady=5)
            kind = assets[identity]["kind"]
            thumb = ctk.CTkLabel(card, text={"image": "图片", "video": "▶ 视频", "audio": "♫ 音频"}.get(kind, "素材"), height=112, text_color="#9CA3AF")
            thumb.pack(fill="x", padx=7, pady=(8, 3))
            title = ctk.CTkLabel(card, text=values[0][:24] + ("…" if len(values[0]) > 24 else ""), anchor="w", wraplength=160)
            title.pack(fill="x", padx=12)
            meta = ctk.CTkLabel(card, text=f"{values[2]} · {values[3]}", anchor="w", text_color="#86868B", font=("Microsoft YaHei UI", 11))
            meta.pack(fill="x", padx=12, pady=(0, 8))
            self.cards[identity] = (card, thumb, meta)
            for widget in (card, thumb, title, meta):
                widget.bind("<Button-1>", lambda event, key=identity: self.choose(key, event.state & 0x0004))
                widget.bind("<Double-Button-1>", lambda event, key=identity: self.open(key))
            if identity in self.images:
                self._show_thumb(identity)
            elif identity not in self.pending:
                self.pending.add(identity)
                self.pool.submit(self._thumbnail, assets[identity])
        self._paint()
        pages = max(1, (len(self.rows) + self.PAGE_SIZE - 1) // self.PAGE_SIZE)
        self.page_label.configure(text=f"{self.page + 1} / {pages} · Ctrl 点击多选")
        self.previous.configure(state="normal" if self.page else "disabled")
        self.next.configure(state="normal" if self.page + 1 < pages else "disabled")

    def _thumbnail(self, asset):
        image, seconds = None, None
        try:
            from media_inspection import image_thumbnail, video_poster, probe_audio_duration
            path = Path(asset["path"])
            if asset["kind"] == "image":
                image = image_thumbnail(path, (170, 112))
            elif asset["kind"] == "video":
                image = video_poster(path, (170, 112))
            else:
                seconds = probe_audio_duration(path)
        except Exception:
            pass
        self.results.put((asset["id"], image, seconds))

    def _drain(self):
        if self.closed:
            return
        try:
            while True:
                identity, image, seconds = self.results.get_nowait()
                self.pending.discard(identity)
                self.images[identity] = (ctk.CTkImage(image, image, size=image.size) if image else None, seconds)
                self._show_thumb(identity)
        except queue.Empty:
            pass
        self.after(100, self._drain)

    def _show_thumb(self, identity):
        if identity not in self.cards:
            return
        image, seconds = self.images[identity]
        _, thumb, meta = self.cards[identity]
        if image:
            thumb.configure(image=image, text="")
        if seconds is not None:
            meta.configure(text=f"音频 · {int(seconds)//60}:{int(seconds)%60:02d}")

    def _paint(self):
        for key, (card, _, _) in self.cards.items():
            card.configure(border_color="#0071E3" if key in self.selected else "#E8E8ED")

    def choose(self, identity, multiple=False):
        if multiple:
            self.selected.symmetric_difference_update({identity})
        else:
            self.selected = {identity}
        self._paint()
        self.on_selected()

    def open(self, identity):
        self.choose(identity)
        self.on_preview()

    def selection(self):
        return tuple(key for key in self.rows if key in self.selected)

    def selection_set(self, *identities):
        self.selected = set(identities) & self.rows.keys()
        self._paint()
        self.on_selected()

    def selection_remove(self, *identities):
        self.selected.difference_update(identities)
        self._paint()

    def exists(self, identity):
        return identity in self.rows

    def destroy(self):
        self.closed = True
        self.pool.shutdown(wait=False, cancel_futures=True)
        super().destroy()
