"""Versioned offline boundary, deliberately separate from runtime tool schemas."""
from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
from collections import Counter, defaultdict
from pathlib import Path

SCHEMA = "cognitive-eval/v1"
CATEGORIES = {"group_response", "private_promise", "feedback_unknown",
              "memory_correction", "identity_scope", "silence"}
FIELDS = {"decision", "message", "evidence_refs", "memory_refs", "completed_goals",
          "delivery_status", "relation", "appraisal_refs", "observation_status"}
LIST_FIELDS = {"evidence_refs", "memory_refs", "completed_goals", "appraisal_refs"}
DELIVERY = {"unknown": 0, "local_observed": 1, "delivered": 2, "read": 3}
DIMENSIONS = ("attribution", "completion", "disturbance", "privacy", "appraisal_basis")
METRICS = ("model_calls", "input_tokens", "output_tokens", "cost_usd", "latency_ms")
CONDITIONS = {"full", "without_memory", "without_goals", "without_appraisal"}


def load_jsonl(path):
    rows = []
    with Path(path).open(encoding="utf-8") as stream:
        for line_no, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid_json_line:{line_no}") from exc
            if not isinstance(row, dict):
                raise ValueError(f"object_required_line:{line_no}")
            rows.append(row)
    return rows


def digest(value):
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def atomic_text(path, text):
    """Write only to caller-selected artifacts; no app data/config imports."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def write_json(path, value):
    atomic_text(path, json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n")


def write_jsonl(path, values):
    atomic_text(path, "".join(json.dumps(v, ensure_ascii=False, allow_nan=False) + "\n" for v in values))


def differences(left, right, path=""):
    if type(left) is not type(right):
        return [path]
    if isinstance(left, dict):
        if left.keys() != right.keys():
            return [path]
        return [p for k in left for p in differences(left[k], right[k], path + "/" + k)]
    if isinstance(left, list):
        if len(left) != len(right):
            return [path]
        return [p for i, (a, b) in enumerate(zip(left, right))
                for p in differences(a, b, path + "/" + str(i))]
    return [] if left == right else [path]


def at_path(value, path):
    for key in path.strip("/").split("/"):
        value = value[int(key)] if isinstance(value, list) else value[key]
    return value


def check_response(response):
    if not isinstance(response, dict) or set(response) != FIELDS:
        raise ValueError("response_fields_invalid")
    for key in LIST_FIELDS:
        values = response[key]
        if (not isinstance(values, list) or any(not isinstance(v, str) or not v for v in values)
                or len(set(values)) != len(values)):
            raise ValueError("response_refs_invalid:" + key)
    if not isinstance(response["message"], str):
        raise ValueError("message_invalid")
    options = {"decision": {"reply", "wait", "act"}, "delivery_status": set(DELIVERY),
               "relation": {"unchanged", "supported_revision"},
               "observation_status": {"unknown", "quiet", "active"}}
    for key, allowed in options.items():
        if not isinstance(response[key], str) or response[key] not in allowed:
            raise ValueError("response_enum_invalid:" + key)
    if response["decision"] == "wait" and response["message"]:
        raise ValueError("wait_cannot_send_message")


def _scope(scope):
    if (not isinstance(scope, dict) or set(scope) != {"platform", "account", "chat"}
            or any(not isinstance(v, str) or not v.strip() for v in scope.values())):
        raise ValueError("scope_invalid")


def _booleans(value, keys):
    if not isinstance(value, dict) or any(type(value.get(k)) is not bool for k in keys):
        raise ValueError("boolean_fields_invalid")


def _refs(values, allowed):
    if (not isinstance(values, list) or not values
            or any(not isinstance(v, str) or v not in allowed for v in values)
            or len(set(values)) != len(values)):
        raise ValueError("input_evidence_refs_invalid")


def validate_input(data):
    needed = {"scope", "dialogue", "authorization", "context", "evidence", "memories",
              "goals", "appraisals", "link", "observation", "tool_results"}
    if not isinstance(data, dict) or set(data) != needed:
        raise ValueError("input_fields_invalid")
    _scope(data["scope"])
    auth, context = data["authorization"], data["context"]
    _booleans(auth, ("reply", "send_reminder", "read_linked"))
    _booleans(context, ("addressed", "resolved", "self_event"))
    if context["task"] not in CATEGORIES or type(context["memory_head_revision"]) is not int or context["memory_head_revision"] < 1:
        raise ValueError("context_invalid")
    for name in ("evidence", "memories", "goals", "appraisals", "dialogue"):
        if not isinstance(data[name], list):
            raise ValueError("input_list_required:" + name)
        ids = [item.get("id") for item in data[name] if isinstance(item, dict)]
        if (len(ids) != len(data[name]) or any(not isinstance(i, str) or not i for i in ids)
                or len(set(ids)) != len(ids)):
            raise ValueError("input_ids_invalid:" + name)
    evidence_ids = {e["id"] for e in data["evidence"]}
    goal_ids = {g["id"] for g in data["goals"]}
    for event in data["evidence"]:
        _scope(event["scope"])
        _booleans(event, ("visible",))
        if "self" in event:
            _booleans(event, ("self",))
        if event["kind"] not in {"message", "tool_receipt"} or not isinstance(event["text"], str):
            raise ValueError("evidence_kind_or_text_invalid")
        if event["kind"] == "tool_receipt" and event.get("goal_id") not in goal_ids:
            raise ValueError("tool_receipt_goal_unknown")
    for message in data["dialogue"]:
        if message["id"] not in evidence_ids or not all(isinstance(message[k], str) for k in ("actor", "text")):
            raise ValueError("dialogue_invalid")
    for memory in data["memories"]:
        _scope(memory["scope"])
        _booleans(memory, ("active",))
        _refs(memory["evidence_refs"], evidence_ids)
        if type(memory["revision"]) is not int or memory["revision"] < 1 or not isinstance(memory["value"], str):
            raise ValueError("memory_invalid")
    for goal in data["goals"]:
        _booleans(goal, ("authorized", "due", "dependencies_ready"))
        if goal["state"] not in {"active", "cancelled", "closed"}:
            raise ValueError("goal_state_invalid")
    for appraisal in data["appraisals"]:
        _scope(appraisal["scope"])
        _refs(appraisal["evidence_refs"], evidence_ids)
        if not isinstance(appraisal["meaning"], str) or not appraisal["meaning"]:
            raise ValueError("appraisal_meaning_required")
    link, observation = data["link"], data["observation"]
    _booleans(link, ("verified",))
    _scope(link["source"])
    _scope(link["target"])
    _booleans(observation, ("healthy",))
    if observation["delivery_status"] not in DELIVERY:
        raise ValueError("delivery_status_invalid")
    hours = observation["elapsed_hours"]
    if type(hours) not in (int, float) or not math.isfinite(hours) or hours < 0:
        raise ValueError("elapsed_hours_invalid")
    if not isinstance(data["tool_results"], dict) or set(data["tool_results"]) - goal_ids:
        raise ValueError("tool_goal_unknown")
    for result in data["tool_results"].values():
        if (not isinstance(result, dict) or result.get("status") not in {"pending", "success", "failure"}
                or result.get("evidence_ref") not in evidence_ids):
            raise ValueError("tool_result_invalid")


def validate_cases(rows, *, fixed=True):
    pairs = defaultdict(list)
    ids = set()
    required = {"schema", "id", "pair_id", "variant", "category", "title", "input",
                "intervention", "expected", "forbidden", "rubric"}
    for row in rows:
        if set(row) != required or row["schema"] != SCHEMA:
            raise ValueError("scenario_schema_invalid")
        if row["id"] in ids or not isinstance(row["id"], str) or not row["id"]:
            raise ValueError("scenario_id_duplicate_or_invalid")
        ids.add(row["id"])
        if row["category"] not in CATEGORIES or row["variant"] not in {"base", "counterfactual"}:
            raise ValueError("scenario_category_or_variant_invalid")
        if not row["forbidden"] or not isinstance(row["rubric"], str) or not row["rubric"]:
            raise ValueError("scenario_rubric_required")
        check_response(dict(row["expected"], message="" if row["expected"].get("decision") == "wait" else "fixture"))
        data = row["input"]
        validate_input(data)
        needed = {"scope", "dialogue", "authorization", "context", "evidence", "memories",
                  "goals", "appraisals", "link", "observation", "tool_results"}
        if not isinstance(data, dict) or set(data) != needed:
            raise ValueError("input_fields_invalid")
        if set(data["scope"]) != {"platform", "account", "chat"}:
            raise ValueError("scope_invalid")
        for key in ("evidence", "memories", "goals", "appraisals"):
            refs = [item["id"] for item in data[key]]
            if len(refs) != len(set(refs)):
                raise ValueError("input_refs_duplicate:" + key)
        pairs[row["pair_id"]].append(row)
    for pair in pairs.values():
        if len(pair) != 2 or {r["variant"] for r in pair} != {"base", "counterfactual"}:
            raise ValueError("pair_requires_two_variants")
        a, b = sorted(pair, key=lambda r: r["variant"])
        change = a["intervention"]
        if (change != b["intervention"] or set(change) != {"path", "before", "after"}
                or a["category"] != b["category"]
                or differences(a["input"], b["input"]) != [change["path"]]
                or at_path(a["input"], change["path"]) != change["before"]
                or at_path(b["input"], change["path"]) != change["after"]):
            raise ValueError("counterfactual_must_change_one_declared_factor")
    if fixed and (len(rows) != 40 or len(pairs) != 20 or {r["category"] for r in rows} != CATEGORIES):
        raise ValueError("fixed_suite_requires_40_cases_20_pairs_6_categories")
    return rows


def validate_usage(usage):
    if not isinstance(usage, dict) or set(usage) - set(METRICS):
        raise ValueError("usage_fields_invalid")
    for key, value in usage.items():
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or value < 0:
            raise ValueError("usage_value_invalid:" + key)
        if key in {"model_calls", "input_tokens", "output_tokens"} and not isinstance(value, int):
            raise ValueError("usage_count_must_be_integer:" + key)
    return usage


def summarize_metrics(results):
    out = {}
    for key in METRICS:
        measured = [r["usage"][key] for r in results if r["usage"].get(key) is not None]
        out[key] = {"total": sum(measured) if len(measured) == len(results) else None,
                    "measured_cases": len(measured), "total_cases": len(results)}
    return out


def group_results(results, field):
    groups = defaultdict(list)
    for row in results:
        groups[row[field]].append(row)
    return {name: {"total": len(items), "passed": sum(r["passed"] for r in items),
                   "violations": dict(Counter(v for r in items for v in r["violations"]))}
            for name, items in sorted(groups.items())}
