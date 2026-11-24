# -*- coding: utf-8 -*-
"""重启恢复：把写失败时留在世界模型里的关闭补写进认知库。

为什么需要它
============
执行侧关闭的顺序是「先写认知库；写不进去才本地兜底」。本地兜底是为了防止下一
tick 重放已经执行过的动作，但它把完成事实留在了文件里——如果进程随后重启，
认知库并不知道这件事已经完成。本模块在启动时扫一遍世界模型，把带
``_pending`` + ``evidence_ref`` 的本地关闭补写成 ``close`` 提案：

* 只导入**执行事实**（有 evidence_ref 的关闭）；过期/观察类关闭没有执行证据，
  不冒充「完成」——方案对「过期」和「完成」分得很清；
* 写成功后追加 ``goal_imported`` 标记，避免每次启动重复导入；
* 提交本身幂等，重复跑不会多出第二条 close。

边界：没有会话坐标的旧模型跳过（无从判断属于哪个会话）；导入失败只告警，
不阻断启动——宁可下次再试，也不让恢复流程拖垮引擎。
"""
from __future__ import annotations

from typing import Any

from safelog import warn

__all__ = ["import_all_pending", "import_pending_closes", "pending_local_closes"]

_CLOSED = "intention_closed"
_IMPORTED = "goal_imported"
_SCOPE_FIELDS = ("platform", "account_id", "chat_id")


def _imported_identity(event: Any) -> tuple[str, str]:
    payload = event.payload or {}
    return (str(payload.get("key") or ""), str(payload.get("imported_from") or ""))


def pending_local_closes(model: Any) -> tuple[dict[str, str], ...]:
    """还没导入的本地执行关闭（只认带 evidence_ref 的）。"""
    imported = {
        _imported_identity(event) for event in model.events if event.kind == _IMPORTED
    }
    out: list[dict[str, str]] = []
    for event in model.events:
        if event.kind != _CLOSED:
            continue
        payload = event.payload or {}
        if not payload.get("_pending"):
            continue
        key = str(payload.get("key") or "")
        reference = str(payload.get("evidence_ref") or "")
        at = str(event.at or "")
        if not key or not reference or (key, at) in imported:
            continue
        out.append(
            {
                "key": key,
                "at": at,
                "result": str(payload.get("result") or ""),
                "evidence_ref": reference,
                "state": str(payload.get("state") or ""),
            }
        )
    return tuple(out)


def import_pending_closes(chat_id: Any, model: Any) -> int:
    """把一个会话里待导入的本地关闭补写进认知库；返回成功条数。"""
    scope = dict(getattr(model, "scope", {}) or {})
    if not all(scope.get(name) for name in _SCOPE_FIELDS):
        return 0

    from core.context.goal_close import commit_close

    imported = 0
    for item in pending_local_closes(model):
        try:
            ok = commit_close(
                model,
                item["key"],
                at=item["at"],
                result=item["result"],
                evidence_ref=item["evidence_ref"],
                scope=scope,
                mark=False,
                state=item["state"],
            )
        except Exception as exc:  # noqa: BLE001 - 单条失败不拖垮其余会话
            warn("goal_import_failed", key=item["key"], err=repr(exc)[:120])
            continue
        if not ok:
            continue
        model.add(
            _IMPORTED,
            {"key": item["key"], "imported_from": item["at"]},
            at="",
        )
        imported += 1
    return imported


def import_all_pending() -> dict[str, int]:
    """启动时扫一遍已知/盘上的世界模型；返回每个会话导入了几条。"""
    from core.context.situation_store import (
        all_world_models,
        get_world_model,
        persist_world_model,
    )

    report: dict[str, int] = {}
    try:
        chats = list(all_world_models().items())
    except Exception as exc:  # noqa: BLE001 - 扫描失败不阻断启动
        warn("goal_import_scan_failed", err=repr(exc)[:120])
        return report

    for identifier, snapshot in chats:
        chat_id = str(snapshot.scope.get("chat_id") or identifier)
        try:
            # 走 get_world_model：导入标记要写回同一个缓存对象，否则 persist
            # 会把另一个模型落盘（盘上枚举出来的只是只读副本）。
            model = get_world_model(chat_id, scope=snapshot.scope)
            count = import_pending_closes(chat_id, model)
            if count:
                persist_world_model(chat_id, scope=model.scope)
            report[identifier] = count
        except Exception as exc:  # noqa: BLE001 - 单个会话失败不影响其它
            warn(
                "goal_import_chat_failed",
                chat=str(chat_id)[:48],
                err=repr(exc)[:120],
            )
    return report
