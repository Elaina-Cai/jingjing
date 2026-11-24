# -*- coding: utf-8 -*-
"""出站文本的「最后一步」：摘计划 / 开事 / 补问句 / 去完成式（R2 从 outbound 迁出）。

为什么这几件事必须住在出口：出口是**所有**对外文本的最后一站（回话、主动搭话、
审批件、节奏器都从这里出去），只有它在「真要发出去的那一刻」还拿得到正文与目标。

为什么现在只剩一份（2026-10-23 合一）：``core/routing/turn_pipeline.py`` 里曾经有
三份并行实现——一份 ``<plan>`` 摘块 + 开事、两份「把在等批准的那件事问一次」（其中
一份不记账）。后果是同一句话可能被补两句问句，而「哪一份生效」取决于这一轮走的是
直回还是触发路径。真机事故是 2026-10-21 13:01 的 ``job-e20bc554e00aafc8``：事开成
了、问句没出门（问句写在只有旧链路才走的那个分支里）。

现在的分工：

* **路由侧**只做两件本地判断（不消费、不开事）：把机器语法从正文里摘掉（摘不掉的
  绝不许发到聊天里）、记下「这一轮模型附了计划」（好让计划器别再问一次「哪几步」）。
  附了的计划塞进 ``context.extra["_pending_plan"]`` 交给出口。
* **出口**（本模块）负责：摘干净 + 消费成「一件事」+ 记 ``plan_block_consumed``，
  再把「有件在等你批准」的问句补一次并记账（``ask``），最后去掉没有台账证据的完成式。

全部是纯文本进出 + 一次台账写入；不调模型、不阻塞发送（每一步失败都退回原文）。
"""
from __future__ import annotations

import re
from typing import Any


def chat_id_of(context: Any) -> str:
    """从出站上下文里取目标会话（三个候选键，按顺序取第一个非空）。"""
    for key in ("chat_id", "target_id", "target"):
        value = getattr(context, key, "") if context is not None else ""
        if value:
            return str(value)
    return ""


def trace_id_of(context: Any) -> str:
    return str(getattr(context, "trace_id", "") or "") if context is not None else ""


def drop_done_claims(text: str, *, log: Any) -> str:
    """把「已发 / 我去发」这类完成式从句子里去掉——没有台账证据就不许这么说。"""
    body = str(text or "")
    if not body:
        return body
    try:
        from core.jobs import ask as _ask

        if not _ask.looks_like_done_claim(body):
            return body
        kept = [
            piece for piece in re.split(r"(?<=[。！？!?])", body)
            if piece.strip() and not _ask.looks_like_done_claim(piece)
        ]
        clean = "".join(kept).strip()
        log.warning("done_claim_without_evidence len=%d kept=%d", len(body), len(clean))
        return clean or "这条我还没发出去，别当成了。"
    except Exception as exc:  # noqa: BLE001 - 守卫失败也照发
        log.warning("done_claim_guard_failed", error=repr(exc))
        return body


def consume_plan_block(text: str, context: Any, *, log: Any) -> str:
    """把正文里的 ``<plan>`` 块摘掉并消费成「一件事」；返回可以真发出去的正文。

    ``context.extra["_pending_plan"]`` 由路由侧在**摘掉正文之后**塞进来（它先摘是为了
    不让机器语法漏到聊天里，但不该由它开事：开事要知道真实目标与会话追踪号）。两条
    来源取到计划就消费，取到就 ``pop`` 掉，避免同一份计划被消费两次。
    """
    try:
        from core.jobs import plan_block

        pending = None
        extra = getattr(context, "extra", None)
        if isinstance(extra, dict):
            pending = extra.pop("_pending_plan", None)
        clean, extracted = plan_block.extract(text)
        plan = pending if pending is not None else extracted
        if plan is None:
            return clean or text
        chat_id = chat_id_of(context)
        if not chat_id:
            log.warning("plan_block_no_target")
            return clean or text
        job_id = plan_block.consume(plan, chat_id=chat_id, trace_id=trace_id_of(context))
        log.info("plan_block_consumed", job_id=job_id)
        return clean or text
    except Exception as exc:  # noqa: BLE001 - 摘块失败也不许拦住发送
        log.warning("plan_block_hook_failed", error=repr(exc))
        return text


def append_pending_ask(text: str, context: Any, *, log: Any) -> str:
    """把「有一件在等你批准」的问句附到真正发出去的那条后面（只问一次）。"""
    try:
        from core.jobs import ask as ask_mod
        from core.jobs.store import default_store

        chat_id = chat_id_of(context)
        if not chat_id:
            return text
        if not ask_mod.is_owner_private(chat_id):
            # 批准请求只送主人本人的私聊（2026-10-21 真机：问句被拼进了群里那条回复）
            log.info("job_ask_deferred chat_id=%s reason=not_owner_private", chat_id)
            return text
        ledger = default_store()
        items = ask_mod.pending_asks(ledger, chat_id, asked=ledger.asked_refs(chat_id))
        if not items:
            return text
        item = items[0]
        merged = (text + "\n\n" + str(item["text"])).strip()
        ledger.append(
            item["job_id"], "ask",
            payload={"ref": item["ref"], "step_n": item.get("step_n", 0)},
            actor="judge",
        )
        log.info("job_ask_appended", job_id=item["job_id"], ref=item["ref"])
        return merged
    except Exception as exc:  # noqa: BLE001 - 问不出口也不许妨碍说话
        log.warning("job_ask_append_failed", error=type(exc).__name__)
        return text


__all__ = [
    "append_pending_ask",
    "chat_id_of",
    "consume_plan_block",
    "drop_done_claims",
    "trace_id_of",
]
