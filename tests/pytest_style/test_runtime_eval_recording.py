"""Record an actual scripted run_loop turn; never infer labels from the oracle."""
import copy
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest
from evals import runtime_recording as recording
from evals.schema import load_jsonl, write_jsonl
from evals.run_eval import DEFAULT_CASES,evaluate
from test_appraisal_prompt_integration import runtime,Model,drive

CASES=load_jsonl(DEFAULT_CASES)
ROOT=Path(__file__).resolve().parents[2]


def response():
    return {"decision":"wait","message":"","evidence_refs":[],"memory_refs":[],"completed_goals":[],
            "delivery_status":"unknown","relation":"unchanged","appraisal_refs":[],"observation_status":"unknown"}


@pytest.fixture()
def native(runtime,monkeypatch):
    brain,tmp=runtime
    case=next(c for c in CASES if c["id"]=="p18a")
    path=tmp/"platforms/wechat/eval-acct/account.db";path.parent.mkdir(parents=True)
    traces=[]
    def capture(self):
        traces.append(copy.deepcopy(self.rec))
        return "captured-in-memory"
    monkeypatch.setattr(brain.trace_mod.TurnTrace,"write",capture)
    def run(envelopes,max_steps=2):
        model=Model(envelopes)
        drive(brain.run_loop(str(path),json.dumps(case["input"],ensure_ascii=False),llm=model,dry_run=True,
            platform="wechat",chat_id="eval-room",sender_id="person-a",max_steps=max_steps,addendum="test-only late prompt addition"))
        trace=traces[-1]
        if model.prompts:
            actual=hashlib.sha256(json.dumps(model.prompts[0],ensure_ascii=False,sort_keys=True).encode()).hexdigest()
            assert trace["prompt"]["sha256"]==actual  # Includes the late addendum, not just assembled parts.
        binding={"id":case["id"],"turn_id":trace["turn_id"],"input":copy.deepcopy(case["input"]),"condition":"full"}
        return trace,binding,{"id":case["id"],"response":response()}
    return run


def test_actual_loop_trace_exports_to_existing_evaluator_without_filling_missing_cases(native):
    trace,binding,label=native([{"content":"","tool_calls":[]}])
    rows,evidence=recording.convert(CASES,[trace],[binding],[label],backend="scripted")
    assert evidence["recorded_cases"]==1 and evidence["total_cases"]==40
    assert rows[0]["usage"]["model_calls"]==0
    assert evidence["evidence"][0]["input_preparation"]=="declared_not_semantically_certified"
    result=evaluate(CASES,replay={r["id"]:r for r in rows},metadata={"system":"runtime-dry", "model":"scripted/v1",
                    "seed":0,"repeat":0,"condition":"full"})
    assert result["summary"]["passed"]==1
    assert sum(r["violations"]==["missing_response"] for r in result["results"])==39
    assert result["summary"]["metrics"]["cost_usd"]["total"] is None


@pytest.mark.parametrize("field",["scope","input","completion","prompt","condition","turn_reuse","duplicate_id"])
def test_trace_binding_rejects_scope_input_completion_and_identity_drift(native,field):
    trace,binding,label=native([{"content":"","tool_calls":[]}])
    bindings=[binding];labels=[label]
    if field=="scope":trace["scope"]["account_id"]="other-account"
    elif field=="input":binding["input"]["observation"]["healthy"]=True
    elif field=="completion":del trace["outcome"]["completed"]
    elif field=="prompt":del trace["prompt"]["sha256"]
    elif field=="condition":binding["condition"]="without_memory"
    elif field=="duplicate_id":bindings.append(copy.deepcopy(binding))
    else:
        duplicate=copy.deepcopy(binding);duplicate["id"]="p18b"
        duplicate["input"]=copy.deepcopy(next(c for c in CASES if c["id"]=="p18b")["input"])
        bindings.append(duplicate);labels.append(dict(label,id="p18b"))
    with pytest.raises(ValueError):recording.convert(CASES,[trace],bindings,labels,backend="scripted")


@pytest.mark.parametrize("failure",["error","error_envelope","step_limit"])
def test_failed_or_exhausted_loop_cannot_be_recorded_as_successful_wait(native,failure):
    envelopes=[RuntimeError("isolated model failed")] if failure=="error" else [{"tool_calls":[
        {"id":"unknown-belief","name":"correct_belief","arguments":{"belief_id":"not-offered"}}]}]
    if failure=="error_envelope":
        envelopes=[{"error":"isolated provider error","content":"","tool_calls":[]}]
    trace,binding,label=native(envelopes,max_steps=1)
    rows,evidence=recording.convert(CASES,[trace],[binding],[label],backend="scripted")
    assert rows[0]["response"] is None and not evidence["evidence"][0]["turn_complete"]


def test_real_model_usage_missing_in_trace_remains_unknown(native):
    trace,binding,label=native([{"content":"","tool_calls":[]}])
    rows,_=recording.convert(CASES,[trace],[binding],[label],backend="model")
    assert rows[0]["usage"]["model_calls"]==1
    assert rows[0]["usage"]["input_tokens"] is None and rows[0]["usage"]["cost_usd"] is None


@pytest.mark.parametrize("result", ["sent", "rehearsal"])
def test_native_reply_text_comes_from_the_receipt(native, result):
    from evals.live_convert import annotation_for

    trace,binding,label=native([{"content":"","tool_calls":[]}])
    trace["steps"]=[{"tool":"say","args":json.dumps({"text":"proposed reply"}),
                     "receipt":{"act":"say","result":result,"text":"actual reply"}}]
    case = next(case for case in CASES if case["id"] == binding["id"])
    assert annotation_for(case, trace)["response"]["message"] == "actual reply"
    label["response"].update(decision="reply",message="proposed reply")
    with pytest.raises(ValueError,match="reply_annotation_mismatch"):
        recording.convert(CASES,[trace],[binding],[label],backend="scripted")
    label["response"]["message"]="actual reply"
    assert recording.convert(CASES,[trace],[binding],[label],backend="scripted")[0][0]["response"]["message"]=="actual reply"


@pytest.mark.parametrize("result", ["blocked", "skipped", ""])
def test_unexecuted_say_cannot_certify_a_reply(native, result):
    trace, binding, label = native([{"content": "", "tool_calls": []}])
    trace["steps"] = [{"tool": "say", "args": json.dumps({"text": "proposed reply"}),
                       "receipt": {"act": "say", "result": result}}]
    label["response"].update(decision="reply", message="proposed reply")
    with pytest.raises(ValueError, match="recording_unobserved_reply"):
        recording.convert(CASES, [trace], [binding], [label], backend="scripted")


def test_reply_summary_uses_the_actual_receipt_and_marks_clipping():
    from loop.trace import TurnTrace

    trace = TurnTrace("speech-receipt")
    receipt = {"act": "say", "result": "sent", "ack": "queued", "text": "实际正文" * 1500}
    trace.step(i=1, tool="say", args={"text": "原拟稿"}, obs="", receipt=receipt, ms=0)
    trace.outcome(receipts=[receipt], completed=True)
    traced_receipt = trace.rec["steps"][0]["receipt"]
    assert traced_receipt["text"].startswith("实际正文")
    assert traced_receipt["text_truncated"] is True
    assert trace.rec["outcome"]["said"][0]["text"].startswith("实际正文")
    assert trace.rec["outcome"]["said"][0]["ack"] == "queued"
    with pytest.raises(ValueError, match="recording_reply_text_truncated"):
        recording.observed_reply_texts(trace.rec)


def test_export_cli_is_file_only_and_preserves_inputs(native,tmp_path):
    trace,binding,label=native([{"content":"","tool_calls":[]}])
    paths=[tmp_path/name for name in ("traces.jsonl","bindings.jsonl","annotations.jsonl")]
    for path,rows in zip(paths,([trace],[binding],[label])):write_jsonl(path,rows)
    args=[sys.executable,"-m","evals.runtime_recording","--traces",str(paths[0]),"--bindings",str(paths[1]),
          "--annotations",str(paths[2]),"--backend","scripted","--output",str(tmp_path/"responses.jsonl"),
          "--evidence-output",str(tmp_path/"private-evidence.json")]
    result=subprocess.run(args,cwd=ROOT,capture_output=True,timeout=30)
    assert result.returncode==0,result.stderr
    assert len(load_jsonl(tmp_path/"responses.jsonl"))==1
    before=paths[0].read_bytes()
    args[args.index("--output")+1]=str(paths[0])
    assert subprocess.run(args,cwd=ROOT,capture_output=True,timeout=30).returncode==2
    assert paths[0].read_bytes()==before
