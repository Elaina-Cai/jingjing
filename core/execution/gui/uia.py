"""UIA 读屏：把窗口的**控件树**读成一段可寻址的文字（不是截图）。

为什么要有它（与 `cdp.py`、`wechat.py` 的分工）：

- `cdp.py`：Chromium 内核（浏览器 / Electron），读的是页面 DOM。
- `wechat.py`：微信走**数据库读、界面写**，读侧根本不碰界面。
- 本模块：**原生窗口**（Qt / Win32 / WinForms）的控件树——OCR 只能看到字，
  控件树还能看到「这是什么角色、能不能点、点哪里」，而且文字框是精确的。

三条纪律：

1. `GUI_CHANNEL=off` 时**连 UIA 都不初始化**（不摸任何窗口），抛 ``blocked``。
2. **只读**：这里没有 `Invoke` / `SetValue` / `Click`，一个写动作都不许出现在本文件。
3. 有界：最多 ``MAX_ELEMENTS`` 个节点、``MAX_CHARS`` 字，超了就截断并标 ``truncated``
   （借 Windows-MCP 的做法：给模型一份「有预算的树」，而不是无限深的全量 dump）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

import structlog

from core import features
from core.execution.gui.base import GuiError, GuiSnapshot, normalize_text
from safelog import warn

log = structlog.get_logger(__name__)

#: 一次最多读多少个控件（够看清一屏，又不会把 prompt 撑爆）。
MAX_ELEMENTS: int = 500

#: 正文上限（与工具包 §7 的「工具返回进 prompt 合计 ≤600 字」不冲突：这里单独限）。
MAX_CHARS: int = 4000

#: 树的深度上限（Qt 应用的装饰层能有十几层空 Pane）。
MAX_DEPTH: int = 12

#: 这些角色「能动手」，在行尾标出来——读的时候就知道哪里可点。
ACTIONABLE_ROLES: tuple[str, ...] = (
    "ButtonControl",
    "ListItemControl",
    "MenuItemControl",
    "TabItemControl",
    "HyperlinkControl",
    "CheckBoxControl",
    "RadioButtonControl",
    "EditControl",
    "ComboBoxControl",
)


class UiaBackend(Protocol):
    """真正摸系统的部分（测试里换成假的，不碰真窗口）。"""

    def windows(self) -> list[dict[str, Any]]:
        """顶层窗口：``{"title", "class", "hwnd", "rect": (x, y, w, h), "active": bool}``。"""

    def tree(self, hwnd: int, *, max_depth: int = MAX_DEPTH) -> list[dict[str, Any]]:
        """控件树（先序）：``{"role", "name", "rect": (x, y, w, h), "depth": int}``。"""


def _mode() -> str:
    """唯一读点：`GUI_CHANNEL` 的三档。"""
    return features.mode(features.GUI_CHANNEL, features.MODE_OFF)


def pick_window(
    windows: list[dict[str, Any]],
    target: str = "",
) -> dict[str, Any] | None:
    """按 ``target`` 选窗口：空 = 前台窗口；否则标题/类名包含它（唯一命中才算）。

    为什么要「唯一」：两个窗口标题都含「微信」时，猜错就会读到别的进程；
    宁可不读（``no_target``），也不给模型一份不属于它的屏。
    """
    if not windows:
        return None
    needle = str(target or "").strip().lower()
    if not needle:
        for win in windows:
            if win.get("active"):
                return win
        return windows[0]
    hits = [
        win
        for win in windows
        if needle in str(win.get("title") or "").lower()
        or needle in str(win.get("class") or "").lower()
    ]
    if len(hits) != 1:
        return None
    return hits[0]


def _rect_text(rect: Any) -> str:
    try:
        x, y, w, h = (int(v) for v in tuple(rect)[:4])
    except Exception:  # noqa: BLE001 - 矩形形状不标准就不写坐标
        warn("core/execution/gui/uia.py:99 Exception", err=repr(__import__("sys").exc_info()[1]))
        return ""
    return f"({x},{y},{w},{h})"


def render_tree(
    nodes: list[dict[str, Any]],
    *,
    max_elements: int = MAX_ELEMENTS,
    max_chars: int = MAX_CHARS,
) -> tuple[str, bool]:
    """把控件树渲染成文字行；返回 ``(text, truncated)``。

    每行：``[深度] 角色 "名字" (x,y,w,h) *可点``。空名字的容器也留着（它们提供结构）。
    """
    lines: list[str] = []
    used = 0
    truncated = False
    for node in nodes:
        if used >= max_elements:
            truncated = True
            break
        depth = max(0, int(node.get("depth") or 0))
        role = str(node.get("role") or "").strip()
        name = str(node.get("name") or "").strip()
        rect = _rect_text(node.get("rect"))
        line = f"{'  ' * depth}[{depth}] {role}"
        if name:
            line += f' "{name}"'
        if rect:
            line += f" {rect}"
        if role in ACTIONABLE_ROLES:
            line += " *可动"
        used += 1
        if used and sum(len(x) + 1 for x in lines) + len(line) > max_chars:
            truncated = True
            break
        lines.append(line)
    return ("\n".join(lines), truncated)


class UiaReader:
    """读一屏控件树。``backend`` 不给就用真的 `uiautomation`（延迟 import）。"""

    def __init__(
        self,
        *,
        backend: UiaBackend | None = None,
        max_elements: int = MAX_ELEMENTS,
        max_chars: int = MAX_CHARS,
        taken_at: str = "",
    ) -> None:
        self._backend = backend
        self._max_elements = max(1, int(max_elements))
        self._max_chars = max(80, int(max_chars))
        self._taken_at = taken_at

    def _get_backend(self) -> UiaBackend:
        if self._backend is not None:
            return self._backend
        try:
            from core.execution.gui.uia_win import AutomationBackend
        except Exception as exc:  # noqa: BLE001 - 没装 UIA 依赖就是「读不了」
            raise GuiError("unsupported", f"装不上 UIA 后端：{exc}") from exc
        self._backend = AutomationBackend()
        return self._backend

    def snapshot(self, target: str = "", *, max_chars: int | None = None) -> GuiSnapshot:
        mode = _mode()
        if mode != features.MODE_ON:
            # off / shadow 都不摸窗口：读屏是**会打扰前台**的动作，不许偷偷做。
            raise GuiError("blocked", f"GUI_CHANNEL={mode}：控件树读屏没开")
        backend = self._get_backend()
        windows = list(backend.windows() or [])
        win = pick_window(windows, target)
        if win is None:
            raise GuiError("no_target", f"没有唯一匹配的窗口（target={target!r}）")
        hwnd = int(win.get("hwnd") or 0)
        nodes = list(backend.tree(hwnd, max_depth=MAX_DEPTH) or [])
        text, truncated_chars = render_tree(
            nodes,
            max_elements=self._max_elements,
            max_chars=self._max_chars,
        )
        title = str(win.get("title") or "")
        head = f"窗口: {title} [{win.get('class') or ''}] {_rect_text(win.get('rect'))}".strip()
        text, cut = normalize_text(f"{head}\n{text}" if text else head, max_chars=max_chars or self._max_chars)
        return GuiSnapshot(
            target=title,
            url="",
            title=title,
            text=text,
            taken_at=self._taken_at,
            truncated=bool(cut or truncated_chars),
        )
