"""Phase 2 DB-backed CommandService tests.

Run directly:
    python tests/test_command_service_db.py
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
_TEST_TMP_ROOT = ROOT / "data" / "_command_service_test"
UTC = timezone.utc
W = "wechat"

# 数据库隔离（照 tests/test_event_chain_links.py）：`_trace_terminal_shadow` 会经
# `core.ingestion.ingestors._trace_group_membership` → `core.bootstrap.get_meta_repo()`
# → `init_meta_db + run_migrations`，不隔离就把 pending 迁移落到**真实**
# `data/yinyue_meta.db` / 配置里的 account 库上（2026-09-29 事故）。
# 必须在 import core.* 之前设好：PathSettings 在 core.config.settings 导入时就取值。
_ISO_ROOT = (_TEST_TMP_ROOT / "_iso").resolve()
_ISO_ROOT.mkdir(parents=True, exist_ok=True)
os.environ["META_DB_PATH"] = str(_ISO_ROOT / "meta.db")
os.environ["PLATFORMS_DIR"] = str(_ISO_ROOT / "platforms")
os.environ["DATA_DIR"] = str(_ISO_ROOT / "data")
# 守卫：env 只在 core.config.settings **首次**导入前设好才有效，要是别的模块先导入
# 就把真实路径固化进 config 了。这里硬失败，绝不静默把迁移落到生产库。
from core.config.settings import config as _config  # noqa: E402

assert Path(_config.paths.meta_db_path).resolve() != (
    ROOT / "data" / "yinyue_meta.db"
).resolve(), f"测试数据库隔离失效：meta_db_path={_config.paths.meta_db_path}"


def _make_temp_dir() -> Path:
    _TEST_TMP_ROOT.mkdir(parents=True, exist_ok=True)
    path = _TEST_TMP_ROOT / f"case_{uuid.uuid4().hex}"
    path.mkdir(parents=True, exist_ok=False)
    return path


from core.terminal.command_ledger import (  # noqa: E402
    CommandStatus,
)
from core.terminal.command_service import (  # noqa: E402
    CommandService,
    command_recorder_for_service,
)
from core.terminal.identity_service import IdentityService  # noqa: E402
from core.terminal.principal import (  # noqa: E402
    Capability,
    ChatType,
    GroupMembershipPeriod,
    PrincipalContext,
    RelationLevel,
    RelationshipState,
)
from db.meta_repo import MetaRepositoryManager  # noqa: E402
from db.migrations import run_migrations  # noqa: E402
from db.repo_commands import CommandRepository  # noqa: E402


def _dt(day: int = 1, hour: int = 10) -> datetime:
    return datetime(2026, 1, day, hour, 0, 0, tzinfo=UTC)


def _owner_identity() -> IdentityService:
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
        },
    )


def _group_identity() -> IdentityService:
    return IdentityService(
        relationships={
            (W, "bot_a", "p_master"): RelationshipState(
                W, "bot_a", "p_master", RelationLevel.OWNER, 1.0, "seed"
            ),
        },
        person_identities={(W, "master_raw"): "p_master"},
        group_memberships={
            (W, "g1@bot_a", "p_member"): [
                GroupMembershipPeriod(start_at=_dt(1) - timedelta(days=1))
            ],
        },
        group_query_policies={(W, "g1", ""): True},
    )


def _build(tmp: Path):
    db_path = tmp / "commands.db"
    meta = MetaRepositoryManager(str(db_path))
    run_migrations(str(db_path))
    return meta, CommandRepository(meta.engine), db_path


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


def test_migration_34_creates_command_tables() -> None:
    import sqlite3

    tmp = _make_temp_dir()
    meta = None
    db_path = tmp / "commands.db"
    try:
        meta, repo, db_path = _build(tmp)
        conn = sqlite3.connect(str(db_path))
        try:
            tables = {row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )}
            version = conn.execute("SELECT MAX(version) FROM _schema_version").fetchone()[0]
        finally:
            conn.close()
        for table in ("command_records", "authorization_records", "command_query_logs"):
            assert table in tables, table
        assert version >= 34
    finally:
        _cleanup(meta, db_path)
        shutil.rmtree(tmp, ignore_errors=True)


def test_command_service_roundtrip() -> None:
    tmp = _make_temp_dir()
    meta = None
    db_path = tmp / "commands.db"
    try:
        meta, repo, db_path = _build(tmp)
        identity = _owner_identity()
        service = CommandService(identity, repository=repo)
        owner_command = service.record_command(
            created_at=_dt(1, 10),
            issuer_person_id="p_owner",
            issuer_account_id="owner_raw",
            issuer_role="master",
            origin_platform=W,
            origin_bot_account_id="bot_a",
            origin_chat_id="owner_raw",
            origin_chat_type="private",
            raw_request="帮我记一下明天开会",
            action_type="command",
            metadata={"intent": "command"},
        )
        friend_command = service.record_command(
            created_at=_dt(1, 11),
            issuer_person_id="p_friend",
            issuer_account_id="friend_raw",
            issuer_role="ordinary",
            origin_platform=W,
            origin_bot_account_id="bot_a",
            origin_chat_id="friend_raw",
            origin_chat_type="private",
            raw_request="今天天气怎么样",
            action_type="question",
        )
        authorization = service.record_authorization(
            owner_command.command_id,
            raw_owner_conversation=[{"role": "owner", "text": "帮我记一下"}],
            clarification_events=[{"text": "几点？"}],
            final_confirmation="明天 9 点",
        )
        service.set_status(owner_command.command_id, CommandStatus.DONE)
        service.revoke(friend_command.command_id, at=_dt(2))

        # A fresh service loads the projection from SQLite.
        reloaded = CommandService(identity, repository=CommandRepository(meta.engine))
        assert len(reloaded) == 2
        loaded_owner = reloaded.get(owner_command.command_id)
        loaded_friend = reloaded.get(friend_command.command_id)
        assert loaded_owner is not None
        assert loaded_owner.status == CommandStatus.DONE
        assert loaded_owner.created_at is not None
        assert loaded_owner.created_at.tzinfo is None
        assert loaded_owner.metadata["intent"] == "command"
        assert loaded_owner.authorization_id == authorization.authorization_id
        assert loaded_friend is not None
        assert loaded_friend.status == CommandStatus.REVOKED
        assert loaded_friend.revoked_at is not None

        loaded_auth = reloaded.authorization(authorization.authorization_id)
        assert loaded_auth is not None
        assert loaded_auth.final_confirmation == "明天 9 点"
        assert loaded_auth.raw_owner_conversation[0]["text"] == "帮我记一下"
        assert repo.count_commands() == 2
    finally:
        _cleanup(meta, db_path)
        shutil.rmtree(tmp, ignore_errors=True)


def test_command_recorder_writes_sqlite_not_jsonl() -> None:
    tmp = _make_temp_dir()
    meta = None
    db_path = tmp / "commands.db"
    jsonl_path = tmp / "commands.jsonl"
    try:
        meta, repo, db_path = _build(tmp)
        identity = _owner_identity()
        service = CommandService(identity, repository=repo)
        recorder = command_recorder_for_service(
            service, identity, path=jsonl_path
        )
        record = recorder.record_from_message(
            {
                "msg_id": "m1",
                "chat_id": "owner_raw",
                "chat_type": "private",
                "sender_id": "owner_raw",
                "sender_name": "老板",
                "content": "帮我给张三发个消息",
            },
            platform=W,
            account_id="bot_a",
        )
        assert record is not None
        assert record.issuer_role == "master"
        assert repo.count_commands() == 1
        assert not jsonl_path.exists()
        loaded = CommandService(identity, repository=CommandRepository(meta.engine))
        assert len(loaded) == 1
    finally:
        _cleanup(meta, db_path)
        shutil.rmtree(tmp, ignore_errors=True)


def test_command_service_group_query_after_reload() -> None:
    tmp = _make_temp_dir()
    meta = None
    db_path = tmp / "commands.db"
    try:
        meta, repo, db_path = _build(tmp)
        identity = _group_identity()
        service = CommandService(identity, repository=repo)
        service.record_command(
            created_at=_dt(1, 10),
            issuer_person_id="p_master",
            issuer_account_id="master_raw",
            issuer_role="master",
            origin_platform=W,
            origin_bot_account_id="bot_a",
            origin_chat_id="g1",
            origin_chat_type="group",
            raw_request="以我的名义发一条聚会通知",
            action_type="send_message",
            clarified_spec={"summary": "发一条聚会通知"},
            metadata={"public_summary": "发了一条聚会通知"},
        )
        reloaded = CommandService(identity, repository=CommandRepository(meta.engine))
        member = PrincipalContext(
            platform=W,
            bot_account_id="bot_a",
            chat_id="g1",
            chat_type=ChatType.GROUP,
            person_id="p_member",
            display_name="成员",
            relation_to_bot=RelationLevel.ORDINARY,
            group_id="g1",
            capabilities=frozenset({Capability.QUERY_GROUP_COMMANDS}),
            occurred_at=_dt(2),
        )
        visible = reloaded.query_commands(member, group_id="g1")
        assert len(visible) == 1
        answer = reloaded.render_query(member, group_id="g1")
        assert "鲸鲸自己决定" in answer
        assert repo.count_query_logs() == 1
    finally:
        _cleanup(meta, db_path)
        shutil.rmtree(tmp, ignore_errors=True)


def test_event_stream_wires_command_recorder_to_db() -> None:
    tmp = _make_temp_dir()
    meta = None
    db_path = tmp / "commands.db"
    try:
        from core import features
        from core.terminal.command_runtime import (
            get_terminal_command_recorder,
            install_terminal_command_service,
            reset_terminal_command_service,
        )
        from core.terminal.event_runtime import (
            install_terminal_event_store,
            reset_terminal_event_store,
        )
        from core.terminal.event_store_db import TerminalEventStore
        from core.terminal.shadow import record_message_events

        meta, repo, db_path = _build(tmp)
        identity = _owner_identity()
        command_service = CommandService(identity, repository=repo)
        event_store = TerminalEventStore(meta.engine)
        install_terminal_command_service(command_service)
        install_terminal_event_store(event_store)
        try:
            count = record_message_events(
                [{
                    "msg_id": "m9",
                    "chat_id": "owner_raw",
                    "chat_type": "private",
                    "sender_id": "owner_raw",
                    "sender_name": "老板",
                    "content": "帮我查一下明天的日程",
                    "created_at": _dt(1, 10).isoformat(),
                    "source": W,
                }],
                platform=W,
                account_id="bot_a",
                identity_service=identity,
                command_recorder=get_terminal_command_recorder(identity),
            )
            assert count == 1
            assert event_store.count() == 1
            assert repo.count_commands() == 1
            loaded = CommandService(identity, repository=CommandRepository(meta.engine))
            assert len(loaded) == 1
        finally:
            reset_terminal_command_service()
            reset_terminal_event_store()
    finally:
        _cleanup(meta, db_path)
        shutil.rmtree(tmp, ignore_errors=True)


def test_ingestion_hook_writes_events_and_commands() -> None:
    tmp = _make_temp_dir()
    meta = None
    db_path = tmp / "commands.db"
    try:
        from unittest.mock import patch

        from core import features
        from core.ingestion.ingestors import _trace_terminal_shadow
        from core.terminal.command_runtime import (
            install_terminal_command_service,
            reset_terminal_command_service,
        )
        from core.terminal.event_runtime import (
            install_terminal_event_store,
            reset_terminal_event_store,
        )
        from core.terminal.event_store_db import TerminalEventStore

        meta, repo, db_path = _build(tmp)
        identity = _owner_identity()
        command_service = CommandService(identity, repository=repo)
        event_store = TerminalEventStore(meta.engine)
        install_terminal_command_service(command_service)
        install_terminal_event_store(event_store)
        try:
            with patch(
                "core.terminal.event_runtime.get_event_identity_service",
                return_value=identity,
            ):
                _trace_terminal_shadow(
                    [{
                        "msg_id": "m10",
                        "chat_id": "owner_raw",
                        "chat_type": "private",
                        "sender_id": "owner_raw",
                        "sender_name": "老板",
                        "content": "帮我安排明天上午的事",
                        "created_at": _dt(1, 10).isoformat(),
                        "source": W,
                    }],
                    platform=W,
                    account_id="bot_a",
                )
            assert event_store.count() == 1
            assert repo.count_commands() == 1
        finally:
            reset_terminal_command_service()
            reset_terminal_event_store()
    finally:
        _cleanup(meta, db_path)
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    tests = [
        test_migration_34_creates_command_tables,
        test_command_service_roundtrip,
        test_command_recorder_writes_sqlite_not_jsonl,
        test_command_service_group_query_after_reload,
        test_event_stream_wires_command_recorder_to_db,
        test_ingestion_hook_writes_events_and_commands,
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
