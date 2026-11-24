"""桥接一旦调用投递，异常/缺回执不能触发旧发送器 fallback。"""
import asyncio
from types import SimpleNamespace

import pytest
from core.contracts.delivery import Receipt
from core.speech import legacy_bridge as bridge, delivery
from core.speech.outbound import OutboundGateway


def run_inline(coro):
    try:
        coro.send(None)
    except StopIteration as result:
        return result.value
    raise AssertionError("controlled dispatch unexpectedly suspended")


@pytest.fixture(autouse=True)
def inline_thread_dispatch(monkeypatch):
    async def dispatch(fn, *args, **kwargs):
        return fn(*args, **kwargs)
    monkeypatch.setattr(asyncio, "to_thread", dispatch)


@pytest.fixture
def enabled_bridge(monkeypatch):
    monkeypatch.setattr(bridge,"legacy_via_exit_mode",lambda:"on")
    monkeypatch.setattr(bridge,"in_exit_scope",lambda *a:True)
    delivery.reset_idempotency()
    calls=[]
    def send(*a,**k):
        calls.append("physical_effect")
        return Receipt(status="sent",kind="say",target="room",ack="unconfirmed")
    adapter=SimpleNamespace(platform="wechat",send=send,capabilities=lambda:SimpleNamespace(enabled=True,kinds={"say"},targets=set()))
    reg=SimpleNamespace(get=lambda name:adapter,is_enabled=lambda name:True)
    return reg,calls


def test_sent_then_ledger_failure_preserves_send_result_and_bridge_ownership(enabled_bridge):
    reg,calls=enabled_bridge
    def ledger(row):
        raise RuntimeError("ledger failed after physical send")
    result=bridge.deliver_via_exit("wechat","room","same",registry=reg,ledger=ledger,turn_id="ledger-fail")
    assert calls==["physical_effect"]
    assert result.handled is True
    assert result.status=="sent"
    assert result.reason==""
    assert result.outcome.ok is True


@pytest.mark.parametrize("bad", [[],None,[None],{"status":"sent"}])
def test_missing_or_malformed_receipt_cannot_release_ownership(enabled_bridge,monkeypatch,bad):
    reg,_=enabled_bridge
    monkeypatch.setattr(delivery,"deliver",lambda *a,**k:bad)
    result=bridge.deliver_via_exit("wechat","room","same",registry=reg,ledger=lambda r:None)
    assert result.handled is True
    assert result.reason=="execution_unknown"


@pytest.mark.parametrize("result", [None,"raise"])
def test_outbound_thread_boundary_never_falls_back_after_dispatch(monkeypatch,result):
    monkeypatch.setattr(bridge,"legacy_via_exit_mode",lambda:"on")
    calls=[]
    def invoke(*a,**k):
        calls.append("dispatched")
        if result=="raise":
            raise RuntimeError("thread returned no response")
        return result
    monkeypatch.setattr(bridge,"deliver_via_exit",invoke)
    applied=SimpleNamespace(target=SimpleNamespace(platform="wechat",chat_id="room"),content="same",kind="text")
    got=run_inline(OutboundGateway._send_via_exit_bridge(None,applied,"room",None))
    assert calls==["dispatched"]
    assert got is not None
    assert "执行结果未知" in got.error


def test_shadow_dispatch_remains_dry_and_does_not_take_over(monkeypatch):
    monkeypatch.setattr(bridge,"legacy_via_exit_mode",lambda:"shadow")
    calls=[]
    def invoke(*a,**k):
        calls.append(k["dry_run"])
        return bridge.BridgeResult(False,status="skipped",reason="dry_run")
    monkeypatch.setattr(bridge,"deliver_via_exit",invoke)
    applied=SimpleNamespace(target=SimpleNamespace(platform="wechat",chat_id="room"),content="same",kind="text")
    assert run_inline(OutboundGateway._send_via_exit_bridge(None,applied,"room",None)) is None
    assert calls==[True]
