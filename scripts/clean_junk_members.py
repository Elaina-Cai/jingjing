"""清掉「不是人」的群成员关系（self / unknown / <群id>@chatroom）。

2026-10-23 实测：互刷群 9 行成员 = 7 真人 + `self` + `unknown`，测试群 5 行 =
3 真人 + `unknown` + 一条以群 id 命名的「人」。根因是 `db/repo_people.py` 的
`resolve_message` 把任何非空 sender 当 wxid 建号，而本地库会把「自己发的」写成
`self`、「取不到 wxid」写成 `unknown`、群系统消息的发送人写成群 id。

本脚本只动**成员关系**（`group_memberships` / `group_membership_periods`）：
消息证据（`message_evidence`）与人对实体（`person_entities`）保持原样——那是
历史事实，删了会丢消息归属线索。修完 `resolve_message` 之后不会再长回来。

用法::

    python scripts/clean_junk_members.py              # 只报告（默认 dry-run）
    python scripts/clean_junk_members.py --apply      # 落库（先备份 meta 库）

只删「这一行本身也看不出是谁」的成员关系（昵称是 unknown / self / 空，或者
它其实就是这个群自己）。**名字是真名的行一律留着**：那是「没拿到 wxid 的真人」
（实测 `10000000003@chatroom` 里 `dbw没有几把678310600` 名下 141 条消息属于这类），
删掉等于抹掉一个成员。
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from db.repo_people import is_person_sender  # noqa: E402

DEFAULT_DB = ROOT / "data" / "yinyue_meta.db"
BACKUP_DIR = ROOT / "backups"


def junk_person_ids(conn: sqlite3.Connection) -> dict[str, str]:
    """person_id -> 为什么它不是「这个群里的人」。

    只认两类机械事实，别的（尤其「有真昵称但没有 wxid」的人）一律不碰：
    * 它全部身份值都是占位值（self / unknown / none / null）；
    * 它的身份值就是某个会话自己（``<chat_id>@chatroom``）——群系统消息的发送人。
    """
    rows = conn.execute(
        "select person_id, identity_value from person_identities"
    ).fetchall()
    by_person: dict[str, list[str]] = {}
    for person_id, value in rows:
        by_person.setdefault(str(person_id), []).append(str(value or ""))
    junk: dict[str, str] = {}
    for person_id, values in by_person.items():
        if values and not any(is_person_sender(v) for v in values):
            junk[person_id] = " / ".join(sorted(set(values)))
    return junk


def deletable_rows(conn: sqlite3.Connection, junk: dict[str, str]) -> list[tuple[int, str, str, str]]:
    """(id, chat_id, 为什么, 昵称·条数)。

    两条同时成立的语义：这个「人」不是人（junk），并且这一行本身也看不出是谁
    （昵称是占位值 / 空）——或者它其实就是这个群自己。
    昵称是真名字的行要留着：那是「没拿到 wxid 的真人」，删掉等于抹掉一个成员。
    """
    plan: list[tuple[int, str, str, str]] = []
    for row_id, person_id, chat_id, nickname, count in conn.execute(
        "select id, person_id, chat_id, group_nickname, message_count"
        " from group_memberships order by chat_id, id"
    ):
        person_id = str(person_id)
        chat_id = str(chat_id)
        nickname = str(nickname or "")
        why = junk.get(person_id, "")
        if not why:
            continue
        if not is_person_sender(nickname) or person_id and why == chat_id:
            plan.append((int(row_id), chat_id, why or chat_id, f"{nickname or '(空昵称)'} · {count} 条"))
        elif chat_id in why.split(" / "):
            plan.append((int(row_id), chat_id, why, f"{nickname or '(空昵称)'} · {count} 条"))
    return plan


def report(conn: sqlite3.Connection) -> tuple[dict[str, str], list[tuple[int, str, str, str]]]:
    junk = junk_person_ids(conn)
    return junk, deletable_rows(conn, junk)


def apply(conn: sqlite3.Connection, plan: list[tuple[int, str, str, str]]) -> dict[str, int]:
    ids = [row_id for row_id, _chat, _why, _detail in plan]
    counts = {"group_memberships": 0, "group_membership_periods": 0}
    person_ids: list[str] = []
    for row_id in ids:
        row = conn.execute(
            "select person_id from group_memberships where id=?", (row_id,)
        ).fetchone()
        if row is None:
            continue
        person_ids.append(str(row[0]))
        cur = conn.execute("delete from group_memberships where id=?", (row_id,))
        counts["group_memberships"] += cur.rowcount or 0
    for person_id in set(person_ids):
        left = conn.execute(
            "select count(*) from group_memberships where person_id=?", (person_id,)
        ).fetchone()[0]
        if left:
            continue
        cur = conn.execute(
            "delete from group_membership_periods where person_id=?", (person_id,)
        )
        counts["group_membership_periods"] += cur.rowcount or 0
    conn.commit()
    return counts


def member_counts(conn: sqlite3.Connection) -> list[tuple[str, int]]:
    return [
        (str(chat_id), int(n))
        for chat_id, n in conn.execute(
            "select chat_id, count(*) from group_memberships group by chat_id order by 2 desc"
        )
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description="清掉不是人的群成员关系（默认 dry-run）")
    parser.add_argument("--meta-db", default=str(DEFAULT_DB))
    parser.add_argument("--apply", action="store_true", help="真的落库（先备份）")
    args = parser.parse_args()

    db_path = Path(args.meta_db)
    if not db_path.exists():
        print(f"[skip] 没有这个库：{db_path}")
        return 1
    conn = sqlite3.connect(str(db_path))
    try:
        before = member_counts(conn)
        junk, plan = report(conn)
        print(f"库：{db_path}")
        print(f"可疑人号 {len(junk)} 个；它们的成员关系 {len(plan)} 行：")
        for _row_id, chat_id, why, detail in plan:
            print(f"  - {chat_id}: {why} ({detail})")
        if not plan:
            print("没有需要清的成员关系。")
            return 0
        if not args.apply:
            print("\n[dry-run] 没动库。加 --apply 才落。")
            return 0

        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup = BACKUP_DIR / f"meta_before_clean_junk_members_{stamp}.db"
        with sqlite3.connect(str(backup)) as dst:
            conn.backup(dst)
        print(f"\n[备份] {backup}")
        counts = apply(conn, plan)
        print(f"[删除] group_memberships={counts['group_memberships']}"
              f" group_membership_periods={counts['group_membership_periods']}")
        print("清完各群人数：")
        for chat_id, n in member_counts(conn):
            old = dict(before).get(chat_id, 0)
            print(f"  - {chat_id}: {old} -> {n}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())