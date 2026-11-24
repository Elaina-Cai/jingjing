from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.jobs import ask
from core.jobs.ingest import parse_plan

FAILS: list[str] = []
COUNT = 0


def check(name: str, cond: bool) -> None:
    global COUNT
    COUNT += 1
    if cond:
        print(f"  ok {name}")
    else:
        FAILS.append(name)
        print(f"  FAIL {name}")


def t_effects_string() -> None:
    step = type("S", (), {"action": "send:群|正文", "effects": "external_send"})()
    check("effects 字符串→单元素元组", ask.effects_of(step) == ("external_send",))
    check("后果是收不回来", "收不回来" in ask.consequence(step))


def t_ingest_infers_send() -> None:
    payload = {"plan": {"goal": "群里说一句", "steps": [{"n": 1, "action": "send:群|正文"}]}}
    step = parse_plan(payload).steps[0]
    check("缺 effects 时 send 推成 external_send", step.effects == "external_send")
    check("gui_click 推成 ui_control", __import__("core.jobs.ingest", fromlist=["x"])._infer_effects("gui_click:x") == "ui_control")
    check("认不出来算 read", __import__("core.jobs.ingest", fromlist=["x"])._infer_effects("whatever") == "read")


def t_phrase() -> None:
    step = type("S", (), {"action": "send:鲸鲸的测试群|今天下雨记得带伞", "effects": "external_send"})()
    phrase = ask.action_phrase(step)
    check("念正文", "今天下雨记得带伞" in phrase)
    check("不念竖线", "|" not in phrase)
    job = type("J", (), {"chat_id": "wxid_x"})()
    check("模板目标来自动作", "鲸鲸的测试群" in ask.template(job, step))
    check("模板里带后果", "收不回来" in ask.template(job, step))


def t_plan_hint_exists() -> None:
    """提示词常量必须真的存在：缺了会被 _plan_hint_block 的 try/except 吞掉，一个字都进不去。"""
    from core.jobs import plan_block as pb

    check("PLAN_HINT 存在", isinstance(pb.PLAN_HINT, str) and pb.PLAN_HINT.strip() != "")
    check("≤200 字", len(pb.PLAN_HINT) <= 200)
    check("教了 send: 写法", "send:" in pb.PLAN_HINT)
    check("说了要等批准", "批准" in pb.PLAN_HINT)
    check("说了不是自己发", "不是你自己发" in pb.PLAN_HINT)
    check("禁止答我发不了", "我发不了" in pb.PLAN_HINT)


def main() -> int:
    for fn in (t_effects_string, t_ingest_infers_send, t_phrase, t_plan_hint_exists):
        print(fn.__name__)
        fn()
    print(f"RESULT {len(FAILS)} failed / {COUNT} checks")
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())
