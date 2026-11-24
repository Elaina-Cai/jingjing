"""Permission tests for the WeChat person-profile query tool."""

from __future__ import annotations

import asyncio
import os
import sys
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
    os.path.dirname(os.path.abspath(__file__)), "..", "data", "_test_person_profile_tool_iso"
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


from core.contracts.events import ChatType
from core.contracts.person_memory import (
    ObservationKind,
    ObservationVisibility,
    PersonObservation,
    PersonProfileSnapshot,
    ProfileScopeType,
)
from db.meta_models import init_meta_db
from db.repo_people import PeopleRepository
from db.repo_person_memory import PersonMemoryRepository
from core.terminal.authority import (
    AuthorityService,
    install_authority_service,
    reset_authority_service,
)
from core.terminal.identity_service import IdentityService
from core.terminal.principal import RelationLevel, RelationshipState


class _FakeMeta:
    def __init__(self, engine):
        self.engine = engine
        self.people = PeopleRepository(engine)


def _obs(
    observation_id: str,
    *,
    person_id: str,
    occurred_scope_type: ChatType,
    occurred_scope_id: str,
    visibility: ObservationVisibility,
    content: str,
    speaker_person_id: str = "",
) -> PersonObservation:
    return PersonObservation(
        observation_id=observation_id,
        person_id=person_id,
        platform="wechat",
        account_id="wxid_bot",
        occurred_scope_type=occurred_scope_type,
        occurred_scope_id=occurred_scope_id,
        speaker_person_id=speaker_person_id or person_id,
        source_evidence_id=f"ev_{observation_id}",
        source_message_id=f"msg_{observation_id}",
        kind=ObservationKind.FACT,
        content=content,
        confidence=0.9,
        visibility=visibility,
    )


def test_profile_tool_owner_and_self_permissions() -> None:
    root = Path(__file__).resolve().parent.parent
    db_path = root / "data" / "_test_person_profile_tool.db"
    for suffix in ("", "-wal", "-shm"):
        Path(str(db_path) + suffix).unlink(missing_ok=True)
    engine = init_meta_db(str(db_path))
    try:
        people = PeopleRepository(engine)
        memory = PersonMemoryRepository(engine)
        target = people.resolve_identity(
            platform="wechat",
            account_id="wxid_bot",
            identity_type="wxid",
            identity_value="wxid_seed06",
            display_name="P6",
            source="test",
        )
        assert target is not None
        target_id = target.person_id
        # Authority is always on; tests must inject deterministic identity
        # instead of relying on config owner_wxids fallback.
        authority = AuthorityService(IdentityService(
            relationships={
                ("wechat", "wxid_bot", "p_test_owner"): RelationshipState(
                    "wechat", "wxid_bot", "p_test_owner",
                    RelationLevel.OWNER, 1.0, "test",
                ),
            },
            person_identities={
                ("wechat", "wxid_seed01"): "p_test_owner",
                ("wechat", "wxid_seed06"): target_id,
            },
        ))
        install_authority_service(authority)

        memory.save_many([
            _obs(
                "group_xiaoyu", person_id=target_id,
                occurred_scope_type=ChatType.GROUP,
                occurred_scope_id="10000000001@chatroom",
                visibility=ObservationVisibility.GROUP_SHARED,
                content="P6在群里说周末去图书馆",
            ),
            _obs(
                "private_xiaoyu", person_id=target_id,
                occurred_scope_type=ChatType.PRIVATE,
                occurred_scope_id="wxid_seed06",
                visibility=ObservationVisibility.PRIVATE_ONLY,
                content="P6私聊说喜欢冰美式",
            ),
            _obs(
                "private_other_about_xiaoyu", person_id=target_id,
                occurred_scope_type=ChatType.PRIVATE,
                occurred_scope_id="wxid_seed05",
                visibility=ObservationVisibility.PRIVATE_ONLY,
                content="P5私聊说P6明天不来",
                speaker_person_id="wxid_seed05",
            ),
        ])
        memory.save_profile(PersonProfileSnapshot(
            profile_id="profile_xiaoyu",
            person_id=target_id,
            platform="wechat",
            account_id="wxid_bot",
            scope_type=ProfileScopeType.GLOBAL,
            scope_id="",
            summary="P6：普通联系人，喜欢冰美式",
            topics=("学习",),
            observation_count=3,
        ))

        from agents.tools.person_profile import GetPersonProfileTool

        tool = GetPersonProfileTool(meta_repo=_FakeMeta(engine))
        assert tool.resolve_target_from_text(
            "她和你说过什么话你记得不\n记得呀，P6是那个群里的成员",
            platform="wechat",
            account_id="wxid_bot",
        ) == "P6"

        async def query(extra: dict) -> str:
            tool.set_context(SimpleNamespace(extra=extra, account_context=None))
            result = await tool.execute(person="P6", query="P6最近怎么样")
            assert not result.is_error, result.error
            return result.output

        owner_output = asyncio.run(query({
            "sender_id": "wxid_seed01",
            "sender_name": "Elaina",
            "chat_id": "wxid_seed01",
            "is_group": False,
            "platform": "wechat",
            "account_id": "wxid_bot",
        }))
        assert "P6在群里说周末去图书馆" in owner_output
        assert "P6私聊说喜欢冰美式" in owner_output
        assert "P5私聊说P6明天不来" in owner_output  # owner override
        assert "喜欢冰美式" in owner_output

        self_output = asyncio.run(query({
            "sender_id": "wxid_seed06",
            "sender_name": "P6",
            "chat_id": "wxid_seed06",
            "is_group": False,
            "platform": "wechat",
            "account_id": "wxid_bot",
        }))
        assert "P6在群里说周末去图书馆" in self_output
        assert "P6私聊说喜欢冰美式" in self_output
        assert "P5私聊说P6明天不来" not in self_output

        other_output = asyncio.run(query({
            "sender_id": "wxid_seed05",
            "sender_name": "P5",
            "chat_id": "wxid_seed05",
            "is_group": False,
            "platform": "wechat",
            "account_id": "wxid_bot",
        }))
        assert "P6私聊说喜欢冰美式" not in other_output
        assert "P5私聊说P6明天不来" in other_output
    finally:
        reset_authority_service()
        engine.dispose()
        for suffix in ("", "-wal", "-shm"):
            Path(str(db_path) + suffix).unlink(missing_ok=True)


def test_scoped_tool_registry_uses_meta_repo() -> None:
    """Account-scoped registries must pass meta_repo, not ctx.repo.

    Regression: RuntimeRegistry.tools_for used to pass ctx.repo as meta_repo,
    so get_person_profile opened the account DB and failed with
    "no such table: person_profile_snapshots".
    """
    from core.runtime import AccountContext, RuntimeRegistry

    class _FakeMeta:
        engine = object()
        people = object()

    previous = os.environ.get("YINYUE_FEATURE_PERSON_PROFILE_TOOLS")
    os.environ["YINYUE_FEATURE_PERSON_PROFILE_TOOLS"] = "1"
    try:
        meta = _FakeMeta()
        ctx = AccountContext(
            platform="wechat",
            account_id="wxid_bot",
            role="yinyue",
            config=SimpleNamespace(send_enabled=False),
            account_repo=object(),
            meta_repo=meta,
        )
        # Keep create_default from trying to build a ScopedRepository with fakes.
        ctx._repo = object()
        runtime = RuntimeRegistry([ctx], meta_repo=meta)
        registry = runtime.tools_for(ctx)
        tool = registry.get("get_person_profile")
        assert tool is not None
        assert tool._meta() is meta
    finally:
        if previous is None:
            os.environ.pop("YINYUE_FEATURE_PERSON_PROFILE_TOOLS", None)
        else:
            os.environ["YINYUE_FEATURE_PERSON_PROFILE_TOOLS"] = previous


def main() -> bool:
    tests = [
        test_profile_tool_owner_and_self_permissions,
        test_scoped_tool_registry_uses_meta_repo,
    ]
    failed: list[str] = []
    for test in tests:
        try:
            test()
            print(f"  ok   {test.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed.append(test.__name__)
            print(f"  FAIL {test.__name__}: {exc!r}")
    print(f"person profile tool checks: {len(tests) - len(failed)}/{len(tests)} passed")
    return not failed


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
