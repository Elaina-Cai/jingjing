"""UIA 的真后端（comtypes + uiautomation）。只在 ``GUI_CHANNEL=on`` 时被 ``uia.py`` 惰性 import。

为什么要单独一个文件：`uia.py` 是**可测的纯逻辑**（选窗口、渲染、预算），
它不该为了跑测试去摸真窗口；真窗口那些 `try/except` 都堆在这里。

`uiautomation` 的控件对象给的是 `BoundingRectangle`（left/top/right/bottom），
和 win32 的 `GetWindowRect` 有 12px 级别的差（DWM 边框），**两套都是物理像素**，
不要拿它去做缩放换算（踩过：以为差 1.5 倍，其实是边框）。
"""

from __future__ import annotations

from typing import Any

import structlog
from safelog import warn

log = structlog.get_logger(__name__)


def _rect_tuple(rect: Any) -> tuple[int, int, int, int]:
    """`BoundingRectangle` → ``(x, y, w, h)``；形状不认识就返回全零。"""
    try:
        left = int(rect.left)
        top = int(rect.top)
        right = int(rect.right)
        bottom = int(rect.bottom)
    except Exception:  # noqa: BLE001 - 有的控件没有矩形
        warn("core/execution/gui/uia_win.py:27 Exception", err=repr(__import__("sys").exc_info()[1]))
        return (0, 0, 0, 0)
    return (left, top, max(0, right - left), max(0, bottom - top))


class AutomationBackend:
    """`uia.py::UiaBackend` 的真实现。"""

    def __init__(self) -> None:
        import uiautomation as auto  # 延迟到构造，import 失败由调用方兜住

        self._auto = auto

    # --- 只读 ---------------------------------------------------------

    def windows(self) -> list[dict[str, Any]]:
        auto = self._auto
        active = 0
        try:
            fg = auto.GetForegroundControl()
            active = int(getattr(fg, "NativeWindowHandle", 0) or 0)
        except Exception:  # noqa: BLE001 - 前台读不到不影响列窗口
            warn("core/execution/gui/uia_win.py:48 Exception", err=repr(__import__("sys").exc_info()[1]))
            active = 0
        out: list[dict[str, Any]] = []
        try:
            children = auto.GetRootControl().GetChildren()
        except Exception as exc:  # noqa: BLE001
            log.warning("uia_root_failed error=%r", exc)
            return out
        for child in children:
            try:
                hwnd = int(getattr(child, "NativeWindowHandle", 0) or 0)
                if not hwnd:
                    continue
                out.append(
                    {
                        "title": str(getattr(child, "Name", "") or ""),
                        "class": str(getattr(child, "ClassName", "") or ""),
                        "hwnd": hwnd,
                        "rect": _rect_tuple(getattr(child, "BoundingRectangle", None)),
                        "active": hwnd == active,
                    }
                )
            except Exception:  # noqa: BLE001 - 单个窗口坏了不影响别的
                warn("core/execution/gui/uia_win.py:70 Exception", err=repr(__import__("sys").exc_info()[1]))
                continue
        return out

    def tree(self, hwnd: int, *, max_depth: int = 12) -> list[dict[str, Any]]:
        auto = self._auto
        nodes: list[dict[str, Any]] = []
        try:
            root = auto.ControlFromHandle(hwnd)
        except Exception as exc:  # noqa: BLE001
            log.warning("uia_handle_failed hwnd=%s error=%r", hwnd, exc)
            return nodes
        if root is None:
            return nodes

        def walk(control: Any, depth: int) -> None:
            if depth > max_depth:
                return
            try:
                nodes.append(
                    {
                        "role": str(getattr(control, "ControlTypeName", "") or ""),
                        "name": str(getattr(control, "Name", "") or ""),
                        "rect": _rect_tuple(getattr(control, "BoundingRectangle", None)),
                        "depth": depth,
                    }
                )
                children = control.GetChildren()
            except Exception:  # noqa: BLE001
                warn("core/execution/gui/uia_win.py:98 Exception", err=repr(__import__("sys").exc_info()[1]))
                return
            for child in children or []:
                walk(child, depth + 1)

        walk(root, 0)
        return nodes

    # --- 唯一一个「动窗口」的入口（不属于读屏，单独列出来）----------------

    def show(self, title: str = "") -> bool:
        """把窗口唤到前台并恢复（微信最小化到托盘时 win32 那套全是 err=5）。

        顺序：UIA 的 `Show()` → `SetFocus()` → win32 `ShowWindow(SW_RESTORE)`。
        返回是否「看起来成功」（有任意一步不抛异常即算过，真伪由调用方再验一次）。
        """
        auto = self._auto
        needle = str(title or "").strip().lower()
        candidates: list[Any] = []
        try:
            for child in auto.GetRootControl().GetChildren():
                name = str(getattr(child, "Name", "") or "")
                if not needle or needle in name.lower():
                    candidates.append(child)
        except Exception as exc:  # noqa: BLE001
            log.warning("uia_show_enumerate_failed error=%r", exc)
            return False
        if not candidates:
            return False
        win = candidates[0]
        ok = False
        for step_name, action in (
            ("show", lambda: win.Show(1)),
            ("focus", lambda: win.SetFocus()),
        ):
            try:
                action()
                ok = True
            except Exception as exc:  # noqa: BLE001
                log.warning("uia_show_step_failed step=%s error=%r", step_name, exc)
        try:
            import win32con
            import win32gui

            hwnd = int(getattr(win, "NativeWindowHandle", 0) or 0)
            if hwnd:
                win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
                win32gui.SetForegroundWindow(hwnd)
                ok = True
        except Exception as exc:  # noqa: BLE001
            log.warning("uia_show_win32_failed error=%r", exc)
        return ok
