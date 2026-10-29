import io
import sys
import unittest
from pathlib import Path

from PIL import Image, ImageFont

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from image_renderer import COLORS, FONT_PATH, render_board_image


class BoardRenderingTests(unittest.TestCase):
    def test_png_shows_all_rows_and_comparison_colors(self):
        rows = [
            {"name": "选手一", "cells": [
                {"label": "国家", "value": "中国", "level": "correct"},
                {"label": "年龄", "value": "26↑", "level": "close"},
                {"label": "战队", "value": "很长的战队名称 EDward Gaming", "level": "wrong"},
            ]},
            {"name": "选手二", "cells": [
                {"label": "国家", "value": "瑞典", "level": "wrong"},
                {"label": "年龄", "value": "31↓", "level": "wrong"},
                {"label": "战队", "value": "Ninjas in Pyjamas", "level": "correct"},
            ]},
        ]
        one = Image.open(io.BytesIO(render_board_image("弗一把 · 猜选手", rows[:1], "还剩 7 次机会")))
        image = Image.open(io.BytesIO(render_board_image("弗一把 · 猜选手", rows, "还剩 6 次机会")))
        self.assertEqual(image.format, "PNG")
        self.assertGreater(image.height, one.height)
        colors = {rgb for _, rgb in image.getcolors(image.width * image.height)}
        for color in (COLORS["correct"], COLORS["close"], COLORS["wrong"]):
            self.assertIn(tuple(bytes.fromhex(color[1:])), colors)

    def test_bundled_font_supports_chinese_and_arrow(self):
        font = ImageFont.truetype(str(FONT_PATH), 24)
        # A missing glyph yields the same .notdef bitmap as an unsupported code point.
        missing = bytes(font.getmask("\U0010ffff"))
        for char in "猜选手国家年龄↑↓":
            self.assertNotEqual(bytes(font.getmask(char)), missing, char)

    def test_empty_history_rejected(self):
        with self.assertRaises(ValueError):
            render_board_image("猜选手", [])


if __name__ == "__main__":
    unittest.main()
