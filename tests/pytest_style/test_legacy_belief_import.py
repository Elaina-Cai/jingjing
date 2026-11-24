from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
from uuid import uuid4

import pytest

from core.context import cognitive_commit as cc, situation_store as store
from core.memory import legacy_import as importer
from core.memory.legacy_audit import audit_database
from core.memory.beliefs import BeliefMemoryProvider
from core.terminal.principal import PrincipalContext, ChatType, RelationLevel


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(importer, "SANDBOX_ROOTS", (tmp_path,))
    monkeypatch.setenv("YINYUE_WORLD_MODEL_DIR", str(tmp_path / "world"))
    monkeypatch.setenv("YINYUE_COGNITIVE_DB", str(tmp_path / "target.db"))
    store.reset_world_models()
    yield
    store.reset_world_models()


def source_database(path, *, second=False, scope="same"):
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE memory_projections(memory_id TEXT PRIMARY KEY, memory_type TEXT, content TEXT, platform TEXT, bot_account_id TEXT, metadata_json TEXT, source_event_ids_json TEXT, visibility_scope_json TEXT, sensitivity TEXT, valid_from TEXT)")
        conn.execute("CREATE TABLE terminal_events(event_id TEXT PRIMARY KEY,platform TEXT,bot_account_id TEXT,chat_id TEXT,event_type TEXT,content TEXT,sensitivity TEXT,visibility_scope_json TEXT)")
        for index in range(2 if second else 1):
            conn.execute("INSERT INTO memory_projections VALUES(?,?,?,?,?,?,?,?,?,?)", (f"old-{index}", "fact", f"原始地址记录-{index}", "wechat", "a", json.dumps({"chat_id": scope}), json.dumps([f"event-{index}"]), '["public"]', "normal", "2026-03-01"))
            conn.execute("INSERT INTO terminal_events VALUES(?,?,?,?,?,?,?,?)", (f"event-{index}", "wechat", "a", scope, "message", "原始聊天证据", "normal", '["public"]'))
        conn.commit()
    finally:
        conn.close()


def manifest(path):
    report = audit_database(path, source_id="meta")
    return {"schema_version": 2, "source_id": "meta", "reviews": [{"source_key": row["source_key"], "row_fingerprint": row["row_fingerprint"], "evidence_fingerprint": row.get("evidence_fingerprint", ""), "decision": "approved", "reviewer": "test-reviewer", "valid_from": "2026-01-01"} for row in report["records"]]}


def apply(source, target, review=None, dry=False):
    return importer.import_reviewed(source, source_id="meta", review=review or manifest(source), target=target, dry_run=dry)


def events(path):
    conn = sqlite3.connect(path)
    try:
        return list(conn.execute("SELECT event_id,payload FROM cognitive_events ORDER BY seq"))
    finally:
        conn.close()


def test_dry_uses_one_read_snapshot_and_does_not_create_target(tmp_path):
    source, target = tmp_path / "source.db", tmp_path / "target.db"
    source_database(source)
    before = source.read_bytes()
    result = apply(source, target, dry=True)
    assert result["selected"] == 1 and result["committed"] == 0
    assert not target.exists() and source.read_bytes() == before


def test_import_replay_reload_and_real_provider_are_scoped(tmp_path):
    source, target = tmp_path / "source.db", tmp_path / "target.db"
    source_database(source)
    before = source.read_bytes()
    assert apply(source, target)["committed"] == 1
    first = events(target)
    assert apply(source, target)["duplicates"] == 1
    assert events(target) == first
    assert source.read_bytes() == before
    store.reset_world_models()
    principal = PrincipalContext("wechat", "a", "same", ChatType.PRIVATE, "person-a", "A", RelationLevel.UNKNOWN)
    hits = BeliefMemoryProvider().recall(principal)
    assert len(hits) == 1 and "原始地址记录-0" in hits[0].content
    assert hits[0].source_event_ids == ("event-0",)
    assert hits[0].metadata["valid_from"] == "2026-01-01 00:00:00"
    other = PrincipalContext("wechat", "b", "same", ChatType.PRIVATE, "person-b", "B", RelationLevel.UNKNOWN)
    assert BeliefMemoryProvider().recall(other) == []


@pytest.mark.parametrize("field,value", [("row_fingerprint", "wrong"), ("decision", "eligible_for_review"), ("reviewer", ""), ("valid_from", ""), ("valid_from", "2026-01-01T00:00:00Z")])
def test_review_errors_block_whole_batch_before_target_creation(tmp_path, field, value):
    source, target = tmp_path / "source.db", tmp_path / "target.db"
    source_database(source, second=True)
    review = manifest(source)
    review["reviews"][1][field] = value
    with pytest.raises(ValueError):
        apply(source, target, review)
    assert not target.exists()


def test_source_change_after_review_is_not_silently_imported(tmp_path):
    source, target = tmp_path / "source.db", tmp_path / "target.db"
    source_database(source)
    review = manifest(source)
    conn = sqlite3.connect(source)
    conn.execute("UPDATE memory_projections SET content='changed'")
    conn.commit()
    conn.close()
    with pytest.raises(ValueError, match="reviewed_source_changed"):
        apply(source, target, review)
    assert not target.exists()


def test_refreshed_review_cannot_overwrite_existing_import(tmp_path):
    source, target = tmp_path / "source.db", tmp_path / "target.db"
    source_database(source)
    apply(source, target)
    first = events(target)
    conn = sqlite3.connect(source)
    conn.execute("UPDATE memory_projections SET content='changed'")
    conn.commit()
    conn.close()
    with pytest.raises(ValueError, match="legacy_import_conflict"):
        apply(source, target)
    assert events(target) == first


def test_production_or_source_alias_targets_are_refused(tmp_path):
    source = tmp_path / "source.db"
    source_database(source)
    review = manifest(source)
    with pytest.raises(ValueError, match="source_and_target"):
        apply(source, source, review)
    outside = tmp_path.parent / "not-sandbox.db"
    with pytest.raises(ValueError, match="isolated_sandbox"):
        apply(source, outside, review)
    assert not outside.exists()


def test_unmarked_existing_database_is_not_modified(tmp_path):
    source, target = tmp_path / "source.db", tmp_path / "target.db"
    source_database(source)
    conn = sqlite3.connect(target)
    conn.execute("CREATE TABLE precious(value TEXT)")
    conn.commit()
    conn.close()
    before = target.read_bytes()
    with pytest.raises(ValueError, match="not_import_sandbox"):
        apply(source, target)
    assert target.read_bytes() == before


def test_scope_cannot_be_overridden_by_review_file(tmp_path):
    source, target = tmp_path / "source.db", tmp_path / "target.db"
    source_database(source)
    review = manifest(source)
    review["reviews"][0]["scope"] = {"platform": "qq", "account_id": "b", "chat_id": "other"}
    with pytest.raises(ValueError, match="invalid_review_fields"):
        apply(source, target, review)
    assert not target.exists()


def test_target_domain_prevents_reuse_for_a_different_scope(tmp_path):
    source, target = tmp_path / "source.db", tmp_path / "target.db"
    other = tmp_path / "other.db"
    source_database(source)
    source_database(other, scope="other")
    apply(source, target)
    first = events(target)
    with pytest.raises(ValueError, match="target_source_or_scope_conflict"):
        apply(other, target)
    assert events(target) == first


def test_failed_sql_batch_rolls_back_every_record_and_retry_succeeds(tmp_path):
    source, target = tmp_path / "source.db", tmp_path / "target.db"
    source_database(source, second=True)
    conn = cc.connect(str(target))
    conn.execute("CREATE TABLE legacy_import_sandbox(singleton INTEGER PRIMARY KEY,source_id TEXT,scope_key TEXT)")
    conn.execute("INSERT INTO legacy_import_sandbox VALUES(1,'meta',?)", (cc._scope_key(cc.Scope("wechat", "a", "same")),))
    conn.execute("CREATE TRIGGER reject_second BEFORE INSERT ON cognitive_events WHEN (SELECT COUNT(*) FROM cognitive_events)>0 BEGIN SELECT RAISE(ABORT,'injected failure'); END")
    conn.close()
    with pytest.raises(sqlite3.IntegrityError):
        apply(source, target)
    assert events(target) == []
    conn = sqlite3.connect(target)
    conn.execute("DROP TRIGGER reject_second")
    conn.commit()
    conn.close()
    assert apply(source, target)["committed"] == 2


def test_lost_response_recovers_as_idempotent_retry(tmp_path, monkeypatch):
    source, target = tmp_path / "source.db", tmp_path / "target.db"
    source_database(source)
    original = cc.commit_updates
    with monkeypatch.context() as patch:
        def lost(*args, **kwargs):
            original(*args, **kwargs)
            raise OSError("lost response")
        patch.setattr(cc, "commit_updates", lost)
        with pytest.raises(OSError):
            apply(source, target)
    assert apply(source, target)["duplicates"] == 1
    assert len(events(target)) == 1


def test_deferred_records_stay_only_in_source(tmp_path):
    source, target = tmp_path / "source.db", tmp_path / "target.db"
    source_database(source, second=True)
    review = manifest(source)
    review["reviews"][1]["decision"] = "defer"
    result = apply(source, target, review)
    assert result["committed"] == 1 and result["retained_in_source"] == 1
    conn = sqlite3.connect(source)
    assert conn.execute("SELECT COUNT(*) FROM memory_projections").fetchone()[0] == 2
    conn.close()


def test_unsupported_legacy_rows_are_retained_and_reported(tmp_path):
    source, target = tmp_path / "old-schema.db", tmp_path / "target.db"
    conn = sqlite3.connect(source)
    conn.execute("CREATE TABLE knowledge_docs(title TEXT)")
    conn.execute("INSERT INTO knowledge_docs VALUES('unmapped record')")
    conn.commit()
    conn.close()
    result = apply(source, target, {"schema_version": 2, "source_id": "meta", "reviews": []})
    assert result["selected"] == 0 and result["retained_in_source"] == 1
    assert result["omitted_rows"] == {"knowledge_docs": 1}
    assert not target.exists()


def test_multiple_conversations_cannot_share_a_batch(tmp_path):
    source, target = tmp_path / "source.db", tmp_path / "target.db"
    source_database(source, second=True)
    conn = sqlite3.connect(source)
    conn.execute("UPDATE memory_projections SET metadata_json=? WHERE memory_id='old-1'", (json.dumps({"chat_id": "other"}),))
    conn.execute("UPDATE terminal_events SET chat_id='other' WHERE event_id='event-1'")
    conn.commit()
    conn.close()
    with pytest.raises(ValueError, match="one_conversation_per_import_batch"):
        apply(source, target)
    assert not target.exists()


@pytest.mark.parametrize("field,value", [("source_event_ids_json", "[]"), ("visibility_scope_json", '["owner_private"]')])
def test_approval_cannot_override_missing_evidence_or_private_scope(tmp_path, field, value):
    source, target = tmp_path / "source.db", tmp_path / "target.db"
    source_database(source)
    conn = sqlite3.connect(source)
    conn.execute(f"UPDATE memory_projections SET {field}=?", (value,))
    conn.commit()
    conn.close()
    with pytest.raises(ValueError, match="not_eligible"):
        apply(source, target)
    assert not target.exists()


def test_correction_after_import_is_not_overwritten_by_import_replay(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from core.memory import belief_commit
    from core.terminal import shadow
    from core.terminal.event_model import Event
    source, target = tmp_path / "source.db", tmp_path / "target.db"
    source_database(source)
    apply(source, target)
    scope = cc.Scope("wechat", "a", "same")
    offer = belief_commit.offers(scope)["b1"]
    proof = Event("new-correction", datetime(2026, 2, 1), platform="wechat", bot_account_id="a", chat_id="same", actor_person_id="person-a", content="之前记错了，地址是上海")
    monkeypatch.setattr(shadow, "get_default_store", lambda: SimpleNamespace(get=lambda ref: proof if ref == proof.event_id else None))
    result = belief_commit.submit(offer, scope=scope, value="上海", change_kind="correction", evidence_ref=proof.event_id, visible=frozenset({proof.event_id}))
    assert result["result"] == "committed"
    assert apply(source, target)["duplicates"] == 1
    assert len(events(target)) == 2
    store.reset_world_models()
    principal = PrincipalContext("wechat", "a", "same", ChatType.PRIVATE, "person-a", "A", RelationLevel.UNKNOWN)
    hits = BeliefMemoryProvider().recall(principal)
    assert len(hits) == 1 and "上海" in hits[0].content
    assert all("原始地址记录" not in hit.content for hit in hits)


def test_cli_defaults_to_dry_and_apply_is_still_sandbox_only(tmp_path):
    source = tmp_path / "source.db"
    source_database(source)
    review = tmp_path / "review.json"
    review.write_text(json.dumps(manifest(source)), encoding="utf-8")
    root = Path(__file__).resolve().parents[2]
    target = root / ".test_tmp" / ("legacy-import-cli-" + uuid4().hex) / "target.db"
    command = [sys.executable, "-I", str(root / "scripts/import_legacy_beliefs.py"), "--source", str(source), "--source-id", "meta", "--review", str(review), "--target", str(target)]
    preview = subprocess.run(command, capture_output=True, encoding="utf-8", timeout=20)
    assert preview.returncode == 0, preview.stderr
    assert json.loads(preview.stdout)["dry_run"] is True and not target.exists()
    result = subprocess.run([*command, "--apply"], capture_output=True, encoding="utf-8", timeout=20)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["committed"] == 1
    replay = subprocess.run([*command, "--apply"], capture_output=True, encoding="utf-8", timeout=20)
    assert json.loads(replay.stdout)["duplicates"] == 1
