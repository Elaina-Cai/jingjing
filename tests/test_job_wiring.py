# -*- coding: utf-8 -*-
"""第 12 个实体「在手的事」的接线测试：三条管子的两端必须焊在板边 / 立方体棱上。

**为什么要有这个测试**：3D 图里两套 y 轴口径并存——
  addBoard(w, h, x, y, z) 的 y 是世界坐标（向上为正），渲染成 translate3d(x, -y, z)；
  addPipe3D([x, y, z], ...) 的 y 是 CSS 坐标（向下为正）。
v0.1 的三条管子照世界坐标写，于是管子全悬在空中、和板子差着几百像素（业主肉眼发现）。
本测试把它们钉死：任何一端离开板边就红。
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VIEWER = ROOT / "output" / "3d" / "viewer_css.html"
DOC = ROOT / "docs" / "decisions" / "办_在手的事.md"

HALF = 280          # LLM_SIDE / 2 = 560 / 2；也是线框立方体的半边长
Z_LLM = 1000        # LLM_Z


def check(name: str, cond: bool, detail: str = "") -> bool:
    print(("PASS " if cond else "FAIL ") + name + ("" if not detail else " | " + detail))
    return bool(cond)


def parse_pipe(src: str, label: str):
    """返回 addPipe3D 两端点 [(x, y, z), (x, y, z)]；第三分量可以是 LLM_Z"""
    pat = (
        r"addPipe3D\(\[([^\]]+?)\]\s*,\s*\[([^\]]+?)\]\s*,\s*'"
        + re.escape(label) + r"'"
    )
    m = re.search(pat, src)
    if not m:
        return None

    def pt(raw: str):
        parts = [x.strip() for x in raw.split(",")]
        if len(parts) != 3:
            return None
        out = []
        for p in parts:
            if p == "LLM_Z":
                out.append(Z_LLM)
            else:
                out.append(float(p))
        return tuple(out)

    a, b = pt(m.group(1)), pt(m.group(2))
    if a is None or b is None:
        return None
    return a, b


def on_edge(pt, rect, tol=0.5) -> bool:
    x0, x1, y0, y1 = rect
    x, y = pt[0], pt[1]
    if not (x0 - tol <= x <= x1 + tol and y0 - tol <= y <= y1 + tol):
        return False
    return abs(x - x0) <= tol or abs(x - x1) <= tol or abs(y - y0) <= tol or abs(y - y1) <= tol


def main() -> int:
    src = VIEWER.read_text(encoding="utf-8")
    doc = DOC.read_text(encoding="utf-8")
    ok = True

    job = (470, 770, 190, 410)        # 在手的事：world y=-300 → CSS y=300，半高 110
    exit_ = (470, 770, -110, 110)     # 出口：world y=0
    judge = (0 - HALF, 0 + HALF, 0 - HALF, 0 + HALF)
    cube = (950 - HALF, 950 + HALF, 600 - HALF, 600 + HALF)   # 工具包近面（z=280）

    ok &= check("板子写在画里",
                "addBoard(300, 220, 620, -300, LLM_Z, 't_job.png', '在手的事')" in src)

    p1 = parse_pipe(src, "job 读数 / advance()")
    ok &= check("管子『job 读数 / advance()』存在", p1 is not None)
    if p1:
        a, b = p1
        ok &= check("  判端焊在判板右沿 (x=280, y∈[-280,280], z=LLM_Z)",
                    on_edge(a, judge) and a[2] == Z_LLM, "端点 %s" % (a,))
        ok &= check("  事端焊在在手的事左沿 (x=470, y∈[190,410], z=LLM_Z)",
                    on_edge(b, job) and b[2] == Z_LLM, "端点 %s" % (b,))

    p2 = parse_pipe(src, "只送已批准")
    ok &= check("管子『只送已批准』存在", p2 is not None)
    if p2:
        a, b = p2
        ok &= check("  上端焊在在手的事上沿 (y=190)",
                    on_edge(a, job) and a[2] == Z_LLM, "端点 %s" % (a,))
        ok &= check("  下端焊在出口下沿 (y=110)",
                    on_edge(b, exit_) and b[2] == Z_LLM, "端点 %s" % (b,))

    p3 = parse_pipe(src, "任务预算")
    ok &= check("管子『任务预算』存在", p3 is not None)
    if p3:
        a, b = p3
        ok &= check("  起端焊在在手的事右沿 (x=770)",
                    on_edge(a, job) and a[2] == Z_LLM, "端点 %s" % (a,))
        ok &= check("  末端焊在工具包近面上沿 (y=320, z=280)",
                    on_edge(b, cube) and b[2] == 280, "端点 %s" % (b,))

    ok &= check("文档写明两套 y 轴口径（防止再犯）",
                "CSS 坐标（向下为正）" in doc and "世界坐标（向上为正）" in doc)

    failed = 0 if ok else 1
    print("RESULT %d failed" % failed)
    return failed


if __name__ == "__main__":
    raise SystemExit(main())
