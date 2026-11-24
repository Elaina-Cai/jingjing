"""A1-d 执行回执接线（``EXECUTING→DONE`` + 计划层不变式）的守卫用例。

被验收的东西（`docs/archive/任务清单_2026-10-06.md:43` A1-d 原始定义；`docs/decisions/架构评估.md:96` / §5 X8 第③条
前置；`docs/decisions/架构评估.md:233` R-C5）

* **回执接线**：``core/tasks/slot_fill.py`` 的 ``SlotFillMachine.complete`` 接到**真实
  执行结果**——成功 → ``EXECUTING→DONE`` + ``work_item_runs`` 有 ``outcome`` / 耗时 /
  外部消息 id，``work_items.status=done``；失败 → 失败态 + 原因（**绝不写完成**）；
* **计划层不变式**：「要替主人做事」的步骤（``PROMISE_TOOLS``）**只有拿到执行回执之后
  才允许被当作已完成**；没有回执 → 该计划不得视为完成，只能走 R-C5 的上报
  （``over_budget`` / ``asked_user`` 路径）。
* **不做的事**（主人已明确拒绝）：**不在出站/生成出口用关键词黑名单拦"我记着了"这类
  话术**——那是 prompt 级补丁。判据是回执这个**事实**，不是话术（见 ⑦ 的结构锁）。

用例（与验收单逐条对应）
------------------------
① ``test_success_closes_executing_to_done`` —— 成功：``EXECUTING→DONE``，run 有
   outcome / 耗时 / 外部 id，``work_items.status=done``；
② ``test_failed_execution_never_writes_done`` —— 失败：失败态 + 原因，**绝不可** DONE；
③ ``test_live_fixture_no_receipt_is_not_completion`` —— **live fixture**
   ``@鲸鲸 12点叫我去吃饭``：有回执 ⇔ 可当完成；读不到回执行 → 走上报，断言不存在
   「计划完成但无回执被当成完成」这一格；
④ ``test_over_budget_goes_to_report`` —— 超预算 → ``over_budget`` / ``asked_user`` 走到，
   上报那一句真的发出去（不静默）；
⑤ ``test_receipt_is_idempotent`` —— 同一回执重复来只记一次、状态不回退；
⑥ ``test_off_is_byte_identical`` —— ``off``（默认）→ 回执入口**零调用**、零新增行，
   出口与接线前一致；含"本功能没有新模块"这一格的自证；
⑦ ``test_no_outbound_promise_filter`` —— 结构锁：判定入口**没有**文本参数（不看话术）、
   三个被改的文件里**没有**任何承诺词黑名单 / 出站改写调用。

测试卫生
--------
* **"关"一律显式 ``off``**（不靠"env 未设"——那是 ``off`` 与 ``shadow`` 的分界）；
  import 期把本文件依赖的每个开关都钉死：``EXEC_RECEIPT`` / ``SLOT_FILL`` ×
  ``WORK_ITEM_LEDGER`` / ``WORK_ITEM_RUNS``（后两个是 ``tests/test_env_flag_isolation.py``
  的兄弟组，**要钉一起钉**）+ ``PLAN_LOOP``（``_verify_step`` 的口径不许被环境改写）；
* **不写生产库**：自带 ``create_all`` 出来的临时 meta 库；``LOG_DIR`` / ``META_DB_PATH`` /
  ``PLATFORMS_DIR`` / ``DATA_DIR`` 全指向 ``data/`` 下的隔离目录。

Run directly::

    python tests/test_exec_receipt.py
"""

from __future__ import annotations

import ast
import asyncio
import os
import sys
import traceback
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

ROOT = Path(__file__).resolve().parent.parent
_TEST_LOG_DIR = str(Path("data") / "_test_logs_exec_receipt")
Path(_TEST_LOG_DIR).mkdir(parents=True, exist_ok=True)
os.environ["LOG_DIR"] = _TEST_LOG_DIR
_ISO_ROOT = (ROOT / "data" / "_exec_receipt_test" / "_iso").resolve()
_ISO_ROOT.mkdir(parents=True, exist_ok=True)
os.environ["META_DB_PATH"] = str(_ISO_ROOT / "meta.db")
os.environ["PLATFORMS_DIR"] = str(_ISO_ROOT / "platforms")
os.environ["DATA_DIR"] = str(_ISO_ROOT / "data")

# 日志初始化会去碰 LOG_DIR：这里只留隔离目录，初始化本身拦掉。
import core.infra.logging as _logging  # noqa: E402

_logging.init_logging = lambda *args, **kwargs: None

# ---- 开关钉死（import 期，显式；**不许**靠"env 未设"）----------------------
#: 本文件依赖的开关（同进程里别的测试文件在 **import 期**也会改 env —— 例如
#: ``tests/test_threads_focus.py:72`` 把 ``LEDGER × RUNS`` 钉成 ``off``，pytest 先
#: 收集全部模块再跑用例，所以只钉一次不够：每个用例开头由 :func:`_pin` 重钉一遍。
_PINS = {
    "YINYUE_FEATURE_EXEC_RECEIPT": "off",              # 本文件的"关"位：显式 off
    "YINYUE_FEATURE_SLOT_FILL": "off",
    "YINYUE_FEATURE_WORK_ITEM_LEDGER": "1",            # 落库用例依赖（兄弟组一起钉）
    "YINYUE_FEATURE_WORK_ITEM_RUNS": "1",
    "YINYUE_FEATURE_PLAN_LOOP": "off",                 # _verify_step 的旧口径
}
os.environ.update(_PINS)


def _pin() -> None:
    """每个用例开始时重钉本文件依赖的开关（理由见 :data:`_PINS`）。"""
    os.environ.update(_PINS)

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from agents.base import AgentContext  # noqa: E402
from agents.conversational import (  # noqa: E402
    _DESTRUCTIVE_TOOLS,
    ConversationalAgent,
)
from agents.planner import Plan, SubTask, TaskStatus  # noqa: E402
from agents.tools.base import ToolResult  # noqa: E402
from core import features  # noqa: E402
from core.tasks import slot_fill, store  # noqa: E402
from db.meta_models import (  # noqa: E402
    MetaBase,
    TerminalEvent,
    WorkItemRow,
    WorkItemRunRow,
)

CHAT_ID = "10000000002@chatroom"
PLAN_ID = "plan-live-1"
#: 今晚的真实形态（验收单 ③ 的原话）。
LIVE_MESSAGE = "@鲸鲸 12点叫我去吃饭"
#: 计划里那一步的说明（真实 planner 产出的形状：一个承诺型工具步 + 一步回话）。
LIVE_STEP = "12点提醒主人去吃饭"
REPLY_TEXT = "好，12点我叫你。"
CTX = {
    "chat_id": CHAT_ID,
    "session_id": 7,
    "thread_id": "thread-exec-receipt",
    "turn_id": "turn-1",
    "msg_id": "msg-1",
    "platform": "wechat",
    "account_id": "bot-1",
    "sender_account_id": "acct-1",
    "sender_person_id": "person-1",
    "is_group": True,
}


# ------------------------------------------------------------------- 隔离设施
@contextmanager
def switches(*, slot_fill_mode: str = "on", receipt: str = "on",
             ledger: bool = True, runs: bool = True):
    """临时改四个开关（``features`` 每次读 env，不用重启）。"""
    keys = {
        "YINYUE_FEATURE_SLOT_FILL": slot_fill_mode,
        "YINYUE_FEATURE_EXEC_RECEIPT": receipt,
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


class _Repo:
    """meta 库替身（``store.resolve_engine`` 认 ``meta_engine`` / ``meta.engine``）。"""

    def __init__(self, engine):
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


class _StubTool:
    """计划里那个承诺型工具（``ToolResult`` 的形状与真工具一致）。"""

    def __init__(self, output: str = "", data=None, error: str = ""):
        self.name = "create_reminder"
        self.result = ToolResult(output, data=data, error=error)
        self.calls: list[dict] = []

    async def execute(self, **kwargs):
        self.calls.append(dict(kwargs))
        return self.result


class _StubEvolve:
    def record_plan_outcome(self, plan, quality):
        return None

    def save_state(self):
        return None


class _StubPlanner:
    """``_react_with_planner`` 需要的那几个方法（执行原语按真口径设 status/data）。"""

    def __init__(self, plan, tools=None):
        self.plan = plan
        self.tools = dict(tools or {})
        self.context = None
        self.replans = 0

    def set_context(self, context):
        self.context = context

    async def understand_and_plan(self, user_message, context_summary="", session_id=None):
        return self.plan

    async def execute_task_with_retry(self, task, tools, on_progress=None):
        tool = tools.get(task.tool)
        if tool is None:
            task.result = f"工具 {task.tool} 不存在"
            task.status = TaskStatus.FAILED
            return task, task.result
        result = await tool.execute(**dict(task.tool_args or {}))
        if result.is_error:
            task.result = f"[错误] {result.error}"
            task.status = TaskStatus.FAILED
            return task, task.result
        task.result = result.output
        task.data = result.data
        task.status = TaskStatus.DONE
        return task, f"[{task.tool}] {result.output[:80]}"

    async def assess_quality(self, plan, user_message):
        return {
            "completeness": 1.0, "accuracy": 1.0,
            "should_iterate": False, "suggestion": "",
        }

    async def replan_after_observation(self, plan, observation):
        self.replans += 1
        return plan


async def _stub_synthesize(user_message, history, summary, observations):
    return REPLY_TEXT


def _live_plan(*, tool_args: dict | None = None, attempts: int = 0) -> Plan:
    """真实形态的计划：一个承诺型工具步 + 一步回话（回话步不是承诺）。"""
    promise = SubTask(id=1, description=LIVE_STEP, tool="create_reminder",
                      tool_args=dict(tool_args or {}))
    promise.retry_count = int(attempts)
    reply = SubTask(id=2, description="回一句知道了吗", tool=None, depends_on=[1])
    return Plan(goal="12点叫主人去吃饭", subtasks=[promise, reply], plan_id=PLAN_ID)


def _agent(repo, plan: Plan, tool: _StubTool) -> ConversationalAgent:
    agent = ConversationalAgent.__new__(ConversationalAgent)
    agent._task_sessions = {}
    agent._planner = _StubPlanner(plan, tools={tool.name: tool})
    agent._repo = repo
    agent._evolve = _StubEvolve()      # ``evolve`` 是只读 property，写私有位
    agent._tool_registry = {tool.name: tool}   # ``tools`` 同上
    agent._synthesize_reply = _stub_synthesize
    return agent


def _context() -> AgentContext:
    return AgentContext(extra=dict(CTX, task_session=True))


def _turn(agent, text: str) -> str:
    """一个真实回合（**每轮清掉进程内缓存**：状态只能从台账读回）。"""
    slot_fill.clear_pending()
    return asyncio.run(agent._react_with_planner(text, "", "", _context()))


def _confirm(*, tool: _StubTool, plan: Plan, repo, receipt: str = "on",
             slot_fill_mode: str = "on", attempts: int = 0) -> tuple[str, str]:
    """跑完 live fixture 的三轮：请求 → 补参数 → 引用确认（返回 (确认那轮的回复, 计划id)）。"""
    agent = _agent(repo, plan, tool)
    with switches(slot_fill_mode=slot_fill_mode, receipt=receipt):
        _turn(agent, LIVE_MESSAGE)
        _turn(agent, "12点，叫我吃饭")
        token = _load(repo).confirm_token
        text = _turn(agent, f"确认 {token}")
    return text, plan.plan_id


def _load(repo, plan_id: str = PLAN_ID):
    return slot_fill.load(repo, slot_fill.fill_id_for(CHAT_ID, plan_id))


def _rows(engine) -> list[WorkItemRow]:
    with Session(engine) as session:
        return list(session.query(WorkItemRow).order_by(WorkItemRow.id).all())


def _run_rows(engine) -> list[WorkItemRunRow]:
    with Session(engine) as session:
        return list(session.query(WorkItemRunRow).order_by(WorkItemRunRow.id).all())


def _events(engine, event_type: str = "") -> list[TerminalEvent]:
    with Session(engine) as session:
        query = session.query(TerminalEvent)
        if event_type:
            query = query.filter(TerminalEvent.event_type == event_type)
        return list(query.order_by(TerminalEvent.id).all())


# ------------------------------------------------------------------- ① 成功
def test_success_closes_executing_to_done() -> None:
    _pin()
    engine, repo = _fresh("success")
    try:
        plan = _live_plan()
        tool = _StubTool(
            output="已创建提醒: 叫我吃饭，将在 2026年10月7日 12:00 在群里提醒",
            data={"id": 4242, "remind_at": "2026-10-07T12:00:00"},
        )
        text, _ = _confirm(tool=tool, plan=plan, repo=repo)
        fill = _load(repo)
        rows, runs = _rows(engine), _run_rows(engine)

        assert text == REPLY_TEXT, f"成功要正常出话，不是上报：{text!r}"
        assert fill is not None and fill.state == slot_fill.DONE, fill
        assert fill.has_receipt() and fill.receipt_ok(), fill.receipt
        assert fill.receipt["outcome"] == "sent"
        assert fill.receipt["run_id"], "回执要带这次 run 的身份"
        assert fill.receipt["trace_ref"].startswith("terminal_event:"), fill.receipt
        assert fill.receipt["external_msg_id"] == "4242", fill.receipt
        assert fill.receipt["steps"][0]["ok"] is True, fill.receipt["steps"]
        assert fill.receipt["steps"][0]["tool"] == "create_reminder"

        assert len(rows) == 1, "一条待办 = 一行 work_items（不新建表、不重复插）"
        assert rows[0].status == "done", rows[0].status
        assert rows[0].created_by == "slot_fill" and rows[0].kind == "act"
        assert rows[0].acceptance_criteria == store.ACCEPTANCE_SLOT_FILL

        assert len(runs) == 1, runs
        run = runs[0]
        assert run.outcome == "sent", run.outcome
        assert run.finished_at is not None, "回执要有结束时间"
        assert run.trace_ref.startswith("terminal_event:"), run.trace_ref
        assert run.delivered_external_msg_id == "4242", run.delivered_external_msg_id
        assert run.error_message == ""
        assert len(_events(engine, "work_item_run_started")) == 1
        assert len(_events(engine, "work_item_run_finished")) == 1

        # 计划层不变式：有回执 ⇔ 可当完成（同一个 live fixture 上双向都判）
        receipt = slot_fill.load_receipt(repo, fill.fill_id)
        assert receipt is not None and receipt.source == "ledger", receipt
        assert slot_fill.plan_completion(plan, [receipt]).complete is True
        assert slot_fill.plan_completion(plan, []).complete is False
    finally:
        engine.dispose()

    # 耗时是**真数**（A1-d 的回执四要素之一）：时钟可控地造一次"已执行、等回执"的收口
    engine2, repo2 = _fresh("duration")
    try:
        from datetime import datetime, timedelta

        started = datetime(2026, 10, 6, 23, 59, 0)
        fill = slot_fill.new_fill(
            slot_fill.SlotFillInput(
                chat_id=CHAT_ID, action_id="plan-duration",
                open_slots=slot_fill.slots_for_tools(["create_reminder"]),
            ),
            now=started,
        )
        fill.state = slot_fill.EXECUTING
        fill.executed = True
        fill.work_item_id = store.slot_fill_work_item_id(fill.fill_id)
        with switches():
            store.record_slot_fill(repo2, fill, anchor={"chat_id": CHAT_ID}, now=started)
            handle = store.record_slot_fill_run(
                repo2, work_item_id=fill.work_item_id, now=started,
            )
            assert handle is not None, "run 没落库：LEDGER × RUNS 没钉住？"
            machine = slot_fill.SlotFillMachine(fill, repo=repo2, now=started)
            machine.complete(
                outcome="sent", delivered_external_msg_id="77",
                now=started + timedelta(milliseconds=1500),
            )
            stored = slot_fill.load(repo2, fill.fill_id)
        run = _run_rows(engine2)[0]
        assert run.duration_ms == 1500, run.duration_ms
        assert run.delivered_external_msg_id == "77", run.delivered_external_msg_id
        assert stored.receipt["duration_ms"] == 1500, stored.receipt
        assert stored.receipt["external_msg_id"] == "77", stored.receipt
        assert stored.receipt["run_id"] == handle["run_id"], stored.receipt
        assert stored.receipt["trace_ref"] == run.trace_ref, stored.receipt
    finally:
        engine2.dispose()


# ------------------------------------------------------------------- ② 失败
def test_failed_execution_never_writes_done() -> None:
    _pin()
    engine, repo = _fresh("failure")
    try:
        plan = _live_plan()
        tool = _StubTool(error="提醒时间已经过了")
        agent = _agent(repo, plan, tool)
        with switches():
            _turn(agent, LIVE_MESSAGE)
            _turn(agent, "12点，叫我吃饭")
            token = _load(repo).confirm_token
            text = _turn(agent, f"确认 {token}")
            fill = _load(repo)
            rows, runs = _rows(engine), _run_rows(engine)

            assert text != REPLY_TEXT, "没办成就不许说办好了"
            assert "第 1 步" in text and "没拿到执行回执" in text, text
            assert "提醒时间已经过了" in text, f"原因必须带给用户：{text}"

            assert fill.state != slot_fill.DONE, "**绝不可** DONE"
            assert fill.state == slot_fill.ABORTED, fill.state
            assert fill.has_receipt() and not fill.receipt_ok(), fill.receipt
            assert fill.receipt["outcome"] == "failed", fill.receipt
            assert "提醒时间已经过了" in fill.receipt["reason"], fill.receipt
            assert fill.work_item_status() == "failed", fill.work_item_status()

            assert len(rows) == 1 and rows[0].status == "failed", rows[0].status
            assert "提醒时间已经过了" in rows[0].last_error, rows[0].last_error
            assert rows[0].last_error_class, "失败要分类（§5.5）"
            assert rows[0].attempts == rows[0].max_attempts, (
                "失败是终态：N5 没有重试方，留 open 就是造僵尸行"
            )
            assert rows[0].finished_at is not None
            assert len(runs) == 1 and runs[0].outcome == "failed", runs[0].outcome
            assert "提醒时间已经过了" in runs[0].error_message, runs[0].error_message
            assert runs[0].finished_at is not None

            # 失败之后她再说一句：如实说没办成（不是"办好了"，也不是含糊的"搁久了"）
            follow = _turn(agent, "那你再看看吧")
            assert "没办成" in follow, follow
            assert "12点" not in follow or "没办成" in follow
            assert _load(repo).state == slot_fill.ABORTED
    finally:
        engine.dispose()


# ------------------------------------------------------------------- ③ live
def test_live_fixture_no_receipt_is_not_completion() -> None:
    _pin()
    # (a) 直接判据：执行都跑完了（计划"完成"），但**一个回执都没有** → 不得视为完成
    plan = _live_plan()
    for task in plan.subtasks:
        task.status = TaskStatus.DONE
    assert plan.all_done is True, "前提：计划自己的步骤都是 DONE"
    verdict = slot_fill.plan_completion(plan, [])
    assert verdict.complete is False, "计划完成 + 无回执 = 不许被当成办好了"
    assert verdict.missing == (1,) and verdict.missing_tools == ("create_reminder",)
    assert verdict.asked_user is True and verdict.report.strip(), verdict
    assert "第 1 步" in verdict.report and "执行回执" in verdict.report, verdict.report

    # (b) 端到端：**回执没落库**（写失败）→ 台账里查不到回执行 → 这一轮走上报，
    #     不是静默、也不是报喜。注意判据是**回执**而不是行状态：run 的镜像可能已经
    #     把标签写成 done，所以"没办成的样子"必须由回执说了算。
    engine, repo = _fresh("no_receipt")
    try:
        live = _live_plan()
        tool = _StubTool(output="已创建提醒", data={"id": 4242})
        agent = _agent(repo, live, tool)
        original_persist = store.record_slot_fill

        def _drop_done(repo_arg, fill_arg, **kwargs):
            data = fill_arg.to_dict() if hasattr(fill_arg, "to_dict") else dict(fill_arg or {})
            if str(data.get("state") or "") == slot_fill.DONE:
                return None            # 这一次回执写失败（= 台账里没有回执行）
            return original_persist(repo_arg, fill_arg, **kwargs)

        store.record_slot_fill = _drop_done
        try:
            with switches():
                _turn(agent, LIVE_MESSAGE)
                _turn(agent, "12点，叫我吃饭")
                token = _load(repo).confirm_token
                text = _turn(agent, f"确认 {token}")
        finally:
            store.record_slot_fill = original_persist
        fill_id = slot_fill.fill_id_for(CHAT_ID, PLAN_ID)
        assert text != REPLY_TEXT, "查不到回执行还报喜 = 假承诺"
        assert "没拿到执行回执" in text, text
        assert live.all_done is True, "执行确实跑完了（这正是危险的那一格）"
        assert slot_fill.load_receipt(repo, fill_id) is None, "台账里不该有回执行"
        assert slot_fill.load(repo, fill_id).has_receipt() is False
        assert slot_fill.plan_completion(live, []).complete is False
    finally:
        engine.dispose()


# ------------------------------------------------------------------- ④ 上报
def test_over_budget_goes_to_report() -> None:
    _pin()
    # (a) 判据层：步骤预算到顶（attempts=3，§7 拍板项 5）→ over_budget + asked_user
    step = SubTask(id=1, description=LIVE_STEP, tool="create_reminder")
    step.retry_count = 3
    over = Plan(goal="12点叫主人去吃饭", subtasks=[step], plan_id=PLAN_ID)
    verdict = slot_fill.plan_completion(over, [])
    assert verdict.over_budget is True, verdict
    assert verdict.asked_user is True, verdict
    assert verdict.report.strip(), "超预算**必须上报**（R-C5），不许静默放弃"
    assert "要我再试一次" in verdict.report, verdict.report

    # 没到顶就不许乱报"超预算"（守卫得能真的分辨）
    step2 = SubTask(id=1, description=LIVE_STEP, tool="create_reminder")
    step2.retry_count = 1
    soft = slot_fill.plan_completion(
        Plan(goal="12点叫主人去吃饭", subtasks=[step2], plan_id=PLAN_ID), []
    )
    assert soft.complete is False and soft.over_budget is False, soft

    # (b) 接线层：上报那一句真的发出去（captured 的结论与出口文本同一份）
    engine, repo = _fresh("over_budget")
    try:
        live = _live_plan(attempts=3)
        tool = _StubTool(error="工具炸了")
        agent = _agent(repo, live, tool)
        captured: dict = {}
        original = slot_fill.settle_plan_execution

        def spy(**kwargs):
            verdict_seen = original(**kwargs)
            captured["verdict"] = verdict_seen
            return verdict_seen

        slot_fill.settle_plan_execution = spy
        try:
            with switches():
                _turn(agent, LIVE_MESSAGE)
                _turn(agent, "12点，叫我吃饭")
                token = _load(repo).confirm_token
                text = _turn(agent, f"确认 {token}")
        finally:
            slot_fill.settle_plan_execution = original
        seen = captured.get("verdict")
        assert seen is not None and seen.complete is False, captured
        assert seen.over_budget is True and seen.asked_user is True, seen
        assert text == seen.report, f"上报没发出去（静默放弃）：{text!r}"
        assert text != REPLY_TEXT
    finally:
        engine.dispose()


# ------------------------------------------------------------------- ⑤ 幂等
def test_receipt_is_idempotent() -> None:
    _pin()
    engine, repo = _fresh("idempotent")
    try:
        plan = _live_plan()
        tool = _StubTool(output="已创建提醒", data={"id": 99})
        text, _ = _confirm(tool=tool, plan=plan, repo=repo)
        assert text == REPLY_TEXT, text
        fill = _load(repo)
        runs_before = len(_run_rows(engine))
        events_before = len(_events(engine, "work_item_run_finished"))
        receipt_before = dict(fill.receipt)
        assert runs_before == 1 and events_before == 1

        with switches():
            # 同一条回执重复来（同一份执行态）：只记一次、状态不回退
            again = slot_fill.settle_plan_execution(
                plan=plan, context=dict(CTX), repo=repo,
            )
            twice = _load(repo)
            assert again is not None and again.complete is True, again
            assert len(_run_rows(engine)) == runs_before, "回执只许记一次"
            assert len(_events(engine, "work_item_run_finished")) == events_before
            assert twice.state == slot_fill.DONE and twice.receipt_ok()
            assert twice.receipt["run_id"] == receipt_before["run_id"]

            # 事后有人拿一条**失败**回执来覆盖：不许把已收口的状态拉回去
            machine = slot_fill.SlotFillMachine(twice, repo=repo)
            refused = machine.fail(reason="事后又说失败了")
            after = _load(repo)
            assert refused.action in ("already", "refused"), refused
            assert after.state == slot_fill.DONE, after.state
            assert after.receipt["outcome"] == "sent", after.receipt
            assert _rows(engine)[0].status == "done"
            assert len(_run_rows(engine)) == runs_before
            assert len(_events(engine, "work_item_run_finished")) == events_before

            # 再来一条"成功"回执同样不许写第二次
            done_again = slot_fill.SlotFillMachine(after, repo=repo).complete()
            assert done_again.action in ("already", "refused"), done_again
            assert len(_run_rows(engine)) == runs_before
            assert _load(repo).state == slot_fill.DONE
    finally:
        engine.dispose()


# ------------------------------------------------------------------- ⑥ off
def test_off_is_byte_identical() -> None:
    _pin()
    # ① 直接入口：off 一个字段都不读、一行都不写
    engine, repo = _fresh("receipt_off")
    try:
        with switches(receipt="off", slot_fill_mode="on"):
            assert slot_fill.exec_receipt_mode() == features.MODE_OFF
            assert slot_fill.receipt_tracing() is False
            assert slot_fill.receipt_enforcing() is False
            assert slot_fill.settle_plan_execution(
                plan=_live_plan(), context=dict(CTX), repo=repo,
            ) is None
            assert _rows(engine) == [] and _run_rows(engine) == []
    finally:
        engine.dispose()

    # ② 接线那一格：off 时回执入口**一次都不被调用**（换成"一碰就炸"的替身也一样），
    #    执行照旧、出口照旧，且**不留任何回执行**（这就是"逐字节退回接线前"的取证）
    engine2, repo2 = _fresh("receipt_off_wired")
    try:
        plan = _live_plan()
        tool = _StubTool(output="已创建提醒", data={"id": 5})
        agent = _agent(repo2, plan, tool)
        original = slot_fill.settle_plan_execution
        slot_fill.settle_plan_execution = _boom
        try:
            with switches(receipt="off", slot_fill_mode="on"):
                text, _ = _confirm(tool=tool, plan=plan, repo=repo2, receipt="off")
        finally:
            slot_fill.settle_plan_execution = original
        assert text == REPLY_TEXT, f"off 必须照旧出口：{text!r}"
        fill = _load(repo2)
        assert fill.state == slot_fill.EXECUTING, "off 不推进 EXECUTING→DONE"
        assert fill.has_receipt() is False and fill.receipt == {}
        assert _rows(engine2)[0].status == "running", _rows(engine2)[0].status
        assert _events(engine2, "work_item_run_finished") == []
    finally:
        engine2.dispose()

    # ③ 前置：SLOT_FILL=off 时（没有待收口的 EXECUTING）回执层按 off 处理 —— 一次都不进
    with switches(receipt="on", slot_fill_mode="off"):
        assert slot_fill.exec_receipt_mode() == features.MODE_OFF
        assert slot_fill.receipt_enforcing() is False

    # ④ shadow：收口 + 落库 + 留痕，但**不改这一轮对外说的话**（读数模式）
    engine4, repo4 = _fresh("receipt_shadow")
    try:
        plan4 = _live_plan()
        tool4 = _StubTool(output="已创建提醒", data={"id": 6})
        text4, _ = _confirm(tool=tool4, plan=plan4, repo=repo4, receipt="shadow")
        fill4 = _load(repo4)
        assert text4 == REPLY_TEXT, f"shadow 不许改出口文本：{text4!r}"
        assert fill4.state == slot_fill.DONE and fill4.receipt_ok(), "shadow 要收口留痕"
        assert _run_rows(engine4)[0].outcome == "sent"
    finally:
        engine4.dispose()

    # ⑤ 本功能**没有新模块**：`off` 时连"新模块"都不存在，所以"不 import"这一格
    #    落成"入口零调用 + 零新表"——两者都被上面的 boom 替身与下面这条断言钉住。
    assert not (ROOT / "core" / "tasks" / "exec_receipt.py").exists(), (
        "A1-d 是接线，不是新层：回执的 owner 就在 core/tasks/slot_fill.py"
    )
    from db import meta_models

    assert not [name for name in meta_models.MetaBase.metadata.tables if "receipt" in name]


def _boom(*args, **kwargs):
    raise AssertionError("off 位下回执入口被调用了（回滚位破口）")


# ------------------------------------------------------------------- ⑦ 结构锁
_TOUCHED = (
    ROOT / "core" / "tasks" / "slot_fill.py",
    ROOT / "core" / "tasks" / "store.py",
    ROOT / "agents" / "conversational.py",
)
#: 承诺话术的样本（**出站口不许拿它们当判据**；判定只认回执这个事实）。
_PROMISE_PHRASES = ("我记着了", "我记下了", "我记住了", "办好了", "已经安排", "我这就去")
#: 出站改写 / 闸门函数：回执层一个都不许调（那会变成"在出口改话术"）。
_OUTBOUND_CALLS = frozenset({
    "rerender_output_gate", "rerender_self_voice", "strip_canned_phrases",
    "strip_residual_tails", "apply_guards", "evaluate", "GateConfig",
})


def test_no_outbound_promise_filter() -> None:
    _pin()
    # (a) **判定入口没有文本参数**：`plan_completion` / `unreceipted_promises` 只看
    #     "步骤 + 回执"，签名里既没有 reply 也没有 text —— 这是"不看话术"的结构证据。
    import inspect

    for fn in (slot_fill.plan_completion, slot_fill.unreceipted_promises):
        params = set(inspect.signature(fn).parameters)
        assert not (params & {"text", "reply", "reply_text", "outbound", "message"}), (
            f"{fn.__name__} 的签名里出现了文本参数：{sorted(params)}"
        )
    source = inspect.getsource(slot_fill.plan_completion)
    assert "reply" not in source and "text" not in source.replace("context", ""), (
        "判定函数体里出现了话术变量"
    )

    # (b) 三个被改的文件里**没有**承诺词黑名单：AST 只认"真当判据用"的形状
    #     ——字符串集合字面量 / `"我记着了" in X` 这种成员判定；注释与文档不误伤。
    for path in _TOUCHED:
        tree_b = ast.parse(path.read_text(encoding="utf-8-sig"))
        for node in ast.walk(tree_b):
            if isinstance(node, (ast.List, ast.Tuple, ast.Set, ast.Dict)):
                items = (
                    list(node.keys) + list(node.values)
                    if isinstance(node, ast.Dict) else list(node.elts)
                )
                words = [
                    str(item.value) for item in items
                    if isinstance(item, ast.Constant) and isinstance(item.value, str)
                ]
                hits = [
                    word for word in words
                    if any(phrase in word for phrase in _PROMISE_PHRASES)
                ]
                assert not hits, (
                    f"{path.name} 里出现了承诺话术的词表/字面量集合：{hits}"
                )
            if isinstance(node, ast.Compare) and any(
                isinstance(op, (ast.In, ast.NotIn)) for op in node.ops
            ):
                for side in (node.left, *node.comparators):
                    if isinstance(side, ast.Constant) and isinstance(side.value, str):
                        assert not any(
                            phrase in side.value for phrase in _PROMISE_PHRASES
                        ), f"{path.name} 里用承诺话术做成员判定：{side.value!r}"

    # (c) 回执那一段**不调任何出站改写 / 闸门函数**（AST，不靠 grep 假阴性）
    tree = ast.parse(_TOUCHED[0].read_text(encoding="utf-8-sig"))
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name not in ("plan_completion", "settle_plan_execution",
                             "receipt_from_fill", "unreceipted_promises",
                             "_record_receipt", "complete", "fail"):
            continue
        for sub in ast.walk(node):
            if isinstance(sub, ast.Call):
                fn = sub.func
                name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
                assert name not in _OUTBOUND_CALLS, (
                    f"{node.name} 调了出站改写/闸门函数 {name}：回执层不许碰出口话术"
                )

    # (d) 唯一读点：`core/` + `agents/` 里读 `features.EXEC_RECEIPT` 的只有 owner 自己
    #     （别的模块一律调 ``slot_fill.exec_receipt_mode()``，不许各写一份读法）
    readers: set[str] = set()
    for base in ("core", "agents"):
        for path in (ROOT / base).rglob("*.py"):
            try:
                tree_d = ast.parse(path.read_text(encoding="utf-8-sig"))
            except (SyntaxError, UnicodeDecodeError):
                continue
            for node in ast.walk(tree_d):
                if (
                    isinstance(node, ast.Attribute)
                    and node.attr == "EXEC_RECEIPT"
                    and isinstance(node.value, ast.Name)
                    and node.value.id in ("features", "_features")
                ):
                    readers.add(path.relative_to(ROOT).as_posix())
    assert readers == {"core/tasks/slot_fill.py"}, readers

    # (e) 「承诺型工具」这张表不许漂：N5 槽位表 = 毁灭性工具集 = 承诺集
    assert set(slot_fill.ACTION_SLOT_SPECS) == set(_DESTRUCTIVE_TOOLS)
    assert slot_fill.PROMISE_TOOLS == frozenset(_DESTRUCTIVE_TOOLS), slot_fill.PROMISE_TOOLS

    # (f) 计划层不变式的唯一性：判定只此一处（别的模块不许各写一份）
    for path in (ROOT / "agents").rglob("*.py"):
        body = path.read_text(encoding="utf-8-sig")
        assert "_receipt_ok" not in body and "def plan_completion" not in body, path


# ------------------------------------------------------------------- 主入口
def main() -> bool:
    print("=" * 60)
    print("exec receipt (A1-d) checks")
    print("=" * 60)
    tests = [
        test_success_closes_executing_to_done,
        test_failed_execution_never_writes_done,
        test_live_fixture_no_receipt_is_not_completion,
        test_over_budget_goes_to_report,
        test_receipt_is_idempotent,
        test_off_is_byte_identical,
        test_no_outbound_promise_filter,
    ]
    results = []
    for test in tests:
        try:
            test()
            results.append((test.__name__, True))
        except Exception:  # pragma: no cover - diagnostics only
            traceback.print_exc()
            results.append((test.__name__, False))
    print()
    passed = sum(1 for _, ok in results if ok)
    for name, ok in results:
        print(f"  {'ok  ' if ok else 'FAIL'} {name}")
    print(f"{passed}/{len(results)} passed")
    return passed == len(results)


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
