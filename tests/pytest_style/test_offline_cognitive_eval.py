"""Counterexamples for the offline harness; no production runtime imports."""
from __future__ import annotations

import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest

from evals import fixtures, report, run_eval
from evals.schema import (CATEGORIES, check_response, digest, load_jsonl,
                         validate_cases, validate_usage, write_jsonl)

ROOT = Path(__file__).resolve().parents[2]
CASES = load_jsonl(run_eval.DEFAULT_CASES)


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_scripted_policy_obeys_independent_labels(case):
    assert run_eval.score(case, fixtures.respond(case["input"])) == []


def test_fixed_dataset_and_single_factor_contract():
    assert validate_cases(CASES) == CASES
    assert len(CASES) == 40
    assert len({c["pair_id"] for c in CASES}) == 20
    assert {c["category"] for c in CASES} == CATEGORIES
    assert digest(CASES) == digest(copy.deepcopy(CASES))


@pytest.mark.parametrize("mutation", ["duplicate", "missing", "two_factors", "wrong_path", "same_variant", "changed_category"])
def test_bad_dataset_is_rejected(mutation):
    rows = copy.deepcopy(CASES)
    if mutation == "duplicate":
        rows[1]["id"] = rows[0]["id"]
    elif mutation == "missing":
        rows.pop()
    elif mutation == "two_factors":
        rows[1]["input"]["authorization"]["reply"] = False
    elif mutation == "wrong_path":
        rows[0]["intervention"]["path"] = "/context/resolved"
    elif mutation == "same_variant":
        rows[1]["variant"] = "base"
    else:
        rows[1]["category"] = "silence"
    with pytest.raises(ValueError):
        validate_cases(rows)


@pytest.mark.parametrize("mode,guard", [("unsafe_delivery", "forbidden:delivery_inflation"),
    ("unsafe_memory", "forbidden:memory_scope_or_version"),
    ("unsafe_completion", "forbidden:unproven_completion"),
    ("unsafe_relation", "forbidden:unsupported_relation"),
    ("unsafe_reply", "forbidden:unauthorized_reply")])
def test_broken_policies_are_caught(mode, guard):
    result = run_eval.evaluate(CASES, fixture=mode)
    assert result["summary"]["passed"] < 40
    assert any(guard in row["violations"] for row in result["results"])


def test_fixture_does_not_read_expected_or_mutate_input():
    case = copy.deepcopy(CASES[1])
    before = copy.deepcopy(case["input"])
    response = fixtures.respond(case["input"])
    case["expected"]["decision"] = "wait"
    assert fixtures.respond(case["input"]) == response
    assert run_eval.score(case, response)
    assert case["input"] == before


@pytest.mark.parametrize("response", [None, {}, {"decision": "wait"}, "hello"])
def test_invalid_responses_are_failures(response):
    assert run_eval.score(CASES[0], response)[0].startswith("invalid_response:")


def test_wait_cannot_hide_a_message_and_refs_cannot_duplicate():
    response = fixtures.respond(CASES[0]["input"])
    response["message"] = "still sending"
    with pytest.raises(ValueError, match="wait_cannot"):
        check_response(response)
    response["message"] = ""
    response["evidence_refs"] = ["e1", "e1"]
    with pytest.raises(ValueError, match="refs_invalid"):
        check_response(response)


def metadata(**overrides):
    return dict({"system": "candidate", "model": "recorded-test-model", "seed": 1, "repeat": 0, "condition": "full"}, **overrides)


def recorded():
    return {c["id"]: {"id": c["id"], "response": fixtures.respond(c["input"])} for c in CASES}


def test_missing_replay_is_failure_and_usage_stays_unknown():
    rows = recorded()
    del rows[CASES[0]["id"]]
    result = run_eval.evaluate(CASES, replay=rows, metadata=metadata())
    assert result["summary"]["passed"] == 39
    assert result["results"][0]["violations"] == ["missing_response"]
    assert result["summary"]["metrics"]["model_calls"]["total"] is None
    assert result["summary"]["metrics"]["cost_usd"]["measured_cases"] == 0


@pytest.mark.parametrize("kind", ["duplicate", "unknown", "bad_usage", "bad_json"])
def test_replay_index_rejects_bad_records(tmp_path, kind):
    rows = list(recorded().values())
    if kind == "duplicate":
        rows.append(rows[0])
    elif kind == "unknown":
        rows[0]["id"] = "not-in-suite"
    elif kind == "bad_usage":
        rows[0]["usage"] = {"model_calls": -1}
    path = tmp_path / "recorded.jsonl"
    write_jsonl(path, rows)
    if kind == "bad_json":
        path.write_text("{broken", encoding="utf-8")
    with pytest.raises(ValueError):
        run_eval.replay_index(path, CASES)


@pytest.mark.parametrize("usage", [{"cost_usd": float("nan")}, {"latency_ms": float("inf")},
    {"model_calls": True}, {"input_tokens": 1.5}, {"cost_usd": -1}, {"other": 1}])
def test_invalid_usage_is_not_silently_counted(usage):
    with pytest.raises(ValueError):
        validate_usage(usage)


def test_partial_metrics_are_unknown_and_complete_totals_are_measured():
    rows = recorded()
    rows[CASES[0]["id"]]["usage"] = {"cost_usd": 0.01}
    result = run_eval.evaluate(CASES, replay=rows, metadata=metadata())
    assert result["summary"]["metrics"]["cost_usd"]["total"] is None
    assert result["summary"]["metrics"]["cost_usd"]["measured_cases"] == 1
    for row in rows.values():
        row["usage"] = {"model_calls": 2}
    result = run_eval.evaluate(CASES, replay=rows, metadata=metadata())
    assert result["summary"]["metrics"]["model_calls"]["total"] == 80


def test_recorded_mode_requires_provenance():
    with pytest.raises(ValueError, match="metadata"):
        run_eval.evaluate(CASES, replay=recorded())
    with pytest.raises(ValueError, match="metadata"):
        run_eval.evaluate(CASES, replay=recorded(), metadata=metadata(condition="without_goals"))


@pytest.mark.parametrize("setting,value", [("model", "other-model"), ("seed", 9), ("repeat", 1), ("condition", "without_goals")])
def test_comparison_rejects_uncontrolled_settings(setting, value):
    left = run_eval.evaluate(CASES)
    right = copy.deepcopy(left)
    right["metadata"][setting] = value
    with pytest.raises(ValueError, match="setting_mismatch"):
        report.compare_runs(left, right)


def test_comparison_names_each_regression_and_pair_group_is_complete():
    good = run_eval.evaluate(CASES)
    broken = run_eval.evaluate(CASES, fixture="unsafe_delivery")
    compared = report.compare_runs(good, broken)
    assert len(compared["regressions"]) == 40
    assert good["summary"]["pairs"]["p01"] == {"total": 2, "passed": 2, "violations": {}}
    assert good["quality_verdict"].startswith("unmeasured")


def test_blind_packet_hides_labels_and_run_identity_and_key_can_rejoin():
    runs = [run_eval.evaluate(CASES), run_eval.evaluate(CASES, fixture="unsafe_memory")]
    packet, key, scores = report.blind_packet(CASES, runs, seed=7)
    encoded = json.dumps(packet)
    for forbidden in ('"expected"', '"metadata"', '"system"', '"passed"', '"violations"', 'fixture:', 'p01a'):
        assert forbidden not in encoded
    assert len(packet) == 40 and len(key) == 80 and len(scores) == 80
    assert all(all(v is None for v in row["scores"].values()) for row in scores)
    lookup = {k["code"]: k for k in key}
    for question in packet:
        for candidate in question["candidates"]:
            mapped = lookup[candidate["code"]]
            actual = next(r for r in runs[mapped["run_index"]]["results"] if r["id"] == mapped["case_id"])
            assert candidate["response"] == actual["response"]
    assert report.blind_packet(CASES, runs, seed=7) == (packet, key, scores)


def test_ablations_are_explicit_and_do_not_claim_semantic_contribution():
    full = run_eval.evaluate(CASES)
    only = report.make_report(CASES, [full])
    assert all(a["status"] == "not_run" for a in only["ablations"])
    assert only["variability"][0]["stdev_failure_rate"] is None
    runs = [full] + [run_eval.evaluate(CASES, condition=c) for c in ("without_memory", "without_goals", "without_appraisal")]
    result = report.make_report(CASES, runs)
    assert all(a["measurements"][0]["changed_cases"] for a in result["ablations"])
    assert all(a["semantic_contribution"] == "unmeasured" for a in result["ablations"])
    assert result["human_review"]["status"] == "not_scored"
    assert result["release_gate"] == "not_evaluated"


def test_report_rejects_suite_drift_and_duplicate_samples():
    result = run_eval.evaluate(CASES)
    with pytest.raises(ValueError, match="duplicate_repeat"):
        report.make_report(CASES, [result, copy.deepcopy(result)])
    changed = copy.deepcopy(result)
    changed["suite_sha256"] = "not-the-suite"
    with pytest.raises(ValueError, match="suite_mismatch"):
        report.make_report(CASES, [changed])


def test_repeated_samples_report_variability():
    left = run_eval.evaluate(CASES, replay=recorded(), metadata=metadata())
    rows = recorded()
    del rows[CASES[0]["id"]]
    right = run_eval.evaluate(CASES, replay=rows, metadata=metadata(repeat=1))
    variation = report.make_report(CASES, [left, right])["variability"][0]
    assert variation["samples"] == 2
    assert variation["min_failure_rate"] == 0
    assert variation["max_failure_rate"] == 1 / 40
    assert variation["stdev_failure_rate"] > 0


def test_harness_cannot_use_network_and_has_zero_model_calls(monkeypatch):
    import socket
    def denied(*args, **kwargs):
        pytest.fail("offline harness attempted network")
    monkeypatch.setattr(socket, "socket", denied)
    result = run_eval.evaluate(CASES)
    report.blind_packet(CASES, [result])
    assert result["summary"]["metrics"]["model_calls"]["total"] == 0
    assert result["summary"]["metrics"]["cost_usd"]["total"] == 0


def test_cli_artifacts_and_failure_exit_codes(tmp_path):
    good_path, bad_path = tmp_path / "good.json", tmp_path / "bad.json"
    def command(module, *args):
        return subprocess.run([sys.executable, "-m", module, *map(str, args)], cwd=ROOT,
                              capture_output=True, text=True, timeout=30)
    good = command("evals.run_eval", "--output", good_path)
    assert good.returncode == 0, good.stderr
    bad = command("evals.run_eval", "--fixture", "unsafe_delivery", "--output", bad_path)
    assert bad.returncode == 1, bad.stderr
    outdir = tmp_path / "report"
    rendered = command("evals.report", "--runs", good_path, bad_path, "--outdir", outdir)
    assert rendered.returncode == 0, rendered.stderr
    assert len(load_jsonl(outdir / "blind.jsonl")) == 40
    assert len(json.loads((outdir / "private/blind_key.json").read_text(encoding="utf-8"))) == 80
    assert "未评分" in (outdir / "report.md").read_text(encoding="utf-8")
    text = good_path.read_bytes()
    blocked = command("evals.report", "--runs", good_path, "--outdir", good_path.parent)
    # This outdir is safe: report.json has a different name than good.json.
    assert blocked.returncode == 0
    assert good_path.read_bytes() == text


def test_cli_does_not_overwrite_source_case_file(tmp_path):
    source = tmp_path / "cases.jsonl"
    source.write_bytes(run_eval.DEFAULT_CASES.read_bytes())
    original = source.read_bytes()
    result = subprocess.run([sys.executable, "-m", "evals.run_eval", "--cases", str(source),
                             "--output", str(source)], cwd=ROOT, capture_output=True, timeout=30)
    assert result.returncode == 2
    assert source.read_bytes() == original


@pytest.mark.parametrize("path,value", [("reply", "false"), ("reply", 0), ("read_linked", None)])
def test_permission_values_cannot_be_truthy_strings(path, value):
    rows = copy.deepcopy(CASES)
    rows[0]["input"]["authorization"][path] = value
    with pytest.raises(ValueError, match="boolean_fields"):
        validate_cases(rows)


def test_request_message_is_not_a_completion_receipt():
    case = copy.deepcopy(next(c for c in CASES if c["id"] == "p05b"))
    case["input"]["tool_results"]["g1"]["evidence_ref"] = "e1"
    response = fixtures.respond(case["input"])
    assert response["completed_goals"] == []
    response["completed_goals"] = ["g1"]
    assert "forbidden:unproven_completion" in run_eval.score(case, response)


def test_receipt_for_another_goal_cannot_close_this_goal():
    case = copy.deepcopy(next(c for c in CASES if c["id"] == "p05b"))
    case["input"]["evidence"][1]["goal_id"] = "other-goal"
    response = fixtures.respond(case["input"])
    assert response["completed_goals"] == []
    response["completed_goals"] = ["g1"]
    assert "forbidden:unproven_completion" in run_eval.score(case, response)


def test_report_prevents_overwriting_an_input_run(tmp_path):
    run = run_eval.evaluate(CASES)
    from evals.schema import write_json
    path = tmp_path / "report.json"
    write_json(path, run)
    before = path.read_bytes()
    result = subprocess.run([sys.executable, "-m", "evals.report", "--runs", str(path),
                             "--outdir", str(tmp_path)], cwd=ROOT, capture_output=True, timeout=30)
    assert result.returncode == 2
    assert path.read_bytes() == before


@pytest.mark.parametrize("target", ["result", "summary"])
def test_report_does_not_trust_saved_green_flags(target):
    result = run_eval.evaluate(CASES, fixture="unsafe_memory")
    if target == "result":
        result["results"][0]["passed"] = True
        result["results"][0]["violations"] = []
    else:
        result["summary"]["passed"] = 40
    with pytest.raises(ValueError, match="tampered_or_stale"):
        report.make_report(CASES, [result])


def test_unknown_relative_cost_and_zero_baseline_do_not_invent_savings():
    left = run_eval.evaluate(CASES, replay=recorded(), metadata=metadata(system="old"))
    right = run_eval.evaluate(CASES, replay=recorded(), metadata=metadata(system="new"))
    assert report.compare_runs(left, right)["metric_changes"]["cost_usd"] == {"delta": None, "ratio": None}
    zero = run_eval.evaluate(CASES)
    assert report.compare_runs(zero, zero)["metric_changes"]["cost_usd"] == {"delta": 0, "ratio": None}
