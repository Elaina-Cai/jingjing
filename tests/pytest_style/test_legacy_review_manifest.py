from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from core.memory import legacy_import as importer
from core.memory.legacy_audit import audit_database
from core.memory.legacy_review import check_review, review_template
from test_legacy_belief_import import source_database


@pytest.fixture()
def source(tmp_path, monkeypatch):
    monkeypatch.setattr(importer, "SANDBOX_ROOTS", (tmp_path,))
    path = tmp_path / "copy.db"
    source_database(path)
    conn = sqlite3.connect(path)
    conn.execute("UPDATE terminal_events SET content='PRIVATE ORIGINAL EVIDENCE'")
    conn.commit()
    conn.close()
    return path


def report(source):
    return audit_database(source, source_id="meta")


def approved(source):
    template = review_template(report(source))
    template["reviews"][0].update(decision="approved", reviewer="reviewer-a", valid_from="2026-01-01")
    return template


def apply(source, target, review, dry=False):
    return importer.import_reviewed(source, source_id="meta", review=review, target=target, dry_run=dry)


def test_template_is_redacted_stable_and_never_approves(source):
    before = source.read_bytes()
    first = review_template(report(source))
    assert first == review_template(report(source))
    assert first["schema_version"] == 2
    assert all(item["decision"] == "defer" and item["reviewer"] == "" and item["valid_from"] == "" for item in first["reviews"])
    assert "PRIVATE ORIGINAL EVIDENCE" not in json.dumps(first)
    assert "原始地址记录" not in json.dumps(first, ensure_ascii=False)
    assert source.read_bytes() == before


def test_unedited_template_is_checkable_but_does_not_import(source, tmp_path):
    template = review_template(report(source))
    checked = check_review(report(source), template)
    assert checked["ok"] and checked["approved"] == 0
    target = tmp_path / "target.db"
    assert apply(source, target, template)["selected"] == 0
    assert not target.exists()


def test_approved_template_runs_dry_and_then_imports_idempotently(source, tmp_path):
    review = approved(source)
    target = tmp_path / "target.db"
    assert check_review(report(source), review)["ok"]
    assert apply(source, target, review, dry=True)["selected"] == 1
    assert not target.exists()
    assert apply(source, target, review)["committed"] == 1
    assert apply(source, target, review)["duplicates"] == 1


def test_evidence_edit_invalidates_review_even_when_memory_row_is_unchanged(source, tmp_path):
    review = approved(source)
    previous_row = report(source)["records"][0]["row_fingerprint"]
    conn = sqlite3.connect(source)
    conn.execute("UPDATE terminal_events SET content='CHANGED PROOF'")
    conn.commit()
    conn.close()
    current = report(source)
    assert current["records"][0]["row_fingerprint"] == previous_row
    checked = check_review(current, review)
    assert not checked["ok"]
    assert any(issue["reason"] == "reviewed_evidence_changed_or_unbound" for issue in checked["issues"])
    target = tmp_path / "target.db"
    with pytest.raises(ValueError, match="reviewed_evidence_changed"):
        apply(source, target, review)
    assert not target.exists()


def test_new_approval_after_evidence_change_cannot_overwrite_previous_import(source, tmp_path):
    target = tmp_path / "target.db"
    apply(source, target, approved(source))
    conn = sqlite3.connect(source)
    conn.execute("UPDATE terminal_events SET content='CHANGED PROOF'")
    conn.commit()
    conn.close()
    with pytest.raises(ValueError, match="legacy_import_conflict"):
        apply(source, target, approved(source))


def test_row_edit_invalidates_approval(source):
    review = approved(source)
    conn = sqlite3.connect(source)
    conn.execute("UPDATE memory_projections SET content='changed memory'")
    conn.commit()
    conn.close()
    assert any(issue["reason"] == "reviewed_source_changed" for issue in check_review(report(source), review)["issues"])


def test_deferred_drift_is_reported_without_silently_approving(source):
    review = review_template(report(source))
    conn = sqlite3.connect(source)
    conn.execute("UPDATE terminal_events SET content='CHANGED PROOF'")
    conn.commit()
    conn.close()
    checked = check_review(report(source), review)
    assert checked["ok"] and checked["approved"] == 0
    assert checked["issues"] and not any(issue["blocking"] for issue in checked["issues"])


@pytest.mark.parametrize("change", ["v1", "source", "duplicate", "scope", "bad_decision", "missing_evidence_fingerprint"])
def test_malformed_or_unbound_review_cannot_import(source, tmp_path, change):
    review = approved(source)
    if change == "v1":
        review["schema_version"] = 1
    elif change == "source":
        review["source_id"] = "another-source"
    elif change == "duplicate":
        review["reviews"].append(deepcopy(review["reviews"][0]))
    elif change == "scope":
        review["reviews"][0]["scope"] = {"chat_id": "another"}
    elif change == "bad_decision":
        review["reviews"][0]["decision"] = ["approved"]
    else:
        review["reviews"][0].pop("evidence_fingerprint")
    assert not check_review(report(source), review)["ok"]
    target = tmp_path / "target.db"
    with pytest.raises(ValueError):
        apply(source, target, review)
    assert not target.exists()


def test_new_rows_remain_unreviewed_and_are_not_auto_selected(source, tmp_path):
    review = approved(source)
    conn = sqlite3.connect(source)
    conn.execute("INSERT INTO memory_projections SELECT 'new-row',memory_type,'NEW PRIVATE BODY',platform,bot_account_id,metadata_json,source_event_ids_json,visibility_scope_json,sensitivity,valid_from FROM memory_projections")
    conn.commit()
    conn.close()
    checked = check_review(report(source), review)
    assert checked["ok"] and len(checked["unreviewed_source_keys"]) == 1
    result = apply(source, tmp_path / "target.db", review)
    assert result["selected"] == 1 and result["retained_in_source"] == 1


def test_cli_template_and_check_modes_are_read_only_json(source, tmp_path):
    root = Path(__file__).resolve().parents[2]
    command = [sys.executable, "-I", str(root / "scripts/audit_legacy_memory.py"), "--db", str(source), "--source-id", "meta"]
    result = subprocess.run([*command, "--review-template"], capture_output=True, encoding="utf-8", timeout=20)
    assert result.returncode == 0, result.stderr
    template = json.loads(result.stdout)
    path = tmp_path / "review.json"
    path.write_text(json.dumps(template), encoding="utf-8")
    checked = subprocess.run([*command, "--check-review", str(path)], capture_output=True, encoding="utf-8", timeout=20)
    assert checked.returncode == 0 and json.loads(checked.stdout)["approved"] == 0
    template["reviews"][0].update(decision="approved", reviewer="A", valid_from="2026-01-01")
    template["reviews"][0]["evidence_fingerprint"] = "stale"
    path.write_text(json.dumps(template), encoding="utf-8")
    stale = subprocess.run([*command, "--check-review", str(path)], capture_output=True, encoding="utf-8", timeout=20)
    assert stale.returncode != 0 and not json.loads(stale.stdout)["ok"]


@pytest.mark.parametrize("change,reason", [
    ("missing_content", "original_message_content_unavailable"),
    ("blank_content", "original_message_content_unavailable"),
    ("summary", "unsupported_evidence_type"),
    ("self_flag", "self_message_is_not_external_fact_evidence"),
    ("self_actor", "self_message_is_not_external_fact_evidence"),
    ("bad_metadata", "original_message_metadata_unverifiable"),
])
def test_manual_approval_cannot_promote_incomplete_or_self_evidence(source, tmp_path, change, reason):
    conn = sqlite3.connect(source)
    conn.execute("ALTER TABLE terminal_events ADD COLUMN metadata_json TEXT DEFAULT '{}'")
    conn.execute("ALTER TABLE terminal_events ADD COLUMN actor_account_id TEXT DEFAULT ''")
    if change == "missing_content":
        conn.execute("UPDATE terminal_events SET content=NULL")
    elif change == "blank_content":
        conn.execute("UPDATE terminal_events SET content='   '")
    elif change == "summary":
        conn.execute("UPDATE terminal_events SET event_type='memory_write'")
    elif change == "self_flag":
        conn.execute("UPDATE terminal_events SET metadata_json=?", (json.dumps({"is_self": True}),))
    elif change == "self_actor":
        conn.execute("UPDATE terminal_events SET actor_account_id=bot_account_id")
    else:
        conn.execute("UPDATE terminal_events SET metadata_json='[]'")
    conn.commit()
    conn.close()
    before = source.read_bytes()
    current = report(source)
    assert current["records"][0]["disposition"] == "legacy_unverified"
    assert reason in current["records"][0]["reasons"]
    template = review_template(current)
    assert check_review(current, template)["ok"]  # 可留存、可核对，不能批准覆盖判据。
    template["reviews"][0].update(decision="approved", reviewer="A", valid_from="2026-01-01")
    assert not check_review(current, template)["ok"]
    target = tmp_path / "target.db"
    with pytest.raises(ValueError, match="not_eligible"):
        apply(source, target, template)
    assert not target.exists() and source.read_bytes() == before


def test_old_schema_without_original_content_is_unverified(source):
    conn = sqlite3.connect(source)
    conn.execute("ALTER TABLE terminal_events DROP COLUMN content")
    conn.commit()
    conn.close()
    record = report(source)["records"][0]
    assert record["disposition"] == "legacy_unverified"
    assert record["reasons"] == ["original_message_content_unavailable"]


def test_one_summary_reference_cannot_be_hidden_among_original_messages(source, tmp_path):
    conn = sqlite3.connect(source)
    conn.execute("INSERT INTO terminal_events SELECT 'summary',platform,bot_account_id,chat_id,'memory_write',content,sensitivity,visibility_scope_json FROM terminal_events")
    conn.execute("UPDATE memory_projections SET source_event_ids_json=?", (json.dumps(["event-0", "summary"]),))
    conn.commit()
    conn.close()
    assert report(source)["records"][0]["disposition"] == "legacy_unverified"
    with pytest.raises(ValueError, match="not_eligible"):
        apply(source, tmp_path / "target.db", approved(source))


def test_external_message_with_explicit_nonself_metadata_remains_reviewable(source, tmp_path):
    conn = sqlite3.connect(source)
    conn.execute("ALTER TABLE terminal_events ADD COLUMN metadata_json TEXT DEFAULT '{}'")
    conn.execute("ALTER TABLE terminal_events ADD COLUMN actor_account_id TEXT DEFAULT ''")
    conn.execute("UPDATE terminal_events SET metadata_json=?,actor_account_id='external-user'", (json.dumps({"is_self": False}),))
    conn.commit()
    conn.close()
    assert report(source)["records"][0]["disposition"] == "eligible_for_review"
    assert apply(source, tmp_path / "target.db", approved(source))["committed"] == 1
