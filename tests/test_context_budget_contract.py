"""块预算契约（2026-09-24）：截断策略一处声明 + 截断/丢弃必须留证据 + 未登记块不静默顶名。

背景（两次事故同源）：
* [时间感知] 曾因 priority=AUX 被"整块丢"的隐式规则裁掉（从双份变零份）；
* 名单块曾会被 RELATION 的"保头截断"静默砍尾（所以它现在自声明规模 + 自封顶）。
两件事都源于「处理方式藏在数字比较里」。本文件锁三样东西：
1) 优先级 → 截断策略的显式契约表；
2) 每条 truncated/dropped 都带 omitted_chars（证据），并落日志；
3) 未登记的块不再沿用上一块的 name/source（曾出现块名顶错的事故），
   也不再会被静默丢弃。

Run directly:
    python tests/test_context_budget_contract.py
"""

from __future__ import annotations

import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ["YINYUE_FEATURE_CONTEXT_BUDGET"] = "1"

from core.context import assembler  # noqa: E402
from core.context.budget import (  # noqa: E402
    AUX,
    CUT_DROP,
    CUT_HEAD,
    CUT_NONE,
    CUT_POLICY,
    CUT_TAIL,
    HISTORY,
    MEMORY,
    PERSONA,
    RELATION,
    apply_budget,
    cut_strategy,
)
from core.context.contracts import ContextBlock  # noqa: E402

ROOT = os.path.join(os.path.dirname(__file__), "..")


def test_cut_policy_is_one_explicit_table() -> None:
    assert CUT_POLICY == {
        PERSONA: CUT_NONE,
        RELATION: CUT_HEAD,
        MEMORY: CUT_HEAD,
        HISTORY: CUT_TAIL,
        AUX: CUT_DROP,
    }, CUT_POLICY
    # 契约外的优先级：保守截头，不静默丢整块
    assert cut_strategy(99) == CUT_HEAD


def test_truncation_carries_omitted_evidence() -> None:
    block = ContextBlock("[事件记忆]", "[事件记忆]\n" + "记" * 3000, "memory.gateway", MEMORY)
    bundle, decisions = apply_budget([block])
    d = decisions[0]
    assert d.action == "truncated", d
    assert d.omitted_chars > 0 and d.omitted_chars == d.chars_before - d.chars_after, d
    assert bundle.blocks[0].chars == d.chars_after, (bundle.blocks[0].chars, d)
    assert bundle.blocks[0].content.startswith("[事件记忆]")  # 头保住


def test_drop_carries_full_omission() -> None:
    block = ContextBlock("[主动触达规则]", "[主动触达规则]\n" + "规" * 1000, "context.proactive", AUX)
    bundle, decisions = apply_budget([block])
    assert bundle.blocks == (), bundle.blocks
    d = decisions[0]
    assert d.action == "dropped" and d.omitted_chars == d.chars_before > 0, d


def test_persona_is_never_cut_even_with_total_budget() -> None:
    persona = ContextBlock("[人格]", "[人格]\n" + "人" * 5000, "persona.identity", PERSONA)
    aux = ContextBlock("[主动触达规则]", "[主动触达规则]\n" + "规" * 1000, "context.proactive", AUX)
    bundle, decisions = apply_budget([persona, aux], total_budget=100)
    names = [b.name for b in bundle.blocks]
    assert "[人格]" in names, names  # PERSONA 永不裁
    assert "[主动触达规则]" not in names  # total 超限时 AUX 先丢
    assert any(d.action == "dropped" for d in decisions)


def test_undeclared_block_is_marked_not_inherited() -> None:
    blocks = assembler._classify(
        [
            "[指代事实]\n说话人=张三",
            "[没登记的块头]\n一段新内容",
            "续行（没有方括号）",
        ]
    )
    assert blocks[0].source == "context.pronoun_facts", blocks[0]
    assert blocks[1].source == "undeclared", blocks[1]
    assert blocks[1].name.startswith("[没登记的块头]"), blocks[1]
    assert blocks[1].priority == PERSONA, "未登记块按 PERSONA 处理，不许被静默丢"
    # 续行仍继承它所属的块
    assert blocks[2].source == "undeclared", blocks[2]


def test_every_rendered_header_is_declared() -> None:
    """源码级守卫：persona_brain 里渲染的每个 [xxx] 块头都必须登记进 _BLOCK_POLICY。"""
    src = open(
        os.path.join(ROOT, "core", "terminal", "persona_brain.py"), encoding="utf-8"
    ).read()
    headers = set(re.findall(r'"(\[[^\]\n"]{1,24}\])', src))
    assert headers, "没从 persona_brain 里扫到块头，守卫需要更新"
    prefixes = [entry[0] for entry in assembler._BLOCK_POLICY]
    missing = [
        h for h in sorted(headers) if not any(h.startswith(p) for p in prefixes)
    ]
    assert not missing, f"persona_brain 渲染了未登记的块（会被标 undeclared）：{missing}"


def main() -> int:
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    failed = 0
    for test in tests:
        try:
            test()
            print(f"PASS {test.__name__}")
        except AssertionError as exc:
            failed += 1
            print(f"FAIL {test.__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
