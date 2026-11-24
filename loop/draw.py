"""把一句话画成图片。

她说「给你看」的时候，手上得真有东西。这里只做一件很小的事：
文字 → 一张深色海报 PNG，可选逐帧生成（frames>1 就是"会动的"：
连发几张，微信里看起来就是在长出来）。

故意不做自由排版 / 外部素材：她给什么字就排什么字，不替她编内容。
"""
from __future__ import annotations

import os
import re
import time

OUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "data", "artifacts", "draw")

_FONT_CANDIDATES = [
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\msyhbd.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
    r"C:\Windows\Fonts\Deng.ttf",
]

_W, _H = 720, 720
_BG = (12, 22, 40)
_BG2 = (18, 44, 78)
_FG = (226, 238, 255)
_ACCENT = (108, 176, 255)


def _font(size: int):
    from PIL import ImageFont
    for path in _FONT_CANDIDATES:
        if os.path.isfile(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue
    return ImageFont.load_default()


def _slug(text: str) -> str:
    s = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "", text or "")[:12]
    return s or "draw"


def _wrap(draw_obj, text: str, font, max_w: int) -> list[str]:
    lines: list[str] = []
    for raw in str(text or "").split("\n"):
        if not raw:
            lines.append("")
            continue
        cur = ""
        for ch in raw:
            probe = cur + ch
            if draw_obj.textlength(probe, font=font) <= max_w:
                cur = probe
            else:
                lines.append(cur)
                cur = ch
        lines.append(cur)
    return lines


def render(text: str, *, frames: int = 1, footer: str = "") -> list[str]:
    """把 text 画成 frames 张 png，返回文件路径列表（按播放顺序）。"""
    from PIL import Image, ImageDraw

    frames = max(1, min(int(frames or 1), 5))
    stamp = time.strftime("%Y%m%d-%H%M%S")
    base = os.path.join(OUT_DIR, "%s-%s" % (stamp, _slug(text)))
    os.makedirs(OUT_DIR, exist_ok=True)

    body_font, foot_font = _font(46), _font(24)
    probe = ImageDraw.Draw(Image.new("RGB", (_W, _H)))
    lines = _wrap(probe, str(text or "").strip()[:220], body_font, _W - 120)
    paths: list[str] = []

    for i in range(frames):
        img = Image.new("RGB", (_W, _H), _BG)
        d = ImageDraw.Draw(img)
        # 背景：竖向渐变 + 一条海面线，跟她的配色走
        for y in range(_H):
            t = y / float(_H)
            d.line([(0, y), (_W, y)],
                   fill=(int(_BG[0] + (_BG2[0] - _BG[0]) * t),
                         int(_BG[1] + (_BG2[1] - _BG[1]) * t),
                         int(_BG[2] + (_BG2[2] - _BG[2]) * t)))
        d.line([(0, _H - 130), (_W, _H - 130)], fill=_ACCENT, width=2)

        # 逐帧长出来：第 i 帧只显示前 (i+1)/frames 的行
        show_n = max(1, round(len(lines) * (i + 1) / frames))
        total_h = len(lines[:show_n]) * 62
        y = (_H - 130 - total_h) // 2
        for line in lines[:show_n]:
            w = d.textlength(line, font=body_font)
            d.text(((_W - w) / 2, y), line, font=body_font, fill=_FG)
            y += 62
        if footer:
            w = d.textlength(footer, font=foot_font)
            d.text(((_W - w) / 2, _H - 90), footer, font=foot_font, fill=_ACCENT)

        path = "%s-%02d.png" % (base, i + 1)
        img.save(path, "PNG")
        paths.append(path)
    return paths


__all__ = ["render", "OUT_DIR"]
