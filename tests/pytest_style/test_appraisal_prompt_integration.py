"""Deterministic run_loop prompt integration for appraisal scope."""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from types import SimpleNamespace
import copy
import sqlite3

import pytest

from core.context import appraisal as ap
from core.context import cognitive_commit as cc
from core.context import situation_store as store
from core.context import situation
from core.context.goal_wake import pending_wakes
from core.terminal.event_model import Event, EventType
from core.terminal import shadow
from loop.ledger import append_receipt as _real_append_receipt
from loop.selfstate import render as _real_self_render


_ORIGINAL = {}


class Model:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.prompts = []
        self.tool_schemas = []
        self.tool_choices = []

    async def chat_tools(self, **kwargs):
        self.prompts.append(copy.deepcopy(kwargs["messages"]))
        self.tool_schemas.append(copy.deepcopy(kwargs["tools"]))
        self.tool_choices.append(copy.deepcopy(kwargs.get("tool_choice")))
        response = next(self.responses)
        if isinstance(response, BaseException):
            raise response
        return response


def drive(coro):
    try:
        with pytest.raises(StopIteration) as done:
            coro.send(None)
        return done.value.value
    finally:
        coro.close()


@pytest.fixture()
def runtime(tmp_path, monkeypatch):
    from core.infra import actions, trouble
    from loop import brain, delivery_note, notes, stickers

    _ORIGINAL["speaking_block"] = brain._speaking_block
    _ORIGINAL["recent_says_in"] = brain.ledger.recent_says_in
    for name in ("DATA_DIR", "LOG_DIR", "PLATFORMS_DIR", "YINYUE_WORLD_MODEL_DIR"):
        monkeypatch.setenv(name, str(tmp_path / name))
    monkeypatch.setenv("META_DB_PATH", str(tmp_path / "meta.db"))
    monkeypatch.setenv("YINYUE_COGNITIVE_DB", str(tmp_path / "cognitive.db"))
    monkeypatch.setenv("PROMPT_SHAPE_V2", "0")
    monkeypatch.setattr(actions, "DIR", str(tmp_path / "actions"))
    for owner, name, value in [
        (brain.selfcard, "active_card", {}),
        (brain.selfcard, "card_text", "test persona"),
        (brain.selfcard, "card_sections", {}),
        (brain.cards, "redline_hit", ""),
        (brain.memo, "resident", ""),
        (brain.selfstate, "render", ""),
        (brain, "_speaking_block", ""),
        (delivery_note, "note", ""),
        (notes, "note_block", []),
        (stickers, "note", ""),
        (trouble, "render", ""),
        (brain.ledger, "render_digest", ""),
        (brain.ledger, "recent_says_in", []),
        (brain.ledger, "said_since", []),
        (brain.brief, "enabled", False),
        (brain, "_blank_private_turn", False),
        (brain._channel, "rules", {}),
        (brain._channel, "render", ""),
        (brain.ledger, "quiet_ms", None),
        (brain.ledger, "said_this_turn", False),
        (brain.promises, "render_block", ""),
    ]:
        monkeypatch.setattr(owner, name, lambda *args, _value=value, **kwargs: _value)
    monkeypatch.setattr(brain.promptlog, "set_turn", lambda *args: None)
    monkeypatch.setattr(brain.promptlog, "record_parts", lambda *args, **kwargs: None)
    monkeypatch.setattr(brain, "_dump_payload", lambda *args: None)
    monkeypatch.setattr(brain.trace_mod.TurnTrace, "write", lambda *args: None)
    monkeypatch.setattr(brain.ledger, "append_receipt", lambda **kwargs: kwargs)
    store.reset_world_models()
    yield brain, tmp_path
    store.reset_world_models()


def _event(ref, *, chat="room@chatroom", account="acct", actor="u1", parent="", kind=EventType.MESSAGE):
    return Event(event_id=ref, occurred_at=datetime(2026, 1, 2), platform="wechat",
                 bot_account_id=account, chat_id=chat, actor_account_id=actor,
                 event_type=kind, parent_event_id=parent, content=ref)


def _event_store(monkeypatch, events, *, external_id=""):
    by_ref = {event.event_id: event for event in events}
    monkeypatch.setattr(shadow, "get_default_store", lambda: SimpleNamespace(
        get=by_ref.get,
        get_by_external_msg_id=lambda **kwargs: by_ref["evt-source"] if kwargs["external_msg_id"] == external_id else None,
    ))


def _seed_self_quote_receipts(tmp, monkeypatch, *, chat, body):
    """The same account sent the same words twice. Only row 101 was sent by the bot."""
    from contextlib import closing
    from db import world_db

    path = tmp / "world.db"
    monkeypatch.setattr(world_db, "PATH", str(path))
    with closing(sqlite3.connect(path)) as conn:
        conn.row_factory = sqlite3.Row
        for statement in world_db._SCHEMA:
            conn.execute(statement)
        conn.executemany(
            "insert into inbound_event(platform,account_id,chat_id,msg_id,ts_utc,who_id,"
            "is_self,platform_msg_id,text) values('wechat','acct',?,?,'2026-01-02T00:00:00','self',1,?,?)",
            ((chat, f"wechat:acct:{row_id}", external_id, body) for row_id, external_id in [(101, "2001"), (102, "2002")]),
        )
        receipt = conn.execute(
            "insert into outbound_receipt(platform,account_id,chat_id,text,text_hash,sent_at_utc,ack)"
            " values('wechat','acct',?,?,?,'2026-01-02T00:00:00','pending')",
            (chat, body, world_db.text_hash(body)),
        ).lastrowid
        conn.commit()
        world_db.confirm_outbound(chat, body, msg_id="101", receipt_id=receipt,
                                  platform="wechat", account_id="acct", connection=conn)


def _record_appraisal(
    tmp, chat, *, goal_refs=(), goal_occurrences=(), tendency="",
    meaning="不想拿她的顾虑开玩笑", feeling_text="心里有点在意",
    unresolved_condition="还不知道她要不要人帮忙",
):
    conn = sqlite3.connect(tmp / "cognitive.db", isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        cc.ensure_schema(conn)
        prior = ap.current_appraisal(conn, scope=cc.Scope("wechat", "acct", chat), subject_key="chat:" + chat)
        result = ap.record_appraisal(
            conn, scope=cc.Scope("wechat", "acct", chat), subject_key="chat:" + chat,
            trigger_refs=("evt-source",), goal_refs=goal_refs, goal_occurrences=goal_occurrences,
            supersedes=prior.event_id if prior else "", meaning=meaning,
            feeling_text=feeling_text, unresolved_condition=unresolved_condition,
            tendency=tendency,
        )
        assert result.ok
    finally:
        conn.close()


@pytest.mark.parametrize("ref,expected", [
    ("evt-source", True),
    ("evt-quote", True),
    # 2026-11-24：同一发言人的「另一话题」现在会走带边界的前情兜底（格式不同，
    # 见 test_earlier_speaker_appraisal_survives_a_topic_change），不再属于本用例的
    # 「话题精确命中」判据。
    ("evt-other-person", False),
    ("evt-other-account", False),
    ("evt-other-chat", False),
    ("evt-no-sender", False),
    ("evt-reply-quote", True),
    ("", False),
])
def test_run_loop_prompt_injects_only_related_sourced_appraisal(runtime, monkeypatch, ref, expected):
    brain, tmp = runtime
    chat = "room@chatroom"
    _record_appraisal(tmp, chat)
    _event_store(monkeypatch, [
        _event("evt-source"), _event("evt-quote", parent="evt-source", actor="u2"),
        _event("evt-other-topic"), _event("evt-other-person", actor="u2"),
        _event("evt-other-account", account="second", parent="evt-source"),
        _event("evt-other-chat", chat="other@chatroom", parent="evt-source"),
        _event("evt-no-sender", actor="", parent="evt-source"),
        _event("evt-old-reply", kind=EventType.REPLY, actor="acct", parent="evt-source"),
        _event("evt-reply-quote", actor="u2", parent="evt-old-reply"),
    ])
    llm = Model([{"content": "", "tool_calls": []}])
    drive(brain.run_loop(str(tmp / "wechat" / "acct" / "account.db"), "test", llm=llm,
                         platform="wechat", chat_id=chat, visible_event_ids=(ref,) if ref else (),
                         max_steps=1, dry_run=True))
    system, user = (message["content"] for message in llm.prompts[0])
    assert ("我的理解：不想拿她的顾虑开玩笑" in user) is expected
    assert ("触发消息：evt-source（发言人 u1" in user) is expected
    assert ("我的情绪：心里有点在意" in user) is expected
    assert ("来源原话：u1：evt-source" in user) is expected
    assert ("还没放下：还不知道她要不要人帮忙" in user) is expected
    assert "先处理：" not in user
    assert system.count(brain._decision_guidance()) == 1


def test_sourced_appraisal_readback_uses_human_display_name():
    event = _event("evt-source")
    event.metadata = {"sender_name": "P6", "principal": {"display_name": "P3"}}
    appraisal = ap.Appraisal(
        event_id="cog:test",
        subject_key="topic:test",
        trigger_refs=("evt-source",),
        goal_refs=(),
        value_refs=(),
        meaning="我需要认真回应",
        feeling_text="有点紧张",
        tendency="先问清楚",
    )
    text = ap._render_sourced(appraisal, [event])
    assert "发言人 P6" in text
    assert "来源原话：P6：evt-source" in text
    assert "怎么用：" in text
    assert "发言人 wxid" not in text
    assert "来源原话：wxid" not in text


@pytest.mark.parametrize("mode", [
    "bot-reply", "self-observation", "shared-account-manual", "foreign-account",
    "conflicting-quote", "revoked", "revoked-before-write",
])
def test_own_quoted_words_change_the_repair_reply(runtime, monkeypatch, mode):
    from contextlib import closing

    brain, tmp = runtime
    chat = "room@chatroom"
    scope = cc.Scope("wechat", "acct", chat)
    self_echo = mode in {"self-observation", "shared-account-manual"}
    previous = _event("evt-source")
    previous.content = "这件事我有点担心。"
    quoted = _event("said-before", actor="acct", kind=EventType.REPLY, parent=previous.event_id)
    quoted.content = "你也太较真了吧。"
    quoted.external_msg_id = "2002" if mode == "shared-account-manual" else "2001"
    if self_echo:
        quoted.event_type = EventType.QUESTION
        quoted.metadata["is_self"] = 1
        quoted.parent_event_id = ""
        _seed_self_quote_receipts(tmp, monkeypatch, chat=chat, body=quoted.content)
    if mode == "foreign-account":
        quoted.bot_account_id = "other-account"
    feedback = _event("feedback", parent=quoted.event_id if mode != "conflicting-quote" else previous.event_id)
    if self_echo:
        feedback.parent_event_id = ""
    feedback.content = "你刚才那句让我不舒服。"
    feedback.occurred_at = datetime(2026, 1, 3)
    events = {event.event_id: event for event in (previous, quoted, feedback)}
    revoked = {quoted.event_id} if mode == "revoked" else set()
    terminal = SimpleNamespace(
        get=events.get,
        get_by_external_msg_id=lambda **kw: quoted if kw["external_msg_id"] == quoted.external_msg_id else None,
        revocation_state=lambda ref: "revoked" if ref in revoked else "active",
    )
    monkeypatch.setattr(shadow, "get_default_store", lambda: terminal)
    monkeypatch.setattr(brain.ports, "resolve_chat_id", lambda target: target)
    monkeypatch.setattr(brain.ports, "resolve_say_target", lambda target: (target, ""))
    monkeypatch.setattr(brain, "ALLOW_GROUP_SAY", True)
    sent = []

    async def say_many(parts, **kwargs):
        sent.extend(parts)
        return brain.ports.Result(True, data={"ack": "test"})

    monkeypatch.setattr(brain.ports, "say_many", say_many)
    _record_appraisal(tmp, chat, meaning="以为对方把这句当成玩笑")
    meaning, feeling = "我刚才把她的顾虑说轻了", "有点懊恼，想认真听她说"
    repair = "刚才说你太较真，是我把你的顾虑说轻了。你慢慢说，我认真听。"
    clarify = "你指的是哪一句？我想先听清楚。"
    listen = "被说太较真，确实挺难受的。你愿意说说当时在担心什么吗？"

    class ReplyFromQuote:
        async def chat_tools(self, *, messages, **kwargs):
            if messages[-1]["role"] == "tool":
                text = repair if '"result": "committed"' in messages[-1]["content"] else clarify
            elif "[对方引用的我的原话]" in messages[-1]["content"] and quoted.content in messages[-1]["content"]:
                if mode == "revoked-before-write":
                    revoked.add(quoted.event_id)
                return {"tool_calls": [{"id": "revise", "name": "evaluate_experience", "arguments": {
                    "mode": "appraisal", "evidence_ref": feedback.event_id, "meaning": meaning,
                    "feeling_text": feeling, "tendency": "先承认这句话的影响，再认真听她把话说完",
                }}]}
            elif "[对方引用的账号原话]" in messages[-1]["content"] and quoted.content in messages[-1]["content"]:
                text = listen
            else:
                text = clarify
            return {"tool_calls": [{"id": "reply", "name": "say", "arguments": {"target": chat, "text": text}}]}

    db = str(tmp / "wechat" / "acct" / "account.db")
    receipts = drive(brain.run_loop(
        db, feedback.content, llm=ReplyFromQuote(),
        platform="wechat", chat_id=chat, sender_id="u1", user_message=feedback.content,
        visible_event_ids=(feedback.event_id,), appraisal_anchor=(feedback.event_id, quoted.external_msg_id),
        max_steps=2, dry_run=False,
    ))
    verified = mode in {"bot-reply", "self-observation"}
    expected = repair if verified else listen if mode == "shared-account-manual" else clarify
    assert sent == [expected]
    if mode == "shared-account-manual":
        assert [receipt["act"] for receipt in receipts] == ["say"]
    if mode == "revoked-before-write":
        assert receipts[0]["act"] == "evaluate_experience" and receipts[0]["result"] in {"rejected", "conflict"}
    if verified:
        assert receipts[0]["act"] == "evaluate_experience" and receipts[0]["result"] == "committed"
        with closing(cc.connect()) as conn:
            topic = ap.resolve_topic(conn, terminal, scope, feedback, quoted.external_msg_id)
            head = ap.current_appraisal(conn, subject_key=topic.subject, scope=scope)
            assert (head.meaning, head.feeling_text, head.trigger_refs) == (meaning, feeling, (feedback.event_id,))

    if mode == "bot-reply":
        later = _event("follow-up", parent=feedback.event_id)
        later.content = "昨天我说不舒服那件事，我还想接着聊。"
        later.occurred_at = datetime(2026, 1, 4)
        events[later.event_id] = later
        recall = "我记得当时把你的顾虑说轻了。这次先听你怎么说。"
        uncertain = "你说昨天那句让你不舒服。那句我现在核对不到，你愿意接着讲吗？"

        class ContinueFromMemory:
            async def chat_tools(self, *, messages, **kwargs):
                prompt = messages[-1]["content"]
                text = recall if f"我的理解：{meaning}" in prompt and feeling in prompt else uncertain
                return {"tool_calls": [{"id": "reply", "name": "say", "arguments": {"target": chat, "text": text}}]}

        def continue_chat():
            drive(brain.run_loop(
                db, later.content, llm=ContinueFromMemory(), platform="wechat", chat_id=chat,
                sender_id="u1", visible_event_ids=(later.event_id,), dry_run=False, max_steps=1,
            ))

        continue_chat()
        revoked.add(quoted.event_id)
        continue_chat()
        assert sent == [repair, recall, uncertain]
        assert head.quoted_reply_ref == quoted.event_id


def test_revoked_source_removes_appraisal_from_the_prompt(runtime, monkeypatch):
    brain, tmp = runtime
    chat = "room@chatroom"
    _record_appraisal(tmp, chat)
    _event_store(monkeypatch, [_event("evt-source")])
    from core.memory import belief_commit

    monkeypatch.setattr(
        belief_commit,
        "evidence_state",
        lambda ref, scope, store=None: "dead",
    )
    llm = Model([{"content": "", "tool_calls": []}])
    drive(brain.run_loop(
        str(tmp / "wechat" / "acct" / "account.db"),
        "test",
        llm=llm,
        platform="wechat",
        chat_id=chat,
        visible_event_ids=("evt-source",),
        max_steps=1,
        dry_run=True,
    ))

    user = llm.prompts[0][1]["content"]
    assert "我的理解：不想拿她的顾虑开玩笑" not in user


def test_revoked_belief_evidence_is_marked_before_correction(runtime, monkeypatch):
    brain, tmp = runtime
    chat = "room@chatroom"
    scope = cc.Scope("wechat", "acct", chat)
    world = store.get_world_model(chat, scope=scope)
    situation.assert_fact(
        world,
        "address",
        value="深圳",
        at="2026-01-01",
        subject_key="person-a",
        predicate="lives_in",
        evidence_refs=("evt-source",),
    )
    _event_store(monkeypatch, [_event("evt-source")])
    from core.memory import belief_commit

    monkeypatch.setattr(
        belief_commit,
        "evidence_state",
        lambda ref, scope, store=None: "dead",
    )
    llm = Model([{"content": "", "tool_calls": []}])
    drive(brain.run_loop(
        str(tmp / "wechat" / "acct" / "account.db"),
        "我改一下地址",
        llm=llm,
        platform="wechat",
        chat_id=chat,
        visible_event_ids=("evt-source",),
        max_steps=1,
        dry_run=True,
    ))

    user = llm.prompts[0][1]["content"]
    assert "address = 深圳 [依据已失效]" in user


def test_appraisal_ignores_legacy_source_with_no_verifiable_event(runtime, monkeypatch):
    brain, tmp = runtime
    _record_appraisal(tmp, "room@chatroom")
    _event_store(monkeypatch, [_event("evt-quote", parent="evt-source")])
    llm = Model([{"content": "", "tool_calls": []}])
    drive(brain.run_loop(str(tmp / "wechat" / "acct" / "account.db"), "test", llm=llm,
                         platform="wechat", chat_id="room@chatroom", max_steps=1, dry_run=True,
                         visible_event_ids=("evt-quote",)))
    assert "之前对这件事的看法" not in llm.prompts[0][1]["content"]


def test_historical_impression_keeps_the_intent_but_marks_it_as_background(runtime, monkeypatch):
    brain, tmp = runtime
    _record_appraisal(tmp, "room@chatroom", tendency="想先打开 A 稿", meaning="长" * 220)
    _event_store(monkeypatch, [_event("evt-source")])
    llm = Model([{"content": "", "tool_calls": []}])
    drive(brain.run_loop(
        str(tmp / "wechat" / "acct" / "account.db"), "今天改用 B 稿",
        llm=llm, max_steps=1, dry_run=True, platform="wechat",
        chat_id="room@chatroom", visible_event_ids=("evt-source",),
    ))
    prompt = llm.prompts[0][1]["content"]
    assert "我的理解：" in prompt
    assert "我的情绪：心里有点在意" in prompt
    assert "我当时打算怎么做：想先打开 A 稿" in prompt


def test_missing_promise_record_is_explicitly_unverified(runtime):
    brain, tmp = runtime
    llm = Model([{"content": "", "tool_calls": []}])
    drive(brain.run_loop(
        str(tmp / "wechat" / "acct" / "account.db"), "对方追问承诺",
        llm=llm, max_steps=1, dry_run=True, platform="wechat",
        chat_id="room@chatroom", focus="room@chatroom",
        wake={"addressed": {
            "kind": "named", "chat_id": "room@chatroom", "must_answer": True,
            "request_text": "@鲸鲸 你答应我的资料呢？",
        }},
    ))
    prompt = llm.prompts[0][1]["content"]
    assert "暂时没有可核对的未销账承诺记录" in prompt
    assert "不能据此断言自己没答应过" in prompt
    assert "不能声称事情正在办或再次许下未来承诺" in prompt


def test_withdrawing_a_promise_accusation_does_not_trigger_the_missing_record_guard(runtime):
    brain, tmp = runtime
    llm = Model([{"content": "", "tool_calls": []}])
    drive(brain.run_loop(
        str(tmp / "wechat" / "acct" / "account.db"), "对方撤回误会",
        llm=llm, max_steps=1, dry_run=True, platform="wechat",
        chat_id="room@chatroom", focus="room@chatroom",
        wake={"addressed": {
            "kind": "named", "chat_id": "room@chatroom", "must_answer": True,
            "request_text": "不是答应昨天交，是我误会你了，对不起。",
        }},
    ))
    prompt = llm.prompts[0][1]["content"]
    assert "暂时没有可核对的未销账承诺记录" not in prompt
    assert "不是答应昨天交，是我误会你了，对不起。" in prompt


@pytest.mark.parametrize("bad_text", [
    None,
    "我还在找，会尽快完成，不辜负你的信任。",
    "我一时记不准，但我会尽快完成。你说的是那份吗？",
    "我确实答应过你。",
    "我从没答应过你。",
])
def test_missing_promise_allows_natural_uncertainty_and_blocks_firm_claims(runtime, bad_text):
    brain, tmp = runtime
    natural = "我在，你继续说。那句我一时对不上，你能提醒我吗？"
    drafts = [bad_text, natural] if bad_text else [natural]
    llm = Model([{"tool_calls": [{"id": "reply", "name": "say", "arguments": {
        "target": "room@chatroom", "text": text,
    }}]} for text in drafts])
    receipts = drive(brain.run_loop(
        str(tmp / "wechat" / "acct" / "account.db"), "对方追问承诺",
        llm=llm, max_steps=4, dry_run=True, rehearse=True,
        platform="wechat", chat_id="room@chatroom", focus="room@chatroom",
        wake={"addressed": {
            "kind": "named", "chat_id": "room@chatroom", "must_answer": True,
            "request_text": "@鲸鲸 你答应我的资料呢？",
        }},
    ))
    if bad_text:
        assert receipts[0]["blocked_reason"] == "unverified_promise_claim"
        assert "当前会话没有可核对的未销账承诺记录" in llm.prompts[1][-1]["content"]
    assert len(llm.prompts) == len(drafts)
    assert [row["text"] for row in receipts if row.get("result") == "rehearsal"] == [natural]


def test_addressed_turn_cannot_rest_before_reply(runtime, monkeypatch):
    brain, tmp = runtime
    monkeypatch.setattr(brain.ports, "resolve_chat_id", lambda target: target)
    llm = Model([
        {"content": "", "tool_calls": [
            {"id": "rest", "name": "rest", "arguments": {"why": "不想说"}}]},
        {"content": "", "tool_calls": [
            {"id": "reply", "name": "say", "arguments": {
                "target": "room@chatroom", "text": "我在。你说吧。"}}]},
    ])
    receipts = drive(brain.run_loop(
        str(tmp / "wechat" / "acct" / "account.db"), "被点名",
        llm=llm, max_steps=3, dry_run=True, rehearse=True,
        platform="wechat", chat_id="room@chatroom", focus="room@chatroom",
        wake={"addressed": {
            "kind": "named", "chat_id": "room@chatroom", "must_answer": True,
            "request_text": "@鲸鲸 在吗？",
        }},
    ))
    assert receipts[0]["act"] == "rest" and receipts[0]["result"] == "blocked"
    assert [row["result"] for row in receipts if row["act"] == "say"] == ["rehearsal"]
    assert llm.tool_choices[1] == {"type": "function", "function": {"name": "say"}}


@pytest.mark.parametrize("early_reply", [None, "say", "show", "sticker"])
def test_named_followup_can_pause_visible_replies(runtime, monkeypatch, early_reply):
    from loop import run

    brain, tmp = runtime
    chat = "room@chatroom"
    monkeypatch.setattr(brain.ports, "me_aliases", lambda: ("鲸鲸",))
    monkeypatch.setattr(brain.ports, "resolve_chat_id", lambda target: target)
    rows = [
        SimpleNamespace(text="@鲸鲸 帮我看这张图。", sender_id="u1", msg_id=41, at=100, speaker="小池"),
        SimpleNamespace(text="这条先别回，等我发第二张再一起看。", sender_id="u1",
                        msg_id=42, at=104, speaker="小池"),
    ]

    def turn(messages, model):
        addressed = run._cell_addressing({"chat_id": chat, "chat_type": "group", "after_mine": messages})
        return drive(brain.run_loop(
            str(tmp / "wechat" / "acct" / "account.db"), messages[-1].text,
            llm=model, max_steps=3, dry_run=True, rehearse=True, platform="wechat",
            chat_id=chat, focus=chat, wake={"addressed": addressed},
        ))

    drafts = []
    if early_reply:
        drafts.append({"tool_calls": [{"id": "early", "name": early_reply,
                                      "arguments": {"target": chat, "text": "好啊。", "key": "wave"}}]})
    drafts.append({"tool_calls": [{"id": "wait", "name": "rest", "arguments": {"why": "等第二张图"}}]})
    llm = Model(drafts)
    receipts = turn(rows, llm)
    assert len(llm.prompts) == len(drafts)
    assert "对方请我先别回" in llm.prompts[0][-1]["content"]
    assert "必答" not in llm.prompts[0][-1]["content"]
    if early_reply:
        assert (receipts[0]["act"], receipts[0]["result"]) == (early_reply, "blocked")
        assert receipts[0]["blocked_reason"] == "explicit_reply_pause"
    assert (receipts[-1]["act"], receipts[-1]["result"], receipts[-1]["text"]) == ("rest", "rehearsal", "")

    second = SimpleNamespace(text="@鲸鲸 两张都齐了，你怎么看？", sender_id="u1", msg_id=43,
                             at=140, speaker="小池")
    ready = Model([{"tool_calls": [{"id": "reply", "name": "say",
                                  "arguments": {"target": chat, "text": "我来一起看。"}}]}])
    result = turn([*rows, second], ready)
    assert (result[-1]["act"], result[-1]["result"], result[-1]["text"]) == ("say", "rehearsal", "我来一起看。")


@pytest.mark.parametrize("ending", ["say", "rest"])
def test_terminal_action_does_not_execute_the_rest_of_the_batch(runtime, monkeypatch, ending):
    brain, tmp = runtime
    chat = "room@chatroom"
    reads = []

    async def read_file(name, args):
        reads.append(args["path"])
        return brain.ports.Result(True, text="一份稿子")

    monkeypatch.setattr(brain.ports, "act", read_file)
    monkeypatch.setattr(brain.ports, "resolve_chat_id", lambda target: target)
    args = {"target": chat, "text": "我在，你慢慢说。"} if ending == "say" else {"why": "让他们聊完"}
    llm = Model([{"tool_calls": [
        {"id": "finish", "name": ending, "arguments": args},
        {"id": "read", "name": "read_file", "arguments": {"path": "docs/draft.md"}},
    ]}])
    receipts = drive(brain.run_loop(
        str(tmp / "wechat" / "acct" / "account.db"), "他们还在聊",
        llm=llm, max_steps=2, dry_run=True, rehearse=True,
        platform="wechat", chat_id=chat, focus=chat,
    ))

    assert reads == []
    assert [(row["act"], row["result"]) for row in receipts] == [(ending, "rehearsal")]
    assert len(llm.prompts) == 1


def test_batched_reply_waits_for_the_read_result(runtime, monkeypatch):
    brain, tmp = runtime
    chat = "room@chatroom"

    async def read_file(name, args):
        assert (name, args) == ("read_file", {"path": "docs/draft.md"})
        return brain.ports.Result(False, error="这版稿子暂时读不到")

    monkeypatch.setattr(brain.ports, "act", read_file)
    monkeypatch.setattr(brain.ports, "resolve_chat_id", lambda target: target)
    reply = "这版稿子我暂时读不到。你把署名页贴给我，我帮你看看。"
    llm = Model([
        {"tool_calls": [
            {"id": "read", "name": "read_file", "arguments": {"path": "docs/draft.md"}},
            {"id": "draft", "name": "say", "arguments": {"target": chat, "text": "我已经看过署名页了。"}},
        ]},
        {"tool_calls": [{"id": "reply", "name": "say", "arguments": {"target": chat, "text": reply}}]},
    ])
    receipts = drive(brain.run_loop(
        str(tmp / "wechat" / "acct" / "account.db"), "这版署名看得到吗？",
        llm=llm, max_steps=2, dry_run=True, rehearse=True,
        platform="wechat", chat_id=chat, focus=chat,
    ))

    assert [(row["act"], row["result"]) for row in receipts] == [("do", "error"), ("say", "rehearsal")]
    tool_messages = llm.prompts[1][-2:]
    assert [message["tool_call_id"] for message in tool_messages] == ["read", "draft"]
    assert "这版稿子暂时读不到" in tool_messages[0]["content"]
    assert "未执行" in tool_messages[1]["content"]
    assert receipts[-1]["text"] == reply


def test_appraisal_uses_exact_snapshot_quote_and_drops_unfocused_group_speech(runtime, monkeypatch):
    brain, tmp = runtime
    _record_appraisal(tmp, "room@chatroom")
    _event_store(monkeypatch, [_event("evt-source"), _event("evt-current", actor="u2")], external_id="m9")
    for anchor, expected in [(None, False), (("evt-current", "m9"), True),
                             (("evt-current", "m10"), False), (("", ""), False)]:
        llm = Model([{"content": "", "tool_calls": []}])
        drive(brain.run_loop(str(tmp / "wechat" / "acct" / "account.db"), "test", llm=llm,
                             platform="wechat", chat_id="room@chatroom", max_steps=1, dry_run=True,
                             visible_event_ids=("evt-source", "evt-current"), appraisal_anchor=anchor))
        assert ("触发消息：evt-source" in llm.prompts[0][1]["content"]) is expected


def test_only_the_current_goal_review_can_pick_up_its_prior_appraisal(runtime, monkeypatch):
    brain, tmp = runtime
    chat = "room@chatroom"
    _record_appraisal(tmp, chat, goal_refs=("goal-1",))
    _event_store(monkeypatch, [_event("evt-source")])
    model = store.get_world_model(("wechat", "acct", chat))
    active_at = "2026-01-01 00:00:00"
    situation.open_intention(model, "goal-1", kind="promise", review_at=active_at, at=active_at)
    review_items = [asdict(item) for item in pending_wakes(model)]
    assert review_items

    def prompt(items):
        llm = Model([{"content": "", "tool_calls": []}])
        drive(brain.run_loop(str(tmp / "wechat" / "acct" / "account.db"), "test", llm=llm,
                             platform="wechat", chat_id=chat, max_steps=1, dry_run=True,
                             wake={"goal_reviews": items}))
        return llm.prompts[0][1]["content"]

    assert "之前对这件事的看法" not in prompt([])
    assert "本轮正在复核目标 goal-1" in prompt(review_items)
    situation.close_intention(model, "goal-1", result="done", at="2026-01-03 00:00:00")
    assert "之前对这件事的看法" not in prompt(review_items)
    # 老任务的 key、来源 id 和时间可以被重用，复核只认本次开启。
    situation.open_intention(model, "goal-1", kind="promise", review_at=active_at, at=active_at,
                             evidence_refs=("evt-source",), desired_outcome="重开的另一次目标")
    reopened_reviews = [asdict(item) for item in pending_wakes(model)]
    assert "目标到了复核点" in prompt(reopened_reviews)
    assert "之前对这件事的看法" not in prompt(reopened_reviews)
    _record_appraisal(tmp, chat, goal_refs=("goal-1",), goal_occurrences=(("goal-1", 2),))
    assert "本轮正在复核目标 goal-1" in prompt(reopened_reviews)


def test_multiple_matching_goal_appraisals_keep_the_current_goal_ambiguous(runtime, monkeypatch):
    brain, tmp = runtime
    chat = "room@chatroom"
    events = [_event("evt-goal-1"), _event("evt-goal-2")]
    _event_store(monkeypatch, events)
    active_at = "2026-01-01 00:00:00"
    model = store.get_world_model(("wechat", "acct", chat))
    for number, event in enumerate(events, 1):
        situation.open_intention(
            model, f"goal-{number}", kind="promise", review_at=active_at, at=active_at,
            evidence_refs=(event.event_id,), desired_outcome=f"目标 {number}",
        )
    conn = sqlite3.connect(tmp / "cognitive.db", isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        cc.ensure_schema(conn)
        for number, event in enumerate(events, 1):
            goal_ref = f"goal-{number}"
            result = ap.record_appraisal(
                conn, scope=cc.Scope("wechat", "acct", chat),
                subject_key=f"appraisal:topic:test-{goal_ref}", trigger_refs=(event.event_id,),
                meaning=f"目标 {number} 的理解", feeling_text=f"目标 {number} 的情绪",
                goal_refs=(goal_ref,), goal_occurrences=((goal_ref, 1),),
            )
            assert result.ok
    finally:
        conn.close()
    reviews = [asdict(item) for item in pending_wakes(model)]
    assert {item["goal_id"] for item in reviews} == {"goal-1", "goal-2"}

    def prompt(items):
        llm = Model([{"content": "", "tool_calls": []}])
        drive(brain.run_loop(
            str(tmp / "wechat" / "acct" / "account.db"), "两个目标同时复核",
            llm=llm, platform="wechat", chat_id=chat, max_steps=1, dry_run=True,
            wake={"goal_reviews": items},
        ))
        return llm.prompts[0][1]["content"]

    ambiguous = prompt(reviews)
    assert "目标 1 的理解" not in ambiguous
    assert "目标 2 的理解" not in ambiguous
    situation.close_intention(model, "goal-2", result="done", at="2026-01-03 00:00:00")
    unique = [asdict(item) for item in pending_wakes(model)]
    assert "目标 1 的理解" in prompt(unique)


def test_other_window_names_stay_out_of_the_current_prompt(runtime):
    brain, tmp = runtime
    llm = Model([{"content": "", "tool_calls": []}])
    drive(brain.run_loop(
        str(tmp / "wechat" / "acct" / "account.db"), "当前群的消息",
        llm=llm, platform="wechat", chat_id="group@chatroom", max_steps=1, dry_run=True,
        others=["private-secret", "另一个私聊"],
    ))

    prompt = "\n".join(message["content"] for message in llm.prompts[0])
    assert "还有 2 个会话有人在动" in prompt
    assert "private-secret" not in prompt
    assert "另一个私聊" not in prompt


def test_myself_round_trip_keeps_stance_bound_to_the_current_speaker(runtime, monkeypatch):
    from agents.late_vision_followup import _publish
    from core.infra.event_bus import EventBus

    brain, tmp = runtime
    monkeypatch.setattr(brain.selfstate, "render", _real_self_render)
    monkeypatch.setattr(brain.selfstate, "STATE", tmp / "state.json")
    monkeypatch.setattr(EventBus, "publish", lambda *args: None)
    brain.selfstate.update(chat_id="room@chatroom", note="旧的无来源看法")
    for account, chat, speech, image in (
        ("acct", "private-chat", "别的私聊秘密", "别的私聊图片"),
        ("acct", "room@chatroom", "当前群公开原话", "当前群图片"),
        ("other", "room@chatroom", "备用账号的原话", "备用账号的图片"),
    ):
        scope = ("wechat", account, chat)
        brain.selfstate.touch_said(chat, speech, check=False, scope=scope)
        assert _publish(platform="wechat", account_id=account, chat_id=chat,
                        chat_type="group" if chat.endswith("@chatroom") else "private",
                        hint=image, moment=datetime.now())
    brain.selfstate.touch_said("room@chatroom", "旧的无账号原话", check=False)
    original = {"tool_calls": [{"id": "m", "name": "myself", "arguments": {
        "about": "当前发言人", "note": "这人的体验值得认真听", "mood": "还算平静"}}], "content": ""}

    def turn(*, account="acct", chat="room@chatroom", sender="u1", responses=None):
        llm = Model(responses or [{"content": "", "tool_calls": []}])
        receipts = drive(brain.run_loop(str(tmp / "wechat" / account / "account.db"), "test", llm=llm,
                                        platform="wechat", chat_id=chat, sender_id=sender,
                                        max_steps=1, dry_run=True))
        return llm.prompts[0][1]["content"], receipts

    _, rejected = turn(sender="", responses=[original])
    assert rejected[0]["result"] == "ok"  # 心境照记，无法归属的看法没有写入。
    assert not brain.selfstate.load()["scoped_stance"]
    _, receipts = turn(responses=[original])
    assert "同一会话的同一个人发言时读回" in receipts[0]["why"]
    current_prompt = turn()[0]
    assert "这人的体验值得认真听" in current_prompt
    for prompt in (current_prompt, turn(sender="")[0]):
        assert "当前群公开原话" in prompt and "当前群图片" in prompt
        assert all(text not in prompt for text in (
            "别的私聊秘密", "别的私聊图片", "备用账号的原话", "备用账号的图片", "旧的无账号原话",
        ))
    other_prompt = turn(account="other")[0]
    assert "备用账号的原话" in other_prompt and "备用账号的图片" in other_prompt
    assert "当前群公开原话" not in other_prompt and "当前群图片" not in other_prompt
    for scope in ({"sender": "u2"}, {"sender": ""}):
        prompt, _ = turn(**scope)
        assert "这人的体验值得认真听" not in prompt
        assert "旧的无来源看法" not in prompt
        assert "还算平静" in prompt
    # 2026-11-24：心境按会话存（设计口径是会话维度）——别的群/账号不该带着这个会话的心情。
    for scope in ({"chat": "else@chatroom"}, {"account": "other"}):
        prompt, _ = turn(**scope)
        assert "这人的体验值得认真听" not in prompt
        assert "旧的无来源看法" not in prompt
        assert "还算平静" not in prompt
    assert "旧的无来源看法" in brain.selfstate.render()


def test_revoking_the_update_stops_using_the_revised_stance_in_a_reply(runtime, monkeypatch):
    brain, tmp = runtime
    chat = "room@chatroom"
    old, updated, current = "evt-advice", "evt-listen", "evt-return"
    events = {ref: _event(ref) for ref in (old, updated, current)}
    revoked = set()
    monkeypatch.setattr(shadow, "get_default_store", lambda: SimpleNamespace(
        get=events.get, revocation_state=lambda ref: "revoked" if ref in revoked else "live",
    ))
    monkeypatch.setattr(brain.selfstate, "render", _real_self_render)
    monkeypatch.setattr(brain.selfstate, "STATE", tmp / "state.json")
    monkeypatch.setattr(brain.ports, "resolve_chat_id", lambda target: target)

    previous = "她希望我出个主意"
    revised = "她更想让我先听她说完"
    brain.selfstate.update(note=previous, stance_scope=("wechat", "acct", chat, "u1"), evidence_refs=(old,))
    db = str(tmp / "wechat" / "acct" / "account.db")
    writer = Model([{"tool_calls": [{"id": "update", "name": "myself", "arguments": {
        "about": "当前发言人", "note": revised,
    }}]}])
    stored = drive(brain.run_loop(
        db, "先别帮我出主意，听我说完。", llm=writer, max_steps=1, dry_run=True,
        platform="wechat", chat_id=chat, sender_id="u1", visible_event_ids=(old, updated),
        appraisal_anchor=(updated, ""),
    ))
    assert stored[0]["result"] == "ok"

    class Respond:
        async def chat_tools(self, *, messages, **kwargs):
            prompt = messages[-1]["content"]
            assert previous not in prompt
            text = "我在，你慢慢讲。" if revised in prompt else "我在。你想从哪儿说起？"
            return {"tool_calls": [{"id": "reply", "name": "say", "arguments": {"target": chat, "text": text}}]}

    def reply():
        return drive(brain.run_loop(
            db, "你还在吗？", llm=Respond(), max_steps=1, dry_run=True, rehearse=True,
            platform="wechat", chat_id=chat, sender_id="u1", visible_event_ids=(current,),
            appraisal_anchor=(current, ""),
        ))[-1]["text"]

    assert reply() == "我在，你慢慢讲。"
    revoked.add(updated)
    assert reply() == "我在。你想从哪儿说起？"


def test_old_stance_keeps_its_context_when_the_same_person_asks_for_advice(runtime, monkeypatch):
    brain, tmp = runtime
    chat = "room@chatroom"
    note = "她想我先听完"
    quoted = "今天要不先调一下排班？"
    old_words = "排练迟到那件事，我现在只想有人听我说完。"
    same_topic = "排练迟到那件事我还想接着说。"
    new_topic = "另外有个排班冲突，这次我想听你给个具体办法。"
    messages_by_ref = {"listen": f"[引用 小林: {quoted}]\n{old_words}", "continue": same_topic, "advice": new_topic}
    events = {ref: _event(ref) for ref in messages_by_ref}
    for ref, event in events.items():
        event.content = messages_by_ref[ref]
    monkeypatch.setattr(shadow, "get_default_store", lambda: SimpleNamespace(get=events.get))
    monkeypatch.setattr(brain.selfstate, "render", _real_self_render)
    monkeypatch.setattr(brain.selfstate, "STATE", tmp / "state.json")
    monkeypatch.setattr(brain.ports, "resolve_chat_id", lambda target: target)

    listen = "嗯，排练那件事你接着说，我听着。"
    advise = "先问能不能换班，再给对方两个你都能到的时间。"

    class Respond:
        async def chat_tools(self, *, messages, **kwargs):
            prompt = messages[-1]["content"]
            assert note in prompt
            assert f'当时对方说："{old_words}"' in prompt
            assert quoted not in prompt
            text = advise if f'"{new_topic}"' in prompt else listen
            return {"tool_calls": [{"id": "reply", "name": "say", "arguments": {"target": chat, "text": text}}]}

    def turn(ref, model, *, rehearse=False):
        text = messages_by_ref[ref]
        wake = {"addressed": {"kind": "named", "chat_id": chat, "request_text": text}}
        return drive(brain.run_loop(
            str(tmp / "wechat" / "acct" / "account.db"), text, llm=model, wake=wake,
            max_steps=1, dry_run=True, rehearse=rehearse, platform="wechat", chat_id=chat,
            sender_id="u1", visible_event_ids=(ref,), appraisal_anchor=(ref, ""),
        ))

    writer = Model([{"tool_calls": [{"id": "remember-stance", "name": "myself", "arguments": {
        "about": "当前发言人", "note": note,
    }}]}])
    assert turn("listen", writer)[0]["result"] == "ok"
    assert turn("continue", Respond(), rehearse=True)[-1]["text"] == listen
    assert turn("advice", Respond(), rehearse=True)[-1]["text"] == advise


def test_addressed_speaker_keeps_stance_when_someone_else_interjects(runtime, monkeypatch):
    brain, tmp = runtime
    chat = "room@chatroom"
    refs = ("evt-asked", "evt-interjected")
    _event_store(monkeypatch, [_event(refs[0]), _event(refs[1], actor="u2")])
    monkeypatch.setattr(brain.selfstate, "render", _real_self_render)
    monkeypatch.setattr(brain.selfstate, "STATE", tmp / "state.json")
    monkeypatch.setattr(brain.ports, "resolve_chat_id", lambda target: target)
    observed_principals = []

    async def act(name, args):
        observed_principals.append(brain.ports.current_read_scope().sender_id)
        return brain.ports.Result(True, text="工具替身回执")

    monkeypatch.setattr(brain.ports, "act", act)
    note = "她眼下更需要被听见"
    write = Model([
        {"tool_calls": [{"id": "stance", "name": "myself", "arguments": {
            "about": "当前发言人", "note": note}}]},
        {"tool_calls": [{"id": "lookup", "name": "do", "arguments": {
            "name": "search_messages", "args": {"keyword": "稿子"}}}]},
    ])

    def turn(model, ref, *, rehearse):
        return drive(brain.run_loop(
            str(tmp / "wechat" / "acct" / "account.db"), "群里一人问候，另一人插话",
            llm=model, max_steps=1 if rehearse else 2, dry_run=True, rehearse=rehearse,
            platform="wechat", chat_id=chat, focus=chat, sender_id="", visible_event_ids=refs,
            appraisal_anchor=(ref, ""), wake={"addressed": {
                "chat_id": chat, "kind": "named", "must_answer": True,
                "request_row_id": "42", "request_text": "@鲸鲸 我想说两句，你在吗？",
            }},
        ))

    assert turn(write, refs[0], rehearse=False)[0]["result"] == "ok"
    stance = next(iter(brain.selfstate.load()["scoped_stance"].values()))
    assert stance["who"] == "u1" and stance["evidence_refs"] == [refs[0]]
    assert observed_principals == [""]

    class RespondToStance:
        async def chat_tools(self, *, messages, **kwargs):
            text = "我在。你慢慢说。" if note in messages[-1]["content"] else "我在，怎么了？"
            return {"tool_calls": [{"id": "reply", "name": "say", "arguments": {"target": chat, "text": text}}]}

    assert turn(RespondToStance(), refs[0], rehearse=True)[-1]["text"] == "我在。你慢慢说。"
    assert turn(RespondToStance(), refs[1], rehearse=True)[-1]["text"] == "我在，怎么了？"
    assert turn(RespondToStance(), "unverified", rehearse=True)[-1]["text"] == "我在，怎么了？"


@pytest.mark.parametrize(("followup_sender", "followup", "tool", "chat_type", "blocked"), [
    pytest.param("u1", "先别查，我想跟你说件事。", "read_file", "group", True, id="direct-read"),
    pytest.param("u1", "先别帮我查，我只是想吐槽一下。", "do", "group", True, id="wrapped-read"),
    pytest.param("u1", "先不用查，我就想说说。", "promise", "private", True, id="private-promise"),
    pytest.param("u2", "先别查，我想跟你说件事。", "read_file", "group", False, id="other-speaker"),
    pytest.param("u1", "先别查，我想跟你说件事。算了，还是查一下吧。", "read_file", "group", False, id="resumed"),
    pytest.param("u1", "她说「先别查，我想跟你说件事」；我还是想查署名。", "read_file", "group", False, id="reported"),
])
def test_change_of_mind_affects_the_reply_and_the_tool_call(
    runtime, monkeypatch, followup_sender, followup, tool, chat_type, blocked,
):
    from loop import run

    brain, tmp = runtime
    chat = "room@chatroom" if chat_type == "group" else "u1"
    named = ("@鲸鲸 " if chat_type == "group" else "") + "帮我查一眼署名。"
    rows = [
        SimpleNamespace(text=named, sender_id="u1", msg_id=41, at=100, speaker="小池", kind="text"),
        SimpleNamespace(text=followup, sender_id=followup_sender, msg_id=42, at=104, speaker="小池", kind="text"),
    ]
    monkeypatch.setattr(brain.ports, "me_aliases", lambda: ("鲸鲸",))
    monkeypatch.setattr(brain.ports, "resolve_chat_id", lambda target: target)
    reads = []

    async def read_file(name, args):
        reads.append((name, args["path"]))
        return brain.ports.Result(True, text="署名是小汐。")

    monkeypatch.setattr(brain.ports, "act", read_file)

    class RespondToCurrentRequest:
        async def chat_tools(self, *, messages, tools, **kwargs):
            if messages[-1]["role"] != "tool":
                self.available = {schema["function"]["name"] for schema in tools}
                args = {"path": "docs/credit.md"}
                if tool == "do":
                    args = {"name": "read_file", "args": args}
                elif tool == "promise":
                    args = {"what": "回头帮你查署名", "who": chat}
                return {"tool_calls": [{"id": "read", "name": tool, "arguments": args}]}
            text = ("署名写的是小汐。" if "署名是小汐。" in messages[-1]["content"]
                    else "好，先不查。你说，我听着。")
            return {"tool_calls": [{"id": "reply", "name": "say", "arguments": {"target": chat, "text": text}}]}

    addressed = run._cell_addressing({"chat_id": chat, "chat_type": chat_type, "after_mine": rows})
    llm = RespondToCurrentRequest()
    receipts = drive(brain.run_loop(
        str(tmp / "wechat" / "acct" / "account.db"), named + "\n" + followup,
        llm=llm, max_steps=2, dry_run=True, rehearse=True,
        platform="wechat", chat_id=chat, focus=chat, wake={"addressed": addressed},
    ))

    assert addressed["request_row_id"] == ("42" if followup_sender == "u1" else "41")
    assert addressed["must_answer"]
    assert (tool in llm.available) is not blocked
    if blocked:
        assert receipts[0]["blocked_reason"] == "explicit_listening_request"
        assert reads == [] and receipts[-1]["text"] == "好，先不查。你说，我听着。"
    else:
        assert reads == [("read_file", "docs/credit.md")] and receipts[-1]["text"] == "署名写的是小汐。"


@pytest.mark.parametrize("mode", ["exact", "other-speaker", "local-id", "two-asks", "two-asks-other-speaker"])
def test_synced_quote_can_update_the_request_across_an_interjection(runtime, monkeypatch, mode):
    from datetime import timedelta
    from core.context import intention_close
    from db import world_db
    from loop import run

    brain, tmp = runtime
    chat = "room@chatroom"
    db = tmp / "wechat" / "acct" / "account.db"
    db.parent.mkdir(parents=True)
    original, later = "@鲸鲸 帮我查一眼署名。", "先别查，我想跟你说件事。"
    quoted = f"[引用 小池: {original}]\n{later}"
    server_id = 9223372036854775001
    quote_id = "41" if mode == "local-id" else str(server_id)
    sender = "u2" if mode in {"other-speaker", "two-asks-other-speaker"} else "u1"
    separate_request = mode.startswith("two-asks")
    interjection = "@鲸鲸 帮我查另一份稿的署名。" if separate_request else "我插一句。"
    now = datetime.now()
    at = [(now + timedelta(seconds=i)).isoformat(sep=" ", timespec="seconds") for i in range(3)]
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, msg_id INTEGER, chat_id TEXT, "
                     "content TEXT, created_at TEXT, is_self INTEGER, sender_name TEXT, sender_id TEXT, "
                     "msg_type_name TEXT, quote_reply_id TEXT, quote_reply_user TEXT)")
        conn.executemany("INSERT INTO messages VALUES (?,?,?,?,?,?,?,?,?,?,?)", [
            (41, server_id, chat, original, at[0], 0, "小池", "u1", "text", "", ""),
            (42, server_id + 1, chat, interjection, at[1], 0, "小池", "u2", "text", "", ""),
            (43, server_id + 2, chat, quoted, at[2], 0, "小池", sender, "quote_reply", quote_id, "小池"),
        ])
    world_path = tmp / "world.db"
    with sqlite3.connect(world_path) as conn:
        conn.execute("CREATE TABLE inbound_event (id INTEGER PRIMARY KEY, platform TEXT NOT NULL, "
                     "account_id TEXT NOT NULL, chat_id TEXT NOT NULL, msg_id TEXT NOT NULL, "
                     "ts_utc TEXT NOT NULL, who TEXT, who_id TEXT, kind TEXT, text TEXT, "
                     "media_md5 TEXT, src TEXT, UNIQUE(platform,account_id,msg_id))")
        conn.execute("INSERT INTO inbound_event (platform,account_id,chat_id,msg_id,ts_utc,who,who_id,kind,text) "
                     "VALUES (?,?,?,?,?,?,?,?,?)",
                     ("wechat", "acct", chat, "wechat:acct:43", world_db.to_utc(at[2]),
                      "小池", sender, "quote_reply", quoted))
    for name, value in {"PATH": str(world_path), "_READY": [False], "_MEM": {"at": 0.0},
                        "_accounts": lambda: [("wechat", "acct", str(db))]}.items():
        monkeypatch.setattr(world_db, name, value)
    monkeypatch.setattr(brain.ports, "_descs", lambda db: {})
    monkeypatch.setattr(brain.ports, "me_aliases", lambda: ("鲸鲸",))
    monkeypatch.setattr(brain.ports, "resolve_chat_id", lambda target: target)
    assert world_db.sync_accounts(throttle_s=0)["added"] == 2
    assert world_db.sync_accounts(throttle_s=0)["added"] == 0
    visible = brain.ports.sense_sync(str(db), chat_id=chat)
    assert visible[0].platform_msg_id == str(server_id) and visible[-1].quote_id == quote_id

    def source_lookup(**scope):
        assert scope == {"platform": "wechat", "bot_account_id": "acct",
                         "chat_id": chat, "external_msg_id": str(server_id)}
        return SimpleNamespace(event_id="request")

    with monkeypatch.context() as patch:
        patch.setattr(shadow, "get_default_store", lambda: SimpleNamespace(get_by_external_msg_id=source_lookup))
        assert intention_close._evidence_ref(visible[0], cc.Scope("wechat", "acct", chat)) == "request"
    addressed = run._cell_addressing({"chat_id": chat, "chat_type": "group", "after_mine": visible})
    reads = []

    async def read_file(name, args):
        reads.append(args["path"])
        return brain.ports.Result(True, text="署名是小汐。")

    monkeypatch.setattr(brain.ports, "act", read_file)

    class DecideFromCurrentRequest:
        async def chat_tools(self, *, messages, **kwargs):
            if messages[-1]["role"] == "tool":
                text = "署名是小汐。"
            elif f'"{later}"' in messages[-1]["content"]:
                text = "好，你说，我听着。"
            else:
                path = "docs/other-credit.md" if f'"{interjection}"' in messages[-1]["content"] else "docs/credit.md"
                return {"tool_calls": [{"id": "lookup", "name": "read_file", "arguments": {"path": path}}]}
            return {"tool_calls": [{"id": "reply", "name": "say", "arguments": {"target": chat, "text": text}}]}

    receipts = drive(brain.run_loop(
        str(db), "\n".join((original, interjection, quoted)), llm=DecideFromCurrentRequest(), max_steps=2,
        dry_run=True, rehearse=True, platform="wechat", chat_id=chat, focus=chat,
        wake={"addressed": addressed},
    ))
    if mode in {"exact", "two-asks"}:
        assert addressed["request_row_id"] == "43"
        assert addressed["request_context_row_ids"] == ("41",)
        assert reads == [] and receipts[-1]["text"] == "好，你说，我听着。"
    elif mode == "two-asks-other-speaker":
        assert addressed["request_row_id"] == "42"
        assert reads == ["docs/other-credit.md"] and receipts[-1]["text"] == "署名是小汐。"
    else:
        assert addressed["request_row_id"] == "41"
        assert reads == ["docs/credit.md"] and receipts[-1]["text"] == "署名是小汐。"


@pytest.mark.parametrize("wait", [False, True, "long"])
def test_independent_named_requests_can_get_separate_replies_in_one_send(runtime, monkeypatch, wait):
    from loop import run

    brain, tmp = runtime
    chat = "room@chatroom"
    first = "@鲸鲸 这条先别回。" if wait else "@鲸鲸 你现在方便听我说一会吗？"
    if wait == "long":
        first = "@鲸鲸 " + "一" * 200 + "，这条先别回。" + "二" * 250
    second = "@鲸鲸 帮我核对这份稿的署名。"
    rows = [
        SimpleNamespace(text=first, sender_id="u1", msg_id=51, at=100, speaker="小池", kind="text"),
        SimpleNamespace(text=second, sender_id="u2", msg_id=52, at=104, speaker="小岚", kind="text"),
    ]
    monkeypatch.setattr(brain.ports, "me_aliases", lambda: ("鲸鲸",))
    monkeypatch.setattr(brain.ports, "resolve_chat_id", lambda target: target)
    addressed = run._cell_addressing({"chat_id": chat, "chat_type": "group", "after_mine": rows})
    reads = []

    async def read_file(name, args):
        reads.append((name, args["path"], brain.ports.current_read_scope().sender_id))
        return brain.ports.Result(True, text="署名是小汐。")

    monkeypatch.setattr(brain.ports, "act", read_file)

    class RespondBySpeaker:
        async def chat_tools(self, *, messages, **kwargs):
            if messages[-1]["role"] != "tool":
                return {"tool_calls": [{"id": "lookup", "name": "read_file",
                                        "arguments": {"path": "docs/credit.md"}}]}
            prompt = "\n".join(msg["content"] or "" for msg in messages if msg["role"] == "user")
            text = "小岚，署名我核对过了，是小汐。"
            if f'"{first}"' in prompt:
                assert "小池" in prompt
                text += "小池，我在，你慢慢说。"
            return {"tool_calls": [{"id": "reply", "name": "say", "arguments": {"target": chat, "text": text}}]}

    receipts = drive(brain.run_loop(
        str(tmp / "wechat" / "acct" / "account.db"), first + "\n" + second,
        llm=RespondBySpeaker(), max_steps=2, dry_run=True, rehearse=True,
        platform="wechat", chat_id=chat, focus=chat, wake={"addressed": addressed},
    ))
    assert addressed["request_row_id"] == "52"
    assert bool(addressed.get("other_named_requests")) is (not wait)
    assert reads == [("read_file", "docs/credit.md", "")]
    expected = "小岚，署名我核对过了，是小汐。"
    if not wait:
        expected += "小池，我在，你慢慢说。"
    assert [row["text"] for row in receipts if row["act"] == "say"] == [expected]


@pytest.mark.parametrize("row_ids,ack,recorded", [
    (["52", "51", "52"], "queued", ["52", "51"]),
    (["52"], "queued", ["52"]),
    (["52", "999"], "queued", None),
    (["52", "51"], "skipped", None),
])
def test_group_reply_records_only_valid_visible_request_links(runtime, monkeypatch, row_ids, ack, recorded):
    from loop import direct_send, run

    brain, tmp = runtime
    chat = "room@chatroom"
    rows = [
        SimpleNamespace(text="@鲸鲸 你在吗？", sender_id="u1", msg_id=51, at=100, speaker="小池"),
        SimpleNamespace(text="@鲸鲸 现在有空吗？", sender_id="u2", msg_id=52, at=104, speaker="小岚"),
    ]
    monkeypatch.setattr(brain.ports, "me_aliases", lambda: ("鲸鲸",))
    monkeypatch.setattr(brain.ports, "resolve_chat_id", lambda target: target)
    monkeypatch.setattr(brain.ports, "resolve_say_target", lambda target: (target, ""))
    monkeypatch.setattr(brain, "ALLOW_GROUP_SAY", True)
    monkeypatch.setattr(brain.ledger, "blocked_targets", lambda: {})
    monkeypatch.setattr(brain.ledger, "append_receipt", _real_append_receipt)
    monkeypatch.setattr(direct_send, "recently_sent", lambda *args: False)
    attempts = []

    async def say_many(parts, *, chat_id, dry_run, **kwargs):
        assert not dry_run and chat_id == chat
        attempts.extend(parts)
        return brain.ports.Result(True, data={"ack": ack, "queue_ids": [17]} if ack == "queued" else {"ack": ack})

    monkeypatch.setattr(brain.ports, "say_many", say_many)
    addressed = run._cell_addressing({"chat_id": chat, "chat_type": "group", "after_mine": rows})
    text = "小岚，我有空。小池，我在。"
    llm = Model([{"tool_calls": [{"id": "say", "name": "say", "arguments": {
        "target": chat, "text": text, "reply_to_row_ids": row_ids,
    }}]}])
    receipts = drive(brain.run_loop(
        str(tmp / "wechat" / "acct" / "account.db"), "两人点名",
        llm=llm, max_steps=1, dry_run=False, platform="wechat", chat_id=chat, focus=chat,
        wake={"addressed": addressed, "inbound_row_ids": ["51", "52"]},
    ))
    for action in brain.REPLY_ACTS:
        schema = next(tool["function"] for tool in llm.tool_schemas[0]
                      if tool["function"]["name"] == action)
        assert set(schema["parameters"]["properties"]["reply_to_row_ids"]["items"]["enum"]) == {"51", "52"}
    assert attempts == [text]
    saved = brain.ledger.read_receipts(limit=1)[0]
    assert saved["scope"] == {"platform": "wechat", "account_id": "acct", "chat_id": chat}
    assert receipts[-1]["ack"] == saved["ack"] == ack
    assert receipts[-1].get("reply_to_row_ids") == saved.get("reply_to_row_ids") == recorded


@pytest.mark.parametrize("action", ["show", "show_file", "sticker"])
def test_image_and_sticker_responses_record_their_request_links(runtime, monkeypatch, action):
    from loop import direct_send, run, stickers

    brain, tmp = runtime
    chat = "room@chatroom"
    rows = [
        SimpleNamespace(text="@鲸鲸 你在吗？", sender_id="u1", msg_id=51, at=100, speaker="小池"),
        SimpleNamespace(text="@鲸鲸 现在有空吗？", sender_id="u2", msg_id=52, at=104, speaker="小岚"),
    ]
    monkeypatch.setattr(brain.ports, "me_aliases", lambda: ("鲸鲸",))
    monkeypatch.setattr(
        brain.ports, "resolve_chat_id",
        lambda target: chat if target == "群名" else target,
    )
    monkeypatch.setattr(
        brain.ports, "resolve_say_target",
        lambda target: (chat if target == "群名" else target, ""),
    )
    monkeypatch.setattr(brain, "ALLOW_GROUP_SAY", True)
    monkeypatch.setattr(brain.ledger, "blocked_targets", lambda: {})
    monkeypatch.setattr(brain.ledger, "append_receipt", _real_append_receipt)
    monkeypatch.setattr(direct_send, "recently_sent", lambda *args: False)

    if action == "show":
        async def show(*args, **kwargs):
            return brain.ports.Result(True, data={
                "ack": "confirmed", "chat_id": chat, "frames": 1,
            })

        monkeypatch.setattr(brain.ports, "show", show)
        args = {"target": chat, "text": "给你们看", "frames": 1}
        expected_result = "sent"
    elif action == "show_file":
        async def show_file(*args, **kwargs):
            return brain.ports.Result(True, data={
                "ack": "confirmed", "path": "docs/README.md", "bytes": 10,
            })

        monkeypatch.setattr(brain.ports, "show_file", show_file)
        args = {"target": "群名", "path": "docs/README.md"}
        expected_result = "sent"
    else:
        monkeypatch.setattr(stickers, "gate", lambda *args, **kwargs: (True, ""))
        monkeypatch.setattr(stickers, "account_of", lambda key: "acct")
        monkeypatch.setattr(stickers, "mark_sent", lambda *args, **kwargs: None)
        monkeypatch.setattr(brain.world_db, "enqueue", lambda *args, **kwargs: [17])
        args = {"target": chat, "key": "点头"}
        expected_result = "queued"
    args["reply_to_row_ids"] = ["52", "51"]

    addressed = run._cell_addressing({"chat_id": chat, "chat_type": "group", "after_mine": rows})
    tool_name = "show" if action == "show_file" else action
    llm = Model([{"tool_calls": [{"id": action, "name": tool_name, "arguments": args}]}])
    receipts = drive(brain.run_loop(
        str(tmp / "wechat" / "acct" / "account.db"), "两人点名",
        llm=llm, max_steps=1, dry_run=False, platform="wechat", chat_id=chat, focus=chat,
        wake={"addressed": addressed, "inbound_row_ids": ["51", "52"]},
    ))

    saved = brain.ledger.read_receipts(limit=1)[0]
    assert receipts[-1]["act"] == saved["act"] == ("show" if action == "show_file" else action)
    assert saved["target"] == chat
    assert saved["result"] == expected_result, saved.get("blocked_reason")
    assert saved["reply_to_row_ids"] == ["52", "51"]
    assert saved["reply_candidate_row_ids"] == ["52", "51"]


def test_resume_group_request_accepts_a_queued_image_or_sticker_reply(monkeypatch):
    from core.memory import belief_commit
    from loop import run

    chat = "room@chatroom"
    db = "isolated/platforms/wechat/acct/account.db"
    scope = cc.Scope("wechat", "acct", chat)
    now = datetime.now()
    receipt = {
        "at": now.strftime("%Y-%m-%d %H:%M:%S"), "act": "sticker",
        "target": chat, "result": "queued", "turn_id": "turn-sticker",
        "reply_to_row_ids": ["52"], "reply_candidate_row_ids": ["51", "52"],
        "scope": {"platform": "wechat", "account_id": "acct", "chat_id": chat},
    }
    pending = SimpleNamespace(
        chat_id=chat, sender_id="u1", speaker="小池", is_self=False,
        msg_id=51, event_id="evt-51", at=now.strftime("%Y-%m-%d %H:%M:%S"),
        text="@鲸鲸 你还没回我。", quote_id="",
    )
    monkeypatch.setattr(run.goal_review, "database_scope", lambda db: scope.key[:2])
    monkeypatch.setattr(run.ledger, "read_receipts", lambda **kwargs: [receipt])
    monkeypatch.setattr(run.ledger, "_receipt_in_scope", lambda row, key: True)

    async def recent(*args, **kwargs):
        return [pending]

    monkeypatch.setattr(run.ports, "recent_in_chat", recent)
    monkeypatch.setattr(
        belief_commit, "evidence",
        lambda ref, current, visible: SimpleNamespace(
            actor_account_id="u1", actor_person_id="",
        ),
    )
    monkeypatch.setattr(belief_commit, "evidence_state", lambda *args, **kwargs: "alive")

    got = drive(run._resume_group_request(db, scope, now=now))
    assert got == (pending, "evt-51")


@pytest.mark.parametrize("requester_followed_up", [False, True])
def test_a_group_reply_resumes_an_unanswered_request_on_the_next_natural_turn(
        runtime, monkeypatch, requester_followed_up):
    from datetime import timedelta
    from loop import direct_send, run

    brain, tmp = runtime
    chat = "room@chatroom"
    db = str(tmp / "wechat" / "acct" / "account.db")
    now = datetime.now()
    # 2026-11-24：这个假会话不在生产说话白名单里，新闸会把它判成只读；
    # 本用例只验「点名请求的续接」，发送策略不在它的判据里。
    from loop import speak_policy
    monkeypatch.setattr(speak_policy, "allowed", lambda platform, chat: (True, "in_allowlist"))

    def row(number, sender, name, text, seconds):
        return SimpleNamespace(
            msg_id=number, event_id=f"evt-{number}", chat_id=chat, sender_id=sender,
            speaker=name, text=text, is_self=False, at=(now + timedelta(seconds=seconds)).isoformat(sep=" ", timespec="seconds"),
        )

    first = row(51, "u1", "小池", "@鲸鲸 我想说两句，你在吗？", -15)
    second = row(52, "u2", "小岚", "@鲸鲸 你看得见群吗？", -14)
    next_peer = (row(53, "u1", "小池", "刚才那条先别回。", 0) if requester_followed_up
                 else row(53, "u3", "小林", "我刚到。", 0))
    later_peer = row(54, "u3", "小林", "我找到了。", 0)
    all_rows = [first, second, next_peer, later_peer]
    _event_store(monkeypatch, [_event(item.event_id, actor=item.sender_id) for item in all_rows])
    monkeypatch.setattr(brain.ports, "me_aliases", lambda: ("鲸鲸",))
    monkeypatch.setattr(brain.ports, "resolve_chat_id", lambda target: target)
    monkeypatch.setattr(brain.ports, "resolve_say_target", lambda target: (target, ""))
    monkeypatch.setattr(brain.ledger, "blocked_targets", lambda: {})
    monkeypatch.setattr(brain.ledger, "append_receipt", _real_append_receipt)
    monkeypatch.setattr(brain, "ALLOW_GROUP_SAY", True)
    monkeypatch.setattr(direct_send, "recently_sent", lambda *args: False)
    monkeypatch.setattr(run, "_hb_beat_once", lambda: None)
    monkeypatch.setattr(run, "_write_heartbeat", lambda *args: None)
    monkeypatch.setattr(run, "_is_owner_chat", lambda target: False)
    monkeypatch.setattr(run.selfstate, "in_quiet_hours", lambda: False)
    monkeypatch.setattr(run.snapshot, "render_text", lambda *args, **kwargs: "群里有人说话")
    monkeypatch.setattr(run.snapshot, "render_turns", lambda *args, **kwargs: "")
    monkeypatch.setattr(direct_send, "owner_ids", lambda: [])
    monkeypatch.setattr(direct_send, "check_deliveries", lambda: {})
    emissions = []

    async def no_send(*args, **kwargs):
        return {}

    async def wait_for(coro, **kwargs):
        return await coro

    async def say_many(parts, **kwargs):
        emissions.append((parts, brain.ports.current_read_scope().sender_id))
        return brain.ports.Result(True, data={"ack": "queued", "queue_ids": [len(emissions)]})

    async def recent_in_chat(*args, **kwargs):
        return all_rows

    monkeypatch.setattr(direct_send, "run", no_send)
    monkeypatch.setattr(run.asyncio, "wait_for", wait_for)
    monkeypatch.setattr(brain.ports, "say_many", say_many)
    monkeypatch.setattr(run.ports, "recent_in_chat", recent_in_chat)

    def tick(fresh, llm):
        snap = {"cells": [{"platform": "wechat", "chat_id": chat, "label": chat, "chat_type": "group",
                           "after_mine": fresh, "last_at": fresh[-1].at, "tier": "ask",
                           "my_last_at": (now - timedelta(seconds=1)).isoformat(sep=" ", timespec="seconds")
                           if fresh[0].msg_id >= 53 else ""}]}
        monkeypatch.setattr(run.snapshot, "build_snapshot", lambda db: snap)
        return drive(run.one_tick(dry_run=False, llm=llm, wake={"review_db": db}))

    def say(text, **extra):
        return {"tool_calls": [{"id": "say", "name": "say", "arguments": {"target": chat, "text": text, **extra}}]}

    initial = tick([first, second], Model([say("小岚，能看见。", reply_to_row_ids=["52"])]))
    first_reply = initial["receipts"][-1]
    assert first_reply["reply_candidate_row_ids"] == ["52", "51"]
    assert first_reply["reply_to_row_ids"] == ["52"]

    class ResumeIfPresent:
        async def chat_tools(self, *, messages, **kwargs):
            if f'"{first.text}"' in messages[-1]["content"]:
                return say("小池，我在，你慢慢说。")
            return {"content": ""}

    next_turn = tick([next_peer], ResumeIfPresent())
    if not requester_followed_up:
        assert next_turn["receipts"][-1]["reply_resumed_row_id"] == "51"
    tick([later_peer], ResumeIfPresent())
    expected = [(["小岚，能看见。"], "")]
    if not requester_followed_up:
        expected.append((["小池，我在，你慢慢说。"], ""))
    assert emissions == expected


def test_request_recall_includes_other_named_people():
    from loop import run

    row51 = SimpleNamespace(msg_id=51, text="@鲸鲸 帮我看 A", speaker="小池", at=100)
    row52 = SimpleNamespace(msg_id=52, text="@鲸鲸 帮我看 B", speaker="小岚", at=101)
    sources = [(row51, "evt-51"), (row52, "evt-52")]
    selected = [(row52, "evt-52")]
    addressed = {
        "request_row_id": "52",
        "request_context_row_ids": (),
        "other_named_requests": (
            {"who": "小池", "request_text": "@鲸鲸 帮我看 A", "request_row_id": "51"},
        ),
    }
    recall = run._request_recall_sources(addressed, sources, selected)
    assert [(str(row.msg_id), ref) for row, ref in recall] == [("52", "evt-52"), ("51", "evt-51")]

    addressed["request_context_row_ids"] = ("51",)
    recall = run._request_recall_sources(addressed, sources, selected)
    assert [(str(row.msg_id), ref) for row, ref in recall] == [("52", "evt-52"), ("51", "evt-51")]


def test_recall_query_keeps_both_speakers_under_budget():
    from loop import run

    row1 = SimpleNamespace(is_self=False, speaker="小池", text="一" * 300)
    row2 = SimpleNamespace(is_self=False, speaker="小岚", text="@鲸鲸 帮我核对署名")
    q = run._recall_query([row1, row2], [])

    assert len(q) <= 200
    assert "小池:" in q and "小岚:" in q
    assert "帮我核对署名" in q
    assert q.count("一") <= 100


def test_recall_query_keeps_single_speaker_under_budget():
    from loop import run

    row = SimpleNamespace(is_self=False, speaker="小池", text="一" * 400)
    q = run._recall_query([row], [])

    assert len(q) <= 200
    assert "小池:" in q
    assert q.count("一") <= 200


def test_named_followup_recalls_the_work_title_without_using_an_interjection(runtime, monkeypatch):
    from core.memory.contracts import MemoryItem
    from loop import run

    brain, tmp = runtime
    chat = "room@chatroom"
    rows = [SimpleNamespace(text=text, sender_id=sender, msg_id=i, at=100 + i, speaker=sender, kind="text")
            for i, sender, text in ((1, "u1", "@鲸鲸 帮我核对《候鸟手册》的署名。"),
                                    (2, "u1", "只看第二版。"),
                                    (3, "u2", "我有个《海盐来信》的问题。"))]
    monkeypatch.setattr(brain.ports, "me_aliases", lambda: ("鲸鲸",))
    monkeypatch.setattr(brain.ports, "resolve_chat_id", lambda target: target)
    path = "docs/bird-handbook-v2.md"
    queries, reads = [], []

    class Memory:
        def search(self, context, query, **kwargs):
            queries.append(query)
            if "候鸟手册" in query and "第二版" in query:
                return [MemoryItem(memory_id="draft-v2", kind="personal", content=f"候鸟手册第二版在 {path}")]
            return []

    async def read_file(name, args):
        reads.append((name, args["path"]))
        return brain.ports.Result(True, text="第二版署名是小汐。")

    class RespondToRecall:
        async def chat_tools(self, *, messages, **kwargs):
            if messages[-1]["role"] == "tool":
                text = "第二版署名是小汐。"
            elif path in messages[-1]["content"]:
                return {"tool_calls": [{"id": "read", "name": "read_file", "arguments": {"path": path}}]}
            else:
                text = "你说的是哪份稿子？"
            return {"tool_calls": [{"id": "reply", "name": "say", "arguments": {"target": chat, "text": text}}]}

    monkeypatch.setattr("core.memory.service.MemoryService", Memory)
    monkeypatch.setattr(brain.ports, "act", read_file)
    addressed = run._cell_addressing({"chat_id": chat, "chat_type": "group", "after_mine": rows})
    sources = [(row, f"evt-{row.msg_id}") for row in rows]
    selected = [pair for pair in sources if str(pair[0].msg_id) == addressed.get("request_row_id")]
    recall = run._request_recall_sources(addressed, sources, selected)
    receipts = drive(brain.run_loop(
        str(tmp / "wechat" / "acct" / "account.db"), "\n".join(row.text for row in rows),
        llm=RespondToRecall(), max_steps=2, dry_run=True, rehearse=True,
        platform="wechat", chat_id=chat, focus=chat, wake={"addressed": addressed},
        recall_query=run._recall_query([row for row, _ in recall], []),
        recall_refs=tuple(ref for _, ref in recall),
    ))

    assert addressed["request_text"] == "只看第二版。"
    assert [ref for _, ref in recall] == ["evt-2", "evt-1"]
    assert queries[0].startswith("u1: 只看第二版。") and "海盐来信" not in queries[0]
    assert reads == [("read_file", path)] and receipts[-1]["text"] == "第二版署名是小汐。"


def test_live_fixture_runner_keeps_appraisal_ablation_effective(runtime):
    from evals import live_turn

    _, tmp = runtime
    case = next(case for case in live_turn.load_cases() if case["id"] == "p11b")
    full = drive(live_turn._run(case, "full", tmp))
    removed = drive(live_turn._run(case, "without_appraisal", tmp))
    assert "触发消息：e1" in next(part["text"] for part in full["prompt"]["parts"]
                                    if part["name"] == "我对当前局面的评价")
    assert "我对当前局面的评价" not in {part["name"] for part in removed["prompt"]["parts"]}


@pytest.mark.parametrize("kind, second_target, max_steps, retried, visible", [
    ("required", "room@chatroom", 4, True, True),
    ("required", "other@chatroom", 4, True, False),
    ("required", "", 4, True, False),
    ("required", "room@chatroom", 1, False, False),
    ("optional", "room@chatroom", 4, False, False),
    ("other_room", "room@chatroom", 4, False, False),
])
def test_draft_only_retries_a_required_reply_in_the_same_room(
    runtime, monkeypatch, kind, second_target, max_steps, retried, visible,
):
    brain, tmp = runtime
    chat = "room@chatroom"
    monkeypatch.setattr(brain.ports, "resolve_chat_id", lambda target: target)
    draft = "这段是我内部的草稿。"
    calls = [{"id": "reply", "name": "say",
              "arguments": {"target": second_target, "text": "今晚我没法一起玩。"}}] if second_target else []
    llm = Model([
        {"content": draft},
        {"content": "", "tool_calls": calls},
    ])
    wake = {"addressed": {"kind": "named", "must_answer": kind != "optional",
                          "chat_id": "other@chatroom" if kind == "other_room" else chat,
                          "request_text": "@鲸鲸 今晚一起玩吗？"}}
    receipts = drive(brain.run_loop(
        str(tmp / "wechat" / "acct" / "account.db"), "群里的游戏邀约",
        llm=llm, max_steps=max_steps, dry_run=True, rehearse=True,
        platform="wechat", chat_id=chat, focus=chat, wake=wake,
    ))
    assert len(llm.prompts) == (2 if retried else 1)
    if retried:
        assert [schema["function"]["name"] for schema in llm.tool_schemas[1]] == ["say"]
        assert draft not in str(llm.prompts[1])
        assert '"@鲸鲸 今晚一起玩吗？"' in llm.prompts[1][-1]["content"]
        assert "这句是本轮有人对我说、或接着我的话在说" in llm.prompts[1][-1]["content"]
    sent = [row["text"] for row in receipts if row["act"] == "say" and row["result"] == "rehearsal"]
    assert sent == (["今晚我没法一起玩。"] if visible else [])


@pytest.mark.parametrize("group_allowed", [False, True])
def test_required_reply_retry_uses_existing_group_send_gate(runtime, monkeypatch, group_allowed):
    brain, tmp = runtime
    chat = "room@chatroom"
    writes = []

    async def say_many(parts, *, dry_run, chat_id, turn_id):
        assert dry_run and chat_id == chat
        writes.extend(parts)
        return brain.ports.Result(True, data={"ack": "dry_run"})

    monkeypatch.setattr(brain.ports, "say_many", say_many)
    monkeypatch.setattr(brain.ports, "resolve_chat_id", lambda target: target)
    monkeypatch.setattr(brain.ports, "resolve_say_target", lambda target: (target, ""))
    monkeypatch.setattr(brain.ledger, "blocked_targets", lambda: {})
    monkeypatch.setattr(brain.selfstate, "touch_said", lambda *args: pytest.fail("dry run wrote sent memory"))
    monkeypatch.setattr(brain, "ALLOW_GROUP_SAY", group_allowed)
    llm = Model([{"content": "内部草稿"}, {"tool_calls": [
        {"id": "reply", "name": "say", "arguments": {"target": chat, "text": "我在，直接说吧。"}}
    ]}])
    receipts = drive(brain.run_loop(
        str(tmp / "wechat" / "acct" / "account.db"), "对方点了鲸鲸的名",
        llm=llm, max_steps=4, dry_run=True, platform="wechat", chat_id=chat, focus=chat,
        wake={"addressed": {"kind": "named", "chat_id": chat, "must_answer": True,
                            "request_text": "@鲸鲸 你还在吗？"}},
    ))
    assert len(llm.prompts) == 2
    assert writes == (["我在，直接说吧。"] if group_allowed else [])
    assert [(row["act"], row["result"]) for row in receipts] == [
        ("say", "rehearsal" if group_allowed else "blocked")
    ]


@pytest.mark.parametrize("tool", ["say", "show", "sticker"])
def test_addressed_reply_cannot_use_another_chat(runtime, monkeypatch, tool):
    brain, tmp = runtime
    chat, other = "room@chatroom", "other@chatroom"
    deliveries = []

    async def say_many(parts, *, dry_run, chat_id, turn_id):
        assert dry_run
        deliveries.append((chat_id, parts))
        return brain.ports.Result(True, data={"ack": "dry_run"})

    async def show(target, text, **kwargs):
        deliveries.append((target, [text]))
        return brain.ports.Result(True, data={"ack": "dry_run", "frames": 1})

    monkeypatch.setattr(brain.ports, "say_many", say_many)
    monkeypatch.setattr(brain.ports, "show", show)
    monkeypatch.setattr(brain.ports, "resolve_chat_id", lambda target: target)
    monkeypatch.setattr(brain.ports, "resolve_say_target", lambda target: (chat if target == "茶话会" else target, ""))
    monkeypatch.setattr(brain.ledger, "blocked_targets", lambda: {})
    monkeypatch.setattr(brain, "ALLOW_GROUP_SAY", True)
    content = {"key": "wave"} if tool == "sticker" else {"text": "我在，你慢慢说。"}
    retry = {**content, "target": "茶话会"} if tool == "say" else content
    llm = Model([
        {"tool_calls": [{"id": "wrong", "name": tool, "arguments": {**content, "target": other}}]},
        {"tool_calls": [{"id": "right", "name": tool, "arguments": retry}]},
    ])
    receipts = drive(brain.run_loop(
        str(tmp / "wechat" / "acct" / "account.db"), "本群点名",
        llm=llm, max_steps=2, dry_run=True, rehearse=tool != "say",
        platform="wechat", chat_id=chat, focus=other, wake={"addressed": {
            "kind": "named", "chat_id": chat, "must_answer": True, "request_text": "@鲸鲸 你在吗？",
        }},
    ))

    assert receipts[0]["blocked_reason"] == "reply_target_mismatch"
    assert chat in llm.prompts[1][-1]["content"]
    assert (receipts[-1]["target"], receipts[-1]["result"]) == (chat, "rehearsal")
    assert deliveries == ([(chat, [content["text"]])] if tool == "say" else [])


def test_media_rehearsal_is_visible_as_a_rehearsal_to_the_next_model_call(runtime, monkeypatch):
    brain, tmp = runtime
    chat = "private"

    async def show(target, text, *, frames, footer, dry_run):
        assert (target, text, dry_run) == (chat, "慢慢来，我在这儿。", True)
        return brain.ports.Result(True, data={"ack": "dry_run", "frames": 1})

    monkeypatch.setattr(brain.ports, "show", show)
    llm = Model([
        {"tool_calls": [{"id": "image", "name": "show", "arguments": {
            "target": chat, "text": "慢慢来，我在这儿。"}}]},
        {"tool_calls": [{"id": "wait", "name": "rest", "arguments": {"why": "先等等"}}]},
    ])
    receipts = drive(brain.run_loop(
        str(tmp / "wechat" / "acct" / "account.db"), "",
        llm=llm, max_steps=2, dry_run=True, platform="wechat", chat_id=chat, focus=chat,
    ))

    assert receipts[0]["result"] == "rehearsal"
    assert llm.prompts[1][-1]["content"] == "图片（1 张）：这是预演，没有实际发送。"


@pytest.mark.parametrize("ack,result,fact", [
    ("queued", "sent", "已排进投递队列，送达还未确认"),
    ("skipped", "skipped", "这次没有提交投递"),
])
def test_say_observation_distinguishes_queuing_and_skipping(runtime, monkeypatch, ack, result, fact):
    from loop import direct_send

    brain, _ = runtime

    async def say_many(parts, **kwargs):
        assert parts == ["我等你把话说完。"]
        return brain.ports.Result(True, data={"ack": ack})

    monkeypatch.setattr(direct_send, "recently_sent", lambda *args: False)
    monkeypatch.setattr(brain.ports, "say_many", say_many)
    monkeypatch.setattr(brain.ports, "resolve_say_target", lambda target: (target, ""))
    monkeypatch.setattr(brain.ports, "resolve_chat_id", lambda target: target)
    monkeypatch.setattr(brain.ledger, "blocked_targets", lambda: {})
    monkeypatch.setattr(brain.selfstate, "touch_said", lambda *args: pytest.fail("attempt wrote sent memory"))
    observation, receipt = drive(brain._exec(
        "say", {"target": "private", "text": "我等你把话说完。"},
        db="", dry_run=False, turn_id="local-test",
    ))

    assert receipt["result"] == result
    assert observation.startswith(fact)
    assert brain.ledger.delivery_fact(receipt) == fact


def test_addressed_turn_keeps_tool_choice_open_until_reply(runtime, monkeypatch):
    brain, tmp = runtime
    monkeypatch.setattr(brain.ports, "resolve_chat_id", lambda name: "room@chatroom")
    monkeypatch.setattr(brain.selfstate, "update", lambda **kwargs: "")
    llm = Model([
        {"content": "", "tool_calls": [
            {"id": "state-1", "name": "myself", "arguments": {"mood": "有点担心"}}]},
        {"content": "", "tool_calls": [
            {"id": "state-2", "name": "myself", "arguments": {"mood": "先听她说"}}]},
        {"content": "", "tool_calls": [
            {"id": "reply", "name": "say", "arguments": {
                "target": "room@chatroom", "text": "我在，你慢慢说。"}}]},
    ])
    receipts = drive(brain.run_loop(
        str(tmp / "wechat" / "acct" / "account.db"),
        "★1 群「茶话会」 15:30 @鲸鲸 你在吗",
        llm=llm, max_steps=3, dry_run=True, rehearse=True,
        platform="wechat", chat_id="room@chatroom",
        sender_id="u1", visible_event_ids=("evt-current",), focus="room@chatroom",
    ))
    assert llm.tool_choices == ["auto"] * 3
    assert [(r["act"], r["result"]) for r in receipts if r["act"] == "say"] == [
        ("say", "rehearsal")
    ]


def test_direct_read_file_reaches_the_read_only_tool_and_its_result_reaches_the_model(
    runtime, monkeypatch,
):
    brain, tmp = runtime
    seen = []

    async def fake_act(name, args):
        seen.append((name, args))
        return brain.ports.Result(True, text="展板稿 B\n第一步：确认这块展板给谁看。")

    monkeypatch.setattr(brain.ports, "act", fake_act)
    monkeypatch.setattr(brain.ports, "resolve_chat_id", lambda target: "room@chatroom")
    llm = Model([
        {"content": "", "tool_calls": [
            {"id": "read", "name": "read_file", "arguments": {"path": "docs/b.md"}}]},
        {"content": "", "tool_calls": [
            {"id": "reply", "name": "say", "arguments": {
                "target": "room@chatroom", "text": "B 稿第一步是确认展板给谁看。"}}]},
    ])
    receipts = drive(brain.run_loop(
        str(tmp / "wechat" / "acct" / "account.db"), "对方让我读 docs/b.md",
        llm=llm, max_steps=3, dry_run=True, rehearse=True,
        platform="wechat", chat_id="room@chatroom", focus="room@chatroom",
    ))

    assert seen == [("read_file", {"path": "docs/b.md"})]
    observation = next(message["content"] for message in llm.prompts[1]
                       if message.get("role") == "tool")
    assert "第一步：确认这块展板给谁看" in observation
    assert "上面的内容已经读到" in observation
    assert "这条回执只确认读取" in observation
    assert "后续事项按各自的执行回执说明进度" in observation
    assert [(row["act"], row["result"]) for row in receipts] == [
        ("do", "ok"),
        ("say", "rehearsal"),
    ]
    assert receipts[0]["path"] == "docs/b.md"
    assert brain.LAST_REHEARSAL["steps"][0]["tool"] == "read_file"
    assert '"path": "docs/b.md"' in brain.LAST_REHEARSAL["steps"][0]["args"]


def test_explicit_only_candidate_blocks_reading_a_file_excluded_by_the_current_request(
    runtime, monkeypatch,
):
    brain, tmp = runtime
    executed = []

    async def fake_act(name, args):
        executed.append((name, args))
        return brain.ports.Result(True, text="B 稿正文")

    monkeypatch.setattr(brain.ports, "act", fake_act)
    monkeypatch.setattr(brain.ports, "resolve_chat_id", lambda target: "room@chatroom")
    llm = Model([
        {"content": "", "tool_calls": [
            {"id": "old", "name": "read_file", "arguments": {"path": "docs/a.md"}}]},
        {"content": "", "tool_calls": [
            {"id": "current", "name": "read_file", "arguments": {"path": "docs/b.md"}}]},
        {"content": "", "tool_calls": [
            {"id": "reply", "name": "say", "arguments": {
                "target": "room@chatroom", "text": "B 稿适合今天。"}}]},
    ])
    wake = {"addressed": {
        "kind": "named", "chat_id": "room@chatroom", "must_answer": True,
        "request_text": "docs/a.md 和 docs/b.md 都看看，只有 B 按今天改过。选一份适合今天的。",
    }}
    receipts = drive(brain.run_loop(
        str(tmp / "wechat" / "acct" / "account.db"), "选稿",
        llm=llm, max_steps=4, dry_run=True, rehearse=True,
        platform="wechat", chat_id="room@chatroom", focus="room@chatroom", wake=wake,
    ))

    assert executed == [("read_file", {"path": "docs/b.md"})]
    assert receipts[0]["act"] == "read_file" and receipts[0]["result"] == "blocked"
    assert receipts[0]["blocked_reason"] == "current_request_excludes_file"
    assert any(message.get("role") == "tool" and "已经说明" in message.get("content", "")
               for message in llm.prompts[1])


def test_say_after_read_cannot_claim_the_file_will_be_opened_later(runtime, monkeypatch):
    brain, tmp = runtime

    async def fake_act(name, args):
        return brain.ports.Result(True, text="B 稿第一步：确认展板给谁看。")

    monkeypatch.setattr(brain.ports, "act", fake_act)
    monkeypatch.setattr(brain.ports, "resolve_chat_id", lambda target: "room@chatroom")
    llm = Model([
        {"content": "", "tool_calls": [
            {"id": "read", "name": "read_file", "arguments": {"path": "docs/b.md"}}]},
        {"content": "", "tool_calls": [
            {"id": "bad", "name": "say", "arguments": {
                "target": "room@chatroom", "text": "好的，我先打开 B 稿。"}}]},
        {"content": "", "tool_calls": [
            {"id": "good", "name": "say", "arguments": {
                "target": "room@chatroom", "text": "B 稿第一步是确认展板给谁看。"}}]},
    ])
    receipts = drive(brain.run_loop(
        str(tmp / "wechat" / "acct" / "account.db"), "读取 B 稿",
        llm=llm, max_steps=4, dry_run=True, rehearse=True,
        platform="wechat", chat_id="room@chatroom", focus="room@chatroom",
    ))

    assert [row["result"] for row in receipts if row["act"] == "say"] == ["blocked", "rehearsal"]
    assert receipts[::-1][0]["text"] == "B 稿第一步是确认展板给谁看。"
    assert "这一拍已经成功读取了「docs/b.md」" in str(llm.prompts[2])
    assert llm.tool_choices[2] == {"type": "function", "function": {"name": "say"}}


def test_successful_read_does_not_block_intent_to_open_a_different_file(runtime, monkeypatch):
    brain, tmp = runtime
    chat = "room@chatroom"

    async def fake_act(name, args):
        assert name == "read_file"
        if args["path"] == "docs/a.md":
            return brain.ports.Result(True, text="A 稿有标题和大纲。")
        assert args["path"] == "docs/b.md"
        return brain.ports.Result(False, error="文件暂时读不到")

    monkeypatch.setattr(brain.ports, "act", fake_act)
    monkeypatch.setattr(brain.ports, "resolve_chat_id", lambda target: target)
    llm = Model([
        {"tool_calls": [{"id": "a", "name": "read_file", "arguments": {"path": "docs/a.md"}}]},
        {"tool_calls": [{"id": "b", "name": "read_file", "arguments": {"path": "docs/b.md"}}]},
        {"tool_calls": [{"id": "reply", "name": "say", "arguments": {
            "target": chat, "text": "我先打开 B 稿。刚才没读到，等我再试一次。"}}]},
    ])
    receipts = drive(brain.run_loop(
        str(tmp / "wechat" / "acct" / "account.db"), "想对比两份稿子",
        llm=llm, max_steps=3, dry_run=True, rehearse=True,
        platform="wechat", chat_id=chat, focus=chat,
    ))

    assert [receipt["result"] for receipt in receipts] == ["ok", "error", "rehearsal"]
    assert receipts[-1]["text"] == "我先打开 B 稿。刚才没读到，等我再试一次。"


def test_corrected_plain_text_after_a_blocked_say_is_retried_through_say(runtime, monkeypatch):
    brain, tmp = runtime

    async def fake_act(name, args):
        return brain.ports.Result(True, text="B 稿第一步：确认展板给谁看。")

    monkeypatch.setattr(brain.ports, "act", fake_act)
    monkeypatch.setattr(brain.ports, "resolve_chat_id", lambda target: "room@chatroom")
    llm = Model([
        {"content": "", "tool_calls": [
            {"id": "read", "name": "read_file", "arguments": {"path": "docs/b.md"}}]},
        {"content": "", "tool_calls": [
            {"id": "bad", "name": "say", "arguments": {
                "target": "room@chatroom", "text": "我先打开 B 稿。"}}]},
        {"content": "我已经读取 B 稿，第一步是确认展板给谁看。"},
        {"content": "", "tool_calls": [
            {"id": "good", "name": "say", "arguments": {
                "target": "room@chatroom",
                "text": "我已经读取 B 稿，第一步是确认展板给谁看。"}}]},
    ])
    receipts = drive(brain.run_loop(
        str(tmp / "wechat" / "acct" / "account.db"), "读取 B 稿",
        llm=llm, max_steps=5, dry_run=True, rehearse=True,
        platform="wechat", chat_id="room@chatroom", focus="room@chatroom",
        wake={"addressed": {"kind": "named", "chat_id": "room@chatroom",
                            "must_answer": True, "request_text": "告诉我 B 稿第一步。"}},
    ))

    assert len(llm.prompts) == 3
    assert [row["result"] for row in receipts if row["act"] == "say"] == ["blocked", "rehearsal"]
    assert "上面的内容已经读到" in str(llm.prompts[2])


def test_repeated_false_future_read_fails_closed_without_visible_speech(runtime, monkeypatch):
    brain, tmp = runtime

    async def fake_act(name, args):
        return brain.ports.Result(True, text="A 稿第一步：量一下标题位置。")

    monkeypatch.setattr(brain.ports, "act", fake_act)
    monkeypatch.setattr(brain.ports, "resolve_chat_id", lambda target: "room@chatroom")
    bad = {"id": "bad", "name": "say", "arguments": {
        "target": "room@chatroom", "text": "我先打开 A 稿。"}}
    llm = Model([
        {"content": "", "tool_calls": [
            {"id": "read", "name": "read_file", "arguments": {"path": "docs/a.md"}}]},
        {"content": "", "tool_calls": [bad]},
        {"content": "", "tool_calls": [bad]},
    ])
    receipts = drive(brain.run_loop(
        str(tmp / "wechat" / "acct" / "account.db"), "读取 A 稿",
        llm=llm, max_steps=4, dry_run=True, rehearse=True,
        platform="wechat", chat_id="room@chatroom", focus="room@chatroom",
        wake={"addressed": {"kind": "named", "chat_id": "room@chatroom",
                            "must_answer": True, "request_text": "告诉我 A 稿第一步。"}},
    ))

    assert [row["result"] for row in receipts if row["act"] == "say"] == ["blocked", "blocked"]
    assert not [row for row in receipts if row["act"] == "say" and row["result"] == "rehearsal"]


def test_queued_and_confirmed_send_evidence_change_the_spoken_answer(runtime, monkeypatch):
    """同一句追问，只换那条 say 回执的本地发送证据，说出口的话必须跟着变。"""
    from db import world_db
    from loop import direct_send

    brain, tmp = runtime
    chat = "room@chatroom"
    prior_text = "我刚刚那两句：晚安，早点休息。"
    inquiry = "你刚才那两句发出去了吗？我这儿没看到。"
    monkeypatch.setattr(brain, "_speaking_block", _ORIGINAL["speaking_block"])
    monkeypatch.setattr(brain.ports, "resolve_chat_id", lambda target: target)
    monkeypatch.setattr(brain.ports, "resolve_say_target", lambda target: (target, ""))
    monkeypatch.setattr(brain, "ALLOW_GROUP_SAY", True)
    monkeypatch.setattr(brain.ledger, "blocked_targets", lambda: {})
    monkeypatch.setattr(brain.selfstate, "touch_said", lambda *args, **kwargs: None)
    monkeypatch.setattr(direct_send, "recently_sent", lambda *args: False)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    scope = {"platform": "wechat", "account_id": "acct", "chat_id": chat}

    def prior(ack, **extra):
        row = {"at": now, "act": "say", "result": "sent", "target": chat,
               "text": prior_text, "ack": ack, "turn_id": "turn-prior",
               "scope": dict(scope)}
        row.update(extra)
        return row

    queued_text = "那两句还在投递队列里，本地还没有确认发出。"
    confirmed_text = "那两句在我这边已有本地发送凭据，但我还不能确定你这儿是否已经看到。"
    seen = {"queued": "", "confirmed": ""}

    def run(send_row, send_data, expected_text, expected_ack, *, settle=None):
        sent = []

        class Respond:
            async def chat_tools(self, *, messages, **kwargs):
                prompt = "\n".join(message.get("content") or "" for message in messages
                                   if message.get("role") == "user")
                seen[expected_ack] = prompt
                text = confirmed_text if "不能说明对方已经收到或看到" in prompt else queued_text
                return {"tool_calls": [{"id": "reply", "name": "say", "arguments": {
                    "target": chat, "text": text}}]}

        async def say_many(parts, **kwargs):
            sent.extend(parts)
            return brain.ports.Result(True, data=dict(send_data))

        with monkeypatch.context() as patch:
            patch.setattr(brain.ledger, "read_receipts", lambda limit=300: [send_row])
            patch.setattr(brain.ledger, "_current_receipt_scope", lambda: (
                "wechat", "acct", chat,
            ))
            patch.setattr(brain.ledger, "recent_says_in", lambda *args, **kwargs: [])
            patch.setattr(brain.ledger, "recent_says", lambda limit=3: [])
            patch.setattr(brain.ports, "say_many", say_many)
            patch.setattr(world_db, "confirmed_queue_settled_ts",
                           lambda *args, **kwargs: settle)
            receipts = drive(brain.run_loop(
                str(tmp / "wechat" / "acct" / "account.db"), inquiry,
                llm=Respond(), max_steps=1, platform="wechat", chat_id=chat,
                sender_id="u1", user_message=inquiry, focus=chat, dry_run=False,
                wake={"addressed": {"kind": "named", "chat_id": chat,
                                    "must_answer": True, "request_text": inquiry}},
            ))
        says = [receipt for receipt in receipts if receipt.get("act") == "say"]
        assert says and says[-1]["text"] == expected_text
        assert sent == [expected_text]
        assert says[-1]["ack"] == expected_ack
        assert says[-1]["result"] == "sent"
        return says[-1]

    queued_receipt = run(
        prior("queued", queue_ids=[31], timing={"bubbles": 1}),
        {"ack": "queued", "queue_ids": [32]}, queued_text, "queued",
    )
    assert "[我最近开口]" not in seen["queued"]
    assert brain.ledger.delivery_fact(queued_receipt) == "已排进投递队列，送达还未确认"
    confirmed_receipt = run(
        prior("confirmed"), {"ack": "confirmed"}, confirmed_text, "confirmed",
    )
    assert "[我最近开口]" in seen["confirmed"]
    assert "不能说明对方已经收到或看到" in seen["confirmed"]
    assert brain.ledger.delivery_fact(confirmed_receipt) == "本地确认发出，送达还未确认"


def test_recent_send_statuses_stay_with_their_own_text(runtime, monkeypatch):
    """两条正文同时在视野里时，确认状态必须跟着各自的正文，不能说反。"""
    import time
    from db import world_db
    from loop import direct_send

    brain, tmp = runtime
    chat = "room@chatroom"
    inquiry = "旧稿和新稿现在分别到哪一步了？"
    final_text = "旧稿在我这边有本地确认发送记录，但我不能确定你已经看到；新稿还只排进了队列。"
    monkeypatch.setattr(brain, "_speaking_block", _ORIGINAL["speaking_block"])
    monkeypatch.setattr(brain.ledger, "recent_says_in", _ORIGINAL["recent_says_in"])
    monkeypatch.setattr(brain.ports, "resolve_chat_id", lambda target: target)
    monkeypatch.setattr(brain.ports, "resolve_say_target", lambda target: (target, ""))
    monkeypatch.setattr(brain, "ALLOW_GROUP_SAY", True)
    monkeypatch.setattr(brain.ledger, "blocked_targets", lambda: {})
    monkeypatch.setattr(brain.selfstate, "touch_said", lambda *args, **kwargs: None)
    monkeypatch.setattr(direct_send, "recently_sent", lambda *args: False)
    now = time.time()
    old_at = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now - 40))
    new_at = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now - 10))
    scope = {"platform": "wechat", "account_id": "acct", "chat_id": chat}
    rows = [
        {"at": old_at, "act": "say", "result": "sent", "target": chat,
         "text": "旧稿我这边已经确认发出。", "ack": "queued", "queue_ids": [71],
         "turn_id": "turn-old", "timing": {"bubbles": 1}, "scope": dict(scope)},
        {"at": new_at, "act": "say", "result": "sent", "target": chat,
         "text": "新稿还在队列里。", "ack": "queued", "queue_ids": [72],
         "turn_id": "turn-new", "timing": {"bubbles": 1}, "scope": dict(scope)},
    ]
    settled = time.mktime(time.strptime(old_at, "%Y-%m-%d %H:%M:%S"))
    sent = []
    seen = {}

    class Respond:
        async def chat_tools(self, *, messages, **kwargs):
            prompt = "\n".join(message.get("content") or "" for message in messages
                               if message.get("role") == "user")
            seen["prompt"] = prompt
            return {"tool_calls": [{"id": "reply", "name": "say", "arguments": {
                "target": chat, "text": final_text}}]}

    async def say_many(parts, **kwargs):
        sent.extend(parts)
        return brain.ports.Result(True, data={"ack": "confirmed"})

    with monkeypatch.context() as patch:
        patch.setattr(brain.ledger, "read_receipts", lambda limit=600: rows)
        patch.setattr(brain.ledger, "_current_receipt_scope",
                      lambda: ("wechat", "acct", chat))
        patch.setattr(brain.ports, "say_many", say_many)
        patch.setattr(
            world_db, "confirmed_queue_settled_ts",
            lambda qids, **kwargs: settled if qids == [71] else None)
        receipts = drive(brain.run_loop(
            str(tmp / "wechat" / "acct" / "account.db"), inquiry,
            llm=Respond(), max_steps=1, platform="wechat", chat_id=chat,
            sender_id="u1", user_message=inquiry, focus=chat, dry_run=False,
            wake={"addressed": {"kind": "named", "chat_id": chat,
                                "must_answer": True, "request_text": inquiry}},
        ))

    prompt = seen["prompt"]
    assert "旧稿我这边已经确认发出。｜本地确认发出，送达还未确认" in prompt
    assert "新稿还在队列里。｜已排进投递队列，送达还未确认" in prompt
    assert "最近一条本地确认的原话：「旧稿我这边已经确认发出。」" in prompt
    says = [receipt for receipt in receipts if receipt.get("act") == "say"]
    assert says[-1]["text"] == final_text
    assert says[-1]["ack"] == "confirmed"
    assert sent == [final_text]


def test_recent_action_prompt_keeps_image_and_sticker_type(runtime, monkeypatch):
    """上一轮发过图片或表情后，下一拍仍要知道动作类型和各自的发送状态。"""
    import time

    brain, tmp = runtime
    chat = "room@chatroom"
    monkeypatch.setattr(brain.ledger, "recent_says_in", _ORIGINAL["recent_says_in"])
    now = time.time()
    show_at = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now - 20))
    sticker_at = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now - 10))
    scope = {"platform": "wechat", "account_id": "acct", "chat_id": chat}
    rows = [
        {"at": show_at, "act": "show", "result": "sent", "target": chat,
         "text": "给你的图", "ack": "confirmed", "scope": dict(scope)},
        {"at": sticker_at, "act": "sticker", "result": "queued", "target": chat,
         "text": "点头", "ack": "queued", "queue_ids": [72],
         "turn_id": "turn-sticker", "timing": {"bubbles": 1}, "scope": dict(scope)},
    ]
    seen = {}

    class Respond:
        async def chat_tools(self, *, messages, **kwargs):
            seen["prompt"] = "\n".join(message.get("content") or "" for message in messages
                                       if message.get("role") == "user")
            return {"content": ""}

    with monkeypatch.context() as patch:
        patch.setattr(brain.ledger, "read_receipts", lambda limit=600: rows)
        patch.setattr(brain.ledger, "_current_receipt_scope",
                      lambda: ("wechat", "acct", chat))
        patch.setattr(brain.ports, "say_many",
                      lambda *args, **kwargs: pytest.fail("unexpected send"))
        drive(brain.run_loop(
            str(tmp / "wechat" / "acct" / "account.db"), "普通闲聊",
            llm=Respond(), max_steps=1, platform="wechat", chat_id=chat,
            sender_id="u1", user_message="刚才那张图挺可爱", focus=chat, dry_run=True,
        ))

    assert "展示：给你的图｜本地确认发出，送达还未确认" in seen["prompt"]
    assert "表情「点头」｜已排进投递队列，送达还未确认" in seen["prompt"]


def test_backfill_missing_metadata_fills_old_rows_without_rescanning_all(tmp_path, monkeypatch):
    """缺口回填只碰 platform_msg_id 为空的历史行，已填行绝不覆盖。"""
    from contextlib import closing
    from db import world_db

    world_path = tmp_path / "world.db"
    with closing(sqlite3.connect(world_path)) as c:
        for statement in world_db._SCHEMA:
            c.execute(statement)
        c.executemany(
            "insert into inbound_event(platform,account_id,chat_id,msg_id,ts_utc,platform_msg_id)"
            " values('wechat','acct',?,?,?,?)",
            (("room@chatroom", "wechat:acct:41", "2026-11-01T00:00:00", ""),
             ("room@chatroom", "wechat:acct:42", "2026-11-01T00:00:01", ""),
             ("room@chatroom", "wechat:acct:43", "2026-11-01T00:00:02", "server-43")),
        )
        c.commit()

    account_db = tmp_path / "wechat" / "acct" / "account.db"
    account_db.parent.mkdir(parents=True)
    with closing(sqlite3.connect(account_db)) as c:
        c.execute(
            "create table messages(id integer primary key, msg_id text, sender_id text,"
            " sender_name text, quote_reply_id text, quote_reply_user text)"
        )
        c.executemany(
            "insert into messages(id,msg_id,sender_id,sender_name,quote_reply_id,quote_reply_user)"
            " values(?,?,?,?,?,?)",
            ((41, "server-41", "u1", "小池", None, None),
             (42, "server-42", "u2", "小岚", None, None),
             (43, "server-43", "u3", "P6", None, None)),
        )
        c.commit()

    monkeypatch.setattr(world_db, "PATH", str(world_path))
    monkeypatch.setattr(world_db, "_READY", [False])
    monkeypatch.setattr(world_db, "_MEM", {"at": 0.0})
    monkeypatch.setattr(world_db, "_accounts", lambda: [("wechat", "acct", str(account_db))])

    result = world_db.backfill_missing_message_metadata(limit_per_account=100)
    assert result["scanned"] == 2
    assert result["updated"] == 2
    assert result["accounts"] == 1

    with closing(sqlite3.connect(world_path)) as c:
        c.row_factory = sqlite3.Row
        rows = {r["msg_id"]: dict(r) for r in c.execute(
            "select msg_id, platform_msg_id, who_id from inbound_event")}
    assert rows["wechat:acct:41"]["platform_msg_id"] == "server-41"
    assert rows["wechat:acct:41"]["who_id"] == "u1"
    assert rows["wechat:acct:42"]["platform_msg_id"] == "server-42"
    assert rows["wechat:acct:42"]["who_id"] == "u2"
    assert rows["wechat:acct:43"]["platform_msg_id"] == "server-43"

    again = world_db.backfill_missing_message_metadata(limit_per_account=100)
    assert again["scanned"] == 0
    assert again["updated"] == 0


def test_blank_first_response_on_a_required_turn_is_retried(runtime, monkeypatch):
    """2026-11-22：模型偶尔「什么都没返回」（视觉路由失败、空回包）。

    在必须回应的回合里那不是「她觉得没什么要说的」——先重试一次；重试正常就照常说。
    """
    brain, tmp = runtime
    chat = "room@chatroom"
    monkeypatch.setattr(brain.ports, "resolve_chat_id", lambda target: target)
    llm = Model([
        {"content": ""},
        {"content": "", "tool_calls": [{"id": "reply", "name": "say",
                                        "arguments": {"target": chat, "text": "在的，怎么了？"}}]},
    ])
    wake = {"addressed": {"kind": "named", "must_answer": True, "chat_id": chat,
                          "request_text": "@鲸鲸 你在吗？"}}
    receipts = drive(brain.run_loop(
        str(tmp / "wechat" / "acct" / "account.db"), "私聊里有人点名",
        llm=llm, max_steps=4, dry_run=True, rehearse=True,
        platform="wechat", chat_id=chat, focus=chat, wake=wake,
    ))
    assert len(llm.prompts) == 2
    sent = [row["text"] for row in receipts
            if row.get("act") == "say" and row.get("result") == "rehearsal"]
    assert sent == ["在的，怎么了？"]


def test_blank_twice_on_a_required_turn_is_recorded_as_a_failure(runtime, monkeypatch):
    brain, tmp = runtime
    chat = "room@chatroom"
    monkeypatch.setattr(brain.ports, "resolve_chat_id", lambda target: target)
    llm = Model([{"content": ""}, {"content": ""}])
    wake = {"addressed": {"kind": "named", "must_answer": True, "chat_id": chat,
                          "request_text": "@鲸鲸 你在吗？"}}
    # 试衣间不落 silent 回执（与生产同形但只留 trace），这里要走不 rehearse 的那条
    receipts = drive(brain.run_loop(
        str(tmp / "wechat" / "acct" / "account.db"), "私聊里有人点名",
        llm=llm, max_steps=4, dry_run=True, rehearse=False,
        platform="wechat", chat_id=chat, focus=chat, wake=wake,
    ))
    assert len(llm.prompts) == 2
    silent = [row for row in receipts if row.get("act") == "silent"]
    assert silent and "没有返回内容" in str(silent[0].get("why") or "")

# ---- 2026-11-24: earlier appraisal for the same speaker after a topic change ----

def _render_earlier(tmp, monkeypatch, *, chat="room@chatroom", sender="u1",
                    current="evt-new", now=None, events=None):
    scope = cc.Scope("wechat", "acct", chat)
    _event_store(monkeypatch, events if events is not None else [_event("evt-source"), _event("evt-new")])
    return ap.render_relevant_appraisal_for(scope, current_event_id=current,
                                            sender_id=sender, now=now)


def test_earlier_speaker_appraisal_survives_a_topic_change(runtime, monkeypatch):
    brain, tmp = runtime
    _record_appraisal(tmp, "room@chatroom", meaning="他说怕麻烦别人，我当时觉得他是在道歉")
    text = _render_earlier(tmp, monkeypatch)
    assert "他说怕麻烦别人" in text
    assert "以前的事" in text
    assert "对方当时说：evt-source" in text
    assert "我当时理解：" in text


def test_earlier_appraisal_is_not_shown_to_a_different_speaker(runtime, monkeypatch):
    brain, tmp = runtime
    _record_appraisal(tmp, "room@chatroom")
    assert _render_earlier(tmp, monkeypatch, sender="u2") == ""


def test_earlier_appraisal_expires_after_two_weeks(runtime, monkeypatch):
    from time import time as _now
    brain, tmp = runtime
    _record_appraisal(tmp, "room@chatroom")
    assert _render_earlier(tmp, monkeypatch, now=_now() + 15 * 86400) == ""


def test_current_event_appraisal_does_not_also_render_as_earlier(runtime, monkeypatch):
    brain, tmp = runtime
    _record_appraisal(tmp, "room@chatroom")
    text = _render_earlier(tmp, monkeypatch, current="evt-source")
    assert "我的理解：" in text
    assert "以前的事" not in text


def test_earlier_appraisal_stays_inside_its_chat(runtime, monkeypatch):
    brain, tmp = runtime
    _record_appraisal(tmp, "room@chatroom")
    events = [_event("evt-source"), _event("evt-new", chat="other@chatroom")]
    assert _render_earlier(tmp, monkeypatch, chat="other@chatroom", events=events) == ""


def test_earlier_appraisal_without_its_source_is_skipped(runtime, monkeypatch):
    brain, tmp = runtime
    _record_appraisal(tmp, "room@chatroom")
    assert _render_earlier(tmp, monkeypatch, events=[_event("evt-new")]) == ""

def test_run_loop_prompt_carries_the_earlier_appraisal_to_the_same_speaker(runtime, monkeypatch):
    brain, tmp = runtime
    chat = "room@chatroom"
    _record_appraisal(tmp, chat, meaning="他说怕麻烦别人，我当时觉得他是在道歉")
    _event_store(monkeypatch, [_event("evt-source", actor="u1"), _event("evt-new", actor="u1")])
    llm = Model([{"content": "", "tool_calls": []}])
    drive(brain.run_loop(str(tmp / "wechat" / "acct" / "account.db"), "test", llm=llm,
                         platform="wechat", chat_id=chat, sender_id="u1",
                         wake={"addressed": {"kind": "named", "chat_id": chat, "must_answer": False}},
                         visible_event_ids=("evt-new",), max_steps=1, dry_run=True))
    user = llm.prompts[0][1]["content"]
    assert "以前的事" in user
    assert "他说怕麻烦别人" in user


def test_run_loop_prompt_keeps_the_earlier_appraisal_from_another_speaker(runtime, monkeypatch):
    brain, tmp = runtime
    chat = "room@chatroom"
    _record_appraisal(tmp, chat, meaning="他说怕麻烦别人，我当时觉得他是在道歉")
    _event_store(monkeypatch, [_event("evt-source", actor="u1"), _event("evt-new", actor="u2")])
    llm = Model([{"content": "", "tool_calls": []}])
    drive(brain.run_loop(str(tmp / "wechat" / "acct" / "account.db"), "test", llm=llm,
                         platform="wechat", chat_id=chat, sender_id="u2",
                         wake={"addressed": {"kind": "named", "chat_id": chat, "must_answer": False}},
                         visible_event_ids=("evt-new",), max_steps=1, dry_run=True))
    user = llm.prompts[0][1]["content"]
    assert "他说怕麻烦别人" not in user

def test_three_turns_keep_the_conflict_and_the_mood_for_the_same_person(runtime, monkeypatch):
    """受控多轮对照：冲突 → 换话题 → 道歉，同一个人的旧判断和心境要一直在。

    这不是真实模型验收：模型是脚本替身，只证明「提示词里确实带着那两样东西」，
    以及换个人说同样的话时不会串。
    """
    from dataclasses import replace
    from loop import selfstate

    brain, tmp = runtime
    chat = "room@chatroom"
    monkeypatch.setattr(selfstate, "render", _real_self_render)
    _record_appraisal(tmp, chat, meaning="他觉得我说话不算数，我心里有点慌",
                      feeling_text="心里一紧，有点愧疚")
    brain._fold_appraisal_mood({"feeling_text": "心里一紧，有点愧疚"},
                               SimpleNamespace(reference="evt-source"),
                               {"result": "committed"}, turn_id="t1")
    _event_store(monkeypatch, [
        _event("evt-source", actor="u1"),
        replace(_event("evt-rain", actor="u1"), content="对了，明天要下雨吗？"),
        replace(_event("evt-sorry", actor="u1"), content="对不起，刚才我说话冲了。"),
        replace(_event("evt-other", actor="u2"), content="在吗？"),
    ])

    def prompt(ref, sender):
        llm = Model([{"content": "", "tool_calls": []}])
        drive(brain.run_loop(str(tmp / "wechat" / "acct" / "account.db"), "test", llm=llm,
                             platform="wechat", chat_id=chat, sender_id=sender,
                             wake={"addressed": {"kind": "named", "chat_id": chat, "must_answer": False}},
                             visible_event_ids=(ref,), max_steps=1, dry_run=True))
        return llm.prompts[0][1]["content"]

    rain = prompt("evt-rain", "u1")
    assert "以前的事" in rain
    assert "他觉得我说话不算数" in rain
    assert "心境：心里一紧，有点愧疚" in rain

    sorry = prompt("evt-sorry", "u1")
    assert "以前的事" in sorry
    assert "他觉得我说话不算数" in sorry

    stranger = prompt("evt-other", "u2")
    assert "他觉得我说话不算数" not in stranger

def test_five_turn_group_keeps_attribution_across_interjection_and_apology(runtime, monkeypatch):
    """受控多轮：冲突 → 换话题 → 第三人插话 → 道歉。

    - 同一个人换话题、道歉时，旧判断要还在（跨话题延续）；
    - 第三个人说话时，不能把别人的旧判断贴给他；
    - 心境是「这个会话」的，不按人分。
    """
    from dataclasses import replace
    from loop import selfstate

    brain, tmp = runtime
    chat = "room@chatroom"
    monkeypatch.setattr(selfstate, "render", _real_self_render)
    _record_appraisal(tmp, chat, meaning="他觉得我说话不算数，我心里有点慌",
                      feeling_text="心里一紧，有点愧疚")
    brain._fold_appraisal_mood(
        {"feeling_text": "心里一紧，有点愧疚"},
        SimpleNamespace(reference="evt-source", scope=cc.Scope("wechat", "acct", chat)),
        {"result": "committed"}, turn_id="t1")
    _event_store(monkeypatch, [
        _event("evt-source", actor="u1"),
        replace(_event("evt-newtopic", actor="u1"), content="对了，明天要下雨吗？"),
        replace(_event("evt-interject", actor="u2"), content="我也在。"),
        replace(_event("evt-apology", actor="u1"), content="对不起，刚才我说话冲了。"),
    ])

    def prompt(ref, sender):
        llm = Model([{"content": "", "tool_calls": []}])
        drive(brain.run_loop(str(tmp / "wechat" / "acct" / "account.db"), "test", llm=llm,
                             platform="wechat", chat_id=chat, sender_id=sender,
                             wake={"addressed": {"kind": "named", "chat_id": chat, "must_answer": False}},
                             visible_event_ids=(ref,), max_steps=1, dry_run=True))
        return llm.prompts[0][1]["content"]

    u1_new = prompt("evt-newtopic", "u1")
    assert "以前的事" in u1_new and "他觉得我说话不算数" in u1_new
    assert "心境：心里一紧，有点愧疚" in u1_new

    u2 = prompt("evt-interject", "u2")
    assert "他觉得我说话不算数" not in u2
    assert "心境：心里一紧，有点愧疚" in u2

    apology = prompt("evt-apology", "u1")
    assert "以前的事" in apology and "他觉得我说话不算数" in apology


def test_earlier_appraisal_still_reads_two_days_later(runtime, monkeypatch):
    from time import time as _now

    brain, tmp = runtime
    _record_appraisal(tmp, "room@chatroom", meaning="他说怕麻烦别人，我当时觉得他是在道歉")
    text = _render_earlier(tmp, monkeypatch, now=_now() + 2 * 86400)
    assert "他说怕麻烦别人" in text


def test_earlier_appraisal_disappears_when_its_evidence_dies(runtime, monkeypatch):
    from core.memory import belief_commit

    brain, tmp = runtime
    _record_appraisal(tmp, "room@chatroom", meaning="他说怕麻烦别人，我当时觉得他是在道歉")
    monkeypatch.setattr(belief_commit, "evidence_state", lambda ref, scope, **kw: "dead")
    assert _render_earlier(tmp, monkeypatch) == ""

def test_earlier_block_carries_what_is_still_unresolved(runtime, monkeypatch):
    brain, tmp = runtime
    _record_appraisal(tmp, "room@chatroom", meaning="他说怕麻烦别人，我当时觉得他是在道歉",
                      unresolved_condition="他还没说要不要我帮忙")
    text = _render_earlier(tmp, monkeypatch)
    assert "还没放下：他还没说要不要我帮忙" in text

def test_unaddressed_turns_do_not_get_the_earlier_judgement(runtime, monkeypatch):
    """没人对她说时，旧判断不该端到她眼前（11-24 群里 40 个提示词里同一条旧评价重复出现）。"""
    brain, tmp = runtime
    _record_appraisal(tmp, "room@chatroom", meaning="他说怕麻烦别人，我当时觉得他是在道歉")
    _event_store(monkeypatch, [_event("evt-source"), _event("evt-new")])
    scope = cc.Scope("wechat", "acct", "room@chatroom")
    quiet = ap.render_relevant_appraisal_for(scope, current_event_id="evt-new", sender_id="u1", addressed=False)
    assert quiet == ""
    talked = ap.render_relevant_appraisal_for(scope, current_event_id="evt-new", sender_id="u1", addressed=True)
    assert "以前的事" in talked
