"""Post-send ledger failures must be visible, not swallowed, and must not rewrite the send result."""
from __future__ import annotations

from types import SimpleNamespace

from core.execution.gui import wechat_send as gui


def test_outbound_record_failure_is_logged_and_result_preserved(monkeypatch):
    result = SimpleNamespace(ok=True, status="unconfirmed", ack="unconfirmed", detail="")
    monkeypatch.setattr(gui, "_orig_send_and_verify", lambda *a, **kw: result)
    seen = []
    monkeypatch.setattr(gui, "warn", lambda event, **kw: seen.append((event, kw)))
    import db.world_db as world
    def boom(*a, **kw):
        raise RuntimeError("ledger down")
    monkeypatch.setattr(world, "record_outbound", boom)

    assert gui.send_and_verify("room", "same") is result
    assert seen and seen[0][0] == "wechat_gui_outbound_record_failed"
    assert "ledger down" in seen[0][1]["err"]


def test_receipt_store_failure_is_logged_without_replacing_receipt(caplog):
    import logging
    class BrokenStore:
        def record_receipt(self, *args, **kwargs):
            raise OSError("isolated receipt store unavailable")
    receipt = gui.SendReceipt(status="unconfirmed", chat="isolated-chat")
    with caplog.at_level(logging.WARNING, logger=gui.__name__):
        gui._record(BrokenStore(), receipt, turn_id="turn")
    assert receipt.status == "unconfirmed"
    assert any(r.msg == "wechat_gui_receipt_record_failed" and "unavailable" in r.err for r in caplog.records)
