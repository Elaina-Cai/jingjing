"""GUI 主方法使用明确账号的本地观察，不能升级为送达事实。"""
import contextlib
import sqlite3
from types import SimpleNamespace

import pytest
from core.execution.gui import wechat, wechat_send as gui
from db import world_db


@pytest.fixture
def isolated_gui(tmp_path, monkeypatch):
    monkeypatch.setattr(gui, "_mode", lambda: "on")
    monkeypatch.setattr(gui, "_probe", lambda *a, **k: None)
    monkeypatch.setattr(gui, "warn", lambda *a, **k: None)
    monkeypatch.setattr(world_db, "ROOT", str(tmp_path))
    monkeypatch.setattr(world_db, "record_outbound", lambda *a, **k: None)
    monkeypatch.setattr(wechat, "device_guard", lambda *a, **k: contextlib.nullcontext())
    monkeypatch.setattr(wechat, "display_name_for", lambda chat: chat)
    monkeypatch.setattr(wechat, "fill_message", lambda *a, **k: SimpleNamespace(filled_verified=True,text_sha256="fill"))
    from core.execution import wx_chat
    monkeypatch.setattr(wx_chat, "invalidate_active_chat", lambda *a: None)
    def forbidden(*a, **k):
        raise AssertionError("must not read default snapshot")
    monkeypatch.setattr(wechat, "_decrypted_db", forbidden)
    monkeypatch.setattr(wechat, "rows_after", forbidden)
    return tmp_path


def make_account(root, account):
    path = root / "data/platforms/wechat" / account / "account.db"
    path.parent.mkdir(parents=True)
    with sqlite3.connect(path) as c:
        c.execute("create table messages(id integer,chat_id text,content text,is_self integer, sender_id text generated always as (case when is_self=1 then 'self' else 'other' end) virtual)")
        c.execute("insert into messages values(1,'room','same',1)")
    return path


@pytest.mark.parametrize("variant,expected", [("exact",2),("other_chat",0),("other_account",0),("body_diff",0),("multiple",0),("old_only",0)])
def test_gui_real_local_observation(isolated_gui, variant, expected):
    root = isolated_gui
    path = make_account(root,"a")
    foreign = make_account(root,"b")
    presses = []
    def press():
        presses.append(1)
        if variant != "old_only":
            target = foreign if variant == "other_account" else path
            with sqlite3.connect(target) as c:
                c.execute("insert into messages values(2,?,?,1)", ("elsewhere" if variant=="other_chat" else "room", "sa me" if variant=="body_diff" else "same"))
                if variant=="multiple":
                    c.execute("insert into messages values(3,'room','same',1)")
        return True
    result = gui.send_and_verify("room","same",account_id="a",deps=SimpleNamespace(press_enter=press),polls=1)
    assert presses == [1]
    assert result.status == "unconfirmed"
    assert result.server_id == 0
    assert result.local_id == expected
    assert result.evidence["account_id"] == "a"
    assert result.as_receipt()["ack"] == "unconfirmed"


def test_missing_source_sends_once_but_never_claims_observation(isolated_gui):
    presses=[]
    result=gui.send_and_verify("room","same",deps=SimpleNamespace(press_enter=lambda: presses.append(1) or True))
    assert presses == [1]
    assert result.status == "unconfirmed"
    assert result.local_id == result.server_id == result.polls == 0


def test_foreign_explicit_path_blocked_before_send(isolated_gui):
    make_account(isolated_gui,"a")
    foreign=make_account(isolated_gui,"b")
    def forbidden():
        raise AssertionError("wrong source cannot send")
    result=gui.send_and_verify("room","same",account_id="a",db_path=foreign,deps=SimpleNamespace(press_enter=forbidden))
    assert result.status == "blocked"


@pytest.mark.parametrize("source", [{"account_id":"../a"}, {"account_id":"missing"}, {"platform":"qq"}])
def test_invalid_explicit_source_blocks_before_send(isolated_gui, source):
    def forbidden():
        raise AssertionError("invalid source cannot send")
    result=gui.send_and_verify("room","same",deps=SimpleNamespace(press_enter=forbidden),**source)
    assert result.status == "blocked"


@pytest.mark.parametrize("outcome", [False, None, "raise_after_key"])
def test_key_result_unknown_never_claims_not_sent_or_retries(isolated_gui, monkeypatch, outcome):
    from core.execution import wx_chat
    presses, invalidated, records, world_records = [], [], [], []
    monkeypatch.setattr(wx_chat, "invalidate_active_chat", lambda why: invalidated.append(why))
    monkeypatch.setattr(world_db, "record_outbound", lambda *a, **k: world_records.append((a,k)))
    def press():
        presses.append("physical_effect")
        if outcome == "raise_after_key":
            raise RuntimeError("driver lost response after key")
        return outcome
    store=SimpleNamespace(record_receipt=lambda receipt, **kw: records.append(receipt))
    result=gui.send_and_verify("room","same",deps=SimpleNamespace(press_enter=press),store=store,turn_id="test")
    assert presses == ["physical_effect"]
    assert invalidated == ["after_send"]
    assert result.status == "unconfirmed"
    assert result.evidence["execution"] == "unknown"
    assert result.local_id == result.server_id == 0
    assert "press_result_unknown" in result.detail
    assert len(records) == len(world_records) == 1
    assert records[0]["ack"] == world_records[0][1]["ack"] == "unconfirmed"


def test_fill_failure_remains_before_key_boundary(isolated_gui, monkeypatch):
    from core.execution.gui.base import GuiError
    def fill(*a, **k):
        raise GuiError("not_connected", "no input")
    def forbidden():
        raise AssertionError("fill failure cannot execute send key")
    monkeypatch.setattr(wechat,"fill_message",fill)
    result=gui.send_and_verify("room","same",deps=SimpleNamespace(press_enter=forbidden))
    assert result.status == "failed"
    assert "fill_failed" in result.detail


def test_guard_exit_after_key_cannot_report_pre_send_block(isolated_gui, monkeypatch):
    presses=[]
    @contextlib.contextmanager
    def guard(*a, **k):
        yield
        raise wechat.device_lease_mod.UiBusy("lease exit failed")
    monkeypatch.setattr(wechat,"device_guard",guard)
    result=gui.send_and_verify("room","same",deps=SimpleNamespace(press_enter=lambda: presses.append(1) or True))
    assert presses == [1]
    assert result.status == "unconfirmed"
    assert result.evidence["execution"] == "unknown"
    assert "guard_exit_failed" in result.detail



def test_delayed_local_row_stays_unconfirmed_and_records_poll_count(isolated_gui, monkeypatch):
    path = make_account(isolated_gui, "a")
    assert path
    calls = []
    def delayed(*a, **kw):
        calls.append(1)
        return 0 if len(calls) < 3 else 42
    from core.execution import local_text_receipt as local
    monkeypatch.setattr(local, "observe", delayed)
    presses = []
    result = gui.send_and_verify(
        "room", "same", account_id="a", deps=SimpleNamespace(press_enter=lambda: presses.append(1) or True),
        polls=3, poll_interval_s=0, sleep=lambda _s: None,
    )
    assert presses == [1]
    assert result.status == "unconfirmed"
    assert result.local_id == 42
    assert result.polls == 3
    assert result.evidence["evidence_kind"] == "local_outgoing_record"
    assert "不代表平台送达或已读" in result.detail


def test_duplicate_same_text_does_not_reuse_previous_row(isolated_gui):
    make_account(isolated_gui, "a")
    def first_press():
        import sqlite3
        with sqlite3.connect(isolated_gui / "data/platforms/wechat/a/account.db") as c:
            c.execute("insert into messages values(2,'room','same',1)")
        return True
    first = gui.send_and_verify("room", "same", account_id="a",
                                deps=SimpleNamespace(press_enter=first_press), polls=1)
    assert first.local_id == 2
    second = gui.send_and_verify("room", "same", account_id="a",
                                 deps=SimpleNamespace(press_enter=lambda: True), polls=1)
    assert second.status == "unconfirmed"
    assert second.local_id == 0
    assert second.evidence["evidence_kind"] == "unknown"
