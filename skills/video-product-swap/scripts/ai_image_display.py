"""Bounded background image decoding and resizing. Never creates or touches Tk objects."""
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
import math
from pathlib import Path

from PIL import Image, ImageOps


class ImageDisplay:
    def __init__(self, deliver):
        self.deliver = deliver
        self.preview_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="image-preview")
        self.thumb_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="image-thumbnail")
        self.pending_preview = None
        self.sources, self.renders = OrderedDict(), OrderedDict()
        self.closed = False

    def _submit(self, pool, kind, context, work):
        if self.closed: return None
        future = pool.submit(work)
        def done(completed):
            if completed.cancelled() or self.closed: return
            try: result, error = completed.result(), None
            except Exception: result, error = None, "图片无法读取，请检查文件后重新选择。"
            if not self.closed: self.deliver(kind, context, result, error)
        future.add_done_callback(done)
        return future

    def preview(self, context):
        # At most the running render and the newest selection remain queued.
        if self.pending_preview: self.pending_preview.cancel()
        self.pending_preview = self._submit(self.preview_pool, "display_preview", context, lambda: self._render(context))

    def thumbnail(self, key):
        path, size = key
        def load():
            with Image.open(path) as source:
                source.draft("RGB", size)
                thumb = ImageOps.exif_transpose(source).convert("RGB")
                thumb.thumbnail(size, Image.Resampling.LANCZOS)
            return thumb
        self._submit(self.thumb_pool, "display_thumbnail", key, load)

    @staticmethod
    def _remember(cache, key, image, pixel_budget):
        if image.width * image.height > pixel_budget: return
        cache[key] = image; cache.move_to_end(key)
        while sum(i.width * i.height for i in cache.values()) > pixel_budget:
            cache.popitem(last=False)

    def _render(self, context):
        path = Path(context["path"])
        stat = path.stat()
        file_key = (str(path), stat.st_size, stat.st_mtime_ns)
        source = self.sources.get(file_key)
        if source is None:
            with Image.open(path) as original:
                source = ImageOps.exif_transpose(original).convert("RGB")
            self._remember(self.sources, file_key, source, 24_000_000)
        else: self.sources.move_to_end(file_key)
        w, h = context["size"]
        fit = min(max(1, w - 12) / source.width, max(1, h - 12) / source.height, 1)
        scale = min(fit * context["zoom"], 6000 / max(source.size), math.sqrt(6_000_000 / (source.width * source.height)))
        size = (max(1, round(source.width * scale)), max(1, round(source.height * scale)))
        key = (file_key, size)
        rendered = self.renders.get(key)
        if rendered is None:
            rendered = source.resize(size, Image.Resampling.LANCZOS)
            self._remember(self.renders, key, rendered, 8_000_000)
        else: self.renders.move_to_end(key)
        return {"image": rendered, "scale": scale}

    def close(self, wait=False):
        self.closed = True
        self.preview_pool.shutdown(wait=wait, cancel_futures=True)
        self.thumb_pool.shutdown(wait=wait, cancel_futures=True)
