from dataclasses import replace
from datetime import datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, text

from agents.tools.memory import RememberTool
from core.memory.explicit_write import commit_explicit, projection_version
from core.memory.service import MemoryService
from core.terminal.event_model import Event, EventType
from core.terminal.event_store_db import TerminalEventStore
from core.terminal.principal import VisibilityScope, RelationLevel
from db.meta_models import TerminalEvent, MemoryProjectionRow, FeedbackSample
from db.repo_memory_projections import MemoryProjectionRepository
from test_legacy_read_binding import P, execute


@pytest.fixture()
def db(tmp_path,monkeypatch):
    from core.terminal import event_runtime,store_port
    from core.rules import engine as rules
    import asyncio
    engine=create_engine('sqlite:///'+str(tmp_path/'explicit.db'))
    TerminalEvent.__table__.create(engine)
    MemoryProjectionRow.__table__.create(engine)
    FeedbackSample.__table__.create(engine)
    from db.repo_feedback import FeedbackRepository
    repo=MemoryProjectionRepository(engine)
    store=TerminalEventStore(engine)
    monkeypatch.setattr(event_runtime,'default_event_store',lambda:store)
    monkeypatch.setattr(store_port,'get_meta_repo',lambda:SimpleNamespace(engine=engine))
    monkeypatch.setattr(rules,'decide',lambda ctx:SimpleNamespace(allowed=True))
    async def inline(func,*a,**kw):return func(*a,**kw)
    monkeypatch.setattr(asyncio,'to_thread',inline)
    yield SimpleNamespace(engine=engine,repo=repo,store=store,feedback=FeedbackRepository(engine))
    engine.dispose()


def event(key='write-1',content='主人确认的新信息',chat='same'):
    return Event.new(event_type=EventType.MEMORY_WRITE,event_id=key,content=content,occurred_at=datetime(2026,11,8),
        platform='wechat',bot_account_id='a',chat_id=chat,actor_person_id=P.person_id,
        visibility_scope=frozenset({VisibilityScope.OWNER_ONLY}),metadata={'memory_type':'semantic','explicit_remember':True,'source':'test','principal':P.as_dict()})


def apply(db,e,**kw):return commit_explicit(e,store=db.store,repository=db.repo,**kw)


def counts(db):
    with db.engine.connect() as conn:
        return tuple(conn.execute(text('SELECT COUNT(*) FROM '+table)).scalar() for table in ('terminal_events','memory_projections'))


def context(principal=P):
    return SimpleNamespace(principal=principal,extra={'platform':principal.platform,'account_id':principal.bot_account_id,'chat_id':principal.chat_id})


def test_single_commit_and_identical_replay_are_consistent(db):
    e=event()
    first=apply(db,e)
    assert first.source_event_ids==('write-1',) and e.sequence>0
    assert counts(db)==(1,1)
    assert apply(db,e).content==first.content and counts(db)==(1,1)


def test_event_identity_collision_cannot_change_existing_projection(db):
    apply(db,event())
    with pytest.raises(ValueError,match='identity_conflict'):
        apply(db,event(content='伪装同编号的新正文'))
    assert counts(db)==(1,1) and db.repo.get('proj:write-1').content=='主人确认的新信息'


@pytest.mark.parametrize('flush',[False,True])
def test_projection_failure_rolls_back_the_event_and_retry_is_safe(db,monkeypatch,flush):
    original=db.repo.upsert_in_session
    e=event()
    with monkeypatch.context() as patch:
        def fail(session,item):
            if flush:
                original(session,item)
                session.flush()
            raise OSError('injected projection failure')
        patch.setattr(db.repo,'upsert_in_session',fail)
        with pytest.raises(OSError,match='injected'):
            apply(db,e)
    assert counts(db)==(0,0)
    apply(db,event('intervening'))
    apply(db,e)
    assert counts(db)==(2,2)
    assert len({row.sequence for row in db.store.all()})==2


def test_response_loss_replay_does_not_revive_closed_projection(db):
    e=event()
    apply(db,e)
    db.repo.invalidate(['proj:write-1'])
    closed=db.repo.get('proj:write-1').valid_to
    assert apply(db,e).valid_to==closed
    assert counts(db)==(1,1)


def test_nonprojectable_memory_rolls_back_even_its_source_event(db):
    with pytest.raises(ValueError,match='not_projectable'):
        apply(db,event(content='@'))
    assert counts(db)==(0,0)


def test_store_in_another_database_cannot_receive_half_a_write(db,tmp_path):
    other=create_engine('sqlite:///'+str(tmp_path/'other.db'))
    try:
        with pytest.raises(ValueError,match='same_database'):
            commit_explicit(event(),store=TerminalEventStore(other),repository=db.repo)
        assert counts(db)==(0,0)
    finally:other.dispose()


def test_explicit_supersession_is_versioned_scoped_and_atomic(db):
    old=apply(db,event('old'))
    expected=projection_version(old)
    revised=event('new',content='更正后的新事实')
    apply(db,revised,supersedes=((old.memory_id,expected),))
    assert db.repo.get(old.memory_id).valid_to==datetime(2026,11,8)
    apply(db,revised,supersedes=((old.memory_id,expected),))
    assert counts(db)==(2,2)


@pytest.mark.parametrize('change',['other_chat','stale_version','visibility','sensitivity'])
def test_invalid_supersession_changes_neither_target_nor_event(db,change):
    old=apply(db,event('old',chat='foreign' if change=='other_chat' else 'same'))
    if change=='sensitivity':
        old=replace(old,sensitivity='owner_chain_of_thought')
        db.repo.upsert(old)
    expected=projection_version(old)
    if change=='stale_version':db.repo.upsert(replace(old,content='已被其他写者更新'))
    with pytest.raises(ValueError,match='supersedes_'):
        new=event('new')
        if change=='visibility':new.visibility_scope=frozenset({VisibilityScope.PUBLIC})
        apply(db,new,supersedes=((old.memory_id,expected),))
    assert counts(db)==(1,1) and db.repo.get(old.memory_id).valid_to is None


def test_failure_after_target_closure_rolls_back_the_whole_change(db,monkeypatch):
    old=apply(db,event('old'))
    def fail(session,item):
        session.flush()  # target closure has reached SQLite but must still roll back
        raise OSError('after closure')
    monkeypatch.setattr(db.repo,'upsert_in_session',fail)
    with pytest.raises(OSError):apply(db,event('new'),supersedes=((old.memory_id,projection_version(old)),))
    assert db.repo.get(old.memory_id).valid_to is None and counts(db)==(1,1)


def test_tool_and_application_service_use_the_same_authority_and_commit(db):
    tool=RememberTool(None,None,None);tool.set_context(context())
    assert not execute(tool,content='工具保存的明确摘要',memory_type='semantic').error
    item=MemoryService().remember(context(),'应用服务保存的明确摘要')
    assert item is not None and counts(db)==(2,2)
    assert (item.platform,item.bot_account_id,item.chat_id)==(P.platform,P.bot_account_id,P.chat_id)


@pytest.mark.parametrize('condition',['non_owner','rule_denied','scope_conflict'])
def test_application_service_cannot_bypass_tool_authority(db,monkeypatch,condition):
    from core.rules import engine as rules
    principal=replace(P,relation_to_bot=RelationLevel.ORDINARY) if condition=='non_owner' else P
    ctx=context(principal)
    if condition=='rule_denied':monkeypatch.setattr(rules,'decide',lambda ctx:SimpleNamespace(allowed=False,explanation='quota denied'))
    if condition=='scope_conflict':ctx.extra['account_id']='foreign'
    tool=RememberTool(None,None,None);tool.set_context(ctx)
    assert execute(tool,content='不能落盘的摘要').error
    assert MemoryService().remember(ctx,'不能绕过的摘要') is None
    assert counts(db)==(0,0)


def test_similar_text_and_final_version_phrase_do_not_invalidate_other_memories(db):
    value='主人喜欢在周六上午去图书馆整理旅行计划和阅读笔记，这是一条需要保留的长信息'
    old=apply(db,event('old',content=value))
    foreign=apply(db,event('foreign',content=value,chat='another'))
    tool=RememberTool(None,None,None);tool.set_context(context())
    assert not execute(tool,content=value+'，最新版以此为准',memory_type='semantic').error
    assert db.repo.get(old.memory_id).valid_to is None
    assert db.repo.get(foreign.memory_id).valid_to is None


def test_background_correction_uses_the_same_atomic_version_check(db):
    from core.quality import corrections
    old=apply(db,event('old'))
    sample=db.feedback.add(source='correction',platform='wechat',bot_account_id='a',chat_id='same',chat_type='private',person_id=P.person_id,sample_text='地址记错了',created_at=datetime(2026,11,8))
    result=corrections._apply(sample=sample,extraction={'action':'upsert','fact':'后台根据明确更正得到的新摘要','confidence':1,'target_memory_id':old.memory_id},
        candidates=[{'memory_id':old.memory_id,'full_content':old.content,'content':old.content,'visibility':['owner_only'],'version':projection_version(old)}],
        meta_repo=SimpleNamespace(memory_projections=db.repo,feedback_samples=db.feedback))
    assert result=='learned' and counts(db)==(2,2)
    assert db.repo.get(old.memory_id).valid_to is not None


def test_legacy_model_forget_cannot_invalidate_a_fact(db):
    from core.quality import corrections
    old=apply(db,event('old'))
    sample=SimpleNamespace(platform='wechat',bot_account_id='a',chat_id='same',chat_type='private',person_id=P.person_id)
    result=corrections._apply(sample=sample,extraction={'action':'forget','confidence':1,'target_memory_id':old.memory_id},
        candidates=[{'memory_id':old.memory_id,'version':projection_version(old)}],meta_repo=SimpleNamespace(memory_projections=db.repo))
    assert result=='reviewed' and counts(db)==(1,1)
    assert db.repo.get(old.memory_id).valid_to is None
