"""over_limit 按「目的地槽位」记账（2026-10-21 真机事故）。"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.speech import delivery as D  # noqa: E402

FAILS: list[str] = []
COUNT = 0


def check(name, cond, detail=""):
    global COUNT
    COUNT += 1
    if cond:
        print(f"  ok {name}")
    else:
        FAILS.append(name)
        print(f"  FAIL {name} {detail}")


class _Caps:
    kinds = frozenset({"say"})
    targets = frozenset()
    enabled = True


class _Adapter:
    def __init__(self):
        self.calls = []

    def capabilities(self):
        return _Caps()

    def send(self, payload):
        self.calls.append(str(payload.get("text", "")))
        return {"ok": True, "ack": "platform_id", "platform_msg_id": "1"}


def _item(text, target="c"):
    return D.DeliverItem(kind="say", target=target, text=text)


def _g(item, ad, *, turn_id, max_items):
    return D._guard(item, turn_id=turn_id, caps=ad.capabilities(),
                    adapter=ad, dry_run=False, max_items=max_items)


def t_bubbles_not_limited():
    D._SLOTS_PER_TURN.clear()
    D._SENT.clear()
    ad = _Adapter()
    out = [_g(_item(f"b{i}"), ad, turn_id="t1", max_items=2) for i in range(5)]
    blocked = [r for r in out if r is not None]
    check("同一会话 5 个气泡都不被拦", not blocked, str([r.reason for r in blocked]))


def t_two_targets_limited():
    D._SLOTS_PER_TURN.clear()
    D._SENT.clear()
    ad = _Adapter()
    a = _g(_item("x", "c1"), ad, turn_id="t2", max_items=1)
    b = _g(_item("y", "c2"), ad, turn_id="t2", max_items=1)
    check("第 1 个目的地放行", a is None)
    check("第 2 个目的地按额度拦", b is not None and b.reason == "over_limit",
          str(getattr(b, "reason", "")))


def t_duplicate_still_first():
    D._SLOTS_PER_TURN.clear()
    D._SENT.clear()
    ad = _Adapter()
    sent = _item("same")
    D._SENT[("t3", sent.kind, sent.target, sent.content_hash())] = True
    again = _g(_item("same"), ad, turn_id="t3", max_items=2)
    check("重复仍是 duplicate 而非 over_limit",
          again is not None and again.reason == "duplicate", str(getattr(again, "reason", "")))


def main():
    for fn in (t_bubbles_not_limited, t_two_targets_limited, t_duplicate_still_first):
        print(fn.__name__)
        fn()
    print(f"RESULT {len(FAILS)} failed / {COUNT} checks")
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())
