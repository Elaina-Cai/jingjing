"""撤回：右键那条消息 → 菜单里找「撤回」→ 点 → 回读确认。

三条设计判断（都是真机打出来的）：

1. **菜单里有没有「撤回」就是时间判据**。微信只对自己 2 分钟内发的消息给出这一项；
   所以 `revoke_message` 不自己算时钟，它只认**屏幕上真实出现的那一项**——
   菜单里没有就撤不了，这也是"撤回窗口"唯一不会骗人的证据。
   （时间预判留给 `core/speech/correction.py::can_revoke`，它决定要不要试。）
2. **右键必须用 `SetCursorPos` + `mouse_event` 长按**：`wx_human.click(button="right")`
   走的是 SendInput 短按，Qt 的菜单不出来（真机三次实测 0 像素变化）。
   见 `wx_human.click_menu()`。
3. **不确认就当没撤回**：点完菜单后必须同时看到两件事——那句话不在会话里了、
   会话里多了一行提到「撤回」的系统提示。少一件就 `failed`，绝不乐观报成功。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence

from core import features
from core.execution.gui.base import GuiError
from core.execution.gui.wechat import WECHAT_TITLE_HINTS, _looks_like_wechat
from safelog import warn

log = logging.getLogger(__name__)

#: 菜单里那一项的字（微信 4.x 中文界面）。
REVOKE_TOKEN = "撤回"

#: 点完右键等多久再拍菜单（Qt 菜单是独立的窗口，出现需要几十毫秒）。
MENU_WAIT_S = 0.6

#: 点完「撤回」等多久再回读。
AFTER_WAIT_S = 1.0

#: 一次按钮/菜单 OCR 最多看多少项（防某个界面把整屏文字都当成菜单）。
MENU_MAX_ITEMS = 24

#: 一句话最长多少字（与填字同量级）。
TEXT_MAX_CHARS = 200

#: 滚多少「一格」算翻一屏（一格 = WHEEL_DELTA 120，四格约一屏）。
WHEEL_PAGE = 480

#: 滚完一屏等多久再截图（等渲染跟上）。
SCROLL_WAIT_S = 0.45

#: 滚 480 单位大约走多少行。真机实测 5~6 行；只是估算，每一轮都用锚点重新对齐来纠偏。
ROWS_PER_PAGE = 5.0

#: 逼近最多几轮（每轮重新对齐，误差自我修正；正常两轮就到位）。
MAX_APPROACH_ROUNDS = 4

#: 一轮最多滚几屏（防止一次估算错得太离谱把库翻穿）。
MAX_PAGES_PER_ROUND = 20

#: 没有链路可对（库读不到）时的盲翻上限——这条路贵，所以短。
BLIND_MAX_PAGES = 3

STATUS_REVOKED = "revoked"
STATUS_FAILED = "failed"
STATUS_BLOCKED = "blocked"


class RevokeDeps(Protocol):
    """撤回要用的六件事——测试里全换成假的。"""

    def window_title(self) -> str: ...

    def foreground_title(self) -> str: ...

    def wake(self) -> bool: ...

    def open_chat(self, chat: str) -> Mapping[str, Any]: ...

    def items(self) -> Sequence[tuple]:
        """窗口内 OCR：``(text, cx, cy, x, y, score)``，窗口局部坐标。"""

    def menu_items(self) -> Sequence[tuple]:
        """右键菜单里的每一项：``(名字, 窗口内 x, 窗口内 y)``。

        为什么单开一个方法：菜单是**另一个顶层窗口**（类名
        ``Qt51514QWindowToolSaveBits``），OCR 读它极不稳（同一份菜单连读三次
        得三种结果，真机实测），UIA 才能给出确定的名字与坐标。
        """

    def click_right(self, x: int, y: int) -> None: ...

    def scroll(self, delta: int) -> None:
        """滚轮：正的往上翻（更早），负的往下翻。"""

    def click(self, x: int, y: int) -> None: ...

    def cancel(self) -> None:
        """关掉菜单（ESC）。"""

    def sleep(self, seconds: float) -> None: ...


@dataclass(frozen=True)
class RevokeReceipt:
    """一次撤回的回执。`status` 只有 revoked / failed / blocked。"""

    status: str
    detail: str
    chat: str = ""
    text_sha256: str = ""
    menu_item: str = ""
    local: tuple[int, int] = (0, 0)
    at: str = ""

    def is_revoked(self) -> bool:
        return self.status == STATUS_REVOKED

    def as_receipt(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "ack": "platform_id" if self.is_revoked() else "none",
            "kind": "revoke",
            "target": self.chat,
            "reason": "" if self.is_revoked() else self.detail,
            "detail": self.detail[:200],
            "ref": f"wechat-revoke#{self.text_sha256[:8]}" if self.text_sha256 else "",
            "at": self.at,
        }


def _mode() -> str:
    """唯一读点。"""
    return features.mode(features.WECHAT_REVOKE, features.MODE_OFF)


def _norm(text: object) -> str:
    return "".join(str(text or "").split())


def _digest(text: str) -> str:
    import hashlib

    return hashlib.sha256(_norm(text).encode("utf-8")).hexdigest()[:16]


def find_row(items: Sequence[tuple], text: str) -> tuple | None:
    """在 OCR 结果里找「就是这句」的那一行；多于一条即返回 None（不猜哪条）。"""
    want = _norm(text)
    if not want:
        return None
    hits = [it for it in items if isinstance(it, (list, tuple)) and _norm(it[0]) == want]
    if len(hits) != 1:
        return None
    return hits[0]


def find_menu_item(items: Sequence[tuple], token: str = REVOKE_TOKEN) -> tuple | None:
    """菜单里找那一项。只认「整项就是这个词」，避免点到「撤回」附近的别的东西。"""
    hits = [
        it
        for it in list(items)[:MENU_MAX_ITEMS]
        if isinstance(it, (list, tuple)) and _norm(it[0]) == token
    ]
    if len(hits) != 1:
        return None
    return hits[0]


def _has_revoke_notice(items: Sequence[tuple]) -> bool:
    return any(
        isinstance(it, (list, tuple)) and REVOKE_TOKEN in str(it[0] or "")
        for it in items
    )


def _deps() -> RevokeDeps:  # pragma: no cover - 真机实现
    from core.execution import wx_chat, wx_human, wx_vision

    class _Deps:
        def __init__(self) -> None:
            # OCR 给的是**窗口内**坐标，鼠标要的是**屏幕**坐标；origin 由 items() 记下。
            self._origin: tuple[int, int] = (0, 0)

        def window_title(self) -> str:
            win = wx_vision.find_wechat_window()
            return getattr(win, "title", "") if win else ""

        def foreground_title(self) -> str:
            win = wx_vision.find_wechat_window()
            if win and win.is_foreground():
                return getattr(win, "title", "")
            return ""

        def wake(self) -> bool:
            from core.execution.gui.wechat import wake_window

            return wake_window()

        def open_chat(self, chat: str) -> Mapping[str, Any]:
            return wx_chat.open_chat_strict(chat) or {}

        def items(self) -> Sequence[tuple]:
            win = wx_vision.find_wechat_window()
            img, rect = wx_vision.capture_window(win)
            try:
                self._origin = (int(rect[0]), int(rect[1]))
            except Exception:  # noqa: BLE001 - 拿不到就按 (0,0)，下一道闸会拦住
                warn("core/execution/gui/wechat_revoke.py:208 Exception", err=repr(__import__("sys").exc_info()[1]))
                self._origin = (0, 0)
            items = list(wx_vision.ocr_items(img))
            # 只认「正文区」里的行：左侧会话列表会把同一条预览也念出来（真机踩过：
            # 右键点到了列表预览上，菜单根本不出现），输入框下面也不是消息。
            try:
                layout = wx_vision.detect_layout(img)
                left = int(getattr(layout, "chat_left", 0) or 0)
                box = getattr(layout, "input_box", None)
                bottom = (int(box[1]) - 10) if box else int(img.height)
            except Exception:  # noqa: BLE001 - 量不出来就退化成整窗，靠「唯一命中」兜底
                warn("core/execution/gui/wechat_revoke.py:218 Exception", err=repr(__import__("sys").exc_info()[1]))
                left, bottom = 0, int(img.height)
            return [it for it in items if int(it[1]) >= left and int(it[2]) < bottom]

        def scroll(self, delta: int) -> None:
            """把鼠标放到正文区中心再滚（滚轮跟着鼠标走，别滚到会话列表上）。"""
            clicks = int(delta) // int(getattr(wx_human, "WHEEL_DELTA", 120) or 120)
            if clicks == 0:
                clicks = 1 if int(delta) > 0 else -1
            win = wx_vision.find_wechat_window()
            img, rect = wx_vision.capture_window(win)
            left, bottom = 0, int(img.height)
            try:
                layout = wx_vision.detect_layout(img)
                left = int(getattr(layout, "chat_left", 0) or 0)
                box = getattr(layout, "input_box", None)
                if box:
                    bottom = max(80, int(box[1]) - 10)
            except Exception:  # noqa: BLE001 - 量不出来就滚窗口中心
                warn("core/execution/gui/wechat_revoke.py:236 Exception", err=repr(__import__("sys").exc_info()[1]))
            x = int(rect[0]) + (left + int(img.width)) // 2
            y = int(rect[1]) + (60 + bottom) // 2
            wx_human.scroll(clicks, at=(x, y))

        def menu_items(self) -> Sequence[tuple]:
            """枚举微信同进程的弹出菜单，返回 ``(名字, 窗口内 x, 窗口内 y)``。"""
            ox, oy = self._origin
            try:
                import uiautomation as auto
                import win32gui
                import win32process

                main_hwnd = int(getattr(wx_vision.find_wechat_window(), "hwnd", 0) or 0)
                if not main_hwnd:
                    return []
                pid = win32process.GetWindowThreadProcessId(main_hwnd)[1]
                popups: list = []

                def walk(hwnd, _arg) -> bool:
                    if not win32gui.IsWindowVisible(hwnd) or hwnd == main_hwnd:
                        return True
                    try:
                        if win32process.GetWindowThreadProcessId(hwnd)[1] != pid:
                            return True
                    except Exception:  # noqa: BLE001
                        warn("core/execution/gui/wechat_revoke.py:262 Exception", err=repr(__import__("sys").exc_info()[1]))
                        return True
                    left, top, right, bottom = win32gui.GetWindowRect(hwnd)
                    width, height = right - left, bottom - top
                    if width < 40 or height < 20 or width > 700 or height > 900:
                        return True
                    popups.append(hwnd)
                    return True

                win32gui.EnumWindows(walk, None)
                out = []
                for hwnd in popups:
                    try:
                        children = auto.ControlFromHandle(hwnd).GetChildren()
                    except Exception:  # noqa: BLE001
                        warn("core/execution/gui/wechat_revoke.py:276 Exception", err=repr(__import__("sys").exc_info()[1]))
                        continue
                    for child in children:
                        if child.ControlTypeName not in ("MenuItemControl", "ListItemControl"):
                            continue
                        name = str(child.Name or "").strip()
                        if not name:
                            continue
                        rect = child.BoundingRectangle
                        out.append((
                            name,
                            (rect.left + rect.right) // 2 - ox,
                            (rect.top + rect.bottom) // 2 - oy,
                        ))
                return out
            except Exception as exc:  # noqa: BLE001 - 读不到菜单就当没有
                log.warning("wechat_revoke_menu_read_failed error=%r", exc)
                return []

        def click_right(self, x: int, y: int) -> None:
            ox, oy = self._origin
            wx_human.click_menu(ox + int(x), oy + int(y))

        def click(self, x: int, y: int) -> None:
            ox, oy = self._origin
            wx_human.click(ox + int(x), oy + int(y))

        def cancel(self) -> None:
            wx_human.hotkey(0x1B)

        def sleep(self, seconds: float) -> None:
            import time

            time.sleep(max(0.0, float(seconds)))

    return _Deps()


_CLOCK_RE = re.compile(r"^\s*(\d{1,2}):(\d{2})")


def _visible_span(items: Sequence[tuple]) -> tuple[int, int] | None:
    """屏幕上正文区那一段「钟点」的范围（秒）；一行钟点都读不到就 None。

    注意：微信 4.x **不是逐条显示时间**（只在换时段/跨天处插一行），所以大多数时候这里是
    None——方向就按「默认往上」走。这个函数只为「目标明显比屏上最新还新」这一种少见情形服务。
    """
    secs: list[int] = []
    for it in items:
        if not isinstance(it, (list, tuple)):
            continue
        hit = _CLOCK_RE.match(str(it[0] or ""))
        if not hit:
            continue
        hour, minute = int(hit.group(1)), int(hit.group(2))
        if 0 <= hour < 24 and 0 <= minute < 60:
            secs.append(hour * 3600 + minute * 60)
    if not secs:
        return None
    return min(secs), max(secs)


def _direction_for(chat: str, body: str, items: Sequence[tuple]) -> int:
    """该往上翻（+1）还是往下翻（-1）。

    判据是库里的 ``create_time`` 与屏上的钟点：目标比屏上最早那条还早 → 往上；
    比最晚那条还晚 → 往下。库读不到、或屏上没有钟点，默认往上——新消息总把旧消息往上推，
    所以「刚说完那句被人刷走」在九成情况下要往上翻。
    """
    span = _visible_span(items)
    if span is None:
        return 1
    try:
        from core.execution.gui.wechat import message_epoch

        epoch = message_epoch(chat, body)
    except Exception:  # noqa: BLE001 - 库读不了就按默认方向，不能让撤回卡死
        warn("core/execution/gui/wechat_revoke.py:352 Exception", err=repr(__import__("sys").exc_info()[1]))
        epoch = None
    if epoch is None:
        return 1
    when = datetime.fromtimestamp(int(epoch))
    if when.date() < datetime.now().date():
        return 1
    sec = when.hour * 3600 + when.minute * 60 + when.second
    low, high = span
    if sec < low:
        return 1
    if sec > high:
        return -1
    return 1


def _use_chain() -> bool:
    """能不能读库做锚点。默认能；测试里可以关掉走盲翻分支。"""
    return True


def _key(text: Any) -> str:
    """行文本的比较键：全文去空白（OCR 有时把空格弄丢，真机踩过）。"""
    return " " .join(str(text or "").split()).replace(" ", "")


def _anchors(screen: Sequence[str], rows: Sequence[Mapping[str, Any]]) -> list[tuple[int, int]]:
    """屏幕上那些「在库里只出现一次」的行 → ``[(屏幕下标, 库下标)]``。

    只认唯一行：同一句话说过两遍以上就不当锚点（宁可少认几个，也不认错）。
    """
    keys = [_key(r.get("text", "")) for r in rows]
    seen: dict[str, int] = {}
    for k in keys:
        if k:
            seen[k] = seen.get(k, 0) + 1
    pos = {k: i for i, k in enumerate(keys) if k and seen.get(k) == 1}
    out: list[tuple[int, int]] = []
    for si, text in enumerate(screen):
        k = _key(text)
        if k and k in pos:
            out.append((si, pos[k]))
    return out


def _offset(anchors: Sequence[tuple[int, int]]) -> int | None:
    """屏幕第 0 行对应库里的第几条（取所有锚点的中位数，抗个别错配）。"""
    if not anchors:
        return None
    diffs = sorted(dbi - si for si, dbi in anchors)
    return diffs[len(diffs) // 2]


def _target_index(rows: Sequence[Mapping[str, Any]], body: str) -> int | None:
    """目标那句在库里的下标；不唯一或找不到就 None。"""
    want = _key(body)
    if not want:
        return None
    hits = [i for i, r in enumerate(rows) if _key(r.get("text", "")) == want]
    return hits[0] if len(hits) == 1 else None


def _approach(
    impl: Any,
    body: str,
    rows: Sequence[Mapping[str, Any]],
    *,
    chat: str,
    rounds: int = MAX_APPROACH_ROUNDS,
) -> tuple[tuple | None, int, int]:
    """按库里的有序链路一步步逼近目标那句话：``(命中的行, 滚了几屏, 方向)``。

    每一步：截图 → 用唯一行对齐出「屏幕第 0 行 = 库里第几条」→ 算出目标在上方/下方第几条 →
    按 ``ROWS_PER_PAGE`` 折算成滚轮量一次滚过去 → 重新对齐。误差靠每轮重算吸收，
    所以通常两轮就停；目标明明落在屏内却找不到可点行（媒体/被折叠）就停手，不猜。
    """
    scroller = getattr(impl, "scroll", None)
    total = 0
    direction = 0
    for _ in range(int(rounds)):
        items = impl.items()
        row = find_row(items, body)
        if row is not None:
            return row, total, direction
        if _target_index(rows, body) is None:
            # 这句在库里不唯一（或压根找不到）——没有目标就没有方向，停手。
            return None, total, direction
        if not callable(scroller):
            break
        screen = [str(it[0]) for it in items if isinstance(it, (list, tuple))]
        target = _target_index(rows, body)
        anchors = _anchors(screen, rows)
        base = _offset(anchors)
        if target is None or len(anchors) < 2 or base is None:
            break
        lo = min(si for si, _ in anchors) + base
        hi = max(si for si, _ in anchors) + base
        if lo <= target <= hi:
            # 库里说它就该在这一屏，屏幕上却没有可点的行 → 媒体/被折叠，停手。
            log.info("wechat_revoke_target_occluded target=%s lo=%s hi=%s", target, lo, hi)
            break
        gap = target - lo if target < lo else target - hi
        pages = max(1, min(MAX_PAGES_PER_ROUND, int(round(abs(gap) / ROWS_PER_PAGE))))
        direction = 1 if gap < 0 else -1
        scroller(direction * WHEEL_PAGE * pages)
        total += pages
        try:
            impl.sleep(SCROLL_WAIT_S)
        except Exception:  # noqa: BLE001
            warn("core/execution/gui/wechat_revoke.py:460 Exception", err=repr(__import__("sys").exc_info()[1]))
    return None, total, direction


def _find_with_scroll(
    impl: Any, body: str, items: Sequence[tuple], *, chat: str, pages: int
) -> tuple[tuple | None, int, int]:
    """先看当前屏；找不到就按方向翻屏找。返回 ``(命中的行, 翻了几屏, 方向)``。

    翻屏只负责「让那句重新出现在屏幕上」；翻不到时由调用方滚回原位。
    """
    row = find_row(items, body)
    if row is not None:
        return row, 0, 0
    scroller = getattr(impl, "scroll", None)
    if not callable(scroller) or pages <= 0:
        return None, 0, 0
    direction = _direction_for(chat, body, items)
    done = 0
    prev = _screen_key(items)
    for _ in range(int(pages)):
        scroller(direction * WHEEL_PAGE)
        done += 1
        try:
            impl.sleep(SCROLL_WAIT_S)
        except Exception:  # noqa: BLE001 - 没有 sleep 的壳就当作已经等过了
            warn("core/execution/gui/wechat_revoke.py:486 Exception", err=repr(__import__("sys").exc_info()[1]))
        fresh = impl.items()
        row = find_row(fresh, body)
        if row is not None:
            return row, done, direction
        key = _screen_key(fresh)
        if key and key == prev:
            # 屏幕上什么都没变：要么到头了，要么这次滚轮没生效——再翻也没用。
            log.info("wechat_revoke_scroll_stuck pages=%s", done)
            break
        prev = key
    return None, done, direction


def _screen_key(items: Sequence[tuple]) -> str:
    """当前屏的指纹（首尾两行的文字），用来判断「再翻也不动了」。"""
    texts = [str(it[0]) for it in items if isinstance(it, (list, tuple))]
    if not texts:
        return ""
    return "\x1f".join((texts[0], texts[-1], str(len(texts))))


def _scroll_back(impl: Any, direction: int, pages: int) -> None:
    """把屏幕滚回原处（翻了几屏就退几屏）；失败只记日志，回执照写。"""
    scroller = getattr(impl, "scroll", None)
    if not callable(scroller) or not pages:
        return
    try:
        scroller(-direction * WHEEL_PAGE * int(pages))
    except Exception:  # noqa: BLE001
        log.warning("wechat_revoke_scroll_back_failed pages=%s", pages)
        return
    try:
        impl.sleep(SCROLL_WAIT_S)
    except Exception:  # noqa: BLE001
        warn("core/execution/gui/wechat_revoke.py:521 Exception", err=repr(__import__("sys").exc_info()[1]))


def revoke_message(
    chat: str,
    text: str,
    *,
    deps: RevokeDeps | None = None,
    now: datetime | None = None,
    menu_wait_s: float = MENU_WAIT_S,
    after_wait_s: float = AFTER_WAIT_S,
) -> RevokeReceipt:
    """撤回会话里「就是这句」的那条消息。

    fail closed 的五种情形（都不会点「撤回」或不会乐观报成功）：
    开关关着 / 没给会话 / 没给正文 / 屏幕上找不到这句（或有好几句一样的）/
    菜单里没有「撤回」/ 点完看不出变化。
    """
    stamp = (now or datetime.now()).isoformat(sep=" ", timespec="seconds")
    body = str(text or "").strip()
    digest = _digest(body)
    if _mode() == features.MODE_OFF:
        raise GuiError("blocked", "WECHAT_REVOKE=off：撤回没开")
    if not str(chat or "").strip():
        raise GuiError("no_target", "没给会话名，不知道在哪撤回")
    if not body:
        raise GuiError("unsupported", "要撤回哪一句？正文不能空")
    if len(body) > TEXT_MAX_CHARS:
        raise GuiError("blocked", f"要撤回的正文超过 {TEXT_MAX_CHARS} 字")

    impl = deps or _deps()
    if not _looks_like_wechat(str(impl.window_title() or "")):
        raise GuiError("not_connected", "没找到微信窗口")
    if not _looks_like_wechat(str(impl.foreground_title() or "")):
        wake = getattr(impl, "wake", None)
        if not (callable(wake) and wake()):
            raise GuiError("no_target", "微信窗口唤不出来（唤不出就不点，免得点在别的窗口上）")

    opened = impl.open_chat(chat)
    if isinstance(opened, Mapping) and opened.get("ok") is False:
        raise GuiError(
            "no_target",
            "没切到会话（%s）：%s" % (chat, opened.get("error") or "未知原因"),
        )

    rows: list[Mapping[str, Any]] = []
    if _use_chain():
        try:
            from core.execution.gui.wechat import recent_rows

            rows = recent_rows(chat)
        except Exception:  # noqa: BLE001 - 读不到链路不影响撤回，退回盲翻
            warn("core/execution/gui/wechat_revoke.py:573 Exception", err=repr(__import__("sys").exc_info()[1]))
            rows = []
    if rows:
        row, scrolled, direction = _approach(impl, body, rows, chat=chat)
    else:
        row, scrolled, direction = _find_with_scroll(
            impl, body, impl.items(), chat=chat, pages=BLIND_MAX_PAGES
        )
    if row is None:
        if scrolled:
            _scroll_back(impl, direction, scrolled)
        note = f"（翻了 {scrolled} 屏也没找到）" if scrolled else ""
        return RevokeReceipt(
            status=STATUS_FAILED,
            detail=f"not_found: 屏幕上找不到这一句{note}（或有多句一样，不猜哪条）",
            chat=chat,
            text_sha256=digest,
            at=stamp,
        )

    local = (int(row[1]), int(row[2]))
    impl.click_right(*local)
    impl.sleep(menu_wait_s)

    menu = impl.menu_items()
    item = find_menu_item(menu)
    if item is None:
        impl.cancel()
        return RevokeReceipt(
            status=STATUS_FAILED,
            detail=(
                "no_revoke: 菜单里没有「撤回」（超过 2 分钟，或右键没打在这条消息上）"
            ),
            chat=chat,
            text_sha256=digest,
            local=local,
            at=stamp,
        )

    impl.click(int(item[1]), int(item[2]))
    impl.sleep(after_wait_s)

    after = impl.items()
    still_there = find_row(after, body) is not None
    notice = _has_revoke_notice(after)
    if still_there or not notice:
        return RevokeReceipt(
            status=STATUS_FAILED,
            detail=(
                "unverified: 点了撤回但屏幕上看不出变化"
                f"（这句还在={still_there}，有撤回提示={notice}）"
            ),
            chat=chat,
            text_sha256=digest,
            menu_item=str(item[0]),
            local=local,
            at=stamp,
        )

    if scrolled:
        _scroll_back(impl, direction, scrolled)
    return RevokeReceipt(
        status=STATUS_REVOKED,
        detail="revoked: 那句不在了，会话里多了撤回提示"
        + (f"（先翻了 {scrolled} 屏才找到）" if scrolled else ""),
        chat=chat,
        text_sha256=digest,
        menu_item=str(item[0]),
        local=local,
        at=stamp,
    )
