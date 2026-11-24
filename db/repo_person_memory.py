"""SQLAlchemy adapter for the platform-neutral person memory ports.

Only this module knows about tables.  ``core.people`` receives plain domain
objects and can be tested with in-memory fakes or reused for QQ/other
platforms without modification.

R-I3（docs/需求契约.md §三）要求记忆范围在**数据层**强制，所以本模块的召回读口
``list_for_person`` 自己带行级过滤：拿不到合法 ``VisibleScope`` 就**什么都不返回**
（fail closed），不把"相关"实现成"链到这个人"。
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any, Sequence

import structlog
from sqlalchemy import and_, delete, func, or_, select

from core.contracts.events import ChatType
from core.contracts.person_memory import (
    ObservationKind,
    ObservationStatus,
    ObservationVisibility,
    PersonObservation,
    PersonProfileSnapshot,
    ProfileScopeType,
)
from core.people.visibility import VisibleScope
from db.meta_models import (
    PersonObservation as PersonObservationRow,
    PersonObservationLink,
    PersonProfileSnapshot as PersonProfileSnapshotRow,
)
from db.repo_base import BaseRepository
from safelog import warn

log = structlog.get_logger()


def _clean(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _json_loads(value: str | None, fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except Exception:
        warn("db/repo_person_memory.py:53 Exception", err=repr(__import__("sys").exc_info()[1]))
        return fallback


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _observation_links(observation: PersonObservation) -> list[tuple[str, str]]:
    links: list[tuple[str, str]] = []

    def add(person_id: str, relation: str) -> None:
        person_id = _clean(person_id)
        if person_id and (person_id, relation) not in links:
            links.append((person_id, relation))

    add(observation.person_id, "subject")
    add(observation.speaker_person_id, "speaker")
    for person_id in observation.mentioned_person_ids:
        add(person_id, "mentioned")
    return links


def _scope_problem(scope: Any) -> str:
    """Reject anything that is not a usable :class:`VisibleScope` (fail closed)."""
    if not isinstance(scope, VisibleScope):
        return "invalid_scope_type"
    try:
        return scope.problem()
    except Exception:  # noqa: BLE001 - 坏 scope 只能变成"拒绝"，不能变成"放行"
        warn("db/repo_person_memory.py:82 Exception", err=repr(__import__("sys").exc_info()[1]))
        return "invalid_scope"


def _scope_row_condition(scope: VisibleScope) -> Any:
    """Row-level R-I1 filter; mirror of ``visibility.observation_visible_to``.

    The requester link is read from ``person_observation_links`` (subject /
    speaker / mentioned), which is exactly the set of
    ``PersonObservation.related_person_ids`` the pure rule uses.
    """
    requester = _clean(scope.requester_person_id)
    requester_links = select(PersonObservationLink.observation_id).where(
        PersonObservationLink.person_id == requester
    )
    if scope.is_group_like:
        return and_(
            PersonObservationRow.occurred_scope_type.in_(
                (ChatType.GROUP.value, ChatType.CHANNEL.value)
            ),
            PersonObservationRow.occurred_scope_id == scope.chat_id,
            # 宽松写法（!= private_only）在枚举值畸形时会放行；白名单写法对畸形
            # 值取"拒绝"（fail-closed），对正常写入口径与纯函数逐条等价。
            PersonObservationRow.visibility
            == ObservationVisibility.GROUP_SHARED.value,
        )
    chat_id = _clean(scope.effective_chat_id())
    own_private_chat = and_(
        PersonObservationRow.occurred_scope_type == ChatType.PRIVATE.value,
        PersonObservationRow.occurred_scope_id == chat_id,
    )
    # 群 → 当事人私聊的单向投递：只有与该发起人相关的 group_shared 群观察。
    group_transfer = and_(
        PersonObservationRow.occurred_scope_type.in_(
            (ChatType.GROUP.value, ChatType.CHANNEL.value)
        ),
        PersonObservationRow.visibility == ObservationVisibility.GROUP_SHARED.value,
        PersonObservationRow.observation_id.in_(requester_links),
    )
    return or_(own_private_chat, group_transfer)


def _log_scope_denied(*, person_id: str, scope: Any, reason: str) -> None:
    """One warning per refused read; never raises (a log must not open a hole)."""
    try:
        log.warning(
            "person_memory_scope_denied",
            person_id=person_id,
            reason=reason,
            platform=str(getattr(scope, "platform", "") or ""),
            account_id=str(getattr(scope, "account_id", "") or ""),
            chat_type=str(
                getattr(getattr(scope, "chat_type", ""), "value", "")
                or getattr(scope, "chat_type", "")
                or ""
            ),
            chat_id=str(getattr(scope, "chat_id", "") or ""),
            requester_person_id=str(
                getattr(scope, "requester_person_id", "") or ""
            ),
            invariant="R-I1/R-I3",
        )
    except Exception:  # noqa: BLE001
        warn("db/repo_person_memory.py:144 Exception", err=repr(__import__("sys").exc_info()[1]))


class PersonMemoryRepository(BaseRepository):
    """Repository implementing ObservationRepository + ProfileRepository."""

    # --------------------------------------------------------------- observations
    def save_many(self, observations: Sequence[PersonObservation]) -> int:
        written = 0
        with self._session() as session:
            for observation in observations:
                observation_id = _clean(observation.observation_id)
                if not observation_id:
                    observation_id = uuid.uuid4().hex
                row = session.execute(
                    select(PersonObservationRow).where(
                        PersonObservationRow.observation_id == observation_id
                    ).limit(1)
                ).scalar_one_or_none()
                if row is None:
                    row = PersonObservationRow(observation_id=observation_id)
                    session.add(row)
                else:
                    session.execute(
                        delete(PersonObservationLink).where(
                            PersonObservationLink.observation_id == observation_id
                        )
                    )
                self._apply_observation(row, observation)
                for person_id, relation in _observation_links(observation):
                    session.add(PersonObservationLink(
                        observation_id=observation_id,
                        person_id=person_id,
                        relation=relation,
                    ))
                written += 1
            session.commit()
        return written

    # ----------------------------------------------------------------- 召回读口
    def list_for_person(
        self,
        person_id: str,
        *,
        scope: VisibleScope | None = None,
        since: datetime | None = None,
        limit: int = 200,
        include_inactive: bool = False,
    ) -> list[PersonObservation]:
        """Return observations of one person **visible in the caller's scope**.

        宪法级不变量（docs/需求契约.md §三 R-I1/R-I2/R-I3）——**不可配置、不在 UI
        出现、不被任何规则覆盖**：查记忆只允许「该动作发起人 + 当前会话」。

        ``scope`` 是**必需**参数：缺它、或它不合法（没有租户 / 没有发起人 / 群会话
        没有会话键）时本方法**拒绝读取**——返回空列表 + warning，绝不退回
        "按 person_id 全取"那条无过滤的老路。

        行级条件（与 ``core.people.visibility.observation_visible_to`` 逐条等价，
        等价性由 ``tests/test_memory_scope.py`` 的 parity 用例锁住）：

        * 租户：``platform`` + ``account_id`` 必须与 scope 一致；
        * 发起人是 owner：同租户内可见（既有 owner 口径，见 visibility.py）；
        * 群/频道会话：只返回**本群**发生的、且 ``visibility != private_only``
          的观察（别群的、别人私聊的一律排除）；
        * 私聊会话：只返回**这一间**私聊里发生的观察，或"该发起人相关"的
          ``group_shared`` 群观察（群 → 当事人私聊的单向投递）；
        * ``sensitivity`` 非 normal、``visibility`` 为 owner_only/sensitive 的
          观察对非 owner 一律排除；作废观察对谁都不可见。

        两处刻意"更严"（畸形枚举值 / NULL 一律拒绝，属于 fail-closed 方向，
        正常写入口径下与纯函数逐条等价）：``sensitivity`` 比较前 trim+lower、
        群分支要求 ``visibility`` 恰为 ``group_shared``。
        """
        person_id = _clean(person_id)
        reason = "missing_scope" if scope is None else _scope_problem(scope)
        if not person_id:
            reason = reason or "missing_person_id"
        if reason:
            _log_scope_denied(person_id=person_id, scope=scope, reason=reason)
            return []

        observation_ids = select(PersonObservationLink.observation_id).where(
            PersonObservationLink.person_id == person_id
        )
        query = select(PersonObservationRow).where(
            PersonObservationRow.observation_id.in_(observation_ids)
        )
        query = query.where(PersonObservationRow.platform == scope.platform)
        query = query.where(PersonObservationRow.account_id == scope.account_id)
        if not scope.viewer_is_owner:
            query = query.where(
                PersonObservationRow.visibility.notin_(
                    (
                        ObservationVisibility.OWNER_ONLY.value,
                        ObservationVisibility.SENSITIVE.value,
                    )
                )
            )
            query = query.where(
                func.lower(func.trim(PersonObservationRow.sensitivity)).in_(("", "normal"))
            )
            query = query.where(_scope_row_condition(scope))
        # 作废/被取代的观察对读路径不可见（2026-09-22 清垃圾时发现缺了这一层）
        if not include_inactive:
            query = query.where(
                PersonObservationRow.status == ObservationStatus.ACTIVE.value
            )
        if since is not None:
            query = query.where(PersonObservationRow.observed_at >= since)
        query = query.order_by(
            PersonObservationRow.observed_at.desc(),
            PersonObservationRow.id.desc(),
        ).limit(max(1, int(limit or 200)))
        with self._session() as session:
            rows = list(session.execute(query).scalars().all())
        return [self._to_domain_observation(row) for row in rows]

    def list_for_projection(
        self,
        person_id: str,
        *,
        platform: str = "",
        account_id: str = "",
        since: datetime | None = None,
        limit: int = 1000,
        include_inactive: bool = False,
    ) -> list[PersonObservation]:
        """Internal read for the profile aggregator — **not a recall API**.

        画像聚合要把一个人在所有会话里的观察折成画像快照，所以它按
        ``person + 租户`` 取行、不按会话裁。它**不是**面向会话/发起人的读口：
        任何"查记忆"都必须走 :meth:`list_for_person`（行级过滤只在那一条路上，
        缺 scope 即拒绝）。名字故意起得刺眼，好让 review 一眼看出这不是召回。
        """
        person_id = _clean(person_id)
        if not person_id:
            return []
        observation_ids = select(PersonObservationLink.observation_id).where(
            PersonObservationLink.person_id == person_id
        )
        query = select(PersonObservationRow).where(
            PersonObservationRow.observation_id.in_(observation_ids)
        )
        platform = _clean(platform)
        account_id = _clean(account_id)
        if platform:
            query = query.where(PersonObservationRow.platform == platform)
        if account_id:
            query = query.where(PersonObservationRow.account_id == account_id)
        if not include_inactive:
            query = query.where(
                PersonObservationRow.status == ObservationStatus.ACTIVE.value
            )
        if since is not None:
            query = query.where(PersonObservationRow.observed_at >= since)
        query = query.order_by(
            PersonObservationRow.observed_at.desc(),
            PersonObservationRow.id.desc(),
        ).limit(max(1, int(limit or 1000)))
        with self._session() as session:
            rows = list(session.execute(query).scalars().all())
        return [self._to_domain_observation(row) for row in rows]

    def list_all(
        self,
        *,
        platform: str = "",
        account_id: str = "",
        limit: int = 100000,
    ) -> list[PersonObservation]:
        query = select(PersonObservationRow)
        platform = _clean(platform)
        account_id = _clean(account_id)
        if platform:
            query = query.where(PersonObservationRow.platform == platform)
        if account_id:
            query = query.where(PersonObservationRow.account_id == account_id)
        query = query.order_by(PersonObservationRow.id.asc()).limit(max(1, int(limit or 100000)))
        with self._session() as session:
            rows = list(session.execute(query).scalars().all())
        return [self._to_domain_observation(row) for row in rows]

    @staticmethod
    def _apply_observation(row: PersonObservationRow, observation: PersonObservation) -> None:
        row.person_id = _clean(observation.person_id)
        row.platform = _clean(observation.platform)
        row.account_id = _clean(observation.account_id)
        row.occurred_scope_type = _clean(
            observation.occurred_scope_type.value
            if isinstance(observation.occurred_scope_type, ChatType)
            else observation.occurred_scope_type
        )
        row.occurred_scope_id = _clean(observation.occurred_scope_id)
        row.speaker_person_id = _clean(observation.speaker_person_id)
        row.mentioned_person_ids_json = _json_dumps(list(observation.mentioned_person_ids))
        row.source_evidence_id = _clean(observation.source_evidence_id)
        row.source_message_id = _clean(observation.source_message_id)
        row.observed_at = observation.observed_at
        row.kind = _clean(
            observation.kind.value if isinstance(observation.kind, ObservationKind) else observation.kind
        )
        row.content = observation.content
        row.raw_quote = observation.raw_quote
        row.normalized_key = _clean(observation.normalized_key)
        row.confidence = float(observation.confidence or 0.0)
        row.importance = float(observation.importance or 0.0)
        row.visibility = _clean(
            observation.visibility.value
            if isinstance(observation.visibility, ObservationVisibility)
            else observation.visibility
        )
        row.sensitivity = _clean(observation.sensitivity) or "normal"
        row.status = _clean(
            observation.status.value
            if isinstance(observation.status, ObservationStatus)
            else observation.status
        ) or ObservationStatus.ACTIVE.value
        row.expires_at = observation.expires_at
        row.model_version = _clean(observation.model_version)
        row.evidence_ids_json = _json_dumps(list(observation.evidence_ids))
        row.extra_json = _json_dumps(dict(observation.extra or {}))
        row.updated_at = datetime.now()

    @staticmethod
    def _to_domain_observation(row: PersonObservationRow) -> PersonObservation:
        try:
            scope_type = ChatType(row.occurred_scope_type or ChatType.UNKNOWN.value)
        except ValueError:
            warn("db/repo_person_memory.py:373 ValueError", err=repr(__import__("sys").exc_info()[1]))
            scope_type = ChatType.UNKNOWN
        try:
            kind = ObservationKind(row.kind or ObservationKind.UNKNOWN.value)
        except ValueError:
            warn("db/repo_person_memory.py:377 ValueError", err=repr(__import__("sys").exc_info()[1]))
            kind = ObservationKind.UNKNOWN
        try:
            visibility = ObservationVisibility(
                row.visibility or ObservationVisibility.GROUP_SHARED.value
            )
        except ValueError:
            warn("db/repo_person_memory.py:383 ValueError", err=repr(__import__("sys").exc_info()[1]))
            visibility = ObservationVisibility.PRIVATE_ONLY
        try:
            status = ObservationStatus(row.status or ObservationStatus.ACTIVE.value)
        except ValueError:
            warn("db/repo_person_memory.py:387 ValueError", err=repr(__import__("sys").exc_info()[1]))
            status = ObservationStatus.ACTIVE
        return PersonObservation(
            observation_id=row.observation_id,
            person_id=row.person_id,
            platform=row.platform or "",
            account_id=row.account_id or "",
            occurred_scope_type=scope_type,
            occurred_scope_id=row.occurred_scope_id or "",
            speaker_person_id=row.speaker_person_id or "",
            mentioned_person_ids=tuple(
                str(value) for value in _json_loads(row.mentioned_person_ids_json, [])
                if str(value).strip()
            ),
            source_evidence_id=row.source_evidence_id or "",
            source_message_id=row.source_message_id or "",
            observed_at=row.observed_at,
            kind=kind,
            content=row.content or "",
            raw_quote=row.raw_quote or "",
            normalized_key=row.normalized_key or "",
            confidence=float(row.confidence or 0.0),
            importance=float(row.importance or 0.0),
            visibility=visibility,
            sensitivity=row.sensitivity or "normal",
            status=status,
            expires_at=row.expires_at,
            model_version=row.model_version or "",
            evidence_ids=tuple(
                str(value) for value in _json_loads(row.evidence_ids_json, [])
                if str(value).strip()
            ),
            extra=_json_loads(row.extra_json, {}),
        )

    # ----------------------------------------------------------------- profiles
    def get_profile(
        self,
        person_id: str,
        *,
        platform: str,
        account_id: str,
        scope_type: ProfileScopeType,
        scope_id: str,
    ) -> PersonProfileSnapshot | None:
        query = select(PersonProfileSnapshotRow).where(
            PersonProfileSnapshotRow.person_id == _clean(person_id),
            PersonProfileSnapshotRow.platform == _clean(platform),
            PersonProfileSnapshotRow.account_id == _clean(account_id),
            PersonProfileSnapshotRow.scope_type == _clean(
                scope_type.value if isinstance(scope_type, ProfileScopeType) else scope_type
            ),
            PersonProfileSnapshotRow.scope_id == _clean(scope_id),
        ).limit(1)
        with self._session() as session:
            row = session.execute(query).scalar_one_or_none()
        return self._to_domain_profile(row) if row is not None else None

    def save_profile(self, profile: PersonProfileSnapshot) -> PersonProfileSnapshot:
        person_id = _clean(profile.person_id)
        platform = _clean(profile.platform)
        account_id = _clean(profile.account_id)
        scope_type = _clean(
            profile.scope_type.value
            if isinstance(profile.scope_type, ProfileScopeType)
            else profile.scope_type
        )
        scope_id = _clean(profile.scope_id)
        with self._session() as session:
            row = session.execute(
                select(PersonProfileSnapshotRow).where(
                    PersonProfileSnapshotRow.person_id == person_id,
                    PersonProfileSnapshotRow.platform == platform,
                    PersonProfileSnapshotRow.account_id == account_id,
                    PersonProfileSnapshotRow.scope_type == scope_type,
                    PersonProfileSnapshotRow.scope_id == scope_id,
                ).limit(1)
            ).scalar_one_or_none()
            if row is None:
                row = PersonProfileSnapshotRow(
                    profile_id=_clean(profile.profile_id) or uuid.uuid4().hex,
                    person_id=person_id,
                    platform=platform,
                    account_id=account_id,
                    scope_type=scope_type,
                    scope_id=scope_id,
                )
                session.add(row)
            row.profile_id = _clean(profile.profile_id) or row.profile_id
            row.summary = profile.summary
            row.style = profile.style
            row.topics_json = _json_dumps(list(profile.topics))
            row.active_hours_json = _json_dumps(list(profile.active_hours))
            row.emotion_tendency = profile.emotion_tendency
            row.relationship_tone = profile.relationship_tone
            row.group_map_json = _json_dumps(dict(profile.group_map or {}))
            row.confidence = float(profile.confidence or 0.0)
            row.observation_count = int(profile.observation_count or 0)
            row.version = max(1, int(profile.version or 1))
            row.updated_at = datetime.now()
            session.commit()
            session.refresh(row)
        return self._to_domain_profile(row)

    @staticmethod
    def _to_domain_profile(row: PersonProfileSnapshotRow) -> PersonProfileSnapshot:
        try:
            scope_type = ProfileScopeType(row.scope_type or ProfileScopeType.GLOBAL.value)
        except ValueError:
            warn("db/repo_person_memory.py:495 ValueError", err=repr(__import__("sys").exc_info()[1]))
            scope_type = ProfileScopeType.GLOBAL
        return PersonProfileSnapshot(
            profile_id=row.profile_id,
            person_id=row.person_id,
            platform=row.platform or "",
            account_id=row.account_id or "",
            scope_type=scope_type,
            scope_id=row.scope_id or "",
            summary=row.summary or "",
            style=row.style or "",
            topics=tuple(str(value) for value in _json_loads(row.topics_json, [])),
            active_hours=tuple(
                str(value) for value in _json_loads(row.active_hours_json, [])
            ),
            emotion_tendency=row.emotion_tendency or "",
            relationship_tone=row.relationship_tone or "",
            group_map={
                str(key): str(value)
                for key, value in (_json_loads(row.group_map_json, {}) or {}).items()
            },
            confidence=float(row.confidence or 0.0),
            observation_count=int(row.observation_count or 0),
            version=int(row.version or 1),
            created_at=row.created_at,
            updated_at=row.updated_at,
        )
