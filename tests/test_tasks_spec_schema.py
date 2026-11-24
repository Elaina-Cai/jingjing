# -*- coding: utf-8 -*-
"""B5：``work_items.spec_json`` 的 kind 级 schema（``docs/archive/任务清单_2026-10-06.md:50``）。

覆盖七组断言：

① **逐 kind 规则**：``answer`` / ``notify`` / ``act`` 各给合法与非法用例，非法用例
   **整串断言错误清单**（``<kind>.<字段>: 期望 X，实得 Y``）；
② **未知字段 default-deny**：顶层与嵌套（``repeat_spec`` / ``legacy`` / ``slot_fill`` /
   ``slots[]``）各来一发；
③ **写入点消费**：``store.record_created`` / ``record_slot_fill`` 对不合规 spec
   **不写**（返回 ``None`` + 行数不变）；``record_migrated`` **抛** ``SpecSchemaError``
   （迁移脚本只认异常，返回 ``None`` 会被算成「既没插也没失败」）；
④ **三个真实写入路径零违规读数**：真工具（``CreateReminderTool`` → ``record_created``）、
   真迁移（``record_migrated``，含 ``repeat_spec`` 与未识别原文）、真状态机
   （``gate_plan_confirmation`` → ``record_slot_fill``）——逐行读回 ``spec_json`` 再校验；
⑤ **与「缺信息先问」联动**：notify 缺 content / 群聊缺 @对象 → 工具走**既有问路**
   （error 文本），台账 0 行；act 缺槽位值 → 状态机**问**（R-P1）且行是 ``waiting_user``，
   而「槽位没齐却写 done」的 spec 被 schema 拒（不许硬写完成）；
⑥ **answer 没有写入点**（``core/tasks/store.py:23`` 自述）：全仓唯一的三个 ``_persist_created``
   调用方都在 notify/act 上，静态钉住「answer 的规则先立着，等生产者出现」；
⑦ **版本常量与枚举只有一份**：``SPEC_SCHEMA_VERSION`` 的唯一声明处是 spec_schema，
   ``STATES`` / ``WORK_ITEM_STATUSES`` / ``RUN_OUTCOMES`` 由 schema 读 owner。

隔离前导照 ``tests/test_work_item_p1.py``：LOG_DIR / META_DB_PATH / PLATFORMS_DIR /
DATA_DIR 全部指向仓库内 scratch 目录；**测试自带 meta 库是 ``create_all`` 出来的临时
文件，绝不碰生产库**。

Run directly:
    python tests/test_tasks_spec_schema.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import traceback
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

ROOT = Path(__file__).resolve().parent.parent
_TEST_LOG_DIR = str(Path("data") / "_test_logs_tasks_spec_schema")
Path(_TEST_LOG_DIR).mkdir(parents=True, exist_ok=True)
os.environ["LOG_DIR"] = _TEST_LOG_DIR
_ISO_ROOT = (ROOT / "data" / "_tasks_spec_schema_test" / "_iso").resolve()
_ISO_ROOT.mkdir(parents=True, exist_ok=True)
os.environ["META_DB_PATH"] = str(_ISO_ROOT / "meta.db")
os.environ["PLATFORMS_DIR"] = str(_ISO_ROOT / "platforms")
os.environ["DATA_DIR"] = str(_ISO_ROOT / "data")

# scheduler 一族的 import 期日志初始化：拦掉，日志只进隔离目录。
import core.infra.logging as _logging  # noqa: E402

_logging.init_logging = lambda *args, **kwargs: None

from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from agents.tools.reminder import CreateReminderTool  # noqa: E402
from core import features  # noqa: E402
from core.tasks import slot_fill, spec_schema, store  # noqa: E402
from core.tasks.mapping import map_reminder_row  # noqa: E402
from db.meta_models import MetaBase, WorkItemRow  # noqa: E402

CHAT_ID = "12345@chatroom"
ACT_CTX = {
    "chat_id": CHAT_ID,
    "session_id": 7,
    "thread_id": "thread-b5",
    "turn_id": "turn-b5",
    "msg_id": "msg-b5",
    "platform": "wechat",
    "account_id": "bot-1",
    "sender_account_id": "acct-1",
    "sender_person_id": "person-1",
    "is_group": True,
}
TOOL_CTX = SimpleNamespace(
    extra={
        "platform": "wechat",
        "account_id": "bot_a",
        "chat_id": CHAT_ID,
        "is_group": True,
        "sender_id": "p_owner",
        "sender_name": "老板",
        "turn_id": "turn-b5-tool",
        "parent_event_id": "evt-parent-b5",
    },
    messages=[{"msg_id": "msg-b5-tool", "turn_id": "turn-b5-tool"}],
    account_context=None,
    accounts={"wechat": "bot_a"},
    owner_accounts={},
)


# ------------------------------------------------------------------- 隔离设施
@contextmanager
def switches(slot_mode: str = "on", *, ledger: bool = True, runs: bool = True):
    """临时改三个开关（``features`` 每次读 env，不用重启）。"""
    keys = {
        "YINYUE_FEATURE_SLOT_FILL": slot_mode,
        "YINYUE_FEATURE_WORK_ITEM_LEDGER": "1" if ledger else "0",
        "YINYUE_FEATURE_WORK_ITEM_RUNS": "1" if runs else "0",
    }
    saved = {key: os.environ.get(key) for key in keys}
    os.environ.update(keys)
    try:
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


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


class _Repo:
    """meta 库替身（``store.resolve_engine`` 认 ``meta_engine`` / ``meta.engine``）。"""

    def __init__(self, engine):
        self.personas = _FakePersonas()
        self.reminders = _FakeReminders()
        self.meta_engine = engine
        self.meta = SimpleNamespace(engine=engine)
        self.engine = engine


def _fresh(name: str):
    path = _ISO_ROOT / f"{name}.db"
    for suffix in ("", "-wal", "-shm"):
        Path(str(path) + suffix).unlink(missing_ok=True)
    engine = create_engine(f"sqlite:///{path}")
    MetaBase.metadata.create_all(engine)
    return engine, _Repo(engine)


def _count(engine, table: str = "work_items") -> int:
    with engine.connect() as conn:
        return int(conn.execute(text(f"select count(*) from {table}")).scalar() or 0)


def _rows(engine) -> list[WorkItemRow]:
    with Session(engine) as session:
        return list(session.query(WorkItemRow).order_by(WorkItemRow.id).all())


def _spec_of(engine, work_item_id: str) -> dict:
    with Session(engine) as session:
        row = session.query(WorkItemRow).filter(
            WorkItemRow.work_item_id == work_item_id
        ).one()
        return json.loads(row.spec_json or "{}")


def _find_by_legacy(engine, legacy_id: int) -> WorkItemRow | None:
    with Session(engine) as session:
        return session.query(WorkItemRow).filter(
            WorkItemRow.legacy_reminder_id == legacy_id
        ).one_or_none()


def _violations(engine) -> dict[str, tuple[str, ...]]:
    """逐行读回 ``spec_json`` 再校验：``{work_item_id: 错误清单}``（只留不合规的）。"""
    found: dict[str, tuple[str, ...]] = {}
    for row in _rows(engine):
        errors = spec_schema.validate_spec(row.kind, json.loads(row.spec_json or "{}"))
        if errors:
            found[row.work_item_id] = errors
    return found


# ------------------------------------------------------------------- 替身输入
def _reminder_row(**overrides):
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
        "source_chat_id": CHAT_ID,
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


def _legacy_dict(**overrides) -> dict:
    """迁移脚本从 sqlite 读出来的那一行（``migrate()`` 收的就是 dict）。"""
    now = datetime.now()
    data = {
        "id": 101,
        "persona_id": 7,
        "content": "该开会了",
        "remind_at": (now + timedelta(hours=1)).replace(microsecond=0),
        "status": "pending",
        "repeat_rule": None,
        "source_platform": "wechat",
        "source_account_id": "bot_a",
        "source_chat_id": CHAT_ID,
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
    return data


def _plan(plan_id: str, *, description: str = "提醒吃药", args: dict | None = None):
    subtask = SimpleNamespace(
        id=1, description=description, tool="create_reminder",
        tool_args=dict(args or {}), status=None,
    )
    return SimpleNamespace(
        plan_id=plan_id, subtasks=[subtask], all_done=False,
        needs_clarification=False, clarification_question="",
    )


def _gate(plan, text: str, repo, ctx: dict | None = None):
    return slot_fill.gate_plan_confirmation(
        plan=plan, incoming_text=text, context=dict(ctx or ACT_CTX), repo=repo,
    )


def _run_tool(repo, **kwargs):
    tool = CreateReminderTool(repo)
    tool.authoritative_is_owner = lambda: True
    tool.set_context(TOOL_CTX)
    remind_at = (datetime.now() + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M")
    return asyncio.run(tool.execute(remind_at=remind_at, **kwargs))


def _notify_spec(**overrides) -> dict:
    """``mapping.map_reminder_row`` 的**真**输出（合法基线），用例只改要测的那一项。"""
    spec = dict(map_reminder_row(_legacy_dict()).spec_json)
    spec.update(overrides)
    return spec


# =========================================================== ① 逐 kind：answer
def test_answer_accepts_minimal_spec():
    """answer 合法：``schema_version`` + 非空 ``content``（§5.2 / §4.3）。"""
    assert spec_schema.validate_spec(
        "answer", {"schema_version": 1, "content": "好，我在"}
    ) == ()
    # 版本键缺省也不行（三个 kind 同一个口径：每个 JSON 带 schema_version）。
    assert spec_schema.validate_spec("answer", {"content": "好，我在"}) == (
        "answer.schema_version: 期望 必填，实得 缺失",
    )


def test_answer_rejects_missing_content_and_unknown_key():
    """answer 非法：缺必填 + 空内容 + 未知字段，错误清单整串可断言。"""
    assert spec_schema.validate_spec("answer", {}) == (
        "answer.schema_version: 期望 必填，实得 缺失",
        "answer.content: 期望 必填，实得 缺失",
    )
    assert spec_schema.validate_spec(
        "answer", {"schema_version": 1, "content": "   ", "steps": []}
    ) == (
        "answer.steps: 期望 允许集 schema_version/content 之内，实得 未知字段，值 [](list)",
        "answer.content: 期望 str(非空)，实得 '   '(str)",
    )


# =========================================================== ② 逐 kind：notify
def test_notify_accepts_mapping_shape():
    """notify 合法：映射真输出的三种形态（一次性 / 识别 / 未识别原文）+ 迁移的 source_db。"""
    one_shot = map_reminder_row(_legacy_dict())
    daily = map_reminder_row(_legacy_dict(repeat_rule="daily"))
    unknown = map_reminder_row(_legacy_dict(repeat_rule="每月最后一天"))
    for mapped in (one_shot, daily, unknown):
        assert spec_schema.validate_spec(mapped.kind, mapped.spec_json) == (), mapped.spec_json
    # 迁移路径会补 ``legacy.source_db``（record_migrated），同样合法。
    migrated = json.loads(json.dumps(daily.spec_json))
    migrated["legacy"]["source_db"] = "acct_a"
    assert spec_schema.validate_spec("notify", migrated) == ()
    # 认不出的 repeat_rule → 原文**永远**留一份（B2 的硬不变式）。
    assert unknown.spec_json["legacy_repeat_rule"] == "每月最后一天"
    assert unknown.spec_json["repeat_spec"] is None


def test_notify_rejects_violations_with_exact_errors():
    """notify 非法：必填 / 类型 / 枚举 / 区间 / 嵌套未知字段，逐条给具体错误。"""
    cases = (
        (
            {k: v for k, v in _notify_spec().items() if k != "content"},
            ("notify.content: 期望 必填，实得 缺失",),
        ),
        (
            _notify_spec(content=""),
            ("notify.content: 期望 str(非空)，实得 ''(str)",),
        ),
        (
            _notify_spec(content=123),
            ("notify.content: 期望 str(非空)，实得 123(int)",),
        ),
        (
            _notify_spec(schema_version=2),
            ("notify.schema_version: 期望 1(int)，实得 2(int)",),
        ),
        (
            {k: v for k, v in _notify_spec().items() if k != "legacy_repeat_rule"},
            ("notify.legacy_repeat_rule: 期望 必填，实得 缺失",),
        ),
        (
            _notify_spec(repeat_spec={"every": 1, "unit": "day", "anchor": "due",
                                      "by_weekday": [1]}),
            ("notify.repeat_spec.by_weekday: 期望 允许集 every/unit/anchor 之内，"
             "实得 未知字段，值 [1](list)",),
        ),
        (
            _notify_spec(repeat_spec={"every": 1, "unit": "fortnight", "anchor": "due"}),
            ("notify.repeat_spec.unit: 期望 ∈ hour/day/week/month，实得 'fortnight'(str)",),
        ),
        (
            _notify_spec(repeat_spec={"every": 0, "unit": "day", "anchor": "due"}),
            ("notify.repeat_spec.every: 期望 int(≥1)，实得 0(int)",),
        ),
        (
            _notify_spec(repeat_spec={"every": 1, "unit": "day", "anchor": "completed"}),
            ("notify.repeat_spec.anchor: 期望 ∈ due，实得 'completed'(str)",),
        ),
        (
            _notify_spec(repeat_spec={"every": 1, "unit": "day"}),
            ("notify.repeat_spec.anchor: 期望 必填，实得 缺失",),
        ),
        (
            _notify_spec(repeat_spec="daily"),
            ("notify.repeat_spec: 期望 None 或 Mapping(every/unit/anchor)，"
             "实得 'daily'(str)",),
        ),
        (
            _notify_spec(legacy=[1]),
            ("notify.legacy: 期望 Mapping，实得 [1](list)",),
        ),
        (
            _notify_spec(legacy={"reminder_id": 1, "persona_id": 7,
                                 "requester_name": "老板"}),
            ("notify.legacy.anchors_missing: 期望 必填，实得 缺失",),
        ),
        (
            _notify_spec(legacy={"reminder_id": "1", "persona_id": None,
                                 "requester_name": "老板", "anchors_missing": True}),
            ("notify.legacy.reminder_id: 期望 int(≥1) 或 None，实得 '1'(str)",),
        ),
        (
            _notify_spec(legacy={"reminder_id": 1, "persona_id": 7,
                                 "requester_name": "老板", "anchors_missing": "true"}),
            ("notify.legacy.anchors_missing: 期望 bool，实得 'true'(str)",),
        ),
    )
    for spec, expected in cases:
        got = spec_schema.validate_spec("notify", spec)
        assert got == expected, f"{spec}\n期望 {expected}\n实得 {got}"


# ============================================================== ③ 逐 kind：act
def test_act_accepts_real_slot_fill():
    """act 合法：状态机每个状态的 ``to_dict()``（真对象）+ 计划步骤的位置。"""
    engine, repo = _fresh("act_ok")
    try:
        with switches("on"):
            fill = slot_fill.new_fill(
                slot_fill.SlotFillInput(
                    chat_id=CHAT_ID, action_id="plan-b5-ok", goal="提醒吃药",
                    open_slots=slot_fill.slots_for_tools(["create_reminder"]),
                )
            )
            assert spec_schema.validate_spec(
                "act", store.slot_fill_spec(fill.to_dict())
            ) == ()
            for state, status in (("VALIDATING", "waiting_user"), ("CONFIRMING", "waiting_user"),
                                  ("EXECUTING", "running")):
                fill.state = state
                spec = store.slot_fill_spec(fill.to_dict())
                assert spec["slot_fill"]["work_item_status"] == status, state
                assert spec_schema.validate_spec("act", spec) == (), (state, spec)
            # DONE（收口）与 ABORTED（失败回执）都合法，且各自带对的 work_item_status。
            fill.state = "DONE"
            spec = store.slot_fill_spec(fill.to_dict())
            assert spec["slot_fill"]["work_item_status"] == "done"
            assert spec_schema.validate_spec("act", spec) == ()
            fill.state = "ABORTED"
            fill.receipt = {"outcome": "failed", "ok": False}
            spec = store.slot_fill_spec(fill.to_dict())
            assert spec["slot_fill"]["work_item_status"] == "failed"
            assert spec_schema.validate_spec("act", spec) == ()
            # 计划步骤的位置（docs/任务模型.md:176）：结构合法即可，字段不发明。
            steps = {"goal": "提醒吃药", "slot_fill": fill.to_dict(),
                     "steps": [{"id": 1, "tool": "create_reminder"}]}
            assert spec_schema.validate_spec("act", steps) == ()
    finally:
        engine.dispose()


def test_act_rejects_violations_with_exact_errors():
    """act 非法：缺 slot_fill / 槽位表残缺 / 状态枚举 / 硬写完成 / 上限。"""
    base = slot_fill.new_fill(
        slot_fill.SlotFillInput(
            chat_id=CHAT_ID, action_id="plan-b5-bad", goal="提醒吃药",
            open_slots=slot_fill.slots_for_tools(["create_reminder"]),
        )
    ).to_dict()

    def spec_with(**slot_fill_overrides) -> dict:
        payload = dict(base)
        payload.update(slot_fill_overrides)
        return store.slot_fill_spec(payload)

    cases = (
        ({}, ("act.goal: 期望 必填，实得 缺失", "act.slot_fill: 期望 必填，实得 缺失")),
        ({"goal": "提醒吃药"}, ("act.slot_fill: 期望 必填，实得 缺失",)),
        ({"goal": "提醒吃药", "slot_fill": [1]},
         ("act.slot_fill: 期望 Mapping，实得 [1](list)",)),
        (
            {"goal": "x", "slot_fill": {}},
            ("act.slot_fill.fill_id: 期望 必填，实得 缺失",
             "act.slot_fill.state: 期望 必填，实得 缺失",
             "act.slot_fill.slots: 期望 必填，实得 缺失",
             "act.slot_fill.work_item_status: 期望 必填，实得 缺失"),
        ),
        (
            {"goal": "x", "slot_fill": dict(base, fill_id="   ")},
            ("act.slot_fill.fill_id: 期望 str(非空)，实得 '   '(str)",),
        ),
        (
            {"goal": "x", "slot_fill": dict(base, state="PAUSED")},
            ("act.slot_fill.state: 期望 ∈ GATHERING/VALIDATING/CONFIRMING/EXECUTING/"
             "DONE/ABORTED，实得 'PAUSED'(str)",),
        ),
        (
            {"goal": "x", "slot_fill": dict(base, work_item_status="done")},
            ("act.slot_fill.work_item_status: 期望 state=DONE 才允许 done"
             "（当前 state=GATHERING），实得 'done'(str)",),
        ),
        (
            {"goal": "x", "slot_fill": dict(base, state="DONE")},
            ("act.slot_fill.work_item_status: 期望 state=DONE → done，"
             "实得 'waiting_user'(str)",),
        ),
        (
            {"goal": "x", "slot_fill": dict(base, nudges=-1)},
            ("act.slot_fill.nudges: 期望 int(≥0)，实得 -1(int)",),
        ),
        (
            {"goal": "x", "slot_fill": dict(base, slots=[{"expected_type": "时间"}])},
            ("act.slot_fill.slots[0].name: 期望 必填，实得 缺失",),
        ),
        (
            {"goal": "x", "slot_fill": dict(base, slots=[
                {"name": "时间", "expected_type": "时间", "ttl_seconds": 0},
            ])},
            ("act.slot_fill.slots[0].ttl_seconds: 期望 int(≥1)，实得 0(int)",),
        ),
        (
            {"goal": "x", "slot_fill": dict(base, slots=[
                {"name": "时间", "expected_type": "时间"},
                {"name": "时间", "expected_type": "时间"},
            ])},
            ("act.slot_fill.slots: 期望 槽位名不重复（slots_for_tools 按名去重），"
             "实得 重复 时间",),
        ),
        (
            {"goal": "x", "slot_fill": dict(base, transitions=[
                {"from": "GATHERING", "to": "PAUSED", "reason": "x", "at": "2026-10-07 10:00:00"},
            ])},
            ("act.slot_fill.transitions[0].to: 期望 ∈ GATHERING/VALIDATING/CONFIRMING/"
             "EXECUTING/DONE/ABORTED，实得 'PAUSED'(str)",),
        ),
        (
            {"goal": "x", "slot_fill": dict(base, receipt={"outcome": "maybe", "ok": "yes"})},
            ("act.slot_fill.receipt.outcome: 期望 ∈ sent/silent/failed/blocked，"
             "实得 'maybe'(str)",
             "act.slot_fill.receipt.ok: 期望 bool，实得 'yes'(str)"),
        ),
        (
            {"goal": "x", "slot_fill": dict(base, spec_kind="notify")},
            ("act.slot_fill.spec_kind: 期望 与行 kind 一致（act），实得 'notify'(str)",),
        ),
        (
            {"goal": "x", "slot_fill": dict(base, archive=[1])},
            ("act.slot_fill.archive: 期望 Mapping，实得 [1](list)",),
        ),
        (
            {"goal": "x", "slot_fill": dict(base, notes=["不是 dict"])},
            ("act.slot_fill.notes[0]: 期望 Mapping，实得 '不是 dict'(str)",),
        ),
        (
            {"goal": "x", "slot_fill": base, "steps": [1]},
            ("act.steps[0]: 期望 Mapping，实得 1(int)",),
        ),
    )
    for spec, expected in cases:
        got = spec_schema.validate_spec("act", spec)
        assert got == expected, f"{spec}\n期望 {expected}\n实得 {got}"

    # asked_batch 的硬上限（R-P1 一次最多问 3 项）另测：错误里的实得是列表，整串太长。
    over = spec_with(asked_batch=["a", "b", "c", "d"])
    got = spec_schema.validate_spec("act", over)
    assert len(got) == 1 and "asked_batch" in got[0] and "≤3 项" in got[0], got
    assert spec_schema.validate_spec("act", spec_with(asked_batch=["a", "b", "c"])) == ()


# ================================================== ④ 未知字段 default-deny
def test_unknown_field_is_default_denied_at_every_level():
    """未知字段：顶层 + ``repeat_spec`` / ``legacy`` / ``slot_fill`` / ``slots[]`` 四层。"""
    base = _notify_spec()
    got = spec_schema.validate_spec("notify", dict(base, note="随手记"))
    assert got == (
        "notify.note: 期望 允许集 schema_version/content/repeat_spec/legacy_repeat_rule/"
        "legacy 之内，实得 未知字段，值 '随手记'(str)",
    ), got
    assert got[0].count("期望") == 1 and "实得" in got[0] and "notify.note" in got[0]

    nested = dict(
        base,
        repeat_spec={"every": 1, "unit": "day", "anchor": "due", "by_weekday": [1]},
        legacy={"reminder_id": 1, "persona_id": 7, "requester_name": "老板",
                "anchors_missing": True, "why": "手写的"},
    )
    got = spec_schema.validate_spec("notify", nested)
    assert got == (
        "notify.repeat_spec.by_weekday: 期望 允许集 every/unit/anchor 之内，"
        "实得 未知字段，值 [1](list)",
        "notify.legacy.why: 期望 允许集 reminder_id/persona_id/requester_name/"
        "anchors_missing/source_db 之内，实得 未知字段，值 '手写的'(str)",
    ), got

    fill = slot_fill.new_fill(
        slot_fill.SlotFillInput(
            chat_id=CHAT_ID, action_id="plan-b5-unknown", goal="提醒吃药",
            open_slots=slot_fill.slots_for_tools(["create_reminder"]),
        )
    ).to_dict()
    got = spec_schema.validate_spec("act", store.slot_fill_spec(dict(fill, ttl="1h")))
    assert len(got) == 1 and got[0].startswith("act.slot_fill.ttl:") and "未知字段" in got[0], got
    bad_slot = dict(fill, slots=[dict(fill["slots"][0], widget="x")])
    got = spec_schema.validate_spec("act", store.slot_fill_spec(bad_slot))
    assert len(got) == 1 and "act.slot_fill.slots[0].widget" in got[0], got

    # 未知 kind 也只报一条，不做二次推断。
    assert spec_schema.validate_spec("remind", {}) == (
        "kind: 期望 answer/notify/act 之一，实得 'remind'(str)",
    )


# ============================================ ⑤ 写入点消费（不合规不写）
def test_record_created_refuses_invalid_spec():
    """``record_created``：同一行改一个字段，合规写、不合规不写（+ 明确错误）。"""
    engine, repo = _fresh("created_gate")
    try:
        with switches("on"):
            first = store.record_created(repo, _reminder_row(id=1), extra=TOOL_CTX.extra)
            assert first and _count(engine) == 1
            assert spec_schema.validate_spec(
                "notify", _spec_of(engine, first)
            ) == ()

            # 空 content（绕过工具直连写入点）：不合规 ⇒ 不写。
            blank = store.record_created(
                repo, _reminder_row(id=2, content=""), extra=TOOL_CTX.extra
            )
            assert blank is None, "空 content 的 notify spec 不该落库"
            assert _count(engine) == 1, _rows(engine)
            assert _find_by_legacy(engine, 2) is None
    finally:
        engine.dispose()


def test_record_slot_fill_refuses_invalid_spec():
    """``record_slot_fill``：坏 payload（缺身份 / 坏状态）不写；真 fill 照写。"""
    engine, repo = _fresh("act_gate")
    try:
        with switches("on"):
            ok = slot_fill.new_fill(
                slot_fill.SlotFillInput(
                    chat_id=CHAT_ID, action_id="plan-b5-write", goal="提醒吃药",
                    open_slots=slot_fill.slots_for_tools(["create_reminder"]),
                )
            )
            written = store.record_slot_fill(repo, ok, anchor={"chat_id": CHAT_ID})
            assert written == store.slot_fill_work_item_id(ok.fill_id)
            assert _count(engine) == 1
            assert spec_schema.validate_spec("act", _spec_of(engine, written)) == ()

            bad = store.record_slot_fill(
                repo, {"fill_id": "", "state": "PAUSED"}, anchor={"chat_id": CHAT_ID}
            )
            assert bad is None, "不合规的 act spec 不该落库"
            assert _count(engine) == 1, _rows(engine)
    finally:
        engine.dispose()


def test_record_migrated_raises_on_invalid_spec():
    """``record_migrated``：不合规 ⇒ **抛**（脚本按 failed 记账），且不留半行。"""
    engine, _ = _fresh("migrate_gate")
    try:
        written = store.record_migrated(
            engine, _legacy_dict(id=201, repeat_rule="12小时"), source_db="meta"
        )
        assert written and _count(engine) == 1
        spec = _spec_of(engine, written)
        assert spec["legacy_repeat_rule"] == "12小时" and spec["legacy"]["source_db"] == "meta"
        assert spec_schema.validate_spec("notify", spec) == ()

        try:
            store.record_migrated(
                engine, _legacy_dict(id=202, content=""), source_db="meta"
            )
        except spec_schema.SpecSchemaError as exc:
            assert exc.kind == "notify", exc.kind
            assert exc.errors and "notify.content" in exc.errors[0], exc.errors
        else:
            raise AssertionError("空 content 的存量行必须让迁移大声失败")
        assert _count(engine) == 1, "抛错之后不许留下半行"
    finally:
        engine.dispose()


# ============================== ⑥ 三个真实写入路径零违规（隔离库读数）
def test_three_real_write_paths_report_zero_violations():
    """真工具 / 真迁移 / 真状态机三条写入路径，逐行读回 ``spec_json`` 校验。"""
    engine, repo = _fresh("three_paths")
    try:
        readings: list[tuple[str, str, str, tuple[str, ...]]] = []
        with switches("on"):
            # ① notify：CreateReminderTool → work_item_store.record_created
            result = _run_tool(repo, content="开会", mention=["我"])
            assert not result.is_error, result.error
            tool_row = _find_by_legacy(engine, 1)
            assert tool_row is not None
            # ② 迁移：scripts/migrate_reminders_to_work_items.py → record_migrated
            m1 = store.record_migrated(
                engine, _legacy_dict(id=301, repeat_rule="2h"), source_db="meta"
            )
            m2 = store.record_migrated(
                engine, _legacy_dict(id=302, repeat_rule="每月最后一天"), source_db="acct_a"
            )
            # ③ act：gate_plan_confirmation → record_slot_fill（含跨轮 upsert）
            ask = _gate(_plan("plan-b5-read"), "帮我定个提醒", repo)
            assert ask, "缺槽位就该问"
            _gate(_plan("plan-b5-read"), "明天早上8点，提醒我吃药", repo)
            fill = slot_fill.load(
                repo, slot_fill.fill_id_for(CHAT_ID, "plan-b5-read")
            )
            assert fill is not None and fill.state == "CONFIRMING", fill

            for path, work_item_id in (
                ("notify/record_created", tool_row.work_item_id),
                ("notify/record_migrated", m1),
                ("notify/record_migrated(未识别原文)", m2),
                ("act/record_slot_fill", store.slot_fill_work_item_id(fill.fill_id)),
            ):
                spec = _spec_of(engine, work_item_id)
                kind = "act" if path.startswith("act") else "notify"
                errors = spec_schema.validate_spec(kind, spec)
                readings.append((path, work_item_id, kind, errors))

        assert _count(engine) == 4, _rows(engine)
        for path, work_item_id, kind, errors in readings:
            print(
                f"  [reading] {path}: work_item={work_item_id[:14]} kind={kind} "
                f"violations={len(errors)}"
            )
        assert all(not errors for _, _, _, errors in readings), readings
        # 迁移那条未识别的原文确实留住了（schema 也认它）。
        assert _spec_of(engine, m2)["legacy_repeat_rule"] == "每月最后一天"
        assert _violations(engine) == {}
    finally:
        engine.dispose()


# ================================ ⑦ 与「缺信息先问」联动（不硬写）
def test_missing_info_asks_instead_of_writing_notify():
    """notify 缺必填：工具走**既有问路**（error 文本），台账一行都不落。"""
    engine, repo = _fresh("ask_notify")
    try:
        with switches("on"):
            blank = _run_tool(repo, content="")
            assert blank.is_error and "不能为空" in blank.error, blank
            no_mention = _run_tool(repo, content="开会")
            assert no_mention.is_error and "先问" in no_mention.error, no_mention
            assert repo.reminders.added == [], "问路阶段不许建 reminder"
            assert _count(engine) == 0, _rows(engine)

        # 同一处缺口正是 schema 拒的那一条（问路与写入门同源）。
        assert spec_schema.validate_spec("notify", _notify_spec(content="")) == (
            "notify.content: 期望 str(非空)，实得 ''(str)",
        )
        assert spec_schema.validate_spec("notify", _notify_spec(content="开会")) == ()
    finally:
        engine.dispose()


def test_missing_info_asks_instead_of_writing_act():
    """act 缺槽位：状态机**问**（R-P1）+ 行是 ``waiting_user``；「没齐却写 done」被拒。"""
    engine, repo = _fresh("ask_act")
    try:
        with switches("on"):
            ask = _gate(_plan("plan-b5-ask"), "帮我定个提醒", repo)
            assert ask and "还差" in ask and "我需要" in ask, ask
            fill = slot_fill.load(repo, slot_fill.fill_id_for(CHAT_ID, "plan-b5-ask"))
            assert fill is not None and fill.state == slot_fill.GATHERING
            row = _rows(engine)[0]
            spec = json.loads(row.spec_json)
            assert spec["slot_fill"]["work_item_status"] == "waiting_user", spec
            assert row.status == "waiting_user"
            assert spec_schema.validate_spec("act", spec) == ()
            assert [s["valid"] for s in spec["slot_fill"]["slots"]] == [False, False]

            # 缺信息却把这条点成完成 = schema 拒（§4.3：done 由发送结果决定）。
            assert spec_schema.validate_spec(
                "act", store.slot_fill_spec(dict(fill.to_dict(), work_item_status="done"))
            ) == (
                "act.slot_fill.work_item_status: 期望 state=DONE 才允许 done"
                "（当前 state=GATHERING），实得 'done'(str)",
            )
            # 补上值 → 推进到 CONFIRMING（仍然不是 done），台账同一行 upsert。
            _gate(_plan("plan-b5-ask"), "明天早上8点，提醒我吃药", repo)
            after = json.loads(_rows(engine)[0].spec_json)
            assert after["slot_fill"]["state"] == "CONFIRMING"
            assert after["slot_fill"]["work_item_status"] == "waiting_user"
            assert _count(engine) == 1
    finally:
        engine.dispose()


# ================================== ⑧ 常量与枚举只有一份 / answer 无生产者
def test_schema_constants_have_a_single_owner():
    """``SPEC_SCHEMA_VERSION`` 唯一声明处 = spec_schema；枚举读 owner，不复制。"""
    from core.tasks import mapping

    assert mapping.SPEC_SCHEMA_VERSION == spec_schema.SPEC_SCHEMA_VERSION == 1
    assert mapping.REPEAT_ANCHOR_DUE == spec_schema.REPEAT_ANCHOR_DUE
    assert set(spec_schema._owner_states()) == set(slot_fill.STATES)
    assert set(spec_schema._owner_statuses()) == set(store.WORK_ITEM_STATUSES)
    assert set(spec_schema._owner_outcomes()) == set(store.RUN_OUTCOMES)
    # 单位常量就是映射别名表实际用的那几个（漂移会当场炸：写入点会拒）。
    units = {unit for _, unit in mapping._REPEAT_ALIASES.values()}
    assert units <= set(spec_schema.REPEAT_UNITS), units


def test_answer_kind_has_no_writer_yet():
    """``answer`` 的规则先立着：写入点只有 notify / act 两条（静态钉住）。"""
    source = (ROOT / "core" / "tasks" / "store.py").read_text(encoding="utf-8")
    kind_lines = [
        line.strip() for line in source.splitlines()
        if '"kind"' in line and "answer" in line
    ]
    assert kind_lines == [], f"store 里出现了 answer 生产者，B5 得跟上：{kind_lines}"
    assert '_persist_created' in source
    assert source.count("_persist_created(") >= 3, "三个写入点都该经 _persist_created"
    # 但规则本身是可用的（生产者出现即生效）。
    assert spec_schema.validate_spec("answer", {"schema_version": 1, "content": "好"}) == ()


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
