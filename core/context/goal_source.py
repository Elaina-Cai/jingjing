"""目标来源是否仍可作为当前依据的唯一只读判定。"""
from __future__ import annotations

from typing import Any

from core.context import cognitive_commit as cc
from core.memory import belief_commit
from safelog import warn

_LABELS = {
    "unknown": "[依据未核实]",
    "unverified": "[来源未记录]",
}


def source_label(state: str) -> str:
    """把来源状态转成提示词里的短标签；有效或已失效状态不加标签。"""
    return _LABELS.get(str(state or ""), "")


def goal_source_state(goal: Any, scope: Any, *, store: Any = None) -> str:
    """汇总目标的来源状态：``alive`` / ``dead`` / ``unknown`` / ``unverified``。

    任一来源已确认撤回就判 ``dead``，因为目标是对该请求的执行承诺。
    查不到来源是 ``unknown``，保留目标并显式提示未核实。没有来源引用是
    ``unverified``，保留旧目标但标明来源没有登记。
    """
    refs = tuple(
        str(ref).strip()
        for ref in (getattr(goal, "evidence_refs", ()) or ())
        if str(ref).strip()
    )
    if not refs:
        return "unverified"
    identity = cc._scope_identity(scope)
    if identity is None:
        return "unknown"
    evidence_scope = cc.Scope(*identity)
    try:
        if store is None:
            states = [
                belief_commit.evidence_state(ref, evidence_scope)
                for ref in refs
            ]
        else:
            states = [
                belief_commit.evidence_state(ref, evidence_scope, store=store)
                for ref in refs
            ]
    except Exception as exc:  # noqa: BLE001 - 查不了来源不等于来源已撤回
        warn(
            "goal_source_state_failed",
            goal=str(getattr(goal, "key", "") or ""),
            scope=identity,
            err=repr(exc)[:120],
        )
        return "unknown"
    if "dead" in states:
        return "dead"
    if "alive" in states:
        return "alive"
    return "unknown"


__all__ = ["goal_source_state", "source_label"]
