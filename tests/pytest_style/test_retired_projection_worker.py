import ast
from datetime import datetime
import importlib.util
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest

from core.memory.legacy_write_freeze import freeze_projection_writes, LegacyProjectionFrozen
from core.terminal.memory_projection import MemoryProjector, projection_cursor_key
from core.terminal.event_model import Event, EventType
from core.terminal import memory_runtime
from db.meta_models import MemoryProjectionCursor
from db.repo_memory_projections import MemoryProjectionRepository
from test_legacy_read_binding import setup, execute


@pytest.fixture()
def case(setup,monkeypatch):
    MemoryProjectionCursor.__table__.create(setup.engine)
    setup.repo.set_cursor_pair(projection_cursor_key(),12,34)
    def forbidden(*a,**kw):raise AssertionError('retired projection must not fetch or consume events')
    projector=MemoryProjector(SimpleNamespace(since_sequence=forbidden),setup.repo)
    monkeypatch.setattr(memory_runtime,'get_memory_projector',lambda **kw:projector)
    return SimpleNamespace(base=setup,projector=projector)


def freeze(case):
    return freeze_projection_writes(case.base.binding,dry_run=False)


@pytest.mark.parametrize('mode',['pending','direct','backfill'])
def test_all_projector_entry_points_preserve_cursor_and_do_not_fetch_events(case,mode):
    freeze(case)
    def events():raise AssertionError('retired generator was consumed');yield
    for _ in range(3):
        if mode=='pending':stats=case.projector.project_pending()
        elif mode=='direct':stats=case.projector.project_events(events())
        else:stats=case.projector.backfill()
        assert stats=={'status':'retired','reason':'legacy_projection_writes_frozen','scanned':0,'projected':0,'cursor':12,'cursor_pair':(12,34)}
    assert case.base.repo.get_cursor_pair(projection_cursor_key())==(12,34)


def test_retirement_survives_repository_and_worker_restart(case):
    freeze(case)
    case.base.engine.dispose()
    repo=MemoryProjectionRepository(case.base.engine)
    projector=MemoryProjector(None,repo)
    assert projector.project_pending()['status']=='retired'
    assert projector.backfill()['cursor_pair']==(12,34)


def test_retired_runtime_is_distinct_from_failed_or_empty_batch(case):
    freeze(case)
    stats=memory_runtime.project_pending_memory()
    assert stats['status']=='retired' and 'error' not in stats


@pytest.mark.parametrize('kind',['lookup_error','damaged_gate','missing_owner'])
def test_read_faults_and_partial_freeze_are_errors_not_retirement(case,monkeypatch,kind):
    if kind=='lookup_error':
        def failure():raise OSError('projection lookup unavailable')
        monkeypatch.setattr(case.base.repo,'assert_writable',failure)
    else:
        freeze(case)
        conn=sqlite3.connect(case.base.source)
        conn.execute('DROP TABLE legacy_projection_write_owner' if kind=='missing_owner' else 'DROP TRIGGER legacy_projection_update_frozen')
        conn.commit();conn.close()
    stats=memory_runtime.project_pending_memory()
    assert stats.get('error') and stats.get('status')!='retired'
    assert case.base.repo.get_cursor_pair(projection_cursor_key())==(12,34)


def test_direct_cursor_reset_cannot_bypass_retirement(case):
    freeze(case)
    with pytest.raises(LegacyProjectionFrozen):
        case.base.repo.set_cursor(projection_cursor_key(),0)
    assert case.base.repo.get_cursor_pair(projection_cursor_key())==(12,34)


def test_unfrozen_empty_batch_still_reports_current_cursor(case):
    case.projector.event_store=SimpleNamespace(since_sequence=lambda *a,**kw:[])
    stats=memory_runtime.project_pending_memory()
    assert stats['cursor_pair']==(12,34) and stats.get('status')!='retired'
    assert case.projector.backfill()['cursor_pair']==(0,0)


def cli():
    path=Path(__file__).resolve().parents[2]/'scripts/phase2_project_memory.py'
    spec=importlib.util.spec_from_file_location('isolated_projection_cli',path)
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize('backfill',[False,True])
def test_projection_cli_reports_retirement_and_does_not_clear_cursor(case,capsys,backfill):
    freeze(case)
    args=['--batches','3']+(['--backfill'] if backfill else [])
    assert cli().main(args)==3
    report=json.loads(capsys.readouterr().out)
    assert report['status']=='retired' and report['cursor']==12 and report['projected']==0
    assert case.base.repo.get_cursor_pair(projection_cursor_key())==(12,34)


def scheduler_function():
    path=Path(__file__).resolve().parents[2]/'scheduler/main.py'
    tree=ast.parse(path.read_text(encoding='utf-8-sig'))
    node=next(item for item in tree.body if isinstance(item,ast.AsyncFunctionDef) and item.name=='run_memory_projection')
    calls=[]
    async def inline(func,**kw):return func(**kw)
    def forbidden(*a,**kw):raise AssertionError('retired scheduler must not calculate lag or send notifications')
    namespace={'asyncio':SimpleNamespace(to_thread=inline),'log':SimpleNamespace(info=lambda *a,**kw:calls.append((a,kw)),warning=lambda *a,**kw:calls.append((a,kw))),
        '_projection_lag':forbidden,'_notify':forbidden,'_projection_lag_notified_at':0}
    exec(compile(ast.Module(body=[node],type_ignores=[]),str(path),'exec'),namespace)
    return namespace['run_memory_projection'],calls


def test_actual_scheduler_function_stops_before_lag_alerts(case):
    freeze(case)
    run,calls=scheduler_function()
    coroutine=run()
    try:
        with pytest.raises(StopIteration):coroutine.send(None)
    finally:coroutine.close()
    assert len(calls)==1 and calls[0][0][0]=='memory_projection_retired'
    assert calls[0][1]['cursor']==12


def event():
    return Event.new(event_type=EventType.MESSAGE,event_id='new-arrival',occurred_at=datetime(2026,11,8),
        platform='wechat',bot_account_id='a',chat_id='same',content='我喜欢阅读新书',sequence=100)


def test_freeze_after_event_fetch_is_rechecked_before_projection(case):
    def fetch(*a,**kw):
        freeze(case)
        return [event()]
    case.projector.event_store=SimpleNamespace(since_sequence=fetch)
    stats=memory_runtime.project_pending_memory()
    assert stats['status']=='retired'
    assert case.base.repo.get('proj:new-arrival') is None
    assert case.base.repo.get_cursor_pair(projection_cursor_key())==(12,34)


def test_freeze_during_iteration_does_not_advance_cursor_or_claim_success(case):
    def events():
        freeze(case)
        yield event()
    case.projector.event_store=SimpleNamespace(since_sequence=lambda *a,**kw:events())
    first=memory_runtime.project_pending_memory()
    assert first.get('error') and first.get('status')!='retired'
    assert case.base.repo.get('proj:new-arrival') is None
    assert case.base.repo.get_cursor_pair(projection_cursor_key())==(12,34)
    assert memory_runtime.project_pending_memory()['status']=='retired'
