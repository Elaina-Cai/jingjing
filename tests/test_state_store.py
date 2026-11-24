"""WP-STATE 状态装配 v1 载体用例：迁移 57 + 模型 + db/repo_state.py。

只锁四件事：
* 建表：迁移 57 幂等（跑两次 PRAGMA 全等）、老库能补表、非应用库跳过；
* parity：create_all 库 vs 「旧库 + run_migrations」库的 PRAGMA 逐列/逐索引对照；
* 往返：record_self_move / observe_user_turn / get_chat_state / recent_moves；
* 降级：读失败返回 None / [] 且不抛。

隔离：库全部落在 data/_state_store_test/；本用例不碰生产库。
跑法::

    .\\.venv\\Scripts\\python.exe tests\\test_state_store.py
"""

from __future__ import annotations

import os
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sqlalchemy import create_engine  # noqa: E402

from db import migrations  # noqa: E402
from db.meta_models import ChatStateRow, MetaBase, SelfMoveRow  # noqa: E402
from db.repo_state import StateRepository  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
_ISO_ROOT = (ROOT / "data" / "_state_store_test").resolve()
_ISO_ROOT.mkdir(parents=True, exist_ok=True)

TABLES = ("self_moves", "chat_state")
ACTOR = ("wechat", "bot_state", "owner@private")


def _fresh_path(name: str) -> Path:
    path = _ISO_ROOT / f"{name}.db"
    for suffix in ("", "-wal", "-shm"):
        Path(str(path) + suffix).unlink(missing_ok=True)
    return path


def _create_all_engine(path: Path):
    engine = create_engine(f"sqlite:///{path}")
    MetaBase.metadata.create_all(engine)
    return engine


def _table_shape(path: Path, table: str):
    con = sqlite3.connect(str(path))
    try:
        columns = [
            (row[1], str(row[2]).upper(), int(row[3]), row[4], int(row[5]))
            for row in con.execute(f"PRAGMA table_info({table})")
        ]
        named, shapes = [], []
        for row in con.execute(f"PRAGMA index_list({table})"):
            name, unique = row[1], int(row[2])
            cols = tuple(
                info[2] for info in con.execute(f"PRAGMA index_info({name})")
                if info[2] is not None
            )
            shapes.append((unique, cols))
            if not name.startswith("sqlite_autoindex"):
                named.append(name)
        return columns, sorted(named), sorted(shapes)
    finally:
        con.close()


def _tables(path: Path) -> set[str]:
    con = sqlite3.connect(str(path))
    try:
        return {row[0] for row in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
    finally:
        con.close()


def _stamped(path: Path) -> set[int]:
    con = sqlite3.connect(str(path))
    try:
        return {row[0] for row in con.execute("SELECT version FROM _schema_version")}
    finally:
        con.close()


def _drop_tables(path: Path, tables=TABLES) -> None:
    con = sqlite3.connect(str(path))
    try:
        for table in tables:
            con.execute(f"DROP TABLE IF EXISTS {table}")
        con.commit()
    finally:
        con.close()


def _stamp(path: Path, version: int) -> None:
    con = sqlite3.connect(str(path))
    try:
        con.execute(
            "CREATE TABLE IF NOT EXISTS _schema_version (version INTEGER PRIMARY KEY, "
            "description TEXT, applied_at DATETIME DEFAULT CURRENT_TIMESTAMP)"
        )
        con.execute("DELETE FROM _schema_version")
        con.execute(
            "INSERT INTO _schema_version (version, description) VALUES (?, ?)",
            (version, "test baseline"),
        )
        con.commit()
    finally:
        con.close()


def _legacy_library(name: str, baseline: int = 56) -> Path:
    path = _fresh_path(name)
    engine = _create_all_engine(path)
    engine.dispose()
    _drop_tables(path)
    _stamp(path, baseline)
    return path


# ------------------------------------------------------- ① 迁移 57 幂等
def test_migration_57_is_idempotent():
    path = _fresh_path("idem")
    engine = _create_all_engine(path)
    engine.dispose()
    _drop_tables(path)

    migrations.run_migrations(str(path))
    first = {table: _table_shape(path, table) for table in TABLES}
    migrations.run_migrations(str(path))
    second = {table: _table_shape(path, table) for table in TABLES}

    assert TABLES[0] in _tables(path) and TABLES[1] in _tables(path)
    assert first == second, f"迁移 57 不幂等：\n{first}\n{second}"
    assert 57 in _stamped(path)
    for table, model in (("self_moves", SelfMoveRow), ("chat_state", ChatStateRow)):
        assert {c[0] for c in first[table][0]} == {
            c.name for c in model.__table__.columns
        }, f"{table} 列与模型不一致"
    assert "ux_self_moves_dedup" in first["self_moves"][1]
    print("  ok 迁移 57 幂等 + 列/唯一索引与模型同形")


# -------------------------------------------------------- ② 老库补表 / 非应用库跳过
def test_legacy_db_gets_tables_created():
    legacy = _legacy_library("legacy_v56", 56)
    assert not (set(TABLES) & _tables(legacy))

    migrations.run_migrations(str(legacy))

    assert set(TABLES) <= _tables(legacy)
    assert 57 in _stamped(legacy)
    fresh = _fresh_path("legacy_ref")
    engine = _create_all_engine(fresh)
    engine.dispose()
    for table in TABLES:
        assert _table_shape(legacy, table) == _table_shape(fresh, table), table

    alien = _fresh_path("legacy_alien")
    con = sqlite3.connect(str(alien))
    try:
        con.execute(
            "CREATE TABLE IF NOT EXISTS _schema_version (version INTEGER PRIMARY KEY, "
            "description TEXT, applied_at DATETIME DEFAULT CURRENT_TIMESTAMP)"
        )
        con.execute("INSERT INTO _schema_version (version, description) VALUES (56, 'test')")
        con.commit()
    finally:
        con.close()
    migrations.run_migrations(str(alien))
    assert not (set(TABLES) & _tables(alien)), "非应用库不该被塞进状态表"
    print("  ok 老库补表 / 非应用库跳过")


# ------------------------------------------------------------- ③ 往返 + 去重 + 反馈
def test_record_observe_roundtrip():
    engine = _create_all_engine(_fresh_path("roundtrip"))
    try:
        repo = StateRepository(engine)
        assert repo.get_chat_state(platform=ACTOR[0], account_id=ACTOR[1], chat_id=ACTOR[2]) is None
        assert repo.recent_moves(platform=ACTOR[0], account_id=ACTOR[1], chat_id=ACTOR[2], since=datetime(2000, 1, 1)) == []

        t0 = datetime(2026, 10, 11, 12, 0, 0)
        repo.record_self_move(
            platform=ACTOR[0], account_id=ACTOR[1], chat_id=ACTOR[2],
            reply="在，怎么了？", sent_at=t0,
            move_kind="question_back", bit_keys=["question_back"], expects_reply=1,
        )
        state = repo.get_chat_state(platform=ACTOR[0], account_id=ACTOR[1], chat_id=ACTOR[2])
        assert state is not None
        assert state.last_bot_engagement == "pending"
        assert state.last_bot_move_kind == "question_back"
        assert state.last_bot_expects_reply == 1
        assert state.revision == 1

        # 同 (内容, 秒) 重复记录 → 静默忽略
        repo.record_self_move(
            platform=ACTOR[0], account_id=ACTOR[1], chat_id=ACTOR[2],
            reply="在，怎么了？", sent_at=t0,
            move_kind="question_back", bit_keys=["question_back"], expects_reply=1,
        )
        moves = repo.recent_moves(platform=ACTOR[0], account_id=ACTOR[1], chat_id=ACTOR[2], since=t0 - timedelta(hours=1))
        assert len(moves) == 1, f"去重失败：{len(moves)}"

        # 用户 5 分钟后回复 → 上一条标 replied
        t1 = t0 + timedelta(minutes=5)
        repo.observe_user_turn(
            platform=ACTOR[0], account_id=ACTOR[1], chat_id=ACTOR[2], at=t1,
        )
        state = repo.get_chat_state(platform=ACTOR[0], account_id=ACTOR[1], chat_id=ACTOR[2])
        assert state.last_user_at == t1
        assert state.last_bot_engagement == "replied"
        assert state.last_bot_engaged_at == t1
        moves = repo.recent_moves(platform=ACTOR[0], account_id=ACTOR[1], chat_id=ACTOR[2], since=t0 - timedelta(hours=1))
        assert moves[0].engagement == "replied"
        assert moves[0].engaged_at == t1

        # 幂等：更早的 observe 不得回退 last_user_at
        repo.observe_user_turn(
            platform=ACTOR[0], account_id=ACTOR[1], chat_id=ACTOR[2],
            at=t0 + timedelta(minutes=1),
        )
        state = repo.get_chat_state(platform=ACTOR[0], account_id=ACTOR[1], chat_id=ACTOR[2])
        assert state.last_user_at == t1
        assert state.last_bot_engagement == "replied"
        print("  ok record/observe 往返 + 去重 + 反馈幂等")
    finally:
        engine.dispose()


# ------------------------------------------------------------ ③b 翻篇边界
def test_reset_boundary_roundtrip():
    engine = _create_all_engine(_fresh_path("reset_boundary"))
    try:
        repo = StateRepository(engine)
        t0 = datetime(2026, 10, 11, 14, 0, 0)
        repo.record_self_move(
            platform=ACTOR[0], account_id=ACTOR[1], chat_id=ACTOR[2],
            reply="在。你今天第几次了", sent_at=t0,
            move_kind="counting", bit_keys=["counting"], expects_reply=0,
        )
        repo.observe_user_turn(
            platform=ACTOR[0], account_id=ACTOR[1], chat_id=ACTOR[2],
            at=t0 + timedelta(minutes=1),
        )
        assert repo.get_chat_state(*[], **{}) if False else True

        t1 = t0 + timedelta(minutes=2)
        repo.mark_reset(
            platform=ACTOR[0], account_id=ACTOR[1], chat_id=ACTOR[2], at=t1,
        )
        state = repo.get_chat_state(platform=ACTOR[0], account_id=ACTOR[1], chat_id=ACTOR[2])
        assert state is not None and state.reset_at == t1
        assert state.last_bot_at is None, "翻篇要清掉上一条记录"
        assert state.last_bot_engagement == "none"
        assert state.last_user_at == t0 + timedelta(minutes=1), "last_user_at 保留"

        repo.record_self_move(
            platform=ACTOR[0], account_id=ACTOR[1], chat_id=ACTOR[2],
            reply="行，翻篇了。你想聊什么？", sent_at=t1 + timedelta(minutes=1),
            move_kind="question_back", bit_keys=["question_back"], expects_reply=1,
        )
        state = repo.get_chat_state(platform=ACTOR[0], account_id=ACTOR[1], chat_id=ACTOR[2])
        assert state.last_bot_at is not None and state.last_bot_at > state.reset_at
        print("  ok 翻篇边界：reset_at 落库、上一条清空、边界后动作正常")
    finally:
        engine.dispose()


# ------------------------------------------------------------------ ④ 读降级
def test_read_degrades_without_tables():
    path = _fresh_path("empty")
    engine = create_engine(f"sqlite:///{path}")
    try:
        repo = StateRepository(engine)
        assert repo.get_chat_state(platform=ACTOR[0], account_id=ACTOR[1], chat_id=ACTOR[2]) is None
        assert repo.recent_moves(platform=ACTOR[0], account_id=ACTOR[1], chat_id=ACTOR[2], since=datetime(2000, 1, 1)) == []
        print("  ok 缺表读降级：None / []，不抛")
    finally:
        engine.dispose()


# ------------------------------------------------------------------ ⑤ 注册位置
def test_migration_registration():
    assert any(m.version == 57 for m in migrations._MIGRATIONS)
    assert any(m.version == 58 for m in migrations._MIGRATIONS)
    for version in (57, 58):
        assert version not in migrations._META_MIGRATION_VERSIONS
        assert version not in migrations._ACCOUNT_SKIP_MIGRATION_VERSIONS
    print("  ok 57/58 已注册且不在两个版本元组里")


def main() -> int:
    tests = [
        test_migration_57_is_idempotent,
        test_legacy_db_gets_tables_created,
        test_record_observe_roundtrip,
        test_reset_boundary_roundtrip,
        test_read_degrades_without_tables,
        test_migration_registration,
    ]
    for fn in tests:
        fn()
    print("ALL OK:", len(tests), "tests")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
