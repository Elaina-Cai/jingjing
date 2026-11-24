import re, unicodedata
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

out = Path(".test_tmp/3d")
FS, CW, CH, PAD = 30, 15, 34, 24
BASE = int(CH * 0.78)
FONT = ImageFont.truetype(r"C:\Windows\Fonts\msyh.ttc", FS)

COLORS = {
    "window":   (214, 236, 214),
    "persona":  (255, 214, 140),
    "attitude": (150, 226, 255),
    "mood":     (255, 172, 220),
}

def wid(ch):
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1

BOX = set("─│┌┐└┘├┤┬┴┼═║┃┊┄┅")

def draw_box(d, ch, x, y, color, w=2):
    cx, cy = x + CW / 2, y + CH / 2
    L, R, T, B = x, x + CW, y, y + CH
    def hl(a, b, yy): d.line([(a, yy), (b, yy)], fill=color, width=w)
    def vl(xx, a, b): d.line([(xx, a), (xx, b)], fill=color, width=w)
    m = {
        "─": lambda: hl(L, R, cy),
        "│": lambda: vl(cx, T, B),
        "┌": lambda: (hl(cx, R, cy), vl(cx, cy, B)),
        "┐": lambda: (hl(L, cx, cy), vl(cx, cy, B)),
        "└": lambda: (hl(cx, R, cy), vl(cx, T, cy)),
        "┘": lambda: (hl(L, cx, cy), vl(cx, T, cy)),
        "├": lambda: (vl(cx, T, B), hl(cx, R, cy)),
        "┤": lambda: (vl(cx, T, B), hl(L, cx, cy)),
        "┬": lambda: (hl(L, R, cy), vl(cx, cy, B)),
        "┴": lambda: (hl(L, R, cy), vl(cx, T, cy)),
        "┼": lambda: (hl(L, R, cy), vl(cx, T, B)),
        "═": lambda: (hl(L, R, cy - 1), hl(L, R, cy + 1)),
        "║": lambda: (vl(cx - 1, T, B), vl(cx + 1, T, B)),
        "┃": lambda: vl(cx, T, B, ) if False else d.line([(cx, T), (cx, B)], fill=color, width=3),
        "┊": lambda: [vl(cx, T + k * 6, T + k * 6 + 3) for k in range(6)],
        "┄": lambda: [hl(L + k * 6, L + k * 6 + 3, cy) for k in range(3)],
        "┅": lambda: [hl(L + k * 6, L + k * 6 + 3, cy) for k in range(3)],
    }
    f = m.get(ch)
    if f: f()

def render(name, lines):
    cols = max((sum(wid(c) for c in l) for l in lines), default=0)
    W, H = int(cols * CW + PAD * 2), int(len(lines) * CH + PAD * 2)
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    color = COLORS[name]
    for r, line in enumerate(lines):
        col = 0
        y = PAD + r * CH
        for ch in line:
            w = wid(ch)
            x = PAD + col * CW
            if ch.strip():
                if ch in BOX:
                    draw_box(d, ch, x, y, color)
                else:
                    d.text((x, y + BASE), ch, font=FONT, fill=color, anchor="ls")
            col += w
    path = out / ("canvas_%s.png" % name)
    img.save(path)
    return path, img, lines

def verify(name, img, lines):
    a = img.load()
    ok = bad = 0
    for r, line in enumerate(lines):
        col = 0
        for ch in line:
            if ch in BOX:
                x = int(PAD + col * CW + CW / 2); y = int(PAD + r * CH + CH / 2)
                hit = any(a[x + dx, y + dy][3] > 0 for dx in (-4, 0, 4) for dy in (-4, 0, 4))
                ok += hit; bad += (not hit)
            col += wid(ch)
    ink = sum(1 for px in img.getdata() if px[3] > 0)
    print("%-9s %s  size=%s  box_ok=%d box_missing=%d  ink_px=%d" % (name, "OK" if bad == 0 else "WARN", img.size, ok, bad, ink))

docs = {"persona": Path("docs/decisions/人设层_存储与读取.md"),
        "attitude": Path("docs/decisions/态度层_存储与读取.md"),
        "mood": Path("docs/decisions/情绪层_存储与读取.md")}
for name, p in docs.items():
    blocks = re.findall(r"```[a-zA-Z]*\n(.*?)```", p.read_text(encoding="utf-8"), re.S)
    cands = [b for b in blocks if "persona_versions" in b or "态度行" in b or "chat_mood" in b or "─" in b]
    best = max(cands, key=lambda b: b.count("\n"))
    (out / ("canvas_%s.txt" % name)).write_text(best, encoding="utf-8")

for name in ("window", "persona", "attitude", "mood"):
    lines = (out / ("canvas_%s.txt" % name)).read_text(encoding="utf-8").splitlines()
    while lines and not lines[0].strip(): lines.pop(0)
    while lines and not lines[-1].strip(): lines.pop()
    path, img, lines = render(name, lines)
    verify(name, img, lines)