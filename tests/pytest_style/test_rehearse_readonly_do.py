# -*- coding: utf-8 -*-
"""试衣间的只读通道：只读工具真跑，写与外发仍然不做。

2026-11-18：以前 `do` 在试衣间里一律被挡，于是「她想去查」能测、「查到了没」测不到，
C 那条永远停在选择意图。现在只放行 `read_only_in_plan()` 里的工具。

这两条一起看才有意义：放行了什么，和**没有**放行什么。
"""
from __future__ import annotations

import asyncio

import pytest

from loop import brain, ports


def test_rehearse_runs_read_only_tool_and_returns_content(monkeypatch):
    seen: list[tuple[str, dict]] = []

    async def fake_act(name, args):
        seen.append((name, dict(args)))
        return ports.Result(True, text="# docs 索引\n- 部署说明：docs/deploy.md\n")

    monkeypatch.setattr(ports, "act", fake_act)
    obs, receipt = asyncio.run(brain._rehearse_step(
        "do", {"name": "read_file", "args": {"path": "docs/README.md"}}, turn_id="t-1"))

    assert seen == [("read_file", {"path": "docs/README.md"})], "只读工具应当真的被执行"
    assert "docs/deploy.md" in obs, "读到的内容要进 observation，否则等于没读"
    assert receipt["result"] == "ok" and receipt["act"] == "do"


def test_rehearse_still_refuses_writes_and_sends(monkeypatch):
    def forbidden(*_a, **_k):
        raise AssertionError("写/外发不许在试衣间执行")

    async def forbidden_async(*_a, **_k):
        raise AssertionError("写/外发不许在试衣间执行")

    monkeypatch.setattr(ports, "act", forbidden_async)
    monkeypatch.setattr(ports, "show", forbidden)
    for tool in ("create_reminder", "send_message", "notify_user"):
        obs, receipt = asyncio.run(brain._rehearse_step(
            "do", {"name": tool, "args": {}}, turn_id="t-2"))
        assert receipt["result"] == "blocked", tool
        assert "试衣间" in obs and "没有执行" in obs, (tool, obs)


def test_rehearse_read_only_failure_is_reported_not_swallowed(monkeypatch):
    async def failing_act(name, args):
        return ports.Result(False, error="没有这个文件：docs/nope.md")

    monkeypatch.setattr(ports, "act", failing_act)
    obs, receipt = asyncio.run(brain._rehearse_step(
        "do", {"name": "read_file", "args": {"path": "docs/nope.md"}}, turn_id="t-3"))
    assert receipt["result"] == "error"
    assert "没有这个文件" in obs, "读失败要说出来，不能装作读到了"
