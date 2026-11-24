from __future__ import annotations

import asyncio
import importlib.util
import sqlite3
import tempfile
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import create_engine, text

from core.quality import corrections as corr
from core.quality import feedback_lease
from test_explicit_memory_commit import db, counts
from test_legacy_read_binding import P, execute


def _meta(db):
    return SimpleNamespace(memory_projections=db.repo, feedback_samples=db.feedback)


def test_lease_is_exclusive_until_expiry_and_token_release(tmp_path):
    engine = create_engine("sqlite:///" + str(tmp_path / "lease.db"))
    feedback_lease.ensure_schema(engine)
    base = datetime(2026, 11, 8, 12, 0, 0)
    first = feedback_lease.try_acquire(engine, 1, owner_id="a", ttl_s=10, now=base)
    assert first
    assert feedback_lease.try_acquire(
        engine, 1, owner_id="b", ttl_s=10, now=base + timedelta(seconds=5)
    ) == ""
    assert feedback_lease.release(engine, 1, "wrong-token") is False
    second = feedback_lease.try_acquire(
        engine, 1, owner_id="b", ttl_s=10, now=base + timedelta(seconds=11)
    )
    assert second
    assert feedback_lease.held(engine, 1, second, now=base + timedelta(seconds=12)) is True
    assert feedback_lease.release(engine, 1, second) is True
    assert feedback_lease.held(engine, 1, second, now=base + timedelta(seconds=12)) is False
    engine.dispose()


def test_concurrent_consumers_do_not_both_call_the_model(db, monkeypatch):
    feedback_lease.ensure_schema(db.engine)
    db.feedback.add(
        source="correction",
        platform="wechat",
        bot_account_id="a",
        chat_id="same",
        chat_type="private",
        person_id="p",
        sample_text="之前说错了，地址需要更正",
        created_at=datetime(2026, 11, 8),
    )
    calls = []

    async def fake(sample, meta_repo, llm, lease_token=""):
        calls.append(lease_token)
        await asyncio.sleep(0.05)
        return "deferred"

    monkeypatch.setattr(corr, "_process_sample", fake)
    meta = _meta(db)

    async def run_twice():
        return await asyncio.gather(
            corr.process_pending_corrections(meta_repo=meta, llm=object(), lease=True),
            corr.process_pending_corrections(meta_repo=meta, llm=object(), lease=True),
        )

    stats = asyncio.run(run_twice())
    assert len(calls) == 1
    assert sum(item["deferred"] for item in stats) == 1
    assert sum(item["leased"] for item in stats) == 1
    with db.engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM feedback_leases")).scalar() == 0


def test_missing_lease_table_fails_closed(db, monkeypatch):
    called = []

    async def fake(*a, **kw):
        called.append(1)
        return "deferred"

    monkeypatch.setattr(corr, "_process_sample", fake)
    result = asyncio.run(
        corr.process_pending_corrections(meta_repo=_meta(db), llm=object(), lease=True)
    )
    assert result["status"] == "lease_unavailable"
    assert not called


def _init_script():
    path = Path(__file__).resolve().parents[2] / "scripts" / "init_feedback_leases.py"
    spec = importlib.util.spec_from_file_location("init_feedback_leases", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _lease_table_exists(path):
    conn = sqlite3.connect(str(path))
    try:
        return bool(conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='feedback_leases'"
        ).fetchone())
    finally:
        conn.close()


def test_init_script_dry_run_then_apply_uses_same_ddl():
    script = _init_script()
    assert " ".join(script.CREATE_SQL.split()) == " ".join(feedback_lease.CREATE_SQL.split())
    base = Path(tempfile.gettempdir()) / "yinyue-legacy-import" / ("lease-" + uuid.uuid4().hex[:8])
    base.mkdir(parents=True)
    path = base / "lease.db"
    sqlite3.connect(str(path)).close()
    assert script.main(["--db", str(path)]) == 0
    assert not _lease_table_exists(path)
    assert script.main(["--db", str(path), "--apply"]) == 0
    assert _lease_table_exists(path)
    assert script.main(["--db", str(path), "--apply"]) == 0


def test_init_script_rejects_outside_sandbox(tmp_path, monkeypatch):
    script = _init_script()
    monkeypatch.setattr(script,"SANDBOX_ROOTS",(tmp_path / "approved",))
    base = tmp_path / "outside"
    base.mkdir(parents=True)
    path = base / "lease.db"
    assert not script._inside_sandbox(path)
    sqlite3.connect(str(path)).close()
    try:
        assert script.main(["--db", str(path)]) == 2
        assert not _lease_table_exists(path)
    finally:
        for item in base.iterdir():
            item.unlink()
        base.rmdir()


def test_lease_lost_during_model_call_does_not_commit(db, monkeypatch):
    sample = db.feedback.add(
        source="correction",
        platform="wechat",
        bot_account_id="a",
        chat_id="same",
        chat_type="private",
        person_id=P.person_id,
        sample_text="之前说错了，地址需要更正",
        created_at=datetime(2026, 11, 8),
    )
    monkeypatch.setattr(corr, "_owner_ids", lambda platform: {P.person_id})
    monkeypatch.setattr(corr, "_rate_ok", lambda key: True)
    monkeypatch.setattr(corr, "_lookup_quoted", lambda *a: None)
    monkeypatch.setattr(corr, "_collect_candidates", lambda **kw: [])
    async def model(*a, **kw):
        return {"action": "upsert", "fact": "主人更正后的明确资料", "confidence": 1}
    monkeypatch.setattr(corr, "_extract", model)
    monkeypatch.setattr(feedback_lease, "table_exists", lambda engine: True)
    monkeypatch.setattr(feedback_lease, "try_acquire", lambda *a, **kw: "lease-token")
    monkeypatch.setattr(feedback_lease, "release", lambda *a, **kw: True)
    monkeypatch.setattr(feedback_lease, "held", lambda *a, **kw: False)
    meta = _meta(db)
    result = execute(SimpleNamespace(
        execute=lambda **kw: corr.process_pending_corrections(
            meta_repo=meta, llm=object(), lease=True
        )
    ))
    assert result["lease_lost"] == 1
    assert counts(db) == (0, 0)


def _process_command(db, tmp_path, worker, hold=False):
    import os
    import sys
    root = Path(__file__).resolve().parents[2]
    command = [sys.executable, "-c", "import runpy; runpy.run_path('tests/fixtures/feedback_process_worker.py', run_name='__main__')",
               str(db.engine.url.database), worker, str(tmp_path / (worker + ".ready")), str(tmp_path / "release")]
    if hold:
        command.append("--hold")
    env = os.environ.copy()
    env.update(META_DB_PATH=str(tmp_path / "worker-meta.db"), YINYUE_COGNITIVE_DB=str(tmp_path / "worker-cognitive.db"),
               DATA_DIR=str(tmp_path / "data"), PLATFORMS_DIR=str(tmp_path / "platforms"),
               YINYUE_WORLD_MODEL_DIR=str(tmp_path / "world"), YINYUE_CV3_AUTOSTART="off", PYTHONIOENCODING="utf-8")
    return command, root, env


def _prepare_process_sample(db):
    feedback_lease.ensure_schema(db.engine)
    with db.engine.begin() as conn:
        conn.execute(text("CREATE TABLE test_model_calls(worker TEXT)"))
    return db.feedback.add(source="correction", platform="wechat", bot_account_id="a", chat_id="same",
        chat_type="private", person_id="person-a", sample_text="更正之前的明确资料", created_at=datetime(2026, 11, 8))


def _wait_model_ready(process, path):
    import time
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and not path.exists() and process.poll() is None:
        time.sleep(0.01)
    assert path.exists(), process.communicate(timeout=5)


def test_real_processes_share_lease_and_only_one_calls_model_and_consumes(db, tmp_path):
    import subprocess
    _prepare_process_sample(db)
    command, cwd, env = _process_command(db, tmp_path, "first", hold=True)
    first = subprocess.Popen(command, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8")
    try:
        _wait_model_ready(first, tmp_path / "first.ready")
        command, cwd, env = _process_command(db, tmp_path, "second")
        second = subprocess.run(command, cwd=cwd, env=env, capture_output=True, text=True, encoding="utf-8", timeout=20)
        assert second.returncode == 0, second.stdout + second.stderr
        stats = __import__("json").loads(second.stdout.strip().splitlines()[-1])
        assert stats["leased"] == 1
        (tmp_path / "release").touch()
        stdout, stderr = first.communicate(timeout=20)
        assert first.returncode == 0, stdout + stderr
        assert __import__("json").loads(stdout.strip().splitlines()[-1])["learned"] == 1
        assert counts(db) == (1, 1)
        with db.engine.connect() as conn:
            assert conn.execute(text("SELECT worker FROM test_model_calls")).all() == [("first",)]
            assert conn.execute(text("SELECT status FROM feedback_samples")).scalar() == "learned"
    finally:
        if first.poll() is None:
            first.terminate()
            first.communicate(timeout=10)


def test_process_crash_then_expired_lease_recovers_without_double_commit(db, tmp_path):
    import subprocess
    _prepare_process_sample(db)
    command, cwd, env = _process_command(db, tmp_path, "crashed", hold=True)
    crashed = subprocess.Popen(command, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8")
    try:
        _wait_model_ready(crashed, tmp_path / "crashed.ready")
    finally:
        if crashed.poll() is None:
            crashed.terminate()
        crashed.communicate(timeout=10)
    assert counts(db) == (0, 0)
    with db.engine.begin() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM feedback_leases")).scalar() == 1
        # Advance the persisted test clock instead of waiting ten minutes.
        conn.execute(text("UPDATE feedback_leases SET expires_at='2000-01-01 00:00:00'"))
    command, cwd, env = _process_command(db, tmp_path, "recovery")
    recovered = subprocess.run(command, cwd=cwd, env=env, capture_output=True, text=True, encoding="utf-8", timeout=20)
    assert recovered.returncode == 0, recovered.stdout + recovered.stderr
    assert counts(db) == (1, 1)
    with db.engine.connect() as conn:
        assert conn.execute(text("SELECT status FROM feedback_samples")).scalar() == "learned"
        assert conn.execute(text("SELECT COUNT(*) FROM test_model_calls")).scalar() == 2
        assert conn.execute(text("SELECT COUNT(*) FROM feedback_leases")).scalar() == 0
