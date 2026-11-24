"""Phase 2 MemoryGateway tests: bounded recall + PrincipalContext filtering.

Run directly:
    python tests/test_memory_gateway.py
"""

from __future__ import annotations

import os
import shutil
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import _tempdirs  # noqa: E402,F401  临时目录纪律（见 tests/_tempdirs.py）：SQLite 临时文件走它

ROOT = Path(__file__).resolve().parent.parent
_TEST_TMP_ROOT = ROOT / "data" / "_memory_gateway_test"
UTC = timezone.utc
W = "wechat"


def _make_temp_dir() -> Path:
    _TEST_TMP_ROOT.mkdir(parents=True, exist_ok=True)
    path = _TEST_TMP_ROOT / f"case_{uuid.uuid4().hex}"
    path.mkdir(parents=True, exist_ok=False)
    return path


# 本文件测的是「旧行为」（低分召回照进上下文）。批1 灰度期间 .env 里
# YINYUE_FEATURE_MEMORY_GATE=1 会随 core.bootstrap 注入进程，导致这里
# 0.29 分的命中被闸门滤掉、断言 `[事件记忆` 失败。prompt/召回测试必须在
# 模块顶固定开关，与 test_context_assembler_equivalence / replay 脚本同规矩。
os.environ["YINYUE_FEATURE_MEMORY_GATE"] = "0"
os.environ["YINYUE_FEATURE_UNCERTAIN_VOICE"] = "0"


from core.terminal.event_model import Event, EventType, Sensitivity  # noqa: E402
from core.terminal.event_store_db import TerminalEventStore  # noqa: E402
from core.terminal.identity_service import IdentityService  # noqa: E402
from core.terminal.memory_gateway import (  # noqa: E402
    MemoryGateway,
    attach_terminal_memory,
    install_memory_gateway,
    reset_memory_gateway,
)
from core.terminal.principal import (  # noqa: E402
    Capability,
    ChatType,
    GroupMembershipPeriod,
    RelationLevel,
    RelationshipState,
    VisibilityScope,
)
from db.meta_repo import MetaRepositoryManager  # noqa: E402
from db.migrations import run_migrations  # noqa: E402


def _dt(day: int = 1, hour: int = 10) -> datetime:
    return datetime(2026, 1, day, hour, 0, 0, tzinfo=UTC)


def _identity() -> IdentityService:
    return IdentityService(
        relationships={
            (W, "bot_a", "p_owner"): RelationshipState(
                W, "bot_a", "p_owner", RelationLevel.OWNER, 1.0, "seed"
            ),
            (W, "bot_a", "p_friend"): RelationshipState(
                W, "bot_a", "p_friend", RelationLevel.ORDINARY, 0.4, "seed"
            ),
        },
        person_identities={
            (W, "owner_raw"): "p_owner",
            (W, "friend_raw"): "p_friend",
            (W, "member_raw"): "p_member",
        },
        group_memberships={
            (W, "g1@bot_a", "p_member"): [
                GroupMembershipPeriod(start_at=_dt(1) - timedelta(days=1))
            ],
        },
    )


def _build(tmp: Path):
    db_path = tmp / "memory.db"
    meta = MetaRepositoryManager(str(db_path))
    run_migrations(str(db_path))
    return meta, TerminalEventStore(meta.engine), db_path


def _cleanup(meta, db_path: Path) -> None:
    if meta is not None:
        try:
            meta.engine.dispose()
        except Exception:
            pass
    for suffix in ("", "-wal", "-shm"):
        try:
            db_path.with_name(db_path.name + suffix).unlink(missing_ok=True)
        except PermissionError:
            pass


def _seed_events(store: TerminalEventStore) -> None:
    store.append(Event.new(
        event_type=EventType.MESSAGE,
        occurred_at=_dt(1, 9),
        platform=W,
        bot_account_id="bot_a",
        chat_id="friend_raw",
        chat_type="private",
        actor_person_id="p_friend",
        content="明天上海会不会下雨",
        visibility_scope=frozenset({VisibilityScope.ISSUER_ONLY}),
        metadata={"sender_name": "朋友", "group_id": ""},
    ))
    store.append(Event.new(
        event_type=EventType.REPLY,
        occurred_at=_dt(1, 9),
        platform=W,
        bot_account_id="bot_a",
        chat_id="friend_raw",
        chat_type="private",
        actor_person_id="p_owner",
        content="我帮你看一下天气预报",
        visibility_scope=frozenset({VisibilityScope.ISSUER_ONLY}),
        metadata={"sender_name": "", "group_id": ""},
    ))
    store.append(Event.new(
        event_type=EventType.MESSAGE,
        occurred_at=_dt(1, 10),
        platform=W,
        bot_account_id="bot_a",
        chat_id="g1",
        chat_type="group",
        actor_person_id="p_member",
        content="群里在讨论周末的聚会安排",
        visibility_scope=frozenset({VisibilityScope.GROUP_MEMBERS}),
        metadata={"sender_name": "群友", "group_id": "g1"},
    ))
    store.append(Event.new(
        event_type=EventType.THOUGHT,
        occurred_at=_dt(1, 11),
        platform=W,
        bot_account_id="bot_a",
        chat_id="friend_raw",
        chat_type="private",
        actor_person_id="p_owner",
        content="原始思维链不能被记忆召回",
        sensitivity=Sensitivity.OWNER_CHAIN_OF_THOUGHT,
        visibility_scope=frozenset({VisibilityScope.OWNER_ONLY}),
        metadata={"group_id": ""},
    ))


def _principal(service: IdentityService, raw_id: str, *, group: bool = False):
    chat_id = "g1" if group else raw_id
    return service.resolve_principal(
        platform=W,
        bot_account_id="bot_a",
        chat_id=chat_id,
        chat_type="group" if group else "private",
        sender_id=raw_id,
        sender_name="群友" if group else "朋友",
        occurred_at=_dt(2),
    )


def test_bounded_recall_owner_and_thought_exclusion() -> None:
    tmp = _make_temp_dir()
    meta = None
    db_path = tmp / "memory.db"
    try:
        meta, store, db_path = _build(tmp)
        identity = _identity()
        _seed_events(store)
        gateway = MemoryGateway(event_store=store, identity_service=identity)
        owner = _principal(identity, "owner_raw")
        hits = gateway.recall(owner, query="天气")
        contents = [hit.content for hit in hits]
        assert any("下雨" in content for content in contents)
        assert all("思维链" not in content for content in contents)
        # Owner can see all tenants, but thought is never memory.
        assert len(hits) == 3
        assert all(hit.source_event_ids for hit in hits)
    finally:
        _cleanup(meta, db_path)
        shutil.rmtree(tmp, ignore_errors=True)


def test_ordinary_contact_only_recalls_own_scope() -> None:
    tmp = _make_temp_dir()
    meta = None
    db_path = tmp / "memory.db"
    try:
        meta, store, db_path = _build(tmp)
        identity = _identity()
        _seed_events(store)
        gateway = MemoryGateway(event_store=store, identity_service=identity)
        friend = _principal(identity, "friend_raw")
        hits = gateway.recall(friend)
        assert hits
        assert all("下雨" in hit.content or "天气预报" in hit.content for hit in hits)
        assert all("聚会" not in hit.content for hit in hits)
        assert all("思维链" not in hit.content for hit in hits)
    finally:
        _cleanup(meta, db_path)
        shutil.rmtree(tmp, ignore_errors=True)


def test_group_member_recall_respects_membership_scope() -> None:
    tmp = _make_temp_dir()
    meta = None
    db_path = tmp / "memory.db"
    try:
        meta, store, db_path = _build(tmp)
        identity = _identity()
        _seed_events(store)
        gateway = MemoryGateway(event_store=store, identity_service=identity)
        member = _principal(identity, "member_raw", group=True)
        assert member.has(Capability.QUERY_GROUP_COMMANDS)
        hits = gateway.recall(member)
        assert [hit.content for hit in hits] == ["群里在讨论周末的聚会安排"]
        # A group member never gets private ordinary hits.
        assert all("下雨" not in hit.content for hit in hits)
    finally:
        _cleanup(meta, db_path)
        shutil.rmtree(tmp, ignore_errors=True)


def test_unknown_identity_gets_no_recall() -> None:
    tmp = _make_temp_dir()
    meta = None
    db_path = tmp / "memory.db"
    try:
        meta, store, db_path = _build(tmp)
        identity = _identity()
        _seed_events(store)
        gateway = MemoryGateway(event_store=store, identity_service=identity)
        stranger = _principal(identity, "stranger_raw")
        assert not stranger.memory_scopes
        assert gateway.recall(stranger) == []
    finally:
        _cleanup(meta, db_path)
        shutil.rmtree(tmp, ignore_errors=True)


def test_query_relevance_orders_hits() -> None:
    tmp = _make_temp_dir()
    meta = None
    db_path = tmp / "memory.db"
    try:
        meta, store, db_path = _build(tmp)
        identity = _identity()
        _seed_events(store)
        gateway = MemoryGateway(event_store=store, identity_service=identity)
        owner = _principal(identity, "owner_raw")
        hits = gateway.recall(owner, query="天气预报")
        assert hits[0].content == "我帮你看一下天气预报"
    finally:
        _cleanup(meta, db_path)
        shutil.rmtree(tmp, ignore_errors=True)


def test_attach_to_context_and_feature_gate() -> None:
    tmp = _make_temp_dir()
    meta = None
    db_path = tmp / "memory.db"
    try:
        meta, store, db_path = _build(tmp)
        identity = _identity()
        _seed_events(store)
        gateway = MemoryGateway(event_store=store, identity_service=identity)
        owner = _principal(identity, "owner_raw")
        context = SimpleNamespace(
            extra={
                "platform": W,
                "account_id": "bot_a",
                "chat_id": "owner_raw",
                "sender_id": "owner_raw",
                "sender_name": "老板",
                "user_message": "上次天气的事",
            },
            principal=owner,
            account_context=None,
        )
        block = attach_terminal_memory(context, gateway=gateway, query="天气")
        assert "[事件记忆" in block
        assert context.extra["terminal_memory_count"] >= 1
        assert context.extra["terminal_memory_sources"]

        install_memory_gateway(gateway)
        try:
            context2 = SimpleNamespace(
                extra={
                    "platform": W,
                    "account_id": "bot_a",
                    "chat_id": "owner_raw",
                    "sender_id": "owner_raw",
                    "sender_name": "老板",
                    "user_message": "天气",
                },
                principal=owner,
                account_context=None,
            )
            assert "[事件记忆" in attach_terminal_memory(context2)
        finally:
            reset_memory_gateway()
    finally:
        _cleanup(meta, db_path)
        shutil.rmtree(tmp, ignore_errors=True)


def test_group_recall_never_crosses_groups() -> None:
    tmp = _make_temp_dir()
    meta = None
    db_path = tmp / "memory.db"
    try:
        meta, store, db_path = _build(tmp)
        identity = _identity()
        _seed_events(store)
        gateway = MemoryGateway(event_store=store, identity_service=identity)
        store.append(Event.new(
            event_type=EventType.MESSAGE,
            occurred_at=_dt(2, 9),
            platform=W,
            bot_account_id="bot_a",
            chat_id="g2",
            chat_type="group",
            actor_person_id="p_other",
            content="二组的秘密：周五去露营",
            visibility_scope=frozenset({VisibilityScope.GROUP_MEMBERS}),
            metadata={"sender_name": "二组群友", "group_id": "g2"},
        ))
        member = _principal(identity, "member_raw", group=True)
        assert all(
            "露营" not in hit.content
            for hit in gateway.recall(member, query="露营 聚会")
        )
        owner_in_group = _principal(identity, "owner_raw", group=True)
        assert all(
            "露营" not in hit.content
            for hit in gateway.recall(owner_in_group, query="露营 聚会")
        )
        # same isolation on the projection path (the one live prompts use)
        from core.terminal.memory_projection import MemoryProjector
        from db.repo_memory_projections import MemoryProjectionRepository

        proj_repo = MemoryProjectionRepository(meta.engine)
        projector = MemoryProjector(store, proj_repo)
        for event in store.all():
            item = projector.project_event(event)
            if item is not None:
                proj_repo.upsert(item)
        gateway_p = MemoryGateway(
            event_store=store,
            projection_repository=proj_repo,
            identity_service=identity,
        )
        for principal in (member, owner_in_group):
            contents = [hit.content for hit in gateway_p.recall(principal, query="露营 聚会")]
            assert all("露营" not in c for c in contents), contents
    finally:
        _cleanup(meta, db_path)
        shutil.rmtree(tmp, ignore_errors=True)


def test_revoke_rendered_as_system_line() -> None:
    """批B：revokemsg 不原样进 [事件记忆]，渲染成一行系统通知。"""
    from core.terminal.memory_gateway import MemoryGateway, MemoryHit

    raw = (
        '<?xml version="1.0"?><sysmsg type="revokemsg"><revokemsg>'
        '<content>"P6" 撤回了一条消息</content></revokemsg></sysmsg>'
    )
    hit = MemoryHit(
        memory_id="m1",
        event_type="episode",
        content=raw,
        speaker="unknown",
        occurred_at=datetime(2026, 9, 24, 18, 51),
        chat_id="c1",
        chat_type="group",
        visibility_scope=("public",),
        source_event_ids=("e1",),
        score=0.5,
    )
    out = MemoryGateway.render([hit])
    assert "sysmsg" not in out, out
    assert "- [系统] [09-24 18:51] P6 撤回了一条消息" in out, out

def main() -> int:
    tests = [
        test_bounded_recall_owner_and_thought_exclusion,
        test_group_recall_never_crosses_groups,
        test_ordinary_contact_only_recalls_own_scope,
        test_group_member_recall_respects_membership_scope,
        test_unknown_identity_gets_no_recall,
        test_query_relevance_orders_hits,
        test_attach_to_context_and_feature_gate,
        test_revoke_rendered_as_system_line,
    ]
    failed = 0
    for test in tests:
        try:
            test()
            print(f"PASS {test.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            import traceback

            print(f"FAIL {test.__name__}: {exc!r}")
            traceback.print_exc()
    print(f"{len(tests) - failed}/{len(tests)} tests passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
