"""ports 无来源操作绑定不能按前缀/时间窗确认发送。"""
import json
import sqlite3
import threading
import glob

from loop import ports, ledger
from core.infra import actions
from db import world_db


def test_legacy_proof_and_callbacks_cannot_read_spawn_or_confirm(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("unbound proof has no read or confirmation capability")
    monkeypatch.setattr(ports, "_account_db", forbidden)
    monkeypatch.setattr(sqlite3, "connect", forbidden)
    monkeypatch.setattr(threading, "Thread", forbidden)
    monkeypatch.setattr(glob, "glob", forbidden)
    monkeypatch.setattr(ledger, "record_delivery", forbidden)
    monkeypatch.setattr(world_db, "confirm_outbound", forbidden)
    monkeypatch.setattr(ports, "warn", lambda *a, **k: None)
    assert ports._self_proof("room", "same prefix", 123) == ""
    assert ports._self_proof_window("room", 123) == ""
    ports._confirm_later("room", "same", "turn", 123, delay_s=1)
    ports._confirm_image_later("room", "image")


def test_historical_backfill_reports_missing_binding_without_writes(tmp_path, monkeypatch):
    path = tmp_path / "actions.jsonl"
    path.write_text(json.dumps(dict(act="say",ack="unconfirmed",turn_id="t",target="room",text="same",at="2026-11-07 12:00:00")),encoding="utf-8")
    monkeypatch.setattr(actions, "path", lambda day: str(path))
    def forbidden(*args, **kwargs):
        raise AssertionError("historical backfill cannot guess proof or append confirmation")
    monkeypatch.setattr(ledger, "record_delivery", forbidden)
    monkeypatch.setattr(ports, "_self_proof", forbidden)
    monkeypatch.setattr(ports, "_self_proof_window", forbidden)
    before = path.read_bytes()
    result = ports.backfill_receipts()
    assert result["checked"] == 1
    assert result["confirmed"] == 0
    assert result["still_missing"] == 1
    assert result["missing"][0]["reason"] == "operation_binding_missing"
    assert path.read_bytes() == before
