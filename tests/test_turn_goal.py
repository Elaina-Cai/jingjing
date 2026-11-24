# -*- coding: utf-8 -*-
"""回合级目标（TurnGoal）+ 回合笔记（2026-10-23 Step 2c）。

守的是什么
==========
1. 目标是一个**可读的结构**：kind / about / to_whom / criteria，不是一句放在
   prompt 里的自然语言；kind 认不出折成 none（猜错种类会让"该办的事"被当成闲聊答过去）；
2. 目标坏形状**不许让整轮 fail-open**：perceive 的 goal 解析失败只等于"没有目标"，
   route 的合法性仍然独立判断；
3. 回合结束写的是**笔记**（role=note），不是她说过的话；历史渲染必须分开标；
4. 只写结构与一行摘要，不把整段思考搬进下一次 prompt。
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agents.perceive import parse_perceive_args, render_card, render_receipt  # noqa: E402
from agents.turn_session import (  # noqa: E402
    GOAL_KINDS,
    TurnGoal,
    TurnSession,
    render_turn_note,
)

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


def test_goal_normalization() -> None:
    check("词表", GOAL_KINDS == ("answer", "do", "both", "none"))
    g = TurnGoal(kind="DO", about="  查一下  明天天气 ", criteria="给出温度")
    check("kind 小写归一", g.kind == "do", g.kind)
    check("空白折叠", g.about == "查一下 明天天气", g.about)
    check("真目标", g.is_real)
    check("认不出的 kind -> none", TurnGoal(kind="chat", about="x").kind == "none")
    empty = TurnGoal(kind="do", about="   ")
    check("空 about -> none", empty.kind == "none")
    check("空 about 不算真目标", not empty.is_real)
    long_about = TurnGoal(kind="do", about="啊" * 400)
    check("about 截断", len(long_about.about) <= 200, str(len(long_about.about)))


def test_goal_from_payload() -> None:
    check("非 dict -> None", TurnGoal.from_payload("x") is None)
    check("缺 about -> None", TurnGoal.from_payload({"kind": "do"}) is None)
    g = TurnGoal.from_payload({"kind": "both", "about": "边说边办", "to": "wxid_a"})
    check("to 可当 to_whom", g is not None and g.to_whom == "wxid_a", str(g))
    check("as_dict 四字段", set((g.as_dict() if g else {})) == {"kind", "about", "to_whom", "criteria"})


def test_goal_line() -> None:
    g = TurnGoal(kind="do", about="查天气", criteria="给出温度")
    line = g.render_line()
    check("行里有种类", "办" in line, line)
    check("行里有目标", "查天气" in line)
    check("行里有判据", "给出温度" in line)
    check("没有判据就不写括号", "算成" not in TurnGoal(kind="answer", about="答一句").render_line())


def test_turn_note_render() -> None:
    g = TurnGoal(kind="both", about="约明天的局", criteria="时间地点定下来")
    note = render_turn_note(g, route="speak", addressee="her", outcome="答了", why="她点名问我")
    check("有前缀", note.startswith("〔这轮〕"), note)
    for token in ("想：约明天的局", "算成：时间地点定下来", "判：speak", "对：her", "因为：她点名问我", "结果：答了"):
        check(f"含 {token}", token in note, note)
    check("没目标也能写判与果", "想：" not in render_turn_note(None, route="skip", outcome="没说"))
    check("什么都没有 -> 空", render_turn_note(None) == "")
    check("why 截断", len(render_turn_note(None, why="啊" * 300)) < 120)


def test_perceive_parses_goal() -> None:
    r = parse_perceive_args({
        "route": "speak", "addressee": "her", "why": "问我",
        "goal": {"kind": "do", "about": "查天气", "criteria": "给出温度"},
    })
    check("goal 解析出来", r.goal is not None and r.goal.kind == "do", str(r.goal))
    bad = parse_perceive_args({
        "route": "skip", "addressee": "other", "why": "他们在聊",
        "goal": {"kind": "do"},  # 缺 about
    })
    check("坏 goal 折成 None", bad.goal is None)
    check("坏 goal 不影响 route", bad.route == "skip")
    try:
        parse_perceive_args({"route": "nope", "addressee": "her", "why": "x"})
        raised = False
    except ValueError:
        raised = True
    check("route 非法仍然抛（行为开关不猜）", raised)


def test_goal_shows_in_card_and_receipt() -> None:
    r = parse_perceive_args({
        "route": "speak", "addressee": "her", "why": "问我",
        "goal": {"kind": "do", "about": "查天气"},
    })
    check("卡片有目标行", "goal: 办 · 查天气" in render_card(r), render_card(r))
    check("回执有目标行", "goal: 办 · 查天气" in render_receipt(r))
    check("回执有收口指引", "按它收口" in render_receipt(r))
    plain = parse_perceive_args({"route": "speak", "addressee": "her", "why": "闲聊"})
    check("没目标时卡片不出现 goal", "goal:" not in render_card(plain))
    check("没目标时回执不出现 goal", "goal:" not in render_receipt(plain))


def test_session_holds_goal() -> None:
    s = TurnSession(system="S", current="c")
    check("默认没有目标", s.goal is None)
    g = TurnGoal(kind="answer", about="答清楚")
    s.set_goal(g)
    check("写进会话", s.goal is g)
    snap = s.snapshot()
    check("快照带目标", snap.get("goal", {}).get("about") == "答清楚", str(snap.get("goal")))
    s.set_goal("不是目标对象")
    check("非 TurnGoal 当没有", s.goal is None)


def test_history_renders_note_as_note() -> None:
    from agents.conversational import ConversationalAgent

    fake_self = SimpleNamespace(persona=SimpleNamespace(name="鲸鲸"))
    rows = [
        SimpleNamespace(role="user", content="在吗", created_at=None, chat_id="", media_path=""),
        SimpleNamespace(role="note", content="〔这轮〕想：查天气", created_at=None, chat_id="", media_path=""),
        SimpleNamespace(role="assistant", content="在", created_at=None, chat_id="", media_path=""),
    ]
    text = ConversationalAgent._format_history(fake_self, rows)
    check("笔记单独标", "[你自己记的·不是说过的话]" in text, text)
    check("笔记不冒充她", text.count("鲸鲸") == 1, text)


def _src(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def test_wiring() -> None:
    conv = _src("agents/conversational.py")
    check("perceive 后写目标", "session.set_goal(getattr(result, \"goal\", None))" in conv)
    check("回合末写笔记", 'role="note"' in conv)
    check("用纯函数渲染笔记", "render_turn_note(" in conv)
    check("历史单独渲染 note", "你自己记的·不是说过的话" in conv)
    per = _src("agents/perceive.py")
    check("schema 有 goal", '"goal": {' in per)
    check("parse 里有 TurnGoal", "TurnGoal.from_payload" in per)


def main() -> int:
    for fn in (
        test_goal_normalization,
        test_goal_from_payload,
        test_goal_line,
        test_turn_note_render,
        test_perceive_parses_goal,
        test_goal_shows_in_card_and_receipt,
        test_session_holds_goal,
        test_history_renders_note_as_note,
        test_wiring,
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