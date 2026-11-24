"""预检报告 → 暂缓审核模板 → 漂移核对；不生成批准，也不修改源库。"""
from __future__ import annotations

from datetime import datetime

SCHEMA_VERSION = 2
REVIEW_FIELDS = {"source_key", "row_fingerprint", "evidence_fingerprint", "decision", "reviewer", "valid_from", "valid_to"}


def review_validity(item: dict) -> tuple[str, str]:
    if not item.get("valid_from"):
        raise ValueError("review_valid_from_required_not_capture_time")
    start = datetime.fromisoformat(str(item["valid_from"]))
    end = datetime.fromisoformat(str(item["valid_to"])) if item.get("valid_to") else None
    if start.tzinfo is not None or (end and end.tzinfo is not None):
        raise ValueError("review_validity_requires_local_time")
    if end and end <= start:
        raise ValueError("invalid_review_valid_interval")
    return start.isoformat(sep=" "), end.isoformat(sep=" ") if end else ""


def review_template(report: dict) -> dict:
    """不复制正文、不沿用抓取时间作有效时间；所有项默认 defer。"""
    return {"schema_version": SCHEMA_VERSION, "source_id": report["source_id"],
            "instructions": "所有项默认暂缓。核对原始记录和证据后，才可将本会话合格项改为 approved，填写 reviewer 和确认的有效区间。预检合格不等于批准。",
            "audit": report,
            "reviews": [{"source_key": item["source_key"], "row_fingerprint": item["row_fingerprint"],
                         "evidence_fingerprint": item.get("evidence_fingerprint", ""), "decision": "defer",
                         "reviewer": "", "valid_from": "", "valid_to": ""} for item in report["records"]]}


def check_review(report: dict, review: dict) -> dict:
    result = {"ok": True, "source_id": report["source_id"], "approved": 0, "deferred": 0,
              "issues": [], "unreviewed_source_keys": []}
    def issue(key, reason, blocking=True):
        result["issues"].append({"source_key": key, "reason": reason, "blocking": blocking})
        if blocking:
            result["ok"] = False
    if not isinstance(review, dict) or review.get("schema_version") != SCHEMA_VERSION or review.get("source_id") != report["source_id"] or not isinstance(review.get("reviews"), list):
        issue("", "invalid_review_manifest_regenerate_v2")
        return result
    records = {item["source_key"]: item for item in report["records"]}
    seen = set()
    scopes = set()
    for item in review["reviews"]:
        if not isinstance(item, dict) or set(item) - REVIEW_FIELDS:
            issue("", "invalid_review_fields")
            continue
        key = item.get("source_key")
        if not isinstance(key, str) or key in seen or key not in records:
            issue(key if isinstance(key, str) else "", "review_source_key_missing_or_duplicated")
            continue
        seen.add(key)
        record = records[key]
        if not isinstance(item.get("decision"), str):
            issue(key, "explicit_review_required")
            continue
        approved = item.get("decision") == "approved"
        if not approved and item.get("decision") not in {"defer", "reject"}:
            issue(key, "explicit_review_required")
            continue
        if item.get("row_fingerprint") != record["row_fingerprint"]:
            issue(key, "reviewed_source_changed", blocking=approved)
        if not record.get("evidence_fingerprint") or item.get("evidence_fingerprint") != record.get("evidence_fingerprint"):
            issue(key, "reviewed_evidence_changed_or_unbound", blocking=approved)
        if not approved:
            result["deferred"] += 1
            continue
        result["approved"] += 1
        if not isinstance(item.get("reviewer"), str) or not item["reviewer"].strip():
            issue(key, "explicit_review_required")
        if record["disposition"] != "eligible_for_review":
            issue(key, "reviewed_record_is_not_eligible")
        else:
            scopes.add(tuple(record["scope"][field] for field in ("platform", "account_id", "chat_id")))
        try:
            review_validity(item)
        except (ValueError, TypeError):
            issue(key, "invalid_or_missing_review_validity")
    if len(scopes) > 1:
        issue("", "one_conversation_per_import_batch")
    result["unreviewed_source_keys"] = sorted(set(records) - seen)
    return result
