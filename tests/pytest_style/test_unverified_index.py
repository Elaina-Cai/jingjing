#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""未验证索引的形状锁（漏账第 ④ 项的收尾）。

钉四件事，**不钉条数**（未验证的条数只该随着销账减少）：

1. `docs/交接.md` 里那张 `### 未验证索引` 表能解析，编号 1..N 连续且唯一；
2. 每行四列有内容：状态只能是四选一，`怎么验证` 不许空（`已过期` 的行必须写一句结论）；
3. 正文里出现的 `#N` 必须都能在表里找到（不许悬空引用）；
4. 正文里凡是出现「未验证」的行，必须带编号（**只在这类行上认 `#N`**，因为 `#N` 同时也是踩坑档案的编号）——状态为 `待外部`/`可验证` 的行也必须在正文被引用一次，
   这样账与正文不会各说一套；反过来，没被引用的行只有 `已知边界`/`已过期` 允许存在（它们是结论，不是待办）。

判据写成纯函数 `violations()`，真文档喂一遍、假数据也喂一遍（每条规则都有反证）。
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DOC = ROOT / "docs" / "交接.md"

HEADING = "### 未验证索引"
STATUSES = ("待外部", "可验证", "已知边界", "已过期")
PENDING = ("待外部", "可验证")
ID_RE = re.compile(r"#(\d{1,3})")


def _cells(line: str) -> list[str]:
    body = line.strip().strip("|")
    return [c.strip() for c in body.split("|")]


def index_rows(text: str) -> list[dict]:
    """从整份文档里抽出索引表；表头之后连续的 `| #N |` 行才算一行。"""
    rows: list[dict] = []
    started = False
    for line in text.splitlines():
        if line.startswith(HEADING):
            started = True
            continue
        if not started:
            continue
        if not line.startswith("|"):
            if rows:
                break
            continue
        cells = _cells(line)
        if not cells or not re.fullmatch(r"#\d{1,3}", cells[0]):
            continue
        if len(cells) != 6:
            rows.append({"id": cells[0], "bad_cells": len(cells)})
            continue
        rows.append({
            "id": cells[0],
            "where": cells[1],
            "claim": cells[2],
            "why": cells[3],
            "how": cells[4],
            "status": cells[5],
        })
    return rows


def body_text(text: str) -> str:
    """表以外的正文：索引表那一段（标题 + `>` 说明 + `|` 行 + 其后的空行）整段切掉。"""
    keep: list[str] = []
    state = "pre"
    for line in text.splitlines():
        if state == "pre":
            if line.startswith(HEADING):
                state = "table"
                continue
            keep.append(line)
            continue
        if state == "table":
            if line.startswith("|") or line.startswith(">") or not line.strip():
                continue
            state = "post"
            keep.append(line)
            continue
        keep.append(line)
    return "\n".join(keep)


def violations(rows: list[dict], body: str) -> list[str]:
    bad: list[str] = []
    if not rows:
        return ["索引表一行都没解析出来（表头或格式变了？）"]

    ids = []
    for row in rows:
        if "bad_cells" in row:
            bad.append("%s 列数不对（%d 列，应为 6）" % (row["id"], row["bad_cells"]))
            continue
        try:
            ids.append(int(row["id"].lstrip("#")))
        except ValueError:
            bad.append("%s 不是 #数字" % row["id"])
            continue
        if row["status"] not in STATUSES:
            bad.append("%s 状态 %r 不在四选一里" % (row["id"], row["status"]))
        for col in ("where", "claim", "why", "how"):
            if not row[col]:
                bad.append("%s 的 %s 是空的" % (row["id"], col))
        if row["status"] == "已过期" and "结论" not in row["how"]:
            bad.append("%s 标了已过期却没写一句结论（`怎么验证` 列）" % row["id"])

    if ids:
        want = list(range(min(ids), max(ids) + 1))
        if sorted(ids) != want:
            bad.append("编号不连续或有重复：%s" % sorted(ids))
        if min(ids) != 1:
            bad.append("编号不从 1 开始：%s" % sorted(ids))

    claims = [row.get("claim", "") for row in rows if row.get("claim")]
    dup = {c for c in claims if claims.count(c) > 1}
    if dup:
        bad.append("同样的主张登记了两次：%s" % "; ".join(sorted(dup)))

    known = {int(i) for i in ids}
    cited: set[int] = set()
    for n, line in enumerate(body.splitlines(), 1):
        if "未验证" not in line:
            continue
        found = ID_RE.findall(line)
        if not found:
            bad.append("正文第 %d 行的「未验证」没有编号：%s" % (n, line.strip()[:40]))
        for raw in found:
            num = int(raw)
            cited.add(num)
            if num not in known:
                bad.append("正文第 %d 行引用了不存在的 #%d（未验证编号与踩坑档案编号不是一套）" % (n, num))

    for row in rows:
        if "bad_cells" in row:
            continue
        if row["status"] in PENDING and int(row["id"].lstrip("#")) not in cited:
            bad.append("%s（%s）是待办，却没有任何正文引用它" % (row["id"], row["status"]))
    return bad


def test_ledger_parses_and_has_no_shape_problem() -> None:
    text = DOC.read_text(encoding="utf-8")
    rows = index_rows(text)
    assert rows, " docs/交接.md 里找不到未验证索引表"
    assert violations(rows, body_text(text)) == []


def test_every_pending_row_is_reachable_from_body() -> None:
    text = DOC.read_text(encoding="utf-8")
    rows = index_rows(text)
    body = body_text(text)
    pending = [r["id"] for r in rows if r.get("status") in PENDING]
    assert pending, " 一条待办都没有，说明状态列写错了"
    for rid in pending:
        num = rid.lstrip("#")
        assert re.search(r"#%s(?!\d)" % num, body), " %s 在待办里，正文却没引用" % rid


def test_unindexed_claim_is_caught() -> None:
    rows = [{"id": "#1", "where": "x", "claim": "c", "why": "w", "how": "h", "status": "待外部"}]
    bad = violations(rows, "## 某节\n\n- 未验证：这句话没有编号。\n")
    assert any("没有编号" in b for b in bad), bad


def test_dangling_reference_is_caught() -> None:
    rows = [{"id": "#1", "where": "x", "claim": "c", "why": "w", "how": "h", "status": "待外部"}]
    bad = violations(rows, "- 未验证 #7：指向不存在的一行。\n- 未验证 #1：正常的引用。\n")
    assert any("不存在的 #7" in b for b in bad), bad


def test_orphan_pending_row_is_caught() -> None:
    rows = [
        {"id": "#1", "where": "x", "claim": "c1", "why": "w", "how": "h", "status": "待外部"},
        {"id": "#2", "where": "x", "claim": "c2", "why": "w", "how": "h", "status": "可验证"},
    ]
    bad = violations(rows, "- 未验证 #1：只引用了第一条。\n")
    assert any("#2" in b and "没有任何正文引用" in b for b in bad), bad


def test_conclusion_rows_may_stand_alone() -> None:
    rows = [{"id": "#1", "where": "x", "claim": "c", "why": "—", "how": "结论：已不需要", "status": "已过期"}]
    assert violations(rows, "## 某节\n") == []


def test_bad_status_and_missing_conclusion_are_caught() -> None:
    rows = [
        {"id": "#1", "where": "x", "claim": "c1", "why": "w", "how": "h", "status": "大概吧"},
        {"id": "#2", "where": "x", "claim": "c2", "why": "w", "how": "h", "status": "已过期"},
    ]
    bad = violations(rows, "- 未验证 #1：a\n- 未验证 #2：b\n")
    assert any("不在四选一里" in b for b in bad), bad
    assert any("却没写一句结论" in b for b in bad), bad


def test_duplicate_claim_and_gap_are_caught() -> None:
    rows = [
        {"id": "#1", "where": "x", "claim": "同一句", "why": "w", "how": "h", "status": "待外部"},
        {"id": "#3", "where": "x", "claim": "同一句", "why": "w", "how": "h", "status": "待外部"},
    ]
    bad = violations(rows, "- 未验证 #1：a\n- 未验证 #3：b\n")
    assert any("编号不连续" in b for b in bad), bad
    assert any("登记了两次" in b for b in bad), bad
