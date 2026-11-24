# -*- coding: utf-8 -*-
"""GUI 驱动层的共用形状：快照、错误码、只读协议。

这里只放**数据形状**和一个纯函数（`normalize_text`），不放任何 I/O —— 这样
backend（CDP / UIA / ADB / Vision）只负责「怎么读」，裁剪与归一只有一份实现。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol
from safelog import warn

#: 错误码（封闭集合，出口的第六态与工具包都按这个分类）。
GUI_ERROR_CODES: tuple[str, ...] = (
    "not_connected",   # 连不上驱动（浏览器没开 / 端口不对 / App 没起）
    "no_target",       # 连上了但没有这个目标（关掉的标签页、没打开的窗口）
    "timeout",         # 连上了、命令没回来
    "unsupported",     # 这个 backend 不支持这个动作（例如只读驱动被要求点击）
    "blocked",         # 开关没开 / 不在白名单（默认拒绝）
)

#: 「截图可验」门槛：目标区域至少这么多个像素变化（工具包 §4.1 第 3 条）。
#: 与 `core/tools/registry.py::UI_MIN_PIXELS_CHANGED` 同值——两处不许漂。
MIN_CHANGED_PIXELS: int = 100

#: 选择器的上限（一个选择器长过一句人话，就不是「点那一个」了）。
SELECTOR_MAX_CHARS: int = 200

#: 一次最多输入多少字（单行输入框放不下更长的东西；长文走文件）。
TEXT_MAX_CHARS: int = 200

#: 文本上限（进 prompt 的预算是 4000 字，这里只是兜底）。
SNAPSHOT_MAX_CHARS: int = 4000

#: 允许**跳转**的协议：只有 http(s)。`javascript:` / `file:` / `data:` / `chrome:` 一律拒——
#: 前两者等于在页面里执行代码或读本地文件，后两者是驱动自己的通道，都不是「去一个网页」。
NAV_SCHEMES: tuple[str, ...] = ("http://", "https://")

_BLANK_RUN = re.compile(r"\n{3,}")
_TRAILING_WS = re.compile(r"[ \t]+$", re.MULTILINE)


class GuiError(RuntimeError):
    """驱动层的失败：带**封闭**错误码，不带平台异常文本（那是 detail）。"""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code if code in GUI_ERROR_CODES else "unsupported"
        self.detail = str(detail or "")[:300]
        super().__init__(f"{self.code}: {self.detail}" if self.detail else self.code)


@dataclass(frozen=True)
class GuiSnapshot:
    """一次「看一眼」的结果：**只读、无副作用、可 diff**。"""

    target: str = ""
    url: str = ""
    title: str = ""
    text: str = ""
    taken_at: str = ""
    truncated: bool = False

    def digest(self) -> str:
        """状态摘要（写回执的 `state_before` / `state_after` 就用它）。"""
        raw = "\x1f".join((self.url, self.title, self.text))
        return hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()[:16]

    def is_empty(self) -> bool:
        return not (self.text or "").strip()

    def as_dict(self) -> dict[str, object]:
        return {
            "target": self.target,
            "url": self.url,
            "title": self.title,
            "text": self.text,
            "taken_at": self.taken_at,
            "truncated": self.truncated,
            "digest": self.digest(),
        }


def check_nav_url(url: object) -> str:
    """跳转前的唯一校验：只许 http(s)、不许空、不许控制字符。纯函数，便于穷举。"""
    raw = " ".join(str(url or "").split())
    if not raw:
        raise GuiError("unsupported", "跳转目标为空")
    if any(ch in raw for ch in ("\x00", "\n", "\r", "\t")):
        raise GuiError("blocked", "跳转目标里含控制字符")
    if not raw.lower().startswith(NAV_SCHEMES):
        raise GuiError("blocked", f"只许 http(s) 跳转：{raw[:120]}")
    return raw


def check_text(text: object) -> str:
    """输入前的唯一校验：非空、<=200 字、不含 NUL/CR/TAB。纯函数，便于穷举。

    为什么不许 TAB/CR：在网页里这两个字符会被当成「切换焦点 / 提交」，
    于是「打字」变成了「按一下回车」——不可逆，必须挡在入口。
    """
    raw = str(text or "")
    if not raw.strip():
        raise GuiError("unsupported", "要输入的文字为空")
    if any(ch in raw for ch in ("\x00", "\r", "\t")):
        raise GuiError("blocked", "要输入的文字里含控制字符（NUL/CR/TAB）")
    if len(raw) > TEXT_MAX_CHARS:
        raise GuiError("blocked", f"要输入的文字太长（{len(raw)} > {TEXT_MAX_CHARS}）")
    return raw


@dataclass
class TypeResult:
    """一次「打字」的结果：**输入框的值**是唯一判据（不是像素差）。

    为什么不用像素：打一个字只变十几个像素，100 像素的门槛会把它判成失败；
    而「输入框里的值变了没有」是精确的、可复算的，正好当 state_before/state_after。
    """

    target: str = ""
    action: str = "type"
    selector: str = ""
    url_before: str = ""
    url_after: str = ""
    title_after: str = ""
    state_before: str = ""
    state_after: str = ""
    changed: dict[str, object] = field(default_factory=dict)
    value_before: str = ""
    value_after: str = ""
    expect_found: bool = False
    at: str = ""

    def is_changed(self) -> bool:
        return bool(self.changed)

    def criteria(self) -> dict[str, bool]:
        return {"value": bool(self.value_after) and self.value_after != self.value_before}

    def as_receipt(self) -> dict[str, object]:
        return {
            "kind": "gui_input",
            "target": self.target,
            "selector": self.selector,
            "state_before": self.state_before,
            "state_after": self.state_after,
            "changed": dict(self.changed),
            "value_len": len(self.value_after),
            "at": self.at,
        }


@dataclass(frozen=True)
class GuiMotion:
    """一次「动手」的结果：**状态差异式回执**。

    写动作没有幂等键（点两次就是两单），所以回执不能只写「发了」，
    必须写「动手前后差在哪」——这就是 `state_before / state_after / changed` 的用处。
    """

    target: str = ""
    action: str = ""
    url_before: str = ""
    url_after: str = ""
    title_after: str = ""
    state_before: str = ""
    state_after: str = ""
    changed: Mapping[str, Any] = field(default_factory=dict)
    #: 「我要看到的那句话」出现了吗：不算变化，但决定这次动手有没有达到目的。
    expect_found: bool = False
    at: str = ""

    def is_changed(self) -> bool:
        """有没有**实质**变化。空 dict 表示「动了，但界面没变」。"""
        return bool(self.changed)

    def as_dict(self) -> dict[str, object]:
        return {
            "target": self.target,
            "action": self.action,
            "url_before": self.url_before,
            "url_after": self.url_after,
            "title_after": self.title_after,
            "state_before": self.state_before,
            "state_after": self.state_after,
            "changed": dict(self.changed),
            "expect_found": self.expect_found,
            "at": self.at,
        }


def diff_snapshots(before: GuiSnapshot, after: GuiSnapshot) -> dict[str, Any]:
    """前后两屏的差异：只列**真的变了**的字段；一样就返回空 dict。"""
    changed: dict[str, Any] = {}
    if before.url != after.url:
        changed["url"] = {"before": before.url, "after": after.url}
    if before.title != after.title:
        changed["title"] = {"before": before.title, "after": after.title}
    if before.digest() != after.digest():
        changed["digest"] = {"before": before.digest(), "after": after.digest()}
    return changed


def expect_found(snapshot: GuiSnapshot, expect: str) -> bool:
    """「我要看到的那句话」在不在这一屏里（URL / 标题 / 正文都算）。"""
    needle = str(expect or "")
    if not needle:
        return False
    haystack = "\n".join((snapshot.url, snapshot.title, snapshot.text))
    return needle in haystack


class GuiReader(Protocol):
    """只读驱动：只承诺「能看一眼」。"""

    def snapshot(self, target: str = "", *, max_chars: int = SNAPSHOT_MAX_CHARS) -> GuiSnapshot:
        ...


def normalize_text(raw: object, *, max_chars: int = SNAPSHOT_MAX_CHARS) -> tuple[str, bool]:
    """把界面正文压成可进 prompt 的文本：`(文本, 是否被截断)`。

    只做三件事：去行尾空白、把连续空行压成一个、按字数截断。
    **不做摘要、不做重排**——读到的就是读到的，裁剪是唯一允许的加工。
    """
    text = str(raw or "").replace("\r\n", "\n").replace("\r", "\n")
    text = _TRAILING_WS.sub("", text)
    text = _BLANK_RUN.sub("\n\n", text).strip()
    limit = max(1, int(max_chars or SNAPSHOT_MAX_CHARS))
    if len(text) <= limit:
        return text, False
    return text[:limit].rstrip(), True


def check_selector(selector: object) -> str:
    """点击前的校验：非空、有上限、不带控制字符。纯函数，便于穷举。"""
    raw = " ".join(str(selector or "").split())
    if not raw:
        raise GuiError("unsupported", "选择器为空")
    if len(raw) > SELECTOR_MAX_CHARS:
        raise GuiError("blocked", f"选择器太长（>{SELECTOR_MAX_CHARS} 字）：{raw[:80]}")
    return raw


def foreground_window_title() -> str:
    """当前前台窗口的标题；取不到就返回空串（空串 = 判据不成立，**fail closed**）。

    为什么不比进程 id：CDP 这一档拿不到目标 hwnd，只能拿到「前台窗口在叫什么」。
    用标题匹配代替进程归属是**明确降级**——所以标题必须由注册项声明（`window_title`），
    不许「看到什么点什么」。
    """
    try:
        import ctypes

        user32 = ctypes.windll.user32  # type: ignore[attr-defined]
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return ""
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return ""
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        return " ".join(str(buf.value or "").split())
    except Exception:  # noqa: BLE001 - 非 Windows / 无权限一律当「取不到」
        warn("core/execution/gui/base.py:266 Exception", err=repr(__import__("sys").exc_info()[1]))
        return ""


def window_title_matches(declared: str, actual: str) -> bool:
    """声明的窗口标题是不是**包含**在实际标题里（大小写不敏感）。"""
    want = str(declared or "").strip().lower()
    have = str(actual or "").strip().lower()
    if not want or not have:
        return False
    return want in have


def count_changed_pixels(before: bytes, after: bytes) -> int:
    """两张 PNG 之间变化了至少 1 个通道的像素数；算不了返回 -1（= 证据缺失）。"""
    if not before or not after:
        return -1
    try:
        import io as _io

        from PIL import Image
    except Exception:  # noqa: BLE001 - 没装 PIL 就是「验不了」
        warn("core/execution/gui/base.py:287 Exception", err=repr(__import__("sys").exc_info()[1]))
        return -1
    try:
        a = Image.open(_io.BytesIO(before)).convert("RGB")
        b = Image.open(_io.BytesIO(after)).convert("RGB")
        if a.size != b.size:
            return -1
        pa, pb = a.load(), b.load()
        width, height = a.size
        changed = 0
        for y in range(height):
            for x in range(width):
                if pa[x, y] != pb[x, y]:
                    changed += 1
        return changed
    except Exception:  # noqa: BLE001
        warn("core/execution/gui/base.py:302 Exception", err=repr(__import__("sys").exc_info()[1]))
        return -1


@dataclass(frozen=True)
class ClickResult:
    """一次点击的回执：状态差异 + **三条判据的原始观测** + 证据文件。"""

    target: str = ""
    action: str = "click"
    selector: str = ""
    url_before: str = ""
    url_after: str = ""
    title_after: str = ""
    state_before: str = ""
    state_after: str = ""
    changed: dict[str, Any] = field(default_factory=dict)
    foreground_title: str = ""
    foreground_ok: bool = False
    title_ok: bool = False
    pixels_changed: int = -1
    evidence: str = ""
    evidence_before: str = ""
    expect_found: bool = False
    at: str = ""

    def is_changed(self) -> bool:
        return bool(self.changed)

    def criteria(self) -> dict[str, bool]:
        return {
            "foreground": bool(self.foreground_ok),
            "title": bool(self.title_ok),
            "pixels": int(self.pixels_changed) >= MIN_CHANGED_PIXELS,
        }

    def as_receipt(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "selector": self.selector,
            "state_before": self.state_before,
            "state_after": self.state_after,
            "changed": dict(self.changed),
            "criteria": self.criteria(),
            "pixels_changed": int(self.pixels_changed),
            "evidence": self.evidence,
            "evidence_before": self.evidence_before,
        }
