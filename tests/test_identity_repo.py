"""P2 identity / evidence foundation checks.

Run directly: python tests/test_identity_repo.py
"""

from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sqlalchemy import text

from db.meta_repo import MetaRepositoryManager
from db.migrations import run_migrations

_ROOT = Path(__file__).resolve().parent.parent
WECHAT = "wechat"
_DB = _ROOT / "data" / "_test_identity.db"
_MIGRATION_DB = _ROOT / "data" / "_test_migration.db"


def _cleanup(path: Path) -> None:
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(str(path) + suffix)
        candidate.unlink(missing_ok=True)


def test_identity_repo() -> bool:
    print("\n[identity repository]")
    _cleanup(_DB)
    try:
        meta = MetaRepositoryManager(str(_DB))
        repo = meta.people

        first = repo.resolve_message({
            "msg_id": "1",
            "sender_id": "wxid_zhang",
            "sender_name": "张三",
            "chat_id": "group_a@chatroom",
            "chat_type": "group",
            "source": "wechat",
            "account_id": "wxid_bot",
        })
        second = repo.resolve_message({
            "msg_id": "2",
            "sender_id": "wxid_zhang",
            "sender_name": "张三",
            "chat_id": "group_a@chatroom",
            "chat_type": "group",
            "source": "wechat",
            "account_id": "wxid_bot",
        })
        ok = first is not None and second is not None
        ok &= first.person_id == second.person_id
        memberships = repo.list_memberships(first.person_id, chat_id="group_a@chatroom")
        ok &= len(memberships) == 1 and memberships[0].message_count == 2
        ref = repo.resolve_message({
            "msg_id": "5",
            "sender_id": "wxid_zhang",
            "sender_name": "张三",
            "chat_id": "group_a@chatroom",
            "chat_type": "group",
            "source": "wechat",
            "account_id": "wxid_bot",
        })
        ok &= ref is not None and ref.person_id == first.person_id


        weak_a = repo.resolve_message({
            "msg_id": "3",
            "sender_name": "小王",
            "chat_id": "group_a@chatroom",
            "chat_type": "group",
            "source": "wechat",
            "account_id": "wxid_bot",
        })
        weak_b = repo.resolve_message({
            "msg_id": "4",
            "sender_name": "小王",
            "chat_id": "group_b@chatroom",
            "chat_type": "group",
            "source": "wechat",
            "account_id": "wxid_bot",
        })
        ok &= weak_a is not None and weak_b is not None
        ok &= weak_a.person_id != weak_b.person_id
        weak_a2 = repo.resolve_message({
            "msg_id": "5",
            "sender_name": "小王",
            "chat_id": "group_a@chatroom",
            "chat_type": "group",
            "source": "wechat",
            "account_id": "wxid_bot",
        })
        ok &= weak_a2 is not None and weak_a2.person_id == weak_a.person_id


        found = repo.find_persons("张三", platform="wechat", account_id="wxid_bot")
        ok &= any(p.person_id == first.person_id for p in found)

        with meta.engine.connect() as conn:
            cols = {row[1] for row in conn.execute(text("PRAGMA table_info(conversations)"))}
        ok &= {"platform", "account_id", "chat_id", "sender_id"} <= cols
        print(f"  person: {first.person_id}")
        print(f"  memberships: {[(m.chat_id, m.message_count) for m in memberships]}")
        print(f"  weak scopes distinct: {weak_a.person_id != weak_b.person_id}")
        print("  ok" if ok else "  FAIL")
        return bool(ok)
    finally:
        try:
            meta.engine.dispose()
        except Exception:
            pass
        _cleanup(_DB)


def test_merge_persons() -> bool:
    print("\n[explicit merge]")
    _cleanup(_DB)
    try:
        meta = MetaRepositoryManager(str(_DB))
        repo = meta.people
        source = repo.resolve_identity(
            platform="wechat", account_id="wxid_bot",
            identity_type="nickname", identity_value="小号",
            display_name="小号", confidence=0.4, source="manual",
        )
        target = repo.resolve_identity(
            platform="wechat", account_id="wxid_bot",
            identity_type="wxid", identity_value="wxid_real",
            display_name="真名", confidence=0.95, source="contact",
        )
        ok = source is not None and target is not None
        moved = repo.merge_persons(source.person_id, target.person_id)
        ok &= moved
        ok &= repo.get_person(source.person_id).status == "merged"
        ok &= any(i.person_id == target.person_id for i in repo.list_identities(target.person_id))
        print("  ok" if ok else "  FAIL")
        return bool(ok)
    finally:
        try:
            meta.engine.dispose()
        except Exception:
            pass
        _cleanup(_DB)
def test_account_isolation() -> bool:
    print("\n[account isolation]")
    _cleanup(_DB)
    meta = None
    try:
        meta = MetaRepositoryManager(str(_DB))
        repo = meta.people
        a = repo.resolve_identity(
            platform="wechat", account_id="bot_a",
            identity_type="wxid", identity_value="wxid_same",
            display_name="同一个 wxid", confidence=0.95,
        )
        b = repo.resolve_identity(
            platform="wechat", account_id="bot_b",
            identity_type="wxid", identity_value="wxid_same",
            display_name="同一个 wxid", confidence=0.95,
        )
        ok = a is not None and b is not None
        ok &= a.person_id != b.person_id
        print(f"  account_scoped_distinct: {a.person_id != b.person_id}")
        print("  ok" if ok else "  FAIL")
        return bool(ok)
    finally:
        try:
            if meta is not None:
                meta.engine.dispose()
        except Exception:
            pass
        _cleanup(_DB)





def test_strong_identity_cross_chat() -> bool:
    """同一 wxid 在多个群 => 同一 person；同昵称跨群 => 不同 person。"""
    print("\n[strong identity across chats]")
    _cleanup(_DB)
    meta = None
    try:
        meta = MetaRepositoryManager(str(_DB))
        repo = meta.people
        people = []
        for chat in ("group_a@chatroom", "group_b@chatroom", "group_c@chatroom"):
            people.append(repo.resolve_message({
                "msg_id": chat,
                "sender_id": "wxid_zhang",
                "sender_name": "张三",
                "chat_id": chat,
                "chat_type": "group",
                "source": "wechat",
                "account_id": "wxid_bot",
            }))
        ok = all(p is not None for p in people)
        person_ids = {p.person_id for p in people if p is not None}
        ok &= len(person_ids) == 1
        identities = repo.list_identities(next(iter(person_ids)))
        # one identity row per chat = "seen here", but a single person entity
        same_count = len(identities) == 3
        ok &= same_count
        if not same_count:
            print(f"  identity rows: {[(i.chat_id, i.identity_type) for i in identities]}")
        ok &= len({i.chat_id for i in identities}) == 3
        ok &= len(repo.find_persons("wxid_zhang", platform="wechat",
                                    account_id="wxid_bot")) == 1

        # weak nickname must stay chat-scoped (no regression)
        weak_a = repo.resolve_message({
            "msg_id": "w1", "sender_name": "小王",
            "chat_id": "group_a@chatroom", "chat_type": "group",
            "source": "wechat", "account_id": "wxid_bot",
        })
        weak_b = repo.resolve_message({
            "msg_id": "w2", "sender_name": "小王",
            "chat_id": "group_b@chatroom", "chat_type": "group",
            "source": "wechat", "account_id": "wxid_bot",
        })
        ok &= weak_a is not None and weak_b is not None
        ok &= weak_a.person_id != weak_b.person_id

        # placeholders are not identities even when typed as wxid
        ph_a = repo.resolve_identity(
            platform="wechat", account_id="wxid_bot",
            identity_type="wxid", identity_value="unknown",
            chat_id="group_a@chatroom", display_name="unknown", source="message",
        )
        ph_b = repo.resolve_identity(
            platform="wechat", account_id="wxid_bot",
            identity_type="wxid", identity_value="unknown",
            chat_id="group_b@chatroom", display_name="unknown", source="message",
        )
        ok &= ph_a is not None and ph_b is not None
        ok &= ph_a.person_id != ph_b.person_id

        # same wxid on a different bot account stays a different person
        other = repo.resolve_identity(
            platform="wechat", account_id="wxid_bot_2",
            identity_type="wxid", identity_value="wxid_zhang",
            chat_id="group_a@chatroom", display_name="张三", source="message",
        )
        ok &= other is not None and other.person_id not in person_ids
        print(f"  cross_chat_same_person: {len(person_ids) == 1}")
        print(f"  identities: {len(identities)}")
        print("  ok" if ok else "  FAIL")
        return bool(ok)
    finally:
        try:
            if meta is not None:
                meta.engine.dispose()
        except Exception:
            pass
        _cleanup(_DB)


def test_resolve_person_id_strong_first() -> bool:
    """读侧：强标识先查全局，弱标识再查 scope；强标识不被 scope 行遮蔽。"""
    print("\n[resolve_person_id strong first]")
    from core.terminal.identity_service import IdentityService

    service = IdentityService(
        strong_identities={(WECHAT, "bot_a", "wxid_x"): "p_x"},
        person_identity_scopes={
            (WECHAT, "bot_a", "wxid_x"): "p_stale",
            (WECHAT, "bot_a", "小王"): "p_weak_a",
        },
        person_identities={(WECHAT, "小王"): "p_weak_global"},
    )
    ok = service.resolve_person_id(WECHAT, "wxid_x", "bot_a") == "p_x"
    ok &= service.resolve_person_id(WECHAT, "小王", "bot_a") == "p_weak_a"
    ok &= service.resolve_person_id(WECHAT, "nobody", "bot_a").startswith("unresolved:")
    # no account: strong row without account still resolves, then global fallback
    service2 = IdentityService(
        strong_identities={(WECHAT, "", "wxid_y"): "p_y"},
        person_identities={(WECHAT, "小王"): "p_weak_global"},
    )
    ok &= service2.resolve_person_id(WECHAT, "wxid_y") == "p_y"
    ok &= service2.resolve_person_id(WECHAT, "小王") == "p_weak_global"
    print(f"  strong_first: {ok}")
    print("  ok" if ok else "  FAIL")
    return bool(ok)


def test_conversation_migration() -> bool:
    print("\n[conversation identity migration]")
    _cleanup(_MIGRATION_DB)
    try:
        conn = sqlite3.connect(str(_MIGRATION_DB))
        conn.execute(
            "CREATE TABLE conversations ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "persona_id INTEGER, session_id INTEGER, role TEXT, "
            "content TEXT, emotion TEXT, metadata TEXT, created_at DATETIME)"
        )
        conn.commit()
        conn.close()

        run_migrations(str(_MIGRATION_DB))

        conn = sqlite3.connect(str(_MIGRATION_DB))
        cols = {row[1] for row in conn.execute("PRAGMA table_info(conversations)")}
        conn.close()
        ok = {"platform", "account_id", "chat_id", "sender_id"} <= cols
        print(f"  columns: {sorted(cols)}")
        print("  ok" if ok else "  FAIL")
        return bool(ok)
    finally:
        _cleanup(_MIGRATION_DB)


def main() -> bool:
    print("=" * 50)
    print("identity repository checks")
    print("=" * 50)
    ok = test_identity_repo()
    ok &= test_merge_persons()
    ok &= test_account_isolation()
    ok &= test_strong_identity_cross_chat()
    ok &= test_resolve_person_id_strong_first()
    ok &= test_conversation_migration()
    print("\n" + ("all passed" if ok else "FAILED"))
    return bool(ok)


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
