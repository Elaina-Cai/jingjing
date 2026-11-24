import sqlite3

import pytest

from loop import receipts


@pytest.mark.parametrize("body,mine,expected", [("hello",1,True), ("hel lo",1,False), ("hello",0,False), ("other",1,False)])
def test_id_exists_only_verifies_exact_self_body(tmp_path, body, mine, expected):
    path = tmp_path / "data/platforms/wechat/a/account.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as con:
        con.execute("create table messages(id integer,chat_id text,content text,is_self integer, sender_id text generated always as (case when is_self=1 then 'self' else 'other' end) virtual)")
        con.execute("insert into messages values(7,'room',?,?)", (body, mine))
    result = receipts.verify_row({"id":1,"state":"sent","ack":"confirmed","why":"msg 7","chat_id":"room","platform":"wechat","account_id":"a","payload":{"text":"hello"}}, db=path)
    assert result.verified is expected
    assert result.verification_error == ""


def test_missing_body_is_unchecked_not_success(tmp_path):
    result = receipts.verify_row({"id":1,"state":"sent","ack":"confirmed","why":"msg 7","chat_id":"room"}, db=tmp_path / "missing.db")
    assert not result.verified
    assert result.verification_error == "observation_inputs_missing"


def test_direct_send_real_queue_read_preserves_verification_inputs(tmp_path, monkeypatch):
    from loop import direct_send
    path = tmp_path / "data/platforms/wechat/a/account.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as con:
        con.execute("create table messages(id integer,chat_id text,content text,is_self integer, sender_id text generated always as (case when is_self=1 then 'self' else 'other' end) virtual)")
        con.execute("insert into messages values(7,'room','hello',1)")
    monkeypatch.setattr(receipts, "_account_db", lambda: str(path))
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.execute("create table outbound_queue(id integer,state text,ack text,why text,chat_id text,payload text,platform text default 'wechat',account_id text default 'a')")
    con.execute("insert into outbound_queue(id,state,ack,why,chat_id,payload) values(?,?,?,?,?,?)", (1, "sent", "confirmed", "msg 7", "room", '{"text":"hello"}'))
    rows = direct_send._query_queue([1], conn=con)
    assert rows[0]["chat_id"] == "room"
    assert direct_send._verified({"queue_ids":[1]}, {1: rows[0]}) == (True, [])
    con.close()


@pytest.fixture(autouse=True)
def explicit_source_root(tmp_path, monkeypatch):
    from db import world_db
    monkeypatch.setattr(world_db, "ROOT", str(tmp_path))



def test_direct_send_rechecks_late_local_evidence_without_resending_or_repeating_notice(tmp_path,monkeypatch):
    from loop import direct_send,owner_copy
    path=tmp_path/'data/platforms/wechat/a/account.db'
    path.parent.mkdir(parents=True)
    with sqlite3.connect(path) as c:
        c.execute('create table messages(id integer,chat_id text,content text,sender_id text)')
    notices=[]
    monkeypatch.setattr(owner_copy,'note',lambda *a,**kw:notices.append((a,kw)))
    def forbidden(*a,**kw):
        raise AssertionError('delivery checks cannot send')
    monkeypatch.setattr(direct_send.ports,'say_many',forbidden)
    with sqlite3.connect(':memory:') as c:
        c.row_factory=sqlite3.Row
        c.execute('create table outbound_queue(id integer,state text,ack text,why text,chat_id text,payload text,platform text,account_id text)')
        c.execute("insert into outbound_queue values(1,'sent','unconfirmed','','room',?,'wechat','a')",('{"text":"same"}',))
        state={'items':{'order':{'state':'queued','queue_ids':[1],'target':'room'}}}
        assert direct_send.check_deliveries(state=state,conn=c)['unverified']==['order']
        for _ in range(3):
            direct_send.check_deliveries(state=state,conn=c)
        assert len(notices)==1
        with sqlite3.connect(path) as account:
            account.execute("insert into messages values(7,'room','same','self')")
        c.execute("update outbound_queue set ack='confirmed',why='msg 7' where id=1")
        result=direct_send.check_deliveries(state=state,conn=c)
        assert result['local_observed']==['order']
        assert state['items']['order']['state']=='local_observed'
        assert state['items']['order']['evidence_kind']=='local_outgoing_record'
        assert 'unverified_why' not in state['items']['order']
        assert direct_send.check_deliveries(state=state,conn=c)['checked']==0
        assert len(notices)==1


@pytest.mark.parametrize('owned,broken',[(True,False),(True,True),(False,False),(False,True)])
def test_queue_query_closes_only_its_own_connection(monkeypatch,owned,broken):
    from loop import direct_send
    from db import world_db
    class Connection:
        closed=False
        def execute(self,*a):
            if broken:
                raise RuntimeError('query failed')
            return self
        def fetchall(self):
            return []
        def close(self):
            self.closed=True
    conn=Connection()
    monkeypatch.setattr(world_db,'conn',lambda:conn)
    monkeypatch.setattr(direct_send,'warn',lambda *a,**kw:None)
    assert direct_send._query_queue([1],conn=None if owned else conn)==[]
    assert conn.closed is owned


def test_unchanged_unknown_orders_do_not_rewrite_state_or_repeat_notice(monkeypatch):
    from loop import direct_send,owner_copy
    state={'items':{'order':{'state':'queued','queue_ids':[1]}}}
    saves,notices=[],[]
    monkeypatch.setattr(direct_send,'load_state',lambda:state)
    monkeypatch.setattr(direct_send,'save_state',lambda value:saves.append(1))
    monkeypatch.setattr(direct_send,'_query_queue',lambda *a,**kw:[dict(id=1,state='sent',ack='unconfirmed',why='')])
    monkeypatch.setattr(owner_copy,'note',lambda *a,**kw:notices.append(1))
    for _ in range(100):
        assert direct_send.check_deliveries()['unverified']==['order']
    assert saves==notices==[1]


@pytest.mark.parametrize('queue_state,ack,expected',[('unverified','unconfirmed','unverified'),('skipped','dry_run','skipped')])
def test_order_distinguishes_unknown_execution_and_dry_skip(monkeypatch,queue_state,ack,expected):
    from loop import direct_send,owner_copy
    monkeypatch.setattr(owner_copy,'note',lambda *a,**kw:None)
    monkeypatch.setattr(direct_send,'_query_queue',lambda *a,**kw:[dict(id=1,state=queue_state,ack=ack,why='')])
    state={'items':{'order':{'state':'queued','queue_ids':[1]}}}
    result=direct_send.check_deliveries(state=state)
    assert state['items']['order']['state']==expected
    assert result[expected]==['order']
    assert result['failed']==result['local_observed']==[]
    if expected=='unverified':
        assert '执行结果未知' in state['items']['order']['unverified_why']
        assert '还没发出去' not in state['items']['order']['unverified_why']
