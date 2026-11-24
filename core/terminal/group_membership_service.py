"""Group membership transition ingestion (join / leave / re-join).

WeChat group system notices are content-only messages: ``"A"邀请"B"加入了群聊``,
``"B"退出了群聊``, ``"A"将"B"移出了群聊``.  This service parses the notice,
resolves the *target* deterministically, and writes the epoch table used by
``IdentityService.is_member_at`` / command visibility.

Resolution rules are deliberately conservative:

* prefer the unique exact person matched by the parsed target name;
* only use the raw sender id as a fallback for a join notice (a leave notice's
  raw sender may be the operator, not the person who left);
* never guess between multiple people with the same nickname -- skip instead.
"""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable

import structlog

from core import temporal
from core.identity.platforms import identity_type_for, is_group_chat
from safelog import warn

log = structlog.get_logger()

_JOIN_MARKERS = (
    "加入了群聊",
    "加入群聊",
    "通过扫描",
    "分享的二维码加入",
)
_LEAVE_MARKERS = (
    "退出了群聊",
    "退出群聊",
    "移出了群聊",
    "移出群聊",
    "被移出",
    "离开群聊",
    "离开了群聊",
)

_QUOTE_MAP = str.maketrans({
    "“": '"',
    "”": '"',
    "「": '"',
    "」": '"',
    "『": '"',
    "』": '"',
    "‘": '"',
    "’": '"',
})

_QUOTED_RE = re.compile(r'"([^"]+)"')


@dataclass(frozen=True)
class MembershipTransition:
    action: str  # join / leave
    target_name: str
    source: str = "membership_notice"
    raw_excerpt: str = ""


def _normalize(content: str) -> str:
    return str(content or "").translate(_QUOTE_MAP).strip()


def _last_quoted(content: str) -> str:
    matches = _QUOTED_RE.findall(content)
    return matches[-1].strip() if matches else ""


def parse_membership_notice(content: str) -> MembershipTransition | None:
    text = _normalize(content)
    if not text:
        return None

    # The terminal's own account joining through someone else's invite is not
    # a member transition for the owner's people graph.
    if "邀请你加入了群聊" in text or "你邀请你加入了群聊" in text:
        return None

    action = ""
    if any(marker in text for marker in _LEAVE_MARKERS):
        action = "leave"
    elif any(marker in text for marker in _JOIN_MARKERS):
        action = "join"
    if not action:
        return None

    target = ""
    if action == "join":
        for pattern in (
            r"邀请\"([^\"]+)\"加入了?群聊",
            r"\"([^\"]+)\"通过扫码加入",
            r"\"([^\"]+)\"通过.*?加入",
            r"\"([^\"]+)\"加入了?群聊",
            r"\"([^\"]+)\"加入",
        ):
            match = re.search(pattern, text)
            if match:
                target = match.group(1).strip()
                break
    else:
        for pattern in (
            r"将\"([^\"]+)\"移出了?群聊",
            r"\"([^\"]+)\"被.*?移出",
            r"\"([^\"]+)\"退出了?群聊",
            r"\"([^\"]+)\"离开了?群聊",
            r"\"([^\"]+)\"移出了?群聊",
        ):
            match = re.search(pattern, text)
            if match:
                target = match.group(1).strip()
                break
    if not target:
        target = _last_quoted(text)
    if not target or target in {"你", "我", "自己", "有人"}:
        return None
    return MembershipTransition(
        action=action,
        target_name=target,
        raw_excerpt=text[:200],
    )


class GroupMembershipService:
    """Apply parsed membership notices to ``group_membership_periods``."""

    def __init__(self, meta_repo: Any):
        self.meta_repo = meta_repo

    # ------------------------------------------------------------------ apply
    def apply_messages(
        self,
        messages: Iterable[dict],
        *,
        platform: str,
        account_id: str,
    ) -> dict[str, int]:
        stats = {"scanned": 0, "joined": 0, "left": 0, "unresolved": 0}
        for message in messages:
            if not isinstance(message, dict):
                continue
            if str(message.get("chat_type") or "") != "group":
                continue
            chat_id = str(message.get("chat_id") or "").strip()
            if not chat_id:
                continue
            content = message.get("content") or ""
            if isinstance(content, bytes):
                content = content.decode("utf-8", errors="replace")
            type_name = str(message.get("msg_type_name") or "").lower()
            looks_like_notice = (
                type_name in {"system", "group_notice", "group_system"}
                or any(marker in str(content) for marker in _JOIN_MARKERS)
                or any(marker in str(content) for marker in _LEAVE_MARKERS)
            )
            if not looks_like_notice:
                continue
            transition = parse_membership_notice(str(content))
            if transition is None:
                continue
            stats["scanned"] += 1
            try:
                person_id = self._resolve_target(
                    transition,
                    message=message,
                    platform=platform,
                    account_id=account_id,
                    chat_id=chat_id,
                )
            except Exception:
                warn("core/terminal/group_membership_service.py:179 Exception", err=repr(__import__("sys").exc_info()[1]))
                person_id = ""
            if not person_id:
                stats["unresolved"] += 1
                continue
            occurred_at = (
                temporal.parse_datetime(message.get("created_at"))
                or datetime.now()
            )
            try:
                if transition.action == "join":
                    self._open_period(
                        person_id,
                        transition,
                        platform=platform,
                        account_id=account_id,
                        chat_id=chat_id,
                        occurred_at=occurred_at,
                    )
                    stats["joined"] += 1
                else:
                    if self._close_period(
                        person_id,
                        transition,
                        platform=platform,
                        account_id=account_id,
                        chat_id=chat_id,
                        occurred_at=occurred_at,
                    ):
                        stats["left"] += 1
                    else:
                        stats["unresolved"] += 1
            except Exception as exc:  # noqa: BLE001 - ingestion must not break
                log.warning(
                    "membership_period_write_failed",
                    platform=platform,
                    account_id=account_id,
                    chat_id=chat_id,
                    error=str(exc)[:160],
                )
                stats["unresolved"] += 1
        return stats

    # -------------------------------------------------------------- resolve
    def _resolve_target(
        self,
        transition: MembershipTransition,
        *,
        message: dict,
        platform: str,
        account_id: str,
        chat_id: str,
    ) -> str:
        people = self.meta_repo.people
        name = transition.target_name
        if name:
            try:
                matches = list(people.find_persons(
                    name,
                    platform=platform,
                    account_id=account_id,
                    limit=8,
                ))
            except Exception:
                warn("core/terminal/group_membership_service.py:242 Exception", err=repr(__import__("sys").exc_info()[1]))
                matches = []
            exact = [
                person for person in matches
                if str(getattr(person, "display_name", "") or "").strip() == name
            ]
            if len(exact) == 1:
                return str(exact[0].person_id)
            if len(matches) == 1:
                return str(matches[0].person_id)
        if transition.action != "join":
            # A leave notice's raw sender may be the admin who kicked them.
            return ""
        raw = str(message.get("sender_id") or "").strip()
        if not raw or raw in {"unknown", account_id} or is_group_chat(platform, chat_id=raw):
            return ""
        try:
            person = people.resolve_identity(
                platform=platform,
                account_id=account_id,
                identity_type=identity_type_for(platform, ""),
                identity_value=raw,
                chat_id=chat_id,
                display_name=name,
                confidence=0.7,
                source="membership_notice",
            )
        except Exception:
            warn("core/terminal/group_membership_service.py:269 Exception", err=repr(__import__("sys").exc_info()[1]))
            return ""
        return str(getattr(person, "person_id", "") or "")

    # --------------------------------------------------------------- writes
    def _open_period(
        self,
        person_id: str,
        transition: MembershipTransition,
        *,
        platform: str,
        account_id: str,
        chat_id: str,
        occurred_at: datetime,
    ) -> None:
        people = self.meta_repo.people
        people.upsert_membership(
            person_id,
            platform=platform,
            account_id=account_id,
            chat_id=chat_id,
            group_nickname=transition.target_name,
        )
        people.open_membership_period(
            person_id,
            platform=platform,
            account_id=account_id,
            chat_id=chat_id,
            joined_at=occurred_at,
            source=transition.source,
        )

    def _close_period(
        self,
        person_id: str,
        transition: MembershipTransition,
        *,
        platform: str,
        account_id: str,
        chat_id: str,
        occurred_at: datetime,
    ) -> bool:
        people = self.meta_repo.people
        closed = people.close_membership_period(
            person_id,
            platform=platform,
            account_id=account_id,
            chat_id=chat_id,
            left_at=occurred_at,
            source=transition.source,
        )
        if closed:
            try:
                people.upsert_membership(
                    person_id,
                    platform=platform,
                    account_id=account_id,
                    chat_id=chat_id,
                    group_nickname=transition.target_name,
                )
            except Exception:
                warn("core/terminal/group_membership_service.py:329 Exception", err=repr(__import__("sys").exc_info()[1]))
        return closed



# ---------------------------------------------------------------------------
# Step 46（批D 归属改造）：在场证据 v2 的 canonical 群成员名单读口。
# 唯一来源 = group_memberships（昵称）+ person_identities（wxid）；TTL 缓存。
# 纯读、异常一律空元组（没有名单不等于不在群，消费方按没有证据动作）。
# ---------------------------------------------------------------------------
_MEMBER_CACHE: dict[str, tuple[float, tuple[str, ...]]] = {}
_MEMBER_CACHE_LOCK = threading.Lock()
MEMBER_CACHE_TTL_SECONDS = 300.0
MEMBER_CACHE_MAX = 64


def _load_member_keys(chat_id: str, *, meta_repo: Any = None) -> tuple[str, ...]:
    try:
        from sqlalchemy import text as _sa_text

        from core.terminal.store_port import get_meta_repo

        repo = meta_repo if meta_repo is not None else get_meta_repo()
        with repo.engine.connect() as conn:
            rows = conn.execute(
                _sa_text(
                    "select gm.group_nickname, pi.identity_value "
                    "from group_memberships gm "
                    "left join person_identities pi "
                    "  on pi.person_id = gm.person_id "
                    " and pi.platform = gm.platform "
                    " and pi.identity_type = 'wxid' "
                    "where gm.chat_id = :chat"
                ),
                {"chat": chat_id},
            ).fetchall()
    except Exception:  # noqa: BLE001 - 读不到名单 = 没有这份证据
        warn("core/terminal/group_membership_service.py:366 Exception", err=repr(__import__("sys").exc_info()[1]))
        return ()
    out: list[str] = []
    for nickname, wxid in rows:
        for value in (nickname, wxid):
            text = str(value or "").strip()
            if text and text not in out:
                out.append(text)
    return tuple(out)


def known_member_keys(
    chat_id: str,
    *,
    ttl: float = MEMBER_CACHE_TTL_SECONDS,
    meta_repo: Any = None,
) -> tuple[str, ...]:
    """canonical 群成员名单（昵称 + wxid），带 TTL 缓存；空 chat_id 返回空元组。"""
    chat = str(chat_id or "").strip()
    if not chat:
        return ()
    now = time.monotonic()
    with _MEMBER_CACHE_LOCK:
        hit = _MEMBER_CACHE.get(chat)
        if hit is not None and ttl >= 0 and (now - hit[0]) <= ttl:
            return hit[1]
    keys = _load_member_keys(chat, meta_repo=meta_repo)
    with _MEMBER_CACHE_LOCK:
        _MEMBER_CACHE[chat] = (now, keys)
        if len(_MEMBER_CACHE) > MEMBER_CACHE_MAX:
            oldest = sorted(_MEMBER_CACHE.items(), key=lambda item: item[1][0])[
                : len(_MEMBER_CACHE) - MEMBER_CACHE_MAX
            ]
            for key, _value in oldest:
                _MEMBER_CACHE.pop(key, None)
    return keys


def clear_member_cache() -> None:
    """测试/重启用：清空名单缓存。"""
    with _MEMBER_CACHE_LOCK:
        _MEMBER_CACHE.clear()
