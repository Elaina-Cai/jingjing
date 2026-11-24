# -*- coding: utf-8 -*-
"""目标关闭的唯一提交口（对话侧与执行侧共用）。

为什么要单独一个模块：关闭有两条来源——「对方说办完了」和「到点执行完了」。
两条都写 ``commit_updates``，但证据不同（消息事件 id / 执行记录 id）。把提交、
幂等、去重、派生标记收在这里，调用方只负责给出「哪条证据」。

三条纪律：
1. 没有证据就不关闭（``close`` 在提交契约里是必须带 evidence 的操作）；
2. 没有会话坐标就不关闭（不知道属于哪个会话的完成事实不写）；
3. 认知库写失败返回 False，由调用方决定是否本地兜底——**绝不能因为写失败就重放
   已经执行过的动作**。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping

from core.infra import skips
from safelog import warn

__all__ = ["close_local", "commit_close"]


def _as_scope(value: Any) -> Any:
    """``Scope`` / dict / 模型上的 scope 字段 → ``Scope``；不完整返回 None。"""
    from core.context.cognitive_commit import Scope

    if isinstance(value, Scope):
        return value if value.complete else None
    if not value:
        return None
    if isinstance(value, dict):
        data = value
    else:
        data = {
            "platform": getattr(value, "platform", ""),
            "account_id": getattr(value, "account_id", ""),
            "chat_id": getattr(value, "chat_id", ""),
            "chat_type": getattr(value, "chat_type", ""),
            "group_id": getattr(value, "group_id", ""),
        }
    platform = str(data.get("platform") or "")
    account = str(data.get("account_id") or "")
    chat = str(data.get("chat_id") or "")
    if not (platform and account and chat):
        return None
    return Scope(
        platform=platform,
        account_id=account,
        chat_id=chat,
        chat_type=str(data.get("chat_type") or "private"),
        group_id=str(data.get("group_id") or ""),
    )


def _mark_closed(model: Any, key: str, at: str, result: str = "", state: str = "") -> None:
    if model is None:
        return
    payload = {
        "key": str(key),
        "result": str(result or "cognitive:close"),
        "_derived": True,
    }
    if str(state or ""):
        payload["state"] = str(state)
    try:
        model.add("intention_closed", payload, at=str(at or ""))
    except Exception as exc:  # noqa: BLE001 - 标记失败不影响已提交的事实
        warn("goal_close_mark_failed", err=repr(exc)[:120])


def _verified_ref(reference: str, scope: Any, state: str) -> bool:
    """来源记录必须存在、同会话，执行完成还须有终态事实。

    ``reminder:<id>@<due>`` 里的 due 认的是「这一次到点」：重复到点投递成功后
    ``mark_done`` 会把状态改回 pending、把 ``remind_at`` 推进到下一次，此时
    只能靠「行里的时刻已经不是 due」证明这一跳真的投出去了。
    不带 ``@`` 的旧引用保持原口径（只认 done / failed）。
    """
    if scope is None or not reference:
        return False
    try:
        if reference.startswith("reminder:"):
            from core.terminal.store_port import get_repo
            raw_id, _, due = reference.split(":", 1)[1].partition("@")
            row = get_repo().reminders.get(int(raw_id))
            if row is None:
                return False
            if (row.source_platform, row.source_account_id, row.source_chat_id) != scope.key:
                return False
            if state == "abandoned":
                return row.status == "failed"
            if row.status == "done":
                return True
            if not due:
                return False
            remind_at = getattr(row, "remind_at", None)
            current = (remind_at.strftime("%Y-%m-%d %H:%M")
                       if isinstance(remind_at, datetime)
                       else " ".join(str(remind_at or "").split()))
            return bool(current) and current != due
        from core.terminal.shadow import get_default_store
        row = get_default_store().get(reference)
        return row is not None and (row.platform,row.bot_account_id,row.chat_id) == scope.key
    except Exception as exc:
        warn("goal_evidence_lookup_failed", ref=reference, err=repr(exc)[:120])
        return False


def close_local(
    model: Any,
    key: str,
    *,
    at: str = "",
    result: str = "",
    evidence_ref: str = "",
    state: str = "",
) -> None:
    """写失败时的本地关闭：留下可导入的证据，等补偿任务补写认知库。

    没有 ``evidence_ref`` 时只是普通本地关闭（例如观察/过期），不会被补偿任务
    当成「执行完成」导入——过期不等于完成，方案对这两件事分得很清。
    """
    if not evidence_ref and state != "abandoned":
        from core.context.situation import project
        goal = project(model.events).intentions.get(str(key))
        if goal is None or goal.kind != "observe":
            raise ValueError("goal_evidence_required")
    if evidence_ref and not _verified_ref(str(evidence_ref), _as_scope(getattr(model,"scope",None)), state):
        raise ValueError("goal_evidence_unverified")
    payload: dict[str, Any] = {"key": str(key), "result": str(result or "")}
    if str(state or ""):
        payload["state"] = str(state)
    reference = str(evidence_ref or "")
    if reference:
        payload["evidence_ref"] = reference
        payload["_pending"] = True
    model.add("intention_closed", payload, at=str(at or ""))


def commit_close(
    model: Any,
    key: str,
    *,
    at: str = "",
    result: str = "",
    evidence_ref: str = "",
    scope: Any = None,
    mark: bool = True,
    state: str = "",
    payload: Mapping[str, Any] | None = None,
    source: str = "execution",
    idempotency_prefix: str = "goal-close",
) -> bool:
    """提交一次目标关闭；返回是否已落账（重复提交也算成功）。

    ``scope`` 不给就用 ``model.scope``；两者都没有/不完整 → 不写。
    ``mark=True`` 时在模型里补一条 ``_derived`` 关闭事件（同进程立即可见，
    落盘时会被过滤，不形成第二份记录）。
    """
    from core.context.cognitive_commit import (
        CommitContext,
        Proposal,
        closed_states_for_scope,
        commit_updates,
        connect,
    )

    subject = str(key or "")
    ref = str(evidence_ref or "")
    bound = _as_scope(scope if scope is not None else getattr(model, "scope", None))
    model_scope = _as_scope(getattr(model, "scope", None))
    if model_scope is not None and bound is not None and model_scope != bound:
        return False
    if not subject or not ref or bound is None or not _verified_ref(ref, bound, state):
        return False

    try:
        # 已经关闭就不再加一条：同一影响不因多读一次而重复累计。
        terminal = closed_states_for_scope(bound).get(subject)
        if terminal:
            if mark:
                _mark_closed(model, subject, at, str(result or ""), terminal)
            return True
    except Exception as exc:  # noqa: BLE001 - 读不到就按未关闭继续；提交本身仍幂等
        skips.skip("goal_close.commit_close:terminal_lookup", "degraded", repr(exc)[:120])

    conn = None
    try:
        conn = connect()
        body = (
            dict(payload)
            if payload is not None
            else {
                "intention_key": subject,
                "result": str(result or ""),
                "at": str(at or ""),
            }
        )
        if str(state or ""):
            # 显式终态优先于「按 result 猜」：执行失败不能被读成完成。
            body["state"] = str(state)
        outcome = commit_updates(
            conn,
            context=CommitContext(scope=bound, source=str(source or ""), visible_event_ids=frozenset({ref}), allowed_subjects=frozenset({subject})),
            proposals=[
                Proposal(
                    proposal_id="close:%s:%s" % (subject, ref),
                    subject_key=subject,
                    operation="close",
                    payload=body,
                    evidence_refs=(ref,),
                    occurrence=ref,
                    occurred_at=str(at or ""),
                )
            ],
            idempotency_key="%s:%s:%s" % (
                str(idempotency_prefix or "goal-close"),
                bound.chat_id,
                ref,
            ),
        )
        if not outcome.ok:
            warn(
                "goal_close_rejected",
                key=subject,
                reasons=[reason for _pid, reason in outcome.rejected],
            )
            return False
    except Exception as exc:  # noqa: BLE001 - 写失败由调用方决定是否本地兜底
        warn("goal_close_failed", key=subject, err=repr(exc)[:120])
        return False
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception as exc:  # noqa: BLE001
                skips.skip("goal_close.commit_close:conn_close", "degraded", repr(exc)[:120])

    if mark:
        marked_result = str(result or "")
        if not marked_result and isinstance(payload, Mapping):
            marked_result = str(payload.get("result") or "")
        _mark_closed(model, subject, at, marked_result, state)
    return True
