# -*- coding: utf-8 -*-
"""自我立方体 · 人设铭牌板：读 .test_tmp/persona_board.txt，严格渲染成 738x1374（比例 0.5375）。
   独立于 render_canvases（那份被改过，会产出别的比例）。"""
from PIL import Image, ImageDraw, ImageFont
from pathlib import Path
import re

W, H = 738, 1373
BG = (10, 14, 22); BORDER = (255, 210, 150)
TITLE = (255, 226, 170); TEXT = (228, 234, 242); DIM = (146, 158, 176); HEAD = (255, 196, 120)
F = lambda n, s: ImageFont.truetype(rf"C:\Windows\Fonts\{n}", s)

lines = [l.rstrip() for l in Path(".test_tmp/persona_board.txt").read_text(encoding="utf-8").splitlines()]
img = Image.new("RGB", (W, H), BG); d = ImageDraw.Draw(img)
d.rounded_rectangle([6, 6, W-6, H-6], radius=18, outline=BORDER, width=3)

f_t = F("msyhbd.ttc", 42); f_h = F("msyhbd.ttc", 25); f_i = F("msyh.ttc", 21); f_s = F("msyh.ttc", 18)
d.text((W/2, 44), "人设 · 铭牌", font=f_t, fill=TITLE, anchor="mm")
d.text((W/2, 80), "只读 · 版本化 · 只有人能改", font=f_s, fill=DIM, anchor="mm")
d.line([34, 102, W-34, 102], fill=BORDER, width=2)
y = 120
for raw in lines:
    s = raw.strip()
    if not s:
        y += 10; continue
    if s.startswith("【") and s.endswith("】"):
        y += 8
        d.text((40, y), s, font=f_h, fill=HEAD); y += 36; continue
    # 缩进层级：原样保留前导空格，映射到像素
    indent = len(raw) - len(raw.lstrip())
    x = 44 + min(indent, 8) * 11
    f = f_i if indent <= 2 else F("msyh.ttc", 19)
    fill = TEXT if indent <= 2 else DIM
    # 自动换行（按像素宽）
    line = ""
    for ch in s:
        if d.textlength(line + ch, font=f) > W - x - 44:
            d.text((x, y), line, font=f, fill=fill); y += 27; line = ch
        else:
            line += ch
    if line:
        d.text((x, y), line, font=f, fill=fill)
    y += 28
d.line([34, H-64, W-34, H-64], fill=BORDER, width=2)
d.text((W/2, H-34), "铭牌不动：身份/称呼/禁忌改一次要人点头", font=f_s, fill=DIM, anchor="mm")
out = Path("output/3d/persona.png"); img.save(out)
print("persona", img.size, "aspect=%.4f" % (W/H), out.stat().st_size, "bytes")