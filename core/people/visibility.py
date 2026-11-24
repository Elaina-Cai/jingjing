"""Pure visibility and ranking rules for person observations.

The important rule is directional:

* a group observation about person P may be used in P's private chat;
* P's private observation must never be used in a group chat;
* a private observation from person Q must never leak to person P, even if P is
  mentioned inside Q's private message.

Owner override is intentionally a parameter, not an environment lookup: the
caller must resolve the owner id (for example a wxid) first.  Names are never
authority.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, Sequence

from core import temporal
from core.contracts.events import ChatType
from core.contracts.person_memory import (
    ObservationStatus,
    ObservationVisibility,
    PersonObservation,
)
from safelog import warn

_GROUP_LIKE = (ChatType.GROUP, ChatType.CHANNEL)


def profile_allowed(profile, request) -> bool:
    """摘要同样需要来源范围；全局聚合摘要只允许主人私聊读取。"""
    from core.contracts.person_memory import ProfileScopeType
    if profile is None or not all((request.platform, request.account_id, request.chat_id)):
        return False
    if (profile.person_id, profile.platform, profile.account_id) != (
        request.target_person_id, request.platform, request.account_id):
        return False
    if profile.scope_type == ProfileScopeType.GLOBAL:
        return bool(request.viewer_is_owner and request.chat_type == ChatType.PRIVATE)
    expected = ProfileScopeType.GROUP if request.chat_type in _GROUP_LIKE else ProfileScopeType.PRIVATE
    return profile.scope_type == expected and profile.scope_id == request.chat_id



def _same_tenant(observation: PersonObservation, platform: str, account_id: str) -> bool:
    return (
        observation.platform == str(platform or "")
        and observation.account_id == str(account_id or "")
    )


def _is_group_like(chat_type: ChatType | str) -> bool:
    value = chat_type.value if isinstance(chat_type, ChatType) else str(chat_type or "")
    return value in {ChatType.GROUP.value, ChatType.CHANNEL.value}


# --------------------------------------------------------------------------
# 宪法级不变量（docs/需求契约.md §三 R-I1 / R-I2 / R-I3）——**不可配置**：
#
#   R-I1  查记忆只允许「该动作发起人 + 当前会话」：不得跨群、不得查他人私聊。
#   R-I2  高于所有可配置规则、**不可修改、不展示**（连它的存在都不进 UI）。
#   R-I3  须在**数据层**强制（行级过滤），不是靠提示词自觉。
#
# * 没有环境变量、feature flag、规则表字段或管理面开关能放宽它；
# * 没有"调用方自己过滤"的备用分支：数据层拿不到合法 scope 就**什么都不返回**；
# * 判定口径只在本模块一处：`observation_visible_to` 是唯一 owner，
#   `VisibleScope` 只是把"当前会话 + 发起人"捆成一个值，
#   `db/repo_person_memory.list_for_person` 把同一组条件翻成 SQL 行级过滤，
#   逐条等价性由 `tests/test_memory_scope.py` 的 parity 用例锁住。
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class VisibleScope:
    """The conversation a recall request is made from (R-I1).

    ``requester_person_id`` is the person who caused the action (the speaker of
    the current turn, or the owner driving the console); ``chat_id`` is the
    current conversation.  Both are required: a read without them has no scope
    and must be refused instead of silently falling back to "everything about
    this person".
    """

    platform: str
    account_id: str
    requester_person_id: str
    chat_type: ChatType | str = ChatType.PRIVATE
    chat_id: str = ""
    viewer_is_owner: bool = False

    def __post_init__(self) -> None:
        chat_type = self.chat_type
        if not isinstance(chat_type, ChatType):
            try:
                chat_type = ChatType(str(chat_type or "").strip().lower())
            except ValueError:
                warn("core/people/visibility.py:83 ValueError", err=repr(__import__("sys").exc_info()[1]))
                chat_type = ChatType.UNKNOWN
        object.__setattr__(self, "chat_type", chat_type)
        for name in ("platform", "account_id", "requester_person_id", "chat_id"):
            object.__setattr__(self, name, str(getattr(self, name) or "").strip())
        object.__setattr__(self, "viewer_is_owner", bool(self.viewer_is_owner))

    @property
    def is_group_like(self) -> bool:
        return _is_group_like(self.chat_type)

    def effective_chat_id(self) -> str:
        """Conversation key; a private chat with no explicit id *is* the requester."""
        if self.chat_id:
            return self.chat_id
        if self.chat_type == ChatType.PRIVATE:
            return self.requester_person_id
        return ""

    def problem(self) -> str:
        """Why this scope authorises nothing (``""`` = usable).

        Used by every read path as the fail-closed test; the reasons are
        deliberately machine-readable so logs can be counted.
        """
        if not self.platform:
            return "missing_platform"
        if not self.account_id:
            return "missing_account_id"
        if not self.requester_person_id:
            return "missing_requester_person_id"
        if self.chat_type not in (ChatType.PRIVATE, *_GROUP_LIKE):
            return "unknown_chat_type"
        if self.is_group_like and not self.chat_id:
            return "missing_group_chat_id"
        return ""


def observation_in_scope(observation: PersonObservation, scope: VisibleScope | None) -> bool:
    """``observation_visible_to`` expressed over a :class:`VisibleScope`.

    A ``None`` or unusable scope is always ``False`` (fail closed).
    """
    if scope is None:
        return False
    if scope.problem():
        return False
    return observation_visible_to(
        observation,
        platform=scope.platform,
        account_id=scope.account_id,
        chat_type=scope.chat_type,
        chat_id=scope.effective_chat_id(),
        viewer_person_id=scope.requester_person_id,
        viewer_is_owner=scope.viewer_is_owner,
    )


def observation_visible_to(
    observation: PersonObservation,
    *,
    platform: str,
    account_id: str,
    chat_type: ChatType | str,
    chat_id: str,
    viewer_person_id: str = "",
    viewer_is_owner: bool = False,
    owner_cross_account: bool = False,
) -> bool:
    """Return whether one observation may be injected into this context."""

    # 作废的观察对谁都不可见（包括 owner）。2026-09-22 清理垃圾观察时发现：
    # owner 分支原先在 status 检查之前直接放行，redacted/superseded 仍会进画像。
    if observation.status != ObservationStatus.ACTIVE:
        return False

    if viewer_is_owner:
        if _same_tenant(observation, platform, account_id):
            return True
        return bool(owner_cross_account)

    if str(observation.sensitivity or "normal").strip().lower() not in ("", "normal"):
        return False
    if not _same_tenant(observation, platform, account_id):
        return False
    if observation.visibility in (
        ObservationVisibility.OWNER_ONLY,
        ObservationVisibility.SENSITIVE,
    ):
        return False

    current_chat_id = str(chat_id or "")
    current_person_id = str(viewer_person_id or "")

    if _is_group_like(chat_type):
        return (
            observation.is_group_scope()
            and observation.occurred_scope_id == current_chat_id
            and observation.visibility != ObservationVisibility.PRIVATE_ONLY
        )

    if chat_type == ChatType.PRIVATE:
        # Own private history is always in scope for the current private chat.
        if (
            observation.occurred_scope_type == ChatType.PRIVATE
            and observation.occurred_scope_id == current_chat_id
        ):
            return True
        # One-way group -> subject-private transfer.
        if (
            observation.is_group_scope()
            and observation.visibility == ObservationVisibility.GROUP_SHARED
            and current_person_id
            and current_person_id in observation.related_person_ids
        ):
            return True
        return False

    return False


def filter_visible_observations(
    observations: Iterable[PersonObservation],
    *,
    platform: str,
    account_id: str,
    chat_type: ChatType | str,
    chat_id: str,
    viewer_person_id: str = "",
    viewer_is_owner: bool = False,
    owner_cross_account: bool = False,
) -> list[PersonObservation]:
    return [
        observation
        for observation in observations
        if observation_visible_to(
            observation,
            platform=platform,
            account_id=account_id,
            chat_type=chat_type,
            chat_id=chat_id,
            viewer_person_id=viewer_person_id,
            viewer_is_owner=viewer_is_owner,
            owner_cross_account=owner_cross_account,
        )
    ]


def _utcnow() -> datetime:
    return datetime.now()


def _recency_score(observed_at: datetime | None, now: datetime | None = None) -> float:
    if observed_at is None:
        return 0.4
    current = now or _utcnow()
    if observed_at.tzinfo is not None:
        observed_at = temporal.to_naive_local(observed_at)
    age_days = max((current - observed_at).total_seconds() / 86400.0, 0.0)
    return 1.0 / (1.0 + age_days / 30.0)


def _relevance_score(text: str, query: str) -> float:
    text = str(text or "").lower().strip()
    query = str(query or "").lower().strip()
    if not text or not query:
        return 0.0
    query_tokens = {token for token in query.replace("\n", " ").split(" ") if token}
    if not query_tokens:
        return 0.0
    text_tokens = {token for token in text.replace("\n", " ").split(" ") if token}
    if not text_tokens:
        return 0.0
    intersect = len(query_tokens & text_tokens)
    return intersect / max(len(query_tokens), 1)


def rank_observations(
    observations: Sequence[PersonObservation],
    *,
    query: str = "",
    current_scope_type: ChatType | str | None = None,
    current_scope_id: str = "",
    now: datetime | None = None,
) -> list[PersonObservation]:
    """Order observations by usefulness for the current turn.

    This is deliberately lexical-only and dependency-free.  An embedding-based
    scorer can replace it later without touching the repositories.
    """

    def score(observation: PersonObservation) -> float:
        base = float(observation.confidence or 0.0) * 0.45
        base += float(observation.importance or 0.0) * 0.20
        base += _recency_score(observation.observed_at, now=now) * 0.15
        base += _relevance_score(
            f"{observation.content} {observation.normalized_key}",
            query,
        ) * 0.20
        if current_scope_type and observation.occurred_scope_type == current_scope_type:
            if observation.occurred_scope_id == str(current_scope_id or ""):
                base += 0.10
        if observation.kind.value in ("plan", "event", "preference"):
            base += 0.03
        return base

    return sorted(observations, key=score, reverse=True)
