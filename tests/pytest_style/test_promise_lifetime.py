"""承诺生命周期由显式事件决定，不由两天查询窗口决定。"""
import json
from pathlib import Path

import pytest

from core.infra import actions
from loop import ledger


def put(day, rows):
    path = Path(actions.path(day))
    path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", encoding="utf-8")
    return path


def test_old_promise_survives_and_later_closure_is_seen(tmp_path, monkeypatch):
    monkeypatch.setattr(actions, "DIR", str(tmp_path))
    old = put("2020-01-01", [{"kind": "promise", "id": "old", "what": "找资料"}])
    before = old.read_bytes()
    assert [r["id"] for r in ledger.open_promises()] == ["old"]
    assert "找资料" in ledger.render_promises()
    closed = put("2020-01-07", [{"kind": "promise_closed", "ref": "old"}])
    assert ledger.open_promises() == []
    assert old.read_bytes() == before
    assert closed.exists()


def test_empty_directory_and_new_file_invalidate_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(actions, "DIR", str(tmp_path))
    assert ledger.open_promises() == []
    put("2020-01-01", [{"kind": "promise", "id": "1", "what": "第一件"}])
    rows = ledger.open_promises()
    assert len(rows) == 1
    rows[0]["what"] = "调用方修改副本"
    assert ledger.open_promises()[0]["what"] == "第一件"


def test_existing_file_change_and_explicit_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(actions, "DIR", str(tmp_path))
    put("2020-01-01", [{"kind": "promise", "id": "1"}, {"kind": "promise", "id": "2"}])
    assert len(ledger.open_promises()) == 2
    assert ledger.open_promises(limit=1)[0]["id"] == "2"
    put("2020-01-01", [{"kind": "promise", "id": "1"}, {"kind": "promise_closed", "ref": "1"}])
    assert ledger.open_promises() == []


def test_corrupt_history_does_not_look_like_no_promises(tmp_path, monkeypatch):
    monkeypatch.setattr(actions, "DIR", str(tmp_path))
    Path(actions.path("2020-01-01")).write_text("{broken", encoding="utf-8")
    with pytest.raises(ValueError):
        ledger.open_promises()


def test_unchanged_history_is_not_reread(tmp_path, monkeypatch):
    monkeypatch.setattr(actions, "DIR", str(tmp_path))
    put("2020-01-01", [{"kind": "promise", "id": "cached"}])
    assert ledger.open_promises()[0]["id"] == "cached"

    def unexpected_read(*args, **kwargs):
        raise AssertionError("未变化的流水不应再次读取")

    monkeypatch.setattr(actions, "open", unexpected_read, raising=False)
    for _ in range(20):
        assert ledger.open_promises()[0]["id"] == "cached"
