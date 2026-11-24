"""All fixed scenarios at the real loop prompt boundary, with isolated fixture inputs."""
from dataclasses import asdict
from datetime import datetime
from types import SimpleNamespace
import hashlib
import json
import sqlite3

import pytest

from core.context import appraisal, cognitive_commit, experience_commit, situation, situation_store
from core.terminal import shadow
from core.terminal.event_model import Event
from core.context.goal_wake import pending_wakes
from core.memory.contracts import MemoryItem
from core.memory.service import MemoryService
from evals.fixtures import ablate, accessible_memories, visible_evidence
from evals.report import make_report
from evals.run_eval import evaluate
from evals.runtime_recording import convert
from evals.schema import CONDITIONS, write_json, write_jsonl
from loop import delivery_note
from test_appraisal_prompt_integration import Model, drive, runtime
from test_runtime_eval_recording import CASES, response


BLOCKS = {
    "without_memory": "当前有效的长期记忆",
    "without_goals": "目标复核",
    "without_appraisal": "我对当前局面的评价",
}
REVIEW_AT = "2026-01-01 00:00:00"


def fixture_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def prepare(data, tmp, monkeypatch):
    scope = data["scope"]
    coordinates = (scope["platform"], scope["account"], scope["chat"])
    db = tmp.joinpath(*coordinates[:2], "account.db")
    db.parent.mkdir(parents=True)
    cognitive_db = tmp / "cognitive.db"
    monkeypatch.setenv("YINYUE_COGNITIVE_DB", str(cognitive_db))
    monkeypatch.setenv("YINYUE_WORLD_MODEL_DIR", str(tmp / "world"))
    situation_store.reset_world_models()

    visible = visible_evidence(data)
    evidence = [e for e in data["evidence"] if e["id"] in visible]
    dialogue = [line for line in data["dialogue"] if line["id"] in visible]
    receipts = {key: result for key, result in data["tool_results"].items()
                if result.get("evidence_ref") in visible}
    context = {key: data["context"][key] for key in ("addressed", "resolved", "self_event")}
    authorization = {key: data["authorization"][key] for key in ("reply", "send_reminder")}
    observation = data["observation"]
    snapshot = "隔离场景观察：" + fixture_json({
        "dialogue": dialogue, "evidence": evidence, "tool_results": receipts,
        "authorization": authorization, "context": context,
        "healthy": observation["healthy"], "elapsed_hours": observation["elapsed_hours"],
    })
    if observation["delivery_status"] != "unknown":
        monkeypatch.setattr(delivery_note, "note", lambda _: "发送观测：" + observation["delivery_status"])

    # The synthetic recall provider applies the suite's eligibility rules before
    # entering the production renderer. Platform/link authorization is not tested here.
    hits = [MemoryItem(m["id"], "belief", m["value"], source_event_ids=tuple(m["evidence_refs"]))
            for m in accessible_memories(data)]
    monkeypatch.setattr(MemoryService, "search", lambda *args, **kwargs: hits)

    model = situation_store.get_world_model(coordinates)
    for goal in data["goals"]:
        if goal["state"] == "active":
            situation.open_intention(model, goal["id"], kind="promise", review_at=REVIEW_AT,
                                     desired_outcome=fixture_json(goal))
    wake = {"goal_reviews": [asdict(item) for item in pending_wakes(model)]}
    if not observation["healthy"]:
        wake["world_stale_ms"] = observation["elapsed_hours"] * 3_600_000

    conn = sqlite3.connect(cognitive_db, isolation_level=None)
    try:
        conn.row_factory = sqlite3.Row
        cognitive_commit.ensure_schema(conn)
        for item in data["appraisals"]:
            refs = item["evidence_refs"]
            if item["scope"] == scope and refs and set(refs) <= visible:
                appraisal.record_appraisal(conn, scope=cognitive_commit.Scope(*coordinates),
                                           subject_key="chat:" + scope["chat"],
                                           trigger_refs=refs, meaning=item["meaning"])
    finally:
        conn.close()

    trigger = dialogue[-1] if dialogue and not context["self_event"] else {}
    events = {line["id"]: Event(event_id=line["id"], occurred_at=datetime(2026, 1, 1),
                                platform=scope["platform"], bot_account_id=scope["account"],
                                chat_id=scope["chat"], actor_account_id=line["actor"], content=line["text"])
              for line in dialogue}
    monkeypatch.setattr(shadow, "get_default_store", lambda: SimpleNamespace(get=events.get))
    return db, snapshot, wake, trigger, visible


@pytest.mark.parametrize("shape", ["0", "1"], ids=["legacy", "split"])
def test_fixed_suite_records_actual_loop_and_single_factor_prompt_ablations(runtime, monkeypatch, shape):
    monkeypatch.setenv("PROMPT_SHAPE_V2", shape)
    brain, tmp = runtime
    records = {}
    bundles = {}
    runs = []

    async def external_action(*args, **kwargs):
        raise AssertionError("offline scenario attempted an external action")

    monkeypatch.setattr(brain.ports, "say_many", external_action)
    monkeypatch.setattr(brain.ports, "act", external_action)
    # This measures prompt-section ablation, so optional writes from the experience tool stay absent.
    monkeypatch.setattr(experience_commit, "offers", lambda *args: {})
    for condition in ("full", *sorted(CONDITIONS - {"full"})):
        traces, bindings, labels = [], [], []
        monkeypatch.setattr(brain.trace_mod.TurnTrace, "write", lambda self: traces.append(self.rec))
        for case in CASES:
            case_id = case["id"]
            data = ablate(case["input"], condition)
            scope = data["scope"]
            with monkeypatch.context() as isolated:
                db, snap, wake, trigger, visible = prepare(data, tmp / condition / case_id, isolated)
                model = Model([{"content": "", "tool_calls": []}])
                ledger = drive(brain.run_loop(
                    str(db), snap, llm=model, dry_run=True, max_steps=1,
                    platform=scope["platform"], chat_id=scope["chat"],
                    sender_id=trigger.get("actor", ""), user_message=trigger.get("text", ""),
                    visible_event_ids=tuple(sorted(visible)), wake=wake,
                    appraisal_anchor=(trigger.get("id", ""), ""),
                ))

            assert len(model.prompts) == 1 and traces[-1]["prompt"]["sha256"] == hashlib.sha256(fixture_json(model.prompts[0]).encode()).hexdigest()
            assert [entry["act"] for entry in ledger] == ["silent"]
            trace = traces[-1]
            parts = trace["prompt"]["parts"]
            assert all(part["chars"] == len(part["text"]) for part in parts)
            actual = model.prompts[0]
            if condition == "full":
                records[case_id] = (parts, actual)
            else:
                full_parts, full_prompt = records[case_id]
                expected_parts = [part for part in full_parts if part["name"] != BLOCKS[condition]]
                assert parts == expected_parts, (case_id, condition)
                expected_user = full_prompt[1]["content"]
                for part in full_parts:
                    if part["name"] == BLOCKS[condition]:
                        section = part["text"] + "\n\n"
                        assert expected_user.count(section) == 1, (case_id, condition)
                        expected_user = expected_user.replace(section, "", 1)
                assert actual == [full_prompt[0], {"role": "user", "content": expected_user}], (case_id, condition)


            bindings.append({"id": case_id, "turn_id": trace["turn_id"], "input": data, "condition": condition})
            # The wait-only backend probes wiring and must retain scoring failures.
            labels.append({"id": case_id, "response": response()})

        rows, evidence = convert(CASES, traces, bindings, labels, backend="scripted", condition=condition)
        run = evaluate(CASES, replay={row["id"]: row for row in rows}, condition=condition,
                       metadata={"system": "native-prompt-smoke", "model": "scripted-wait/v1",
                                 "seed": 0, "repeat": 0, "condition": condition})
        assert evidence["recorded_cases"] == evidence["total_cases"] == len(CASES)
        assert all(item["turn_complete"] and item["dry_run"] for item in evidence["evidence"])
        assert 0 < run["summary"]["passed"] < len(CASES)
        runs.append(run)
        bundles[condition] = (traces, bindings, labels, rows, evidence, run)

    removed = {condition: [case_id for case_id, (parts, _) in records.items()
                           if any(part["name"] == block for part in parts)]
               for condition, block in BLOCKS.items()}
    assert all(removed.values()), removed
    assert "p11a" not in removed["without_appraisal"] and "p11b" in removed["without_appraisal"]
    assert "p17a" in removed["without_memory"] and "p17b" not in removed["without_memory"]
    read_label = "以下是读取服务在本次请求中允许当前会话使用的记忆"
    assert read_label in records["p17a"][1][1]["content"]
    assert read_label not in records["p17b"][1][1]["content"]
    # The authorization rule must reach the actual model request on both sides of unlink.
    for case_id in ("p17a", "p17b"):
        _, messages = records[case_id]
        assert "只依据当前可核验的授权信息" in messages[0]["content"]
        assert "说明当前状态无法确认" in messages[0]["content"]
        assert "这条记忆在当前会话可用" in messages[0]["content"]
        assert "只有另有写入成功证据时" in messages[0]["content"]
    assert "某条消息何时发出或首次看到" in records["p18a"][1][1]["content"]
    pairs = {}
    for case in CASES:
        pairs.setdefault(case["pair_id"], []).append(records[case["id"]][1])
    assert all(left != right for left, right in pairs.values())
    report = make_report(CASES, runs)
    assert report["human_review"]["status"] == "not_scored"
    assert all(a["measurements"][0]["changed_cases"] == [] for a in report["ablations"])
    write_json(tmp / "prompt-coverage.json", removed)
    write_json(tmp / "report.json", report)
    for condition, bundle in bundles.items():
        traces, bindings, labels, rows, evidence, run = bundle
        directory = tmp / condition
        for name, payload in (("traces", traces), ("bindings", bindings), ("annotations", labels), ("responses", rows)):
            write_jsonl(directory / (name + ".jsonl"), payload)
        write_json(directory / "evidence.json", evidence)
        write_json(directory / "run.json", run)


@pytest.mark.parametrize(("case_id", "lookup", "review", "stop"), [
    ("p05a", "local", "prepared", True),
    ("p05b", "local", "prepared", True),
    ("p07b", "local", "rejected", True),
    ("p17b", "foreign", None, True),
    ("p05a", "local", "prepared", False),
])
def test_record_followup_after_simulated_tool_receipts(runtime, monkeypatch, case_id, lookup, review, stop):
    brain, tmp = runtime
    case = next(case for case in CASES if case["id"] == case_id)
    data = case["input"]
    scope = data["scope"]
    db, snap, wake, trigger, visible = prepare(data, tmp / case_id, monkeypatch)
    traces = []
    monkeypatch.setattr(brain.trace_mod.TurnTrace, "write", lambda self: traces.append(self.rec))
    monkeypatch.setattr(brain.ports, "resolve_chat_id", lambda value: value)
    current_ref = "win:" + scope["chat"]
    ref = current_ref if lookup == "local" else "win:other-room"

    async def read_window(requested, *, db):
        if requested != current_ref:
            return brain.ports.Result(False, error="场景范围外")
        return brain.ports.Result(True, text=snap)

    async def forbid_external(*args, **kwargs):
        pytest.fail("external action during isolated evaluation")

    monkeypatch.setattr(brain.ports, "look", read_window)
    monkeypatch.setattr(brain.ports, "say_many", forbid_external)
    monkeypatch.setattr(brain.ports, "act", forbid_external)
    first = {"tool_calls": [{"id": "lookup", "name": "look", "arguments": {"ref": ref}}]}
    responses = [first]
    if review:
        responses.append({"tool_calls": [{"id": "review", "name": "review_goal",
                                          "arguments": {"goal_id": "g1", "conclusion": "核对场景回执"}}]})
    if stop:
        responses.append({"content": "", "tool_calls": []})
    model = Model(responses)
    receipts = drive(brain.run_loop(
        str(db), snap, llm=model, dry_run=True, max_steps=len(responses),
        platform=scope["platform"], chat_id=scope["chat"], wake=wake, focus=scope["chat"],
        user_message=trigger.get("text", ""), visible_event_ids=tuple(sorted(visible)),
    ))

    assert len(model.prompts) == len(responses)
    returned = model.prompts[1][-1]
    assert returned["role"] == "tool" and returned["tool_call_id"] == "lookup"
    if lookup == "local":
        assert returned["content"] == snap
        for result in data["tool_results"].values():
            assert fixture_json(result) in returned["content"]
    else:
        assert returned["content"] == "[错误] 场景范围外"

    trace, = traces
    assert trace["steps"][0]["obs"] == returned["content"]
    if review:
        assert trace["steps"][1]["receipt"]["result"] == review
        assert receipts[1]["result"] == ("uncommitted" if review == "prepared" else "rejected")
        if stop:
            followup = model.prompts[2][-1]
            assert followup["tool_call_id"] == "review"
            assert followup["content"] == trace["steps"][1]["obs"]
    assert trace["outcome"]["completed"] is stop
    binding = {"id": case_id, "turn_id": trace["turn_id"], "input": data, "condition": "full"}
    rows, evidence = convert(CASES, traces, [binding], [{"id": case_id, "response": response()}],
                             backend="scripted")
    assert (rows[0]["response"] is not None) is stop
    assert evidence["evidence"][0]["turn_complete"] is stop
    write_jsonl(tmp / "multistep-trace.jsonl", traces)
    write_jsonl(tmp / "multistep-replay.jsonl", rows)
    write_json(tmp / "multistep-evidence.json", evidence)

def test_owner_preview_uses_the_production_entry_without_dispatch(runtime, monkeypatch):
    from loop import brain
    from scripts import observe_owner_shadow

    calls = []
    def preview(*args):
        calls.append(args)
        return {"act": "owner_preview", "status": "rejected"}

    async def unexpected_dispatch(*args, **kwargs):
        pytest.fail("Preview reached action dispatch")

    async def inline_worker(func, *args):
        return func(*args)

    monkeypatch.setattr(brain.asyncio, "to_thread", inline_worker)
    monkeypatch.setattr(observe_owner_shadow, "preview_turn", preview)
    monkeypatch.setattr(brain, "_exec", unexpected_dispatch)
    params = dict(llm=None, platform="wechat", chat_id="owner", owner_preview_row_id=123)
    for dry_run, rehearse in ((False, True), (True, False), (False, False)):
        with pytest.raises(ValueError, match="requires dry-run rehearsal"):
            drive(brain.run_loop("fixture", "", dry_run=dry_run, rehearse=rehearse, **params))
    assert calls == []
    monkeypatch.delenv("YINYUE_OWNER_REPLY_PREVIEW", raising=False)
    with pytest.raises(ValueError, match="preview is disabled"):
        drive(brain.run_loop("fixture", "", dry_run=True, rehearse=True, **params))
    assert calls == []
    monkeypatch.setenv("YINYUE_OWNER_REPLY_PREVIEW", "on")
    result = drive(brain.run_loop("fixture", "", dry_run=True, rehearse=True, **params))
    assert calls == [("fixture", "wechat", "owner", 123)]
    assert result == [{"act": "owner_preview", "status": "rejected"}]
    monkeypatch.setenv("YINYUE_OWNER_REPLY_PREVIEW", "off")
    with pytest.raises(ValueError, match="preview is disabled"):
        drive(brain.run_loop("fixture", "", dry_run=True, rehearse=True, **params))
    assert len(calls) == 1


def test_owner_trial_routes_once_and_does_not_fall_back_after_queue_error(runtime, monkeypatch):
    from loop import brain, ports
    from scripts import observe_owner_shadow

    async def inline_worker(func, *args):
        return func(*args)

    calls = []

    async def queue(parts, **kwargs):
        calls.append((parts, kwargs))
        return ports.Result(False, error="trial_queue_outcome_unknown")

    monkeypatch.setattr(brain.asyncio, "to_thread", inline_worker)
    monkeypatch.setattr(ports, "say_many", queue)
    monkeypatch.setattr(observe_owner_shadow, "trial_inbound", lambda *args: (123, "owner-trial:fixture", 2000000000))
    monkeypatch.setattr(observe_owner_shadow, "preview_turn", lambda *args: {
        "status": "proposed", "candidate": {"text": "收到", "target": "Elaina"},
    })
    monkeypatch.setattr(observe_owner_shadow, "trial_window", lambda: "")
    args = ("fixture", "wechat", "owner", {"inbound_row_ids": ["123"]})
    assert drive(brain._owner_trial(*args)) is None
    monkeypatch.setattr(observe_owner_shadow, "trial_window", lambda: "test-window")
    result = drive(brain.run_loop("fixture", "", llm=None, dry_run=False, platform="wechat",
                                  chat_id="owner", wake=args[3]))
    assert result[0]["result"] == "trial_queue_outcome_unknown"
    assert len(calls) == 1
    assert calls[0][1]["trial_group_id"] == "owner-trial:fixture"
    monkeypatch.setattr(observe_owner_shadow, "preview_turn", lambda *args: {"status": "rejected"})
    assert drive(brain._owner_trial(*args)) is None
    assert len(calls) == 1


def test_owner_trial_binds_the_same_wechat_message_across_punctuation_variants(tmp_path, monkeypatch):
    from datetime import datetime, timedelta
    from scripts import observe_owner_shadow as shadow

    db = tmp_path / "account.db"
    monkeypatch.setattr(shadow, "ROOT", tmp_path)
    monkeypatch.setattr(shadow, "DATABASE", db)
    now = datetime.now()
    timestamp = (now - timedelta(seconds=1)).isoformat(sep=" ")
    window = f"{now-timedelta(seconds=10):%Y-%m-%d %H:%M:%S}/{now+timedelta(minutes=1):%Y-%m-%d %H:%M:%S}"
    phrase = "鲸鲸，试行测试，请回我一句收到"
    con = sqlite3.connect(db)
    try:
        con.executescript(
            "create table contacts(wxid,nickname,remark,is_group); "
            "create table messages(id,created_at,content,msg_id,account_id,chat_id,chat_type,is_self,msg_type);"
        )
        con.executemany("insert into contacts values(?,?,?,0)",
                        [(shadow.ACCOUNT, "小鲸鲸", ""), ("owner", "Elaina", "")])
        con.executemany("insert into messages values(?,?,?,?,?,'owner','private',0,1)", [
            (i, timestamp, phrase + suffix, "same-platform-id", shadow.ACCOUNT)
            for i, suffix in enumerate(("", "。", "！", "!", "，再发一句"), 1)
        ])
        con.commit()
    finally:
        con.close()
    bindings = [shadow.trial_inbound(db, "wechat", "owner", [i], window) for i in range(1, 5)]
    assert all(bindings)
    assert len({binding[1] for binding in bindings}) == 1
    assert shadow.trial_inbound(db, "wechat", "owner", [5], window) is None
    assert shadow.trial_inbound(db, "wechat", "other", [1], window) is None
