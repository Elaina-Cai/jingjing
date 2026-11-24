from dataclasses import replace
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine

from core.terminal import memory_gateway as mg
from core.terminal.memory_projection import MemoryProjectionItem
from core.terminal.principal import PrincipalContext, ChatType, RelationLevel
from core.memory.contracts import MemoryItem, memory_item_from_hit
from core.memory.providers import PersonalMemoryProvider, KnowledgeMemoryProvider
from core.memory.hub import MemoryHub
from core.memory.recall_router import render_hits
from db.meta_models import MemoryProjectionRow
from db.repo_memory_projections import MemoryProjectionRepository

NOW = datetime(2026, 11, 8, 12)
P = PrincipalContext('wechat', 'a', 'chat', ChatType.PRIVATE, 'p', 'A', RelationLevel.OWNER)


@pytest.fixture(autouse=True)
def no_trace(monkeypatch):
    monkeypatch.setattr(mg, '_trace_recall', lambda *a, **k: None)
    monkeypatch.setattr(mg, 'memory_gate_enabled', lambda: False)


@pytest.fixture()
def repo(tmp_path):
    engine = create_engine('sqlite:///' + str(tmp_path / 'projections.db'))
    MemoryProjectionRow.__table__.create(engine)
    yield MemoryProjectionRepository(engine)
    engine.dispose()


def item(**changes):
    base = MemoryProjectionItem('proj:old', memory_type='fact', content='旧地址北京',
        platform='wechat', bot_account_id='a', source_event_ids=('original',),
        occurred_at=NOW - timedelta(days=10), metadata={'chat_id': 'chat', 'explicit_remember': True})
    return replace(base, **changes)


class ForbiddenStore:
    def query_visible(self, *a, **k):
        raise AssertionError('projection read must not fall back to original events')


@pytest.mark.parametrize('end', [NOW - timedelta(seconds=1), NOW])
def test_expired_or_corrected_projection_cannot_reappear_via_raw_events(repo, end):
    repo.upsert(item(valid_to=end))
    gateway = mg.MemoryGateway(projection_repository=repo, event_store=ForbiddenStore())
    assert gateway.recall(P, query='北京', now=NOW) == []


def test_unbounded_projection_remains_current_at_explicit_future_clock(repo):
    repo.upsert(item())
    gateway = mg.MemoryGateway(projection_repository=repo, event_store=ForbiddenStore())
    assert len(gateway.recall(P, now=NOW + timedelta(days=3660))) == 1


def test_future_end_is_not_closed_early(repo):
    repo.upsert(item(valid_to=NOW + timedelta(days=1)))
    gateway = mg.MemoryGateway(projection_repository=repo, event_store=ForbiddenStore())
    assert len(gateway.recall(P, now=NOW)) == 1


def test_empty_projection_store_has_no_implicit_raw_authority(repo):
    assert mg.MemoryGateway(projection_repository=repo, event_store=ForbiddenStore()).recall(P) == []


def test_projection_read_failure_does_not_change_authority(monkeypatch):
    def unavailable(**kwargs):
        raise OSError('offline projection store')
    gateway = mg.MemoryGateway(projection_repository=SimpleNamespace(query_candidates=unavailable), event_store=ForbiddenStore())
    assert gateway.recall(P) == []


def test_raw_event_mode_still_works_when_no_projection_repository():
    from core.terminal.event_model import Event, EventType
    event = Event.new(event_type=EventType.MESSAGE, event_id='original', content='他确实说过的话',
        occurred_at=NOW - timedelta(days=10), platform='wechat', bot_account_id='a', chat_id='chat')
    store = SimpleNamespace(query_visible=lambda *a, **k: [event])
    hits = mg.MemoryGateway(event_store=store).recall(P, now=NOW)
    assert [hit.memory_id for hit in hits] == ['original']


def test_vector_get_cannot_resurrect_invalidated_or_private_projection(repo):
    old = item(valid_to=NOW - timedelta(days=1))
    thought = item(memory_id='proj:thought', content='PRIVATE THOUGHT', sensitivity='owner_chain_of_thought')
    repo.upsert(old)
    repo.upsert(thought)
    # query_candidates misses these; vector IDs widen through get but must use the same gate.
    narrow = SimpleNamespace(query_candidates=lambda **kw: [], get=repo.get)
    gateway = mg.MemoryGateway(projection_repository=narrow, event_store=ForbiddenStore(),
        vector_recaller=lambda *a, **k: {old.memory_id: 1., thought.memory_id: 1.})
    assert gateway.recall(P, query='北京', now=NOW) == []


def test_real_hub_keeps_corrected_projection_closed_after_reopen(repo):
    repo.upsert(item())
    gateway = mg.MemoryGateway(projection_repository=repo, event_store=ForbiddenStore())
    hub = MemoryHub([PersonalMemoryProvider(gateway)])
    assert hub.recall(P, '北京', kinds=['fact'])
    repo.invalidate(['proj:old'], at=NOW - timedelta(days=1))
    repo.upsert(item())  # replay must preserve the invalidation tombstone
    reopened = MemoryHub([PersonalMemoryProvider(mg.MemoryGateway(projection_repository=MemoryProjectionRepository(repo.engine), event_store=ForbiddenStore()))])
    assert reopened.recall(P, '北京', kinds=['fact']) == []


def execute(tool, **kwargs):
    coro = tool.execute(**kwargs)
    try:
        with pytest.raises(StopIteration) as done:
            coro.send(None)
        return done.value.value
    finally:
        coro.close()


def summary():
    return memory_item_from_hit(mg.MemoryHit('proj:summary', '摘要中的地址上海', None, 'fact',
        source_event_ids=('remember-event',), is_explicit_remember=True, subject_person_ids=('person-a',)))


def test_conversion_preserves_provenance_without_promoting_summary_to_fact():
    hit = summary()
    assert hit.metadata['explicit_remember'] is True
    assert hit.metadata['provenance_status'] == 'source_linked'
    assert hit.subject_person_ids == ('person-a',)
    output = render_hits([hit])
    assert '[记忆摘要·未核实]' in output and '[事实]' not in output
    assert datetime.now().strftime('%m-%d') not in output


@pytest.mark.parametrize('kind,metadata,refs,label', [
    ('personal', {}, (), '[未核实旧记录]'),
    ('personal', {'memory_type':'fact'}, ('e',), '[某人自述]'),
    ('personal', {'memory_type':'self'}, ('e',), '[我说过]'),
    ('knowledge', {}, (), '[抓取资料·未核实]'),
    ('belief', {}, ('e',), '[事实版本]'),
    ('meme', {'source':'web'}, (), '[梗]·网络词典'),
    ('meme', {}, ('e',), '[梗]·本群'),
])
def test_source_label_agrees_between_body_and_index(kind, metadata, refs, label):
    hit = MemoryItem('id', kind, '可核对的记忆内容', metadata=metadata, source_event_ids=refs)
    assert label in render_hits([hit])
    entries = mg.index_entries([hit])
    assert entries[0]['source_label'] == label
    assert label in mg.render_index(entries)


@pytest.mark.parametrize("kind", ["notes", "belief"])
def test_unknown_evidence_state_is_visible_in_the_rendered_label(kind):
    hit = MemoryItem(
        "unknown-evidence",
        kind,
        "这条笔记的来源暂时无法核验",
        source_event_ids=("missing-event",),
        metadata={"evidence_state": "unknown"},
    )
    output = render_hits([hit])
    assert "依据未核实" in output


def test_mixed_sources_receive_independent_labels():
    hits = [summary(), MemoryItem('b', 'belief', '有效事实版本'),
        MemoryItem('k', 'knowledge', '网页抓取资料'), MemoryItem('w', 'meme', '网络词典条目', metadata={'source':'web'})]
    output = render_hits(hits)
    for label in ['[记忆摘要·未核实]', '[事实版本]', '[抓取资料·未核实]', '[梗]·网络词典']:
        assert output.count(label) == 1
    assert '[事实]' not in output


def test_index_tool_retains_summary_label(monkeypatch):
    from core.memory.index_scope import bind_index
    from agents.tools.memory_index import ReadMemoryTool
    ctx = SimpleNamespace(principal=P, extra={'platform':'wechat','account_id':'a','chat_id':'chat'})
    bind_index(ctx, mg.index_entries([summary()]), P)
    tool = ReadMemoryTool()
    tool.set_context(ctx)
    result = execute(tool, keys='m1')
    assert '[记忆摘要·未核实]' in result.output and '摘要中的地址上海' in result.output


@pytest.mark.parametrize('created,updated', [(datetime(2026,1,1), datetime(2026,10,1)), (None, datetime(2026,10,1))])
def test_knowledge_capture_time_is_not_update_or_fact_validity(created, updated):
    doc = SimpleNamespace(id=1, title='Python部署', content='Python部署的网页资料', created_at=created, updated_at=updated)
    provider = KnowledgeMemoryProvider(SimpleNamespace(knowledge_docs=SimpleNamespace(list_all=lambda **k: [doc])))
    hits = provider.recall(P, 'Python部署')
    assert len(hits) == 1
    hit = hits[0]
    assert hit.occurred_at == created
    assert hit.metadata['as_of'] == (created.isoformat() if created else '')
    assert hit.metadata['time_semantics'] == 'captured_at'
    assert hit.metadata['validity_status'] == 'unknown'
    assert hit.metadata['provenance_status'] == 'unverified_capture'


def test_legacy_recall_tool_uses_same_provenance_labels(monkeypatch):
    from agents.tools.memory import RecallMemoriesTool
    from core.execution import data_channels
    monkeypatch.setattr(data_channels, 'recall_items_for_context', lambda *a, **kw: [summary()])
    tool = RecallMemoriesTool(None, None, None)
    tool.set_context(SimpleNamespace(extra={}))
    result = execute(tool, query='地址')
    assert '[记忆摘要·未核实]' in result.output and '来源=event' not in result.output


def attached(repo, monkeypatch, *, index=True):
    monkeypatch.setenv('YINYUE_MEMORY_INDEX', 'on' if index else 'off')
    repo.upsert(item())
    ctx = SimpleNamespace(principal=P, extra={'platform':'wechat','account_id':'a','chat_id':'chat'})
    gateway = mg.MemoryGateway(projection_repository=repo, event_store=ForbiddenStore())
    monkeypatch.setattr(gateway, 'principal_from_context', lambda *a, **kw: P)
    block = gateway.attach_to_context(ctx)
    assert '旧地址北京' in block
    return gateway, ctx


@pytest.mark.parametrize('change', ['invalidate', 'content', 'visibility', 'evidence', 'remove', 'failure'])
def test_pushed_projection_index_revalidates_without_replacing_original_text(repo, monkeypatch, change):
    from agents.tools.memory_index import ReadMemoryTool
    gateway, ctx = attached(repo, monkeypatch)
    before = ctx.memory_index_snapshot.payload
    if change == 'invalidate':
        repo.invalidate(['proj:old'])
    elif change == 'content':
        repo.upsert(item(content='新正文不可偷换'))
    elif change == 'visibility':
        repo.upsert(item(sensitivity='owner_chain_of_thought'))
    elif change == 'evidence':
        repo.upsert(item(source_event_ids=('different-proof',)))
    elif change == 'remove':
        monkeypatch.setattr(repo, 'get', lambda key: None)
    else:
        def unavailable(key):
            raise OSError('offline')
        monkeypatch.setattr(repo, 'get', unavailable)
    tool = ReadMemoryTool()
    tool.set_context(ctx)
    result = execute(tool, keys='m1')
    assert result.error and '旧地址北京' not in result.output and '新正文' not in result.output
    assert ctx.memory_index_snapshot.payload == before


def test_index_revalidation_is_read_only_and_does_not_depend_on_current_ranking(repo, monkeypatch):
    from agents.tools.memory_index import ReadMemoryTool
    gateway, ctx = attached(repo, monkeypatch)
    before = repo.get('proj:old').as_dict()
    monkeypatch.setattr(repo, 'query_candidates', lambda **kw: [])
    tool = ReadMemoryTool()
    tool.set_context(ctx)
    assert '旧地址北京' in execute(tool, keys='m1').output
    assert repo.get('proj:old').as_dict() == before


@pytest.mark.parametrize('index', [True, False])
def test_cached_prompt_cannot_reuse_invalidated_projection(repo, monkeypatch, index):
    gateway, ctx = attached(repo, monkeypatch, index=index)
    repo.invalidate(['proj:old'])
    assert '旧地址北京' not in gateway.attach_to_context(ctx)
    assert ctx.extra['terminal_memory_count'] == 0


def test_runtime_factory_cannot_silently_select_raw_event_mode_on_projection_failure(monkeypatch):
    from core.terminal import event_runtime, store_port
    monkeypatch.setattr(mg, '_gateway', None)
    monkeypatch.setattr(mg, '_error', '')
    monkeypatch.setattr(event_runtime, 'get_event_identity_service', lambda: object())
    def unavailable():
        raise OSError('projection database unavailable')
    monkeypatch.setattr(store_port, 'get_meta_repo', unavailable)
    assert mg.get_memory_gateway() is None
    assert 'projection database unavailable' in mg._error
    assert mg._gateway is None
