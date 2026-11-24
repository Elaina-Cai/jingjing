from __future__ import annotations

import asyncio
from contextlib import contextmanager
from types import SimpleNamespace

from agents import proactive_trigger


def test_proactive_turn_binds_memory_and_passes_it_to_agent(monkeypatch):
    import core.bootstrap as bootstrap
    import core.speech.turn as speech_turn
    from core.config.settings import config

    order = []
    captured = {}

    class Runtime:
        default = SimpleNamespace(account_id="acct", repo=object())

        def get(self, platform, account_id):
            return self.default

        @contextmanager
        def activate(self, account):
            captured["activated"] = account
            yield

    class Agent:
        async def execute(self, context):
            order.append("execute")
            captured["prompt"] = context.extra["user_message"]
            captured["supported"] = context.extra["memory_claim_supported_texts"]
            return SimpleNamespace(
                output={"reply": "今天想跟你说句话"},
                tools_used=(),
            )

    class Gateway:
        async def send_text(self, draft, snapshot, context):
            order.append("send")
            captured["draft"] = draft
            captured["context"] = context
            return SimpleNamespace(ok=True, audit_id=1)

    def bind(context):
        order.append("bind")
        return "m1 [依据未核实] address = 深圳"

    monkeypatch.setattr(bootstrap, "get_runtime", lambda: Runtime())
    monkeypatch.setattr(bootstrap, "build_agent", lambda: Agent())
    monkeypatch.setattr(bootstrap, "get_tools", lambda: None)
    monkeypatch.setattr(bootstrap, "get_outbound_gateway", lambda: Gateway())
    monkeypatch.setattr(
        speech_turn,
        "snapshot_from_context",
        lambda *args, **kwargs: SimpleNamespace(
            turn=SimpleNamespace(
                request_id="request-1",
                target=SimpleNamespace(),
            )
        ),
    )
    monkeypatch.setattr(type(config), "get_all_accounts", lambda self: {})
    monkeypatch.setattr(type(config), "get_all_owner_accounts", lambda self: {})
    monkeypatch.setattr(proactive_trigger, "bind_proactive_memory", bind)

    result = asyncio.run(proactive_trigger.run_proactive_turn(
        platform="wechat",
        bot_account_id="acct",
        chat_id="c1",
        chat_type="private",
        hint="主动说一句自然的话",
        source="manual",
        trigger_kind="proactive",
        enforce_proactive_policy=False,
        reply_prefix="",
        supported_texts=(),
    ))

    assert result.ok is True and result.sent is True
    assert order == ["bind", "execute", "send"]
    assert "[本轮可用记忆]" in captured["prompt"]
    assert "m1 [依据未核实] address = 深圳" in captured["prompt"]
    assert captured["supported"] == []
    assert captured["draft"].content == "今天想跟你说句话"
    assert captured["context"].memory_index_snapshot is None
