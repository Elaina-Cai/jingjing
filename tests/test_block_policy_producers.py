# -*- coding: utf-8 -*-
"""`_BLOCK_POLICY` 的产者守卫：登记的块必须真有人产，前缀不许撞车。

为什么需要这个文件（2026-10-23）：
`core/context/assembler.py::_BLOCK_POLICY` 是「她看到了什么」的账本。它有两种腐烂方式，
两种都不会报错，只会在 `context_trace` 里冒充别的块：

1. **登记了但没人产**：`("[性格与表达]", ..., "persona.traits", ...)` —— traits 早在
   `core/terminal/persona_brain.py:282-284` 就被折进 `[人格]` 了，这条登记再也匹配
   不到任何文本。预算表里多一格、trace 里少一块。
2. **前缀与真实文本不一致**：`("[要真办就附一段计划]", ...)`，而
   `core/jobs/plan_block.py::PLAN_HINT` 的真实开头是「要真办就附 <plan>…」⇒ `_classify`
   永远匹配不上，计划契约块被挂到上一块的 source 上（那类 trace 冒充 2026-09-24 实测过）。

判据只看源码里的字面量：**每个登记前缀必须在非 assembler 的生产代码里至少出现一次**。
动态拼接出块头的情况由 `_DYNAMIC_ALLOWED` 显式豁免——豁免表当前为空，是因为历史上
32 条登记全部是字面量；将来真要动态拼接，就得在这里写一行并说明理由。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.context.assembler import _BLOCK_POLICY  # noqa: E402

_REPO = Path(__file__).resolve().parent.parent
_SCAN_DIRS = ("core", "agents", "scheduler")
_SELF = "core/context/assembler.py"
# 块头由代码拼出来（不是字面量）时在这里登记，并写清为什么
_DYNAMIC_ALLOWED: dict[str, str] = {}

_SOURCES: dict[str, str] | None = None


def _sources() -> dict[str, str]:
    """相对路径 -> 源码文本（只读一次，跳过 assembler 自己）。"""
    global _SOURCES
    if _SOURCES is None:
        found: dict[str, str] = {}
        for root in _SCAN_DIRS:
            for path in (_REPO / root).rglob("*.py"):
                rel = path.relative_to(_REPO).as_posix()
                if rel == _SELF:
                    continue
                try:
                    found[rel] = path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
        _SOURCES = found
    return _SOURCES


def _producers_of(prefix: str) -> list[str]:
    return [rel for rel, text in _sources().items() if prefix in text]


def test_every_registered_prefix_has_a_producer() -> None:
    missing = []
    for prefix, name, source, _prio, _budget in _BLOCK_POLICY:
        if prefix in _DYNAMIC_ALLOWED:
            continue
        if not _producers_of(prefix):
            missing.append(f"{prefix} (source={source})")
    assert not missing, (
        "这些登记没有任何生产代码在渲染（登记了但没人产 ⇒ trace 少一块、预算表多一格）："
        + ", ".join(missing)
    )


def test_prefixes_are_unique() -> None:
    seen: dict[str, str] = {}
    dupes = []
    for prefix, _name, source, _prio, _budget in _BLOCK_POLICY:
        if prefix in seen:
            dupes.append(f"{prefix}: {seen[prefix]} vs {source}")
        seen[prefix] = source
    assert not dupes, (
        "同块头两源（trace 与预算都分不清谁是谁，必须改名或合一）：" + "; ".join(dupes)
    )


def test_dynamic_allowlist_entries_are_justified() -> None:
    bad = [p for p, why in _DYNAMIC_ALLOWED.items() if not str(why).strip()]
    assert not bad, f"动态豁免必须写理由：{bad}"


def test_plan_hint_prefix_matches_real_text() -> None:
    from core.jobs.plan_block import PLAN_HINT  # noqa: E402

    prefixes = [p for p, _n, source, _pr, _b in _BLOCK_POLICY if source == "jobs.plan_hint"]
    assert len(prefixes) == 1, f"计划契约块应恰好一条登记，实际 {prefixes}"
    assert PLAN_HINT.startswith(prefixes[0]), (
        f"登记前缀 {prefixes[0]!r} 与 PLAN_HINT 开头 {PLAN_HINT[:24]!r} 不一致"
        "（不一致 ⇒ _classify 永远匹配不上）"
    )


def test_retired_traits_block_stays_deleted() -> None:
    prefixes = [p for p, _n, _s, _pr, _b in _BLOCK_POLICY]
    retired = (
        "[性格与表达]", "[基本表达]", "[不确定与沉默]",
        "[沉默协议]", "[拿不准时]", "[安全与边界]", "[语气风格]", "[遇事怎么反应]",
    )
    still = [p for p in prefixes if p in retired]
    assert not still, (
        f"这些块 2026-10-23 已退役（四层化：人设只留身份首行，判断交给 N1）：{still}；"
        "要加回来先改 core/context/prompt_layers.py 的 RETIRED_BLOCKS 并说明理由"
    )
    assert "[指代事实]" in prefixes and "[当前局面]" in prefixes


def test_world_window_took_over_the_read_view_header() -> None:
    prefixes = [p for p, _n, _s, _pr, _b in _BLOCK_POLICY]
    # 2026-10-23 四层化：[读场]（N1 的 semantic_reads）与 [本轮理解] 一起退役，
    # system 侧不再有这两个块；世界模型投影那块叫 [世界窗口]。
    assert prefixes.count("[读场]") == 0, "读场块已退役，别再登记"
    assert prefixes.count("[本轮理解]") == 0, "判词块已退役，别再登记"
    assert "[世界窗口]" in prefixes


if __name__ == "__main__":
    for _p in (str(Path(__file__).resolve().parent.parent), str(Path(__file__).resolve().parent)):
        if _p not in sys.path:
            sys.path.insert(0, _p)
    from _mini_pytest import run_all as _run_all

    sys.exit(0 if _run_all(globals()) else 1)
