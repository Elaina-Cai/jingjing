"""旧库与隔离导入日志的只读验收；来源核对不等于已完成读取权切换。"""
from __future__ import annotations

from collections import Counter
from datetime import datetime
import json
from pathlib import Path
import sqlite3

from core.context import cognitive_commit as cc
from core.context.situation import Belief, WorldEvent
from core.memory.beliefs import _date, resolve_versions
from core.memory.belief_commit import logical_subject
from core.memory.legacy_review import review_validity
from core.memory.legacy_audit import read_snapshot
from core.memory.legacy_import import _target, import_domain


def verify_import(source, *, source_id: str, target, at: str = "", production: bool = False) -> dict:
    stamp = _date(at) if at else datetime.now()
    source = Path(source).resolve(strict=True)
    target = _target(target, source, production=production).resolve(strict=True)
    snapshot = read_snapshot(source, source_id=source_id)
    source_id = snapshot.report["source_id"]
    conn = sqlite3.connect(target.as_uri() + "?mode=ro", uri=True)
    try:
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
        domain = import_domain(conn)
        if domain is None or domain[0] != source_id:
            raise ValueError("target_source_or_scope_conflict")
        identity = json.loads(domain[1])
        if not isinstance(identity, list) or len(identity) != 3 or not all(isinstance(x, str) and x for x in identity):
            raise ValueError("invalid_target_scope")
        rows = conn.execute("SELECT source,operation,subject_key,payload FROM cognitive_events WHERE " + cc._SCOPE_SQL + "=? ORDER BY seq", (domain[1],)).fetchall()
    finally:
        conn.close()
    events, imports = [], {}
    for origin, operation, subject, raw in rows:
        if (origin, operation) not in {("legacy_import", "believe"), ("belief_tool", "revise")}:
            continue
        body = json.loads(raw)
        fact = body.get("world_fact") if isinstance(body, dict) else None
        if not isinstance(fact, dict) or not isinstance(fact.get("key"), str) or not fact["key"] or fact.get("scope") != ":".join(identity):
            raise ValueError("invalid_import_journal_fact")
        events.append(WorldEvent("fact_asserted", str(body.get("at") or ""), fact))
        if origin == "legacy_import":
            legacy = body.get("legacy")
            if not isinstance(legacy, dict) or legacy.get("source_id") != source_id or legacy.get("source_key") != fact.get("key"):
                raise ValueError("invalid_import_journal_binding")
            key = legacy["source_key"]
            if key in imports:
                raise ValueError("duplicated_import_journal_key")
            if fact.get("subject_key") != key or subject != logical_subject(Belief(key=key, subject_key=key)):
                raise ValueError("invalid_import_journal_subject")
            review_validity(fact)
            imports[key] = (legacy, fact)
    scope = dict(zip(("platform", "account_id", "chat_id"), identity))
    active = resolve_versions(events, scope=scope, at=stamp)
    active_keys = {belief.key for belief in active}
    active_subjects = {belief.subject_key or belief.key for belief in active}
    current = {row["source_key"]: row for row in snapshot.report["records"]}
    records = []
    for key, (legacy, fact) in sorted(imports.items()):
        record = current.get(key)
        state = "source_record_missing"
        if record is not None:
            if record["scope"] != scope:
                state = "source_scope_changed"
            elif record["row_fingerprint"] != legacy.get("row_fingerprint"):
                state = "source_row_changed"
            elif not legacy.get("evidence_fingerprint") or record.get("evidence_fingerprint") != legacy["evidence_fingerprint"]:
                state = "source_evidence_changed_or_unbound"
            else:
                if fact.get("value") != str(snapshot.rows[key]["content"]) or fact.get("evidence_refs") != record["evidence_refs"]:
                    raise ValueError("invalid_import_journal_content_or_evidence")
                state = "unchanged"
        effective = "original_current" if key in active_keys else "revised_current" if key in active_subjects else "inactive_at_query_time"
        records.append({"source_key": key, "source_state": state, "effective_state": effective,
                        "legacy_reader_overlap_possible": record is not None})
    states = Counter(row["source_state"] for row in records)
    from core.memory.legacy_write_freeze import verify_freeze_owner
    frozen = verify_freeze_owner(source, source_id=source_id, scope_key=domain[1], target=target)
    return {"schema_version": 1, "read_only": True, "source_id": source_id, "scope": scope,
            "at": stamp.isoformat(sep=" "), "integrity_ok": bool(records) and all(row["source_state"] == "unchanged" for row in records),
            "legacy_projection_writes_frozen": frozen,
            "cutover_ready": False, "pending": (["no_committed_imports"] if not records else []) + ["legacy_reader_source_binding_and_exclusive_ownership_not_proven"],
            "counts": {"imported": len(records), "source_states": dict(sorted(states.items())),
                       "effective_states": dict(sorted(Counter(row["effective_state"] for row in records).items())),
                       "unimported": len(set(current) - set(imports)),
                       "possible_legacy_overlaps": sum(row["legacy_reader_overlap_possible"] for row in records)},
            "source_tables": snapshot.report["tables"], "source_omitted_rows": snapshot.report["omitted_rows"],
            "records": records,
            "notice": "仅验收显式来源与隔离日志，不扫描生产库，不修改数据。源行保留可能被旧读取者再次召回；完整来源绑定与唯一读取权尚未证明，不能据此启用生产切换。未导入条目不代表错误或已获批准。"}


def verify_cutover(source, *, source_id: str, target, binding, repository, at: str = "",
                   production: bool = False) -> dict:
    """把导入完整性、停写权和读取权绑定合并为可复核的切换门槛。

    verify_import 的 cutover_ready 永远保守为 False；只有本函数在同一份
    只读快照上重新核对 binding.snapshot(repository) 成功，才可置 True。
    调用方不能只传一个布尔值来打开切换。
    """
    report = verify_import(source, source_id=source_id, target=target, at=at, production=production)
    pending: list[str] = []
    if not report["integrity_ok"]:
        pending.append("import_integrity_not_proven")
    if not report["legacy_projection_writes_frozen"]:
        pending.append("legacy_projection_write_freeze_not_proven")
    detail: dict[str, object] = {}
    try:
        source_path = Path(source).resolve(strict=True)
        target_path = Path(target).resolve(strict=True)
        if Path(binding.source).resolve() != source_path:
            raise ValueError("cutover_binding_source_mismatch")
        if Path(binding.target).resolve() != target_path:
            raise ValueError("cutover_binding_target_mismatch")
        if binding.source_id != report["source_id"]:
            raise ValueError("cutover_binding_source_id_mismatch")
        scope = report["scope"]
        scope_key = cc._scope_key(cc.Scope(scope["platform"], scope["account_id"], scope["chat_id"]))
        if binding.scope_key != scope_key:
            raise ValueError("cutover_binding_scope_mismatch")
        keys = binding.snapshot(repository)
        imported = {item["source_key"] for item in report["records"]}
        if not imported or not imported <= keys:
            raise ValueError("cutover_binding_incomplete")
        detail = {"source": str(source_path), "target": str(target_path),
                  "scope_key": scope_key, "keys": len(keys), "imported": len(imported)}
    except (OSError, TypeError, ValueError) as exc:
        pending.append("legacy_reader_binding_not_proven")
        detail = {"error": str(exc)}
    result = dict(report)
    result["cutover_ready"] = not pending
    result["pending"] = pending
    result["read_binding"] = detail
    return result
