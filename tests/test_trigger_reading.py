"""`core.routing.trigger_reading`（R2 S1）：触发原因的搬运是纯函数。

守两件事：
1. **搬运不改判据**——reason / summons 从载荷里逐字取，缺字段给空值，不在这里重算任何布尔；
2. 折挂起的轻拍失败时退回现状（审计字段不该把回合炸掉）。

续写读数（`ContinuationReading` / `continuation_reading_of`）随回合归属整族于 2026-10-23 删除。
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.routing import trigger_reading as TR  # noqa: E402
from core.routing.turn_decision import ROUTE_SEGMENT_MENTION  # noqa: E402

FAILED = []


def check(label, ok, detail=""):
    print(("  OK   " if ok else "  FAIL ") + label)
    if not ok:
        FAILED.append(label)
        if detail:
            print("        " + str(detail))


class _State:
    """只记「有没有被要求折挂起的轻拍」以及返回什么。"""

    def __init__(self, folded=None):
        self.calls = []
        self.folded = folded

    def take_pending_pats(self, reason):
        self.calls.append(reason)
        if isinstance(self.folded, Exception):
            raise self.folded
        return self.folded if self.folded is not None else reason


def q(**kw):
    return SimpleNamespace(**kw)


def t_summons_of():
    check("缺字段给空元组", TR.summons_of(q()) == ())
    check("list 也转成元组", TR.summons_of(q(summons=["pat", "name"])) == ("pat", "name"))
    check("None 不算「有呼唤形式」", TR.summons_of(q(summons=None)) == ())


def t_trigger_reasons_prefers_unit():
    state = _State()
    reason, reasons = TR.trigger_reasons_of(
        SimpleNamespace(reason="mentioned_in_group"),
        q(trigger_reason="patted_in_group+content", summons=("patted_in_group",)),
        ROUTE_SEGMENT_MENTION,
        state,
        unit_summons=("patted_in_group",),
    )
    check("单元口径优先于路由段", reason == "patted_in_group+content", reason)
    check("复合形态拆进元组", reasons == ("patted_in_group", "content"), reasons)
    check("单元自带呼唤形式时不再折老路", state.calls == [], state.calls)


def t_trigger_reasons_folds_pending_pats():
    state = _State(folded="mentioned_in_group+patted_in_group")
    reason, reasons = TR.trigger_reasons_of(
        SimpleNamespace(reason="mentioned_in_group"),
        q(summons=()),
        ROUTE_SEGMENT_MENTION,
        state,
        unit_summons=(),
    )
    check("点名且单元无呼唤形式 → 折挂起的轻拍", reason.endswith("+patted_in_group"), reason)
    check("折过之后同样拆开", reasons == ("mentioned_in_group", "patted_in_group"), reasons)
    check("折一次就够", len(state.calls) == 1, state.calls)


def t_trigger_reasons_never_folds_ambient():
    state = _State(folded="ambient+patted_in_group")
    reason, reasons = TR.trigger_reasons_of(
        SimpleNamespace(reason="ambient"), q(summons=()), "ambient", state,
        unit_summons=(),
    )
    check("非点名路径不去折", state.calls == [], state.calls)
    check("也就不可能出现复合形态", (reason, reasons) == ("ambient", ("ambient",)), (reason, reasons))


def t_trigger_reasons_survives_folding_error():
    state = _State(folded=RuntimeError("世界模型读不到"))
    reason, reasons = TR.trigger_reasons_of(
        SimpleNamespace(reason="mentioned_in_group"), q(summons=()), ROUTE_SEGMENT_MENTION, state,
        unit_summons=(),
    )
    check("折不动就退回现状（审计字段不该把回合炸掉）", reason == "mentioned_in_group", reason)
    check("元组与字符串口径一致", reasons == ("mentioned_in_group",), reasons)


def t_module_is_pure():
    src = (ROOT / "core" / "routing" / "trigger_reading.py").read_text(encoding="utf-8")
    for bad in ("sqlite3", "open(", "llm.chat", "structlog"):
        check(f"纯函数层不出现 {bad!r}", bad not in src)


TESTS = [
    t_summons_of,
    t_trigger_reasons_prefers_unit,
    t_trigger_reasons_folds_pending_pats,
    t_trigger_reasons_never_folds_ambient,
    t_trigger_reasons_survives_folding_error,
    t_module_is_pure,
]


def main():
    for fn in TESTS:
        print("\n" + fn.__name__)
        fn()
    print()
    if FAILED:
        print("trigger reading: %d FAILED" % len(FAILED))
        for name in FAILED:
            print("  - " + name)
        return 1
    print("trigger reading checks: all passed (%d)" % len(TESTS))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
