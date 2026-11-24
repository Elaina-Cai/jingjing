"""Phase 2: budget policy (priority, truncate/drop, user-prompt caps).

Run directly:
    python tests/test_context_budget.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# 本文件测的是「预算开着」时的行为，必须自己钉住开关，不能继承同进程里
# 别的用例留下的值——历史上 test_context_assembler_equivalence 会在导入期
# 把 YINYUE_FEATURE_CONTEXT_BUDGET 置 0 且（在只跑用例函数的 runner 下）
# 不还原，导致这里的断言莫名失败。
os.environ["YINYUE_FEATURE_CONTEXT_BUDGET"] = "1"

from core.context import assembler, user_prompt  # noqa: E402
from core.context.budget import (  # noqa: E402
    AUX,
    HISTORY,
    MEMORY,
    PERSONA,
    RELATION,
    apply_budget,
)
from core.context.contracts import ContextBlock  # noqa: E402


def test_persona_kept_aux_dropped_memory_trimmed() -> None:
    blocks = [
        ContextBlock("[人格]", "[人格]\n" + "人" * 5000, "persona.identity", PERSONA),
        ContextBlock("[事件记忆]", "[事件记忆]\n" + "记" * 3000, "memory.gateway", MEMORY),
        ContextBlock("[主动触达规则]", "[主动触达规则]\n" + "规" * 1000, "context.proactive", AUX),
    ]
    bundle, decisions = apply_budget(blocks)
    by_name = {block.name: block for block in bundle.blocks}
    assert "[人格]" in by_name and by_name["[人格]"].chars > 5000  # never truncated
    assert "[事件记忆]" in by_name and by_name["[事件记忆]"].chars <= 1500
    assert "[主动触达规则]" not in by_name  # helper block dropped entirely
    actions = {d.name: d.action for d in decisions}
    assert actions["[人格]"] == "full"
    assert actions["[事件记忆]"] == "truncated"
    assert actions["[主动触达规则]"] == "dropped"
    print("  ok   persona kept / memory trimmed / aux dropped")


def test_history_keeps_newest_lines() -> None:
    lines = [f"第{i}句" for i in range(400)]  # ~2.2k chars total, over budget
    block = ContextBlock(
        "[群聊最近消息]",
        "[群聊最近消息]\n" + "\n".join(lines),
        "session.group_recent",
        HISTORY,
    )
    bundle, decisions = apply_budget([block])
    text = bundle.blocks[0].content
    assert "第399句" in text and "第0句" not in text
    assert bundle.blocks[0].chars <= 1200
    assert decisions[0].action == "truncated"
    print("  ok   history trims from the oldest side")


def test_under_budget_is_noop() -> None:
    blocks = [
        ContextBlock("[人格]", "[人格]\n短", "persona.identity", PERSONA),
        ContextBlock("[关系事实]", "[关系事实]\n- 关系层级: 主人", "principal.relation", RELATION),
    ]
    bundle, decisions = apply_budget(blocks)
    assert bundle.render() == "\n\n".join(b.content for b in blocks)
    assert all(d.action == "full" for d in decisions)
    print("  ok   under-budget blocks pass through unchanged")


def test_user_prompt_caps() -> None:
    assert user_prompt.cap_head("x" * 100, 10, force=True) == "x" * 10
    assert user_prompt.cap_or_drop("y" * 100, 10, force=True) == ""
    assert user_prompt.cap_or_drop("short", 10, force=True) == "short"

    lines = "\n".join(f"第{i}句" for i in range(300))
    kept = user_prompt.cap_tail_lines(lines, 50, force=True)
    assert "第299句" in kept and "第0句" not in kept

    # 注意：time_affect_prefix 返回的是块列表；把整列表当字符串判断会漏检，
    # 必须逐块 join 后再断言（历史上这里因类型误用而静默通过）。
    parts = user_prompt.time_affect_prefix(
        message_time=None, affect_block="情" * 500, now_value=None
    )
    assert "情" * 500 not in "\n".join(parts)  # over-budget affect dropped

    group = user_prompt.group_blocks(
        is_group=True, group_recent_context="\n".join(f"第{i}句" for i in range(500))
    )
    assert group and "第0句" not in group[0]
    print("  ok   user-prompt caps (head/tail/drop)")


def test_assembler_applies_budget() -> None:
    from core.terminal import persona_brain

    huge = [
        "[人格]\n" + "人" * 5000,
        "[主动触达规则]\n" + "规" * 1000,
        "[事件记忆]\n" + "记" * 3000,
    ]
    original = persona_brain.build_persona_blocks
    persona_brain.build_persona_blocks = lambda *args, **kwargs: huge
    try:
        off = assembler.build_bundle(None, apply=False)
        assert {b.name for b in off.blocks} == {"[人格]", "[主动触达规则]", "[事件记忆]"}

        on = assembler.build_bundle(None, apply=True)
        names = {b.name: b for b in on.blocks}
        assert "[主动触达规则]" not in names
        assert names["[事件记忆]"].chars <= 1500
        assert names["[人格]"].chars > 5000
    finally:
        persona_brain.build_persona_blocks = original
    print("  ok   assembler applies the budget when enabled")


def main() -> bool:
    tests = [
        test_persona_kept_aux_dropped_memory_trimmed,
        test_history_keeps_newest_lines,
        test_under_budget_is_noop,
        test_user_prompt_caps,
        test_assembler_applies_budget,
    ]
    failed: list[str] = []
    for test in tests:
        try:
            test()
        except Exception as exc:  # noqa: BLE001
            failed.append(test.__name__)
            print(f"  FAIL {test.__name__}: {exc!r}")
    print(f"context budget checks: {len(tests) - len(failed)}/{len(tests)} passed")
    return not failed


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
