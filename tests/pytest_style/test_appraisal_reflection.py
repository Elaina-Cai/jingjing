"""P4 appraisal/reflection contracts: evidence, revision, replay, choice."""
from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace

import pytest

from core.context import appraisal as ap
from core.context import cognitive_commit as cc
from core.context import reflection as rf
from core.terminal.event_model import Event, EventType


@pytest.fixture()
def db(tmp_path):
    conn = sqlite3.connect(tmp_path / "cognitive.db", isolation_level=None)
    conn.row_factory = sqlite3.Row
    cc.ensure_schema(conn)
    yield conn
    conn.close()


SCOPE = cc.Scope("wechat", "acct", "chat-1")


def _event_count(conn, subject_key):
    row = conn.execute(
        "SELECT COUNT(*) FROM cognitive_events WHERE subject_key=?", (subject_key,)
    ).fetchone()
    return int(row[0])


def test_appraisal_binds_source_goal_and_revision(db):
    result = ap.record_appraisal(
        db,
        scope=SCOPE,
        subject_key="appraisal:promise-1",
        trigger_refs=("evt-1", "evt-2"),
        goal_refs=("goal:promise-1",),
        value_refs=("value:care",),
        meaning="答应找的资料链接失效了",
        feeling_text="有点挫败",
        tendency="换一个来源",
        unresolved_condition="链接仍不可用",
        review_at="2026-11-09 10:00",
    )
    assert result.ok and len(result.committed) == 1

    current = ap.current_appraisal(db, subject_key="appraisal:promise-1", scope=SCOPE)
    assert current is not None
    assert current.meaning == "答应找的资料链接失效了"
    assert current.trigger_refs == ("evt-1", "evt-2")
    assert current.goal_refs == ("goal:promise-1",)
    assert current.value_refs == ("value:care",)
    assert current.revision == 1

    state = cc.read_state(db, "appraisal:promise-1", scope=SCOPE)
    assert state is not None and state["operation"] == "appraise"
    row = db.execute(
        "SELECT evidence_refs FROM cognitive_events WHERE subject_key=?",
        ("appraisal:promise-1",),
    ).fetchone()
    assert json.loads(row["evidence_refs"]) == ["evt-1", "evt-2"]


def test_replay_same_experience_100_times_never_accumulates(db):
    first = ap.record_appraisal(
        db, scope=SCOPE, subject_key="appraisal:a", trigger_refs=("evt-1",),
        meaning="一次评价",
    )
    assert first.ok
    for _ in range(99):
        again = ap.record_appraisal(
            db, scope=SCOPE, subject_key="appraisal:a", trigger_refs=("evt-1",),
            meaning="一次评价",
        )
        assert again.ok and again.committed == ()
        assert again.duplicates

    assert _event_count(db, "appraisal:a") == 1
    assert ap.current_appraisal(db, subject_key="appraisal:a", scope=SCOPE).revision == 1


def test_supersedes_revises_current_and_keeps_history(db):
    first = ap.record_appraisal(
        db, scope=SCOPE, subject_key="appraisal:a", trigger_refs=("evt-1",),
        meaning="旧解释",
    )
    old = ap.current_appraisal(db, subject_key="appraisal:a", scope=SCOPE)
    assert first.ok and old is not None

    revised = ap.record_appraisal(
        db, scope=SCOPE, subject_key="appraisal:a", trigger_refs=("evt-2",),
        meaning="新解释", supersedes=old.event_id,
    )
    assert revised.ok and revised.revisions["appraisal:a"] == 2

    current = ap.current_appraisal(db, subject_key="appraisal:a", scope=SCOPE)
    assert current is not None and current.meaning == "新解释"
    assert current.revision == 2 and current.event_id != old.event_id
    assert _event_count(db, "appraisal:a") == 2

    # replaying the revision is idempotent; a stale supersedes is refused.
    replay = ap.record_appraisal(
        db, scope=SCOPE, subject_key="appraisal:a", trigger_refs=("evt-2",),
        meaning="新解释", supersedes=old.event_id,
    )
    assert replay.ok and replay.duplicates and _event_count(db, "appraisal:a") == 2
    stale = ap.record_appraisal(
        db, scope=SCOPE, subject_key="appraisal:a", trigger_refs=("evt-3",),
        meaning="分支", supersedes=old.event_id,
    )
    assert not stale.ok and stale.rejected[0][1] == "supersedes_not_current"


def _topic_event(event_id, text, *, actor="first", scope=SCOPE, parent=""):
    return Event.new(event_id=event_id, event_type=EventType.MESSAGE, platform=scope.platform,
                     bot_account_id=scope.account_id, chat_id=scope.chat_id, actor_account_id=actor,
                     parent_event_id=parent, content=text)


def _seed_named_topic(conn, message, *, scope=SCOPE):
    topic = ap.resolve_topic(conn, None, scope, message)
    assert topic.name
    saved = ap.record_appraisal(conn, scope=scope, subject_key=topic.subject,
                                trigger_refs=(message.event_id,), topic_name=topic.name, meaning="首次判断")
    assert saved.ok
    return topic


def test_bare_name_resolves_only_to_its_owners_subject_in_scope(db):
    first = _seed_named_topic(db, _topic_event("first-1", "《候鸟手册》需要核对"))
    message = _topic_event("first-2", "关于候鸟手册，我拿到了目录。")
    assert ap.resolve_topic(db, None, SCOPE, message).subject == first.subject

    stranger = _topic_event("second-1", "候鸟手册还在修订。", actor="second")
    assert not ap.resolve_topic(db, None, SCOPE, stranger).name
    second = _seed_named_topic(db, _topic_event("second-2", "《候鸟手册》需要修订", actor="second"))
    assert second.subject != first.subject
    assert ap.resolve_topic(db, None, SCOPE, stranger).subject == second.subject
    assert ap.resolve_topic(db, None, SCOPE, message).subject == first.subject

    other_scope = cc.Scope("wechat", "other-account", SCOPE.chat_id)
    same_person = _topic_event("other-1", message.content, scope=other_scope)
    assert not ap.resolve_topic(db, None, other_scope, same_person).name


def test_bare_name_distinguishes_ascii_words_and_leaves_short_titles_to_markup(db):
    technical = _seed_named_topic(db, _topic_event("tool-1", "关于`AgentBox`"))
    _seed_named_topic(db, _topic_event("short-1", "《活着》"))
    valid = _topic_event("tool-2", "AgentBox和AgentBox都已验证。")
    assert ap.resolve_topic(db, None, SCOPE, valid).subject == technical.subject
    for text in ("xAgentBox", "AgentBox2", "AgentBox_dev", "agentbox", "人总得活着"):
        assert not ap.resolve_topic(db, None, SCOPE, _topic_event("other", text)).name


def test_names_competing_in_a_plain_mention_or_quote_leave_it_unbound(db):
    source = _topic_event("long-1", "《候鸟手册》")
    short = _seed_named_topic(db, source)
    extended = _seed_named_topic(db, _topic_event("long-2", "《候鸟手册增补》"))
    _seed_named_topic(db, _topic_event("other-1", "《海盐来信》"))
    store = SimpleNamespace(get={source.event_id: source}.get)
    for text in ("候鸟手册增补可以读了", "候鸟手册和海盐来信怎么选", "《候鸟手册》还是海盐来信？"):
        assert not ap.resolve_topic(db, store, SCOPE, _topic_event("comparison", text)).name
    assert ap.resolve_topic(db, store, SCOPE, _topic_event("marked", "《候鸟手册增补》")).subject == extended.subject

    quoted = _topic_event("reply", "继续核对吧", parent=source.event_id)
    assert ap.resolve_topic(db, store, SCOPE, quoted).subject == short.subject
    conflicting = _topic_event("reply-2", "海盐来信还得看一遍", parent=source.event_id)
    assert not ap.resolve_topic(db, store, SCOPE, conflicting).name


def test_bare_mention_declines_when_lookup_limits_are_exceeded(db, monkeypatch):
    _seed_named_topic(db, _topic_event("long-1", "《候鸟手册》"))
    _seed_named_topic(db, _topic_event("long-2", "《候鸟手册增补》"))
    warnings = []
    monkeypatch.setattr(ap, "warn", lambda event, **fields: warnings.append(event))
    monkeypatch.setattr(ap, "_PLAIN_MENTION_MAX_CANDIDATES", 1)
    assert not ap.resolve_topic(db, None, SCOPE, _topic_event("too-many", "候鸟手册")).name
    long_message = _topic_event("too-long", "候鸟手册" + "常" * ap._PLAIN_MENTION_MAX_CHARS)
    assert not ap.resolve_topic(db, None, SCOPE, long_message).name
    assert warnings == ["appraisal_plain_mention_candidates_exhausted", "appraisal_plain_mention_too_long"]


def test_appraisal_requires_trigger_and_meaning(db):
    with pytest.raises(ValueError, match="appraisal_trigger_required"):
        ap.record_appraisal(db, scope=SCOPE, subject_key="a", trigger_refs=(), meaning="x")
    with pytest.raises(ValueError, match="appraisal_meaning_required"):
        ap.record_appraisal(db, scope=SCOPE, subject_key="a", trigger_refs=("e",), meaning=" ")


def test_unrelated_appraisal_and_other_scope_do_not_change_current(db):
    ap.record_appraisal(
        db, scope=SCOPE, subject_key="appraisal:a", trigger_refs=("evt-1",), meaning="A"
    )
    before = ap.current_appraisal(db, subject_key="appraisal:a", scope=SCOPE)
    ap.record_appraisal(
        db, scope=SCOPE, subject_key="appraisal:b", trigger_refs=("evt-9",), meaning="B"
    )
    after = ap.current_appraisal(db, subject_key="appraisal:a", scope=SCOPE)
    assert before == after

    other = cc.Scope("wechat", "acct", "chat-2")
    assert ap.current_appraisal(db, subject_key="appraisal:a", scope=other) is None


def test_render_and_focus_only_use_current_appraisal(db):
    ap.record_appraisal(
        db, scope=SCOPE, subject_key="appraisal:a", trigger_refs=("evt-1",),
        meaning="资料链接失效", feeling_text="挫败", tendency="换来源",
        unresolved_condition="链接仍不可用",
    )
    current = ap.current_appraisal(db, subject_key="appraisal:a", scope=SCOPE)
    text = ap.render_appraisal(current)
    assert "我的理解：资料链接失效" in text
    assert "未解：链接仍不可用" in text
    assert ap.focus_hint(current).startswith("先处理：")
    assert ap.render_appraisal(None) == ""
    assert ap.focus_hint(None) == ""


def test_reflection_replay_and_correction_change_method_choice(db):
    rec = rf.record_lesson(
        db, scope=SCOPE, method="search", outcome="success",
        evidence_refs=("evt-1",), lesson="先检索再回答", confidence=0.9,
    )
    assert rec.ok
    a = rf.current_lesson(db, method="search", scope=SCOPE)
    assert a is not None and a.revision == 1
    assert rf.select_method(db, methods=["search", "ask"], scope=SCOPE) == "search"

    rf.record_lesson(
        db, scope=SCOPE, method="ask", outcome="success",
        evidence_refs=("evt-2",), lesson="先问再查", confidence=0.8,
    )
    assert rf.select_method(db, methods=["search", "ask"], scope=SCOPE) == "search"

    corrected = rf.record_lesson(
        db, scope=SCOPE, method="search", outcome="failure",
        evidence_refs=("evt-3",), lesson="检索源失效时不要硬查", confidence=0.9,
        supersedes=a.event_id,
    )
    assert corrected.ok
    current = rf.current_lesson(db, method="search", scope=SCOPE)
    assert current is not None and current.outcome == "failure" and current.revision == 2
    assert rf.select_method(db, methods=["search", "ask"], scope=SCOPE) == "ask"


def test_reflection_replay_100_times_never_accumulates(db):
    first = rf.record_lesson(
        db, scope=SCOPE, method="search", outcome="success",
        evidence_refs=("evt-1",), lesson="有效", confidence=0.7,
    )
    assert first.ok
    for _ in range(99):
        again = rf.record_lesson(
            db, scope=SCOPE, method="search", outcome="success",
            evidence_refs=("evt-1",), lesson="有效", confidence=0.7,
        )
        assert again.ok and again.committed == () and again.duplicates
    assert _event_count(db, "method:search") == 1


def test_reflection_rejects_missing_evidence_and_bad_outcome(db):
    with pytest.raises(ValueError, match="reflection_evidence_required"):
        rf.record_lesson(db, scope=SCOPE, method="m", outcome="success",
                         evidence_refs=(), lesson="x")
    with pytest.raises(ValueError, match="outcome_invalid"):
        rf.record_lesson(db, scope=SCOPE, method="m", outcome="maybe",
                         evidence_refs=("e",), lesson="x")


def test_render_lesson_marks_outcome_and_caps_confidence(db):
    rf.record_lesson(
        db, scope=SCOPE, method="search", outcome="success",
        evidence_refs=("evt-1",), lesson="先检索", confidence=9.0,
    )
    lesson = rf.current_lesson(db, method="search", scope=SCOPE)
    assert lesson is not None and lesson.confidence == 1.0
    text = rf.render_lesson(lesson)
    assert "有效" in text and "置信度 1.00" in text
    assert rf.render_lesson(None) == ""



def _db_path(conn) -> str:
    row = conn.execute("PRAGMA database_list").fetchone()
    return str(row[2])


def test_prompt_block_reads_current_scope_only(db, monkeypatch):
    monkeypatch.setenv("YINYUE_COGNITIVE_DB", _db_path(db))
    ap.record_appraisal(
        db,
        scope=SCOPE,
        subject_key="chat:chat-1",
        trigger_refs=("evt-1",),
        meaning="这件事很重要",
        tendency="先确认",
    )
    text = ap.render_appraisal_for(SCOPE, "chat:chat-1")
    assert "我的理解：这件事很重要" in text
    assert "可尝试：先确认" in text
    assert ap.render_appraisal_for(cc.Scope("wechat", "acct", "chat-2"), "chat:chat-1") == ""


def test_scope_lessons_are_read_only_and_ranked(db, monkeypatch):
    monkeypatch.setenv("YINYUE_COGNITIVE_DB", _db_path(db))
    rf.record_lesson(
        db, scope=SCOPE, method="search", outcome="success",
        evidence_refs=("evt-1",), lesson="先检索", confidence=0.9,
    )
    rf.record_lesson(
        db, scope=SCOPE, method="ask", outcome="failure",
        evidence_refs=("evt-2",), lesson="别追问", confidence=0.8,
    )
    lessons = rf.current_lessons_for_scope(SCOPE)
    assert {lesson.method for lesson in lessons} == {"search", "ask"}
    assert rf.rank_methods(lessons, ["ask", "search"]) == "search"
    assert rf.rank_methods(lessons, ["ask"]) == ""


def test_planner_reflection_hint_puts_evidence_first(monkeypatch):
    from agents.planner import Planner

    class _Ctx:
        extra = {"platform": "wechat", "account_id": "acct", "chat_id": "chat-1"}

    planner = Planner(None, {})
    planner.set_context(_Ctx())
    lesson = rf.Lesson("e", "search", "success", ("x",), "先检索", 0.9, 1)
    monkeypatch.setattr(rf, "current_lessons_for_scope", lambda scope: (lesson,))
    text = planner._reflection_hint([("search", 0.9), ("other", 0.1)])
    assert "优先：" in text and "search" in text



def test_brain_appraisal_block_requires_current_proof(db, monkeypatch):
    monkeypatch.setenv("YINYUE_COGNITIVE_DB", _db_path(db))
    ap.record_appraisal(
        db,
        scope=SCOPE,
        subject_key="chat:chat-1",
        trigger_refs=("evt-1",),
        meaning="需要先确认再回答",
        unresolved_condition="还缺一条证据",
    )
    from loop import brain
    from loop.ports import ReadScope

    scope = ReadScope("wechat", "acct", "chat-1", "", "")
    assert brain._appraisal_block("wechat", "chat-1", scope) == ""
    assert brain._appraisal_block("wechat", "chat-2", scope,
                                  visible_event_ids=("evt-1",)) == ""


def test_planner_experience_hint_merges_method_lessons(monkeypatch):
    from agents.planner import Planner

    class _Ctx:
        extra = {"platform": "wechat", "account_id": "acct", "chat_id": "chat-1"}

    planner = Planner(None, {})
    planner.set_context(_Ctx())
    lesson = rf.Lesson("e", "search", "success", ("x",), "先检索", 0.9, 1)
    monkeypatch.setattr(rf, "current_lessons_for_scope", lambda scope: (lesson,))
    text = planner._experience_hint("查一下")
    assert "[方法经验]" in text
    assert "优先：" in text and "search" in text



def test_brain_wires_appraisal_block_without_stealing_scoped_turn():
    from pathlib import Path

    src = (Path(__file__).resolve().parents[2] / "loop" / "brain.py").read_text(encoding="utf-8")
    assert "@ports.scoped_turn\nasync def run_loop(" in src
    assert '("我对当前局面的评价", _app_text)' in src

def test_experience_hint_orders_evidenced_method_before_weaker_tool(monkeypatch):
    from agents.planner import Planner

    class _Ctx:
        extra = {"platform": "wechat", "account_id": "acct", "chat_id": "chat-1"}

    class _Evolve:
        def _extract_pattern(self, message):
            return "p"

        def suggest_tools_for_pattern(self, pattern):
            return [("ask", 0.9), ("search", 0.8)]

    planner = Planner(None, {}, evolve=_Evolve())
    planner.set_context(_Ctx())
    lessons = (
        rf.Lesson("e1", "search", "success", ("x",), "先检索", 0.9, 1),
        rf.Lesson("e2", "ask", "failure", ("y",), "别追问", 0.8, 1),
    )
    monkeypatch.setattr(rf, "current_lessons_for_scope", lambda scope: lessons)
    text = planner._experience_hint("查一下")
    history = next(line for line in text.splitlines() if line.startswith("[历史经验"))
    assert history.index("search") < history.index("ask")
    assert "优先：" in text and "search" in text



def test_reflection_revision_replay_does_not_fail_head_check_or_accumulate(db):
    first = rf.record_lesson(db, scope=SCOPE, method="lookup", outcome="failure", evidence_refs=("e1",), lesson="旧方法失效")
    assert first.ok
    head = rf.current_lesson(db, method="lookup", scope=SCOPE)
    args = dict(scope=SCOPE, method="lookup", outcome="success", evidence_refs=("e2",), lesson="新来源有效", supersedes=head.event_id)
    revised = rf.record_lesson(db, **args)
    assert revised.ok
    for _ in range(100):
        replay = rf.record_lesson(db, **args)
        assert replay.ok and replay.duplicates and not replay.committed
    assert rf.current_lesson(db, method="lookup", scope=SCOPE).revision == 2
    # A reused identity with changed contents is still a conflict.
    changed = rf.record_lesson(db, **dict(args, lesson="另一个内容"))
    assert not changed.ok
