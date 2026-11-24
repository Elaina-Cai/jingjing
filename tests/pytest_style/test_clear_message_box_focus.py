"""清输入框必须先聚焦（core/execution/wx_chat.py::clear_message_box）。

2026-11-04 真机：切过去不等于焦点在输入框，只发 Ctrl+A/Delete 会打在搜索框或
会话列表上，框里的残句原样留着 —— desk.log 里 `desk_residue_cleared ok=false`
连了三次，一条消息重试到第 10 次。测试只打桩，不碰真实键鼠。
"""
from __future__ import annotations

import pytest

from core.execution import wx_chat


def _patch(monkeypatch, order, texts):
    monkeypatch.setattr(wx_chat, "focus_message_box", lambda *a, **k: order.append("focus") or True)
    monkeypatch.setattr(wx_chat, "_clear_focused_field", lambda *a, **k: order.append("clear"))
    it = iter(texts)
    monkeypatch.setattr(wx_chat, "message_box_text", lambda *a, **k: next(it))


def test_focus_happens_before_the_clear_keys(monkeypatch):
    order, seen = [], []
    _patch(monkeypatch, order, ["", ""])
    assert wx_chat.clear_message_box() is True
    assert order == ["focus", "clear"]


def test_it_reads_the_box_back_before_saying_ok(monkeypatch):
    order, seen = [], []
    _patch(monkeypatch, order, ["上一轮没发出去的半句", ""])
    assert wx_chat.clear_message_box() is True
    assert order == ["focus", "clear", "focus", "clear"]


def test_it_gives_up_after_two_rounds(monkeypatch):
    order, seen = [], []
    _patch(monkeypatch, order, ["还在", "还在", "还在"])
    assert wx_chat.clear_message_box() is False
    assert order == ["focus", "clear", "focus", "clear"]


def test_a_driver_error_is_not_reported_as_cleared(monkeypatch):
    order, seen = [], []
    _patch(monkeypatch, order, [""])

    def boom(*a, **k):
        raise RuntimeError("focus 失败")

    monkeypatch.setattr(wx_chat, "focus_message_box", boom)
    assert wx_chat.clear_message_box() is False
    assert order == []
