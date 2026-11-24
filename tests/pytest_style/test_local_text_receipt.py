import sqlite3
import pytest

from core.execution.local_text_receipt import account_path, baseline, observe


def test_observation_uses_source_chat_cursor_and_exact_body(tmp_path):
    path = tmp_path / "data" / "platforms" / "wechat" / "a" / "account.db"
    path.parent.mkdir(parents=True)
    with sqlite3.connect(path) as con:
        con.execute("create table messages(id integer primary key,chat_id text,is_self integer,content text, sender_id text generated always as (case when is_self=1 then 'self' else 'other' end) virtual)")
        con.execute("insert into messages values(1,'room',1,'same')")
    resolved = account_path(tmp_path, "wechat", "a")
    start = baseline(resolved)
    assert start == 1
    assert observe(resolved, "room", "same", start) == ""
    with sqlite3.connect(path) as con:
        con.executemany("insert into messages values(?,?,?,?)", [(2,"elsewhere",1,"same"), (3,"room",0,"same"), (4,"room",1,"sa me"), (5,"room",1,"same")])
    assert observe(resolved, "room", "same", start) == "5"
    with sqlite3.connect(path) as con:
        con.execute("insert into messages values(6,'room',1,'same')")
    assert observe(resolved, "room", "same", start) == ""
    assert observe(resolved, "room", "same", None) == ""
    assert account_path(tmp_path, "wechat", "../a") is None
    assert account_path(tmp_path, "qq", "a") is None


@pytest.mark.parametrize('sender,mine,expected',[('other',1,False),(None,1,False),('',1,False),('self',0,True),('self',1,True)])
def test_explicit_sender_is_shared_truth_for_live_and_historical_observation(tmp_path,monkeypatch,sender,mine,expected):
    from core.execution.local_text_receipt import matches
    from loop import receipts
    from db import world_db
    path=tmp_path/'data/platforms/wechat/a/account.db'
    path.parent.mkdir(parents=True)
    with sqlite3.connect(path) as con:
        con.execute('create table messages(id integer,chat_id text,is_self integer,content text,sender_id text)')
        con.execute("insert into messages values(7,'room',?,'same',?)",(mine,sender))
    monkeypatch.setattr(world_db,'ROOT',str(tmp_path))
    assert observe(path,'room','same',0)==('7' if expected else '')
    if sender in (None,''):
        from core.execution.local_text_receipt import SenderEvidenceUnavailable
        with pytest.raises(SenderEvidenceUnavailable):
            matches(path,'7','room','same')
    else:
        assert matches(path,'7','room','same') is expected
    row=dict(id=1,state='sent',ack='confirmed',why='msg 7',chat_id='room',platform='wechat',account_id='a',payload={'text':'same'})
    assert receipts.verify_row(row).verified is expected


@pytest.mark.parametrize("sender_schema", [False,True])
def test_missing_sender_column_is_unavailable_and_preserves_history(tmp_path,monkeypatch,sender_schema):
    from core.execution.local_text_receipt import SenderEvidenceUnavailable
    from loop import receipts
    from db import world_db
    path=tmp_path/'data/platforms/wechat/a/account.db'
    path.parent.mkdir(parents=True)
    with sqlite3.connect(path) as con:
        con.execute('create table messages(id integer,chat_id text,is_self integer,content text)')
        con.execute("insert into messages values(7,'room',1,'same')")
        if sender_schema:
            con.execute("alter table messages add column sender_id text")
    monkeypatch.setattr(world_db,'ROOT',str(tmp_path))
    if sender_schema:
        assert observe(path,'room','same',0)==''
    else:
        with pytest.raises(SenderEvidenceUnavailable):
            observe(path,'room','same',0)
    with sqlite3.connect(':memory:') as con:
        con.execute('create table outbound_queue(id integer,chat_id text,state text,ack text,why text,payload text,platform text,account_id text)')
        con.execute("insert into outbound_queue values(1,'room','sent','confirmed','msg 7',?,'wechat','a')",('{"text":"same"}',))
        before=con.execute('select * from outbound_queue').fetchone()
        result=receipts.mark_unverified(conn=con,dry_run=False)
        assert result['marked']==[]
        assert result['unresolved']==[{'queue_id':1,'why':'sender_evidence_unavailable'}]
        assert con.execute('select * from outbound_queue').fetchone()==before


@pytest.mark.parametrize('change',['replace','boundary_edit','truncate'])
def test_snapshot_rejects_replaced_or_rebuilt_source(tmp_path,change):
    from core.execution.local_text_receipt import capture,ObservationSourceChanged
    path=tmp_path/'account.db'
    def create(target):
        with sqlite3.connect(target) as c:
            c.execute('create table messages(id integer primary key,chat_id text,content text,sender_id text)')
            c.execute("insert into messages values(1,'room','old','self')")
        c.close()
    create(path)
    start,token=capture(path)
    with sqlite3.connect(path) as c:
        c.execute("insert into messages values(2,'room','same','self')")
    c.close()
    assert observe(path,'room','same',start,source_token=token)=='2'
    if change=='replace':
        replacement=tmp_path/'replacement.db'
        create(replacement)
        with sqlite3.connect(replacement) as c:
            c.execute("insert into messages values(2,'room','same','self')")
        c.close()
        replacement.replace(path)
    else:
        with sqlite3.connect(path) as c:
            if change=='boundary_edit':
                c.execute("update messages set content='changed' where id=1")
            else:
                c.execute('delete from messages')
                c.execute("insert into messages values(2,'room','same','self')")
    with pytest.raises(ObservationSourceChanged):
        observe(path,'room','same',start,source_token=token)


@pytest.mark.parametrize('token',['','{}','[]','null'])
def test_invalid_source_snapshot_cannot_confirm(tmp_path,token):
    from core.execution.local_text_receipt import ObservationSourceChanged
    with pytest.raises(ObservationSourceChanged):
        observe(tmp_path/'unused','room','same',0,source_token=token)


def test_time_window_ignores_epoch_synced_at(tmp_path):
    from core.execution.local_text_receipt import capture
    path = tmp_path / "data" / "platforms" / "wechat" / "a" / "account.db"
    path.parent.mkdir(parents=True)
    with sqlite3.connect(path) as con:
        con.execute("create table messages(id integer primary key,chat_id text,content text,sender_id text,created_at text,synced_at text)")
        con.execute("insert into messages values(1,'room','old','self','2000-01-01 00:00:00','2000-01-01 00:00:00')")
    start, token = capture(path)
    from datetime import datetime
    now = datetime.now().isoformat(sep=" ", timespec="microseconds")
    with sqlite3.connect(path) as con:
        # 自回填的行 synced_at 被置成 epoch，但消息时间晚于发送前游标。
        con.execute("insert into messages values(?,?,?,?,?,?)", (900, "room", "same", "self", now, "1970-01-01 00:00:00"))
    assert observe(path, "room", "same", start, source_token=token) == "900"


def test_legacy_id_cursor_past_current_data_cannot_confirm(tmp_path):
    from core.execution.local_text_receipt import capture
    path = tmp_path / "account.db"
    with sqlite3.connect(path) as con:
        con.execute("create table messages(id integer primary key,chat_id text,content text,sender_id text)")
        con.execute("insert into messages values(5,'room','old','self')")
    start, token = capture(path)
    with sqlite3.connect(path) as con:
        con.execute("insert into messages values(2,'room','same','self')")
    assert start == 5
    assert observe(path, "room", "same", start, source_token=token) == ""

@pytest.mark.parametrize('sender,mine,expected',[('a',1,True),('a',0,True),('other',1,False),('self',1,True)])
def test_own_account_wxid_counts_as_self_evidence(tmp_path,sender,mine,expected):
    """2026-11-24：现役 account.db 的自发消息是 sender_id=账号 wxid（is_self=1）。

    旧判定只认字面量 'self'，于是送达回读五次都找不到、ack 永远 unconfirmed。
    """
    from core.execution.local_text_receipt import matches
    path=tmp_path/'data/platforms/wechat/a/account.db'
    path.parent.mkdir(parents=True)
    with sqlite3.connect(path) as con:
        con.execute('create table messages(id integer,chat_id text,is_self integer,content text,sender_id text)')
        con.execute("insert into messages values(7,'room',?,'same',?)",(mine,sender))
    assert observe(path,'room','same',0)==('7' if expected else '')
    assert matches(path,'7','room','same') is expected
