"""Phase 0 terminal shadow-model tests.

Run directly:
    python tests/test_terminal_phase0.py
"""

from __future__ import annotations

import os
import shutil
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

ROOT = Path(__file__).resolve().parent.parent
_TEST_TMP_ROOT = ROOT / "data" / "_terminal_phase0_test"


def _make_temp_dir() -> Path:
    _TEST_TMP_ROOT.mkdir(parents=True, exist_ok=True)
    path = _TEST_TMP_ROOT / f"case_{uuid.uuid4().hex}"
    path.mkdir(parents=True, exist_ok=False)
    return path

from core.terminal import (  # noqa: E402
    AccountLinkGraph,
    AccountRef,
    Capability,
    ChatType,
    CommandAccessDenied,
    CommandLedger,
    CommandStatus,
    Event,
    EventType,
    GroupMembershipPeriod,
    IdentityMode,
    IdentityService,
    PrincipalContext,
    PublicRenderPolicy,
    RelationLevel,
    RelationshipState,
    Sensitivity,
    VisibilityScope,
)
from core.terminal.principal import pick_closer_relationship  # noqa: E402
from core.terminal.event_store_db import TerminalEventStore  # noqa: E402
from core.terminal.adapters import (  # noqa: E402
    build_identity_service,
    load_group_memberships,
    load_owner_relationships,
    load_person_identities,
    load_relationships,
)
from core.terminal.command_shadow import ShadowCommandRecorder  # noqa: E402
from core.terminal.shadow import (  # noqa: E402
    record_action_event,
    record_message_events,
    record_owner_thought,
    record_reply_event,
    record_thought_event,
)
from sqlalchemy import text  # noqa: E402
from db.meta_repo import MetaRepositoryManager  # noqa: E402
from db.migrations import run_migrations  # noqa: E402
from types import SimpleNamespace  # noqa: E402

UTC = timezone.utc
W = "wechat"
A = AccountRef(W, "yinyue_a")
B = AccountRef(W, "yinyue_b")


class _MemoryStore:
    """Minimal in-memory event store for Phase 0 tests.

    The debug JSONL store was deleted in the Phase 5 D cleanup; tests only
    need append/all/count plus the shared visibility ACL.
    """

    def __init__(self) -> None:
        self._events: list[Event] = []
        self._sequence = 0

    def append(self, event: Event) -> Event:
        if not event.event_id:
            event.event_id = f"evt_{len(self._events) + 1}"
        self._sequence += 1
        if not event.sequence:
            event.sequence = self._sequence
        self._events.append(event)
        return event

    def all(self) -> tuple[Event, ...]:
        return tuple(self._events)

    def count(self) -> int:
        return len(self._events)

    def query_visible(self, principal, limit: int | None = None):
        from core.terminal.visibility import event_visible

        out = [e for e in self._events if event_visible(e, principal)]
        return out[-limit:] if limit else out
C = AccountRef(W, "yinyue_c")


def _dt(hour: int = 12, day: int = 1) -> datetime:
    return datetime(2026, 1, day, hour, 0, 0, tzinfo=UTC)


def _private_principal(
    service: IdentityService,
    *,
    account: AccountRef = A,
    raw_id: str,
    name: str = "",
) -> PrincipalContext:
    return service.resolve_principal(
        platform=account.platform,
        bot_account_id=account.account_id,
        chat_id=raw_id,
        chat_type="private",
        sender_id=raw_id,
        sender_name=name or raw_id,
        occurred_at=_dt(),
    )


def test_link_graph_transitive_and_reversible() -> None:
    graph = AccountLinkGraph()
    graph.add_link(A, B)
    graph.add_link(B, C)
    assert graph.connected(A, C)
    assert graph.component_of(A) == frozenset({A, B, C})
    assert len(graph.connected_components()) == 1

    graph.remove_link(A, B)
    assert not graph.connected(A, B)
    assert graph.component_of(A) == frozenset({A})
    assert graph.component_of(B) == frozenset({B, C})


def test_effective_relationship_uses_closer_link() -> None:
    graph = AccountLinkGraph()
    graph.add_link(A, B)
    relationships = {
        (W, A.account_id, "p1"): RelationshipState(
            W, A.account_id, "p1", RelationLevel.ORDINARY, 0.2, "a"
        ),
        (W, B.account_id, "p1"): RelationshipState(
            W, B.account_id, "p1", RelationLevel.INTIMATE, 0.9, "b"
        ),
    }
    effective = graph.effective_relationship(A, "p1", relationships)
    assert effective is not None
    assert effective.level == RelationLevel.INTIMATE
    assert effective.bot_account_id == B.account_id

    graph.set_manual_source(graph.component_id_of(A), "p1", A.account_id)
    manual = graph.effective_relationship(A, "p1", relationships)
    assert manual is not None and manual.bot_account_id == A.account_id

    owner = RelationshipState(
        W, A.account_id, "p1", RelationLevel.OWNER, 0.1, "manual"
    )
    ordinary = RelationshipState(
        W, B.account_id, "p1", RelationLevel.ORDINARY, 99.0, "high-intimacy"
    )
    assert pick_closer_relationship([ordinary, owner]).level == RelationLevel.OWNER


def _owner_service(*, links: bool = False) -> IdentityService:
    graph = AccountLinkGraph()
    if links:
        graph.add_link(A, B)
    relationships = {
        (W, A.account_id, "p_master"): RelationshipState(
            W, A.account_id, "p_master", RelationLevel.OWNER, 0.9, "seed"
        ),
        (W, A.account_id, "p_friend"): RelationshipState(
            W, A.account_id, "p_friend", RelationLevel.ORDINARY, 0.3, "seed"
        ),
        (W, B.account_id, "p_master"): RelationshipState(
            W, B.account_id, "p_master", RelationLevel.OWNER, 0.9, "seed"
        ),
    }
    return IdentityService(
        link_graph=graph,
        relationships=relationships,
        person_identities={
            (W, "master_wxid"): "p_master",
            (W, "friend_wxid"): "p_friend",
        },
    )


def test_principal_owner_ordinary_group_and_thought_acl() -> None:
    service = _owner_service()
    owner_private = _private_principal(service, raw_id="master_wxid", name="主人")
    assert owner_private.relation_to_bot == RelationLevel.OWNER
    assert owner_private.has(Capability.VIEW_RAW_THOUGHT)
    assert owner_private.has(Capability.VIEW_OWNER_PRIVATE_MEMORY)
    assert VisibilityScope.OWNER_ONLY in owner_private.memory_scopes

    friend_private = _private_principal(service, raw_id="friend_wxid", name="朋友")
    assert friend_private.relation_to_bot == RelationLevel.ORDINARY
    assert not friend_private.has(Capability.VIEW_RAW_THOUGHT)
    assert not friend_private.has(Capability.VIEW_OWNER_PRIVATE_MEMORY)
    assert friend_private.memory_scopes == frozenset({VisibilityScope.ISSUER_ONLY})

    owner_group = service.resolve_principal(
        platform=W,
        bot_account_id=A.account_id,
        chat_id="group_1",
        chat_type="group",
        sender_id="master_wxid",
        sender_name="主人",
        occurred_at=_dt(),
    )
    assert owner_group.chat_type == ChatType.GROUP
    assert owner_group.relation_to_bot == RelationLevel.OWNER
    assert not owner_group.has(Capability.VIEW_OWNER_PRIVATE_MEMORY)
    assert owner_group.memory_scopes == frozenset({VisibilityScope.GROUP_MEMBERS})

    unknown = _private_principal(service, raw_id="stranger_wxid", name="陌生人")
    assert unknown.relation_to_bot == RelationLevel.UNKNOWN
    assert not unknown.has(Capability.VIEW_RAW_THOUGHT)
    assert not unknown.has(Capability.VIEW_OWNER_PRIVATE_MEMORY)


def test_event_store_visibility_and_persistence() -> None:
    service = _owner_service()
    owner = _private_principal(service, raw_id="master_wxid", name="主人")
    friend = _private_principal(service, raw_id="friend_wxid", name="朋友")
    group_member = service.resolve_principal(
        platform=W,
        bot_account_id=A.account_id,
        chat_id="group_1",
        chat_type="group",
        sender_id="friend_wxid",
        sender_name="朋友",
        occurred_at=_dt(),
    )

    tmp = _make_temp_dir()
    meta = None
    try:
        db_path = tmp / "phase0_meta.db"
        meta = MetaRepositoryManager(str(db_path))
        run_migrations(str(db_path))
        store = TerminalEventStore(meta.engine)
        thought = record_owner_thought(
            content="内部推理：先确认时间再决定", store=store
        )
        group_event = Event.new(
            event_type=EventType.MESSAGE,
            occurred_at=_dt(),
            platform=W,
            bot_account_id=A.account_id,
            chat_id="group_1",
            chat_type="group",
            actor_person_id="p_friend",
            content="群里说了句话",
            visibility_scope=frozenset({VisibilityScope.GROUP_MEMBERS}),
            metadata={"group_id": "group_1"},
        )
        store.append(group_event)

        # SQLite round-trips events; compare by event id instead of object
        # identity because query_visible returns reconstructed Event objects.
        owner_ids = {e.event_id for e in store.query_visible(owner)}
        friend_ids = {e.event_id for e in store.query_visible(friend)}
        member_ids = {e.event_id for e in store.query_visible(group_member)}
        assert thought.event_id in owner_ids
        assert thought.event_id not in friend_ids
        assert group_event.event_id in member_ids
        assert group_event.event_id not in friend_ids
        assert group_event.event_id in owner_ids

        reopened = TerminalEventStore(meta.engine)
        reloaded = reopened.all()
        assert len(reloaded) == 2
        assert reloaded[0].sensitivity == Sensitivity.OWNER_CHAIN_OF_THOUGHT
    finally:
        if meta is not None:
            try:
                meta.engine.dispose()
            except Exception:
                pass
        shutil.rmtree(tmp, ignore_errors=True)


def _group_ledger() -> tuple[CommandLedger, IdentityService]:
    t0 = _dt(hour=10)
    t1 = _dt(hour=11)
    t2 = _dt(hour=12)
    t3 = _dt(hour=13)
    service = IdentityService(
        relationships={
            (W, A.account_id, "p_master"): RelationshipState(
                W, A.account_id, "p_master", RelationLevel.OWNER, 0.9, "seed"
            ),
        },
        group_memberships={
            (W, "group_1", "p_member"): [
                GroupMembershipPeriod(start_at=t0 - timedelta(days=1))
            ],
            (W, "group_1", "p_new"): [
                GroupMembershipPeriod(start_at=t2)
            ],
            (W, "group_1", "p_ex"): [
                GroupMembershipPeriod(
                    start_at=t0 - timedelta(days=2),
                    end_at=t3 - timedelta(minutes=30),
                )
            ],
        },
        group_query_policies={(W, "group_1", ""): True},
        person_identities={
            (W, "master_wxid"): "p_master",
            (W, "friend_wxid"): "p_friend",
            (W, "new_wxid"): "p_new",
            (W, "ex_wxid"): "p_ex",
        },
    )
    ledger = CommandLedger(service)
    ledger.record_command(
        created_at=t0,
        issuer_person_id="p_master",
        issuer_account_id="master_wxid",
        issuer_role="master",
        origin_platform=W,
        origin_bot_account_id=A.account_id,
        origin_chat_id="group_1",
        origin_chat_type="group",
        raw_request="以我的名义发一条聚会通知",
        action_type="send_message",
        clarified_spec={"summary": "发一条聚会通知"},
        metadata={"public_summary": "发了一条聚会通知"},
        nominal_name="阿银",
        identity_mode=IdentityMode.AS_OWNER_NAMED,
        public_render_policy=PublicRenderPolicy.OWNER_NAMED,
    )
    ledger.record_command(
        created_at=t1,
        issuer_person_id="p_friend",
        issuer_account_id="friend_wxid",
        issuer_role="group_member",
        origin_platform=W,
        origin_bot_account_id=A.account_id,
        origin_chat_id="group_1",
        origin_chat_type="group",
        raw_request="帮我在群里发个表情",
        action_type="send_sticker",
        clarified_spec={"summary": "发了一个表情"},
        metadata={"public_summary": "发了一个表情"},
    )
    ledger.record_command(
        created_at=t1,
        issuer_person_id="p_friend",
        issuer_account_id="friend_wxid",
        issuer_role="ordinary",
        origin_platform=W,
        origin_bot_account_id=A.account_id,
        origin_chat_id="friend_wxid",
        origin_chat_type="private",
        raw_request="帮我查一下天气",
        action_type="search_weather",
        clarified_spec={"summary": "查了天气"},
    )
    return ledger, service


def test_command_visibility_by_role_and_membership_period() -> None:
    ledger, service = _group_ledger()
    owner_cmd = ledger.all_commands()[0]
    group_cmd = ledger.all_commands()[1]
    private_cmd = ledger.all_commands()[2]

    owner = service.resolve_principal(
        platform=W,
        bot_account_id=A.account_id,
        chat_id="master_wxid",
        chat_type="private",
        sender_id="master_wxid",
        sender_name="主人",
        occurred_at=_dt(),
    )
    assert {c.command_id for c in ledger.query_commands(owner)} == {
        owner_cmd.command_id,
        group_cmd.command_id,
        private_cmd.command_id,
    }

    issuer = _private_principal(service, raw_id="friend_wxid", name="朋友")
    own = ledger.query_commands(issuer)
    assert {c.command_id for c in own} == {group_cmd.command_id, private_cmd.command_id}

    current_member = service.resolve_principal(
        platform=W,
        bot_account_id=A.account_id,
        chat_id="group_1",
        chat_type="group",
        sender_id="member_wxid",
        sender_name="成员",
        occurred_at=_dt(hour=14),
    )
    # Map the group member to p_member, since the raw id above is only a shadow.
    current_member = PrincipalContext(
        platform=current_member.platform,
        bot_account_id=current_member.bot_account_id,
        chat_id=current_member.chat_id,
        chat_type=ChatType.GROUP,
        person_id="p_member",
        display_name="成员",
        relation_to_bot=RelationLevel.ORDINARY,
        group_id="group_1",
        capabilities=frozenset({Capability.QUERY_GROUP_COMMANDS}),
        occurred_at=_dt(hour=14),
    )
    visible = ledger.query_commands(current_member, group_id="group_1")
    assert {c.command_id for c in visible} == {owner_cmd.command_id, group_cmd.command_id}

    new_member = PrincipalContext(
        platform=W,
        bot_account_id=A.account_id,
        chat_id="group_1",
        chat_type=ChatType.GROUP,
        person_id="p_new",
        display_name="新人",
        relation_to_bot=RelationLevel.ORDINARY,
        group_id="group_1",
        capabilities=frozenset({Capability.QUERY_GROUP_COMMANDS}),
        occurred_at=_dt(hour=14),
    )
    assert ledger.query_commands(new_member, group_id="group_1") == []

    ex_member = PrincipalContext(
        platform=W,
        bot_account_id=A.account_id,
        chat_id="ex_wxid",
        chat_type=ChatType.PRIVATE,
        person_id="p_ex",
        display_name="退群的人",
        relation_to_bot=RelationLevel.ORDINARY,
        group_id="group_1",
        capabilities=frozenset(),
        occurred_at=_dt(hour=14),
    )
    assert {c.command_id for c in ledger.query_commands(ex_member, group_id="group_1")} == {
        owner_cmd.command_id,
        group_cmd.command_id,
    }

    ledger.render_query(current_member, group_id="group_1")
    assert ledger.query_logs()
    rendered = ledger.render_query(current_member, group_id="group_1")
    assert "主人" not in rendered
    assert "阿银" in rendered
    assert "鲸鲸自己决定" in rendered
    assert "raw_request" not in rendered


def test_group_query_switch_denies_non_owner_but_not_owner() -> None:
    ledger, service = _group_ledger()
    # Rebuild with the switch off.
    disabled = IdentityService(
        group_memberships={
            (W, "group_1", "p_member"): [GroupMembershipPeriod(start_at=_dt(hour=9))]
        },
        group_query_policies={(W, "group_1", ""): False},
    )
    disabled_ledger = CommandLedger(disabled)
    disabled_ledger.record_command(
        created_at=_dt(hour=10),
        issuer_person_id="p_friend",
        issuer_account_id="friend_wxid",
        issuer_role="group_member",
        origin_platform=W,
        origin_bot_account_id=A.account_id,
        origin_chat_id="group_1",
        origin_chat_type="group",
        raw_request="群里发个东西",
    )
    member = PrincipalContext(
        platform=W,
        bot_account_id=A.account_id,
        chat_id="group_1",
        chat_type=ChatType.GROUP,
        person_id="p_member",
        display_name="成员",
        relation_to_bot=RelationLevel.ORDINARY,
        group_id="group_1",
        capabilities=frozenset({Capability.QUERY_GROUP_COMMANDS}),
        occurred_at=_dt(hour=12),
    )
    try:
        disabled_ledger.query_commands(member, group_id="group_1")
        raise AssertionError("expected CommandAccessDenied")
    except CommandAccessDenied:
        pass

    owner = PrincipalContext(
        platform=W,
        bot_account_id=A.account_id,
        chat_id="master_wxid",
        chat_type=ChatType.PRIVATE,
        person_id="p_master",
        display_name="主人",
        relation_to_bot=RelationLevel.OWNER,
        capabilities=frozenset({Capability.VIEW_RAW_THOUGHT}),
        occurred_at=_dt(hour=12),
    )
    assert len(disabled_ledger.query_commands(owner)) == 1


def test_shadow_message_recording_is_read_only() -> None:
    service = _owner_service()
    tmp = _make_temp_dir()
    try:
        store = _MemoryStore()
        command_recorder = ShadowCommandRecorder(service, path=tmp / "commands.jsonl")
        count = record_message_events(
            [
                {
                    "msg_id": "m1",
                    "chat_id": "friend_wxid",
                    "chat_type": "private",
                    "sender_id": "friend_wxid",
                    "sender_name": "朋友",
                    "content": "在吗",
                    "created_at": _dt().isoformat(),
                    "source": W,
                }
            ],
            platform=W,
            account_id=A.account_id,
            identity_service=service,
            store=store,
            command_recorder=command_recorder,
        )
        assert count == 1
        events = store.all()
        assert len(events) == 1
        assert events[0].event_type == EventType.QUESTION
        assert events[0].actor_person_id == "friend_wxid"
        assert events[0].metadata["principal"]["relation_to_bot"] == "ordinary"
        assert events[0].metadata["intent"] == "question"
        assert command_recorder.count() == 1
        assert (tmp / "commands.jsonl").exists()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_adapters_read_real_meta_repo() -> None:
    tmp = _make_temp_dir()
    db_path = tmp / "adapter_meta.db"
    meta = None
    try:
        meta = MetaRepositoryManager(str(db_path))
        run_migrations(str(db_path))
        owner = meta.people.resolve_identity(
            platform=W,
            account_id=A.account_id,
            identity_type="wxid",
            identity_value="owner_wxid",
            display_name="Owner",
        )
        friend = meta.people.resolve_identity(
            platform=W,
            account_id=A.account_id,
            identity_type="wxid",
            identity_value="friend_wxid",
            display_name="Friend",
        )
        assert owner is not None and friend is not None
        meta.people.upsert_membership(
            friend.person_id,
            platform=W,
            account_id=A.account_id,
            chat_id="group_1",
            group_nickname="朋友",
        )
        with meta.engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO relationships "
                    "(persona_id, target_wxid, relation_type, intimacy, notes, updated_at) "
                    "VALUES (1, 'friend_wxid', '朋友', 0.6, '', :updated_at)"
                ),
                {"updated_at": datetime.now()},
            )

        identities = load_person_identities(meta)
        assert identities[(W, "owner_wxid")] == owner.person_id
        assert identities[(W, "friend_wxid")] == friend.person_id

        memberships = load_group_memberships(meta)
        member_key = (W, f"group_1@{A.account_id}", friend.person_id)
        assert member_key in memberships
        assert memberships[member_key][0].covers(datetime.now(timezone.utc))

        relationships = load_relationships(meta, identities=identities)
        rel_key = (W, A.account_id, friend.person_id)
        assert rel_key in relationships
        assert relationships[rel_key].level == RelationLevel.ORDINARY

        fake_ac = SimpleNamespace(
            platform=W,
            account_id=A.account_id,
            role="yinyue",
            owner_wxids=["owner_wxid"],
        )
        fake_cfg = SimpleNamespace(
            all_account_configs=lambda: [fake_ac],
            wechat=SimpleNamespace(get_owner_wxids=lambda: []),
        )
        owner_states = load_owner_relationships(
            identities=identities, config_obj=fake_cfg
        )
        assert owner_states[(W, A.account_id, owner.person_id)].level == RelationLevel.OWNER

        service = build_identity_service(meta_repo=meta, config_obj=fake_cfg)
        owner_principal = service.resolve_principal(
            platform=W,
            bot_account_id=A.account_id,
            chat_id="owner_wxid",
            chat_type="private",
            sender_id="owner_wxid",
            sender_name="Owner",
            occurred_at=datetime.now(timezone.utc),
        )
        assert owner_principal.is_owner
        assert owner_principal.has(Capability.VIEW_RAW_THOUGHT)
    finally:
        if meta is not None:
            try:
                meta.engine.dispose()
            except Exception:
                pass
        for suffix in ("", "-wal", "-shm"):
            try:
                (tmp / f"adapter_meta.db{suffix}").unlink(missing_ok=True)
            except PermissionError:
                pass
        shutil.rmtree(tmp, ignore_errors=True)


def test_shadow_reply_action_thought_helpers() -> None:
    tmp = _make_temp_dir()
    try:
        store = _MemoryStore()
        reply = record_reply_event(
            content="在的",
            platform=W,
            bot_account_id=A.account_id,
            chat_id="friend_wxid",
            chat_type="private",
            person_id="p_friend",
            store=store,
        )
        action = record_action_event(
            action="search_weather",
            action_input={"city": "上海"},
            output="晴",
            ok=True,
            platform=W,
            bot_account_id=A.account_id,
            chat_id="friend_wxid",
            chat_type="private",
            person_id="p_friend",
            store=store,
        )
        thought = record_thought_event(
            content="我先查一下天气再回答",
            platform=W,
            bot_account_id=A.account_id,
            chat_id="friend_wxid",
            chat_type="private",
            person_id="p_friend",
            store=store,
        )
        assert reply.event_type == EventType.REPLY
        assert action.event_type == EventType.ACTION
        assert thought.sensitivity == Sensitivity.OWNER_CHAIN_OF_THOUGHT

        owner = PrincipalContext(
            platform=W,
            bot_account_id=A.account_id,
            chat_id="owner_wxid",
            chat_type=ChatType.PRIVATE,
            person_id="p_master",
            display_name="Owner",
            relation_to_bot=RelationLevel.OWNER,
            capabilities=frozenset(
                {Capability.VIEW_RAW_THOUGHT, Capability.VIEW_OWNER_PRIVATE_MEMORY}
            ),
        )
        friend = PrincipalContext(
            platform=W,
            bot_account_id=A.account_id,
            chat_id="friend_wxid",
            chat_type=ChatType.PRIVATE,
            person_id="p_friend",
            display_name="Friend",
            relation_to_bot=RelationLevel.ORDINARY,
            capabilities=frozenset(),
        )
        visible_owner = store.query_visible(owner)
        visible_friend = store.query_visible(friend)
        assert reply in visible_owner and action in visible_owner and thought in visible_owner
        assert reply in visible_friend
        assert action not in visible_friend
        assert thought not in visible_friend
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_command_intent_classifier() -> None:
    from core.terminal.command_intent import classify_event_type, classify_intent

    assert classify_event_type("帮我给张三发个消息") == EventType.COMMAND
    assert classify_event_type("麻烦你帮我查一下天气") == EventType.COMMAND
    assert classify_event_type("能不能帮我发一下") == EventType.COMMAND
    assert classify_event_type("今天天气怎么样") == EventType.QUESTION
    assert classify_event_type("你好呀") == EventType.MESSAGE
    result = classify_intent("提醒我明天开会")
    assert result.event_type == EventType.COMMAND
    assert "提醒我" in result.reasons


def test_command_shadow_recorder() -> None:
    service = _owner_service()
    tmp = _make_temp_dir()
    try:
        path = tmp / "commands.jsonl"
        recorder = ShadowCommandRecorder(service, path=path)
        owner = _private_principal(service, raw_id="master_wxid", name="主人")
        friend = _private_principal(service, raw_id="friend_wxid", name="朋友")

        command = recorder.record_from_message(
            {
                "msg_id": "m1",
                "chat_id": "master_wxid",
                "chat_type": "private",
                "sender_id": "master_wxid",
                "content": "帮我给张三发个消息",
            },
            platform=W,
            account_id=A.account_id,
            principal=owner,
        )
        question = recorder.record_from_message(
            {
                "msg_id": "m2",
                "chat_id": "friend_wxid",
                "chat_type": "private",
                "sender_id": "friend_wxid",
                "content": "今天天气怎么样",
            },
            platform=W,
            account_id=A.account_id,
            principal=friend,
        )
        chitchat = recorder.record_from_message(
            {
                "msg_id": "m3",
                "chat_id": "friend_wxid",
                "chat_type": "private",
                "sender_id": "friend_wxid",
                "content": "你好呀",
            },
            platform=W,
            account_id=A.account_id,
            principal=friend,
        )
        assert command is not None
        assert command.issuer_role == "master"
        assert command.action_type == "command"
        assert question is not None
        assert question.issuer_role == "ordinary"
        assert question.action_type == "question"
        assert chitchat is None
        assert path.exists()
        lines = path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 2
        loaded = ShadowCommandRecorder.load_jsonl(path)
        assert len(loaded) == 2
        assert loaded[0].command_id == command.command_id
        reloaded_ledger = CommandLedger(service)
        assert reloaded_ledger.load_records(loaded) == 2
        assert len(reloaded_ledger.query_commands(owner)) == 2
        assert len(recorder.ledger.query_commands(owner)) == 2
        assert {c.command_id for c in recorder.ledger.query_commands(friend)} == {
            question.command_id
        }
        rendered = recorder.ledger.render_query(owner)
        assert "帮我给张三发个消息" in rendered
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    tests = [
        test_link_graph_transitive_and_reversible,
        test_effective_relationship_uses_closer_link,
        test_principal_owner_ordinary_group_and_thought_acl,
        test_event_store_visibility_and_persistence,
        test_command_visibility_by_role_and_membership_period,
        test_group_query_switch_denies_non_owner_but_not_owner,
        test_shadow_message_recording_is_read_only,
        test_adapters_read_real_meta_repo,
        test_shadow_reply_action_thought_helpers,
        test_command_intent_classifier,
        test_command_shadow_recorder,
    ]
    failed = 0
    for test in tests:
        try:
            test()
            print(f"PASS {test.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"FAIL {test.__name__}: {exc!r}")
    print(f"{len(tests) - failed}/{len(tests)} tests passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
