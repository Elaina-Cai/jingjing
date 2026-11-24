import asyncio
from types import SimpleNamespace

import pytest

from agents.tools.memory_index import ReadMemoryTool
from core.terminal.memory_gateway import MemoryGateway, MemoryHit


class Gateway(MemoryGateway):
    def __init__(self):
        self.calls = 0

    def principal_from_context(self, context, *, refresh=False):
        if getattr(context, "principal", None) is None:
            from core.terminal.principal import PrincipalContext, ChatType, RelationLevel
            data=context.extra
            context.principal=PrincipalContext(data['platform'],data['account_id'],data['chat_id'],
                ChatType.GROUP if data.get('is_group') else ChatType.PRIVATE,'person-a','A',RelationLevel.ORDINARY,
                group_id=data.get('group_id',data['chat_id'] if data.get('is_group') else ''))
        return context.principal

    def recall(self, principal, **kwargs):
        self.calls += 1
        return [MemoryHit(memory_id="secret", content="原始私聊正文", occurred_at=None, event_type="message", score=1)]


def context():
    return SimpleNamespace(extra={"platform": "wechat", "account_id": "bot", "chat_id": "wxid_a", "is_group": False, "sender_id": "a"})


def read(ctx):
    tool = ReadMemoryTool()
    tool.set_context(ctx)
    coro=tool.execute(keys="m1")
    try:
        coro.send(None)
    except StopIteration as done:
        return done.value
    raise AssertionError("index-only read unexpectedly suspended")


def test_same_context_can_read_immutable_index(monkeypatch):
    monkeypatch.setenv("YINYUE_MEMORY_INDEX", "1")
    ctx = context()
    gateway = Gateway()
    gateway.attach_to_context(ctx)
    # 取出来的是副本：改副本不影响快照里的正文（extra 里已没有可改的副本）。
    ctx.memory_index_snapshot.entries_for(ctx)[0]["content"] = "被替换的正文"
    result = read(ctx)
    assert "原始私聊正文" in result.output and "被替换" not in result.output


@pytest.mark.parametrize("key,value", [("chat_id", "room@chatroom"), ("account_id", "other"), ("platform", "qq"), ("sender_id", "b"), ("is_group", True)])
def test_changed_context_cannot_reuse_full_text(monkeypatch, key, value):
    monkeypatch.setenv("YINYUE_MEMORY_INDEX", "1")
    ctx = context()
    Gateway().attach_to_context(ctx)
    ctx.extra[key] = value
    result = read(ctx)
    assert result.error and "原始私聊正文" not in result.output


def test_raw_index_without_binding_cannot_read():
    ctx = context()
    ctx.extra["memory_index"] = [{"key": "m1", "content": "未授权正文"}]
    result = read(ctx)
    assert result.error and "未授权正文" not in result.output


def test_rebinding_context_does_not_reuse_cached_preview(monkeypatch):
    monkeypatch.setenv("YINYUE_MEMORY_INDEX", "1")
    ctx = context()
    gateway = Gateway()
    gateway.attach_to_context(ctx)
    ctx.extra["chat_id"] = "wxid_b"
    from dataclasses import replace
    ctx.principal=replace(ctx.principal,chat_id="wxid_b")
    gateway.attach_to_context(ctx)
    assert gateway.calls == 2


def principal():
    from core.terminal.principal import PrincipalContext, RelationLevel, ChatType
    return PrincipalContext("wechat", "bot", "wxid_a", ChatType.PRIVATE, "person-a", "A", RelationLevel.ORDINARY)


def test_changed_authorization_cannot_reuse_index(monkeypatch):
    from dataclasses import replace
    from core.terminal.principal import RelationLevel
    monkeypatch.setenv("YINYUE_MEMORY_INDEX", "1")
    ctx = context()
    ctx.principal = principal()
    Gateway().attach_to_context(ctx)
    ctx.principal = replace(ctx.principal, relation_to_bot=RelationLevel.UNKNOWN)
    assert read(ctx).error


def test_stale_principal_cannot_regenerate_foreign_preview(monkeypatch):
    monkeypatch.setenv("YINYUE_MEMORY_INDEX", "1")
    ctx = context()
    ctx.principal = principal()
    gateway = Gateway()
    gateway.attach_to_context(ctx)
    ctx.extra["chat_id"] = "wxid_b"
    assert gateway.attach_to_context(ctx) == ""
    assert gateway.calls == 1


@pytest.mark.parametrize('change',[{'group_id':'other-group'},{'metadata':(('membership_epoch','2'),)},{'intimacy':0.9}])
def test_entire_authorization_snapshot_is_bound(monkeypatch,change):
    from dataclasses import replace
    from core.terminal.principal import ChatType
    monkeypatch.setenv('YINYUE_MEMORY_INDEX','1')
    ctx=context()
    ctx.extra.update(chat_id='room',is_group=True,group_id='room')
    ctx.principal=replace(principal(),chat_id='room',chat_type=ChatType.GROUP,group_id='room')
    Gateway().attach_to_context(ctx)
    assert not read(ctx).error
    ctx.principal=replace(ctx.principal,**change)
    result=read(ctx)
    assert result.error and '原始私聊正文' not in result.output


def test_changed_group_coordinate_rejects_both_preview_and_full_text(monkeypatch):
    from dataclasses import replace
    from core.terminal.principal import ChatType
    monkeypatch.setenv('YINYUE_MEMORY_INDEX','1')
    ctx=context()
    ctx.extra.update(chat_id='room',is_group=True,group_id='room')
    ctx.principal=replace(principal(),chat_id='room',chat_type=ChatType.GROUP,group_id='room')
    gateway=Gateway()
    assert gateway.attach_to_context(ctx)
    ctx.extra['group_id']='foreign-group'
    assert read(ctx).error
    assert gateway.attach_to_context(ctx)==''
    assert gateway.calls==1
