# -*- coding: utf-8 -*-
"""B6 落地回归：库内原子取号、sequence 白名单体检、失败不静默。

对应 docs/任务模型.md「事件契约 / sequence 发号约定」与
``core/terminal/sequence_policy.py``：

① 两个 store 实例（两个进程的替身）交替 append 不重号（旧的进程内计数器必重号）；
② 多线程跨实例并发 append：号唯一、无缺口、行数对得上；
③ 取号失败不静默：坏库 append 直接抛（不是悄悄发 0 号）；
④ 读不到 sequence ≠ 没有缺口：``reconcile_sequences`` 报 error + ok=False；
⑤ 白名单三段（129–491 / 717–898 / 1490–1515）内的存量空缺不算问题，
   段外新缺口必须被识别，且**只报告不修**；白名单在**调用时**解析
   （``whitelist=None`` → 模块常量），monkeypatch / 显式传参都能覆盖；
⑥ 重号只报告：线上同形数据（三段空缺 + 8 个存量重号）判 OK，新增重号判回归；
⑦ ``work_item_*`` 事件类型已注册，读写往返不降级成 ``message``；
⑧ retention 墓碑事件走 ``store.append`` 的库内取号（不许再有第二个发号者），
   墓碑写失败时上抛并**在删除之前**中止；
⑨ 唯一键冲突但回查也失败时抛错，绝不返回 sequence=0 的「假落库」事件。

Run directly:
    python tests/test_sequence_allocation.py
"""

from __future__ import annotations

import os
import shutil
import sys
import threading
import traceback
import uuid
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

ROOT = Path(__file__).resolve().parent.parent
_TMP_ROOT = ROOT / "data" / "_sequence_alloc_test"
# 隔离前导（照 tests/test_event_chain_links.py）：任何一处 init_singletons() 都会在
# 真实 meta / account 库上跑迁移，必须先把路径指到测试目录。
_TEST_LOG_DIR = str(Path("data") / "_test_logs_sequence_alloc")
Path(_TEST_LOG_DIR).mkdir(parents=True, exist_ok=True)
os.environ["LOG_DIR"] = _TEST_LOG_DIR
_ISO_ROOT = (ROOT / "data" / "_sequence_alloc_test" / "_iso").resolve()
_ISO_ROOT.mkdir(parents=True, exist_ok=True)
os.environ["META_DB_PATH"] = str(_ISO_ROOT / "meta.db")
os.environ["PLATFORMS_DIR"] = str(_ISO_ROOT / "platforms")
os.environ["DATA_DIR"] = str(_ISO_ROOT / "data")

from sqlalchemy import create_engine, delete, insert  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from core.terminal.event_model import Event, EventType  # noqa: E402
from core.terminal.event_store_db import TerminalEventStore  # noqa: E402
from core.terminal.reconciler import reconcile_sequences  # noqa: E402
from core.terminal.sequence_policy import (  # noqa: E402
    SEQUENCE_DUPLICATE_WHITELIST,
    SEQUENCE_GAP_WHITELIST,
    count_numbers,
    find_duplicates,
    find_gap_ranges,
    is_whitelisted_duplicate,
    split_gap_ranges,
)

W = "wechat"
ACCOUNT = "bot_a"
WORK_ITEM_EVENT_TYPES = (
    "work_item_created",
    "work_item_run_started",
    "work_item_run_finished",
    "work_item_cancelled",
    "work_item_rescheduled",
)


def _make_temp_dir() -> Path:
    _TMP_ROOT.mkdir(parents=True, exist_ok=True)
    path = _TMP_ROOT / f"case_{uuid.uuid4().hex}"
    path.mkdir(parents=True, exist_ok=False)
    return path


def _dt(hour: int = 12, minute: int = 0) -> datetime:
    """naive 本地墙钟（与事件流口径一致）。"""
    return datetime(2026, 9, 30, hour, minute, 0)


def _build_store(tmp: Path):
    from db.meta_repo import MetaRepositoryManager
    from db.migrations import run_migrations

    db_path = tmp / "sequence_meta.db"
    meta = MetaRepositoryManager(str(db_path))
    run_migrations(str(db_path))
    return meta, TerminalEventStore(meta.engine), db_path


def _cleanup(meta, db_path: Path) -> None:
    if meta is not None:
        try:
            meta.engine.dispose()
        except Exception:
            pass
    for suffix in ("", "-wal", "-shm"):
        try:
            db_path.with_name(db_path.name + suffix).unlink(missing_ok=True)
        except (PermissionError, FileNotFoundError):
            pass


def _event(content: str = "x", sequence: int = 0) -> Event:
    return Event.new(
        event_type=EventType.MESSAGE,
        occurred_at=_dt(),
        platform=W,
        bot_account_id=ACCOUNT,
        chat_id="friend_raw",
        chat_type="private",
        content=content,
        sequence=sequence,
    )


def test_two_store_instances_do_not_duplicate_sequence() -> None:
    """同一进程里两个 store 实例 = 两个进程的替身：旧计数器实现必重号。"""
    tmp = _make_temp_dir()
    meta = None
    db_path = tmp / "sequence_meta.db"
    try:
        meta, store, db_path = _build_store(tmp)
        first = store.append(_event("a"))
        assert first.sequence == 1, first.sequence

        other = TerminalEventStore(meta.engine)  # 另一个进程视角：不再读 max 起步
        second = other.append(_event("b"))
        third = store.append(_event("c"))
        assert [first.sequence, second.sequence, third.sequence] == [1, 2, 3]
        assert store.count() == 3
    finally:
        _cleanup(meta, db_path)
        shutil.rmtree(tmp, ignore_errors=True)


def test_concurrent_appends_across_instances_are_unique() -> None:
    """多线程 × 双实例并发：号唯一且连续（无重号、无缺口）。"""
    tmp = _make_temp_dir()
    meta = None
    db_path = tmp / "sequence_meta.db"
    try:
        meta, store, db_path = _build_store(tmp)
        stores = [store, TerminalEventStore(meta.engine)]
        per_thread = 20
        workers = 4
        collected: list[int] = []
        guard = threading.Lock()

        def _worker(index: int) -> None:
            local = []
            for _ in range(per_thread):
                event = stores[index % len(stores)].append(_event(f"t{index}"))
                local.append(event.sequence)
            with guard:
                collected.extend(local)

        threads = [threading.Thread(target=_worker, args=(i,)) for i in range(workers)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        expected = workers * per_thread
        assert len(collected) == expected
        assert len(set(collected)) == expected, "并发下出现重号"
        assert sorted(collected) == list(range(1, expected + 1))
        assert store.count() == expected
    finally:
        _cleanup(meta, db_path)
        shutil.rmtree(tmp, ignore_errors=True)


def test_append_failure_is_not_silent() -> None:
    """坏库 append 必须抛：旧实现的构造期读失败会静默 return 0。"""
    tmp = _make_temp_dir()
    db_path = tmp / "no_tables.db"
    engine = None
    try:
        engine = create_engine(f"sqlite:///{db_path}")
        store = TerminalEventStore(engine)  # 构造不再读库，不炸
        raised = False
        try:
            store.append(_event("boom"))
        except Exception:
            raised = True
        assert raised, "没有 terminal_events 表的库必须报错，不能静默发号"
    finally:
        if engine is not None:
            engine.dispose()
        shutil.rmtree(tmp, ignore_errors=True)


def test_sequence_read_failure_is_reported_not_silent() -> None:
    """读不到 sequence 时报告 error + ok=False；「0 缺口」不等于「读不到」。"""
    tmp = _make_temp_dir()
    db_path = tmp / "no_tables.db"
    engine = None
    try:
        engine = create_engine(f"sqlite:///{db_path}")
        report = reconcile_sequences(TerminalEventStore(engine))
        assert report.error, "读失败必须带 error"
        assert not report.ok
        assert report.sequence_gaps == 0 and report.rows == 0
        assert "error" in report.as_dict()
        # 拿不到 sequence 的 store 也一样：不许当作「无缺口」
        duck = reconcile_sequences(object())
        assert duck.error and not duck.ok
    finally:
        if engine is not None:
            engine.dispose()
        shutil.rmtree(tmp, ignore_errors=True)


def test_whitelist_excludes_legacy_gaps_and_flags_new_ones() -> None:
    """三段白名单：段内 571 个存量空缺不报，段外缺口一个都不能漏（变异自检）。"""
    sequences = (
        list(range(1, 129))
        + list(range(492, 717))
        + list(range(899, 1490))
        + list(range(1516, 1601))
    )
    gaps = find_gap_ranges(sequences)
    new_gaps, known_gaps = split_gap_ranges(gaps)
    assert [(item.start, item.end) for item in known_gaps] == [
        (129, 491),
        (717, 898),
        (1490, 1515),
    ]
    assert count_numbers(known_gaps) == 571
    assert count_numbers(new_gaps) == 0
    assert [list(item) for item in SEQUENCE_GAP_WHITELIST] == [
        [129, 491],
        [717, 898],
        [1490, 1515],
    ]
    # 变异自检：白名单一旦被清空，同样的数据会被报成 571 个新缺口。
    assert count_numbers(split_gap_ranges(gaps, ())[0]) == 571
    # 段外缺口必须被识别（例如 1601 之后又跳到 1700）。
    new_gaps = split_gap_ranges(find_gap_ranges(sequences + [1700]))[0]
    assert [(item.start, item.end) for item in new_gaps] == [(1601, 1699)]


def test_new_gap_outside_whitelist_is_reported_only() -> None:
    """段外新缺口被报出来，且对账器**不修**（删掉的那行不会自己回来）。"""
    tmp = _make_temp_dir()
    meta = None
    db_path = tmp / "sequence_meta.db"
    try:
        meta, store, db_path = _build_store(tmp)
        for _ in range(6):
            store.append(_event("m"))
        from db.meta_models import TerminalEvent

        with Session(meta.engine) as session:
            session.execute(delete(TerminalEvent).where(TerminalEvent.sequence == 3))
            session.commit()

        report = reconcile_sequences(store)
        assert report.rows == 5
        assert report.sequence_gaps == 1
        assert report.sequence_gap_ranges == [{"start": 3, "end": 3, "count": 1}]
        assert report.sequence_whitelisted_gaps == 0
        assert not report.ok  # 段外缺口 = 回归，退出码才会红
        assert store.count() == 5  # 只报告不修
    finally:
        _cleanup(meta, db_path)
        shutil.rmtree(tmp, ignore_errors=True)


def _seed_live_shaped_table(meta) -> None:
    """造一份与线上同形的最小表：1..6089 + 三段空缺 + 8 个存量重号。"""
    from db.meta_models import TerminalEvent

    rows = [
        {
            "event_id": f"seed-{sequence}",
            "occurred_at": _dt(),
            "sequence": sequence,
            "content": "",
        }
        for sequence in range(1, 6090)
    ]
    with Session(meta.engine) as session:
        session.execute(insert(TerminalEvent), rows)
        for start, end in SEQUENCE_GAP_WHITELIST:
            session.execute(
                delete(TerminalEvent).where(
                    TerminalEvent.sequence >= start, TerminalEvent.sequence <= end
                )
            )
        session.execute(
            insert(TerminalEvent),
            [
                {
                    "event_id": f"dup-{sequence}",
                    "occurred_at": _dt(),
                    "sequence": sequence,
                    "content": "",
                }
                for sequence in SEQUENCE_DUPLICATE_WHITELIST
            ],
        )
        session.commit()


def test_live_shaped_duplicates_are_known_but_new_ones_are_regressions() -> None:
    """线上同形：571 存量空缺 + 8 存量重号 → OK；再添一个新重号 → 回归。"""
    tmp = _make_temp_dir()
    meta = None
    db_path = tmp / "sequence_meta.db"
    try:
        meta, store, db_path = _build_store(tmp)
        _seed_live_shaped_table(meta)

        report = reconcile_sequences(store)
        assert report.sequence_gaps == 0, report.sequence_gap_ranges
        assert report.sequence_whitelisted_gaps == 571
        assert report.sequence_duplicates == 8
        assert report.sequence_new_duplicates == 0
        assert report.sequence_gaps == 0 and report.ok
        assert is_whitelisted_duplicate(2554) and is_whitelisted_duplicate(6089)
        assert find_duplicates([1, 1, 2]) == {1: 2}

        from db.meta_models import TerminalEvent

        with Session(meta.engine) as session:
            session.execute(
                insert(TerminalEvent),
                [
                    {
                        "event_id": "new-dup-1",
                        "occurred_at": _dt(),
                        "sequence": 4,
                        "content": "",
                    }
                ],
            )
            session.commit()
        report2 = reconcile_sequences(store)
        assert report2.sequence_duplicates == 9
        assert report2.sequence_new_duplicates == 1
        assert not report2.ok
        assert report2.duplicate_samples[0]["sequence"] == 4
        assert report2.duplicate_samples[0]["whitelisted"] is False
        assert store.count() == report2.rows  # 只报告不修
    finally:
        _cleanup(meta, db_path)
        shutil.rmtree(tmp, ignore_errors=True)


def test_work_item_event_types_are_registered_and_roundtrip() -> None:
    """B6 契约里的 5 个事件名必须注册，否则读回静默降级成 message。"""
    values = {item.value for item in EventType}
    for name in WORK_ITEM_EVENT_TYPES:
        assert name in values, name

    tmp = _make_temp_dir()
    meta = None
    db_path = tmp / "sequence_meta.db"
    try:
        meta, store, db_path = _build_store(tmp)
        for name in WORK_ITEM_EVENT_TYPES:
            store.append(
                Event.new(
                    event_type=EventType(name),
                    occurred_at=_dt(),
                    platform=W,
                    bot_account_id=ACCOUNT,
                    content=name,
                )
            )
        loaded = {event.event_type.value for event in store.recent(limit=20)}
        for name in WORK_ITEM_EVENT_TYPES:
            assert name in loaded, f"{name} 读回被降级成 {loaded}"
        # 显式传号仍按原值落库（不覆盖调用方给出的号）。
        explicit = store.append(_event("explicit", sequence=42))
        assert explicit.sequence == 42
        assert store.get(explicit.event_id).sequence == 42
    finally:
        _cleanup(meta, db_path)
        shutil.rmtree(tmp, ignore_errors=True)


def test_whitelist_is_resolved_at_runtime_and_overridable() -> None:
    """P2-3b：白名单在调用时解析（whitelist=None → 模块常量），patch 得动。"""
    from unittest.mock import patch

    import core.terminal.sequence_policy as policy

    sequences = (
        list(range(1, 129))
        + list(range(492, 717))
        + list(range(899, 1490))
        + list(range(1516, 1601))
    )
    gaps = find_gap_ranges(sequences)
    # 默认行为不变：三段存量空缺仍归「已知」
    assert count_numbers(split_gap_ranges(gaps)[0]) == 0

    with patch.object(policy, "SEQUENCE_GAP_WHITELIST", ()):
        new_gaps, known_gaps = split_gap_ranges(gaps)
        assert count_numbers(new_gaps) == 571 and known_gaps == []
    with patch.object(policy, "SEQUENCE_GAP_WHITELIST", ((129, 129),)):
        new_gaps, known_gaps = split_gap_ranges(gaps)
        assert count_numbers(known_gaps) == 1
        assert (new_gaps[0].start, new_gaps[0].end) == (130, 491)
    # patch 退出后回到常量本身；显式传参始终优先（配置覆盖走这条路）
    assert count_numbers(split_gap_ranges(gaps)[0]) == 0
    assert split_gap_ranges(gaps, ())[1] == []


def test_retention_tombstone_goes_through_store_allocation() -> None:
    """P1-2：墓碑事件必须走 store.append 的库内取号，不再是「第二个发号者」。"""
    from unittest.mock import patch

    from core.terminal.audit_export import RetentionPolicy, apply_retention
    from core.terminal.event_store_db import TerminalEventStore

    tmp = _make_temp_dir()
    meta = None
    db_path = tmp / "sequence_meta.db"
    try:
        meta, store, db_path = _build_store(tmp)
        for _ in range(3):
            store.append(_event("m"))

        calls: list[str] = []
        original = TerminalEventStore.append

        def _counting(self, event):
            calls.append(str(getattr(event.event_type, "value", event.event_type)))
            return original(self, event)

        with patch.object(TerminalEventStore, "append", _counting):
            result = apply_retention(
                meta.engine,
                # 全 0 = 不删任何行，只验墓碑这条路（与真实时钟无关）
                RetentionPolicy(
                    event_days=0,
                    projection_days=0,
                    command_days=0,
                    authorization_days=0,
                    query_log_days=0,
                    link_edge_days=0,
                    membership_days=0,
                ),
                dry_run=False,
            )
        assert result["dry_run"] is False
        assert calls == ["system"], "墓碑没走 store.append（又变回自己读 max 发号了）"
        tombstones = [
            event for event in store.recent(10)
            if event.event_type == EventType.SYSTEM
        ]
        assert len(tombstones) == 1
        tombstone = tombstones[0]
        assert tombstone.sequence == 4  # 库内原子取号：接在存量最大号之后
        assert tombstone.content.startswith("[retention]")
        assert tombstone.metadata.get("type") == "retention_purge"
        assert store.get(tombstone.event_id).sequence == 4
    finally:
        _cleanup(meta, db_path)
        shutil.rmtree(tmp, ignore_errors=True)


def test_retention_tombstone_failure_aborts_before_delete() -> None:
    """P1-2：墓碑写失败必须上抛并中止删除（旧实现的 ``except: pass`` 会删了没留痕）。"""
    from datetime import timedelta
    from unittest.mock import patch

    from core.terminal.audit_export import RetentionPolicy, apply_retention
    from core.terminal.event_store_db import TerminalEventStore

    tmp = _make_temp_dir()
    meta = None
    db_path = tmp / "sequence_meta.db"
    try:
        meta, store, db_path = _build_store(tmp)
        old = Event.new(
            event_type=EventType.MESSAGE,
            occurred_at=datetime.now() - timedelta(days=10),
            platform=W,
            bot_account_id=ACCOUNT,
            chat_id="friend_raw",
            chat_type="private",
            content="十天前的旧行",
        )
        store.append(old)
        assert store.count() == 1

        with patch.object(
            TerminalEventStore, "append", side_effect=RuntimeError("tombstone down")
        ):
            try:
                apply_retention(
                    meta.engine, RetentionPolicy(event_days=1), dry_run=False
                )
            except RuntimeError:
                pass
            else:
                raise AssertionError("墓碑失败必须上抛，不能静默继续删")
        assert store.count() == 1, "墓碑没落地就不许动删除"
        assert store.get(old.event_id) is not None
    finally:
        _cleanup(meta, db_path)
        shutil.rmtree(tmp, ignore_errors=True)


def test_integrity_error_without_stored_row_raises_not_fake_zero() -> None:
    """P3：唯一键冲突且回查也失败时，绝不把 sequence=0 的事件当成功返回。"""
    from unittest.mock import patch

    from sqlalchemy.exc import IntegrityError

    from core.terminal.event_store_db import TerminalEventStore

    tmp = _make_temp_dir()
    meta = None
    db_path = tmp / "sequence_meta.db"
    try:
        meta, store, db_path = _build_store(tmp)
        event = _event("conflict")
        conflict = IntegrityError(
            "INSERT INTO terminal_events", {}, Exception("UNIQUE constraint failed")
        )
        # 造出「INSERT 撞唯一键」但「回查不到已存行」的窄边
        with patch("core.terminal.event_store_db.insert", side_effect=conflict), patch.object(
            TerminalEventStore, "get", return_value=None
        ):
            try:
                returned = store.append(event)
            except RuntimeError as exc:
                assert "回查不到" in str(exc)
            else:
                raise AssertionError(
                    f"必须抛错，不许返回未落库事件（sequence={returned.sequence}）"
                )
        assert event.sequence == 0  # 手里这条确实没落库
        assert store.count() == 0
    finally:
        _cleanup(meta, db_path)
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> bool:
    tests = [fn for name, fn in globals().items() if name.startswith("test_")]
    failed = 0
    for fn in tests:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception:
            failed += 1
            print(f"FAIL {fn.__name__}")
            traceback.print_exc()
    print(f"{len(tests) - failed}/{len(tests)} passed")
    return failed == 0


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
