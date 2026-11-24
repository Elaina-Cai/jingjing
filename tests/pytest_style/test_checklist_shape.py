"""清单（`docs/该抄的清单.md`）的形状锁。

为什么要有这把锁（2026-10-29）：
1. 给 P2 分区加标题时按行号插入，标题落在了 `BR-069` 的「理由」和「状态」之间——
   渲染出来像两条目，实际是把一条目的状态挂到了分区标题下；
2. `PIT-048` 从头到尾没有状态行，肉眼扫 27 条完全看不出来。
两件都是「不报错的错」：文档照常渲染，读者照常读，账目已经错了。

判据三条：每条自成一块 / 状态行不能被 `###` 劈开 / 分区标题里的条数必须等于实际条数。
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DOC = ROOT / "docs" / "该抄的清单.md"
ENTRY = re.compile(r"^- \*\*`([A-Z]{2,5}-\d{3})`\*\*")
STATUS = "  - 状态："
KINDS = ("已完成", "未开工", "不适用", "部分完成")
SECTION = re.compile(r"^### (P\d) · .*?（(\d+) 条）")
TOTAL = re.compile(r"^## 2\. 优先级（(\d+) 条")


def _segments(lines: list[str]) -> list[tuple[int, str]]:
    """返回 [(条目所在行下标, 条目 id)]。"""
    return [(i, m.group(1)) for i, l in enumerate(lines) if (m := ENTRY.match(l))]


def violations(text: str) -> list[str]:
    """清单形状的全部问题；空列表 = 形状是对的。"""
    lines = text.splitlines()
    entries = _segments(lines)
    out: list[str] = []
    if len(entries) < 20:
        return ["清单只解析出 %d 条，判据要求 >= 20（解析器坏了？）" % len(entries)]
    for k, (i, eid) in enumerate(entries):
        end = len(lines)
        for j in range(i + 1, len(lines)):
            if ENTRY.match(lines[j]) or lines[j].startswith("## "):
                end = j
                break
        seg = lines[i:end]
        status = [j for j, l in enumerate(seg) if l.startswith(STATUS)]
        heads = [j for j, l in enumerate(seg) if l.startswith("###")]
        if len(status) != 1:
            out.append("%s: 状态行 %d 条（必须恰好 1 条）" % (eid, len(status)))
            continue
        if heads and heads[0] < status[0]:
            out.append("%s: 分区标题插在条目和它的状态行之间（第 %d 行）" % (eid, i + heads[0] + 1))
        body = seg[status[0]]
        kind = body[len(STATUS):].split("**")[1] if "**" in body else ""
        if kind not in KINDS:
            out.append("%s: 状态取值 %r 不在 %s 里" % (eid, kind, "/".join(KINDS)))
    # 分区标题里的条数必须等于实际条数
    counted: dict[str, int] = {}
    for i, l in enumerate(lines):
        m = SECTION.match(l)
        if not m:
            continue
        p, n = m.group(1), int(m.group(2))
        real = 0
        for j in range(i + 1, len(lines)):
            if lines[j].startswith("###") or lines[j].startswith("## "):
                break
            if ENTRY.match(lines[j]):
                real += 1
        counted[p] = real
        if real != n:
            out.append("%s 写着 %d 条，实际 %d 条" % (p, n, real))
    for i, l in enumerate(lines):
        m = TOTAL.match(l)
        if m:
            claimed, real = int(m.group(1)), len(entries)
            if claimed != real:
                out.append("总标题写着 %d 条，实际 %d 条" % (claimed, real))
            break
    return out


def test_real_checklist_shape() -> None:
    assert violations(DOC.read_text(encoding="utf-8")) == []


def test_missing_status_is_caught() -> None:
    broken = DOC.read_text(encoding="utf-8").replace(STATUS + "**已完成**", "  - 备注：**已完成**", 1)
    assert any("状态行 0 条" in m or "状态行 2 条" in m for m in violations(broken))


def test_header_inside_entry_is_caught() -> None:
    text = DOC.read_text(encoding="utf-8")
    lines = text.splitlines()
    i = next(j for j, l in enumerate(lines) if l.startswith(STATUS))
    lines.insert(i, "### P9 · 塞进来的标题（1 条）")
    assert any("插在条目和它的状态行之间" in m for m in violations("\n".join(lines)))


def test_unknown_status_kind_is_caught() -> None:
    # 变异必须打在「条目状态行」上：文档开头图例里也有 **已完成**，改那处等于没改（2026-10-29 踩过）
    broken = DOC.read_text(encoding="utf-8").replace(STATUS + "**已完成**", STATUS + "**差不多完成了**", 1)
    assert any("不在 已完成/未开工" in m for m in violations(broken))


def test_section_count_mismatch_is_caught() -> None:
    broken = DOC.read_text(encoding="utf-8").replace("### P2 · 记着，别忘（7 条）", "### P2 · 记着，别忘（9 条）", 1)
    assert any("P2 写着 9 条，实际 7 条" in m for m in violations(broken))


def test_total_count_mismatch_is_caught() -> None:
    broken = DOC.read_text(encoding="utf-8").replace("## 2. 优先级（27 条", "## 2. 优先级（30 条", 1)
    assert any("总标题写着 30 条，实际 27 条" in m for m in violations(broken))
