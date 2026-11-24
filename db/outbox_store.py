"""account.db 侧的事件 outbox（任务模型 §7.1 ②）。

设计：消息行与 outbox 行在**同一个事务**里提交（``MessageRepository.add``
/ ``SyncService`` 的 ``own_session``），事件投递（meta.db 的
``terminal_events``）由 forwarder（``core/terminal/outbox.py``）幂等完成。

为什么需要它：事件写入以前是 best-effort 的「先落消息、后写事件」，写失败
被静默吞掉，且 ``messages.exists`` 命中后永不重试 → 历史洞。outbox 把
「消息已落库」与「事件已投递」解耦：失败留在 pending，退避重试、计数、
超限告警；重试不重放命令记录（command_records 是 best-effort 投影）。

REC-8（迁移 51）：投递方必须**先夺取行级所有权**（``claim_outbox_batch``：
``status='inflight'`` + ``claimed_at/claimed_by/lease_until``，``attempts``
在夺取时原子 +1），结清时按 ``claimed_by`` 校验（``mark_outbox_retry``）。
这样两条并发 drain 不会再拿同一份 ``attempts`` 快照各写一个绝对值（漏计、
绕过止损），也不会重复投递同一行。

P1-a：``attempts`` 门只约束 ``pending``；**租约过期的 inflight 一律可回收**
（哪怕 attempts 已到止损线），收尾时直接落 ``failed``。否则「attempts=5 +
租约过期」的行既进不了 claim、又没人能结清，会永久卡在 inflight。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import text
from sqlalchemy.orm import Session
from safelog import warn

log = structlog.get_logger()

_COUNT_KEYS = (
    "pending", "inflight", "failed", "delivered", "stale",
    "oldest_pending_age_seconds",
)


def _parse_datetime(value: Any) -> datetime | None:
    """裸 SQL 读回的 DATETIME：可能是 datetime（有类型）或 ISO 字符串。"""
    if isinstance(value, datetime):
        return value
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        warn("db/outbox_store.py:48 ValueError", err=repr(__import__("sys").exc_info()[1]))
        return None


def outbox_table_ready(session: Any) -> bool:
    """本 session 见过的库是否有 event_outbox 表（结果缓存在 session.info）。"""
    cached = session.info.get("event_outbox_ready")
    if cached is not None:
        return bool(cached)
    row = session.execute(
        text("SELECT 1 FROM sqlite_master WHERE type='table' AND name='event_outbox'")
    ).first()
    ready = row is not None
    session.info["event_outbox_ready"] = ready
    if not ready:
        log.warning(
            "event_outbox_table_missing",
            hint="account DB 未迁移到 47；本次不写 outbox，事件仍走直写",
        )
    return ready


def enqueue_message_outbox(session: Any, msg: Any) -> bool:
    """把一行消息登记进 outbox（与消息写入同一事务）。返回是否写入。"""
    msg_id = str(getattr(msg, "msg_id", "") or "")
    if not msg_id:
        # 没有平台 id 的行（msg_id=0/None）无法做幂等键，跳过；它们仍会走直写。
        return False
    if not outbox_table_ready(session):
        return False
    session.execute(
        text(
            "INSERT OR IGNORE INTO event_outbox "
            "(platform, account_id, chat_id, msg_id, status, attempts, "
            " next_attempt_at, last_error, created_at) "
            "VALUES (:platform, :account_id, :chat_id, :msg_id, 'pending', 0, "
            " NULL, '', :created_at)"
        ),
        {
            "platform": str(getattr(msg, "source", "") or ""),
            "account_id": str(getattr(msg, "account_id", "") or ""),
            "chat_id": str(getattr(msg, "chat_id", "") or ""),
            "msg_id": msg_id,
            "created_at": datetime.now(),
        },
    )
    return True


def list_pending_outbox(
    engine: Any,
    *,
    platform: str,
    account_id: str,
    now: datetime,
    limit: int = 200,
) -> list[dict]:
    """到期（或首次）的 pending 行，按 id 升序。"""
    with Session(engine) as session:
        rows = session.execute(
            text(
                "SELECT id, platform, account_id, chat_id, msg_id, attempts "
                "FROM event_outbox "
                "WHERE platform = :platform AND account_id = :account_id "
                "  AND status = 'pending' "
                "  AND (next_attempt_at IS NULL OR next_attempt_at <= :now) "
                "ORDER BY id ASC LIMIT :limit"
            ),
            {
                "platform": str(platform or ""),
                "account_id": str(account_id or ""),
                "now": now,
                "limit": max(1, int(limit)),
            },
        ).mappings().all()
    return [dict(row) for row in rows]


def _has_claim_columns(session: Any) -> bool:
    """本 session 见的 ``event_outbox`` 是否有 REC-8 的 claim 列。"""
    rows = session.execute(text("PRAGMA table_info(event_outbox)")).all()
    return any(str(row[1]) == "claimed_at" for row in rows)


def outbox_claim_ready(engine: Any) -> bool:
    """本库是否已迁移到 51（``event_outbox`` 有 ``claimed_at`` 列）。

    False ⇒ 调用方走降级读路径（``list_pending_outbox``）：未迁移的旧库仍能
    投递，只是没有行级所有权。表不存在同样返回 False（PRAGMA 无行）。
    """
    with Session(engine) as session:
        return _has_claim_columns(session)


def claim_outbox_batch(
    engine: Any,
    *,
    platform: str,
    account_id: str,
    now: datetime,
    claimed_by: str,
    lease_seconds: int = 120,
    limit: int = 200,
    max_attempts: int | None = None,
) -> list[dict]:
    """原子夺取一批到期行（REC-8）：行级所有权 + 租约 + ``attempts`` 自增。

    - 一条 ``UPDATE ... RETURNING`` 完成「选行 → 夺取 → 取回」：选中的行与
      返回的行必然是同一批（不存在「先 SELECT 再 UPDATE」在 WAL 下的读→写
      升级失败，也不会把别的执行者刚夺走的行算进本批）。
    - 可抢的行有两类：
      * ``status='pending'`` 且到期（``next_attempt_at`` 为空或已过）；
      * **租约已过期**的 ``inflight``（``lease_until <= now``，owner 崩了/卡住）
        —— **一律可回收**，不看 ``attempts``。P1-a：``attempts`` 已到止损线的
        行如果连 reclaim 都进不来，就永远卡在 inflight，doctor 恒定 stale，
        而按口径② stale 又不发通知 → 永久静默。回收后由结清逻辑按 ``attempts``
        直接落 ``failed``（见 ``core/terminal/outbox.drain_outbox``）。
    - ``attempts`` 在夺取时自增 ⇒ 它是**投递尝试次数**，不再是「失败次数」。
      传了 ``max_attempts`` 时，门**只挂在 pending 分支**：已到止损线的 pending
      行不会被再次投递（宁可留成 stale 让人看见，也不复活），但被夺走的过期
      inflight 仍能收尾。
    - ``next_attempt_at`` 一并推到租约终点：租约内别人抢不走，租约过了自动
      回到可抢状态（等于 lease 就是退避上限）。
    - 取行顺序回到 ``ORDER BY id ASC``（REC-1 的老 FIFO 语义）：按 ``attempts``
      排序会让重试行被不断到来的新行挤住，饿死的恰好是最需要收尾的那批。

    返回行含 ``id/platform/account_id/chat_id/msg_id/attempts``（``attempts``
    已是夺取后的值）。
    """
    lease = now + timedelta(seconds=max(1, int(lease_seconds)))
    params: dict[str, Any] = {
        "platform": str(platform or ""),
        "account_id": str(account_id or ""),
        "now": now,
        "lease": lease,
        "who": str(claimed_by or ""),
        "limit": max(1, int(limit)),
    }
    attempts_guard = ""
    if max_attempts is not None:
        attempts_guard = " AND attempts < :max_attempts"
        params["max_attempts"] = max(1, int(max_attempts))
    pending_due = (
        "status='pending' "
        "AND (next_attempt_at IS NULL OR next_attempt_at <= :now)"
        f"{attempts_guard}"
    )
    sql = (
        "UPDATE event_outbox SET status='inflight', attempts=attempts+1, "
        "claimed_at=:now, claimed_by=:who, lease_until=:lease, "
        "next_attempt_at=:lease "
        "WHERE id IN ("
        "  SELECT id FROM event_outbox "
        "   WHERE platform=:platform AND account_id=:account_id "
        f"     AND (({pending_due}) "
        "          OR (status='inflight' AND lease_until IS NOT NULL "
        "              AND lease_until <= :now)) "
        "   ORDER BY id ASC LIMIT :limit) "
        "RETURNING id, platform, account_id, chat_id, msg_id, attempts"
    )
    with Session(engine) as session:
        rows = session.execute(text(sql), params).mappings().all()
        session.commit()
    return [dict(row) for row in rows]


def outbox_status_counts(engine: Any, *, now: datetime) -> dict:
    """一行 SQL 出 outbox 指标（REC-3）：pending/inflight/failed/delivered/stale。

    - ``stale`` = ``status='inflight'`` 且租约已过期（owner 崩了或卡住；下一轮
      claim 会回收它，见 ``claim_outbox_batch``）。未迁移到 51 的旧库没有
      ``lease_until`` 列 ⇒ stale 记 0，其余计数照旧；
    - ``oldest_pending_age_seconds`` = 最旧 pending 行的年龄（无 pending 记 0）。
    """
    empty = {key: 0 for key in _COUNT_KEYS}
    with Session(engine) as session:
        if session.execute(text(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='event_outbox'"
        )).first() is None:
            return dict(empty)
        stale_expr = "0"
        if _has_claim_columns(session):
            stale_expr = (
                "SUM(CASE WHEN status='inflight' AND lease_until IS NOT NULL "
                "AND lease_until <= :now THEN 1 ELSE 0 END)"
            )
        row = session.execute(
            text(
                "SELECT "
                "COALESCE(SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END), 0) "
                "    AS pending, "
                "COALESCE(SUM(CASE WHEN status='inflight' THEN 1 ELSE 0 END), 0) "
                "    AS inflight, "
                "COALESCE(SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END), 0) "
                "    AS failed, "
                "COALESCE(SUM(CASE WHEN status='delivered' THEN 1 ELSE 0 END), 0) "
                "    AS delivered, "
                f"COALESCE({stale_expr}, 0) AS stale, "
                "MIN(CASE WHEN status='pending' THEN created_at END) "
                "    AS oldest_pending_at "
                "FROM event_outbox"
            ),
            {"now": now},
        ).mappings().first()
    counts = {
        key: int(row[key] or 0)
        for key in _COUNT_KEYS
        if key != "oldest_pending_age_seconds"
    }
    oldest = _parse_datetime(row["oldest_pending_at"])
    counts["oldest_pending_age_seconds"] = (
        max(0, int((now - oldest).total_seconds())) if oldest is not None else 0
    )
    return counts


def mark_outbox_delivered(engine: Any, ids: list[int], when: datetime) -> int:
    ids = [int(i) for i in ids if i]
    if not ids:
        return 0
    updated = 0
    with Session(engine) as session:
        # 结清字段一致性：delivered 也是「结清」，claim 字段必须一起清空
        # （与 mark_outbox_retry 同口径，否则 inflight 指标/claimed_by 会骗人）。
        # 未迁移到 51 的旧库没有这些列，条件拼装保持降级路径可用。
        clear_claim = (
            ", claimed_at=NULL, claimed_by='', lease_until=NULL"
            if _has_claim_columns(session)
            else ""
        )
        for row_id in ids:
            # delivered 是终态：只挡「已经销过账」的行（保住首个 delivered_at），
            # pending / failed 都可以转 delivered（事件确认在库里）。
            result = session.execute(
                text(
                    "UPDATE event_outbox SET status='delivered', "
                    "delivered_at=:when, last_error='', next_attempt_at=NULL"
                    f"{clear_claim} "
                    "WHERE id=:id AND status != 'delivered'"
                ),
                {"when": when, "id": row_id},
            )
            updated += int(result.rowcount or 0)
        session.commit()
    return updated


def mark_outbox_delivered_keys(
    engine: Any,
    *,
    platform: str,
    account_id: str,
    keys: list[tuple[str, str]],
    when: datetime,
) -> int:
    """直写成功后按 (chat_id, msg_id) 销账，避免 forwarder 再投一遍。"""
    if not keys:
        return 0
    updated = 0
    with Session(engine) as session:
        # 同 mark_outbox_delivered：结清时清空 claim 字段；旧库（无 claim 列）跳过。
        clear_claim = (
            ", claimed_at=NULL, claimed_by='', lease_until=NULL"
            if _has_claim_columns(session)
            else ""
        )
        for chat_id, msg_id in keys:
            # 同 mark_outbox_delivered：delivered 是终态，只挡已销账的行。
            result = session.execute(
                text(
                    "UPDATE event_outbox SET status='delivered', "
                    "delivered_at=:when, last_error='', next_attempt_at=NULL"
                    f"{clear_claim} "
                    "WHERE platform=:platform AND account_id=:account_id "
                    "  AND chat_id=:chat_id AND msg_id=:msg_id "
                    "  AND status != 'delivered'"
                ),
                {
                    "when": when,
                    "platform": str(platform or ""),
                    "account_id": str(account_id or ""),
                    "chat_id": str(chat_id or ""),
                    "msg_id": str(msg_id or ""),
                },
            )
            updated += int(result.rowcount or 0)
        session.commit()
    return updated


def mark_outbox_retry(
    engine: Any,
    row_id: int,
    *,
    next_attempt_at: datetime | None,
    error: str,
    failed: bool = False,
    claimed_by: str = "",
) -> int:
    """结清一次 claim 的投递结果；返回是否写入（0 = 租约已易主/行已销账）。

    谓词是 ``status='inflight' AND claimed_by=:claimed_by``（REC-8）：只有**自己
    那一份租约**的行才允许被重写。此前 ``status='pending'`` 的守卫挡不住
    「另一条路径拿旧快照把 inflight 行写回 pending」，也没规定谁有权写。

    ``attempts`` 不在这里写：夺取时已由 ``claim_outbox_batch`` 原子 +1，重复写
    就会回到「绝对值覆盖」的老毛病（并发漏计）。``failed=True`` 表示到达止损
    线，落 failed 终态。成功结清后清空 claim 字段，``pending`` 行的
    ``claimed_by`` 不留旧值（否则 inflight 指标会骗人）。
    """
    with Session(engine) as session:
        result = session.execute(
            text(
                "UPDATE event_outbox SET status=:status, "
                "next_attempt_at=:next_at, last_error=:error, "
                "claimed_at=NULL, claimed_by='', lease_until=NULL "
                "WHERE id=:id AND status = 'inflight' AND claimed_by = :who"
            ),
            {
                "status": "failed" if failed else "pending",
                "next_at": next_attempt_at,
                "error": str(error or "")[:300],
                "id": int(row_id),
                "who": str(claimed_by or ""),
            },
        )
        session.commit()
        return int(result.rowcount or 0)


def mark_outbox_retry_legacy(
    engine: Any,
    row_id: int,
    *,
    attempts: int,
    next_attempt_at: datetime | None,
    error: str,
    failed: bool = False,
) -> None:
    """未迁移到 51 的旧库降级路径：attempts 绝对值写入 + 只挡 pending。

    旧库没有 claim 列，``list_pending_outbox`` 读到的就是 pending 行，因此仍用
    REC-1 的 ``status='pending'`` 守卫挡「已到止损线的行被旧快照打回 pending」。
    新库一律走 ``mark_outbox_retry``。
    """
    with Session(engine) as session:
        session.execute(
            text(
                "UPDATE event_outbox SET status=:status, attempts=:attempts, "
                "next_attempt_at=:next_at, last_error=:error "
                "WHERE id=:id AND status = 'pending'"
            ),
            {
                "status": "failed" if failed else "pending",
                "attempts": int(attempts),
                "next_at": next_attempt_at,
                "error": str(error or "")[:300],
                "id": int(row_id),
            },
        )
        session.commit()

