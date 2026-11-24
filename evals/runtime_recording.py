"""Read explicit native traces; no runtime import, model client or network entry point."""
from __future__ import annotations

import argparse
from pathlib import Path

from .fixtures import ablate
from .run_eval import DEFAULT_CASES
from .schema import CONDITIONS, check_response, digest, load_jsonl, validate_cases, write_json, write_jsonl, validate_usage


def _index(rows, field):
    result={}
    for row in rows:
        value=row.get(field)
        if not isinstance(value,str) or not value or value in result:
            raise ValueError("recording_duplicate_or_invalid:"+field)
        result[value]=row
    return result


def observed_reply_texts(trace: dict) -> list[str]:
    """Read the text accepted by the send path or captured in rehearsal."""
    said = []
    for step in trace.get("steps") or ():
        receipt = step.get("receipt")
        if not isinstance(receipt, dict) or receipt.get("act") != "say":
            continue
        if receipt.get("result") not in {"sent", "rehearsal"}:
            continue
        text = receipt.get("text")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("recording_reply_text_missing")
        if receipt.get("text_truncated") is True:
            raise ValueError("recording_reply_text_truncated")
        said.append(text)
    return said


def convert(cases, traces, bindings, annotations, *, condition="full", backend):
    """Use annotations for interpretation and runtime receipts for reply text.

    Input preparation is declared and hash-checked, not certified by the trace.
    A partial recording remains partial when evaluated against all forty cases.
    """
    validate_cases(cases)
    if condition not in CONDITIONS or backend not in {"scripted","model"}:
        raise ValueError("recording_mode_invalid")
    suite=_index(cases,"id");native=_index(traces,"turn_id")
    mapped=_index(bindings,"id");labels=_index(annotations,"id")
    if not mapped or set(mapped)!=set(labels) or set(mapped)-set(suite):
        raise ValueError("recording_annotation_ids_mismatch")
    used=set();rows=[];evidence=[]
    for identifier,binding in mapped.items():
        if set(binding)!={"id","turn_id","input","condition"} or binding["condition"]!=condition:
            raise ValueError("recording_binding_invalid")
        turn_id=binding["turn_id"]
        if turn_id in used or turn_id not in native:
            raise ValueError("recording_turn_missing_or_reused")
        used.add(turn_id)
        expected_input=ablate(suite[identifier]["input"],condition)
        if digest(binding["input"])!=digest(expected_input):
            raise ValueError("recording_input_changed")
        trace=native[turn_id];scope=trace.get("scope",{})
        actual_scope={"platform":scope.get("platform"),"account":scope.get("account_id"),"chat":scope.get("chat_id")}
        if actual_scope!=expected_input["scope"]:
            raise ValueError("recording_scope_mismatch")
        outcome=trace.get("outcome",{})
        if type(outcome.get("completed")) is not bool:
            raise ValueError("recording_completion_unknown")
        if trace.get("prompt",{}).get("hash_basis")!="initial_messages":
            raise ValueError("recording_actual_prompt_unknown")
        prompt_hash=trace.get("prompt",{}).get("sha256")
        if not isinstance(prompt_hash,str) or len(prompt_hash)!=64:
            raise ValueError("recording_prompt_hash_missing")
        if set(labels[identifier])!={"id","response"}:
            raise ValueError("recording_annotation_fields_invalid")
        calls=trace.get("calls")
        if not isinstance(calls,list) or any(not isinstance(c,dict) for c in calls):
            raise ValueError("recording_calls_invalid")
        failed=bool(trace.get("notes",{}).get("llm_error")) or not outcome["completed"] or any(c.get("error") for c in calls)
        response=labels[identifier]["response"]
        if failed:
            response=None
        else:
            check_response(response)
            if any(step.get("tool") in {"sticker", "show"} for step in trace.get("steps") or ()):
                raise ValueError("recording_multimedia_adapter_required")
            said = observed_reply_texts(trace)
            if said:
                if response["decision"]!="reply" or response["message"]!="\n".join(said):
                    raise ValueError("recording_reply_annotation_mismatch")
            elif response["decision"]=="reply" or response["message"]:
                raise ValueError("recording_unobserved_reply")
            if response["decision"]=="act" and not trace.get("steps"):
                raise ValueError("recording_unobserved_action")
        usage={"model_calls":0 if backend=="scripted" else (None if failed else len(calls)),
               "input_tokens":0 if backend=="scripted" else None,
               "output_tokens":0 if backend=="scripted" else None,
               "cost_usd":0 if backend=="scripted" else None,"latency_ms":outcome.get("ms")}
        validate_usage(usage)
        rows.append({"id":identifier,"response":response,"usage":usage})
        evidence.append({"id":identifier,"turn_id":turn_id,"trace_sha256":digest(trace),
            "input_sha256":digest(expected_input),"prompt_sha256":prompt_hash,"backend_declared":backend,
            "condition":condition,"turn_complete":not failed,"input_preparation":"declared_not_semantically_certified",
            "normalization":"independent_annotation_with_native_reply_check","dry_run":trace.get("dry_run")})
    return rows,{"schema":"runtime-recording/v1","suite_sha256":digest(cases),"recorded_cases":len(rows),
                 "total_cases":len(cases),"evidence":evidence}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases",type=Path,default=DEFAULT_CASES)
    for name in ("traces","bindings","annotations","output","evidence-output"):
        parser.add_argument("--"+name,type=Path,required=True)
    parser.add_argument("--backend",choices=["scripted","model"],required=True)
    parser.add_argument("--condition",choices=sorted(CONDITIONS),default="full")
    args=parser.parse_args(argv)
    try:
        inputs={p.resolve() for p in (args.cases,args.traces,args.bindings,args.annotations)}
        outputs={args.output.resolve(),args.evidence_output.resolve()}
        if len(outputs)!=2 or inputs & outputs:
            raise ValueError("recording_output_overwrites_input_or_evidence")
        rows,evidence=convert(load_jsonl(args.cases),load_jsonl(args.traces),load_jsonl(args.bindings),
            load_jsonl(args.annotations),condition=args.condition,backend=args.backend)
        write_jsonl(args.output,rows);write_json(args.evidence_output,evidence)
    except (ValueError,TypeError,KeyError,OSError) as exc:
        parser.error(str(exc))
    print(f"exported {len(rows)} native turns; semantic quality unmeasured")
    return 0


if __name__=="__main__":
    raise SystemExit(main())
