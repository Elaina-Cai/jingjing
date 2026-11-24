"""P0-WP3：N3 焦点状态（``session_focus`` 每会话一行的最小跨轮状态）。

被验对象是**状态载体**：迁移 54 + ``db.meta_models.SessionFocusState`` +
``db/repo_focus.py``。**本 WP 不做聚类、不改 prompt、不接线消费方**（P1），
所以这里只锁四件事：

* **建表**：迁移 54 幂等（跑两次 PRAGMA 全等）、老库能补表；
* **往返**：``upsert`` → ``get``（含 ``suspended_json``）、``revision`` 可观测；
* **降级**：**读不到返回 ``None`` 且不抛**（丢行只损失"一句确认"，不得拒绝服务）；
* **parity**：``create_all`` 库 vs 「旧库 + ``run_migrations``」库的 PRAGMA 逐列
  对照（照 ``tests/test_work_item_p1.py::test_ddl_parity_and_migration_registration``
  的做法），并断言 **54 不在两个版本元组里**（进哪个都是"哪里都不执行"）。

隔离：库全部落在 ``data/_focus_state_test/``；本用例**不碰生产库**
（生产库的迁移由下次重启的 bootstrap 跑，见报告"未做"）。

跑法::

    .\\.venv\\Scripts\\python.exe tests\\test_focus_state.py
"""

from __future__ import annotations

import os
import sqlite3
import sys
import traceback
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sqlalchemy import create_engine  # noqa: E402

from db import migrations  # noqa: E402
from db.meta_models import MetaBase, SessionFocusState  # noqa: E402
from db.repo_focus import FocusStateRepository, normalize_suspended  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
_ISO_ROOT = (ROOT / "data" / "_focus_state_test").resolve()
_ISO_ROOT.mkdir(parents=True, exist_ok=True)

TABLE = "session_focus"
ACTOR = ("wechat", "bot_focus", "room@chatroom")


# --------------------------------------------------------------------- 隔离设施
def _fresh_path(name: str) -> Path:
    path = _ISO_ROOT / f"{name}.db"
    for suffix in ("", "-wal", "-shm"):
        Path(str(path) + suffix).unlink(missing_ok=True)
    return path


def _create_all_engine(path: Path):
    """``create_all`` 出来的干净 meta 库（新库路径）。"""
    engine = create_engine(f"sqlite:///{path}")
    MetaBase.metadata.create_all(engine)
    return engine


def _table_shape(path: Path, table: str = TABLE):
    """PRAGMA 形状：列（名/类型/NOT NULL/DEFAULT/PK）+ 索引 + 唯一形状。"""
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


def _drop_focus(path: Path) -> None:
    """把 ``session_focus`` 抹掉，模拟"这条迁移还没跑过"的老库。"""
    con = sqlite3.connect(str(path))
    try:
        con.execute(f"DROP TABLE IF EXISTS {TABLE}")
        con.commit()
    finally:
        con.close()


def _legacy_library(name: str, baseline: int) -> Path:
    """老库 = create_all 建好、抹掉 ``session_focus``、把版本压回 ``baseline``。"""
    path = _fresh_path(name)
    engine = _create_all_engine(path)
    engine.dispose()
    _drop_focus(path)
    con = sqlite3.connect(str(path))
    try:
        con.execute(
            "CREATE TABLE IF NOT EXISTS _schema_version (version INTEGER PRIMARY KEY, "
            "description TEXT, applied_at DATETIME DEFAULT CURRENT_TIMESTAMP)"
        )
        con.execute("DELETE FROM _schema_version")
        con.execute(
            "INSERT OR REPLACE INTO _schema_version (version, description) VALUES (?, ?)",
            (baseline, "test baseline"),
        )
        con.commit()
    finally:
        con.close()
    return path


# ------------------------------------------------------- ① 迁移 54 幂等（两次全等）
def test_migration_54_is_idempotent():
    path = _fresh_path("idem")
    engine = _create_all_engine(path)
    engine.dispose()
    _drop_focus(path)                       # 从"没有这张表"开始，让 54 真的建一次

    migrations.run_migrations(str(path))
    first = _table_shape(path)
    migrations.run_migrations(str(path))    # 第二次：应完全 no-op（IF NOT EXISTS）
    second = _table_shape(path)

    assert TABLE in _tables(path)
    assert first == second, f"迁移 54 不幂等：\n{first}\n{second}"
    assert 54 in _stamped(path)
    # 建出来的列一个不少（口径 = 模型字段名）
    assert {column[0] for column in first[0]} == {
        column.name for column in SessionFocusState.__table__.columns
    }
    # 主键三列 AND NOT NULL（复合键里 NULL 会让唯一性形同虚设）
    pks = [column for column in first[0] if column[4]]
    assert [column[0] for column in pks] == ["platform", "account_id", "chat_id"]
    assert all(column[3] for column in pks), "主键列必须 NOT NULL"
    print(f"  ok 迁移 54 幂等，列={[c[0] for c in first[0]]}")


# -------------------------------------------------------- ② 老库 ALTER / 建表分支
def test_legacy_db_gets_table_created():
    """老库（版本 53、无 ``session_focus``）跑迁移 → 建表；新库不重复建。"""
    legacy = _legacy_library("legacy_v53", 53)
    assert TABLE not in _tables(legacy)

    migrations.run_migrations(str(legacy))

    assert TABLE in _tables(legacy)
    assert 54 in _stamped(legacy)
    # 老库补出来的形状 = create_all 的形状（parity 的弱化前置）
    fresh = _fresh_path("legacy_ref")
    engine = _create_all_engine(fresh)
    engine.dispose()
    assert _table_shape(legacy) == _table_shape(fresh)

    # 账号库（有 terminal_events、无 account_registry）：焦点状态同样合法 → 照建。
    account = _fresh_path("legacy_account")
    account_engine = _create_all_engine(account)
    account_engine.dispose()
    _drop_focus(account)
    con = sqlite3.connect(str(account))
    try:
        con.execute("DROP TABLE IF EXISTS account_registry")
        con.execute(
            "CREATE TABLE IF NOT EXISTS _schema_version (version INTEGER PRIMARY KEY, "
            "description TEXT, applied_at DATETIME DEFAULT CURRENT_TIMESTAMP)"
        )
        con.execute("DELETE FROM _schema_version")
        con.execute("INSERT INTO _schema_version (version, description) VALUES (53, 'test')")
        con.commit()
    finally:
        con.close()
    migrations.run_migrations(str(account))
    assert TABLE in _tables(account), "账号库也该有焦点状态（按 (platform,account_id,chat_id) 分会话）"

    # 非应用库（连 terminal_events 都没有）：守卫直接 return，不建表。
    alien = _fresh_path("legacy_alien")
    con = sqlite3.connect(str(alien))
    try:
        con.execute(
            "CREATE TABLE IF NOT EXISTS _schema_version (version INTEGER PRIMARY KEY, "
            "description TEXT, applied_at DATETIME DEFAULT CURRENT_TIMESTAMP)"
        )
        con.execute("INSERT INTO _schema_version (version, description) VALUES (53, 'test')")
        con.commit()
    finally:
        con.close()
    migrations.run_migrations(str(alien))
    assert TABLE not in _tables(alien), "非应用库不该被塞进应用表"

    print("  ok 老库补表 / 账号库照建 / 非应用库跳过")


# ------------------------------------------------- ③ upsert → get 往返（含挂起）
def test_upsert_get_roundtrip_including_suspended():
    path = _fresh_path("roundtrip")
    engine = _create_all_engine(path)
    repo = FocusStateRepository(engine)
    try:
        assert repo.get(*ACTOR) is None

        since = datetime(2026, 10, 1, 21, 15, 30)
        saved = repo.upsert(
            *ACTOR,
            current_topic="站台 / 湛江 出行安排",
            current_anchor_person_id="p_fanpan",
            previous_topic="晚饭吃什么",
            suspended=["表情包来源追问", "周末电影"],
            topic_since=since,
        )
        assert saved.revision == 1, "新建行 revision 从 1 起"
        assert saved.topic_since == since
        assert saved.suspended == ["表情包来源追问", "周末电影"]
        assert isinstance(saved.suspended_json, str) and saved.suspended_json.startswith("[")

        loaded = repo.get(*ACTOR)
        assert loaded is not None
        assert loaded.platform == ACTOR[0] and loaded.account_id == ACTOR[1]
        assert loaded.chat_id == ACTOR[2]
        assert loaded.current_topic == "站台 / 湛江 出行安排"
        assert loaded.current_anchor_person_id == "p_fanpan"
        assert loaded.previous_topic == "晚饭吃什么"
        assert loaded.suspended == ["表情包来源追问", "周末电影"], "suspended_json 往返"
        assert loaded.topic_since == since
        assert loaded.revision == 1

        # 只换焦点：没传的字段保持原值（previous_topic / suspended / topic_since）。
        moved = repo.upsert(
            *ACTOR,
            current_topic="周末电影",
            current_anchor_person_id="p_elaina",
            now=since + timedelta(minutes=3),
        )
        assert moved.revision == 2
        assert moved.current_topic == "周末电影"
        assert moved.previous_topic == "晚饭吃什么", "没传就不该被清空"
        assert moved.suspended == ["表情包来源追问", "周末电影"]
        assert moved.topic_since == since, "换话题但没给 topic_since → 保持原值"
        assert moved.updated_at == since + timedelta(minutes=3)

        # 显式清空挂起清单（传 []）与显式换 previous_topic（传 ""）。
        cleared = repo.upsert(
            *ACTOR,
            current_topic="周末电影",
            previous_topic="",
            suspended=[],
        )
        assert cleared.suspended == [] and cleared.previous_topic == ""
        assert cleared.revision == 3

        # 每会话一行：主键复合，换 chat 就是另一行。
        repo.upsert("wechat", "bot_focus", "other@chatroom", current_topic="别的会话")
        assert repo.count() == 2
        assert repo.get(*ACTOR).current_topic == "周末电影"
        assert repo.get("wechat", "bot_focus", "other@chatroom").current_topic == "别的会话"

        # 键相同、再 upsert：仍是一行（不是插入第二行）。
        repo.upsert(*ACTOR, current_topic="还是周末电影")
        assert repo.count() == 2, "同一 (platform, account_id, chat_id) 只能一行"
        print("  ok upsert→get 往返（含 suspended_json / 每会话一行）")
    finally:
        engine.dispose()


# ------------------------------------------ ④ 读不到返回 None 且不抛（降级行为）
def test_get_returns_none_when_row_or_table_missing():
    engine = create_engine("sqlite://")            # 表都没有
    repo = FocusStateRepository(engine)
    try:
        assert repo.get(*ACTOR) is None            # 表不存在 → None（不抛）
        assert repo.get("", "", "") is None
    finally:
        engine.dispose()

    path = _fresh_path("degraded")
    engine = _create_all_engine(path)
    repo = FocusStateRepository(engine)
    try:
        assert repo.get(*ACTOR) is None            # 表在、行不在 → None
        _drop_focus(path)                          # 表被抹掉（老库 / 迁移没跑）
        assert repo.get(*ACTOR) is None, "表缺失必须降级成 None"

        # 损坏的表结构（缺列）同样降级，不把坏结构当错误抛给回合。
        con = sqlite3.connect(str(path))
        try:
            con.execute(f"CREATE TABLE {TABLE} (platform TEXT PRIMARY KEY, junk TEXT)")
            con.commit()
        finally:
            con.close()
        assert repo.get(*ACTOR) is None, "结构不相容也必须降级成 None"
        assert repo.clear(*ACTOR) is False         # 读侧口径一致：不抛
        assert repo.bump_revision(*ACTOR) == 0     # 同上：无行 = 0
    finally:
        engine.dispose()
    print("  ok 读不到 → None / clear→False / bump→0（全都不抛）")


# ----------------------------------------------------- ⑤ revision 变更可观测
def test_revision_is_observable_and_bump_does_not_touch_updated_at():
    path = _fresh_path("revision")
    engine = _create_all_engine(path)
    repo = FocusStateRepository(engine)
    try:
        first = repo.upsert(*ACTOR, current_topic="A", topic_since=datetime(2026, 10, 1, 9, 0, 0))
        assert first.revision == 1

        bumped = repo.bump_revision(*ACTOR)
        assert bumped == 2, "bump_revision 返回新值"
        after_bump = repo.get(*ACTOR)
        assert after_bump.revision == 2
        assert after_bump.updated_at == first.updated_at, "bump 只推 revision，不动 updated_at"
        assert repo.bump_revision(*ACTOR) == 3

        # upsert 顺带 +1；bump=False 时不动。
        assert repo.upsert(*ACTOR, current_topic="B").revision == 4
        assert repo.upsert(*ACTOR, current_topic="C", bump=False).revision == 4

        # 无行：0（与 get→None 同义）。
        assert repo.bump_revision("wechat", "nobody", "nowhere") == 0
    finally:
        engine.dispose()
    print("  ok revision 单调可观测（bump 不动 updated_at）")


# ------------------------------------------------- ⑥ DDL 与模型 parity + 注册位置
def test_ddl_parity_and_migration_registration():
    fresh = _fresh_path("parity_fresh")
    engine = _create_all_engine(fresh)
    engine.dispose()

    legacy = _legacy_library("parity_legacy", 53)
    migrations.run_migrations(str(legacy))

    assert _table_shape(fresh) == _table_shape(legacy), "create_all vs 迁移 54 的建表漂移"

    stamped = _stamped(legacy)
    assert 54 in stamped and max(stamped) >= 54

    # 两个坑的规避：54 不在两个注册元组里（进哪个都会「哪里都不执行」）。
    assert 54 not in migrations._META_MIGRATION_VERSIONS, "进了元链 → 40–53 在 META 永久跳过"
    assert 54 not in migrations._ACCOUNT_SKIP_MIGRATION_VERSIONS, "进了 skip 集 → 哪里都不执行"
    assert any(m.version == 54 for m in migrations._MIGRATIONS), "54 必须注册"

    # 所有 NOT NULL 列都有 server_default（仓库既有教训：迁移只传部分字段也能插）。
    for column in SessionFocusState.__table__.columns:
        if column.nullable:
            continue
        assert column.server_default is not None, f"NOT NULL 列缺 server_default: {column.name}"

    # 索引：模型与迁移同名同列（parity 已覆盖形状，这里锁名字，防止改名后静默漂移）。
    model_indexes = {index.name: tuple(index.columns.keys())
                     for index in SessionFocusState.__table__.indexes}
    assert model_indexes == {
        "ix_session_focus_anchor": ("current_anchor_person_id",),
        "ix_session_focus_updated": ("updated_at",),
    }, model_indexes
    _columns, names, _shapes = _table_shape(legacy)
    assert names == sorted(model_indexes), f"迁移里的索引与模型不一致: {names}"

    # 主键 = (platform, account_id, chat_id)：复合唯一，不靠第二套唯一索引。
    assert [column.name for column in SessionFocusState.__table__.primary_key.columns] == [
        "platform", "account_id", "chat_id"
    ]
    print("  ok DDL/模型 parity + 54 注册位置 + NOT NULL 全带 server_default")


# ---------------------------------------------- ⑦ 序列化与写路径失败要能被看见
def test_suspended_normalisation_and_write_failure_is_loud():
    import json as _json

    def _as_list(value):
        return _json.loads(normalize_suspended(value))

    assert normalize_suspended(None) == "[]"
    assert normalize_suspended([]) == "[]"
    assert _as_list(["a"]) == ["a"]
    assert _as_list('["a","b"]') == ["a", "b"]      # 已是 JSON 文本 → 规范化
    assert _as_list("a") == ["a"]                   # 裸文本当单个话题
    assert _as_list({"topic": "x"}) == [{"topic": "x"}]
    assert _as_list(_json.dumps(["湛江", "电影"], ensure_ascii=False)) == ["湛江", "电影"]

    path = _fresh_path("loud")
    engine = _create_all_engine(path)
    repo = FocusStateRepository(engine)
    try:
        repo.upsert(*ACTOR, current_topic="A", suspended=["x"])
        _drop_focus(path)
        # 写路径**不**降级：写不进去是配置/磁盘问题，必须被看见（读路径才降级）。
        try:
            repo.upsert(*ACTOR, current_topic="B")
        except Exception as exc:                   # noqa: BLE001 - 只验"要抛"
            assert "session_focus" in str(exc) or "no such table" in str(exc)
        else:
            raise AssertionError("表缺失时 upsert 必须抛，而不是静默丢写")
    finally:
        engine.dispose()
    print("  ok suspended 归一化 + 写失败照抛（读降级 / 写响亮）")


# ---------------------------------------------------------------- 自带 runner
def main() -> bool:
    tests = [fn for name, fn in globals().items() if name.startswith("test_")]
    failed = 0
    print("=" * 60)
    print("N3 焦点状态（session_focus）")
    print("=" * 60)
    for fn in tests:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception:
            failed += 1
            print(f"FAIL {fn.__name__}")
            traceback.print_exc()
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return failed == 0


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
