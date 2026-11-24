# -*- coding: utf-8 -*-
r"""会话分窗：`chat_id` 只在 (平台, 账号) 里唯一。

钉住四条：
1. `conversation_key` 可逆且不会因为分隔符而撞键（微信 `group:123` ≠ QQ 群 123）；
2. 只传 `chat_id`（默认）时行为**与接线前一致**——不许悄悄改变存量语义；
   自动档（U-36 ②）：窗口内单视角 → 按它过滤；两视角以上 → 不过滤并发 warning；
3. 传了 `platform` / `account_id` 就真的隔开：两个账号在同一个群 → 两份窗口；
4. 群与私聊、群与群之间不许互串。

跑法：``.venv_it\Scripts\python.exe -X utf8 -u tests/test_conversation_partition.py``
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import create_engine, text  # noqa: E402

from core.context import chat_window as cw  # noqa: E402

PASSED = 0
FAILED = 0

DDL = (
    "CREATE TABLE terminal_events ("
    "id INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT, occurred_at DATETIME, "
    "sequence INTEGER DEFAULT 0, platform TEXT DEFAULT '', bot_account_id TEXT DEFAULT '', "
    "chat_id TEXT DEFAULT '', chat_type TEXT DEFAULT '', actor_person_id TEXT DEFAULT '', "
    "actor_account_id TEXT DEFAULT '', recipient_person_ids_json TEXT DEFAULT '[]', "
    "event_type TEXT DEFAULT 'message', content TEXT, parent_event_id TEXT DEFAULT '', "
    "external_msg_id TEXT DEFAULT '', metadata_json TEXT DEFAULT '{}')",
    "CREATE TABLE person_identities (identity_value TEXT, person_id TEXT)",
    "CREATE TABLE person_entities (person_id TEXT, display_name TEXT)",
)


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print(f"PASS {name}")
    else:
        FAILED += 1
        print(f"FAIL {name}" + (f"  [{detail}]" if detail else ""))


def _engine():
    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        for sql in DDL:
            conn.execute(text(sql))
    return engine


def _add(engine, *, chat: str, content: str, platform: str = "wechat",
         account: str = "wxid-bot", minutes: int = 0, chat_type: str = "group",
         event_type: str = "message") -> None:
    # 曾经钉死 2026-10-21 12:00 —— resolve_scope/load_messages 默认用墙钟，
    # 过了 7 天窗口整段被排除（2026-10-22 就红了）。改成相对当前时间。
    when = datetime.now() - timedelta(minutes=minutes)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO terminal_events (event_id, occurred_at, platform, bot_account_id,"
                " chat_id, chat_type, actor_person_id, event_type, content, metadata_json)"
                " VALUES (:e, :t, :p, :a, :c, :ct, :actor, :k, :body, '{}')"
            ),
            {"e": f"e-{chat}-{minutes}-{platform}", "t": when, "p": platform, "a": account,
             "c": chat, "ct": chat_type, "actor": f"actor-{minutes}", "k": event_type,
             "body": content},
        )


def t_key_is_injective() -> None:
    a = cw.conversation_key("wechat", "wxid-1", "group:123")
    b = cw.conversation_key("qq", "123", "123")
    check("不同三元组不会撞键", a != b, f"{a} / {b}")
    check("可逆", cw.split_conversation_key(a) == ("wechat", "wxid-1", "group:123"),
          str(cw.split_conversation_key(a)))
    tricky = cw.conversation_key("wechat", "a|b", "c%d")
    check("分隔符与百分号被转义后仍可逆",
          cw.split_conversation_key(tricky) == ("wechat", "a|b", "c%d"),
          str(cw.split_conversation_key(tricky)))
    check("坏键不抛", cw.split_conversation_key("only-one") == ("", "", "only-one"))


def t_default_unchanged(engine) -> None:
    _add(engine, chat="g1", content="A 账号看到的一行", account="wxid-A", minutes=1)
    _add(engine, chat="g1", content="B 账号看到的一行", account="wxid-B", minutes=2)
    rows = cw.load_messages("g1", engine=engine)
    check("默认（不传平台/账号）行为与接线前一致：两账号混在一份里",
          len(rows) == 2, str([r.content for r in rows]))


def t_account_isolated(engine) -> None:
    a = cw.load_messages("g1", platform="wechat", account_id="wxid-A", engine=engine)
    b = cw.load_messages("g1", platform="wechat", account_id="wxid-B", engine=engine)
    check("账号 A 只看到自己那份", [r.content for r in a] == ["A 账号看到的一行"],
          str([r.content for r in a]))
    check("账号 B 只看到自己那份", [r.content for r in b] == ["B 账号看到的一行"],
          str([r.content for r in b]))


def t_chats_never_mix(engine) -> None:
    _add(engine, chat="group:1", content="群里的话", platform="qq", account="10001", minutes=3)
    db = cw.load_messages("group:1", platform="qq", account_id="10001", engine=engine)
    other = cw.load_messages("group:2", platform="qq", account_id="10001", engine=engine)
    check("群 1 只有群里的话", [r.content for r in db] == ["群里的话"],
          str([r.content for r in db]))
    check("群 2 是空的（不串群）", other == [], str(other))

    wechat_group = cw.load_messages("group:1", platform="wechat", account_id="wxid-A",
                                    engine=engine)
    check("同名 chat_id 换个平台就查不到", wechat_group == [],
          str([r.content for r in wechat_group]))


def t_group_vs_private(engine) -> None:
    _add(engine, chat="wxid-friend", content="私聊一句", chat_type="private", minutes=4)
    private = cw.load_messages("wxid-friend", engine=engine)
    group = cw.load_messages("wxid-friend", platform="wechat", account_id="wxid-OTHER",
                             engine=engine)
    check("私聊只出私聊那句", [r.content for r in private] == ["私聊一句"],
          str([r.content for r in private]))
    check("别人的窗口不串进来", group == [], str(group))


def t_time_window(engine) -> None:
    old = datetime(2026, 10, 1, 9, 0)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO terminal_events (event_id, occurred_at, platform, bot_account_id,"
                " chat_id, chat_type, actor_person_id, event_type, content, metadata_json)"
                " VALUES ('e-old', :t, 'wechat', 'wxid-A', 'g1', 'group', 'x', 'message',"
                " '很久以前', '{}')"
            ),
            {"t": old},
        )
    rows = cw.load_messages("g1", platform="wechat", account_id="wxid-A", days=2,
                            now=datetime(2026, 10, 21, 12, 0), engine=engine)
    check("回看天数把更早的排除", [r.content for r in rows] == ["A 账号看到的一行"],
          str([r.content for r in rows]))


def t_auto_scope(engine) -> None:
    """不传 platform/account 时按会话自报的视角读（U-36 ②）。

    三档：窗口内只有一个视角 → 按它过滤（行为与"混在一份"相同，但语义定死了）；
    两个以上视角 → **不过滤**（超集，不静默吞另一视角）；一行都没有 → 不加条件。
    """
    _add(engine, chat="g-auto1", content="单视角的一条", account="wxid-SOLO", minutes=5)
    # resolve_scope 的窗口语义是 [since, ∞)，since 默认 datetime.now()。夹具写的是
    # 「刚才这几分钟」，所以要把窗口起点显式往前挪，否则永远读不到自己刚写的行。
    _win = datetime.now() - timedelta(days=1)
    sc = cw.resolve_scope("g-auto1", engine=engine, since=_win)
    check("单视角会话：reason=single 且给出平台/账号", sc == ("wechat", "wxid-SOLO", "single"),
          str(sc))
    rows = cw.load_messages("g-auto1", engine=engine)
    check("单视角会话：默认读到的就是它的那行", [r.content for r in rows] == ["单视角的一条"],
          str([r.content for r in rows]))

    _add(engine, chat="g-auto2", content="A 视角", account="wxid-A", minutes=6)
    _add(engine, chat="g-auto2", content="B 视角", account="wxid-B", minutes=7)
    sc2 = cw.resolve_scope("g-auto2", engine=engine, since=_win)
    check("两视角会话：reason=ambiguous 且不给过滤条件", sc2 == ("", "", "ambiguous"), str(sc2))
    both = cw.load_messages("g-auto2", engine=engine)
    check("两视角会话：不过滤（超集，不吞另一视角）", len(both) == 2,
          str([r.content for r in both]))

    none_scope = cw.resolve_scope("g-empty", engine=engine, since=_win)
    check("空会话：reason=none", none_scope == ("", "", "none"), str(none_scope))
    check("空会话读出来是空表", cw.load_messages("g-empty", engine=engine) == [], "")

    explicit = cw.load_messages("g-auto2", platform="wechat", account_id="wxid-A", engine=engine)
    check("显式 scope 永远赢过自动档", [r.content for r in explicit] == ["A 视角"],
          str([r.content for r in explicit]))


def main() -> int:
    engine = _engine()
    t_key_is_injective()
    for fn in (t_default_unchanged, t_account_isolated, t_chats_never_mix,
               t_group_vs_private, t_time_window, t_auto_scope):
        print(f"[{fn.__name__}]")
        fn(engine)
    print()
    print(f"RESULT {FAILED} failed / {PASSED + FAILED} checked")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
