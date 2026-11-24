# -*- coding: utf-8 -*-
"""外部世界两块板：只保留「外部世界」四个字。"""
from PIL import Image, ImageDraw, ImageFont
from pathlib import Path

BG = (10, 14, 22); BORDER = (120, 200, 255); TEXT = (226, 236, 248)
F = lambda s: ImageFont.truetype(r"C:\Windows\Fonts\msyhbd.ttc", s)

def make(w, h, size, out):
    img = Image.new("RGB", (w, h), BG)
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([6, 6, w - 6, h - 6], radius=14, outline=BORDER, width=3)
    f = F(size)
    d.text((w / 2, h / 2), "外部世界", font=f, fill=TEXT, anchor="mm")
    img.save(out)
    print("wrote", out, img.size, Path(out).stat().st_size, "bytes")

make(600, 1400, 118, "output/3d/t_outer.png")        # 竖板 240x560
make(1300, 620, 210, "output/3d/t_outer_flat.png")   # 横板 1300x620