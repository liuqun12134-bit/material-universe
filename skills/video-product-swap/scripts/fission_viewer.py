"""Local image comparison with independent pan and shared zoom; no image mutation."""
from tkinter import Canvas, Toplevel, StringVar

import customtkinter as ctk
from PIL import Image, ImageOps, ImageTk


def open_comparison(root, choices, selected):
    window = Toplevel(root, bg="#F5F5F7")
    window.title("图片细节对比")
    window.geometry("1020x720")
    window.minsize(650, 460)
    window.transient(root)
    ctk.CTkLabel(window, text="滚轮缩放 · 拖动平移 · 100% 为原始像素大小", text_color="#6E6E73").pack(pady=10)
    toolbar = ctk.CTkFrame(window, fg_color="transparent"); toolbar.pack(fill="x", padx=14)
    zoom = StringVar(value="适应窗口")
    ctk.CTkOptionMenu(toolbar, variable=zoom, values=["适应窗口", "50%", "100%", "200%"], command=lambda _: redraw()).pack(side="right")
    panels = ctk.CTkFrame(window, fg_color="transparent"); panels.pack(fill="both", expand=True, padx=10, pady=10)
    panels.grid_columnconfigure((0, 1), weight=1, uniform="compare")
    panels.grid_rowconfigure(0, weight=1)
    state = []
    for col, value in enumerate((next(iter(choices)), selected)):
        panel = ctk.CTkFrame(panels, fg_color="white"); panel.grid(row=0, column=col, sticky="nsew", padx=4)
        choice = StringVar(value=value)
        ctk.CTkOptionMenu(panel, variable=choice, values=list(choices), command=lambda _, c=col: load(c)).pack(fill="x", padx=10, pady=10)
        canvas = Canvas(panel, bg="#E7E9ED", highlightthickness=0)
        canvas.pack(fill="both", expand=True, padx=6, pady=(0, 6))
        state.append({"canvas": canvas, "choice": choice, "photo": None, "image": None})

    def load(index):
        s = state[index]
        try:
            with Image.open(choices[s["choice"].get()]) as image:
                s["image"] = ImageOps.exif_transpose(image).convert("RGB")
        except OSError:
            s["image"] = None
        redraw()

    def redraw():
        for s in state:
            canvas, source = s["canvas"], s["image"]
            canvas.delete("all")
            if source is None:
                continue
            w, h = max(1, canvas.winfo_width()), max(1, canvas.winfo_height())
            scale = min(w / source.width, h / source.height) if zoom.get() == "适应窗口" else float(zoom.get().strip("%")) / 100
            # Bound the display allocation even for large references at 200%.
            scale = min(scale, 6000 / max(source.size))
            size = (max(1, round(source.width * scale)), max(1, round(source.height * scale)))
            s["photo"] = ImageTk.PhotoImage(source.resize(size, Image.Resampling.LANCZOS), master=canvas)
            canvas.create_image(max(w, size[0]) / 2, max(h, size[1]) / 2, image=s["photo"], anchor="center")
            canvas.configure(scrollregion=(0, 0, max(w, size[0]), max(h, size[1])))

    def wheel(delta):
        steps = ["适应窗口", "50%", "100%", "200%"]
        zoom.set(steps[max(0, min(3, steps.index(zoom.get()) + (1 if delta > 0 else -1)))])
        redraw()

    for s in state:
        canvas = s["canvas"]
        canvas.bind("<ButtonPress-1>", lambda e, c=canvas: c.scan_mark(e.x, e.y))
        canvas.bind("<B1-Motion>", lambda e, c=canvas: c.scan_dragto(e.x, e.y, gain=1))
        canvas.bind("<Configure>", lambda _: redraw())
        canvas.bind("<MouseWheel>", lambda e: wheel(e.delta))
    for index in range(2):
        load(index)
    window.after(50, redraw)
    return window
