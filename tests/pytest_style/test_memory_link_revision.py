from dataclasses import replace
from types import SimpleNamespace

from core.terminal.memory_gateway import MemoryGateway, MemoryHit
from core.terminal.principal import ChatType, PrincipalContext, RelationLevel


class FakeLinkService:
    def __init__(self, gateway):
        self.gateway = gateway

    def replace_link_graph(self, graph=None):
        self.gateway.revoked = True
        return True


class RevocableGateway(MemoryGateway):
    def __init__(self):
        self.calls = 0
        self.revoked = False
        self.identity_service = FakeLinkService(self)
        self.authority_service = None
        self._link_revision = 1

    def principal_from_context(self, context, *, refresh=False):
        principal = context.principal
        if refresh and self.revoked:
            return replace(
                principal,
                relation_to_bot=RelationLevel.UNKNOWN,
                memory_scopes=frozenset(),
            )
        return principal

    def recall(self, principal, **kwargs):
        self.calls += 1
        if self.revoked:
            return []
        return [
            MemoryHit(
                memory_id="secret",
                content="撤销前可见的私聊正文",
                occurred_at=None,
                event_type="message",
                score=1.0,
            )
        ]


def principal():
    return PrincipalContext(
        "wechat",
        "bot",
        "wxid_a",
        ChatType.PRIVATE,
        "person-a",
        "A",
        RelationLevel.ORDINARY,
    )


def context():
    return SimpleNamespace(
        extra={
            "platform": "wechat",
            "account_id": "bot",
            "chat_id": "wxid_a",
            "is_group": False,
            "sender_id": "a",
        },
        principal=principal(),
    )


def test_link_revision_change_invalidates_bound_index(monkeypatch):
    monkeypatch.setenv("YINYUE_MEMORY_INDEX", "1")
    ctx = context()
    gateway = RevocableGateway()
    gateway.attach_to_context(ctx)
    assert gateway.calls == 1
    assert "撤销前可见的私聊正文" in ctx.extra["terminal_memory_block"]

    from core.terminal import link_runtime

    monkeypatch.setattr(link_runtime, "current_link_revision", lambda: 2)
    monkeypatch.setattr(
        link_runtime,
        "refresh_identity_service_links",
        lambda service: service.replace_link_graph(None),
    )

    gateway.attach_to_context(ctx)
    assert gateway.revoked is True
    assert gateway.calls == 2
    assert ctx.extra["terminal_memory_block"] == ""


def _read(ctx):
    from agents.tools.memory_index import ReadMemoryTool

    tool = ReadMemoryTool()
    tool.set_context(ctx)
    coro = tool.execute(keys="m1")
    try:
        coro.send(None)
    except StopIteration as done:
        return done.value
    raise AssertionError("index-only read unexpectedly suspended")


def test_bound_index_stops_reading_after_link_revision_change(monkeypatch):
    monkeypatch.setenv("YINYUE_MEMORY_INDEX", "1")
    from core.terminal import link_runtime

    monkeypatch.setattr(link_runtime, "current_link_revision", lambda: 1)
    ctx = context()
    gateway = RevocableGateway()
    gateway._link_revision = 1
    gateway.attach_to_context(ctx)
    first = _read(ctx)
    assert "撤销前可见的私聊正文" in first.output

    monkeypatch.setattr(link_runtime, "current_link_revision", lambda: 99)
    result = _read(ctx)
    assert result.error == "no_memory_index"
    assert "撤销前可见的私聊正文" not in result.output


def test_real_graph_revocation_reaches_real_gateway_and_previously_bound_index(tmp_path, monkeypatch):
    from datetime import datetime
    from sqlalchemy import create_engine
    from core.terminal.identity_service import IdentityService
    from core.terminal.link_graph import AccountLinkGraph
    from core.terminal.principal import AccountRef, RelationshipState
    from core.terminal.memory_projection import MemoryProjectionItem
    from db.meta_models import MemoryProjectionRow
    from db.repo_memory_projections import MemoryProjectionRepository
    from core.terminal import link_runtime, memory_gateway
    qq, wx = AccountRef("qq", "qq-bot"), AccountRef("wechat", "wx-bot")
    graph = AccountLinkGraph([(qq, wx)])
    service = IdentityService(link_graph=graph,
        person_identities={("wechat", "sender"): "person-a"},
        relationships={("qq", "qq-bot", "person-a"): RelationshipState("qq", "qq-bot", "person-a", RelationLevel.ORDINARY)})
    monkeypatch.setattr(link_runtime, "current_link_revision", lambda: graph.revision)
    monkeypatch.setattr(link_runtime, "load_live_link_graph", lambda: graph)
    monkeypatch.setenv("YINYUE_MEMORY_INDEX", "on")
    monkeypatch.setattr(memory_gateway, "memory_gate_enabled", lambda: False)
    monkeypatch.setattr(memory_gateway, "_trace_recall", lambda *a, **kw: None)
    engine = create_engine("sqlite:///" + str(tmp_path / "projections.db"))
    try:
        MemoryProjectionRow.__table__.create(engine)
        repo = MemoryProjectionRepository(engine)
        repo.upsert(MemoryProjectionItem("proj:linked", memory_type="fact", content="明确关联才可见的资料",
            platform="qq", bot_account_id="qq-bot", visibility_scope=("issuer_only",),
            occurred_at=datetime(2026, 11, 8), source_event_ids=("native-source",), metadata={"chat_id": "qq-private"}))
        gateway = MemoryGateway(identity_service=service, projection_repository=repo)
        ctx = SimpleNamespace(extra={"platform": "wechat", "account_id": "wx-bot", "chat_id": "sender", "sender_id": "sender"})
        gateway.attach_to_context(ctx)
        assert "明确关联才可见的资料" in _read(ctx).output
        graph.remove_link(qq, wx)
        assert "明确关联才可见的资料" not in _read(ctx).output
        gateway.attach_to_context(ctx)
        assert "明确关联才可见的资料" not in ctx.extra.get("terminal_memory_block", "")
        assert gateway.recall(gateway.principal_from_context(ctx, refresh=True)) == []
    finally:
        engine.dispose()
