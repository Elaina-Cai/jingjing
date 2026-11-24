# -*- coding: utf-8 -*-
"""3D 图的「点板看文档」链路测试：注册了点击的板，必须三件齐备。

三件 = ①`reg(...)` 注册了点击（否则点不动）②`DOCKEY[标签]` 有映射（否则 `showDoc(null)` 静默关掉面板）
③`docdata` 里真有那段 HTML（否则 `showDoc` 拿不到正文）。
2026-10-21 的真实故障就是漏了 ②③：`DOCS` 加了「在手的事」，板子却点不出文档。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VIEWER = ROOT / "output" / "3d" / "viewer_css.html"


def check(name: str, cond: bool, detail: str = "") -> bool:
    print(("PASS " if cond else "FAIL ") + name + ("" if not detail else " | " + detail))
    return bool(cond)


def block(src: str, start: str, end: str) -> str:
    i = src.index(start) + len(start)
    j = src.index(end, i)
    return src[i:j]


def main() -> int:
    src = VIEWER.read_text(encoding="utf-8")
    ok = True

    docdata = json.loads(block(src, '<script id="docdata" type="application/json">', "</script>"))
    dockey = dict(re.findall(r"'([^']+)'\s*:\s*'([^']+)'", block(src, "const DOCKEY = {", "};")))
    docs = dict(re.findall(r"'([^']+)'\s*:\s*'(docs/decisions/[^']+)'", block(src, "const DOCS = {", "};")))
    regs = re.findall(r"reg\(\s*\w+\s*,\s*'([^']+)'", src)

    ok &= check("注册了点击的板 ≥ 9 块", len(regs) >= 9, "labels=%s" % (regs,))
    ok &= check("每块注册的板都有唯一标签", len(regs) == len(set(regs)), "重复：%s"
                % [x for x in regs if regs.count(x) > 1])

    for label in regs:
        key = dockey.get(label)
        ok &= check("① 点得动：「%s」已 reg()" % label, True)
        ok &= check("② 有 DOCKEY 映射：「%s」→ %r" % (label, key), key is not None)
        if key is None:
            continue
        ok &= check("③ docdata 里有正文：「%s」→ %s" % (label, key),
                    key in docdata and "dhead" in docdata[key],
                    "%d 字" % len(docdata.get(key, "")))
        path = docs.get(label)
        ok &= check("④ 有 .md 来源：「%s」" % label,
                    path is not None and (ROOT / path).exists(), str(path))

    # 第 12 个实体（2026-10-21 新增）三条链路单独钉一遍
    for label in ("在手的事",):
        ok &= check("新实体「%s」在 reg 列表里" % label, label in regs)
        ok &= check("新实体「%s」有 DOCKEY" % label, label in dockey)
        ok &= check("新实体「%s」有 docdata" % label, dockey.get(label) in docdata)
        ok &= check("新实体「%s」有 .md 路径" % label, label in docs)

    failed = 0 if ok else 1
    print("RESULT %d failed" % failed)
    return failed


if __name__ == "__main__":
    raise SystemExit(main())
