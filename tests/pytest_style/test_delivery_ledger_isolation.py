"""发送事实不能被旁注回调失败覆盖。"""
from types import SimpleNamespace

import pytest
from core.contracts.delivery import DeliverItem, Receipt
from core.speech import delivery


@pytest.mark.parametrize("status,ack,reason", [("sent","unconfirmed",""),("partial","unconfirmed","execution_unknown"),("failed","error","platform_error")])
def test_ledger_failure_preserves_receipt_and_prevents_same_turn_resend(monkeypatch,status,ack,reason):
    delivery.reset_idempotency()
    calls,events,records=[],[],[]
    def send(kind,target,payload):
        calls.append(payload)
        return Receipt(status=status,kind=kind,target=target,ack=ack,reason=reason)
    def ledger(receipt):
        records.append(receipt)
        raise RuntimeError("read-only ledger")
    monkeypatch.setattr(delivery,"warn",lambda event,**fields:events.append((event,fields)))
    adapter=SimpleNamespace(platform="fake",send=send,capabilities=lambda:SimpleNamespace(enabled=True,kinds={"say"},targets=set()))
    item=DeliverItem(kind="say",target="room",text="same")
    result=delivery.deliver([item],turn_id="ledger-proof",registry={"fake":adapter},platform="fake",ledger=ledger,backoff_s=0)[0]
    assert (result.status,result.ack,result.reason)==(status,ack,reason)
    assert len(calls)==len(records)==1
    assert records[0] is result
    assert events[0][0]=="delivery_ledger_write_failed"
    assert events[0][1]["turn_id"]=="ledger-proof"
    assert events[0][1]["target"]=="room"
    again=delivery.deliver([item],turn_id="ledger-proof",registry={"fake":adapter},platform="fake",backoff_s=0)[0]
    assert again.reason=="duplicate"
    assert len(calls)==1


def test_dry_result_survives_ledger_failure_without_sending(monkeypatch):
    delivery.reset_idempotency()
    def forbidden(*a,**k):
        raise AssertionError("dry must not send")
    def ledger(receipt):
        raise RuntimeError("ledger unavailable")
    monkeypatch.setattr(delivery,"warn",lambda *a,**k:None)
    adapter=SimpleNamespace(platform="fake",send=forbidden,capabilities=lambda:SimpleNamespace(enabled=True,kinds={"say"},targets=set()))
    result=delivery.deliver([DeliverItem(kind="say",target="room",text="same")],turn_id="dry-ledger",registry={"fake":adapter},platform="fake",dry_run=True,ledger=ledger)[0]
    assert result.status=="skipped"
    assert result.reason=="dry_run"
