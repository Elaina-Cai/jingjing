"""私聊也要有上下文（2026-10-30 用户定「甲」）。

背景：``loop/snapshot.py`` 的 ``prev``（「↳ 再往前」）原来只在
``if d["chat_type"] == "group"`` 里构建 —— 私聊只给「他上一条」。
甲方案：私聊也给 ``PREV_IN_DM`` 条，并且因为全局拉取是**共享的 800 行配额**，
安静会话必须先按 ``chat_id`` 补拉一次，否则「再往前」永远空。

这里锁三件：补拉把 chat_id 真的传下去、合并去重、合并保序。
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from loop import ports, snapshot  # noqa: E402


def _row(at: str, mid: int, chat: str = "dm1") -> SimpleNamespace:
    return SimpleNamespace(at=at, msg_id=mid, chat_id=chat, text="x", is_self=False, speaker="他")


def test_context_rows_passes_chat_id(monkeypatch) -> None:
    seen: dict = {}

    def fake(db, *, since_hours=168, limit=400, chat_id=""):
        seen.update(db=db, since_hours=since_hours, limit=limit, chat_id=chat_id)
        return [_row("2026-10-30 10:00:00", 2)]

    monkeypatch.setattr(ports, "sense_sync", fake)
    got = snapshot._context_rows("data/world.db", "dm1", 8)
    assert len(got) == 1
    assert seen["chat_id"] == "dm1"
    assert seen["limit"] == 8


def test_context_rows_without_chat_id_is_empty(monkeypatch) -> None:
    called: list = []
    monkeypatch.setattr(ports, "sense_sync", lambda *a, **k: called.append(1) or [])
    assert snapshot._context_rows("data/world.db", "", 8) == []
    assert called == []


def test_merge_rows_dedups_and_keeps_order() -> None:
    a = [_row("10:00:00", 1), _row("10:01:00", 2)]
    b = [_row("10:01:00", 2), _row("10:02:00", 3)]
    got = snapshot._merge_rows(a, b)
    assert [(r.at, r.msg_id) for r in got] == [("10:00:00", 1), ("10:01:00", 2), ("10:02:00", 3)]


def test_dm_budget_is_smaller_than_group() -> None:
    assert 0 < snapshot.PREV_IN_DM < snapshot.PREV_IN_GROUP
