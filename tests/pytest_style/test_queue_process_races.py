"""真实独立进程竞争同一临时 SQLite；无发送器或生产库访问。"""
import json
import time
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest
from db import world_db as world

WORKER = r'''
import contextlib,json,sqlite3,sys,time,os
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from db import world_db as w
path,gate,action,rid,qid,ready=sys.argv[2:]
@contextlib.contextmanager
def connection():
 c=sqlite3.connect(path,timeout=15)
 c.row_factory=sqlite3.Row
 try:
  with c: yield c
 finally: c.close()
w._open=connection
w.conn=connection
Path(ready).touch()
limit=time.monotonic()+15
while not Path(gate).exists():
 if time.monotonic()>limit: raise RuntimeError('gate timeout')
 time.sleep(.01)
try:
 if action=='confirm':
  result=w.confirm_outbound('room','same',receipt_id=int(rid),platform='wechat',account_id='a',msg_id='7',queue_id=int(qid),attempt_no=1)
 elif action=='snapshot': result=w.save_outbound_observation(int(qid),int(rid),owner='worker',attempt_no=1)
 elif action=='record': result=w.record_outbound('room@chatroom','same',platform='wechat',account_id='a',turn_id='record-race',seq=1,ack='unconfirmed')
 else: result=w.reap_stale(now=1000)
 print(json.dumps({'result':result}))
except ValueError as exc: print(json.dumps({'error':str(exc)}))
'''


def seed(path,count=1):
    with sqlite3.connect(path) as c:
        for ddl in world._SCHEMA:
            c.execute(ddl)
        for i in range(1,count+1):
            qid=i*10
            c.execute("insert into outbound_queue(id,idem_key,platform,account_id,chat_id,text_hash,state,ack,attempts,owner,heartbeat_at) values(?,?,'wechat','a','room',?,'sending','pending',1,'worker',1)",(qid,str(qid),world.text_hash('same')))
            c.execute("insert into outbound_attempt(queue_id,attempt_no,owner,result) values(?,1,'worker','running')",(qid,))
            c.execute("insert into outbound_receipt(id,turn_id,seq,platform,account_id,chat_id,text,text_hash,ack,sent_at_utc) values(?,?,1,'wechat','a','room','same',?,'unconfirmed','2026-11-07T00:00:00')",(i,'queue:'+str(qid),world.text_hash('same')))


def compete(tmp_path,actions):
    path=tmp_path/'world.db'
    script=tmp_path/'worker.py'
    script.write_text(WORKER,encoding='utf-8')
    gate=tmp_path/'go'
    root=Path(__file__).resolve().parents[2]
    processes=[]
    try:
        ready_paths=[]
        for index,(action,rid,qid) in enumerate(actions):
            ready_paths.append(tmp_path/(str(index)+".ready"))
            processes.append(subprocess.Popen([sys.executable,'-I',str(script),str(root),str(path),str(gate),action,str(rid),str(qid),str(ready_paths[-1])],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,encoding='utf-8',errors='replace',creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0)))
        deadline=time.monotonic()+15
        while not all(p.exists() for p in ready_paths):
            assert time.monotonic()<deadline,'workers failed to reach ready barrier'
            assert all(p.poll() is None for p in processes),'worker exited before barrier'
            time.sleep(.01)
        gate.touch()
        results=[]
        for proc in processes:
            out,err=proc.communicate(timeout=30)
            assert proc.returncode==0,(out,err)
            results.append(json.loads(out.strip().splitlines()[-1]))
        return results
    finally:
        for proc in processes:
            if proc.poll() is None:
                proc.kill()
                proc.communicate()


@pytest.mark.parametrize('round_id',range(4))
def test_processes_repeat_same_confirmation_idempotently(tmp_path,round_id):
    seed(tmp_path/'world.db')
    results=compete(tmp_path,[('confirm',1,10),('confirm',1,10)])
    assert sorted(r['result'] for r in results)==[0,1]
    with sqlite3.connect(tmp_path/'world.db') as c:
        assert c.execute("select ack,why from outbound_queue where id=10").fetchone()==('confirmed','msg 7')


@pytest.mark.parametrize('round_id',range(4))
def test_processes_cannot_claim_one_message_for_two_operations(tmp_path,round_id):
    seed(tmp_path/'world.db',count=2)
    results=compete(tmp_path,[('confirm',1,10),('confirm',2,20)])
    assert sum(r.get('result')==1 for r in results)==1
    assert sum(r.get('error')=='outbound_confirmation_evidence_claimed' for r in results)==1
    with sqlite3.connect(tmp_path/'world.db') as c:
        assert c.execute("select count(*) from outbound_receipt where ack='confirmed'").fetchone()[0]==1
        assert c.execute("select count(*) from outbound_queue where ack='confirmed'").fetchone()[0]==1


@pytest.mark.parametrize('round_id',range(4))
def test_process_confirmation_and_reap_keep_both_execution_and_observation(tmp_path,round_id):
    seed(tmp_path/'world.db')
    results=compete(tmp_path,[('confirm',1,10),('reap',0,0)])
    assert all(r.get('result')==1 for r in results)
    with sqlite3.connect(tmp_path/'world.db') as c:
        assert c.execute('select state,ack,why from outbound_queue where id=10').fetchone()==('unverified','confirmed','msg 7')
        assert c.execute('select result from outbound_attempt').fetchone()[0]=='aborted'


@pytest.mark.parametrize('round_id',range(4))
def test_process_snapshot_conflict_keeps_one_immutable_baseline(tmp_path,round_id):
    seed(tmp_path/'world.db')
    results=compete(tmp_path,[('snapshot',4,10),('snapshot',9,10)])
    assert sum('result' in r for r in results)==1
    assert sum(r.get('error')=='observation_snapshot_conflict' for r in results)==1
    with sqlite3.connect(tmp_path/'world.db') as c:
        rows=c.execute('select baseline from outbound_observation where queue_id=10').fetchall()
        assert len(rows)==1 and rows[0][0] in (4,9)


@pytest.mark.parametrize('round_id',range(4))
def test_processes_record_same_operation_return_one_receipt(tmp_path,round_id):
    seed(tmp_path/'world.db')
    results=compete(tmp_path,[('record',0,0),('record',0,0)])
    assert results[0]['result']==results[1]['result']>0
    with sqlite3.connect(tmp_path/'world.db') as c:
        assert c.execute("select count(*) from outbound_receipt where turn_id='record-race'").fetchone()[0]==1
