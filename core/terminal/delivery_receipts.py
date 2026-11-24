# -*- coding: utf-8 -*-
"""出口回执落账（U-11）：`delivery_receipts`，append-only 旁注。

出处：`docs/decisions/出口.md` §4「落账」/ §5 六种结果 / §12 量具。

为什么是旁注不是账本正行
------------------------
回执是**我们自己的动作**的结果，不是外部世界发生的事实；账本（`terminal_events`）
只收有外部来源的事件。这张表可以整表删掉重建（代价是这几天的投递读数没了），
不影响"她说过什么"——那条在账本的回显行里。

三条纪律
--------
1. **每条回执一行**，六种状态都写（`sent` / `skipped` / `blocked` / `failed` /
   `partial` / `pending_approval`）——"没发出去"也必须有行，否则重复发送率算不准；
2. **写失败只 log**，绝不阻断一个正常回合；
3. **同一回合、同一对象、同一 kind 只写一行**（唯一索引 + `INSERT OR IGNORE`），
   所以重放一个回合不会把读数灌成两倍。
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Mapping

from sqlalchemy import text

from core import features

log = logging.getLogger(__name__)

#: 表名（迁移 65 与本模块必须同形）。
TABLE = "delivery_receipts"

CREATE_TABLE_SQL = (
    "CREATE TABLE IF NOT EXISTS delivery_receipts ("
    "receipt_id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,"
    "turn_id TEXT DEFAULT '' NOT NULL,"
    "chat_id TEXT DEFAULT '' NOT NULL,"
    "status TEXT DEFAULT '' NOT NULL,"
    "kind TEXT DEFAULT '' NOT NULL,"
    "target TEXT DEFAULT '' NOT NULL,"
    "ack TEXT DEFAULT 'none' NOT NULL,"
    "reason TEXT DEFAULT '' NOT NULL,"
    "platform_msg_id TEXT DEFAULT '' NOT NULL,"
    "client_msg_id TEXT DEFAULT '' NOT NULL,"
    "adapter TEXT DEFAULT '' NOT NULL,"
    "detail TEXT DEFAULT '' NOT NULL,"
    "ref TEXT DEFAULT '' NOT NULL,"
    "ts DATETIME NOT NULL"
    ")"
)

CREATE_INDEX_SQL = (
    "CREATE INDEX IF NOT EXISTS ix_delivery_receipts_chat_ts "
    "ON delivery_receipts (chat_id, ts)"
)

CREATE_UNIQUE_SQL = (
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_delivery_receipts_turn_ref "
    "ON delivery_receipts (turn_id, kind, ref, target)"
)

INSERT_SQL = (
    "INSERT OR IGNORE INTO delivery_receipts "
    "(turn_id, chat_id, status, kind, target, ack, reason, platform_msg_id,"
    " client_msg_id, adapter, detail, ref, ts) "
    "VALUES (:turn_id, :chat_id, :status, :kind, :target, :ack, :reason,"
    " :platform_msg_id, :client_msg_id, :adapter, :detail, :ref, :ts)"
)

_engine: Any = None


def ddl_statements() -> list[str]:
    """迁移 65 用：三句 DDL（都幂等）。"""
    return [CREATE_TABLE_SQL, CREATE_INDEX_SQL, CREATE_UNIQUE_SQL]


def set_engine(engine: Any) -> None:
    """测试/嵌入用：覆盖默认引擎（``None`` = 回到懒加载的 meta 库）。"""
    global _engine
    _engine = engine


def _get_engine() -> Any:
    global _engine
    if _engine is not None:
        return _engine
    from core.terminal.store_port import get_meta_repo

    _engine = get_meta_repo().engine
    return _engine


def _mode() -> str:
    """唯一读点：``DELIVERY_CONTRACT`` 三态（默认 off）。"""
    return str(features.mode(features.DELIVERY_CONTRACT, features.MODE_OFF) or features.MODE_OFF)


def record_receipt(
    receipt: Any,
    *,
    turn_id: str,
    chat_id: str = "",
    ts: datetime | None = None,
) -> bool:
    """写一行回执。返回是否真的插入了（重复行、off 档、写失败都返 False）。"""
    if _mode() == features.MODE_OFF:
        # off 档一行不写（默认行为逐字不变）；shadow/on 都写——见 `core/speech/receipts.py`
        # 顶部第 1 条：回执记的是"这次发成了什么"，与"谁负责传输"无关。
        return False
    if not str(turn_id or "").strip():
        log.info("delivery_receipt_skipped reason=empty_turn_id")
        return False
    # `Receipt` 对象或 dict 都收：出口那条路传的是 dataclass，测试传 dict。
    # 不转换就 `receipt.get(...)` → AttributeError → 整行回执静默丢失。
    receipt = _as_mapping(receipt)
    payload = {
        "turn_id": str(turn_id),
        "chat_id": str(chat_id or ""),
        "status": str(receipt.get("status", "") or ""),
        "kind": str(receipt.get("kind", "") or ""),
        "target": str(receipt.get("target", "") or ""),
        "ack": str(receipt.get("ack", "") or "none"),
        "reason": str(receipt.get("reason", "") or ""),
        "platform_msg_id": str(receipt.get("platform_msg_id", "") or ""),
        "client_msg_id": str(receipt.get("client_msg_id", "") or ""),
        "adapter": str(receipt.get("adapter", "") or ""),
        "detail": str(receipt.get("detail", "") or "")[:200],
        "ref": str(receipt.get("ref", "") or ""),
        "ts": ts or datetime.now(),
    }
    try:
        with _get_engine().begin() as conn:
            result = conn.execute(text(INSERT_SQL), payload)
        return bool(getattr(result, "rowcount", 0))
    except Exception as exc:  # noqa: BLE001 - 旁注失败绝不阻断主链
        log.info("delivery_receipt_write_failed error=%s turn_id=%s", type(exc).__name__, turn_id)
        return False


def update_pending_ack(
    target: str,
    *,
    ack: str,
    platform_msg_id: str = "",
    client_msg_id: str = "",
    window_s: float = 180.0,
    now: datetime | None = None,
) -> int:
    """兼容旧调用；缺少发送操作和账号绑定，禁止按目标/时间窗补写回执。"""
    if _mode() != features.MODE_OFF:
        log.info("delivery_receipt_ack_binding_missing target=%s", str(target or ""))
    return 0



def make_ledger(*, turn_id: str, chat_id: str = ""):
    """给 ``deliver_pending_approvals(ledger=...)`` 用的钩子。

    `Receipt` 里没有 `turn_id`（一次投递不必知道回合），所以回合与会话在闭包里绑好。
    """

    def _ledger(receipt: Any) -> None:
        record_receipt(_as_mapping(receipt), turn_id=turn_id, chat_id=chat_id)

    return _ledger


def _as_mapping(receipt: Any) -> dict[str, Any]:
    """`Receipt` 或 dict 都收：出口传对象，测试传 dict。"""
    if isinstance(receipt, Mapping):
        return dict(receipt)
    fields = ("status", "kind", "target", "ack", "reason", "platform_msg_id",
              "client_msg_id", "adapter", "detail", "ref")
    return {name: getattr(receipt, name, "") for name in fields}


def find_ref(ref: str) -> dict[str, Any] | None:
    """按回执号找回那条回执（更正要用它把 `ref` 变回一条回执）。

    只认最近一行：同一 `ref` 理论上唯一（唯一索引 `(turn_id, kind, ref, target)`），
    但补发/重放可能留多行，取最新的一条最接近"现在这条说的是什么"。
    """
    key = str(ref or "").strip()
    if not key:
        return None
    sql = (
        "SELECT turn_id, chat_id, status, kind, target, ack, reason, detail, ref, ts "
        "FROM delivery_receipts WHERE ref = :ref ORDER BY receipt_id DESC LIMIT 1"
    )
    try:
        with _get_engine().connect() as conn:
            row = conn.execute(text(sql), {"ref": key}).fetchone()
    except Exception as exc:  # noqa: BLE001 - 查不到就当没有，不阻断主链
        log.info("delivery_receipt_find_failed error=%s", type(exc).__name__)
        return None
    if row is None:
        return None
    return {
        "turn_id": str(row[0] or ""),
        "chat_id": str(row[1] or ""),
        "status": str(row[2] or ""),
        "kind": str(row[3] or ""),
        "target": str(row[4] or ""),
        "ack": str(row[5] or ""),
        "reason": str(row[6] or ""),
        "detail": str(row[7] or ""),
        "ref": str(row[8] or ""),
        "at": str(row[9] or ""),
    }


def count_by_status(*, chat_id: str | None = None) -> dict[str, int]:
    """量具口径：各状态各多少行（``重复发送率`` 的分母就是 ``sent`` 的行数）。"""
    sql = "SELECT status, COUNT(*) AS n FROM delivery_receipts"
    params: dict[str, Any] = {}
    if chat_id:
        sql += " WHERE chat_id = :chat_id"
        params["chat_id"] = chat_id
    sql += " GROUP BY status"
    try:
        with _get_engine().connect() as conn:
            rows = conn.execute(text(sql), params).fetchall()
    except Exception as exc:  # noqa: BLE001
        log.info("delivery_receipt_count_failed error=%s", type(exc).__name__)
        return {}
    return {str(row[0]): int(row[1]) for row in rows}


def recent(*, chat_id: str = "", limit: int = 50) -> list[dict[str, Any]]:
    """最近的若干行（报表/排查用，只读）。"""
    sql = (
        "SELECT turn_id, chat_id, status, kind, target, ack, reason, detail, ref, ts "
        "FROM delivery_receipts"
    )
    params: dict[str, Any] = {"limit": max(0, int(limit))}
    if chat_id:
        sql += " WHERE chat_id = :chat_id"
        params["chat_id"] = chat_id
    sql += " ORDER BY receipt_id DESC LIMIT :limit"
    try:
        with _get_engine().connect() as conn:
            rows = conn.execute(text(sql), params).fetchall()
    except Exception as exc:  # noqa: BLE001
        log.info("delivery_receipt_read_failed error=%s", type(exc).__name__)
        return []
    return [dict(row._mapping) for row in rows]
