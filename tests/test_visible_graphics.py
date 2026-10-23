import unittest
from unittest.mock import Mock

import pymupdf

from pdf_flow import visible_drawings


class VisibleGraphicsTests(unittest.TestCase):
    def test_nested_clips_and_exit_restore_the_correct_visible_bounds(self):
        page = Mock()
        page.get_drawings.return_value = [
            {"type": "clip", "level": 0, "scissor": (20, 20, 200, 200)},
            {"type": "group", "level": 1, "rect": (0, 0, 600, 600)},
            {"type": "clip", "level": 1, "scissor": (80, 70, 140, 130)},
            {"type": "f", "level": 2, "rect": (-500, -500, 600, 600)},
            {"type": "s", "level": 1, "rect": (30, 30, 60, 60)},
            {"type": "f", "level": 0, "rect": (300, 300, 330, 330)},
        ]
        result = visible_drawings(page)
        self.assertEqual([list(d["rect"]) for d in result],
                         [[80, 70, 140, 130], [30, 30, 60, 60], [300, 300, 330, 330]])
        page.get_drawings.assert_called_once_with(extended=True)

    def test_clipping_keeps_visible_rules_and_discards_fully_hidden_paths(self):
        page = Mock()
        page.get_drawings.return_value = [
            {"type": "clip", "level": 0, "scissor": (110, 90, 130, 120)},
            {"type": "s", "level": 1, "rect": (100, 100, 150, 100)},
            {"type": "s", "level": 1, "rect": (120, 80, 120, 130)},
            {"type": "s", "level": 1, "rect": (200, 100, 250, 110)},
        ]
        self.assertEqual([list(d["rect"]) for d in visible_drawings(page)],
                         [[110, 100, 130, 100], [120, 90, 120, 120]])

    def test_real_pdf_plot_does_not_occupy_the_other_column(self):
        with pymupdf.open() as pdf:
            page = pdf.new_page(width=600, height=600)
            page.draw_rect((-50, -50, 650, 300), fill=(0, 0, 0))
            xref = page.get_contents()[0]
            contents = pdf.xref_stream(xref)
            pdf.update_stream(xref, b"q\n40 360 220 200 re W n\n" + contents + b"\nQ")
            self.assertGreater(page.get_drawings()[0]["rect"].x1, 600)
            boxes = [drawing["rect"] for drawing in visible_drawings(page)]
            self.assertTrue(boxes)
            for box in boxes:
                self.assertGreaterEqual(box.x0, 39.99)
                self.assertLessEqual(box.x1, 260.01)
                self.assertLessEqual(box.y1, 240.01)


if __name__ == "__main__":
    unittest.main()
