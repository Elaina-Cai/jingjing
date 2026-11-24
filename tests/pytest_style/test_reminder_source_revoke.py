from __future__ import annotations

from datetime import datetime

from sqlalchemy import create_engine

from core.context import intention_bridge
from core.context.cognitive_commit import Scope
from core.context.situation import project
from core.context.situation_store import get_world_model, reset_world_models
from db.meta_models import MetaBase, Reminder
from db.repo_reminder import ReminderRepository


def test_reminder_repository_cancel_stops_pending_scan():
    engine = create_engine("sqlite://")
    MetaBase.metadata.create_all(engine)
    repo = ReminderRepository(engine)
    row = repo.add(
        Reminder(
            content="提醒我喝水",
            remind_at=datetime(2026, 11, 20, 8, 0),
            status="pending",
            source_chat_id="c1",
            created_at=datetime(2026, 11, 19, 8, 0),
        )
    )

    assert repo.cancel(row.id) is True
    assert repo.get(row.id).status == "cancelled"
    assert repo.cancel(row.id) is True


def test_reminder_intention_keeps_its_request_event(tmp_path, monkeypatch):
    monkeypatch.setenv("YINYUE_WORLD_MODEL_DIR", str(tmp_path / "world"))
    monkeypatch.setenv("YINYUE_COGNITIVE_DB", str(tmp_path / "cognitive.db"))
    monkeypatch.setattr(
        intention_bridge,
        "reminder_intentions_enabled",
        lambda: True,
    )
    reset_world_models()
    scope = Scope("wechat", "bot", "c1")

    assert intention_bridge.open_reminder_intention(
        chat_id="c1",
        reminder_id=7,
        due_at="2026-11-20 08:00",
        evidence="提醒我喝水",
        at="2026-11-19 08:00",
        scope=scope,
        source_refs=("event-request",),
    )
    assert intention_bridge.open_reminder_intention(
        chat_id="c1",
        reminder_id=7,
        due_at="2026-11-20 08:00",
        evidence="提醒我喝水",
        at="2026-11-19 08:00",
        scope=scope,
        source_refs=("event-request",),
    )

    model = get_world_model("c1", scope=scope)
    goal = project(model.events).intentions["reminder:7@2026-11-20 08:00"]
    assert goal.evidence_refs == ("event-request",)
