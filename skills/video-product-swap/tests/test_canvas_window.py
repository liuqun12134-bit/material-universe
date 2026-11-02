import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from canvas_window import descendant_process_ids


class CanvasProcessTests(unittest.TestCase):
    def test_launcher_renderer_descendants_exclude_other_python_processes(self):
        self.assertEqual(descendant_process_ids(10, [(30, 20), (20, 10), (41, 40), (50, 30)]), {10, 20, 30, 50})
        self.assertEqual(descendant_process_ids(10, []), {10})
        self.assertEqual(descendant_process_ids(10, [(10, 20), (20, 10)]), {10, 20})


if __name__ == "__main__":
    unittest.main()
