"""调度周期 job 测试（REC-1 / REC-2：outbox drain 与对账器接进调度）。

覆盖：
- 两个 job 按 flag 注册，默认间隔 1 / 30 分钟，且能用 env 覆盖；
- flag 置 0 时不注册，但既有 job 照旧（不是整个 schedule_jobs 被跳过）；
- 运行期把 flag 置 0：已注册的 job 立刻不干活，且只在状态切换时记一行
  ``job_disabled_by_flag``（免重启承诺）；
- apply 由 ``TERMINAL_RECONCILE_APPLY`` 决定；窗口固定 ``now - 24h``、
  ``max_messages=2000``、``page_size=500``；打满上限要 warning（不静默截断）；
- 窗口外不写：60 天前的洞既不被扫描也不被补写（只有近 24h 的行在范围内）；
- ``_notify`` 不许静默吞异常，推送失败不消费 3600s 去重窗口。

Run directly:
    python tests/test_scheduler_jobs.py
"""

from __future__ import annotations

import asyncio
import inspect
import os
import shutil
import sys
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Iterator
from unittest.mock import patch

_TEST_LOG_DIR = str(Path("data") / "_test_logs_scheduler_jobs")
Path(_TEST_LOG_DIR).mkdir(parents=True, exist_ok=True)
os.environ["LOG_DIR"] = _TEST_LOG_DIR

# 数据库隔离（照 tests/test_event_outbox.py）：本文件 import scheduler.main（连带
# core.config.settings 固化路径），任何一次 init_singletons / run_migrations 都会落到
# **真实** meta/account 库（2026-09-29 事故：一次测试把 Migration 50 落到生产库）。
# 必须在 import core.* 之前设好。
_ISO_ROOT = (Path("data") / "_scheduler_jobs_test" / "_iso").resolve()
_ISO_ROOT.mkdir(parents=True, exist_ok=True)
# 目录必须先建好：PathSettings 由 DATA_DIR/META_DB_PATH 拼出库路径，父目录不存在时
# SQLite 只会报 "unable to open database file"（2026-10-17 定位：_iso/data 缺失）。
for _sub in ("data", "platforms"):
    (_ISO_ROOT / _sub).mkdir(parents=True, exist_ok=True)
os.environ["META_DB_PATH"] = str(_ISO_ROOT / "meta.db")
os.environ["PLATFORMS_DIR"] = str(_ISO_ROOT / "platforms")
os.environ["DATA_DIR"] = str(_ISO_ROOT / "data")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _tempdirs  # noqa: E402,F401  钉住 TEMP/TMP：SQLite GetTempPath 不可写时会报 SQLITE_CANTOPEN

import core.infra.logging as _logging

_logging.init_logging = lambda *args, **kwargs: None

# 守卫：env 只在 core.config.settings **首次**导入前设好才有效。这里硬失败，
# 绝不静默把迁移落到生产库。
from core.config.settings import config as _config  # noqa: E402

assert Path(_config.paths.meta_db_path).resolve() != (
    Path(__file__).resolve().parent.parent / "data" / "yinyue_meta.db"
).resolve(), f"测试数据库隔离失效：meta_db_path={_config.paths.meta_db_path}"

# scheduler.main 在下面导入；它导入时就调 init_logging，故必须先打桩。
import scheduler.main as sm
from core import features  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
_TMP_ROOT = ROOT / "data" / "_scheduler_jobs_test"
W = "wechat"
ACCOUNT = "bot_a"


@contextmanager
def _env(values: dict[str, str]) -> Iterator[None]:
    """临时改 env；出块还原（flag 每次读 env，所以这样就能开关）。"""
    saved = {key: os.environ.get(key) for key in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for key, old in saved.items():
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old


class _Do:
    def __init__(self, jobs, unit, interval):
        self.jobs = jobs
        self.unit = unit
        self.interval = interval

    def do(self, fn, *args, **kwargs):
        # 立即调用一次：注册进来的都是 lambda: _run_async(...)，替身返回协程名。
        self.jobs.append((self.unit, self.interval, fn()))
        return fn


class _Day:
    def __init__(self, jobs):
        self.jobs = jobs

    def at(self, when):
        return _Do(self.jobs, "day", None)


class _Every:
    def __init__(self, jobs, interval):
        self.jobs = jobs
        self.interval = interval

    @property
    def minutes(self):
        return _Do(self.jobs, "minutes", self.interval)

    @property
    def hours(self):
        return _Do(self.jobs, "hours", self.interval)

    @property
    def seconds(self):
        return _Do(self.jobs, "seconds", self.interval)

    @property
    def day(self):
        return _Day(self.jobs)


class _RecordingSchedule:
    """替身 schedule：记录 (unit, interval, 回调返回值)。"""

    def __init__(self) -> None:
        self.jobs: list[tuple[str, int | None, object]] = []

    def every(self, interval: int | None = None) -> _Every:
        return _Every(self.jobs, interval)


def _record_run_async(coro, **kwargs):  # _run_async 现在带 name/timeout 关键字
    """替身 _run_async：不真的跑，只记协程名（关掉避免 never-awaited）。"""
    name = getattr(coro, "__qualname__", type(coro).__name__)
    try:
        coro.close()
    except Exception:  # noqa: BLE001
        pass
    return name


@contextmanager
def _scheduled_jobs() -> Iterator[list[tuple[str, int | None, object]]]:
    """跑一次 schedule_jobs()，返回它注册了哪些 job（不碰真 schedule/loop）。"""
    recorder = _RecordingSchedule()
    with patch.object(sm, "schedule", recorder), patch.object(
        sm, "_run_async", _record_run_async
    ), patch.object(sm, "log_background_metrics", lambda: None), patch.object(
        sm, "_notify", lambda *args, **kwargs: None
    ):
        sm.schedule_jobs()
        yield recorder.jobs


def _job_names(jobs) -> list[str]:
    return [str(name) for _, _, name in jobs]


def _make_temp_dir() -> Path:
    _TMP_ROOT.mkdir(parents=True, exist_ok=True)
    path = _TMP_ROOT / f"case_{uuid.uuid4().hex}"
    path.mkdir(parents=True, exist_ok=False)
    return path


def _build_meta(tmp: Path):
    from db.meta_repo import MetaRepositoryManager
    from db.migrations import run_migrations

    db_path = tmp / "scheduler_jobs_meta.db"
    meta = MetaRepositoryManager(str(db_path))
    run_migrations(str(db_path))
    return meta, db_path


def _build_account(tmp: Path):
    from db.account_repo import AccountRepositoryManager

    db_path = tmp / "account.db"
    return AccountRepositoryManager(str(db_path), W, ACCOUNT), db_path


def _add_message(repo, msg_id: int, *, created: datetime, text: str = "在吗") -> None:
    from db.models import Message

    repo.messages.add(
        Message(
            msg_id=int(msg_id),
            chat_id="friend_raw",
            chat_type="private",
            sender_id="friend_raw",
            sender_name="朋友",
            content=text,
            created_at=created,
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


def _fake_report(**overrides):
    values = dict(
        platform=W, account_id=ACCOUNT, scanned=0, matched=0, missing=0,
        inserted=0, failed=0, skipped_no_msg_id=0, applied=False,
        by_chat={}, samples=[],
    )
    values.update(overrides)
    return SimpleNamespace(**values)


class _RecordingLog:
    """替身 logger：只记 (event, kwargs)，供断言日志事件用。"""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def _record(self, event: str, **kwargs) -> None:
        self.events.append((event, kwargs))

    info = debug = warning = error = _record

    def named(self, event: str) -> list[dict]:
        return [kwargs for name, kwargs in self.events if name == event]


def _reconcile_patches(repo, store, *, alerts=None, spy=None):
    """对账 job 的替身：一个账号 + 指定 store + 可选 reconcile_account 替身。"""
    from contextlib import ExitStack

    ctx = SimpleNamespace(platform=W, account_id=ACCOUNT, account_repo=repo)
    stack = ExitStack()
    stack.enter_context(patch.object(
        sm, "get_runtime", lambda: SimpleNamespace(all_contexts=lambda: [ctx]),
    ))
    stack.enter_context(patch(
        "core.terminal.event_runtime.get_terminal_event_store",
        lambda **kwargs: store,
    ))
    stack.enter_context(patch(
        "core.terminal.event_runtime.get_event_identity_service",
        lambda: None,
    ))
    stack.enter_context(patch.object(
        sm, "_notify",
        lambda title, message: (alerts if alerts is not None else []).append(message),
    ))
    if spy is not None:
        stack.enter_context(patch(
            "core.terminal.reconciler.reconcile_account", spy,
        ))
    return stack


def test_jobs_registered_with_expected_intervals() -> None:
    """默认 1 分钟 drain + 30 分钟对账；间隔可用 env 覆盖。"""
    with _env({
        "YINYUE_FEATURE_OUTBOX_MAINTENANCE": "1",
        "YINYUE_FEATURE_TERMINAL_RECONCILE": "1",
    }):
        with _scheduled_jobs() as jobs:
            pass
        assert ("minutes", 1, "run_outbox_maintenance") in jobs, jobs
        assert ("minutes", 30, "run_terminal_reconcile") in jobs, jobs
        # Step 12：意图 tick 每分钟注册；flag 关由协程体自己立即返回。
        # 必须排在扫描器之前：shadow 先审计，扫描器随后投递并收口镜像意图。
        assert ("minutes", 1, "run_intention_tick") in jobs, jobs
        names = _job_names(jobs)
        assert names.index("run_intention_tick") < names.index("run_reminder_delivery"), names

        with _env({"YINYUE_OUTBOX_DRAIN_INTERVAL_MINUTES": "7",
                   "YINYUE_TERMINAL_RECONCILE_INTERVAL_MINUTES": "11"}):
            with _scheduled_jobs() as overridden:
                pass
        assert ("minutes", 7, "run_outbox_maintenance") in overridden, overridden
        assert ("minutes", 11, "run_terminal_reconcile") in overridden, overridden


def test_jobs_on_by_default_is_one_source_of_truth() -> None:
    """默认开这一条事实只有一处（_JOBS_ON_BY_DEFAULT）；行为仍是"默认注册"。

    静态面：两个 job 的默认值都取自同一个集合，不许再各写一个 True 字面量。
    行为面：完全不设 env 时两个 job 照旧注册（默认值保持 True）。
    """
    source = inspect.getsource(sm.schedule_jobs)
    for flag in ("OUTBOX_MAINTENANCE", "TERMINAL_RECONCILE"):
        assert f"features.{flag}, True)" not in source, (
            f"{flag} 的默认值又硬编码成 True 了：默认值只许来自 _JOBS_ON_BY_DEFAULT"
        )
    assert "_JOBS_ON_BY_DEFAULT" in inspect.getsource(sm._job_disabled_by_flag), (
        "运行期默认值也必须来自同一来源"
    )
    for flag in (features.OUTBOX_MAINTENANCE, features.TERMINAL_RECONCILE):
        assert flag in sm._JOBS_ON_BY_DEFAULT, flag

    env = {f"YINYUE_FEATURE_{name.upper()}": "" for name in sm._JOBS_ON_BY_DEFAULT}
    with _env(env):
        os.environ.pop(f"YINYUE_FEATURE_{features.OUTBOX_MAINTENANCE.upper()}", None)
        os.environ.pop(f"YINYUE_FEATURE_{features.TERMINAL_RECONCILE.upper()}", None)
        with _scheduled_jobs() as jobs:
            pass
        names = _job_names(jobs)
    assert "run_outbox_maintenance" in names, names
    assert "run_terminal_reconcile" in names, names

def test_flags_off_register_nothing() -> None:
    """两个 flag 都置 0：新 job 不注册，既有 job 不受影响。"""
    with _env({
        "YINYUE_FEATURE_OUTBOX_MAINTENANCE": "0",
        "YINYUE_FEATURE_TERMINAL_RECONCILE": "0",
    }):
        with _scheduled_jobs() as jobs:
            pass

    names = _job_names(jobs)
    assert "run_outbox_maintenance" not in names, names
    assert "run_terminal_reconcile" not in names, names
    assert "run_reminder_delivery" in names, names      # 既有 1 分钟 job 照旧
    assert "run_memory_projection" in names, names


def test_apply_flag_drives_reconcile_and_window_is_24h() -> None:
    """apply 由 flag 决定；窗口是 now-24h、max_messages=2000、page_size=500。"""
    calls: list[dict] = []

    def _spy(**kwargs):
        calls.append(kwargs)
        return _fake_report(missing=2, scanned=3)

    with _reconcile_patches(object(), object(), spy=_spy):
        with _env({"YINYUE_FEATURE_TERMINAL_RECONCILE_APPLY": "0"}):
            asyncio.run(sm.run_terminal_reconcile())
        assert calls[-1]["apply"] is False, calls[-1]

        with _env({"YINYUE_FEATURE_TERMINAL_RECONCILE_APPLY": "1"}):
            asyncio.run(sm.run_terminal_reconcile())
        assert calls[-1]["apply"] is True, calls[-1]

    assert len(calls) == 2, calls
    for call in calls:
        assert call["page_size"] == 500, call
        assert call["max_messages"] == 2000, call
        age = (datetime.now() - call["since"]).total_seconds()
        assert abs(age - 24 * 3600) < 120, call["since"]


def test_alert_is_deduplicated_within_the_hour() -> None:
    """缺口告警 3600s 去重：连续两跑只推一次（仿 PROJECTION_LAG_WARN）。"""
    alerts: list[str] = []

    with _reconcile_patches(
        object(), object(), alerts=alerts,
        spy=lambda **kwargs: _fake_report(missing=3, scanned=5),
    ), patch.object(sm, "_terminal_reconcile_notified_at", 0.0):
        asyncio.run(sm.run_terminal_reconcile())
        asyncio.run(sm.run_terminal_reconcile())

    assert len(alerts) == 1, alerts


def test_flag_off_at_runtime_stops_jobs_without_restart() -> None:
    """免重启承诺：注册后把 flag 置 0，job 立刻不干活；只在切换时记一行日志。"""
    logger = _RecordingLog()
    drains: list[int] = []
    reconciles: list[dict] = []

    def _fake_drain():
        drains.append(1)
        return {"accounts": 0, "picked": 0, "delivered": 0, "retried": 0,
                "failed": 0, "missing_message": 0, "gaps": []}

    def _fake_reconcile(**kwargs):
        reconciles.append(kwargs)
        return {"accounts": 0, "scanned": 0, "matched": 0, "missing": 0,
                "inserted": 0, "failed": 0, "skipped_no_msg_id": 0}

    off = {"YINYUE_FEATURE_OUTBOX_MAINTENANCE": "0",
           "YINYUE_FEATURE_TERMINAL_RECONCILE": "0"}
    with patch.object(sm, "log", logger), patch.object(
        sm, "_jobs_disabled_logged", set()      # 隔离模块级「记过日志」状态
    ), patch.object(
        sm, "_sync_pipeline_running", False
    ), patch.object(
        sm, "_drain_outbox_all_accounts", _fake_drain
    ), patch.object(
        sm, "_reconcile_all_accounts", _fake_reconcile
    ):
        with _env(off):
            asyncio.run(sm.run_outbox_maintenance())
            asyncio.run(sm.run_outbox_maintenance())
            asyncio.run(sm.run_terminal_reconcile())
            asyncio.run(sm.run_terminal_reconcile())

        assert [kw["job"] for kw in logger.named("job_disabled_by_flag")] == [
            "outbox_maintenance", "terminal_reconcile",
        ], logger.events                       # 连跑两次只记一行（不刷屏）
        assert drains == [] and reconciles == [], (drains, reconciles)

        with _env({"YINYUE_FEATURE_OUTBOX_MAINTENANCE": "1",
                   "YINYUE_FEATURE_TERMINAL_RECONCILE": "1"}):
            asyncio.run(sm.run_outbox_maintenance())     # 重开：恢复干活
            asyncio.run(sm.run_terminal_reconcile())
        assert len(drains) == 1 and len(reconciles) == 1, (drains, reconciles)

        with _env(off):
            asyncio.run(sm.run_outbox_maintenance())     # 再关：重新记一行
        assert [kw["job"] for kw in logger.named("job_disabled_by_flag")] == [
            "outbox_maintenance", "terminal_reconcile", "outbox_maintenance",
        ], logger.events


def test_outbox_maintenance_reports_inflight_and_stale() -> None:
    """REC-3/REC-8/P1-a：drain 汇总带 inflight/stale；止损线上的过期行会被收尾。

    P1-a 之后「attempts 到止损线 + 租约过期」不再是永久 stale：下一轮 drain 必须
    回收它（这里投递成功 → delivered）。租约**未**过期的在投行仍只算 inflight，
    不许被抢。stale 出口（/api/status + doctor）与口径②（stale 不单独推通知）由
    下面的合成 totals 覆盖。
    """
    from sqlalchemy import text

    from core.terminal.event_store_db import TerminalEventStore
    from db.outbox_store import claim_outbox_batch

    tmp = _make_temp_dir()
    meta = None
    try:
        meta, _ = _build_meta(tmp)
        repo, _ = _build_account(tmp)
        store = TerminalEventStore(meta.engine)
        _add_message(repo, 9100, created=datetime.now() - timedelta(minutes=5))
        _add_message(repo, 9101, created=datetime.now() - timedelta(minutes=5))
        now = datetime.now()
        claimed = claim_outbox_batch(
            repo.engine, platform=W, account_id=ACCOUNT, now=now,
            claimed_by="dead", lease_seconds=120,
        )
        assert len(claimed) == 2, claimed
        with repo.engine.begin() as conn:
            # 9100：owner 崩了 + attempts 已到止损线（旧实现下永远卡住）
            conn.execute(text(
                "UPDATE event_outbox SET attempts=5, lease_until=:t, "
                "next_attempt_at=:t WHERE msg_id='9100'"
            ), {"t": now - timedelta(minutes=5)})
            # 9101：租约还在有效期内，别人不许抢
            conn.execute(text(
                "UPDATE event_outbox SET lease_until=:t, next_attempt_at=:t "
                "WHERE msg_id='9101'"
            ), {"t": now + timedelta(hours=1)})

        ctx = SimpleNamespace(platform=W, account_id=ACCOUNT, account_repo=repo)
        with patch.object(
            sm, "get_runtime",
            lambda: SimpleNamespace(all_contexts=lambda: [ctx]),
        ), patch(
            "core.terminal.event_runtime.get_terminal_event_store",
            lambda **kwargs: store,
        ), patch(
            "core.terminal.event_runtime.get_event_identity_service",
            lambda: None,
        ):
            totals = sm._drain_outbox_all_accounts()

        assert totals["picked"] == 1, totals          # 只回收过期的那行
        assert totals["delivered"] == 1, totals       # 卡死行终于被投递（P1-a）
        assert totals["failed"] == 0, totals
        assert totals["inflight"] == 1, totals        # 9101 仍在租约内
        assert totals["stale"] == 0, totals           # 不再有永久 stale
        assert totals["gaps"] == [], totals["gaps"]

        # 口径②：stale 只进指标出口，不单独触发通知（用合成 totals 直接验文案路径）
        stale_totals = {
            "accounts": 1, "picked": 0, "delivered": 0, "retried": 0,
            "failed": 0, "missing_message": 0, "inflight": 0, "stale": 2,
            "gaps": [{"platform": W, "account_id": ACCOUNT, "failed": 0,
                      "missing_message": 0, "inflight": 0, "stale": 2}],
        }
        alerts: list[str] = []
        logger = _RecordingLog()
        with patch.object(
            sm, "_drain_outbox_all_accounts", lambda: dict(stale_totals)
        ), patch.object(
            sm, "_sync_pipeline_running", False
        ), patch.object(sm, "log", logger), patch.object(
            sm, "_outbox_notified_at", 0.0
        ), patch.object(
            sm, "_notify", lambda title, message: alerts.append(message)
        ):
            asyncio.run(sm.run_outbox_maintenance())
        assert alerts == [], alerts
        assert logger.named("outbox_maintenance_gaps") == [], logger.events

        # 有 failed 时：文案带 inflight/stale（缺口明细也在）
        with_failed = dict(totals)
        with_failed.update({"failed": 1, "picked": 1, "inflight": 1, "stale": 1})
        with_failed["gaps"] = [{
            "platform": W, "account_id": ACCOUNT, "failed": 1,
            "missing_message": 0, "inflight": 1, "stale": 1,
        }]
        alerts2: list[str] = []
        logger2 = _RecordingLog()
        with patch.object(
            sm, "_drain_outbox_all_accounts", lambda: dict(with_failed)
        ), patch.object(
            sm, "_sync_pipeline_running", False
        ), patch.object(sm, "log", logger2), patch.object(
            sm, "_outbox_notified_at", 0.0
        ), patch.object(
            sm, "_notify", lambda title, message: alerts2.append(message)
        ):
            asyncio.run(sm.run_outbox_maintenance())
        assert alerts2 and "stale=1" in alerts2[0], alerts2
        assert f"{W}/{ACCOUNT}" in alerts2[0], alerts2
        gaps = logger2.named("outbox_maintenance_gaps")
        assert gaps and gaps[0]["stale"] == 1 and gaps[0]["inflight"] == 1, logger2.events
    finally:
        _cleanup(meta, tmp)


def test_truncated_reconcile_scan_warns() -> None:
    """扫描打满 max_messages：必须 warning（窗口最旧那段不能被无声跳过）。"""
    logger = _RecordingLog()
    with _reconcile_patches(
        object(), object(),
        spy=lambda **kwargs: _fake_report(
            scanned=sm.TERMINAL_RECONCILE_MAX_MESSAGES
        ),
    ), patch.object(sm, "log", logger):
        asyncio.run(sm.run_terminal_reconcile())

    truncated = logger.named("terminal_reconcile_truncated")
    assert len(truncated) == 1, logger.events
    assert truncated[0]["scanned"] == sm.TERMINAL_RECONCILE_MAX_MESSAGES, truncated
    assert truncated[0]["max_messages"] == sm.TERMINAL_RECONCILE_MAX_MESSAGES
    assert truncated[0]["window_hours"] == sm.TERMINAL_RECONCILE_WINDOW_HOURS
    assert truncated[0]["account_id"] == ACCOUNT, truncated

    # 没打满就不该报截断
    quiet = _RecordingLog()
    with _reconcile_patches(
        object(), object(),
        spy=lambda **kwargs: _fake_report(
            scanned=sm.TERMINAL_RECONCILE_MAX_MESSAGES - 1
        ),
    ), patch.object(sm, "log", quiet):
        asyncio.run(sm.run_terminal_reconcile())
    assert quiet.named("terminal_reconcile_truncated") == [], quiet.events


def test_notify_failure_is_logged_and_keeps_alert_window() -> None:
    """``_notify`` 不许静默吞异常；推送确认失败也不消费 3600s 去重窗口。"""
    logger = _RecordingLog()
    real_notify = sm._notify
    pushes: list[tuple[str, str]] = []

    def _boom(title, message):
        raise RuntimeError("push down")

    with _reconcile_patches(
        object(), object(),
        spy=lambda **kwargs: _fake_report(missing=3, scanned=5),
    ), patch.object(sm, "_notify", real_notify), patch.object(
        sm, "log", logger
    ), patch.object(
        sm, "send_notification", _boom
    ), patch.object(sm, "_terminal_reconcile_notified_at", 0.0):
        asyncio.run(sm.run_terminal_reconcile())
        assert sm._terminal_reconcile_notified_at == 0.0, "推送失败不能吃掉去重窗口"

        failed = logger.named("notify_failed")
        assert failed, logger.events
        assert "push down" in str(failed[0].get("error")), failed

        with patch.object(
            sm, "send_notification",
            lambda title, message: pushes.append((title, message)),
        ):
            assert sm._notify("微信助理", "连通性测试") is True
    assert pushes and pushes[0][1] == "连通性测试", pushes


def test_out_of_window_hole_is_not_written() -> None:
    """60 天前的洞：不在扫描窗口内；apply 打开也不会被补写。"""
    from core.terminal.event_store_db import TerminalEventStore
    from core.terminal.reconciler import reconcile_account as real_reconcile

    tmp = _make_temp_dir()
    meta = None
    try:
        meta, _ = _build_meta(tmp)
        repo, _ = _build_account(tmp)
        store = TerminalEventStore(meta.engine)
        now = datetime.now()
        _add_message(repo, 9001, created=now - timedelta(days=60), text="六十天前的洞")
        _add_message(repo, 9002, created=now - timedelta(minutes=5), text="今天的洞")

        calls: list = []

        def _spy(**kwargs):
            report = real_reconcile(**kwargs)
            calls.append((kwargs, report))
            return report

        alerts: list[str] = []
        with _reconcile_patches(repo, store, alerts=alerts, spy=_spy), patch.object(
            sm, "_terminal_reconcile_notified_at", 0.0
        ):
            with _env({"YINYUE_FEATURE_TERMINAL_RECONCILE_APPLY": "0"}):
                asyncio.run(sm.run_terminal_reconcile())
            report = calls[-1][1]
            assert report.scanned == 1, report.as_dict()     # 老行在窗口外，没扫
            assert report.missing >= 1, report.as_dict()     # 近 24h 的洞被报出
            assert store.count() == 0                        # dry-run 不写
            assert alerts, "缺口必须告警"

            with _env({"YINYUE_FEATURE_TERMINAL_RECONCILE_APPLY": "1"}):
                asyncio.run(sm.run_terminal_reconcile())
            assert store.count() == 1, store.count()         # 只补今天的洞
            assert store.get_by_external_msg_id(
                platform=W, bot_account_id=ACCOUNT, chat_id="friend_raw",
                external_msg_id="9001",
            ) is None, "窗口外的洞不允许被补写"
            assert store.get_by_external_msg_id(
                platform=W, bot_account_id=ACCOUNT, chat_id="friend_raw",
                external_msg_id="9002",
            ) is not None
    finally:
        _cleanup(meta, tmp)


def main() -> int:
    tests = [
        test_jobs_registered_with_expected_intervals,
        test_flags_off_register_nothing,
        test_flag_off_at_runtime_stops_jobs_without_restart,
        test_apply_flag_drives_reconcile_and_window_is_24h,
        test_alert_is_deduplicated_within_the_hour,
        test_truncated_reconcile_scan_warns,
        test_notify_failure_is_logged_and_keeps_alert_window,
        test_out_of_window_hole_is_not_written,
        test_outbox_maintenance_reports_inflight_and_stale,
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
