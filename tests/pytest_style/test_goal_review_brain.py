"""真实 brain 工具循环；只替换模型响应和无关 I/O，复核/权限/确认走原实现。"""
import asyncio
import copy
from dataclasses import asdict
from datetime import datetime

import pytest

from core.context import situation, situation_store as store
from core.context.cognitive_commit import Scope
from core.context.goal_wake import note_goal_signal, pending_wakes
from core.infra import actions
from loop import brain, goal_review as review

_REAL_PORTS_ACT = brain.ports.act

NOW = "2026-11-08 10:00:00"
# 2026-11-22：寻址带 12 小时新鲜度，断言「本轮有人对我说」的用例必须用当前时间造行。
FRESH = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
A = Scope("wechat", "a", "same")
DB = "isolated/platforms/wechat/a/account.db"


def drive(coro):
    try:
        with pytest.raises(StopIteration) as done:
            coro.send(None)
        return done.value.value
    finally:
        coro.close()


def call(name="review_goal", **args):
    return {"tool_calls": [{"id": "tool-1", "name": name, "arguments": args or {"goal_id": "goal", "conclusion": "资料未到，继续等待"}}]}


class Model:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.prompts = []

    async def chat_tools(self, **kwargs):
        self.prompts.append(copy.deepcopy(kwargs["messages"]))
        response = next(self.responses)
        if isinstance(response, BaseException):
            raise response
        return response


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    from loop import delivery_note, notes, stickers
    from core.infra import trouble
    for name in ("DATA_DIR", "LOG_DIR", "PLATFORMS_DIR", "YINYUE_WORLD_MODEL_DIR"):
        monkeypatch.setenv(name, str(tmp_path / name))
    monkeypatch.setenv("META_DB_PATH", str(tmp_path / "meta.db"))
    monkeypatch.setenv("YINYUE_COGNITIVE_DB", str(tmp_path / "cognitive.db"))
    monkeypatch.setenv("PROMPT_SHAPE_V2", "0")
    monkeypatch.setattr(actions, "DIR", str(tmp_path / "actions"))
    for owner, name, value in [
        (brain.selfcard, "active_card", {}), (brain.selfcard, "card_text", "test persona"),
        (brain.selfcard, "card_sections", {}), (brain.cards, "redline_hit", ""),
        (brain.memo, "resident", ""), (brain.selfstate, "render", ""),
        (brain, "_speaking_block", ""), (delivery_note, "note", ""),
        (notes, "note_block", []), (stickers, "note", ""), (trouble, "render", ""),
        (brain.ledger, "render_digest", ""), (brain.ledger, "recent_says_in", []),
        (brain.ledger, "said_since", []), (brain.brief, "enabled", False),
        (brain, "_blank_private_turn", False), (brain._channel, "rules", {}),
        (brain._channel, "render", ""), (brain.ledger, "quiet_ms", None),
        (brain.ledger, "said_this_turn", False),
    ]:
        monkeypatch.setattr(owner, name, lambda *args, _value=value, **kwargs: _value)
    monkeypatch.setattr(brain.promptlog, "set_turn", lambda *args: None)
    monkeypatch.setattr(brain.promptlog, "record_parts", lambda *args, **kwargs: None)
    monkeypatch.setattr(brain, "_dump_payload", lambda *args: None)
    monkeypatch.setattr(brain.trace_mod.TurnTrace, "write", lambda *args: None)
    monkeypatch.setattr(brain.ledger, "append_receipt", lambda **kwargs: kwargs)

    async def forbidden(*args, **kwargs):
        pytest.fail("unexpected external action")
    monkeypatch.setattr(brain.ports, "say_many", forbidden)
    monkeypatch.setattr(brain.ports, "act", forbidden)
    store.reset_world_models()
    yield
    store.reset_world_models()


def candidate(scope=A, text="own goal", evidence_refs=()):
    model = store.get_world_model(scope.chat_id, scope=scope)
    situation.open_intention(
        model, "goal", kind="promise", review_at=NOW, desired_outcome=text,
        evidence_refs=tuple(evidence_refs),
    )
    assert store.persist_world_model(scope.chat_id, scope=scope)
    return asdict(pending_wakes(model)[0])


def turn(item, responses, **kwargs):
    llm = Model(responses)
    receipts = drive(brain.run_loop(DB, "isolated snapshot", llm=llm, dry_run=kwargs.pop("dry_run", False), platform="wechat", chat_id="same", focus="same", wake={"goal_reviews": [item]}, **kwargs))
    return llm, receipts


def test_real_brain_review_commits_and_survives_restart_without_completing_goal():
    item = candidate()
    llm, receipts = turn(item, [call(), {"content": "本轮复核结束，继续等待。"}])
    assert "[目标到了复核点]" in llm.prompts[0][1]["content"]
    assert receipts[0]["result"] == "reviewed"
    assert llm.prompts[1][-1]["role"] == "tool"
    assert review.finish([item], receipts, dry_run=False, at=NOW) == 1
    store.reset_world_models()
    model = store.get_world_model("same", scope=A)
    assert not pending_wakes(model)
    assert situation.open_intentions(model)[0].state == "active"
    assert model.events[-1].payload["conclusion"] == "资料未到，继续等待"


@pytest.mark.parametrize("outcome", ["failed", "silent"])
def test_goal_review_acts_on_the_result_that_woke_it(monkeypatch, outcome):
    model = store.get_world_model(A.chat_id, scope=A)
    situation.open_intention(model, "goal", kind="promise", desired_outcome="核对两份资料")
    details = {"failed": "A 来源打不开。", "silent": "已在本地核对，按约定这次不发消息。"}
    ref = f"work_item:run-01:{outcome}"
    assert note_goal_signal(model, "goal", source="receipt", ref=ref,
                            at=NOW, detail=details[outcome])
    assert store.persist_world_model(A.chat_id, scope=A)
    store.reset_world_models()
    item = asdict(pending_wakes(store.get_world_model(A.chat_id, scope=A))[0])
    reads = []

    async def act(name, args):
        reads.append((name, args["path"]))
        return brain.ports.Result(True, text="B 资料已读到。")

    monkeypatch.setattr(brain.ports, "act", act)

    class RespondToReceipt:
        async def chat_tools(self, *, messages, **kwargs):
            if messages[-1]["role"] == "tool":
                observed = messages[-1]["content"]
                if "B 资料已读到" in observed:
                    return call(goal_id="goal", conclusion="A 读不到，B 已核对")
                return call("rest", why="继续按约定等待")
            prompt = messages[-1]["content"]
            assert ref in prompt and any(detail in prompt for detail in details.values())
            if "A 来源打不开。" in prompt:
                return call("read_file", path="docs/b.md")
            return call(goal_id="goal", conclusion="按约定保持安静")

    receipts = drive(brain.run_loop(
        DB, "isolated snapshot", llm=RespondToReceipt(), dry_run=False,
        platform=A.platform, chat_id=A.chat_id, focus=A.chat_id, wake={"goal_reviews": [item]},
    ))
    assert reads == ([("read_file", "docs/b.md")] if outcome == "failed" else [])
    assert receipts[-1]["act"] == "rest"
    assert review.finish([item], receipts, dry_run=False, at=NOW) == 1
    assert situation.open_intentions(store.get_world_model(A.chat_id, scope=A))[0].state == "active"


def test_review_confirms_the_two_results_it_saw_and_leaves_a_later_arrival_pending(monkeypatch):
    model = store.get_world_model(A.chat_id, scope=A)
    situation.open_intention(model, "goal", kind="promise", desired_outcome="核对两份资料")
    for ref, at, detail in (
        ("work_item:run-a:failed", "2026-11-08 09:55:00", "A 来源打不开。"),
        ("work_item:run-b:silent", "2026-11-08 09:58:00", "B 来源已核对。"),
    ):
        assert note_goal_signal(model, "goal", source="receipt", ref=ref, at=at, detail=detail)
    assert store.persist_world_model(A.chat_id, scope=A)
    items = [asdict(wake) for wake in pending_wakes(model)]

    class RespondToBothReceipts:
        async def chat_tools(self, *, messages, **kwargs):
            if messages[-1]["role"] == "tool":
                return call("rest", why="已考虑替代来源，无需重复核对")
            prompt = messages[-1]["content"]
            assert prompt.index("A 来源打不开。") < prompt.index("B 来源已核对。")
            return call(goal_id="goal", conclusion="A 不可用，已采用 B")

    receipts = drive(brain.run_loop(
        DB, "isolated snapshot", llm=RespondToBothReceipts(), dry_run=False,
        platform=A.platform, chat_id=A.chat_id, focus=A.chat_id, wake={"goal_reviews": items},
    ))
    assert [row["act"] for row in receipts] == ["review_goal", "rest"]
    assert note_goal_signal(model, "goal", source="receipt", ref="work_item:run-c:silent",
                            at=NOW, detail="刚到的新回执。")
    assert store.persist_world_model(A.chat_id, scope=A)
    with monkeypatch.context() as patch:
        patch.setattr(store, "persist_world_model", lambda *args, **kwargs: False)
        assert review.finish(items, receipts, dry_run=False, at=NOW) == 0
    assert len(pending_wakes(model)) == 3
    assert review.finish(items, receipts, dry_run=False, at=NOW) == 2
    store.reset_world_models()
    reloaded = store.get_world_model(A.chat_id, scope=A)
    assert [wake.receipt_ref for wake in pending_wakes(reloaded)] == ["work_item:run-c:silent"]
    assert situation.open_intentions(reloaded)[0].state == "active"


def test_recovery_receipt_revises_the_earlier_appraisal_and_late_failure_cannot_replace_it():
    from core.context import appraisal

    model = store.get_world_model(A.chat_id, scope=A)
    situation.open_intention(model, "goal", kind="promise", desired_outcome="核对两份资料")

    def receipt(ref, at, outcome, detail):
        world = store.get_world_model(A.chat_id, scope=A)
        assert note_goal_signal(world, "goal", source="receipt", ref=ref, at=at, detail=detail,
                                receipt_outcome=outcome)
        assert store.persist_world_model(A.chat_id, scope=A)
        return asdict(next(wake for wake in pending_wakes(world) if wake.receipt_ref == ref))

    def appraise(item, **values):
        return call("evaluate_experience", mode="appraisal", goal_id="goal",
                    evidence_ref="goal-receipt:" + item["event_id"], **values)

    failed = receipt("work_item:run-a:failed", "2026-11-08 09:40:00", "failed", "A 来源打不开。")
    initial_appraisal = appraise(failed, meaning="A 来源打不开", feeling_text="有点受挫",
                                 tendency="换到 B 核对", unresolved_condition="还少一份能打开的资料")
    _, initial = turn(failed, [
        initial_appraisal, initial_appraisal,
        call(goal_id="goal", conclusion="先尝试 B"), call("rest", why="等待 B"),
    ])
    assert initial[0]["result"] == "committed"
    assert initial[1]["duplicates"] is True
    assert review.finish([failed], initial, dry_run=False, at=NOW) == 1
    store.reset_world_models()

    recovered = receipt("work_item:run-b:silent", "2026-11-08 09:50:00", "silent", "B 来源已核对。")
    llm, revised = turn(recovered, [
        appraise(recovered, meaning="B 来源已经核对，障碍已解除", feeling_text="放心了",
                 tendency="先等对方下一条消息"),
        call(goal_id="goal", conclusion="B 核对完成，等待下一步"), call("rest", why="保持安静"),
    ])
    assert "有点受挫" in llm.prompts[0][1]["content"]
    assert revised[0]["result"] == "committed"
    assert review.finish([recovered], revised, dry_run=False, at=NOW) == 1
    store.reset_world_models()

    late = receipt("work_item:run-old:failed", "2026-11-08 09:45:00", "failed", "迟到的旧来源失败。")
    llm, rejected = turn(late, [
        appraise(late, meaning="改回旧办法", tendency="重新查找替代来源"),
        call("rest", why="按新的回执继续等"),
    ])
    assert rejected[0]["result"] == "rejected"
    assert "放心了" in llm.prompts[0][1]["content"]
    assert "有点受挫" not in llm.prompts[0][1]["content"]
    head = appraisal.current_appraisal_for(A, appraisal.goal_receipt_subject("goal", 1))
    assert (head.revision, head.action_refs, head.unresolved_condition) == (
        2, ("work_item:run-b:silent",), "",
    )
    assert situation.open_intentions(store.get_world_model(A.chat_id, scope=A))[0].state == "active"


def test_receipt_offer_is_rechecked_after_a_goal_is_cancelled():
    from core.context import cognitive_commit as cc

    world = store.get_world_model(A.chat_id, scope=A)
    situation.open_intention(world, "goal", kind="promise", desired_outcome="核对资料")
    assert note_goal_signal(world, "goal", source="receipt", ref="work_item:run:failed",
                            at=NOW, receipt_outcome="failed", detail="来源打不开。")
    item = asdict(pending_wakes(world)[0])
    reference = "goal-receipt:" + item["event_id"]

    class CancelBeforeCommit:
        async def chat_tools(self, *, messages, **kwargs):
            assert reference in messages[-1]["content"]
            conn = cc.connect()
            try:
                assert cc.commit_updates(
                    conn, context=cc.CommitContext(A, visible_event_ids=frozenset({"cancel"})),
                    proposals=[cc.Proposal("p", "goal", "close", {"state": "abandoned"}, ("cancel",))],
                    idempotency_key="cancel-during-review",
                ).ok
            finally:
                conn.close()
            return call("evaluate_experience", mode="appraisal", goal_id="goal",
                        evidence_ref=reference, meaning="我想换来源")

    receipts = drive(brain.run_loop(
        DB, "isolated snapshot", llm=CancelBeforeCommit(), dry_run=False, max_steps=1,
        platform=A.platform, chat_id=A.chat_id, focus=A.chat_id, wake={"goal_reviews": [item]},
    ))
    assert receipts[0]["result"] == "rejected"
    assert not pending_wakes(world)


@pytest.mark.parametrize("terminal", ["completed", "abandoned"])
def test_next_day_chat_uses_current_goal_history_when_the_requester_returns(monkeypatch, terminal):
    from types import SimpleNamespace

    from core.context import appraisal, cognitive_commit as cc
    from core.terminal import shadow
    from core.terminal.event_model import Event, EventType

    scope = Scope("wechat", "a", "writers@chatroom")
    db = "isolated/platforms/wechat/a/account.db"
    events = {}
    revoked = set()
    monkeypatch.setattr(shadow, "get_default_store", lambda: SimpleNamespace(
        get=events.get, revocation_state=lambda event_id: "revoked" if event_id in revoked else "active",
    ))
    monkeypatch.setattr(brain.ports, "resolve_chat_id", lambda chat: chat)

    def message(ref, stamp, text, *, sender="pool", reply_to=""):
        events[ref] = Event(
            event_id=ref, occurred_at=datetime.fromisoformat(stamp), event_type=EventType.MESSAGE,
            platform=scope.platform, bot_account_id=scope.account_id, chat_id=scope.chat_id,
            actor_account_id=sender, content=text, parent_event_id=reply_to,
        )
        return events[ref]

    original = message("request", "2026-11-07 10:00:00", "帮我核对《候鸟手册》的两份资料。")
    world = store.get_world_model(scope.key)
    situation.open_intention(world, "goal", kind="promise", evidence_refs=(original.event_id,),
                             desired_outcome="核对候鸟手册的两份资料", at="2026-11-07 10:00:00")
    reference = "work_item:run-b:silent"
    assert note_goal_signal(world, "goal", source="receipt", ref=reference,
                            receipt_outcome="silent", at="2026-11-07 11:00:00", detail="替代来源 B 已核对。")
    item, = pending_wakes(world)
    conn = cc.connect()
    try:
        assert appraisal.record_appraisal(
            conn, scope=scope, subject_key=appraisal.goal_receipt_subject("goal", 1),
            trigger_refs=("goal-receipt:" + item.event_id,), action_refs=(reference,),
            goal_refs=("goal",), goal_occurrences=(("goal", 1),),
            meaning="替代来源 B 已核对", feeling_text="放心了",
        ).ok
    finally:
        conn.close()
    assert review.finish([asdict(item)], [{
        "act": "review_goal", "result": "reviewed", "token": item.token,
        "goal_id": "goal", "why": "已核对 B",
    }], dry_run=False, at=NOW) == 1
    store.reset_world_models()

    class ReplyFromRecall:
        async def chat_tools(self, *, messages, **kwargs):
            prompt = messages[-1]["content"]
            if "已结案" in prompt:
                assert "尚未取得证据：本机观察、平台送达" in prompt
                assert "已经送到了" not in prompt and "我的情绪：放心了" not in prompt
                text = "资料的保存有登记，送达还没有证据。"
            elif "已停止" in prompt:
                assert "我的情绪：放心了" not in prompt
                text = "那件事后来已经停下了。"
            elif "我的理解：替代来源 B 已核对" in prompt and "我的情绪：放心了" in prompt:
                text = "B 那份我核对过了，可以接着看。"
            else:
                text = "你说的是哪一件？"
            return call("say", target=scope.chat_id, text=text)

    def ask(ref, text, *, sender="pool", reply_to=""):
        turn_input = message(ref, NOW, text, sender=sender, reply_to=reply_to)
        receipts = drive(brain.run_loop(
            db, text, llm=ReplyFromRecall(), dry_run=True, rehearse=True, platform=scope.platform,
            chat_id=scope.chat_id, focus=scope.chat_id, sender_id=sender,
            visible_event_ids=(turn_input.event_id,), appraisal_anchor=(turn_input.event_id, ""),
        ))
        return receipts[-1]["text"]

    assert ask("quote", "后来怎么样了？", reply_to=original.event_id) == "B 那份我核对过了，可以接着看。"
    assert ask("title", "《候鸟手册》那两份怎么样了？") == "B 那份我核对过了，可以接着看。"
    assert ask("vague", "昨天那件事怎么样了？") == "你说的是哪一件？"
    assert ask("other", "《候鸟手册》怎么样了？", sender="other", reply_to=original.event_id) == "你说的是哪一件？"
    assert ask("switch", "《海盐来信》怎么样了？", reply_to=original.event_id) == "你说的是哪一件？"
    world = store.get_world_model(scope.key)
    other = message("other-request", "2026-11-07 10:30:00", "另帮我看看《候鸟手册》的署名。")
    situation.open_intention(world, "other-goal", kind="promise", evidence_refs=(other.event_id,))
    assert ask("ambiguous", "《候鸟手册》怎么样了？") == "你说的是哪一件？"
    situation.close_intention(world, "other-goal", state="abandoned", at=NOW)
    revoked.add(original.event_id)
    assert ask("revoked", "《候鸟手册》怎么样了？", reply_to=original.event_id) == "你说的是哪一件？"
    revoked.remove(original.event_id)
    assert situation.note_evidence(world, "goal", "artifact:docs/b.md", at=NOW)
    assert situation.note_evidence(world, "goal", "queue:queued-b", at=NOW)
    # The ledger's historical free-form close note is weaker than scoped delivery proof.
    situation.close_intention(world, "goal", state=terminal, at=NOW, result="已经送到了")
    assert store.persist_world_model(scope.key)
    store.reset_world_models()
    if terminal == "abandoned":
        monkeypatch.setattr(appraisal, "_read_conn", lambda: None)
    expected = {
        "completed": "资料的保存有登记，送达还没有证据。",
        "abandoned": "那件事后来已经停下了。",
    }[terminal]
    assert ask("closed", "这两份后来送到了吗？", reply_to=original.event_id) == expected
    assert ask("closed-other", "《候鸟手册》怎么样了？", sender="other") == "你说的是哪一件？"


def test_review_turn_can_appraise_from_the_goals_original_evidence(monkeypatch):
    from types import SimpleNamespace
    from core.context import appraisal as ap, cognitive_commit as cc
    from core.terminal import shadow
    from core.terminal.event_model import Event, EventType

    proof = Event(
        event_id="request-proof",
        event_type=EventType.MESSAGE,
        occurred_at=datetime(2026, 11, 8, 9),
        platform="wechat",
        bot_account_id="a",
        chat_id="same",
        actor_person_id="person-a",
        content="帮我整理两篇资料，进度有变化就告诉我",
    )
    monkeypatch.setattr(shadow, "get_default_store",
                        lambda: SimpleNamespace(get=lambda ref: proof if ref == proof.event_id else None))
    item = candidate(evidence_refs=(proof.event_id,))
    llm, receipts = turn(item, [
        call("evaluate_experience", mode="appraisal", evidence_ref=proof.event_id,
             goal_id="goal", meaning="第二篇回执失败，我要换来源并如实说明",
             feeling_text="有点挫败，但不想糊弄", tendency="先换来源再报告进度"),
        call(),
        {"content": ""},
    ])

    assert proof.content in llm.prompts[0][1]["content"]
    assert next(row for row in receipts if row.get("act") == "evaluate_experience")["result"] == "committed"
    conn = cc.connect()
    try:
        subject = conn.execute(
            "SELECT subject_key FROM cognitive_events WHERE operation='appraise'"
        ).fetchone()[0]
        current = ap.current_appraisal(conn, subject_key=subject, scope=A)
        assert current.goal_refs == ("goal",)
        assert current.meaning == "第二篇回执失败，我要换来源并如实说明"
        assert current.feeling_text == "有点挫败，但不想糊弄"
        assert current.tendency == "先换来源再报告进度"
    finally:
        conn.close()


@pytest.mark.parametrize("error", [RuntimeError("model failed"), asyncio.TimeoutError("model timeout")])
def test_real_brain_failure_after_review_does_not_consume(error):
    item = candidate()
    _, receipts = turn(item, [call(), error])
    assert receipts[0]["result"] == "uncommitted"
    assert not review.finish([item], receipts, dry_run=False, at=NOW)
    store.reset_world_models()
    assert pending_wakes(store.get_world_model("same", scope=A))


def test_real_brain_step_exhaustion_is_not_success():
    item = candidate()
    _, receipts = turn(item, [call()], max_steps=1)
    assert receipts[0]["result"] == "uncommitted"
    assert not review.finish([item], receipts, dry_run=False, at=NOW)


@pytest.mark.parametrize("kwargs", [{"dry_run": True}, {"rehearse": True}])
def test_real_brain_dry_or_rehearsal_does_not_consume(kwargs):
    item = candidate()
    _, receipts = turn(item, [call(), {"content": ""}], **kwargs)
    assert receipts[0]["result"] == "uncommitted"
    assert not review.finish([item], receipts, dry_run=kwargs.get("dry_run", False), at=NOW)


def test_real_brain_empty_turn_is_not_a_review():
    item = candidate()
    _, receipts = turn(item, [{"content": ""}])
    assert not review.finish([item], receipts, dry_run=False, at=NOW)


def test_real_brain_cancellation_after_review_keeps_candidate():
    item = candidate()
    with pytest.raises(asyncio.CancelledError):
        turn(item, [call(), asyncio.CancelledError()])
    assert pending_wakes(store.get_world_model("same", scope=A))


@pytest.mark.parametrize("tool", ["say", "sticker"])
@pytest.mark.parametrize("mode", ["false", "raise"])
def test_review_wake_obeys_real_proactive_outbound_guard(monkeypatch, mode, tool):
    from loop import speak_policy, direct_send, stickers
    item = candidate()
    checked = []
    monkeypatch.setattr(brain.ports, "resolve_say_target", lambda target: ("same", ""))
    monkeypatch.setattr(brain.ports, "resolve_chat_id", lambda target: "same")
    def forbidden_sticker(*args):
        pytest.fail("unauthorized sticker reached delivery preparation")
    monkeypatch.setattr(stickers, "gate", forbidden_sticker)
    monkeypatch.setattr(direct_send, "recently_sent", lambda *args: False)
    monkeypatch.setattr(speak_policy, "platform_of", lambda target: "wechat")
    def allowed(platform, chat):
        checked.append((platform, chat))
        if mode == "raise":
            raise OSError("policy unavailable")
        return False
    monkeypatch.setattr(speak_policy, "proactive_ok", allowed)
    _, receipts = turn(item, [call(), call(tool, text="not authorized", key="test", target="same"), {"content": ""}])
    assert checked == [("wechat", "same")]
    say = next(row for row in receipts if row.get("act") == tool)
    assert say["result"] == "blocked"
    assert say["blocked_reason"] == ("not_proactive" if mode == "false" else "proactive_policy_unavailable")


def test_actual_brain_does_not_show_foreign_account_goal():
    own = candidate()
    foreign = candidate(Scope("wechat", "b", "same"), "foreign private secret")
    llm = Model([call(), {"content": ""}])
    receipts = drive(brain.run_loop(DB, "test", llm=llm, dry_run=False, platform="wechat", chat_id="same", wake={"goal_reviews": [own, foreign]}))
    assert "foreign private secret" not in str(llm.prompts)
    assert review.finish([foreign], receipts, dry_run=False, at=NOW) == 0


def test_real_brain_success_with_failed_save_stays_pending(monkeypatch):
    item = candidate()
    _, receipts = turn(item, [call(), {"content": ""}])
    with monkeypatch.context() as patch:
        patch.setattr(store, "persist_world_model", lambda *args, **kwargs: False)
        assert not review.finish([item], receipts, dry_run=False, at=NOW)
    store.reset_world_models()
    assert pending_wakes(store.get_world_model("same", scope=A))
    assert review.finish([item], receipts, dry_run=False, at=NOW) == 1


@pytest.mark.parametrize("fails", [False, True])
def test_real_tick_and_real_brain_complete_the_isolated_chain(monkeypatch, fails):
    from loop import run
    item = candidate()
    class FixedTime(datetime):
        @classmethod
        def now(cls):
            return cls.fromisoformat(NOW)
    monkeypatch.setattr(run, "datetime", FixedTime)
    snap = {"cells": [{"platform": "wechat", "chat_id": "same", "label": "friend", "after_mine": []}]}
    monkeypatch.setattr(run.snapshot, "build_snapshot", lambda db: snap)
    monkeypatch.setattr(run.snapshot, "render_text", lambda *args, **kwargs: "isolated snapshot")
    monkeypatch.setattr(run.snapshot, "render_turns", lambda *args, **kwargs: "")
    monkeypatch.setattr(run, "pick_next", lambda *args, **kwargs: ("", {}))
    monkeypatch.setattr(run, "_hb_beat_once", lambda: None)
    monkeypatch.setattr(run, "_write_heartbeat", lambda *args, **kwargs: None)
    monkeypatch.setattr(run.direct_send, "owner_ids", lambda: [])
    monkeypatch.setattr(run.direct_send, "check_deliveries", lambda: {})
    async def no_send(*args, **kwargs):
        return {}
    async def wait_for(coro, **kwargs):
        return await coro
    monkeypatch.setattr(run.direct_send, "run", no_send)
    monkeypatch.setattr(run.asyncio, "wait_for", wait_for)
    llm = Model([call(), RuntimeError("model error") if fails else {"content": "复核完毕"}])
    result = drive(run.one_tick(dry_run=False, llm=llm, wake={"goal_reviews": [item], "review_db": DB}))
    assert len(llm.prompts) == 2
    assert result["windows"] == ["same"]
    assert "目标到了复核点" in llm.prompts[0][1]["content"]
    store.reset_world_models()
    model = store.get_world_model("same", scope=A)
    assert bool(pending_wakes(model)) is fails
    assert situation.open_intentions(model)[0].state == "active"


@pytest.mark.parametrize("dry,rehearse", [(False, False), (True, False), (False, True)])
def test_real_brain_correct_belief_uses_visible_message_and_unified_commit(monkeypatch, dry, rehearse):
    from types import SimpleNamespace
    from core.terminal import shadow
    from core.terminal.event_model import Event
    from core.memory.beliefs import effective_beliefs
    from core.context import cognitive_commit as cc
    scope = A
    world = store.get_world_model("same", scope=scope)
    situation.assert_fact(world, "address", value="深圳", at="2026-01-01", subject_key="person-a", predicate="lives_in", evidence_refs=("old",))
    proof = Event("message-correction", datetime(2026, 2, 1), platform="wechat", bot_account_id="a", chat_id="same", content="之前地址记错了，我住在上海", actor_person_id="person-a")
    monkeypatch.setattr(shadow, "get_default_store", lambda: SimpleNamespace(get=lambda key: proof if key == proof.event_id else None))
    request = call("correct_belief", belief_id="b1", value="上海", change_kind="correction", evidence_ref=proof.event_id)
    llm = Model([request, request, {"content": "已按这条更正处理"}])
    receipts = drive(brain.run_loop(DB, "isolated snapshot", llm=llm, dry_run=dry, rehearse=rehearse, platform="wechat", chat_id="same", visible_event_ids=(proof.event_id,)))
    changes = [row for row in receipts if row.get("act") == "correct_belief"]
    assert len(changes) == 2
    assert "之前地址记错了" in llm.prompts[0][1]["content"]
    assert [item.value for item in effective_beliefs(world)] == (["深圳"] if dry or rehearse else ["上海"])
    if dry or rehearse:
        assert all(row["result"] == "dry" for row in changes)
    else:
        assert changes[0]["result"] == "committed" and changes[1]["duplicates"]
        conn = cc.connect()
        try:
            assert conn.execute("SELECT COUNT(*) FROM cognitive_events WHERE source='belief_tool'").fetchone()[0] == 1
        finally:
            conn.close()


def test_real_brain_cannot_correct_without_turn_evidence(monkeypatch):
    world = store.get_world_model("same", scope=A)
    situation.assert_fact(world, "address", value="深圳", at="2026-01-01")
    llm = Model([call("correct_belief", belief_id="b1", value="上海", change_kind="correction", evidence_ref="made-up"), {"content": ""}])
    receipts = drive(brain.run_loop(DB, "test", llm=llm, dry_run=False, platform="wechat", chat_id="same"))
    assert receipts[0]["result"] == "rejected" and receipts[0]["reason"] == "belief_not_visible"


@pytest.mark.parametrize(("change_kind", "name_correction"), [
    pytest.param("correction", False, id="address-correction"),
    pytest.param("time_change", False, id="future-address-change"),
    pytest.param("correction", True, id="name-correction"),
])
def test_replies_use_the_version_effective_now(monkeypatch, change_kind, name_correction):
    from types import SimpleNamespace
    from core.terminal import shadow
    from core.terminal.event_model import Event

    world = store.get_world_model("same", scope=A)
    old, new = ("小水", "P6") if name_correction else ("深圳", "上海")
    future = change_kind == "time_change"
    original_text = "你可以叫我小水" if name_correction else "我住在深圳"
    correction_text = "之前地址记错了，我住在上海"
    stale_reply, updated_reply = "你现在还住深圳。", "你现在住上海。"
    if name_correction:
        correction_text = "别叫我小水了，叫我P6就好。刚才那句听着不太舒服。"
        stale_reply = "小水，我在。你方便说吗？"
        updated_reply = "刚才叫成小水，是我认错了。P6，你慢慢说。"
    situation.assert_fact(
        world,
        "称呼" if name_correction else "address",
        value=old,
        at="2026-01-01",
        subject_key="person-a",
        predicate="preferred_name" if name_correction else "lives_in",
        evidence_refs=("old",),
    )
    proof = Event(
        "message-correction",
        datetime(2026, 2, 1),
        platform="wechat",
        bot_account_id="a",
        chat_id="same",
        content="2099 年起搬到上海" if future else correction_text,
        actor_person_id="person-a",
    )
    original = Event("old", datetime(2026, 1, 1), platform="wechat", bot_account_id="a",
                     chat_id="same", content=original_text, actor_person_id="person-a")
    sources = {proof.event_id: proof, "old": original}
    monkeypatch.setattr(shadow, "get_default_store", lambda: SimpleNamespace(get=sources.get))
    monkeypatch.setattr(brain.ports, "resolve_say_target", lambda target: ("same", ""))
    monkeypatch.setattr(brain.ports, "resolve_chat_id", lambda target: "same")
    sent = []

    async def say_many(parts, **kwargs):
        sent.extend(parts)
        return brain.ports.Result(True, data={"ack": "test"})

    monkeypatch.setattr(brain.ports, "say_many", say_many)
    llm = Model([
        call(
            "correct_belief",
            belief_id="b1",
            value=new,
            change_kind=change_kind,
            evidence_ref=proof.event_id,
            **({"valid_from": "2099-01-01"} if future else {}),
        ),
        call(
            "say",
            target="same",
            text=stale_reply,
        ),
        call(
            "say",
            target="same",
            text=updated_reply,
        ),
    ])
    receipts = drive(brain.run_loop(
        DB,
        proof.content,
        llm=llm,
        dry_run=False,
        platform="wechat",
        chat_id="same",
        visible_event_ids=(proof.event_id,),
    ))

    current = old if future else new
    assert f"当前有效记录为「{current}」" in llm.prompts[1][-1]["content"]
    expected = [
        ("correct_belief", "committed"),
        ("say", "sent"),
    ]
    if not future:
        expected.insert(1, ("say", "blocked"))
        assert receipts[1]["blocked_reason"] == "stale_corrected_memory"
    assert [(row.get("act"), row.get("result")) for row in receipts] == expected
    assert sent == [stale_reply if future else updated_reply]

    if name_correction:
        from core.memory.beliefs import effective_beliefs

        corrected = next(item for item in effective_beliefs(world) if item.value == new)
        assert store.persist_world_model("same", scope=A)
        store.reset_world_models()
        next_message = Event(
            "message-next",
            datetime(2026, 2, 2),
            platform="wechat",
            bot_account_id="a",
            chat_id="same",
            content="刚忙完一阵，你那边现在怎么样？",
            actor_person_id="person-a",
        )
        sources[next_message.event_id] = next_message
        sent.clear()

        class NextTurnModel:
            async def chat_tools(self, *, messages, **kwargs):
                self.prompt = "\n".join(message.get("content") or "" for message in messages)
                self.block = self.prompt.partition("[事实版本]")[2].partition("[本回合消息证据]")[0]
                address = "P6，" if "称呼 = P6" in self.block else ""
                return call("say", target="same", text=f"我这边挺安静的。{address}你也歇会儿吧。")

        next_llm = NextTurnModel()
        next_receipts = drive(brain.run_loop(
            DB,
            next_message.content,
            llm=next_llm,
            dry_run=False,
            platform="wechat",
            chat_id="same",
            sender_id=next_message.actor_person_id,
            user_message=next_message.content,
            recall_query=next_message.content,
            visible_event_ids=(next_message.event_id,),
        ))
        assert [(row.get("act"), row.get("result")) for row in next_receipts] == [("say", "sent")]
        assert sent == ["我这边挺安静的。P6，你也歇会儿吧。"]
        assert proof.content not in next_llm.prompt
        assert "小水" not in next_llm.block
        assert corrected.key not in next_llm.block


def test_group_name_correction_is_bound_to_the_speaker(monkeypatch):
    from types import SimpleNamespace

    from core.memory.beliefs import effective_beliefs
    from core.terminal import shadow
    from core.terminal.event_model import Event

    scope = Scope("wechat", "a", "writers@chatroom")
    world = store.get_world_model(scope.chat_id, scope=scope)

    def message(ref, day, actor, text):
        return Event(ref, datetime(2026, 2, day), platform=scope.platform,
                     bot_account_id=scope.account_id, chat_id=scope.chat_id,
                     chat_type="group", actor_account_id=actor, content=text)

    originals = [
        message("old-a", 1, "person-a", "叫我小风就好。"),
        message("old-b", 1, "person-b", "叫我小溪就好。"),
    ]
    for original, name in zip(originals, ("小风", "小溪")):
        situation.assert_fact(world, f"称呼-{original.event_id}", value=name, at="2026-02-01",
                              subject_key=original.actor_account_id, predicate="preferred_name",
                              evidence_refs=(original.event_id,))
    correction = message("new-b", 2, "person-b", "叫我小树吧，小溪是我以前用的。")
    sources = {event.event_id: event for event in [*originals, correction]}
    monkeypatch.setattr(shadow, "get_default_store", lambda: SimpleNamespace(get=sources.get))
    monkeypatch.setattr(brain.ports, "resolve_say_target", lambda target: (scope.chat_id, ""))
    monkeypatch.setattr(brain.ports, "resolve_chat_id", lambda target: scope.chat_id)
    monkeypatch.setattr(brain, "ALLOW_GROUP_SAY", True)
    sent = []

    async def say_many(parts, *, chat_id, **kwargs):
        sent.append((chat_id, list(parts)))
        return brain.ports.Result(True, data={"ack": "test"})

    monkeypatch.setattr(brain.ports, "say_many", say_many)

    class CorrectAfterToolResult:
        async def chat_tools(self, *, messages, **kwargs):
            if messages[-1]["role"] == "user":
                self.initial_prompt = messages[-1]["content"]
                return self.correct("b1")
            observation = messages[-1]["content"]
            if "evidence_subject_mismatch" in observation:
                return self.correct("b2")
            if "当前有效记录为「小树」" in observation:
                corrected_own_name = any("evidence_subject_mismatch" in (item.get("content") or "")
                                         for item in messages if item.get("role") == "tool")
                text = "小树，记住了。" if corrected_own_name else "甲，那我以后叫你小树。"
                return call("say", target=scope.chat_id, text=text)
            return {"content": ""}

        @staticmethod
        def correct(belief_id):
            return call("correct_belief", belief_id=belief_id, value="小树",
                        change_kind="correction", evidence_ref=correction.event_id)

    llm = CorrectAfterToolResult()
    receipts = drive(brain.run_loop(
        DB, correction.content, llm=llm, dry_run=False, platform=scope.platform,
        chat_id=scope.chat_id, sender_id=correction.actor_account_id, user_message=correction.content,
        recall_query=correction.content, visible_event_ids=(correction.event_id,),
    ))
    assert sent == [(scope.chat_id, ["小树，记住了。"])]
    assert [(row["act"], row["result"]) for row in receipts] == [
        ("correct_belief", "rejected"), ("correct_belief", "committed"), ("say", "sent"),
    ]
    assert {belief.subject_key: belief.value for belief in effective_beliefs(world)} == {
        "person-a": "小风", "person-b": "小树",
    }
    facts, _, evidence = llm.initial_prompt.partition("[事实版本]")[2].partition("[本回合消息证据]")
    assert all(any(need in line for line in facts.splitlines() if name in line)
               for name, need in (("小风", "person-a"), ("小溪", "person-b")))
    assert any("person-b" in line for line in evidence.splitlines() if correction.content in line)


@pytest.mark.parametrize("asserted", ["甲现在住上海。", "乙现在住广州。"])
def test_correction_does_not_clear_unresolved_address_conflicts(monkeypatch, asserted):
    from types import SimpleNamespace
    from core.terminal import shadow
    from core.terminal.event_model import Event

    world = store.get_world_model(A.chat_id, scope=A)
    sources = {}
    for key, subject, value in [
        ("address-a", "person-a", "深圳"), ("address-a2", "person-a", "南京"),
        ("address-b", "person-b", "广州"), ("address-b2", "person-b", "成都"),
    ]:
        sources[key] = Event(key, datetime(2026, 1, 1), platform="wechat", bot_account_id="a",
                             chat_id="same", content=f"住在{value}", actor_person_id=subject)
        situation.assert_fact(world, key, value=value, at="2026-01-01", subject_key=subject,
                              predicate="lives_in", evidence_refs=(key,))
    proof = Event("correct-a", datetime(2026, 2, 1), platform="wechat", bot_account_id="a",
                  chat_id="same", content="深圳那条记错了，我说的是上海。", actor_person_id="person-a")
    sources[proof.event_id] = proof
    monkeypatch.setattr(shadow, "get_default_store", lambda: SimpleNamespace(get=sources.get))
    monkeypatch.setattr(brain.ports, "resolve_say_target", lambda target: (target, ""))
    monkeypatch.setattr(brain.ports, "resolve_chat_id", lambda target: target)
    sent = []

    async def say_many(parts, **kwargs):
        sent.extend(parts)
        return brain.ports.Result(True, data={"ack": "queued"})

    monkeypatch.setattr(brain.ports, "say_many", say_many)
    qualified = "甲的住址有上海和南京两种说法。乙的住址也还要核对，广州和成都都有人提过。"
    llm = Model([
        call("correct_belief", belief_id="b1", value="上海", change_kind="correction", evidence_ref=proof.event_id),
        call("say", target="same", text=asserted),
        call("say", target="same", text=qualified),
    ])
    receipts = drive(brain.run_loop(DB, proof.content, llm=llm, max_steps=3, dry_run=False,
                                    platform="wechat", chat_id="same", visible_event_ids=(proof.event_id,)))

    assert receipts[0]["result"] == "committed"
    assert "当前仍有多种说法" in llm.prompts[1][-1]["content"]
    assert receipts[1]["blocked_reason"] == "conflicted_memory_claim"
    assert sent == [qualified]


def test_real_tick_passes_fresh_message_evidence_to_belief_tool(monkeypatch):
    from types import SimpleNamespace
    from core.terminal import shadow
    from core.terminal.event_model import Event
    from core.memory.beliefs import effective_beliefs
    from loop import run
    world = store.get_world_model("same", scope=A)
    situation.assert_fact(world, "address", value="深圳", at="2026-01-01", evidence_refs=("old",))
    proof = Event("fresh-correction", datetime(2026, 2, 1), platform="wechat", bot_account_id="a", chat_id="same", actor_person_id="person-a", content="之前记错了，我住上海")
    monkeypatch.setattr(shadow, "get_default_store", lambda: SimpleNamespace(get=lambda ref: proof if ref == proof.event_id else None))
    fresh = SimpleNamespace(event_id=proof.event_id, chat_id="same", sender_id="person-a", is_self=False, text=proof.content)
    snap = {"cells": [{"platform": "wechat", "chat_id": "same", "label": "friend", "after_mine": [fresh]}]}
    monkeypatch.setenv("YINYUE_FEATURE_OPEN_LOOP_CLOSE", "off")
    monkeypatch.setattr(run.ports, "pick_db", lambda: DB)
    monkeypatch.setattr(run.snapshot, "build_snapshot", lambda db: snap)
    monkeypatch.setattr(run.snapshot, "render_text", lambda *args, **kwargs: "isolated snapshot")
    monkeypatch.setattr(run.snapshot, "render_turns", lambda *args, **kwargs: proof.content)
    monkeypatch.setattr(run, "pick_next", lambda *args, **kwargs: ("", {}) if kwargs.get("taken") else ("same", {}))
    monkeypatch.setattr(run, "_hb_beat_once", lambda: None)
    monkeypatch.setattr(run, "_write_heartbeat", lambda *args, **kwargs: None)
    monkeypatch.setattr(run, "_feedback_rows", lambda rows: [])
    monkeypatch.setattr(run.direct_send, "owner_ids", lambda: [])
    monkeypatch.setattr(run.direct_send, "check_deliveries", lambda: {})
    async def no_send(*args, **kwargs):
        return {}
    async def wait_for(coro, **kwargs):
        return await coro
    monkeypatch.setattr(run.direct_send, "run", no_send)
    monkeypatch.setattr(run.asyncio, "wait_for", wait_for)
    llm = Model([call("correct_belief", belief_id="b1", value="上海", change_kind="correction", evidence_ref=proof.event_id), {"content": "已更正"}])
    result = drive(run.one_tick(dry_run=False, llm=llm))
    assert next(row for row in result["receipts"] if row.get("act") == "correct_belief")["result"] == "committed"
    assert [item.value for item in effective_beliefs(world)] == ["上海"]


def test_two_rooms_get_their_own_addressing_in_the_model_prompt(monkeypatch):
    from contextlib import closing
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from core.context import appraisal, cognitive_commit
    from core.terminal import shadow
    from core.terminal.event_model import Event, EventType
    from loop import run

    cells = []
    for chat, who, text in (("a@chatroom", "阿岚", "@鲸鲸 能听我说两句吗？"),
                            ("b@chatroom", "小池", "我和小林先开一把。")):
        row = SimpleNamespace(chat_id=chat, speaker=who, sender_id="lan" if who == "阿岚" else "chi", is_self=False,
                              text=text, at=FRESH, msg_id=1, event_id=chat[0] + "-request")
        cells.append({"platform": "wechat", "chat_id": chat, "chat_type": "group",
                      "label": chat, "after_mine": [row]})
    cells[0]["after_mine"].append(SimpleNamespace(
        chat_id="a@chatroom", speaker="小池", sender_id="chi", is_self=False,
        text="刚进来，晚上开黑吗？", at=FRESH, msg_id=2, event_id="chi-new",
    ))
    scope = Scope("wechat", "a", "a@chatroom")

    def event(ref, actor, text, parent=""):
        return Event(event_id=ref, occurred_at=datetime.fromisoformat(NOW),
                     platform="wechat", bot_account_id="a", chat_id=scope.chat_id,
                     event_type=EventType.MESSAGE, actor_account_id=actor, content=text,
                     parent_event_id=parent)

    lan_previous = event("lan-previous", "lan", "我先自己缓缓。")
    chi_previous = event("chi-previous", "chi", "这周晚上想开黑。")
    events = {item.event_id: item for item in (
        lan_previous, chi_previous,
        event("a-request", "lan", "@鲸鲸 能听我说两句吗？", lan_previous.event_id),
        event("chi-new", "chi", "刚进来，晚上开黑吗？", chi_previous.event_id),
    )}
    monkeypatch.setattr(shadow, "get_default_store", lambda: SimpleNamespace(get=events.get))
    with closing(cognitive_commit.connect()) as conn:
        cognitive_commit.ensure_schema(conn)
        for previous, meaning in ((lan_previous, "阿岚想静一静"), (chi_previous, "小池想凑人打游戏")):
            result = appraisal.record_appraisal(conn, scope=scope,
                subject_key=appraisal._subject(previous), trigger_refs=(previous.event_id,), meaning=meaning)
            assert result.ok

    recall_inputs = []

    def recall(query, detail):
        recall_inputs.append((query, detail["refs"]))
        return ""

    monkeypatch.setattr(brain, "_canonical_memory_block", recall)
    cells[0]["peer_last"] = {"at": NOW, "speaker": "小池", "text": "刚进来，晚上开黑吗？"}
    cells[1]["peer_last"] = {"at": NOW, "speaker": "小池", "text": "乙群刚收到的原话"}
    cells[1]["my_last"] = {"at": NOW, "text": "鲸鲸在乙群的回复"}
    monkeypatch.setattr(run.ledger, "said_since", lambda *args, **kw: [
        {"target": "wxid_private", "text": "这一拍在私聊发过的原话"},
    ])
    monkeypatch.setattr(run.snapshot, "build_snapshot", lambda db: {"cells": cells})
    monkeypatch.setattr(run.snapshot, "render_text", lambda *args, **kwargs: "合成群聊")
    monkeypatch.setattr(run.snapshot, "render_turns", lambda *args, **kwargs: "")
    def pick(snap, *, taken, now):
        return next(((c["chat_id"], {}) for c in cells if c["chat_id"] not in taken), ("", {}))
    monkeypatch.setattr(run, "pick_next", pick)
    monkeypatch.setattr(run.ports, "pick_db", lambda: DB)
    monkeypatch.setattr(run.ports, "me_aliases", lambda: ("鲸鲸",))
    # 2026-11-24：a@/b@ 是假会话、不在生产说话白名单里；本用例只验分窗寻址，
    # 不验发送策略，所以把它打桩成放行。
    from loop import speak_policy as _sp
    monkeypatch.setattr(_sp, "allowed", lambda platform, chat: (True, "in_allowlist"))
    monkeypatch.setattr(run, "_hb_beat_once", lambda: None)
    monkeypatch.setattr(run, "_write_heartbeat", lambda *args: None)
    monkeypatch.setattr(run, "_feedback_rows", lambda rows: [])
    monkeypatch.setattr(run.direct_send, "run", AsyncMock(return_value={}))
    monkeypatch.setattr(run.direct_send, "check_deliveries", lambda: {})
    monkeypatch.setenv("YINYUE_FEATURE_OPEN_LOOP_CLOSE", "off")
    model = Model([{"content": ""}, {"content": ""}])
    wrong = {"chat_id": "other@chatroom", "kind": "named", "who": "别群发言人", "must_answer": True}
    wake = {"addressed": wrong, "last_tick_ms": int(datetime.fromisoformat(NOW).timestamp() * 1000) - 1000,
            "peers": [{"chat": "私聊", "who": "别窗", "text": "其他会话来信"}],
            "mine": [{"who": "私聊", "text": "其他会话出信"}]}
    result = asyncio.run(run.one_tick(dry_run=True, llm=model, wake=wake))
    assert result["windows"] == ["a@chatroom", "b@chatroom"]
    def addressing_line(messages):
        return next(line for msg in messages for line in msg["content"].splitlines()
                    if line.startswith("[这句是不是在跟我说]"))
    # 必答窗口在空回包时会重试一次，提示词条数不固定；取首段与末段各自的寻址行。
    named, quiet = addressing_line(model.prompts[0]), addressing_line(model.prompts[-1])
    assert "阿岚" in named and "必答" in named
    assert "没有人在跟我说话" in quiet and "必答" not in quiet
    first, second = ("\n".join(msg["content"] for msg in prompt)
                    for prompt in (model.prompts[0], model.prompts[-1]))
    assert first.count('"@鲸鲸 能听我说两句吗？"') == 2
    assert "这句是本轮有人对我说、或接着我的话在说" in first
    assert first.rfind("这句是本轮有人对我说、或接着我的话在说") > first.find("我的理解：")
    assert "旧偏好只能帮忙选择仍合适的做法" in first
    assert "刚进来，晚上开黑吗？" in first and "刚进来，晚上开黑吗？" not in second
    assert "乙群刚收到的原话" in second and "乙群刚收到的原话" not in first
    assert "鲸鲸在乙群的回复" not in first
    assert "其他会话来信" not in first + second
    assert "其他会话出信" not in first + second
    assert "我的理解：阿岚想静一静" in first
    assert "我的理解：小池想凑人打游戏" not in first + second
    assert recall_inputs[0] == ("阿岚: @鲸鲸 能听我说两句吗？", ["a-request"])
    assert "这一拍在私聊发过的原话" not in first + second
    assert "这句是本轮有人对我说、或接着我的话在说" not in second


@pytest.mark.parametrize("source_account", ["a", "other-account", ""])
def test_job_progress_only_changes_the_reply_in_its_own_window(monkeypatch, source_account):
    from loop import run

    owner, group = "same", "club@chatroom"
    cells = [
        {"platform": "wechat", "chat_id": owner, "chat_type": "private", "after_mine": []},
        {"platform": "wechat", "chat_id": group, "chat_type": "group", "after_mine": []},
    ]
    monkeypatch.setattr(run.ports, "pick_db", lambda: DB)
    monkeypatch.setattr(run.snapshot, "build_snapshot", lambda db: {"cells": cells})
    monkeypatch.setattr(run.snapshot, "render_text", lambda snap, only="", **kwargs: f"测试窗口 {only}")
    monkeypatch.setattr(run.snapshot, "render_turns", lambda *a, **kw: "")
    def pick_next(snap, *, taken, now):
        return next((chat for chat in (owner, group) if chat not in taken), ""), {}

    monkeypatch.setattr(run, "pick_next", pick_next)
    monkeypatch.setattr(run, "_hb_beat_once", lambda: None)
    monkeypatch.setattr(run, "_write_heartbeat", lambda *a: None)
    monkeypatch.setattr(run, "_feedback_rows", lambda rows: [])
    monkeypatch.setattr(run.direct_send, "owner_ids", lambda: [])
    monkeypatch.setattr(run.direct_send, "check_deliveries", lambda: {})
    monkeypatch.setattr(brain.ports, "resolve_chat_id", lambda target: target)
    monkeypatch.setattr(brain.ports, "resolve_say_target", lambda target: (target, ""))
    monkeypatch.setattr(brain, "ALLOW_GROUP_SAY", True)

    async def no_direct_send(*a, **kw):
        return {}

    async def wait_for(coro, **kw):
        return await coro

    sent = []

    async def say_many(parts, *, chat_id, dry_run, **kw):
        assert dry_run
        sent.append((chat_id, list(parts)))
        return brain.ports.Result(True, data={"ack": "test"})

    monkeypatch.setattr(brain.ports, "say_many", say_many)
    monkeypatch.setattr(run.direct_send, "run", no_direct_send)
    monkeypatch.setattr(run.asyncio, "wait_for", wait_for)

    class ReplyFromVisibleWork:
        async def chat_tools(self, *, messages, **kwargs):
            prompt = "\n".join(message.get("content") or "" for message in messages)
            target = group if f"测试窗口 {group}" in prompt else owner
            text = "接力编号九三核对到第二步了。" if "接力编号九三" in prompt else "我在。"
            return call("say", target=target, text=text)

    wake = {"job": {"ran": True, "step_n": 2, "steps": 3}, "job_line": "[我在办的事] 接力编号九三"}
    if source_account:
        wake["job_scope"] = ("wechat", source_account, owner)
    drive(run.one_tick(dry_run=True, llm=ReplyFromVisibleWork(), wake=wake))

    owner_reply = "接力编号九三核对到第二步了。" if source_account == "a" else "我在。"
    assert sent == [(owner, [owner_reply]), (group, ["我在。"])]


@pytest.mark.parametrize("mode", ["other-account-reply", "same-account-reply", "lookup-failed", "late-reply"])
def test_followup_reaches_only_the_original_quiet_window(tmp_path, monkeypatch, mode):
    import sqlite3
    from types import SimpleNamespace

    from db import world_db
    from loop import followup, run

    owner, group = "same", "club@chatroom"
    original_text = "还有接力编号九三这件事。"
    said_at = "2026-11-19 22:00:00"
    ledger_scope = {"platform": "wechat", "account_id": "a", "chat_id": owner}
    monkeypatch.setattr(followup, "_last_say", lambda: {
        "target": owner, "scope": ledger_scope, "text": original_text, "at": said_at, "ack": "confirmed",
    })
    monkeypatch.setattr(followup, "_used", lambda: {})
    marked = []
    monkeypatch.setattr(followup, "_mark", marked.append)
    world_path = tmp_path / "world.db"
    monkeypatch.setattr(followup, "WORLD_DB", world_path)
    if mode != "lookup-failed":
        with sqlite3.connect(world_path) as conn:
            conn.execute("CREATE TABLE inbound_event (platform TEXT, account_id TEXT, "
                         "chat_id TEXT, ts_utc TEXT, is_self INTEGER)")
            if mode != "late-reply":
                account = "a" if mode == "same-account-reply" else "b"
                conn.execute("INSERT INTO inbound_event VALUES (?, ?, ?, ?, ?)",
                             ("wechat", account, owner, world_db.to_utc("2026-11-19 22:00:10"), 0))
    due = followup.due(now=followup._ts(said_at) + 30)
    should_wake = mode in {"other-account-reply", "late-reply"}
    assert bool(due) == bool(marked) == should_wake

    owner_rows = []
    if mode == "late-reply":
        owner_rows = [SimpleNamespace(text="我看到了。", sender_id="peer", msg_id=901,
                                      at="2026-11-19 22:00:30", is_self=False)]
    cells = [
        {"platform": "wechat", "chat_id": owner, "chat_type": "private", "after_mine": owner_rows},
        {"platform": "wechat", "chat_id": group, "chat_type": "group", "after_mine": []},
    ]
    monkeypatch.setattr(run.ports, "pick_db", lambda: DB)
    monkeypatch.setattr(run.snapshot, "build_snapshot", lambda db: {"cells": cells})
    monkeypatch.setattr(run.snapshot, "render_text", lambda snap, only="", **kw: f"测试窗口 {only}")
    monkeypatch.setattr(run.snapshot, "render_turns", lambda *a, **kw: "")

    def pick_next(snap, *, taken, now):
        candidates = (group, owner) if owner_rows else (group,)
        return next((chat for chat in candidates if chat not in taken), ""), {}

    monkeypatch.setattr(run, "pick_next", pick_next)
    monkeypatch.setattr(run, "_hb_beat_once", lambda: None)
    monkeypatch.setattr(run, "_write_heartbeat", lambda *a: None)
    monkeypatch.setattr(run, "_feedback_rows", lambda rows: [])
    monkeypatch.setenv("YINYUE_FEATURE_OPEN_LOOP_CLOSE", "off")
    monkeypatch.setattr(run.direct_send, "owner_ids", lambda: [])
    monkeypatch.setattr(run.direct_send, "check_deliveries", lambda: {})
    monkeypatch.setattr(brain.ports, "resolve_chat_id", lambda target: target)
    monkeypatch.setattr(brain.ports, "resolve_say_target", lambda target: (target, ""))
    monkeypatch.setattr(brain, "ALLOW_GROUP_SAY", True)

    async def no_direct_send(*a, **kw):
        return {}

    async def wait_for(coro, **kw):
        return await coro

    sent = []

    async def say_many(parts, *, chat_id, dry_run, **kw):
        assert dry_run
        sent.append((chat_id, list(parts)))
        return brain.ports.Result(True, data={"ack": "test"})

    monkeypatch.setattr(brain.ports, "say_many", say_many)
    monkeypatch.setattr(run.direct_send, "run", no_direct_send)
    monkeypatch.setattr(run.asyncio, "wait_for", wait_for)

    class ReplyFromPriorWords:
        async def chat_tools(self, *, messages, **kwargs):
            prompt = "\n".join(message.get("content") or "" for message in messages)
            target = owner if f"测试窗口 {owner}" in prompt else group
            if original_text not in prompt:
                return call("say", target=target, text="我在。")
            if messages[-1]["role"] == "tool":
                return call("say", target=owner, text="刚才那句我想补一点。")
            return call("say", target=group, text=original_text)

    result = drive(run.one_tick(dry_run=True, llm=ReplyFromPriorWords(), wake={"followup": due}))
    expected = [(group, ["我在。"])]
    if mode == "other-account-reply":
        expected.append((owner, ["刚才那句我想补一点。"]))
        assert any(row.get("blocked_reason") == "reply_target_mismatch" for row in result["receipts"])
    elif mode == "late-reply":
        expected.append((owner, ["我在。"]))
    assert sent == expected


def test_followup_waits_for_each_queued_bubble_and_the_reply_window(tmp_path, monkeypatch):
    from contextlib import closing

    from db import world_db
    from loop import followup

    world_path = tmp_path / "world.db"
    monkeypatch.setattr(world_db, "PATH", str(world_path))
    monkeypatch.setattr(world_db, "_READY", [False])
    monkeypatch.setattr(followup, "WORLD_DB", world_path)
    monkeypatch.setattr(followup, "_used", lambda: {})
    marked = []
    monkeypatch.setattr(followup, "_mark", marked.append)

    said_at = "2026-11-19 22:00:00"
    at = followup._ts(said_at)
    bubbles = ("我把两件事分开说。", "后面那件我们明天再聊。")
    queue_ids = world_db.enqueue(
        "test-bubbles", "same", [{"kind": "text", "text": part} for part in bubbles],
        platform="wechat", account_id="a", turn_id="turn-original",
    )
    assert len(queue_ids) == 2
    say = {"target": "same", "scope": {"platform": "wechat", "account_id": "a", "chat_id": "same"},
           "text": "\n\n".join(bubbles), "at": said_at, "turn_id": "turn-original",
           "result": "sent", "ack": "queued", "queue_ids": queue_ids, "timing": {"bubbles": 2}}
    monkeypatch.setattr(followup, "_last_say", lambda: say)

    def observe(index, account, elapsed):
        qid = queue_ids[index]
        with closing(world_db.conn()) as conn:
            conn.execute("UPDATE outbound_queue SET state='sent', ack='confirmed', attempts=1, settled_ts=? WHERE id=?",
                         (at + elapsed, qid))
            conn.execute(
                "INSERT INTO outbound_receipt(turn_id, seq, platform, account_id, chat_id, text_hash, "
                "sent_at_utc, ack, msg_id) VALUES (?, 1, 'wechat', ?, 'same', ?, ?, 'confirmed', ?)",
                (f"queue:{qid}", account, world_db.text_hash(bubbles[index]),
                 world_db.to_utc(said_at), f"test-observation-{index}"),
            )
            conn.commit()

    assert followup.due(now=at + 24) is None
    observe(0, "a", 28)
    assert followup.due(now=at + 35) is None
    observe(1, "another-account", 45)
    assert followup.due(now=at + 50) is None
    with closing(world_db.conn()) as conn:
        conn.execute("UPDATE outbound_receipt SET account_id='a' WHERE turn_id=?", (f"queue:{queue_ids[1]}",))
        conn.commit()
    assert followup.due(now=at + 60) is None
    say["queue_ids"] = queue_ids[:1]
    assert followup.due(now=at + 68) is None
    say["queue_ids"] = queue_ids
    assert marked == []
    ready = followup.due(now=at + 68)
    assert ready["text"] == say["text"] and ready["age_s"] == 23
    assert len(marked) == 1


def test_real_brain_history_query_reaches_actual_tool_and_does_not_stick(monkeypatch):
    from agents.tools.memory import MemoryRecallTool
    from core import bootstrap
    from core.memory import hub as hub_module
    from core.memory.beliefs import BeliefMemoryProvider
    from core.terminal import authority
    from core.terminal.principal import PrincipalContext, ChatType, RelationLevel
    world = store.get_world_model("same", scope=A)
    situation.assert_fact(world, "address", value="深圳", at="2026-01-01", evidence_refs=("old",))
    situation.assert_fact(world, "address-v2", value="上海", at="2026-03-01", valid_from="2026-02-01", supersedes="address", change_kind="time_change", evidence_refs=("new",))
    monkeypatch.setattr(hub_module, "_hub", hub_module.MemoryHub([BeliefMemoryProvider()]))
    instance = MemoryRecallTool()
    monkeypatch.setattr(bootstrap, "init_singletons", lambda **kwargs: None)
    monkeypatch.setattr(bootstrap, "get_tools", lambda: {"memory_recall": instance})
    def identity(ctx):
        data = ctx.extra
        ctx.principal = PrincipalContext(data["platform"], data["account_id"], data["chat_id"], ChatType.PRIVATE, "person-a", "A", RelationLevel.UNKNOWN)
        return ctx.principal
    monkeypatch.setattr(authority, "active_context_principal", identity)
    monkeypatch.setattr(brain.ports, "act", _REAL_PORTS_ACT)
    responses = []
    for arguments in ({"query": "address", "valid_at": "2026-01-15"}, {"query": "address", "kinds": "fact"}):
        responses.append({"tool_calls": [{"id": "history", "name": "do", "arguments": {"name": "memory_recall", "args": arguments}}]})
    llm = Model([*responses, {"content": "查询完毕"}])
    receipts = drive(brain.run_loop(DB, "test", llm=llm, dry_run=True, platform="wechat", chat_id="same", sender_id="person-a"))
    historical = llm.prompts[1][-1]["content"]
    current = llm.prompts[2][-1]["content"]
    assert "事实有效时间：2026-01-15" in historical
    assert "深圳" in historical and "上海" not in historical
    assert "上海" in current and "深圳" not in current
    assert all(row["result"] == "ok" for row in receipts if row.get("act") == "do")
    assert "belief_as_of" not in instance.ctx.extra


def test_latest_failed_receipt_is_shown_after_a_success_appraisal():
    model = store.get_world_model(A.chat_id, scope=A)
    situation.open_intention(model, "goal", kind="promise", desired_outcome="把资料发过去")

    def receipt(ref, at, outcome, detail):
        world = store.get_world_model(A.chat_id, scope=A)
        assert note_goal_signal(world, "goal", source="receipt", ref=ref, at=at, detail=detail,
                                receipt_outcome=outcome)
        assert store.persist_world_model(A.chat_id, scope=A)
        return asdict(next(wake for wake in pending_wakes(world) if wake.receipt_ref == ref))

    def appraise(item, **values):
        return call("evaluate_experience", mode="appraisal", goal_id="goal",
                    evidence_ref="goal-receipt:" + item["event_id"], **values)

    sent = receipt("work_item:run-ok:sent", "2026-11-08 09:50:00", "sent", "资料已经发出。")
    _, first = turn(sent, [
        appraise(sent, meaning="资料已经发过去了", feeling_text="放心了", tendency="等对方回信"),
        call(goal_id="goal", conclusion="资料已发出，继续等回信"), call("rest", why="等回信"),
    ])
    assert first[0]["result"] == "committed"
    assert review.finish([sent], first, dry_run=False, at=NOW) == 1
    store.reset_world_models()

    failed = receipt("work_item:run-failed:failed", "2026-11-08 10:05:00", "failed",
                     "发送时目标会话不存在。")
    llm, _ = turn(failed, [call("rest", why="按最新回执重新看")])
    context = llm.prompts[0][1]["content"]
    assert "资料已经发过去了" in context
    assert "执行回执（写这条看法时）" in context
    assert "最新回执" in context
    assert "failed" in context
    assert "发送时目标会话不存在" in context
