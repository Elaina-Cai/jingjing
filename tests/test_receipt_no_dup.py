"""回执只落一行：出口收对象、桥接管时不重复记账（2026-10-21 真机抓到）。

真机现象：私聊两条都发成功了，但每条都留了两行回执（`wechat_send` 一行带
`ref=wechat#<local_id>`，出口又写一行 `ref=""`）。重复发送率是我们唯一的
安全量具，用自己的重复记账污染它等于把量具调坏。
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.contracts.delivery import Receipt  # noqa: E402
from core.terminal import delivery_receipts as dr  # noqa: E402

FAILS: list[str] = []
COUNT = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global COUNT
    COUNT += 1
    if cond:
        print(f"  ok {name}")
    else:
        FAILS.append(f"{name} {detail}")
        print(f"  FAIL {name} {detail}")


class _Cursor:
    rowcount = 1


class _Conn:
    def __init__(self, bag: list) -> None:
        self._bag = bag

    def execute(self, _sql, params=None):
        self._bag.append(dict(params or {}))
        return _Cursor()


class _Engine:
    def __init__(self) -> None:
        self.bag: list[dict] = []

    def begin(self):
        bag = self.bag

        class _Ctx:
            def __enter__(self_inner):
                return _Conn(bag)

            def __exit__(self_inner, *exc):
                return False

        return _Ctx()


def _with_engine(mode: str):
    engine = _Engine()
    old_get, old_mode = dr._get_engine, dr._mode
    dr._get_engine = lambda: engine
    dr._mode = lambda: mode
    return engine, (old_get, old_mode)


def _restore(saved) -> None:
    dr._get_engine, dr._mode = saved


def t_receipt_object_is_accepted() -> None:
    engine, saved = _with_engine("shadow")
    try:
        ok = dr.record_receipt(
            Receipt(status="sent", kind="say", target="wxid_x", ack="platform_id",
                    platform_msg_id="123", detail="local_id=9 server_id=123"),
            turn_id="t1", chat_id="wxid_x",
        )
    finally:
        _restore(saved)
    check("对象也收：返回插入成功", ok is True, f"ok={ok}")
    check("对象也收：一行", len(engine.bag) == 1, str(len(engine.bag)))
    row = engine.bag[0] if engine.bag else {}
    check("对象也收：status 对", row.get("status") == "sent", row.get("status"))
    check("对象也收：platform_msg_id 对", row.get("platform_msg_id") == "123", row.get("platform_msg_id"))
    check("对象也收：detail 对", "local_id=9" in str(row.get("detail")), row.get("detail"))


def t_mapping_still_works() -> None:
    engine, saved = _with_engine("shadow")
    try:
        ok = dr.record_receipt({"status": "failed", "kind": "say", "target": "wxid_y"},
                               turn_id="t2")
    finally:
        _restore(saved)
    check("dict 照旧", ok is True and engine.bag[0]["status"] == "failed", str(engine.bag))


def t_off_and_empty_turn_write_nothing() -> None:
    engine, saved = _with_engine("off")
    try:
        ok_off = dr.record_receipt({"status": "sent"}, turn_id="t3")
    finally:
        _restore(saved)
    check("off 档不写", ok_off is False and not engine.bag, str(engine.bag))
    engine2, saved2 = _with_engine("shadow")
    try:
        ok_empty = dr.record_receipt({"status": "sent"}, turn_id="")
    finally:
        _restore(saved2)
    check("空 turn_id 不写", ok_empty is False and not engine2.bag, str(engine2.bag))


def t_bridge_skip() -> None:
    from core.speech import outbound as ob
    import core.speech.receipts as rc

    calls: list[dict] = []
    old = rc.record_delivery_receipt
    rc.record_delivery_receipt = lambda *a, **kw: calls.append({"args": a, "kw": kw})
    gw = object.__new__(ob.OutboundGateway)
    applied = SimpleNamespace(target=SimpleNamespace(chat_id="wxid_x"), who="Elaina")
    try:
        gw._record_delivery_receipt(applied, object(), "", None, skip=True)
        check("桥接管：出口一行都不写", not calls, str(calls))
        gw._record_delivery_receipt(applied, object(), "", None)
        check("旧路：照旧写一行", len(calls) == 1, str(calls))
        check("旧路：chat_id 取 target", calls and calls[0]["kw"].get("chat_id") == "wxid_x", str(calls[:1]))
    finally:
        rc.record_delivery_receipt = old


def t_static() -> None:
    import inspect

    from core.speech.outbound import OutboundGateway

    params = inspect.signature(OutboundGateway._record_delivery_receipt).parameters
    skip = params.get("skip")
    check("静态：skip 是 keyword-only 且缺省 False",
          skip is not None and skip.kind is inspect.Parameter.KEYWORD_ONLY
          and skip.default is False, str(skip))
    # 「调用点传 skip」「skip 早退」这两条的真身由上面 t_bridge_skip 的
    # 「桥接管：出口一行都不写 / 旧路：照旧写一行」直接回答，不再读源码文本。
    dsrc = (ROOT / "core/terminal/delivery_receipts.py").read_text(encoding="utf-8")
    check("静态：record_receipt 里转换", "receipt = _as_mapping(receipt)" in dsrc)


def main() -> int:
    for fn in (t_receipt_object_is_accepted, t_mapping_still_works,
               t_off_and_empty_turn_write_nothing, t_bridge_skip, t_static):
        print(fn.__name__)
        fn()
    print(f"RESULT {len(FAILS)} failed / {COUNT} checks")
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())
