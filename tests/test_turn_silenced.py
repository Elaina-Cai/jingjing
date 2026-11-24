# -*- coding: utf-8 -*-
"""沉默台账：一条消息没回，必须能回答"为什么没回"（2026-10-23）。

守的是什么
==========
1. ``cause`` 是封闭词表，判词原文另存 ``reason``——不认识的原因归 other 但**不许丢原文**；
2. ``note_silenced`` 只记账：返回值恒 None，日志抛异常也不许把主流程带下去；
3. 接线是静态的：路由（route / trigger）、调度派发、噪音闸门、perceive、[SILENT]、
   工具循环没出稿，六处都必须真的在记——否则"没回"又回到靠猜。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.turn import silence  # noqa: E402

FAILS: list[str] = []
COUNT = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global COUNT
    COUNT += 1
    if cond:
        print(f"  ok   {name}")
    else:
        FAILS.append(f"{name} {detail}")
        print(f"  FAIL {name} {detail}")


class _FakeLog:
    def __init__(self, *, boom: bool = False) -> None:
        self.rows: list[tuple[str, dict]] = []
        self.boom = boom

    def info(self, event: str, **kwargs) -> None:
        if self.boom:
            raise RuntimeError("log down")
        self.rows.append((event, kwargs))


def test_cause_vocabulary() -> None:
    causes = list(silence.SILENT_CAUSES)
    check("词表非空", len(causes) >= 10, str(len(causes)))
    check("词表无重复", len(causes) == len(set(causes)))
    check("词表都是小写蛇形", all(c == c.lower() and " " not in c for c in causes))
    check("other 在词表里（兜底必须有名）", "other" in causes)


def test_cause_of_folds_decision_reasons() -> None:
    check("拍别人", silence.cause_of("pat_not_self") == "pat_other")
    check("群纯拍", silence.cause_of("patted_in_group") == "pat_grace_group")
    check("自己发的", silence.cause_of("self_sent") == "self_sent")
    check("never -> 模式", silence.cause_of("never") == "no_reply_mode")
    check("带参数判词按冒号前折", silence.cause_of("unknown_reply_mode:'mentions'") == "no_reply_mode")
    check("词表内原样", silence.cause_of("noise_gate") == "noise_gate")
    check("空 -> other", silence.cause_of("") == "other")
    check("认不出 -> other", silence.cause_of("something_new") == "other")


def test_note_is_log_only_and_keeps_raw_reason() -> None:
    log = _FakeLog()
    out = silence.note_silenced(
        "pat_not_self", chat_id="c1", msg_id="m1", why="拍的是别人",
        is_group=True, where="route", log=log,
    )
    check("返回值恒 None", out is None)
    check("只记一条", len(log.rows) == 1, str(log.rows))
    event, fields = log.rows[0]
    check("事件名", event == "turn_silenced", event)
    check("cause 已折叠", fields.get("cause") == "pat_other", str(fields))
    check("原文留在 reason", fields.get("reason") == "pat_not_self", str(fields))
    check("chat/msg 带上", fields.get("chat_id") == "c1" and fields.get("msg_id") == "m1")
    check("where 带上", fields.get("where") == "route")
    check("is_group 带上", fields.get("is_group") is True)
    check("why 截到 200 以内", len(str(fields.get("why") or "")) <= 200)


def test_unknown_cause_keeps_text() -> None:
    log = _FakeLog()
    silence.note_silenced("brand_new_reason", log=log)
    fields = log.rows[0][1]
    check("认不出 -> other", fields.get("cause") == "other", str(fields))
    check("但原文没丢", fields.get("reason") == "brand_new_reason", str(fields))


def test_log_failure_never_raises() -> None:
    try:
        silence.note_silenced("group_buffered", log=_FakeLog(boom=True))
        ok = True
    except Exception as exc:  # noqa: BLE001
        ok = False
        print("    raised", repr(exc))
    check("日志炸了也不抛", ok)


def _src(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def test_wiring_is_static_and_complete() -> None:
    router = _src("core/routing/router.py")
    check("路由有台账助手", "def _ledger_silence(" in router)
    check("_route_one 记账", "_ledger_silence(decision, msg, where=\"route\")" in router)
    check("trigger 记账", 'where="trigger"' in router)
    sched = _src("scheduler/main.py")
    check("派发侧记账", "from core.turn.silence import note_silenced" in sched)
    pipe = _src("core/routing/turn_pipeline.py")
    check("噪音闸门记账", '"noise_gate"' in pipe and "note_silenced(" in pipe)
    conv = _src("agents/conversational.py")
    for cause in ("perceive_skip", "perceive_defer", "silent_token", "no_reply_text"):
        check(f"回合侧记 {cause}", f'"{cause}"' in conv)


def main() -> int:
    for fn in (
        test_cause_vocabulary,
        test_cause_of_folds_decision_reasons,
        test_note_is_log_only_and_keeps_raw_reason,
        test_unknown_cause_keeps_text,
        test_log_failure_never_raises,
        test_wiring_is_static_and_complete,
    ):
        print(fn.__name__)
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            import traceback

            traceback.print_exc()
            FAILS.append("%s raised %r" % (fn.__name__, exc))
    print("RESULT %d failed / %d checks" % (len(FAILS), COUNT))
    for item in FAILS:
        print("  - " + item)
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())