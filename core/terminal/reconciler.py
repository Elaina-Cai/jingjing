"""消息事件对账器（任务模型 §7.1 ④，2026-09-28）。

扫 account.db 的 ``messages`` 与 meta 的 ``terminal_events``，按
``(platform, bot_account_id, chat_id, external_msg_id)`` 找缺口：

- **dry-run（默认）**：只报告，不写任何东西；
- **apply**：用 ``record_message_events`` 幂等补写（不重放命令记录），
  补完后再查库复核 inserted / failed，不以返回值自证。

另有 **sequence 体检**（B6，2026-09-30，:func:`reconcile_sequences`）：报
``terminal_events.sequence`` 的「白名单外新缺口」与「重号」，**只报告不修改**
（存量 571 个空缺 + 8 个重号是 2026-09-21 测试事件清理的既成事实，白名单见
``core.terminal.sequence_policy``；补号 / 重排等于改写事实层，不做）。

边界：
- 只补消息类事件；出站回复（reply）等不属于这里的缺口；
- 补写顺序按 ``created_at`` 从旧到新（分页读是倒序，收集后整体反转），
  让追加进事件流的 sequence 顺序尽量贴近发生顺序；
- 对账器是兜底，不是替身：写的可见性修复仍在注入端（outbox 待做）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Sequence

import structlog

from core.terminal.sequence_policy import (
    SEQUENCE_DUPLICATE_WHITELIST,
    SEQUENCE_GAP_WHITELIST,
    count_numbers,
    find_duplicates,
    find_gap_ranges,
    is_whitelisted_duplicate,
    split_gap_ranges,
)
from safelog import warn

log = structlog.get_logger()


@dataclass
class ReconcileReport:
    platform: str = ""
    account_id: str = ""
    scanned: int = 0
    matched: int = 0
    missing: int = 0
    inserted: int = 0
    failed: int = 0
    skipped_no_msg_id: int = 0
    applied: bool = False
    by_chat: dict[str, int] = field(default_factory=dict)
    samples: list[dict] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.failed == 0

    def as_dict(self) -> dict:
        return {
            "platform": self.platform,
            "account_id": self.account_id,
            "scanned": self.scanned,
            "matched": self.matched,
            "missing": self.missing,
            "inserted": self.inserted,
            "failed": self.failed,
            "skipped_no_msg_id": self.skipped_no_msg_id,
            "applied": self.applied,
            "by_chat": dict(sorted(self.by_chat.items(), key=lambda kv: -kv[1])),
            "samples": list(self.samples),
            "ok": self.ok,
        }


@dataclass
class SequenceReport:
    """``terminal_events.sequence`` 体检报告（B6，**只报告不修改**）。

    口径（见 ``core.terminal.sequence_policy``）：

    - ``sequence_gaps``：**白名单之外**的新缺口号数（应为 0，>0 才是回归）；
    - ``sequence_whitelisted_gaps``：白名单内的存量空缺号数（571，已知事实，不告警）；
    - ``sequence_duplicates``：全部重号个数（含存量 8 个）；
    - ``sequence_new_duplicates``：不在存量名单里的重号个数（应为 0）；
    - ``error``：读不到 sequence 时的错误——读不到 ≠ 无缺口，此时 ``ok`` 为 False。
    """

    rows: int = 0
    max_sequence: int = 0
    sequence_gaps: int = 0
    sequence_gap_ranges: list[dict] = field(default_factory=list)
    sequence_whitelisted_gaps: int = 0
    sequence_whitelisted_ranges: list[dict] = field(default_factory=list)
    sequence_duplicates: int = 0
    sequence_new_duplicates: int = 0
    duplicate_samples: list[dict] = field(default_factory=list)
    zero_sequence_rows: int = 0
    error: str = ""

    @property
    def ok(self) -> bool:
        return (
            not self.error
            and self.sequence_gaps == 0
            and self.sequence_new_duplicates == 0
        )

    def as_dict(self) -> dict:
        return {
            "rows": self.rows,
            "max_sequence": self.max_sequence,
            "sequence_gaps": self.sequence_gaps,
            "sequence_gap_ranges": list(self.sequence_gap_ranges),
            "sequence_whitelisted_gaps": self.sequence_whitelisted_gaps,
            "sequence_whitelisted_ranges": list(self.sequence_whitelisted_ranges),
            "sequence_duplicates": self.sequence_duplicates,
            "sequence_new_duplicates": self.sequence_new_duplicates,
            "duplicate_samples": list(self.duplicate_samples),
            "zero_sequence_rows": self.zero_sequence_rows,
            "gap_whitelist": [list(item) for item in SEQUENCE_GAP_WHITELIST],
            "duplicate_whitelist": list(SEQUENCE_DUPLICATE_WHITELIST),
            "error": self.error,
            "ok": self.ok,
        }


def reconcile_sequences(store: Any, *, sample_limit: int = 10) -> SequenceReport:
    """扫全表 ``sequence``：报「白名单外新缺口」与「重号」，**不修**。

    ``store`` 需要提供 ``sequence_rows()``（``TerminalEventStore`` 有）。
    读失败时报告里带 ``error`` 且 ``ok=False``：读不到就说不确定，不许当成「没缺口」。
    """
    report = SequenceReport()
    reader = getattr(store, "sequence_rows", None)
    if reader is None:
        report.error = "store 没有 sequence_rows()：取不到 sequence，不当作无缺口"
        log.warning(
            "reconciler_sequence_reader_missing", store=type(store).__name__
        )
        return report
    try:
        rows = [
            (int(sequence or 0), str(event_id or ""))
            for sequence, event_id in reader()
        ]
    except Exception as exc:  # noqa: BLE001 - 读不到要在报告里可见，不炸调用方
        report.error = f"{type(exc).__name__}: {exc}"[:200]
        log.warning("reconciler_sequence_read_failed", error=report.error)
        return report

    sequences = [sequence for sequence, _ in rows]
    report.rows = len(rows)
    report.max_sequence = max(sequences) if sequences else 0
    report.zero_sequence_rows = sum(1 for value in sequences if value <= 0)

    new_gaps, known_gaps = split_gap_ranges(find_gap_ranges(sequences))
    limit = max(0, int(sample_limit))
    report.sequence_gaps = count_numbers(new_gaps)
    report.sequence_gap_ranges = [item.as_dict() for item in new_gaps[:limit]]
    report.sequence_whitelisted_gaps = count_numbers(known_gaps)
    report.sequence_whitelisted_ranges = [item.as_dict() for item in known_gaps]

    duplicates = find_duplicates(sequences)
    by_sequence: dict[int, list[str]] = {}
    for sequence, event_id in rows:
        by_sequence.setdefault(sequence, []).append(event_id)
    report.sequence_duplicates = len(duplicates)
    new_duplicates = [
        sequence
        for sequence in duplicates
        if not is_whitelisted_duplicate(sequence)
    ]
    report.sequence_new_duplicates = len(new_duplicates)
    legacy_duplicates = [
        sequence for sequence in duplicates if is_whitelisted_duplicate(sequence)
    ]
    for sequence in (new_duplicates + legacy_duplicates)[:limit]:
        report.duplicate_samples.append(
            {
                "sequence": sequence,
                "count": duplicates[sequence],
                "whitelisted": is_whitelisted_duplicate(sequence),
                "event_ids": by_sequence.get(sequence, [])[:4],
            }
        )
    if not report.ok:
        log.warning(
            "reconciler_sequence_regression",
            gaps=report.sequence_gaps,
            new_duplicates=report.sequence_new_duplicates,
            rows=report.rows,
        )
    return report


def _row_key(raw: dict) -> tuple[str, str]:
    return (
        str(raw.get("chat_id") or ""),
        str(raw.get("msg_id") or raw.get("id") or ""),
    )


def _existing_key_set(
    store: Any, platform: str, account_id: str
) -> set[tuple[str, str]] | None:
    """库内已记录的四元组键集合；拿不到时返回 None（退化为逐个查）。"""
    lister = getattr(store, "recorded_message_keys", None)
    if lister is None:
        lister = getattr(store, "external_msg_ids", None)
    if lister is None:
        return None
    try:
        return set(lister(platform=platform, bot_account_id=account_id))
    except Exception as exc:  # noqa: BLE001 - 对账器要能退化运行
        log.warning(
            "reconciler_existing_lookup_failed",
            platform=platform,
            account_id=account_id,
            error=str(exc)[:200],
        )
        return None


def _is_recorded(
    store: Any,
    *,
    platform: str,
    account_id: str,
    chat_id: str,
    msg_id: str,
    existing: set[tuple[str, str]] | None,
) -> bool:
    if existing is not None:
        return (chat_id, msg_id) in existing
    lookup = getattr(store, "get_by_external_msg_id", None)
    if lookup is None:
        return False
    try:
        return lookup(
            platform=platform,
            bot_account_id=account_id,
            chat_id=chat_id,
            external_msg_id=msg_id,
        ) is not None
    except Exception:  # noqa: BLE001
        warn("core/terminal/reconciler.py:248 Exception", err=repr(__import__("sys").exc_info()[1]))
        return False


def _add_missing_sample(report: ReconcileReport, raw: dict, limit: int) -> None:
    if len(report.samples) >= limit:
        return
    report.samples.append(
        {
            "chat_id": str(raw.get("chat_id") or ""),
            "msg_id": str(raw.get("msg_id") or raw.get("id") or ""),
            "created_at": str(raw.get("created_at") or ""),
            "sender_id": str(raw.get("sender_id") or ""),
            "msg_type_name": str(raw.get("msg_type_name") or ""),
            "content": str(raw.get("content") or "")[:40],
        }
    )


def _split_present(
    store: Any,
    *,
    platform: str,
    account_id: str,
    keys: Sequence[tuple[str, str]],
    existing: set[tuple[str, str]] | None,
) -> tuple[int, list[tuple[str, str]]]:
    """复核补写结果：返回 (已在库内数量, 仍缺键列表)。"""
    if existing is not None:
        absent = [key for key in keys if key not in existing]
        return len(keys) - len(absent), absent
    present = 0
    absent = []
    for key in keys:
        if _is_recorded(
            store,
            platform=platform,
            account_id=account_id,
            chat_id=key[0],
            msg_id=key[1],
            existing=None,
        ):
            present += 1
        else:
            absent.append(key)
    return present, absent


def _apply_missing(
    report: ReconcileReport,
    rows: list[dict],
    *,
    platform: str,
    account_id: str,
    store: Any,
    identity_service: Any | None,
) -> None:
    """幂等补写并复核（不重放命令记录：command_recorder 不传）。

    先修旧代码留下的空 ``external_msg_id``（从 JSON 回填），这样同一消息
    不会因实列为空被当成缺口而补出第二份事件。
    """
    from core.terminal.shadow import record_message_events

    repair = getattr(store, "backfill_external_msg_ids", None)
    if repair is not None:
        try:
            repaired = int(
                repair(platform=platform, bot_account_id=account_id) or 0
            )
            if repaired:
                log.info(
                    "reconciler_external_ids_repaired",
                    platform=platform,
                    account_id=account_id,
                    count=repaired,
                )
        except Exception as exc:  # noqa: BLE001 - 修复失败不阻塞补写
            log.warning(
                "reconciler_external_id_repair_failed",
                platform=platform,
                account_id=account_id,
                error=str(exc)[:200],
            )

    record_message_events(
        rows,
        platform=platform,
        account_id=account_id,
        identity_service=identity_service,
        store=store,
        return_events=False,
    )
    keys = [_row_key(raw) for raw in rows]
    after = _existing_key_set(store, platform, account_id)
    present, absent = _split_present(
        store,
        platform=platform,
        account_id=account_id,
        keys=keys,
        existing=after,
    )
    report.applied = True
    report.inserted = present
    report.failed = len(absent)
    for chat_id, msg_id in absent:
        log.warning(
            "reconciler_insert_failed",
            platform=platform,
            account_id=account_id,
            chat_id=chat_id,
            msg_id=msg_id,
        )


def reconcile_messages(
    messages: Sequence[dict],
    *,
    platform: str,
    account_id: str,
    store: Any,
    identity_service: Any | None = None,
    apply: bool = False,
    sample_limit: int = 10,
) -> ReconcileReport:
    """对一批已 ``to_dict`` 的消息（按时间升序）做对账 / 补写。"""
    report = ReconcileReport(platform=platform, account_id=account_id)
    existing = _existing_key_set(store, platform, account_id)
    missing_rows: list[dict] = []
    for raw in messages:
        if not isinstance(raw, dict):
            continue
        report.scanned += 1
        chat_id, msg_id = _row_key(raw)
        if not msg_id:
            report.skipped_no_msg_id += 1
            continue
        if _is_recorded(
            store,
            platform=platform,
            account_id=account_id,
            chat_id=chat_id,
            msg_id=msg_id,
            existing=existing,
        ):
            report.matched += 1
            continue
        report.missing += 1
        report.by_chat[chat_id] = report.by_chat.get(chat_id, 0) + 1
        _add_missing_sample(report, raw, sample_limit)
        missing_rows.append(raw)
    if apply:
        _apply_missing(
            report,
            missing_rows,
            platform=platform,
            account_id=account_id,
            store=store,
            identity_service=identity_service,
        )
    return report


def reconcile_account(
    *,
    repo: Any,
    platform: str,
    account_id: str,
    store: Any,
    identity_service: Any | None = None,
    apply: bool = False,
    chat_id: str = "",
    since: datetime | None = None,
    page_size: int = 2000,
    max_messages: int | None = None,
    sample_limit: int = 10,
) -> ReconcileReport:
    """扫一个账号库（分页），收集缺口后按时间升序补写。

    ``repo`` 是 ``AccountRepositoryManager``（或提供同形 ``messages`` 的对象）。
    """
    report = ReconcileReport(platform=platform, account_id=account_id)
    existing = _existing_key_set(store, platform, account_id)
    missing_rows: list[dict] = []
    offset = 0
    remaining = int(max_messages) if max_messages else None
    while True:
        limit = max(1, int(page_size))
        if remaining is not None:
            if remaining <= 0:
                break
            limit = min(limit, remaining)
        rows = repo.messages.get_recent(
            limit=limit,
            offset=offset,
            chat_id=chat_id or None,
            since=since,
        )
        if not rows:
            break
        for row in rows:
            raw = repo.messages.to_dict(row)
            if not isinstance(raw, dict):
                continue
            report.scanned += 1
            chat_key, msg_key = _row_key(raw)
            if not msg_key:
                report.skipped_no_msg_id += 1
                continue
            if _is_recorded(
                store,
                platform=platform,
                account_id=account_id,
                chat_id=chat_key,
                msg_id=msg_key,
                existing=existing,
            ):
                report.matched += 1
                continue
            report.missing += 1
            report.by_chat[chat_key] = report.by_chat.get(chat_key, 0) + 1
            _add_missing_sample(report, raw, sample_limit)
            missing_rows.append(raw)
        offset += len(rows)
        if remaining is not None:
            remaining -= len(rows)
        if len(rows) < limit:
            break
    if apply:
        if missing_rows:
            missing_rows.reverse()  # 分页「新→旧」，反转成「旧→新」再补。
        _apply_missing(
            report,
            missing_rows,
            platform=platform,
            account_id=account_id,
            store=store,
            identity_service=identity_service,
        )
    return report
