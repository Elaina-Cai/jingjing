# -*- coding: utf-8 -*-
"""把账本里的「我答应过的事」变成目标真源里的一条 `promise` 目标。

方案 §8：`promise` 是「有承诺来源的目标」的工具外观。账本（`loop/ledger.py`）
继续记流水与审计，但**欠着/完成**由目标投影回答。这里只做目标侧的桥：
- 一个账本承诺对应 `promise:<ledger_id>`；
- 已存在（无论 open/closed）就不重复开，重放幂等；
- 目标带 `origin=self_promise` 与可回指的 evidence（原话）。
"""
from __future__ import annotations

from typing import Any

__all__ = [
    "close_promise_goal",
    "ensure_promise_goal",
    "promise_goal_ids",
    "promise_goal_key",
]

_PREFIX = "promise:"


def promise_goal_key(promise_id: Any) -> str:
    raw = str(promise_id or "").strip()
    if not raw:
        return ""
    return f"{_PREFIX}{raw}"


def _intentions(model: Any) -> dict:
    from core.context.situation import project

    return dict(project(model.events).intentions)


def promise_goal_ids(chat_id: Any, *, scope: Any = None) -> frozenset[str]:
    """这个会话里已经映射过的承诺目标（含已关闭的，避免账本行重复展示）。"""
    if not str(chat_id or "").strip():
        return frozenset()
    from core.execution.data_channels import get_world

    model = get_world(chat_id, scope=scope)
    return frozenset(
        key for key in _intentions(model) if key.startswith(_PREFIX)
    )


def ensure_promise_goal(
    chat_id: Any,
    *,
    promise_id: Any,
    what: str,
    who: str = "",
    due: str = "",
    at: str = "",
    scope: Any = None,
    source_refs: tuple[str, ...] | list[str] = (),
) -> str:
    """确保这条承诺有对应目标；返回目标 key（空串 = 缺 chat 或 id）。"""
    key = promise_goal_key(promise_id)
    chat = str(chat_id or "").strip()
    text = " ".join(str(what or "").split())
    if not key or not chat or not text:
        return ""

    from core.context.situation import open_intention
    from core.execution.data_channels import get_world, save_world

    model = get_world(chat, scope=scope)
    if key in _intentions(model):
        current = _intentions(model)[key]
        for ref in source_refs or ():
            reference = str(ref or "").strip()
            if reference and reference not in current.evidence_refs:
                from core.context.situation import note_evidence

                note_evidence(
                    model,
                    key,
                    reference,
                    at=str(at or ""),
                    source="promise_source",
                )
        if not save_world(chat, scope=model.scope):
            raise OSError("promise_goal_persist_failed")
        return key
    open_intention(
        model,
        key,
        kind="promise",
        at=str(at or ""),
        evidence=text,
        due_at=str(due or ""),
        origin="self_promise",
        motivation="我答应了对方",
        desired_outcome=text,
        done_when="对方确认，或对应的执行事实出现",
        evidence_refs=(
            f"ledger:{promise_id}",
            *(str(ref) for ref in source_refs if str(ref)),
        ),
        scope=f"chat:{chat}",
    )
    if not save_world(chat, scope=model.scope):
        raise OSError("promise_goal_persist_failed")
    return key


def close_promise_goal(
    chat_id: Any,
    *,
    promise_id: Any,
    at: str = "",
    result: str = "",
    evidence_ref: str = "",
    scope: Any = None,
    state: str = "completed",
) -> bool:
    """关闭承诺目标；账本行是证据，不是第二条销账通道。

    返回 False = 没有这个目标（旧账本行还没映射）或参数不全。写认知库失败时
    退回本地关闭并留 `_pending` 证据，重启后由补偿任务导入。
    """
    key = promise_goal_key(promise_id)
    chat = str(chat_id or "").strip()
    if not key or not chat:
        return False

    from core.context.situation import project
    from core.execution.data_channels import get_world, save_world

    model = get_world(chat, scope=scope)
    if key not in project(model.events).intentions:
        return False
    reference = str(evidence_ref or "")
    from core.context.goal_close import close_local, commit_close

    if not reference:
        if state != "abandoned":
            return False
        close_local(model, key, at=str(at or ""), result=str(result or ""), state=state)
        save_world(chat, scope=model.scope)
        return True

    ok = commit_close(
        model,
        key,
        at=str(at or ""),
        result=str(result or ""),
        evidence_ref=reference,
        scope=scope,
        mark=True,
        state=state,
    )
    if not ok:
        close_local(
            model,
            key,
            at=str(at or ""),
            result=str(result or ""),
            evidence_ref=reference,
            state=state,
        )
    save_world(chat, scope=model.scope)
    return True
