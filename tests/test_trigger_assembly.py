# -*- coding: utf-8 -*-
"""触发路径的装配点（R2 的 S4）：``_trigger_agent_context`` 只做装配。

这一层是 19 个 extra 键的唯一落点，所以锁两件事：

1. **键集合**：``TriggerAssembly`` 装配出的 ``extra`` 必须恰好是那 16 个基础键；
   少一个生成层就静默读不到（``pronoun_rows`` / ``room_situation_block`` /
   ``referent`` 这三个最容易在搬运时漏）。
   2026-10-23 深夜：``semantic_reads`` / ``turn_understanding`` 随读场块（N1 语义读数）
   退役，装配点不再产它们，基础键 18 → 16。
2. **只装配**：函数体不许碰 llm / 数据库 / 窗口之外的东西，也不许判受话
   —— 受话与回不回都在上游（``decide_turn`` / ``model_addressee_veto``）。

Run directly: python tests/test_trigger_assembly.py
"""
from __future__ import annotations

import inspect
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.routing import turn_pipeline as tp  # noqa: E402

FAILS: list[str] = []
COUNT = [0]


def check(label: str, cond: bool, detail: str = "") -> None:
    COUNT[0] += 1
    if cond:
        print(f"  ok  {label}")
    else:
        FAILS.append(label if not detail else f"{label} :: {detail}")
        print(f"  --  {label} :: {detail}")


#: S4 装配出来就该有这些基础键（媒体/续写/计划是回合后续加的，不在这里）。
BASE_KEYS = {
    "user_message", "platform", "account_id", "chat_id", "sender_id",
    "sender_name", "is_group", "session_id", "reply_account",
    "group_recent_context", "window_turns", "pronoun_rows",
    "room_situation_block",
    "message_time", "world_state", "referent",
}


def _question() -> SimpleNamespace:
    return SimpleNamespace(
        content="青协招新你想去吗",
        sender_id="wxid_a",
        sender_name="青",
        msg_id="m1",
        created_at="2026-10-22 19:00:00",
    )


def _world() -> SimpleNamespace:
    return SimpleNamespace(
        turns=[{"who": "青", "text": "青协招新你想去吗"}],
        rows_data=[{"rowid": 7}],
        window_hash="wh",
        window_full_hash="wfh",
        rows_hash="rh",
        as_dict=lambda: {"turns": 1, "latest": "青"},
    )


def _assembly(**over) -> "tp.TriggerAssembly":
    kw = dict(
        question=_question(),
        turn_world=_world(),
        chat_id="123@chatroom",
        group_platform="wechat",
        group_account="self",
        session_id="s1",
        group_context="青: 青协招新你想去吗",
        room_block="【群里现在】…",
        trigger_reasons=("mention",),
    )
    kw.update(over)
    return tp.TriggerAssembly(**kw)


def t_extra_has_exactly_the_base_keys() -> None:
    ctx = tp._trigger_agent_context(_assembly())
    keys = set(ctx.extra)
    check("基础键一个不少", BASE_KEYS <= keys, str(sorted(BASE_KEYS - keys)))
    extra_only = keys - BASE_KEYS
    check(
        "装配点不自己加别的键（媒体/续写由回合写）",
        not extra_only,
        str(sorted(extra_only)),
    )


def t_values_come_from_the_assembly() -> None:
    asm = _assembly()
    ctx = tp._trigger_agent_context(asm)
    check("chat_id 逐字", ctx.extra["chat_id"] == asm.chat_id, str(ctx.extra["chat_id"]))
    check("sender 逐字", ctx.extra["sender_name"] == "青")
    check("平台/账号逐字", (ctx.extra["platform"], ctx.extra["account_id"]) == ("wechat", "self"))
    check("窗口文本逐字", ctx.extra["group_recent_context"] == asm.group_context)
    check("局势块逐字", ctx.extra["room_situation_block"] == asm.room_block)
    # 负锁：读场块退役后，装配点不许再把语义读数塞进 extra（否则又是只写不读）。
    check("不再产 semantic_reads", "semantic_reads" not in ctx.extra)
    check("不再产 turn_understanding", "turn_understanding" not in ctx.extra)
    check("trigger 是外部消息", ctx.trigger == "external_message")


def t_world_state_carries_trigger_reasons() -> None:
    ctx = tp._trigger_agent_context(_assembly(trigger_reasons=("mention", "pat")))
    ws = ctx.extra["world_state"]
    check("world_state 是 dict", isinstance(ws, dict), str(type(ws)))
    check("trigger_reasons 是 list", ws.get("trigger_reasons") == ["mention", "pat"], str(ws))
    check("world_state 带 world 自己的字段", ws.get("turns") == 1, str(ws))


def t_missing_world_degrades_to_empty() -> None:
    ctx = tp._trigger_agent_context(_assembly(turn_world=None))
    check("没有世界面时 world_state 是 {}", ctx.extra["world_state"] == {}, str(ctx.extra["world_state"]))
    check("window_turns 是空表", ctx.extra["window_turns"] == [])
    check("pronoun_rows 是空表", ctx.extra["pronoun_rows"] == [])


def t_assembly_is_a_frozen_carrier() -> None:
    asm = _assembly()
    try:
        asm.chat_id = "改"  # type: ignore[misc]
    except Exception:
        froze = True
    else:
        froze = False
    check("TriggerAssembly 不可变（搬错字段不会半路被改）", froze)


def t_assembly_does_not_decide_speaking() -> None:
    src = inspect.getsource(tp._trigger_agent_context)
    for word in ("llm", "sqlite3", "model_addressee_veto", "decide_turn", "log.info"):
        check(f"装配点不碰 {word}", word not in src, word)
    body = inspect.getsource(tp.TriggerAssembly)
    check("载体也不碰决策", "decide" not in body.lower().replace("decision", ""), "")


def t_read_payload_helpers_are_gone() -> None:
    """负锁：读场块退役后，发布口那套白名单拷贝函数跟着一起消失。"""
    body = (ROOT / "core" / "routing" / "turn_pipeline.py").read_text(encoding="utf-8")
    for name in ("_semantic_payload", "READ_PAYLOAD_FIELDS"):
        check(f"发布口.{name} 已退役", name not in body)


def main() -> int:
    print()
    print("trigger assembly (R2-S4)")
    for fn in (
        t_extra_has_exactly_the_base_keys,
        t_values_come_from_the_assembly,
        t_world_state_carries_trigger_reasons,
        t_missing_world_degrades_to_empty,
        t_assembly_is_a_frozen_carrier,
        t_assembly_does_not_decide_speaking,
        t_read_payload_helpers_are_gone,
    ):
        print(f"[{fn.__name__}]")
        fn()
    print()
    print(f"RESULT {len(FAILS)} failed / {COUNT[0]} checks")
    for line in FAILS:
        print("  -", line)
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())
