"""事件 outbox 的 forwarder（account.db → meta.db terminal_events）。

- **幂等**：投递走 ``record_message_events``（四元组幂等），再查库复核；
- **行级所有权（REC-8）**：先 ``claim_outbox_batch`` 原子夺取（status 转
  inflight + 租约 + ``attempts`` 自增），只有夺到的行才计入 ``picked`` 并投递；
  两条并发 drain（周期 job / 同步路径）不会各写一份 ``attempts`` 绝对值
  （漏计、绕过止损），也不会重复投递同一行。未迁移到 51 的旧库降级为旧的纯读
  路径（只告警，不抛错）；
- **失败可见**：``attempts`` 是投递尝试次数；失败按退避写 ``next_attempt_at``；
  到 max_attempts 标 failed 并打 error 日志（供告警）；租约过期的 inflight 行
  一律可回收（P1-a），哪怕 attempts 已到止损线也会被收尾成 failed —— 不能让它
  永久卡在 inflight（doctor 恒定 stale，口径②又不发通知）；
- **不重放命令**：重试只补事件。``command_records`` 是 best-effort 投影，
  ``command_id`` 每次调用新生成 uuid，重放会造重复行。

调用点：``core/ingestion/ingestors._trace_terminal_shadow``（每次同步/接收
后顺带 drain）；对账器（reconciler）仍是扫缺兜底。
"""

from __future__ import annotations

import os
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import structlog

from db.outbox_store import (
    claim_outbox_batch,
    list_pending_outbox,
    mark_outbox_delivered,
    mark_outbox_delivered_keys,
    mark_outbox_retry,
    mark_outbox_retry_legacy,
    outbox_claim_ready,
)
from safelog import warn

log = structlog.get_logger()

_MAX_BACKOFF_SECONDS = 300
# 一次投递最多占这么多秒；租约过期即视为 owner 崩了/卡住，下一轮 drain 回收。
_OUTBOX_LEASE_SECONDS = 120


def backoff_seconds(attempts: int) -> int:
    """5s 起步、翻倍、封顶 300s。"""
    attempts = max(1, int(attempts or 1))
    return min(_MAX_BACKOFF_SECONDS, 5 * (2 ** (attempts - 1)))


@dataclass
class OutboxDrainReport:
    platform: str = ""
    account_id: str = ""
    picked: int = 0
    delivered: int = 0
    retried: int = 0
    failed: int = 0
    missing_message: int = 0
    delivered_ids: dict[tuple[str, str], str] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "platform": self.platform,
            "account_id": self.account_id,
            "picked": self.picked,
            "delivered": self.delivered,
            "retried": self.retried,
            "failed": self.failed,
            "missing_message": self.missing_message,
            "delivered_ids": len(self.delivered_ids),
        }


def _raw_key(raw: dict) -> tuple[str, str]:
    return (
        str(raw.get("chat_id") or ""),
        str(raw.get("msg_id") or raw.get("id") or ""),
    )


def drain_outbox(
    *,
    repo: Any,
    platform: str,
    account_id: str,
    store: Any,
    identity_service: Any | None = None,
    limit: int = 200,
    max_attempts: int = 5,
    now: datetime | None = None,
) -> OutboxDrainReport:
    """把到期的行投递到事件流（幂等、带退避与止损）。

    REC-8：先按行夺取所有权（``claim_outbox_batch``，``attempts`` 在夺取时原
    子 +1），只有本执行者夺到的行才计入 ``picked``；结清时按 ``claimed_by``
    校验，别人夺走的行不会被本执行者的旧快照改写。未迁移到 51 的旧库退回旧
    的纯读 + 绝对值写入路径（``log.warning("outbox_claim_unavailable")``）。
    """
    now = now or datetime.now()
    report = OutboxDrainReport(platform=platform, account_id=account_id)
    # 每次 claim 带 uuid nonce：同一进程同一线程重夺（租约过期回收）之后，
    # 旧 claim 的迟到写不能再命中 ``claimed_by`` 守卫（只用 pid/线程标识的话，
    # 新旧 claim 是同一个字符串，老 owner 能把新 owner 的行写坏）。
    claimed_by = (
        f"{platform}/{account_id}/pid{os.getpid()}/thr{threading.get_ident()}"
        f"/claim{uuid.uuid4().hex[:8]}"
    )
    claim_ready = outbox_claim_ready(repo.engine)
    if claim_ready:
        rows = claim_outbox_batch(
            repo.engine,
            platform=platform,
            account_id=account_id,
            now=now,
            claimed_by=claimed_by,
            lease_seconds=_OUTBOX_LEASE_SECONDS,
            limit=limit,
            max_attempts=max_attempts,
        )
    else:
        log.warning(
            "outbox_claim_unavailable",
            platform=platform,
            account_id=account_id,
            hint="account DB 未迁移到 51；退回旧读路径，无行级所有权",
        )
        rows = list_pending_outbox(
            repo.engine,
            platform=platform,
            account_id=account_id,
            now=now,
            limit=limit,
        )
    report.picked = len(rows)
    if not rows:
        return report

    def _attempts(row: dict) -> int:
        """本次投递的尝试序号：新库由 claim 加过，旧库在这里 +1。"""
        value = int(row["attempts"] or 0)
        return value if claim_ready else value + 1

    def _settle(row_id: int, *, attempts: int, failed: bool,
                next_at: datetime | None, error: str) -> None:
        # P1-a：到止损线一律落 failed，不再退回 pending。回收「attempts 已满的
        # 过期 inflight」正是走这条分支：退回去的话下一次 claim 的 pending 门
        # 又会挡住它，行就永远结不了（doctor 恒定 stale，口径②又不发通知）。
        if attempts >= max_attempts:
            failed = True
            next_at = None
        if claim_ready:
            mark_outbox_retry(
                repo.engine,
                row_id,
                next_attempt_at=next_at,
                error=error,
                failed=failed,
                claimed_by=claimed_by,
            )
        else:
            mark_outbox_retry_legacy(
                repo.engine,
                row_id,
                attempts=attempts,
                next_attempt_at=next_at,
                error=error,
                failed=failed,
            )

    payloads: list[dict] = []
    pending: list[tuple[dict, dict]] = []
    for row in rows:
        msg = repo.messages.find_by_identity(
            chat_id=row["chat_id"],
            msg_id=row["msg_id"],
            source=row.get("platform") or None,
            account_id=row.get("account_id") or None,
        )
        if msg is None:
            report.missing_message += 1
            _settle(
                row["id"],
                attempts=_attempts(row),
                failed=True,
                next_at=None,
                error="message_row_missing",
            )
            log.error(
                "outbox_message_missing",
                platform=platform,
                account_id=account_id,
                chat_id=row["chat_id"],
                msg_id=row["msg_id"],
            )
            continue
        raw = repo.messages.to_dict(msg)
        payloads.append(raw)
        pending.append((row, raw))

    if payloads:
        from core.terminal.shadow import record_message_events

        record_message_events(
            payloads,
            platform=platform,
            account_id=account_id,
            identity_service=identity_service,
            store=store,
            return_events=False,
        )
        lookup = getattr(store, "recorded_message_event_ids", None)
        recorded: dict[tuple[str, str], str] = {}
        if lookup is not None:
            try:
                recorded = lookup(platform=platform, bot_account_id=account_id)
            except Exception as exc:  # noqa: BLE001 - 复核失败按未投递处理
                log.warning(
                    "outbox_verify_failed",
                    platform=platform,
                    account_id=account_id,
                    error=str(exc)[:200],
                )
        delivered_ids: list[int] = []
        for row, raw in pending:
            event_id = recorded.get(_raw_key(raw), "")
            if event_id:
                delivered_ids.append(row["id"])
                report.delivered += 1
                report.delivered_ids[_raw_key(raw)] = event_id
                continue
            attempts = _attempts(row)
            if attempts >= max_attempts:
                report.failed += 1
                _settle(
                    row["id"],
                    attempts=attempts,
                    failed=True,
                    next_at=None,
                    error="delivery_failed_max_attempts",
                )
                log.error(
                    "outbox_delivery_failed",
                    platform=platform,
                    account_id=account_id,
                    chat_id=row["chat_id"],
                    msg_id=row["msg_id"],
                    attempts=attempts,
                )
            else:
                report.retried += 1
                _settle(
                    row["id"],
                    attempts=attempts,
                    failed=False,
                    next_at=now + timedelta(seconds=backoff_seconds(attempts)),
                    error="delivery_failed",
                )
                log.warning(
                    "outbox_delivery_retry",
                    platform=platform,
                    account_id=account_id,
                    chat_id=row["chat_id"],
                    msg_id=row["msg_id"],
                    attempts=attempts,
                )
        if delivered_ids:
            mark_outbox_delivered(repo.engine, delivered_ids, now)

    log.info(
        "outbox_drained",
        platform=platform,
        account_id=account_id,
        picked=report.picked,
        delivered=report.delivered,
        retried=report.retried,
        failed=report.failed,
        missing_message=report.missing_message,
    )
    return report


def settle_outbox_batch(
    *,
    repo: Any,
    platform: str,
    account_id: str,
    store: Any,
    messages: list[dict],
    now: datetime | None = None,
) -> int:
    """直写成功后，把本批「已落地」的 outbox 行销账（forwarder 不再重投）。"""
    lookup = getattr(store, "recorded_message_event_ids", None)
    if lookup is None:
        return 0
    try:
        recorded = lookup(platform=platform, bot_account_id=account_id)
    except Exception:  # noqa: BLE001 - 销账失败留给 forwarder 幂等重投
        warn("core/terminal/outbox.py:301 Exception", err=repr(__import__("sys").exc_info()[1]))
        return 0
    keys = [
        _raw_key(raw)
        for raw in messages
        if isinstance(raw, dict) and _raw_key(raw) in recorded
    ]
    if not keys:
        return 0
    return mark_outbox_delivered_keys(
        repo.engine,
        platform=platform,
        account_id=account_id,
        keys=keys,
        when=now or datetime.now(),
    )
