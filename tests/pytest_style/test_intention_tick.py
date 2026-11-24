# -*- coding: utf-8 -*-
"""Step 12（交接 §5 TODO 1）：意图调度 tick —— 到点执行 / 关闭 / 失败重试 / 防双投。

覆盖：
① 默认 off = 零登记零执行：create_reminder 不落意图，tick 不扫不执行；
② on：创建提醒时登记 ``reminder:<id>@<due>`` 意图；
③ shadow：只审计 ``intention_tick_shadow``，不执行、意图保持 open；
④ on：tick 到点执行成功 → reminders.done + intention_closed；
⑤ 可重试失败：前两次留 open（下一 tick 重试），第 3 次止损 → failed + closed；
⑥ 双轨防重：``INTENTION_TICK=on`` 且仍有 open 意图时，``run_reminder_delivery``
   让位；意图丢失/flag 非 on 时扫描器照旧兜底；
⑦ 重复提醒：成功后旧意图关闭、按下一到点时刻开新意图（新 key）。
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from structlog.testing import capture_logs

# 2026-11-24：这个模块在 import 时改 os.environ（为了把库指到 data/_intention_tick_test）。
# 不改回来会串给同进程里后跑的测试（实测 legacy_read_binding 因此红）。import 前快照，模块测完还原。
_ENV_SNAPSHOT = {k: os.environ.get(k) for k in (
    "LOG_DIR", "META_DB_PATH", "PLATFORMS_DIR", "DATA_DIR", "YINYUE_WORLD_MODEL_DIR")}

_TEST_LOG_DIR = str(Path("data") / "_test_logs_intention_tick")
Path(_TEST_LOG_DIR).mkdir(parents=True, exist_ok=True)
os.environ["LOG_DIR"] = _TEST_LOG_DIR
_ROOT = Path(__file__).resolve().parents[2]
_ISO = (_ROOT / "data" / "_intention_tick_test" / "_iso").resolve()
_ISO.mkdir(parents=True, exist_ok=True)
os.environ["META_DB_PATH"] = str(_ISO / "meta.db")  # 强制本模块的库，别沿用别处 import 时留下的
os.environ["PLATFORMS_DIR"] = str(_ISO / "platforms")
os.environ["DATA_DIR"] = str(_ISO / "data")
# 2026-11-24：世界模型目录以前没指到 ISO，会落到默认（生产）目录——别的测试留下的
# 世界模型会串进来，_purge_disk 也会去删那边的文件。这里显式指到测试目录。
os.environ["YINYUE_WORLD_MODEL_DIR"] = str(_ISO / "world")
sys.path.insert(0, str(_ROOT))

import core.infra.logging as _logging  # noqa: E402

_logging.init_logging = lambda *args, **kwargs: None

import agents.reminder_trigger as rt  # noqa: E402
import scheduler.main as sm  # noqa: E402
from agents.tools.reminder import CreateReminderTool  # noqa: E402
from core import features  # noqa: E402
from core.context import intention_tick  # noqa: E402
from core.context.intention_bridge import (  # noqa: E402
    reminder_due_text,
    reminder_intention_key,
)
from core.context.intention_runner import IntentionExecution  # noqa: E402
from core.context.situation import open_intention, open_intentions, project  # noqa: E402
from core.context.situation_store import get_world_model, reset_world_models  # noqa: E402

_FLAG = features.env_key(features.INTENTION_TICK)
CHAT = "wxid_friend"


@pytest.fixture(scope="module", autouse=True)
def _restore_environ_after_module():
    yield
    for key, value in _ENV_SNAPSHOT.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


@pytest.fixture(autouse=True)
def _clean_world_models(monkeypatch):
    # 2026-11-02：open_reminder_intention 会 persist 世界模型到磁盘，
    # reset_world_models() 只清内存 —— 不清盘就会把上一个用例的意图带进下一个。
    from core.context.situation_store import _model_path

    def _purge_disk() -> None:
        for cid in (CHAT, "c-off"):
            path = _model_path(cid)
            if path is not None:
                try:
                    path.unlink()
                except OSError:
                    pass
        # 2026-11-24：带 scope 的世界模型文件名是 (平台,账号,会话) 的 sha256，按 chat 算的
        # 路径删不到它；这个目录本来就是本模块专用的，直接把 *.json 清干净。
        for stale in (_ISO / "world").glob("*.json"):
            try:
                stale.unlink()
            except OSError:
                pass

    # 2026-11-24：别的测试可能把 _READ_SCOPE 留在这个线程上，世界模型会按「带 scope 的键」
    # 落盘，而这里按 chat 键 purge —— 对不上就会把上一轮的意图带进来。测试期间显式清空。
    # 同时每个用例都强制指回 ISO 目录：别的模块 import 时加载 .env 会把
    # YINYUE_WORLD_MODEL_DIR 改成 data/world_models，测试就会读到历史残留。
    monkeypatch.setenv("YINYUE_WORLD_MODEL_DIR", str(_ISO / "world"))
    monkeypatch.setenv("META_DB_PATH", str(_ISO / "meta.db"))
    monkeypatch.setenv("DATA_DIR", str(_ISO / "data"))
    monkeypatch.setenv("PLATFORMS_DIR", str(_ISO / "platforms"))
    from loop import ports as _ports

    scope_token = _ports._READ_SCOPE.set(None)
    _purge_disk()
    reset_world_models()
    try:
        yield
    finally:
        reset_world_models()
        _purge_disk()
        _ports._READ_SCOPE.reset(scope_token)


def _due_at(hours: int = -1) -> datetime:
    return datetime.now() + timedelta(hours=hours)


def _future_text(hours: int = 1) -> str:
    return (datetime.now() + timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M")


def _row(**overrides) -> SimpleNamespace:
    row = {
        "id": 11,
        "content": "该开会了",
        "remind_at": _due_at(-1),
        "status": "pending",
        "repeat_rule": None,
        "attempts": 0,
        "source_platform": "wechat",
        "source_account_id": "wxid_bot",
        "source_chat_id": CHAT,
        "source_chat_type": "private",
        "mention_name": None,
        "mention_names": None,
        "purpose": "remind",
    }
    row.update(overrides)
    return SimpleNamespace(**row)


class _FakeReminders:
    """提醒仓储替身：语义对齐真 ``ReminderRepository`` 的读/写口。"""

    def __init__(self, rows=None):
        self.rows = list(rows or [])

    def add(self, reminder):
        reminder.id = 1 if not self.rows else max(r.id for r in self.rows) + 1
        self.rows.append(reminder)
        return reminder

    def list_pending(self):
        now = datetime.now()
        return [
            r for r in self.rows
            if r.status == "pending" and r.remind_at is not None and r.remind_at <= now
        ]

    def list_all(self, limit=50):
        return list(self.rows)[:limit]

    def get(self, reminder_id):
        for r in self.rows:
            if r.id == reminder_id:
                return r
        return None

    def record_attempt(self, reminder_id):
        row = self.get(reminder_id)
        if row is None:
            return 0
        row.attempts = int(row.attempts or 0) + 1
        return row.attempts

    def mark_done(self, reminder_id):
        row = self.get(reminder_id)
        if row is None:
            return
        if row.repeat_rule:
            next_at = self._calc_next_repeat(row.remind_at, row.repeat_rule)
            if next_at:
                row.remind_at = next_at
                row.status = "pending"
                row.attempts = 0
                return
        row.status = "done"

    def mark_failed(self, reminder_id):
        row = self.get(reminder_id)
        if row is not None:
            row.status = "failed"

    def count_pending_for_requester(self, *args, **kwargs):
        return 0

    def _calc_next_repeat(self, current, rule):
        if rule == "daily" and isinstance(current, datetime):
            return current + timedelta(days=1)
        return None


class _Repo:
    def __init__(self, rows=None):
        self.reminders = _FakeReminders(rows)


class _FakePersonas:
    def list_active(self):
        return [SimpleNamespace(id=7)]


class _ToolRepo(_Repo):
    def __init__(self):
        super().__init__()
        self.personas = _FakePersonas()


def _ctx(chat_id: str = CHAT, *, is_group: bool = False, name: str = "老王"):
    return SimpleNamespace(
        extra={
            "platform": "wechat",
            "account_id": "wxid_bot",
            "chat_id": chat_id,
            "is_group": is_group,
            "sender_id": chat_id,
            "sender_name": name,
        },
        account_context=None,
        accounts={"wechat": "wxid_bot"},
        owner_accounts={},
        messages=[],
    )


def _create_reminder(repo, monkeypatch, *, chat_id: str = CHAT, **kwargs):
    tool = CreateReminderTool(repo)
    tool.set_context(_ctx(chat_id, is_group=kwargs.pop("is_group", False)))
    tool.authoritative_is_owner = lambda: True
    return asyncio.run(tool.execute(
        content=kwargs.pop("content", "开会"),
        remind_at=kwargs.pop("remind_at", _future_text()),
        **kwargs,
    ))


def _open_due_intention(chat_id: str = CHAT, reminder_id: int = 11, due=None) -> str:
    due = due or _due_at(-1)
    # 2026-11-09：关闭要验 reminder:<id> 在同一会话，world 必须带完整坐标。
    # 生产的 world 由 known_worlds() 从磁盘读、天然有三元组；这里用同样的形状建。
    model = get_world_model(("wechat", "wxid_bot", chat_id))
    key = reminder_intention_key(reminder_id, due)
    open_intention(model, key, kind="remind", due_at=reminder_due_text(due))
    return key


# ------------------------------------------------------------------ ① off
def test_off_is_noop(monkeypatch) -> None:
    monkeypatch.delenv(_FLAG, raising=False)
    assert intention_tick.tick_mode() == "off"

    repo = _ToolRepo()
    result = _create_reminder(repo, monkeypatch)
    assert not result.is_error, result.error
    assert open_intentions(get_world_model(CHAT)) == ()

    key = _open_due_intention(chat_id="c-off", reminder_id=99)
    called: list[tuple] = []

    async def execute(chat_id, intent):  # pragma: no cover - off 不许被调到
        called.append((chat_id, intent.key))
        return IntentionExecution(ok=True)

    report = asyncio.run(intention_tick.run_tick(execute, models={
        "c-off": get_world_model("c-off"),
    }))
    assert report.mode == "off" and report.due == 0
    assert called == []
    assert project(get_world_model("c-off").events).intentions[key].status == "open"


# ------------------------------------------------------------------ ② on 登记
def test_on_create_registers_reminder_intention(monkeypatch) -> None:
    monkeypatch.setenv(_FLAG, "on")
    repo = _ToolRepo()
    remind_at = _future_text()
    result = _create_reminder(repo, monkeypatch, remind_at=remind_at)
    assert not result.is_error, result.error

    opened = open_intentions(get_world_model(CHAT), kind="remind")
    assert len(opened) == 1, opened
    expected_due = datetime.strptime(remind_at, "%Y-%m-%d %H:%M")
    assert opened[0].key == reminder_intention_key(1, expected_due)
    assert opened[0].due_at == remind_at


# ------------------------------------------------------------------ ③ shadow
def test_shadow_audits_without_executing(monkeypatch) -> None:
    monkeypatch.setenv(_FLAG, "shadow")
    key = _open_due_intention()
    called: list[tuple] = []

    async def execute(chat_id, intent):  # pragma: no cover - shadow 不许执行
        called.append((chat_id, intent.key))
        return IntentionExecution(ok=True)

    with capture_logs() as logs:
        report = asyncio.run(intention_tick.run_tick(execute))

    assert report.mode == "shadow"
    assert report.due == 1 and report.executed == 0 and report.retried == 0
    assert called == []
    assert project(get_world_model(CHAT).events).intentions[key].status == "open"
    shadows = [row for row in logs if row.get("event") == "intention_tick_shadow"]
    assert shadows and shadows[0]["due"] == 1 and shadows[0]["keys"] == [key], logs


# ------------------------------------------------------------------ ④ on 执行
def test_on_tick_executes_and_closes(monkeypatch) -> None:
    monkeypatch.setenv(_FLAG, "on")
    key = _open_due_intention()
    # 关闭要验 reminder:11 同会话且已终态：仓库打桩成「这条已 done」。
    repo = _Repo([_row(status="done")])
    from core.terminal import store_port as _store_port
    monkeypatch.setattr(_store_port, "get_repo", lambda: repo)
    seen: list[tuple] = []

    async def execute(chat_id, intent):
        seen.append((chat_id, intent.key))
        return IntentionExecution(ok=True, result="sent", evidence_ref="reminder:11")

    report = asyncio.run(intention_tick.run_tick(execute))

    assert seen == [(CHAT, key)]
    assert report.executed == 1 and report.closed == 1 and report.retried == 0
    view = project(get_world_model(CHAT).events)
    assert view.intentions[key].status == "closed"
    assert view.intentions[key].result == "sent"


# ------------------------------------------------------------------ ⑤ 重试
def test_retry_stays_open_until_attempt_limit(monkeypatch) -> None:
    monkeypatch.setenv(_FLAG, "on")
    key = _open_due_intention()
    calls: list[int] = []

    async def flaky(chat_id, intent):
        calls.append(1)
        return IntentionExecution(
            ok=False, result="send_failed", retryable=True, attempts=len(calls),
        )

    for _ in range(2):
        report = asyncio.run(intention_tick.run_tick(flaky))
        assert report.retried == 1 and report.closed == 0 and report.failed == 0
        assert project(get_world_model(CHAT).events).intentions[key].status == "open"

    report = asyncio.run(intention_tick.run_tick(flaky))
    assert report.closed == 1 and report.failed == 1 and report.retried == 0
    view = project(get_world_model(CHAT).events)
    assert view.intentions[key].status == "closed"
    assert view.intentions[key].result == "send_failed", view.intentions[key]
    assert len(calls) == 3


# ------------------------------------------------------------- ⑥ 调度器端到端
def _patch_delivery(monkeypatch, repo, turn):
    monkeypatch.setattr(sm, "get_repo", lambda: repo)
    monkeypatch.setattr(rt, "run_reminder_turn", turn)
    # 2026-11-09：关闭时 goal_close._verified_ref 走 core.terminal.store_port.get_repo
    # 验 reminder:<id>（同会话 + 终态），与 sm.get_repo 是两个入口，必须一起打桩，
    # 否则证据校验查真库 → goal_evidence_unverified。
    from core.terminal import store_port as _store_port
    monkeypatch.setattr(_store_port, "get_repo", lambda: repo)


def test_scheduler_tick_delivers_and_closes(monkeypatch) -> None:
    monkeypatch.setenv(_FLAG, "on")
    row = _row()
    repo = _Repo([row])
    key = _open_due_intention(due=row.remind_at)

    async def fake_turn(**kwargs):
        return rt.ProactiveTurnResult(True, sent=True, reply="到点啦")

    _patch_delivery(monkeypatch, repo, fake_turn)
    report = asyncio.run(sm.run_intention_tick())

    assert report.executed == 1
    assert row.status == "done"
    view = project(get_world_model(CHAT).events)
    assert view.intentions[key].status == "closed"
    assert view.intentions[key].result == "sent"


def test_scheduler_tick_failure_retries_then_stops(monkeypatch) -> None:
    monkeypatch.setenv(_FLAG, "on")
    row = _row()
    repo = _Repo([row])
    key = _open_due_intention(due=row.remind_at)

    async def failing_turn(**kwargs):
        return rt.ProactiveTurnResult(False, sent=False, reason="send_failed")

    notifications: list[tuple] = []
    _patch_delivery(monkeypatch, repo, failing_turn)
    monkeypatch.setattr(
        sm, "send_notification", lambda *args, **kwargs: notifications.append(args),
    )

    first = asyncio.run(sm.run_intention_tick())
    assert first.retried == 1 and row.attempts == 1 and row.status == "pending"
    second = asyncio.run(sm.run_intention_tick())
    assert second.retried == 1 and row.attempts == 2 and row.status == "pending"
    third = asyncio.run(sm.run_intention_tick())
    assert third.closed == 1 and third.failed == 1
    assert row.attempts == 3 and row.status == "failed"
    assert len(notifications) == 1, notifications
    view = project(get_world_model(CHAT).events)
    assert view.intentions[key].status == "closed"


def test_scan_defers_governed_reminder_to_tick(monkeypatch) -> None:
    monkeypatch.setenv(_FLAG, "on")
    row = _row(remind_at=datetime.now() - timedelta(minutes=1))
    repo = _Repo([row])
    _open_due_intention(due=row.remind_at)

    async def must_not_run(**kwargs):  # pragma: no cover - 扫描器必须让位
        raise AssertionError("有 open 意图的提醒不许被扫描器投递")

    _patch_delivery(monkeypatch, repo, must_not_run)
    asyncio.run(sm.run_reminder_delivery())
    assert row.attempts == 0 and row.status == "pending"

    # 意图台账丢失（重启/未落盘）时，扫描器必须照旧兜底投递——不静默。
    reset_world_models()
    delivered: list[dict] = []

    async def fake_turn(**kwargs):
        delivered.append(kwargs)
        return rt.ProactiveTurnResult(True, sent=True)

    _patch_delivery(monkeypatch, repo, fake_turn)
    asyncio.run(sm.run_reminder_delivery())
    assert row.status == "done" and len(delivered) == 1


def test_stale_open_intention_falls_back_to_scanner(monkeypatch) -> None:
    """tick 停摆（到点意图超时未收口）→ 扫描器兜底投递，不静默积压。"""
    monkeypatch.setenv(_FLAG, "on")
    row = _row(remind_at=datetime.now() - timedelta(hours=1))
    repo = _Repo([row])
    _open_due_intention(due=row.remind_at)
    delivered: list[dict] = []

    async def fake_turn(**kwargs):
        delivered.append(kwargs)
        return rt.ProactiveTurnResult(True, sent=True)

    _patch_delivery(monkeypatch, repo, fake_turn)
    asyncio.run(sm.run_reminder_delivery())

    assert row.status == "done" and len(delivered) == 1


def test_repeat_success_opens_next_occurrence_intention(monkeypatch) -> None:
    monkeypatch.setenv(_FLAG, "on")
    row = _row(repeat_rule="daily")
    repo = _Repo([row])
    first_due = row.remind_at
    first_key = _open_due_intention(due=first_due)

    async def fake_turn(**kwargs):
        return rt.ProactiveTurnResult(True, sent=True)

    _patch_delivery(monkeypatch, repo, fake_turn)
    report = asyncio.run(sm.run_intention_tick())

    assert report.executed == 1
    next_due = first_due + timedelta(days=1)
    assert row.status == "pending" and row.remind_at == next_due
    view = project(get_world_model(CHAT).events)
    assert view.intentions[first_key].status == "closed"
    second_key = reminder_intention_key(11, next_due)
    assert view.intentions[second_key].status == "open"

def test_shadow_scan_delivers_and_closes_mirror_intention(monkeypatch) -> None:
    """shadow：tick 只审计，扫描器照投；终局后镜像意图收口（不重复审计）。"""
    monkeypatch.setenv(_FLAG, "shadow")
    row = _row()
    repo = _Repo([row])
    key = _open_due_intention(due=row.remind_at)

    async def fake_turn(**kwargs):
        return rt.ProactiveTurnResult(True, sent=True)

    _patch_delivery(monkeypatch, repo, fake_turn)
    with capture_logs() as logs:
        report = asyncio.run(sm.run_intention_tick())
    assert report.mode == "shadow" and report.due == 1
    assert any(entry.get("event") == "intention_tick_shadow" for entry in logs), logs
    assert row.status == "pending"
    assert project(get_world_model(CHAT).events).intentions[key].status == "open"

    asyncio.run(sm.run_reminder_delivery())
    assert row.status == "done"
    view = project(get_world_model(CHAT).events)
    assert view.intentions[key].status == "closed"
    assert "delivered_by_scanner" in view.intentions[key].result
