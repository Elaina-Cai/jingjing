"""Phase 5 Track B: event-sourced analysis worker tests.

Run directly:
    python tests/test_analysis_from_events.py
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sqlite3
import sys
import uuid
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# 兄弟开关钉死（守卫 tests/test_env_flag_isolation.py §③）：本文件测的是"人物记忆
# 能不能读事件流"（PERSON_MEMORY / ANALYSIS_FROM_EVENTS / PERSON_LLM 三个各自用例内
# 显式设置）；同族的读侧召回与档案聚合与本文件无关，钉在自己的缺省位，别跟 .env 走。
os.environ["YINYUE_FEATURE_PERSON_RECALL"] = "0"
os.environ["YINYUE_FEATURE_PERSON_PROFILE_AGGREGATE"] = "0"

ROOT = Path(__file__).resolve().parent.parent
_TEST_TMP_ROOT = ROOT / "data" / "_analysis_from_events_test"
UTC = timezone.utc
W = "wechat"
DAY = date(2026, 9, 20)


def _make_temp_dir() -> Path:
    _TEST_TMP_ROOT.mkdir(parents=True, exist_ok=True)
    path = _TEST_TMP_ROOT / f"case_{uuid.uuid4().hex}"
    path.mkdir(parents=True, exist_ok=False)
    return path


from core.analysis.event_reader import AnalysisEventReader, is_analysis_event  # noqa: E402
from core.analysis.projectors import (  # noqa: E402
    PROJECTION_DIGEST,
    PROJECTION_GROUP,
    PROJECTION_MESSAGE,
)
from core.analysis.workers import (  # noqa: E402
    ANALYSIS_CURSOR_KEY,
    AnalysisFromEventsWorker,
    run_daily_digest_once,
    run_analysis_worker_once,
)
from core.terminal.event_model import Event, EventType, Sensitivity  # noqa: E402
from core.terminal.event_store_db import TerminalEventStore  # noqa: E402
from db.meta_repo import MetaRepositoryManager  # noqa: E402


def _dt(hour: int = 10) -> datetime:
    return datetime(DAY.year, DAY.month, DAY.day, hour, 0, 0, tzinfo=UTC)


def _build(tmp: Path):
    db_path = tmp / "analysis.db"
    meta = MetaRepositoryManager(str(db_path))
    store = TerminalEventStore(meta.engine)
    return meta, store, db_path


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


def _seed(store: TerminalEventStore):
    message = Event.new(
        event_type=EventType.MESSAGE,
        occurred_at=_dt(9),
        platform=W,
        bot_account_id="bot_a",
        chat_id="u1",
        chat_type="private",
        actor_person_id="u1",
        actor_account_id="u1",
        content="我明天要开会，帮我记住",
        metadata={
            "msg_id": "m1",
            "sender_name": "P6",
            "is_self": 0,
            "msg_type_name": "text",
        },
    )
    group = Event.new(
        event_type=EventType.MESSAGE,
        occurred_at=_dt(10),
        platform=W,
        bot_account_id="bot_a",
        chat_id="g1",
        chat_type="group",
        actor_person_id="u2",
        actor_account_id="u2",
        content="周末聚会安排一下，@鲸鲸 帮忙订个地方",
        metadata={
            "msg_id": "m2",
            "sender_name": "群友",
            "is_self": 0,
            "msg_type_name": "text",
        },
    )
    thought = Event.new(
        event_type=EventType.THOUGHT,
        occurred_at=_dt(11),
        platform=W,
        bot_account_id="bot_a",
        chat_id="u1",
        chat_type="private",
        actor_person_id="u1",
        content="这条思维链不能进分析",
        sensitivity=Sensitivity.OWNER_CHAIN_OF_THOUGHT,
        metadata={"msg_id": "thought"},
    )
    for event in (message, group, thought):
        store.append(event)
    return message, group, thought


def test_migration_38_creates_analysis_projection_tables() -> None:
    tmp = _make_temp_dir()
    meta = None
    db_path = tmp / "analysis.db"
    try:
        meta, _store, db_path = _build(tmp)
        conn = sqlite3.connect(str(db_path))
        try:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            version = conn.execute(
                "SELECT MAX(version) FROM _schema_version"
            ).fetchone()[0]
        finally:
            conn.close()
        assert "analysis_projections" in tables
        assert "analysis_projection_cursors" in tables
        assert version >= 38
    finally:
        _cleanup(meta, db_path)
        shutil.rmtree(tmp, ignore_errors=True)


def test_reader_skips_owner_thought_and_worker_keeps_provenance() -> None:
    tmp = _make_temp_dir()
    meta = None
    db_path = tmp / "analysis.db"
    try:
        meta, store, db_path = _build(tmp)
        message, _group, thought = _seed(store)
        reader = AnalysisEventReader(store)
        raw = reader.read_events(0, limit=20)
        assert len(raw) == 3
        projectable = reader.projectable_events(raw)
        assert message.event_id in {event.event_id for event in projectable}
        assert thought.event_id not in {event.event_id for event in projectable}
        assert not is_analysis_event(thought)

        repo = meta.analysis_projections
        first = AnalysisFromEventsWorker(store, repo).run(limit=20)
        assert first["scanned"] == 3
        assert first["projected"] == 2
        assert first["written"] == 2
        assert first["failed"] == 0
        assert first["cursor"] == 3  # cursor also passes over the skipped thought

        drafts = repo.all()
        message_drafts = [
            draft for draft in drafts if draft.projection_type == PROJECTION_MESSAGE
        ]
        group_drafts = [
            draft for draft in drafts if draft.projection_type == PROJECTION_GROUP
        ]
        assert len(message_drafts) == 1 and len(group_drafts) == 1
        assert message_drafts[0].source_event_ids == (message.event_id,)
        assert all(draft.source_event_ids for draft in drafts)
        assert "思维链" not in message_drafts[0].content

        second = AnalysisFromEventsWorker(store, repo).run(limit=20)
        assert second["scanned"] == 0
        assert repo.count() == 2
    finally:
        _cleanup(meta, db_path)
        shutil.rmtree(tmp, ignore_errors=True)


def test_worker_retries_cursor_after_write_failure() -> None:
    class _FlakyRepo:
        # ENG-23：worker 现在走 (sequence, id) 复合游标；这个替身必须同时实现
        # pair 入口，否则测的是「老单值回退分支」而不是真实生产路径。
        def __init__(self, inner):
            self.inner = inner
            self.fail = True

        def get_cursor(self, key):
            return self.inner.get_cursor(key)

        def set_cursor(self, key, sequence, last_id=0):
            self.inner.set_cursor(key, sequence, last_id)

        def get_cursor_pair(self, key):
            return self.inner.get_cursor_pair(key)

        def set_cursor_pair(self, key, sequence, last_id=0):
            self.inner.set_cursor_pair(key, sequence, last_id)

        def upsert(self, draft):
            if self.fail:
                raise RuntimeError("disk busy")
            self.inner.upsert(draft)

    tmp = _make_temp_dir()
    meta = None
    db_path = tmp / "analysis.db"
    try:
        meta, store, db_path = _build(tmp)
        _seed(store)
        flaky = _FlakyRepo(meta.analysis_projections)
        first = AnalysisFromEventsWorker(store, flaky).run(limit=20)
        assert first["failed"] == 2
        assert first["cursor"] == 0
        assert meta.analysis_projections.count() == 0

        flaky.fail = False
        second = AnalysisFromEventsWorker(store, flaky).run(limit=20)
        assert second["failed"] == 0
        assert second["written"] == 2
        assert second["cursor"] == 3
    finally:
        _cleanup(meta, db_path)
        shutil.rmtree(tmp, ignore_errors=True)


def test_daily_digest_uses_event_and_analysis_sources() -> None:
    tmp = _make_temp_dir()
    meta = None
    db_path = tmp / "analysis.db"
    try:
        meta, store, db_path = _build(tmp)
        message, group, _thought = _seed(store)
        repo = meta.analysis_projections
        run_analysis_worker_once(store, repo, limit=20)

        result = run_daily_digest_once(store, repo, target_date=DAY)
        assert result["written"] == 1
        digest = repo.get(f"analysis:daily_digest:{DAY.isoformat()}")
        assert digest is not None
        assert digest.projection_type == PROJECTION_DIGEST
        assert set(digest.source_event_ids) == {message.event_id, group.event_id}
        assert "私聊要闻" in digest.content
        assert "群聊话题" in digest.content
        assert "思维链" not in digest.content
    finally:
        _cleanup(meta, db_path)
        shutil.rmtree(tmp, ignore_errors=True)


def test_worker_never_writes_legacy_memory_tables() -> None:
    tmp = _make_temp_dir()
    meta = None
    db_path = tmp / "analysis.db"
    try:
        meta, store, db_path = _build(tmp)
        _seed(store)
        run_analysis_worker_once(store, meta.analysis_projections, limit=20)
        conn = sqlite3.connect(str(db_path))
        try:
            conversations = conn.execute(
                "SELECT COUNT(*) FROM conversations"
            ).fetchone()[0]
        finally:
            conn.close()
        assert conversations == 0
    finally:
        _cleanup(meta, db_path)
        shutil.rmtree(tmp, ignore_errors=True)


class _FakePeople:
    def resolve_message(self, message, *, platform="", account_id=""):
        return SimpleNamespace(person_id="p_terminal")


class _FakeMeta:
    def __init__(self, engine):
        self.engine = engine
        self.people = _FakePeople()


def test_person_memory_can_read_terminal_events() -> None:
    tmp = _make_temp_dir()
    meta = None
    db_path = tmp / "analysis.db"
    key_memory = "YINYUE_FEATURE_PERSON_MEMORY"
    key_events = "YINYUE_FEATURE_ANALYSIS_FROM_EVENTS"
    previous_memory = os.environ.get(key_memory)
    previous_events = os.environ.get(key_events)
    try:
        meta, _store, db_path = _build(tmp)
        from scheduler import person_memory

        fake = _FakeMeta(meta.engine)
        original_get_meta = person_memory.get_meta_repo
        os.environ[key_memory] = "1"
        os.environ[key_events] = "1"
        os.environ["YINYUE_FEATURE_PERSON_LLM"] = "0"
        person_memory.get_meta_repo = lambda: fake
        terminal_event = Event.new(
            event_type=EventType.MESSAGE,
            occurred_at=_dt(12),
            platform="qq",
            bot_account_id="bot_10001",
            chat_id="qq_group_1",
            chat_type="group",
            actor_person_id="qq_user_1",
            actor_account_id="qq_user_1",
            content="我明天要去上课",
            metadata={
                "msg_id": "qq_msg_1",
                "sender_name": "P6",
                "is_self": 0,
                "msg_type_name": "text",
            },
        )
        written = asyncio.run(
            person_memory.process_terminal_events([terminal_event])
        )
        assert written == 1
        conn = sqlite3.connect(str(db_path))
        try:
            rows = conn.execute(
                "SELECT person_id, platform, content FROM person_observations"
            ).fetchall()
        finally:
            conn.close()
        assert rows
        assert rows[0][0] == "p_terminal"
        assert rows[0][1] == "qq"
    finally:
        try:
            if "person_memory" in locals():
                person_memory.get_meta_repo = original_get_meta
        except Exception:
            pass
        if previous_memory is None:
            os.environ.pop(key_memory, None)
        else:
            os.environ[key_memory] = previous_memory
        if previous_events is None:
            os.environ.pop(key_events, None)
        else:
            os.environ[key_events] = previous_events
        _cleanup(meta, db_path)
        shutil.rmtree(tmp, ignore_errors=True)


def test_person_new_messages_prefers_terminal_event_ids() -> None:
    tmp = _make_temp_dir()
    meta = None
    db_path = tmp / "analysis.db"
    key_memory = "YINYUE_FEATURE_PERSON_MEMORY"
    key_events = "YINYUE_FEATURE_ANALYSIS_FROM_EVENTS"
    key_llm = "YINYUE_FEATURE_PERSON_LLM"
    previous = {
        key: os.environ.get(key)
        for key in (key_memory, key_events, key_llm)
    }
    try:
        meta, store, db_path = _build(tmp)
        from scheduler import person_memory
        from core.terminal import event_runtime

        terminal_event = Event.new(
            event_type=EventType.MESSAGE,
            occurred_at=_dt(13),
            platform="qq",
            bot_account_id="bot_10001",
            chat_id="qq_group_1",
            chat_type="group",
            actor_person_id="qq_user_1",
            actor_account_id="qq_user_1",
            content="我明天要去上课，事件流里的内容",
            metadata={
                "msg_id": "qq_msg_event",
                "sender_name": "P6",
                "is_self": 0,
                "msg_type_name": "text",
            },
        )
        store.append(terminal_event)

        fake = _FakeMeta(meta.engine)
        original_get_meta = person_memory.get_meta_repo
        original_get_store = event_runtime.get_terminal_event_store
        os.environ[key_memory] = "1"
        os.environ[key_events] = "1"
        os.environ[key_llm] = "0"
        person_memory.get_meta_repo = lambda: fake
        event_runtime.get_terminal_event_store = lambda force=False: store
        try:
            written = asyncio.run(
                person_memory.process_new_messages(
                    SimpleNamespace(
                        payload={
                            "messages": [
                                {
                                    "is_self": 0,
                                    "sender_id": "qq_user_1",
                                    "sender_name": "P6",
                                    "chat_id": "qq_group_1",
                                    "chat_type": "group",
                                    "content": "payload 里的内容",
                                    "msg_type_name": "text",
                                    "msg_id": "payload_msg",
                                }
                            ],
                            "terminal_event_ids": [terminal_event.event_id],
                        }
                    )
                )
            )
        finally:
            person_memory.get_meta_repo = original_get_meta
            event_runtime.get_terminal_event_store = original_get_store
        assert written == 1
        conn = sqlite3.connect(str(db_path))
        try:
            contents = [
                row[0]
                for row in conn.execute(
                    "SELECT content FROM person_observations"
                )
            ]
        finally:
            conn.close()
        assert contents
        assert all("事件流里的内容" in content for content in contents)
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        _cleanup(meta, db_path)
        shutil.rmtree(tmp, ignore_errors=True)



def test_multi_batch_union_keeps_provenance() -> None:
    tmp = _make_temp_dir()
    meta = None
    db_path = tmp / "analysis.db"
    try:
        meta, store, db_path = _build(tmp)
        repo = meta.analysis_projections
        first = Event.new(
            event_type=EventType.MESSAGE,
            occurred_at=_dt(9),
            platform=W,
            bot_account_id="bot_a",
            chat_id="u1",
            chat_type="private",
            actor_person_id="u1",
            content="第一条消息",
            metadata={"msg_id": "m1", "sender_name": "P6"},
        )
        store.append(first)
        run_analysis_worker_once(store, repo, limit=1)
        draft = repo.get(
            repo.all()[0].projection_id
        )
        assert draft is not None and draft.source_event_ids == (first.event_id,)

        second = Event.new(
            event_type=EventType.MESSAGE,
            occurred_at=_dt(10),
            platform=W,
            bot_account_id="bot_a",
            chat_id="u1",
            chat_type="private",
            actor_person_id="u1",
            content="第二条消息",
            metadata={"msg_id": "m2", "sender_name": "P6"},
        )
        store.append(second)
        run_analysis_worker_once(store, repo, limit=5)
        draft = repo.get(repo.all()[0].projection_id)
        assert draft is not None
        assert set(draft.source_event_ids) == {first.event_id, second.event_id}
    finally:
        _cleanup(meta, db_path)
        shutil.rmtree(tmp, ignore_errors=True)



def main() -> int:
    tests = [
        test_migration_38_creates_analysis_projection_tables,
        test_reader_skips_owner_thought_and_worker_keeps_provenance,
        test_worker_retries_cursor_after_write_failure,
        test_daily_digest_uses_event_and_analysis_sources,
        test_worker_never_writes_legacy_memory_tables,
        test_person_memory_can_read_terminal_events,
        test_multi_batch_union_keeps_provenance,
        test_person_new_messages_prefers_terminal_event_ids,
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
