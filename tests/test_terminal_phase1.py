"""Phase 1 deterministic authority / capability tests.

Run directly:
    python tests/test_terminal_phase1.py
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

# ---------------------------------------------------------------------------
# 数据库隔离（照 tests/test_event_chain_links.py / test_command_service_db.py）：
# 本文件会经 core.bootstrap.init_singletons() 触发 init_meta_db + run_migrations
# （bootstrap 兜底、runtime 访问器等），不隔离就把 pending 迁移落到**真实**
# meta / account 库。2026-09-29 实测：Migration 51 被本套测试落到了生产库
# （data/yinyue_meta.db + 4 个账号库，applied_at 14:58:19Z）。必须在 import core.*
# 之前设好：PathSettings 在 core.config.settings **首次**导入时就取值。
# ---------------------------------------------------------------------------
_ISO_ROOT = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "data", "_test_terminal_phase1_iso"
)
os.makedirs(_ISO_ROOT, exist_ok=True)
os.environ["META_DB_PATH"] = os.path.join(_ISO_ROOT, "meta.db")
os.environ["PLATFORMS_DIR"] = os.path.join(_ISO_ROOT, "platforms")
os.environ["DATA_DIR"] = os.path.join(_ISO_ROOT, "data")

from core.config.settings import config as _config  # noqa: E402

# 守卫：env 只在 core.config.settings 首次导入前设好才有效，否则真实路径已经
# 固化进 config。这里硬失败，绝不静默把迁移/写操作落到生产库。
assert os.path.realpath(_config.paths.meta_db_path) != os.path.realpath(
    os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "data", "yinyue_meta.db"
    )
), f"测试数据库隔离失效：meta_db_path={_config.paths.meta_db_path}"


ROOT = Path(__file__).resolve().parent.parent
_TEST_TMP_ROOT = ROOT / "data" / "_terminal_phase1_test"
UTC = timezone.utc
W = "wechat"


def _make_temp_dir() -> Path:
    _TEST_TMP_ROOT.mkdir(parents=True, exist_ok=True)
    path = _TEST_TMP_ROOT / f"case_{uuid.uuid4().hex}"
    path.mkdir(parents=True, exist_ok=False)
    return path


from core.terminal.authority import (  # noqa: E402
    AuthorityService,
    active_context_has_capability,
    active_context_is_owner,
    active_context_principal,
    active_is_owner,
    active_principal,
    attach_principal,
    context_principal,
    relation_label,
    reset_authority_service,
    unknown_principal,
)
from core.terminal.command_ledger import CommandLedger  # noqa: E402
from core.terminal.identity_service import IdentityService  # noqa: E402
from core.terminal.link_graph import AccountLinkGraph  # noqa: E402
from core.terminal.principal import (  # noqa: E402
    AccountRef,
    Capability,
    CapabilityDenied,
    ChatType,
    GroupMembershipPeriod,
    PrincipalContext,
    RelationLevel,
    RelationshipState,
    VisibilityScope,
)
from core.speech.outbound import OutboundGateway  # noqa: E402


def _dt(day: int = 1, hour: int = 12) -> datetime:
    return datetime(2026, 1, day, hour, 0, 0, tzinfo=UTC)


def _service(
    *,
    account: str = "bot_a",
    owner_raw: str = "owner_raw",
    friend_raw: str = "friend_raw",
) -> IdentityService:
    return IdentityService(
        relationships={
            (W, account, "p_owner"): RelationshipState(
                W, account, "p_owner", RelationLevel.OWNER, 1.0, "seed"
            ),
            (W, account, "p_friend"): RelationshipState(
                W, account, "p_friend", RelationLevel.ORDINARY, 0.4, "seed"
            ),
        },
        person_identities={
            (W, owner_raw): "p_owner",
            (W, friend_raw): "p_friend",
        },
        group_memberships={
            (W, "g1@" + account, "p_member"): [
                GroupMembershipPeriod(
                    start_at=_dt(1),
                    end_at=_dt(2),
                    epoch=1,
                ),
                GroupMembershipPeriod(start_at=_dt(3), epoch=2),
            ],
        },
        person_identity_scopes={
            (W, account, "p_member_raw"): "p_member",
        },
    )


# ---------------------------------------------------------------------------
# Principal / capability matrix
# ---------------------------------------------------------------------------
def test_unknown_identity_has_zero_capabilities() -> None:
    service = _service()
    principal = service.resolve_principal(
        platform=W,
        bot_account_id="bot_a",
        chat_id="stranger_raw",
        chat_type="private",
        sender_id="stranger_raw",
        sender_name="主人",  # display name must never grant authority
        occurred_at=_dt(),
    )
    assert principal.relation_to_bot == RelationLevel.UNKNOWN
    assert principal.capabilities == frozenset()
    assert principal.memory_scopes == frozenset()
    assert not principal.is_owner
    try:
        principal.require(Capability.VIEW_RAW_THOUGHT)
        raise AssertionError("expected CapabilityDenied")
    except CapabilityDenied:
        pass


def test_known_ordinary_private_only_sees_issuer_scope() -> None:
    service = _service()
    principal = service.resolve_principal(
        platform=W,
        bot_account_id="bot_a",
        chat_id="friend_raw",
        chat_type="private",
        sender_id="friend_raw",
        sender_name="朋友",
        occurred_at=_dt(),
    )
    assert principal.relation_to_bot == RelationLevel.ORDINARY
    assert principal.memory_scopes == frozenset({VisibilityScope.ISSUER_ONLY})
    assert principal.has(Capability.VIEW_COMPONENT_MEMORY)
    assert not principal.has(Capability.VIEW_OWNER_PRIVATE_MEMORY)
    assert not principal.has(Capability.SEND_AS_OWNER)


def test_owner_capabilities_and_group_owner_no_private_memory() -> None:
    service = _service()
    owner = service.resolve_principal(
        platform=W,
        bot_account_id="bot_a",
        chat_id="owner_raw",
        chat_type="private",
        sender_id="owner_raw",
        sender_name="老板",
        occurred_at=_dt(),
    )
    for capability in (
        Capability.VIEW_RAW_THOUGHT,
        Capability.VIEW_OWNER_PRIVATE_MEMORY,
        Capability.VIEW_ALL_COMMANDS,
        Capability.SEND_AS_OWNER,
        Capability.EXECUTE_OWNER_COMMAND,
    ):
        assert owner.has(capability), capability

    group_owner = service.resolve_principal(
        platform=W,
        bot_account_id="bot_a",
        chat_id="g1",
        chat_type="group",
        sender_id="owner_raw",
        sender_name="老板",
        occurred_at=_dt(4),
    )
    assert group_owner.is_owner
    assert group_owner.has(Capability.VIEW_RAW_THOUGHT)
    assert group_owner.has(Capability.QUERY_GROUP_COMMANDS)
    assert not group_owner.has(Capability.VIEW_OWNER_PRIVATE_MEMORY)
    assert group_owner.memory_scopes == frozenset({VisibilityScope.GROUP_MEMBERS})


def test_membership_epochs_respect_leave_and_rejoin() -> None:
    service = _service()
    assert service.is_member_at(W, "g1", "p_member", _dt(1, 18), account_id="bot_a")
    assert not service.is_member_at(W, "g1", "p_member", _dt(2, 12), account_id="bot_a")
    assert not service.is_member_at(W, "g1", "p_member", _dt(3, 0), account_id="bot_a")
    assert service.is_member_at(W, "g1", "p_member", _dt(3, 12), account_id="bot_a")
    # A sender with no row is a non-member once membership data exists.
    assert not service.is_member_at(W, "g1", "p_unknown", _dt(1, 18), account_id="bot_a")


def test_account_scoped_identity_does_not_cross_accounts() -> None:
    service = IdentityService(
        person_identity_scopes={
            (W, "bot_a", "shared_raw"): "person_a",
            (W, "bot_b", "shared_raw"): "person_b",
        }
    )
    a = service.resolve_person_id(W, "shared_raw", "bot_a")
    b = service.resolve_person_id(W, "shared_raw", "bot_b")
    assert a == "person_a" and b == "person_b"
    assert a != b


# ---------------------------------------------------------------------------
# Authority gateway
# ---------------------------------------------------------------------------
def _authority_context(service, *, sender_id: str, sender_name: str = ""):
    context = SimpleNamespace(
        extra={
            "platform": W,
            "account_id": "bot_a",
            "chat_id": sender_id,
            "sender_id": sender_id,
            "sender_name": sender_name,
        },
        account_context=None,
        principal=None,
    )
    principal = active_context_principal(context, service=service)
    return context, principal


def test_authority_resolves_stable_id_and_never_display_name() -> None:
    service = AuthorityService(_service())
    owner = active_principal(
        platform=W,
        account_id="bot_a",
        chat_id="owner_raw",
        sender_id="owner_raw",
        sender_name="随便什么名字",
        service=service,
    )
    assert owner is not None and owner.is_owner

    stranger = active_principal(
        platform=W,
        account_id="bot_a",
        chat_id="stranger_raw",
        sender_id="stranger_raw",
        sender_name="主人",  # impersonation attempt
        service=service,
    )
    assert stranger is not None and not stranger.is_owner
    assert active_is_owner(
        platform=W,
        account_id="bot_a",
        chat_id="stranger_raw",
        sender_id="stranger_raw",
        sender_name="老板",
        service=service,
    ) is False


def test_context_principal_is_attached_and_consumed() -> None:
    service = AuthorityService(_service())
    context, principal = _authority_context(
        service, sender_id="owner_raw", sender_name="老板"
    )
    assert principal is not None and principal.is_owner
    assert context_principal(context) is principal
    assert active_context_is_owner(context) is True
    assert (
        active_context_has_capability(context, Capability.SEND_AS_OWNER) is True
    )

    stranger_context, stranger = _authority_context(
        service, sender_id="stranger_raw", sender_name="老板"
    )
    assert stranger is not None and not stranger.is_owner
    assert active_context_is_owner(stranger_context) is False
    assert (
        active_context_has_capability(stranger_context, Capability.SEND_AS_OWNER)
        is False
    )


def test_missing_service_fails_closed_to_unknown() -> None:
    # No service and no attached principal -> UNKNOWN, never a legacy owner-id
    # recovery.  Authority is always active, so the answer is False, not None.
    assert (
        active_is_owner(
            platform=W,
            account_id="bot_a",
            chat_id="x",
            sender_id="owner_raw",
        )
        is False
    )
    unknown = unknown_principal(
        platform=W,
        account_id="bot_a",
        chat_id="x",
        sender_id="owner_raw",
        sender_name="老板",
    )
    assert not unknown.is_owner
    assert unknown.capabilities == frozenset()
    reset_authority_service()


def test_command_ledger_honours_view_all_capability() -> None:
    service = _service()
    ledger = CommandLedger(service)
    ledger.record_command(
        created_at=_dt(),
        issuer_person_id="p_friend",
        issuer_account_id="friend_raw",
        issuer_role="ordinary",
        origin_platform=W,
        origin_bot_account_id="bot_a",
        origin_chat_id="friend_raw",
        origin_chat_type="private",
        raw_request="帮我查天气",
    )
    requester = PrincipalContext(
        platform=W,
        bot_account_id="bot_a",
        chat_id="office",
        chat_type=ChatType.PRIVATE,
        person_id="p_office",
        display_name="办公号",
        relation_to_bot=RelationLevel.ORDINARY,
        capabilities=frozenset({Capability.VIEW_ALL_COMMANDS}),
        occurred_at=_dt(),
    )
    assert len(ledger.query_commands(requester)) == 1
    assert "帮我查天气" in ledger.render_query(requester)


# ---------------------------------------------------------------------------
# Tool / outbound gates
# ---------------------------------------------------------------------------
def test_search_tool_owner_check_consumes_principal() -> None:
    from agents.tools.search import SearchMessagesTool

    tool = SearchMessagesTool(repo=None)
    owner = PrincipalContext(
        platform=W,
        bot_account_id="bot_a",
        chat_id="owner_raw",
        chat_type=ChatType.PRIVATE,
        person_id="p_owner",
        display_name="老板",
        relation_to_bot=RelationLevel.OWNER,
        capabilities=frozenset({Capability.VIEW_ALL_COMMANDS}),
        occurred_at=_dt(),
    )
    owner_ctx = SimpleNamespace(extra={}, account_context=None, principal=owner)
    tool.set_context(owner_ctx)
    assert tool._is_owner(W, "bot_a", "p_owner") is True

    friend = PrincipalContext(
        platform=W,
        bot_account_id="bot_a",
        chat_id="friend_raw",
        chat_type=ChatType.PRIVATE,
        person_id="p_friend",
        display_name="朋友",
        relation_to_bot=RelationLevel.ORDINARY,
        capabilities=frozenset(),
        occurred_at=_dt(),
    )
    friend_ctx = SimpleNamespace(extra={}, account_context=None, principal=friend)
    tool.set_context(friend_ctx)
    assert tool._is_owner(W, "bot_a", "p_friend") is False


def test_outbound_owner_account_requires_capability() -> None:
    draft = SimpleNamespace(account_choice="owner")
    owner = PrincipalContext(
        platform=W,
        bot_account_id="bot_a",
        chat_id="owner_raw",
        chat_type=ChatType.PRIVATE,
        person_id="p_owner",
        display_name="老板",
        relation_to_bot=RelationLevel.OWNER,
        capabilities=frozenset({Capability.SEND_AS_OWNER}),
        occurred_at=_dt(),
    )
    owner_ctx = SimpleNamespace(extra={}, principal=owner, account_context=None)
    assert OutboundGateway._owner_account_error(draft, owner_ctx) == ""

    friend = PrincipalContext(
        platform=W,
        bot_account_id="bot_a",
        chat_id="friend_raw",
        chat_type=ChatType.PRIVATE,
        person_id="p_friend",
        display_name="朋友",
        relation_to_bot=RelationLevel.ORDINARY,
        capabilities=frozenset(),
        occurred_at=_dt(),
    )
    friend_ctx = SimpleNamespace(extra={}, principal=friend, account_context=None)
    error = OutboundGateway._owner_account_error(draft, friend_ctx)
    assert error and "权限" in error
    # Missing context fails closed too: no deterministic principal can prove
    # SEND_AS_OWNER, so the legacy config path must not be used.
    missing_context_error = OutboundGateway._owner_account_error(draft, None)
    assert missing_context_error and "权限" in missing_context_error


def test_relation_label_for_send_policy() -> None:
    owner = _service().resolve_principal(
        platform=W,
        bot_account_id="bot_a",
        chat_id="owner_raw",
        sender_id="owner_raw",
        sender_name="老板",
        occurred_at=_dt(),
    )
    assert relation_label(owner) == "亲密"
    group = _service().resolve_principal(
        platform=W,
        bot_account_id="bot_a",
        chat_id="g1",
        chat_type="group",
        sender_id="owner_raw",
        occurred_at=_dt(4),
    )
    assert relation_label(group) == "群聊"


# ---------------------------------------------------------------------------
# Real meta DB adapters/migrations
# ---------------------------------------------------------------------------
def test_meta_adapters_load_epochs_and_link_edges() -> None:
    tmp = _make_temp_dir()
    meta = None
    try:
        from sqlalchemy import text

        from db.meta_models import AccountLinkEdge, GroupMembershipPeriod as PeriodRow
        from db.meta_repo import MetaRepositoryManager
        from db.migrations import run_migrations
        from core.terminal.adapters import (
            build_identity_service,
            load_account_link_edges,
            load_group_membership_periods,
            load_scoped_person_identities,
        )

        db_path = tmp / "phase1_meta.db"
        meta = MetaRepositoryManager(str(db_path))
        run_migrations(str(db_path))

        # Two active edges -> transitive component; one revoked edge ignored.
        with meta.engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO account_link_edges "
                "(platform_a, account_a, platform_b, account_b, created_by, revision) "
                "VALUES (:pa, :aa, :pb, :ab, 'owner', 1)"
            ), {"pa": W, "aa": "bot_a", "pb": W, "ab": "bot_b"})
            conn.execute(text(
                "INSERT INTO account_link_edges "
                "(platform_a, account_a, platform_b, account_b, created_by, revision) "
                "VALUES (:pa, :aa, :pb, :ab, 'owner', 1)"
            ), {"pa": W, "aa": "bot_b", "pb": W, "ab": "bot_c"})
            conn.execute(text(
                "INSERT INTO account_link_edges "
                "(platform_a, account_a, platform_b, account_b, created_by, revision, revoked_at) "
                "VALUES (:pa, :aa, :pb, :ab, 'owner', 1, :revoked)"
            ), {"pa": W, "aa": "bot_c", "pb": W, "ab": "bot_d", "revoked": datetime.now()})

        # Explicit leave/re-join epochs.
        with meta.engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO group_membership_periods "
                "(person_id, platform, account_id, chat_id, joined_at, left_at, epoch, source) "
                "VALUES (:pid, :platform, :aid, 'g1', :joined, :left, 1, 'test')"
            ), {"pid": "p_member", "platform": W, "aid": "bot_a",
                "joined": _dt(1).replace(tzinfo=None), "left": _dt(2).replace(tzinfo=None)})
            conn.execute(text(
                "INSERT INTO group_membership_periods "
                "(person_id, platform, account_id, chat_id, joined_at, left_at, epoch, source) "
                "VALUES (:pid, :platform, :aid, 'g1', :joined, NULL, 2, 'test')"
            ), {"pid": "p_member", "platform": W, "aid": "bot_a",
                "joined": _dt(3).replace(tzinfo=None)})

        graph = AccountLinkGraph()
        graph = load_account_link_edges(meta, graph)
        a = AccountRef(W, "bot_a")
        b = AccountRef(W, "bot_b")
        c = AccountRef(W, "bot_c")
        d = AccountRef(W, "bot_d")
        assert graph.connected(a, b)
        assert graph.connected(a, c)
        assert not graph.connected(c, d)

        periods = load_group_membership_periods(meta)
        key = (W, "g1@bot_a", "p_member")
        assert key in periods and len(periods[key]) == 2

        # Strong identities keep working even when first observed in a group
        # (chat_id is only a scope for weak nickname identities).
        with meta.engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO person_identities "
                "(person_id, platform, account_id, identity_type, identity_value, "
                " chat_id, confidence, source, first_seen_at, last_seen_at) "
                "VALUES ('p_member', :platform, 'bot_a', 'wxid', 'wxid_member', "
                " 'g1', 0.95, 'test', :now, :now)"
            ), {"platform": W, "now": _dt(1).replace(tzinfo=None)})

        scoped = load_scoped_person_identities(meta)
        assert scoped.get((W, "bot_a", "wxid_member")) == "p_member"
        assert isinstance(scoped, dict)

        fake_ac = SimpleNamespace(
            platform=W,
            account_id="bot_a",
            role="yinyue",
            owner_wxids=[],
        )
        fake_cfg = SimpleNamespace(
            all_account_configs=lambda: [fake_ac],
            wechat=SimpleNamespace(get_owner_wxids=lambda: []),
        )
        service = build_identity_service(meta_repo=meta, config_obj=fake_cfg)
        assert service.link_graph.connected(a, b)
        assert service.link_graph.connected(a, c)
        assert service.resolve_person_id(W, "wxid_member", "bot_a") == "p_member"
        assert service.is_known_person(W, "p_member")
        assert service.is_member_at(W, "g1", "p_member", _dt(1, 18), account_id="bot_a")
        assert not service.is_member_at(W, "g1", "p_member", _dt(2, 12), account_id="bot_a")
        assert service.is_member_at(W, "g1", "p_member", _dt(3, 18), account_id="bot_a")
    finally:
        if meta is not None:
            try:
                meta.engine.dispose()
            except Exception:
                pass
        for suffix in ("", "-wal", "-shm"):
            try:
                (tmp / f"phase1_meta.db{suffix}").unlink(missing_ok=True)
            except PermissionError:
                pass
        shutil.rmtree(tmp, ignore_errors=True)


def test_installed_authority_service_is_always_used() -> None:
    from core.terminal.authority import (
        get_authority_service,
        install_authority_service,
    )

    service = AuthorityService(_service())
    install_authority_service(service)
    try:
        assert get_authority_service() is service
        assert active_is_owner(
            platform=W,
            account_id="bot_a",
            chat_id="owner_raw",
            sender_id="owner_raw",
            sender_name="任何名字",
        ) is True
        assert active_is_owner(
            platform=W,
            account_id="bot_a",
            chat_id="stranger_raw",
            sender_id="stranger_raw",
            sender_name="老板",
        ) is False
    finally:
        reset_authority_service()


def main() -> int:
    tests = [
        test_unknown_identity_has_zero_capabilities,
        test_known_ordinary_private_only_sees_issuer_scope,
        test_owner_capabilities_and_group_owner_no_private_memory,
        test_membership_epochs_respect_leave_and_rejoin,
        test_account_scoped_identity_does_not_cross_accounts,
        test_authority_resolves_stable_id_and_never_display_name,
        test_context_principal_is_attached_and_consumed,
        test_missing_service_fails_closed_to_unknown,
        test_command_ledger_honours_view_all_capability,
        test_search_tool_owner_check_consumes_principal,
        test_outbound_owner_account_requires_capability,
        test_relation_label_for_send_policy,
        test_meta_adapters_load_epochs_and_link_edges,
        test_installed_authority_service_is_always_used,
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
