import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys

import pytest

from core.memory.legacy_audit import audit_database, classify


def row(**kwargs):
    return {"memory_id": "old:1", "memory_type": "fact", "content": "PRIVATE OLD CONTENT", "platform": "wechat", "bot_account_id": "a", "metadata_json": '{"chat_id":"same"}', "source_event_ids_json": '["event"]', "visibility_scope_json": '["public"]', "sensitivity": "normal", "valid_from": "2026-01-01", **kwargs}


def event(**kwargs):
    return {"event_id": "event", "platform": "wechat", "bot_account_id": "a", "chat_id": "same", "event_type": "message", "content": "PRIVATE ORIGINAL SOURCE", "sensitivity": "normal", "visibility_scope_json": '["public"]', **kwargs}


def grade(record=None, proof=None):
    return classify(record or row(), table="memory_projections", source_id="meta", resolve_event=lambda ref: event() if proof is None else proof)


def database(path):
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE memory_projections(memory_id TEXT PRIMARY KEY,memory_type TEXT,content TEXT,platform TEXT,bot_account_id TEXT,metadata_json TEXT,source_event_ids_json TEXT,visibility_scope_json TEXT,sensitivity TEXT,valid_from TEXT)")
        item = row()
        conn.execute("INSERT INTO memory_projections VALUES(?,?,?,?,?,?,?,?,?,?)", tuple(item[key] for key in ("memory_id", "memory_type", "content", "platform", "bot_account_id", "metadata_json", "source_event_ids_json", "visibility_scope_json", "sensitivity", "valid_from")))
        conn.execute("CREATE TABLE terminal_events(event_id TEXT PRIMARY KEY,platform TEXT,bot_account_id TEXT,chat_id TEXT,event_type TEXT,content TEXT,sensitivity TEXT,visibility_scope_json TEXT)")
        proof = event()
        conn.execute("INSERT INTO terminal_events VALUES(?,?,?,?,?,?,?,?)", tuple(proof.values()))
        conn.execute("CREATE TABLE knowledge_docs(id INTEGER PRIMARY KEY, content TEXT,source_url TEXT)")
        conn.execute("INSERT INTO knowledge_docs VALUES(1,'DO NOT PRINT DOCUMENT CONTENT','https://example.invalid')")
        conn.commit()
    finally:
        conn.close()


def test_original_evidence_is_only_eligible_for_review_not_truth_verification():
    result = grade()
    assert result["disposition"] == "eligible_for_review"
    assert result["scope"] == {"platform": "wechat", "account_id": "a", "chat_id": "same"}
    assert "not_truth_verified" in result["reasons"][0]
    assert "PRIVATE OLD CONTENT" not in json.dumps(result)


def test_empty_scope_is_not_made_public_by_public_label():
    result = grade(row(platform="", bot_account_id="", metadata_json="{}", source_event_ids_json="[]"))
    assert result["disposition"] == "needs_scope"
    assert result["scope"] is None
    assert "public_label_does_not_resolve_scope" in result["reasons"]


def test_complete_original_source_can_resolve_missing_coordinates():
    result = grade(row(platform="", bot_account_id="", metadata_json="{}"))
    assert result["disposition"] == "eligible_for_review"
    assert result["scope"]["chat_id"] == "same"


def test_declared_scope_without_source_is_unverified():
    result = grade(row(source_event_ids_json='["missing"]'), proof={})
    assert result["disposition"] == "legacy_unverified"


@pytest.mark.parametrize("field,value", [("chat_id", "other"), ("bot_account_id", "b"), ("platform", "qq")])
def test_source_coordinate_conflict_is_quarantined(field, value):
    result = grade(proof=event(**{field: value}))
    assert result["disposition"] == "conflict"


def test_sources_from_two_conversations_are_not_merged():
    result = classify(row(source_event_ids_json='["one","two"]'), table="memory_projections", source_id="meta", resolve_event=lambda ref: event(chat_id="same" if ref == "one" else "other"))
    assert result["disposition"] == "conflict"


@pytest.mark.parametrize("proof", [event(event_type="thought"), event(sensitivity="owner_chain_of_thought")])
def test_thoughts_and_sensitive_sources_are_never_fact_candidates(proof):
    assert grade(proof=proof)["disposition"] == "excluded"


@pytest.mark.parametrize("label", ["owner_private", "owner_chain_of_thought", "self"])
def test_private_visibility_is_not_downgraded_by_projection_public_label(label):
    result = grade(proof=event(visibility_scope_json=json.dumps([label])))
    assert result["disposition"] == "excluded"


def test_group_source_cannot_be_promoted_by_projection_public_label():
    result = grade(proof=event(visibility_scope_json='["group_members"]'))
    assert result["disposition"] == "conflict"
    assert "source_visibility_conflict" in result["reasons"]


def test_missing_source_visibility_requires_review():
    result = grade(proof=event(visibility_scope_json="[]"))
    assert result["disposition"] == "legacy_unverified"


@pytest.mark.parametrize("kind", ["self", "relationship", "episode", "diary", "warmth"])
def test_non_fact_records_are_not_converted_by_content(kind):
    assert grade(row(memory_type=kind))["disposition"] == "excluded"


@pytest.mark.parametrize("kwargs", [{"metadata_json": "bad"}, {"source_event_ids_json": '{}'}, {"source_event_ids_json": '[123]'}, {"valid_from": "bad"}, {"valid_to": "2025-12-01"}])
def test_bad_provenance_and_intervals_are_reported_as_invalid(kwargs):
    assert grade(row(**kwargs))["disposition"] == "invalid"


def test_knowledge_url_and_quality_do_not_prove_public_fact():
    result = classify({"id": 7, "content": "private diary", "source_url": "https://example.invalid", "quality_score": 1.0}, table="knowledge_docs", source_id="meta", resolve_event=lambda ref: event())
    assert result["disposition"] == "needs_scope"
    assert "capture_is_not_fact_evidence" in result["reasons"]


def test_whole_database_audit_is_read_only_and_report_has_no_body(tmp_path):
    path = tmp_path / "copy.db"
    database(path)
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    report = audit_database(path, source_id="meta")
    assert report["total"] == sum(report["counts"].values()) == 2
    assert report["counts"] == {"eligible_for_review": 1, "needs_scope": 1}
    assert report["tables"] == {"memory_projections": "scanned", "knowledge_docs": "scanned"}
    assert "PRIVATE OLD CONTENT" not in json.dumps(report)
    assert "DO NOT PRINT DOCUMENT CONTENT" not in json.dumps(report)
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


def test_same_logical_source_is_stable_across_copies_and_repeated_scans(tmp_path):
    path = tmp_path / "one.db"
    database(path)
    copy = tmp_path / "two.db"
    shutil.copyfile(path, copy)
    first = audit_database(path, source_id="meta")
    assert first == audit_database(copy, source_id="meta")
    assert first == audit_database(path, source_id="meta")
    assert first["records"][0]["source_key"] != audit_database(path, source_id="other")["records"][0]["source_key"]


def test_missing_source_is_not_created(tmp_path):
    path = tmp_path / "absent.db"
    with pytest.raises(FileNotFoundError):
        audit_database(path, source_id="meta")
    assert not path.exists()


def test_empty_and_unsupported_schemas_are_explicit(tmp_path):
    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE memory_projections(old_content TEXT)")
    conn.execute("INSERT INTO memory_projections VALUES('old')")
    conn.commit()
    conn.close()
    report = audit_database(path, source_id="meta")
    assert report["total"] == 0
    assert report["omitted_rows"] == {"memory_projections": 1}
    assert report["tables"]["memory_projections"] == "unsupported_schema"
    assert report["tables"]["knowledge_docs"] == "not_present"


def test_old_schema_duplicate_event_id_is_not_resolved_by_first_row(tmp_path):
    path = tmp_path / "legacy.db"
    database(path)
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE old_events AS SELECT * FROM terminal_events")
    conn.execute("DROP TABLE terminal_events")
    conn.execute("ALTER TABLE old_events RENAME TO terminal_events")
    conn.execute("INSERT INTO terminal_events SELECT event_id,platform,bot_account_id,'another-chat',event_type,content,sensitivity,visibility_scope_json FROM terminal_events")
    conn.commit()
    conn.close()
    report = audit_database(path, source_id="meta")
    record = next(row for row in report["records"] if row["table"] == "memory_projections")
    assert record["disposition"] == "legacy_unverified"
    assert "evidence_unresolved" in record["reasons"]


def test_command_line_is_json_only_and_has_no_apply_mode(tmp_path):
    path = tmp_path / "copy.db"
    database(path)
    root = Path(__file__).resolve().parents[2]
    command = [sys.executable, "-I", str(root / "scripts/audit_legacy_memory.py"), "--db", str(path), "--source-id", "meta"]
    result = subprocess.run(command, capture_output=True, encoding="utf-8", timeout=20)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["total"] == 2
    denied = subprocess.run([*command, "--apply"], capture_output=True, encoding="utf-8", timeout=20)
    assert denied.returncode != 0
