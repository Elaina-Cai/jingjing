"""Read-only Phase 0 identity resolver.

The service turns raw platform message coordinates into a deterministic
``PrincipalContext``.  It never writes to a database and never calls an LLM.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Mapping, Sequence

from core.terminal.link_graph import AccountLinkGraph
from core.terminal.principal import (
    AccountRef,
    Capability,
    ChatType,
    GroupMembershipPeriod,
    PrincipalContext,
    RelationLevel,
    RelationshipState,
    VisibilityScope,
)


@dataclass(frozen=True)
class AccountGrant:
    platform: str
    account_id: str
    read_enabled: bool = True
    operate_enabled: bool = True

    @property
    def ref(self) -> AccountRef:
        return AccountRef(self.platform, self.account_id)


MembershipKey = tuple[str, str, str]
PolicyKey = tuple[str, str, str]


class IdentityService:
    """Deterministic shadow identity resolution.

    Parameters are deliberately injectable so tests and future adapters can use
    real repositories without changing this module.
    """

    def __init__(
        self,
        *,
        link_graph: AccountLinkGraph | None = None,
        grants: Mapping[AccountRef, AccountGrant] | None = None,
        relationships: Mapping[tuple[str, str, str], RelationshipState] | None = None,
        person_identities: Mapping[tuple[str, str], str] | None = None,
        person_identity_scopes: Mapping[tuple[str, str, str], str] | None = None,
        strong_identities: Mapping[tuple[str, str, str], str] | None = None,
        group_memberships: Mapping[MembershipKey, Sequence[GroupMembershipPeriod]] | None = None,
        group_query_policies: Mapping[PolicyKey, bool] | None = None,
        third_party_allowed: set[tuple[str, str]] | None = None,
        proactive_allowed: set[tuple[str, str]] | None = None,
        proactive_targets: set[tuple[str, str, str]] | None = None,
    ):
        self.link_graph = link_graph or AccountLinkGraph()
        self._grants: dict[AccountRef, AccountGrant] = dict(grants or {})
        self._relationships: dict[tuple[str, str, str], RelationshipState] = dict(
            relationships or {}
        )
        self._person_identities: dict[tuple[str, str], str] = dict(person_identities or {})
        self._person_identity_scopes: dict[tuple[str, str, str], str] = dict(
            person_identity_scopes or {}
        )
        # Account-wide identifiers (wxid/qq_uin/phone/...) keyed by
        # ``(platform, account_id, raw_id)``: one person even when the id was
        # observed in many groups.  Weak, chat-scoped names stay in
        # ``_person_identity_scopes``.
        self._strong_identities: dict[tuple[str, str, str], str] = dict(
            strong_identities or {}
        )
        self._group_memberships: dict[MembershipKey, list[GroupMembershipPeriod]] = {
            key: list(periods) for key, periods in (group_memberships or {}).items()
        }
        self._group_query_policies: dict[PolicyKey, bool] = dict(
            group_query_policies or {}
        )
        self._third_party_allowed = set(third_party_allowed or ())
        self._proactive_allowed = set(proactive_allowed or ())
        self._proactive_targets = set(proactive_targets or ())

        # Deterministic "this person is known" set.  Only used to keep the
        # default capability level low for genuinely unresolved ids; names are
        # never consulted here.
        known: set[tuple[str, str]] = set()
        for state in self._relationships.values():
            if state.person_id and state.level != RelationLevel.UNKNOWN:
                known.add((state.platform, state.person_id))
        for (platform, _raw), person_id in self._person_identities.items():
            if platform and person_id:
                known.add((platform, person_id))
        for (platform, _account, _raw), person_id in self._person_identity_scopes.items():
            if platform and person_id:
                known.add((platform, person_id))
        self._known_person_ids = known

    # ------------------------------------------------------------------ helpers
    def grant_for(self, platform: str, account_id: str) -> AccountGrant:
        account = AccountRef(platform, account_id)
        return self._grants.get(account, AccountGrant(platform, account_id))

    def replace_link_graph(self, link_graph: AccountLinkGraph | None) -> None:
        """Hot-swap the account-link graph after a DB revision change."""
        self.link_graph = link_graph or AccountLinkGraph()

    def replace_proactive_targets(
        self, targets: set[tuple[str, str, str]] | None
    ) -> None:
        """Hot-swap the owner-managed proactive whitelist."""
        self._proactive_targets = set(targets or ())

    def resolve_person_id(self, platform: str, raw_id: str, account_id: str = "") -> str:
        """Resolve a raw platform id to one person id.

        Order matters: **strong identifiers first, as an account-wide fact**
        (a wxid is the same person in every group of one bot account), then
        weak identifiers inside their ``chat``/``account`` scope.  A global
        row with an empty ``account_id`` is honoured for callers that do not
        know the account.
        """
        raw_id = str(raw_id or "").strip()
        if not raw_id:
            return ""
        platform = str(platform or "").strip()
        account_id = str(account_id or "").strip()
        if account_id:
            strong = self._strong_identities.get((platform, account_id, raw_id))
            if strong:
                return strong
        strong_global = self._strong_identities.get((platform, "", raw_id))
        if strong_global:
            return strong_global
        if account_id:
            scoped = self._person_identity_scopes.get((platform, account_id, raw_id))
            if scoped:
                return scoped
        known = self._person_identities.get((platform, raw_id))
        if known:
            return known
        # Stable shadow fallback; a later identity adapter may merge this.
        return f"unresolved:{platform}:{raw_id}"

    def is_known_person(self, platform: str, person_id: str) -> bool:
        return bool(person_id) and (str(platform or ""), str(person_id)) in (
            self._known_person_ids
        )

    def person_role(
        self,
        platform: str,
        bot_account_id: str,
        person_id: str,
        *,
        level: RelationLevel | None = None,
    ) -> str:
        """Deterministic role label used by command ledgers and audit views."""
        if level == RelationLevel.OWNER:
            return "master"
        if level == RelationLevel.UNKNOWN or not self.is_known_person(platform, person_id):
            return "unknown"
        # Group membership is context-specific; callers that know they are in a
        # group may prefer "group_member".  This helper keeps the person-level
        # answer deterministic.
        return "ordinary"

    def relationship_for(self, account: AccountRef, person_id: str) -> RelationshipState:
        if not person_id:
            return RelationshipState(
                platform=account.platform,
                bot_account_id=account.account_id,
                person_id="",
                level=RelationLevel.UNKNOWN,
                source="no-person",
            )
        if self.link_graph.links():
            linked = self.link_graph.effective_relationship(
                account, person_id, self._relationships
            )
            if linked is not None:
                return linked
        direct = self._relationships.get((account.platform, account.account_id, person_id))
        if direct is not None:
            return direct
        return RelationshipState(
            platform=account.platform,
            bot_account_id=account.account_id,
            person_id=person_id,
            level=RelationLevel.UNKNOWN,
            intimacy=0.0,
            source="default",
        )

    def component_id_for(self, account: AccountRef) -> str:
        if not self.link_graph.links():
            return str(account)
        return self.link_graph.component_id_of(account)

    def component_accounts(self, account: AccountRef) -> frozenset[AccountRef]:
        return self.link_graph.component_of(account)

    def _periods_for(
        self,
        platform: str,
        group_id: str,
        person_id: str,
        account_id: str = "",
    ) -> list[GroupMembershipPeriod]:
        candidates = [
            (platform, f"{group_id}@{account_id}", person_id),
            (platform, group_id, person_id),
            (platform, f"*@{account_id}", person_id),
            (platform, "*", person_id),
        ]
        for key in candidates:
            periods = self._group_memberships.get(key)
            if periods:
                return list(periods)
        return []

    def membership_periods(
        self, platform: str, group_id: str, person_id: str, account_id: str = ""
    ) -> list[GroupMembershipPeriod]:
        return self._periods_for(platform, group_id, person_id, account_id)

    def is_member_at(
        self,
        platform: str,
        group_id: str,
        person_id: str,
        when: datetime,
        *,
        account_id: str = "",
    ) -> bool:
        return any(
            period.covers(when)
            for period in self._periods_for(platform, group_id, person_id, account_id)
        )

    def group_query_policy(self, platform: str, bot_account_id: str, group_id: str) -> bool:
        for key in (
            (platform, f"{group_id}@{bot_account_id}", bot_account_id),
            (platform, group_id, bot_account_id),
            (platform, group_id, ""),
        ):
            if key in self._group_query_policies:
                return bool(self._group_query_policies[key])
        return False

    def can_query_group_commands(
        self,
        platform: str,
        bot_account_id: str,
        group_id: str,
        principal: PrincipalContext,
    ) -> bool:
        if principal.is_owner:
            return True
        if not principal.has(Capability.QUERY_GROUP_COMMANDS):
            return False
        return self.group_query_policy(platform, bot_account_id, group_id)

    def _group_member_at(
        self,
        platform: str,
        group_id: str,
        person_id: str,
        when: datetime,
        bot_account_id: str = "",
    ) -> bool:
        periods = self._periods_for(platform, group_id, person_id, bot_account_id)
        if not periods:
            # No membership row for this person.  If the process has no
            # membership data at all, preserve the legacy "a group participant
            # is a member" default; once any membership data exists, absence is
            # treated as non-membership (fail closed for old commands).
            return not self._group_memberships
        return any(period.covers(when) for period in periods)

    # ------------------------------------------------------------------ resolve
    def resolve_principal(
        self,
        *,
        platform: str,
        bot_account_id: str,
        chat_id: str,
        chat_type: str = "private",
        sender_id: str = "",
        sender_name: str = "",
        occurred_at: datetime | None = None,
        group_id: str = "",
    ) -> PrincipalContext:
        when = occurred_at or datetime.now()
        normalized_chat = (
            ChatType.GROUP
            if str(chat_type or "").lower() == "group"
            else ChatType.PRIVATE
        )
        effective_group_id = group_id or (
            chat_id if normalized_chat == ChatType.GROUP else ""
        )
        sender_raw = str(sender_id or "").strip()
        if not sender_raw and normalized_chat == ChatType.PRIVATE:
            sender_raw = str(chat_id or "").strip()
        person_id = self.resolve_person_id(platform, sender_raw, bot_account_id)
        account = AccountRef(platform, bot_account_id)
        relationship = self.relationship_for(account, person_id)
        component_id = self.component_id_for(account)

        capabilities: set[Capability] = set()
        memory_scopes: set[VisibilityScope] = set()
        relationship_key = (f"{platform}:{bot_account_id}", person_id)
        unresolved = (not person_id) or person_id.startswith("unresolved:")
        proactive_chat_id = str(
            effective_group_id if normalized_chat == ChatType.GROUP else chat_id or ""
        )
        proactive_target_key = (
            str(platform or ""),
            str(bot_account_id or ""),
            proactive_chat_id,
        )
        proactive_target_allowed = (
            proactive_target_key in self._proactive_targets
            or (str(platform or ""), "", proactive_chat_id)
            in self._proactive_targets
        )

        if relationship.level == RelationLevel.OWNER:
            capabilities.update(
                {
                    Capability.VIEW_RAW_THOUGHT,
                    Capability.VIEW_COMPONENT_MEMORY,
                    Capability.VIEW_ALL_COMMANDS,
                    Capability.EXECUTE_OWNER_COMMAND,
                    Capability.SEND_AS_OWNER,
                }
            )
            if normalized_chat == ChatType.PRIVATE:
                capabilities.add(Capability.VIEW_OWNER_PRIVATE_MEMORY)
                memory_scopes.update(
                    {VisibilityScope.OWNER_ONLY, VisibilityScope.COMPONENT}
                )
            else:
                # A group owner is still owner, but private memory is not
                # injected into the group context.
                capabilities.add(Capability.QUERY_GROUP_COMMANDS)
                memory_scopes.add(VisibilityScope.GROUP_MEMBERS)
            if relationship_key in self._proactive_allowed or proactive_target_allowed:
                capabilities.add(Capability.PROACTIVE_MESSAGE)
        else:
            # A person is recognized by stable identity/relationship facts,
            # never by a display name.  Recorded strangers are deliberately
            # left at the low-permission tier.
            recognized = (
                not unresolved
                and relationship.level != RelationLevel.STRANGER
                and (
                    relationship.level != RelationLevel.UNKNOWN
                    or self.is_known_person(platform, person_id)
                )
            )
            is_group = normalized_chat == ChatType.GROUP
            member = (
                self._group_member_at(
                    platform,
                    effective_group_id,
                    person_id,
                    when,
                    bot_account_id,
                )
                if is_group and person_id
                else False
            )
            if is_group:
                if member:
                    memory_scopes.add(VisibilityScope.GROUP_MEMBERS)
                    # Membership is confirmed independently of the relation
                    # row, so the group-command capability is available even
                    # when no relationship exists yet.
                    capabilities.add(Capability.QUERY_GROUP_COMMANDS)
                    if recognized:
                        capabilities.add(Capability.VIEW_COMPONENT_MEMORY)
            elif recognized:
                capabilities.add(Capability.VIEW_COMPONENT_MEMORY)
                memory_scopes.add(VisibilityScope.ISSUER_ONLY)
            # Explicit opt-ins are still gated by a recognized identity.
            if recognized and relationship_key in self._third_party_allowed:
                capabilities.add(Capability.CONTACT_THIRD_PARTY)
            if recognized and (
                relationship_key in self._proactive_allowed
                or proactive_target_allowed
            ):
                capabilities.add(Capability.PROACTIVE_MESSAGE)

        return PrincipalContext(
            platform=platform,
            bot_account_id=bot_account_id,
            chat_id=chat_id,
            chat_type=normalized_chat,
            person_id=person_id,
            display_name=sender_name or sender_raw or chat_id,
            relation_to_bot=relationship.level,
            intimacy=float(relationship.intimacy or 0.0),
            group_id=effective_group_id,
            link_component_id=component_id,
            capabilities=frozenset(capabilities),
            memory_scopes=frozenset(memory_scopes),
            occurred_at=when,
        )
