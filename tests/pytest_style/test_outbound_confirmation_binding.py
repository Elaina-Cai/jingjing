import contextlib
import sqlite3

import pytest

from db import world_db as world


@pytest.fixture
def queue(tmp_path, monkeypatch):
    path = str(tmp_path / "world.db")
    @contextlib.contextmanager
    def connection():
        con = sqlite3.connect(path)
        con.row_factory = sqlite3.Row
        try:
            with con:
                yield con
        finally:
            con.close()
    with connection() as con:
        for ddl in world._SCHEMA:
            con.execute(ddl)
        for i, account in [(1,"a"), (2,"a"), (3,"b")]:
            con.execute("insert into outbound_receipt(id,turn_id,seq,platform,account_id,chat_id,text,text_hash,ack,sent_at_utc) values(?,?,0,'wechat',?,'room','same',?,'unconfirmed','2026-11-07T00:00:00')",
                        (i, str(i), account, world.text_hash("same")))
    monkeypatch.setattr(world, "conn", connection)
    monkeypatch.setattr(world, "_open", connection)
    return connection


def confirm(receipt, account="a", mid="7", **kw):
    return world.confirm_outbound("room", "same", receipt_id=receipt, platform="wechat", account_id=account, msg_id=mid, **kw)


def test_exact_binding_and_idempotence(queue):
    assert confirm(1) == 1
    assert confirm(1) == 0
    with queue() as con:
        assert [tuple(r) for r in con.execute("select id,ack from outbound_receipt order by id")] == [(1,"confirmed"), (2,"unconfirmed"), (3,"unconfirmed")]


def test_same_account_message_cannot_be_claimed_twice_but_other_account_can(queue):
    assert confirm(1) == 1
    with pytest.raises(ValueError, match="claimed"):
        confirm(2)
    assert confirm(3, account="b") == 1


def test_source_and_evidence_cannot_be_replaced(queue):
    with pytest.raises(ValueError, match="source_mismatch"):
        confirm(3)
    assert confirm(1) == 1
    with pytest.raises(ValueError, match="conflict"):
        confirm(1, mid="8")


def test_legacy_text_only_update_has_no_write_capability(queue):
    with pytest.raises(ValueError, match="source_missing"):
        world.confirm_outbound("room", "same", msg_id="7")
    with queue() as con:
        assert con.execute("select count(*) from outbound_receipt where ack='confirmed'").fetchone()[0] == 0


def test_queue_and_receipt_confirmation_are_atomic(queue):
    with queue() as con:
        con.execute("update outbound_receipt set turn_id='queue:10',seq=1 where id=1")
        con.execute("insert into outbound_queue(id,idem_key,platform,account_id,chat_id,text_hash,state,attempts) values(10,'op','wechat','a','room',?,'sent',1)", (world.text_hash("same"),))
    assert confirm(1, queue_id=10, attempt_no=1) == 1
    with queue() as con:
        assert con.execute("select ack from outbound_queue where id=10").fetchone()[0] == "confirmed"
    with pytest.raises(ValueError, match="queue_mismatch"):
        confirm(2, mid="8", queue_id=10, attempt_no=1)
    with queue() as con:
        assert con.execute("select ack from outbound_receipt where id=2").fetchone()[0] == "unconfirmed"


def test_desk_callback_commits_bound_local_observation(queue, tmp_path, monkeypatch):
    from loop import desk
    path = tmp_path / "account.db"
    with sqlite3.connect(path) as con:
        con.execute("create table messages(id integer,chat_id text,is_self integer,content text, sender_id text generated always as (case when is_self=1 then 'self' else 'other' end) virtual)")

    from core.execution.local_text_receipt import capture
    start,token=capture(path)
    with sqlite3.connect(path) as con:
        con.execute("insert into messages values(7,'room',1,'same')")
    with queue() as con:
        con.execute("update outbound_receipt set turn_id='queue:10',seq=1 where id=1")
        con.execute("insert into outbound_queue(id,idem_key,platform,account_id,chat_id,text_hash,state,attempts) values(10,'op','wechat','a','room',?,'sent',1)", (world.text_hash("same"),))
    class InlineThread:
        def __init__(self, target, **kw):
            self.target = target
        def start(self):
            self.target()
    monkeypatch.setattr(desk.threading, "Thread", InlineThread)
    monkeypatch.setattr(desk.time, "sleep", lambda *a: None)
    monkeypatch.setattr(desk, "_CONFIRM_DELAYS", (0,))
    monkeypatch.setattr(desk, "_log", lambda *a, **kw: None)
    monkeypatch.setattr(desk, "_mark_started", lambda *a: None)
    monkeypatch.setattr(desk, "_mark_finished", lambda *a: None)
    monkeypatch.setattr(desk, "_mark_pending_start", lambda *a: None)
    desk._confirm_queue_later(10, "room", "same", receipt_id=1, account_id="a", path=path, baseline=start, attempt_no=1, source_token=token)
    with queue() as con:
        assert tuple(con.execute("select ack,msg_id from outbound_receipt where id=1").fetchone()) == ("confirmed", "7")
        assert tuple(con.execute("select ack,why from outbound_queue where id=10").fetchone()) == ("confirmed", "msg 7")


@pytest.mark.parametrize("ok", [True, False])
def test_late_settle_and_timeout_preserve_bound_confirmation(queue, monkeypatch, ok):
    with queue() as con:
        con.execute("update outbound_receipt set turn_id='queue:10',seq=1 where id=1")
        con.execute("insert into outbound_queue(id,idem_key,platform,account_id,chat_id,text_hash,state,attempts) values(10,'op','wechat','a','room',?,'sending',1)", (world.text_hash("same"),))
        con.execute("insert into outbound_attempt(queue_id,attempt_no,result) values(10,1,'running')")
    with queue() as con:
        con.execute("update outbound_queue set owner='worker' where id=10")
        con.execute("update outbound_attempt set owner='worker' where queue_id=10")
    monkeypatch.setattr(world, "_open", queue)
    assert confirm(1, queue_id=10, attempt_no=1) == 1
    assert world.mark_confirmation_unknown(10, "timeout", attempt_no=1) == 0
    state = world.settle(10, ok, owner="worker", attempt_no=1, ack="pending", why="late result", now=1000)
    assert state == ("sent" if ok else "unverified")
    with queue() as con:
        assert tuple(con.execute("select ack,why from outbound_queue where id=10").fetchone()) == ("confirmed", "msg 7")
        assert con.execute("select ack from outbound_attempt where queue_id=10").fetchone()[0] == "confirmed"
    assert world.mark_confirmation_unknown(10, "restart", attempt_no=1) == 0


def test_timeout_before_confirmation_can_be_corrected(queue):
    with queue() as con:
        con.execute("update outbound_receipt set turn_id='queue:10',seq=1 where id=1")
        con.execute("insert into outbound_queue(id,idem_key,platform,account_id,chat_id,text_hash,state,attempts) values(10,'op','wechat','a','room',?,'sent',1)", (world.text_hash("same"),))
        assert world.mark_confirmation_unknown(10, "timeout", attempt_no=1, connection=con) == 1
    assert confirm(1, queue_id=10, attempt_no=1) == 1
    with queue() as con:
        assert con.execute("select ack from outbound_queue where id=10").fetchone()[0] == "confirmed"


def test_observation_survives_connection_restart_and_rejects_rewrite(queue, monkeypatch):
    monkeypatch.setattr(world, "_open", queue)
    with queue() as con:
        con.execute("insert into outbound_queue(id,idem_key,platform,account_id,chat_id,text_hash,state,attempts) values(10,'op','wechat','a','room',?,'sending',1)", (world.text_hash("same"),))
        con.execute("update outbound_receipt set turn_id='queue:10',seq=1 where id=1")
    with queue() as con:
        con.execute("update outbound_queue set owner='worker' where id=10")
        con.execute("insert into outbound_attempt(queue_id,attempt_no,owner,result) values(10,1,'worker','running')")
    world.save_outbound_observation(10, 42, owner="worker", attempt_no=1)
    world.save_outbound_observation(10, 42, owner="worker", attempt_no=1)
    with pytest.raises(ValueError, match="snapshot_conflict"):
        world.save_outbound_observation(10, 99, owner="worker", attempt_no=1)
    world.bind_observation_receipt(10, 1, owner="worker", attempt_no=1)
    world.bind_observation_receipt(10, 1, owner="worker", attempt_no=1)
    with queue() as reopened:
        row = reopened.execute("select platform,account_id,chat_id,baseline,receipt_id from outbound_observation where queue_id=10").fetchone()
        assert tuple(row) == ("wechat", "a", "room", 42, 1)
    with pytest.raises(ValueError, match="receipt_mismatch"):
        world.bind_observation_receipt(10, 3, owner="worker", attempt_no=1)


def test_observation_rejects_unclaimed_or_source_unknown_queue(queue, monkeypatch):
    monkeypatch.setattr(world, "_open", queue)
    with queue() as con:
        con.execute("insert into outbound_queue(id,idem_key,chat_id,state) values(10,'op','room','pending')")
    with pytest.raises(ValueError, match="source_missing"):
        world.save_outbound_observation(10, 0, owner="worker", attempt_no=1)
    with queue() as con:
        assert con.execute("select count(*) from outbound_observation").fetchone()[0] == 0


def test_restart_recovery_reuses_saved_cursor_and_never_sends(queue, tmp_path, monkeypatch):
    from loop import desk
    monkeypatch.setattr(world, "_open", queue)
    monkeypatch.setattr(world, "_read_conn", queue)
    with queue() as con:
        con.execute("update outbound_receipt set turn_id='queue:10',seq=1 where id=1")
        con.execute("insert into outbound_queue(id,idem_key,platform,account_id,chat_id,text_hash,state,attempts) values(10,'op','wechat','a','room',?,'sending',1)", (world.text_hash("same"),))
    with queue() as con:
        con.execute("update outbound_queue set owner='worker' where id=10")
        con.execute("insert into outbound_attempt(queue_id,attempt_no,owner,result) values(10,1,'worker','running')")
    path = tmp_path / "data/platforms/wechat/a/account.db"
    path.parent.mkdir(parents=True)
    with sqlite3.connect(path) as con:
        con.execute("create table messages(id integer,chat_id text,is_self integer,content text, sender_id text generated always as (case when is_self=1 then 'self' else 'other' end) virtual)")
        con.execute("insert into messages values(6,'room',1,'same')")
    from core.execution.local_text_receipt import capture
    start,token=capture(path)
    world.save_outbound_observation(10,start,owner='worker',attempt_no=1,source_token=token)
    world.bind_observation_receipt(10,1,owner='worker',attempt_no=1)
    with queue() as con:
        con.execute("update outbound_queue set state='sent' where id=10")
    with sqlite3.connect(path) as con:
        con.execute("insert into messages values(7,'room',1,'same')")
    class InlineThread:
        def __init__(self, target, **kw):
            self.target = target
        def start(self):
            self.target()
    monkeypatch.setattr(desk, "ROOT", str(tmp_path))
    monkeypatch.setattr(desk, "_RUNNING", {})
    monkeypatch.setattr(desk, "_PENDING_START", {})
    monkeypatch.setattr(desk.threading, "Thread", InlineThread)
    monkeypatch.setattr(desk.time, "sleep", lambda *a: None)
    monkeypatch.setattr(desk, "_CONFIRM_DELAYS", (0,))
    monkeypatch.setattr(desk, "_log", lambda *a, **kw: None)
    calls = []
    monkeypatch.setattr(desk, "_sender", lambda: calls.append("send"))
    assert desk.recover_observations() == {"resumed": 1, "unavailable": 0, "expired": 0}
    assert desk.recover_observations()["resumed"] == 0
    assert calls == []
    with queue() as con:
        assert tuple(con.execute("select state,ack,attempts from outbound_queue where id=10").fetchone()) == ("sent", "confirmed", 1)
        assert con.execute("select msg_id from outbound_receipt where id=1").fetchone()[0] == "7"


def test_changed_attempt_cannot_receive_old_evidence(queue):
    with queue() as con:
        con.execute("update outbound_receipt set turn_id='queue:10',seq=1 where id=1")
        con.execute("insert into outbound_queue(id,idem_key,platform,account_id,chat_id,text_hash,state,attempts) values(10,'op','wechat','a','room',?,'sent',2)", (world.text_hash("same"),))
    with pytest.raises(ValueError, match="attempt_changed"):
        confirm(1, queue_id=10, attempt_no=1)
    with queue() as con:
        assert con.execute("select ack from outbound_receipt where id=1").fetchone()[0] == "unconfirmed"


@pytest.mark.parametrize("repair", ["backfill", "settle_stuck"])
def test_backfill_uses_same_bound_observation_and_atomic_commit(queue, tmp_path, monkeypatch, repair):
    from loop import receipts
    monkeypatch.setattr(world, "_open", queue)
    with queue() as con:
        con.execute("update outbound_receipt set turn_id='queue:10',seq=1 where id=1")
        con.execute("insert into outbound_queue(id,idem_key,platform,account_id,chat_id,text_hash,state,attempts) values(10,'op','wechat','a','room',?,'sending',1)", (world.text_hash("same"),))
    with queue() as con:
        con.execute("update outbound_queue set owner='worker' where id=10")
        con.execute("insert into outbound_attempt(queue_id,attempt_no,owner,result) values(10,1,'worker','running')")
    path = tmp_path / "data/platforms/wechat/a/account.db"
    path.parent.mkdir(parents=True)
    with sqlite3.connect(path) as con:
        con.execute("create table messages(id integer,chat_id text,is_self integer,content text, sender_id text generated always as (case when is_self=1 then 'self' else 'other' end) virtual)")
        con.execute("insert into messages values(6,'room',1,'same')")
    from core.execution.local_text_receipt import capture
    start,token=capture(path)
    world.save_outbound_observation(10,start,owner='worker',attempt_no=1,source_token=token)
    world.bind_observation_receipt(10,1,owner='worker',attempt_no=1)
    with queue() as con:
        con.execute("update outbound_queue set state='sent' where id=10")
    with sqlite3.connect(path) as con:
        con.execute("insert into messages values(7,'room',1,'same')")
    with queue() as con:
        dry = receipts.backfill(conn=con, root=tmp_path, dry_run=True)
        assert dry["fixed"] == [{"queue_id": 10, "msg_id": "7"}]
        assert con.execute("select ack from outbound_queue where id=10").fetchone()[0] != "confirmed"
        if repair == "backfill":
            actual = receipts.backfill(conn=con, root=tmp_path, dry_run=False)
            assert actual["fixed"] == dry["fixed"]
        else:
            called = []
            actual = receipts.settle_stuck(10, conn=con, root=tmp_path,
                confirm=lambda *a, **kw: called.append("override"), enqueue=lambda *a, **kw: called.append("send"))
            assert actual["ok"] is True and actual["msg_id"] == "7"
            assert called == []
        assert con.execute("select ack from outbound_queue where id=10").fetchone()[0] == "confirmed"
        assert con.execute("select ack from outbound_receipt where id=1").fetchone()[0] == "confirmed"
        assert receipts.backfill(conn=con, root=tmp_path, dry_run=False)["fixed"] == []


@pytest.mark.parametrize("confirm_first", [True,False])
def test_reap_stale_keeps_bound_observation_in_either_order(queue,confirm_first):
    with queue() as con:
        con.execute("update outbound_receipt set turn_id='queue:10',seq=1 where id=1")
        con.execute("insert into outbound_queue(id,idem_key,platform,account_id,chat_id,text_hash,state,attempts,heartbeat_at) values(10,'reap','wechat','a','room',?,'sending',1,1)",(world.text_hash("same"),))
        con.execute("insert into outbound_attempt(queue_id,attempt_no,result) values(10,1,'running')")
    if confirm_first:
        assert confirm(1,queue_id=10,attempt_no=1)==1
    assert world.reap_stale(now=1000)==1
    if not confirm_first:
        assert confirm(1,queue_id=10,attempt_no=1)==1
    with queue() as con:
        row=con.execute("select state,ack,why,attempts from outbound_queue where id=10").fetchone()
        assert tuple(row)==("unverified","confirmed","msg 7",1)
        assert con.execute("select result from outbound_attempt where queue_id=10").fetchone()[0]=="aborted"
    assert world.reap_stale(now=1001)==0


def test_reap_does_not_preserve_ack_without_matching_bound_receipt(queue):
    with queue() as con:
        con.execute("insert into outbound_queue(id,idem_key,platform,account_id,chat_id,text_hash,state,ack,why,attempts,heartbeat_at) values(10,'fake','wechat','a','room',?,'sending','confirmed','msg 7',1,1)",(world.text_hash("same"),))
    assert world.reap_stale(now=1000)==1
    with queue() as con:
        row=con.execute("select state,ack from outbound_queue where id=10").fetchone()
        assert tuple(row)==("unverified","unconfirmed")


def test_reap_starts_write_transaction_before_reading_candidates(queue,monkeypatch):
    statements=[]
    @contextlib.contextmanager
    def traced():
        with queue() as con:
            con.set_trace_callback(statements.append)
            yield con
    monkeypatch.setattr(world,"_open",traced)
    assert world.reap_stale(now=1000)==0
    assert statements[0].lower()=="begin immediate"
    assert "select * from outbound_queue" in statements[1].lower()


@pytest.mark.parametrize("stale_owner,stale_attempt", [("old",1),("new",1),("old",2)])
def test_settle_rejects_old_claim_without_touching_new_attempt(queue,stale_owner,stale_attempt):
    with queue() as con:
        con.execute("insert into outbound_queue(id,idem_key,platform,chat_id,state,not_after,max_attempts) values(10,'claim','wechat','room','pending',9999,3)")
    first=world.claim_next(owner="old",now=100)
    assert world.settle(10,False,owner="old",attempt_no=first["attempts"],not_executed=True,now=101)=="pending"
    current=world.claim_next(owner="new",now=102)
    assert current["attempts"]==2
    with queue() as con:
        before=[tuple(r) for r in con.execute("select * from outbound_queue")]
        attempts=[tuple(r) for r in con.execute("select * from outbound_attempt order by attempt_no")]
    with pytest.raises(ValueError,match="settle_claim_mismatch"):
        world.settle(10,True,owner=stale_owner,attempt_no=stale_attempt,now=103)
    with queue() as con:
        assert [tuple(r) for r in con.execute("select * from outbound_queue")]==before
        assert [tuple(r) for r in con.execute("select * from outbound_attempt order by attempt_no")]==attempts
    assert world.settle(10,True,owner="new",attempt_no=2,ack="unconfirmed",now=104)=="sent"
    with pytest.raises(ValueError,match="settle_claim_mismatch"):
        world.settle(10,False,owner="new",attempt_no=2,not_executed=True,now=105)


def test_settle_requires_claim_and_running_attempt(queue):
    with pytest.raises(ValueError,match="settle_claim_missing"):
        world.settle(10,True)
    with queue() as con:
        con.execute("insert into outbound_queue(id,idem_key,chat_id,state,owner,attempts) values(10,'broken','room','sending','worker',1)")
    with pytest.raises(ValueError,match="settle_attempt_missing"):
        world.settle(10,True,owner="worker",attempt_no=1)
    with queue() as con:
        assert con.execute("select state from outbound_queue where id=10").fetchone()[0]=="sending"


def test_desk_execute_passes_current_claim_to_real_settle(queue,monkeypatch):
    from types import SimpleNamespace
    from loop import desk
    with queue() as con:
        con.execute("insert into outbound_queue(id,idem_key,platform,chat_id,state,not_after) values(10,'desk','wechat','room','pending',9999)")
    claimed=world.claim_next(owner="worker",now=100)
    monkeypatch.setattr(world,"claim_next",lambda **kw:claimed)
    monkeypatch.setattr(desk.threading,"Thread",lambda **kw:SimpleNamespace(start=lambda:None))
    monkeypatch.setattr(desk,"deliver",lambda *a,**kw:(True,"unconfirmed",""))
    monkeypatch.setattr(desk,"_log",lambda *a,**kw:None)
    monkeypatch.setattr(desk._residue,"take_terminal",lambda qid:False)
    result=desk.execute_one(owner="worker")
    assert result["state"]=="sent"
    with queue() as con:
        assert con.execute("select result from outbound_attempt where queue_id=10").fetchone()[0]=="sent"


@pytest.mark.parametrize("state,owner,attempt,expected", [("sending","worker",1,True),("sending","other",1,False),("sending","worker",2,False),("sent","worker",1,False),("unverified","worker",1,False)])
def test_heartbeat_only_renews_current_running_claim(queue,state,owner,attempt,expected):
    with queue() as con:
        con.execute("insert into outbound_queue(id,idem_key,platform,chat_id,state,not_after) values(10,'beat','wechat','room','pending',9999)")
    world.claim_next(owner="worker",now=100)
    with queue() as con:
        con.execute("update outbound_queue set state=? where id=10",(state,))
    assert world.heartbeat(10,owner,attempt_no=attempt,now=200) is expected
    with queue() as con:
        assert con.execute("select heartbeat_at from outbound_queue where id=10").fetchone()[0]==(200 if expected else 100)


def test_heartbeat_rejects_missing_claim_and_nonrunning_attempt(queue):
    with pytest.raises(ValueError,match="heartbeat_claim_missing"):
        world.heartbeat(10,"worker")
    with queue() as con:
        con.execute("insert into outbound_queue(id,idem_key,chat_id,state,owner,attempts,heartbeat_at) values(10,'beat-broken','room','sending','worker',1,100)")
    assert world.heartbeat(10,"worker",attempt_no=1,now=200) is False



def test_expiry_only_changes_pending_and_notifies_after_commit(queue,monkeypatch):
    with queue() as con:
        con.execute("insert into outbound_queue(id,idem_key,group_id,seq,platform,chat_id,state,not_after) values(10,'ttl-running','g',0,'wechat','room','pending',200)")
    world.claim_next(owner="worker",now=100)
    with queue() as con:
        for qid,group,seq,deadline in [(11,'g',1,200),(12,'g',2,9999),(13,'h',0,200)]:
            con.execute("insert into outbound_queue(id,idem_key,group_id,seq,platform,chat_id,state,not_after) values(?,?,?,?, 'wechat','room','pending',?)",(qid,str(qid),group,seq,deadline))
    notices,errors=[],[]
    monkeypatch.setattr(world,"warn",lambda event,**kw:errors.append((event,kw)))
    def notify(qid,*args):
        with queue() as con:
            con.execute("begin immediate")
            assert con.execute("select state from outbound_queue where id=?",(qid,)).fetchone()[0]=='expired'
        notices.append(qid)
        if qid==11:
            raise RuntimeError("notification offline")
    assert world.expire(now=300,on_expired=notify)==3
    assert notices==[11,13]
    assert errors[0][0]=='queue_expiry_notification_failed'
    with queue() as con:
        assert dict(con.execute("select id,state from outbound_queue"))=={10:'sending',11:'expired',12:'skipped',13:'expired'}
    assert world.settle(10,True,owner='worker',attempt_no=1,ack='unconfirmed',now=301)=='sent'
    assert world.expire(now=302,on_expired=notify)==0



def test_dry_queue_completion_does_not_become_sent_experience(queue,monkeypatch):
    from types import SimpleNamespace
    from loop import desk,delivery_note
    with queue() as con:
        con.execute("insert into outbound_queue(id,idem_key,platform,chat_id,state,not_after) values(10,'dry-queue','wechat','room','pending',9999)")
    claimed=world.claim_next(owner='dry-worker',now=100)
    monkeypatch.setattr(world,'claim_next',lambda **kw:claimed)
    monkeypatch.setattr(desk.threading,'Thread',lambda **kw:SimpleNamespace(start=lambda:None))
    monkeypatch.setattr(desk,'_bootstrap',lambda:None)
    monkeypatch.setattr(desk,'_label',lambda chat:chat)
    monkeypatch.setattr(desk,'_log',lambda *a,**kw:None)
    monkeypatch.setattr(desk._residue,'take_terminal',lambda qid:False)
    def forbidden():
        raise AssertionError('dry must not obtain sender')
    monkeypatch.setattr(desk,'_sender',forbidden)
    assert desk.execute_one(dry=True,owner='dry-worker')['state']=='skipped'
    with queue() as con:
        assert tuple(con.execute("select state,ack,settled_ts>0 from outbound_queue where id=10").fetchone())==('skipped','dry_run',1)
        assert con.execute("select result from outbound_attempt where queue_id=10").fetchone()[0]=='dry_run'
        assert con.execute("select count(*) from outbound_receipt where turn_id='queue:10'").fetchone()[0]==0
    for state in ('sent','skipped'):
        assert delivery_note._one(dict(state=state,ack='dry_run',created_ts=1))==''



def test_late_timeout_cannot_downgrade_new_or_historical_attempt(queue):
    with queue() as con:
        con.execute("insert into outbound_queue(id,idem_key,chat_id,state,attempts,ack) values(10,'timeout-scope','room','sent',2,'pending')")
        for attempt in (1,2):
            con.execute("insert into outbound_attempt(queue_id,attempt_no,ack,result) values(10,?,'pending','running')",(attempt,))
    assert world.mark_confirmation_unknown(10,'old timeout',attempt_no=1)==0
    with queue() as con:
        assert con.execute("select ack from outbound_queue where id=10").fetchone()[0]=='pending'
    assert world.mark_confirmation_unknown(10,'current timeout',attempt_no=2)==1
    with queue() as con:
        assert [tuple(r) for r in con.execute("select attempt_no,ack from outbound_attempt where queue_id=10 order by attempt_no")]==[(1,'pending'),(2,'unconfirmed')]



def test_snapshot_and_receipt_binding_reject_late_worker_and_old_receipt(queue):
    with queue() as con:
        con.execute("insert into outbound_queue(id,idem_key,platform,account_id,chat_id,text_hash,state,not_after,max_attempts) values(10,'bind-claim','wechat','a','room',?,'pending',9999,3)",(world.text_hash('same'),))
        con.execute("update outbound_receipt set turn_id='queue:10',seq=1 where id=1")
        con.execute("update outbound_receipt set turn_id='queue:10',seq=2 where id=2")
    world.claim_next(owner='old',now=100)
    world.save_outbound_observation(10,4,owner='old',attempt_no=1)
    assert world.settle(10,False,owner='old',attempt_no=1,not_executed=True,now=101)=='pending'
    world.claim_next(owner='new',now=102)
    with pytest.raises(ValueError,match='claim_mismatch'):
        world.save_outbound_observation(10,8,owner='old',attempt_no=1)
    world.save_outbound_observation(10,9,owner='new',attempt_no=2)
    with pytest.raises(ValueError,match='claim_mismatch'):
        world.bind_observation_receipt(10,1,owner='old',attempt_no=1)
    with pytest.raises(ValueError,match='receipt_mismatch'):
        world.bind_observation_receipt(10,1,owner='new',attempt_no=2)
    world.bind_observation_receipt(10,2,owner='new',attempt_no=2)
    with queue() as con:
        assert [tuple(r) for r in con.execute("select attempt_no,baseline,receipt_id from outbound_observation where queue_id=10 order by attempt_no")]==[(1,4,0),(2,9,2)]
    with pytest.raises(ValueError,match='receipt_attempt_mismatch'):
        confirm(1,queue_id=10,attempt_no=2)
    with pytest.raises(ValueError,match='attempt_missing'):
        confirm(2,queue_id=10)
    assert confirm(2,queue_id=10,attempt_no=2)==1



@pytest.mark.parametrize("fault", [None,"residue_reset","record_raise","record_zero","bind_observation_receipt","schedule_observation"])
def test_desk_retry_records_and_binds_its_own_receipt_sequence(queue,tmp_path,monkeypatch,fault):
    from types import SimpleNamespace
    from loop import desk
    from core import features
    from core.execution import wx_sender,chat_alias
    path=tmp_path/'data/platforms/wechat/a/account.db'
    path.parent.mkdir(parents=True)
    with sqlite3.connect(path) as con:
        con.execute("create table messages(id integer,chat_id text,is_self integer,content text, sender_id text generated always as (case when is_self=1 then 'self' else 'other' end) virtual)")
    with queue() as con:
        con.execute("insert into outbound_queue(id,idem_key,platform,account_id,chat_id,text_hash,payload,state,attempts,not_after,max_attempts) values(10,'desk-attempt','wechat','a','room',?,?,'pending',1,9999,3)",(world.text_hash('same'),'{"text":"same"}'))
        con.execute("insert into outbound_attempt(queue_id,attempt_no,owner,result) values(10,1,'old','not_executed')")
        con.execute("update outbound_receipt set turn_id='queue:10',seq=1 where id=1")
    claimed=world.claim_next(owner='new',now=100)
    assert claimed['attempts']==2
    monkeypatch.setattr(desk,'ROOT',str(tmp_path))
    monkeypatch.setattr(desk,'_bootstrap',lambda:None)
    monkeypatch.setattr(desk,'_label',lambda chat:chat)
    monkeypatch.setattr(desk,'_dry',lambda chat:False)
    monkeypatch.setattr(desk,'_log',lambda *a,**kw:None)
    monkeypatch.setattr(desk._residue,'reset',lambda qid:None)
    monkeypatch.setattr(wx_sender,'account_send_options',lambda *a,**kw:{})
    monkeypatch.setattr(chat_alias,'key_of',lambda chat:chat)
    monkeypatch.setattr(features,'dry_run_send',lambda *a:False)
    sends,observers=[],[]
    def send(*a,**kw):
        sends.append(a)
        return {'ok':True,'ack':'unconfirmed'}
    monkeypatch.setattr(desk,'_sender',lambda:SimpleNamespace(send_text=send))
    monkeypatch.setattr(desk,'_confirm_queue_later',lambda *a,**kw:observers.append(kw))
    logs=[]
    monkeypatch.setattr(desk,'_log',lambda event,**kw:logs.append((event,kw)))
    def boom(*a,**kw):
        raise RuntimeError('injected post-send failure')
    if fault=='residue_reset':
        monkeypatch.setattr(desk._residue,'reset',boom)
    elif fault=='record_raise':
        monkeypatch.setattr(world,'record_outbound',boom)
    elif fault=='record_zero':
        monkeypatch.setattr(world,'record_outbound',lambda *a,**kw:0)
    elif fault=='bind_observation_receipt':
        monkeypatch.setattr(world,'bind_observation_receipt',boom)
    elif fault=='schedule_observation':
        monkeypatch.setattr(desk,'_confirm_queue_later',boom)
    assert desk.deliver(claimed)==(True,'unconfirmed','')
    assert len(sends)==1
    if fault:
        assert any(event in ('desk_post_send_failed','desk_post_send_receipt_missing') for event,_ in logs)
    if fault in ('record_raise','record_zero','bind_observation_receipt'):
        assert observers==[]
        with queue() as con:
            assert con.execute('select receipt_id from outbound_observation where queue_id=10 and attempt_no=2').fetchone()[0]==0
        return
    if fault=='schedule_observation':
        assert observers==[]
        return
    assert len(sends)==len(observers)==1
    assert observers[0]['attempt_no']==2
    with queue() as con:
        receipt=con.execute("select receipt_id from outbound_observation where queue_id=10 and attempt_no=2").fetchone()[0]
        assert receipt != 1
        assert tuple(con.execute("select turn_id,seq from outbound_receipt where id=?",(receipt,)).fetchone())==('queue:10',2)



def test_confirm_thread_start_failure_clears_tracking(monkeypatch):
    from loop import desk,owner_copy
    monkeypatch.setattr(desk,'_PENDING_START',{})
    monkeypatch.setattr(desk,'_RUNNING',{})
    monkeypatch.setattr(desk,'_log',lambda *a,**kw:None)
    monkeypatch.setattr(owner_copy,'note',lambda *a,**kw:None)
    class BrokenThread:
        def __init__(self,**kw):
            pass
        def start(self):
            raise RuntimeError('thread unavailable')
    monkeypatch.setattr(desk.threading,'Thread',BrokenThread)
    desk._confirm_queue_later(10,'room','same',receipt_id=1,account_id='a',path='unused',baseline=0,attempt_no=1)
    assert desk._PENDING_START==desk._RUNNING=={}



@pytest.mark.parametrize('fault,expected', [('heartbeat','pending'),('deliver','unverified'),('malformed','unverified')])
def test_execute_boundary_failures_always_settle_without_unsafe_retry(queue,monkeypatch,fault,expected):
    from loop import desk
    with queue() as con:
        con.execute("insert into outbound_queue(id,idem_key,platform,chat_id,state,not_after,max_attempts) values(10,'boundary','wechat','room','pending',9999,3)")
    claimed=world.claim_next(owner='worker',now=100)
    monkeypatch.setattr(world,'claim_next',lambda **kw:claimed)
    monkeypatch.setattr(desk,'_log',lambda *a,**kw:None)
    monkeypatch.setattr(desk._residue,'take_terminal',lambda qid:False)
    class Thread:
        def __init__(self,**kw):
            pass
        def start(self):
            if fault=='heartbeat':
                raise RuntimeError('thread unavailable')
    monkeypatch.setattr(desk.threading,'Thread',Thread)
    calls=[]
    def deliver(*a,**kw):
        calls.append('called')
        if fault=='deliver':
            raise RuntimeError('response lost')
        return object()
    monkeypatch.setattr(desk,'deliver',deliver)
    assert desk.execute_one(owner='worker')['state']==expected
    assert len(calls)==(0 if fault=='heartbeat' else 1)
    with queue() as con:
        assert con.execute('select result from outbound_attempt where queue_id=10').fetchone()[0]==('not_executed' if fault=='heartbeat' else 'uncertain')



@pytest.mark.parametrize('change',[{'text':'sa me'},{'account_id':'b'},{'platform':'qq'},{'chat_id':'elsewhere@chatroom'}])
def test_record_outbound_exact_retry_returns_original_and_conflicts_do_not_write(queue,monkeypatch,change):
    monkeypatch.setattr(world,'warn',lambda *a,**kw:None)
    kwargs=dict(chat_id='room@chatroom',text='same',platform='wechat',account_id='a',turn_id='idem',seq=1,ack='unconfirmed')
    first=world.record_outbound(**kwargs)
    assert first>0
    assert world.record_outbound(**dict(kwargs,ack='pending'))==first
    with queue() as con:
        before=[tuple(r) for r in con.execute('select * from outbound_receipt order by id')]
    assert world.record_outbound(**dict(kwargs,**change))==0
    with queue() as con:
        assert [tuple(r) for r in con.execute('select * from outbound_receipt order by id')]==before
    next_id=world.record_outbound(**dict(kwargs,seq=None))
    assert next_id>first


def test_repeated_confirmation_repairs_only_missing_queue_state(queue):
    with queue() as con:
        con.execute("update outbound_receipt set turn_id='queue:10',seq=1 where id=1")
        con.execute("insert into outbound_queue(id,idem_key,platform,account_id,chat_id,text_hash,state,attempts,ack) values(10,'idem-confirm','wechat','a','room',?,'sent',1,'pending')",(world.text_hash('same'),))
    assert confirm(1)==1
    with queue() as con:
        before=tuple(con.execute('select * from outbound_receipt where id=1').fetchone())
    assert confirm(1,queue_id=10,attempt_no=1)==1
    assert confirm(1,queue_id=10,attempt_no=1)==0
    with queue() as con:
        assert tuple(con.execute('select * from outbound_receipt where id=1').fetchone())==before
        assert tuple(con.execute('select ack,why from outbound_queue where id=10').fetchone())==('confirmed','msg 7')
        con.execute("update outbound_queue set why='msg other' where id=10")
    with pytest.raises(ValueError,match='queue_conflict'):
        confirm(1,queue_id=10,attempt_no=1)
    with queue() as con:
        assert con.execute('select why from outbound_queue where id=10').fetchone()[0]=='msg other'



def test_missing_receipt_is_not_reported_as_idempotent_confirmation(queue):
    with queue() as con:
        before=[tuple(r) for r in con.execute('select * from outbound_receipt order by id')]
    with pytest.raises(ValueError,match='receipt_missing'):
        confirm(999)
    with queue() as con:
        assert [tuple(r) for r in con.execute('select * from outbound_receipt order by id')]==before



def test_source_snapshot_persists_immutably_and_legacy_is_not_upgraded(queue,tmp_path):
    from core.execution.local_text_receipt import capture
    path=tmp_path/'source.db'
    with sqlite3.connect(path) as c:
        c.execute('create table messages(id integer,chat_id text,content text,sender_id text)')
    start,token=capture(path)
    with queue() as c:
        for qid in (10,20):
            c.execute("insert into outbound_queue(id,idem_key,platform,account_id,chat_id,state,not_after) values(?,?,'wechat','a','room','pending',9999)",(qid,str(qid)))
    world.claim_next(owner='worker',now=100)
    world.save_outbound_observation(10,start,owner='worker',attempt_no=1,source_token=token)
    world.save_outbound_observation(10,start,owner='worker',attempt_no=1,source_token=token)
    with pytest.raises(ValueError,match='source_conflict'):
        world.save_outbound_observation(10,start,owner='worker',attempt_no=1,source_token='different')
    world.claim_next(owner='worker',now=101)
    world.save_outbound_observation(20,start,owner='worker',attempt_no=1)
    with pytest.raises(ValueError,match='legacy_source_missing'):
        world.save_outbound_observation(20,start,owner='worker',attempt_no=1,source_token=token)
    with queue() as c:
        assert [tuple(r) for r in c.execute('select queue_id,source_token from outbound_observation_source')]==[(10,token)]


def test_trial_enqueue_reuses_original_deadline_and_cannot_be_claimed_after_it(queue):
    import time

    deadline = time.time() + 60
    args = ("owner-trial:inbound", "room", [{"kind": "text", "text": "收到"}])
    first = world.enqueue(*args, account_id="a", max_attempts=1, expires_at=deadline)
    repeated = world.enqueue(*args, account_id="a", max_attempts=1, expires_at=deadline + 60)
    assert world.enqueue("owner-trial:second", args[1], args[2], account_id="a",
                         max_attempts=1, expires_at=deadline) == []
    assert first == repeated
    with queue() as con:
        assert [tuple(r) for r in con.execute("select id,not_after,max_attempts from outbound_queue")] == [
            (first[0], deadline, 1),
        ]
    assert world.claim_next(owner="tester", now=deadline) is None


@pytest.mark.parametrize("stop_marker", [True, False])
def test_desk_rechecks_trial_stop_after_claim(tmp_path, monkeypatch, stop_marker):
    import time
    from types import SimpleNamespace
    from core.execution import local_text_receipt, wx_sender
    from loop import desk
    from scripts import observe_owner_shadow

    monkeypatch.setattr(observe_owner_shadow, "ROOT", tmp_path)
    (tmp_path / "logs").mkdir()
    if stop_marker:
        (tmp_path / "logs" / "owner-reply-trial.window").write_text("active", encoding="utf-8")
        (tmp_path / "logs" / "owner-reply-trial.stop").touch()
    monkeypatch.setattr(desk, "_bootstrap", lambda: None)
    monkeypatch.setattr(desk, "_label", lambda chat: chat)
    monkeypatch.setattr(desk, "_dry", lambda chat: False)
    monkeypatch.setattr(local_text_receipt, "account_path", lambda *args: None)
    monkeypatch.setattr(wx_sender, "account_send_options", lambda *args, **kwargs: {})

    def forbidden(*args, **kwargs):
        pytest.fail("A claimed trial item reached the sender after stop")

    monkeypatch.setattr(desk, "_sender", lambda: SimpleNamespace(send_text=forbidden))
    result = desk.deliver({"kind": "text", "chat_id": "owner", "account_id": "a",
                           "payload": {"text": "收到"}, "group_id": "owner-trial:fixture",
                           "not_after": time.time() + 60})
    assert tuple(result) == (False, "blocked", "owner_trial_stopped_or_expired")
