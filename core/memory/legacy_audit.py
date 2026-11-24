"""旧记忆分级预检；只读输入库，不导入、不修改来源、不把公开标签当授权。"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sqlite3


def _hash(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str).encode()).hexdigest()


def _json(value, expected):
    result = json.loads(value) if isinstance(value, str) else value
    if not isinstance(result, expected):
        raise ValueError("invalid_json_shape")
    return result


def _identity(row):
    return tuple(str(row.get(key) or "") for key in ("platform", "bot_account_id", "chat_id"))


def classify(row: dict, *, table: str, source_id: str, resolve_event) -> dict:
    """eligible_for_review 仅表示来源与范围可核对，不是事实真值或导入授权。"""
    key = row.get("memory_id") if table == "memory_projections" else row.get("id")
    result = {"source_key": "legacy:" + _hash([source_id, table, key]), "table": table,
              "record_id": str(key) if key is not None else "", "row_fingerprint": _hash(row),
              "content_hash": _hash(str(row.get("content") or "")), "scope": None,
              "evidence_refs": [], "disposition": "needs_scope", "reasons": []}
    reasons = result["reasons"]
    if key is None or not str(key).strip() or not str(row.get("content") or "").strip():
        result.update(disposition="invalid", reasons=["missing_identity_or_content"])
        return result
    if table == "knowledge_docs":
        reasons.extend(["scope_not_recorded", "capture_is_not_fact_evidence"])
        return result
    if table != "memory_projections":
        raise ValueError("unsupported_legacy_table")
    try:
        meta = _json(row.get("metadata_json") or "{}", dict)
        refs = _json(row.get("source_event_ids_json") or "[]", list)
        visibility = _json(row.get("visibility_scope_json") or "[]", list)
        if any(not isinstance(label, str) for label in visibility):
            raise ValueError("invalid_visibility_label")
        if any(not isinstance(ref, str) or not ref.strip() for ref in refs):
            raise ValueError("invalid_event_reference")
    except (ValueError, TypeError):
        result.update(disposition="invalid", reasons=["invalid_provenance_json"])
        return result
    result["evidence_refs"] = list(dict.fromkeys(refs))
    result["declared_visibility"] = visibility
    result["sensitivity"] = str(row.get("sensitivity") or "normal")
    if not visibility:
        result.update(disposition="legacy_unverified", reasons=["projection_visibility_unknown"])
        return result
    if any(label not in {"public", "group_members"} for label in visibility):
        result.update(disposition="excluded", reasons=["projection_visibility_requires_separate_review"])
        return result
    if str(row.get("memory_type") or "") != "fact":
        result.update(disposition="excluded", reasons=["not_fact_projection"])
        return result
    if str(row.get("sensitivity") or "normal") != "normal":
        result.update(disposition="excluded", reasons=["sensitive_memory_requires_separate_review"])
        return result
    start, end = row.get("valid_from"), row.get("valid_to")
    try:
        parsed_start = datetime.fromisoformat(str(start)) if start else None
        parsed_end = datetime.fromisoformat(str(end)) if end else None
        if parsed_start and parsed_end and parsed_end <= parsed_start:
            raise ValueError("empty_or_reversed_interval")
    except (ValueError, TypeError):
        result.update(disposition="invalid", reasons=["invalid_valid_interval"])
        return result
    result["validity"] = {"from": str(start or ""), "to": str(end or ""), "semantics": "legacy_requires_review"}
    declared = (str(row.get("platform") or ""), str(row.get("bot_account_id") or ""), str(meta.get("chat_id") or ""))
    events = []
    evidence_state = []
    missing = []
    for reference in result["evidence_refs"]:
        event = resolve_event(reference)
        evidence_state.append((reference, _hash(event) if event is not None else None))
        if event is None:
            missing.append(reference)
        else:
            events.append(event)
    result["evidence_fingerprint"] = _hash(sorted(evidence_state))
    if any(str(event.get("event_type") or "") == "thought" or str(event.get("sensitivity") or "normal") != "normal" for event in events):
        result.update(disposition="excluded", reasons=["private_or_thought_evidence"])
        return result
    source_visibility = []
    try:
        for event in events:
            labels = _json(event.get("visibility_scope_json") or "[]", list)
            source_visibility.append({"event_id": event.get("event_id", ""), "visibility": labels})
            if any(label not in {"public", "group_members"} for label in labels):
                result.update(disposition="excluded", reasons=["private_or_thought_evidence"])
                return result
    except (ValueError, TypeError):
        result.update(disposition="legacy_unverified", reasons=["source_visibility_unverified"])
        return result
    result["source_visibility"] = source_visibility
    if "public" in visibility and any("group_members" in item["visibility"] for item in source_visibility):
        result.update(disposition="conflict", reasons=["source_visibility_conflict"])
        return result
    identities = {_identity(event) for event in events}
    if len(identities) > 1 or any(any(left and right and left != right for left, right in zip(declared, identity)) for identity in identities):
        result.update(disposition="conflict", reasons=["source_scope_conflict"])
        return result
    evidence_identity = next(iter(identities)) if identities else ("", "", "")
    identity = tuple(left or right for left, right in zip(declared, evidence_identity))
    if all(identity):
        result["scope"] = dict(zip(("platform", "account_id", "chat_id"), identity))
    if not all(identity):
        reasons.append("scope_not_resolved")
        if "public" in visibility:
            reasons.append("public_label_does_not_resolve_scope")
        if missing:
            reasons.append("evidence_missing")
        return result
    if not refs or missing or any(not all(_identity(event)) for event in events) or any(not item["visibility"] for item in source_visibility):
        result["disposition"] = "legacy_unverified"
        reasons.append("evidence_unresolved" if missing else "evidence_not_verifiable")
        return result
    if any(event.get("event_type") != "message" for event in events):
        result.update(disposition="legacy_unverified", reasons=["unsupported_evidence_type"])
        return result
    # 摘要的引用存在不代表原始依据尚可核对；禁止记忆用自身写入循环作证。
    if any(not isinstance(event.get("content"), str) or not event["content"].strip() for event in events):
        result.update(disposition="legacy_unverified", reasons=["original_message_content_unavailable"])
        return result
    try:
        evidence_meta = [_json(event.get("metadata_json") or "{}", dict) for event in events]
    except (ValueError, TypeError):
        result.update(disposition="legacy_unverified", reasons=["original_message_metadata_unverifiable"])
        return result
    if any(meta.get("is_self") or (event.get("actor_account_id") and event["actor_account_id"] == event.get("bot_account_id")) for event, meta in zip(events, evidence_meta)):
        result.update(disposition="legacy_unverified", reasons=["self_message_is_not_external_fact_evidence"])
        return result
    result.update(disposition="eligible_for_review", reasons=["provenance_and_scope_resolved_not_truth_verified"])
    return result


@dataclass(frozen=True)
class LegacySnapshot:
    report: dict
    rows: dict = field(repr=False)


def audit_database(path: str | Path, *, source_id: str) -> dict:
    return read_snapshot(path, source_id=source_id).report


def read_snapshot(path: str | Path, *, source_id: str) -> LegacySnapshot:
    """同一只读事务内同时保留预检与原行；正文仅供服务构造导入，不进报告。"""
    source_id = source_id.strip()
    if not source_id:
        raise ValueError("source_id_required")
    path = Path(path).resolve(strict=True)
    conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        evidence_columns = {row[1] for row in conn.execute("PRAGMA table_info(terminal_events)")} if "terminal_events" in tables else set()
        evidence_readable = {"event_id", "platform", "bot_account_id", "chat_id", "event_type", "sensitivity", "visibility_scope_json"} <= evidence_columns
        def resolve(reference):
            if not evidence_readable:
                return None
            found = conn.execute("SELECT * FROM terminal_events WHERE event_id=?", (reference,)).fetchmany(2)
            return dict(found[0]) if len(found) == 1 else None
        records = []
        raw_rows = {}
        table_status = {}
        omitted = {}
        for table in ("memory_projections", "knowledge_docs"):
            if table not in tables:
                table_status[table] = "not_present"
                continue
            columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
            identity = "memory_id" if table == "memory_projections" else "id"
            if not {identity, "content"} <= columns:
                table_status[table] = "unsupported_schema"
                omitted[table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                continue
            table_status[table] = "scanned"
            for row in conn.execute(f"SELECT * FROM {table} ORDER BY {identity}"):
                record = classify(dict(row), table=table, source_id=source_id, resolve_event=resolve)
                if record["source_key"] in raw_rows:
                    raise ValueError("ambiguous_source_record_identity")
                records.append(record)
                raw_rows[record["source_key"]] = dict(row)
        counts = dict(sorted(Counter(record["disposition"] for record in records).items()))
        report = {"schema_version": 1, "source_id": source_id, "read_only": True,
                "evidence_status": "readable" if evidence_readable else "missing_or_unsupported",
                "tables": table_status, "omitted_rows": omitted, "total": len(records), "counts": counts, "records": records,
                "notice": "只读预检；eligible_for_review 不是自动导入授权。非事实条目仅排除本轮 Belief 导入，原记录保留。不导入、不推断公开权限，不验证语义真假或自动识别自由文本矛盾；conflict 仅指来源坐标或可见范围冲突。未知归属和未核实记录保留待核对。"}
        return LegacySnapshot(report, raw_rows)
    finally:
        conn.close()
