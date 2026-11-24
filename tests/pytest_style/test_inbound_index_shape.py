"""``inbound_event`` 的索引要能服务「按会话取上下文」这条查询。

2026-10-30：`build_snapshot` 只做一次全局 800 行拉取，会话上下文靠二次查询补；
但 `ix_in_chat(chat_id, ts_utc)` 前导列不含 `account_id`，而所有查询都带
`account_id=?`，于是优化器宁可沿 `ix_in_ts` 扫时间范围 —— 补查询等于没走索引。

这里直接拿 ``db.world_db._SCHEMA`` 在临时库上建表（它就是「声明」本身），
不碰模块的全局连接路径。
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from db.world_db import _SCHEMA  # noqa: E402

SQL = (
    "select msg_id from inbound_event where account_id=? "
    "and ts_utc>=? and chat_id=? order by ts_utc desc limit 20"
)


def _con(tmp_path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(str(tmp_path / "w.db"))
    for stmt in _SCHEMA:
        con.execute(stmt)
    return con


def _plan(con: sqlite3.Connection) -> str:
    rows = con.execute("explain query plan " + SQL, ("a", "2026-01-01T00:00:00", "c"))
    return " ".join(str(r[-1]) for r in rows)


def test_index_exists(tmp_path: Path) -> None:
    con = _con(tmp_path)
    try:
        names = {r[1] for r in con.execute("pragma index_list(inbound_event)")}
    finally:
        con.close()
    assert "ix_in_acct_chat_ts" in names, sorted(names)


def test_per_chat_query_uses_the_new_index(tmp_path: Path) -> None:
    con = _con(tmp_path)
    try:
        plan = _plan(con)
    finally:
        con.close()
    assert "ix_in_acct_chat_ts" in plan, plan


def test_plan_is_not_the_time_only_scan(tmp_path: Path) -> None:
    con = _con(tmp_path)
    try:
        plan = _plan(con)
    finally:
        con.close()
    assert "ix_in_ts" not in plan, plan
