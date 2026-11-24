"""没有前台窗口时不能抛 TypeError。

2026-11-20 实测：锁屏期间 `GetForegroundWindow()` 返回 0（ctypes 里是 None），
`foreground_hwnd()` 直接 `int(None)` 抛 TypeError，整次发送被判失败——那天 18 次，
全部到同一个会话、每小时一条。
"""
from __future__ import annotations

from types import SimpleNamespace

import ctypes

from core.execution import wx_vision


def test_foreground_hwnd_returns_zero_when_no_window(monkeypatch):
    monkeypatch.setattr(ctypes, "windll",
                        SimpleNamespace(user32=SimpleNamespace(GetForegroundWindow=lambda: None)),
                        raising=False)
    assert wx_vision.foreground_hwnd() == 0


def test_foreground_hwnd_returns_the_handle(monkeypatch):
    monkeypatch.setattr(ctypes, "windll",
                        SimpleNamespace(user32=SimpleNamespace(GetForegroundWindow=lambda: 4242)),
                        raising=False)
    assert wx_vision.foreground_hwnd() == 4242
