# -*- coding: utf-8 -*-
"""请求 → 回复 → @对象 的确定性链（2026-09-28 @错人修法）。

数据来源（已存在于事件流，不用新写钩子）：
  * 她的回复行：``event_type='reply'`` + ``parent_event_id``（= 被回应那条消息事件的
    **event_id**；A1-c 之前的存量行里写的是平台 msg_id，读侧按「像不像事件 id」兼容）
    + ``recipient_person_ids_json``（她@给了谁）；平台 msg_id 另存 ``metadata.parent_msg_id``；
  * 被回应行：metadata 里的 ``msg_id``（平台消息 id）。
``chat_window`` 先把 ``parent_event_id`` 解析成平台 msg_id（行的 ``parent_msg_id``），
再把它对上某行的 ``msg_id``，就得到"她答的是哪条"。

用途（三处消费）：
  1. admission 候选行标注 ``[已答 13:24→@Elaina]``（给 judge 判据）；
  2. 轮次组装把"请求 + 她对它的答复"成对带进视野（自环保底的一部分）；
  3. 局况一行事实。

纪律：只做**确定性连接**（parent 能对上才算），不猜；缺 parent 的一律不连（只留观察）。
开关：``YINYUE_ANSWER_LINKS=0`` 一键回退（默认开）。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, Sequence
from safelog import warn
from core.infra.envflags import flag, text


def answer_links_enabled() -> bool:
    raw = text("YINYUE_ANSWER_LINKS").lower()
    return flag("YINYUE_ANSWER_LINKS", True)


@dataclass(frozen=True)
class AnswerLink:
    """一条"她答复了某条消息"的链（确定性）。"""

    request_event_id: str = ""
    request_time: datetime | None = None
    request_speaker: str = ""
    request_content: str = ""
    reply_event_id: str = ""
    reply_time: datetime | None = None
    target_person_id: str = ""
    target_name: str = ""
    target_mismatch: bool = False  # 她@的人 ≠ 请求作者（只观察，不因此断路）


def _event_id(msg: Any) -> str:
    return str(getattr(msg, "event_id", "") or "")


def _msg_id(msg: Any) -> str:
    return str(getattr(msg, "msg_id", "") or "")


def _parent_id(msg: Any) -> str:
    return str(getattr(msg, "parent_msg_id", "") or "")


def _is_reply(msg: Any) -> bool:
    return bool(getattr(msg, "is_self", False)) and str(getattr(msg, "event_type", "") or "") == "reply"


def _reply_target(msg: Any, request: Any) -> tuple[str, str]:
    """她这条回复的对象：``(person_id, 显示名)``。

    2026-09-30 收口：``person_id`` 只取结构字段 ``recipient_person_ids_json``
    （发送路径写的，确定性）；显示名走 canonical 的收件人（``@X`` 由
    ``resolve_attribution`` 解析），不再在本模块里自己写 ``@`` 正则。链上
    实在没有收件人时退回"被回应那条的作者"——那是 ``parent_event_id`` 的
    确定连接，不是"最近说话人"的猜测。
    """
    recipients = tuple(getattr(msg, "recipients", ()) or ())
    target_id = str(recipients[0]) if recipients else ""
    target_name = ""
    try:
        from core.context.attribution import attribution_enabled, default_context

        if attribution_enabled():
            attribution = default_context(is_group=True).resolve(msg)
            if attribution is not None:
                for addressee in attribution.addressees:
                    if addressee.person_id == target_id:
                        target_name = addressee.display_name
                        break
                if not target_name or target_name == target_id:
                    # 身份表里没有名字：退回这条消息自己 @ 出来的显示名。
                    for addressee in attribution.addressees:
                        if (
                            addressee.display_name
                            and addressee.display_name != addressee.person_id
                        ):
                            target_name = addressee.display_name
                            break
    except Exception:  # noqa: BLE001 - 归属渲染失败不阻断链
        warn("core/context/answer_links.py:97 Exception", err=repr(__import__("sys").exc_info()[1]))
        target_name = ""
    if not target_name:
        target_name = str(getattr(request, "speaker", "") or "")
    return target_id, target_name


def derive_answer_links(rows: Sequence[Any]) -> list[AnswerLink]:
    """从 rows 里派生"请求→回复"链；无法确定性连接的一律不产出。"""
    if not answer_links_enabled() or not rows:
        return []
    by_msg_id: dict[str, Any] = {}
    for msg in rows:
        mid = _msg_id(msg)
        if mid and not bool(getattr(msg, "is_self", False)):
            by_msg_id.setdefault(mid, msg)
    out: list[AnswerLink] = []
    for msg in rows:
        if not _is_reply(msg):
            continue
        pid = _parent_id(msg)
        if not pid or pid not in by_msg_id:
            continue
        request = by_msg_id[pid]
        target_id, target_name = _reply_target(msg, request)
        author_id = str(getattr(request, "author_id", "") or "")
        out.append(
            AnswerLink(
                request_event_id=_event_id(request),
                request_time=getattr(request, "occurred_at", None),
                request_speaker=str(getattr(request, "speaker", "") or ""),
                request_content=str(getattr(request, "content", "") or "")[:40],
                reply_event_id=_event_id(msg),
                reply_time=getattr(msg, "occurred_at", None),
                target_person_id=target_id,
                target_name=target_name,
                target_mismatch=bool(target_id and author_id and target_id != author_id),
            )
        )
    return out


def links_by_request(rows: Sequence[Any]) -> dict[str, AnswerLink]:
    """{request_event_id: link}（同一请求被答多次时取最早一条）。"""
    out: dict[str, AnswerLink] = {}
    for link in derive_answer_links(rows):
        if link.request_event_id and link.request_event_id not in out:
            out[link.request_event_id] = link
    return out


def annotate_candidate_line(
    line: str, row: Any, links: dict[str, AnswerLink]
) -> str:
    """给"已答的请求行"加前缀（不改原文；只加确定性的链事实）。"""
    if bool(getattr(row, "is_pat", False)) or bool(getattr(row, "is_revoke", False)):
        return line  # 拍一拍/系统通知不标：它们不是"被问的请求"
    link = links.get(_event_id(row))
    if link is None:
        return line
    when = link.reply_time.strftime("%H:%M") if link.reply_time else "--:--"
    return f"[已答 {when}→@{link.target_name or link.request_speaker}] {line}"


def augment_rows_with_links(
    selected: Sequence[Any],
    all_rows: Sequence[Any],
    *,
    max_add: int = 6,
    window_seconds: float = 1800.0,
) -> list[Any]:
    """把"成对的另一半"补进 selected（请求进了就给答复；答复进了就给请求）。

    只补最近 ``window_seconds`` 内、至多 ``max_add`` 条；返回按时间排序的新列表。
    """
    if not answer_links_enabled() or not selected or not all_rows:
        return list(selected)
    links = derive_answer_links(all_rows)
    if not links:
        return list(selected)
    index: dict[str, Any] = {}
    for msg in all_rows:
        eid = _event_id(msg)
        if eid:
            index.setdefault(eid, msg)
    selected_ids = {_event_id(m) for m in selected if _event_id(m)}
    additions: list[Any] = []
    for link in links:
        if link.request_event_id in selected_ids and link.reply_event_id not in selected_ids:
            rep = index.get(link.reply_event_id)
            if rep is not None:
                additions.append(rep)
        elif link.reply_event_id in selected_ids and link.request_event_id not in selected_ids:
            req = index.get(link.request_event_id)
            if req is not None:
                additions.append(req)
    if not additions:
        return list(selected)
    newest = max(
        (m.occurred_at for m in selected if getattr(m, "occurred_at", None)),
        default=None,
    )
    seen = set(selected_ids)
    kept: list[Any] = []
    for msg in sorted(additions, key=lambda m: getattr(m, "occurred_at", None) or datetime.min):
        when = getattr(msg, "occurred_at", None)
        if newest is not None and when is not None:
            if (newest - when).total_seconds() > window_seconds:
                continue
        eid = _event_id(msg)
        if eid and eid in seen:
            continue
        if eid:
            seen.add(eid)
        kept.append(msg)
        if len(kept) >= max(1, int(max_add)):
            break
    if not kept:
        return list(selected)
    merged = list(selected) + kept
    merged.sort(key=lambda m: getattr(m, "occurred_at", None) or datetime.min)
    return merged
