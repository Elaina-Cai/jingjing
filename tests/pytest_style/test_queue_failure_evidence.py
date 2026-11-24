import contextlib
import sqlite3

import pytest

from db import world_db as world
from loop import desk


@pytest.mark.parametrize("evidence,terminal,expected", [(False, False, "unverified"), (False, True, "unverified"), (True, False, "pending"), (True, True, "failed")])
def test_retry_requires_explicit_nonexecution(monkeypatch, tmp_path, evidence, terminal, expected):
    path = str(tmp_path / "world.db")
    @contextlib.contextmanager
    def connection():
        con = sqlite3.connect(path)
        con.row_factory = sqlite3.Row
        try:
            yield con
        finally:
            con.close()
    with connection() as con:
        for ddl in world._SCHEMA:
            con.execute(ddl)
        con.execute("insert into outbound_queue(id,idem_key,chat_id,state,attempts,not_after) values(1,'test','room','sending',1,9999)")
        con.execute("insert into outbound_attempt(queue_id,attempt_no,result) values(1,1,'running')")
        con.execute("update outbound_queue set owner='worker'")
        con.execute("update outbound_attempt set owner='worker'")
        con.commit()
    monkeypatch.setattr(world, "_open", connection)
    assert world.settle(1, False, owner="worker", attempt_no=1, why="发送超时", not_executed=evidence, terminal=terminal, now=1000) == expected
    with connection() as con:
        result = con.execute("select result from outbound_attempt").fetchone()[0]
        assert result == ("not_executed" if evidence else "uncertain")


def test_error_text_cannot_trigger_second_send(monkeypatch):
    calls = []
    class Sender:
        def send_text(self, *args):
            calls.append(args)
            return {"ok": False, "error": "无法激活微信窗口"}
    monkeypatch.setattr(desk, "_bootstrap", lambda: None)
    monkeypatch.setattr(desk, "_label", lambda chat: chat)
    monkeypatch.setattr(desk, "_dry", lambda chat: False)
    monkeypatch.setattr(desk, "_sender", Sender)
    monkeypatch.setattr(desk, "_log", lambda *a, **kw: None)
    outcome = desk.deliver({"id": 1, "chat_id": "room", "kind": "text", "payload": {"text": "hello"}})
    assert outcome.ok is False
    assert len(calls) == 1
    assert not getattr(outcome, "not_executed", False)


def test_file_preflight_provides_nonexecution_evidence(monkeypatch):
    monkeypatch.setattr(desk, "_bootstrap", lambda: None)
    monkeypatch.setattr(desk, "_label", lambda chat: chat)
    monkeypatch.setattr(desk, "_dry", lambda chat: False)
    monkeypatch.setattr(desk, "_sender", lambda: object())
    result = desk.deliver({"chat_id": "room", "kind": "file", "payload": {"path": ""}})
    assert result.not_executed is True
    assert result.ok is False
