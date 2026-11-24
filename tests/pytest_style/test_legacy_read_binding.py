from datetime import datetime
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine

from agents.tools.memory_index import ReadMemoryTool
from core.context import situation_store as store
from core.memory import legacy_import as importer
from core.memory.beliefs import BeliefMemoryProvider
from core.memory.hub import MemoryHub
from core.memory.providers import PersonalMemoryProvider
from core.memory.legacy_read_binding import load_read_binding
from core.terminal import memory_gateway as mg
from core.terminal.memory_projection import MemoryProjectionItem
from core.terminal.principal import PrincipalContext, ChatType, RelationLevel
from db.meta_models import MemoryProjectionRow
from db.repo_memory_projections import MemoryProjectionRepository
from test_legacy_belief_import import manifest
from test_legacy_acceptance import revise

P=PrincipalContext('wechat','a','same',ChatType.PRIVATE,'person-a','A',RelationLevel.OWNER)


@pytest.fixture()
def setup(tmp_path,monkeypatch):
    source,target=tmp_path/'source.db',tmp_path/'target.db'
    engine=create_engine('sqlite:///'+str(source))
    MemoryProjectionRow.__table__.create(engine)
    repo=MemoryProjectionRepository(engine)
    conn=sqlite3.connect(source)
    conn.execute('CREATE TABLE terminal_events(event_id TEXT,platform TEXT,bot_account_id TEXT,chat_id TEXT,event_type TEXT,content TEXT,sensitivity TEXT,visibility_scope_json TEXT)')
    for i in range(2):
        conn.execute('INSERT INTO terminal_events VALUES(?,?,?,?,?,?,?,?)',(f'event-{i}','wechat','a','same','message','外部原始证据','normal','["public"]'))
    conn.commit();conn.close()
    for i in range(2):
        repo.upsert(MemoryProjectionItem(f'proj:{i}',memory_type='fact',content=f'旧地址记录-{i}',platform='wechat',bot_account_id='a',source_event_ids=(f'event-{i}',),occurred_at=datetime(2026,1,1),metadata={'chat_id':'same'}))
    monkeypatch.setattr(importer,'SANDBOX_ROOTS',(tmp_path,))
    monkeypatch.setenv('YINYUE_COGNITIVE_DB',str(target))
    monkeypatch.setenv('YINYUE_WORLD_MODEL_DIR',str(tmp_path/'world'))
    monkeypatch.setenv('YINYUE_MEMORY_INDEX','on')
    monkeypatch.delenv('YINYUE_LEGACY_READ_BINDING',raising=False)
    monkeypatch.setattr(mg,'_trace_recall',lambda *a,**kw:None)
    monkeypatch.setattr(mg,'memory_gate_enabled',lambda:False)
    store.reset_world_models()
    review=manifest(source)
    review['reviews'][1]['decision']='defer'
    importer.import_reviewed(source,source_id='meta',review=review,target=target,dry_run=False)
    config=tmp_path/'binding.json'
    config.write_text(json.dumps({'schema_version':1,'source':str(source),'source_id':'meta','target':str(target)}),encoding='utf-8')
    binding=load_read_binding(config)
    yield SimpleNamespace(source=source,target=target,repo=repo,engine=engine,config=config,binding=binding)
    store.reset_world_models();engine.dispose()


def gateway(setup,bound=True):
    return mg.MemoryGateway(projection_repository=setup.repo,read_binding=setup.binding if bound else None)


def test_only_explicitly_imported_ids_leave_legacy_recall_and_no_data_is_modified(setup):
    before=setup.source.read_bytes(),setup.target.read_bytes()
    assert {h.memory_id for h in gateway(setup,False).recall(P)}=={'proj:0','proj:1'}
    assert {h.memory_id for h in gateway(setup).recall(P)}=={'proj:1'}
    hub=MemoryHub([PersonalMemoryProvider(gateway(setup)),BeliefMemoryProvider()])
    hits=hub.recall(P,kinds=['fact'],limit=20)
    assert sum('旧地址记录-0' in h.content for h in hits)==1
    assert any(h.kind=='belief' for h in hits)
    assert before==(setup.source.read_bytes(),setup.target.read_bytes())


def test_correction_and_restart_do_not_revive_old_personal_value(setup,monkeypatch):
    revise(monkeypatch)
    store.reset_world_models()
    setup.binding=load_read_binding(setup.config)
    hub=MemoryHub([PersonalMemoryProvider(gateway(setup)),BeliefMemoryProvider()])
    hits=hub.recall(P,kinds=['fact'],limit=20)
    assert any('上海' in h.content for h in hits)
    assert all('旧地址记录-0' not in h.content for h in hits)
    assert 'proj:1' in {h.memory_id for h in hits}


def test_same_content_is_not_a_transfer_of_ownership(setup):
    from dataclasses import replace
    setup.repo.upsert(replace(setup.repo.get('proj:1'),content='旧地址记录-0'))
    hits=gateway(setup).recall(P)
    assert [h.memory_id for h in hits]==['proj:1']


def execute(tool,**kwargs):
    coro=tool.execute(**kwargs)
    try:
        with pytest.raises(StopIteration) as done:
            coro.send(None)
        return done.value.value
    finally:
        coro.close()


def test_existing_index_and_cached_preview_cannot_bypass_new_read_owner(setup,monkeypatch):
    g=gateway(setup,False)
    ctx=SimpleNamespace(principal=P,extra={'platform':'wechat','account_id':'a','chat_id':'same'})
    monkeypatch.setattr(g,'principal_from_context',lambda *a,**kw:P)
    assert '旧地址记录-0' in g.attach_to_context(ctx)
    entries=ctx.memory_index_snapshot.entries_for(ctx)
    key=next(e['key'] for e in entries if e['memory_id']=='proj:0')
    g.read_binding=setup.binding
    tool=ReadMemoryTool();tool.set_context(ctx)
    result=execute(tool,keys=key)
    assert result.error and '旧地址记录-0' not in result.output
    assert '旧地址记录-0' not in g.attach_to_context(ctx)


def test_source_row_update_cannot_reclaim_transferred_read_right(setup):
    from dataclasses import replace
    setup.repo.upsert(replace(setup.repo.get('proj:0'),content='CHANGED LEGACY VALUE'))
    assert all(h.memory_id!='proj:0' for h in gateway(setup).recall(P))
    with pytest.raises(ValueError,match='integrity_failed'):
        load_read_binding(setup.config)


@pytest.mark.parametrize('change',['missing_owner','other_domain','broken_json'])
def test_target_drift_denies_legacy_read_instead_of_falling_back(setup,change):
    conn=sqlite3.connect(setup.target)
    if change=='missing_owner':
        conn.execute('DELETE FROM cognitive_events')
    elif change=='other_domain':
        conn.execute("UPDATE legacy_import_sandbox SET source_id='another'")
    else:
        conn.execute("UPDATE cognitive_events SET payload='[]'")
    conn.commit();conn.close()
    assert gateway(setup).recall(P)==[]


def test_current_belief_target_must_match_the_bound_journal(setup,monkeypatch):
    monkeypatch.setenv('YINYUE_COGNITIVE_DB',str(setup.target.with_name('another.db')))
    assert gateway(setup).recall(P)==[]
    with pytest.raises(ValueError,match='match_belief_reader'):
        load_read_binding(setup.config)


def test_binding_cannot_be_used_against_another_source_database(setup,tmp_path):
    other=create_engine('sqlite:///'+str(tmp_path/'other.db'))
    try:
        MemoryProjectionRow.__table__.create(other)
        other_repo=MemoryProjectionRepository(other)
        other_repo.upsert(setup.repo.get('proj:0'))
        assert mg.MemoryGateway(projection_repository=other_repo,read_binding=setup.binding).recall(P)==[]
        assert mg.MemoryGateway(projection_repository=other_repo).recall(P)
    finally:
        other.dispose()


def test_bound_gateway_cannot_silently_switch_to_event_only_mode(setup):
    with pytest.raises(ValueError,match='requires_projection_repository'):
        mg.MemoryGateway(read_binding=setup.binding)
    g=gateway(setup)
    g.projection_repository=None
    assert g.recall(P)==[]


@pytest.mark.parametrize('change',['extra_scope','wrong_source_id','nonobject','old_schema'])
def test_unknown_or_ambiguous_binding_is_rejected(setup,change):
    config=json.loads(setup.config.read_text(encoding='utf-8'))
    if change=='extra_scope':config['scope']={'account_id':'foreign'}
    elif change=='wrong_source_id':config['source_id']='another'
    elif change=='nonobject':config=[]
    else:config['schema_version']=0
    setup.config.write_text(json.dumps(config),encoding='utf-8')
    with pytest.raises(ValueError):load_read_binding(setup.config)


def test_runtime_factory_loads_binding_and_reconstructs_it_after_restart(setup,monkeypatch):
    from core.terminal import event_runtime,store_port
    monkeypatch.setenv('YINYUE_LEGACY_READ_BINDING',str(setup.config))
    # 2026-11-24：别的测试模块 import 时会加载 .env，里面 LEGACY_PROJECTIONS_DISCARDED=1；
    # 本用例要的就是「读投影」，必须显式关掉弃置开关，否则网关直接返回 []。
    monkeypatch.delenv('YINYUE_LEGACY_PROJECTIONS_DISCARDED',raising=False)
    monkeypatch.setattr(event_runtime,'get_event_identity_service',lambda:None)
    monkeypatch.setattr(store_port,'get_meta_repo',lambda:SimpleNamespace(engine=setup.engine))
    monkeypatch.setattr(mg,'_gateway',None);monkeypatch.setattr(mg,'_error','')
    for _ in range(2):
        mg._gateway=None;mg._error=''
        g=mg.get_memory_gateway()
        assert g is not None and {h.memory_id for h in g.recall(P)}=={'proj:1'}
    setup.config.write_text('[]',encoding='utf-8')
    mg._gateway=None;mg._error=''
    assert mg.get_memory_gateway() is None


@pytest.mark.parametrize('field',['source','target'])
def test_relative_source_locations_are_not_stable_bindings(setup,field):
    config=json.loads(setup.config.read_text(encoding='utf-8'))
    config[field]='ambiguous.db'
    setup.config.write_text(json.dumps(config),encoding='utf-8')
    with pytest.raises(ValueError,match='absolute_paths'):
        load_read_binding(setup.config)


def test_replacing_source_file_invalidates_an_existing_binding(setup,tmp_path):
    import shutil
    replacement=tmp_path/'replacement.db'
    setup.engine.dispose()
    shutil.copyfile(setup.source,replacement)
    replacement.replace(setup.source)
    assert gateway(setup).recall(P)==[]


def test_unreadable_owner_journal_does_not_allow_old_recall_or_index_expansion(setup,monkeypatch):
    from core.memory import legacy_read_binding as policy
    g=gateway(setup,False)
    ctx=SimpleNamespace(principal=P,extra={'platform':'wechat','account_id':'a','chat_id':'same'})
    monkeypatch.setattr(g,'principal_from_context',lambda *a,**kw:P)
    g.attach_to_context(ctx)
    g.read_binding=setup.binding
    original=policy.sqlite3.connect
    def unavailable(path,*a,**kw):
        if str(path).startswith(setup.target.as_uri()):raise OSError('owner database offline')
        return original(path,*a,**kw)
    monkeypatch.setattr(policy.sqlite3,'connect',unavailable)
    assert g.recall(P)==[]
    tool=ReadMemoryTool();tool.set_context(ctx)
    assert execute(tool,keys='m1').error


def test_binding_applies_to_vector_only_candidates_too(setup):
    from unittest.mock import patch
    g=gateway(setup)
    g._vector_recaller=lambda *a,**kw:{'proj:0':1.0}
    with patch.object(setup.repo,'query_candidates',return_value=[]):
        assert g.recall(P,query='旧地址')==[]


def test_read_binding_supports_actual_repository_write_handles(setup):
    from db.write_guard import WriteHandle
    handle=WriteHandle(setup.source,what='isolated read binding test',factory=lambda:setup.engine)
    repo=MemoryProjectionRepository(handle)
    g=mg.MemoryGateway(projection_repository=repo,read_binding=setup.binding)
    assert {h.memory_id for h in g.recall(P)}=={'proj:1'}
