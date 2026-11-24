"""Platform identity adapter tests.

Verifies that generic identity code no longer hardcodes ``wxid`` or
``@chatroom``: platform conventions live in the adapter registry.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.contracts.events import ChatType
from core.identity.platforms import (
    adapter_for,
    chat_type_for,
    identity_type_for,
    is_group_chat,
    register_platform_adapter,
    PlatformIdentityAdapter,
)
from db.meta_models import init_meta_db
from db.repo_people import PeopleRepository


def test_identity_type_mapping() -> None:
    assert identity_type_for("wechat") == "wxid"
    assert identity_type_for("qq") == "qq_uin"
    assert identity_type_for("qq", "qq_uid") == "qq_uid"
    assert identity_type_for("unknown_platform").endswith("_user_id")
    assert adapter_for("qq").identity_type == "qq_uin"


def test_chat_type_mapping_is_canonical_first() -> None:
    assert chat_type_for("qq", "group", "group_openid") == ChatType.GROUP
    assert chat_type_for("qq", "private", "friend_openid") == ChatType.PRIVATE
    assert chat_type_for("wechat", "", "room@chatroom") == ChatType.GROUP
    # QQ group ids have no WeChat-style suffix; missing chat_type stays unknown.
    assert chat_type_for("qq", "", "group_openid") == ChatType.UNKNOWN
    assert is_group_chat("qq", "group", "group_openid") is True


def test_registered_adapter_is_used() -> None:
    register_platform_adapter(PlatformIdentityAdapter(
        platform="matrix",
        identity_type="matrix_user_id",
    ))
    try:
        assert identity_type_for("matrix") == "matrix_user_id"
        assert adapter_for("matrix").identity_type == "matrix_user_id"
    finally:
        # Restore a clean registry by removing via a no-op?  The registry keeps
        # the adapter for the test process only; this test does not mutate
        # persistent state.
        pass


def test_people_repository_uses_platform_adapter() -> None:
    root = Path(__file__).resolve().parent.parent
    db_path = root / "data" / "_test_identity_platforms.db"
    for suffix in ("", "-wal", "-shm"):
        Path(str(db_path) + suffix).unlink(missing_ok=True)
    engine = init_meta_db(str(db_path))
    try:
        repo = PeopleRepository(engine)

        qq_person = repo.resolve_message(
            {
                "sender_id": "10001",
                "sender_name": "P6",
                "chat_id": "qq_group_openid_1",
                "chat_type": "group",
                "account_id": "bot_uin_1",
                "source": "qq",
            },
            platform="qq",
            account_id="bot_uin_1",
        )
        assert qq_person is not None
        assert qq_person.platform == "qq"
        qq_identity = repo.get_identity(
            "qq", "bot_uin_1", "qq_uin", "10001", "qq_group_openid_1",
        )
        assert qq_identity is not None
        assert qq_identity.person_id == qq_person.person_id

        wx_person = repo.resolve_message(
            {
                "sender_id": "wxid_legacy_1",
                "sender_name": "Elaina",
                "chat_id": "room@chatroom",
                "chat_type": "",
                "account_id": "wxid_bot",
                "source": "wechat",
            },
            platform="wechat",
            account_id="wxid_bot",
        )
        assert wx_person is not None
        # Legacy WeChat suffix fallback still maps to a group-scoped wxid.
        wx_identity = repo.get_identity(
            "wechat", "wxid_bot", "wxid", "wxid_legacy_1", "room@chatroom",
        )
        assert wx_identity is not None
    finally:
        engine.dispose()
        for suffix in ("", "-wal", "-shm"):
            Path(str(db_path) + suffix).unlink(missing_ok=True)


def main() -> bool:
    tests = [
        test_identity_type_mapping,
        test_chat_type_mapping_is_canonical_first,
        test_registered_adapter_is_used,
        test_people_repository_uses_platform_adapter,
    ]
    failed: list[str] = []
    for test in tests:
        try:
            test()
            print(f"  ok   {test.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed.append(test.__name__)
            print(f"  FAIL {test.__name__}: {exc!r}")
    print(f"identity platform checks: {len(tests) - len(failed)}/{len(tests)} passed")
    return not failed


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
