"""台账公共外键（ledger_key）契约。

锁四件事：键只从 extra 里取、取不到留空不猜；写行包含 kind+event_id+chat_id+at；
写失败返回 False 且**不抛**；trace_id 只在有时才出现（避免空串污染配对）。
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from _tempdirs import temp_base  # noqa: E402
from core.routing.ledger_key import LedgerKey, emit, key_of  # noqa: E402

FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(("ok " if ok else "FAIL ") + name + ("" if ok else f" {detail}"))
    if not ok:
        FAILS.append(name)


def t_key_from_extra() -> None:
    k = key_of(SimpleNamespace(extra={"parent_event_id": "e1", "chat_id": "c1"}), trace_id="t1")
    check("event_id/chat_id 从 extra 取", (k.event_id, k.chat_id) == ("e1", "c1"))
    check("trace_id 透传", k.trace_id == "t1")
    fallback = key_of({"event_id": "e2"})
    check("parent_event_id 缺失时退回 event_id", fallback.event_id == "e2")
    empty = key_of(None)
    check("拿不到就空串（不猜）", empty.event_id == "" and empty.chat_id == "")
    check("空 trace_id 不进 dict", "trace_id" not in empty.as_dict())


def t_emit_writes_line() -> None:
    path = temp_base() / "ledger_key_test.jsonl"
    if path.exists():
        path.unlink()
    k = LedgerKey(event_id="e9", chat_id="c9", at="2026-10-23T00:00:00", trace_id="t9")
    ok = emit("route", k, path=path, route="speak", reason="x")
    check("写成功返回 True", ok is True)
    line = path.read_text(encoding="utf-8").strip()
    check("行里有 kind 与公共键", '"kind": "route"' in line and '"event_id": "e9"' in line)
    check("payload 也写进去", '"route": "speak"' in line)


def t_emit_never_raises() -> None:
    bad = temp_base() / "no_such_dir" / "\x00bad.jsonl"
    check("写失败返回 False 不抛", emit("x", LedgerKey(), path=bad) is False)


def main() -> int:
    t_key_from_extra()
    t_emit_writes_line()
    t_emit_never_raises()
    print(f"\nRESULT {len(FAILS)} failed")
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())