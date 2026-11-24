# -*- coding: utf-8 -*-
"""批B 整体验收（B1 正经事信号 / B2 计划执行 / B3 人设通道）。

验收口径（docs/群聊社会认知_新架构_施工单.md 批B）：
* 决策可审计：三态链路各自的 chain / 审计事件（plan_path、serious_signals、
  persona_channel_*）可查、可回放；
* 行动规划可回放：执行载荷纯 JSON、同输入两次结果一致、防双执行；
* off 不变：三个 flag 默认 off，off 时零计算/零读/零注入，get_persona 不进
  默认工具表（工具 schema 也不进 prompt）；
* 灰度无撤回：写/外发工具永不自动执行、NEVER 优先级不被越过、回滚=置 off+重启。

只做**离线**结构+行为验收（现行执行模式：施工期 flag 保持 off，不跑灰度收数）。
"""
from __future__ import annotations

import asyncio
import contextlib
import importlib
import json
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from agents.planner import (  # noqa: E402
    PLAN_LOOP_READ_ONLY_TOOLS,
    Planner,
    TaskStatus,
    plan_exec_tools,
    plan_execution_hint,
    plan_loop_mode,
)
from agents.tools.base import ToolRegistry  # noqa: E402
from core import features  # noqa: E402
from core.execution.persona_channel import (  # noqa: E402
    PersonaView,
    persona_channel_mode,
    persona_digest,
    persona_note,
)
from core.routing import turn_pipeline  # noqa: E402

TS = "2026-03-01 12:00"


@contextlib.contextmanager
def _env(**values):
    saved = {key: os.environ.get(key) for key in values}
    for key, value in values.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = str(value)
    try:
        yield
    finally:
        for key, old in saved.items():
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old


@contextlib.contextmanager
def _patch_attr(module, name, value):
    old = getattr(module, name)
    setattr(module, name, value)
    try:
        yield
    finally:
        setattr(module, name, old)


class _RecLog:
    def __init__(self):
        self.events = []

    def _record(self, level, event, **kw):
        self.events.append((level, str(event), kw))

    def info(self, event, **kw):
        self._record("info", event, **kw)

    def warning(self, event, **kw):
        self._record("warning", event, **kw)

    def error(self, event, **kw):
        self._record("error", event, **kw)

    def debug(self, event, **kw):
        self._record("debug", event, **kw)

    def names(self):
        return [event for _level, event, _kw in self.events]

    def get(self, name):
        for _level, event, kw in self.events:
            if event == name:
                return kw
        return {}


@contextlib.contextmanager
def _patch_log(module):
    rec = _RecLog()
    with _patch_attr(module, "log", rec):
        yield rec


# ---------------------------------------------------------------------------
# 条款 0：flag 默认 off（off 不变的前提）
# ---------------------------------------------------------------------------

def test_accept_flags_default_off_and_registered() -> None:
    with _env(
        YINYUE_FEATURE_PLAN_LOOP=None,
        YINYUE_FEATURE_PERSONA_CHANNEL=None,
    ):
        assert plan_loop_mode() == "off"
        assert persona_channel_mode() == "off"
    for name in (features.PLAN_LOOP, features.PERSONA_CHANNEL):
        assert name in features.TRI_STATE_FLAGS
    # 2026-11-02 猜证（GUESS_VERIFY）退役，计数随之下调。
    # +JOB_LEDGER（U-10 在手的事台账）：77 → 78
    # 2026-10-23 R2：serious_signals 与 silence_consumed 随判决层一起退役，82 → 81
    # 2026-10-23 N1 扫尾：TURN_OWNERSHIP → 80；ROOM_EPISODE / ADDRESSEE_CONTRACT / 归属族六旗标 → 72；REPLY_INTENT + UNKNOWN_FIRST_CLASS → 68
    # 2026-10-24 读场/按需记忆整族退役（READ_BLOCK / NEEDS_MEMORY_INJECT / RECALL_IN_WINDOW / MEMORY_TOOL_VISIBILITY / MEMORY_TOOL_FALLBACK）→ 63
    assert len(features.TRI_STATE_FLAGS) == 57  # 2026-11-02 猜证退役：59 → 58


# ---------------------------------------------------------------------------
# 条款 1：决策可审计（B1）
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# 条款 2：行动规划可回放（B2）
# ---------------------------------------------------------------------------

def _plan(tool="search_messages"):
    return SimpleNamespace(subtasks=[SimpleNamespace(
        id=1, description="查一下消息", tool=tool, status=TaskStatus.PENDING,
        attempts=0, switch_log=(), result="",
    )])


def _result(**overrides):
    base = dict(
        replans=0, over_budget=False, asked_user=False, report="已办完",
        steps=[{"id": 1, "description": "查一下消息"}],
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _context(**overrides):
    base = dict(
        extra={}, plan_object=None, plan_result=None,
        plan_execution=None, plan_execution_started=False,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class _AcceptPlanner(Planner):
    """真接线的 Planner + 只桩掉 LLM 出口：规划与执行走同一份 Plan。"""

    def __init__(self, tools):
        super().__init__(None, tools)
        self.calls = []

    async def plan_turn_with_plan(self, user_message, context_summary="", session_id=None):
        return _result(), _plan()

    async def run_plan_loop(self, user_message, *, tools=None, session_id=None,
                            plan=None, ts_factory=None):
        self.calls.append({
            "user_message": str(user_message),
            "tools": sorted(tools.keys()) if tools is not None else [],
            "ts": str(ts_factory() if callable(ts_factory) else ""),
        })
        for task in tuple(getattr(plan, "subtasks", ()) or ()):
            task.status = TaskStatus.DONE
            task.result = f"{task.tool}:ok"
        return _result()


def _run_full_flow():
    """决策→plan_path 留痕→装配 plan_object→按步骤执行→回放载荷（一次完整链）。"""
    planner_module = importlib.import_module("agents.planner")
    dt_module = importlib.import_module("core.observability.decision_trace")
    trace_records = []
    rec = _RecLog()

    def _record(record, **_kw):
        trace_records.append(dict(record))
        return True

    with _env(YINYUE_FEATURE_PLAN_LOOP="on"):
        with _patch_attr(planner_module, "log", rec):
            with _patch_attr(dt_module, "record_decision", _record):
                planner = _AcceptPlanner({"search_messages": object()})
                context = _context()
                payload = asyncio.run(planner.attach_content_plan(
                    context, chat_id="c1",
                    current="帮查下", origin="direct", extra=context.extra,
                ))
                assert payload is not None
                assert context.plan_object is not None
                assert trace_records and trace_records[0]["kind"] == "plan_path"
                assert trace_records[0]["plan_loop"] == "on"
                assert trace_records[0]["plan_digest"]
                execution = asyncio.run(planner.execute_attached_plan(
                    context, tools=planner.tools, user_message="帮查下",
                    ts_factory=lambda: TS,
                ))
                assert execution == context.plan_execution
                assert execution["steps"][0]["status"] == "done"
                assert planner.calls[-1]["ts"] == TS
                assert "content_plan_attached" in rec.names()
                assert "plan_execution_done" in rec.names()
                return dict(trace_records[0]), execution


def test_accept_b2_off_rollback_zero_trace() -> None:
    dt_module = importlib.import_module("core.observability.decision_trace")
    trace_records = []
    with _env(YINYUE_FEATURE_PLAN_LOOP="off"):
        with _patch_attr(dt_module, "record_decision",
                         lambda record, **kw: trace_records.append(dict(record)) or True):
            planner = _AcceptPlanner({"search_messages": object()})
            context = _context()
            out = asyncio.run(planner.attach_content_plan(
                context, chat_id="c1",
                current="帮查下", origin="direct", extra=context.extra,
            ))
        assert out is None
        assert context.plan_object is None
        assert planner.calls == []                # off：不执行任何步骤
        assert trace_records == []                # off：连审计字节都不多
        assert plan_execution_hint({"steps": [{"id": 1, "status": "done"}]}) == ""


def test_accept_b2_replay_deterministic() -> None:
    record_a, exec_a = _run_full_flow()
    record_b, exec_b = _run_full_flow()
    assert record_a == record_b                   # 同输入两次：留痕一致
    assert exec_a == exec_b                       # 同输入两次：执行载荷一致
    replay = json.dumps({"record": record_a, "execution": exec_a}, ensure_ascii=False)
    assert "查一下消息" in replay                  # 纯 JSON，可直接回放/审计
    with _env(YINYUE_FEATURE_PLAN_LOOP="on"):
        block = plan_execution_hint(exec_a)
        assert block.startswith("[步骤执行结果]") and "search_messages:ok" in block


def test_accept_b2_guards_write_tool_and_double_execution() -> None:
    planner_module = importlib.import_module("agents.planner")
    with _env(YINYUE_FEATURE_PLAN_LOOP="on"):
        # 写/外发工具步：整轮不执行
        planner = _AcceptPlanner({"send_message": object()})
        context = _context(plan_object=_plan(tool="send_message"))
        with _patch_log(planner_module) as rec:
            out = asyncio.run(planner.execute_attached_plan(
                context, tools=planner.tools, user_message="发", ts_factory=lambda: TS,
            ))
        assert out is None and planner.calls == []
        assert context.plan_execution_started is False
        assert "plan_execution_skipped_write_tool" in rec.names()
        # task_session 既有路径：不双执行
        session_context = _context(
            plan_object=_plan(), extra={"task_session": {"id": 1}},
        )
        planner2 = _AcceptPlanner({"search_messages": object()})
        assert asyncio.run(planner2.execute_attached_plan(
            session_context, tools=planner2.tools, user_message="x",
        )) is None
        # 防双执行：同 context 第二次返回 None，读数不被覆盖
        context2 = _context(plan_object=_plan())
        planner3 = _AcceptPlanner({"search_messages": object()})
        first = asyncio.run(planner3.execute_attached_plan(
            context2, tools=planner3.tools, user_message="x", ts_factory=lambda: TS,
        ))
        second = asyncio.run(planner3.execute_attached_plan(
            context2, tools=planner3.tools, user_message="x", ts_factory=lambda: TS,
        ))
        assert first is not None and second is None
        assert context2.plan_execution == first


# ---------------------------------------------------------------------------
# 条款 3：B3 人设通道（off 零读、审计、digest 不泄正文）
# ---------------------------------------------------------------------------

class _Row:
    def __init__(self, pid, name, system_prompt="你是鲸鲸", traits="温柔", voice_config="简短"):
        self.id = pid
        self.name = name
        self.system_prompt = system_prompt
        self.traits = traits
        self.voice_config = voice_config


class _Personas:
    def __init__(self, rows, reads):
        self._rows = list(rows)
        self._reads = reads

    def _touch(self):
        self._reads.append(1)

    def get_by_id(self, pid):
        self._touch()
        return next((r for r in self._rows if int(r.id) == int(pid)), None)

    def list_active(self):
        self._touch()
        return list(self._rows[:1])

    def list_all(self):
        self._touch()
        return list(self._rows)


class _Repo:
    def __init__(self, rows):
        self.reads = []
        self.personas = _Personas(rows, self.reads)


def test_accept_b3_tool_registration_gated_by_flag() -> None:
    with _env(YINYUE_FEATURE_PERSONA_CHANNEL="off"):
        registry = ToolRegistry.create_default(repo=_Repo([_Row(1, "鲸鲸")]))
        assert "get_persona" not in registry.all_tools()   # off：schema 不进 prompt
    with _env(YINYUE_FEATURE_PERSONA_CHANNEL="shadow"):
        registry = ToolRegistry.create_default(repo=_Repo([_Row(1, "鲸鲸")]))
        assert "get_persona" not in registry.all_tools()   # shadow 只记账：schema 也不进 prompt
    with _env(YINYUE_FEATURE_PERSONA_CHANNEL="on"):
        registry = ToolRegistry.create_default(repo=_Repo([_Row(1, "鲸鲸")]))
        assert "get_persona" in registry.all_tools()
        assert plan_exec_tools(registry).get("get_persona") is not None


def test_accept_b3_off_zero_read_shadow_on_audit() -> None:
    repo = _Repo([_Row(1, "鲸鲸")])
    router = SimpleNamespace(_repo=repo)
    with _env(YINYUE_FEATURE_PLAN_LOOP="on", YINYUE_FEATURE_PERSONA_CHANNEL="off"):
        with _patch_log(turn_pipeline) as rec:
            note = turn_pipeline._persona_plan_note(router, chat_id="c1", origin="direct")
        assert note == "" and repo.reads == [] and rec.names() == []
    with _env(YINYUE_FEATURE_PLAN_LOOP="on", YINYUE_FEATURE_PERSONA_CHANNEL="shadow"):
        with _patch_log(turn_pipeline) as rec:
            note = turn_pipeline._persona_plan_note(router, chat_id="c1", origin="direct")
        assert note == "" and repo.reads and "persona_channel_shadow" in rec.names()
    with _env(YINYUE_FEATURE_PLAN_LOOP="on", YINYUE_FEATURE_PERSONA_CHANNEL="on"):
        with _patch_log(turn_pipeline) as rec:
            note = turn_pipeline._persona_plan_note(router, chat_id="c1", origin="direct")
        assert note.startswith("[人设]") and "persona_channel_used" in rec.names()
    # PLAN_LOOP 不规划就不读（off 不变的另一半）
    repo2 = _Repo([_Row(1, "鲸鲸")])
    with _env(YINYUE_FEATURE_PLAN_LOOP="off", YINYUE_FEATURE_PERSONA_CHANNEL="on"):
        assert turn_pipeline._persona_plan_note(
            SimpleNamespace(_repo=repo2), chat_id="c1",
        ) == ""
        assert repo2.reads == []


def test_accept_b3_digest_has_no_prompt_body_and_note_bounded() -> None:
    secret = "秘密系统提示" * 40
    view = PersonaView(
        persona_id=7, name="鲸鲸", system_prompt=secret,
        traits="温柔" * 200, voice_config="短句" * 100,
    )
    digest = persona_digest(view)
    assert "system_prompt" not in digest
    assert digest["has_system_prompt"] is True
    dumped = json.dumps(digest, ensure_ascii=False)
    assert secret not in dumped and "秘密系统提示秘密系统提示" not in dumped
    note = persona_note(view)
    assert note.startswith("[人设] 名字：鲸鲸")
    assert "…" in note and len(note) < 240            # 摘要有界，不整段倾倒


# ---------------------------------------------------------------------------
# 条款 4：灰度无撤回（结构不变量 + 回滚位）
# ---------------------------------------------------------------------------

def test_accept_gray_rollout_safety_and_rollback() -> None:
    write_tools = {
        "send_message", "create_reminder", "notify_user", "remember",
        "send_sticker", "create_todo", "complete_todo", "emit_event",
    }
    assert not (write_tools & set(PLAN_LOOP_READ_ONLY_TOOLS))
    view = plan_exec_tools({"search_messages": object(), "send_message": object()})
    assert view.get("search_messages") is not None
    assert view.get("send_message") is None
    # 回滚位：三个 flag 全 off → 零动作、零读、工具不在默认表（重启即回接线前）
    with _env(
        YINYUE_FEATURE_PLAN_LOOP="off",
        YINYUE_FEATURE_PERSONA_CHANNEL="off",
    ):
        assert plan_loop_mode() == persona_channel_mode() == "off"
        repo = _Repo([_Row(1, "鲸鲸")])
        assert turn_pipeline._persona_plan_note(
            SimpleNamespace(_repo=repo), chat_id="c1",
        ) == "" and repo.reads == []
        assert "get_persona" not in ToolRegistry.create_default(
            repo=_Repo([_Row(1, "鲸鲸")]),
        ).all_tools()


TESTS = [
    test_accept_flags_default_off_and_registered,
    test_accept_b2_off_rollback_zero_trace,
    test_accept_b2_replay_deterministic,
    test_accept_b2_guards_write_tool_and_double_execution,
    test_accept_b3_tool_registration_gated_by_flag,
    test_accept_b3_off_zero_read_shadow_on_audit,
    test_accept_b3_digest_has_no_prompt_body_and_note_bounded,
    test_accept_gray_rollout_safety_and_rollback,
]


def main() -> int:
    failures = []
    for fn in TESTS:
        try:
            fn()
            print(f"  ok   {fn.__name__}")
        except Exception as exc:  # noqa: BLE001
            import traceback
            failures.append((fn.__name__, exc))
            print(f"  FAIL {fn.__name__}: {exc}")
            traceback.print_exc()
    print(f"batch-b acceptance checks: {len(TESTS) - len(failures)}/{len(TESTS)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
