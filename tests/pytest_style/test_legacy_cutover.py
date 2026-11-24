from dataclasses import replace
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

import pytest

from core.memory.legacy_acceptance import verify_cutover, verify_import
from core.memory.legacy_read_binding import load_read_binding
from core.memory.legacy_write_freeze import freeze_projection_writes
from test_legacy_read_binding import setup, gateway, P


def _report(setup, binding=None):
    return verify_cutover(setup.source, source_id="meta", target=setup.target,
                          binding=binding or setup.binding, repository=setup.repo)


def _events(path):
    conn = sqlite3.connect(path)
    try:
        return conn.execute("SELECT COUNT(*) FROM cognitive_events").fetchone()[0]
    finally:
        conn.close()


def test_binding_alone_does_not_flip_cutover_ready(setup):
    report = _report(setup)
    assert report["integrity_ok"] is True
    assert report["cutover_ready"] is False
    assert "legacy_projection_write_freeze_not_proven" in report["pending"]
    assert verify_import(setup.source, source_id="meta", target=setup.target)["cutover_ready"] is False


def test_freeze_binding_and_integrity_together_prove_cutover(setup):
    assert freeze_projection_writes(setup.binding, dry_run=False)["frozen"] is True
    binding = load_read_binding(setup.config)
    report = _report(setup, binding)
    assert report["cutover_ready"] is True
    assert report["pending"] == []
    assert report["read_binding"]["imported"] == 1
    assert report["read_binding"]["keys"] == 1
    assert verify_import(setup.source, source_id="meta", target=setup.target)["cutover_ready"] is False


def test_source_drift_revokes_cutover(setup):
    binding = setup.binding
    conn = sqlite3.connect(setup.source)
    conn.execute("UPDATE memory_projections SET content='CHANGED' WHERE memory_id='proj:0'")
    conn.commit()
    conn.close()
    report = _report(setup, binding)
    assert report["integrity_ok"] is False
    assert report["cutover_ready"] is False
    assert "import_integrity_not_proven" in report["pending"]


def test_binding_scope_drift_does_not_prove_cutover(setup):
    freeze_projection_writes(setup.binding, dry_run=False)
    binding = load_read_binding(setup.config)
    bad = replace(binding, scope_key='["wechat","b","same"]')
    report = _report(setup, bad)
    assert report["cutover_ready"] is False
    assert "legacy_reader_binding_not_proven" in report["pending"]


def test_cli_requires_binding_before_cutover(setup):
    root = Path(__file__).resolve().parents[2]
    command = [sys.executable, "-I", str(root / "scripts/verify_legacy_import.py"),
               "--source", str(setup.source), "--source-id", "meta", "--target", str(setup.target),
               "--at", "2026-11-08", "--binding", str(setup.config), "--require-cutover"]
    blocked = subprocess.run(command, capture_output=True, encoding="utf-8", timeout=20)
    assert blocked.returncode == 2, blocked.stdout + blocked.stderr
    assert json.loads(blocked.stdout)["cutover_ready"] is False

    freeze_projection_writes(setup.binding, dry_run=False)
    ready = subprocess.run(command, capture_output=True, encoding="utf-8", timeout=20)
    assert ready.returncode == 0, ready.stdout + ready.stderr
    payload = json.loads(ready.stdout)
    assert payload["cutover_ready"] is True and payload["pending"] == []


def test_rollback_removes_binding_without_erasing_target_journal(setup):
    freeze_projection_writes(setup.binding, dry_run=False)
    load_read_binding(setup.config)
    source_before = setup.source.read_bytes()
    target_before = setup.target.read_bytes()
    event_count = _events(setup.target)
    setup.config.unlink()
    with pytest.raises(FileNotFoundError):
        load_read_binding(setup.config)
    assert setup.source.read_bytes() == source_before
    assert setup.target.read_bytes() == target_before
    assert _events(setup.target) == event_count
    assert gateway(setup, False).recall(P) == []
    assert verify_import(setup.source, source_id="meta", target=setup.target)["integrity_ok"] is True


def test_drill_script_runs_isolated_chain():
    workdir = Path(tempfile.gettempdir()) / "yinyue-legacy-import" / ("drill-" + uuid.uuid4().hex)
    try:
        env = os.environ.copy()
        env["YINYUE_CV3_AUTOSTART"] = "off"
        result = subprocess.run(
            [sys.executable, str(Path(__file__).resolve().parents[2] / "scripts" / "drill_legacy_cutover.py"),
             "--workdir", str(workdir)],
            cwd=str(Path(__file__).resolve().parents[2]), env=env,
            capture_output=True, text=True, encoding="utf-8", timeout=60)
        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)
        assert payload["ok"] is True
        assert [step["step"] for step in payload["steps"]] == [
            "audit", "review", "import_dry", "import_apply", "acceptance",
            "freeze", "cutover_proof", "restart", "rollback"]
        assert payload["steps"][6]["observed"]["cutover_ready"] is True
        assert payload["steps"][8]["observed"]["target_journal_preserved"] is True
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _attempt_fact(target, scope=None):
    from core.context import cognitive_commit as cc
    scope = scope or cc.Scope("wechat", "a", "same")
    conn = cc.connect(str(target))
    try:
        return cc.commit_updates(conn, context=cc.CommitContext(scope=scope, visible_event_ids=frozenset({"event-0"})),
            proposals=(cc.Proposal(proposal_id="post-rollback", subject_key="owned-fact", operation="believe", scope=scope, evidence_refs=("event-0",)),),
            idempotency_key="post-rollback")
    finally:
        conn.close()


def test_rollback_hands_back_both_reads_and_exclusive_memory_write_authority(setup):
    from core.memory.legacy_write_freeze import unfreeze_projection_writes
    from core.memory.beliefs import BeliefMemoryProvider
    from core.context import cognitive_commit as cc
    freeze_projection_writes(setup.binding, dry_run=False)
    before = _events(setup.target)
    result = unfreeze_projection_writes(setup.binding, dry_run=False,
            reason="isolated rollback", confirm="unfreeze:" + setup.binding.scope_key)
    assert result["target_memory_retired"]
    assert _events(setup.target) == before
    denied = _attempt_fact(setup.target)
    assert not denied.ok and denied.rejected[0][1] == "cognitive_memory_retired_use_legacy"
    # Another chat's authority is unaffected by this handback.
    assert _attempt_fact(setup.target, cc.Scope("wechat", "a", "other-chat")).ok
    assert BeliefMemoryProvider().recall(P, limit=20) == []
    from dataclasses import replace
    setup.repo.upsert(replace(setup.repo.get("proj:0"), content="old writer resumed"))
    rebound = load_read_binding(setup.config)
    assert rebound.snapshot(setup.repo) == frozenset()
    from core.terminal.memory_gateway import MemoryGateway
    hits = MemoryGateway(projection_repository=setup.repo, read_binding=rebound).recall(P)
    assert any(h.content == "old writer resumed" for h in hits)
    with pytest.raises(ValueError, match="target_memory_retired"):
        freeze_projection_writes(rebound, dry_run=False)


def test_failed_handback_rolls_back_target_retirement_and_keeps_old_writer_frozen(setup):
    from core.memory.legacy_write_freeze import unfreeze_projection_writes, UNFREEZE_LOG_SQL
    from core.memory.legacy_ownership import retired
    freeze_projection_writes(setup.binding, dry_run=False)
    conn = sqlite3.connect(setup.source)
    conn.execute(UNFREEZE_LOG_SQL)
    conn.execute("CREATE TRIGGER fail_audit BEFORE INSERT ON legacy_projection_unfreeze_log "
                 "BEGIN SELECT RAISE(ABORT,'isolated audit failure'); END")
    conn.commit()
    conn.close()
    with pytest.raises(sqlite3.IntegrityError, match="isolated audit failure"):
        unfreeze_projection_writes(setup.binding, dry_run=False,
                reason="failure injection", confirm="unfreeze:" + setup.binding.scope_key)
    conn = sqlite3.connect(setup.target)
    try:
        assert not retired(conn, setup.binding.scope_key)
    finally:
        conn.close()
    assert _report(setup)["cutover_ready"]
    assert _attempt_fact(setup.target).ok


def test_rollback_database_guard_blocks_raw_journal_insert(setup):
    from core.memory.legacy_write_freeze import unfreeze_projection_writes
    freeze_projection_writes(setup.binding, dry_run=False)
    unfreeze_projection_writes(setup.binding, dry_run=False, reason="guard check",
                               confirm="unfreeze:" + setup.binding.scope_key)
    conn = sqlite3.connect(setup.target)
    try:
        with pytest.raises(sqlite3.IntegrityError, match="cognitive_memory_retired"):
            conn.execute("INSERT INTO cognitive_events(event_id,idempotency_key,proposal_id,subject_key,operation,payload,"
                         "evidence_refs,scope,actor_key,source,supersedes,revision,occurred_at,committed_at) "
                         "SELECT 'rogue','rogue','rogue',subject_key,operation,payload,evidence_refs,scope,actor_key,"
                         "source,supersedes,revision,occurred_at,committed_at FROM cognitive_events LIMIT 1")
    finally:
        conn.close()


@pytest.mark.parametrize("replace_guard", [False, True])
def test_handback_binding_rejects_missing_or_replaced_target_guard(setup, replace_guard):
    from core.memory.legacy_write_freeze import unfreeze_projection_writes
    freeze_projection_writes(setup.binding, dry_run=False)
    unfreeze_projection_writes(setup.binding, dry_run=False, reason="guard integrity",
                               confirm="unfreeze:" + setup.binding.scope_key)
    conn = sqlite3.connect(setup.target)
    try:
        conn.execute("DROP TRIGGER legacy_memory_rollback_guard")
        if replace_guard:
            conn.execute("CREATE TRIGGER legacy_memory_rollback_guard BEFORE INSERT ON cognitive_events BEGIN SELECT 1; END")
        conn.commit()
    finally:
        conn.close()
    with pytest.raises(ValueError, match="rollback_guard_missing_or_changed"):
        load_read_binding(setup.config)
