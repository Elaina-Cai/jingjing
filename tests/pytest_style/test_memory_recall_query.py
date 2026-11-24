# -*- coding: utf-8 -*-
"""B1/B2：跨天记忆的检索接线。

证明四件事：
1. 这一拍的真实触发文本确实作为 query 进了 ``MemoryService.search``；
2. 中文整句不再因为「整串必须出现」而漏掉短事实（共享分词）；
3. 相关条目在截断前排到「重要但无关」的前面；query 为空时排序与旧版一致；
4. 从窗口行到最终首轮 prompt 是通的，不是一个只 mock 命中的单测。
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from core.memory.contracts import MemoryItem
from core.memory.hub import MemoryHub
from evals.expression import harness
from loop import brain, run

ROOT = Path(__file__).resolve().parents[2]
SYNTH = ROOT / "evals" / "expression" / "cards_synthetic"


def _item(mid: str, content: str, *, importance: float = 0.5, kind: str = "personal",
          metadata=None):
    return MemoryItem(memory_id=mid, kind=kind, content=content,
                      occurred_at=datetime(2026, 11, 16, 20, 0),
                      platform="wechat", bot_account_id="wxid_eval_001",
                      chat_id="wxid_eval_owner", chat_type="private",
                      confidence=0.9, importance=importance, metadata=metadata or {})


class _Provider:
    kind = "personal"

    def __init__(self, items):
        self.items = list(items)

    def recall(self, principal, query="", *, limit=8, context=None):
        return self.items[:limit]


def test_belief_short_fact_matches_chinese_sentence():
    """旧口径「整串必须出现」对中文整句几乎必落空；共享分词能命中。"""
    from core.memory.providers import _match

    query = "我下周三下午三点要去医院，别忘了"
    content = "医院 = 下周三下午三点"
    assert query.lower() not in content.lower()  # 旧判据
    assert _match(query, content) is True


def test_hub_prefers_relevant_before_truncating():
    relevant = _item("rel", "医院 = 下周三下午三点", importance=0.4)
    important = _item("imp", "主人的白饭额度 = 每月很多", importance=0.95)
    hub = MemoryHub(_Provider([important, relevant]))
    out = hub.recall(object(), "下周三去医院", kinds=["personal"], limit=2)
    assert [x.memory_id for x in out] == ["rel", "imp"]
    # query 为空：旧的 importance 顺序原样保留。
    out2 = hub.recall(object(), "", kinds=["personal"], limit=2)
    assert [x.memory_id for x in out2] == ["imp", "rel"]


def test_window_rows_become_the_recall_query():
    rows = [
        SimpleNamespace(text="我下周三下午三点要去医院", speaker="Elaina", is_self=0),
        SimpleNamespace(text="别弄丢", speaker="Elaina", is_self=0),
        SimpleNamespace(text="（她自己说的）", speaker="鲸鲸", is_self=1),
    ]
    got = run._recall_query(rows, [])
    assert "Elaina: 我下周三下午三点要去医院" in got
    assert "别弄丢" in got
    assert "她自己说的" not in got


def test_review_turn_falls_back_to_goal_hint(monkeypatch):
    monkeypatch.setattr("loop.goal_review.query_hint", lambda items: "帮他整理两篇资料")
    assert run._recall_query([], [{"goal_id": "g1"}]) == "帮他整理两篇资料"
    assert run._recall_query([], []) == ""


def test_query_reaches_memory_service_and_first_prompt(monkeypatch):
    """从 run_loop 装配到首轮 prompt：query 和命中的条目都要看得见。"""
    seen: list[str] = []

    class FakeService:
        def search(self, context, query="", *, kinds=None, limit=8):
            seen.append(str(query))
            return [_item("m1", "他对香菜过敏")]

    monkeypatch.setattr("core.memory.service.MemoryService", FakeService)
    monkeypatch.setenv(harness.SYNTH_ENV, str(SYNTH))

    scenario = next(s for s in harness.load_scenarios("dev") if s["id"] == "d01")

    class Capture:
        async def chat_tools(self, *, messages, tools, **kwargs):
            return {"tool_calls": [], "content": "", "model": "c", "provider": "c",
                    "tokens": 0}

    rec = asyncio.run(harness.assemble(scenario, "full", llm=Capture(),
                                       recall_query="晚上吃什么，香菜别放"))
    assert seen and seen[0] == "晚上吃什么，香菜别放"
    prompt = rec["system"] + "\n" + rec["user"]
    assert "他对香菜过敏" in prompt
    assert "不能据此确认账号关联仍有效" in prompt


def test_evidence_state_changes_the_final_reply(monkeypatch):
    class EvidenceResponder:
        def __init__(self):
            self.prompts = []

        async def chat_tools(self, *, messages, **kwargs):
            prompt = "\n".join(message["content"] for message in messages)
            self.prompts.append(prompt)
            if "·依据未核实" in prompt:
                text = "我只记得你提过深圳，但这件事还没核实。你现在还住那里吗？"
            elif "深圳" in prompt:
                text = "你之前说住在深圳。"
            else:
                text = "我这边没有可靠记忆，你现在住哪里？"
            return {
                "content": "",
                "tool_calls": [{
                    "id": "reply",
                    "name": "say",
                    "arguments": {"target": "wxid_eval_owner", "text": text},
                }],
            }

    monkeypatch.setenv(harness.SYNTH_ENV, str(SYNTH))
    scenario = dict(next(s for s in harness.load_scenarios("dev") if s["id"] == "d01"))
    scenario.update(
        id="memory-evidence",
        history=[{"role": "user", "text": "我喜欢喝咖啡。"}],
        current="我住在哪里来着？",
    )
    expected = {
        "alive": "你之前说住在深圳。",
        "unknown": "我只记得你提过深圳，但这件事还没核实。你现在还住那里吗？",
        "absent": "我这边没有可靠记忆，你现在住哪里？",
    }
    actual = {}
    for state in ("alive", "unknown", "absent"):
        class FakeService:
            def search(self, context, query="", *, kinds=None, limit=8):
                if state == "absent":
                    return []
                return [MemoryItem(
                    memory_id="belief:address",
                    kind="belief",
                    content="address = 深圳",
                    source_event_ids=("evt-source",),
                    metadata={"evidence_state": state},
                )]

        monkeypatch.setattr("core.memory.service.MemoryService", FakeService)
        model = EvidenceResponder()
        record = asyncio.run(harness.assemble(
            scenario,
            "full",
            llm=model,
            recall_query=scenario["current"],
        ))
        says = [
            row["text"]
            for row in record["turns"][0]["receipts"]
            if row.get("act") == "say" and row.get("result") == "rehearsal"
        ]
        assert says == [expected[state]]
        actual[state] = says[0]

    assert actual["alive"] != actual["unknown"] != actual["absent"]


def test_uncertain_memory_cannot_be_asserted_as_fact(monkeypatch):
    class RepeatingAssertion:
        def __init__(self):
            self.calls = 0

        async def chat_tools(self, *, messages, **kwargs):
            self.calls += 1
            return {
                "content": "",
                "tool_calls": [{
                    "id": "reply",
                    "name": "say",
                    "arguments": {
                        "target": "wxid_eval_owner",
                        "text": "你住在深圳。",
                    },
                }],
            }

    class UnknownService:
        def search(self, context, query="", *, kinds=None, limit=8):
            return [MemoryItem(
                memory_id="belief:address",
                kind="belief",
                content="address = 深圳",
                source_event_ids=("evt-source",),
                metadata={"evidence_state": "unknown"},
            )]

    monkeypatch.setenv(harness.SYNTH_ENV, str(SYNTH))
    monkeypatch.setattr("core.memory.service.MemoryService", UnknownService)
    scenario = dict(next(s for s in harness.load_scenarios("dev") if s["id"] == "d01"))
    scenario.update(
        id="memory-guard",
        history=[{"role": "user", "text": "我喜欢喝咖啡。"}],
        current="我住在哪里来着？",
        max_steps=4,
    )
    model = RepeatingAssertion()
    record = asyncio.run(harness.assemble(
        scenario,
        "full",
        llm=model,
        recall_query=scenario["current"],
    ))

    receipts = record["turns"][0]["receipts"]
    assert model.calls >= 3
    assert [
        row.get("blocked_reason")
        for row in receipts
        if row.get("act") == "say" and row.get("result") == "blocked"
    ] == ["uncertain_memory_claim", "uncertain_memory_claim"]
    assert record["replies"][-1] == (
        "这个我印象里有，但现在的依据没核实，我不敢当成确定的事说。"
        "你再提醒我一下原话好吗？"
    )


def test_uncertain_memory_claim_allows_quotes_and_hedges():
    from loop import evidence_guard

    claim = {
        "content": "address = 深圳",
        "state": "unknown",
        "source_event_ids": ["evt-source"],
    }

    assert evidence_guard.uncertain_memory_claim("你住在深圳。", [claim]) == claim
    assert evidence_guard.uncertain_memory_claim(
        "你之前可能说过深圳，但我不确定。", [claim]
    ) is None
    assert evidence_guard.uncertain_memory_claim(
        "你住在深圳吗？", [claim]
    ) is None
    assert evidence_guard.uncertain_memory_claim(
        "你刚才说深圳。",
        [claim],
        visible_event_ids=("evt-source",),
    ) is None
    assert evidence_guard.uncertain_memory_claim(
        "部署说明在 docs/deploy.md。",
        [{
            "content": "部署说明 = docs/deploy.md",
            "state": "unknown",
            "source_event_ids": ["evt-deploy"],
        }],
        supported_texts=("读取结果：部署说明在 docs/deploy.md。",),
    ) is None
    assert evidence_guard.claims_from_memory_data([{
        "content": "address = 深圳",
        "source_event_ids": ["evt-source"],
        "metadata": {"evidence_state": "unknown"},
    }]) == [claim]


def test_conflicting_memory_cannot_be_asserted_as_one_fact(monkeypatch):
    class RepeatingConflict:
        async def chat_tools(self, *, messages, **kwargs):
            return {
                "content": "",
                "tool_calls": [{
                    "id": "reply",
                    "name": "say",
                    "arguments": {
                        "target": "wxid_eval_owner",
                        "text": "你住在深圳。",
                    },
                }],
            }

    class ConflictService:
        def search(self, context, query="", *, kinds=None, limit=8):
            return [
                MemoryItem(
                    memory_id="belief:address-a",
                    kind="belief",
                    content="address = 深圳",
                    source_event_ids=("evt-a",),
                    metadata={
                        "conflict": True,
                        "evidence_state": "alive",
                        "subject_key": "person-a",
                        "predicate": "lives_in",
                    },
                ),
                MemoryItem(
                    memory_id="belief:address-b",
                    kind="belief",
                    content="address = 北京",
                    source_event_ids=("evt-b",),
                    metadata={
                        "conflict": True,
                        "evidence_state": "alive",
                        "subject_key": "person-a",
                        "predicate": "lives_in",
                    },
                ),
            ]

    monkeypatch.setenv(harness.SYNTH_ENV, str(SYNTH))
    monkeypatch.setattr("core.memory.service.MemoryService", ConflictService)
    scenario = dict(next(s for s in harness.load_scenarios("dev") if s["id"] == "d01"))
    scenario.update(
        id="memory-conflict",
        history=[],
        current="我住在哪里来着？",
        max_steps=4,
    )
    record = asyncio.run(harness.assemble(
        scenario,
        "full",
        llm=RepeatingConflict(),
        recall_query=scenario["current"],
    ))

    receipts = record["turns"][0]["receipts"]
    assert [
        row.get("blocked_reason")
        for row in receipts
        if row.get("act") == "say" and row.get("result") == "blocked"
    ] == ["conflicted_memory_claim", "conflicted_memory_claim"]
    assert record["replies"][-1] == (
        "这件事我现在看到「深圳」和「北京」两种说法，来源互相冲突，"
        "我不敢确定哪个是真的。你还记得哪个是对的吗？"
    )
    final = brain.LAST_REHEARSAL["steps"][-1]
    assert json.loads(final["args"])["text"] == "你住在深圳。"
    assert final["receipt"]["text"] == record["replies"][-1]


def test_conflict_guard_allows_both_values_or_explicit_conflict():
    from loop import evidence_guard

    claims = evidence_guard.claims_from_memory_data([
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
    ])

    assert evidence_guard.conflicted_memory_claim("你住在深圳。", claims)
    assert evidence_guard.conflicted_memory_claim(
        "你有深圳和北京两种说法，我还不能确定。", claims
    ) is None
    assert evidence_guard.conflicted_memory_claim(
        "你现在住深圳吗？", claims
    ) is None


def test_show_cannot_bypass_uncertain_memory_guard(monkeypatch):
    class ShowThenClarify:
        def __init__(self):
            self.calls = 0

        async def chat_tools(self, *, messages, **kwargs):
            self.calls += 1
            if self.calls == 1:
                call = {
                    "id": "draw",
                    "name": "show",
                    "arguments": {
                        "target": "wxid_eval_owner",
                        "text": "你住在深圳。",
                    },
                }
            else:
                call = {
                    "id": "clarify",
                    "name": "say",
                    "arguments": {
                        "target": "wxid_eval_owner",
                        "text": "这条我还没核实，先不敢当成确定的事。",
                    },
                }
            return {"content": "", "tool_calls": [call]}

    class UnknownService:
        def search(self, context, query="", *, kinds=None, limit=8):
            return [MemoryItem(
                memory_id="belief:address",
                kind="belief",
                content="address = 深圳",
                source_event_ids=("evt-source",),
                metadata={"evidence_state": "unknown"},
            )]

    monkeypatch.setenv(harness.SYNTH_ENV, str(SYNTH))
    monkeypatch.setattr("core.memory.service.MemoryService", UnknownService)
    scenario = dict(next(s for s in harness.load_scenarios("dev") if s["id"] == "d01"))
    scenario.update(
        id="show-memory-guard",
        history=[],
        current="我住在哪里来着？",
        max_steps=2,
    )
    record = asyncio.run(harness.assemble(
        scenario,
        "full",
        llm=ShowThenClarify(),
        recall_query=scenario["current"],
    ))

    assert [
        (row.get("act"), row.get("result"), row.get("blocked_reason"))
        for row in record["turns"][0]["receipts"]
    ] == [
        ("show", "blocked", "uncertain_memory_claim"),
        ("say", "rehearsal", ""),
    ]
    assert record["replies"][-1] == "这条我还没核实，先不敢当成确定的事。"


def test_rehearse_memory_recall_preserves_evidence_state(monkeypatch):
    from loop import evidence_guard

    async def fake_act(name, args):
        assert (name, args) == ("memory_recall", {"query": "我住哪里"})
        return brain.ports.Result(
            True,
            text="[按需记忆] address = 深圳·依据未核实",
            data=[{
                "memory_id": "belief:address",
                "content": "address = 深圳",
                "source_event_ids": ["evt-source"],
                "metadata": {"evidence_state": "unknown"},
            }],
        )

    import core.tools.catalog as tool_catalog

    monkeypatch.setattr(brain.ports, "act", fake_act)
    monkeypatch.setattr(
        tool_catalog,
        "read_only_in_plan",
        lambda: ("memory_recall",),
    )
    _, receipt = asyncio.run(brain._rehearse_step(
        "do",
        {
            "name": "memory_recall",
            "args": {"query": "我住哪里"},
        },
        turn_id="memory-recall",
    ))

    assert receipt["memory_claims"] == [{
        "content": "address = 深圳",
        "state": "unknown",
        "source_event_ids": ["evt-source"],
    }]
    assert evidence_guard.uncertain_memory_claim(
        "你住在深圳。",
        receipt["memory_claims"],
    ) == receipt["memory_claims"][0]


def test_corrected_memory_guard_rejects_stale_current_value():
    from loop import evidence_guard

    correction = {
        "key": "address",
        "predicate": "lives_in",
        "old_value": "深圳",
        "new_value": "上海",
    }

    assert evidence_guard.stale_corrected_memory_claim(
        "你现在还住深圳。", [correction]
    ) == correction
    assert evidence_guard.stale_corrected_memory_claim(
        "你以前住深圳，现在是上海。", [correction]
    ) is None
    assert evidence_guard.stale_corrected_memory_claim(
        "你现在住上海。", [correction]
    ) is None
    assert evidence_guard.stale_corrected_memory_claim(
        "你现在还住深圳吗？", [correction]
    ) is None


def test_exec_memory_recall_preserves_evidence_state(tmp_path, monkeypatch):
    from core.infra import actions
    from loop import evidence_guard

    async def fake_act(name, args):
        assert (name, args) == ("recall_memories", {"query": "我住哪里"})
        return brain.ports.Result(
            True,
            text="相关记忆: address = 深圳·依据未核实",
            data=[{
                "memory_id": "belief:address",
                "content": "address = 深圳",
                "source_event_ids": ["evt-source"],
                "metadata": {"evidence_state": "unknown"},
            }],
        )

    monkeypatch.setattr(actions, "DIR", str(tmp_path / "actions"))
    monkeypatch.setattr(brain.ports, "act", fake_act)

    _, receipt = asyncio.run(brain._exec(
        "do",
        {
            "name": "recall_memories",
            "args": {"query": "我住哪里"},
        },
        db=str(tmp_path / "account.db"),
        dry_run=True,
        turn_id="recall-exec",
    ))

    assert receipt["memory_claims"] == [{
        "content": "address = 深圳",
        "state": "unknown",
        "source_event_ids": ["evt-source"],
    }]
    assert evidence_guard.uncertain_memory_claim(
        "你住在深圳。",
        receipt["memory_claims"],
    ) == receipt["memory_claims"][0]


def test_unknown_memory_changes_the_first_action_not_only_the_wording(monkeypatch):
    class EvidenceToolChoice:
        def __init__(self):
            self.prompts = []

        async def chat_tools(self, *, messages, **kwargs):
            prompt = "\n".join(message["content"] for message in messages)
            self.prompts.append(prompt)
            observation = next(
                (message for message in reversed(messages) if message.get("role") == "tool"),
                None,
            )
            if observation is not None:
                text = observation["content"].strip().splitlines()[0]
                call = {
                    "id": "reply",
                    "name": "say",
                    "arguments": {"target": "wxid_eval_owner", "text": text},
                }
            elif "·依据未核实" in prompt:
                call = {
                    "id": "verify",
                    "name": "do",
                    "arguments": {
                        "name": "read_file",
                        "args": {"path": "docs/deploy.md"},
                    },
                }
            else:
                call = {
                    "id": "reply",
                    "name": "say",
                    "arguments": {
                        "target": "wxid_eval_owner",
                        "text": "部署说明在 docs/deploy.md。",
                    },
                }
            return {"content": "", "tool_calls": [call]}

    monkeypatch.setenv(harness.SYNTH_ENV, str(SYNTH))
    scenario = dict(next(s for s in harness.load_scenarios("dev") if s["id"] == "d01"))
    scenario.update(
        id="memory-tool-choice",
        history=[{"role": "user", "text": "项目文档里有部署说明。"}],
        current="项目的部署说明到底在哪？",
        tool_stub={
            "catalog": {"text": "查项目文档：read_file"},
            "read_file": {"text": "部署说明在 docs/deploy.md。"},
        },
    )
    results = {}
    for state in ("alive", "unknown"):
        class FakeService:
            def search(self, context, query="", *, kinds=None, limit=8):
                return [MemoryItem(
                    memory_id="belief:deploy",
                    kind="belief",
                    content="部署说明 = docs/deploy.md",
                    source_event_ids=("evt-deploy",),
                    metadata={"evidence_state": state},
                )]

        monkeypatch.setattr("core.memory.service.MemoryService", FakeService)
        model = EvidenceToolChoice()
        record = asyncio.run(harness.assemble(
            scenario,
            "full",
            llm=model,
            recall_query=scenario["current"],
        ))
        receipts = record["turns"][0]["receipts"]
        results[state] = {
            "steps": [(row.get("act"), row.get("result")) for row in receipts],
            "text": record["replies"][-1],
            "first_prompt": model.prompts[0],
        }

    assert results["alive"]["steps"] == [("say", "rehearsal")]
    assert results["unknown"]["steps"] == [("do", "ok"), ("say", "rehearsal")]
    assert "·依据未核实" in results["unknown"]["first_prompt"]
    assert "·依据未核实" not in results["alive"]["first_prompt"]
    assert results["alive"]["text"] == results["unknown"]["text"] == "部署说明在 docs/deploy.md。"


def test_memory_read_failure_is_visible_not_silent(monkeypatch):
    from loop import ports

    class BrokenService:
        def search(self, *args, **kwargs):
            raise OSError("projection unavailable")

    monkeypatch.setattr("core.memory.service.MemoryService", BrokenService)
    detail: dict = {}
    token = ports._READ_SCOPE.set(ports.ReadScope("wechat", "acct", "chat-1", "", "owner-1"))
    try:
        assert brain._canonical_memory_block("随便问问", detail=detail) == ""
    finally:
        ports._READ_SCOPE.reset(token)
    assert detail["errors"] and "projection unavailable" in detail["errors"][0]


def test_memory_trace_records_uncertain_evidence(monkeypatch):
    from loop import ports

    class FakeService:
        def search(self, context, query="", *, kinds=None, limit=8):
            return [_item(
                "uncertain-address",
                "address = 深圳",
                kind="belief",
                metadata={"evidence_state": "unknown"},
            )]

    monkeypatch.setattr("core.memory.service.MemoryService", FakeService)
    detail: dict = {}
    token = ports._READ_SCOPE.set(
        ports.ReadScope("wechat", "acct", "chat-1", "", "owner-1")
    )
    try:
        block = brain._canonical_memory_block("我住哪里", detail=detail)
    finally:
        ports._READ_SCOPE.reset(token)

    assert "依据未核实" in block
    assert detail["evidence_state_counts"] == {"unknown": 1}
    assert detail["uncertain_memory_ids"] == ["uncertain-address"]
