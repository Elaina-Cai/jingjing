"""Read-only adapters that build the Phase 0 shadow identity service from the
existing project repositories.

The adapter never writes to the database.  It only reads existing identity,
membership, relationship and account-registry rows and converts them into the
deterministic Phase 0 model.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from core import temporal
from core.terminal.identity_service import AccountGrant, IdentityService
from core.terminal.link_graph import AccountLinkGraph
from core.terminal.principal import (
    AccountRef,
    GroupMembershipPeriod,
    RelationLevel,
    RelationshipState,
)
from safelog import warn

DEFAULT_LINK_PATH = Path("configs/account_links.json")
DEFAULT_GROUP_QUERY_PATH = Path("configs/group_query_policies.json")

_RELATION_MAP = {
    "owner": RelationLevel.OWNER,
    "master": RelationLevel.OWNER,
    "主人": RelationLevel.OWNER,
    "intimate": RelationLevel.INTIMATE,
    "亲密": RelationLevel.INTIMATE,
    "familiar": RelationLevel.FAMILIAR,
    "熟悉": RelationLevel.FAMILIAR,
    "ordinary": RelationLevel.ORDINARY,
    "normal": RelationLevel.ORDINARY,
    "普通": RelationLevel.ORDINARY,
    "朋友": RelationLevel.ORDINARY,
    "stranger": RelationLevel.STRANGER,
    "陌生": RelationLevel.STRANGER,
    "unknown": RelationLevel.UNKNOWN,
}


def relation_level(value: Any) -> RelationLevel:
    text = str(value or "").strip().lower()
    if not text:
        return RelationLevel.UNKNOWN
    for key, level in _RELATION_MAP.items():
        if key.lower() in text or text in key.lower():
            return level
    return RelationLevel.UNKNOWN


def _aware(value: datetime | None) -> datetime:
    """归一为应用时钟（naive 本地墙钟）；换算口径唯一 owner = ``temporal``。

    存量 DB 行是 naive 本地时间（``datetime.now()``）——naive 输入按系统本地时区
    解释，原样返回；aware 输入先转本地再去掉 tzinfo。
    """
    if value is None:
        return datetime.now()
    return temporal.to_naive_local(value)


# ``chat_id`` scopes weak identities (group nicknames, remarks).  Strong ids
# such as wxid are account-wide even when first observed in a group; ignoring
# those rows here made the Authority treat every group member as unresolved.
_WEAK_IDENTITY_TYPES = frozenset({
    "nickname", "remark", "group_nickname", "alias", "display_name",
})


def _is_chat_scoped_weak_identity(row: Any) -> bool:
    chat_id = str(getattr(row, "chat_id", "") or "").strip()
    if not chat_id:
        return False
    identity_type = str(getattr(row, "identity_type", "") or "").strip().lower()
    return identity_type in _WEAK_IDENTITY_TYPES


def load_person_identities(meta_repo: Any) -> dict[tuple[str, str], str]:
    """Map (platform, stable_raw_id) -> person_id from PersonIdentity rows."""
    try:
        from db.meta_models import PersonIdentity
    except Exception:
        warn("core/terminal/adapters.py:91 Exception", err=repr(__import__("sys").exc_info()[1]))
        return {}
    out: dict[tuple[str, str], tuple[float, str]] = {}
    try:
        with Session(meta_repo.engine) as session:
            rows = session.execute(select(PersonIdentity)).scalars().all()
        for row in rows:
            if _is_chat_scoped_weak_identity(row):
                continue
            raw = str(getattr(row, "identity_value", "") or "").strip()
            person_id = str(getattr(row, "person_id", "") or "").strip()
            platform = str(getattr(row, "platform", "") or "").strip()
            if not raw or not person_id or not platform:
                continue
            confidence = float(getattr(row, "confidence", 0.0) or 0.0)
            key = (platform, raw)
            existing = out.get(key)
            if existing is None or confidence > existing[0]:
                out[key] = (confidence, person_id)
    except Exception:
        warn("core/terminal/adapters.py:110 Exception", err=repr(__import__("sys").exc_info()[1]))
        return {}
    return {key: person for key, (_confidence, person) in out.items()}


def load_scoped_person_identities(
    meta_repo: Any,
) -> dict[tuple[str, str, str], str]:
    """Map ``(platform, account_id, raw_id)`` -> person_id.

    Account scope is part of the key because the same raw id may legitimately
    belong to different people on different bot accounts before the owner draws
    an explicit link.  Global rows still fall back through
    ``load_person_identities``.
    """
    try:
        from db.meta_models import PersonIdentity
    except Exception:
        warn("core/terminal/adapters.py:127 Exception", err=repr(__import__("sys").exc_info()[1]))
        return {}
    out: dict[tuple[str, str, str], tuple[float, str]] = {}
    try:
        with Session(meta_repo.engine) as session:
            rows = session.execute(select(PersonIdentity)).scalars().all()
        for row in rows:
            if _is_chat_scoped_weak_identity(row):
                continue
            raw = str(getattr(row, "identity_value", "") or "").strip()
            person_id = str(getattr(row, "person_id", "") or "").strip()
            platform = str(getattr(row, "platform", "") or "").strip()
            account_id = str(getattr(row, "account_id", "") or "").strip()
            if not raw or not person_id or not platform or not account_id:
                continue
            confidence = float(getattr(row, "confidence", 0.0) or 0.0)
            key = (platform, account_id, raw)
            existing = out.get(key)
            if existing is None or confidence > existing[0]:
                out[key] = (confidence, person_id)
    except Exception:
        warn("core/terminal/adapters.py:147 Exception", err=repr(__import__("sys").exc_info()[1]))
        return {}
    return {key: person for key, (_confidence, person) in out.items()}


def load_strong_person_identities(
    meta_repo: Any,
) -> dict[tuple[str, str, str], str]:
    """Map ``(platform, account_id, raw_id)`` -> person_id for strong ids only.

    A wxid/q号 is account-wide: the same value observed in five groups is one
    person.  Weak names (nickname/group nickname) are deliberately absent — they
    stay scoped by chat and are loaded by
    :func:`load_scoped_person_identities`.  Placeholder values such as
    ``unknown``/``self`` are not identifiers and are skipped.
    """
    try:
        from db.meta_models import PersonIdentity
        from db.repo_people import is_strong_identity
    except Exception:
        warn("core/terminal/adapters.py:166 Exception", err=repr(__import__("sys").exc_info()[1]))
        return {}
    out: dict[tuple[str, str, str], tuple[float, str]] = {}
    try:
        with Session(meta_repo.engine) as session:
            rows = session.execute(select(PersonIdentity)).scalars().all()
        for row in rows:
            identity_type = str(getattr(row, "identity_type", "") or "").strip()
            identity_value = str(getattr(row, "identity_value", "") or "").strip()
            if not is_strong_identity(identity_type, identity_value):
                continue
            person_id = str(getattr(row, "person_id", "") or "").strip()
            platform = str(getattr(row, "platform", "") or "").strip()
            account_id = str(getattr(row, "account_id", "") or "").strip()
            if not person_id or not platform:
                continue
            key = (platform, account_id, identity_value)
            confidence = float(getattr(row, "confidence", 0.0) or 0.0)
            existing = out.get(key)
            if existing is None or (confidence, person_id) > (existing[0], existing[1]):
                out[key] = (confidence, person_id)
    except Exception:
        warn("core/terminal/adapters.py:187 Exception", err=repr(__import__("sys").exc_info()[1]))
        return {}
    return {key: person for key, (_confidence, person) in out.items()}


def load_group_memberships(meta_repo: Any) -> dict[tuple[str, str, str], list[GroupMembershipPeriod]]:
    """Map (platform, group@account, person_id) -> membership periods.

    Existing GroupMembership rows do not store leave events, so today every
    row is treated as an open-ended period starting at first_seen_at.  When
    ``group_membership_periods`` has explicit epochs they take precedence via
    :func:`load_group_membership_periods`.
    """
    try:
        from db.meta_models import GroupMembership
    except Exception:
        warn("core/terminal/adapters.py:202 Exception", err=repr(__import__("sys").exc_info()[1]))
        return {}
    out: dict[tuple[str, str, str], list[GroupMembershipPeriod]] = {}
    try:
        with Session(meta_repo.engine) as session:
            rows = session.execute(select(GroupMembership)).scalars().all()
        for row in rows:
            platform = str(getattr(row, "platform", "") or "").strip()
            account_id = str(getattr(row, "account_id", "") or "").strip()
            chat_id = str(getattr(row, "chat_id", "") or "").strip()
            person_id = str(getattr(row, "person_id", "") or "").strip()
            if not platform or not chat_id or not person_id:
                continue
            start_at = _aware(getattr(row, "first_seen_at", None))
            period = GroupMembershipPeriod(start_at=start_at, end_at=None)
            account_key = f"{chat_id}@{account_id}" if account_id else chat_id
            for key in ((platform, account_key, person_id), (platform, chat_id, person_id)):
                out.setdefault(key, []).append(period)
    except Exception:
        warn("core/terminal/adapters.py:220 Exception", err=repr(__import__("sys").exc_info()[1]))
        return {}
    return out


def load_group_membership_periods(
    meta_repo: Any,
) -> dict[tuple[str, str, str], list[GroupMembershipPeriod]]:
    """Load explicit joined/left epochs from ``group_membership_periods``."""
    try:
        from db.meta_models import GroupMembershipPeriod as MembershipPeriodRow
    except Exception:
        warn("core/terminal/adapters.py:231 Exception", err=repr(__import__("sys").exc_info()[1]))
        return {}
    out: dict[tuple[str, str, str], list[GroupMembershipPeriod]] = {}
    try:
        with Session(meta_repo.engine) as session:
            rows = session.execute(select(MembershipPeriodRow)).scalars().all()
    except Exception:
        warn("core/terminal/adapters.py:237 Exception", err=repr(__import__("sys").exc_info()[1]))
        return {}
    for row in rows:
        platform = str(getattr(row, "platform", "") or "").strip()
        account_id = str(getattr(row, "account_id", "") or "").strip()
        chat_id = str(getattr(row, "chat_id", "") or "").strip()
        person_id = str(getattr(row, "person_id", "") or "").strip()
        if not platform or not chat_id or not person_id:
            continue
        joined_at = getattr(row, "joined_at", None)
        left_at = getattr(row, "left_at", None)
        period = GroupMembershipPeriod(
            start_at=_aware(joined_at),
            end_at=_aware(left_at) if left_at is not None else None,
            epoch=max(1, int(getattr(row, "epoch", 1) or 1)),
        )
        account_key = f"{chat_id}@{account_id}" if account_id else chat_id
        for key in ((platform, account_key, person_id), (platform, chat_id, person_id)):
            out.setdefault(key, []).append(period)
    return out



def load_relationships(
    meta_repo: Any,
    *,
    identities: Mapping[tuple[str, str], str] | None = None,
) -> dict[tuple[str, str, str], RelationshipState]:
    """Convert Relationship rows into account-scoped RelationshipState rows."""
    try:
        from db.meta_models import PersonIdentity, Relationship
    except Exception:
        warn("core/terminal/adapters.py:268 Exception", err=repr(__import__("sys").exc_info()[1]))
        return {}
    identity_rows: list[tuple[str, str, str, str]] = []
    try:
        with Session(meta_repo.engine) as session:
            relations = session.execute(select(Relationship)).scalars().all()
            identity_models = session.execute(select(PersonIdentity)).scalars().all()
        for row in identity_models:
            identity_rows.append(
                (
                    str(getattr(row, "identity_value", "") or "").strip(),
                    str(getattr(row, "platform", "") or "").strip(),
                    str(getattr(row, "account_id", "") or "").strip(),
                    str(getattr(row, "person_id", "") or "").strip(),
                )
            )
    except Exception:
        warn("core/terminal/adapters.py:284 Exception", err=repr(__import__("sys").exc_info()[1]))
        return {}
    by_value: dict[str, list[tuple[str, str, str]]] = {}
    for value, platform, account_id, person_id in identity_rows:
        if value and platform and person_id:
            by_value.setdefault(value, []).append((platform, account_id, person_id))

    out: dict[tuple[str, str, str], RelationshipState] = {}
    for rel in relations:
        target = str(getattr(rel, "target_wxid", "") or "").strip()
        level = relation_level(getattr(rel, "relation_type", ""))
        intimacy = float(getattr(rel, "intimacy", 0.0) or 0.0)
        if not target:
            continue
        for platform, account_id, person_id in by_value.get(target, ()):
            out[(platform, account_id, person_id)] = RelationshipState(
                platform=platform,
                bot_account_id=account_id,
                person_id=person_id,
                level=level,
                intimacy=intimacy,
                source="relationship",
            )
    return out


def _accounts_for_person(
    identity_rows: Sequence[tuple[str, str, str]], platform: str, person_id: str
) -> set[str]:
    accounts: set[str] = set()
    for _value, row_platform, row_person in identity_rows:
        if row_platform == platform and row_person == person_id:
            accounts.add("")
    # Identity rows do not include account_id in this flattened view; the
    # caller merges with account grants.  Empty account means "all accounts".
    return accounts or {""}


def load_owner_relationships(
    *,
    runtime: Any = None,
    identities: Mapping[tuple[str, str], str] | None = None,
    config_obj: Any = None,
) -> dict[tuple[str, str, str], RelationshipState]:
    """Build owner RelationshipState rows from runtime account configs."""
    identities = identities or {}
    out: dict[tuple[str, str, str], RelationshipState] = {}

    def add_owner(platform: str, account_id: str, owner_raw: str) -> None:
        raw = str(owner_raw or "").strip()
        if not raw:
            return
        person_id = identities.get((platform, raw)) or f"unresolved:{platform}:{raw}"
        out[(platform, account_id, person_id)] = RelationshipState(
            platform=platform,
            bot_account_id=account_id,
            person_id=person_id,
            level=RelationLevel.OWNER,
            intimacy=1.0,
            source="owner-grant",
        )

    try:
        contexts = list(runtime.all_contexts()) if runtime is not None else []
    except Exception:
        warn("core/terminal/adapters.py:348 Exception", err=repr(__import__("sys").exc_info()[1]))
        contexts = []
    if contexts:
        for ctx in contexts:
            platform = str(getattr(ctx, "platform", "") or "")
            account_id = str(getattr(ctx, "account_id", "") or "")
            cfg = getattr(ctx, "config", None)
            owner_ids = list(getattr(cfg, "owner_wxids", []) or []) if cfg is not None else []
            if not owner_ids and platform == "wechat":
                try:
                    owner_ids = list(_get_config().wechat.get_owner_wxids())
                except Exception:
                    warn("core/terminal/adapters.py:359 Exception", err=repr(__import__("sys").exc_info()[1]))
                    owner_ids = []
            for raw in owner_ids:
                add_owner(platform, account_id, raw)
        return out

    try:
        cfg_obj = config_obj or _get_config()
        for ac in cfg_obj.all_account_configs():
            platform = str(getattr(ac, "platform", "") or "")
            account_id = str(getattr(ac, "account_id", "") or "")
            owner_ids = list(getattr(ac, "owner_wxids", []) or [])
            if not owner_ids and platform == "wechat":
                owner_ids = list(cfg_obj.wechat.get_owner_wxids())
            for raw in owner_ids:
                add_owner(platform, account_id, raw)
    except Exception:
        warn("core/terminal/adapters.py:375 Exception", err=repr(__import__("sys").exc_info()[1]))
    return out


def load_account_grants(
    *,
    runtime: Any = None,
    config_obj: Any = None,
) -> dict[AccountRef, AccountGrant]:
    out: dict[AccountRef, AccountGrant] = {}
    try:
        contexts = list(runtime.all_contexts()) if runtime is not None else []
    except Exception:
        warn("core/terminal/adapters.py:388 Exception", err=repr(__import__("sys").exc_info()[1]))
        contexts = []
    if contexts:
        for ctx in contexts:
            platform = str(getattr(ctx, "platform", "") or "")
            account_id = str(getattr(ctx, "account_id", "") or "")
            role = str(getattr(ctx, "role", "yinyue") or "yinyue")
            ref = AccountRef(platform, account_id)
            out[ref] = AccountGrant(
                platform=platform,
                account_id=account_id,
                read_enabled=True,
                operate_enabled=role in {"yinyue", "owner"},
            )
        return out
    try:
        cfg_obj = config_obj or _get_config()
        for ac in cfg_obj.all_account_configs():
            platform = str(getattr(ac, "platform", "") or "")
            account_id = str(getattr(ac, "account_id", "") or "")
            role = str(getattr(ac, "role", "yinyue") or "yinyue")
            ref = AccountRef(platform, account_id)
            out[ref] = AccountGrant(
                platform=platform,
                account_id=account_id,
                read_enabled=True,
                operate_enabled=role in {"yinyue", "owner"},
            )
    except Exception:
        warn("core/terminal/adapters.py:416 Exception", err=repr(__import__("sys").exc_info()[1]))
    return out


def load_link_graph(path: str | Path | None = None) -> AccountLinkGraph:
    file_path = Path(path) if path else DEFAULT_LINK_PATH
    graph = AccountLinkGraph()
    if not file_path.exists():
        return graph
    try:
        data = json.loads(file_path.read_text(encoding="utf-8"))
    except Exception:
        warn("core/terminal/adapters.py:428 Exception", err=repr(__import__("sys").exc_info()[1]))
        return graph
    links = data.get("links", data) if isinstance(data, dict) else data
    manual_sources = data.get("manual_sources", []) if isinstance(data, dict) else []
    if isinstance(links, list):
        for item in links:
            try:
                if isinstance(item, dict):
                    left = _account_from(item.get("a") or item.get("left"))
                    right = _account_from(item.get("b") or item.get("right"))
                else:
                    left = AccountRef(str(item[0]), str(item[1]))
                    right = AccountRef(str(item[2]), str(item[3]))
                if left and right and left != right:
                    graph.add_link(left, right)
            except Exception:
                warn("core/terminal/adapters.py:443 Exception", err=repr(__import__("sys").exc_info()[1]))
                continue
    if isinstance(manual_sources, list):
        for item in manual_sources:
            try:
                component = AccountRef(str(item["platform"]), str(item["account_id"]))
                graph.set_manual_source(
                    graph.component_id_of(component),
                    str(item["person_id"]),
                    str(item.get("source_account_id") or item["account_id"]),
                )
            except Exception:
                warn("core/terminal/adapters.py:454 Exception", err=repr(__import__("sys").exc_info()[1]))
                continue
    return graph


def _account_from(value: Any) -> AccountRef | None:
    if isinstance(value, dict):
        platform = str(value.get("platform") or "").strip()
        account_id = str(value.get("account_id") or "").strip()
        if platform and account_id:
            return AccountRef(platform, account_id)
    return None


def load_account_link_edges(
    meta_repo: Any,
    graph: AccountLinkGraph | None = None,
) -> AccountLinkGraph:
    """Merge active owner-drawn edges from ``account_link_edges`` into a graph."""
    result = graph if graph is not None else AccountLinkGraph()
    if meta_repo is None:
        return result
    try:
        from db.meta_models import AccountLinkEdge
    except Exception:
        warn("core/terminal/adapters.py:478 Exception", err=repr(__import__("sys").exc_info()[1]))
        return result
    try:
        with Session(meta_repo.engine) as session:
            rows = (
                session.execute(
                    select(AccountLinkEdge).where(AccountLinkEdge.revoked_at.is_(None))
                )
                .scalars()
                .all()
            )
    except Exception:
        warn("core/terminal/adapters.py:489 Exception", err=repr(__import__("sys").exc_info()[1]))
        return result
    for row in rows:
        left = AccountRef(
            str(getattr(row, "platform_a", "") or "").strip(),
            str(getattr(row, "account_a", "") or "").strip(),
        )
        right = AccountRef(
            str(getattr(row, "platform_b", "") or "").strip(),
            str(getattr(row, "account_b", "") or "").strip(),
        )
        if (
            left.platform
            and left.account_id
            and right.platform
            and right.account_id
            and left != right
        ):
            result.add_link(left, right)
    return result


def load_group_query_policies(path: str | Path | None = None) -> dict[tuple[str, str, str], bool]:
    file_path = Path(path) if path else DEFAULT_GROUP_QUERY_PATH
    if not file_path.exists():
        return {}
    try:
        data = json.loads(file_path.read_text(encoding="utf-8"))
    except Exception:
        warn("core/terminal/adapters.py:517 Exception", err=repr(__import__("sys").exc_info()[1]))
        return {}
    out: dict[tuple[str, str, str], bool] = {}
    if isinstance(data, dict):
        data = data.get("policies", [])
    if not isinstance(data, list):
        return out
    for item in data:
        try:
            platform = str(item.get("platform") or "")
            account_id = str(item.get("bot_account_id") or item.get("account_id") or "")
            group_id = str(item.get("group_id") or item.get("chat_id") or "")
            enabled = bool(item.get("enabled", False))
            if not platform or not group_id:
                continue
            out[(platform, f"{group_id}@{account_id}", account_id)] = enabled
            out[(platform, group_id, account_id)] = enabled
            out[(platform, group_id, "")] = enabled
        except Exception:
            warn("core/terminal/adapters.py:535 Exception", err=repr(__import__("sys").exc_info()[1]))
            continue
    return out


def load_proactive_targets(meta_repo: Any) -> set[tuple[str, str, str]]:
    """Load enabled owner-managed proactive conversations.

    Keys match :class:`IdentityService`'s proactive target key:
    ``(platform, bot_account_id, chat_id)``.  Disabled rows are deliberately
    omitted from the capability set; the outbound gateway still reads the
    repository directly, so disabling a row takes effect even before a cache
    refresh.
    """
    if meta_repo is None:
        return set()
    try:
        rows = meta_repo.proactive.list_targets(enabled_only=True, limit=10000)
    except Exception:
        warn("core/terminal/adapters.py:553 Exception", err=repr(__import__("sys").exc_info()[1]))
        return set()
    targets: set[tuple[str, str, str]] = set()
    for row in rows:
        platform = str(getattr(row, "platform", "") or "").strip()
        account_id = str(getattr(row, "bot_account_id", "") or "").strip()
        chat_id = str(getattr(row, "chat_id", "") or "").strip()
        if platform and chat_id:
            targets.add((platform, account_id, chat_id))
    return targets


def _get_config() -> Any:
    from core.config.settings import config

    return config


def build_identity_service(
    *,
    meta_repo: Any = None,
    runtime: Any = None,
    config_obj: Any = None,
    link_path: str | Path | None = None,
    group_query_path: str | Path | None = None,
) -> IdentityService:
    """Build a deterministic identity service from existing repositories."""
    identities = load_person_identities(meta_repo) if meta_repo is not None else {}
    scoped_identities = (
        load_scoped_person_identities(meta_repo) if meta_repo is not None else {}
    )
    strong_identities = (
        load_strong_person_identities(meta_repo) if meta_repo is not None else {}
    )
    relationships = (
        load_relationships(meta_repo, identities=identities)
        if meta_repo is not None
        else {}
    )
    owner_relationships = load_owner_relationships(
        runtime=runtime, identities=identities, config_obj=config_obj
    )
    relationships.update(owner_relationships)
    memberships = (
        load_group_memberships(meta_repo) if meta_repo is not None else {}
    )
    if meta_repo is not None:
        # Explicit leave/re-join epochs win over the legacy open-ended rows.
        for key, periods in load_group_membership_periods(meta_repo).items():
            memberships[key] = periods
    link_graph = load_link_graph(link_path)
    link_graph = load_account_link_edges(meta_repo, link_graph)
    grants = load_account_grants(runtime=runtime, config_obj=config_obj)
    proactive_targets = (
        load_proactive_targets(meta_repo) if meta_repo is not None else set()
    )
    return IdentityService(
        link_graph=link_graph,
        grants=grants,
        relationships=relationships,
        person_identities=identities,
        person_identity_scopes=scoped_identities,
        strong_identities=strong_identities,
        group_memberships=memberships,
        group_query_policies=load_group_query_policies(group_query_path),
        proactive_targets=proactive_targets,
    )


def build_identity_service_from_bootstrap() -> IdentityService:
    """Convenience wrapper for runtime/scheduler code paths.

    Import bootstrap lazily so this module stays safe for unit tests.
    """
    from core.terminal.store_port import get_meta_repo
    from core.bootstrap import get_runtime

    return build_identity_service(meta_repo=get_meta_repo(), runtime=get_runtime())
