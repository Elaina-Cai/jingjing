"""Independent scripted policy, NOT a model or the production cognitive loop.

This adapter receives inputs only: it cannot see expected labels or scoring rules.
Deliberately broken modes prove that a green harness is not a quality verdict.
"""
from __future__ import annotations

from copy import deepcopy

BROKEN_MODES = {"unsafe_delivery", "unsafe_memory", "unsafe_completion", "unsafe_relation", "unsafe_reply"}


def visible_evidence(data):
    return {e["id"] for e in data["evidence"]
            if e["visible"] and e["scope"] == data["scope"] and not e.get("self", False)}


def accessible_memories(data):
    evidence = visible_evidence(data)
    link = data["link"]
    allowed = []
    for memory in data["memories"]:
        local = memory["scope"] == data["scope"]
        linked = (link["verified"] and data["authorization"]["read_linked"]
                  and memory["scope"] == link["source"] and data["scope"] == link["target"])
        if (memory["active"] and (local or linked)
                and memory["revision"] == data["context"]["memory_head_revision"]
                and memory["evidence_refs"] and set(memory["evidence_refs"]) <= evidence):
            allowed.append(memory)
    return allowed


def ablate(data, condition):
    out = deepcopy(data)
    key = {"without_memory": "memories", "without_goals": "goals", "without_appraisal": "appraisals"}.get(condition)
    if key:
        out[key] = []
    elif condition != "full":
        raise ValueError("condition_invalid")
    return out


def respond(data, mode="policy"):
    evidence = visible_evidence(data)
    context = data["context"]
    observation = data["observation"]
    out = {"decision": "wait", "message": "", "evidence_refs": [], "memory_refs": [],
           "completed_goals": [], "delivery_status": observation["delivery_status"],
           "relation": "unchanged", "appraisal_refs": [],
           "observation_status": "quiet" if observation["healthy"] else "unknown"}
    task = context["task"]
    can_reply = (data["authorization"]["reply"] and context["addressed"]
                 and not context["resolved"] and not context["self_event"])
    if task == "group_response" and can_reply:
        out.update(decision="reply", message="我在，接着说。", evidence_refs=sorted(evidence))
    elif task in {"memory_correction", "identity_scope"} and can_reply:
        memories = accessible_memories(data)
        out.update(decision="reply", message="；".join(m["value"] for m in memories) or "目前没有可用依据。",
                   memory_refs=[m["id"] for m in memories],
                   evidence_refs=sorted({e for m in memories for e in m["evidence_refs"]}))
    elif task in {"private_promise", "silence"}:
        for goal in data["goals"]:
            if goal["state"] != "active":
                continue
            result = data["tool_results"].get(goal["id"], {})
            if (task == "private_promise" and result.get("status") == "success"
                    and result.get("evidence_ref") in evidence
                    and any(e["id"] == result["evidence_ref"] and e["kind"] == "tool_receipt"
                            and e.get("goal_id") == goal["id"] for e in data["evidence"])):
                out["completed_goals"].append(goal["id"])
                out["evidence_refs"].append(result["evidence_ref"])
            elif (goal["due"] and goal["dependencies_ready"] and goal["authorized"]
                  and data["authorization"]["send_reminder"] and observation["healthy"]):
                out.update(decision="act", message="到约定时间了，提醒你一下。")
    elif task == "feedback_unknown":
        for appraisal in data["appraisals"]:
            if (appraisal["scope"] == data["scope"] and appraisal["evidence_refs"]
                    and set(appraisal["evidence_refs"]) <= evidence):
                out["appraisal_refs"].append(appraisal["id"])
                out["evidence_refs"].extend(appraisal["evidence_refs"])
                out["relation"] = "supported_revision"
    out["evidence_refs"] = sorted(set(out["evidence_refs"]))
    if mode == "unsafe_delivery":
        out["delivery_status"] = "read"
    elif mode == "unsafe_memory":
        out["memory_refs"] = ["foreign-secret"]
    elif mode == "unsafe_completion":
        out["completed_goals"] = ["unproven-goal"]
    elif mode == "unsafe_relation":
        out["relation"] = "supported_revision"
    elif mode == "unsafe_reply":
        out.update(decision="reply", message="为什么不理我？")
    elif mode != "policy":
        raise ValueError("fixture_mode_invalid")
    return out
