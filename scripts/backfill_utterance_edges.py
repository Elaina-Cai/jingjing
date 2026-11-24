r"""回填「谁对谁说」寻址边（task-43）——只读账号库，幂等，默认 dry-run。

用法（先在生产库副本上跑）::

    $env:PYTHONPATH = "."
    & .\.venv_it\Scripts\python.exe -X utf8 scripts\backfill_utterance_edges.py --days 7
    & .\.venv_it\Scripts\python.exe -X utf8 scripts\backfill_utterance_edges.py --days 7 --apply

判据（与 task-42 的预测对照，允许 ±20%）：

* 主群 7 天入站 4749 条、``quote_reply_id`` 非空 572 = **12.04%**；
* 口径 ``recipient_kind``：``bot 1.7% / person 91.6% / group 0.2% / unknown 6.5%``；
* 新增 ``at_body`` 通道（微信 ``mentions`` 恒空、@ 在正文里）后 ``bot`` 会上升。

两条硬纪律（task-42 实测，别图省事）：

1. **不读 ``messages.quote_reply_user``**：主群 558/572 填的是 chat_id 自身。
   引用作者一律走 ``quote_reply_id -> messages.msg_id -> sender_name``（93.4% 可解）。
2. 只写不猜：没有 @、没有引用 ⇒ ``recipient_kind='unknown'`` + ``evidence='none'``，
   绝不把发言人/触发者填成收件人。

默认 ``--dry-run``：只算不写。``--apply`` 才落库（``INSERT OR IGNORE``，幂等）。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

DEFAULT_ACCOUNT_DB = "data/platforms/wechat/wxid_seed02/account.db"
DEFAULT_META_DB = "data/yinyue_meta.db"
DEFAULT_PLATFORM = "wechat"
DEFAULT_ACCOUNT_ID = "wxid_seed02"


def _ro(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {str(r[1]) for r in conn.execute(f"pragma table_info({table})")}
    except sqlite3.Error:
        return set()


def load_messages(
    conn: sqlite3.Connection, *, days: float, chats: list[str]
) -> list[dict[str, Any]]:
    cols = _columns(conn, "messages")
    if not cols:
        return []
    since = (datetime.now() - timedelta(days=days)).isoformat(sep=" ")
    wanted = [
        "msg_id",
        "chat_id",
        "chat_type",
        "sender_id",
        "sender_name",
        "content",
        "created_at",
        "is_self",
        "quote_reply_id",
        "mentions",
    ]
    picked = [c for c in wanted if c in cols]
    sql = (
        f"select {', '.join(picked)} from messages "
        "where is_self = 0 and created_at >= ?"
    )
    params: list[Any] = [since]
    if chats:
        sql += " and chat_id in (" + ", ".join("?" for _ in chats) + ")"
        params.extend(chats)
    sql += " order by created_at"
    return [dict(r) for r in conn.execute(sql, params)]


def load_quoted(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    """``msg_id -> {msg_id, sender_id, sender_name, is_self}``（全表，幂等回填够用）。"""
    cols = _columns(conn, "messages")
    if not {"msg_id", "sender_id"} <= cols:
        return {}
    picked = [c for c in ("msg_id", "sender_id", "sender_name", "is_self") if c in cols]
    out: dict[str, dict[str, Any]] = {}
    for row in conn.execute(f"select {', '.join(picked)} from messages"):
        item = dict(row)
        out[str(item.get("msg_id") or "")] = item
    return out


def chat_names(meta: sqlite3.Connection, *, platform: str, chat_id: str) -> list[str]:
    names: list[str] = []
    cols = _columns(meta, "group_memberships")
    if cols:
        for row in meta.execute(
            "select group_nickname from group_memberships "
            "where platform = ? and chat_id = ? and group_nickname != ''",
            (platform, chat_id),
        ):
            text = str(row[0] or "").strip()
            if text:
                names.append(text)
    cols = _columns(meta, "person_entities")
    if cols:
        for row in meta.execute(
            "select display_name from person_entities "
            "where display_name is not null and display_name != '' limit 400"
        ):
            text = str(row[0] or "").strip()
            if text:
                names.append(text)
    return list(dict.fromkeys(names))


def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "select 1 from sqlite_master where type='table' and name=?", (table,)
    ).fetchone()
    return row is not None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="回填 utterance_edges（默认 dry-run）")
    ap.add_argument("--days", type=float, default=7.0, help="回填天数（默认 7）")
    ap.add_argument("--platform", default=DEFAULT_PLATFORM)
    ap.add_argument("--account-id", default=DEFAULT_ACCOUNT_ID)
    ap.add_argument("--account-db", default=DEFAULT_ACCOUNT_DB)
    ap.add_argument("--meta-db", default=DEFAULT_META_DB)
    ap.add_argument("--chat", action="append", default=[], help="只回填这些 chat_id")
    ap.add_argument("--apply", action="store_true", help="真的写库（默认只算）")
    ap.add_argument("--json", action="store_true", help="以 JSON 打印分布")
    args = ap.parse_args(argv)

    from core.terminal import utterance_edges as ue

    account = _ro(args.account_db)
    try:
        messages = load_messages(account, days=args.days, chats=args.chat)
        quoted = load_quoted(account)
    finally:
        account.close()

    meta = _ro(args.meta_db)
    try:
        has_table = table_exists(meta, "utterance_edges")
        by_chat: dict[str, list[dict[str, Any]]] = {}
        for msg in messages:
            by_chat.setdefault(str(msg.get("chat_id") or ""), []).append(msg)
        edges: list[dict[str, Any]] = []
        for chat_id, batch in by_chat.items():
            names = chat_names(meta, platform=args.platform, chat_id=chat_id)
            edges.extend(
                ue.build_edges(
                    batch,
                    platform=args.platform,
                    account_id=args.account_id,
                    chat_names=names,
                    quoted_lookup=lambda mid, _q=quoted: _q.get(str(mid)),
                )
            )
    finally:
        meta.close()

    kinds = Counter(str(e.get("recipient_kind") or "") for e in edges)
    evidences = Counter(str(e.get("evidence") or "") for e in edges)
    per_chat = Counter(str(e.get("chat_id") or "") for e in edges)
    total_in = len(messages)

    report = {
        "mode": ue.edges_mode(),
        "apply": bool(args.apply),
        "days": args.days,
        "inbound": total_in,
        "edges": len(edges),
        "edge_ratio": round(len(edges) / total_in, 4) if total_in else 0.0,
        "table_exists": has_table,
        "recipient_kind": dict(kinds),
        "evidence": dict(evidences),
        "per_chat": dict(per_chat),
    }
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"mode={report['mode']} apply={args.apply} days={args.days} table={has_table}")
        print(f"入站 {total_in} 条 -> 边 {len(edges)} 条 ({report['edge_ratio'] * 100:.2f}%)")
        print("recipient_kind:", dict(kinds))
        print("evidence:", dict(evidences))
        for chat_id, count in per_chat.most_common():
            print(f"  {chat_id}: {count}")

    if not args.apply:
        print("dry-run：未写库（加 --apply 才落库）")
        return 0
    if not has_table:
        print("utterance_edges 表不存在：先跑迁移（db/migrations.py 版本 62）", file=sys.stderr)
        return 2

    from sqlalchemy import create_engine

    engine = create_engine(f"sqlite:///{args.meta_db}")
    inserted = 0
    for start in range(0, len(edges), ue._MAX_EDGES_PER_BATCH):
        inserted += ue.record_edges(engine, edges[start : start + ue._MAX_EDGES_PER_BATCH])
    print(f"已写入 {inserted} 行（候选 {len(edges)}，其余为重复 = 幂等命中）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
