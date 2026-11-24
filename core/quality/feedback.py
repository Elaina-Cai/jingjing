"""Layer-2 quality feedback: collect real-usage negative signals.

This layer never blocks and never changes replies.  It watches incoming user
messages for two deterministic patterns and stores them as eval samples:

* ``correction`` -- the user says the assistant was wrong / made something up
  ("不对", "你记错了", "我没说过这个");
* ``repeat`` -- the user asks (almost) the same question again, which usually
  means the previous answer failed.

Layer-3 (LLM judge) will read these samples later.  Collection is best-effort:
every entry point swallows its own errors so a broken signal can never break a
reply.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Iterable
from safelog import warn

CORRECTION_MARKERS = (
    "不对",
    "不是这个",
    "不是这样",
    "不是的",
    "错了",
    "你记错",
    "记错了",
    "我没说过",
    "我没说",
    "胡说",
    "瞎说",
    "搞错",
    "答错",
    "不是我说",
    "记错了吧",
)

REPEAT_MARKERS = (
    "再说一遍",
    "再说一次",
    "刚才说什么",
    "你刚说",
    "重说",
    "没听清",
    "听不清",
)

# 2026-09-22：主人确认群名单（引用她的名单原话 + 一句确认）
ROSTER_QUOTE_MARKERS = ("成员清单", "唯一准版")
ROSTER_CONFIRM_MARKERS = (
    "现在对了", "就这版", "就是这个版本", "最准确", "唯一准版",
    "以此为准", "落库", "照这个记", "定了",
)

_PUNCT = re.compile(r"[\s，。！？!?；;、,.…~～\-—_]+")


def _norm(text: str) -> str:
    return _PUNCT.sub("", str(text or "").lower())


def _ngrams(text: str, size: int = 2) -> set[str]:
    if len(text) < size:
        return {text} if text else set()
    return {text[i:i + size] for i in range(len(text) - size + 1)}


def classify_correction(text: str) -> bool:
    """Deterministic 'that was wrong' detector; tuned to avoid questions."""
    raw = str(text or "").strip()
    if not raw or len(raw) > 80:
        return False
    if raw.endswith(("吗", "吗？", "吗?", "？", "?")):
        return False
    return any(marker in raw for marker in CORRECTION_MARKERS)


def is_repeat(previous_user_text: str, new_text: str) -> bool:
    """Same / near-same question asked again."""
    raw = str(new_text or "").strip()
    if any(marker in raw for marker in REPEAT_MARKERS):
        return True
    prev = _norm(previous_user_text)
    cur = _norm(raw)
    if not prev or not cur:
        return False
    if cur == prev and len(cur) >= 5:
        return True
    if min(len(cur), len(prev)) < 8:
        return False
    a, b = _ngrams(cur), _ngrams(prev)
    if not a or not b:
        return False
    dice = 2 * len(a & b) / (len(a) + len(b))
    return dice >= 0.85


def _is_roster_confirm(text: str, quote_content: str = "") -> bool:
    """主人引用名单并确认（"现在对了/就这版"）-> roster_confirm 信号。"""
    raw = str(text or "").strip()
    quote = str(quote_content or "").strip()
    if not raw or not quote:
        return False
    if not any(marker in quote for marker in ROSTER_QUOTE_MARKERS):
        return False
    if raw.endswith(("吗", "吗？", "吗?", "？", "?")):
        return False
    return any(marker in raw for marker in ROSTER_CONFIRM_MARKERS)


def detect_signals(
    previous_user_text: str, new_text: str, *, quote_content: str = ""
) -> list[str]:
    signals: list[str] = []
    if _is_roster_confirm(new_text, quote_content):
        signals.append("roster_confirm")
    if classify_correction(new_text):
        signals.append("correction")
    if is_repeat(previous_user_text, new_text):
        signals.append("repeat")
    return signals


def _field(obj: Any, name: str, default: Any = "") -> Any:
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _latest_inbound_per_chat(messages: Iterable[Any]) -> list[dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for message in messages or ():
        chat_id = str(_field(message, "chat_id", "") or "")
        if not chat_id:
            continue
        if bool(_field(message, "is_self", 0)):
            continue
        content = str(_field(message, "content", "") or "").strip()
        if not content:
            continue
        latest[chat_id] = {
            "chat_id": chat_id,
            "chat_type": str(_field(message, "chat_type", "") or ""),
            "msg_id": str(_field(message, "msg_id", "") or ""),
            "sender_id": str(
                _field(message, "sender_id", "")
                or _field(message, "wxid", "")
                or ""
            ),
            "content": content,
            # Quote-reply is the precise correction channel: it names the exact
            # reply being corrected, so the learning worker can resolve the
            # memories that were in play instead of guessing by similarity.
            "quote_reply_id": str(
                _field(message, "quote_reply_id", "")
                or _field(message, "reply_to", "")
                or ""
            ),
            "quote_content": str(_field(message, "quote_reply_content", "") or ""),
        }
    return list(latest.values())


def _find_reply_event_id(*, chat_id: str, platform: str = "", account_id: str = "") -> str:
    """Best-effort provenance link to the REPLY event being questioned."""
    try:
        from core.terminal.event_model import EventType
        from core.terminal.event_runtime import default_event_store

        store = default_event_store()
        if store is None:
            return ""
        if not platform or not account_id or not chat_id:
            return ""
        rows = store.query(
            platform=platform, bot_account_id=account_id,
            chat_id=chat_id,
            event_types=(EventType.REPLY,),
            limit=5,
        )
        if not rows:
            return ""
        return str(getattr(rows[-1], "event_id", "") or "")
    except Exception:
        warn("core/quality/feedback.py:183 Exception", err=repr(__import__("sys").exc_info()[1]))
        return ""


def _scan_one(
    candidate: dict[str, Any],
    *,
    repo: Any,
    feedback: Any,
    platform: str = "",
    account_id: str = "",
) -> int:
    chat_id = candidate["chat_id"]
    new_text = candidate["content"]
    try:
        history = list(repo.messages.get_by_chat(chat_id, limit=12))
    except Exception:
        warn("core/quality/feedback.py:199 Exception", err=repr(__import__("sys").exc_info()[1]))
        history = []
    candidate_msg_id = candidate.get("msg_id", "")
    rows = [
        row
        for row in history
        if str(_field(row, "msg_id", "") or "") != candidate_msg_id
    ]

    previous_user_text = ""
    previous_reply = None
    for row in reversed(rows):
        content = str(_field(row, "content", "") or "").strip()
        if not content:
            continue
        if bool(_field(row, "is_self", 0)):
            if previous_reply is None:
                previous_reply = row
            continue
        previous_user_text = content
        break

    quote_content = str(candidate.get("quote_content") or "").strip()
    signals = detect_signals(
        previous_user_text, new_text, quote_content=quote_content
    )
    if not signals:
        return 0
    if feedback.recent_duplicate(chat_id=chat_id, sample_text=new_text, platform=platform, account_id=account_id):
        return 0
    reply_preview = quote_content[:300] if quote_content else (
        str(_field(previous_reply, "content", "") or "")[:300]
        if previous_reply is not None
        else ""
    )
    feedback.add(
        source=signals[0],
        platform=platform,
        bot_account_id=account_id,
        chat_id=chat_id,
        chat_type=candidate.get("chat_type", ""),
        person_id=candidate.get("sender_id", ""),
        sample_text=new_text,
        previous_user_text=previous_user_text,
        reply_event_id=_find_reply_event_id(
            chat_id=chat_id, platform=platform, account_id=account_id
        ),
        reply_preview=reply_preview,
        metadata={
            "signals": signals,
            "quote_reply_id": str(candidate.get("quote_reply_id") or ""),
            "quote_content": quote_content[:500],
        },
        created_at=datetime.now(),
    )
    return 1


def record_feedback_signals(
    messages: Iterable[Any],
    *,
    repo: Any,
    platform: str = "",
    account_id: str = "",
    meta_repo: Any = None,
) -> int:
    """Best-effort collection for one inbound batch; returns samples recorded."""
    try:
        candidates = _latest_inbound_per_chat(messages)
    except Exception:
        warn("core/quality/feedback.py:268 Exception", err=repr(__import__("sys").exc_info()[1]))
        return 0
    # No account repo means no history to compare against: skip instead of
    # guessing (also keeps test drivers from touching the live feedback table).
    if not candidates or repo is None:
        return 0
    if meta_repo is None:
        try:
            from core.terminal.store_port import get_meta_repo

            meta_repo = get_meta_repo()
        except Exception:
            warn("core/quality/feedback.py:279 Exception", err=repr(__import__("sys").exc_info()[1]))
            return 0
    feedback = getattr(meta_repo, "feedback_samples", None)
    if feedback is None:
        return 0
    recorded = 0
    for candidate in candidates:
        try:
            recorded += _scan_one(
                candidate,
                repo=repo,
                feedback=feedback,
                platform=platform,
                account_id=account_id,
            )
        except Exception:
            warn("core/quality/feedback.py:294 Exception", err=repr(__import__("sys").exc_info()[1]))
            continue
    return recorded


__all__ = [
    "classify_correction",
    "detect_signals",
    "is_repeat",
    "record_feedback_signals",
]
