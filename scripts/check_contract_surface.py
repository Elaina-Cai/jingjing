# -*- coding: utf-8 -*-
"""校验「文档里写成代码形状的东西，在代码里真的有个位置承接」。

为什么需要它：`docs/decisions/` 是事实源，它说「字段叫 pull_id」「回执叫
sent_unconfirmed」——如果代码里一个字的落点都没有，那么文档的这句要求
**谁也拦不住它被忘掉**。这个脚本把这类句子变成可数的漂移。

判据：把 `docs/decisions/*.md` 里反引号包起来的**代码形状**（含下划线或
CamelCase、带括号的调用/构造）挑出来，去 `core/ agents/ db/ scheduler/ web/
scripts/ tests/` 的标识符集合里查。查不到 → 记为「无人承接」。

> 这不是「文档错了」的判决：也算「未来才实现」。区别在于**有没有切片认领它**
> （`refactor_plan.yaml` 的 files/interfaces 里出现即算认领）。

跑法：``python -X utf8 -u scripts/check_contract_surface.py``
      ``... --strict`` 有任何未认领项即 exit 1
      ``... --doc 出口.md`` 只看一篇
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs" / "decisions"
CODE_DIRS = ("core", "agents", "db", "scheduler", "web", "scripts", "tests")
#: 数据文件不是设计（受话标注集是 50 条人工标注的原文）
SKIP_DOCS = {"受话标注集_50条.md"}

TICK = re.compile(r"`([^`\n]{2,80})`")
#: 只有「要求句」里的代码形状才有承接义务；纯描述/示例行（SQL、JS、shell）不算
REQ_LINE = re.compile(r"(必须|不许|不得|一律|只能|返回|字段|接口|形状|签名|枚举|契|键是|键为)")
IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*")


def code_identifiers() -> set[str]:
    names: set[str] = set()
    pat = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
    for d in CODE_DIRS:
        for path in (ROOT / d).rglob("*.py"):
            if any(part in {".venv", ".venv_it", "__pycache__", ".test_tmp"} for part in path.parts):
                continue
            try:
                names.update(pat.findall(path.read_text(encoding="utf-8", errors="ignore")))
            except OSError:
                continue
    return names


def is_code_shape(raw: str) -> bool:
    """`pull_id` / `sent_unconfirmed` / `Receipt(...)` / `search_memory` 算；`2 小时` 不算。"""
    if any("\u4e00" <= ch <= "\u9fff" for ch in raw):
        return False
    if raw.startswith(("/", "./", "../")) or ".py" in raw or "/" in raw or "://" in raw:
        return False
    # SQL / JS / shell 片段不是「承接点」：带空格、等号、引号的都不算
    if any(ch in raw for ch in (" ", "=", '"', "'", "(", ")", ";", "|", ",", ">")):
        return False
    if raw.upper() in {"PRAGMA", "SELECT", "INSERT", "UPDATE", "DELETE", "ORDER"}:
        return False
    core = raw.split("(")[0].split(".")[-1].strip()
    if not core or not IDENT.match(core):
        return False
    has_call = "(" in raw
    has_underscore = "_" in core and len(core) >= 6
    camel = core[:1].isupper() and len(core) >= 6
    return bool(has_call or has_underscore or camel)


def main() -> int:
    only = None
    if "--doc" in sys.argv:
        only = sys.argv[sys.argv.index("--doc") + 1]
    known = code_identifiers()
    import yaml

    plan = (ROOT / "refactor_plan.yaml").read_text(encoding="utf-8")
    # 认领 = 任何在计划里出现过的标识符（files/interfaces 里出现即算）
    claimed = set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", plan))

    total_docs = 0
    missing_all: dict[str, list[str]] = {}
    for doc in sorted(DOCS.glob("*.md")):
        if doc.name in SKIP_DOCS or (only and doc.name != only):
            continue
        total_docs += 1
        missing: list[str] = []
        seen: set[str] = set()
        for i, line in enumerate(doc.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
            if not REQ_LINE.search(line):
                continue
            for raw in TICK.findall(line):
                if not is_code_shape(raw):
                    continue
                core = raw.split("(")[0].split(".")[-1].strip()
                if core in seen:
                    continue
                seen.add(core)
                if core in known:
                    continue
                tag = "已认领(计划里出现)" if core in claimed else "无人承接"
                missing.append(f"{doc.name}:{i} `{core}` → {tag}")
        if missing:
            missing_all[doc.name] = missing

    for name, items in missing_all.items():
        print(f"— {name}：{len(items)} 项代码形状在 code 里找不到")
        for line in items[:12]:
            print(f"    {line}")
        if len(items) > 12:
            print(f"    … 还有 {len(items) - 12} 项")

    total = sum(len(v) for v in missing_all.values())
    unclaimed = sum(1 for v in missing_all.values() for line in v if "无人承接" in line)
    print(f"RESULT {total_docs} 个文档 / {total} 项未落地（其中 {unclaimed} 项连切片都没认领）")
    strict = "--strict" in sys.argv
    return 1 if (strict and total) else 0


if __name__ == "__main__":
    raise SystemExit(main())
