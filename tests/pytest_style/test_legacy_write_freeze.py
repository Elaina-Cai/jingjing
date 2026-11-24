from dataclasses import replace
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
from types import SimpleNamespace

import pytest
from sqlalchemy.exc import IntegrityError

from core.memory.legacy_write_freeze import (
    assert_projection_writable,
    freeze_projection_writes,
    unfreeze_projection_writes,
    verify_freeze_owner,
    TRIGGERS,
)
from core.memory.legacy_read_binding import load_read_binding
from core.memory.hub import MemoryHub
from core.memory.providers import PersonalMemoryProvider
from core.memory.beliefs import BeliefMemoryProvider
from core.terminal import memory_gateway as mg
from db.repo_memory_projections import MemoryProjectionRepository
from test_legacy_read_binding import setup, gateway, P, execute
from test_legacy_acceptance import revise


def rows(source,table):
    conn=sqlite3.connect(source)
    try:return conn.execute('SELECT * FROM '+table).fetchall()
    finally:conn.close()


def schema(source):
    conn=sqlite3.connect(source)
    try:return conn.execute("SELECT name FROM sqlite_master WHERE name LIKE 'legacy_projection_%'").fetchall()
    finally:conn.close()


def test_dry_preview_is_read_only_and_reports_all_retained_rows(setup):
    before=setup.source.read_bytes(),setup.target.read_bytes()
    result=freeze_projection_writes(setup.binding)
    assert result['dry_run'] and not result['frozen'] and result['preserved_projection_rows']==2
    assert not schema(setup.source)
    assert before==(setup.source.read_bytes(),setup.target.read_bytes())


def test_freeze_is_idempotent_preserves_originals_and_unreviewed_rows(setup):
    before=rows(setup.source,'memory_projections'),rows(setup.source,'terminal_events')
    assert freeze_projection_writes(setup.binding,dry_run=False)['frozen']
    assert freeze_projection_writes(setup.binding,dry_run=False)['already_marked']
    assert before==(rows(setup.source,'memory_projections'),rows(setup.source,'terminal_events'))
    assert len(schema(setup.source))==4
    assert [h.memory_id for h in gateway(setup).recall(P)]==['proj:1']


@pytest.mark.parametrize('sql',[
    "INSERT INTO memory_projections SELECT * FROM memory_projections LIMIT 1",
    "UPDATE memory_projections SET content='BAD VALUE'",
    "DELETE FROM memory_projections",
])
def test_sql_writers_cannot_bypass_freeze(setup,sql):
    freeze_projection_writes(setup.binding,dry_run=False)
    before=rows(setup.source,'memory_projections')
    conn=sqlite3.connect(setup.source)
    try:
        with pytest.raises(sqlite3.IntegrityError,match='legacy_projection_writes_frozen'):
            conn.execute(sql)
    finally:conn.rollback();conn.close()
    assert rows(setup.source,'memory_projections')==before


@pytest.mark.parametrize('operation',['upsert_old','upsert_new','invalidate','delete','delete_all'])
def test_repository_mutations_share_database_guard(setup,operation):
    freeze_projection_writes(setup.binding,dry_run=False)
    before=rows(setup.source,'memory_projections')
    old=setup.repo.get('proj:0')
    with pytest.raises(IntegrityError,match='legacy_projection_writes_frozen'):
        if operation=='upsert_old':setup.repo.upsert(replace(old,content='BAD'))
        elif operation=='upsert_new':setup.repo.upsert(replace(old,memory_id='new-summary'))
        elif operation=='invalidate':setup.repo.invalidate(['proj:0','proj:1'])
        elif operation=='delete':setup.repo.delete('proj:0')
        else:setup.repo.delete_all()
    assert rows(setup.source,'memory_projections')==before


def test_trigger_survives_restart_and_removed_process_configuration(setup,monkeypatch):
    freeze_projection_writes(setup.binding,dry_run=False)
    setup.engine.dispose()
    monkeypatch.delenv('YINYUE_LEGACY_READ_BINDING',raising=False)
    repo=MemoryProjectionRepository(setup.engine)
    with pytest.raises(IntegrityError,match='legacy_projection_writes_frozen'):
        repo.upsert(replace(repo.get('proj:0'),memory_id='after-restart'))


def test_raw_observation_events_remain_writable(setup):
    freeze_projection_writes(setup.binding,dry_run=False)
    conn=sqlite3.connect(setup.source)
    conn.execute("INSERT INTO terminal_events SELECT 'new-observation',platform,bot_account_id,chat_id,event_type,content,sensitivity,visibility_scope_json FROM terminal_events LIMIT 1")
    conn.commit();conn.close()
    assert len(rows(setup.source,'terminal_events'))==3
    assert len(rows(setup.source,'memory_projections'))==2


# 2026-11-24：原来的 test_legacy_remember_reports_failure_before_appending_any_event 删掉了。
# 它守的是「冻结时必须在 append 之前失败」——主人决定重建读侧后，append 事件本身就是正确路径。
# 新语义（冻结时恰好一条事件、零条投影、重复幂等）由
# tests/pytest_style/test_explicit_memory_terminal.py::test_frozen_projection_still_writes_one_idempotent_event 覆盖。

def test_belief_remains_the_writable_owner_after_legacy_freeze(setup,monkeypatch):
    freeze_projection_writes(setup.binding,dry_run=False)
    revise(monkeypatch)
    setup.binding=load_read_binding(setup.config)
    hits=MemoryHub([PersonalMemoryProvider(gateway(setup)),BeliefMemoryProvider()]).recall(P,kinds=['fact'],limit=20)
    assert any('上海' in h.content for h in hits)
    assert all('旧地址记录-0' not in h.content for h in hits)


def test_conflicting_existing_trigger_is_not_replaced_or_partially_installed(setup):
    conn=sqlite3.connect(setup.source)
    conn.execute('CREATE TRIGGER legacy_projection_update_frozen BEFORE UPDATE ON memory_projections BEGIN SELECT 1; END')
    conn.commit();conn.close()
    before=schema(setup.source)
    with pytest.raises(ValueError,match='owner_missing'):
        freeze_projection_writes(setup.binding,dry_run=False)
    assert schema(setup.source)==before


def test_install_failure_rolls_back_marker_and_all_triggers(setup,monkeypatch):
    from core.memory import legacy_write_freeze as freeze
    original=sqlite3.connect
    class FaultConnection(sqlite3.Connection):
        def execute(self,sql,*args,**kwargs):
            if sql==TRIGGERS['legacy_projection_delete_frozen']:raise sqlite3.OperationalError('injected DDL failure')
            return super().execute(sql,*args,**kwargs)
    def connect(*a,**kw):
        kw['factory']=FaultConnection
        return original(*a,**kw)
    monkeypatch.setattr(freeze.sqlite3,'connect',connect)
    with pytest.raises(sqlite3.OperationalError,match='injected'):
        freeze_projection_writes(setup.binding,dry_run=False)
    assert not schema(setup.source)
    assert len(rows(setup.source,'memory_projections'))==2


def test_owner_scope_mismatch_cannot_freeze_store(setup):
    with pytest.raises(ValueError,match='scope_conflict'):
        freeze_projection_writes(replace(setup.binding,scope_key='["wechat","b","same"]'),dry_run=False)
    assert not schema(setup.source)


def test_cli_defaults_to_preview_then_can_apply_only_in_isolation(setup):
    root=Path(__file__).resolve().parents[2]
    command=[sys.executable,'-I',str(root/'scripts/freeze_legacy_projection_writes.py'),'--binding',str(setup.config)]
    preview=subprocess.run(command,capture_output=True,encoding='utf-8',timeout=20)
    assert preview.returncode==0,preview.stdout+preview.stderr
    assert json.loads(preview.stdout)['dry_run'] and not schema(setup.source)
    result=subprocess.run([*command,'--apply'],capture_output=True,encoding='utf-8',timeout=20)
    assert result.returncode==0,result.stdout+result.stderr
    assert json.loads(result.stdout)['frozen'] and len(schema(setup.source))==4


def test_persisted_freeze_is_visible_in_read_only_acceptance_and_preview(setup):
    from core.memory.legacy_acceptance import verify_import
    assert not verify_import(setup.source,source_id='meta',target=setup.target)['legacy_projection_writes_frozen']
    freeze_projection_writes(setup.binding,dry_run=False)
    result=verify_import(setup.source,source_id='meta',target=setup.target)
    assert result['legacy_projection_writes_frozen'] and not result['cutover_ready']
    assert freeze_projection_writes(setup.binding)['frozen']


@pytest.mark.parametrize('change',['trigger_removed','owner_changed'])
def test_broken_persistent_owner_cannot_pass_read_or_acceptance(setup,change):
    from core.memory.legacy_acceptance import verify_import
    freeze_projection_writes(setup.binding,dry_run=False)
    conn=sqlite3.connect(setup.source)
    if change=='trigger_removed':conn.execute('DROP TRIGGER legacy_projection_update_frozen')
    else:conn.execute("UPDATE legacy_projection_write_owner SET source_id='foreign'")
    conn.commit();conn.close()
    assert gateway(setup).recall(P)==[]
    with pytest.raises(ValueError,match='freeze_'):
        load_read_binding(setup.config)
    with pytest.raises(ValueError,match='freeze_'):
        verify_import(setup.source,source_id='meta',target=setup.target)


def test_frozen_source_cannot_be_reassigned_to_a_copied_target(setup,monkeypatch):
    import shutil
    freeze_projection_writes(setup.binding,dry_run=False)
    other=setup.target.with_name('forked-target.db')
    shutil.copyfile(setup.target,other)
    config=json.loads(setup.config.read_text(encoding='utf-8'))
    config['target']=str(other)
    setup.config.write_text(json.dumps(config),encoding='utf-8')
    monkeypatch.setenv('YINYUE_COGNITIVE_DB',str(other))
    with pytest.raises(ValueError,match='owner_conflict'):
        load_read_binding(setup.config)


def test_no_binding_cannot_reopen_legacy_projection_or_raw_event_reads(setup):
    freeze_projection_writes(setup.binding,dry_run=False)
    assert gateway(setup,False).recall(P)==[]
    def forbidden(*a,**kw):raise AssertionError('frozen raw event read must be rejected before fetching events')
    raw=mg.MemoryGateway(event_store=SimpleNamespace(engine=setup.engine,query_visible=forbidden))
    assert raw.recall(P)==[]


def test_old_index_loses_read_access_if_frozen_owner_configuration_is_lost(setup,monkeypatch):
    from agents.tools.memory_index import ReadMemoryTool
    g=gateway(setup,False)
    ctx=SimpleNamespace(principal=P,extra={'platform':'wechat','account_id':'a','chat_id':'same'})
    monkeypatch.setattr(g,'principal_from_context',lambda *a,**kw:P)
    g.attach_to_context(ctx)
    freeze_projection_writes(setup.binding,dry_run=False)
    tool=ReadMemoryTool();tool.set_context(ctx)
    assert execute(tool,keys='m1').error
    assert '旧地址记录' not in g.attach_to_context(ctx)


def test_runtime_factory_refuses_frozen_source_without_a_binding(setup,monkeypatch):
    from core.terminal import event_runtime,store_port
    freeze_projection_writes(setup.binding,dry_run=False)
    monkeypatch.delenv('YINYUE_LEGACY_READ_BINDING',raising=False)
    monkeypatch.setattr(event_runtime,'get_event_identity_service',lambda:None)
    monkeypatch.setattr(store_port,'get_meta_repo',lambda:SimpleNamespace(engine=setup.engine))
    monkeypatch.setattr(mg,'_gateway',None);monkeypatch.setattr(mg,'_error','')
    assert mg.get_memory_gateway() is None
    assert mg._gateway is None and 'frozen' in mg._error


def test_unfrozen_remember_still_uses_the_canonical_event_and_projection(setup,monkeypatch):
    from agents.tools.memory import RememberTool
    from core.terminal import event_runtime,store_port
    from core.terminal.event_store_db import TerminalEventStore
    from core.rules import engine as rules
    from db.meta_models import TerminalEvent,MemoryProjectionRow
    from sqlalchemy import create_engine
    import asyncio
    engine=create_engine('sqlite:///'+str(setup.source.with_name('explicit-success.db')))
    TerminalEvent.__table__.create(engine)
    MemoryProjectionRow.__table__.create(engine)
    store=TerminalEventStore(engine)
    monkeypatch.setattr(event_runtime,'default_event_store',lambda:store)
    monkeypatch.setattr(store_port,'get_meta_repo',lambda:SimpleNamespace(engine=engine))
    monkeypatch.setattr(rules,'decide',lambda ctx:SimpleNamespace(allowed=True))
    async def inline(func,*a,**kw):return func(*a,**kw)
    monkeypatch.setattr(asyncio,'to_thread',inline)
    try:
        tool=RememberTool(None,None,None)
        tool.set_context(SimpleNamespace(principal=P,extra={}))
        result=execute(tool,content='未切换旧库仍可保存这一条摘要',memory_type='semantic')
        emitted=store.all()
        assert not result.error and len(emitted)==1
        projected=MemoryProjectionRepository(engine).get('proj:'+emitted[0].event_id)
        assert projected is not None and projected.source_event_ids==(emitted[0].event_id,)
    finally:
        engine.dispose()


def test_retired_correction_job_does_not_query_samples_call_model_or_mark_them(setup):
    from core.quality import corrections
    freeze_projection_writes(setup.binding,dry_run=False)
    def forbidden(*a,**kw):raise AssertionError('retired job must leave feedback pending and never call a model')
    meta=SimpleNamespace(memory_projections=setup.repo,feedback_samples=SimpleNamespace(list_recent=forbidden,mark_status=forbidden))
    result=execute(SimpleNamespace(execute=lambda **kw:corrections.process_pending_corrections(meta_repo=meta,llm=SimpleNamespace(chat=forbidden))))
    assert result['status']=='retired' and result['scanned']==result['learned']==0


def test_retired_consolidation_exits_before_loading_worlds(setup,monkeypatch):
    from core.quality import consolidation
    freeze_projection_writes(setup.binding,dry_run=False)
    def forbidden(*a,**kw):raise AssertionError('retired consolidation must not scan models')
    monkeypatch.setattr(consolidation,'collect_candidates',forbidden)
    result=consolidation.run_consolidation(repo=setup.repo,mode='on')
    assert result['status']=='retired' and result['candidates']==result['written']==0


def test_background_lookup_failure_is_not_a_false_retirement(setup,monkeypatch):
    from core.quality import consolidation,corrections
    def failure():raise OSError('unavailable write owner')
    monkeypatch.setattr(setup.repo,'assert_writable',failure)
    with pytest.raises(OSError):consolidation.run_consolidation(repo=setup.repo,mode='on')
    meta=SimpleNamespace(memory_projections=setup.repo)
    with pytest.raises(OSError):execute(SimpleNamespace(execute=lambda **kw:corrections.process_pending_corrections(meta_repo=meta)))


def test_unfreeze_dry_run_requires_reason_confirm_and_changes_nothing(setup):
    freeze_projection_writes(setup.binding, dry_run=False)
    before = setup.source.read_bytes()
    with pytest.raises(ValueError, match="reason"):
        unfreeze_projection_writes(
            setup.binding, dry_run=False, confirm="unfreeze:" + setup.binding.scope_key
        )
    with pytest.raises(ValueError, match="confirm"):
        unfreeze_projection_writes(
            setup.binding, dry_run=False, reason="rollback drill", confirm="wrong"
        )
    result = unfreeze_projection_writes(
        setup.binding,
        reason="rollback drill",
        confirm="unfreeze:" + setup.binding.scope_key,
    )
    assert result["dry_run"] and not result["unfrozen"]
    assert before == setup.source.read_bytes()
    assert all(name in {row[0] for row in schema(setup.source)} for name in TRIGGERS)


def test_unfreeze_apply_is_audited_and_preserves_projection_rows(setup):
    freeze_projection_writes(setup.binding, dry_run=False)
    before = rows(setup.source, "memory_projections")
    result = unfreeze_projection_writes(
        setup.binding,
        dry_run=False,
        reason="operator rollback drill",
        confirm="unfreeze:" + setup.binding.scope_key,
    )
    assert result["unfrozen"] and result["removed_triggers"] == len(TRIGGERS)
    assert rows(setup.source, "memory_projections") == before
    assert schema(setup.source) == [("legacy_projection_unfreeze_log",)]
    assert verify_freeze_owner(
        setup.source,
        source_id=setup.binding.source_id,
        scope_key=setup.binding.scope_key,
        target=setup.binding.target,
    ) is False
    assert_projection_writable(setup.engine)
    conn = sqlite3.connect(setup.source)
    try:
        logged = conn.execute(
            "SELECT source_id,scope_key,reason FROM legacy_projection_unfreeze_log"
        ).fetchall()
    finally:
        conn.close()
    assert logged == [(setup.binding.source_id, setup.binding.scope_key, "operator rollback drill")]
    again = unfreeze_projection_writes(
        setup.binding,
        dry_run=False,
        reason="operator rollback drill",
        confirm="unfreeze:" + setup.binding.scope_key,
    )
    assert again["already_unfrozen"] and not again["unfrozen"]


def test_unfreeze_cli_defaults_to_preview_then_requires_explicit_apply(setup):
    freeze_projection_writes(setup.binding,dry_run=False)
    before=rows(setup.source,'memory_projections')
    root=Path(__file__).resolve().parents[2]
    command=[sys.executable,'-I',str(root/'scripts/unfreeze_legacy_projection_writes.py'),
             '--binding',str(setup.config),'--reason','cli rollback drill',
             '--confirm','unfreeze:'+setup.binding.scope_key]
    preview=subprocess.run(command,capture_output=True,encoding='utf-8',timeout=20)
    assert preview.returncode==0,preview.stdout+preview.stderr
    assert json.loads(preview.stdout)['dry_run']
    assert schema(setup.source)!=[] and len(schema(setup.source))==4
    result=subprocess.run([*command,'--apply'],capture_output=True,encoding='utf-8',timeout=20)
    assert result.returncode==0,result.stdout+result.stderr
    payload=json.loads(result.stdout)
    assert payload['unfrozen'] and payload['removed_triggers']==len(TRIGGERS)
    assert rows(setup.source,'memory_projections')==before
    assert schema(setup.source)==[('legacy_projection_unfreeze_log',)]
