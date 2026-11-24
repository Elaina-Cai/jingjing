# -*- coding: utf-8 -*-
"""把 ``live_turn`` 产出的 trace 导出成 `runtime_recording.convert()` 认的格式。

这层只做「结构校验」：trace 的 scope / prompt 哈希 / calls / steps 与场景绑定
是否对得上。**注解是 ``expected`` 派生的机器标注，不是人工评分**——语义质量
（归因对不对、完成判断准不准）这一步不测，别把这个结果当盲评结论。

用法：
    python evals/live_convert.py --condition full
    python evals/live_convert.py --condition without_memory
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

RUNS = ROOT / ".test_tmp" / "live_runs"
OUT = ROOT / ".test_tmp" / "live_export"
CASES = ROOT / "evals" / "scenarios_v1.jsonl"


def load_cases() -> list[dict]:
    return [json.loads(line) for line in CASES.read_text(encoding="utf-8").splitlines() if line.strip()]


def load_traces(condition: str) -> list[dict]:
    out = []
    for path in sorted(RUNS.glob("*.%s.json" % condition)):
        out.append(json.loads(path.read_text(encoding="utf-8")))
    return out


def binding_for(case: dict, trace: dict, condition: str) -> dict:
    from evals.fixtures import ablate

    return {
        "id": case["id"],
        "turn_id": trace["turn_id"],
        "input": ablate(case["input"], condition),
        "condition": condition,
    }


def annotation_for(case: dict, trace: dict) -> dict:
    """从场景 expected + 实际 steps 派生响应。

    decision 由**实际做了什么**决定（说了话=reply，动了别的工具=act，都没做=wait），
    其余字段取场景期望。回复文本来自主循环回执。
    """
    from evals.runtime_recording import observed_reply_texts

    message = "\n".join(observed_reply_texts(trace))
    acted = any(step.get("tool") not in {None, "", "rest", "say"} for step in trace.get("steps") or ())
    if message:
        decision = "reply"
    elif acted:
        decision = "act"
    else:
        decision = "wait"
    exp = case.get("expected") or {}
    return {
        "id": case["id"],
        "response": {
            "decision": decision,
            "message": message,
            "evidence_refs": [str(x) for x in (exp.get("evidence_refs") or [])],
            "memory_refs": [str(x) for x in (exp.get("memory_refs") or [])],
            "completed_goals": [str(x) for x in (exp.get("completed_goals") or [])],
            "delivery_status": str(exp.get("delivery_status") or "unknown"),
            "relation": str(exp.get("relation") or "unchanged"),
            "appraisal_refs": [str(x) for x in (exp.get("appraisal_refs") or [])],
            "observation_status": str(exp.get("observation_status") or "quiet"),
        },
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="导出真实回合（结构校验用）")
    parser.add_argument("--condition", default="full",
                        choices=["full", "without_memory", "without_goals", "without_appraisal"])
    args = parser.parse_args(argv)

    from evals.runtime_recording import convert

    cases = load_cases()
    by_id = {c["id"]: c for c in cases}
    traces = load_traces(args.condition)
    if not traces:
        print("没有 trace：先跑 evals/live_turn.py --condition %s" % args.condition)
        return 1

    bindings = [binding_for(by_id[t["case_id"]], t, args.condition) for t in traces]
    try:
        annotations = [annotation_for(by_id[t["case_id"]], t) for t in traces]
        print("cases=%d traces=%d bindings=%d annotations=%d condition=%s"
              % (len(cases), len(traces), len(bindings), len(annotations), args.condition))
        rows, evidence = convert(
            cases, traces, bindings, annotations,
            condition=args.condition, backend="scripted",
        )
    except (ValueError, TypeError, KeyError) as exc:
        print("STRUCT-FAIL %s: %s" % (type(exc).__name__, exc))
        return 1

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / ("responses.%s.jsonl" % args.condition)).write_text(
        "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8", newline="\n")
    (OUT / ("evidence.%s.json" % args.condition)).write_text(
        json.dumps(evidence, ensure_ascii=False, indent=1, sort_keys=True),
        encoding="utf-8", newline="\n")

    print("RECORDED %d/%d" % (evidence["recorded_cases"], evidence["total_cases"]))
    kinds = {}
    for row in rows:
        kinds[row["response"]["decision"]] = kinds.get(row["response"]["decision"], 0) + 1
    print("decision 分布：%r" % (kinds,))
    print("写出：%s / %s" % (OUT / ("responses.%s.jsonl" % args.condition),
                          OUT / ("evidence.%s.json" % args.condition)))
    print("注意：annotations 由 expected 派生，是结构校验，不是人工盲评。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
