# -*- coding: utf-8 -*-
r"""task-49 验收：名册/「近期说话人」在 **两种身份 id 空间** 下不漏人。

背景：``terminal_events.actor_person_id`` 存量行写 wxid、on 档新行写 meta person id。
`core/context/group_members.py::recent_speaker_ids` 是「近 N 小时说过话」的唯一读口，
如果它只认一种空间，on 之后名册会静默漏人（历史实测：`@` 合法性判据 0/166 命中）。

钉三件事：

1. 同一批事件用 wxid 写（off 档）与用 person id 写（on 档），`recent_speaker_ids`
   返回的**人数相等**，且都归一到 person id 空间（同一人不会因换空间被算两次）；
2. 混写（部分旧行 wxid + 部分新行 person id）人数与两档单写相等；
3. 解析不出的占位符（`unknown`）保留原值、不丢行（既不猜也不炸）。

复算：``.\\.venv_it\\Scripts\\python.exe -X utf8 -u tests\\test_group_members_dual_identity.py``
"""
from __future__ import annotations

import datetime as dt
import os
import pathlib
import sqlite3
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

TMP = ROOT / ".test_tmp" / "group_members_dual"
TMP.mkdir(parents=True, exist_ok=True)

from sqlalchemy import create_engine, text  # noqa: E402

from core.context import group_members as G  # noqa: E402

PASSED = 0
FAILED: list[str] = []

P1, W1 = "p" * 32, "wxid_seed06"
P2, W2 = "a" * 32, "wxid_seed03"
P3, W3 = "b" * 32, "wxid_seed05"
PLATFORM, ACCOUNT, CHAT = "wechat", "wxid_bot", "g1@chatroom"


def ok(name: str, cond: bool, detail: object = "") -> None:
    global PASSED
    if cond:
        PASSED += 1
    else:
        FAILED.append("%s %r" % (name, detail))


def _make_db(name: str, actors: list[str]) -> object:
    path = TMP / f"{name}.db"
    if path.exists():
        path.unlink()
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        create table person_identities(
            id integer primary key autoincrement,
            platform text, account_id text, identity_type text,
            identity_value text, person_id text, chat_id text
        );
        create table group_memberships(
            person_id text, platform text, chat_id text, group_nickname text
        );
        create table terminal_events(
            event_id text, chat_id text, platform text, bot_account_id text,
            actor_person_id text, occurred_at text
        );
        """
    )
    people = [(P1, W1, "P6"), (P2, W2, "P3"), (P3, W3, "P5")]
    for person, wxid, nick in people:
        conn.execute(
            "insert into person_identities(platform, account_id, identity_type, "
            "identity_value, person_id, chat_id) values (?,?,?,?,?,?)",
            (PLATFORM, ACCOUNT, "wxid", wxid, person, CHAT),
        )
        conn.execute(
            "insert into group_memberships(person_id, platform, chat_id, group_nickname) "
            "values (?,?,?,?)",
            (person, PLATFORM, CHAT, nick),
        )
    base = dt.datetime(2026, 10, 18, 12, 0, 0)
    for index, actor in enumerate(actors):
        conn.execute(
            "insert into terminal_events(event_id, chat_id, platform, bot_account_id, "
            "actor_person_id, occurred_at) values (?,?,?,?,?,?)",
            (
                f"e{index}",
                CHAT,
                PLATFORM,
                ACCOUNT,
                actor,
                (base - dt.timedelta(hours=index + 1)).strftime("%Y-%m-%d %H:%M:%S"),
            ),
        )
    conn.commit()
    conn.close()
    return create_engine(f"sqlite:///{path}")


def recent(engine, *, anchor_hours: int) -> tuple[str, ...]:
    repo = type("Repo", (), {"engine": engine})()
    now = dt.datetime(2026, 10, 18, 12, 0, 0) - dt.timedelta(hours=anchor_hours)
    return G.recent_speaker_ids(CHAT, hours=24, now=now, meta_repo=repo, ttl=-1)


def main() -> int:
    actors_off = [W1, W2, W3, "unknown", W1]
    actors_on = [P1, P2, P3, "unknown", P1]
    actors_mixed = [W1, P2, W3, "unknown", P1]

    off = recent(_make_db("off", actors_off), anchor_hours=0)
    on = recent(_make_db("on", actors_on), anchor_hours=2)
    mixed = recent(_make_db("mixed", actors_mixed), anchor_hours=4)

    ok("off.canonical_person", P1 in off and W1 not in off, off)
    ok("on.keeps_person", P1 in on, on)
    ok("same_set.off_on", set(off) == set(on), (off, on))
    ok("same_set.off_mixed", set(off) == set(mixed), (off, mixed))
    ok("same_count", len(off) == len(on) == len(mixed), (len(off), len(on), len(mixed)))
    ok("no_double_count", list(off).count(P1) == 1 and len(off) == 4, off)
    ok("placeholder_kept", "unknown" in off, off)

    # 名单里三个人都在「近期说话」里（名字→人 id 只是另一条路，这里直查人 id 空间）
    for person in (P1, P2, P3):
        ok(f"recent.has.{person[:4]}", person in off, off)

    if FAILED:
        print(f"FAILED {len(FAILED)}/{PASSED + len(FAILED)}")
        for item in FAILED:
            print(" -", item)
        return 1
    print(f"ok {PASSED} checks")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
