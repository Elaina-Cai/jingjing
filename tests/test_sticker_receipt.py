# -*- coding: utf-8 -*-
"""贴纸路径也要写回执（`docs/decisions/出口.md` §5.3）。

事故背景：文字路径 `_record_delivery_receipt` 早在 2026-10 就接上了，贴纸路径
（provider 直发 / IntentBus / 延迟派发）一条都没写 —— 于是 `delivery_receipts`
里"她发过什么"永远缺表情包那一半，重复发送率这个量具只量到一半的样本。

这里钉四条：三条贴纸成功路径各写一行、都带 `kind=sticker`、都不传 skip（贴纸没有
"新出口自己已写过"的情形）、失败路径一行都不写。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.speech import outbound as OB  # noqa: E402
import core.speech.receipts as receipts_mod  # noqa: E402

FAILS: list[str] = []
COUNT = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global COUNT
    COUNT += 1
    if cond:
        print("  ok  " + name)
    else:
        FAILS.append(name)
        print("  FAIL " + name + ((" :: " + str(detail)) if detail else ""))


TARGET = SimpleNamespace(platform="wechat", chat_id="wxid_c", account_id="bot1",
                         chat_type="private")


def _snapshot(platform="wechat"):
    return SimpleNamespace(
        turn=SimpleNamespace(
            trigger_reason="mention",
            target=SimpleNamespace(platform=platform, chat_id="wxid_c",
                                   account_id="bot1", chat_type="private"),
        ),
        audience=None,
    )


def _draft(platform="wechat"):
    return OB.OutboundDraft(
        request_id="req-1",
        kind="sticker",
        target=SimpleNamespace(platform=platform, chat_id="wxid_c",
                               account_id="bot1", chat_type="private"),
        account_id="bot1",
        who="wxid_c",
        mood_id="daily",
        emotion="日常",
        extra={"prechecked_decision": object()},
    )


def _gateway():
    gw = OB.OutboundGateway(SimpleNamespace())
    gw.apply = lambda draft, decision: (draft, "")
    gw._resolve_account_id = lambda applied, snapshot, context: "bot1"
    gw.record = lambda *a, **k: "audit-1"
    gw._mark_context_sent = lambda *a, **k: None
    gw._record_proactive_send = lambda *a, **k: None
    return gw


class _Bus:
    def __init__(self, ok=True):
        self.ok = ok

    async def dispatch(self, intent):
        from core.execution.intent_bus import IntentResult
        return IntentResult(ok=self.ok, intent=intent,
                            error="" if self.ok else "发送失败")


def _capture():
    calls: list[dict] = []

    def fake(applied, result, **kw):
        calls.append({"kind": getattr(applied, "kind", ""),
                      "target": getattr(getattr(applied, "target", None), "chat_id", ""),
                      "kw": kw, "ok": getattr(result, "ok", None)})
        return True

    return calls, fake


def _run(coro):
    return asyncio.run(coro)


def t_bus_path_writes_receipt() -> None:
    calls, fake = _capture()
    old_rec, old_bus = receipts_mod.record_delivery_receipt, None
    receipts_mod.record_delivery_receipt = fake
    import core.execution.intent_bus as bus_mod
    old_bus = bus_mod.get_intent_bus
    bus_mod.get_intent_bus = lambda: _Bus(ok=True)
    try:
        res = _run(_gateway().send_sticker(_draft(), _snapshot(), None))
        check("IntentBus 路径发送成功", getattr(res, "ok", False), res)
        check("  ⇒ 写且只写一行回执", len(calls) == 1, calls)
        if calls:
            check("  ⇒ 回执记的是贴纸", calls[0]["kind"] == "sticker", calls[0])
            check("  ⇒ 回执带会话 id", calls[0]["target"] == "wxid_c", calls[0])
    finally:
        receipts_mod.record_delivery_receipt = old_rec
        bus_mod.get_intent_bus = old_bus


def t_provider_path_writes_receipt() -> None:
    calls, fake = _capture()

    class _Provider:
        capabilities = ("sticker",)

        async def send_sticker(self, who, mood_id="", emotion=""):
            return SimpleNamespace(ok=True, msg_id="m1", error="")

    class _PM:
        def get_by_account(self, platform, account_id):
            return _Provider()

    old_rec = receipts_mod.record_delivery_receipt
    receipts_mod.record_delivery_receipt = fake
    import core.bootstrap as bs
    old_get = bs.get_provider_manager
    bs.get_provider_manager = lambda: _PM()
    try:
        res = _run(_gateway().send_sticker(_draft(platform="qq"), _snapshot("qq"), None))
        check("非微信 provider 路径发送成功", getattr(res, "ok", False), res)
        check("  ⇒ 写且只写一行回执", len(calls) == 1, calls)
        if calls:
            check("  ⇒ 回执记的是贴纸", calls[0]["kind"] == "sticker", calls[0])
    finally:
        receipts_mod.record_delivery_receipt = old_rec
        bs.get_provider_manager = old_get


def t_failure_writes_nothing() -> None:
    calls, fake = _capture()
    old_rec = receipts_mod.record_delivery_receipt
    receipts_mod.record_delivery_receipt = fake
    import core.execution.intent_bus as bus_mod
    old_bus = bus_mod.get_intent_bus
    bus_mod.get_intent_bus = lambda: _Bus(ok=False)
    try:
        res = _run(_gateway().send_sticker(_draft(), _snapshot(), None))
        check("发送失败时不写回执（回执只记真发出去的）",
              getattr(res, "ok", True) is False and calls == [], (res, calls))
    finally:
        receipts_mod.record_delivery_receipt = old_rec
        bus_mod.get_intent_bus = old_bus


def t_pending_sticker_writes_receipt() -> None:
    from core.execution.intent_bus import Intent, IntentType

    calls, fake = _capture()
    old_rec = receipts_mod.record_delivery_receipt
    receipts_mod.record_delivery_receipt = fake
    import core.execution.intent_bus as bus_mod
    old_bus = bus_mod.get_intent_bus
    bus_mod.get_intent_bus = lambda: _Bus(ok=True)
    try:
        intent = Intent(intent_type=IntentType.SEND_STICKER, platform="wechat",
                        account_id="bot1", chat_id="wxid_c",
                        mood_id="daily", emotion="日常", extra={})
        gw = _gateway()
        res = _run(gw.dispatch_pending_sticker(intent, _snapshot(), None))
        check("延迟派发成功", getattr(res, "ok", False), res)
        check("  ⇒ 也写一行回执", len(calls) == 1, calls)
        if calls:
            check("  ⇒ 回执记的是贴纸", calls[0]["kind"] == "sticker", calls[0])
    finally:
        receipts_mod.record_delivery_receipt = old_rec
        bus_mod.get_intent_bus = old_bus


def t_no_skip_for_stickers() -> None:
    src = (ROOT / "core" / "speech" / "outbound.py").read_text(encoding="utf-8")
    sticker_fn = src[src.index("    async def send_sticker("):src.index("    async def dispatch_pending_sticker(")]
    check("贴纸路径不传 skip（没有 '新出口已写过' 的情形）", "skip=" not in sticker_fn)
    check("文字路径的 skip 判据没被顺手删掉", "skip=bridged is not None" in src)


def main() -> int:
    for fn in (
        t_bus_path_writes_receipt,
        t_provider_path_writes_receipt,
        t_failure_writes_nothing,
        t_pending_sticker_writes_receipt,
        t_no_skip_for_stickers,
    ):
        print("\n" + fn.__name__)
        fn()
    print(f"\nRESULT {len(FAILS)} failed / {COUNT} checks")
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())