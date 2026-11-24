"""GUI 旧追认不跨账号猜测，发送包装不另起确认任务。"""
from types import SimpleNamespace
import glob
import sqlite3
import threading

import pytest
from core.execution.gui import wechat_send as gui
from db import world_db


def test_legacy_gui_proof_has_no_read_or_confirmation_capability(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("legacy proof must not scan, spawn or confirm")
    monkeypatch.setattr(glob, "glob", forbidden)
    monkeypatch.setattr(sqlite3, "connect", forbidden)
    monkeypatch.setattr(threading, "Thread", forbidden)
    monkeypatch.setattr(world_db, "confirm_outbound", forbidden)
    monkeypatch.setattr(gui, "warn", lambda *a, **k: None)
    assert gui._gui_self_proof("same", wait_s=0) == ""
    assert gui._gui_self_proof_chat("room", "same", wait_s=0) == ""
    assert gui._gui_confirm_later("room", "same") == {"ok":False,"reason":"operation_binding_missing"}


@pytest.mark.parametrize("status,expected", [("unconfirmed",1),("failed",0),("blocked",0)])
def test_public_wrapper_preserves_result_without_background_confirmation(monkeypatch, status, expected):
    result = SimpleNamespace(status=status, ack="unconfirmed")
    calls = []
    monkeypatch.setattr(gui, "_orig_send_and_verify", lambda *a, **k: result)
    monkeypatch.setattr(world_db, "record_outbound", lambda *a, **k: calls.append((a,k)))
    def forbidden(*args, **kwargs):
        raise AssertionError("wrapper must not schedule legacy confirmation")
    monkeypatch.setattr(gui, "_gui_confirm_later", forbidden)
    assert gui.send_and_verify("room", "same") is result
    assert len(calls) == expected


@pytest.mark.parametrize("mode", ["off", "shadow"])
def test_public_gui_real_dry_gate_has_no_send_or_record(monkeypatch, mode):
    from core import features
    from core.execution.gui import wechat
    monkeypatch.setattr(features, "mode", lambda *a, **k: mode)
    def forbidden(*args, **kwargs):
        raise AssertionError("dry gate must not obtain UI, read store or record sending")
    monkeypatch.setattr(wechat, "_default_deps", forbidden)
    monkeypatch.setattr(wechat, "_decrypted_db", forbidden)
    monkeypatch.setattr(world_db, "record_outbound", forbidden)
    result = gui.send_and_verify("room", "same")
    assert result.status == gui.STATUS_BLOCKED
