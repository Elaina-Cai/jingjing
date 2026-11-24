# -*- coding: utf-8 -*-
"""对话侧销账（P1-1）：听见「办完了」→ 把那条未了结关掉。

**唯一读 ``OPEN_LOOP_CLOSE`` 三态 flag 的地方**（用 :func:`core.features.mode`，
别用 ``enabled``——它会把 shadow 读成 False）：

* ``off``    ← 默认：一行都不走，与接线前逐字节一致（回滚位）；
* ``shadow`` → 只算「打算关哪几条」并记 ``intention_close_shadow``，不改状态；
* ``on``     → 真的提交一条 ``close`` 到认知日志（**纯本地，零外发**）；读侧由
  ``situation_store`` 从日志派生关闭状态，文件只保留开启与旧事件。

为什么要这个模块：这本账之前只有「开」（``open_intention``）和「到点执行」
（``intention_tick``）两头；``intention_runner.match_observe`` 是唯一的关闭匹配器，
但它的条件只有 ``kind="observe"``，且全仓没有调用方——于是线上 7 条开、0 条关，
只长不销。本模块只补一件事：**从刚收到的话里认出"已经办完了"**。

三条保守纪律（宁可不关，不许错关——错关等于把她还欠着的事偷偷抹掉）：

1. 两个条件同时成立才算：句子里有完成标记（「办完了」「喝过了」…）+ 与意图主题
   有字面重合（主题取 ``evidence``，退化到 key 的冒号后半段）；
2. 只认 ``kind in ("remind", "act")``；``observe`` 归 ``match_observe``，别抢它的活；
3. 每次关闭都把**触发句**写进 ``result``（``completed:<句>``），事件流可回溯、
   可人工重开——"为什么关的"永远有答案。
"""
from __future__ import annotations

import re
from typing import Any, Iterable

import structlog

from core import features
from core.context.situation import WorldModel, open_intentions
from safelog import warn

log = structlog.get_logger()

__all__ = ["COMPLETION_MARKERS", "cancel_quoted_requests", "close_from_rows", "close_mode", "match_completion"]

_CANCEL_DIRECTIVE = re.compile(
    r"(?:请)?(?:取消(?:这件事|这事)?|(?:这件事|这事)?(?:不用|不要|别)(?:再)?(?:查|做|办)了)[。！!]*"
)

#: 完成标记：宁少不多。单独出现**不足以关闭**（还要过主题重合那一关）。
COMPLETION_MARKERS: tuple[str, ...] = (
    "办完了", "办好了", "办妥了", "做完了", "搞定了", "弄好了", "完成了",
    "已经", "好了", "过了", "吃了", "喝了", "买了", "交了", "发了", "寄了",
    "done", "finished", "ok了",
)

#: 主题里这些字不算"内容"（不然「记得我」里的"我"会跟任何一句话重合）。
_STOP_CHARS = frozenset(
    "记得一下我你他她它的地得了要去来在是和跟帮给请再还就都也吗呢吧啊么点个这那有没不把"
    "让到上中之后时候间明天今后天上午下晚早周星期分秒年月日号次遍趟位件条张只种些每"
)


def close_mode() -> str:
    """当前档位（off / shadow / on）。"""
    return features.mode(features.OPEN_LOOP_CLOSE, features.MODE_OFF)


def _text_of(obj: Any, name: str) -> str:
    """兼容 dict 与行对象（快照里的 row 有 .text / .at / .speaker / .is_self）。"""
    if isinstance(obj, dict):
        return str(obj.get(name) or "")
    return str(getattr(obj, name, "") or "")


def _flag_of(obj: Any, name: str) -> bool:
    if isinstance(obj, dict):
        return bool(obj.get(name))
    return bool(getattr(obj, name, False))


def _subject_of(intent: Any) -> str:
    """意图的主题文本：优先 ``evidence``，退化到 key 的冒号后半段（``<类别>:<主体>`` → 主体）。

    类别前缀（数据里 key 冒号前那一段）是适配层给的标签，不是主题内容；
    它出现在 ``evidence`` 正文里时也要剥掉，否则会污染"内容字"。
    """
    key = str(getattr(intent, "key", "") or "")
    label = key.split(":", 1)[0] if ":" in key else ""
    evidence = " ".join(str(getattr(intent, "evidence", "") or "").split())
    if evidence:
        return evidence.replace(label, " ") if label else evidence
    key = str(getattr(intent, "key", "") or "")
    if ":" in key:
        return key.split(":", 1)[1]
    return key


def _content_chars(text: str) -> list[str]:
    """主题里的"内容字"：去停用字、去数字、去标点。"""
    out: list[str] = []
    for ch in " ".join(str(text or "").split()):
        if ch in _STOP_CHARS or ch.isdigit():
            continue
        if ch.isalnum():
            out.append(ch.lower())
    return out


def _enough_overlap(subject: list[str], message: str) -> bool:
    """主题内容字在句子里至少重合一半（向上取整）——2 字主题命中 1 字即可。"""
    uniq: list[str] = []
    for ch in subject:
        if ch not in uniq:
            uniq.append(ch)
    if not uniq:
        return False
    need = max(1, (len(uniq) + 1) // 2)
    return sum(1 for ch in uniq if ch in message) >= need


def match_completion(
    model: WorldModel,
    *,
    messages: Iterable[Any] = (),
    kinds: tuple[str, ...] = ("remind", "act"),
) -> tuple[tuple[str, str, str], ...]:
    """哪几条 open 意图已经被这几句话办完了 -> ``(key, 触发句, 触发时刻)``。

    只读：不动 model、不关账。纯函数，便于单测。
    """
    rows: list[tuple[str, str, str]] = []
    for item in messages or ():
        if _flag_of(item, "is_self"):
            continue
        body = " ".join(_text_of(item, "text").split())
        if not body:
            continue
        rows.append((_text_of(item, "at"), _text_of(item, "speaker") or "?", body))
    rows.sort(key=lambda r: r[0])
    if not rows:
        return ()
    matched: list[tuple[str, str, str]] = []
    for intent in open_intentions(model):
        kind = str(getattr(intent, "kind", "") or "")
        if kind not in kinds:
            continue
        subject = _content_chars(_subject_of(intent))
        if not subject:
            continue
        for at, _who, body in rows:
            low = body.lower()
            if not any(mark in low for mark in COMPLETION_MARKERS):
                continue
            if not _enough_overlap(subject, low):
                continue
            matched.append((str(getattr(intent, "key", "") or ""), body, at))
            break
    return tuple(matched)


def cancel_quoted_requests(scope: Any, sources: Iterable[tuple[Any, str]]) -> tuple[str, ...]:
    """Close a uniquely identified pending goal on its requester's direct cancellation."""
    from core.context import appraisal, situation_store
    from core.context.goal_close import commit_close
    from core.conversation.addressing import strip_quote_block
    from core.memory.belief_commit import evidence_state
    from core.terminal.shadow import get_default_store

    if not scope.complete:
        return ()
    closed = []
    for row, ref in sources:
        if not ref or not _CANCEL_DIRECTIVE.fullmatch(strip_quote_block(_text_of(row, "text"))):
            continue
        store = get_default_store()
        message = store.get(ref)
        if not appraisal._peer(message, scope):
            continue
        directive = strip_quote_block(message.content)
        if not _CANCEL_DIRECTIVE.fullmatch(directive):
            continue
        parent = appraisal._quoted_peer(store, scope, message, _text_of(row, "quote_id"))
        if (parent is None or appraisal.author(parent) != appraisal.author(message)
                or parent.occurred_at > message.occurred_at):
            continue
        if any(evidence_state(item.event_id, scope, store=store) != "alive" for item in (message, parent)):
            continue

        with situation_store._LOCK:
            world = situation_store.get_world_model(scope.chat_id, scope=scope)
            situation_store._apply_cognitive_closures(world, scope.chat_id)
            # Match the evidence that opened the goal. Later observations and receipts do not grant authority.
            opened = {event.payload.get("key"): event.payload.get("evidence_refs") or ()
                      for event in world.events if event.kind == "intention_opened"}
            matches = []
            for goal in open_intentions(world):
                refs = opened.get(goal.key, ())
                if isinstance(refs, str):
                    refs = (refs,)
                if (goal.origin in {"owner_request", "self_promise"}
                        and goal.state != "proposed" and parent.event_id in refs):
                    matches.append(goal)
            if len(matches) != 1:
                continue
            goal = matches[0]
            if commit_close(
                world, goal.key, at=message.occurred_at.isoformat(sep=" ", timespec="seconds"),
                result=f"abandoned:{directive}", evidence_ref=ref, source="conversation",
                idempotency_prefix="intention-cancel", state="abandoned",
            ):
                closed.append(goal.key)
    return tuple(closed)


def _evidence_ref(row: Any, scope: Any) -> str:
    """触发句对应的事件 id。

    优先用行上自带的 ``event_id``（测试/rehearse）；世界行用 ``platform_msg_id``
    到事件流里按四元组回查，旧调用方仍可提供 ``msg_id``。查不到就返回空串，
    宁可下一拍再关，也不拿一句话当凭据。
    """
    direct = str(_text_of(row, "event_id") or "")
    if direct:
        return direct
    msg_id = _text_of(row, "platform_msg_id") or _text_of(row, "msg_id")
    if not msg_id:
        return ""
    try:
        from core.terminal.shadow import get_default_store

        found = get_default_store().get_by_external_msg_id(
            platform=str(getattr(scope, "platform", "") or ""),
            bot_account_id=str(getattr(scope, "account_id", "") or ""),
            chat_id=str(getattr(scope, "chat_id", "") or ""),
            external_msg_id=msg_id,
        )
        return str(getattr(found, "event_id", "") or "")
    except Exception:  # noqa: BLE001 - 回查失败按「没有证据」
        return ""


def _commit_close(
    *,
    model: Any = None,
    scope: Any,
    key: str,
    trigger: str,
    at: str,
    ref: str,
    mark: bool = False,
) -> bool:
    """把一次关闭写成认知提交（与执行侧共用同一个提交口）。"""
    from core.context.goal_close import commit_close

    return commit_close(
        model,
        key,
        at=at,
        evidence_ref=ref,
        scope=scope,
        mark=mark,
        payload={
            "intention_key": key,
            "trigger": trigger[:120],
            "result": "completed:%s" % trigger[:20],
            "at": at,
        },
        source="conversation",
        idempotency_prefix="intention-close",
        state="completed",
    )


def close_from_rows(
    chat_id: Any,
    messages: Iterable[Any] = (),
    *,
    at: str = "",
    model: Any = None,
    mode: str = "",
    scope: Any = None,
) -> dict:
    """把这一窗新到的话过一遍，关掉已经办完的意图。返回读数（给 trace / 单测看）。

    关闭的唯一真源是认知事件日志：``scope`` 给了才写（``Scope`` 四元组，由回合
    现场提供）；没给就只匹配、不改变状态——缺身份时宁可不关，也不写一份不知道
    属于哪个会话的完成事实。

    ``model`` 给了就不读盘（单测与 rehearse 用）；关闭后不往文件里补事件，
    只清进程缓存，下一次加载由 ``situation_store`` 从认知库派生关闭状态。
    """
    chat = " ".join(str(chat_id or "").split())
    out: dict = {"mode": "off", "matched": (), "closed": ()}
    if not mode:
        mode = close_mode()
    out["mode"] = mode
    if mode == features.MODE_OFF or not chat:
        return out
    loaded = model is None
    try:
        if loaded:
            from core.execution.data_channels import get_world

            model = get_world(chat, scope=scope)
        matched = match_completion(model, messages=messages)
        out["matched"] = tuple(matched)
        if not matched:
            return out
        if mode == features.MODE_SHADOW:
            log.info(
                "intention_close_shadow",
                chat_id=chat,
                keys=[k for k, _t, _a in matched],
                triggers=[t[:40] for _k, t, _a in matched],
            )
            return out
        if scope is None or not getattr(scope, "complete", False):
            log.info(
                "intention_close_no_scope",
                chat_id=chat,
                keys=[k for k, _t, _a in matched],
            )
            return out
        rows = list(messages or ())
        closed: list[str] = []
        for key, trigger, stamp in matched:
            ref = ""
            for row in rows:
                if (
                    " ".join(_text_of(row, "text").split()) == trigger
                    and _text_of(row, "at") == stamp
                ):
                    ref = _evidence_ref(row, scope)
                    break
            if not ref:
                log.info("intention_close_no_evidence", chat_id=chat, key=key)
                continue
            if _commit_close(
                model=model if loaded else None,
                scope=scope,
                key=key,
                trigger=trigger,
                at=(stamp or at),
                ref=ref,
                mark=loaded,
            ):
                closed.append(key)
        out["closed"] = tuple(closed)
        if closed and loaded:
            # 保存首次绑定的坐标；派生关闭会被 save_world 过滤，不另写完成事实。
            from core.execution.data_channels import save_world
            save_world(chat)
        if closed:
            log.info(
                "intention_closed_by_chat",
                chat_id=chat,
                keys=closed,
                triggers=[t[:40] for _k, t, _a in matched],
            )
        return out
    except Exception as exc:  # noqa: BLE001 - 销账失败不许拖垮这一拍
        warn("intention_close_failed", err=repr(exc)[:120])
        return out
