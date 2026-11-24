"""事件 outbox 测试（任务模型 §7.1 ②）。

覆盖：
- Migration 47：account 库建表 + 幂等；meta 库（无 messages）跳过；
- 消息写入与 outbox 行**同事务**登记（add / add_batch）；
- forwarder 幂等投递：pending → delivered，二跑不再投；
- 投递失败：退避重试（未到期不重试），修好后下一次投递成功；
- 达到止损线：标 failed + 错误留痕；
- 采集钩子 `_trace_terminal_shadow(repo=...)`：返回事件 id 且 outbox 销账；
- 周期 drain（REC-1）：同步没有新消息时也能销账；同步在跑时跳过；
  已销账的行不会被并发的重试写打回 pending；failed 行是终态，不会被 retry 复活；
- 周期 drain 告警（REC-2 修复）：failed / missing_message 都要告警、3600s 去重、
  picked=0 时不打 info。
- REC-8 claim/租约：夺取独占且 attempts 单调（含止损线不再夺）；两线程并发
  drain 只投一次、attempts==1；过期租约被回收（attempts==2）；租约过期瞬间的
  结清与易主后的迟到写；同一 key 只产生 1 条事件。
- P1-a/P2 回归：止损线上的过期 inflight 也能被回收并落 failed（不再永久 stale）；
  claim 取行是 id FIFO；同进程同线程重夺后旧 claim 的迟到写被 uuid nonce 挡下；
  未迁移到 51 的旧库走降级路径；delivered 结清同样清空 claim 字段。

Run directly:
    python tests/test_event_outbox.py
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

ROOT = Path(__file__).resolve().parent.parent
_TMP_ROOT = ROOT / "data" / "_event_outbox_test"

# 数据库隔离（照 tests/test_event_chain_links.py）：`_trace_terminal_shadow` 会经
# `core.ingestion.ingestors._trace_group_membership` → `core.bootstrap.get_meta_repo()`
# → `init_meta_db + run_migrations`，不隔离就把 pending 迁移落到**真实**
# `data/yinyue_meta.db` / 配置里的 account 库上（2026-09-29 事故）。
# 必须在 import core.* 之前设好：PathSettings 在 core.config.settings 导入时就取值。
_ISO_ROOT = (_TMP_ROOT / "_iso").resolve()
_ISO_ROOT.mkdir(parents=True, exist_ok=True)
os.environ["META_DB_PATH"] = str(_ISO_ROOT / "meta.db")
os.environ["PLATFORMS_DIR"] = str(_ISO_ROOT / "platforms")
os.environ["DATA_DIR"] = str(_ISO_ROOT / "data")
# 日志隔离也必须在 core.config.settings **首次**导入前设好：scheduler.main 导入时
# 会 init_logging(log_dir=config.paths.log_dir)，PathSettings 在导入那一刻就把
# LOG_DIR 固化了。此前这段放在 config 导入**之后**，于是 pytest / 直接跑都把
# bot_a 夹具日志写进了真实 logs/yinyue.log（ENG-12，实测 24 行/次）。
# 正统 runner 已经把 LOG_DIR 指到临时根，这里只在没设时补一个仓内测试目录。
_TEST_LOG_DIR = Path("data") / "_test_logs_event_outbox"
_TEST_LOG_DIR.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("LOG_DIR", str(_TEST_LOG_DIR))
# 守卫：env 只在 core.config.settings **首次**导入前设好才有效，要是别的模块先导入
# 就把真实路径固化进 config 了。这里硬失败，绝不静默把迁移落到生产库。
from core.config.settings import config as _config  # noqa: E402

_PROD_DATA_DIR = (ROOT / "data").resolve()
_PROD_META_DB = (_PROD_DATA_DIR / "yinyue_meta.db").resolve()
_PROD_PLATFORMS_DIR = (_PROD_DATA_DIR / "platforms").resolve()

# 三个路径都要挡：只断言 meta_db_path 的话，platforms_dir/data_dir 仍可能指回生产
# （AccountDBManager.register() → run_migrations 直接迁移账号库，Migration 51 的
# 4 个账号库就是这么来的）。
for _label, _path, _prod in (
    ("meta_db_path", _config.paths.meta_db_path, _PROD_META_DB),
    ("platforms_dir", _config.paths.platforms_dir, _PROD_PLATFORMS_DIR),
    ("data_dir", _config.paths.data_dir, _PROD_DATA_DIR),
):
    assert Path(_path).resolve() != _prod, (
        f"测试数据库隔离失效：{_label}={_path}"
    )

# 本文件的 env 生效时（通常情况），三个路径必须都落在本文件的隔离根下；
# 若 config 已被更早导入的测试文件隔离到别处（pytest 收集），上面的「不等于
# 生产路径」即已足够——不把顺序耦合写死进断言。
if Path(_config.paths.meta_db_path).resolve() == (_ISO_ROOT / "meta.db").resolve():
    for _label, _path in (
        ("platforms_dir", _config.paths.platforms_dir),
        ("data_dir", _config.paths.data_dir),
    ):
        assert _ISO_ROOT in Path(_path).resolve().parents, (
            f"测试数据库隔离失效：{_label}={_path} 不在 {_ISO_ROOT} 下"
        )

import importlib

# 坑：`import core.infra.logging as _logging` 绑到的是**标准库 logging** ——
# core/infra/__init__.py 的 `from core.infra.logging import *` 把子模块里的
# `logging` 名字盖到了包属性上（实测 _logging.__file__ 指向
# Python314/Lib/logging/__init__.py），于是 `_logging.init_logging = ...` 打在
# stdlib 上，拦不住 scheduler.main 的真 init_logging → 夹具日志漏进真实
# logs/yinyue.log（ENG-12 的另一半根因）。用 importlib 明确取子模块再打桩。
_logging = importlib.import_module("core.infra.logging")
_logging.init_logging = lambda *args, **kwargs: None

W = "wechat"
ACCOUNT = "bot_a"


def _make_temp_dir() -> Path:
    _TMP_ROOT.mkdir(parents=True, exist_ok=True)
    path = _TMP_ROOT / f"case_{uuid.uuid4().hex}"
    path.mkdir(parents=True, exist_ok=False)
    return path


def _dt(minute: int = 0) -> datetime:
    return datetime(2026, 9, 29, 10, minute, 0)


def _build_meta(tmp: Path):
    from db.meta_repo import MetaRepositoryManager
    from db.migrations import run_migrations

    db_path = tmp / "outbox_meta.db"
    meta = MetaRepositoryManager(str(db_path))
    run_migrations(str(db_path))
    return meta, db_path


def _build_account(tmp: Path):
    from db.account_repo import AccountRepositoryManager

    db_path = tmp / "account.db"
    repo = AccountRepositoryManager(str(db_path), W, ACCOUNT)
    return repo, db_path


def _add_message(repo, msg_id: int, *, chat: str = "friend_raw",
                 text: str = "在吗", created: datetime | None = None):
    from db.models import Message

    repo.messages.add(
        Message(
            msg_id=int(msg_id),
            chat_id=chat,
            chat_type="private" if not chat.endswith("@chatroom") else "group",
            sender_id=chat,
            sender_name="朋友",
            content=text,
            created_at=created or _dt(),
            is_self=0,
            source=W,
            account_id=ACCOUNT,
            msg_type_name="text",
        )
    )


def _cleanup(meta, tmp: Path) -> None:
    if meta is not None:
        try:
            meta.engine.dispose()
        except Exception:  # noqa: BLE001
            pass
    shutil.rmtree(tmp, ignore_errors=True)


class _FlakyStore:
    """包装真 store：对指定 key 的 append 失败 N 次；查询照常代理。"""

    def __init__(self, inner, fail_key: str, fail_times: int = 1) -> None:
        self.inner = inner
        self.fail_key = fail_key
        self.fail_times = int(fail_times)
        self.failures = 0
        self.appends = 0          # append 被调用次数（失败也算一次投递尝试）

    def append(self, event):
        self.appends += 1
        if event.external_msg_id == self.fail_key and self.failures < self.fail_times:
            self.failures += 1
            raise RuntimeError("simulated append failure")
        return self.inner.append(event)

    def get_by_external_msg_id(self, **kwargs):
        return self.inner.get_by_external_msg_id(**kwargs)

    def recorded_message_event_ids(self, **kwargs):
        return self.inner.recorded_message_event_ids(**kwargs)

    def recorded_message_keys(self, **kwargs):
        return self.inner.recorded_message_keys(**kwargs)

    def count(self) -> int:
        return self.inner.count()


def _outbox_rows(engine) -> list[dict]:
    from sqlalchemy import text

    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT * FROM event_outbox ORDER BY id")
        ).mappings().all()
    return [dict(row) for row in rows]


class _RecordingLog:
    """替身 logger：只记 (event, kwargs)，供断言日志事件用。"""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def _record(self, event: str, **kwargs) -> None:
        self.events.append((event, kwargs))

    info = debug = warning = error = _record

    def named(self, event: str) -> list[dict]:
        return [kwargs for name, kwargs in self.events if name == event]


def test_migration_47_creates_outbox_on_account_db() -> None:
    from sqlalchemy import text

    from db.migrations import _m047

    tmp = _make_temp_dir()
    meta = None
    try:
        meta, _ = _build_meta(tmp)
        repo, account_db = _build_account(tmp)

        # create_all 路径：account 库已带表
        with repo.engine.connect() as conn:
            assert conn.execute(text(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='event_outbox'"
            )).first() is not None

        # 模拟旧库：drop 后跑 _m047（幂等）
        with repo.engine.begin() as conn:
            conn.execute(text("DROP TABLE IF EXISTS event_outbox"))
            _m047(conn, None)
            _m047(conn, None)
            assert conn.execute(text(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='event_outbox'"
            )).first() is not None
            indexes = {r[1] for r in conn.execute(text("PRAGMA index_list(event_outbox)"))}
            assert "ux_event_outbox_msg" in indexes, indexes
            assert "ix_event_outbox_pending" in indexes, indexes

        # meta 库没有 messages 表：_m047 必须跳过
        with meta.engine.begin() as conn:
            _m047(conn, None)
            assert conn.execute(text(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='event_outbox'"
            )).first() is None
    finally:
        _cleanup(meta, tmp)


def test_migration_51_adds_claim_columns_and_is_idempotent() -> None:
    """迁移 51：给 v50 旧库补 claim/租约列 + 索引；跑两次结果一致。"""
    from sqlalchemy import text

    from db.migrations import _m051

    tmp = _make_temp_dir()
    meta = None
    try:
        meta, _ = _build_meta(tmp)
        repo, _ = _build_account(tmp)
        # 模拟 v50 旧库：把 create_all 带上的 claim 列删掉
        with repo.engine.begin() as conn:
            for col in ("claimed_at", "claimed_by", "lease_until"):
                conn.execute(text(f"ALTER TABLE event_outbox DROP COLUMN {col}"))
            _m051(conn, None)
            _m051(conn, None)          # 幂等：跑两次不报错、不重复加列
            cols = {
                row[1]
                for row in conn.execute(text("PRAGMA table_info(event_outbox)"))
            }
            assert {"claimed_at", "claimed_by", "lease_until"} <= cols, cols
            indexes = {
                row[1]
                for row in conn.execute(text("PRAGMA index_list(event_outbox)"))
            }
            assert "ix_event_outbox_lease" in indexes, indexes

        # meta 库没有 event_outbox：_m051 必须跳过（不建表）
        with meta.engine.begin() as conn:
            _m051(conn, None)
            assert conn.execute(text(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='event_outbox'"
            )).first() is None
    finally:
        _cleanup(meta, tmp)


def test_message_add_enqueues_outbox_in_same_transaction() -> None:
    tmp = _make_temp_dir()
    meta = None
    try:
        meta, _ = _build_meta(tmp)
        repo, _ = _build_account(tmp)
        _add_message(repo, 1001, text="同事务登记")
        rows = _outbox_rows(repo.engine)
        assert len(rows) == 1, rows
        row = rows[0]
        assert row["status"] == "pending" and row["attempts"] == 0
        assert row["platform"] == W and row["account_id"] == ACCOUNT
        assert row["chat_id"] == "friend_raw" and row["msg_id"] == "1001"

        # add_batch 同样登记
        from db.models import Message

        repo.messages.add_batch([
            Message(msg_id=1002, chat_id="friend_raw", chat_type="private",
                    sender_id="friend_raw", sender_name="朋友", content="批一",
                    created_at=_dt(1), is_self=0, source=W, account_id=ACCOUNT),
            Message(msg_id=1003, chat_id="friend_raw", chat_type="private",
                    sender_id="friend_raw", sender_name="朋友", content="批二",
                    created_at=_dt(2), is_self=0, source=W, account_id=ACCOUNT),
        ])
        assert len(_outbox_rows(repo.engine)) == 3
    finally:
        _cleanup(meta, tmp)


def test_drain_delivers_and_marks_delivered() -> None:
    from core.terminal.event_store_db import TerminalEventStore
    from core.terminal.outbox import drain_outbox

    tmp = _make_temp_dir()
    meta = None
    try:
        meta, _ = _build_meta(tmp)
        repo, _ = _build_account(tmp)
        store = TerminalEventStore(meta.engine)
        _add_message(repo, 2001, text="要投递")

        report = drain_outbox(
            repo=repo, platform=W, account_id=ACCOUNT, store=store, now=_dt(0)
        )
        assert report.picked == 1 and report.delivered == 1, report.as_dict()
        assert report.failed == 0
        assert store.count() == 1
        event = store.get_by_external_msg_id(
            platform=W, bot_account_id=ACCOUNT,
            chat_id="friend_raw", external_msg_id="2001",
        )
        assert event is not None and event.content == "要投递"

        row = _outbox_rows(repo.engine)[0]
        assert row["status"] == "delivered" and row["delivered_at"] is not None

        again = drain_outbox(
            repo=repo, platform=W, account_id=ACCOUNT, store=store, now=_dt(5)
        )
        assert again.picked == 0 and store.count() == 1
    finally:
        _cleanup(meta, tmp)


def test_failed_delivery_retries_with_backoff_then_succeeds() -> None:
    from core.terminal.event_store_db import TerminalEventStore
    from core.terminal.outbox import backoff_seconds, drain_outbox

    tmp = _make_temp_dir()
    meta = None
    try:
        meta, _ = _build_meta(tmp)
        repo, _ = _build_account(tmp)
        inner = TerminalEventStore(meta.engine)
        store = _FlakyStore(inner, "3001", fail_times=1)
        _add_message(repo, 3001, text="先失败后成功")

        now = _dt(0)
        first = drain_outbox(
            repo=repo, platform=W, account_id=ACCOUNT, store=store, now=now
        )
        assert first.delivered == 0 and first.retried == 1, first.as_dict()
        row = _outbox_rows(repo.engine)[0]
        assert row["status"] == "pending" and row["attempts"] == 1
        next_at = row["next_attempt_at"]

        # 未到期：不重试
        early = drain_outbox(
            repo=repo, platform=W, account_id=ACCOUNT, store=store, now=now
        )
        assert early.picked == 0

        # 到期后：投递成功
        due = now + timedelta(seconds=backoff_seconds(1) + 1)
        second = drain_outbox(
            repo=repo, platform=W, account_id=ACCOUNT, store=store, now=due
        )
        assert second.delivered == 1 and second.failed == 0, second.as_dict()
        assert inner.count() == 1
        row = _outbox_rows(repo.engine)[0]
        assert row["status"] == "delivered"
        assert str(next_at)  # 留痕（第一次确实写了 next_attempt_at）
    finally:
        _cleanup(meta, tmp)


def test_failed_delivery_stops_at_max_attempts() -> None:
    from core.terminal.event_store_db import TerminalEventStore
    from core.terminal.outbox import drain_outbox

    tmp = _make_temp_dir()
    meta = None
    try:
        meta, _ = _build_meta(tmp)
        repo, _ = _build_account(tmp)
        inner = TerminalEventStore(meta.engine)
        store = _FlakyStore(inner, "4001", fail_times=99)
        _add_message(repo, 4001, text="永远失败")

        report = drain_outbox(
            repo=repo, platform=W, account_id=ACCOUNT, store=store,
            now=_dt(0), max_attempts=1,
        )
        assert report.failed == 1 and report.delivered == 0, report.as_dict()
        row = _outbox_rows(repo.engine)[0]
        assert row["status"] == "failed" and row["attempts"] == 1
        assert "max_attempts" in str(row["last_error"])
        assert inner.count() == 0
    finally:
        _cleanup(meta, tmp)


def test_trace_shadow_settles_outbox_and_returns_ids() -> None:
    from core.terminal.command_runtime import (
        install_terminal_command_service,
        reset_terminal_command_service,
    )
    from core.terminal.command_service import CommandService
    from core.terminal.event_runtime import (
        install_terminal_event_store,
        reset_terminal_event_store,
    )
    from core.terminal.event_store_db import TerminalEventStore
    from db.repo_commands import CommandRepository

    from core.ingestion.ingestors import _trace_terminal_shadow

    tmp = _make_temp_dir()
    meta = None
    try:
        meta, _ = _build_meta(tmp)
        repo, _ = _build_account(tmp)
        store = TerminalEventStore(meta.engine)
        service = CommandService(None, repository=CommandRepository(meta.engine))
        install_terminal_event_store(store)
        install_terminal_command_service(service)
        try:
            _add_message(repo, 5001, text="钩子投递")
            row = repo.messages.find_by_platform_id(5001)
            assert row is not None
            with patch(
                "core.terminal.event_runtime.get_event_identity_service",
                return_value=None,
            ):
                ids = _trace_terminal_shadow(
                    [repo.messages.to_dict(row)],
                    platform=W,
                    account_id=ACCOUNT,
                    repo=repo,
                )
            assert len(ids) == 1, ids
            assert store.count() == 1
            outbox = _outbox_rows(repo.engine)
            assert outbox[0]["status"] == "delivered", outbox
        finally:
            reset_terminal_command_service()
            reset_terminal_event_store()
    finally:
        _cleanup(meta, tmp)


def _periodic_drain_patches(sm, repo, store, *, ctx=None):
    """周期 drain 的最小替身：一个账号 + 指定 store（避免真 bootstrap）。"""
    from contextlib import ExitStack
    from types import SimpleNamespace

    account_ctx = ctx or SimpleNamespace(
        platform=W, account_id=ACCOUNT, account_repo=repo
    )
    stack = ExitStack()
    stack.enter_context(patch.object(
        sm, "get_runtime",
        lambda: SimpleNamespace(all_contexts=lambda: [account_ctx]),
    ))
    stack.enter_context(patch(
        "core.terminal.event_runtime.get_terminal_event_store",
        lambda **kwargs: store,
    ))
    stack.enter_context(patch(
        "core.terminal.event_runtime.get_event_identity_service",
        lambda: None,
    ))
    return stack


def test_retry_does_not_resurrect_delivered_row() -> None:
    """REC-1/REC-8 并发保护：已销账的行不能被并发的 mark_outbox_retry 打回 pending。"""
    from db.outbox_store import mark_outbox_delivered, mark_outbox_retry

    tmp = _make_temp_dir()
    meta = None
    try:
        meta, _ = _build_meta(tmp)
        repo, _ = _build_account(tmp)
        _add_message(repo, 8001, text="并发销账")
        row_id = int(_outbox_rows(repo.engine)[0]["id"])

        # 同步路径先销账；周期 drain 后到的重试写不允许把它拉回 pending。
        assert mark_outbox_delivered(repo.engine, [row_id], _dt(1)) == 1
        assert mark_outbox_retry(
            repo.engine, row_id, next_attempt_at=_dt(2), error="delivery_failed"
        ) == 0

        row = _outbox_rows(repo.engine)[0]
        assert row["status"] == "delivered", row
        assert row["attempts"] == 0 and row["last_error"] == "", row
    finally:
        _cleanup(meta, tmp)


def test_retry_does_not_resurrect_failed_row() -> None:
    """failed 是终态：并发路径的旧快照 retry 不能把它打回 pending 再投一遍。"""
    from sqlalchemy import text

    from core.terminal.event_store_db import TerminalEventStore
    from core.terminal.outbox import drain_outbox
    from db.outbox_store import mark_outbox_retry

    tmp = _make_temp_dir()
    meta = None
    try:
        meta, _ = _build_meta(tmp)
        repo, _ = _build_account(tmp)
        inner = TerminalEventStore(meta.engine)
        store = _FlakyStore(inner, "8005", fail_times=99)
        _add_message(repo, 8005, text="已到止损线")
        row_id = int(_outbox_rows(repo.engine)[0]["id"])
        with repo.engine.begin() as conn:      # 这一行已经止损
            conn.execute(text(
                "UPDATE event_outbox SET status='failed', attempts=5, "
                "last_error='delivery_failed_max_attempts'"
            ))

        # 另一条路径拿着旧快照（attempts=3）后到：老守卫 != 'delivered' 会复活它。
        assert mark_outbox_retry(
            repo.engine, row_id, next_attempt_at=_dt(9), error="delivery_failed"
        ) == 0
        row = _outbox_rows(repo.engine)[0]
        assert row["status"] == "failed", row
        assert row["attempts"] == 5, row
        assert row["last_error"] == "delivery_failed_max_attempts", row

        # 周期 drain 也不该再把它捞起来重投。
        again = drain_outbox(
            repo=repo, platform=W, account_id=ACCOUNT, store=store,
            now=datetime.now() + timedelta(days=1),
        )
        assert again.picked == 0 and again.failed == 0, again.as_dict()
        assert inner.count() == 0
    finally:
        _cleanup(meta, tmp)


def test_periodic_drain_settles_without_new_messages() -> None:
    """REC-1：同步不再来新消息时，周期 drain 也能销账；attempts 每次尝试只 +1。"""
    from core.terminal.event_store_db import TerminalEventStore
    from core.terminal.outbox import drain_outbox

    tmp = _make_temp_dir()
    meta = None
    try:
        meta, _ = _build_meta(tmp)
        repo, _ = _build_account(tmp)
        inner = TerminalEventStore(meta.engine)
        store = _FlakyStore(inner, "8002", fail_times=1)
        _add_message(repo, 8002, text="周期销账")

        # 第一次投递失败（模拟同步那次 append 抛错），留下 pending。
        # now 取一小时前：退避窗口在真实时间轴上已经过去，周期 job 无需 sleep。
        past = datetime.now() - timedelta(hours=1)
        first = drain_outbox(
            repo=repo, platform=W, account_id=ACCOUNT, store=store, now=past
        )
        assert first.delivered == 0 and first.retried == 1, first.as_dict()
        row = _outbox_rows(repo.engine)[0]
        assert row["status"] == "pending" and row["attempts"] == 1, row

        import scheduler.main as sm

        # 此后不再写任何消息，只有周期 job 在跑。
        with _periodic_drain_patches(sm, repo, store):
            asyncio.run(sm.run_outbox_maintenance())
            row = _outbox_rows(repo.engine)[0]
            assert row["status"] == "delivered", row
            # REC-8：attempts 由 claim 原子 +1（本次投递尝试），不再被并发路径覆盖
            assert row["attempts"] == 2, row
            assert inner.count() == 1

            # 幂等：再跑一次不新增行、不重复投递
            asyncio.run(sm.run_outbox_maintenance())
            rows = _outbox_rows(repo.engine)
            assert len(rows) == 1, rows
            assert rows[0]["status"] == "delivered" and rows[0]["attempts"] == 2
            assert inner.count() == 1
    finally:
        _cleanup(meta, tmp)


def test_periodic_drain_skips_while_sync_running() -> None:
    """同步流水线在跑时周期 job 跳过（同一行 attempts 不能被两条路径双计）。"""
    from types import SimpleNamespace

    tmp = _make_temp_dir()
    meta = None
    try:
        meta, _ = _build_meta(tmp)
        repo, _ = _build_account(tmp)
        _add_message(repo, 8003, text="等同步")

        import scheduler.main as sm

        touched: list[int] = []

        def _contexts():
            touched.append(1)
            return []

        with patch.object(sm, "_sync_pipeline_running", True), patch.object(
            sm, "get_runtime",
            lambda: SimpleNamespace(all_contexts=_contexts),
        ):
            asyncio.run(sm.run_outbox_maintenance())

        assert touched == [], "同步在跑时不应该碰任何账号"
        row = _outbox_rows(repo.engine)[0]
        assert row["status"] == "pending" and row["attempts"] == 0, row
    finally:
        _cleanup(meta, tmp)


def test_periodic_drain_alerts_on_failed_rows() -> None:
    """有行到达止损线时，周期 job 走 _notify 告警（不只是写日志）。"""
    from sqlalchemy import text

    from core.terminal.event_store_db import TerminalEventStore

    tmp = _make_temp_dir()
    meta = None
    try:
        meta, _ = _build_meta(tmp)
        repo, _ = _build_account(tmp)
        inner = TerminalEventStore(meta.engine)
        store = _FlakyStore(inner, "8004", fail_times=99)
        _add_message(repo, 8004, text="永远失败")
        with repo.engine.begin() as conn:      # 已试 4 次，下一次到止损线
            conn.execute(text("UPDATE event_outbox SET attempts=4"))

        import scheduler.main as sm

        alerts: list[str] = []
        logger = _RecordingLog()
        with _periodic_drain_patches(sm, repo, store), patch.object(
            sm, "log", logger
        ), patch.object(
            sm, "_outbox_notified_at", 0.0
        ), patch.object(
            sm, "_notify", lambda title, message: alerts.append(message)
        ):
            asyncio.run(sm.run_outbox_maintenance())

        row = _outbox_rows(repo.engine)[0]
        assert row["status"] == "failed" and row["attempts"] == 5, row
        assert alerts and "outbox" in alerts[0], alerts
        # 文案要能指到账号与计数，否则收到告警也不知道去查谁
        assert f"{W}/{ACCOUNT}" in alerts[0], alerts
        assert "failed=1" in alerts[0], alerts
        assert logger.named("outbox_maintenance_done"), logger.events
        assert inner.count() == 0
    finally:
        _cleanup(meta, tmp)


def test_periodic_drain_alerts_on_missing_message_rows() -> None:
    """消息行丢了（outbox 有行、messages 无行）同样要告警，不能只留日志。"""
    from sqlalchemy import text

    from core.terminal.event_store_db import TerminalEventStore

    tmp = _make_temp_dir()
    meta = None
    try:
        meta, _ = _build_meta(tmp)
        repo, _ = _build_account(tmp)
        inner = TerminalEventStore(meta.engine)
        _add_message(repo, 8006, text="消息行随后被删")
        with repo.engine.begin() as conn:      # outbox 行还在，消息行没了
            conn.execute(text("DELETE FROM messages"))

        import scheduler.main as sm

        alerts: list[str] = []
        with _periodic_drain_patches(sm, repo, inner), patch.object(
            sm, "_outbox_notified_at", 0.0
        ), patch.object(
            sm, "_notify", lambda title, message: alerts.append(message)
        ):
            asyncio.run(sm.run_outbox_maintenance())

        row = _outbox_rows(repo.engine)[0]
        assert row["status"] == "failed", row
        assert "message_row_missing" in str(row["last_error"]), row
        assert alerts, "missing_message 也要告警（不只是 failed）"
        assert "missing_message=1" in alerts[0], alerts
        assert inner.count() == 0
    finally:
        _cleanup(meta, tmp)


def test_outbox_alert_is_deduplicated_within_the_hour() -> None:
    """缺口告警 3600s 去重：连续两跑只推一次；done 只在 picked>0 时打。"""
    import scheduler.main as sm

    alerts: list[str] = []
    logger = _RecordingLog()
    totals = {
        "accounts": 1, "picked": 1, "delivered": 0, "retried": 0,
        "failed": 1, "missing_message": 0, "inflight": 0, "stale": 0,
        "gaps": [{"platform": W, "account_id": ACCOUNT, "failed": 1,
                  "missing_message": 0, "inflight": 0, "stale": 0}],
    }
    with patch.object(sm, "_drain_outbox_all_accounts", lambda: dict(totals)), patch.object(
        sm, "_sync_pipeline_running", False
    ), patch.object(sm, "log", logger), patch.object(
        sm, "_outbox_notified_at", 0.0
    ), patch.object(
        sm, "_notify", lambda title, message: alerts.append(message)
    ):
        asyncio.run(sm.run_outbox_maintenance())
        asyncio.run(sm.run_outbox_maintenance())

    assert len(alerts) == 1, alerts
    assert "failed=1" in alerts[0] and f"{W}/{ACCOUNT}" in alerts[0], alerts
    # REC-3/REC-8：告警文案带上在投与租约过期（stale）计数
    assert "inflight=0" in alerts[0] and "stale=0" in alerts[0], alerts
    assert "在投 0 行" in alerts[0] and "租约过期 0 行" in alerts[0], alerts
    assert len(logger.named("outbox_maintenance_gaps")) == 2, logger.events
    assert len(logger.named("outbox_maintenance_done")) == 2, logger.events


def test_outbox_done_log_skipped_when_nothing_picked() -> None:
    """picked=0 不打 outbox_maintenance_done：1 分钟一跑，日志不该被它涨到 16MB。"""
    import scheduler.main as sm

    logger = _RecordingLog()
    totals = {"accounts": 1, "picked": 0, "delivered": 0, "retried": 0,
              "failed": 0, "missing_message": 0, "inflight": 0, "stale": 0,
              "gaps": []}
    with patch.object(sm, "_drain_outbox_all_accounts", lambda: dict(totals)), patch.object(
        sm, "_sync_pipeline_running", False
    ), patch.object(sm, "log", logger):
        asyncio.run(sm.run_outbox_maintenance())

    assert logger.named("outbox_maintenance_done") == [], logger.events


def test_claim_outbox_batch_is_exclusive_and_monotonic() -> None:
    """①REC-8 claim：同一行只有一个执行者能夺到；attempts 原子 +1（单调）。"""
    from sqlalchemy import text

    from db.outbox_store import claim_outbox_batch, outbox_claim_ready

    tmp = _make_temp_dir()
    meta = None
    try:
        meta, _ = _build_meta(tmp)
        repo, _ = _build_account(tmp)
        # 新库（create_all）就带 claim 列，不依赖迁移
        assert outbox_claim_ready(repo.engine) is True
        _add_message(repo, 6001, text="夺取一")
        _add_message(repo, 6002, text="夺取二")

        first = claim_outbox_batch(
            repo.engine, platform=W, account_id=ACCOUNT, now=_dt(0),
            claimed_by="a", lease_seconds=120,
        )
        assert [row["id"] for row in first] == sorted(row["id"] for row in first)
        assert [row["attempts"] for row in first] == [1, 1], first
        rows = _outbox_rows(repo.engine)
        assert all(row["status"] == "inflight" for row in rows), rows
        assert all(row["claimed_by"] == "a" for row in rows), rows
        assert all(
            row["claimed_at"] is not None and row["lease_until"] is not None
            for row in rows
        ), rows

        # 租约内别人抢不走（同一时刻重跑），attempts 不因「没抢到」而变化
        assert claim_outbox_batch(
            repo.engine, platform=W, account_id=ACCOUNT, now=_dt(0), claimed_by="b"
        ) == []
        assert [row["attempts"] for row in _outbox_rows(repo.engine)] == [1, 1]

        # 租约过期后可回收：attempts 继续单调 +1（不是拿旧快照写绝对值）
        third = claim_outbox_batch(
            repo.engine, platform=W, account_id=ACCOUNT,
            now=_dt(0) + timedelta(seconds=121), claimed_by="b",
        )
        assert [row["attempts"] for row in third] == [2, 2], third
        assert all(row["claimed_by"] == "b" for row in _outbox_rows(repo.engine))

        # 止损线：attempts 已到 max_attempts 的行不再被夺取（不复活）
        with repo.engine.begin() as conn:
            conn.execute(text(
                "UPDATE event_outbox SET status='pending', attempts=5, "
                "next_attempt_at=NULL, claimed_by='', lease_until=NULL"
            ))
        assert claim_outbox_batch(
            repo.engine, platform=W, account_id=ACCOUNT, now=_dt(9),
            claimed_by="c", max_attempts=5,
        ) == []
    finally:
        _cleanup(meta, tmp)


def test_concurrent_drains_claim_the_same_row_once() -> None:
    """②两路 drain 并发（Barrier）：attempts 只 +1、只投递一次、retried 合计 1。"""
    import threading

    from core.terminal.event_store_db import TerminalEventStore
    from core.terminal.outbox import drain_outbox

    tmp = _make_temp_dir()
    meta = None
    try:
        meta, _ = _build_meta(tmp)
        repo, _ = _build_account(tmp)
        inner = TerminalEventStore(meta.engine)
        store = _FlakyStore(inner, "7001", fail_times=1)
        _add_message(repo, 7001, text="并发夺取")

        # 两条路径用**同一个 now**（真实的并发窗口就在同一瞬间）：
        # 后到的执行者不该再夺到刚被结清、退避到 now+5s 的行。
        now = _dt(0)
        reports: list = []
        errors: list = []
        barrier = threading.Barrier(2)

        def _drain():
            try:
                barrier.wait(timeout=10)
                reports.append(drain_outbox(
                    repo=repo, platform=W, account_id=ACCOUNT, store=store, now=now,
                ))
            except Exception as exc:  # noqa: BLE001 - 收进 errors 供断言
                errors.append(exc)

        threads = [threading.Thread(target=_drain) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        assert errors == [], errors
        assert len(reports) == 2, reports
        # 未夺到的行不计入 picked（REC-8 接受的语义变更）
        assert sum(report.picked for report in reports) == 1, [
            report.as_dict() for report in reports
        ]
        assert sum(report.retried for report in reports) == 1, [
            report.as_dict() for report in reports
        ]
        assert store.appends == 1, store.appends       # 同一行只被投递一次
        row = _outbox_rows(repo.engine)[0]
        assert row["status"] == "pending" and row["attempts"] == 1, row
        assert row["claimed_by"] == "" and row["lease_until"] is None, row
        assert inner.count() == 0
    finally:
        _cleanup(meta, tmp)


def test_expired_lease_is_reclaimed_by_next_drain() -> None:
    """③租约过期可回收：picked==1、attempts==2（回收与夺取合计只 +1）。"""
    from sqlalchemy import text

    from core.terminal.event_store_db import TerminalEventStore
    from core.terminal.outbox import drain_outbox
    from db.outbox_store import outbox_status_counts

    tmp = _make_temp_dir()
    meta = None
    try:
        meta, _ = _build_meta(tmp)
        repo, _ = _build_account(tmp)
        inner = TerminalEventStore(meta.engine)
        store = _FlakyStore(inner, "7002", fail_times=99)
        _add_message(repo, 7002, text="过期租约")

        first = drain_outbox(
            repo=repo, platform=W, account_id=ACCOUNT, store=store, now=_dt(0)
        )
        assert first.retried == 1 and first.failed == 0, first.as_dict()

        # 模拟 owner 夺到行之后崩了（没结清）：inflight + 租约已过期
        with repo.engine.begin() as conn:
            conn.execute(text(
                "UPDATE event_outbox SET status='inflight', attempts=1, "
                "claimed_by='dead', claimed_at=:t, lease_until=:t, "
                "next_attempt_at=:t"
            ), {"t": _dt(1)})
        counts = outbox_status_counts(repo.engine, now=_dt(2))
        assert counts["inflight"] == 1 and counts["stale"] == 1, counts

        reclaimed = drain_outbox(
            repo=repo, platform=W, account_id=ACCOUNT, store=inner,
            now=_dt(0) + timedelta(seconds=200),
        )
        assert reclaimed.picked == 1 and reclaimed.delivered == 1, reclaimed.as_dict()
        row = _outbox_rows(repo.engine)[0]
        assert row["status"] == "delivered" and row["attempts"] == 2, row
        assert inner.count() == 1
    finally:
        _cleanup(meta, tmp)


def test_settle_after_lease_expiry_does_not_stick() -> None:
    """④租约过期瞬间：owner 自己的结清仍生效；易主后迟到写被挡，不会卡死。"""
    from core.terminal.event_store_db import TerminalEventStore
    from core.terminal.outbox import drain_outbox
    from db.outbox_store import claim_outbox_batch, mark_outbox_retry

    tmp = _make_temp_dir()
    meta = None
    try:
        meta, _ = _build_meta(tmp)
        repo, _ = _build_account(tmp)
        _add_message(repo, 7003, text="过期瞬间结清")

        claimed = claim_outbox_batch(
            repo.engine, platform=W, account_id=ACCOUNT, now=_dt(0),
            claimed_by="owner", lease_seconds=1,
        )
        assert len(claimed) == 1 and claimed[0]["attempts"] == 1, claimed
        row_id = int(claimed[0]["id"])

        # 租约已过期，但行还在 owner 名下：owner 自己的结清仍然有效
        assert mark_outbox_retry(
            repo.engine, row_id, next_attempt_at=_dt(1), error="late_settle",
            claimed_by="owner",
        ) == 1
        row = _outbox_rows(repo.engine)[0]
        assert row["status"] == "pending" and row["claimed_by"] == "", row

        # 别人夺走后，老 owner 的迟到结清被挡（返回 0），行不会被写坏
        thief = claim_outbox_batch(
            repo.engine, platform=W, account_id=ACCOUNT, now=_dt(1),
            claimed_by="thief", lease_seconds=120,
        )
        assert len(thief) == 1 and thief[0]["attempts"] == 2, thief
        assert mark_outbox_retry(
            repo.engine, row_id, next_attempt_at=None, error="owner_late",
            failed=True, claimed_by="owner",
        ) == 0
        row = _outbox_rows(repo.engine)[0]
        assert row["status"] == "inflight" and row["claimed_by"] == "thief", row

        # 新 owner 正常结清 → 下一轮照常推进（不永久卡在 inflight）
        assert mark_outbox_retry(
            repo.engine, row_id, next_attempt_at=_dt(2), error="delivery_failed",
            claimed_by="thief",
        ) == 1
        store = TerminalEventStore(meta.engine)
        again = drain_outbox(
            repo=repo, platform=W, account_id=ACCOUNT, store=store, now=_dt(2)
        )
        assert again.picked == 1 and again.delivered == 1, again.as_dict()
        row = _outbox_rows(repo.engine)[0]
        assert row["status"] == "delivered" and row["attempts"] == 3, row
    finally:
        _cleanup(meta, tmp)


def test_expired_claim_at_max_attempts_is_reclaimed_and_fails() -> None:
    """P1-a：attempts 到止损线 + 租约过期的 inflight 也必须能被回收、收尾成 failed。

    旧实现：``claim_outbox_batch`` 的 ``attempts < max_attempts`` 门把过期
    inflight 一起挡住，而 ``mark_outbox_retry`` 又要求 owner 在场 ⇒ 行永远卡在
    inflight，doctor 恒定 ``[XX] stale=1``，按口径②（stale 不触发通知）永久静默。
    """
    from sqlalchemy import text

    from core.terminal.event_store_db import TerminalEventStore
    from core.terminal.outbox import drain_outbox
    from db.outbox_store import outbox_status_counts

    tmp = _make_temp_dir()
    meta = None
    try:
        meta, _ = _build_meta(tmp)
        repo, _ = _build_account(tmp)
        inner = TerminalEventStore(meta.engine)
        store = _FlakyStore(inner, "7201", fail_times=99)
        _add_message(repo, 7201, text="止损线上的过期租约")
        with repo.engine.begin() as conn:      # owner 夺满 5 次后崩了，没结清
            conn.execute(text(
                "UPDATE event_outbox SET status='inflight', attempts=5, "
                "claimed_by='dead/pid1/thr1', claimed_at=:t, lease_until=:t, "
                "next_attempt_at=:t"
            ), {"t": _dt(1)})
        counts = outbox_status_counts(repo.engine, now=_dt(2))
        assert counts["inflight"] == 1 and counts["stale"] == 1, counts

        later = _dt(0) + timedelta(seconds=200)
        report = drain_outbox(
            repo=repo, platform=W, account_id=ACCOUNT, store=store,
            now=later, max_attempts=5,
        )
        assert report.picked == 1, report.as_dict()      # 旧实现恒为 0 → 永久 stale
        assert report.failed == 1 and report.delivered == 0, report.as_dict()
        row = _outbox_rows(repo.engine)[0]
        assert row["status"] == "failed", row            # 不再退回 pending
        assert row["attempts"] == 6, row                 # claim 的原子 +1
        assert "max_attempts" in str(row["last_error"]), row
        counts = outbox_status_counts(repo.engine, now=later)
        assert counts["stale"] == 0 and counts["failed"] == 1, counts
        assert inner.count() == 0
    finally:
        _cleanup(meta, tmp)


def test_claim_order_is_id_fifo_not_attempts() -> None:
    """P2：夺取顺序回到 id FIFO——重试行不能被新行挤住（旧实现按 attempts 排序）。"""
    from sqlalchemy import text

    from db.outbox_store import claim_outbox_batch

    tmp = _make_temp_dir()
    meta = None
    try:
        meta, _ = _build_meta(tmp)
        repo, _ = _build_account(tmp)
        _add_message(repo, 9301, text="早到但重试过")
        _add_message(repo, 9302, text="新行 attempts=0")
        with repo.engine.begin() as conn:
            conn.execute(text(
                "UPDATE event_outbox SET attempts=3, next_attempt_at=NULL "
                "WHERE msg_id='9301'"
            ))

        claimed = claim_outbox_batch(
            repo.engine, platform=W, account_id=ACCOUNT, now=_dt(0),
            claimed_by="fifo", limit=1,
        )
        assert [row["msg_id"] for row in claimed] == ["9301"], claimed
    finally:
        _cleanup(meta, tmp)


def test_same_thread_reclaim_invalidates_previous_claim() -> None:
    """P2：同一进程同线程重夺后，旧 claim 的迟到写必须被 uuid nonce 挡下。

    旧 ``claimed_by`` 是 ``platform/account/pid/thr``：同一线程重夺后新旧 owner
    串完全相同，老 owner 的迟到结清仍能命中守卫，把新 owner 的行写坏。
    """
    import re

    from core.terminal.event_store_db import TerminalEventStore
    from core.terminal.outbox import drain_outbox
    from db.outbox_store import mark_outbox_retry

    tmp = _make_temp_dir()
    meta = None
    try:
        meta, _ = _build_meta(tmp)
        repo, _ = _build_account(tmp)
        store = TerminalEventStore(meta.engine)
        _add_message(repo, 7202, text="同线程重夺")

        now = _dt(0)
        claims: list[str] = []
        # 投递函数崩掉 = owner 夺到行之后卡死（行留在 inflight，真实的泄漏形态）
        with patch(
            "core.terminal.shadow.record_message_events",
            side_effect=RuntimeError("owner crashed mid-delivery"),
        ):
            for when in (now, now + timedelta(seconds=121)):
                try:
                    drain_outbox(
                        repo=repo, platform=W, account_id=ACCOUNT,
                        store=store, now=when,
                    )
                except RuntimeError:
                    pass
                else:
                    raise AssertionError("替身没让 drain 崩，用例前提不成立")
                claims.append(str(_outbox_rows(repo.engine)[0]["claimed_by"]))

        first_claim, current_claim = claims
        assert re.search(r"/claim[0-9a-f]{8}$", first_claim), first_claim
        assert current_claim != first_claim, claims        # uuid nonce 换新
        row = _outbox_rows(repo.engine)[0]
        assert row["status"] == "inflight" and row["attempts"] == 2, row

        # 老 owner 的迟到写：不能把新 owner 的行改掉
        assert mark_outbox_retry(
            repo.engine, int(row["id"]), next_attempt_at=None,
            error="stale_owner_late", failed=True, claimed_by=first_claim,
        ) == 0
        row = _outbox_rows(repo.engine)[0]
        assert row["status"] == "inflight" and row["claimed_by"] == current_claim, row
    finally:
        _cleanup(meta, tmp)


def test_legacy_library_without_claim_columns_drains() -> None:
    """P2：未迁移到 51 的旧库（无 claim 列）走降级路径：读 pending + 绝对值结清，不抛错。"""
    from sqlalchemy import text

    from core.terminal.event_store_db import TerminalEventStore
    from core.terminal.outbox import drain_outbox
    from db.outbox_store import outbox_claim_ready

    tmp = _make_temp_dir()
    meta = None
    try:
        meta, _ = _build_meta(tmp)
        repo, _ = _build_account(tmp)
        with repo.engine.begin() as conn:      # 模拟 v50 旧库：claim 列还没补
            for col in ("claimed_at", "claimed_by", "lease_until"):
                conn.execute(text(f"ALTER TABLE event_outbox DROP COLUMN {col}"))
        assert outbox_claim_ready(repo.engine) is False

        _add_message(repo, 9101, text="旧库降级")
        inner = TerminalEventStore(meta.engine)
        store = _FlakyStore(inner, "9101", fail_times=1)
        logger = _RecordingLog()
        with patch("core.terminal.outbox.log", logger):
            first = drain_outbox(
                repo=repo, platform=W, account_id=ACCOUNT, store=store, now=_dt(0)
            )
        assert first.picked == 1 and first.retried == 1, first.as_dict()
        assert logger.named("outbox_claim_unavailable"), logger.events
        row = _outbox_rows(repo.engine)[0]
        assert row["status"] == "pending" and row["attempts"] == 1, row  # 绝对值 +1

        # 到期后投递成功：legacy 结清 + delivered 销账都不能碰不存在的 claim 列
        second = drain_outbox(
            repo=repo, platform=W, account_id=ACCOUNT, store=inner,
            now=_dt(0) + timedelta(seconds=6),
        )
        assert second.delivered == 1, second.as_dict()
        assert inner.count() == 1
        assert _outbox_rows(repo.engine)[0]["status"] == "delivered"
    finally:
        _cleanup(meta, tmp)


def test_delivered_settlement_clears_claim_fields() -> None:
    """P2：delivered 也是结清——claim 字段和 mark_outbox_retry 一样要清空。"""
    from db.outbox_store import (
        claim_outbox_batch,
        mark_outbox_delivered,
        mark_outbox_delivered_keys,
    )

    tmp = _make_temp_dir()
    meta = None
    try:
        meta, _ = _build_meta(tmp)
        repo, _ = _build_account(tmp)
        _add_message(repo, 9201, text="按 id 销账")
        _add_message(repo, 9202, text="按 key 销账")
        claimed = claim_outbox_batch(
            repo.engine, platform=W, account_id=ACCOUNT, now=_dt(0),
            claimed_by="worker/1",
        )
        assert len(claimed) == 2, claimed
        rows = {row["msg_id"]: row for row in _outbox_rows(repo.engine)}
        assert all(row["claimed_by"] == "worker/1" for row in rows.values()), rows

        assert mark_outbox_delivered(
            repo.engine, [int(rows["9201"]["id"])], _dt(1)
        ) == 1
        assert mark_outbox_delivered_keys(
            repo.engine, platform=W, account_id=ACCOUNT,
            keys=[("friend_raw", "9202")], when=_dt(1),
        ) == 1

        for row in _outbox_rows(repo.engine):
            assert row["status"] == "delivered", row
            assert row["claimed_at"] is None, row
            assert row["claimed_by"] == "" and row["lease_until"] is None, row
    finally:
        _cleanup(meta, tmp)


def test_duplicate_delivery_keeps_one_event() -> None:
    """⑤同一 (chat, msg) 只产生 1 条事件：重投由四元组幂等 + 唯一索引兜底。"""
    from sqlalchemy import text

    from core.terminal.event_store_db import TerminalEventStore
    from core.terminal.outbox import drain_outbox
    from core.terminal.shadow import record_message_events

    tmp = _make_temp_dir()
    meta = None
    try:
        meta, _ = _build_meta(tmp)
        repo, _ = _build_account(tmp)
        store = TerminalEventStore(meta.engine)
        _add_message(repo, 7004, text="只发一次")

        first = drain_outbox(
            repo=repo, platform=W, account_id=ACCOUNT, store=store, now=_dt(0)
        )
        assert first.delivered == 1 and store.count() == 1, first.as_dict()

        # 模拟「销账丢了」：同一行被打回 pending，再投一遍
        with repo.engine.begin() as conn:
            conn.execute(text(
                "UPDATE event_outbox SET status='pending', delivered_at=NULL, "
                "claimed_by='', lease_until=NULL, next_attempt_at=NULL"
            ))
        again = drain_outbox(
            repo=repo, platform=W, account_id=ACCOUNT, store=store, now=_dt(1)
        )
        assert again.delivered == 1, again.as_dict()   # 复核命中即销账，不重写事件
        assert store.count() == 1

        # 直写路径再来一次：唯一索引兜底，事件数不变
        row = repo.messages.find_by_platform_id(7004)
        record_message_events(
            [repo.messages.to_dict(row)], platform=W, account_id=ACCOUNT,
            identity_service=None, store=store, return_events=False,
        )
        assert store.count() == 1
    finally:
        _cleanup(meta, tmp)


class _FakeEngine:
    """append 重连用例的 engine 替身：只记 dispose 次数。"""

    def __init__(self) -> None:
        self.disposed = 0

    def dispose(self) -> None:
        self.disposed += 1


class _FakeSessionCtx:
    """`with Session(engine) as session` 的最小替身。"""

    def __init__(self, engine) -> None:
        self.engine = engine

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> bool:
        return False

    def commit(self) -> None:
        pass


def _cantopen() -> Exception:
    import sqlite3

    from sqlalchemy.exc import OperationalError

    return OperationalError(
        "INSERT INTO terminal_events (...) VALUES (...)",
        {},
        sqlite3.OperationalError("unable to open database file"),
    )


def test_store_disposes_pool_and_retries_once_on_cantopen() -> None:
    """2026-10-07 事故回归：坏连接被池反复取用 → 换池 + 新连接重试一次。"""
    import core.terminal.event_store_db as esd
    from core.terminal.event_store_db import TerminalEventStore

    engine = _FakeEngine()
    store = TerminalEventStore(engine)
    recorder = _RecordingLog()
    attempts: list[int] = []

    def fake_append(session, event):
        attempts.append(1)
        if len(attempts) == 1:
            raise _cantopen()
        return event

    saved = (esd.Session, esd.append_in_session, esd.log)
    try:
        esd.Session = _FakeSessionCtx
        esd.append_in_session = fake_append
        esd.log = recorder
        event = object()
        assert store.append(event) is event
    finally:
        esd.Session, esd.append_in_session, esd.log = saved

    assert len(attempts) == 2, attempts          # 失败一次 + 新连接重试一次
    assert engine.disposed == 1                  # 坏连接池被丢弃
    assert recorder.named("terminal_event_store_reconnect")


def test_store_cantopen_still_failing_raises_after_backoff_budget() -> None:
    """新连接仍 CANTOPEN 时不得无限重试，也不能吞成成功。

    预算跟 _CANTOPEN_MAX_RETRIES 走（当前 3 次退避重试 = 最多 4 次尝试，每轮都换池）。
    """
    import core.terminal.event_store_db as esd
    from core.terminal.event_store_db import TerminalEventStore
    from sqlalchemy.exc import OperationalError

    engine = _FakeEngine()
    store = TerminalEventStore(engine)
    attempts: list[int] = []

    def always_cantopen(session, event):
        attempts.append(1)
        raise _cantopen()

    saved = (esd.Session, esd.append_in_session, esd.log)
    try:
        esd.Session = _FakeSessionCtx
        esd.append_in_session = always_cantopen
        esd.log = _RecordingLog()
        try:
            store.append(object())
        except OperationalError:
            pass
        else:
            raise AssertionError("重试后仍 CANTOPEN 必须抛出，不能吞成成功")
    finally:
        esd.Session, esd.append_in_session, esd.log = saved

    assert len(attempts) == esd._CANTOPEN_MAX_RETRIES + 1, attempts      # 失败 1 次 + 退避重试 3 次
    assert engine.disposed == esd._CANTOPEN_MAX_RETRIES + 1            # 每轮都 dispose() 换池


def test_store_does_not_dispose_on_ordinary_lock_error() -> None:
    """database is locked 是忙等语义，交给 busy_timeout，不该换池。"""
    import sqlite3

    import core.terminal.event_store_db as esd
    from core.terminal.event_store_db import TerminalEventStore
    from sqlalchemy.exc import OperationalError

    engine = _FakeEngine()
    store = TerminalEventStore(engine)
    recorder = _RecordingLog()
    attempts: list[int] = []

    def locked(session, event):
        attempts.append(1)
        raise OperationalError(
            "SELECT 1", {}, sqlite3.OperationalError("database is locked")
        )

    saved = (esd.Session, esd.append_in_session, esd.log)
    try:
        esd.Session = _FakeSessionCtx
        esd.append_in_session = locked
        esd.log = recorder
        try:
            store.append(object())
        except OperationalError:
            pass
        else:
            raise AssertionError("database is locked 应当原样抛出")
    finally:
        esd.Session, esd.append_in_session, esd.log = saved

    assert len(attempts) == 1, attempts
    assert engine.disposed == 0
    assert not recorder.named("terminal_event_store_reconnect")


def main() -> int:
    tests = [
        test_migration_47_creates_outbox_on_account_db,
        test_migration_51_adds_claim_columns_and_is_idempotent,
        test_message_add_enqueues_outbox_in_same_transaction,
        test_drain_delivers_and_marks_delivered,
        test_failed_delivery_retries_with_backoff_then_succeeds,
        test_failed_delivery_stops_at_max_attempts,
        test_trace_shadow_settles_outbox_and_returns_ids,
        test_retry_does_not_resurrect_delivered_row,
        test_retry_does_not_resurrect_failed_row,
        test_periodic_drain_settles_without_new_messages,
        test_periodic_drain_skips_while_sync_running,
        test_periodic_drain_alerts_on_failed_rows,
        test_periodic_drain_alerts_on_missing_message_rows,
        test_outbox_alert_is_deduplicated_within_the_hour,
        test_outbox_done_log_skipped_when_nothing_picked,
        test_claim_outbox_batch_is_exclusive_and_monotonic,
        test_concurrent_drains_claim_the_same_row_once,
        test_expired_lease_is_reclaimed_by_next_drain,
        test_settle_after_lease_expiry_does_not_stick,
        test_expired_claim_at_max_attempts_is_reclaimed_and_fails,
        test_claim_order_is_id_fifo_not_attempts,
        test_same_thread_reclaim_invalidates_previous_claim,
        test_legacy_library_without_claim_columns_drains,
        test_delivered_settlement_clears_claim_fields,
        test_duplicate_delivery_keeps_one_event,
        test_store_disposes_pool_and_retries_once_on_cantopen,
        test_store_cantopen_still_failing_raises_after_backoff_budget,
        test_store_does_not_dispose_on_ordinary_lock_error,
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
