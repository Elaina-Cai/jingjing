"""出口只能凭程序侧未执行证据重试，不能凭错误码推断。"""
from types import SimpleNamespace

import pytest
from core.contracts.delivery import DeliverItem, Receipt
from core.execution.wechat_adapter import WechatAdapter, build_registry
from core.speech import delivery


def run(send, *, dry=False):
    delivery.reset_idempotency()
    adapter=SimpleNamespace(platform="fake",send=send,capabilities=lambda:SimpleNamespace(enabled=True,kinds={"say"},targets=set()))
    return delivery.deliver([DeliverItem(kind="say",target="room",text="same")],turn_id="retry-proof",registry={"fake":adapter},platform="fake",backoff_s=0,dry_run=dry)


@pytest.mark.parametrize("proof", [False,None,"true",1])
def test_error_code_without_literal_unexecuted_proof_never_retries(proof):
    calls=[]
    def send(kind,target,payload):
        calls.append(payload)
        return Receipt(status="failed",kind=kind,target=target,ack="error",reason="network",not_executed=proof)
    result=run(send)
    assert len(calls)==1
    assert result[0].status=="failed"


def test_explicit_unexecuted_proof_retries_once_and_survives_context_copy():
    calls=[]
    def send(kind,target,payload):
        calls.append(payload)
        if len(calls)==1:
            return Receipt(status="failed",kind=kind,target=target,ack="error",reason="network",not_executed=True)
        return Receipt(status="sent",kind=kind,target=target,ack="unconfirmed")
    result=run(send)
    assert len(calls)==2
    assert calls[0]==calls[1]
    assert result[0].status=="sent"
    assert result[0].adapter=="fake"


def test_repeated_unexecuted_failure_still_respects_retry_limit():
    calls=[]
    def send(kind,target,payload):
        calls.append(payload)
        return Receipt(status="failed",kind=kind,target=target,reason="platform_error",not_executed=True)
    assert run(send)[0].not_executed is True
    assert len(calls)==2


@pytest.mark.parametrize("wechat_wrapper", [False,True])
def test_side_effect_then_exception_cannot_trigger_second_send(wechat_wrapper,monkeypatch):
    calls=[]
    monkeypatch.setattr(delivery,"warn",lambda *a,**k:None)
    def send(*a,**k):
        calls.append("physical_effect")
        raise RuntimeError("response lost after sending")
    if wechat_wrapper:
        reg=build_registry(adapter=WechatAdapter(send),enable=True)
        delivery.reset_idempotency()
        result=delivery.deliver([DeliverItem(kind="say",target="room",text="same")],turn_id="wx-proof",registry=reg,platform="wechat",backoff_s=0)
    else:
        result=run(send)
    assert calls==["physical_effect"]
    assert result[0].not_executed is False
    assert result[0].status == "partial"
    assert result[0].ack == "unconfirmed"
    assert result[0].reason == "execution_unknown"
    from core.speech.legacy_bridge import receipt_outcome
    outcome=receipt_outcome(result[0])
    assert "执行结果未知" in outcome.error
    assert result[0].platform_msg_id == result[0].client_msg_id == ""



def test_dry_run_never_invokes_adapter():
    def forbidden(*a,**k):
        raise AssertionError("dry cannot call adapter")
    assert run(forbidden,dry=True)[0].reason=="dry_run"


@pytest.mark.parametrize("bad_result", [None, {"status":"sent"}, SimpleNamespace(status="garbage")])
def test_malformed_adapter_result_remains_unknown_without_retry(bad_result):
    calls=[]
    def send(*a,**k):
        calls.append(1)
        return bad_result
    result=run(send)[0]
    assert calls==[1]
    assert (result.status,result.ack,result.reason)==("partial","unconfirmed","execution_unknown")


@pytest.mark.parametrize("bad_result", [None, SimpleNamespace(status="garbage")])
def test_wechat_malformed_result_is_unknown(bad_result):
    result=WechatAdapter(lambda *a,**k:bad_result).send("say","room",{"text":"same"})
    assert (result.status,result.ack,result.reason)==("partial","unconfirmed","execution_unknown")
