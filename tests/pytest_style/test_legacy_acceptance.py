from datetime import datetime
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
from types import SimpleNamespace

import pytest

from core.context import cognitive_commit as cc, situation_store as store
from core.memory import legacy_import as importer, belief_commit
from core.memory.legacy_acceptance import verify_import
from core.terminal import shadow
from core.terminal.event_model import Event, EventType
from test_legacy_belief_import import source_database, manifest


@pytest.fixture()
def imported(tmp_path, monkeypatch):
    source, target = tmp_path / 'source.db', tmp_path / 'target.db'
    monkeypatch.setattr(importer, 'SANDBOX_ROOTS', (tmp_path,))
    monkeypatch.setenv('YINYUE_COGNITIVE_DB', str(target))
    monkeypatch.setenv('YINYUE_WORLD_MODEL_DIR', str(tmp_path / 'world'))
    store.reset_world_models()
    source_database(source, second=True)
    review = manifest(source)
    review['reviews'][1]['decision'] = 'defer'
    importer.import_reviewed(source, source_id='meta', target=target, review=review, dry_run=False)
    yield source, target
    store.reset_world_models()


def verify(imported, at='2026-11-08'):
    source, target = imported
    return verify_import(source, source_id='meta', target=target, at=at)


def test_import_integrity_is_not_production_cutover_authorization(imported):
    source, target = imported
    before = source.read_bytes(), target.read_bytes()
    report = verify(imported)
    assert report['integrity_ok'] and not report['cutover_ready']
    assert report['counts']['imported'] == 1 and report['counts']['unimported'] == 1
    assert report['counts']['possible_legacy_overlaps'] == 1
    assert report['records'][0]['effective_state'] == 'original_current'
    assert '原始地址记录' not in json.dumps(report, ensure_ascii=False)
    assert before == (source.read_bytes(), target.read_bytes())


@pytest.mark.parametrize('change,reason', [
    ('content','source_row_changed'), ('evidence','source_evidence_changed_or_unbound'),
    ('scope','source_scope_changed'), ('remove','source_record_missing'),
])
def test_drift_is_reported_and_never_repaired(imported, change, reason):
    source, target = imported
    conn = sqlite3.connect(source)
    if change == 'content':
        conn.execute("UPDATE memory_projections SET content='PRIVATE CHANGED' WHERE memory_id='old-0'")
    elif change == 'evidence':
        conn.execute("UPDATE terminal_events SET content='PRIVATE CHANGED' WHERE event_id='event-0'")
    elif change == 'scope':
        conn.execute("UPDATE memory_projections SET metadata_json='{}',platform='',bot_account_id='' WHERE memory_id='old-0'")
        conn.execute("UPDATE terminal_events SET chat_id='other' WHERE event_id='event-0'")
    else:
        conn.execute("DELETE FROM memory_projections WHERE memory_id='old-0'")
    conn.commit()
    conn.close()
    before = source.read_bytes(), target.read_bytes()
    report = verify(imported)
    assert not report['integrity_ok'] and not report['cutover_ready']
    assert report['records'][0]['source_state'] == reason
    assert 'PRIVATE CHANGED' not in json.dumps(report)
    assert before == (source.read_bytes(), target.read_bytes())


def revise(monkeypatch, change_kind='correction', valid_from=''):
    scope = cc.Scope('wechat','a','same')
    offer = next(iter(belief_commit.offers(scope).values()))
    proof = Event.new(event_type=EventType.MESSAGE, event_id='correction', occurred_at=datetime(2026,11,8),
        platform='wechat',bot_account_id='a',chat_id='same',content='现在地址上海',actor_person_id='person-a')
    monkeypatch.setattr(shadow,'get_default_store',lambda: SimpleNamespace(get=lambda ref: proof))
    result = belief_commit.submit(offer,scope=scope,value='上海',change_kind=change_kind,
        valid_from=valid_from,evidence_ref='correction',visible=frozenset({'correction'}))
    assert result['result'] == 'committed'


def test_correction_marks_old_reader_overlap_without_resurrecting_original(imported, monkeypatch):
    revise(monkeypatch)
    store.reset_world_models()
    report = verify(imported)
    assert report['integrity_ok']
    assert report['records'][0]['effective_state'] == 'revised_current'
    assert report['records'][0]['legacy_reader_overlap_possible']
    assert not report['cutover_ready']
    # Explicit correction never makes the old erroneous value valid even at an earlier date.
    assert verify(imported, at='2026-06-01')['records'][0]['effective_state'] == 'revised_current'


def test_time_change_uses_same_validity_algorithm_as_live_reader(imported, monkeypatch):
    revise(monkeypatch, 'time_change', '2026-11-08')
    assert verify(imported, at='2026-11-07')['records'][0]['effective_state'] == 'original_current'
    assert verify(imported, at='2026-11-08')['records'][0]['effective_state'] == 'revised_current'
    assert verify(imported, at='2025-12-31')['records'][0]['effective_state'] == 'inactive_at_query_time'


@pytest.mark.parametrize('source_id', ['another',''])
def test_foreign_source_namespace_cannot_inspect_target_as_its_own(imported, source_id):
    source, target = imported
    with pytest.raises(ValueError):
        verify_import(source, source_id=source_id, target=target)


def test_absent_target_is_not_created(imported, tmp_path):
    source, _ = imported
    target = tmp_path / 'missing.db'
    with pytest.raises(FileNotFoundError):
        verify_import(source,source_id='meta',target=target)
    assert not target.exists()


def test_corrupt_journal_does_not_become_a_clean_report(imported):
    source, target = imported
    conn=sqlite3.connect(target)
    conn.execute("UPDATE cognitive_events SET payload='{}'")
    conn.commit();conn.close()
    before=target.read_bytes()
    with pytest.raises(ValueError,match='invalid_import_journal'):
        verify(imported)
    assert target.read_bytes()==before


def test_cli_separates_integrity_success_from_cutover_pending(imported):
    source,target=imported
    root=Path(__file__).resolve().parents[2]
    command=[sys.executable,'-I',str(root/'scripts/verify_legacy_import.py'),'--source',str(source),'--source-id','meta','--target',str(target),'--at','2026-11-08']
    result=subprocess.run(command,capture_output=True,encoding='utf-8',timeout=20)
    assert result.returncode==0,result.stdout+result.stderr
    assert json.loads(result.stdout)['integrity_ok']
    blocked=subprocess.run([*command,'--require-cutover'],capture_output=True,encoding='utf-8',timeout=20)
    assert blocked.returncode==2 and not json.loads(blocked.stdout)['cutover_ready']


@pytest.mark.parametrize('change', ['value','scope','evidence','subject','nonobject'])
def test_target_journal_cannot_claim_consistency_with_tampered_facts(imported, change):
    source,target=imported
    conn=sqlite3.connect(target)
    seq,raw=conn.execute('SELECT seq,payload FROM cognitive_events').fetchone()
    body=json.loads(raw)
    if change=='value':
        body['world_fact']['value']='TAMPERED'
    elif change=='scope':
        body['world_fact']['scope']='wechat:foreign:same'
    elif change=='evidence':
        body['world_fact']['evidence_refs']=['forged-proof']
    elif change=='subject':
        body['world_fact']['subject_key']='foreign'
    else:
        body=[]
    conn.execute('UPDATE cognitive_events SET payload=? WHERE seq=?',(json.dumps(body),seq))
    conn.commit();conn.close()
    with pytest.raises(ValueError,match='invalid_import_journal'):
        verify(imported)


def test_empty_committed_batch_is_not_a_successful_acceptance(imported):
    source,target=imported
    conn=sqlite3.connect(target)
    conn.execute('DELETE FROM cognitive_events')
    conn.commit();conn.close()
    report=verify(imported)
    assert not report['integrity_ok'] and report['counts']['imported']==0
    assert 'no_committed_imports' in report['pending']


def test_unsupported_source_rows_are_visible_in_acceptance_report(imported):
    source,target=imported
    conn=sqlite3.connect(source)
    conn.execute('ALTER TABLE memory_projections RENAME COLUMN content TO old_content')
    conn.commit();conn.close()
    report=verify(imported)
    assert not report['integrity_ok']
    assert report['source_tables']['memory_projections']=='unsupported_schema'
    assert report['source_omitted_rows']=={'memory_projections':2}
    assert report['records'][0]['source_state']=='source_record_missing'


def test_same_logical_source_in_an_offline_copy_is_not_mistaken_for_a_new_import(imported,tmp_path):
    import shutil
    source,target=imported
    copy=tmp_path/'offline-copy.db'
    shutil.copyfile(source,copy)
    assert verify_import(copy,source_id='meta',target=target,at='2026-11-08')==verify(imported)
