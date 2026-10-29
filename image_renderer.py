"""Render a whole guessing game as a local PNG using Pillow.

The game owns comparison and persistence; this module only displays the supplied
snapshots. It does not access the answer, filesystem outside the bundled font,
or the network.
"""
from __future__ import annotations

from io import BytesIO
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

FONT_PATH = Path(__file__).resolve().parent / "assets" / "NotoSansSC.ttf"
COLORS = {
    "correct": "#26775e",
    "close": "#98732c",
    "wrong": "#833c46",
    "unknown": "#4b5862",
}
BG = "#101821"
PANEL = "#1a2733"
TEXT = "#f3f6f8"
MUTED = "#b9c9d1"


def _get(item: Any, key: str, default: Any = "") -> Any:
    return item.get(key, default) if isinstance(item, dict) else getattr(item, key, default)


def _wrap(text: str, font: ImageFont.FreeTypeFont, width: int) -> list[str]:
    """Wrap by measured width, including long words and explicit line breaks."""
    lines: list[str] = []
    for paragraph in str(text).split("\n"):
        line = ""
        for ch in paragraph:
            candidate = line + ch
            if line and font.getlength(candidate) > width:
                # Prefer a word boundary when available, but still split an
                # oversized word so it cannot overflow its card.
                split_at = line.rfind(" ")
                if split_at > 0:
                    lines.append(line[:split_at])
                    line = line[split_at + 1:] + ch
                else:
                    lines.append(line)
                    line = ch.lstrip() if ch.isspace() else ch
            else:
                line = candidate
        lines.append(line)
    return lines or [""]


def _draw_lines(draw: ImageDraw.ImageDraw, lines: list[str], *, x: int, y: int,
                width: int, line_height: int, font: ImageFont.FreeTypeFont,
                fill: str) -> None:
    for line in lines:
        # Center based on the measured advance width; long text has already wrapped.
        left = x + max(0, (width - font.getlength(line)) / 2)
        draw.text((left, y), line, font=font, fill=fill, anchor="lt")
        y += line_height


def _font(size: int, weight: str = "Regular") -> ImageFont.FreeTypeFont:
    font = ImageFont.truetype(str(FONT_PATH), size)
    font.set_variation_by_name(weight)
    return font


def render_board_image(title: str, rows: list[dict], status: str = "") -> bytes:
    """Return one PNG for all guesses.

    Each row has ``name`` and ``cells``. Every cell supplies ``label``, ``value``
    and ``level`` (correct, close, wrong), either as a mapping or an object with
    those attributes. Rows remain in the order supplied by the caller.
    """
    if not rows:
        raise ValueError("a board needs at least one guess")
    if not FONT_PATH.is_file():
        raise FileNotFoundError(f"bundled font missing: {FONT_PATH}")

    title_font = _font(30, "SemiBold")
    name_font = _font(19, "Medium")
    label_font = _font(16, "Medium")
    value_font = _font(20, "Medium")
    status_font = _font(19)

    margin, gap, name_w, cell_w = 24, 6, 154, 124
    max_cells = max(len(_get(row, "cells", [])) for row in rows)
    if not max_cells:
        raise ValueError("a board row needs at least one cell")
    width = max(720, margin * 2 + name_w + gap + max_cells * (cell_w + gap) - gap)
    header_lines = _wrap(title, title_font, width - margin * 2)
    footer_lines = _wrap(status, status_font, width - margin * 2) if status else []
    header_h = 30 + len(header_lines) * 40 + 18
    footer_h = 32 + len(footer_lines) * 30 if footer_lines else 16

    # Precompute each card's text and height before allocating the image.
    prepared = []
    for row in rows:
        name_lines = _wrap(str(_get(row, "name")), name_font, name_w - 20)
        cells = []
        row_h = max(94, len(name_lines) * 26 + 38)
        for cell in _get(row, "cells", []):
            labels = _wrap(str(_get(cell, "label")), label_font, cell_w - 16)
            values = _wrap(str(_get(cell, "value")), value_font, cell_w - 16)
            row_h = max(row_h, 30 + len(labels) * 22 + len(values) * 27)
            cells.append((labels, values, str(_get(cell, "level"))))
        prepared.append((name_lines, cells, row_h))
    height = header_h + sum(row_h for _, _, row_h in prepared) + gap * (len(rows) - 1) + footer_h
    image = Image.new("RGB", (width, height), BG)
    draw = ImageDraw.Draw(image)
    draw.text((margin, 25), "\n".join(header_lines), font=title_font, fill=TEXT, spacing=8)
    y = header_h
    for number, (name_lines, cells, row_h) in enumerate(prepared, 1):
        draw.rounded_rectangle((margin, y, margin + name_w, y + row_h), radius=6, fill=PANEL)
        draw.text((margin + 10, y + 9), f"#{number}", font=label_font, fill=MUTED)
        _draw_lines(draw, name_lines, x=margin + 8, y=y + 35, width=name_w - 16,
                    line_height=26, font=name_font, fill=TEXT)
        x = margin + name_w + gap
        for labels, values, level in cells:
            draw.rounded_rectangle((x, y, x + cell_w, y + row_h), radius=6,
                                   fill=COLORS.get(level, COLORS["wrong"]))
            _draw_lines(draw, labels, x=x + 8, y=y + 12, width=cell_w - 16,
                        line_height=22, font=label_font, fill="#e4eaeb")
            _draw_lines(draw, values, x=x + 8, y=y + 18 + len(labels) * 22,
                        width=cell_w - 16, line_height=27, font=value_font, fill=TEXT)
            x += cell_w + gap
        y += row_h + gap
    if footer_lines:
        draw.text((margin, y + 8), "\n".join(footer_lines), font=status_font, fill=MUTED, spacing=8)
    out = BytesIO()
    image.save(out, format="PNG", optimize=True)
    return out.getvalue()
