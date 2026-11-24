"""Reports and anonymized rater packets for saved offline runs only."""
from __future__ import annotations

import argparse
import json
import random
import statistics
from collections import defaultdict
from pathlib import Path

from .run_eval import DEFAULT_CASES, score
from .schema import (CONDITIONS, DIMENSIONS, SCHEMA, digest, load_jsonl, validate_cases,
                     write_json, write_jsonl, atomic_text, validate_usage, summarize_metrics, group_results)


def check_runs(cases, runs):
    validate_cases(cases)
    expected = {c["id"] for c in cases}
    if not runs:
        raise ValueError("runs_required")
    for run in runs:
        if run["schema"] != SCHEMA or run["suite_sha256"] != digest(cases):
            raise ValueError("report_suite_mismatch")
        ids = [r["id"] for r in run["results"]]
        if set(ids) != expected or len(ids) != len(expected):
            raise ValueError("report_case_ids_mismatch")
        lookup = {c["id"]: c for c in cases}
        for result in run["results"]:
            case = lookup[result["id"]]
            missing = result["response"] is None and result["violations"] == ["missing_response"]
            violations = ["missing_response"] if missing else score(case, result["response"])
            if (result["violations"] != violations or type(result["passed"]) is not bool
                    or result["passed"] != (not violations) or result["pair_id"] != case["pair_id"]
                    or result["category"] != case["category"]):
                raise ValueError("report_result_tampered_or_stale")
            validate_usage(result["usage"])
        results = run["results"]
        passed = sum(r["passed"] for r in results)
        summary = {"total": len(results), "passed": passed, "failure_rate": 1 - passed / len(results),
                   "categories": group_results(results, "category"), "pairs": group_results(results, "pair_id"),
                   "metrics": summarize_metrics(results)}
        # Use the same subtraction order as evaluate to avoid float artifacts.
        summary["failure_rate"] = (len(results) - passed) / len(results)
        if run["summary"] != summary:
            raise ValueError("report_summary_tampered_or_stale")


def compare_runs(baseline, candidate):
    if baseline["suite_sha256"] != candidate["suite_sha256"] or baseline["kind"] != candidate["kind"]:
        raise ValueError("comparison_suite_or_adapter_mismatch")
    for key in ("model", "seed", "repeat", "condition"):
        if baseline["metadata"][key] != candidate["metadata"][key]:
            raise ValueError("comparison_setting_mismatch:" + key)
    left = {r["id"]: r for r in baseline["results"]}
    right = {r["id"]: r for r in candidate["results"]}
    if left.keys() != right.keys():
        raise ValueError("comparison_cases_mismatch")
    metrics = {}
    for name, left_value in baseline["summary"]["metrics"].items():
        before = left_value["total"]
        after = candidate["summary"]["metrics"][name]["total"]
        measured = before is not None and after is not None
        metrics[name] = {"delta": after - before if measured else None,
                         "ratio": after / before if measured and before > 0 else None}
    return {"baseline": baseline["metadata"]["system"], "candidate": candidate["metadata"]["system"],
            "metric_changes": metrics,
            "regressions": [i for i in left if left[i]["passed"] and not right[i]["passed"]],
            "improvements": [i for i in left if not left[i]["passed"] and right[i]["passed"]],
            "semantic_verdict": "unmeasured", "randomness_control": "recorded_seed_only_no_determinism_claim"}


def blind_packet(cases, runs, seed=0):
    check_runs(cases, runs)
    rng = random.Random(seed)
    case_order = list(cases)
    rng.shuffle(case_order)
    indexes = [{row["id"]: row for row in run["results"]} for run in runs]
    packet, key, scores = [], [], []
    for number, case in enumerate(case_order, 1):
        question = f"q{number:03d}"
        order = list(range(len(runs)))
        rng.shuffle(order)
        candidates = []
        for position, run_index in enumerate(order, 1):
            code = question + f"-c{position:02d}"
            response = indexes[run_index][case["id"]]["response"]
            candidates.append({"code": code, "response": response})
            key.append({"code": code, "case_id": case["id"], "run_index": run_index,
                        "metadata": runs[run_index]["metadata"], "expected": case["expected"]})
            scores.append({"code": code, "rater_id": "", "scores": {d: None for d in DIMENSIONS},
                           "comment": ""})
        packet.append({"question": question, "input": case["input"], "candidates": candidates})
    return packet, key, scores


def blind_manifest(cases,runs,packet,key,seed=0):
    return {"schema":"blind-bundle/v1","suite_sha256":digest(cases),"run_sha256":[digest(r) for r in runs],
            "seed":seed,"packet_sha256":digest(packet),"key_sha256":digest(key)}


def make_report(cases, runs, *, review=None, blind_seed=0):
    check_runs(cases, runs)
    human = {"status":"not_scored","dimensions":list(DIMENSIONS)}
    if review is not None:
        from .human_review import verify_review
        human = verify_review(cases,runs,review,seed=blind_seed)
    groups = defaultdict(list)
    for run in runs:
        meta = run["metadata"]
        groups[(meta["system"], meta["condition"], meta["model"])].append(run)
    variability = []
    for (system, condition, model), group in groups.items():
        identities = [(g["metadata"]["repeat"], g["metadata"]["seed"]) for g in group]
        if len(identities) != len(set(identities)):
            raise ValueError("duplicate_repeat_cannot_count_as_independent_sample")
        rates = [g["summary"]["failure_rate"] for g in group]
        variability.append({"system": system, "condition": condition, "model": model,
                            "samples": len(rates), "mean_failure_rate": statistics.mean(rates),
                            "min_failure_rate": min(rates), "max_failure_rate": max(rates),
                            "stdev_failure_rate": statistics.stdev(rates) if len(rates) > 1 else None})
    comparisons = []
    unmatched = []
    full = [(i, r) for i, r in enumerate(runs) if r["metadata"]["condition"] == "full"]
    for pos, (i, left) in enumerate(full):
        for j, right in full[pos + 1:]:
            if left["metadata"]["system"] == right["metadata"]["system"]:
                continue
            try:
                comparison = compare_runs(left, right)
            except ValueError as exc:
                unmatched.append({"left_run": i, "right_run": j, "reason": str(exc)})
            else:
                comparisons.append(dict(comparison, left_run=i, right_run=j))
    ablations = []
    for condition in sorted(CONDITIONS - {"full"}):
        matching = [r for r in runs if r["metadata"]["condition"] == condition]
        changes = []
        for run in matching:
            meta = run["metadata"]
            baseline = [b for _, b in full if all(b["metadata"][k] == meta[k]
                        for k in ("system", "model", "seed", "repeat")) and b["kind"] == run["kind"]]
            if len(baseline) != 1:
                changes.append({"status": "missing_matched_full_run", "metadata": meta})
                continue
            before = {r["id"]: r for r in baseline[0]["results"]}
            changes.append({"status": "measured_output_difference_only", "metadata": meta,
                            "changed_cases": [r["id"] for r in run["results"]
                                              if before[r["id"]]["response"] != r["response"]],
                            "failure_rate_delta": run["summary"]["failure_rate"] - baseline[0]["summary"]["failure_rate"]})
        ablations.append({"condition": condition, "status": "present" if matching else "not_run",
                          "measurements": changes, "semantic_contribution": "unmeasured"})
    return {"schema": SCHEMA, "runs": [{"kind": r["kind"], "metadata": r["metadata"],
                                        "summary": r["summary"]} for r in runs],
            "comparisons": comparisons, "unmatched_comparisons": unmatched,
            "variability": variability, "ablations": ablations,
            "human_review": human,
            "release_gate": "not_evaluated", "semantic_quality": "unmeasured"}


def markdown(report):
    lines = ["# 鲸鲸离线评测记录", "", "仅记录离线输出及规则结果；替身通过不代表真实认知循环通过。",
             "", "人工语义盲评：" + ({"not_scored":"未评分","partial":"部分录入","complete":"评分覆盖完成，待逐案例复核",
             "synthetic_only":"仅合成评分测试，不是人工验收"}.get(report["human_review"]["status"],"未知")) + "。发布门槛：未评估。", ""]
    for index, run in enumerate(report["runs"]):
        summary = run["summary"]
        lines.extend([f"## Run {index}: {run['metadata']['system']}", "",
                      f"适配器：{run['kind']}；条件：{run['metadata']['condition']}。",
                      f"规则通过：{summary['passed']}/{summary['total']}；失败率：{summary['failure_rate']:.1%}。", ""])
        for name, value in summary["metrics"].items():
            total = "未知" if value["total"] is None else str(round(value["total"], 4))
            lines.append(f"- {name}：{total}（实测 {value['measured_cases']}/{value['total_cases']} 条）")
        lines.append("")
    for comparison in report["comparisons"]:
        lines.extend([f"规则回归 {comparison['baseline']} → {comparison['candidate']}："
                      + (", ".join(comparison["regressions"]) or "无"), ""])
    if report["unmatched_comparisons"]:
        lines.extend(["存在参数不匹配的输出，未作可比性结论，见 report.json。", ""])
    lines.extend(["消融只统计输出差异，语义贡献待盲评；缺失消融保持 not_run。",
                  "费用或 token 缺失时保留未知，不补零。单次样本不报告标准差。", ""])
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--runs", nargs="+", required=True, type=Path,
                        help="Comparison direction follows input order: baseline first, candidate second")
    parser.add_argument("--outdir", required=True, type=Path)
    parser.add_argument("--blind-seed", default=0, type=int)
    parser.add_argument("--human-review", type=Path)
    args = parser.parse_args(argv)
    try:
        cases = load_jsonl(args.cases)
        runs = [json.loads(p.read_text(encoding="utf-8")) for p in args.runs]
        review = json.loads(args.human_review.read_text(encoding="utf-8")) if args.human_review else None
        report = make_report(cases, runs, review=review, blind_seed=args.blind_seed)
        packet, key, scores = blind_packet(cases, runs, args.blind_seed)
        outputs = [args.outdir / name for name in ("report.json", "report.md", "blind.jsonl",
                   "human_scores.template.jsonl", "private/blind_key.json", "private/blind_manifest.json")]
        if {p.resolve() for p in outputs} & {p.resolve() for p in [args.cases, *args.runs, *([args.human_review] if args.human_review else [])]}:
            raise ValueError("report_cannot_overwrite_input")
        write_json(outputs[0], report)
        atomic_text(outputs[1], markdown(report))
        write_jsonl(outputs[2], packet)
        write_jsonl(outputs[3], scores)
        write_json(outputs[4], key)
        write_json(outputs[5], blind_manifest(cases,runs,packet,key,args.blind_seed))
    except (ValueError, KeyError, TypeError, OSError) as exc:
        parser.error(str(exc))
    print(f"saved {len(cases)} blind questions / {len(runs)} runs; human status={report['human_review']['status']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
