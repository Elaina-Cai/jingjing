from types import SimpleNamespace

import pytest

from core.speech import claim_consistency
from core.speech.outbound import OutboundGateway


@pytest.mark.parametrize(("text", "blocked"), [
    ("小水，我在。你方便说吗？", True),
    ("小水，我在。以后我叫你P6。", True),
    ("小水，你方便说吗？", True),
    ("刚才叫成小水，是我认错了。小水，你继续。", True),
    ("刚才叫成小水，是我认错了。P6，你慢慢说。", False),
    ("不是小水，是P6。", False),
    ("我之前叫过你小水吗？", False),
])
def test_old_name_checks_each_sentence(text, blocked):
    correction = {"old_value": "小水", "new_value": "P6"}
    match = claim_consistency.stale_corrected_memory_claim(text, [correction])
    assert match == (correction if blocked else None)


def test_corrected_fact_still_allows_a_question_about_the_old_value():
    correction = {"old_value": "深圳", "new_value": "上海"}
    assert claim_consistency.stale_corrected_memory_claim("深圳，还是北京？", [correction]) is None


def test_guard_memory_text_rewrites_uncertain_fact():
    text, audit = claim_consistency.guard_memory_text(
        "你住在深圳。",
        [{
            "content": "address = 深圳",
            "source_event_ids": ["evt-source"],
            "metadata": {"evidence_state": "unknown"},
        }],
    )

    assert text == (
        "这个我印象里有，但现在的依据没核实，我不敢当成确定的事说。"
        "你再提醒我一下原话好吗？"
    )
    assert audit["reason"] == "uncertain_memory_claim"


def test_guard_memory_text_rewrites_one_sided_conflict():
    text, audit = claim_consistency.guard_memory_text(
        "你住在深圳。",
        [
            {
                "content": "address = 深圳",
                "source_event_ids": ["evt-a"],
                "metadata": {
                    "conflict": True,
                    "subject_key": "person-a",
                    "predicate": "lives_in",
                },
            },
            {
                "content": "address = 北京",
                "source_event_ids": ["evt-b"],
                "metadata": {
                    "conflict": True,
                    "subject_key": "person-a",
                    "predicate": "lives_in",
                },
            },
        ],
    )

    assert "「深圳」和「北京」两种说法" in text
    assert audit["reason"] == "conflicted_memory_claim"


def test_outbound_gateway_reads_bound_memory_index(monkeypatch):
    entries = [{
        "content": "address = 深圳",
        "source_event_ids": ["evt-source"],
        "metadata": {"evidence_state": "unknown"},
    }]

    class Snapshot:
        def entries_for(self, context):
            return entries

    import core.memory.index_scope as index_scope

    monkeypatch.setattr(index_scope, "bound_index", lambda context: Snapshot())
    context = SimpleNamespace(extra={})

    text, audit = OutboundGateway(None)._guard_memory_claims(
        "你住在深圳。",
        context,
    )

    assert "依据没核实" in text
    assert audit["reason"] == "uncertain_memory_claim"


def test_outbound_gateway_allows_question_and_new_evidence(monkeypatch):
    entries = [{
        "content": "address = 深圳",
        "source_event_ids": ["evt-source"],
        "metadata": {"evidence_state": "unknown"},
    }]

    class Snapshot:
        def entries_for(self, context):
            return entries

    import core.memory.index_scope as index_scope

    monkeypatch.setattr(index_scope, "bound_index", lambda context: Snapshot())
    gateway = OutboundGateway(None)
    assert gateway._guard_memory_claims("你现在住深圳吗？", SimpleNamespace(extra={})) == (
        "你现在住深圳吗？",
        {},
    )
    assert gateway._guard_memory_claims(
        "你刚才说深圳。",
        SimpleNamespace(extra={"group_window_event_ids": ["evt-source"]}),
    ) == ("你刚才说深圳。", {})
    reminder = "提醒你：参加深圳项目会议。"
    assert gateway._guard_memory_claims(
        reminder,
        SimpleNamespace(extra={"memory_claim_supported_texts": [reminder]}),
    ) == (reminder, {})
    assert gateway._guard_memory_claims(
        reminder,
        SimpleNamespace(extra={"memory_claim_supported_texts": reminder}),
    ) == (reminder, {})


def test_proactive_trigger_binds_memory_before_agent(monkeypatch):
    from agents import proactive_trigger
    import core.terminal.memory_gateway as memory_gateway

    context = SimpleNamespace(extra={})
    seen = []
    monkeypatch.setattr(
        memory_gateway,
        "attach_terminal_memory",
        lambda value: seen.append(value) or "[index]",
    )

    assert proactive_trigger.bind_proactive_memory(context) == "[index]"
    assert seen == [context]


def test_claim_context_keys_pass_strict_extra_contract():
    from agents.base import AgentContext

    context = AgentContext(
        messages=[],
        extra={
            "memory_claim_supported_texts": ["提醒你参加深圳项目会议"],
            "_memory_claim_gate": {
                "action": "rewrite",
                "reason": "uncertain_memory_claim",
            },
        },
    )

    assert context.extra["memory_claim_supported_texts"] == [
        "提醒你参加深圳项目会议"
    ]
    assert context.extra["_memory_claim_gate"]["reason"] == "uncertain_memory_claim"


def test_reminder_content_is_supported_user_evidence(monkeypatch):
    import asyncio

    from agents import reminder_trigger

    seen = {}

    async def fake_run(**kwargs):
        seen.update(kwargs)
        return SimpleNamespace(ok=True, sent=True)

    monkeypatch.setattr(reminder_trigger, "run_proactive_turn", fake_run)
    result = asyncio.run(reminder_trigger.run_reminder_turn(
        platform="wechat",
        chat_id="c1",
        content="参加深圳项目会议",
    ))

    assert result.sent is True
    assert seen["supported_texts"] == ("参加深圳项目会议",)


def test_default_proactive_prompt_does_not_invite_unverified_memory():
    from agents.proactive_trigger import (
        build_proactive_prompt,
        prompt_for_trigger,
        prompt_with_proactive_memory,
    )

    prompt = build_proactive_prompt()

    assert "只结合当前会话里可核实的历史和记忆" in prompt
    assert "来源冲突" in prompt
    assert "不要声称自己记得" in prompt
    assert build_proactive_prompt("只看这句话") == "只看这句话"
    assert prompt_with_proactive_memory(prompt, "[记忆索引]") == (
        prompt + "\n\n[本轮可用记忆]\n[记忆索引]\n"
        "只使用这里有依据的内容；没有依据的事不要补写。"
    )
    assert prompt_with_proactive_memory(prompt, "") == prompt
    assert prompt_for_trigger(
        "只说这条提醒",
        "[记忆索引]",
        trigger_kind="reminder",
    ) == "只说这条提醒"
    assert "[本轮可用记忆]" in prompt_for_trigger(
        prompt,
        "[记忆索引]",
        trigger_kind="proactive",
    )

def test_replace_offending_clause_keeps_non_offending_clause():
    text = claim_consistency.replace_offending_clause(
        "嗯，我懂你说的那种累，不过你是在杭州对吧？",
        ["杭州"],
        "这个我记不准。",
    )
    assert "我懂你说的那种累" in text
    assert "杭州" not in text
    assert "这个我记不准" in text


def test_replace_offending_clause_replaces_single_clause():
    assert claim_consistency.replace_offending_clause(
        "你是在杭州对吧？",
        ["杭州"],
        "这个我记不准。",
    ) == "这个我记不准。"


def test_replace_offending_clause_falls_back_when_value_is_missing():
    replacement = "这个我记不准。"
    assert claim_consistency.replace_offending_clause(
        "你今天听起来很累。",
        ["杭州"],
        replacement,
    ) == replacement


def test_replace_offending_clause_does_not_repeat_replacement():
    text = claim_consistency.replace_offending_clause(
        "杭州今天下雨，杭州很潮",
        ["杭州"],
        "这个我记不准。",
    )
    assert text.count("这个我记不准") == 1


def test_guard_memory_text_conflict_only_replaces_offending_clause():
    text, audit = claim_consistency.guard_memory_text(
        "好啊，我记着你住杭州",
        [
            {
                "content": "address = 杭州",
                "source_event_ids": ["evt-a"],
                "metadata": {
                    "conflict": True,
                    "subject_key": "person-a",
                    "predicate": "lives_in",
                },
            },
            {
                "content": "address = 苏州",
                "source_event_ids": ["evt-b"],
                "metadata": {
                    "conflict": True,
                    "subject_key": "person-a",
                    "predicate": "lives_in",
                },
            },
        ],
    )
    assert "好啊" in text
    assert "两种说法" in text
    assert text != claim_consistency.safe_conflict_reply({
        "values": ["杭州", "苏州"],
    })
    assert audit["reason"] == "conflicted_memory_claim"

