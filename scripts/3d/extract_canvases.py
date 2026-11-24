import re
from pathlib import Path

out = Path(".test_tmp/3d"); out.mkdir(parents=True, exist_ok=True)
srcs = {
    "window": Path(".test_tmp/window_canvas.txt"),
    "persona": Path("docs/decisions/人设层_存储与读取.md"),
    "attitude": Path("docs/decisions/态度层_存储与读取.md"),
    "mood": Path("docs/decisions/情绪层_存储与读取.md"),
}

def wid(ch):
    import unicodedata
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1

def maxwidth(lines):
    return max((sum(wid(c) for c in l) for l in lines), default=0)

for name, p in srcs.items():
    if not p.exists():
        print(name, "MISSING", p); continue
    text = p.read_text(encoding="utf-8")
    if name == "window":
        lines = text.splitlines()
        tag = "file"
    else:
        blocks = re.findall(r"```[a-zA-Z]*\n(.*?)```", text, re.S)
        cands = [b for b in blocks if any(ch in b for ch in "┌│──")] or blocks
        if not cands:
            print(name, "NO BLOCK"); continue
        lines = max(cands, key=lambda b: b.count("\n")).splitlines()
        tag = "fence"
    # 去掉首尾纯空行
    while lines and not lines[0].strip(): lines.pop(0)
    while lines and not lines[-1].strip(): lines.pop()
    dst = out / ("canvas_" + name + ".txt")
    dst.write_text("\n".join(lines), encoding="utf-8")
    print("%-9s %-6s lines=%4d maxcols=%4d -> %s" % (name, tag, len(lines), maxwidth(lines), dst))