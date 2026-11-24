# -*- coding: utf-8 -*-
"""渲染「在手的事」板贴图 t_job.png（板 300x220 → 贴图 600x440）。

一件事的四槽、安全点、等批准才动手——这三句是这块板存在的理由。
改口径的顺序：docs/decisions/办_在手的事.md → 本脚本 → 重跑。
"""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

BG = (10, 14, 22)
BORDER = (120, 200, 255)
TITLE = (168, 226, 255)
TEXT = (228, 234, 242)
DIM = (146, 158, 176)


def _font(name, size):
    return ImageFont.truetype(rf"C:\Windows\Fonts\{name}", size)


def board(w, h, title, lines, out, tsize=64, lsize=28):
    img = Image.new("RGB", (w, h), BG)
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([6, 6, w - 6, h - 6], radius=16, outline=BORDER, width=3)
    d.text((w / 2, h * 0.28), title, font=_font("msyhbd.ttc", tsize), fill=TITLE, anchor="mm")
    y = h * 0.52
    for text, color in lines:
        d.text((w / 2, y), text, font=_font("msyh.ttc", lsize), fill=color, anchor="mm")
        y += lsize * 1.6
    img.save(out)
    print("wrote", out, img.size, Path(out).stat().st_size, "bytes")


board(600, 440, "在 手 的 事", [
    ("一件事 · 四槽", TEXT),
    ("目标 · 步骤 · 安全点", TEXT),
    ("只由判推进，等批准才动手", DIM),
], "output/3d/t_job.png")
