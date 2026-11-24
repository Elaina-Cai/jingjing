"""N5 参数收集状态机（``core/tasks/slot_fill.py``）的守卫用例。

覆盖（与验收单逐条对应）
------------------------
① ``test_first_ask_covers_every_missing_slot`` —— 缺两个槽位**一次问齐**（不是挤牙膏），
   且 ``asked_batch`` = 缺的那些、``≤ MAX_ASK_BATCH``、文案里有 R-P1 要求的"复述已知"；
② ``test_filled_slots_move_to_confirming`` —— 补齐 → ``CONFIRMING``（列出全部参数 + 编号）；
③ ``test_reference_check_accepts_and_rejects`` —— **关键格**：引用/编号对得上 → ``EXECUTING``；
   说了"好/可以"但**指向对不上** → 不执行、``CONFIRMING`` 原地重新问一句；
④ ``test_state_survives_across_turns`` —— 多轮跨消息（每轮清进程内缓存、从台账读回）状态不丢；
⑤ ``test_repeated_confirm_executes_once`` —— 幂等：同一条确认重复来**只执行一次**
   （执行计数 = 1 且 ``work_item_runs`` / ``work_item_run_started`` 各 1）；
⑥ ``test_off_is_byte_identical`` —— ``off`` 逐字节一致：接线那一格仍说旧文案，
   且状态机**一次都不被调用**（把它换成会抛异常的替身也是这个结果）；
⑦ ``test_transitions_are_replayable`` —— 状态迁移可回放（``transitions`` 留痕 + 链完整性）。

测试卫生
--------
* **"关"一律显式 ``off``**（不靠"env 未设"——那是 ``off`` 与 ``shadow`` 的分界）；
  import 期把本文件依赖的每个开关都钉死：``SLOT_FILL`` / ``WORK_ITEM_LEDGER`` ×
  ``WORK_ITEM_RUNS``（后者是 ``tests/test_env_flag_isolation.py`` 的兄弟组，**要钉一起钉**）；
* **不写生产库**：自带 ``create_all`` 出来的临时 meta 库；``LOG_DIR`` / ``META_DB_PATH`` /
  ``PLATFORMS_DIR`` / ``DATA_DIR`` 全指向 ``data/`` 下的隔离目录。

Run directly:
    python tests/test_slot_fill.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import traceback
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

ROOT = Path(__file__).resolve().parent.parent
_TEST_LOG_DIR = str(Path("data") / "_test_logs_slot_fill")
Path(_TEST_LOG_DIR).mkdir(parents=True, exist_ok=True)
os.environ["LOG_DIR"] = _TEST_LOG_DIR
_ISO_ROOT = (ROOT / "data" / "_slot_fill_test" / "_iso").resolve()
_ISO_ROOT.mkdir(parents=True, exist_ok=True)
os.environ["META_DB_PATH"] = str(_ISO_ROOT / "meta.db")
os.environ["PLATFORMS_DIR"] = str(_ISO_ROOT / "platforms")
os.environ["DATA_DIR"] = str(_ISO_ROOT / "data")

# 日志初始化会去碰 LOG_DIR：这里只留隔离目录，初始化本身拦掉。
import core.infra.logging as _logging  # noqa: E402

_logging.init_logging = lambda *args, **kwargs: None

# ---- 开关钉死（import 期，显式；**不许**靠"env 未设"）----------------------
os.environ["YINYUE_FEATURE_SLOT_FILL"] = "off"        # 本文件的"关"位：显式 off
os.environ["YINYUE_FEATURE_WORK_ITEM_LEDGER"] = "1"   # 落库用例依赖（兄弟组两个一起钉）
os.environ["YINYUE_FEATURE_WORK_ITEM_RUNS"] = "1"

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from agents.base import AgentContext  # noqa: E402
from agents.conversational import ConversationalAgent  # noqa: E402
from core import features  # noqa: E402
from core.tasks import slot_fill, store  # noqa: E402
from db.meta_models import MetaBase, TerminalEvent, WorkItemRow, WorkItemRunRow  # noqa: E402

CHAT_ID = "12345@chatroom"
ACTION_ID = "plan-n5-1"
#: ``agents/conversational.py`` 那一格确认逻辑的**冻结旧文案**（off 位的逐字节基准）。
LEGACY_CONFIRM = "我准备这样做：\n1. 提醒吃药 (工具: create_reminder)\n\n可以继续吗？"
CTX = {
    "chat_id": CHAT_ID,
    "session_id": 7,
    "thread_id": "thread-n5",
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
def switches(mode: str = "on", *, ledger: bool = True, runs: bool = True):
    """临时改三个开关（``features`` 每次读 env，不用重启）。"""
    keys = {
        "YINYUE_FEATURE_SLOT_FILL": mode,
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


def _plan(plan_id: str = ACTION_ID, args: dict | None = None):
    """计划替身（只需要 ``plan_id`` / ``subtasks`` / ``all_done`` 这几项）。"""
    subtask = SimpleNamespace(
        id=1, description="提醒吃药", tool="create_reminder",
        tool_args=dict(args or {}), status=None,
    )
    return SimpleNamespace(
        plan_id=plan_id, subtasks=[subtask], all_done=False,
        needs_clarification=False, clarification_question="",
    )


def _gate(plan, text: str, repo, ctx: dict | None = None):
    return slot_fill.gate_plan_confirmation(
        plan=plan, incoming_text=text, context=dict(ctx or CTX), repo=repo,
    )


def _load(repo, plan_id: str = ACTION_ID):
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


def _turns(repo, plan, messages: list[str], *, ctx: dict | None = None):
    """模拟真实回合：**每轮清掉进程内缓存**，状态只能从台账读回。"""
    out = []
    for text in messages:
        slot_fill.clear_pending()
        out.append(_gate(plan, text, repo, ctx))
    return out


# ------------------------------------------------------------------- ① 一次问齐
def test_first_ask_covers_every_missing_slot() -> None:
    engine, repo = _fresh("ask_all")
    try:
        with switches("on"):
            text = _gate(_plan(), "帮我定个提醒", repo)
            fill = _load(repo)
        assert text, "状态机接管后必须给出提问文案"
        assert fill is not None and fill.state == slot_fill.GATHERING, fill
        assert fill.asked_batch == ["时间", "内容"], fill.asked_batch
        assert len(fill.asked_batch) <= slot_fill.MAX_ASK_BATCH
        for name in fill.asked_batch:
            assert name in text, f"一次问齐：{name} 必须出现在同一条消息里\n{text}"
        assert "我记着的是" in text, f"R-P1 要求复述已知信息：\n{text}"
        assert "例如" in text, f"R-P6 要求明确具体的提示：\n{text}"
        assert fill.nudges == 1, "第一次问算一次催问"
        assert fill.pending_slots == ("时间", "内容")
        assert fill.slot("时间").value is None, "第一回合是请求本身，不该被当成回答"
    finally:
        engine.dispose()


# ------------------------------------------------------------------- ② 进确认
def test_filled_slots_move_to_confirming() -> None:
    engine, repo = _fresh("to_confirming")
    try:
        with switches("on"):
            _gate(_plan(), "帮我定个提醒", repo)
            text = _gate(_plan(), "明天早上8点，提醒我吃药", repo)
            fill = _load(repo)
        assert fill is not None and fill.state == slot_fill.CONFIRMING, fill
        assert fill.pending_slots == (), fill.pending_slots
        assert fill.slot("时间").value == "明天早上8点", fill.slot("时间").value
        assert fill.slot("内容").value == "提醒我吃药", fill.slot("内容").value
        assert all(slot.valid and slot.closed for slot in fill.slots)
        assert fill.confirm_ref, "待确认事项必须有身份（引用 id 或短编号）"
        assert fill.confirm_token in text, f"待确认文案里必须印出编号/引用要求：\n{text}"
        for name in ("时间", "内容"):
            assert name in text, f"R-P4：待确认消息要列出全部参数（缺 {name}）\n{text}"
        assert "引用" in text, f"R-P4 要说清引用我这条：\n{text}"
    finally:
        engine.dispose()


# ------------------------------------------------------------------- ③ 引用校验
def test_reference_check_accepts_and_rejects() -> None:
    engine, repo = _fresh("reference")
    try:
        with switches("on"):
            _gate(_plan(), "帮我定个提醒", repo)
            _gate(_plan(), "明天早上8点，提醒我吃药", repo)
            fill = _load(repo)
            token = fill.confirm_token
            assert token.startswith("#")

            # (a) 说了"好"，但**指向对不上** → 不许当确认、不执行、原地重新问
            denied = _gate(_plan(), "好", repo)
            after_denied = _load(repo)
            assert denied, "引用对不上时必须重新问一句（不能静默执行）"
            assert after_denied.state == slot_fill.CONFIRMING, after_denied.state
            assert after_denied.executed is False
            assert after_denied.confirm_word is True and after_denied.confirm_quoted is False
            # 状态没动（拒绝**不是**迁移），但判定留痕必须在（notes 而非 transitions）
            assert [t.reason for t in after_denied.transitions] == [
                "slots_complete", "slots_valid",
            ], [t.reason for t in after_denied.transitions]
            assert after_denied.notes[-1]["kind"] == "confirm_rejected"
            assert after_denied.notes[-1]["detail"] == "no_reference"
            assert after_denied.notes[-1]["state"] == slot_fill.CONFIRMING
            assert "确认" in denied, f"重问要再说一遍怎么确认：\n{denied}"
            assert _run_rows(engine) == [], "没确认就不许有 run"

            # (b) 引了**别的**消息 → 同样是"对不上"
            other = dict(CTX, quoted_msg_id="msg-other")
            denied2 = _gate(_plan(), "好的", repo, other)
            after2 = _load(repo)
            assert denied2 and after2.state == slot_fill.CONFIRMING
            assert after2.notes[-1]["detail"] == "quoted_other"

            # (c) 引用**那一条**（自己那条消息 id 已知时）→ 最强证据，放行
            known = _load(repo)
            known.confirm_message_ref = "msg-confirm-777"
            store.record_slot_fill(repo, known, anchor={"chat_id": CHAT_ID})
            quoted = dict(CTX, quoted_msg_id="msg-confirm-777")
            allowed = _gate(_plan(), "好的", repo, quoted)
            after_allowed = _load(repo)
            assert allowed == "", f"引用对得上就该落到执行循环（返回空串）：{allowed!r}"
            assert after_allowed.state == slot_fill.EXECUTING
            assert after_allowed.executed is True
            assert after_allowed.confirm_quoted is True and after_allowed.confirm_word is True
            assert after_allowed.transitions[-1].reason == "confirm_ref_matched"
            assert after_allowed.transitions[-1].evidence == "quoted_msg_id=msg-confirm-777"
            assert len(_run_rows(engine)) == 1, "确认后要落一次 run"
    finally:
        engine.dispose()

    # (d) 编号兜底（现实引用稀疏时的显式设计）：正文原样带回编号也算"指回同一条"
    engine2, repo2 = _fresh("reference_token")
    try:
        with switches("on"):
            _gate(_plan(), "帮我定个提醒", repo2)
            _gate(_plan(), "明天早上8点，提醒我吃药", repo2)
            token2 = _load(repo2).confirm_token
            assert _gate(_plan(), f"确认 {token2}", repo2) == ""
            assert _load(repo2).state == slot_fill.EXECUTING
            # 否词优先：带"先不用"的确认句一律不算确认（R-P4 不许被语气骗）
            engine3, repo3 = _fresh("reference_negation")
            try:
                _gate(_plan(), "帮我定个提醒", repo3)
                _gate(_plan(), "明天早上8点，提醒我吃药", repo3)
                token3 = _load(repo3).confirm_token
                denied3 = _gate(_plan(), f"先不用 {token3}", repo3)
                assert denied3 and _load(repo3).state == slot_fill.CONFIRMING
            finally:
                engine3.dispose()
    finally:
        engine2.dispose()


# ------------------------------------------------------------------- ④ 跨轮不丢
def test_state_survives_across_turns() -> None:
    engine, repo = _fresh("multi_turn")
    try:
        with switches("on"):
            texts = _turns(repo, _plan(), [
                "帮我定个提醒",
                "明天早上8点，提醒我吃药",
                "好",                      # 引用对不上：不许当确认
            ])
            mid = _load(repo)
            token = mid.confirm_token
            texts.append(_gate(_plan(), f"确认 {token}", repo))
            final = _load(repo)
        assert mid is not None and mid.state == slot_fill.CONFIRMING
        # 迁移只有两次（收齐 → 校验通过）；"好"那轮没有迁移，但有拒绝留痕
        assert [t.reason for t in mid.transitions] == ["slots_complete", "slots_valid"]
        assert mid.notes[-1]["kind"] == "confirm_rejected", mid.notes
        # 状态与槽位一路从台账读回：中间的"好"没把状态打回去、也没丢槽位
        assert mid.slot("时间").value == "明天早上8点"
        assert mid.nudges == 2, mid.nudges
        assert final.state == slot_fill.EXECUTING and final.executed is True
        assert [t.reason for t in final.transitions] == [
            "slots_complete", "slots_valid", "confirm_ref_matched",
        ]
        assert final.created_at and final.expires_at, "契约要求 created_at / expires_at"
        assert texts[0] and texts[1] and texts[2] and texts[3] == ""
    finally:
        engine.dispose()

    # 台账关掉时退进程内缓存，状态同样不许丢（两条路不并存，但都得能用）
    engine2, repo2 = _fresh("multi_turn_cache")
    try:
        slot_fill.clear_pending()                  # 上一段的缓存不跨库复用
        with switches("on", ledger=False, runs=False):
            first = _gate(_plan(), "帮我定个提醒", repo2)
            second = _gate(_plan(), "明天早上8点，提醒我吃药", repo2)
            cached = _load(repo2)
            assert first and second and cached is None, "LEDGER 关时不该写台账"
            live = slot_fill._PENDING[slot_fill.fill_id_for(CHAT_ID, ACTION_ID)]
            assert live.state == slot_fill.CONFIRMING, live.state
            assert _rows(engine2) == []
    finally:
        engine2.dispose()
        slot_fill.clear_pending()


# ------------------------------------------------------------------- ⑤ 幂等
def test_repeated_confirm_executes_once() -> None:
    engine, repo = _fresh("idempotent")
    try:
        with switches("on"):
            _gate(_plan(), "帮我定个提醒", repo)
            _gate(_plan(), "明天早上8点，提醒我吃药", repo)
            token = _load(repo).confirm_token
            first = _gate(_plan(), f"确认 {token}", repo)
            second = _gate(_plan(), f"确认 {token}", repo)
            third = _gate(_plan(), f"确认 {token}", repo)
            fill = _load(repo)
        executes = [value for value in (first, second, third) if value == ""]
        assert len(executes) == 1, f"同一条确认只许执行一次：{first!r} {second!r} {third!r}"
        assert second and third, "重复的确认要有话回（不能静默）"
        assert fill.executed is True and fill.state == slot_fill.EXECUTING
        runs = _run_rows(engine)
        assert len(runs) == 1, f"落库也要只有一次 run：{len(runs)}"
        started = _events(engine, "work_item_run_started")
        assert len(started) == 1, f"work_item_run_started 只许一条：{len(started)}"
        tables = _rows(engine)
        assert len(tables) == 1, "一条待办只对应一行 work_items（确定性身份，不重复插）"
        assert tables[0].status == "running", tables[0].status
        assert tables[0].created_by == "slot_fill" and tables[0].kind == "act"
        assert len(_events(engine, "work_item_created")) == 1, "第二次写只 UPDATE，不追加事件"
    finally:
        engine.dispose()


# ------------------------------------------------------------------- ⑥ off
def test_off_is_byte_identical() -> None:
    # ① 直接入口：off 时一个字段都不读、一行都不写
    engine, repo = _fresh("flag_off")
    try:
        with switches("off"):
            assert slot_fill.slot_fill_mode() == features.MODE_OFF
            assert slot_fill.tracing() is False and slot_fill.enforcing() is False
            assert _gate(_plan(), "帮我定个提醒", repo) is None
            assert _gate(_plan(), "明天早上8点，提醒我吃药", repo) is None
            assert _rows(engine) == [] and _run_rows(engine) == []
            assert _events(engine) == []
    finally:
        engine.dispose()

    # ② 接线那一格：仍说旧文案；把状态机换成"一碰就炸"的替身也一样（逐字节 + 零触碰）
    engine2, repo2 = _fresh("flag_off_wired")
    try:
        with switches("off"):
            agent = _agent(repo2)
            original = slot_fill.gate_plan_confirmation
            slot_fill.gate_plan_confirmation = _boom
            try:
                text = asyncio.run(
                    agent._react_with_planner("帮我定个提醒", "", "", _context())
                )
            finally:
                slot_fill.gate_plan_confirmation = original
            assert text == LEGACY_CONFIRM, f"off 必须逐字节一致：\n{text!r}\n{LEGACY_CONFIRM!r}"
            assert _rows(engine2) == [] and _events(engine2) == []
    finally:
        engine2.dispose()

    # ③ shadow：判定 + 落库 + 留痕，但**对外还是旧文案**（行为与 off 一致）
    engine3, repo3 = _fresh("flag_shadow")
    try:
        with switches("shadow"):
            agent = _agent(repo3)
            shadow_text = asyncio.run(
                agent._react_with_planner("帮我定个提醒", "", "", _context())
            )
            assert shadow_text == LEGACY_CONFIRM, shadow_text
            assert len(_rows(engine3)) == 1, "shadow 要落库（读数从台账来）"
            assert _load(repo3).state == slot_fill.GATHERING
    finally:
        engine3.dispose()

    # ④ on：同一个入口现在说的是状态机的话（收编那一次确认的取证）
    engine4, repo4 = _fresh("flag_on_wired")
    try:
        with switches("on"):
            agent = _agent(repo4)
            on_text = asyncio.run(
                agent._react_with_planner("帮我定个提醒", "", "", _context())
            )
            assert on_text != LEGACY_CONFIRM, "on 位必须接管那一格"
            assert on_text and "时间" in on_text and "内容" in on_text, on_text
    finally:
        engine4.dispose()


def _boom(*args, **kwargs):
    raise AssertionError("off 位下状态机被调用了（回滚位破口）")


def _agent(repo) -> ConversationalAgent:
    agent = ConversationalAgent.__new__(ConversationalAgent)
    agent._task_sessions = {}
    agent._planner = _StubPlanner(_plan())     # planner 是只读 property，写私有位
    agent._repo = repo                         # repo 同上
    return agent


def _context() -> AgentContext:
    return AgentContext(extra=dict(CTX, task_session=True))


class _StubPlanner:
    """只提供 ``_react_with_planner`` 进入确认那一格所需的两个方法。"""

    def __init__(self, plan):
        self.plan = plan
        self.context = None

    def set_context(self, context):
        self.context = context

    async def understand_and_plan(self, user_message, context_summary="", session_id=None):
        return self.plan


# ------------------------------------------------------------------- ⑦ 可回放
def test_transitions_are_replayable() -> None:
    engine, repo = _fresh("replay")
    try:
        with switches("on"):
            _gate(_plan(), "帮我定个提醒", repo)
            _gate(_plan(), "明天早上8点，提醒我吃药", repo)
            _gate(_plan(), "好", repo)
            token = _load(repo).confirm_token
            _gate(_plan(), f"确认 {token}", repo)
            fill = _load(repo)
            steps = [item.to_dict() for item in fill.transitions]
            stored = store.load_slot_fill(repo, fill.fill_id)
        assert len(steps) == 3, steps
        for step in steps:
            assert set(step) == {"from", "to", "reason", "at", "evidence"}, step
            assert step["from"] in slot_fill.STATES and step["to"] in slot_fill.STATES
            assert step["at"], "每一步都要有时间戳（可回放的留痕）"
            assert step["reason"], "每一步都要有理由"
        assert slot_fill.replay(fill.transitions) == fill.state == slot_fill.EXECUTING
        state, problems = slot_fill.verify_chain(fill.transitions)
        assert state == fill.state and problems == [], problems
        # 反向：把链改坏必须报出来（守卫得能真的红）
        broken = [slot_fill.Transition.from_dict(step) for step in steps]
        broken[2].from_state = slot_fill.GATHERING
        _, bad = slot_fill.verify_chain(broken)
        assert bad, "链被改坏却报不出来 = 这条守卫是假的"
        # 判定留痕（非迁移的那些）也在台账里：拒绝过的那一次能查
        assert fill.notes[-1]["kind"] in ("confirm_rejected", "already_executed")
        assert stored["notes"][0]["detail"] == "no_reference", stored["notes"]
        # 台账里留的是同一份 blob（落库不是另抄一份状态）
        assert [item["reason"] for item in stored["transitions"]] == [
            step["reason"] for step in steps
        ]
        assert stored["work_item_status"] == "running"
    finally:
        engine.dispose()


# ------------------------------------------------------------------- 主入口
def main() -> bool:
    print("=" * 60)
    print("slot fill (N5) checks")
    print("=" * 60)
    tests = [
        test_first_ask_covers_every_missing_slot,
        test_filled_slots_move_to_confirming,
        test_reference_check_accepts_and_rejects,
        test_state_survives_across_turns,
        test_repeated_confirm_executes_once,
        test_off_is_byte_identical,
        test_transitions_are_replayable,
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
