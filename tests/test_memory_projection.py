"""Phase 2 memory projection tests.

Run directly:
    python tests/test_memory_projection.py
"""

from __future__ import annotations

import os
import shutil
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import _tempdirs  # noqa: E402,F401  临时目录纪律（见 tests/_tempdirs.py）：SQLite 临时文件走它

ROOT = Path(__file__).resolve().parent.parent
_TEST_TMP_ROOT = ROOT / "data" / "_memory_projection_test"
UTC = timezone.utc
W = "wechat"


def _make_temp_dir() -> Path:
    _TEST_TMP_ROOT.mkdir(parents=True, exist_ok=True)
    path = _TEST_TMP_ROOT / f"case_{uuid.uuid4().hex}"
    path.mkdir(parents=True, exist_ok=False)
    return path


from core.terminal.event_model import Event, EventType, Sensitivity  # noqa: E402
from core.terminal.event_store_db import TerminalEventStore  # noqa: E402
from core.terminal.identity_service import IdentityService  # noqa: E402
from core.terminal.memory_gateway import MemoryGateway  # noqa: E402
from core.terminal.memory_projection import MemoryProjector  # noqa: E402
from core.terminal.principal import (  # noqa: E402
    ChatType,
    GroupMembershipPeriod,
    RelationLevel,
    RelationshipState,
    VisibilityScope,
)
from db.meta_repo import MetaRepositoryManager  # noqa: E402
from db.migrations import run_migrations  # noqa: E402
from db.repo_memory_projections import MemoryProjectionRepository  # noqa: E402


def _dt(day: int = 1, hour: int = 10) -> datetime:
    return datetime(2026, 1, day, hour, 0, 0, tzinfo=UTC)


def _identity() -> IdentityService:
    from datetime import timedelta

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
    store = TerminalEventStore(meta.engine)
    repo = MemoryProjectionRepository(meta.engine)
    return meta, store, repo, db_path


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


def _seed_events(store: TerminalEventStore):
    fact = Event.new(
        event_type=EventType.MESSAGE,
        occurred_at=_dt(1, 9),
        platform=W,
        bot_account_id="bot_a",
        chat_id="friend_raw",
        chat_type="private",
        actor_person_id="p_friend",
        content="我喜欢喝美式咖啡，不加糖。",
        visibility_scope=frozenset({VisibilityScope.ISSUER_ONLY}),
        metadata={"sender_name": "朋友", "group_id": ""},
    )
    question = Event.new(
        event_type=EventType.QUESTION,
        occurred_at=_dt(1, 10),
        platform=W,
        bot_account_id="bot_a",
        chat_id="friend_raw",
        chat_type="private",
        actor_person_id="p_friend",
        content="明天上海天气怎么样？",
        visibility_scope=frozenset({VisibilityScope.ISSUER_ONLY}),
        metadata={"sender_name": "朋友", "group_id": ""},
    )
    group = Event.new(
        event_type=EventType.MESSAGE,
        occurred_at=_dt(1, 11),
        platform=W,
        bot_account_id="bot_a",
        chat_id="g1",
        chat_type="group",
        actor_person_id="p_member",
        content="群里讨论周末的聚会安排",
        visibility_scope=frozenset({VisibilityScope.GROUP_MEMBERS}),
        metadata={"sender_name": "群友", "group_id": "g1"},
    )
    thought = Event.new(
        event_type=EventType.THOUGHT,
        occurred_at=_dt(1, 12),
        platform=W,
        bot_account_id="bot_a",
        chat_id="friend_raw",
        chat_type="private",
        actor_person_id="p_owner",
        content="原始思维链不能被投影",
        sensitivity=Sensitivity.OWNER_CHAIN_OF_THOUGHT,
        visibility_scope=frozenset({VisibilityScope.OWNER_ONLY}),
        metadata={"group_id": ""},
    )
    for event in (fact, question, group, thought):
        store.append(event)
    return fact, question, group, thought


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


def test_migration_35_creates_projection_tables() -> None:
    import sqlite3

    tmp = _make_temp_dir()
    meta = None
    db_path = tmp / "memory.db"
    try:
        meta, store, repo, db_path = _build(tmp)
        conn = sqlite3.connect(str(db_path))
        try:
            tables = {row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )}
            version = conn.execute("SELECT MAX(version) FROM _schema_version").fetchone()[0]
        finally:
            conn.close()
        assert "memory_projections" in tables
        assert "memory_projection_cursors" in tables
        assert version >= 35
    finally:
        _cleanup(meta, db_path)
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------- ENG-23
# 投影游标旧语义 = ``max(sequence)`` + 读侧 ``sequence > :cursor``：
#   ① 跨批（批 A 以 seq=S 结尾 → 游标=S → 批 B 读不到同号 S 的后续行）；
#   ② 迟到行（游标到 S 之后才落库的同号行，现网 2554 就是它）。
# 修法 = 复合键 ``(sequence, id)``。下面两个用例用「199 filler + 两条同号 6083」
# 的场景锁住，缺任何半边都会红。
DUPLICATE_SEQUENCE = 6083
FILLER_SEQUENCE = 6082


def _filler_events(count: int = 199, *, sequence: int = FILLER_SEQUENCE):
    events = []
    for index in range(count):
        events.append(
            Event.new(
                event_type=EventType.MESSAGE,
                occurred_at=_dt(3, 9),
                platform=W,
                bot_account_id="bot_a",
                chat_id="friend_raw",
                chat_type="private",
                actor_person_id="p_friend",
                content=f"第{index}条填充消息，用于把游标推到重号前一行。",
                visibility_scope=frozenset({VisibilityScope.ISSUER_ONLY}),
                metadata={"sender_name": "朋友", "group_id": ""},
            )
        )
    return events


def _append_with_sequence(store: TerminalEventStore, events, sequence: int):
    """按给定 ``sequence`` 落库；返回落库后的 ``(events, row ids)``。

    行 id 必须在写入后回查（自增主键由库分配），游标断言要用它。
    """
    written = []
    for event in events:
        event.sequence = sequence
        store.append(event)
        written.append(event)
    ids = [store.sequence_row_id(event.event_id) for event in written]
    assert all(ids), "sequence_row_id 回查失败，用例前提不成立"
    return written, ids


def _mem_projector(tmp: Path):
    meta, store, repo, db_path = _build(tmp)
    return meta, store, repo, db_path, MemoryProjector(store, repo)


def test_same_sequence_rows_across_batches_are_not_skipped() -> None:
    """① 核心回归：批边界落在重号上时，后一行必须照样投影。"""
    tmp = _make_temp_dir()
    meta = None
    db_path = tmp / "memory.db"
    try:
        meta, store, repo, db_path, projector = _mem_projector(tmp)
        for event in _filler_events(199):
            event.sequence = FILLER_SEQUENCE
            store.append(event)
        twins, twin_ids = _append_with_sequence(
            store,
            [
                Event.new(
                    event_type=EventType.MESSAGE,
                    occurred_at=_dt(3, 10),
                    platform=W,
                    bot_account_id="bot_a",
                    chat_id="friend_raw",
                    chat_type="private",
                    actor_person_id="p_friend",
                    content="重号第一行：我要记住周末的聚会。",
                    visibility_scope=frozenset({VisibilityScope.ISSUER_ONLY}),
                    metadata={"sender_name": "朋友", "group_id": ""},
                ),
                Event.new(
                    event_type=EventType.MESSAGE,
                    occurred_at=_dt(3, 11),
                    platform=W,
                    bot_account_id="bot_a",
                    chat_id="friend_raw",
                    chat_type="private",
                    actor_person_id="p_friend",
                    content="重号第二行：这条是旧游标永远读不到的那条。",
                    visibility_scope=frozenset({VisibilityScope.ISSUER_ONLY}),
                    metadata={"sender_name": "朋友", "group_id": ""},
                ),
            ],
            DUPLICATE_SEQUENCE,
        )

        # 第一次：limit=200 正好停在第一条 6083 上，游标进到 (6083, id1)。
        first = projector.project_pending(limit=200)
        assert first["scanned"] == 200, first
        assert first["cursor"] == DUPLICATE_SEQUENCE
        assert projector.projection_cursor_pair() == (DUPLICATE_SEQUENCE, twin_ids[0])
        assert repo.get("proj:" + twins[0].event_id) is not None
        assert repo.get("proj:" + twins[1].event_id) is None, "第二行还没轮到"

        # 第二次：旧语义下这里 scanned==0（同号被 `> S` 跳过），修好后必须读到。
        second = projector.project_pending(limit=200)
        assert second["scanned"] == 1, second
        assert second["projected"] == 1, second
        assert repo.get("proj:" + twins[1].event_id) is not None
        assert projector.projection_cursor_pair() == (DUPLICATE_SEQUENCE, twin_ids[1])

        # 第三次：幂等（游标是唯一游标位，重跑不重复、不倒退）。
        third = projector.project_pending(limit=200)
        assert third["scanned"] == 0 and third["projected"] == 0, third
        assert projector.projection_cursor_pair() == (DUPLICATE_SEQUENCE, twin_ids[1])
    finally:
        _cleanup(meta, db_path)
        shutil.rmtree(tmp, ignore_errors=True)


def test_late_arriving_same_sequence_row_is_projected() -> None:
    """② 迟到行：游标已到 S，S 之后才落库的同号行仍要投影（现网 2554 场景）。"""
    tmp = _make_temp_dir()
    meta = None
    db_path = tmp / "memory.db"
    try:
        meta, store, repo, db_path, projector = _mem_projector(tmp)
        for index, event in enumerate(_filler_events(3)):
            event.sequence = 10 + index
            store.append(event)
        first_batch, _ids = _append_with_sequence(
            store,
            [
                Event.new(
                    event_type=EventType.MESSAGE,
                    occurred_at=_dt(3, 10),
                    platform=W,
                    bot_account_id="bot_a",
                    chat_id="friend_raw",
                    chat_type="private",
                    actor_person_id="p_friend",
                    content="游标推进会走过这一条。",
                    visibility_scope=frozenset({VisibilityScope.ISSUER_ONLY}),
                    metadata={"sender_name": "朋友", "group_id": ""},
                )
            ],
            DUPLICATE_SEQUENCE,
        )
        projector.project_pending(limit=500)
        assert projector.projection_cursor_pair()[0] == DUPLICATE_SEQUENCE

        # 迟到行：同号、更晚落库（行 id 更大）→ 旧 `> S` 永久读不到。
        late = Event.new(
            event_type=EventType.MESSAGE,
            occurred_at=_dt(3, 12),
            platform=W,
            bot_account_id="bot_a",
            chat_id="friend_raw",
            chat_type="private",
            actor_person_id="p_friend",
            content="迟到行：我要记住这条，它和上一条同号。",
            visibility_scope=frozenset({VisibilityScope.ISSUER_ONLY}),
            metadata={"sender_name": "朋友", "group_id": ""},
        )
        late.sequence = DUPLICATE_SEQUENCE
        store.append(late)
        late_id = store.sequence_row_id(late.event_id)

        caught = projector.project_pending(limit=50)
        assert caught["scanned"] == 1, caught
        assert repo.get("proj:" + late.event_id) is not None
        assert repo.get("proj:" + first_batch[0].event_id) is not None
        assert projector.projection_cursor_pair() == (DUPLICATE_SEQUENCE, late_id)
        assert projector.project_pending(limit=50)["scanned"] == 0
    finally:
        _cleanup(meta, db_path)
        shutil.rmtree(tmp, ignore_errors=True)


def test_upsert_keeps_valid_to_when_replaying() -> None:
    """③ ``valid_to`` 保护：重放不得把已失效行清回 NULL（现网 53 条）。"""
    tmp = _make_temp_dir()
    meta = None
    db_path = tmp / "memory.db"
    try:
        meta, store, repo, db_path, projector = _mem_projector(tmp)
        events = _filler_events(2)
        for index, event in enumerate(events):
            event.sequence = 100 + index
            store.append(event)
        projector.project_pending(limit=50)
        target_id = "proj:" + events[0].event_id
        closed_at = datetime(2026, 9, 30, 12, 0, 0)
        assert repo.invalidate([target_id], at=closed_at) == 1
        assert repo.get(target_id).valid_to == closed_at

        # 重放（backfill 从 0 扫全表）→ 失败模式就是这一列被覆写回 NULL。
        projector.backfill(limit=500)
        assert repo.get(target_id).valid_to == closed_at, "重放复活了已失效记忆"
        assert projector.project_pending(limit=50)["scanned"] == 0
        assert repo.get(target_id).valid_to == closed_at
    finally:
        _cleanup(meta, db_path)
        shutil.rmtree(tmp, ignore_errors=True)


def test_backfill_clears_both_cursor_fields() -> None:
    """⑥ ``backfill()`` 必须整对清游标（只清 sequence 会留下大 id）。"""
    tmp = _make_temp_dir()
    meta = None
    db_path = tmp / "memory.db"
    try:
        meta, store, repo, db_path, projector = _mem_projector(tmp)
        for index, event in enumerate(_filler_events(3)):
            event.sequence = 200 + index
            store.append(event)
        projector.project_pending(limit=50)
        first_pair = projector.projection_cursor_pair()
        assert first_pair[1] > 0, "先要有非零 last_id"

        # 只清 sequence 的旧 backfill 会留下大 id：重放从表中间开始，
        # 前几行永远补不回来。这里用「清掉的 id 必须回到 0 起点能解释的位置」锁住。
        projector.backfill(limit=50)
        pair_after = projector.projection_cursor_pair()
        expected_last = store.sequence_row_id(
            store.since_sequence(0, limit=3, last_id=0)[-1].event_id
        )
        assert pair_after == (200 + 2, expected_last), pair_after
        assert repo.get_cursor_pair("terminal_event") == pair_after
        assert repo.count() == 3, "整对清后重放不新增行（确定性 id）"
    finally:
        _cleanup(meta, db_path)
        shutil.rmtree(tmp, ignore_errors=True)


def test_migration_53_is_idempotent_and_matches_fresh_schema() -> None:
    """⑤ 迁移 53 幂等：跑两次结构一致；老库（无 last_id）由 ALTER 补齐。"""
    import sqlite3

    tmp = _make_temp_dir()
    meta = None
    db_path = tmp / "memory.db"
    try:
        meta, store, repo, db_path = _build(tmp)
        with sqlite3.connect(str(db_path)) as conn:
            before = {
                table: [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]
                for table in ("memory_projection_cursors", "analysis_projection_cursors")
            }
            indexes_before = sorted(
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='index'"
                )
            )
        for table in ("memory_projection_cursors", "analysis_projection_cursors"):
            assert "last_id" in before[table], (table, before[table])
        assert "ix_terminal_events_sequence" in indexes_before
        run_migrations(str(db_path))  # 第二次：应完全 no-op
        with sqlite3.connect(str(db_path)) as conn:
            after = {
                table: [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]
                for table in ("memory_projection_cursors", "analysis_projection_cursors")
            }
            indexes_after = sorted(
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='index'"
                )
            )
        assert after == before
        assert indexes_after == indexes_before

        # 老库形状（v52：游标表没有 last_id）→ 53 用 ALTER 补齐。
        legacy = tmp / "legacy.db"
        with sqlite3.connect(str(legacy)) as conn:
            conn.execute(
                "CREATE TABLE memory_projection_cursors ("
                "key TEXT PRIMARY KEY, last_sequence INTEGER DEFAULT 0, "
                "updated_at DATETIME)"
            )
            conn.execute(
                "CREATE TABLE analysis_projection_cursors ("
                "key TEXT PRIMARY KEY, last_sequence INTEGER DEFAULT 0, "
                "updated_at DATETIME)"
            )
            conn.execute(
                "CREATE TABLE terminal_events (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "sequence INTEGER DEFAULT 0)"
            )
            conn.execute("CREATE TABLE account_registry (account_id TEXT PRIMARY KEY)")
            conn.execute("CREATE TABLE _schema_version (version INTEGER PRIMARY KEY, description TEXT)")
            conn.execute("INSERT INTO _schema_version VALUES (52, 'work_items')")
        run_migrations(str(legacy))
        with sqlite3.connect(str(legacy)) as conn:
            for table in ("memory_projection_cursors", "analysis_projection_cursors"):
                columns = [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]
                assert "last_id" in columns, (table, columns)
            version = conn.execute("SELECT MAX(version) FROM _schema_version").fetchone()[0]
        assert version >= 53
    finally:
        _cleanup(meta, db_path)
        shutil.rmtree(tmp, ignore_errors=True)


def test_project_event_extracts_fact_and_skips_thought() -> None:
    tmp = _make_temp_dir()
    meta = None
    db_path = tmp / "memory.db"
    try:
        meta, store, repo, db_path = _build(tmp)
        fact_event, question_event, group_event, thought_event = _seed_events(store)
        projector = MemoryProjector(store, repo)
        stats = projector.project_events(store.all())
        assert stats["scanned"] == 4
        assert stats["projected"] == 3

        fact = repo.get("proj:" + fact_event.event_id)
        assert fact is not None
        assert fact.memory_type == "fact"
        assert "美式咖啡" in fact.content
        assert fact.source_event_ids == (fact_event.event_id,)
        assert fact.platform == W and fact.bot_account_id == "bot_a"

        question = repo.get("proj:" + question_event.event_id)
        assert question is not None and question.memory_type == "episode"
        assert repo.get("proj:" + thought_event.event_id) is None
        assert repo.get("proj:" + group_event.event_id) is not None
        assert repo.count() == 3
    finally:
        _cleanup(meta, db_path)
        shutil.rmtree(tmp, ignore_errors=True)


def test_project_pending_is_incremental_and_idempotent() -> None:
    tmp = _make_temp_dir()
    meta = None
    db_path = tmp / "memory.db"
    try:
        meta, store, repo, db_path = _build(tmp)
        _seed_events(store)
        projector = MemoryProjector(store, repo)
        first = projector.project_pending(limit=2)
        assert first["scanned"] == 2 and first["projected"] == 2
        second = projector.project_pending(limit=10)
        assert second["scanned"] == 2 and second["projected"] == 1
        third = projector.project_pending(limit=10)
        assert third["scanned"] == 0 and third["projected"] == 0
        assert repo.count() == 3

        # Re-running a backfill must not duplicate rows.
        projector.backfill(limit=100)
        assert repo.count() == 3
    finally:
        _cleanup(meta, db_path)
        shutil.rmtree(tmp, ignore_errors=True)


def test_gateway_prefers_projections_and_keeps_scopes() -> None:
    tmp = _make_temp_dir()
    meta = None
    db_path = tmp / "memory.db"
    try:
        meta, store, repo, db_path = _build(tmp)
        identity = _identity()
        _seed_events(store)
        MemoryProjector(store, repo).project_events(store.all())

        gateway = MemoryGateway(
            event_store=store,
            identity_service=identity,
            projection_repository=repo,
        )
        owner = _principal(identity, "owner_raw")
        owner_hits = gateway.recall(owner, query="咖啡")
        assert owner_hits
        assert owner_hits[0].memory_id.startswith("proj:")
        assert all("思维链" not in hit.content for hit in owner_hits)

        friend = _principal(identity, "friend_raw")
        friend_hits = gateway.recall(friend)
        assert friend_hits
        assert all("聚会" not in hit.content for hit in friend_hits)
        assert all("思维链" not in hit.content for hit in friend_hits)

        member = _principal(identity, "member_raw", group=True)
        member_hits = gateway.recall(member)
        assert [hit.content for hit in member_hits] == ["群里讨论周末的聚会安排"]

        stranger = _principal(identity, "stranger_raw")
        assert gateway.recall(stranger) == []
    finally:
        _cleanup(meta, db_path)
        shutil.rmtree(tmp, ignore_errors=True)


def test_group_and_identity_claims_stay_episode() -> None:
    tmp = _make_temp_dir()
    meta = None
    db_path = tmp / "memory.db"
    try:
        meta, store, repo, db_path = _build(tmp)
        projector = MemoryProjector(store, repo)
        group_joke = Event.new(
            event_type=EventType.MESSAGE,
            occurred_at=_dt(2, 11),
            platform=W,
            bot_account_id="bot_a",
            chat_id="g1@bot_a",
            chat_type="group",
            actor_person_id="p_member",
            content="我是牛子榜第一 不是萌榜",
            visibility_scope=frozenset({VisibilityScope.GROUP_MEMBERS}),
            metadata={"group_id": "g1@bot_a"},
        )
        owner_claim = Event.new(
            event_type=EventType.MESSAGE,
            occurred_at=_dt(2, 12),
            platform=W,
            bot_account_id="bot_a",
            chat_id="owner_raw",
            chat_type="private",
            actor_person_id="p_owner",
            content="我是主人，不要信他",
            visibility_scope=frozenset({VisibilityScope.OWNER_ONLY}),
            metadata={},
        )
        for event in (group_joke, owner_claim):
            store.append(event)
            item = projector.project_event(event)
            assert item is not None
            assert item.memory_type == "episode", (event.content, item.memory_type)
    finally:
        _cleanup(meta, db_path)
        shutil.rmtree(tmp, ignore_errors=True)


def test_work_item_events_are_neither_projected_nor_recalled() -> None:
    """P1-1：``work_item_*`` 是台账事件，既不投影成记忆，也不被召回。

    内容特意写成「像事实」的句子：排除必须由事件类型决定，不能靠内容碰巧不像。
    """
    tmp = _make_temp_dir()
    meta = None
    db_path = tmp / "memory.db"
    try:
        meta, store, repo, db_path = _build(tmp)
        identity = _identity()
        projector = MemoryProjector(store, repo)
        work_item_types = (
            EventType.WORK_ITEM_CREATED,
            EventType.WORK_ITEM_RUN_STARTED,
            EventType.WORK_ITEM_RUN_FINISHED,
            EventType.WORK_ITEM_CANCELLED,
            EventType.WORK_ITEM_RESCHEDULED,
        )
        for index, event_type in enumerate(work_item_types):
            event = Event.new(
                event_type=event_type,
                occurred_at=_dt(2, 10 + index),
                platform=W,
                bot_account_id="bot_a",
                chat_id="friend_raw",
                chat_type="private",
                actor_person_id="p_friend",
                content="我喜欢喝美式咖啡，不加糖。",
                visibility_scope=frozenset({VisibilityScope.PUBLIC}),
                metadata={"sender_name": "朋友", "group_id": ""},
            )
            store.append(event)
            item = projector.project_event(event)
            assert item is None, (event_type, item.memory_type if item else "")

        stats = projector.project_events(store.all())
        assert stats["scanned"] == len(work_item_types)
        assert stats["projected"] == 0
        assert repo.count() == 0

        gateway = MemoryGateway(
            event_store=store,
            identity_service=identity,
            projection_repository=repo,
        )
        owner = _principal(identity, "owner_raw")
        assert gateway.recall(owner, query="美式咖啡") == []
    finally:
        _cleanup(meta, db_path)
        shutil.rmtree(tmp, ignore_errors=True)


def test_row_id_lookup_failure_does_not_livelock_on_same_sequence() -> None:
    """行 id 回查失败时不能把游标写成 ``(S, 0)``：读侧会反复读同号行（死循环）。

    这条回退路径只在「store 不支持 / 回查失败」时走（现网 store 支持），代价是
    那次可能少投同号行里的后几行；但**必须**保证游标真的往前走了。
    """

    class _NoRowIdStore(TerminalEventStore):
        def sequence_row_id(self, event_id: str) -> int:
            return 0

    tmp = _make_temp_dir()
    meta = None
    db_path = tmp / "memory.db"
    try:
        meta, store, repo, db_path = _build(tmp)
        blind = _NoRowIdStore(meta.engine)
        projector = MemoryProjector(blind, repo)
        # 号显式给死：不依赖库内取号（`MAX+1` 在空表 / 已有行时都容易把用例写脆）。
        _append_with_sequence(store, _filler_events(2), FILLER_SEQUENCE)
        twins, _ids = _append_with_sequence(
            store,
            [
                Event.new(
                    event_type=EventType.MESSAGE,
                    occurred_at=_dt(3, 10),
                    platform=W,
                    bot_account_id="bot_a",
                    chat_id="friend_raw",
                    chat_type="private",
                    actor_person_id="p_friend",
                    content=f"回退路径第{index}行：同号两行。",
                    visibility_scope=frozenset({VisibilityScope.ISSUER_ONLY}),
                    metadata={"sender_name": "朋友", "group_id": ""},
                )
                for index in range(2)
            ],
            DUPLICATE_SEQUENCE,
        )

        # limit=3 让第一批正好以「重号那一行」结尾（回查到 0 → 触发回退分支）。
        first = projector.project_pending(limit=3)
        assert first["scanned"] == 3, first
        assert repo.get("proj:" + twins[0].event_id) is not None
        assert projector.projection_cursor_pair() == (DUPLICATE_SEQUENCE + 1, 0), (
            f"回退路径必须越过同号区间，否则下一轮会再读同一批（死循环）："
            f"{projector.projection_cursor_pair()}"
        )
        assert projector.project_pending(limit=3)["scanned"] == 0
    finally:
        _cleanup(meta, db_path)
        shutil.rmtree(tmp, ignore_errors=True)


def _analysis_sources(analysis_repo, event) -> bool:
    """该事件的 ``event_id`` 有没有出现在任何分析投影的 ``source_event_ids`` 里。"""
    return any(
        event.event_id in draft.source_event_ids for draft in analysis_repo.all()
    )


def test_analysis_worker_does_not_skip_same_sequence_rows() -> None:
    """④ 分析投影同构用例：``latest_sequence`` + ``> cursor`` 是同一个 bug。"""
    from core.analysis.workers import AnalysisFromEventsWorker

    tmp = _make_temp_dir()
    meta = None
    db_path = tmp / "memory.db"
    try:
        meta, store, repo, db_path = _build(tmp)
        analysis_repo = meta.analysis_projections
        for index in range(19):
            event = Event.new(
                event_type=EventType.MESSAGE,
                occurred_at=_dt(4, 9),
                platform=W,
                bot_account_id="bot_a",
                chat_id="u1",
                chat_type="private",
                actor_person_id="u1",
                actor_account_id="u1",
                content=f"第{index}条分析填充消息。",
                metadata={"msg_id": f"a{index}", "sender_name": "P6"},
            )
            event.sequence = FILLER_SEQUENCE
            store.append(event)
        twins = [
            Event.new(
                event_type=EventType.MESSAGE,
                occurred_at=_dt(4, 10 + index),
                platform=W,
                bot_account_id="bot_a",
                chat_id="u1",
                chat_type="private",
                actor_person_id="u1",
                actor_account_id="u1",
                content=f"同号分析消息第{index + 1}行。",
                metadata={"msg_id": f"dup{index}", "sender_name": "P6"},
            )
            for index in range(2)
        ]
        for event in twins:
            event.sequence = DUPLICATE_SEQUENCE
            store.append(event)
        twin_ids = [store.sequence_row_id(event.event_id) for event in twins]

        worker = AnalysisFromEventsWorker(store, analysis_repo)
        first = worker.run(limit=20)
        assert first["scanned"] == 20, first
        assert first["cursor"] == DUPLICATE_SEQUENCE
        assert analysis_repo.get_cursor_pair("analysis_from_events") == (
            DUPLICATE_SEQUENCE,
            twin_ids[0],
        )
        assert _analysis_sources(analysis_repo, twins[0]), "第一行应已投影"
        assert not _analysis_sources(analysis_repo, twins[1]), "第二行还没轮到"

        second = worker.run(limit=20)
        assert second["scanned"] == 1, second
        assert _analysis_sources(analysis_repo, twins[1]), "同号第二行没进分析投影"
        assert analysis_repo.get_cursor_pair("analysis_from_events") == (
            DUPLICATE_SEQUENCE,
            twin_ids[1],
        )
        assert worker.run(limit=20)["scanned"] == 0
    finally:
        _cleanup(meta, db_path)
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    tests = [
        test_migration_35_creates_projection_tables,
        test_project_event_extracts_fact_and_skips_thought,
        test_group_and_identity_claims_stay_episode,
        test_work_item_events_are_neither_projected_nor_recalled,
        test_project_pending_is_incremental_and_idempotent,
        test_gateway_prefers_projections_and_keeps_scopes,
        # ENG-23
        test_same_sequence_rows_across_batches_are_not_skipped,
        test_late_arriving_same_sequence_row_is_projected,
        test_upsert_keeps_valid_to_when_replaying,
        test_backfill_clears_both_cursor_fields,
        test_migration_53_is_idempotent_and_matches_fresh_schema,
        test_analysis_worker_does_not_skip_same_sequence_rows,
        test_row_id_lookup_failure_does_not_livelock_on_same_sequence,
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
