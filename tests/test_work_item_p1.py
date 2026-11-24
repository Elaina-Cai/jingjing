# -*- coding: utf-8 -*-
"""P1 落地回归：``work_items`` / ``work_item_runs``（任务模型 §7.1 ① / §5 / §11 P1）。

覆盖九组断言：

① **flag 关 = 零写入**：``work_item_ledger`` 默认关 → 两表 0 行、0 个 ``work_item_*``
   事件，而 ``reminders`` 照旧 1 行（执行权威没变）；
② **创建 → 一行一事件**：一条「提醒我 X」后 ``work_items`` 1 行（kind/trigger/status/
   due_at/goal/priority/max_attempts/idempotency_key/target/legacy_reminder_id）+ 一条
   ``work_item_created``（§5.9 必填齐、**事件行实列 external_msg_id 为空**、metadata 有
   ``turn_id``/``external_msg_id``/``principal``），并能按同一个 ``turn_id`` 串起
   消息事件、任务事件与回复事件；
③ **双写失败不影响创建**：``store.record_created`` 抛错 → 工具仍成功返回、reminders
   仍 1 行、没有异常上抛（ToolResult 逐字不变）；
④ **闲聊/即时问答不产生任务行**：写入点唯一（全仓调用点扫描）+ 被拒的创建不落行；
⑤ **重放幂等**：同 ``legacy_reminder_id`` 再来一次 → 仍 1 行 1 事件；
⑥ **投递观测**：真跑 ``run_reminder_delivery``（投递端被替身）→ sent→done；
   daily→保持 open 且 due_at 顺延、occurrences_done+1；连失 3 次→failed + last_error；
   另外 ``work_item_runs`` 单独开（LEDGER 关）不写任何 run；
   ⑥b **重复口径由权威侧回传**（P1-b）：映射认不出的 ``12小时`` / ``2.5h`` / ``1h1d`` /
   ``-2h``（``0h`` 顺延到原地）在权威侧仍会 pending → 投影保持 open、``due_at`` 与
   ``reminders.remind_at`` **逐值一致**；``每周一`` 两边都不认 → 两边都 done；
   探针拿不到权威结论（替身 repo）→ 保守留 open 并标 ``repeat_unrecognized``；
⑦ **建表 parity**：``create_all`` 库 vs 「v51 + ``run_migrations``」库的 PRAGMA 逐列
   （类型 / NOT NULL / DEFAULT / pk）与索引形状一致；账号库只盖章不建表；52 不在
   ``_META_MIGRATION_VERSIONS`` / ``_ACCOUNT_SKIP_MIGRATION_VERSIONS`` 里；
⑧ ``as_work_item_fields()`` 的键 ⊆ ``work_items`` 列集合，且除身份两列外每个 NOT NULL
   列都有 DEFAULT（迁移脚本只传映射字段也能插入）；
⑨ **迁移脚本 CLI 契约**（P1-a）：``--apply --json`` 首次插入后 stdout 仍是纯 JSON
   （``json.loads`` 成功），日志一律在 stderr；幂等重跑同样干净。

隔离前导照 ``tests/test_reminder_mapping.py``：LOG_DIR / META_DB_PATH / PLATFORMS_DIR /
DATA_DIR 全部指向仓库内 scratch 目录；**测试自带 meta 库是 ``create_all`` 出来的临时
文件，绝不碰生产库**。

Run directly:
    python tests/test_work_item_p1.py
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sqlite3
import subprocess
import sys
import traceback
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

ROOT = Path(__file__).resolve().parent.parent
_TEST_LOG_DIR = str(Path("data") / "_test_logs_work_item_p1")
Path(_TEST_LOG_DIR).mkdir(parents=True, exist_ok=True)
os.environ["LOG_DIR"] = _TEST_LOG_DIR
_ISO_ROOT = (ROOT / "data" / "_work_item_p1_test" / "_iso").resolve()
_ISO_ROOT.mkdir(parents=True, exist_ok=True)
os.environ["META_DB_PATH"] = str(_ISO_ROOT / "meta.db")
os.environ["PLATFORMS_DIR"] = str(_ISO_ROOT / "platforms")
os.environ["DATA_DIR"] = str(_ISO_ROOT / "data")

# scheduler.main 在导入期就会用到日志初始化：先拦掉，日志只进隔离目录。
import core.infra.logging as _logging  # noqa: E402

_logging.init_logging = lambda *args, **kwargs: None

from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy import UniqueConstraint  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

import scheduler.main as sm  # noqa: E402
import agents.reminder_trigger as rt  # noqa: E402
from agents.tools.reminder import CreateReminderTool  # noqa: E402
from core import features  # noqa: E402
from core.tasks import store  # noqa: E402
from core.terminal.event_model import Event, EventType  # noqa: E402
from core.terminal.event_store_db import TerminalEventStore  # noqa: E402
from db.meta_models import (  # noqa: E402
    MetaBase,
    TerminalEvent,
    WorkItemRow,
    WorkItemRunRow,
)

TURN_ID = "turn-p1-test"
PARENT_EVENT_ID = "evt-parent-p1"
MSG_ID = "msg-p1-test"

#: §5.9 ``work_item_created`` 的必填字段（target / requester 是嵌套结构）。
REQUIRED_CREATED = (
    "schema_version",
    "work_item_id",
    "kind",
    "trigger",
    "status",
    "due_at",
    "priority",
    "target",
    "requester",
    "spec_json",
    "idempotency_key",
)
REQUIRED_RUN_STARTED = (
    "schema_version",
    "work_item_id",
    "run_id",
    "run_seq",
    "trigger_source",
    "claim_owner",
    "claimed_at",
)
REQUIRED_RUN_FINISHED = (
    "schema_version",
    "work_item_id",
    "run_id",
    "run_seq",
    "outcome",
    "duration_ms",
    "send_ids",
    "trace_ref",
)


# ------------------------------------------------------------------- 隔离设施
@contextmanager
def flags(*, ledger: bool, runs: bool = False):
    """临时改 flag（``features.enabled`` 每次读 env，不用清模块缓存）。"""
    keys = (features.WORK_ITEM_LEDGER, features.WORK_ITEM_RUNS)
    saved = {key: os.environ.get(features.env_key(key)) for key in keys}
    os.environ[features.env_key(features.WORK_ITEM_LEDGER)] = "1" if ledger else "0"
    os.environ[features.env_key(features.WORK_ITEM_RUNS)] = "1" if runs else "0"
    try:
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(features.env_key(key), None)
            else:
                os.environ[features.env_key(key)] = value


def _flags_off() -> None:
    for key in (features.WORK_ITEM_LEDGER, features.WORK_ITEM_RUNS):
        os.environ[features.env_key(key)] = "0"


def _fresh_engine(name: str):
    """create_all 出来的干净 meta 库（隔离根内）。"""
    path = _ISO_ROOT / f"{name}.db"
    for suffix in ("", "-wal", "-shm"):
        Path(str(path) + suffix).unlink(missing_ok=True)
    engine = create_engine(f"sqlite:///{path}")
    MetaBase.metadata.create_all(engine)
    return engine, path


def _count(engine, table: str) -> int:
    with engine.connect() as conn:
        return int(conn.execute(text(f"select count(*) from {table}")).scalar() or 0)


def _events(engine, event_type: str = "") -> list[TerminalEvent]:
    with Session(engine) as session:
        query = session.query(TerminalEvent)
        if event_type:
            query = query.filter(TerminalEvent.event_type == event_type)
        return list(query.order_by(TerminalEvent.id).all())


def _work_item(engine, **filters) -> WorkItemRow | None:
    with Session(engine) as session:
        query = session.query(WorkItemRow)
        if "legacy_reminder_id" in filters:
            query = query.filter(
                WorkItemRow.legacy_reminder_id == filters["legacy_reminder_id"]
            )
        return query.order_by(WorkItemRow.id.desc()).first()


def _runs(engine, work_item_id: str = "") -> list[WorkItemRunRow]:
    with Session(engine) as session:
        query = session.query(WorkItemRunRow)
        if work_item_id:
            query = query.filter(WorkItemRunRow.work_item_id == work_item_id)
        return list(query.order_by(WorkItemRunRow.run_seq).all())


# ------------------------------------------------------------------- 替身对象
class _FakePersonas:
    def list_active(self):
        return [SimpleNamespace(id=7)]


class _FakeReminders:
    def __init__(self):
        self.added = []

    def count_pending_for_requester(self, requester_id, status="pending"):
        return 0

    def add(self, reminder):
        reminder.id = len(self.added) + 1
        self.added.append(reminder)
        return reminder


class _FakeRepo:
    """创建路径的 repo 替身：reminders 在 META，同时暴露 meta engine。"""

    def __init__(self, engine):
        self.personas = _FakePersonas()
        self.reminders = _FakeReminders()
        self.meta_engine = engine
        self.meta = SimpleNamespace(engine=engine)


class _ScanRepo:
    """投递路径的 repo 替身（照 tests/test_reminder_delivery.py 的 _ScanRepo）。

    ``_calc_next_repeat`` 借的是**权威侧那一个静态函数**（``mark_done`` 内部用的
    同一个，``db/repo_reminder.py``）：替身只提供行，不重写判定口径。
    """

    def __init__(self, engine, due):
        from db.repo_reminder import ReminderRepository

        self._due = list(due)
        self.meta_engine = engine
        self.meta = SimpleNamespace(engine=engine)
        self.attempts: dict[int, int] = {}
        self.done: list[int] = []
        self.failed: list[int] = []
        self.reminders = SimpleNamespace(
            list_pending=self.list_pending,
            record_attempt=self.record_attempt,
            mark_done=self.mark_done,
            mark_failed=self.mark_failed,
            _calc_next_repeat=ReminderRepository._calc_next_repeat,
        )

    def list_pending(self):
        return list(self._due)

    def record_attempt(self, reminder_id: int) -> int:
        self.attempts[reminder_id] = self.attempts.get(reminder_id, 0) + 1
        return self.attempts[reminder_id]

    def mark_done(self, reminder_id: int):
        self.done.append(reminder_id)

    def mark_failed(self, reminder_id: int):
        self.failed.append(reminder_id)


def _ctx(**extra_overrides):
    extra = {
        "platform": "wechat",
        "account_id": "bot_a",
        "chat_id": "room@chatroom",
        "is_group": True,
        "sender_id": "p_owner",
        "sender_name": "老板",
        "turn_id": TURN_ID,
        "parent_event_id": PARENT_EVENT_ID,
    }
    extra.update(extra_overrides)
    return SimpleNamespace(
        extra=extra,
        messages=[{"msg_id": MSG_ID, "turn_id": TURN_ID}],
        account_context=None,
        accounts={"wechat": "bot_a"},
        owner_accounts={},
    )


def _reminder(**overrides):
    """一条真的 ``db.models.Reminder``（字段与工具写库时同形）。"""
    from db.models import Reminder

    now = datetime.now()
    data = {
        "persona_id": 7,
        "content": "该开会了",
        "remind_at": (now + timedelta(hours=1)).replace(microsecond=0),
        "status": "pending",
        "repeat_rule": None,
        "source_platform": "wechat",
        "source_account_id": "bot_a",
        "source_chat_id": "room@chatroom",
        "source_chat_type": "group",
        "requester_id": "p_owner",
        "requester_name": "老板",
        "requester_is_owner": 1,
        "mention_name": None,
        "mention_names": json.dumps(["老板"], ensure_ascii=False),
        "purpose": "remind",
        "attempts": 0,
        "created_at": now.replace(microsecond=0),
    }
    data.update(overrides)
    reminder = Reminder(**data)
    reminder.id = overrides.get("id", 11)
    return reminder


def _due(**overrides):
    row = {
        "id": 11,
        "content": "该开会了",
        "remind_at": (datetime.now() - timedelta(minutes=1)).replace(microsecond=0),
        "repeat_rule": None,
        "source_platform": "wechat",
        "source_account_id": "bot_a",
        "source_chat_id": "room@chatroom",
        "source_chat_type": "group",
        "requester_id": "p_owner",
        "requester_name": "老板",
        "requester_is_owner": 1,
        "mention_name": None,
        "mention_names": None,
        "purpose": "remind",
    }
    row.update(overrides)
    return SimpleNamespace(**row)


@contextmanager
def _patch(obj, name, value):
    original = getattr(obj, name)
    setattr(obj, name, value)
    try:
        yield
    finally:
        setattr(obj, name, original)


def _message_event(engine, *, turn_id: str, external_msg_id: str) -> Event:
    """写一条触发消息事件（采集侧同形），供 turn 链断言用。"""
    store_ = TerminalEventStore(engine)
    event = Event(
        event_id=PARENT_EVENT_ID,
        occurred_at=datetime.now(),
        platform="wechat",
        bot_account_id="bot_a",
        chat_id="room@chatroom",
        chat_type="group",
        actor_person_id="p_owner",
        event_type=EventType.MESSAGE,
        content="提醒我 8 点开会",
        turn_id=turn_id,
        external_msg_id=external_msg_id,
    )
    return store_.append(event)


def _reply_event(engine, *, turn_id: str, parent_event_id: str) -> Event:
    store_ = TerminalEventStore(engine)
    event = Event(
        event_id="evt-reply-p1",
        occurred_at=datetime.now(),
        platform="wechat",
        bot_account_id="bot_a",
        chat_id="room@chatroom",
        chat_type="group",
        event_type=EventType.REPLY,
        content="好，8 点叫你",
        turn_id=turn_id,
        parent_event_id=parent_event_id,
    )
    return store_.append(event)


def _run_tool(engine, **kwargs):
    """跑一次真工具（repo 替身 + 真 store + 隔离 meta 库）。"""
    repo = _FakeRepo(engine)
    tool = CreateReminderTool(repo)
    tool.authoritative_is_owner = lambda: True
    tool.set_context(_ctx())
    remind_at = (datetime.now() + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M")
    result = asyncio.run(tool.execute(
        content="开会", remind_at=remind_at, mention=["我"], **kwargs
    ))
    return repo, result


# ----------------------------------------------------------------- ① flag 关
def test_flag_off_writes_nothing():
    _flags_off()
    engine, _ = _fresh_engine("flag_off")
    try:
        repo, result = _run_tool(engine)
        assert not result.is_error, result.error
        assert len(repo.reminders.added) == 1          # reminders 照旧落行
        assert _count(engine, "work_items") == 0
        assert _count(engine, "work_item_runs") == 0
        assert _events(engine, "work_item_created") == []
        assert [e for e in _events(engine) if e.event_type.startswith("work_item")] == []
    finally:
        engine.dispose()


# ------------------------------------------------------------- ② 创建 → 一行
def test_create_writes_one_row_and_one_event():
    engine, _ = _fresh_engine("created")
    message = _message_event(engine, turn_id=TURN_ID, external_msg_id=MSG_ID)
    try:
        with flags(ledger=True):
            repo, result = _run_tool(engine)
        assert not result.is_error, result.error
        assert result.data["id"] == 1

        row = _work_item(engine, legacy_reminder_id=1)
        assert row is not None
        mapped = repo.reminders.added[0]
        assert row.kind == "notify"
        assert row.trigger == "at"
        assert row.status == "open"
        assert row.due_at == mapped.remind_at
        assert row.goal == "开会"
        assert row.priority == 1
        assert row.max_attempts == 3
        assert row.idempotency_key == row.work_item_id
        assert row.target_platform == "wechat"
        assert row.target_bot_account_id == "bot_a"
        assert row.target_chat_id == "room@chatroom"
        assert row.target_chat_type == "group"
        assert row.origin_chat_id == "room@chatroom"
        assert row.requester_person_id == "p_owner"
        assert row.requester_is_owner == 1
        assert row.legacy_reminder_id == 1
        assert row.external_msg_id == MSG_ID
        assert row.acceptance_criteria == "send_log.ok"
        assert json.loads(row.mention_names_json) == ["老板"]
        assert row.visibility_scope == "group_members"
        assert row.created_at is not None and row.updated_at is not None

        created = _events(engine, "work_item_created")
        assert len(created) == 1
        event = created[0]
        payload = json.loads(event.action_payload_json)
        for key in REQUIRED_CREATED:
            assert key in payload, key
        assert payload["work_item_id"] == row.work_item_id
        assert payload["idempotency_key"] == row.work_item_id
        assert payload["kind"] == "notify"
        assert payload["status"] == "open"
        assert payload["target"]["chat_type"] == "group"
        assert payload["requester"]["person_id"] == "p_owner"
        assert payload["requester"]["is_owner"] is True
        assert payload["spec_json"]["content"] == "开会"
        assert payload["trigger_event_id"] == message.event_id
        # **实列留空**：写了会被 append 的四元组查重静默吞掉（研究实测的坑）。
        assert event.external_msg_id in (None, "")
        meta = json.loads(event.metadata_json)
        assert meta["turn_id"] == TURN_ID
        assert meta["external_msg_id"] == MSG_ID
        assert meta["principal"]["person_id"] == "p_owner"
        assert event.turn_id == TURN_ID
        assert event.parent_event_id == message.event_id
        assert event.actor_person_id == "p_owner"
        assert "group_members" in event.visibility_scope_json
        assert row.source_event_id == event.event_id

        # trace 链：同一个 turn_id 能串起消息 → 任务 → 回复。
        reply = _reply_event(engine, turn_id=TURN_ID, parent_event_id=message.event_id)
        chained = _events(engine)
        same_turn = [e for e in chained if e.turn_id == TURN_ID]
        assert {e.event_id for e in same_turn} == {
            message.event_id, event.event_id, reply.event_id,
        }
        assert reply.parent_event_id == message.event_id
        assert event.parent_event_id == message.event_id
        assert json.loads(json.dumps({"ref": row.source_event_id}))["ref"] == event.event_id
    finally:
        engine.dispose()


# --------------------------------------- ②b 群友（非主人）创建 → 同样一行一事件
def test_group_guest_reminder_projects_work_item():
    """主人拍板 2026-10：群里任何人都能让她建提醒，投影口径不变。

    非主人群友（无白名单）的提醒也要：``reminders`` 1 行 + ``work_items`` 1 行，
    请求人身份落到 ``requester_role=group_member`` / ``requester_is_owner=0``，
    ``target_chat_type=group``、``mention_names`` 按请求者，``max_attempts`` 仍是 3。
    """
    engine, _ = _fresh_engine("guest_group")
    try:
        with flags(ledger=True):
            repo = _FakeRepo(engine)
            tool = CreateReminderTool(repo)          # meta_repo=None：群聊不查白名单
            tool.authoritative_is_owner = lambda: False
            tool.set_context(_ctx(sender_id="p_guest", sender_name="小雨"))
            result = asyncio.run(tool.execute(
                content="提醒我 8 点开会",
                remind_at=(datetime.now() + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M"),
                mention=["我"],
            ))
        assert not result.is_error, result.error

        saved = repo.reminders.added[0]
        assert saved.requester_is_owner == 0
        assert saved.requester_id == "p_guest"
        assert saved.source_chat_type == "group"
        assert json.loads(saved.mention_names) == ["小雨"]

        assert _count(engine, "work_items") == 1
        row = _work_item(engine, legacy_reminder_id=saved.id)
        assert row is not None
        assert row.requester_person_id == "p_guest"
        assert row.requester_role == "group_member"
        assert row.requester_is_owner == 0
        assert row.target_chat_id == "room@chatroom"
        assert row.target_chat_type == "group"
        assert row.owner_only == 0
        assert row.visibility_scope == "group_members"
        assert row.max_attempts == 3
        assert json.loads(row.mention_names_json) == ["小雨"]

        created = _events(engine, "work_item_created")
        assert len(created) == 1
        payload = json.loads(created[0].action_payload_json)
        assert payload["requester"]["person_id"] == "p_guest"
        assert payload["requester"]["role"] == "group_member"
        assert payload["requester"]["is_owner"] is False
        assert payload["target"]["chat_type"] == "group"
    finally:
        engine.dispose()


# --------------------------------------------------------- ③ 双写失败不影响创建
def test_write_failure_does_not_break_tool():
    engine, _ = _fresh_engine("write_failure")

    def boom(*args, **kwargs):
        raise RuntimeError("simulated ledger failure")

    try:
        with flags(ledger=True), _patch(store, "record_created", boom):
            repo, result = _run_tool(engine)
        assert not result.is_error, result.error
        assert "已创建提醒" in result.output
        assert len(repo.reminders.added) == 1
        assert _count(engine, "work_items") == 0
        assert _events(engine, "work_item_created") == []
    finally:
        engine.dispose()


# ------------------------------------------------- ④ 闲聊/即时问答不产生任务行
#: 允许出现的台账写入调用点（**显式白名单**；多一个没登记的写入者 → 下面那条断言红）。
#:
#: * ``agents/tools/reminder.py`` —— 「提醒我 X」的创建投影（P1 原有写入者）；
#: * ``scheduler/main.py`` —— 到点投递的 run 登记 / 收口（P1 原有写入者）；
#: * ``core/tasks/slot_fill.py`` —— **N5 参数收集状态机的合法写入者**（A1-d 回执接线，
#:   2026-10-06）：「等确认的待办必须能跨重启存活」是 R-P8 / A1-d 的硬要求，所以它
#:   必须写 ``work_items`` 行 + ``work_item_runs``。这里**登记**它（原先靠 ``store``
#:   内部的 ``_start_run`` / ``_finish_run`` 别名转调绕过本守卫，等于守卫形同虚设）；
#:   登记完守卫仍是否决式：**未登记**的写入者照旧让它红。
_ALLOWED_LEDGER_WRITERS = frozenset({
    "agents/tools/reminder.py",
    "scheduler/main.py",
    "core/tasks/slot_fill.py",
})

#: 原判据（P1 口径）：直接写 ``work_items`` 行的创建 / 迁移入口。
_LEDGER_CREATE_CALLS = re.compile(
    r"(?<!def )\brecord_(?:created|run_started|run_finished|migrated)\("
)
#: N5 槽位补齐的写入路径（``core/tasks/store.py:525/640/660`` 三个入口）。
#: 必须显式列出：``record_slot_fill(`` 是 ``record_slot_fill_run(`` 的前缀，所以只能
#: 分两条匹配，不能并进上面那条。
_LEDGER_SLOT_FILL_CALLS = re.compile(
    r"(?<!def )\brecord_slot_fill\(|(?<!def )\brecord_slot_fill_run\(|"
    r"(?<!def )\bfinish_slot_fill_run\("
)


def _ledger_write_sites() -> set[str]:
    """扫出**运行期**所有台账写入调用点（静态；迁移脚本是操作者工具，不在内）。"""
    sites = set()
    for base in ("agents", "core", "scheduler"):
        for path in (ROOT / base).rglob("*.py"):
            if "_scratch" in path.parts:
                continue
            body = path.read_text(encoding="utf-8-sig")
            if _LEDGER_CREATE_CALLS.search(body) or _LEDGER_SLOT_FILL_CALLS.search(body):
                sites.add(path.relative_to(ROOT).as_posix())
    return sites


def test_chat_paths_create_no_rows():
    # 静态：写入点**只有登记过的那几个**（见 :data:`_ALLOWED_LEDGER_WRITERS`）。
    # 这不是"任何写入都允许"——多一个未登记的写入者，这条断言就红。
    sites = _ledger_write_sites()
    assert sites == set(_ALLOWED_LEDGER_WRITERS), (
        f"台账写入点与登记表不一致：实况 {sorted(sites)} / 登记 "
        f"{sorted(_ALLOWED_LEDGER_WRITERS)}\n"
        "  多出来的：要么改代码（写入该收口到 core/tasks/store.py 现有入口，别加第二个写点），"
        "要么登记——在 `_ALLOWED_LEDGER_WRITERS` 里加一行，并在它上面那段 `#:` 注释里"
        "写明它为什么必须是写入者（照 slot_fill 那条的写法：哪条硬要求、为什么绕不过）。\n"
        "  变少了的：从表里摘掉——守卫是双向的，登记了却没人写等于留空账。\n"
        "  这张表是**人工白名单**，没有生成器也没有 --write：实况永远由 "
        "`_ledger_write_sites()` 现算。只读台账（repo 读接口）不用登记；"
        "靠 store 内部别名转调绕过本守卫也不行——那正是 slot_fill 当初被登记的原因。"
    )

    # 动态：被拒的创建（时间已过 / 非主人 / 没有地址）一行都不落。
    engine, _ = _fresh_engine("rejected")
    try:
        _flags_off()
        repo = _FakeRepo(engine)
        tool = CreateReminderTool(repo)
        tool.authoritative_is_owner = lambda: True
        tool.set_context(_ctx())
        past = (datetime.now() - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M")
        with flags(ledger=True):
            rejected = asyncio.run(tool.execute(content="开会", remind_at=past))
            act = asyncio.run(tool.execute(
                content="到点查天气",
                remind_at=(datetime.now() + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M"),
                purpose="act",
            ))
        assert rejected.is_error and act.is_error
        assert repo.reminders.added == []
        assert _count(engine, "work_items") == 0
        assert _count(engine, "work_item_runs") == 0
        assert _events(engine) == []
    finally:
        engine.dispose()


# ------------------------------------------------------------------ ⑤ 幂等
def test_replay_same_legacy_reminder_is_idempotent():
    engine, _ = _fresh_engine("idempotent")
    try:
        with flags(ledger=True):
            repo = _FakeRepo(engine)
            reminder = repo.reminders.add(_reminder())
            first = store.record_created(repo, reminder, extra=_ctx().extra)
            second = store.record_created(repo, reminder, extra=_ctx().extra)
        assert first and first == second
        assert _count(engine, "work_items") == 1
        assert len(_events(engine, "work_item_created")) == 1
        assert _count(engine, "work_item_runs") == 0
    finally:
        engine.dispose()


# -------------------------------------------------------------- ⑥ 投递观测
def _prepare_work_item(engine, **reminder_overrides):
    """建一条台账行（legacy_reminder_id=11），供投递观测用例用。"""
    with flags(ledger=True):
        repo = _FakeRepo(engine)
        reminder = _reminder(id=11, **reminder_overrides)
        store.record_created(repo, reminder, extra=_ctx().extra)
    return _work_item(engine, legacy_reminder_id=11)


async def _deliver(scan_repo, result):
    async def fake_turn(**kwargs):
        return result

    with _patch(sm, "get_repo", lambda: scan_repo), \
            _patch(rt, "run_reminder_turn", fake_turn), \
            _patch(sm, "send_notification", lambda *a, **k: None):
        await sm.run_reminder_delivery()


def test_run_sent_marks_done():
    engine, _ = _fresh_engine("run_sent")
    try:
        item = _prepare_work_item(engine)
        scan = _ScanRepo(engine, [_due()])
        with flags(ledger=True, runs=True):
            asyncio.run(_deliver(
                scan, rt.ProactiveTurnResult(True, sent=True, reply="到点啦"),
            ))
        runs = _runs(engine, item.work_item_id)
        assert len(runs) == 1
        assert runs[0].outcome == "sent"
        assert runs[0].run_seq == 1
        assert runs[0].duration_ms >= 0
        assert runs[0].reply_text == "到点啦"
        assert runs[0].trace_ref.startswith("terminal_event:")
        assert runs[0].finished_at is not None
        refreshed = _work_item(engine, legacy_reminder_id=11)
        assert refreshed.status == "done"
        assert refreshed.finished_at is not None
        assert refreshed.occurrences_done == 0

        started = _events(engine, "work_item_run_started")
        finished = _events(engine, "work_item_run_finished")
        assert len(started) == len(finished) == 1
        for event in started:
            payload = json.loads(event.action_payload_json)
            for key in REQUIRED_RUN_STARTED:
                assert key in payload, key
        for event in finished:
            payload = json.loads(event.action_payload_json)
            for key in REQUIRED_RUN_FINISHED:
                assert key in payload, key
            assert payload["outcome"] == "sent"
            assert payload["trace_ref"] == runs[0].trace_ref
        assert finished[0].parent_event_id == started[0].parent_event_id
        assert started[0].external_msg_id in (None, "")
        assert finished[0].external_msg_id in (None, "")
    finally:
        engine.dispose()


def test_run_sent_repeat_keeps_open_and_advances_due():
    engine, _ = _fresh_engine("run_repeat")
    try:
        item = _prepare_work_item(engine, repeat_rule="daily")
        before = _work_item(engine, legacy_reminder_id=11)
        # 扫描行与投影同一份事实：权威侧据此顺延（daily → +1 天）。
        scan = _ScanRepo(engine, [_due(remind_at=before.due_at, repeat_rule="daily")])
        with flags(ledger=True, runs=True):
            asyncio.run(_deliver(scan, rt.ProactiveTurnResult(True, sent=True)))
        after = _work_item(engine, legacy_reminder_id=11)
        assert after.status == "open"
        assert after.due_at == before.due_at + timedelta(days=1)
        assert after.occurrences_done == 1
        assert after.attempts == 0
        assert after.finished_at is None
        assert len(_runs(engine, item.work_item_id)) == 1
    finally:
        engine.dispose()


# ------------------------------------------------- P1-b 重复口径（权威侧回传）
class _AuthorityRepo:
    """投递路径的**真** repo 面：``reminders`` 是 ``ReminderRepository``（真权威）。

    与 ``_ScanRepo`` 的差别是这里没有替身判定：``list_pending`` / ``mark_done``
    都是线上的那一份实现，观测侧必须与它的结论一致。
    """

    def __init__(self, engine):
        from db.repo_reminder import ReminderRepository

        self.meta_engine = engine
        self.meta = SimpleNamespace(engine=engine)
        self.reminders = ReminderRepository(engine)


def _prepare_authority_item(engine, *, rule: str):
    """一条到期的**真** reminders 行 + 它的 ``work_items`` 投影。"""
    repo = _AuthorityRepo(engine)
    reminder = repo.reminders.add(_reminder(
        remind_at=(datetime.now() - timedelta(minutes=1)).replace(microsecond=0),
        repeat_rule=rule,
    ))
    with flags(ledger=True):
        store.record_created(repo, reminder, extra=_ctx().extra)
    return repo, reminder, _work_item(engine, legacy_reminder_id=reminder.id)


def test_unrecognized_repeat_mirrors_authority():
    """P1-b：映射认不出的 repeat_rule，投影必须跟 ``reminders`` 的权威状态一致。

    权威侧 ``_calc_next_repeat`` 是宽松解析，认 ``12小时`` / ``2.5h`` / ``1h1d`` /
    ``-2h``（``0h`` 顺延到原地）；旧写法用 ``repeat_spec_json`` 反推 → 这些行投影落
    ``done``，而 reminders 仍在 pending 继续重复。现在布尔值由投递路径回传。
    """
    cases = (
        ("12小时", True), ("2.5h", True), ("1h1d", True), ("-2h", True),
        ("0h", False),            # 权威侧仍 pending，但周期是 0 → due 不顺延
        ("每周一", False),         # 两边都不认 → 真一次性 → 权威侧置 done
    )
    for index, (rule, advances) in enumerate(cases):
        engine, _ = _fresh_engine(f"p1b_authority_{index}")
        try:
            repo, reminder, item = _prepare_authority_item(engine, rule=rule)
            # 用例前提：这一条在映射侧确实认不出来（无 repeat_spec、原文保留）。
            assert not str(item.repeat_spec_json or "").strip(), rule
            assert json.loads(item.spec_json)["legacy_repeat_rule"] == rule

            with flags(ledger=True, runs=True):
                asyncio.run(_deliver(repo, rt.ProactiveTurnResult(True, sent=True)))

            authority = repo.reminders.list_all(limit=1)[0]
            after = _work_item(engine, legacy_reminder_id=reminder.id)
            repeating = authority.status == "pending"
            assert repeating is (rule != "每周一"), (rule, authority.status)
            # 核心不变式：投影的「结束」与权威的「结束」必须同进同退。
            assert (after.status == "done") is (authority.status == "done"), rule
            if repeating:
                assert after.status == "open", rule
                assert after.due_at == authority.remind_at, rule   # 与权威同一时间
                assert (after.due_at > item.due_at) is advances, rule
                assert after.occurrences_done == 1 and after.attempts == 0
                assert after.finished_at is None
            else:
                assert after.status == "done" and after.finished_at is not None
                assert after.due_at == item.due_at

            finished = _events(engine, "work_item_run_finished")
            payload = json.loads(finished[-1].action_payload_json)
            assert payload["outcome"] == "sent"
            assert payload["repeat_unrecognized"] == (rule if repeating else "")
        finally:
            engine.dispose()


def test_repeat_probe_unavailable_keeps_open():
    """问不到权威结论（替身 repo / 老调用点）→ 保守留 ``open`` + 标原文。

    ``repeat_spec_json`` 是空、``legacy_repeat_rule`` 是 ``12小时``：绝不能因为反推
    不到 repeat 就把投影写成 ``done``（那正是「work_items 已结束、reminders 还在
    重复」）。
    """
    engine, _ = _fresh_engine("p1b_probe_missing")
    try:
        item = _prepare_work_item(engine, repeat_rule="12小时")
        scan = _ScanRepo(engine, [_due(remind_at=item.due_at, repeat_rule="12小时")])
        del scan.reminders._calc_next_repeat              # 探针拿不到权威结论
        with flags(ledger=True, runs=True):
            asyncio.run(_deliver(scan, rt.ProactiveTurnResult(True, sent=True)))
        after = _work_item(engine, legacy_reminder_id=11)
        assert after.status == "open"
        assert after.finished_at is None
        assert after.occurrences_done == 1
        payload = json.loads(
            _events(engine, "work_item_run_finished")[-1].action_payload_json
        )
        assert payload["repeat_unrecognized"] == "12小时"
    finally:
        engine.dispose()


def test_observer_failure_does_not_break_delivery():
    """P2-d：观测器炸了，投递照常走完（创建侧有同款用例，投递侧也必须守住）。

    三个观测调用点各有一层 try/except，探针 ``_calc_next_repeat`` 本身也是 total
    的。这里把 ``store.record_run_started`` / ``record_run_finished`` 一起换成必炸
    的替身：``reminders.mark_done`` 必须照常发生，异常绝不能冒到每分钟的扫描循环。
    """
    engine, _ = _fresh_engine("observer_failure")

    def boom(*args, **kwargs):
        raise RuntimeError("simulated observer failure")

    try:
        _prepare_work_item(engine)
        scan = _ScanRepo(engine, [_due()])
        del scan.reminders._calc_next_repeat          # 探针这一路也断掉
        with flags(ledger=True, runs=True), \
                _patch(store, "record_run_started", boom), \
                _patch(store, "record_run_finished", boom):
            asyncio.run(_deliver(scan, rt.ProactiveTurnResult(True, sent=True)))
        assert scan.done == [11]                      # 投递完成，没有半途而废
        assert scan.attempts == {11: 1}
        assert _count(engine, "work_item_runs") == 0  # 观测侧确实什么都没写成
    finally:
        engine.dispose()


def test_run_failures_stop_at_max_attempts():
    engine, _ = _fresh_engine("run_failed")
    try:
        item = _prepare_work_item(engine)
        scan = _ScanRepo(engine, [_due()])
        with flags(ledger=True, runs=True):
            for _ in range(3):
                asyncio.run(_deliver(
                    scan, rt.ProactiveTurnResult(False, sent=False, reason="send_failed"),
                ))
        runs = _runs(engine, item.work_item_id)
        assert [run.run_seq for run in runs] == [1, 2, 3]
        assert {run.outcome for run in runs} == {"failed"}
        assert scan.failed == [11]
        last = _work_item(engine, legacy_reminder_id=11)
        assert last.status == "failed"
        assert last.attempts == 3
        assert last.max_attempts == 3
        assert last.last_error == "send_failed"
        assert last.last_error_class == "transient"
        assert last.last_error_code == "send_failed"
        assert last.finished_at is not None
        assert len(_events(engine, "work_item_run_finished")) == 3
    finally:
        engine.dispose()


def test_runs_flag_requires_ledger():
    engine, _ = _fresh_engine("runs_without_ledger")
    try:
        item = _prepare_work_item(engine)
        scan = _ScanRepo(engine, [_due()])
        with flags(ledger=False, runs=True):
            asyncio.run(_deliver(scan, rt.ProactiveTurnResult(True, sent=True)))
        assert _count(engine, "work_item_runs") == 0
        assert _events(engine, "work_item_run_started") == []
        assert _work_item(engine, legacy_reminder_id=11).status == "open"
        assert item.status == "open"
        # 投递本身照旧（reminders 侧的状态判定没被观测改动）。
        assert scan.done == [11]
    finally:
        engine.dispose()


# ------------------------------------------------------------- ⑦ 建表 parity
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
            if name.startswith("sqlite_autoindex"):
                shapes.append((unique, cols))
            else:
                named.append(name)
                shapes.append((unique, cols))
        return columns, sorted(named), sorted(shapes)
    finally:
        con.close()


def test_ddl_parity_and_migration_registration():
    from db import migrations

    fresh_engine, fresh_path = _fresh_engine("parity_fresh")
    fresh_engine.dispose()
    legacy_engine, legacy_path = _fresh_engine("parity_v51")
    with legacy_engine.begin() as conn:
        conn.execute(text("DROP TABLE work_item_runs"))
        conn.execute(text("DROP TABLE work_items"))
    legacy_engine.dispose()
    con = sqlite3.connect(str(legacy_path))
    con.execute(
        "CREATE TABLE IF NOT EXISTS _schema_version (version INTEGER PRIMARY KEY, "
        "description TEXT, applied_at DATETIME DEFAULT CURRENT_TIMESTAMP)"
    )
    con.execute("INSERT OR REPLACE INTO _schema_version (version, description) "
                "VALUES (51, 'test baseline')")
    con.commit()
    con.close()

    migrations.run_migrations(str(legacy_path))

    for table in ("work_items", "work_item_runs"):
        assert _table_shape(fresh_path, table) == _table_shape(legacy_path, table), table

    con = sqlite3.connect(str(legacy_path))
    version = con.execute("SELECT MAX(version) FROM _schema_version").fetchone()[0]
    con.close()
    assert version >= 52

    # 账号库：只盖章不建表（守卫靠 account_registry 的存在与否）。
    account_path = _ISO_ROOT / "parity_account.db"
    for suffix in ("", "-wal", "-shm"):
        Path(str(account_path) + suffix).unlink(missing_ok=True)
    con = sqlite3.connect(str(account_path))
    con.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, content TEXT)")
    con.execute("CREATE TABLE terminal_events (id INTEGER PRIMARY KEY, event_id TEXT)")
    con.execute(
        "CREATE TABLE _schema_version (version INTEGER PRIMARY KEY, "
        "description TEXT, applied_at DATETIME DEFAULT CURRENT_TIMESTAMP)"
    )
    con.execute("INSERT INTO _schema_version (version, description) VALUES (51, 'test')")
    con.commit()
    con.close()
    migrations.run_migrations(str(account_path))
    con = sqlite3.connect(str(account_path))
    tables = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    stamped = {row[0] for row in con.execute("SELECT version FROM _schema_version")}
    con.close()
    assert "work_items" not in tables and "work_item_runs" not in tables
    assert 52 in stamped

    # 两个坑的规避：52 不在两个注册元组里（进哪个都会「哪里都不执行」）。
    assert 52 not in migrations._META_MIGRATION_VERSIONS
    assert 52 not in migrations._ACCOUNT_SKIP_MIGRATION_VERSIONS
    assert any(m.version == 52 for m in migrations._MIGRATIONS)


# ------------------------------------------------- ⑧ 映射字段 ⊆ 列 + DEFAULT
def test_mapping_fields_fit_columns():
    from core.tasks.mapping import map_reminder_row

    mapped = map_reminder_row({"id": 1, "content": "x", "purpose": "remind"})
    fields = mapped.as_work_item_fields()
    columns = {column.name for column in WorkItemRow.__table__.columns}
    assert set(fields) <= columns, sorted(set(fields) - columns)

    # 「迁移脚本只传映射字段 + 身份两列」也能插入：除身份列外 NOT NULL 都有 DEFAULT。
    identity = {"work_item_id", "idempotency_key"}
    for column in WorkItemRow.__table__.columns:
        if column.primary_key or column.nullable or column.name in identity:
            continue
        assert column.server_default is not None, column.name

    # runs 表同纪律（工具/观测端只传自己有的字段）。
    for column in WorkItemRunRow.__table__.columns:
        if column.primary_key or column.nullable:
            continue
        if column.name in {"run_id", "work_item_id"}:
            continue
        assert column.server_default is not None, column.name

    # 幂等键 = work_item_id（主人拍板 2026-09-30）：四元组只做非唯一 dedup 索引。
    unique_columns = {
        tuple(sorted(column.name for column in constraint.columns))
        for constraint in WorkItemRow.__table__.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    assert ("work_item_id",) in unique_columns
    assert ("idempotency_key",) in unique_columns
    indexes = {index.name: index for index in WorkItemRow.__table__.indexes}
    assert indexes["ix_work_items_target_dedup"].unique is False
    assert indexes["ix_work_items_status_due"].columns.keys() == ["status", "due_at"]


# --------------------------------------------------- ⑨ 迁移脚本 CLI 契约（P1-a）
def _run_migrate_cli(*args: str, meta_path: Path):
    """真跑一次 ``scripts/migrate_reminders_to_work_items.py``（子进程 + 隔离 env）。"""
    env = dict(os.environ)
    env.update({
        "META_DB_PATH": str(meta_path),
        "PLATFORMS_DIR": str(_ISO_ROOT / "cli_platforms"),
        "DATA_DIR": str(_ISO_ROOT / "cli_data"),
        "LOG_DIR": str(_ISO_ROOT / "cli_logs"),
    })
    return subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "migrate_reminders_to_work_items.py"),
         *args],
        cwd=str(ROOT), env=env, capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=180,
    )


def test_migrate_cli_json_stdout_is_clean():
    """``--apply --json`` 的 stdout 只有 JSON（首次插入路径也一样）。

    structlog 默认打 stdout（本脚本不走 ``init_logging``）：不绑 stderr 的话，
    ``work_item_migrated`` / 映射告警会混进 JSON，``json.loads`` 直接炸。
    """
    engine, path = _fresh_engine("cli_meta")
    try:
        con = sqlite3.connect(str(path))
        con.execute(
            "INSERT INTO reminders (id, content, remind_at, status, repeat_rule, "
            "source_platform, source_account_id, source_chat_id, source_chat_type, "
            "requester_id, requester_name, requester_is_owner, mention_name, "
            "mention_names, purpose, attempts, created_at) "
            "VALUES (1, '该开会了', '2026-10-01 08:00:00', 'pending', '12小时', "
            "'wechat', 'bot_a', 'room@chatroom', 'group', 'p_owner', '老板', 1, "
            "NULL, NULL, 'remind', 0, '2026-09-30 08:00:00')"
        )
        con.commit()
        con.close()

        first = _run_migrate_cli("--apply", "--json", meta_path=path)
        assert first.returncode == 0, first.stderr
        payload = json.loads(first.stdout)          # 混进任何日志行都会在这里红
        assert payload["ledger_ready"] is True
        assert payload["totals"]["inserted"] == 1
        assert payload["totals"]["failed"] == 0
        # 真的走的是插入路径（不是「0 行可迁」的空跑），且日志确实在 stderr。
        assert "work_item_migrated" in first.stderr
        assert "work_item_migrated" not in first.stdout
        con = sqlite3.connect(str(path))
        rows = con.execute(
            "SELECT legacy_reminder_id, spec_json FROM work_items"
        ).fetchall()
        con.close()
        assert len(rows) == 1 and rows[0][0] == 1
        assert json.loads(rows[0][1])["legacy_repeat_rule"] == "12小时"

        # 幂等重跑（duplicate 分支也是 log.info）：stdout 依旧只有 JSON。
        second = _run_migrate_cli("--apply", "--json", meta_path=path)
        assert second.returncode == 0, second.stderr
        again = json.loads(second.stdout)
        assert again["totals"]["inserted"] == 0
        assert again["totals"]["already"] == 1
    finally:
        engine.dispose()


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
