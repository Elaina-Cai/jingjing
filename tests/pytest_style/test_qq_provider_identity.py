"""QQ provider 的真实入口 + 真实 AccountLinkGraph 撤链。

不连真实 NapCat / 不发网络请求：send 打桩到 _api_call，只验证 provider
把群/私聊映射到正确的 OneBot action 与 payload。身份侧用真实
IdentityService + AccountLinkGraph，验证 remove_link 后 component 立即收缩。
"""

from __future__ import annotations

import asyncio

from core.execution.qq_provider import QQProvider
from core.terminal.identity_service import IdentityService
from core.terminal.link_graph import AccountLinkGraph
from core.terminal.principal import AccountRef


def _run(coro):
    return asyncio.run(coro)


def _provider(*, enabled=True, ws_url=""):
    return QQProvider(
        account_id="bot-qq",
        ws_url=ws_url,
        http_url="http://onebot.invalid",
        access_token="",
        send_enabled=enabled,
        media_dir="",
        send_min_interval=0,
    )


def _patch_send(provider, monkeypatch):
    seen = {}

    async def fake_api(action, payload):
        seen["action"] = action
        seen["payload"] = payload
        return {"status": "ok", "retcode": 0, "data": {"message_id": "42"}}

    monkeypatch.setattr(provider, "_api_call", fake_api)
    monkeypatch.setattr("core.features.dry_run_send", lambda chat_id: False)
    return seen


def test_qq_provider_send_group_uses_send_group_msg(monkeypatch):
    provider = _provider()
    seen = _patch_send(provider, monkeypatch)
    result = _run(provider.send("group:333333", "你好"))
    assert result.ok is True and result.msg_id == "42"
    assert seen["action"] == "send_group_msg"
    assert seen["payload"]["group_id"] == 333333
    assert seen["payload"]["message"] == "你好"


def test_qq_provider_send_private_uses_send_private_msg(monkeypatch):
    provider = _provider()
    seen = _patch_send(provider, monkeypatch)
    result = _run(provider.send("user:444444", "在吗"))
    assert result.ok is True
    assert seen["action"] == "send_private_msg"
    assert seen["payload"]["user_id"] == 444444


def test_qq_provider_send_disabled_never_calls_transport(monkeypatch):
    provider = _provider(enabled=False)
    seen = _patch_send(provider, monkeypatch)
    result = _run(provider.send("user:444444", "在吗"))
    assert result.ok is False
    assert "未启用" in result.error
    assert seen == {}


def test_qq_provider_connect_without_ws_url_is_false():
    provider = _provider(ws_url="")
    assert _run(provider.connect()) is False


def test_real_account_link_graph_revocation_shrinks_component():
    qq = AccountRef("qq", "10001")
    wx = AccountRef("wechat", "bot-wx")
    graph = AccountLinkGraph([(qq, wx)])
    service = IdentityService(link_graph=graph)
    assert service.component_accounts(qq) == frozenset({qq, wx})
    before = graph.revision
    graph.remove_link(qq, wx)
    assert graph.revision == before + 1
    service.replace_link_graph(graph)
    assert service.component_accounts(qq) == frozenset({qq})
    assert service.component_accounts(wx) == frozenset({wx})
