"""一拍跑到底的冒烟测试：只要 `one_tick` 抛异常就红。

2026-10-30 事故：455 条测试全绿，但 `one_tick` 每拍都在收尾崩（`_PEND_TRIES` 未定义），
因为没有任何一条测试真的从 `one_tick` 走到 return。这条只钉一件事：
**有积压时，一拍能完整跑完并返回。**
"""
from __future__ import annotations

import asyncio

from loop import brain as B
from loop import run as R
from loop import snapshot as S


def _snap() -> dict:
    cells = [
        {
            "chat_id": "chat-%d" % i,
            "label": "L%d" % i,
            "tier": "ask",
            "last_at": "2026-10-30 19:5%d:00" % i,
            "after_mine": ["刚刚那句"],
            "platform": "wechat",
            "chat_type": "private",
            "relation": 1,
        }
        for i in range(4)
    ]
    return {"at": "2026-10-30 20:00:00", "cells": cells}


def test_one_tick_survives_a_pending_backlog(monkeypatch) -> None:
    seen: list[str] = []

    async def fake_run_loop(*a, **k):
        seen.append(str(k.get("focus") or ""))
        return {"said": [], "blocked": [], "silent": True}

    monkeypatch.setattr(R.ports, "pick_db", lambda: "db")
    monkeypatch.setattr(S, "build_snapshot", lambda db: _snap())
    monkeypatch.setattr(S, "render_text", lambda snap, only=None, **kw: "世界")
    monkeypatch.setattr(B, "run_loop", fake_run_loop)

    out = asyncio.run(R.one_tick(dry_run=True, llm=object()))

    assert isinstance(out, dict) and out, "one_tick 没有返回值"
    assert "pending" in out, "返回里没有 pending：%s" % sorted(out)
    assert seen, "没有任何窗口被交给 run_loop"


def test_pend_tries_exists_and_is_a_counter() -> None:
    assert hasattr(R, "_PEND_TRIES"), "loop/run.py 没有 _PEND_TRIES（每拍 NameError 的元凶）"
    assert isinstance(R._PEND_TRIES, list) and len(R._PEND_TRIES) == 1
