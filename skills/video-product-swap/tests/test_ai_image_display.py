"""Offline regressions for responsive image loading and worker shutdown."""
import queue
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from ai_image_display import ImageDisplay


class ImageDisplayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "preview.png"
        self.events = queue.Queue()
        self.display = ImageDisplay(lambda *event: self.events.put(event))

    def tearDown(self):
        self.display.close(wait=True)
        self.temp.cleanup()

    def request(self, token=1, size=(320, 240), zoom=1):
        return {"token": token, "path": str(self.path), "size": size, "zoom": zoom}

    def preview(self, **kwargs):
        context = self.request(**kwargs)
        self.display.preview(context)
        kind, returned, result, error = self.events.get(timeout=5)
        self.assertEqual(kind, "display_preview")
        self.assertEqual(returned, context)
        self.assertIsNone(error)
        return result["image"]

    def test_background_decode_reused_for_zoom_and_invalidated_on_file_change(self):
        Image.new("RGB", (800, 400), "red").save(self.path)
        opened_on = []
        original_open = Image.open

        def record(*args, **kwargs):
            opened_on.append(threading.get_ident())
            return original_open(*args, **kwargs)

        with patch("PIL.Image.open", side_effect=record):
            fitted = self.preview()
            enlarged = self.preview(token=2, zoom=2)
            self.assertGreater(enlarged.width, fitted.width)
            self.assertEqual(len(opened_on), 1, "zoom decoded the same source again")
            Image.new("RGB", (400, 800), "blue").save(self.path)
            replaced = self.preview(token=3)
            self.assertLess(replaced.width, replaced.height)
            self.assertEqual(replaced.getpixel((0, 0)), (0, 0, 255))
            self.assertEqual(len(opened_on), 2, "modified file used stale pixels")
            key = (str(self.path), (84, 65))
            self.display.thumbnail(key)
            kind, context, thumb, error = self.events.get(timeout=5)
            self.assertEqual((kind, context, error), ("display_thumbnail", key, None))
            self.assertLessEqual(thumb.width, 84)
            self.assertLessEqual(thumb.height, 65)
        self.assertNotIn(threading.get_ident(), opened_on)

    def test_rapid_selection_skips_obsolete_queued_render(self):
        started, release = threading.Event(), threading.Event()
        rendered = []

        def slow_render(context):
            rendered.append(context["token"])
            if context["token"] == 1:
                started.set()
                if not release.wait(5): raise TimeoutError("test did not release render")
            return context["token"]

        with patch.object(self.display, "_render", side_effect=slow_render):
            try:
                self.display.preview(self.request(token=1))
                self.assertTrue(started.wait(5))
                self.display.preview(self.request(token=2))
                self.display.preview(self.request(token=3))
            finally:
                release.set()
            self.assertEqual(self.events.get(timeout=5)[2:], (1, None))
            self.assertEqual(self.events.get(timeout=5)[2:], (3, None))
        self.assertEqual(rendered, [1, 3])

    def test_decode_failure_can_be_retried_after_repair(self):
        self.path.write_bytes(b"incomplete image")
        self.display.preview(self.request())
        kind, context, result, error = self.events.get(timeout=5)
        self.assertEqual(kind, "display_preview")
        self.assertEqual(context["token"], 1)
        self.assertIsNone(result)
        self.assertIn("无法读取", error)
        Image.new("RGB", (120, 80), "green").save(self.path)
        self.assertEqual(self.preview(token=2).getpixel((0, 0)), (0, 128, 0))

    def test_fit_preserves_orientation_and_extreme_zoom_is_bounded(self):
        exif = Image.Exif()
        exif[274] = 6
        Image.new("RGB", (1000, 2000), "white").save(self.path, exif=exif)
        fitted = self.preview()
        self.assertGreater(fitted.width, fitted.height)
        self.assertLessEqual(fitted.width, 320)
        self.assertLessEqual(fitted.height, 240)
        zoomed = self.preview(token=2, zoom=80)
        self.assertLessEqual(zoomed.width * zoomed.height, 6_000_000)
        self.assertLessEqual(max(zoomed.size), 6000)
        Image.new("RGB", (20000, 100), "white").save(self.path)
        wide = self.preview(token=3, zoom=80)
        self.assertLessEqual(max(wide.size), 6000)

    def test_close_cancels_queue_and_does_not_deliver_to_closed_window(self):
        started, release = threading.Event(), threading.Event()

        def slow_render(_):
            started.set()
            if not release.wait(5): raise TimeoutError("test did not release render")
            return "finished"

        with patch.object(self.display, "_render", side_effect=slow_render) as render:
            try:
                self.display.preview(self.request())
                self.assertTrue(started.wait(5))
                self.display.preview(self.request(token=2))
                self.display.close()
                self.display.preview(self.request(token=3))
            finally:
                release.set()
            self.display.close(wait=True)
            self.assertEqual(render.call_count, 1)
        self.assertTrue(self.events.empty())


if __name__ == "__main__": unittest.main()
