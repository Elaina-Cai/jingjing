# -*- coding: utf-8 -*-
"""主动触达的「怎么认 + 记一笔」——从 ``core/speech/outbound.py`` 迁出的纯逻辑（R2）。

为什么在这里：出口要回答「这一条是不是她主动开口的」，答案决定两件事——要不要按
主动触达的策略记账（``terminal.proactive_service.record_send``）、以及出口审计行里
要不要标 ``_proactive``。这段判定原来和发送流程搅在同一个 1700 行文件里，读的人
很容易把它当成「发送的一部分」去改。

判据本身一律是「读入参」，不看进程状态：

* 显式声明：``draft.extra`` 里的 ``proactive`` / ``proactive_send`` / ``_proactive``，
  或 ``send_mode`` / ``trigger`` 写作 ``proactive``；
* 从快照继承：``snapshot.turn.trigger_reason`` 或 ``snapshot.audience`` 是 proactive；
* 从上下文继承：``context.trigger`` 属于主动触达那三个值，或
  ``context.extra["trigger_reason"] == "proactive"``。

**不从「显示名与 wxid 不一致」推断跨会话**：正常回复经常用备注名，而快照里存的是
原始 id，用名字差异去猜会把她对群友的正常回话记成主动触达。

``record_send`` 只有一个副作用：往主动触达服务记一笔（服务不可用就安静放弃——
记账失败绝不许影响已经发出去的那句话）。``content_preview`` 由调用方传，因为出口
那边对 ``_preview`` 有既有口径（200 字截断），测试也在 patch 它。
"""
from __future__ import annotations

from typing import Any
from safelog import warn


def is_proactive_send(draft: Any, snapshot: Any, context: Any = None) -> bool:
    """这一条出站动作是不是由她主动发起的接触。

    正常回复永远不算主动触达。信号可以显式挂在 ``draft.extra`` 上，也可以从
    语音快照（``trigger_reason == 'proactive'`` / ``AudienceKind.PROACTIVE``）
    或 agent 上下文（主动触达那三个 trigger 值）继承。
    """
    extra = getattr(draft, "extra", None) or {}
    for key in ("proactive", "proactive_send", "_proactive"):
        value = extra.get(key)
        if value is True or str(value or "").strip().lower() in (
            "1", "true", "yes", "proactive",
        ):
            return True
    if str(extra.get("send_mode") or "").strip().lower() == "proactive":
        return True
    if str(extra.get("trigger") or "").strip().lower() == "proactive":
        return True

    turn = getattr(snapshot, "turn", None)
    if str(getattr(turn, "trigger_reason", "") or "").strip().lower() == "proactive":
        return True
    audience = getattr(snapshot, "audience", None)
    if str(getattr(audience, "value", audience) or "").strip().lower() == "proactive":
        return True

    trigger = str(getattr(context, "trigger", "") or "").strip().lower()
    if trigger in ("proactive", "proactive_trigger", "yinyue_autonomous"):
        return True
    context_extra = getattr(context, "extra", None) or {}
    if str(
        context_extra.get("trigger_reason") or ""
    ).strip().lower() == "proactive":
        return True
    return False


def proactive_target_id(draft: Any, snapshot: Any, account_id: str = "") -> str:
    """主动触达要记在哪个会话上（显式给的优先，其次才是 ``who`` / 快照目标）。"""
    extra = getattr(draft, "extra", None) or {}
    for key in ("proactive_chat_id", "target_chat_id"):
        value = str(extra.get(key) or "").strip()
        if value:
            return value
    candidate = str(getattr(draft, "who", "") or "").strip()
    if not candidate:
        target = getattr(snapshot, "turn", None)
        target = getattr(target, "target", None)
        candidate = str(getattr(target, "chat_id", "") or "").strip()
    if not candidate or not account_id:
        return candidate
    target_obj = getattr(draft, "target", None)
    if getattr(target_obj, "platform", "") != "wechat":
        return candidate
    if candidate.endswith("@chatroom") or candidate.startswith(("wxid_", "v1_")):
        return candidate
    try:
        # 工具层可能传备注名/昵称；主动触达按原始 chat id 存，所以只在唯一时才解析。
        # 这里刻意不用 wx_chat 的进程级账号 id。
        from core.terminal.store_port import get_account_db_manager

        manager = get_account_db_manager()
        repos = []
        if manager:
            repo = manager.get_repo(target_obj.platform, account_id)
            if repo is not None:
                repos.append(repo)
            for (platform, _account), candidate_repo in manager.all_repos().items():
                if platform != target_obj.platform:
                    continue
                if candidate_repo not in repos:
                    repos.append(candidate_repo)
        resolved_ids: set[str] = set()
        for repo in repos:
            for contact in repo.contacts.search_by_name(candidate, limit=20):
                names = {
                    str(getattr(contact, "nickname", "") or "").strip(),
                    str(getattr(contact, "remark", "") or "").strip(),
                }
                if candidate in names:
                    value = str(getattr(contact, "wxid", "") or "").strip()
                    if value:
                        resolved_ids.add(value)
        if len(resolved_ids) == 1:
            return next(iter(resolved_ids))
    except Exception:
        warn("core/speech/proactive_ledger.py:114 Exception", err=repr(__import__("sys").exc_info()[1]))
    return candidate


def proactive_service() -> Any:
    """主动触达服务；建不出来就返回 None（记账失败绝不影响发送）。"""
    try:
        from core.terminal.proactive_service import build_proactive_service

        return build_proactive_service()
    except Exception:
        warn("core/speech/proactive_ledger.py:125 Exception", err=repr(__import__("sys").exc_info()[1]))
        return None


def record_send(
    draft: Any,
    snapshot: Any,
    context: Any,
    account_id: str,
    *,
    kind: str = "text",
    ok: bool = True,
    error: str = "",
    content_preview: str = "",
) -> None:
    """主动发出去的一条记一笔。不是主动触达就直接返回。"""
    if not is_proactive_send(draft, snapshot, context):
        return
    target_id = proactive_target_id(draft, snapshot, account_id)
    if not target_id:
        return
    service = proactive_service()
    if service is None:
        return
    target_obj = getattr(draft, "target", None)
    service.record_send(
        platform=getattr(target_obj, "platform", ""),
        bot_account_id=account_id or getattr(target_obj, "account_id", ""),
        chat_id=target_id,
        kind=kind,
        trigger="proactive",
        content_preview=content_preview,
        ok=ok,
        error=error,
    )


__all__ = [
    "is_proactive_send",
    "proactive_service",
    "proactive_target_id",
    "record_send",
]
