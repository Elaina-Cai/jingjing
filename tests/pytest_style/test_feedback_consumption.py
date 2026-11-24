from dataclasses import replace
from datetime import datetime
from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

from core.memory import explicit_write as writer
from core.memory.feedback_commit import claim_feedback,finish_feedback,FeedbackConflict
from core.quality import corrections as corr
from core.terminal.event_model import Event,EventType
from core.terminal.principal import VisibilityScope
from db.meta_models import FeedbackSample
from test_explicit_memory_commit import db,counts,event,apply
from test_legacy_read_binding import P,execute


@pytest.fixture()
def case(db,monkeypatch):
    sample=db.feedback.add(source='correction',platform='wechat',bot_account_id='a',chat_id='same',chat_type='private',person_id=P.person_id,sample_text='之前说错了，地址需要更正',created_at=datetime(2026,11,8))
    calls=[]
    original_collect=corr._collect_candidates
    original_extract=corr._extract
    async def model(*a,**kw):
        calls.append(kw)
        return {'action':'upsert','fact':'主人更正后的明确资料','confidence':1}
    monkeypatch.setattr(corr,'_owner_ids',lambda platform:{P.person_id})
    monkeypatch.setattr(corr,'_rate_ok',lambda key:True)
    monkeypatch.setattr(corr,'_lookup_quoted',lambda *a:None)
    monkeypatch.setattr(corr,'_collect_candidates',lambda **kw:[])
    monkeypatch.setattr(corr,'_extract',model)
    def forbidden(*a,**kw):raise AssertionError('separate mark_status must not be used')
    monkeypatch.setattr(db.feedback,'mark_status',forbidden)
    return SimpleNamespace(db=db,sample=sample,meta=SimpleNamespace(memory_projections=db.repo,feedback_samples=db.feedback),calls=calls,original_extract=original_extract,original_collect=original_collect)


def run(case):return execute(SimpleNamespace(execute=lambda **kw:corr.process_pending_corrections(meta_repo=case.meta,llm=object())))


def status(case):
    with Session(case.db.engine) as session:return session.get(FeedbackSample,case.sample.id).status


def feedback_event(claim,value='主人更正后的明确资料'):
    return Event.new(event_type=EventType.MEMORY_WRITE,event_id=claim.event_id,occurred_at=claim.created_at,
        platform='wechat',bot_account_id='a',chat_id='same',actor_person_id=P.person_id,content=value,
        visibility_scope=frozenset({VisibilityScope.OWNER_ONLY}),metadata={'memory_type':'fact','source':'user_correction','explicit_remember':True,'principal':P.as_dict()})


def test_success_commits_event_projection_and_feedback_together(case):
    result=run(case)
    assert result['learned']==1 and status(case)=='learned' and counts(case.db)==(1,1)
    assert run(case)['scanned']==0 and len(case.calls)==1


def test_stale_poll_after_commit_recovers_without_model_or_rewriting(case,monkeypatch):
    assert run(case)['learned']==1
    monkeypatch.setattr(case.db.feedback,'list_pending',lambda **kw:[case.sample])
    result=run(case)
    assert result['recovered']==1 and result['learned']==0
    assert len(case.calls)==1 and counts(case.db)==(1,1)


def test_response_loss_after_commit_is_recovered_after_restart(case,monkeypatch):
    original=writer.commit_explicit
    with monkeypatch.context() as patch:
        def lost(*a,**kw):
            original(*a,**kw)
            raise OSError('lost after commit')
        patch.setattr(writer,'commit_explicit',lost)
        with pytest.raises(OSError):run(case)
    assert status(case)=='learned' and counts(case.db)==(1,1)
    case.db.engine.dispose()
    monkeypatch.setattr(case.db.feedback,'list_pending',lambda **kw:[case.sample])
    assert run(case)['recovered']==1 and len(case.calls)==1


def test_feedback_update_failure_rolls_back_memory_and_can_retry(case):
    with case.db.engine.begin() as conn:
        conn.exec_driver_sql("CREATE TRIGGER reject_feedback BEFORE UPDATE ON feedback_samples WHEN NEW.status='learned' BEGIN SELECT RAISE(ABORT,'injected feedback failure'); END")
    with pytest.raises(IntegrityError):run(case)
    assert counts(case.db)==(0,0) and status(case)=='new'
    with case.db.engine.begin() as conn:conn.exec_driver_sql('DROP TRIGGER reject_feedback')
    assert run(case)['learned']==1 and counts(case.db)==(1,1)


def test_projection_failure_keeps_feedback_pending(case,monkeypatch):
    original=case.db.repo.upsert_in_session
    with monkeypatch.context() as patch:
        def fail(session,item):raise OSError('projection write unavailable')
        patch.setattr(case.db.repo,'upsert_in_session',fail)
        with pytest.raises(OSError):run(case)
    assert counts(case.db)==(0,0) and status(case)=='new'
    assert run(case)['learned']==1


@pytest.mark.parametrize('field,value',[
    ('sample_text','修改过的来源正文'),('metadata_json','{"quote_reply_id":"other"}'),
    ('chat_id','other-chat'),('person_id','other-person'),('status','reviewed'),
])
def test_changed_source_or_late_status_cannot_be_consumed_by_old_interpretation(case,monkeypatch,field,value):
    async def changed(*a,**kw):
        with case.db.engine.begin() as conn:conn.execute(text('UPDATE feedback_samples SET '+field+'=:value WHERE id=:id'),{'value':value,'id':case.sample.id})
        return {'action':'upsert','fact':'过期解释不应写入','confidence':1}
    monkeypatch.setattr(corr,'_extract',changed)
    assert run(case)['conflicts']==1
    assert counts(case.db)==(0,0)
    assert status(case)==('reviewed' if field=='status' else 'new')


def test_late_no_change_result_cannot_downgrade_another_workers_commit(case,monkeypatch):
    claim=claim_feedback(case.sample,case.db.feedback)
    async def late(*a,**kw):
        writer.commit_explicit(feedback_event(claim),store=case.db.store,repository=case.db.repo,feedback=claim)
        return {'action':'none','confidence':1}
    monkeypatch.setattr(corr,'_extract',late)
    assert run(case)['recovered']==1 and status(case)=='learned' and counts(case.db)==(1,1)
    with pytest.raises(FeedbackConflict):finish_feedback(claim,case.db.feedback,'reviewed')


@pytest.mark.parametrize('changed_output',[False,True])
def test_parallel_same_source_has_one_commit_and_no_last_writer_overwrite(case,changed_output):
    claim=claim_feedback(case.sample,case.db.feedback)
    def commit(number):
        receipt={}
        try:
            value=('竞争输出'+str(number)) if changed_output else '并发的相同明确输出'
            writer.commit_explicit(feedback_event(claim,value),store=case.db.store,repository=case.db.repo,feedback=claim,receipt=receipt)
            return receipt['duplicate']
        except writer.ExplicitMemoryConflict:return 'conflict'
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(commit,range(2)))
    assert counts(case.db)==(1,1) and status(case)=='learned'
    assert results.count(False)==1
    assert results.count('conflict' if changed_output else True)==1


@pytest.mark.parametrize('result',[None,{'action':'none','confidence':1},{'action':'forget','confidence':1}])
def test_no_effect_or_failed_model_does_not_create_memory(case,monkeypatch,result):
    async def model(*a,**kw):return result
    monkeypatch.setattr(corr,'_extract',model)
    stats=run(case)
    assert counts(case.db)==(0,0)
    assert status(case)==('new' if result is None else 'reviewed')
    assert stats['deferred' if result is None else 'reviewed']==1


@pytest.mark.parametrize('kind',['unavailable','throws','malformed'])
def test_real_extraction_failure_is_deferred_instead_of_reviewed(case,monkeypatch,kind):
    async def chat(**kw):
        if kind=='throws':raise OSError('model unavailable')
        return 'malformed output'
    llm=None if kind=='unavailable' else SimpleNamespace(chat=chat,parse_json=lambda response,default:None)
    monkeypatch.setattr(corr,'_extract',case.original_extract)
    stats=execute(SimpleNamespace(execute=lambda **kw:corr.process_pending_corrections(meta_repo=case.meta,llm=llm)))
    assert stats['deferred']==1 and status(case)=='new' and counts(case.db)==(0,0)


def test_non_owner_is_cas_skipped_without_model_or_memory(case):
    with case.db.engine.begin() as conn:conn.execute(text("UPDATE feedback_samples SET person_id='ordinary'"))
    stats=run(case)
    assert stats['skipped']==1 and status(case)=='skipped_non_owner'
    assert not case.calls and counts(case.db)==(0,0)


def test_stale_target_version_conflicts_without_consuming_feedback(case,monkeypatch):
    old=apply(case.db,event('old'))
    candidate={'memory_id':old.memory_id,'content':old.content,'full_content':old.content,'visibility':list(old.visibility_scope),'version':writer.projection_version(old)}
    monkeypatch.setattr(corr,'_collect_candidates',lambda **kw:[candidate])
    async def changed(*a,**kw):
        case.db.repo.upsert(replace(old,content='别的写者已更新'))
        return {'action':'upsert','fact':'旧版本不能覆盖当前状态','target_memory_id':old.memory_id,'confidence':1}
    monkeypatch.setattr(corr,'_extract',changed)
    assert run(case)['conflicts']==1 and status(case)=='new'
    assert counts(case.db)==(1,1) and case.db.repo.get(old.memory_id).valid_to is None


def test_source_engine_mismatch_is_rejected_before_model(case,monkeypatch):
    monkeypatch.setattr(case.db.repo,'engine',SimpleNamespace(url=SimpleNamespace(drivername='sqlite',database='another-source.db')))
    monkeypatch.setattr(case.db.repo,'assert_writable',lambda:None)
    assert run(case)['conflicts']==1
    assert not case.calls and status(case)=='new'


def test_recovery_does_not_recreate_a_projection_deleted_after_commit(case,monkeypatch):
    run(case)
    case.db.repo.delete('proj:'+claim_feedback(case.sample,case.db.feedback).event_id)
    monkeypatch.setattr(case.db.feedback,'list_pending',lambda **kw:[case.sample])
    assert run(case)['recovered']==1 and counts(case.db)==(1,0)


@pytest.mark.parametrize('proposal',[
    {}, {'action':[],'confidence':1}, {'action':'unknown','confidence':1},
    {'action':'upsert','confidence':float('nan'),'fact':'不得写入'},
    {'action':'upsert','confidence':float('inf'),'fact':'不得写入'},
    {'action':'upsert','confidence':True,'fact':'不得写入'},
    {'action':'upsert','confidence':2,'fact':'不得写入'},
    {'action':'upsert','confidence':1,'fact':''},
    {'action':'upsert','confidence':1,'fact':{'text':'不得写入'}},
    {'action':'upsert','confidence':1,'fact':'不能把未知目标当新建','target_memory_id':'invented'},
])
def test_invalid_proposals_leave_the_source_pending(case,monkeypatch,proposal):
    async def invalid(*a,**kw):return proposal
    monkeypatch.setattr(corr,'_extract',invalid)
    assert run(case)['deferred']==1
    assert status(case)=='new' and counts(case.db)==(0,0)


def test_cancellation_before_commit_does_not_consume_sample(case,monkeypatch):
    import asyncio
    async def cancelled(*a,**kw):raise asyncio.CancelledError()
    monkeypatch.setattr(corr,'_extract',cancelled)
    with pytest.raises(asyncio.CancelledError):run(case)
    assert status(case)=='new' and counts(case.db)==(0,0)


def test_claim_without_original_text_is_not_processed(case):
    with case.db.engine.begin() as conn:conn.execute(text("UPDATE feedback_samples SET sample_text=''"))
    assert run(case)['conflicts']==1 and not case.calls
    assert status(case)=='new' and counts(case.db)==(0,0)


def test_learned_status_without_matching_event_does_not_fabricate_recovery(case,monkeypatch):
    with case.db.engine.begin() as conn:conn.execute(text("UPDATE feedback_samples SET status='learned'"))
    monkeypatch.setattr(case.db.feedback,'list_pending',lambda **kw:[case.sample])
    assert run(case)['conflicts']==1 and not case.calls
    assert counts(case.db)==(0,0)


def test_rate_deferral_is_scoped_to_full_account_and_preserves_sample(case,monkeypatch):
    seen=[]
    monkeypatch.setattr(corr,'_rate_ok',lambda key:seen.append(key) or False)
    assert run(case)['rate_limited']==1
    assert seen==[('wechat','a','same')] and not case.calls
    assert status(case)=='new' and counts(case.db)==(0,0)


@pytest.mark.parametrize('operation',['upsert','invalidate','delete','delete_all','cursor','explicit'])
def test_projection_mutations_preserve_repository_write_guard(case,operation):
    from db.repo_memory_projections import MemoryProjectionRepository
    class ReadOnlyHandle:
        def materialize(self):return case.db.engine
        def assert_writable(self):raise PermissionError('write guard refused')
    repo=MemoryProjectionRepository(ReadOnlyHandle())
    with pytest.raises(PermissionError,match='write guard'):
        if operation=='upsert':repo.upsert(SimpleNamespace())
        elif operation=='invalidate':repo.invalidate(['existing'])
        elif operation=='delete':repo.delete('existing')
        elif operation=='delete_all':repo.delete_all()
        elif operation=='cursor':repo.set_cursor('terminal_event',99)
        else:writer.commit_explicit(event(),store=case.db.store,repository=repo)
    assert counts(case.db)==(0,0)


def test_read_capability_does_not_require_write_permission(case):
    from db.repo_memory_projections import MemoryProjectionRepository
    class ReadOnlyHandle:
        def materialize(self):return case.db.engine
        def assert_writable(self):raise AssertionError('read path requested write permission')
    repo=MemoryProjectionRepository(ReadOnlyHandle())
    assert repo.get('missing') is None
    repo.assert_writable()  # legacy freeze inspection is read-only, not a write grant


def test_feedback_finish_requires_write_capability(case):
    from db.repo_feedback import FeedbackRepository
    class ReadOnlyHandle:
        def materialize(self):return case.db.engine
        def assert_writable(self):raise PermissionError('feedback write guard refused')
    repo=FeedbackRepository(ReadOnlyHandle())
    claim=claim_feedback(case.sample,repo)
    with pytest.raises(PermissionError):finish_feedback(claim,repo,'reviewed')
    assert status(case)=='new' and counts(case.db)==(0,0)


@pytest.mark.parametrize('action',['none','upsert'])
def test_freeze_during_model_call_keeps_feedback_unconsumed(case,monkeypatch,action):
    from core.memory.legacy_write_freeze import TRIGGERS,LegacyProjectionFrozen
    async def model(*a,**kw):
        with case.db.engine.begin() as conn:
            conn.exec_driver_sql('CREATE TABLE legacy_projection_write_owner(singleton INTEGER PRIMARY KEY,source_id TEXT,scope_key TEXT,target_fingerprint TEXT)')
            conn.exec_driver_sql("INSERT INTO legacy_projection_write_owner VALUES(1,'test','[\"wechat\",\"a\",\"same\"]','test-target')")
            for sql in TRIGGERS.values():conn.exec_driver_sql(sql)
        return {'action':action,'confidence':1,'fact':'停写后不得消费'}
    monkeypatch.setattr(corr,'_extract',model)
    with pytest.raises(LegacyProjectionFrozen):run(case)
    assert status(case)=='new' and counts(case.db)==(0,0)


@pytest.mark.parametrize('change',['valid','other_account','other_chat','unknown_chat','private_to_group','mixed_private_group','wrong_chat_type','thought','expired'])
def test_old_trace_candidates_are_scoped_before_model_access(case,monkeypatch,change):
    from core.terminal import memory_gateway
    from core.terminal.memory_projection import MemoryProjectionItem
    item=MemoryProjectionItem('trace-candidate',content='PRIVATE TRACE BODY',platform='wechat',bot_account_id='a',metadata={'chat_id':'same'},visibility_scope=('owner_only',))
    if change=='other_account':item=replace(item,bot_account_id='foreign')
    elif change=='other_chat':item=replace(item,metadata={'chat_id':'another'})
    elif change=='unknown_chat':item=replace(item,metadata={})
    elif change=='thought':item=replace(item,sensitivity='owner_chain_of_thought')
    elif change=='expired':item=replace(item,valid_to=datetime(2026,1,1))
    elif change=='mixed_private_group':item=replace(item,visibility_scope=('public','owner_only'))
    elif change=='wrong_chat_type':item=replace(item,metadata={'chat_id':'same','chat_type':'group'})
    case.db.repo.upsert(item)
    if change in {'private_to_group','mixed_private_group'}:case.sample.chat_type='group'
    monkeypatch.setattr(memory_gateway,'read_recall_trace',lambda **kw:[{'hits':[{'memory_id':item.memory_id}]}])
    result=case.original_collect(sample=case.sample,meta_repo=case.meta,quoted=SimpleNamespace(created_at=datetime(2026,11,8)))
    assert bool(result)==(change=='valid')


def test_foreign_quote_body_is_not_given_to_model(case,monkeypatch):
    monkeypatch.setattr(corr,'_lookup_quoted',lambda *a:SimpleNamespace(chat_id='foreign-chat',content='PRIVATE QUOTED BODY'))
    assert run(case)['learned']==1
    assert case.calls[0]['quoted_text']==''


def add_sample(case, *, created_at, chat_id='same', metadata=None):
    return case.db.feedback.add(source='correction',platform='wechat',bot_account_id='a',chat_id=chat_id,chat_type='private',person_id=P.person_id,sample_text='另一条需要纠正的记忆',created_at=created_at,metadata=metadata)


def row_status(case,row):
    with Session(case.db.engine) as session:return session.get(FeedbackSample,row.id).status


def test_backlog_is_consumed_oldest_first_instead_of_newest_only(case):
    extras=[add_sample(case,created_at=datetime(2026,11,8,9,0,i)) for i in range(6)]
    result=run(case)
    assert result['learned']==5
    assert status(case)=='learned'
    assert [row_status(case,row) for row in extras]==['learned','learned','learned','learned','new','new']


def test_unreadable_oldest_samples_do_not_block_later_corrections(case):
    bad=[add_sample(case,created_at=datetime(2026,11,7,0,i)) for i in range(6)]
    with case.db.engine.begin() as conn:
        conn.execute(text("UPDATE feedback_samples SET metadata_json='{not json' WHERE id!=:keep"),{'keep':case.sample.id})
    result=run(case)
    assert result['conflicts']==6 and result['learned']==1
    assert all(row_status(case,row)=='new' for row in bad) and status(case)=='learned'


def test_rate_limited_head_does_not_block_other_chats(case,monkeypatch):
    busy=[add_sample(case,created_at=datetime(2026,11,7,0,i),chat_id='busy') for i in range(6)]
    monkeypatch.setattr(corr,'_rate_ok',lambda key:key[2]!='busy')
    result=run(case)
    assert result['rate_limited']==6 and result['learned']==1
    assert all(row_status(case,row)=='new' for row in busy) and status(case)=='learned'


def test_scan_window_keeps_model_budget_separate(case):
    for i in range(12):add_sample(case,created_at=datetime(2026,11,8,9,0,i))
    result=run(case)
    assert result['learned']==5 and len(case.calls)==5 and result['scanned']==5

