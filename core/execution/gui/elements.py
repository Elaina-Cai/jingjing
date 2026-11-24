"""元素表：把「屏幕上有什么」变成一张可查的表。

为什么需要它
------------
「找不着按钮」有三种完全不同的原因（见 docs/decisions/设备与并发.md §5.2）：

A. **它在另一个顶层窗口里**——@ 名单是独立的 `Qt51514QWindowToolSaveBits`（327×180），
   右键菜单也是独立窗口。只截主窗口永远找不到它。
B. **它没被渲染**——长列表里未出现的行，屏幕和 UIA 都没有；只能靠库里的链路逼近。
C. **同名有多个**——两个「发送」、两个同名联系人。看得见不等于点得对。

这张表只解决 A 与 C 的「查」，不解决 B（B 靠账本锚点），也不解决坐标换算
（本表内 rect 一律**屏幕绝对坐标**，谁换错的谁负责——历史上错过两次）。

三条纪律
--------
1. `rect` 一律屏幕绝对坐标（UIA 的 `BoundingRectangle` 就是屏幕坐标，别再叠窗口原点）。
2. 同名多命中 → `unique()` 返回 None，调用方**不许猜**。
3. 代次（generation）不一致就整表作废：坐标会过期，过期坐标点下去是灾难。

数据来源与优先级
----------------
`uia` > `dom` > `ocr`：UIA 有角色与矩形且不受重绘影响；DOM 精确但只在浏览器内；
OCR 最脆（会把一行文字切到下半行）。合并时同名同角色取高优先级那一条的矩形。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence
from safelog import warn

log = logging.getLogger(__name__)

SOURCE_UIA = "uia"
SOURCE_DOM = "dom"
SOURCE_OCR = "ocr"
SOURCES = (SOURCE_UIA, SOURCE_DOM, SOURCE_OCR)
SOURCE_RANK = {SOURCE_UIA: 3, SOURCE_DOM: 2, SOURCE_OCR: 1}

MAX_DEPTH = 12
MAX_ELEMENTS = 500

Rect = tuple[int, int, int, int]  # left, top, right, bottom（屏幕绝对坐标）


def _rect(value: Any) -> Rect:
    try:
        left, top, right, bottom = (int(v) for v in value)
    except Exception:
        warn("core/execution/gui/elements.py:50 Exception", err=repr(__import__("sys").exc_info()[1]))
        return (0, 0, 0, 0)
    return (left, top, right, bottom)


def _area(rect: Rect) -> int:
    return max(0, rect[2] - rect[0]) * max(0, rect[3] - rect[1])


@dataclass(frozen=True)
class Element:
    """屏幕上一个可点的东西。``window`` 是它所属的顶层窗口句柄。"""

    name: str
    role: str = ""
    rect: Rect = (0, 0, 0, 0)
    source: str = SOURCE_UIA
    window: int = 0
    confidence: float = 1.0

    @property
    def center(self) -> tuple[int, int]:
        left, top, right, bottom = self.rect
        return ((left + right) // 2, (top + bottom) // 2)

    @property
    def usable(self) -> bool:
        return bool(self.name.strip()) and _area(self.rect) > 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "role": self.role,
            "rect": list(self.rect),
            "source": self.source,
            "window": self.window,
            "confidence": self.confidence,
        }


@dataclass(frozen=True)
class WindowRef:
    hwnd: int
    title: str = ""
    class_name: str = ""
    rect: Rect = (0, 0, 0, 0)
    active: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "hwnd": self.hwnd,
            "title": self.title,
            "class": self.class_name,
            "rect": list(self.rect),
            "active": self.active,
        }


DEDUPE_TOLERANCE = 8  # 像素：同一颗按钮被两个来源读到，矩形最多差这么多


def _dedupe_key(element: Element, tol: int = DEDUPE_TOLERANCE) -> tuple[int, str, str, int, int]:
    """同一个「东西」才合并：窗口 + 名字 + 角色 + 中心点（按 tol 取格）。

    关键：**同来源的两条不同位置的同名元素必须保留两条**——两个「发送」按钮、
    两条内容相同的消息，都是合法的两个东西。只有跨来源、且落在同一格里的才合并。
    """
    cx, cy = element.center
    return (
        element.window,
        element.name.strip(),
        element.role.strip(),
        cx // max(1, tol),
        cy // max(1, tol),
    )


def merge_elements(*groups: Iterable[Element]) -> list[Element]:
    """同一 (window, name, role) 只留一条，取来源优先级最高者。"""
    best: dict[tuple[int, str, str, int, int], Element] = {}
    order: list[tuple[int, str, str, int, int]] = []
    for group in groups:
        for element in group:
            if not element.name.strip():
                continue
            key = _dedupe_key(element)
            current = best.get(key)
            if current is None:
                best[key] = element
                order.append(key)
                continue
            if SOURCE_RANK.get(element.source, 0) > SOURCE_RANK.get(current.source, 0):
                best[key] = element
    return [best[key] for key in order]


@dataclass
class ScanResult:
    """一次扫描的产物。``generation`` 变了就整表作废。"""

    elements: list[Element] = field(default_factory=list)
    windows: list[WindowRef] = field(default_factory=list)
    generation: int = 0
    dropped: int = 0
    truncated: int = 0
    source_stats: dict[str, int] = field(default_factory=dict)

    def table(self) -> "ElementTable":
        return ElementTable(self.elements, generation=self.generation)

    def window_of(self, hwnd: int) -> WindowRef | None:
        for window in self.windows:
            if window.hwnd == hwnd:
                return window
        return None

    def summary(self) -> dict[str, Any]:
        return {
            "generation": self.generation,
            "elements": len(self.elements),
            "windows": len(self.windows),
            "dropped": self.dropped,
            "truncated": self.truncated,
            "sources": dict(self.source_stats),
        }


class ElementTable:
    """按名字/角色/窗口查元素。多命中一律交给调用方决定，本表不猜。"""

    def __init__(self, elements: Sequence[Element] = (), *, generation: int = 0) -> None:
        self.generation = int(generation)
        self.elements: list[Element] = [e for e in elements if e.name.strip()]

    def __len__(self) -> int:
        return len(self.elements)

    def __iter__(self):
        return iter(self.elements)

    def is_current(self, generation: int) -> bool:
        return int(generation) == self.generation

    def by_window(self, hwnd: int) -> list[Element]:
        return [e for e in self.elements if e.window == int(hwnd)]

    def find(
        self,
        name: str,
        *,
        role: str = "",
        window: int | None = None,
        exact: bool = True,
    ) -> list[Element]:
        needle = (name or "").strip()
        if not needle:
            return []
        low = needle.lower()
        hits: list[Element] = []
        for element in self.elements:
            if window is not None and element.window != int(window):
                continue
            if role and element.role.strip().lower() != role.strip().lower():
                continue
            label = element.name.strip()
            if exact:
                if label != needle:
                    continue
            elif low not in label.lower():
                continue
            hits.append(element)
        hits.sort(key=lambda e: (-SOURCE_RANK.get(e.source, 0), -_area(e.rect)))
        return hits

    def unique(
        self,
        name: str,
        *,
        role: str = "",
        window: int | None = None,
        exact: bool = True,
    ) -> Element | None:
        """唯一命中才返回；0 个或多于 1 个都返回 None（不许猜）。"""
        hits = self.find(name, role=role, window=window, exact=exact)
        if len(hits) != 1:
            return None
        return hits[0]

    def merge(self, other: "ElementTable") -> "ElementTable":
        if other.generation != self.generation:
            raise ValueError(
                f"代次不一致不能合并：{self.generation} vs {other.generation}"
            )
        return ElementTable(
            merge_elements(self.elements, other.elements), generation=self.generation
        )


def _window_refs(backend: Any, *, only: Sequence[int] | None = None) -> list[WindowRef]:
    try:
        raw = backend.windows()
    except Exception as exc:  # noqa: BLE001 - 读不到窗口不该炸
        log.warning("elements_windows_failed error=%r", exc)
        return []
    refs: list[WindowRef] = []
    wanted = {int(h) for h in only} if only else None
    for item in raw or []:
        try:
            hwnd = int(item.get("hwnd") or 0)
        except Exception:
            warn("core/execution/gui/elements.py:259 Exception", err=repr(__import__("sys").exc_info()[1]))
            continue
        if not hwnd or (wanted is not None and hwnd not in wanted):
            continue
        refs.append(
            WindowRef(
                hwnd=hwnd,
                title=str(item.get("title") or ""),
                class_name=str(item.get("class") or ""),
                rect=_rect(item.get("rect") or (0, 0, 0, 0)),
                active=bool(item.get("active")),
            )
        )
    return refs


def scan_once(
    *,
    backend: Any = None,
    hwnds: Sequence[int] | None = None,
    max_depth: int = MAX_DEPTH,
    max_elements: int = MAX_ELEMENTS,
    generation: int = 0,
    source: str = SOURCE_UIA,
) -> ScanResult:
    """扫一遍窗口与元素。``backend`` 可注入（测试不碰真桌面）。"""
    engine = backend if backend is not None else default_backend()
    windows = _window_refs(engine, only=hwnds)
    raw: list[Element] = []
    dropped = 0
    for window in windows:
        try:
            nodes = engine.tree(window.hwnd, max_depth=max_depth) or []
        except Exception as exc:  # noqa: BLE001 - 单个窗口读失败不影响其它
            log.warning("elements_tree_failed hwnd=%s error=%r", window.hwnd, exc)
            continue
        for node in nodes:
            element = Element(
                name=str(node.get("name") or ""),
                role=str(node.get("role") or ""),
                rect=_rect(node.get("rect") or (0, 0, 0, 0)),
                source=source,
                window=window.hwnd,
                confidence=float(node.get("confidence") or 1.0),
            )
            if not element.name.strip():
                dropped += 1
                continue
            raw.append(element)

    merged = merge_elements(raw)
    merged.sort(key=lambda e: (-SOURCE_RANK.get(e.source, 0), -_area(e.rect)))
    truncated = 0
    if len(merged) > max_elements:
        truncated = len(merged) - max_elements
        merged = merged[:max_elements]
    stats: dict[str, int] = {}
    for element in merged:
        stats[element.source] = stats.get(element.source, 0) + 1
    return ScanResult(
        elements=merged,
        windows=windows,
        generation=int(generation),
        dropped=dropped,
        truncated=truncated,
        source_stats=stats,
    )


def default_backend() -> Any:
    """默认 UIA 后端（延迟导入：没装 comtypes 也不影响本模块被 import）。"""
    from core.execution.gui.uia_win import AutomationBackend

    return AutomationBackend()
