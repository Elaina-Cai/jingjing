"""WP-08 renderer 方案 Phase 1/2/3：唯一渲染 owner + 命名谓词单测。

Phase 3（签字）：direct 也走人话；策略/审计侧读 structured_reason，不再吃显示串。

Run directly:
    python tests/test_reason_text.py
"""
from __future__ import annotations

import ast
import inspect
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core import temporal  # noqa: E402
from core.routing.reason_text import (  # noqa: E402
    has_pat,
    is_pure_pat,
    render_reason_text,
)
from core.routing.turn_decision import (  # noqa: E402
    TurnDecision,
    structured_reason,
)


def test_direct_text_is_human_faces() -> bool:
    folded = TurnDecision(
        action="reply_now",
        source="direct",
        reason="patted_in_group+mentioned_in_group",
        trigger_reasons=("patted_in_group", "mentioned_in_group"),
    )
    assert render_reason_text(folded) == "有人在群里拍了拍她，同时 @ 了她"
    single = TurnDecision(action="reply_now", source="direct", reason="mentioned_in_group",
                          trigger_reasons=("mentioned_in_group",))
    assert render_reason_text(single) == "有人在群里 @ 了她"
    fallback = TurnDecision(action="reply_now", source="direct", reason="owner_private_message")
    assert render_reason_text(fallback) == "主人私聊了她"
    assert render_reason_text(None) == ""
    return True


def test_direct_matrix_connectors_and_unknown() -> bool:
    def _render(*pieces: str) -> str:
        return render_reason_text(TurnDecision(
            action="reply_now", source="direct",
            reason="+".join(pieces), trigger_reasons=tuple(pieces),
        ))

    assert _render("mentioned_in_group") == "有人在群里 @ 了她"
    assert _render("patted_in_group") == "有人在群里拍了拍她"
    assert _render("quoted_in_group") == "有人在群里引用了她"
    assert _render("patted_in_private") == "有人在私聊里拍了拍她"
    assert _render("mentioned_in_group", "content") == (
        "有人在群里 @ 了她，另外这条还带了内容"
    )
    assert _render("patted_in_group", "content") == (
        "有人在群里拍了拍她，另外这条还带了内容"
    )
    assert _render("mentioned_in_group", "patted_in_group", "content") == (
        "有人在群里 @ 了她，同时拍了拍她，另外这条还带了内容"
    )
    assert _render("conversation_active") == "对话仍在进行，她接了话"
    # 自定义规则 key / 未知 token：原样透出，不编语义、不丢线索
    assert _render("vip_rule_7") == "vip_rule_7"
    assert _render("mentioned_in_group", "vip_rule_7") == (
        "有人在群里 @ 了她，另外 vip_rule_7"
    )
    return True


def test_conversation_active_wins_over_stale_route_segment() -> bool:
    decision = TurnDecision(
        action="reply_now", source="direct",
        reason="conversation_active", trigger_reasons=("ambient",),
    )
    assert render_reason_text(decision) == "对话仍在进行，她接了话"
    return True


def test_structured_reason_stays_raw_for_policy() -> bool:
    folded = TurnDecision(
        action="reply_now", source="direct",
        reason="mentioned_in_group+patted_in_group",
        trigger_reasons=("mentioned_in_group", "patted_in_group"),
    )
    assert structured_reason(folded) == "mentioned_in_group+patted_in_group"
    assert render_reason_text(folded) != structured_reason(folded)
    single = TurnDecision(action="reply_now", source="direct", reason="owner_private_message")
    assert structured_reason(single) == "owner_private_message"
    assert structured_reason(None) == ""
    return True


def test_pat_direct_decision_declares_direct_source() -> bool:
    """拍直回必须显式 source="direct"；否则 prompt 会落回 trigger 分支（'触发了pat'）。"""
    from core.routing import turn_pipeline

    tree = ast.parse(inspect.getsource(turn_pipeline._decide_direct_route))
    found = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if name != "TurnDecision":
            continue
        kwargs = {kw.arg: kw.value for kw in node.keywords}
        reason = kwargs.get("reason")
        if reason is None or "patted_in_private" not in ast.unparse(reason):
            continue
        source = kwargs.get("source")
        if isinstance(source, ast.Constant) and source.value == "direct":
            found = True
    assert found, "拍直回 TurnDecision 必须显式 source='direct'"
    return True


def test_trigger_text_is_route_specific_human_phrase() -> bool:
    moment = datetime(2026, 10, 9, 12, 0, 0)
    elapsed = temporal.elapsed_phrase(moment)

    def _trigger(segment: str) -> str:
        decision = TurnDecision(
            action="reply_now", source="trigger",
            trigger_reasons=(segment,) if segment else (),
        )
        return render_reason_text(decision, sender_name="P6", message_time=moment)

    assert _trigger("ambient") == f"群里P6{elapsed}说的话进了环境接话"
    assert _trigger("deferred") == f"群里P6{elapsed}说的话先挂起，等状态变化"
    assert _trigger("mentioned_in_group") == f"群里P6{elapsed} @ 了她"
    assert _trigger("patted_in_group") == f"群里P6{elapsed}拍了拍她"
    assert _trigger("") == f"群里P6{elapsed}说的话没有触发她"
    legacy = TurnDecision(
        action="reply_now", source="trigger", trigger_reasons=("ambient",),
    )
    assert render_reason_text(
        legacy, sender_name="P6", message_time=moment, route_label_text="ambient",
    ) == f"群里P6{elapsed}说的话触发了ambient"
    return True


def test_named_predicates_matrix() -> bool:
    pure = TurnDecision(action="reply_now", source="direct",
                        trigger_reasons=("patted_in_group",))
    folded = TurnDecision(action="reply_now", source="direct",
                          trigger_reasons=("patted_in_group", "mentioned_in_group"))
    none = TurnDecision(action="reply_now", source="direct", trigger_reasons=())
    assert is_pure_pat(pure) is True and has_pat(pure) is True
    assert is_pure_pat(folded) is False and has_pat(folded) is True
    assert is_pure_pat(none) is False and has_pat(none) is False
    assert is_pure_pat(None) is False and has_pat(None) is False
    return True


def main() -> bool:
    tests = [
        test_direct_text_is_human_faces,
        test_direct_matrix_connectors_and_unknown,
        test_conversation_active_wins_over_stale_route_segment,
        test_structured_reason_stays_raw_for_policy,
        test_pat_direct_decision_declares_direct_source,
        test_trigger_text_is_route_specific_human_phrase,
        test_named_predicates_matrix,
    ]
    failed = 0
    for fn in tests:
        try:
            fn()
            print(f"  ok   {fn.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"  FAIL {fn.__name__}: {exc!r}")
    print(f"\nreason renderer: {len(tests) - failed}/{len(tests)} passed")
    return failed == 0


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
