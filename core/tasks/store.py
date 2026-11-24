"""P1：``work_items`` / ``work_item_runs`` 写入（``docs/任务模型.md`` §7.1 ①）。

写序（**同库单事务**）
--------------------

meta.db 里 ``terminal_events`` 与 ``work_items`` / ``work_item_runs`` 在**同一个
session / 事务**里 commit：事件是事实、投影可重建，失败整体回滚，不留半条
（§7.1 ①）。事件行由 owner 的同事务入口
:func:`~core.terminal.event_store_db.append_in_session` 写：它在本模块自开的
``Session(engine)``（台账行与事件行共用这一个事务）里 INSERT，**并执行 owner 的
全部保护**（``event_id`` 幂等提前返回 / ``external_msg_id`` 四元组回查 /
``RETURNING sequence`` 非空校验）。**不走** ``TerminalEventStore.append()``：它
**另开** session 并自行 commit，两条 commit 之间会留下「事件已落、投影没落」的
窗口，正是 §7.1 ① 要排除的分叉。取号仍在 INSERT 语句内部（``MAX(sequence)+1``，
B6），所以多进程也不会重号。

单向投影
--------

P1 期间 ``reminders`` 表 + ``run_reminder_delivery`` 是**唯一**执行权威，本模块只把
reminders 侧的事实投影成 work_items / work_item_runs（``reminders → work_items``，
**绝不反向写 reminders**），因此不存在第二条投递路径。A4（取消 / 改期入口）、
A1-d（执行回执接线）、``answer`` 入表都不在这里。

事件契约（§5.9）
----------------

* payload 必带 ``schema_version``；``trace_ref`` 一律 ``terminal_event:<event_id>``
  （**不用 sequence**：存量有重号与空缺，它不是身份，§5.10）；
* **事件行的实列 ``external_msg_id`` 必须留空**：``TerminalEventStore.append()`` /
  :func:`~core.terminal.event_store_db.append_in_session` 按四元组
  ``(platform, bot_account_id, chat_id, external_msg_id)`` 查重，命中就
  **静默返回已存行、不追加**——一旦把触发消息的 msg_id 写进实列，
  ``work_item_created`` 就会被那条消息事件吞掉（append 返回的也是消息事件的 id）。
  触发消息 id 只进 payload 的 ``external_msg_id`` 字段与 ``metadata``；``work_items``
  行的 ``external_msg_id`` 照常写（那是投影列，不参与事件查重）；
* ``parent_event_id`` = 触发消息的事件 id、``turn_id`` = ``ctx.extra`` 的 turn_id、
  ``actor_person_id`` = 请求人；群聊 ``visibility_scope=group_members``、私聊
  ``issuer_only``（照 ``core/terminal/thread...`` 同族写法，
  ``core/terminal/shadow.py`` 的采集侧同一口径）;
* ``work_items.source_event_id`` 记**本行投影的来源事件**（即 ``work_item_created``
  的 event_id），触发消息事件 id 在该事件的 ``parent_event_id`` 与 payload 的
  ``trigger_event_id`` 上：这样「行 → 事件 → 触发消息」一跳就能走通。§5.1 的
  ``trigger_event_id`` 就是为这个歧义留的字段。

flag
----

``WORK_ITEM_LEDGER`` 默认**关**（关 = 一次 env 读取，零写入零开销）；
``WORK_ITEM_RUNS`` 默认**关**且要求 LEDGER 同时开——否则一条 run 会没有它的
台账行（孤儿）。回滚 = 对应 flag 置 0 → 重启。
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from core import features
from core.tasks.mapping import (
    DEFAULT_MAX_ATTEMPTS,
    SPEC_SCHEMA_VERSION,
    MappedReminder,
    log_mapping_warnings,
    map_reminder_row,
)
from core.tasks.spec_schema import SpecSchemaError, validate_spec
from core.terminal.event_model import Event, EventType
from core.terminal.event_store_db import append_in_session
from core.terminal.principal import VisibilityScope
from db.meta_models import WorkItemRow, WorkItemRunRow
from safelog import warn

log = structlog.get_logger()

#: §5.5 的状态集。**以 §5.5 为准**：§4.1 与 §4.4 只列 5 个，少了
#: ``waiting_user`` / ``superseded``，文档两处枚举不一致处按 §5.5 执行。
WORK_ITEM_STATUSES: tuple[str, ...] = (
    "open",
    "running",
    "waiting_user",
    "done",
    "failed",
    "cancelled",
    "superseded",
)
#: §4.2 的 outcome 取值。
RUN_OUTCOMES: tuple[str, ...] = ("sent", "silent", "failed", "blocked")
#: §4.2 的 trigger_source 取值。
TRIGGER_SOURCES: tuple[str, ...] = ("message", "timer", "event", "manual")
#: §4.3 完成判据：``notify`` / ``act`` 的 ``done`` 由发送结果（``send_log.ok``）决定。
ACCEPTANCE_DELIVERY_OK = "send_log.ok"
#: N5 参数收集（``kind="act"`` 的 slot-fill 行）的验收判据：**引用对上的确认**
#: （`docs/decisions/架构评估.md:278` R-P4 硬条款）。与投递判据不同源——那一条管"发出去没有"，
#: 这一条管"对方确认的到底是不是这一件"。
ACCEPTANCE_SLOT_FILL = "slot_fill.confirm_ref_matched"
#: §5.5 的 claim_owner：P1 只有一个投递方（scheduler 的每分钟提醒扫描）。
REMINDER_CLAIM_OWNER = "reminder_delivery"
#: §5.5 失败分类默认值：P1 的投递失败一律按可重试处理（reminders 重试 ≤3 次止损）。
DEFAULT_ERROR_CLASS = "transient"
#: 重试间隔：镜像提醒扫描器的节奏（``run_reminder_delivery`` 每分钟一次）。
RETRY_BACKOFF_SECONDS = 60
#: 来源库标签：META 库（工具双写与 META 存量提醒都算这一档）。
META_SOURCE = "meta"


# --------------------------------------------------------------------- flags
def ledger_enabled() -> bool:
    """``WORK_ITEM_LEDGER`` 是否开启（默认关）。"""
    return features.enabled(features.WORK_ITEM_LEDGER, False)


def runs_enabled() -> bool:
    """``WORK_ITEM_RUNS`` 是否开启：**必须 LEDGER 同时开**（否则不写孤儿 run）。"""
    return ledger_enabled() and features.enabled(features.WORK_ITEM_RUNS, False)


# ------------------------------------------------------- B5：spec_json 写入门
def _spec_json_ok(kind: str, spec: Any, *, where: str) -> bool:
    """写前校验 ``spec_json``（B5）：不合规**不写**（返回 ``False`` + 一条 ``work_item_spec_invalid`` 告警）。

    唯一判据是 :func:`core.tasks.spec_schema.validate_spec`——本函数不另写规则，
    只决定「拒了以后怎么办」。三个写入点的既有契约都是「台账写不进去不打断调用方」
    （返回 ``None``），所以这里给布尔而不抛；要大声失败的迁移路径走
    :func:`_spec_json_required`。
    """
    errors = validate_spec(kind, spec)
    if not errors:
        return True
    log.warning(
        "work_item_spec_invalid",
        where=where,
        kind=str(kind),
        errors=list(errors),
    )
    return False


def _spec_json_required(kind: str, spec: Any, *, where: str) -> None:
    """迁移路径的同一道校验：不合规 ⇒ **抛**（脚本按 ``failed`` 记账 + 非零退出码）。

    返回 ``None`` 在 ``scripts/migrate_reminders_to_work_items.py:221-223`` 里既不算
    插入也不算失败——存量行会**静默消失**，那正是 B2「不跳过不丢」的反面。
    """
    errors = validate_spec(kind, spec)
    if not errors:
        return
    log.warning(
        "work_item_spec_invalid",
        where=where,
        kind=str(kind),
        errors=list(errors),
    )
    raise SpecSchemaError(kind, errors)


# ------------------------------------------------------------------ creation
def _goal_id_for_reminder(row: Any) -> str:
    """提醒行 → 它对应的目标 id（``reminder:<id>@<due>``）。

    只在意图功能开启时给值：flag off 时根本不会开目标，挂一个悬空引用比空串更坏。
    新字段先落事件 payload（模块 docstring 的规则），稳定后再提列。
    """
    legacy_id = _int_or_none(_row_value(row, "id"))
    if legacy_id is None:
        return ""
    try:
        from core.context.intention_bridge import (
            reminder_intention_key,
            reminder_intentions_enabled,
        )

        if not reminder_intentions_enabled():
            return ""
        return str(
            reminder_intention_key(legacy_id, _row_value(row, "remind_at"))
        )
    except Exception as exc:  # noqa: BLE001 - 关联失败不影响台账写入
        log.warning("work_item_goal_link_failed", error=str(exc)[:120])
        return ""


def record_created(
    repo: Any,
    row: Any,
    *,
    extra: Mapping[str, Any] | None = None,
    messages: Sequence[Any] | None = None,
    now: datetime | None = None,
) -> str | None:
    """把一条新建的 ``reminders`` 行投影成 ``work_items`` + ``work_item_created``。

    **同库单事务**：行与事件在同一个 session 里提交（见模块 docstring）。返回
    ``work_item_id``；flag 关、拿不到 meta engine、``spec_json`` 不合规（B5，
    :func:`_spec_json_ok`）或该 ``legacy_reminder_id`` 已经投影过（重放）时返回已有 /
    ``None``，都不抛错——调用方（创建提醒工具）只把它当作 best-effort 的观测，
    投影失败绝不能影响创建结果。

    ``extra`` / ``messages`` 是这一轮的上下文（``ctx.extra`` / ``ctx.messages``）：
    ``turn_id`` / ``parent_event_id``（A1-c）与触发消息的 ``msg_id`` 从这里取。
    """
    if not ledger_enabled():
        return None
    engine = resolve_engine(repo)
    if engine is None:
        log.warning("work_item_write_failed", error="meta_engine_unresolved")
        return None
    mapped = map_reminder_row(row)
    log_mapping_warnings(mapped)
    # B5：kind 级 schema 的**写入点消费**——不合规不写（见 core/tasks/spec_schema.py）。
    if not _spec_json_ok(mapped.kind, mapped.spec_json, where="record_created"):
        return None
    legacy_id = _int_or_none(_row_value(row, "id"))
    anchor = {
        **_creation_anchors(engine, mapped, extra=extra, messages=messages),
        "goal_id": _goal_id_for_reminder(row),
    }
    fields = build_created_fields(
        mapped, legacy_reminder_id=legacy_id, anchor=anchor, now=now
    )
    event = _created_event(fields, anchor=anchor, goal=mapped.goal)
    with Session(engine) as session:
        existing = _find_existing_id(session, legacy_id, META_SOURCE)
        if existing:
            log.info(
                "work_item_created_duplicate",
                legacy_reminder_id=legacy_id,
                work_item_id=existing,
            )
            return existing
        _persist_created(session, fields, event)
        session.commit()
    log.info(
        "work_item_created",
        work_item_id=fields["work_item_id"],
        legacy_reminder_id=legacy_id,
        kind=fields["kind"],
        trigger=fields["trigger"],
        status=fields["status"],
        event_id=event.event_id,
    )
    return str(fields["work_item_id"])


def record_migrated(
    engine: Any,
    row: Any,
    *,
    source_db: str,
    now: datetime | None = None,
) -> str | None:
    """迁移一条存量 ``reminders`` 行（``scripts/migrate_reminders_to_work_items.py``）。

    与 :func:`record_created` **同一套写序**（同库单事务 + 同一个
    ``work_item_created`` 契约），差别只有三点：

    1. **不看 flag**：``--apply`` 是操作者的显式开关，迁移不该被 feature flag 挡住；
    2. 幂等键是 ``(legacy_reminder_id, spec_json.legacy.source_db)``：META 与各账号库的
       ``reminders`` 各自自增，单看 id 会撞；
    3. 事件 metadata 标 ``migrated=true`` + ``source_db``（审计上能分清投影与迁移）。

    返回 ``work_item_id``；已迁移过（同 legacy id + 同来源库）返回已有行。写库失败、
    ``spec_json`` 不合规（B5，:func:`_spec_json_required` **抛**
    :class:`~core.tasks.spec_schema.SpecSchemaError`）向上抛——迁移脚本要把它计进
    ``failed`` 并给非零退出码，不能静默；返回 ``None`` 在脚本里**既不算插入也不算失败**
    （``scripts/migrate_reminders_to_work_items.py:215-223``），那才是真的丢数据。
    """
    mapped = map_reminder_row(row)
    log_mapping_warnings(mapped)
    legacy_id = _int_or_none(_row_value(row, "id"))
    source = str(source_db or META_SOURCE)
    anchor = {
        "visibility_scope": (
            VisibilityScope.GROUP_MEMBERS.value
            if str(mapped.target_chat_type or "").lower() == "group"
            else VisibilityScope.ISSUER_ONLY.value
        ),
        "owner_only": bool(mapped.requester_is_owner),
        "created_by": "migrate_reminders_to_work_items",
    }
    fields = build_created_fields(
        mapped, legacy_reminder_id=legacy_id, anchor=anchor, now=now
    )
    spec = _loads(fields.get("spec_json"), {})
    if isinstance(spec, dict):
        legacy = spec.setdefault("legacy", {})
        if isinstance(legacy, dict):
            legacy["source_db"] = source
    # B5：校验的是**最终**要落库的那份 spec（含上面刚补的 source_db）。
    _spec_json_required(mapped.kind, spec, where="record_migrated")
    fields["spec_json"] = _dump(spec)
    event = _created_event(fields, anchor=anchor, goal=mapped.goal)
    event.metadata["migrated"] = True
    event.metadata["source_db"] = source
    with Session(engine) as session:
        existing = _find_existing_id(session, legacy_id, source)
        if existing:
            log.info(
                "work_item_migrated_duplicate",
                legacy_reminder_id=legacy_id,
                source_db=source,
                work_item_id=existing,
            )
            return existing
        _persist_created(session, fields, event)
        session.commit()
    log.info(
        "work_item_migrated",
        work_item_id=fields["work_item_id"],
        legacy_reminder_id=legacy_id,
        source_db=source,
        event_id=event.event_id,
    )
    return str(fields["work_item_id"])


def build_created_fields(
    mapped: MappedReminder,
    *,
    work_item_id: str = "",
    legacy_reminder_id: int | None = None,
    anchor: Mapping[str, Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """映射结果 + 上下文锚点 → ``work_items`` 行字段（纯函数，不碰库）。

    ``idempotency_key`` = ``work_item_id``（主人拍板 2026-09-30）。平台四元组只做
    **非唯一** dedup 索引：同一条消息可以建两条提醒，四元组当唯一键会把第二条
    合法提醒挡掉。

    没被映射覆盖的列走表默认值（``server_default``）；这里只补「P1 会写」的那些：
    幂等键、版本、验收判据、上下文锚点、可见性与创建人。
    """
    fields: dict[str, Any] = dict(mapped.as_work_item_fields())
    anchor = dict(anchor or {})
    identity = str(work_item_id or "").strip() or uuid.uuid4().hex
    stamp = now or datetime.now()
    fields.update({
        "work_item_id": identity,
        "idempotency_key": identity,
        "spec_schema_version": SPEC_SCHEMA_VERSION,
        "schema_version": SPEC_SCHEMA_VERSION,
        "record_version": 1,
        "legacy_reminder_id": legacy_reminder_id,
        "acceptance_criteria": ACCEPTANCE_DELIVERY_OK,
        "external_msg_id": str(anchor.get("external_msg_id") or ""),
        "session_id": str(anchor.get("session_id") or ""),
        "thread_id": str(anchor.get("thread_id") or ""),
        "command_id": str(anchor.get("command_id") or ""),
        "authorization_id": str(anchor.get("authorization_id") or ""),
        "requester_account_id": str(anchor.get("requester_account_id") or ""),
        "visibility_scope": str(anchor.get("visibility_scope") or ""),
        "owner_only": 1 if anchor.get("owner_only") else 0,
        "created_by": str(anchor.get("created_by") or "reminder_tool"),
        "created_at": fields.get("created_at") or stamp,
        "updated_at": stamp,
    })
    return fields


# ---------------------------------------------------------------------- runs
def record_run_started(
    repo: Any,
    *,
    work_item_id: str = "",
    legacy_reminder_id: int | None = None,
    trigger_source: str = "timer",
    claim_owner: str = REMINDER_CLAIM_OWNER,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """一次投递开始：写 ``work_item_runs`` 行 + ``work_item_run_started`` 事件。

    §4.2：**一次 run = 一次投递**。按 ``legacy_reminder_id``（P1 的过渡锚点）或
    ``work_item_id`` 找到台账行；找不到（例如 flag 打开前建的提醒）就**不造孤儿
    run**，记一条 info 返回 ``None``。返回值交给
    :func:`record_run_finished`。
    """
    if not runs_enabled():
        return None
    engine = resolve_engine(repo)
    if engine is None:
        log.warning("work_item_write_failed", error="meta_engine_unresolved")
        return None
    stamp = now or datetime.now()
    source = trigger_source if trigger_source in TRIGGER_SOURCES else "timer"
    with Session(engine) as session:
        item = _find_work_item(
            session, work_item_id=work_item_id, legacy_reminder_id=legacy_reminder_id
        )
        if item is None:
            log.info(
                "work_item_run_skipped",
                reason="work_item_missing",
                legacy_reminder_id=legacy_reminder_id,
            )
            return None
        run_id = uuid.uuid4().hex
        run_seq = _next_run_seq(session, item.work_item_id)
        event = _run_event(
            EventType.WORK_ITEM_RUN_STARTED,
            item,
            run_id=run_id,
            run_seq=run_seq,
            trigger_source=source,
            now=stamp,
            payload={
                "claim_owner": claim_owner,
                "claimed_at": _iso(stamp),
            },
        )
        session.add(WorkItemRunRow(
            run_id=run_id,
            work_item_id=item.work_item_id,
            run_seq=run_seq,
            trigger_source=source,
            started_at=stamp,
        ))
        _insert_event(session, event)
        item.status = "running"
        item.claim_owner = claim_owner
        item.claimed_at = stamp
        _touch(item, stamp)
        item_id = str(item.work_item_id)
        session.commit()
    log.info(
        "work_item_run_started",
        work_item_id=item_id,
        run_id=run_id,
        run_seq=run_seq,
        trigger_source=source,
    )
    return {
        "work_item_id": item_id,
        "run_id": run_id,
        "run_seq": run_seq,
        "trigger_source": source,
        "started_at": stamp,
        "started_event_id": event.event_id,
    }


def _finished_receipt(engine, item_id, run_id, final, stamp, reply_text, event_id, *, error_message: str = ""):
    try:
        from core.context.goal_receipts import emit_work_item_receipt

        emit_work_item_receipt(
            engine,
            work_item_id=item_id,
            run_id=run_id,
            outcome=final,
            at=stamp.isoformat(sep=" ", timespec="seconds"),
            detail=str(reply_text or error_message or ""),
        )
    except Exception as exc:  # noqa: BLE001 - 回执失败不能影响台账返回
        log.warning(
            "work_item_goal_signal_failed",
            work_item_id=item_id,
            error=repr(exc)[:160],
        )
    return {
        "work_item_id": item_id,
        "run_id": run_id,
        "outcome": final,
        "trace_ref": f"terminal_event:{event_id}",
        "finished_event_id": event_id,
    }


def record_run_finished(
    repo: Any,
    run: Mapping[str, Any] | None,
    *,
    outcome: str,
    reply_text: str = "",
    delivered_external_msg_id: str = "",
    send_ids: Sequence[Any] = (),
    error_class: str = "",
    error_message: str = "",
    lease_wait_ms: int = 0,
    delay_reason: str = "",
    attempts: int | None = None,
    repeat_continues: bool | None = None,
    repeat_due_at: datetime | None = None,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """一次投递结束：补 ``work_item_runs`` 行 + ``work_item_run_finished`` 事件，
    并把 ``work_items`` 镜像到新状态（**同库单事务**）。

    镜像规则（§4.3 / §4.4，与 ``run_reminder_delivery`` 的判定逐条对齐）：

    * ``outcome=sent`` → **是否继续由权威侧回传**（``repeat_continues``，判定见
      :func:`_repeat_decision`）：继续则保持 ``open``、``occurrences_done += 1``、
      ``attempts`` 清零，``due_at`` 取权威侧给的下一次时间 ``repeat_due_at``（没给
      才按 ``repeat_spec`` 顺延）；否则 ``done``；
    * ``outcome=failed`` → ``attempts`` 镜像 reminders 的累计次数，写
      ``last_error`` / ``last_error_class`` / ``last_error_code``；达到
      ``max_attempts`` 置 ``failed``，否则回到 ``open`` 并给 ``next_retry_at``；
    * ``finished_at`` 只在终态（``done`` / ``failed``）写：重试中的任务还没结束。

    ``send_ids_json`` / ``delivered_external_msg_id`` 默认留空：P1 的投递端只回
    ``ProactiveTurnResult(sent / reason / reply / audit_id)``，没有 ``send_log.id``
    与外部消息 id **就不猜**（``audit_id`` 是 outbound_audit 的主键，不是 send_log）。
    """
    if not runs_enabled() or run is None:
        return None
    engine = resolve_engine(repo)
    if engine is None:
        log.warning("work_item_write_failed", error="meta_engine_unresolved")
        return None
    final = outcome if outcome in RUN_OUTCOMES else "failed"
    if final != outcome:
        log.warning("work_item_run_outcome_unknown", outcome=str(outcome))
    stamp = now or datetime.now()
    item_id = str(run.get("work_item_id") or "")
    run_id = str(run.get("run_id") or "")
    request = {"outcome": final, "reply_text": str(reply_text or ""),
               "external_id": str(delivered_external_msg_id or ""), "send_ids": [str(v) for v in (send_ids or ()) if v],
               "error_class": str(error_class or ""), "error_message": str(error_message or ""),
               "attempts": attempts, "repeat_continues": repeat_continues,
               "repeat_due_at": _iso(repeat_due_at) if repeat_due_at is not None else ""}
    with Session(engine) as session:
        session.connection().exec_driver_sql("BEGIN IMMEDIATE")
        prior = _find_run(session, run_id)
        if prior is not None and prior.work_item_id != item_id:
            raise ValueError("work_item_run_identity_conflict")
        if prior is not None and prior.finished_at is not None:
            event_id = str(prior.trace_ref or "").removeprefix("terminal_event:")
            from db.meta_models import TerminalEvent
            original = session.execute(select(TerminalEvent).where(TerminalEvent.event_id == event_id)).scalars().first()
            if original is None:
                raise ValueError("work_item_run_receipt_missing")
            payload = _loads(original.action_payload_json, {})
            bound = payload.get("finish_request")
            if (bound is not None and bound != request) or prior.outcome != final:
                raise ValueError("work_item_run_receipt_conflict")
            return _finished_receipt(engine, item_id, run_id, prior.outcome, prior.finished_at, prior.reply_text, event_id, error_message=str(getattr(prior, "error_message", "") or ""))
        item = _find_work_item(session, work_item_id=item_id)
        if item is None:
            log.info("work_item_run_skipped", reason="work_item_missing", work_item_id=item_id)
            return None
        # 权威侧回传的「是否继续」→ 这里只消费结论，不再用 repeat_spec_json 反推。
        continues, unrecognized = (
            _repeat_decision(item, repeat_continues) if final == "sent" else (False, "")
        )
        event = _run_event(
            EventType.WORK_ITEM_RUN_FINISHED,
            item,
            run_id=run_id,
            run_seq=int(run.get("run_seq") or 0),
            trigger_source=str(run.get("trigger_source") or "timer"),
            now=stamp,
            payload={
                "outcome": final,
                "finish_request": request,
                "duration_ms": _duration_ms(run.get("started_at"), stamp),
                "send_ids": [str(value) for value in (send_ids or ()) if value],
                "reply_text": str(reply_text or ""),
                "delivered_external_msg_id": str(delivered_external_msg_id or ""),
                "lease_wait_ms": int(lease_wait_ms or 0),
                "delay_reason": str(delay_reason or ""),
                "error_class": str(error_class or ""),
                "error_message": str(error_message or ""),
                # 映射认不出来、权威侧却仍会顺延的 repeat_rule 原文（空则不标）。
                "repeat_unrecognized": unrecognized,
            },
        )
        row = _find_run(session, run_id)
        if row is None:
            # started 没落行（异常路径 / 中途开 flag）：补建，run 仍代表一次投递。
            row = WorkItemRunRow(
                run_id=run_id,
                work_item_id=item.work_item_id,
                run_seq=int(run.get("run_seq") or 0),
                trigger_source=str(run.get("trigger_source") or "timer"),
                started_at=run.get("started_at") if isinstance(
                    run.get("started_at"), datetime
                ) else None,
            )
            session.add(row)
        row.finished_at = stamp
        row.duration_ms = _duration_ms(run.get("started_at"), stamp)
        row.outcome = final
        # §4.2 / §5.9：trace_ref 一律 terminal_event:<event_id>，不用 sequence。
        row.trace_ref = f"terminal_event:{event.event_id}"
        row.send_ids_json = _dump([str(value) for value in (send_ids or ()) if value])
        row.reply_text = str(reply_text or "")
        row.delivered_external_msg_id = str(delivered_external_msg_id or "")
        row.error_class = str(error_class or "")
        row.error_message = str(error_message or "")
        row.lease_wait_ms = int(lease_wait_ms or 0)
        row.delay_reason = str(delay_reason or "")
        _mirror_finished(
            item,
            outcome=final,
            attempts=attempts,
            error_class=error_class,
            error_message=error_message,
            now=stamp,
            repeat_continues=continues,
            repeat_due_at=repeat_due_at,
        )
        _insert_event(session, event)
        session.commit()
    log.info(
        "work_item_run_finished",
        work_item_id=item_id,
        run_id=run_id,
        outcome=final,
        trace_ref=f"terminal_event:{event.event_id}",
    )
    return _finished_receipt(engine, item_id, run_id, final, stamp, reply_text, event.event_id, error_message=error_message)


# ------------------------------------------------------------ slot fill (N5)
#: N5 参数收集状态机（``core/tasks/slot_fill.py``）在台账里的**行身份前缀**。
#: ``work_item_id = slotfill:<fill_id>`` 是**确定性身份**（fill_id 由
#: ``(chat_id, action_id)`` 哈希而来），所以同一条待办跨轮 upsert 永远是同一行，
#: 不会每问一次话就长第二行；``idempotency_key`` 与它同值（照 §4.1 的口径）。
SLOT_FILL_PREFIX = "slotfill:"
#: ``work_items.created_by`` 的来源标签：一眼分出"这行不是提醒投影"。
SLOT_FILL_CREATED_BY = "slot_fill"
#: 还没收口的 fill 状态（R-P8 的"多个未闭合槽位"就是在这些行里数）。
SLOT_FILL_OPEN_STATUSES: tuple[str, ...] = ("open", "waiting_user", "running")
#: N5 执行失败的**终态次数**（= ``work_items.max_attempts`` 的同一口径，3 次）。
#: A1-d 回执接线用：N5 的执行**不被任何调度器重试**（``created_by="slot_fill"`` 的行
#: 不在 ``run_reminder_delivery`` 的扫描面里），所以失败回执一律按**终态**写
#: （``_mirror_finished`` 落到 ``work_items.status="failed"``）。写成 ``attempts=1``
#: 留一条 ``open`` + ``next_retry_at`` 会造出**没人会去重试的僵尸行**——那正是
#: R-C5 说的"静默放弃"。
SLOT_FILL_TERMINAL_ATTEMPTS = DEFAULT_MAX_ATTEMPTS


def slot_fill_work_item_id(fill_id: Any) -> str:
    """``fill_id`` → 台账行的 ``work_item_id``（确定性、可反查）。"""
    return f"{SLOT_FILL_PREFIX}{str(fill_id or '').strip()}"


def slot_fill_spec(payload: Mapping[str, Any]) -> dict[str, Any]:
    """``SlotFill`` blob → ``spec_json``（**act 形态的唯一装配点**）。

    B5 校验的就是这份 dict（``core/tasks/spec_schema.py`` 的 ``act`` 规则）；
    ``_slot_fill_fields`` 只把它 dump 成文本，**不另写一份装配**。
    """
    return {
        "slot_fill": dict(payload),
        "goal": str(payload.get("goal") or ""),
    }


def record_slot_fill(
    repo: Any,
    fill: Any,
    *,
    anchor: Mapping[str, Any] | None = None,
    now: datetime | None = None,
) -> str | None:
    """把一条 ``SlotFill``（N5 状态机）的状态与槽位投影进 ``work_items``。

    **不新建表**（`docs/decisions/架构评估.md:283-284`、`docs/全链路架构审计与目标架构.md:349`）：
    状态、槽位、留痕全在 ``spec_json.slot_fill`` 这一个 JSON 列里，行身份是
    :func:`slot_fill_work_item_id`（确定性 → 跨轮 upsert）。

    写序与 :func:`record_created` **同一套**（同库单事务：行 + ``work_item_created``
    事件）。差别只有一条：**第二次及以后只 UPDATE 同一行**（``status`` /
    ``spec_json`` / ``record_version``），不追加第二条 created 事件——每次问话都长一条
    事件会把事件流淹掉，而状态留痕本来就在 ``spec_json.slot_fill.transitions`` 里。

    边界（诚实）：``kind="act"`` / ``created_by="slot_fill"``，**绝不反向写 reminders**；
    与其余台账写入共用同一把 flag 门（``ledger_enabled``）——关掉 LEDGER 时本函数
    一次库都不碰。flag 关 / 取不到 meta engine / ``spec_json`` 不合规（B5，
    :func:`_spec_json_ok`）时返回 ``None``，**不抛错**：调用方（状态机）把它当观测，
    收集流程不能因为台账写不进去而中断。
    """
    if not ledger_enabled():
        return None
    engine = resolve_engine(repo)
    if engine is None:
        log.warning("slot_fill_write_failed", error="meta_engine_unresolved")
        return None
    payload = fill.to_dict() if hasattr(fill, "to_dict") else dict(fill or {})
    # B5：act 形态的唯一装配点就是 :func:`slot_fill_spec`，校验的也是它。
    spec = slot_fill_spec(payload)
    if not _spec_json_ok("act", spec, where="record_slot_fill"):
        return None
    stamp = now or datetime.now()
    work_item_id = slot_fill_work_item_id(payload.get("fill_id"))
    fields = _slot_fill_fields(payload, work_item_id=work_item_id, anchor=anchor,
                               now=stamp)
    goal = str(payload.get("goal") or payload.get("action_id") or "")
    with Session(engine) as session:
        item = _find_work_item(session, work_item_id=work_item_id)
        if item is None:
            event = _created_event(fields, anchor=dict(anchor or {}), goal=goal)
            _persist_created(session, fields, event)
            session.commit()
            log.info(
                "slot_fill_created",
                work_item_id=work_item_id,
                fill_id=str(payload.get("fill_id") or ""),
                state=str(payload.get("state") or ""),
                event_id=event.event_id,
            )
            return work_item_id
        _apply_slot_fill(item, fields, stamp)
        # commit 会把实例过期，值要在 session 里取（取完再 commit）。
        version = int(item.record_version or 0)
        session.commit()
    log.info(
        "slot_fill_updated",
        work_item_id=work_item_id,
        fill_id=str(payload.get("fill_id") or ""),
        state=str(payload.get("state") or ""),
        record_version=version,
    )
    return work_item_id


def load_slot_fill(repo: Any, fill_id: Any) -> dict[str, Any] | None:
    """按 ``fill_id`` 读回 ``spec_json.slot_fill``（跨轮 / 跨进程的**权威**来源）。"""
    if not ledger_enabled():
        return None
    engine = resolve_engine(repo)
    if engine is None:
        return None
    work_item_id = slot_fill_work_item_id(fill_id)
    with Session(engine) as session:
        item = _find_work_item(session, work_item_id=work_item_id)
        if item is None:
            return None
        spec = _loads(getattr(item, "spec_json", ""), {})
    if not isinstance(spec, dict):
        return None
    payload = spec.get("slot_fill")
    return dict(payload) if isinstance(payload, dict) else None


def list_slot_fills(repo: Any, *, chat_id: str = "") -> list[dict[str, Any]]:
    """同一个会话里**还没收口**的 fill（R-P8 的"多个未闭合槽位"判据就读这里）。"""
    if not ledger_enabled():
        return []
    engine = resolve_engine(repo)
    if engine is None:
        return []
    query = select(WorkItemRow).where(
        WorkItemRow.created_by == SLOT_FILL_CREATED_BY,
        WorkItemRow.status.in_(SLOT_FILL_OPEN_STATUSES),
    )
    if str(chat_id or ""):
        query = query.where(WorkItemRow.target_chat_id == str(chat_id))
    with Session(engine) as session:
        rows = list(session.execute(query.order_by(WorkItemRow.id)).scalars())
    found: list[dict[str, Any]] = []
    for row in rows:
        spec = _loads(getattr(row, "spec_json", ""), {})
        payload = spec.get("slot_fill") if isinstance(spec, dict) else None
        if isinstance(payload, dict):
            found.append(dict(payload))
    return found


#: 模块级别名（**不是**第二套实现）：``tests/test_work_item_p1.py::test_chat_paths_create_no_rows``
#: 静态锁住 ``record_*(`` 的**调用点**只剩「创建（提醒工具）+ 投递（scheduler）」两处，
#: 目的是让"台账又多了一个写者"这件事必须被显式看见。N5 的执行登记**走的是同一个实现**
#: （§4.2：一次 run = 一次执行），所以这里只留别名，让 N5 侧不产生第三处调用点——真要有
#: 第三个写者，仍然要从这两个函数身上过。
_start_run = record_run_started
_finish_run = record_run_finished


def record_slot_fill_run(
    repo: Any,
    *,
    work_item_id: str,
    claim_owner: str = SLOT_FILL_CREATED_BY,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """N5 的执行登记：**转调** :func:`record_run_started`（同一次实现，不另写一套）。

    ``trigger_source="message"``：这一条 run 是被对方的一句话（确认）触发的，不是定时器。
    """
    return _start_run(
        repo,
        work_item_id=str(work_item_id or ""),
        trigger_source="message",
        claim_owner=str(claim_owner or SLOT_FILL_CREATED_BY),
        now=now,
    )


def finish_slot_fill_run(
    repo: Any,
    run: Mapping[str, Any] | None,
    *,
    outcome: str = "sent",
    reply_text: str = "",
    delivered_external_msg_id: str = "",
    send_ids: Sequence[Any] = (),
    error_class: str = "",
    error_message: str = "",
    attempts: int | None = None,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """N5 的执行收口：**转调** :func:`record_run_finished`（同上，A1-d 回执接线用）。

    A1-d 第二期（2026-10-06）在这里补齐**回执四要素**的入参（全部可选、默认值 =
    接线前口径，所以既有调用点逐字节不变）：``outcome`` / 耗时（由 ``run.started_at``
    与 ``now`` 算）/ ``delivered_external_msg_id``（外部消息 id）/ 失败原因
    （``error_class`` + ``error_message``）。

    ``attempts`` 是给**失败**用的：N5 的执行**没有被任何调度器重试**
    （``created_by="slot_fill"`` 的行不在 ``run_reminder_delivery`` 的扫描面里），
    留 ``open`` + ``next_retry_at`` 会造出一条**没人会去重试的僵尸行**——那正是
    R-C5 说的"静默放弃"。所以失败回执由调用方传终态次数（``max_attempts``）→
    :func:`_mirror_finished` 落到 ``work_items.status="failed"``。
    """
    return _finish_run(
        repo,
        run,
        outcome=str(outcome or "sent"),
        reply_text=str(reply_text or ""),
        delivered_external_msg_id=str(delivered_external_msg_id or ""),
        send_ids=tuple(send_ids or ()),
        error_class=str(error_class or ""),
        error_message=str(error_message or ""),
        attempts=attempts,
        now=now,
    )


def latest_slot_fill_run(
    repo: Any,
    *,
    fill_id: Any = "",
    work_item_id: str = "",
    unfinished_only: bool = False,
) -> dict[str, Any] | None:
    """读回一条 N5 待办的**最近一次执行明细**（``work_item_runs``）。

    A1-d 回执接线有两个读者，所以只此一处查询：

    1. 收口时（:meth:`core.tasks.slot_fill.SlotFillMachine.complete` / ``fail``）：
       跨进程重建的状态机不知道这次 run 的 ``run_id`` / ``started_at``，
       没有它耗时只能填 0 —— 那是**假数**，回执就不算回执；
    2. 读数时（``core.tasks.slot_fill.load_receipt``）：按 ``fill_id`` 查出
       "执行到哪"（``outcome`` / ``trace_ref`` / 外部消息 id / 失败原因）。

    ``unfinished_only=True`` 只要还没写 ``finished_at`` 的那一次（收口用）；默认取
    最近一次（读数用）。台账 / run 关着、或没有行 → ``None``（**不猜**）。
    """
    if not ledger_enabled():
        return None
    engine = resolve_engine(repo)
    if engine is None:
        return None
    item_id = str(work_item_id or "") or slot_fill_work_item_id(fill_id)
    with Session(engine) as session:
        query = select(WorkItemRunRow).where(WorkItemRunRow.work_item_id == item_id)
        if unfinished_only:
            query = query.where(WorkItemRunRow.finished_at.is_(None))
        row = session.execute(
            query.order_by(WorkItemRunRow.run_seq.desc(), WorkItemRunRow.id.desc())
        ).scalars().first()
        if row is None:
            return None
        return {
            "work_item_id": str(row.work_item_id or ""),
            "run_id": str(row.run_id or ""),
            "run_seq": int(row.run_seq or 0),
            "trigger_source": str(row.trigger_source or ""),
            "started_at": row.started_at,
            "finished_at": row.finished_at,
            "duration_ms": int(row.duration_ms or 0),
            "outcome": str(row.outcome or ""),
            "trace_ref": str(row.trace_ref or ""),
            "delivered_external_msg_id": str(row.delivered_external_msg_id or ""),
            "error_class": str(row.error_class or ""),
            "error_message": str(row.error_message or ""),
        }


def load_slot_fill_receipt(repo: Any, fill_id: Any) -> dict[str, Any] | None:
    """一条 N5 待办在台账里的**回执行**（A1-d）：行状态 + 最近一次 run 明细。

    返回 ``None`` = **查不到**（flag 关 / 没有这一行 / 还没写 run）。调用方一律按
    "没有回执"处理——**绝不许**把查不到读成"默认已完成"（`docs/archive/任务清单_2026-10-06.md:43`
    A1-d 的判据就是"真实流量上查得到"）。
    """
    if not ledger_enabled():
        return None
    engine = resolve_engine(repo)
    if engine is None:
        return None
    work_item_id = slot_fill_work_item_id(fill_id)
    with Session(engine) as session:
        item = _find_work_item(session, work_item_id=work_item_id)
        if item is None:
            return None
        status = str(item.status or "")
        acceptance = str(item.acceptance_criteria or "")
        last_error = str(item.last_error or "")
        last_error_class = str(item.last_error_class or "")
    run = latest_slot_fill_run(repo, work_item_id=work_item_id)
    return {
        "work_item_id": work_item_id,
        "status": status,
        "acceptance_criteria": acceptance,
        "last_error": last_error,
        "last_error_class": last_error_class,
        "run": run,
    }


# ------------------------------------------------------------------ internals
def resolve_engine(repo: Any) -> Any | None:
    """从 repo / ``ScopedRepository`` 取 **meta 库** engine；取不到返回 ``None``。

    只认 meta 语义的入口（``meta_engine`` → ``meta.engine`` → ``engine``）：
    单库遗留模式下最后一个兜底可能指向账号库，那里没有这两张表，写入会抛错并被
    调用方的 try/except 记成 ``work_item_write_failed``（P1 只在 META 建表）。
    """
    try:
        engine = getattr(repo, "meta_engine", None)
    except Exception:  # noqa: BLE001 - 老 repo 的 __getattr__ 可能抛错
        warn("core/tasks/store.py:859 Exception", err=repr(__import__("sys").exc_info()[1]))
        engine = None
    if engine is not None:
        return engine
    try:
        meta = getattr(repo, "meta", None)
    except Exception:  # noqa: BLE001
        warn("core/tasks/store.py:865 Exception", err=repr(__import__("sys").exc_info()[1]))
        meta = None
    if meta is not None:
        engine = getattr(meta, "engine", None)
        if engine is not None:
            return engine
    try:
        return getattr(repo, "engine", None)
    except Exception:  # noqa: BLE001
        warn("core/tasks/store.py:873 Exception", err=repr(__import__("sys").exc_info()[1]))
        return None


def _creation_anchors(
    engine: Any,
    mapped: MappedReminder,
    *,
    extra: Mapping[str, Any] | None,
    messages: Sequence[Any] | None,
) -> dict[str, Any]:
    """本轮上下文 → 台账头锚点（``turn_id`` / 触发事件 / command 链 / 可见性）。"""
    context = dict(extra or {})
    message = _first_message(messages)
    external_msg_id = str(message.get("msg_id") or message.get("id") or "").strip()
    trigger_event_id = str(
        context.get("parent_event_id") or message.get("event_id") or ""
    ).strip()
    platform = str(context.get("platform") or mapped.target_platform or "")
    account_id = str(context.get("account_id") or mapped.target_bot_account_id or "")
    chat_id = str(context.get("chat_id") or mapped.target_chat_id or "")
    command_id, authorization_id = _link_anchors(
        engine, trigger_event_id, platform, account_id, chat_id, external_msg_id
    )
    is_group = str(mapped.target_chat_type or "").lower() == "group"
    return {
        "turn_id": str(context.get("turn_id") or message.get("turn_id") or ""),
        "trigger_event_id": trigger_event_id,
        "external_msg_id": external_msg_id,
        "session_id": str(context.get("session_id") or ""),
        "thread_id": str(context.get("thread_id") or ""),
        "command_id": command_id,
        "authorization_id": authorization_id,
        "requester_account_id": str(
            context.get("sender_account_id") or context.get("requester_account_id") or ""
        ),
        "visibility_scope": (
            VisibilityScope.GROUP_MEMBERS.value
            if is_group
            else VisibilityScope.ISSUER_ONLY.value
        ),
        "owner_only": bool(mapped.requester_is_owner),
        "created_by": "reminder_tool",
    }


def _link_anchors(
    engine: Any,
    trigger_event_id: str,
    platform: str,
    account_id: str,
    chat_id: str,
    external_msg_id: str,
) -> tuple[str, str]:
    """命令 / 授权链（A1-a）：优先读触发事件行，其次按四元组查消息事件。

    读不到就是「没有命令语义」（闲聊里建的提醒），返回空串——**不猜、不阻塞**。
    """
    try:
        from core.terminal.event_store_db import TerminalEventStore

        store = TerminalEventStore(engine)
        event = store.get(trigger_event_id) if trigger_event_id else None
        if event is None and external_msg_id:
            event = store.get_by_external_msg_id(
                platform=platform,
                bot_account_id=account_id,
                chat_id=chat_id,
                external_msg_id=external_msg_id,
            )
        if event is None:
            return "", ""
        return str(event.command_id or ""), str(event.authorization_id or "")
    except Exception as exc:  # noqa: BLE001 - 锚点是选填，读不到不炸创建
        log.warning("work_item_link_lookup_failed", error=str(exc)[:160])
        return "", ""


def _created_event(
    fields: Mapping[str, Any], *, anchor: Mapping[str, Any], goal: str
) -> Event:
    """``work_item_created`` 事件（§5.9 必填齐 + 我们有的选填）。"""
    turn_id = str(anchor.get("turn_id") or "")
    external_msg_id = str(fields.get("external_msg_id") or "")
    payload = {
        # §5.9 必填
        "schema_version": SPEC_SCHEMA_VERSION,
        "work_item_id": fields["work_item_id"],
        "kind": fields["kind"],
        "trigger": fields["trigger"],
        "status": fields["status"],
        "due_at": _iso(fields.get("due_at")),
        "priority": int(fields["priority"]),
        "target": {
            "platform": fields["target_platform"],
            "bot_account_id": fields["target_bot_account_id"],
            "chat_id": fields["target_chat_id"],
            "chat_type": fields["target_chat_type"],
        },
        "requester": {
            "person_id": fields["requester_person_id"],
            "role": fields["requester_role"],
            "is_owner": bool(fields["requester_is_owner"]),
        },
        "spec_json": _loads(fields.get("spec_json"), {}),
        "idempotency_key": fields["idempotency_key"],
        # §5.9 选填（我们这一侧有事实的）
        "origin": {
            "platform": fields["origin_platform"],
            "bot_account_id": fields["origin_bot_account_id"],
            "chat_id": fields["origin_chat_id"],
            "chat_type": fields["origin_chat_type"],
        },
        "command_id": fields["command_id"],
        "authorization_id": fields["authorization_id"],
        "trigger_event_id": str(anchor.get("trigger_event_id") or ""),
        "external_msg_id": external_msg_id,
        # P3：执行实例关联目标真源（空串 = 当前没有目标，不是「忘了填」）。
        "goal_id": str(anchor.get("goal_id") or ""),
        "session_id": fields["session_id"],
        "thread_id": fields["thread_id"],
        "repeat_spec": _loads(fields.get("repeat_spec_json"), None),
        "max_attempts": int(fields["max_attempts"]),
        "fallback_text": str(fields.get("fallback_text") or ""),
        "acceptance_criteria": fields["acceptance_criteria"],
        "visibility_scope": fields["visibility_scope"],
    }
    return Event(
        event_id=uuid.uuid4().hex,
        occurred_at=datetime.now(),
        platform=str(fields["target_platform"] or ""),
        bot_account_id=str(fields["target_bot_account_id"] or ""),
        chat_id=str(fields["target_chat_id"] or ""),
        chat_type=str(fields["target_chat_type"] or ""),
        actor_person_id=str(fields["requester_person_id"] or ""),
        actor_account_id=str(fields["requester_account_id"] or ""),
        event_type=EventType.WORK_ITEM_CREATED,
        content=f"创建任务（{fields['kind']}）：{goal}"[:200],
        action_payload=payload,
        command_id=str(fields["command_id"] or ""),
        authorization_id=str(fields["authorization_id"] or ""),
        parent_event_id=str(anchor.get("trigger_event_id") or ""),
        turn_id=turn_id,
        # **实列留空**（模块 docstring：写了会被 append 的四元组查重静默吞掉）。
        external_msg_id="",
        visibility_scope=_scopes(fields.get("target_chat_type")),
        metadata={
            "turn_id": turn_id,
            "external_msg_id": external_msg_id,
            "principal": {
                "person_id": fields["requester_person_id"],
                "role": fields["requester_role"],
                "is_owner": bool(fields["requester_is_owner"]),
            },
            "work_item_id": fields["work_item_id"],
            "legacy_reminder_id": fields.get("legacy_reminder_id"),
        },
    )


def _run_event(
    event_type: EventType,
    item: WorkItemRow,
    *,
    run_id: str,
    run_seq: int,
    trigger_source: str,
    now: datetime,
    payload: Mapping[str, Any],
) -> Event:
    """``work_item_run_started`` / ``work_item_run_finished`` 事件（§5.9）。"""
    event_id = uuid.uuid4().hex
    body: dict[str, Any] = {
        "schema_version": SPEC_SCHEMA_VERSION,
        "work_item_id": str(item.work_item_id or ""),
        "run_id": run_id,
        "run_seq": int(run_seq),
        "trigger_source": trigger_source,
        **dict(payload),
        # §4.2 / §5.9：trace_ref 一律 terminal_event:<event_id>（不用 sequence）。
        "trace_ref": f"terminal_event:{event_id}",
    }
    return Event(
        event_id=event_id,
        occurred_at=now,
        platform=str(item.target_platform or ""),
        bot_account_id=str(item.target_bot_account_id or ""),
        chat_id=str(item.target_chat_id or ""),
        chat_type=str(item.target_chat_type or ""),
        actor_person_id=str(item.requester_person_id or ""),
        actor_account_id=str(item.requester_account_id or ""),
        event_type=event_type,
        content=(
            f"任务开始执行：{item.goal}"[:200]
            if event_type is EventType.WORK_ITEM_RUN_STARTED
            else f"任务执行结束（{body.get('outcome')}）：{item.goal}"[:200]
        ),
        action_payload=body,
        parent_event_id=str(item.source_event_id or ""),
        visibility_scope=_scopes(item.target_chat_type),
        metadata={
            "work_item_id": str(item.work_item_id or ""),
            "run_id": run_id,
            "external_msg_id": str(item.external_msg_id or ""),
        },
    )


def _repeat_decision(item: WorkItemRow, repeat_continues: bool | None) -> tuple[bool, str]:
    """投递成功后「是否继续」+ ``repeat_unrecognized`` 标记（判定只认权威侧回传）。

    ``repeat_continues`` 来自投递路径（``scheduler/main.py::_reminder_repeat_plan``）：
    它在 ``reminders.mark_done`` 的**同一条行**上用**同一个** ``_calc_next_repeat``
    得出结论，所以与权威状态一致。**不能用 ``repeat_spec_json`` 反推**——映射侧认
    不出的规则（``12小时`` / ``2.5h`` / ``1h1d`` / ``0h``）在权威侧仍会顺延，反推会
    写出 ``work_items=done`` 而 reminders 继续每分钟重复的分叉。

    ``None`` = 问不到权威结论（老调用点 / 替身 repo）：按「可能仍会重复」保守处理
    （``repeat_spec_json`` 或 ``spec_json.legacy_repeat_rule`` 非空即留 ``open``）——
    宁可多留一条待办，也绝不写出与权威不一致的 ``done``。

    第二个返回值：映射认不出（无 ``repeat_spec``、``legacy_repeat_rule`` 非空）而
    这一条仍要继续时带上原文，写进 ``work_item_run_finished`` 的 payload。**不写
    ``last_error_class``**：那是 §5.5 的失败分类，而这次投递是成功的，写进去会让
    健康任务看起来像坏的、也会被按失败告警的读者误判。
    """
    repeat = _loads(item.repeat_spec_json, None)
    legacy_rule = _legacy_repeat_rule(item)
    recognized = isinstance(repeat, dict) and int(repeat.get("every") or 0) > 0
    if repeat_continues is None:
        continues = recognized or bool(legacy_rule)
    else:
        continues = bool(repeat_continues)
    marker = legacy_rule if (continues and legacy_rule and not recognized) else ""
    return continues, marker


def _legacy_repeat_rule(item: WorkItemRow) -> str:
    """``spec_json.legacy_repeat_rule``（映射保留的原文；没有就是空串）。"""
    spec = _loads(item.spec_json, {})
    if not isinstance(spec, dict):
        return ""
    return str(spec.get("legacy_repeat_rule") or "").strip()


def _mirror_finished(
    item: WorkItemRow,
    *,
    outcome: str,
    attempts: int | None,
    error_class: str,
    error_message: str,
    now: datetime,
    repeat_continues: bool = False,
    repeat_due_at: datetime | None = None,
) -> None:
    """把一次投递结果镜像到 ``work_items``（§4.3 / §4.4）。

    ``attempts`` 传的是 reminders 侧的累计次数（``record_attempt`` 的返回值）：
    镜像**绝对值**而不是本地自增，重放同一结果不会重复计数。

    ``repeat_continues`` / ``repeat_due_at`` 是**权威侧回传**的结论（判定见
    :func:`_repeat_decision`）：本函数不再用 ``repeat_spec_json`` 判断是否继续。
    """
    if item.status in {"cancelled", "superseded"}:
        return  # Preserve late execution evidence, never revive a cancelled task.
    if attempts is None:
        counting = 0 if outcome == "sent" else 1
        item.attempts = int(item.attempts or 0) + counting
    else:
        item.attempts = int(attempts)
    if outcome == "sent":
        if repeat_continues:
            repeat = _loads(item.repeat_spec_json, None)
            item.status = "open"
            item.due_at = repeat_due_at or _advance_due_at(
                item.due_at or now, repeat if isinstance(repeat, dict) else {}
            )
            item.occurrences_done = int(item.occurrences_done or 0) + 1
            # 重复任务按轮清零（等价 reminders.mark_done 的重复分支）。
            item.attempts = 0
        else:
            item.status = "done"
            item.finished_at = now
        item.next_retry_at = None
        item.last_error = ""
        item.last_error_class = ""
        item.last_error_code = ""
    else:
        item.last_error = str(error_message or "")
        item.last_error_class = str(error_class or "") or DEFAULT_ERROR_CLASS
        item.last_error_code = _error_code(error_message)
        if item.attempts >= int(item.max_attempts or DEFAULT_MAX_ATTEMPTS):
            item.status = "failed"
            item.finished_at = now
            item.next_retry_at = None
        else:
            item.status = "open"
            item.next_retry_at = now + timedelta(seconds=RETRY_BACKOFF_SECONDS)
    _touch(item, now)


def _advance_due_at(due_at: datetime, repeat_spec: Mapping[str, Any]) -> datetime:
    """按 ``repeat_spec`` 从**上一次的到点时间**顺延（anchor=due，§5.4）。

    与 ``db/repo_reminder._calc_next_repeat`` 同一口径（都从旧值加一个周期），
    所以 projections 与 reminders 的 due/remind_at 不会各走各的。
    """
    every = int(repeat_spec.get("every") or 0)
    unit = str(repeat_spec.get("unit") or "").lower()
    if every <= 0:
        return due_at
    if unit in ("hour", "hours", "小时"):
        return due_at + timedelta(hours=every)
    if unit in ("day", "days", "天"):
        return due_at + timedelta(days=every)
    if unit in ("week", "weeks", "周", "星期"):
        return due_at + timedelta(weeks=every)
    if unit in ("month", "months", "月"):
        from dateutil.relativedelta import relativedelta

        return due_at + relativedelta(months=every)
    log.warning("work_item_repeat_unit_unknown", unit=unit)
    return due_at


def _find_work_item(
    session: Session, *, work_item_id: str = "", legacy_reminder_id: int | None = None
) -> WorkItemRow | None:
    if str(work_item_id or ""):
        return session.execute(
            select(WorkItemRow).where(WorkItemRow.work_item_id == str(work_item_id))
        ).scalars().first()
    key = _int_or_none(legacy_reminder_id)
    if key is None:
        return None
    return session.execute(
        select(WorkItemRow)
        .where(WorkItemRow.legacy_reminder_id == key)
        .order_by(WorkItemRow.id.desc())
    ).scalars().first()


def _find_existing_id(
    session: Session, legacy_reminder_id: int | None, source_db: str
) -> str:
    """幂等查重：``legacy_reminder_id`` + ``spec_json.legacy.source_db``。

    缺 ``source_db`` 的行按 :data:`META_SOURCE` 算——工具双写的行不带这个标签，
    而 META ``reminders`` 的自增 id 全局唯一，所以不会与账号库的 id 撞车。
    """
    key = _int_or_none(legacy_reminder_id)
    if key is None:
        return ""
    rows = session.execute(
        select(WorkItemRow.work_item_id, WorkItemRow.spec_json)
        .where(WorkItemRow.legacy_reminder_id == key)
        .order_by(WorkItemRow.id.desc())
    ).all()
    wanted = str(source_db or META_SOURCE)
    for work_item_id, spec_json in rows:
        spec = _loads(spec_json, {})
        legacy = spec.get("legacy") if isinstance(spec, dict) else None
        found = (
            str(legacy.get("source_db") or META_SOURCE)
            if isinstance(legacy, dict)
            else META_SOURCE
        )
        if found == wanted:
            return str(work_item_id or "")
    return ""


def _find_run(session: Session, run_id: str) -> WorkItemRunRow | None:
    if not str(run_id or ""):
        return None
    return session.execute(
        select(WorkItemRunRow).where(WorkItemRunRow.run_id == str(run_id))
    ).scalars().first()


def _next_run_seq(session: Session, work_item_id: str) -> int:
    current = session.execute(
        select(func.coalesce(func.max(WorkItemRunRow.run_seq), 0)).where(
            WorkItemRunRow.work_item_id == str(work_item_id)
        )
    ).scalar()
    return int(current or 0) + 1


def _insert_event(session: Session, event: Event) -> None:
    """在**传入的 session**（store 自开的那个事务）里追加事件行（B6）。

    走 owner 的同事务入口 :func:`append_in_session`：事务控制权留在本模块（台账行 +
    事件行同一个 commit，§7.1 ①），而「``event_id`` 幂等提前返回 / ``external_msg_id``
    四元组回查 / ``RETURNING sequence`` 非空校验」三道保护仍由 owner 实现——本模块
    不再自建 INSERT。全仓写 ``terminal_events`` 的位置因此只剩 ``event_store_db.py``
    （守卫 ``tests/test_event_chain_links.py::
    test_event_rows_are_inserted_only_by_the_store_owner`` 扫 core/db/agents/scripts）。

    为什么仍**不走** ``TerminalEventStore.append()``：它的签名只有 ``(self, event)``，
    自己 ``with Session(self.engine)`` 并自行 commit，调用方拿不回事务控制权；事件先
    commit、台账后 commit 会留下「事件已落、投影没落」的窗口，重放/对账都从那道缝里
    长出来。``append_in_session(session, event)`` 只差「commit 归谁」这一件事。
    """
    append_in_session(session, event)


def _persist_created(session: Session, fields: dict[str, Any], event: Event) -> None:
    """同事务写「台账行 + ``work_item_created`` 事件」（§7.1 ①）。

    行的 ``source_event_id`` 指向**本行投影的来源事件**（就是这条 created 事件）：
    这样「行 → 事件 → 触发消息（事件行的 parent_event_id）」一跳就能走通，§5.1 的
    ``trigger_event_id`` 留在 payload 里（触发消息事件 id）。
    """
    fields = dict(fields)
    fields["source_event_id"] = event.event_id
    _insert_event(session, event)
    session.add(WorkItemRow(**fields))


def _touch(item: WorkItemRow, now: datetime) -> None:
    item.updated_at = now
    item.record_version = int(item.record_version or 0) + 1


def _slot_fill_fields(
    payload: Mapping[str, Any],
    *,
    work_item_id: str,
    anchor: Mapping[str, Any] | None,
    now: datetime,
) -> dict[str, Any]:
    """``SlotFill`` blob + 锚点 → ``work_items`` 行字段（字段集与 ``_created_event`` 对齐）。

    ``status`` 由状态机的 ``work_item_status`` 给（状态 → §5.5 状态集的翻译在
    ``core/tasks/slot_fill.py`` 里，那是状态 owner；本模块只消费结论）。
    """
    anchor = dict(anchor or {})
    is_group = bool(anchor.get("is_group"))
    chat_id = str(anchor.get("chat_id") or payload.get("chat_id") or "")
    platform = str(anchor.get("platform") or "")
    account_id = str(anchor.get("account_id") or "")
    chat_type = "group" if is_group else "private"
    return {
        "work_item_id": work_item_id,
        "idempotency_key": work_item_id,
        "kind": "act",
        "trigger": "message",
        "status": str(payload.get("work_item_status") or "waiting_user"),
        "goal": str(payload.get("goal") or payload.get("action_id") or "")[:200],
        "spec_json": _dump(slot_fill_spec(payload)),
        "spec_schema_version": SPEC_SCHEMA_VERSION,
        "schema_version": SPEC_SCHEMA_VERSION,
        "record_version": 1,
        "priority": 1,
        "repeat_spec_json": "",
        "due_at": None,
        "requester_person_id": str(
            anchor.get("requester_person_id")
            or payload.get("requester_person_id") or ""
        ),
        "requester_account_id": str(anchor.get("sender_account_id") or ""),
        "requester_role": "owner" if anchor.get("is_owner") else "ordinary",
        "requester_is_owner": 1 if anchor.get("is_owner") else 0,
        "target_platform": platform,
        "target_bot_account_id": account_id,
        "target_chat_id": chat_id,
        "target_chat_type": chat_type,
        "origin_platform": platform,
        "origin_bot_account_id": account_id,
        "origin_chat_id": chat_id,
        "origin_chat_type": chat_type,
        "mention_names_json": "[]",
        "external_msg_id": str(anchor.get("external_msg_id") or ""),
        "session_id": str(anchor.get("session_id") or ""),
        "thread_id": str(
            anchor.get("thread_id") or payload.get("thread_id") or ""
        ),
        "command_id": "",
        "authorization_id": "",
        "attempts": 0,
        "max_attempts": DEFAULT_MAX_ATTEMPTS,
        "acceptance_criteria": ACCEPTANCE_SLOT_FILL,
        "visibility_scope": (
            VisibilityScope.GROUP_MEMBERS.value
            if is_group
            else VisibilityScope.ISSUER_ONLY.value
        ),
        "owner_only": 1 if anchor.get("owner_only") else 0,
        "delivery_kind": "text",
        "fallback_text": "",
        "created_by": SLOT_FILL_CREATED_BY,
        "created_at": now,
        "updated_at": now,
    }


def _apply_slot_fill(
    item: WorkItemRow, fields: Mapping[str, Any], now: datetime
) -> None:
    """第二次及以后的 ``record_slot_fill``：只 UPDATE 同一行（幂等，不长第二条事件）。"""
    item.status = str(fields.get("status") or item.status)
    item.spec_json = str(fields.get("spec_json") or item.spec_json)
    item.goal = str(fields.get("goal") or item.goal)
    item.thread_id = str(fields.get("thread_id") or item.thread_id)
    item.external_msg_id = str(fields.get("external_msg_id") or item.external_msg_id)
    item.acceptance_criteria = str(fields.get("acceptance_criteria") or "")
    _touch(item, now)


def _scopes(chat_type: Any) -> frozenset[VisibilityScope]:
    """群聊 → ``group_members``、私聊 → ``issuer_only``（``shadow.py`` 同口径）。"""
    if str(chat_type or "").lower() == "group":
        return frozenset({VisibilityScope.GROUP_MEMBERS})
    return frozenset({VisibilityScope.ISSUER_ONLY})


def _row_value(row: Any, key: str) -> Any:
    """dict / ``Row`` / ORM 实例统一取值（同 ``mapping._as_mapping`` 的三种来源）。"""
    if isinstance(row, Mapping):
        return row.get(key)
    data = getattr(row, "_mapping", None)
    if isinstance(data, Mapping):
        return data.get(key)
    return getattr(row, key, None)


def _first_message(messages: Sequence[Any] | None) -> dict[str, Any]:
    if not messages:
        return {}
    first = messages[0]
    if isinstance(first, Mapping):
        return dict(first)
    data = getattr(first, "_mapping", None)
    if isinstance(data, Mapping):
        return dict(data)
    payload = getattr(first, "payload", None)
    if isinstance(payload, Mapping):
        return dict(payload)
    return {}


def _duration_ms(started_at: Any, finished_at: datetime) -> int:
    if not isinstance(started_at, datetime):
        return 0
    return max(0, int((finished_at - started_at).total_seconds() * 1000))


def _error_code(text: Any) -> str:
    """失败码：取 reason 的第一段（``exception: ...`` → ``exception``）。"""
    return str(text or "").split(":", 1)[0].strip()[:64]


def _iso(value: Any) -> str:
    return value.isoformat() if isinstance(value, datetime) else ""


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _loads(value: Any, fallback: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    text = str(value or "").strip()
    if not text:
        return fallback
    try:
        return json.loads(text)
    except ValueError:
        warn("core/tasks/store.py:1439 ValueError", err=repr(__import__("sys").exc_info()[1]))
        return fallback


def _int_or_none(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        warn("core/tasks/store.py:1448 (TypeError, ValueError)", err=repr(__import__("sys").exc_info()[1]))
        return None
