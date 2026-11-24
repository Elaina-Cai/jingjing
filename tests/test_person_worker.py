"""Scheduler adapter test with a fake QQ platform.

The worker is the only place that knows scheduler event payloads.  This test
proves it can hand QQ-shaped messages to the core service without any WeChat
field or live bootstrap dependency.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

os.environ["YINYUE_FEATURE_PERSON_MEMORY"] = "1"
os.environ["YINYUE_FEATURE_PERSON_LLM"] = "0"
os.environ["YINYUE_FEATURE_PERSON_PROFILE_AGGREGATE"] = "1"
# 兄弟开关钉死（守卫 tests/test_env_flag_isolation.py §③）：本文件只走
# 规则抽取（PERSON_LLM=0）+ 聚合，读侧召回不参与；钉在缺省位，不吃 .env。
os.environ["YINYUE_FEATURE_PERSON_RECALL"] = "0"

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from db.meta_models import init_meta_db
import scheduler.person_memory as person_memory


class _FakePeople:
    def resolve_message(self, message, *, platform="", account_id=""):
        return SimpleNamespace(person_id="qq_person")


class _FakeMetaRepo:
    def __init__(self, engine):
        self.engine = engine
        self.people = _FakePeople()


def test_worker_processes_qq_event(caplog=None) -> None:
    root = Path(__file__).resolve().parent.parent
    db_path = root / "data" / "_test_person_worker.db"
    for suffix in ("", "-wal", "-shm"):
        Path(str(db_path) + suffix).unlink(missing_ok=True)
    engine = init_meta_db(str(db_path))
    fake_meta = _FakeMetaRepo(engine)
    original = person_memory.get_meta_repo
    person_memory.get_meta_repo = lambda: fake_meta
    try:
        event = SimpleNamespace(
            platform="qq",
            account_id="bot_10001",
            payload={"messages": [
                {
                    "is_self": 0,
                    "sender_id": "qq_user_1",
                    "sender_name": "P6",
                    "chat_id": "qq_group_1",
                    "chat_type": "group",
                    "content": "我明天要去上课",
                    "msg_type_name": "text",
                    "msg_id": "qq_msg_1",
                    "created_at": "2026-09-18 20:00:00",
                },
                {
                    "is_self": 1,
                    "sender_id": "qq_bot",
                    "sender_name": "鲸鲸",
                    "chat_id": "qq_group_1",
                    "chat_type": "group",
                    "content": "我明天要去上课",
                    "msg_type_name": "text",
                    "msg_id": "qq_msg_ignored",
                },
                {
                    "is_self": 0,
                    "sender_id": "qq_user_2",
                    "sender_name": "空消息",
                    "chat_id": "qq_group_1",
                    "chat_type": "group",
                    "content": "",
                    "msg_type_name": "text",
                    "msg_id": "qq_msg_empty",
                },
            ]},
        )
        written = asyncio.run(person_memory.process_new_messages(event))
        assert written == 1

        con = sqlite3.connect(str(db_path))
        rows = con.execute(
            "select person_id, platform, account_id, occurred_scope_type, "
            "occurred_scope_id, content from person_observations"
        ).fetchall()
        con.close()
        assert rows
        assert rows[0][0] == "qq_person"
        assert rows[0][1] == "qq"
        assert rows[0][3] == "group"
        assert "明天" in rows[0][5]

        con = sqlite3.connect(str(db_path))
        profile_count = con.execute(
            "select count(*) from person_profile_snapshots"
        ).fetchone()[0]
        con.close()
        assert profile_count >= 2  # global + group
    finally:
        person_memory.get_meta_repo = original
        engine.dispose()
        for suffix in ("", "-wal", "-shm"):
            Path(str(db_path) + suffix).unlink(missing_ok=True)


def main() -> bool:
    tests = [test_worker_processes_qq_event]
    failed: list[str] = []
    for test in tests:
        try:
            test()
            print(f"  ok   {test.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed.append(test.__name__)
            print(f"  FAIL {test.__name__}: {exc!r}")
    print(f"person worker checks: {len(tests) - len(failed)}/{len(tests)} passed")
    return not failed


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
