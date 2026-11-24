"""前台窗口（只读，纯 ctypes；读不到就返回空 —— fail closed）。"""

from __future__ import annotations

import ctypes
from ctypes import wintypes

from core.senses.base import WindowInfo
from safelog import warn

_user32 = ctypes.windll.user32


def _title_of(hwnd: int) -> str:
    length = int(_user32.GetWindowTextLengthW(hwnd) or 0)
    if length <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(length + 1)
    _user32.GetWindowTextW(hwnd, buf, length + 1)
    return str(buf.value or "")


def _exe_of(hwnd: int) -> str:
    pid = wintypes.DWORD(0)
    _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if not pid.value:
        return ""
    try:
        import psutil

        proc = psutil.Process(int(pid.value))
        return str(proc.name() or "")
    except Exception:  # noqa: BLE001 - 进程名读不到不影响"看见了什么"
        warn("core/senses/window.py:32 Exception", err=repr(__import__("sys").exc_info()[1]))
        return ""


def _rect_of(hwnd: int) -> tuple[int, int, int, int]:
    rect = wintypes.RECT()
    if not _user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return (0, 0, 0, 0)
    return (int(rect.left), int(rect.top), int(rect.right), int(rect.bottom))


def foreground_window() -> WindowInfo:
    """当前前台窗口。任何异常都返回空 :class:`WindowInfo`。"""
    try:
        hwnd = int(_user32.GetForegroundWindow() or 0)
        if not hwnd:
            return WindowInfo()
        title = _title_of(hwnd)
        exe = _exe_of(hwnd)
        if not title and not exe:
            return WindowInfo()
        return WindowInfo(title=title, exe=exe, rect=_rect_of(hwnd), focused=True)
    except Exception:  # noqa: BLE001 - 读窗口失败不算错误，只是"没看见"
        warn("core/senses/window.py:54 Exception", err=repr(__import__("sys").exc_info()[1]))
        return WindowInfo()
