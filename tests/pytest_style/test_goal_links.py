from __future__ import annotations

import importlib.util
import inspect
import json
import sqlite3
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.context import situation_store
from core.context.cognitive_commit import Scope
from core.context.goal_links import ensure_schema, goal_for, link_goal
from core.context.goal_receipts import emit_work_item_receipt, work_item_goal
from core.context.goal_wake import pending_wakes
from core.context.situation import WorldModel, open_intention
from core.contracts.plan import Job
from core.jobs.store import JobStore, ensure_schema as ensure_job_schema


def _db(tmp_path: Path):
    path = tmp_path / "meta.db"
    engine = create_engine(f"sqlite:///{path}")
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE terminal_events ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, event_type TEXT, action_payload_json TEXT,"
            "platform TEXT, bot_account_id TEXT, chat_id TEXT, chat_type TEXT, occurred_at DATETIME)"
        ))
    return path, engine


def _event(engine, work_item_id: str, goal_id: str = "goal-1") -> None:
    payload = {"work_item_id": work_item_id, "goal_id": goal_id}
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO terminal_events "
                "(event_type, action_payload_json, platform, bot_account_id, chat_id, chat_type, occurred_at) "
                "VALUES ('work_item_created', :payload, 'wechat', 'acct', 'chat-1', 'private', '2026-11-08 16:00:00')"
            ),
            {"payload": json.dumps(payload, ensure_ascii=False)},
        )


@pytest.fixture
def env(tmp_path, monkeypatch):
    path, engine = _db(tmp_path)
    monkeypatch.setenv("YINYUE_WORLD_MODEL_DIR", str(tmp_path / "world"))
    situation_store._MODELS.clear()
    yield path, engine
    situation_store._MODELS.clear()
    engine.dispose()


def _open_goal(scope: Scope, goal_id: str = "goal-1") -> None:
    model = WorldModel(scope=scope.to_dict())
    open_intention(model, goal_id, kind="act", at="2026-11-08 16:00:00")
    key = situation_store._key(scope.chat_id, scope.to_dict())
    situation_store._MODELS[key] = model
    assert situation_store.persist_world_model(scope.chat_id, scope=scope)


def test_goal_links_schema_and_idempotent(env):
    _, engine = env
    ensure_schema(engine)
    assert link_goal(engine, subject_kind="work_item", subject_id="wi-1", goal_id="g1", source="test")
    assert link_goal(engine, subject_kind="work_item", subject_id="wi-1", goal_id="g1", source="test")
    assert goal_for(engine, subject_kind="work_item", subject_id="wi-1") == "g1"
    assert goal_for(engine, subject_kind="work_item", subject_id="missing") == ""


def test_work_item_goal_falls_back_to_created_event(env):
    _, engine = env
    _event(engine, "wi-1")
    assert work_item_goal(engine, "wi-1")[0] == "goal-1"
    assert work_item_goal(engine, "missing") is None


def test_emit_receipt_writes_goal_signal_and_dedupes(env):
    _, engine = env
    _event(engine, "wi-1")
    scope = Scope("wechat", "acct", "chat-1")
    _open_goal(scope)
    assert emit_work_item_receipt(
        engine, work_item_id="wi-1", run_id="run-1", outcome="sent", at="2026-11-08 17:00:00"
    )
    model = situation_store.get_world_model("chat-1", scope=scope)
    signals = [e for e in model.events if e.kind == "goal_signal"]
    assert len(signals) == 1
    assert signals[0].payload["source"] == "receipt"
    assert signals[0].payload["ref"] == "work_item:run-1:sent"
    from core.context.goal_wake import pending_wakes
    assert any(item.reason == "receipt" for item in pending_wakes(model))
    assert not emit_work_item_receipt(
        engine, work_item_id="wi-1", run_id="run-1", outcome="sent", at="2026-11-08 17:05:00"
    )


def test_emit_receipt_without_link_or_goal_is_noop(env):
    _, engine = env
    assert not emit_work_item_receipt(
        engine, work_item_id="missing", run_id="r", outcome="sent", at="2026-11-08 17:00:00"
    )
    _event(engine, "wi-2", goal_id="")
    assert not emit_work_item_receipt(
        engine, work_item_id="wi-2", run_id="r", outcome="sent", at="2026-11-08 17:00:00"
    )


def test_record_run_finished_has_best_effort_hook(monkeypatch):
    from datetime import datetime
    from core.tasks import store
    from core.context import goal_receipts
    called=[]
    def failure(*args,**kwargs):
        called.append(kwargs)
        raise OSError("isolated world checkpoint failure")
    monkeypatch.setattr(goal_receipts,"emit_work_item_receipt",failure)
    receipt=store._finished_receipt(object(),"wi","run","sent",datetime(2026,11,8),"body","native-event")
    assert receipt["finished_event_id"]=="native-event" and receipt["outcome"]=="sent"
    assert called[0]["work_item_id"]=="wi" and called[0]["run_id"]=="run"


def _load_script():
    path = ROOT / "scripts" / "migrate_goal_links.py"
    spec = importlib.util.spec_from_file_location("migrate_goal_links", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _sandbox_db(name: str) -> Path:
    base = ROOT / ".test_tmp" / "lead-goal-links"
    base.mkdir(parents=True, exist_ok=True)
    path = base / name
    if path.exists():
        path.unlink()
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE terminal_events (id INTEGER PRIMARY KEY, event_type TEXT, "
        "action_payload_json TEXT, occurred_at DATETIME)"
    )
    con.execute(
        "CREATE TABLE job_events (job_id TEXT, seq INTEGER, kind TEXT, payload TEXT, ts DATETIME)"
    )
    con.execute(
        "INSERT INTO terminal_events (event_type, action_payload_json, occurred_at) VALUES "
        "('work_item_created', ?, '2026-11-08 16:00:00'),"
        "('work_item_created', ?, '2026-11-08 16:01:00'),"
        "('work_item_created', 'not-json', '2026-11-08 16:02:00')",
        (
            json.dumps({"work_item_id": "wi-1", "goal_id": "g1"}),
            json.dumps({"work_item_id": "wi-2", "goal_id": "g2"}),
        ),
    )
    con.execute(
        "INSERT INTO job_events (job_id, seq, kind, payload, ts) VALUES "
        "('job-1', 1, 'created', ?, '2026-11-08 16:03:00')",
        (json.dumps({"job_id": "job-1", "goal_id": "g3"}),),
    )
    con.commit()
    con.close()
    return path


def test_migration_dry_run_does_not_create_table(capsys):
    path = _sandbox_db("dry.db")
    assert _load_script().main(["--db", str(path)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["dry_run"] is True and report["would_link"] == 3 and report["invalid"] == 1
    assert {link["subject_kind"] for link in report["links"]} == {"work_item", "job"}
    con = sqlite3.connect(path)
    assert con.execute("SELECT 1 FROM sqlite_master WHERE name='goal_links'").fetchone() is None
    con.close()


def test_migration_apply_is_idempotent(capsys):
    path = _sandbox_db("apply.db")
    script = _load_script()
    assert script.main(["--db", str(path), "--apply"]) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["linked_total"] == 3
    assert script.main(["--db", str(path), "--apply"]) == 0
    second = json.loads(capsys.readouterr().out)
    assert second["linked_total"] == 3
    con = sqlite3.connect(path)
    assert con.execute("SELECT COUNT(*) FROM goal_links").fetchone()[0] == 3
    assert con.execute("SELECT COUNT(*) FROM goal_links WHERE subject_kind='job'").fetchone()[0] == 1
    con.close()


def test_migration_requires_sandbox_or_explicit_flag(tmp_path, capsys, monkeypatch):
    import tempfile
    import uuid
    script = _load_script()
    monkeypatch.setattr(script,"SANDBOX_ROOTS",(tmp_path / "approved",))
    path = tmp_path / "outside.db"
    assert not script._inside_sandbox(path)
    if path.exists():
        path.unlink()
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE terminal_events (id INTEGER PRIMARY KEY, event_type TEXT, action_payload_json TEXT, occurred_at DATETIME)")
    con.commit()
    con.close()
    try:
        assert script.main(["--db", str(path)]) == 2
        report = json.loads(capsys.readouterr().out)
        assert report["error"] == "outside_sandbox_requires_allow_production"
    finally:
        path.unlink(missing_ok=True)


def test_job_create_links_when_goal_table_exists(env):
    _, engine = env
    ensure_job_schema(engine)
    ensure_schema(engine)
    store = JobStore(engine, mode="on")
    job = Job(job_id="job-link-1", chat_id="chat-1", goal="call mom", goal_id="goal-1")
    assert store.create(job) > 0
    assert goal_for(engine, subject_kind="job", subject_id="job-link-1") == "goal-1"


def test_job_create_without_goal_table_still_works(env):
    _, engine = env
    ensure_job_schema(engine)
    store = JobStore(engine, mode="on")
    job = Job(job_id="job-link-2", chat_id="chat-1", goal="call mom", goal_id="goal-1")
    assert store.create(job) > 0
    assert goal_for(engine, subject_kind="job", subject_id="job-link-2") == ""


def test_job_done_emits_receipt_signal(env):
    _, engine = env
    ensure_job_schema(engine)
    ensure_schema(engine)
    scope = Scope("wechat", "acct", "chat-1", "private")
    _open_goal(scope, "goal-1")
    store = JobStore(engine, mode="on")
    job = Job(job_id="job-done-1", chat_id="chat-1", goal="call mom", goal_id="goal-1")
    assert store.create(job) > 0
    assert store.append(job.job_id, "done") > 0
    model = situation_store.get_world_model("chat-1", scope=scope)
    assert any(w.goal_id == "goal-1" and w.reason == "receipt" for w in pending_wakes(model))


def test_migrated_work_item_link_retains_scope_and_receipt_survives_restart(env):
    _, engine = env
    _event(engine, "wi-migrated")
    scope = Scope("wechat", "acct", "chat-1")
    _open_goal(scope)
    ensure_schema(engine)
    link_goal(engine, subject_kind="work_item", subject_id="wi-migrated", goal_id="goal-1")
    linked = work_item_goal(engine, "wi-migrated")
    assert linked[1]["account_id"] == "acct"
    situation_store._MODELS.clear()
    assert emit_work_item_receipt(engine, work_item_id="wi-migrated", run_id="r1", outcome="sent", at="2026-11-08 17:00:00")
    assert not emit_work_item_receipt(engine, work_item_id="wi-migrated", run_id="r1", outcome="sent", at="2026-11-08 17:00:00")


def test_migrated_work_item_link_cannot_override_conflicting_native_goal(env):
    _, engine = env
    _event(engine, "wi-conflict", goal_id="native-goal")
    ensure_schema(engine)
    link_goal(engine, subject_kind="work_item", subject_id="wi-conflict", goal_id="other-goal")
    assert work_item_goal(engine, "wi-conflict") is None
