"""Actual run_loop writes/reads evidence-bound interpretations using a scripted model."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import datetime
from types import SimpleNamespace

import json

import pytest

from core.context import appraisal as ap, reflection as rf, cognitive_commit as cc, experience_commit as experience
from core.context import situation, situation_store
from core.terminal.event_model import Event, EventType
from core.terminal import shadow
from test_appraisal_prompt_integration import runtime, Model, drive, _real_self_render

SCOPE = cc.Scope("wechat", "acct", "chat-1")


@pytest.fixture()
def live(runtime, monkeypatch):
    brain, tmp = runtime
    sources = {}
    for ref in ("e1", "e2"):
        sources[ref] = Event.new(event_id=ref, event_type=EventType.MESSAGE,
            occurred_at=datetime(2026, 11, 8, 16), platform="wechat", bot_account_id="acct", chat_id="chat-1",
            actor_person_id="person-a", content="这次检索有明确来源，方法有效。" if ref == "e1" else "后来证实来源不可靠，需要修订。")
    monkeypatch.setattr(shadow, "get_default_store", lambda: SimpleNamespace(get=sources.get))
    account = tmp / "platforms/wechat/acct"
    account.mkdir(parents=True)
    return SimpleNamespace(brain=brain, tmp=tmp, account=account / "account.db", sources=sources)


def run(live, args, *, dry=False, rehearse=False, visible=("e1",), scope=SCOPE, sender="person-a", anchor=None):
    model = Model([{"content": "", "tool_calls": [{"id": "call-1", "name": "evaluate_experience", "arguments": args}]},
                   {"content": "", "tool_calls": []}])
    receipts = drive(live.brain.run_loop(str(live.account), "外部反馈原文", llm=model,
        dry_run=dry, rehearse=rehearse, platform=scope.platform, chat_id=scope.chat_id, sender_id=sender,
        visible_event_ids=visible, appraisal_anchor=anchor, max_steps=2))
    return model, next(r for r in receipts if r.get("act") == "evaluate_experience")


def prompt_for(live, ref, *, scope=SCOPE, sender="person-a"):
    model = Model([{"content": "", "tool_calls": []}])
    drive(live.brain.run_loop(str(live.account), live.sources[ref].content, llm=model, dry_run=True,
          platform=scope.platform, chat_id=scope.chat_id, sender_id=sender, visible_event_ids=(ref,), max_steps=1))
    return model.prompts[0][1]["content"]


def appraisal_args(ref="e1", **extra):
    return dict({"mode": "appraisal", "evidence_ref": ref, "meaning": "这一次方法获得相关反馈支持", "tendency": "继续核实来源"}, **extra)


def test_real_loop_generates_appraisal_then_next_turn_reads_it(live):
    model, receipt = run(live, appraisal_args())
    assert receipt["result"] == "committed"
    assert "evaluate_experience" in model.prompts[0][1]["content"] and "e1:" in model.prompts[0][1]["content"]
    # No extra classification/model chain: only the two scripted loop responses.
    assert len(model.prompts) == 2
    live.sources["e2"].parent_event_id = "e1"
    assert "这一次方法获得相关反馈支持" in prompt_for(live, "e2")
    conn = cc.connect(str(live.tmp / "cognitive.db"))
    try:
        subject, actor, source = conn.execute("SELECT subject_key,actor_key,source FROM cognitive_events").fetchone()
        current = ap.current_appraisal(conn, subject_key=subject, scope=SCOPE)
        assert current.trigger_refs == ("e1",)
        assert (actor, source) == ("person-a", "experience_tool")
    finally:
        conn.close()


@pytest.mark.parametrize("kind", [
    EventType.QUESTION,
    EventType.REQUEST,
    EventType.COMMAND,
])
def test_all_human_speech_types_can_support_an_appraisal(live, kind):
    live.sources["e1"].event_type = kind
    _, receipt = run(live, appraisal_args())
    assert receipt["result"] == "committed"


def test_experience_offer_excludes_a_goal_with_revoked_source(live, monkeypatch):
    from core.context.situation_store import get_world_model
    from core.memory import belief_commit

    model = get_world_model("chat-1", scope=SCOPE)
    situation.open_intention(
        model,
        "revoked-goal",
        kind="act",
        at="2026-11-08 16:00",
        evidence_refs=("revoked-request",),
    )
    situation.open_intention(
        model,
        "unknown-goal",
        kind="act",
        at="2026-11-08 16:00",
        evidence_refs=("missing-request",),
    )
    monkeypatch.setattr(
        belief_commit,
        "evidence_state",
        lambda ref, scope: "dead" if ref == "revoked-request" else "unknown",
    )

    offers = experience.offers(SCOPE, frozenset({"e1"}))

    assert offers
    assert all("revoked-goal" not in offer.goals for offer in offers.values())
    assert all("unknown-goal" in offer.goals for offer in offers.values())


def test_action_result_can_update_appraisal_in_same_turn(live, monkeypatch):
    sent = []
    traces = []

    async def read_file(name, args):
        assert (name, args) == ("read_file", {"path": "docs/b.md"})
        return live.brain.ports.Result(True, text="B 稿第一步：确认展板给谁看。")

    async def say_many(parts, **kwargs):
        sent.extend(parts)
        return live.brain.ports.Result(True, data={"ack": "test"})

    monkeypatch.setattr(live.brain.ports, "act", read_file)
    monkeypatch.setattr(live.brain.ports, "say_many", say_many)
    monkeypatch.setattr(live.brain.ports, "resolve_say_target", lambda target: (target, ""))
    monkeypatch.setattr(live.brain.ports, "resolve_chat_id", lambda target: target)
    monkeypatch.setattr(live.brain.selfstate, "touch_said", lambda *args: None)
    monkeypatch.setattr(
        live.brain.trace_mod.TurnTrace,
        "write",
        lambda self: traces.append(self.rec) or "",
    )
    model = Model([
        {"content": "", "tool_calls": [
            {"id": "read", "name": "read_file", "arguments": {"path": "docs/b.md"}}]},
        {"content": "", "tool_calls": [
            {"id": "appraise", "name": "evaluate_experience", "arguments": {
                "mode": "appraisal", "evidence_ref": "e1",
                "meaning": "读过 B 稿后，我确认先问清展板给谁看",
                "feeling_text": "心里踏实了一点", "tendency": "按这个方向继续"}}]},
        {"content": "", "tool_calls": [
            {"id": "reply", "name": "say", "arguments": {
                "target": SCOPE.chat_id, "text": "B 稿第一步是确认展板给谁看。"}}]},
    ])
    receipts = drive(live.brain.run_loop(
        str(live.account), "先读 B 稿再决定",
        llm=model, dry_run=False, rehearse=False, platform=SCOPE.platform,
        chat_id=SCOPE.chat_id, sender_id="person-a",
        visible_event_ids=("e1",), max_steps=4,
    ))

    assert [(row["act"], row["result"]) for row in receipts
            if row["act"] in {"do", "evaluate_experience", "say"}] == [
                ("do", "ok"), ("evaluate_experience", "committed"), ("say", "sent"),
            ]
    assert sent == ["B 稿第一步是确认展板给谁看。"]
    observation = next(message["content"] for message in model.prompts[1]
                       if message.get("role") == "tool")
    assert "B 稿第一步：确认展板给谁看" in observation
    assert "动作结果可按需写回理解" in observation
    judgement = [message["content"] for message in model.prompts[2]
                 if message.get("role") == "tool"][-1]
    assert "[我现在对这件事的判断]" in judgement
    assert "我的理解：读过 B 稿后，我确认先问清展板给谁看" in judgement
    assert "我的情绪：心里踏实了一点" in judgement
    assert "接下来我想：按这个方向继续" in judgement
    assert "这条判断会影响我接下来对谁说、具体怎么说、是否追问、等待，或先做什么" in judgement
    conn = cc.connect(str(live.tmp / "cognitive.db"))
    try:
        subject = conn.execute(
            "SELECT subject_key FROM cognitive_events WHERE operation='appraise'"
        ).fetchone()[0]
        current = ap.current_appraisal(conn, subject_key=subject, scope=SCOPE)
        assert current.meaning == "读过 B 稿后，我确认先问清展板给谁看"
        assert current.feeling_text == "心里踏实了一点"
        assert current.tendency == "按这个方向继续"
        assert len(current.action_refs) == 1
        assert current.action_refs[0].startswith("turn:")
        assert ":step:1:read_file:ok" in current.action_refs[0]
        assert traces[-1]["steps"][0]["receipt"]["action_ref"] == current.action_refs[0]
        assert traces[-1]["steps"][1]["tool"] == "evaluate_experience"
        assert traces[-1]["steps"][1]["receipt"]["action_refs"] == list(current.action_refs)
    finally:
        conn.close()


def test_action_result_does_not_advertise_unoffered_appraisal(live, monkeypatch):
    async def read_file(name, args):
        return live.brain.ports.Result(True, text="文件内容")

    monkeypatch.setattr(live.brain.ports, "act", read_file)
    model = Model([
        {"content": "", "tool_calls": [
            {"id": "read", "name": "read_file", "arguments": {"path": "docs/b.md"}}]},
        {"content": "", "tool_calls": []},
    ])
    drive(live.brain.run_loop(
        str(live.account), "自己先看看文件",
        llm=model, dry_run=True, rehearse=False, platform=SCOPE.platform,
        chat_id=SCOPE.chat_id, max_steps=2,
    ))

    observation = next(message["content"] for message in model.prompts[1]
                       if message.get("role") == "tool")
    assert "文件内容" in observation
    assert "动作结果可按需写回理解" not in observation


def test_current_turn_anchor_is_the_only_appraisal_source_offered(live):
    offers = experience.offers(
        SCOPE, frozenset({"e1", "e2"}), ("e2", ""),
    )
    assert list(offers) == ["e2"]


def test_offer_prefers_human_display_name_over_account_id(live):
    # 模型认识人名，不认识 wxid；有可读称呼时提示里不该只剩账号 ID。
    live.sources["e1"].metadata = {"principal": {"display_name": "P3"}, "sender_name": ""}
    offer = experience.offers(SCOPE, frozenset({"e1"}))["e1"]
    assert offer.speaker == "P3"
    text = experience.render({"e1": offer})
    assert '发言人 "P3"' in text
    assert '发言人 "person-a"' not in text

    # sender_name 比 principal.display_name 更具体，应优先。
    live.sources["e1"].metadata = {"sender_name": "P6", "principal": {"display_name": "P3"}}
    offer = experience.offers(SCOPE, frozenset({"e1"}))["e1"]
    assert offer.speaker == "P6"


def test_offer_falls_back_to_actor_id_without_display_name(live):
    live.sources["e1"].metadata = {}
    offer = experience.offers(SCOPE, frozenset({"e1"}))["e1"]
    assert offer.speaker == "person-a"


def test_related_named_requests_are_offered_for_separate_appraisal(live):
    grants = experience.offers(
        SCOPE, frozenset({"e1", "e2"}), ("e2", ""), related_refs=("e1", "missing", "e1"),
    )
    assert list(grants) == ["e2", "e1"]


def test_run_loop_lists_related_request_in_appraisal_enum(live):
    model = Model([{"content": "", "tool_calls": []}])
    drive(live.brain.run_loop(
        str(live.account), live.sources["e2"].content, llm=model, dry_run=True,
        platform=SCOPE.platform, chat_id=SCOPE.chat_id, sender_id="person-a",
        visible_event_ids=("e1", "e2"), appraisal_anchor=("e2", ""),
        appraisal_related_refs=("e1",), max_steps=1,
    ))
    schema = next(tool["function"]["parameters"] for tool in model.tool_schemas[0]
                  if tool["function"]["name"] == "evaluate_experience")
    enum = schema["properties"]["evidence_ref"]["enum"]
    assert "e2" in enum and "e1" in enum
    prompt = "\n".join(str(message.get("content") or "") for message in model.prompts[0])
    assert "e2" in prompt and "e1" in prompt


def test_two_named_requests_can_be_appraised_then_replied_to_together(live, monkeypatch):
    sent = []

    async def say_many(parts, **kwargs):
        sent.extend(parts)
        return live.brain.ports.Result(True, data={"ack": "test"})

    monkeypatch.setattr(live.brain.ports, "say_many", say_many)
    monkeypatch.setattr(live.brain.ports, "resolve_say_target", lambda target: (target, ""))
    monkeypatch.setattr(live.brain.selfstate, "touch_said", lambda *args: None)
    model = Model([
        {"content": "", "tool_calls": [{"id": "appraise-second", "name": "evaluate_experience", "arguments": {
            "mode": "appraisal", "evidence_ref": "e2", "meaning": "小岚想让我先核对这件事",
            "feeling_text": "认真起来了", "tendency": "先把关键点说清"}}]},
        {"content": "", "tool_calls": [{"id": "appraise-first", "name": "evaluate_experience", "arguments": {
            "mode": "appraisal", "evidence_ref": "e1", "meaning": "小池更需要我先听他讲完",
            "feeling_text": "想放慢一点", "tendency": "先回应他，再解释"}}]},
        {"content": "", "tool_calls": [{"id": "reply-both", "name": "say", "arguments": {
            "target": SCOPE.chat_id, "text": "小岚，关键点我先核对。小池，你先说，我听着。"}}]},
    ])
    receipts = drive(live.brain.run_loop(
        str(live.account), "小池和小岚分别点名", llm=model, dry_run=False, rehearse=False,
        platform=SCOPE.platform, chat_id=SCOPE.chat_id, sender_id="person-a",
        visible_event_ids=("e1", "e2"), appraisal_anchor=("e2", ""),
        appraisal_related_refs=("e1",), max_steps=4,
    ))

    assert [(row["act"], row["result"]) for row in receipts] == [
        ("evaluate_experience", "committed"),
        ("evaluate_experience", "committed"),
        ("say", "sent"),
    ]
    assert sent == ["小岚，关键点我先核对。小池，你先说，我听着。"]
    conn = cc.connect(str(live.tmp / "cognitive.db"))
    try:
        subjects = [row[0] for row in conn.execute(
            "SELECT subject_key FROM cognitive_events WHERE operation='appraise'")]
        heads = [ap.current_appraisal(conn, subject_key=subject, scope=SCOPE)
                 for subject in subjects]
        assert {head.meaning for head in heads if head is not None} == {
            "小岚想让我先核对这件事", "小池更需要我先听他讲完",
        }
    finally:
        conn.close()


def test_cross_author_quote_is_readable_and_keeps_both_appraisals(live):
    scope = cc.Scope("wechat", "acct", "reviewers@chatroom")
    first = "甲还在核对署名"
    second = "乙已经拿到可核对的署名页"
    latest = "乙又找到了作者签字的版本"
    original = replace(live.sources["e1"], chat_id=scope.chat_id, content="《候鸟手册》的署名还在核对。")
    live.sources["e1"] = original
    live.sources["e2"] = replace(original, event_id="e2", actor_person_id="person-b",
                                parent_event_id="e1", content="我这里找到署名页了。")
    live.sources["e3"] = replace(live.sources["e2"], event_id="e3", parent_event_id="",
                                content="《候鸟手册》找到了作者签字的版本。")
    live.sources["e4"] = replace(original, event_id="e4", parent_event_id="e2", content="这份哪里看到的？")
    live.sources["e5"] = replace(original, event_id="e5", content="《候鸟手册》的署名有进展吗？")

    assert run(live, appraisal_args(meaning=first), scope=scope)[1]["result"] == "committed"
    model, receipt = run(live, appraisal_args("e2", meaning=second), scope=scope, visible=("e2",), sender="person-b")
    assert receipt["result"] == "committed"
    assert first in model.prompts[0][1]["content"]
    model, receipt = run(live, appraisal_args("e3", meaning=latest), scope=scope, visible=("e3",), sender="person-b")
    assert receipt["result"] == "committed"
    assert second in model.prompts[0][1]["content"]
    quoted = prompt_for(live, "e4", scope=scope)
    named = prompt_for(live, "e5", scope=scope)
    assert latest in quoted and first not in quoted
    assert first in named and latest not in named


@pytest.mark.parametrize("title", ["《候鸟手册》", ""])
def test_repeated_cross_author_quote_uses_the_current_speakers_revision(live, monkeypatch, title):
    scope = cc.Scope("wechat", "acct", "reviewers@chatroom")
    original = replace(live.sources["e1"], chat_id=scope.chat_id,
                       content=title + "还差一页署名，帮我翻一下。")
    correction = replace(original, event_id="e2", actor_person_id="person-b",
                         content="我这边暂时不用查稿。我就是有点难过，想找你说几句。")
    followup = replace(correction, event_id="e3", content="还是刚才那件事，你在吗？")
    live.sources.update(e1=original, e2=correction, e3=followup)
    monkeypatch.setattr(shadow, "get_default_store", lambda: SimpleNamespace(
        get=live.sources.get,
        get_by_external_msg_id=lambda **coordinates: original if coordinates == {
            "platform": scope.platform, "bot_account_id": scope.account_id,
            "chat_id": scope.chat_id, "external_msg_id": "msg-a",
        } else None,
    ))
    reads = []

    async def read_file(name, args):
        reads.append((name, args))
        return live.brain.ports.Result(True, text="待核对的署名页")

    class FollowUpModel:
        async def chat_tools(self, *, messages, **kwargs):
            prompt = messages[-1]["content"]
            if "有点歉意" in prompt and "先听她说完" in prompt:
                name = "say"
                args = {"target": scope.chat_id, "text": "我在。刚才我急着查稿了，你慢慢说。"}
            else:
                assert "甲想核对署名" in prompt
                name = "read_file"
                args = {"path": "docs/署名.md"}
            return {"tool_calls": [{"id": "choice", "name": name, "arguments": args}]}

    monkeypatch.setattr(live.brain.ports, "act", read_file)
    monkeypatch.setattr(live.brain.ports, "resolve_chat_id", lambda target: target)
    model = FollowUpModel()

    def answer_followup():
        return drive(live.brain.run_loop(
            str(live.account), followup.content, llm=model, max_steps=1,
            dry_run=True, rehearse=True, platform=scope.platform, chat_id=scope.chat_id,
            focus=scope.chat_id, sender_id="person-b", visible_event_ids=("e3",),
            appraisal_anchor=("e3", "msg-a"),
        ))[0]

    assert run(live, appraisal_args(meaning="甲想核对署名"), scope=scope)[1]["result"] == "committed"
    assert answer_followup()["act"] == "do"
    _, revised = run(live, appraisal_args("e2", meaning="乙现在只是想有人听",
                     feeling_text="有点歉意", tendency="先听她说完"),
                     scope=scope, visible=("e2",), sender="person-b", anchor=("e2", "msg-a"))
    assert revised["result"] == "committed"
    reply = answer_followup()
    assert (reply["act"], reply["result"], reply["text"]) == (
        "say", "rehearsal", "我在。刚才我急着查稿了，你慢慢说。",
    )
    assert reads == [("read_file", {"path": "docs/署名.md"})]


def test_short_continuation_reads_and_revises_the_appraisal_of_the_named_work(live, monkeypatch):
    original = replace(live.sources["e1"], content="《候鸟手册》还得核对署名。")
    initial = replace(original, event_id="e2", content="@鲸鲸 再查一下《候鸟手册》的署名。")
    middle = replace(original, event_id="mid", content="第一版发错了。",
                     occurred_at=original.occurred_at.replace(second=3))
    latest = replace(original, event_id="e3", content="只看第二版。",
                     occurred_at=original.occurred_at.replace(second=5))
    later = replace(original, event_id="e4", content="《候鸟手册》那件事现在怎么样？",
                    occurred_at=original.occurred_at.replace(minute=1))
    stranger = replace(initial, event_id="other", actor_person_id="person-b")
    live.sources.update(e1=original, e2=initial, mid=middle, e3=latest, e4=later, other=stranger)
    old = "我对第一版的署名还不放心"
    current = "第二版署名明确，这次的问题解决了"
    _, seeded = run(live, appraisal_args(meaning=old, tendency="先查署名"))
    assert seeded["result"] == "committed"
    reads, replies = [], []
    path = "docs/bird-handbook-v2.md"

    async def act(name, args):
        reads.append((name, args["path"]))
        return live.brain.ports.Result(True, text="第二版署名是小汐。")

    async def say_many(parts, **kwargs):
        replies.extend(parts)
        return live.brain.ports.Result(True, data={"ack": "queued"})

    monkeypatch.setattr(live.brain.ports, "act", act)
    monkeypatch.setattr(live.brain.ports, "say_many", say_many)
    monkeypatch.setattr(live.brain.ports, "resolve_chat_id", lambda target: target)
    monkeypatch.setattr(live.brain.ports, "resolve_say_target", lambda target: (target, ""))

    class Respond:
        async def chat_tools(self, *, messages, **kwargs):
            last = messages[-1]
            if last["role"] == "user" and old in last["content"]:
                name, args = "read_file", {"path": path}
            elif last["role"] == "tool" and "署名是小汐" in last["content"]:
                name, args = "evaluate_experience", appraisal_args(
                    "e3", meaning=current, feeling_text="这下放心了", tendency="把署名告诉她",
                )
            else:
                text = "你说的是哪份第二版？"
                if last["role"] == "tool":
                    assert '"result": "committed"' in last["content"]
                    text = "第二版看清了，署名是小汐。"
                name, args = "say", {"target": SCOPE.chat_id, "text": text}
            return {"tool_calls": [{"id": "choice", "name": name, "arguments": args}]}

    def turn(*context_refs):
        return drive(live.brain.run_loop(
            str(live.account), latest.content, llm=Respond(), max_steps=3,
            dry_run=False, platform=SCOPE.platform, chat_id=SCOPE.chat_id, sender_id="person-a",
            visible_event_ids=(*context_refs, "e3"), appraisal_anchor=("e3", ""),
            appraisal_context_refs=context_refs,
        ))

    assert turn("other")[-1]["text"] == "你说的是哪份第二版？"
    assert reads == []
    receipts = turn("e2", "mid")
    assert [(row["act"], row["result"]) for row in receipts] == [
        ("do", "ok"), ("evaluate_experience", "committed"), ("say", "sent"),
    ]
    assert reads == [("read_file", path)] and replies[-1] == "第二版看清了，署名是小汐。"
    next_prompt = prompt_for(live, "e4")
    assert current in next_prompt and "这下放心了" in next_prompt and old not in next_prompt
    assert "触发消息：e3" in next_prompt


def test_short_and_long_book_titles_keep_comparison_separate(live):
    original = "长书的署名还需要核对"
    comparison = "目前只是在比较阅读计划"
    live.sources["e1"].content = "《候鸟手册》这本书还得确认一下署名。"
    live.sources["e2"].content = "《活着》和《候鸟手册》你比较想先读哪个？"
    live.sources["e3"] = replace(live.sources["e1"], event_id="e3", content="关于《候鸟手册》的署名……")
    assert run(live, appraisal_args(meaning=original))[1]["result"] == "committed"
    model, receipt = run(live, appraisal_args("e2", meaning=comparison), visible=("e2",))
    assert receipt["result"] == "committed"
    assert original not in model.prompts[0][1]["content"]
    named = prompt_for(live, "e3")
    assert original in named and comparison not in named


def test_reflection_feedback_changes_next_method_and_replay_cannot_restore_old_lesson(live):
    args = {"mode": "reflection", "evidence_ref": "e1", "method": "web_search", "outcome": "success",
            "lesson": "优先核对检索来源", "confidence": 0.9}
    _, first = run(live, args)
    assert first["result"] == "committed"
    assert rf.rank_methods(rf.current_lessons_for_scope(SCOPE), ["web_search", "search_messages"]) == "web_search"
    revised = dict(args, evidence_ref="e2", outcome="failure", lesson="来源已被纠正，应换方法")
    assert run(live, revised, visible=("e2",))[1]["result"] == "committed"
    assert rf.rank_methods(rf.current_lessons_for_scope(SCOPE), ["web_search", "search_messages"]) != "web_search"
    # A fresh turn re-offers a newer head. The old source must still resolve to its original receipt.
    replay = run(live, args)[1]
    assert replay["result"] == "committed" and replay["duplicates"]
    assert rf.current_lessons_for_scope(SCOPE)[0].revision == 2
    assert rf.current_lessons_for_scope(SCOPE)[0].outcome == "failure"


@pytest.mark.parametrize("kind", ["self", "other_chat", "memory_summary", "not_visible"])
def test_actual_source_filter_rejects_invisible_self_and_summary_evidence(live, kind):
    event = live.sources["e1"]
    if kind == "self":
        event.metadata["is_self"] = True
    elif kind == "other_chat":
        event.chat_id = "private-other"
    elif kind == "memory_summary":
        event.event_type = EventType.MEMORY_WRITE
    _, receipt = run(live, appraisal_args(), visible=() if kind == "not_visible" else ("e1",))
    assert receipt["result"] == "rejected"
    assert not (live.tmp / "cognitive.db").exists()


@pytest.mark.parametrize("dry,rehearse", [(True, False), (False, True)])
def test_dry_or_rehearsal_never_writes_experience(live, dry, rehearse):
    _, receipt = run(live, appraisal_args(), dry=dry, rehearse=rehearse)
    assert receipt["result"] == "dry"
    assert not (live.tmp / "cognitive.db").exists()


def test_replayed_appraisal_does_not_accumulate_and_changed_payload_conflicts(live):
    args = appraisal_args()
    grant = experience.offers(SCOPE, frozenset({"e1"}))["e1"]
    assert experience.submit(grant, args, scope=SCOPE, visible=frozenset({"e1"}))["result"] == "committed"
    for _ in range(100):
        out = experience.submit(grant, args, scope=SCOPE, visible=frozenset({"e1"}))
        assert out["result"] == "committed" and out["duplicates"]
    changed = experience.submit(grant, dict(args, meaning="同来源另一个解释"), scope=SCOPE, visible=frozenset({"e1"}))
    assert changed["result"] == "conflict"


def test_pre_topic_appraisal_replays_without_creating_another_event(live):
    args = appraisal_args()
    legacy_subject = "chat:" + SCOPE.chat_id
    conn = cc.connect(str(live.tmp / "cognitive.db"))
    try:
        assert ap.record_appraisal(
            conn, scope=SCOPE, subject_key=legacy_subject, trigger_refs=("e1",),
            meaning=args["meaning"], tendency=args["tendency"], actor_key="person-a", source="experience_tool",
            occurrence=experience._occurrence(legacy_subject, "e1"), expected_revision=0,
        ).ok
        visible = frozenset({"e1"})
        grant = experience.offers(SCOPE, visible)["e1"]
        replay = experience.submit(grant, args, scope=SCOPE, visible=visible)
        assert replay["result"] == "committed" and replay["duplicates"]
        changed = experience.submit(grant, dict(args, meaning="另一种解释"), scope=SCOPE, visible=visible)
        assert changed["result"] == "conflict"
        assert [row[0] for row in conn.execute("SELECT subject_key FROM cognitive_events")] == [legacy_subject]
    finally:
        conn.close()


def test_source_changed_while_model_was_running_is_rejected(live):
    grant = experience.offers(SCOPE, frozenset({"e1"}))["e1"]
    live.sources["e1"].content = "外部来源已变更"
    out = experience.submit(grant, appraisal_args(), scope=SCOPE, visible=frozenset({"e1"}))
    assert out["result"] == "rejected" and out["reason"] == "experience_source_changed_or_hidden"


def test_stale_first_writer_cannot_override_newer_interpretation(live):
    for event in live.sources.values():
        event.content += " 说的是《检索手册》。"
    grants = experience.offers(SCOPE, frozenset({"e1", "e2"}))
    assert experience.submit(grants["e1"], appraisal_args(), scope=SCOPE, visible=frozenset(grants))["result"] == "committed"
    stale = experience.submit(grants["e2"], appraisal_args("e2"), scope=SCOPE, visible=frozenset(grants))
    assert stale["result"] == "conflict"
    assert [reason for _, reason in stale["reasons"]] == ["revision_conflict:1"]
    current = experience.offers(SCOPE, frozenset({"e2"}))["e2"]
    assert experience.submit(current, appraisal_args("e2"), scope=SCOPE, visible=frozenset({"e2"}))["result"] == "committed"


@pytest.mark.parametrize("extra", [{"scope": "other"}, {"goal_id": "ungranted-goal"}, {"supersedes": "fake"}])
def test_model_cannot_supply_scope_versions_or_expand_goal_grant(live, extra):
    assert run(live, appraisal_args(**extra))[1]["result"] == "rejected"


def test_actual_tool_schema_only_offers_goals_this_turn_can_use(live):
    def parameters(model):
        return next(tool["function"]["parameters"] for tool in model.tool_schemas[0]
                    if tool["function"]["name"] == "evaluate_experience")

    base = deepcopy(live.brain.SCHEMAS)
    model, rejected = run(live, appraisal_args(goal_id="evaluate_experience"))
    fields = parameters(model)["properties"]
    assert fields["evidence_ref"]["enum"] == ["e1"]
    assert '对方原话（发言人 "person-a"）' in model.prompts[0][1]["content"]
    assert all(t["function"]["name"] != "evaluate_experience" for t in live.brain._turn_schemas({}))
    assert "goal_id" not in fields
    assert "我对这件事的理解" in fields["meaning"]["description"]
    assert "我的情绪" in fields["feeling_text"]["description"]
    assert "我的意图" in fields["tendency"]["description"]
    assert "等待" in fields["tendency"]["description"]
    assert "先停下" in fields["tendency"]["description"]
    assert not ({"meaning", "feeling_text", "tendency"} & set(parameters(model)["required"]))
    assert rejected == {"act": "evaluate_experience", "result": "rejected", "reason": "experience_goal_not_visible"}
    assert not (live.tmp / "cognitive.db").exists()

    world = situation_store.get_world_model(SCOPE.key)
    for key in ("goal-z", "goal-a"):
        situation.open_intention(world, key, kind="promise")
    model, committed = run(live, appraisal_args(goal_id="goal-a"))
    assert parameters(model)["properties"]["goal_id"]["enum"] == ["goal-a", "goal-z"]
    assert "goal_id" not in parameters(model)["required"]
    assert committed["result"] == "committed"

    situation.close_intention(world, "goal-a", result="done")
    situation.close_intention(world, "goal-z", result="done")
    model, rejected = run(live, appraisal_args("e2", goal_id="goal-a"), visible=("e2",))
    assert "goal_id" not in parameters(model)["properties"]
    assert rejected["reason"] == "experience_goal_not_visible"
    assert live.brain.SCHEMAS == base


def test_model_cannot_create_an_arbitrary_method(live):
    args = {"mode": "reflection", "evidence_ref": "e1", "method": "send_message", "outcome": "success", "lesson": "未经授权外发"}
    assert run(live, args)[1]["result"] == "rejected"


def test_appraisal_offer_names_understanding_feeling_attitude_and_intent(live):
    # 评价入口需同时点明理解、态度、情绪、意图四面，否则模型会因语义过窄只选 rest。
    offer = experience.ExperienceOffer(scope=SCOPE, reference="e1", fingerprint="fp", content="我有点难过，想先停一下",
                                       heads=(), goals=frozenset(), methods=frozenset(), speaker="person-a")
    text = experience.render({"e1": offer})
    for phrase in ("对局面的理解", "对某人的态度", "我自己的情绪", "下一步意图", "只是寒暄或收尾就不评价"):
        assert phrase in text
    description = next(tool["function"]["description"] for tool in live.brain.SCHEMAS
                       if tool["function"]["name"] == "evaluate_experience")
    for phrase in ("对局面的理解", "对某人的态度", "我自己的情绪", "下一步意图", "不是每条消息都评价"):
        assert phrase in description


@pytest.mark.parametrize("confidence", [float("nan"), float("inf"), True, -0.1, 1.1])
def test_invalid_confidence_cannot_promote_a_method(live, confidence):
    args = {"mode": "reflection", "evidence_ref": "e1", "method": "web_search", "outcome": "success",
            "lesson": "仅作有来源的方法经验", "confidence": confidence}
    grant = experience.offers(SCOPE, frozenset({"e1"}))["e1"]
    assert experience.submit(grant, args, scope=SCOPE, visible=frozenset({"e1"}))["reason"] == "reflection_confidence_invalid"
    assert not (live.tmp / "cognitive.db").exists()

@pytest.mark.parametrize("move", sorted(ap.NEXT_MOVES))
def test_next_move_reaches_storage_and_the_following_prompt(live, move):
    _, receipt = run(live, appraisal_args(next_move=move))
    assert receipt["result"] == "committed"
    conn = cc.connect(str(live.tmp / "cognitive.db"))
    try:
        subject = conn.execute(
            "SELECT subject_key FROM cognitive_events WHERE operation='appraise'"
        ).fetchone()[0]
        current = ap.current_appraisal(conn, subject_key=subject, scope=SCOPE)
        assert current.next_move == move
    finally:
        conn.close()
    live.sources["e2"].parent_event_id = "e1"
    prompt = prompt_for(live, "e2")
    assert "的方向：" + ap.NEXT_MOVES[move] in prompt


def test_appraisal_written_before_next_move_still_reads_empty(live):
    _, receipt = run(live, appraisal_args())
    assert receipt["result"] == "committed"
    conn = cc.connect(str(live.tmp / "cognitive.db"))
    try:
        event_id, payload, subject = conn.execute(
            "SELECT event_id,payload,subject_key FROM cognitive_events WHERE operation='appraise'"
        ).fetchone()
        old = json.loads(payload)
        old.pop("next_move", None)
        conn.execute("UPDATE cognitive_events SET payload=? WHERE event_id=?",
                     (json.dumps(old, ensure_ascii=False), event_id))
        conn.commit()
        assert ap.current_appraisal(conn, subject_key=subject, scope=SCOPE).next_move == ""
    finally:
        conn.close()


def test_wait_keeps_the_same_turn_from_sending(live, monkeypatch):
    sent = []

    async def say_many(parts, **kwargs):
        sent.extend(parts)
        return live.brain.ports.Result(True, data={"ack": "test"})

    monkeypatch.setattr(live.brain.ports, "say_many", say_many)
    monkeypatch.setattr(live.brain.ports, "resolve_say_target", lambda target: (target, ""))
    monkeypatch.setattr(live.brain.ports, "resolve_chat_id", lambda target: target)
    monkeypatch.setattr(live.brain.selfstate, "touch_said", lambda *args: None)
    model = Model([
        {"content": "", "tool_calls": [{"id": "wait", "name": "evaluate_experience", "arguments": appraisal_args(
            next_move="wait", tendency="这一拍先不开口")}]},
        {"content": "", "tool_calls": [{"id": "reply", "name": "say", "arguments": {
            "target": SCOPE.chat_id, "text": "我先看看。"}}]},
    ])
    receipts = drive(live.brain.run_loop(
        str(live.account), "对方刚说完，先别急着回", llm=model,
        dry_run=False, rehearse=False, platform=SCOPE.platform, chat_id=SCOPE.chat_id,
        sender_id="person-a", visible_event_ids=("e1",), max_steps=2,
    ))
    assert [(row["act"], row["result"]) for row in receipts] == [
        ("evaluate_experience", "committed"), ("say", "blocked"),
    ]
    assert receipts[-1]["blocked_reason"] == "appraisal_wait"
    assert sent == []


def test_wait_allows_a_required_reply_turn_to_rest(live, monkeypatch):
    monkeypatch.setattr(live.brain.ports, "resolve_chat_id", lambda target: target)
    model = Model([
        {"content": "", "tool_calls": [{"id": "wait", "name": "evaluate_experience", "arguments": appraisal_args(
            next_move="wait", tendency="这一拍先等等")}]},
        {"content": "", "tool_calls": [{"id": "rest", "name": "rest", "arguments": {"why": "这一拍先等等"}}]},
    ])
    receipts = drive(live.brain.run_loop(
        str(live.account), "被点名", llm=model, max_steps=2, dry_run=True, rehearse=True,
        platform=SCOPE.platform, chat_id=SCOPE.chat_id, sender_id="person-a",
        visible_event_ids=("e1",),
        wake={"addressed": {"kind": "named", "chat_id": SCOPE.chat_id, "must_answer": True,
                            "request_text": "@鲸鲸 你在吗？"}},
    ))
    assert (receipts[-1]["act"], receipts[-1]["result"]) == ("rest", "rehearsal")
    assert model.tool_choices[1] != {"type": "function", "function": {"name": "say"}}


def test_wait_does_not_rescue_a_bare_text_draft_into_a_reply(live, monkeypatch):
    sent = []

    async def say_many(parts, **kwargs):
        sent.extend(parts)
        return live.brain.ports.Result(True, data={"ack": "test"})

    monkeypatch.setattr(live.brain.ports, "say_many", say_many)
    monkeypatch.setattr(live.brain.ports, "resolve_say_target", lambda target: (target, ""))
    monkeypatch.setattr(live.brain.ports, "resolve_chat_id", lambda target: target)
    monkeypatch.setattr(live.brain.selfstate, "touch_said", lambda *args: None)
    model = Model([
        {"content": "", "tool_calls": [{"id": "wait", "name": "evaluate_experience", "arguments": appraisal_args(
            next_move="wait", tendency="先收住")}]},
        {"content": "我想先等一等，不急着接话。", "tool_calls": []},
    ])
    receipts = drive(live.brain.run_loop(
        str(live.account), "对方刚说完", llm=model, dry_run=False, rehearse=False,
        platform=SCOPE.platform, chat_id=SCOPE.chat_id, sender_id="person-a",
        visible_event_ids=("e1",), max_steps=2,
    ))
    assert not any(row.get("act") == "say" for row in receipts)
    assert receipts[-1]["act"] == "silent"
    assert sent == []


def test_a_bare_acknowledgement_is_swapped_for_the_nearby_sentence():
    """线上常见锚点是「嗯嗯」。拿它当唯一候选，评价机会就被一句附和占掉了。

    10 分钟内有人说过完整的一句，就把候选换成那一句；换不到才保留原锚点。
    """
    from types import SimpleNamespace
    from datetime import datetime, timedelta

    from core.context import experience_commit as ec

    base = datetime(2026, 11, 22, 12, 0)
    def _p(text, minutes):
        return SimpleNamespace(event_id=text, content=text, occurred_at=base + timedelta(minutes=minutes))

    anchor = _p("嗯嗯", 10)
    both = [_p("其实我今天有点难过", 8), anchor]
    got = ec._prefer_words([anchor], both)
    assert [p.content for p in got] == ["其实我今天有点难过"]

    # 附近只有更早的应答词：保留原锚点，不硬凑
    only_filler = [_p("好的", 9), anchor]
    assert [p.content for p in ec._prefer_words([anchor], only_filler)] == ["嗯嗯"]

    # 隔得太远（超过 10 分钟）不换
    far = [_p("昨天聊到的那件事", -60), anchor]
    assert [p.content for p in ec._prefer_words([anchor], far)] == ["嗯嗯"]


def test_committed_appraisal_feeling_updates_the_mood_state(live, monkeypatch):
    from loop import selfstate

    monkeypatch.setattr(selfstate, "render", _real_self_render)
    _, receipt = run(live, appraisal_args(meaning="他说怕麻烦别人，我心里有点在意",
                                          feeling_text="心里有点在意"))
    assert receipt["result"] == "committed"
    key = json.dumps(["wechat", "acct", "chat-1"])
    mood = selfstate.load()["moods"][key]
    assert mood["text"] == "心里有点在意"
    assert mood["evidence_refs"] == ["e1"]
    assert "- 心境：心里有点在意" in selfstate.render(conversation_scope=("wechat", "acct", "chat-1"))
