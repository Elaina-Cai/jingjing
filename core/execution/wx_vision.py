"""Position-independent WeChat UI perception.

The hard problem this module solves
-----------------------------------

The WeChat window can be moved and resized at any time, and its internal layout
does **not** scale proportionally: the left navigation rail keeps a fixed width,
the recent-chat list keeps whatever width the user last dragged it to, and only
the conversation pane absorbs the remaining space.  A coordinate expressed as
"42% of the window width" is therefore worthless -- it lands on a different widget
for every window size.

The fix is to stop guessing geometry and start measuring it.  Every routine here
derives its coordinates from structure that is actually visible in the pixels:

* the conversation pane is bracketed by its header strip, a white band that
  always starts exactly at the pane's left edge, and by the white input field at
  the bottom -- together they locate the pane no matter where the list divider
  was dragged to;
* the input toolbar is the horizontal run of small monochrome glyphs sitting
  just above the window's bottom edge, and its leftmost glyph is the emoji
  button -- so the button is found by shape, not by a memorised offset;
* the sticker panel is the large light popup that straddles the pane boundary
  and floats above the toolbar, so it is recognised by where it sits relative to
  the pane rather than by a stored rectangle;
* sticker thumbnails are the non-background blobs on a regular lattice inside
  that popup, so their cells are read off the lattice.

Nothing here is tied to one window size, one DPI, or one WeChat build.
"""

from __future__ import annotations

import ctypes
import math
import time
from ctypes import wintypes
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Sequence

import structlog
from safelog import warn

log = structlog.get_logger()

__all__ = [
    "ensure_dpi_aware", "WindowInfo", "find_wechat_window", "activate_window",
    "foreground_hwnd", "frame_looks_like_wechat", "why_not_wechat",
    "why_not_searchable", "claim_window_frame", "process_is_higher_integrity",
    "get_ocr", "ocr_items", "find_emoji_button", "find_custom_tab",
    "capture", "capture_window", "Icon", "Layout", "detect_layout",
    "Panel", "detect_panel", "detect_panel_by_diff",
    "StickerCell", "detect_sticker_cells",
    "load_template_rgb", "match_template_multiscale", "find_sticker_in_panel",
    "images_similar", "analyze_image_file",
]

# --------------------------------------------------------------------------------------
# DPI
# --------------------------------------------------------------------------------------

_dpi_state = {"done": False, "mode": None}


def ensure_dpi_aware() -> Optional[str]:
    """Make this process per-monitor DPI aware exactly once.

    Everything downstream assumes that screen coordinates, window rectangles and
    grabbed bitmaps all live in the same physical-pixel space; doing this once is
    what keeps those three consistent on a scaled display.
    """
    if _dpi_state["done"]:
        return _dpi_state["mode"]

    mode = None
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PER_MONITOR_DPI_AWARE
        mode = "per_monitor"
    except Exception:
        warn("core/execution/wx_vision.py:79 Exception", err=repr(__import__("sys").exc_info()[1]))
        try:
            ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
            mode = "per_monitor_v2"
        except Exception:
            warn("core/execution/wx_vision.py:83 Exception", err=repr(__import__("sys").exc_info()[1]))
            try:
                ctypes.windll.user32.SetProcessDPIAware()
                mode = "system"
            except Exception as exc:
                log.warning("dpi_awareness_failed", error=str(exc))

    _dpi_state["done"] = True
    _dpi_state["mode"] = mode
    log.debug("dpi_awareness_set", mode=mode)
    return mode


# --------------------------------------------------------------------------------------
# Window discovery
# --------------------------------------------------------------------------------------

_WECHAT_EXES = ("weixin.exe", "wechat.exe")
_LEGACY_CLASSES = ("Qt51514QWindowIcon", "Qt51515QWindowIcon", "mmui::MainWindow", "WeChatMainWndForPC")


@dataclass
class WindowInfo:
    hwnd: int
    left: int
    top: int
    right: int
    bottom: int
    title: str = ""
    cls: str = ""
    exe: str = ""
    pid: int = 0
    dpi: int = 96
    minimized: bool = False
    visible: bool = True
    score: float = 0.0
    notes: dict = field(default_factory=dict)

    @property
    def width(self) -> int:
        return self.right - self.left

    @property
    def height(self) -> int:
        return self.bottom - self.top

    @property
    def rect(self) -> tuple:
        return (self.left, self.top, self.right, self.bottom)

    def refresh(self) -> "WindowInfo":
        import win32gui

        self.minimized = bool(ctypes.windll.user32.IsIconic(self.hwnd))
        self.visible = bool(ctypes.windll.user32.IsWindowVisible(self.hwnd))
        if self.minimized:
            # A minimised window reports a tiny icon-sized rectangle; the
            # restore geometry is what the window will occupy once shown.
            normal = _placement_rect(self.hwnd)
            if normal:
                self.left, self.top, self.right, self.bottom = normal
        else:
            self.left, self.top, self.right, self.bottom = win32gui.GetWindowRect(self.hwnd)
        try:
            self.dpi = int(ctypes.windll.user32.GetDpiForWindow(self.hwnd)) or 96
        except Exception:
            warn("core/execution/wx_vision.py:148 Exception", err=repr(__import__("sys").exc_info()[1]))
            self.dpi = 96
        return self

    def to_screen(self, lx: float, ly: float) -> tuple:
        return int(round(self.left + lx)), int(round(self.top + ly))

    def is_foreground(self) -> bool:
        return foreground_hwnd() == self.hwnd

    def as_dict(self) -> dict:
        return {"hwnd": self.hwnd, "rect": list(self.rect), "size": [self.width, self.height],
                "title": self.title, "class": self.cls, "exe": self.exe, "pid": self.pid,
                "dpi": self.dpi, "minimized": self.minimized, "visible": self.visible,
                "foreground": self.is_foreground(), "score": round(self.score, 2)}


def _exe_name(pid: int) -> str:
    try:
        import psutil

        return (psutil.Process(pid).name() or "").lower()
    except Exception:
        warn("core/execution/wx_vision.py:170 Exception", err=repr(__import__("sys").exc_info()[1]))
        return ""


class _WINDOWPLACEMENT(ctypes.Structure):
    _fields_ = [
        ("length", wintypes.UINT),
        ("flags", wintypes.UINT),
        ("showCmd", wintypes.UINT),
        ("ptMinPosition", wintypes.POINT),
        ("ptMaxPosition", wintypes.POINT),
        ("rcNormalPosition", wintypes.RECT),
    ]


def _placement_rect(hwnd: int):
    """The geometry a minimised window returns to, in physical pixels."""
    try:
        placement = _WINDOWPLACEMENT()
        placement.length = ctypes.sizeof(_WINDOWPLACEMENT)
        if ctypes.windll.user32.GetWindowPlacement(ctypes.c_void_p(hwnd),
                                                   ctypes.byref(placement)):
            rect = placement.rcNormalPosition
            return int(rect.left), int(rect.top), int(rect.right), int(rect.bottom)
    except Exception:
        warn("core/execution/wx_vision.py:194 Exception", err=repr(__import__("sys").exc_info()[1]))
    return None


def _is_cloaked(hwnd: int) -> bool:
    try:
        value = ctypes.c_int(0)
        ctypes.windll.dwmapi.DwmGetWindowAttribute(
            ctypes.c_void_p(hwnd), ctypes.c_uint(14), ctypes.byref(value), ctypes.sizeof(value)
        )
        return bool(value.value)
    except Exception:
        warn("core/execution/wx_vision.py:206 Exception", err=repr(__import__("sys").exc_info()[1]))
        return False


def process_is_higher_integrity(pid: int) -> bool | None:
    """Whether a process runs at a higher integrity level than ours.

    Windows UIPI silently drops synthesized input (clicks and keys alike)
    aimed at a window owned by a higher-integrity process, and refuses
    foreground changes with "access denied".  An unelevated 鲸鲸 can therefore
    never drive an elevated WeChat -- every send would fail with a misleading
    "text never reached the box".

    Probe: OpenProcess for limited query succeeds even against an elevated
    peer under UAC, but asking for that peer's token is refused with access
    denied; a peer of our own level lets the token read through.  Access
    denied on either call means we are the lower-privileged side.
    """
    if not pid:
        return None
    try:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        adv = ctypes.WinDLL("advapi32", use_last_error=True)
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        adv.OpenProcessToken.restype = wintypes.BOOL
        adv.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                         ctypes.POINTER(wintypes.HANDLE)]
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        TOKEN_QUERY = 0x0008
        handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not handle:
            return True if ctypes.get_last_error() == 5 else None
        token = wintypes.HANDLE()
        got = adv.OpenProcessToken(handle, TOKEN_QUERY, ctypes.byref(token))
        err = ctypes.get_last_error()
        if got:
            k32.CloseHandle(token)
        k32.CloseHandle(handle)
        if not got:
            return True if err == 5 else None
        return False
    except Exception:
        warn("core/execution/wx_vision.py:249 Exception", err=repr(__import__("sys").exc_info()[1]))
        return None


def find_wechat_window(*, allow_minimized: bool = True) -> Optional[WindowInfo]:
    """Locate the main WeChat window by process identity, not by window class.

    WeChat 4.x uses a Qt window class whose numeric suffix changes between builds
    (the old code hardcoded one specific value), so the process executable is the
    only stable anchor.
    """
    import win32gui
    import win32process

    ensure_dpi_aware()

    best: list = [None]

    def consider(hwnd: int) -> None:
        if not win32gui.IsWindow(hwnd) or _is_cloaked(hwnd):
            return

        visible = bool(win32gui.IsWindowVisible(hwnd))
        minimized = bool(ctypes.windll.user32.IsIconic(hwnd))
        if not visible and not allow_minimized:
            return

        try:
            left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        except Exception:
            warn("core/execution/wx_vision.py:278 Exception", err=repr(__import__("sys").exc_info()[1]))
            return

        if minimized:
            normal = _placement_rect(hwnd)
            if normal:
                left, top, right, bottom = normal

        width, height = right - left, bottom - top
        if width < 420 or height < 360:
            return

        try:
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
        except Exception:
            warn("core/execution/wx_vision.py:292 Exception", err=repr(__import__("sys").exc_info()[1]))
            pid = 0

        exe = _exe_name(pid)
        if exe not in _WECHAT_EXES:
            return

        title = win32gui.GetWindowText(hwnd) or ""
        cls = win32gui.GetClassName(hwnd) or ""

        score = 6.0
        if cls.startswith("Qt") and "QWindowIcon" in cls:
            score += 3.0
        if cls in _LEGACY_CLASSES:
            score += 1.0
        if title in ("微信", "WeChat", "Weixin"):
            score += 3.0
        if visible and not minimized:
            score += 2.0
        elif not visible:
            # Parked in the notification area: still the window we must drive,
            # just ranked below one that is already on screen.
            score -= 2.0
        score += min(2.0, (width * height) / 1_500_000.0)

        info = WindowInfo(hwnd=hwnd, left=left, top=top, right=right, bottom=bottom,
                          title=title, cls=cls, exe=exe, pid=pid, minimized=minimized,
                          score=score)
        info.refresh()
        if best[0] is None or info.score > best[0].score:
            best[0] = info

    def enum_cb(hwnd, _lparam):
        try:
            consider(hwnd)
        except Exception:
            warn("core/execution/wx_vision.py:327 Exception", err=repr(__import__("sys").exc_info()[1]))
        return True

    win32gui.EnumWindows(enum_cb, None)

    if best[0] is None:
        # Older code path, kept so nothing regresses on a build whose process
        # name we do not recognise.
        for cls in _LEGACY_CLASSES:
            hwnd = win32gui.FindWindow(cls, None)
            if not hwnd:
                continue
            try:
                left, top, right, bottom = win32gui.GetWindowRect(hwnd)
            except Exception:
                warn("core/execution/wx_vision.py:342 Exception", err=repr(__import__("sys").exc_info()[1]))
                continue
            if right - left < 420 or bottom - top < 360:
                continue
            try:
                _, pid = win32process.GetWindowThreadProcessId(hwnd)
            except Exception:
                warn("core/execution/wx_vision.py:348 Exception", err=repr(__import__("sys").exc_info()[1]))
                pid = 0
            exe = _exe_name(pid)
            if exe and exe not in _WECHAT_EXES:
                continue
            info = WindowInfo(hwnd=hwnd, left=left, top=top, right=right, bottom=bottom,
                              title=win32gui.GetWindowText(hwnd) or "", cls=cls,
                              exe=exe, pid=pid, score=1.0)
            info.refresh()
            best[0] = info
            log.info("wechat_found_by_class_fallback", hwnd=hwnd, cls=cls)
            break

    if best[0] is None:
        log.warning("wechat_window_not_found")
    else:
        log.debug("wechat_window_found", **best[0].as_dict())
    return best[0]


def _raise_to_top(hwnd: int) -> None:
    """Force a window above every non-topmost window without resizing it."""
    try:
        HWND_TOP = 0
        SWP_NOSIZE = 0x0001
        SWP_NOMOVE = 0x0002
        SWP_NOACTIVATE = 0x0010
        ctypes.windll.user32.SetWindowPos(
            ctypes.c_void_p(hwnd), ctypes.c_void_p(HWND_TOP), 0, 0, 0, 0,
            SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE)
    except Exception as exc:
        log.debug("raise_to_top_failed", error=str(exc))


def _force_foreground(hwnd: int, *, tries: int = 3) -> bool:
    """把窗口抢到前台。Windows 只允许"当前前台进程"调 SetForegroundWindow，
    后台进程调用会被静默忽略（日志表现：`wx_activate_failed hwnd=… minimized=False visible=True`）。
    标准绕法：先模拟一次 Alt 键（让本线程短暂获得前台权），再 SetForegroundWindow；
    仍不行就 TOPMOST 抬升 + BringWindowToTop + SwitchToThisWindow，重试若干次。
    """
    import win32con
    import win32gui
    import win32process

    user32 = ctypes.windll.user32
    VK_MENU = 0x12
    for attempt in range(tries):
        if user32.GetForegroundWindow() == hwnd:
            return True
        try:
            if user32.IsIconic(hwnd):
                win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
                time.sleep(0.2)
            user32.keybd_event(VK_MENU, 0, 0, 0)
            user32.keybd_event(VK_MENU, 0, 2, 0)
            try:
                _, target_tid = win32process.GetWindowThreadProcessId(hwnd)
            except Exception:
                target_tid = 0
            our_tid = ctypes.windll.kernel32.GetCurrentThreadId()
            attached = False
            try:
                if target_tid and target_tid != our_tid:
                    attached = bool(user32.AttachThreadInput(our_tid, target_tid, True))
                user32.SetWindowPos(hwnd, -1, 0, 0, 0, 0, 0x0001 | 0x0002)
                win32gui.BringWindowToTop(hwnd)
                user32.SetForegroundWindow(hwnd)
                user32.SetWindowPos(hwnd, -2, 0, 0, 0, 0, 0x0001 | 0x0002)
            finally:
                if attached:
                    try:
                        user32.AttachThreadInput(our_tid, target_tid, False)
                    except Exception:
                        pass
            if user32.GetForegroundWindow() == hwnd:
                return True
            try:
                user32.SwitchToThisWindow(hwnd, True)
            except Exception:
                pass
            if user32.GetForegroundWindow() == hwnd:
                return True
        except Exception:
            warn("core/execution/wx_vision.py:_force_foreground Exception",
                 err=repr(__import__("sys").exc_info()[1]))
        time.sleep(0.25 * (attempt + 1))
    return user32.GetForegroundWindow() == hwnd


def activate_window(win: WindowInfo, *, settle: float = 0.35) -> bool:
    """Bring the window to the foreground, restoring it first if minimised."""
    import win32con
    import win32gui
    import win32process

    user32 = ctypes.windll.user32
    hwnd = win.hwnd

    try:
        if not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
            # Closed to the notification area, or minimised.  SW_SHOW is what
            # actually un-hides a Qt window that was hidden rather than iconic.
            win32gui.ShowWindow(hwnd, win32con.SW_SHOW)
            time.sleep(0.15)
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            time.sleep(settle)

        if user32.GetForegroundWindow() == hwnd:
            win.refresh()
            return True

        try:
            _, target_tid = win32process.GetWindowThreadProcessId(hwnd)
        except Exception:
            warn("core/execution/wx_vision.py:406 Exception", err=repr(__import__("sys").exc_info()[1]))
            target_tid = 0
        our_tid = ctypes.windll.kernel32.GetCurrentThreadId()
        attached = False
        try:
            if target_tid and target_tid != our_tid:
                attached = bool(user32.AttachThreadInput(our_tid, target_tid, True))

            win32gui.BringWindowToTop(hwnd)
            try:
                _force_foreground(hwnd)
            except Exception:
                warn("core/execution/wx_vision.py:417 Exception", err=repr(__import__("sys").exc_info()[1]))

            # Foreground and z-order are separate things.  SetForegroundWindow
            # can hand WeChat the input focus while another window stays painted
            # on top of it, and a screen grab then returns that window's pixels
            # for the part of WeChat it covers -- which is how a browser sitting
            # over the input box made the toolbar look absent.
            _raise_to_top(hwnd)
        finally:
            # Attaching two threads' input queues changes how the system routes
            # every keyboard and mouse event, so detaching is not optional.  If
            # an exception escaped between attach and detach the queues stayed
            # joined, and WeChat stopped responding to clicks altogether -- a
            # window that is plainly there and simply will not be clicked.  The
            # outer handler used to swallow exactly that, silently.
            if attached:
                try:
                    user32.AttachThreadInput(our_tid, target_tid, False)
                except Exception as exc:
                    log.warning("detach_thread_input_failed", error=str(exc))

        # Give the foreground change time to actually land before judging it.
        # A 0.15s check used to call it a failure while Windows was still
        # switching, so the fallback below ran on almost every activation.
        for _ in range(10):
            if user32.GetForegroundWindow() == hwnd:
                break
            time.sleep(0.05)

        if user32.GetForegroundWindow() != hwnd:
            # Still not foreground: Windows refuses foreground changes from a
            # background process, so borrow the foreground window's thread for
            # one more attempt rather than minimising.
            #
            # This used to minimise and restore WeChat instead.  Repeated
            # minimise/restore on a Qt window leaves a frame that is painted but
            # neither repaints nor accepts input -- a window that looks exactly
            # like WeChat and behaves like a screenshot of it.  Resizing or
            # hiding the window is not worth that risk.
            fg = user32.GetForegroundWindow()
            fg_tid = 0
            try:
                if fg:
                    _, fg_tid = win32process.GetWindowThreadProcessId(fg)
            except Exception:
                warn("core/execution/wx_vision.py:462 Exception", err=repr(__import__("sys").exc_info()[1]))
                fg_tid = 0
            borrowed = False
            try:
                if fg_tid and fg_tid != our_tid:
                    borrowed = bool(user32.AttachThreadInput(our_tid, fg_tid, True))
                try:
                    user32.SetForegroundWindow(hwnd)
                except Exception:
                    warn("core/execution/wx_vision.py:470 Exception", err=repr(__import__("sys").exc_info()[1]))
                win32gui.BringWindowToTop(hwnd)
                time.sleep(0.1)
            finally:
                if borrowed:
                    try:
                        user32.AttachThreadInput(our_tid, fg_tid, False)
                    except Exception as exc:
                        log.warning("detach_foreground_thread_failed", error=str(exc))

        win.refresh()
        ok = user32.GetForegroundWindow() == hwnd
        log.debug("activate_window", hwnd=hwnd, ok=ok)
        return ok
    except Exception as exc:
        log.warning("activate_window_failed", error=str(exc))
        return False


# --------------------------------------------------------------------------------------
# Capture and image helpers
# --------------------------------------------------------------------------------------

def foreground_hwnd() -> int:
    """前台窗口句柄；没有前台窗口时返回 0。

    锁屏、远程会话断开、桌面切换期间 GetForegroundWindow() 返回 0，ctypes 里是 None。
    原来直接 int(None) 抛 TypeError，把整次发送判成失败（11-20 实测 18 次，全部到
    同一个会话、每小时一条）。
    """
    return int(ctypes.windll.user32.GetForegroundWindow() or 0)


def why_not_searchable(layout: "Layout") -> str:
    """A weaker test than frame_looks_like_wechat, for the search step only.

    frame_looks_like_wechat demands an input box and a toolbar, which only
    exist once a conversation is open.  Requiring that before searching
    deadlocked the whole flow: WeChat sat on its placeholder pane, every
    attempt refused to search, and so no conversation could ever be opened
    again.  Searching only needs the window-level anchors -- the session list
    and its divider -- because opening a conversation is the point of the
    search, not a precondition for it.

    Typing stays guarded where it actually matters: the query is read back out
    of the search box afterwards, and a query that lands in the message box
    aborts the send.
    """
    if layout.chat_left is None:
        return "找不到会话列表与聊天区的分界线"
    if not (layout.width * 0.12 <= layout.chat_left <= layout.width * 0.88):
        return "会话列表分界线异常 chat_left=%d (窗口宽 %d)" % (layout.chat_left, layout.width)
    if layout.header is None:
        return "找不到会话列表顶部"
    if layout.width < 300 or layout.height < 300:
        return "窗口太小 %dx%d" % (layout.width, layout.height)
    return ""


def why_not_wechat(layout: "Layout") -> str:
    """Explain which invariant a frame failed. Empty string means it passed.

    The check itself answers yes/no; when it says no, the operator needs to
    know *which* anchor was missing -- a covered toolbar, a session that is not
    a chat, and a window that moved mid-grab all look identical otherwise.
    """
    if layout.emoji is None:
        return "找不到输入栏的表情按钮（工具条被遮挡，或微信不在聊天界面）"
    if layout.header is None:
        return "找不到会话头部（微信可能停在非聊天页面）"
    if layout.input_box is None:
        return "找不到输入框"

    width, height = layout.width, layout.height
    if width < 200 or height < 200:
        return "窗口太小 %dx%d" % (width, height)
    if not (width * 0.15 <= layout.chat_left <= width * 0.85):
        return "聊天区分界线位置异常 chat_left=%d (窗口宽 %d)" % (layout.chat_left, width)

    offset = layout.emoji.cx - layout.chat_left
    if not (10.0 <= offset <= width * 0.35):
        return "表情按钮与聊天区边界的距离异常 %d px（正常约 45）" % round(offset)

    if layout.toolbar_y:
        from_bottom = height - layout.toolbar_y
        if not (height * 0.005 <= from_bottom <= height * 0.25):
            return "工具条距窗口底部 %d px 异常" % round(from_bottom)

    hx, hy, hw, hh = layout.header
    if hw < width * 0.25:
        return "会话头部太窄 %d px" % hw
    if not (8 <= hh <= height * 0.12):
        return "会话头部高度异常 %d px" % hh

    ix0, iy0, ix1, iy1 = layout.input_box
    if (ix1 - ix0) < width * 0.20:
        return "输入框太窄 %d px" % (ix1 - ix0)
    if (iy1 - iy0) < height * 0.04:
        return "输入框太扁 %d px" % (iy1 - iy0)

    return ""


def frame_looks_like_wechat(layout: "Layout") -> bool:
    """Whether a grabbed frame actually shows the WeChat conversation view.

    A screen grab returns whatever is painted at those coordinates.  If another
    window covers WeChat, or the window moved between measuring and grabbing,
    the pixels are still perfectly valid -- they just are not WeChat.  Acting on
    them means typing into, and clicking on, the wrong application.

    A single glyph run is not enough evidence: any busy screen can produce one.
    The checks below are the invariants that held across every real WeChat
    capture measured (1618x1537, 1299x1537 and 1548x1255, at 150% DPI):
    all three anchors present, the emoji button a small fixed distance right of
    the pane edge, and the toolbar a small fixed distance above the bottom.
    """
    if layout.emoji is None or layout.header is None or layout.input_box is None:
        return False

    width, height = layout.width, layout.height
    if width < 200 or height < 200:
        return False

    if not (width * 0.15 <= layout.chat_left <= width * 0.85):
        return False

    offset = layout.emoji.cx - layout.chat_left
    if not (10.0 <= offset <= width * 0.35):
        return False

    if layout.toolbar_y:
        from_bottom = height - layout.toolbar_y
        if not (height * 0.005 <= from_bottom <= height * 0.25):
            return False

    hx, hy, hw, hh = layout.header
    if hw < width * 0.25 or not (8 <= hh <= height * 0.12):
        return False

    ix0, iy0, ix1, iy1 = layout.input_box
    if (ix1 - ix0) < width * 0.20 or (iy1 - iy0) < height * 0.04:
        return False

    return True


def capture(rect: Sequence[int]):
    """Grab a screen rectangle as an RGB PIL image."""
    from PIL import ImageGrab

    ensure_dpi_aware()
    left, top, right, bottom = (int(v) for v in rect)
    if right <= left or bottom <= top:
        raise ValueError("empty capture rect: %r" % (rect,))
    return ImageGrab.grab(bbox=(left, top, right, bottom), all_screens=True).convert("RGB")


def capture_window(win: WindowInfo):
    """Grab the whole WeChat window; returns (image, rect) in screen coordinates."""
    win.refresh()
    return capture(win.rect), win.rect


def claim_window_frame(win: WindowInfo, *, attempts: int = 3, settle: float = 0.45):
    """Grab the window, forcing it to the front until the frame really is WeChat.

    A screen grab returns whatever is painted at those coordinates, so an
    occluded WeChat yields a perfectly valid picture of some other application.
    Rather than trusting the coordinates, this checks that the conversation
    chrome is actually present and re-activates the window when it is not.

    Returns (image, layout).  Callers should still confirm the result with
    frame_looks_like_wechat, because a window that stays covered -- or one
    sitting on a login or settings screen -- cannot be driven safely.
    """
    ensure_dpi_aware()
    image, _rect = capture_window(win)
    layout = detect_layout(image)
    if frame_looks_like_wechat(layout):
        return image, layout

    for attempt in range(max(1, attempts)):
        log.info("claiming_window_frame", attempt=attempt + 1, foreground=foreground_hwnd(),
                 target=win.hwnd)
        activate_window(win)
        time.sleep(settle)
        image, _rect = capture_window(win)
        layout = detect_layout(image)
        if frame_looks_like_wechat(layout):
            return image, layout

    return image, layout


def _np():
    import numpy as np

    return np


def _cv2():
    import cv2

    return cv2


def _as_rgb(img):
    np = _np()
    if hasattr(img, "convert"):
        return np.asarray(img.convert("RGB"))
    arr = np.asarray(img)
    if arr.ndim == 2:
        return _cv2().cvtColor(arr, _cv2().COLOR_GRAY2RGB)
    return arr


def _to_gray(arr):
    if arr.ndim == 2:
        return arr
    return _cv2().cvtColor(arr, _cv2().COLOR_RGB2GRAY)


_OCR_ENGINE = None


def get_ocr():
    """Shared RapidOCR engine, constructed once per process.

    OCR is a perception primitive like screenshotting or greying, so it lives
    here rather than inside whichever sender happened to need it first; that
    keeps senders from having to import each other's private helpers.
    """
    global _OCR_ENGINE
    if _OCR_ENGINE is None:
        from rapidocr_onnxruntime import RapidOCR

        _OCR_ENGINE = RapidOCR()
    return _OCR_ENGINE


def ocr_items(image) -> list:
    """Run OCR over an image and return (text, cx, cy, x, y, confidence) tuples."""
    np = _np()
    try:
        result, _ = get_ocr()(np.asarray(image))
    except Exception as exc:
        log.warning("ocr_failed", error=str(exc))
        return []

    items = []
    for entry in result or []:
        bbox, text, confidence = entry
        text = str(text).strip()
        if not text:
            continue
        x_left, y_top = int(bbox[0][0]), int(bbox[0][1])
        x_right, y_bottom = int(bbox[2][0]), int(bbox[2][1])
        items.append((text, (x_left + x_right) // 2, (y_top + y_bottom) // 2,
                      x_left, y_top, float(confidence)))
    return items


def images_similar(img_a, img_b, threshold: float = 0.985) -> bool:
    """True when two grabs of the same region are essentially identical."""
    np = _np()
    cv2 = _cv2()
    a = _to_gray(_as_rgb(img_a))
    b = _to_gray(_as_rgb(img_b))
    if a.shape != b.shape:
        return False
    return float((cv2.absdiff(a, b) > 6).mean()) < (1.0 - threshold)


@dataclass
class Icon:
    """A small glyph located by connected-component analysis, in local pixels."""

    x: int
    y: int
    w: int
    h: int
    area: int = 0
    saturation: float = 0.0

    @property
    def cx(self) -> float:
        return self.x + self.w / 2.0

    @property
    def cy(self) -> float:
        return self.y + self.h / 2.0

    @property
    def cx_i(self) -> int:
        return int(round(self.cx))

    @property
    def cy_i(self) -> int:
        return int(round(self.cy))

    def as_dict(self) -> dict:
        return {"x": self.x, "y": self.y, "w": self.w, "h": self.h,
                "cx": round(self.cx, 1), "cy": round(self.cy, 1), "area": self.area}


def _white_blocks(gray, y0: int, y1: int, *, min_h: int, min_w: int, level: int = 242):
    """Connected components of near-white pixels, largest first."""
    np = _np()
    cv2 = _cv2()
    y0 = max(0, int(y0)); y1 = min(gray.shape[0], int(y1))
    if y1 - y0 < 4:
        return []
    mask = (gray[y0:y1, :] >= level).astype("uint8")
    if mask.sum() < min_h * min_w * 0.2:
        return []
    count, _labels, stats, _centroids = cv2.connectedComponentsWithStats(mask, 8)
    out = []
    for i in range(1, count):
        x, y, w, h, area = (int(v) for v in stats[i])
        if w < min_w or h < min_h:
            continue
        out.append((x, y0 + y, w, h, area))
    out.sort(key=lambda b: -b[4])
    return out


# --------------------------------------------------------------------------------------
# Layout
# --------------------------------------------------------------------------------------

@dataclass
class Layout:
    """Measured geometry of one WeChat window, in window-local pixels."""

    width: int
    height: int
    chat_left: int = 0
    nav_right: int = 0
    input_box: Optional[tuple] = None
    toolbar_y: int = 0
    icons: list = field(default_factory=list)
    emoji: Optional[Icon] = None
    header: Optional[tuple] = None
    notes: dict = field(default_factory=dict)

    def emoji_point(self) -> Optional[tuple]:
        return None if self.emoji is None else (self.emoji.cx_i, self.emoji.cy_i)

    def as_dict(self) -> dict:
        return {
            "size": [self.width, self.height],
            "chat_left": self.chat_left, "nav_right": self.nav_right,
            "header": self.header, "input_box": self.input_box,
            "toolbar_y": self.toolbar_y,
            "emoji": self.emoji.as_dict() if self.emoji else None,
            "icons": [i.as_dict() for i in self.icons],
            "notes": self.notes,
        }


def _find_chat_left(gray, width: int, height: int) -> tuple:
    """Locate the conversation pane's left edge.

    Two independent anchors are consulted, both of which are painted flush with
    the pane edge and neither of which the sticker popup can ever cover:

    * the conversation header strip just under the title bar;
    * the white input field at the bottom of the pane.

    The profile heuristic is kept only as a last resort.
    """
    np = _np()
    cands = {}

    header_blocks = _white_blocks(gray, int(height * 0.028), int(height * 0.115),
                                  min_h=max(12, int(height * 0.022)),
                                  min_w=max(40, int(width * 0.25)))
    if header_blocks:
        hx, hy, hw, hh, _ = header_blocks[0]
        cands["header"] = hx
        cands["_header_box"] = (hx, hy, hw, hh)

    input_blocks = _white_blocks(gray, int(height * 0.58), height - int(height * 0.01),
                                 min_h=max(50, int(height * 0.06)),
                                 min_w=max(60, int(width * 0.20)))
    if input_blocks:
        ix, iy, iw, ih, _ia = input_blocks[0]
        cands["input"] = ix
        cands["_input_box"] = (ix, iy, ix + iw, iy + ih)  # normalised to x0,y0,x1,y1

    estimate = None
    source = None
    if "header" in cands:
        estimate, source = cands["header"], "header"
        if "input" in cands:
            # The input field is inset from the pane edge by a small constant
            # border; if it disagrees wildly the pane is probably split.
            if abs(cands["input"] - cands["header"]) > max(30, width * 0.05):
                estimate, source = cands["header"], "header_only"
    elif "input" in cands:
        estimate, source = cands["input"], "input"

    if estimate is None:
        y0, y1 = int(height * 0.15), int(height * 0.42)
        if y1 - y0 < 40:
            y0, y1 = int(height * 0.10), int(height * 0.55)
        band = gray[y0:y1, :]
        profile = np.percentile(band, 75, axis=0)
        kernel = max(5, int(width * 0.012) | 1)
        smooth = np.convolve(profile, np.ones(kernel) / kernel, mode="same")
        bright = smooth >= 238.0
        need = max(20, int(width * 0.18))
        found = None
        for x in range(int(width * 0.12), width - need):
            if bright[x] and bright[x:x + need].all():
                found = x
                break
        if found is None:
            diff = np.diff(smooth)
            lo, hi = int(width * 0.12), int(width * 0.78)
            if hi > lo:
                idx = lo + int(np.argmax(diff[lo:hi]))
                if diff[idx] > 4:
                    found = idx
        estimate = found if found is not None else int(width * 0.30)
        source = "profile" if found is not None else "fallback"

    # Sanity: the pane must occupy a plausible slice of the window.
    lo, hi = int(width * 0.10), int(width * 0.85)
    estimate = max(lo, min(hi, int(estimate)))

    return estimate, source, cands.get("_header_box"), cands.get("_input_box")


def _find_nav_right(gray, width: int, height: int, chat_left: int) -> int:
    """Width of the fixed left navigation rail.

    The rail is separated from the chat list by a hairline that runs the full
    window height, so the discriminator is vertical uniformity over almost the
    whole height -- which no avatar or message row can fake.  Purely
    informational: nothing downstream depends on it.
    """
    np = _np()
    hi = max(8, min(int(width * 0.16), chat_left - 8))
    lo = max(2, int(width * 0.02))
    if hi - lo < 6:
        return hi if hi > 0 else 0

    band = gray[int(height * 0.06):int(height * 0.94), :hi].astype("float32")
    if band.shape[0] < 40:
        return 0
    col_std = band.std(axis=0)
    col_mean = band.mean(axis=0)

    best_x, best_score = 0, -1.0
    for x in range(lo, hi):
        right = col_mean[min(hi - 1, x + 24)]
        # A divider is flat, and darker than the list background beside it.
        score = (right - col_mean[x]) - col_std[x] * 1.5
        if score > best_score:
            best_score, best_x = score, x
    return int(best_x + 1) if best_score > 0 else 0


def _find_toolbar(gray, width: int, height: int, chat_left: int) -> tuple:
    """Find the input-toolbar glyph row and return (y, icons).

    The toolbar is the only place in the window holding a horizontal run of
    similarly sized monochrome glyphs near the bottom edge.  Working from an
    ink-column profile rather than raw components merges multi-stroke icons such
    as the chat-history glyph into a single run, and the run-clustering step
    naturally separates the toolbar from the isolated navigation hamburger.
    """
    np = _np()
    band_top = max(0, height - int(max(110, height * 0.085)))
    band_bottom = min(height, height - int(max(6, height * 0.008)))
    if band_bottom - band_top < 24:
        return 0, []

    ink = gray[band_top:band_bottom, :] < 205
    if not ink.any():
        return 0, []

    # A window border, taskbar edge or panel rule can run along the bottom of
    # the band.  Left in, it lands inside every glyph's bounding box and
    # inflates its height past the filter below, so clear rows that are dark
    # across a large share of the window before measuring anything.
    wide_rows = ink.sum(axis=1) > (width * 0.5)
    if wide_rows.any():
        ink = ink.copy()
        ink[wide_rows, :] = False
    if not ink.any():
        return 0, []

    # Only the chat pane can hold the input toolbar.  Scanning from x=0 let
    # dark elements in the session list count as icons, so at wider window
    # sizes the run cluster started on something in the chat list and the
    # "first icon" came out left of the pane divider -- an emoji button 39px
    # inside the session list, which is where the click would then land.
    col_ink = ink.sum(axis=0)
    scan_from = max(0, int(chat_left))
    active_cols = [x for x in range(scan_from, width) if col_ink[x] >= 3]
    if not active_cols:
        return 0, []

    gaps = np.diff(active_cols)
    typical = float(np.median(gaps)) if len(gaps) else 1.0
    break_at = max(14.0, typical * 2.2)

    runs = []
    start = prev = active_cols[0]
    for x in active_cols[1:]:
        if x - prev > break_at:
            runs.append((start, prev))
            start = x
        prev = x
    runs.append((start, prev))

    glyphs = []
    for x0, x1 in runs:
        if not (6 <= x1 - x0 <= 46):
            continue
        rows = np.where(ink[:, x0:x1 + 1].sum(axis=1) >= 2)[0]
        if len(rows) < 6:
            continue
        y_top, y_bot = int(rows[0]), int(rows[-1])
        if not (10 <= y_bot - y_top <= 40):
            continue
        glyphs.append(Icon(x0, band_top + y_top, x1 - x0 + 1, y_bot - y_top + 1,
                           int(ink[y_top:y_bot + 1, x0:x1 + 1].sum())))

    if not glyphs:
        return 0, []

    clusters = [[glyphs[0]]]
    for icon in glyphs[1:]:
        last = clusters[-1][-1]
        if abs(icon.cy - last.cy) <= 9 and (icon.x - last.x) <= 130:
            clusters[-1].append(icon)
        else:
            clusters.append([icon])

    def score(cluster):
        if len(cluster) < 2:
            return -1.0
        centres = [i.cx for i in cluster]
        deltas = np.diff(centres)
        spread = float(np.std(deltas)) if len(deltas) else 0.0
        in_pane = 1.0 if cluster[0].x >= chat_left - 10 else 0.0
        return len(cluster) * 2.0 + in_pane * 4.0 - spread / 12.0

    clusters.sort(key=score, reverse=True)
    best = clusters[0]
    if score(best) < 0:
        return 0, []

    y = int(round(float(np.median([i.cy for i in best]))))
    return y, sorted(best, key=lambda i: i.x)


def detect_layout(img) -> Layout:
    """Measure the window geometry from a screenshot of the whole window."""
    np = _np()
    arr = _as_rgb(img)
    height, width = int(arr.shape[0]), int(arr.shape[1])
    gray = _to_gray(arr)

    layout = Layout(width=width, height=height)

    chat_left, source, header_box, input_box = _find_chat_left(gray, width, height)
    layout.chat_left = chat_left
    layout.header = header_box
    layout.input_box = input_box
    layout.notes["chat_left_source"] = source
    layout.nav_right = _find_nav_right(gray, width, height, chat_left)

    toolbar_y, icons = _find_toolbar(gray, width, height, chat_left)
    layout.toolbar_y = toolbar_y
    layout.icons = icons
    if icons:
        layout.emoji = icons[0]
        layout.notes["emoji_offset_from_pane"] = int(round(icons[0].cx)) - chat_left
        if len(icons) >= 2:
            layout.notes["toolbar_pitch"] = round(
                float(np.median(np.diff([i.cx for i in icons]))), 1)
        if layout.toolbar_y:
            layout.notes["toolbar_from_bottom"] = height - layout.toolbar_y

    log.debug("layout_detected", chat_left=layout.chat_left, toolbar_y=layout.toolbar_y,
              emoji=layout.emoji.as_dict() if layout.emoji else None,
              icons=len(icons), source=source, size=(width, height))
    return layout


# --------------------------------------------------------------------------------------
# Sticker panel
# --------------------------------------------------------------------------------------

@dataclass
class Panel:
    """The opened sticker popup, in window-local pixels."""

    x0: int
    y0: int
    x1: int
    y1: int
    tabs: list = field(default_factory=list)
    tab_y: int = 0
    active_tab: int = -1
    content: tuple = (0, 0, 0, 0)
    confidence: float = 0.0
    source: str = "structure"

    @property
    def width(self) -> int:
        return self.x1 - self.x0

    @property
    def height(self) -> int:
        return self.y1 - self.y0

    def as_dict(self) -> dict:
        return {"rect": [self.x0, self.y0, self.x1, self.y1],
                "size": [self.width, self.height],
                "tabs": [t.as_dict() for t in self.tabs], "tab_y": self.tab_y,
                "active_tab": self.active_tab, "content": list(self.content),
                "confidence": round(self.confidence, 3), "source": self.source}


def detect_panel_by_diff(before, after, layout: Layout) -> Optional[Panel]:
    """Locate the popup by differencing the window before and after the click.

    This is the most direct possible answer to "where did the panel appear":
    the popup is, by definition, the large contiguous region that changed.
    """
    np = _np()
    cv2 = _cv2()
    a = _to_gray(_as_rgb(before))
    b = _to_gray(_as_rgb(after))
    if a.shape != b.shape:
        return None

    changed = (cv2.absdiff(a, b) > 18).astype("uint8")
    if changed.sum() < 2000:
        return None

    kernel = np.ones((5, 5), "uint8")
    changed = cv2.morphologyEx(changed, cv2.MORPH_CLOSE, kernel, iterations=2)
    changed = cv2.morphologyEx(changed, cv2.MORPH_OPEN, kernel, iterations=1)

    count, _labels, stats, _centroids = cv2.connectedComponentsWithStats(changed, 8)
    best = None
    for i in range(1, count):
        x, y, w, h, area = (int(v) for v in stats[i])
        if area < 0.04 * layout.width * layout.height:
            continue
        if w < layout.width * 0.15 or h < layout.height * 0.10:
            continue
        if best is None or area > best[4]:
            best = (x, y, w, h, area)

    if best is None:
        return None

    x, y, w, h, area = best
    # If the window itself shifted or animated between the two grabs, every
    # edge lights up and the largest "changed" blob is the whole window.  That
    # is not a popup, so refuse it rather than deriving garbage geometry.
    if area > 0.75 * layout.width * layout.height:
        log.warning("panel_diff_too_large", area=area, window=(layout.width, layout.height))
        return None
    if y + h > (layout.toolbar_y or layout.height) + 8:
        log.debug("panel_diff_below_toolbar", rect=(x, y, w, h))
        return None

    return _refine_panel(after, layout, [x, y, x + w, y + h], source="diff")


def _refine_panel(img, layout: Layout, rect, *, source: str) -> Optional[Panel]:
    """Snap a rough rectangle onto the real popup edges and read its tab bar."""
    np = _np()
    gray = _to_gray(_as_rgb(img))
    height, width = gray.shape

    x0 = max(0, int(rect[0])); y0 = max(0, int(rect[1]))
    x1 = min(width, int(rect[2])); y1 = min(height, int(rect[3]))
    if x1 - x0 < 60 or y1 - y0 < 60:
        return None

    content_bottom = (layout.toolbar_y - 6) if layout.toolbar_y else height

    # Grow horizontally while the row band stays uniformly light.
    mid_y0, mid_y1 = y0 + (y1 - y0) // 4, y1 - (y1 - y0) // 4
    prefix = gray[mid_y0:mid_y1, :]

    def light_frac(xa, xb):
        if xb <= xa:
            return 0.0
        return float((prefix[:, xa:xb] >= 240).mean())

    guard = 0
    while x0 - 4 >= 0 and guard < 200 and light_frac(x0 - 4, x0) >= 0.55:
        x0 -= 4
        guard += 1
    guard = 0
    while x1 + 4 <= width and guard < 200 and light_frac(x1, x1 + 4) >= 0.55:
        x1 += 4
        guard += 1

    # Grow upward the same way.
    guard = 0
    while y0 - 3 >= 0 and guard < 200:
        row = gray[y0 - 3:y0, x0:x1]
        if row.size and float((row >= 240).mean()) >= 0.55:
            y0 -= 3
            guard += 1
        else:
            break

    # Grow downward.  The popup's tab bar is separated from the sticker grid by
    # a solid 3-4px hairline, so a plain "is this row light" test stops one band
    # too early; instead, step over a short dark run when light rows resume.
    guard = 0
    while y1 + 3 <= content_bottom and guard < 400:
        row = gray[y1:y1 + 3, x0:x1]
        frac = float((row >= 240).mean()) if row.size else 0.0
        if frac >= 0.80:
            y1 += 3
            guard += 1
            continue

        resume = None
        for step in range(3, 16, 3):
            probe = gray[y1 + step:y1 + step + 3, x0:x1]
            if probe.size and float((probe >= 240).mean()) >= 0.80:
                resume = y1 + step
                break
        if resume is None:
            break
        y1 = resume
        guard += 1

    panel = Panel(x0=x0, y0=y0, x1=x1, y1=y1, source=source)
    _read_tab_bar(img, panel, layout)
    if panel.tab_y:
        panel.content = (x0, y0, x1, max(y0 + 10, panel.tab_y - 16))
    else:
        panel.content = (x0, y0, x1, y1 - int((y1 - y0) * 0.12))
    return panel


def _read_tab_bar(img, panel: Panel, layout: Layout) -> None:
    """Find the little tab glyphs along the bottom of a sticker popup."""
    np = _np()
    cv2 = _cv2()
    arr = _as_rgb(img)
    gray = _to_gray(arr)
    hsv = cv2.cvtColor(arr, cv2.COLOR_RGB2HSV)

    # The tab strip is the quiet band along the bottom of the popup: the content
    # rows above it carry far more ink.  Locating it by ink density adapts to
    # whichever tab is showing, whereas a fixed-height band can swallow a row of
    # content and merge its columns into the tab glyphs.
    region_top = max(panel.y0, int(panel.y1 - panel.height * 0.30))
    probe = ((gray[region_top:panel.y1, panel.x0:panel.x1] < 215)
             | (hsv[region_top:panel.y1, panel.x0:panel.x1, 1] > 45))
    if probe.shape[0] < 14:
        return

    row_ink = probe.sum(axis=1)
    peak = float(row_ink.max())
    if peak <= 0:
        return
    quiet = max(6.0, peak * 0.25)

    # Walk up from just above the popup's own bottom border while rows stay quiet.
    index = probe.shape[0] - 5
    while index > 0 and row_ink[index] <= quiet:
        index -= 1
    band_top = region_top + index + 1
    if band_top >= panel.y1 - 14:
        band_top = panel.y1 - max(24, int(panel.height * 0.09))
    band_top = max(panel.y0, min(band_top, panel.y1 - 14))

    sub = gray[band_top:panel.y1, panel.x0:panel.x1]
    sat = hsv[band_top:panel.y1, panel.x0:panel.x1, 1]
    if sub.size == 0 or sub.shape[0] < 8:
        return

    # Tab glyphs are thin dark outlines; a column-run profile (rather than raw
    # connected components) keeps a multi-stroke glyph such as the smiley face
    # in one piece and does not depend on any single stroke being dark enough.
    ink = (sub < 215) | (sat > 45)

    # The popup's internal separators are full-width dark rules.  Left in, they
    # land inside every glyph's bounding box and wreck its height, so they are
    # cleared before any column or row measurement happens.
    wide_rows = ink.sum(axis=1) > (ink.shape[1] * 0.55)
    if wide_rows.any():
        ink = ink.copy()
        ink[wide_rows, :] = False

    active_cols = [x for x in range(ink.shape[1]) if int(ink[:, x].sum()) >= 2]
    if len(active_cols) < 4:
        return

    runs = []
    start = prev = active_cols[0]
    for x in active_cols[1:]:
        if x - prev > 3:
            runs.append((start, prev))
            start = x
        prev = x
    runs.append((start, prev))

    glyphs = []
    for x0, x1 in runs:
        if not (7 <= x1 - x0 <= 34):
            continue
        cols = ink[:, x0:x1 + 1]
        rows = np.where(cols.sum(axis=1) >= 1)[0]
        if len(rows) < 6 or (int(rows[-1]) - int(rows[0])) > 34:
            continue
        y_top, y_bot = int(rows[0]), int(rows[-1])
        glyphs.append((x0, x1, y_top, y_bot, int(cols.sum())))

    if not glyphs:
        return

    # Merge runs that belong to the same glyph: they sit much closer together
    # than the pitch between tabs.
    centres = [(a + b) / 2.0 for a, b, _c, _d, _e in glyphs]
    if len(centres) >= 2:
        diffs = [centres[i + 1] - centres[i] for i in range(len(centres) - 1)]
        pitch = float(np.median(diffs))
    else:
        pitch = 0.0
    merge_at = max(18.0, pitch * 0.55)

    merged = []
    for glyph in glyphs:
        if merged and (glyph[0] + glyph[1]) / 2.0 - (merged[-1][0] + merged[-1][1]) / 2.0 <= merge_at:
            a, b, c, d, area = merged[-1]
            merged[-1] = (min(a, glyph[0]), max(b, glyph[1]),
                          min(c, glyph[2]), max(d, glyph[3]), area + glyph[4])
        else:
            merged.append(tuple(glyph))

    tabs = []
    for x0, x1, y_top, y_bot, area in merged:
        glyph = Icon(panel.x0 + x0, band_top + y_top, x1 - x0 + 1, y_bot - y_top + 1, area)
        glyph.saturation = float(sat[y_top:y_bot + 1, x0:x1 + 1].mean())
        tabs.append(glyph)

    if len(tabs) < 2 or len(tabs) > 6:
        _synthesise_tab_bar(panel)
        return

    # The tab strip is left aligned a little way in from the popup's edge.
    # Anything starting further right is some other row of glyphs that happened
    # to fall inside an oversized rectangle.
    first_offset = tabs[0].cx - panel.x0
    if not (-4 <= first_offset <= panel.width * 0.40):
        log.debug("tab_bar_rejected", first_offset=round(first_offset, 1),
                  panel_width=panel.width, tabs=len(tabs))
        return

    panel.tabs = tabs
    panel.tab_y = int(round(float(np.median([t.cy for t in tabs]))))

    # The selected tab is marked by a faint grey pill behind it.  Sampling the
    # non-glyph pixels around each glyph picks that pill up (roughly 240 versus
    # 250 for the popup background), which is a far steadier signal than colour.
    levels = []
    for tab in tabs:
        pad_x, pad_y = 12, 8
        bx0 = max(0, tab.x - panel.x0 - pad_x)
        bx1 = min(sub.shape[1], tab.x - panel.x0 + tab.w + pad_x)
        by0 = max(0, tab.y - band_top - pad_y)
        by1 = min(sub.shape[0], tab.y - band_top + tab.h + pad_y)
        region = sub[by0:by1, bx0:bx1]
        background = region[region >= 200]
        levels.append(float(np.median(background)) if background.size else 255.0)

    lowest = int(np.argmin(levels))
    if levels[lowest] <= (max(levels) - 3.0):
        panel.active_tab = lowest
    else:
        panel.active_tab = -1


# The popup's tab strip is fixed chrome: three glyphs at a constant fraction of
# the popup's own size.  Measured on real captures at a 697x697 popup the glyph
# centres sit at +7.5%, +18.2% and +28.7% of the width, a hair above the bottom
# edge.  Deriving them from the popup rectangle keeps the fallback working when
# a particular tab's icons are too faint to segment.
_TAB_X_RATIOS = (0.0746, 0.1815, 0.2870)
_TAB_Y_RATIO = 0.0502


def _synthesise_tab_bar(panel: Panel) -> None:
    """Place tab slots from the popup geometry when glyphs cannot be measured."""
    if panel.width < 60 or panel.height < 60:
        return
    y = int(round(panel.y1 - panel.height * _TAB_Y_RATIO))
    tabs = []
    for ratio in _TAB_X_RATIOS:
        x = int(round(panel.x0 + panel.width * ratio))
        if not (panel.x0 <= x <= panel.x1):
            return
        tabs.append(Icon(x=x - 9, y=y - 9, w=18, h=18, area=0))
    panel.tabs = tabs
    panel.tab_y = y
    panel.active_tab = -1
    log.debug("tab_bar_synthesised", tab_y=y, count=len(tabs))


def detect_panel(img, layout: Layout) -> Optional[Panel]:
    """Find an opened sticker popup purely from structure, without a 'before' frame."""
    height, width = layout.height, layout.width
    gray = _to_gray(_as_rgb(img))
    content_bottom = (layout.toolbar_y - 6) if layout.toolbar_y else height

    blocks = _white_blocks(gray, int(height * 0.08), content_bottom,
                           min_h=max(100, int(height * 0.18)),
                           min_w=max(120, int(width * 0.22)))
    if not blocks:
        return None

    best = None
    best_score = -1.0
    for x, y, w, h, area in blocks:
        # The popup is centred on the emoji button, which sits a little to the
        # right of the pane edge, so the popup always reaches across into the
        # chat list.  That crossing is the signature nothing else has.
        crosses = 1.0 if x < layout.chat_left - 8 else 0.0
        if not crosses:
            continue
        near_bottom = 1.0 if abs((y + h) - content_bottom) < height * 0.22 else 0.0
        score = crosses * 4.0 + near_bottom * 2.0 + min(2.0, area / (width * height * 0.6))
        if score > best_score:
            best_score = score
            best = (x, y, w, h, area)

    if best is None:
        return None

    x, y, w, h, _area = best
    panel = _refine_panel(img, layout, [x, y, x + w, y + h], source="structure")
    if panel is not None:
        panel.confidence = min(1.0, best_score / 8.0)
    return panel

# --------------------------------------------------------------------------------------
# Sticker grid and template matching
# --------------------------------------------------------------------------------------

@dataclass
class StickerCell:
    """One slot of the sticker lattice, in window-local pixels."""

    cx: int
    cy: int
    x0: int
    y0: int
    x1: int
    y1: int
    row: int = 0
    col: int = 0
    ink: tuple = (0, 0, 0, 0)

    @property
    def size(self) -> tuple:
        return (self.x1 - self.x0, self.y1 - self.y0)

    def as_dict(self) -> dict:
        return {"center": [self.cx, self.cy], "box": [self.x0, self.y0, self.x1, self.y1],
                "row": self.row, "col": self.col, "ink": list(self.ink)}


@dataclass
class StickerMatch:
    score: float
    template: str
    point: tuple
    cell: Optional[StickerCell] = None
    size: tuple = (0, 0)
    detail: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {"score": round(self.score, 4), "template": self.template,
                "point": list(self.point), "size": list(self.size),
                "cell": self.cell.as_dict() if self.cell else None, "detail": self.detail}


def _ink_mask(arr):
    """Non-background pixels: anything that is not flat, desaturated white."""
    cv2 = _cv2()
    gray = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)
    sat = cv2.cvtColor(arr, cv2.COLOR_RGB2HSV)[:, :, 1]
    background = (gray >= 236) & (sat <= 26)
    return (~background).astype("uint8")


def _fit_lattice(values, span: float) -> tuple:
    """Cluster 1-D centres and estimate the pitch between them."""
    np = _np()
    values = sorted(float(v) for v in values)
    if not values:
        return [], float(span)
    clusters = [[values[0]]]
    tolerance = max(10.0, span * 0.055)
    for v in values[1:]:
        if v - clusters[-1][-1] <= tolerance:
            clusters[-1].append(v)
        else:
            clusters.append([v])
    centers = [sum(c) / len(c) for c in clusters]
    if len(centers) >= 2:
        diffs = [centers[i + 1] - centers[i] for i in range(len(centers) - 1)]
        usable = [d for d in diffs if d > 8]
        pitch = float(np.median(usable)) if usable else float(span)
    else:
        pitch = float(span)
    return centers, pitch


def detect_sticker_cells(arr, panel: Panel) -> tuple:
    """Read the sticker lattice out of an opened popup.

    Thumbnails are the only non-background blobs in the popup and WeChat lays
    them on a regular lattice, so the blob centres give both the cell positions
    and the pitch -- which in turn gives the scale a source template should be
    rendered at when matching.
    """
    np = _np()
    cv2 = _cv2()

    x0, y0, x1, y1 = (int(v) for v in panel.content)
    x0 = max(0, x0); y0 = max(0, y0)
    x1 = min(int(arr.shape[1]), x1); y1 = min(int(arr.shape[0]), y1)
    cw, ch = x1 - x0, y1 - y0
    if cw < 90 or ch < 90:
        return [], {"reason": "content_too_small", "content": [x0, y0, x1, y1]}

    sub = arr[y0:y1, x0:x1]
    ink = _ink_mask(sub)
    kernel = np.ones((3, 3), "uint8")
    ink = cv2.morphologyEx(ink, cv2.MORPH_CLOSE, kernel, iterations=2)
    ink = cv2.morphologyEx(ink, cv2.MORPH_OPEN, kernel, iterations=1)

    count, _labels, stats, _centroids = cv2.connectedComponentsWithStats(ink, 8)
    min_area = max(500.0, cw * ch * 0.0025)
    blobs = []
    for i in range(1, count):
        bx, by, bw, bh, area = (int(v) for v in stats[i])
        if area < min_area:
            continue
        if bw > cw * 0.6 or bh > ch * 0.8:
            continue
        if bw < 24 or bh < 24:
            continue
        blobs.append((bx, by, bw, bh, area))

    if not blobs:
        return [], {"reason": "no_blobs", "content": [x0, y0, x1, y1]}

    col_centers, pitch_x = _fit_lattice([b[0] + b[2] / 2.0 for b in blobs], cw)
    row_centers, pitch_y = _fit_lattice([b[1] + b[3] / 2.0 for b in blobs], ch)

    if len(col_centers) < 2:
        pitch_x = max(pitch_x, max([b[2] for b in blobs] or [cw]) * 1.22)
    if len(row_centers) < 2:
        pitch_y = max(pitch_y, max([b[3] for b in blobs] or [ch]) * 1.22)
    pitch_x = float(max(40.0, min(pitch_x, cw)))
    pitch_y = float(max(40.0, min(pitch_y, ch)))

    def snap(value, centers):
        if not centers:
            return value, 0
        best = min(range(len(centers)), key=lambda i: abs(centers[i] - value))
        return centers[best], best

    cells = {}
    for bx, by, bw, bh, _area in blobs:
        sx, col = snap(bx + bw / 2.0, col_centers)
        sy, row = snap(by + bh / 2.0, row_centers)

        ccx, ccy = int(round(sx)), int(round(sy))
        left = max(x0, x0 + int(round(sx - pitch_x / 2.0)))
        top = max(y0, y0 + int(round(sy - pitch_y / 2.0)))
        right = min(x1, x0 + int(round(sx + pitch_x / 2.0)))
        bottom = min(y1, y0 + int(round(sy + pitch_y / 2.0)))
        if right - left < 20 or bottom - top < 20:
            continue

        candidate = StickerCell(cx=x0 + ccx, cy=y0 + ccy, x0=left, y0=top,
                                x1=right, y1=bottom, row=row, col=col,
                                ink=(x0 + bx, y0 + by, x0 + bx + bw, y0 + by + bh))
        key = (row, col)
        existing = cells.get(key)
        if existing is None or bw * bh > (existing.ink[2] - existing.ink[0]) * (existing.ink[3] - existing.ink[1]):
            cells[key] = candidate

    ordered = [cells[k] for k in sorted(cells.keys())]
    meta = {
        "content": [x0, y0, x1, y1],
        "cols": len(col_centers), "rows": len(row_centers),
        "pitch_x": round(pitch_x, 1), "pitch_y": round(pitch_y, 1),
        "col_centers": [round(c + x0, 1) for c in col_centers],
        "row_centers": [round(r + y0, 1) for r in row_centers],
        "blobs": len(blobs), "cells": len(ordered),
    }
    log.debug("sticker_grid_detected", **meta)
    return ordered, meta


def load_template_rgb(path) -> Optional[object]:
    """Load a sticker source image, compositing transparency onto white.

    OpenCV drops the alpha channel, which turns a transparent PNG into a black
    rectangle and destroys the match.  Compositing here keeps the template the
    same colour as the thumbnail WeChat paints on its white popup.
    """
    np = _np()
    try:
        from PIL import Image

        with Image.open(str(path)) as im:
            if im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info):
                rgba = im.convert("RGBA")
                canvas = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
                return np.asarray(Image.alpha_composite(canvas, rgba).convert("RGB"))
            return np.asarray(im.convert("RGB"))
    except Exception as exc:
        log.warning("template_load_failed", path=str(path), error=str(exc))
        return None


def _edge_map(gray):
    cv2 = _cv2()
    return cv2.Canny(gray, 40, 110)


def match_template_multiscale(hay_rgb, template_rgb, target_sizes, *, use_edges: bool = False):
    """Slide a resized template across a region and return the best hits per size.

    The score is normalised cross-correlation on luminance.  Measurements on
    real captures showed this to be the single most discriminative signal
    available: it separated the seven stickers that do have a source template
    from a sticker that does not, whereas edge-only and masked-correlation
    variants either collapsed the margin or picked the wrong cell outright.
    Edge correlation is still computed, but only as corroboration.
    """
    cv2 = _cv2()
    hay_gray = cv2.cvtColor(hay_rgb, cv2.COLOR_RGB2GRAY)
    tpl_gray = cv2.cvtColor(template_rgb, cv2.COLOR_RGB2GRAY)
    tpl_h, tpl_w = tpl_gray.shape[:2]
    hay_h, hay_w = hay_gray.shape[:2]

    # Normalised cross-correlation is undefined for a template with no
    # variance, and OpenCV resolves that 0/0 as a perfect score -- so a blank
    # or near-flat image would "match" every cell at 1.0.  Refuse it outright.
    if float(tpl_gray.std()) < 8.0:
        log.warning("template_has_no_structure", std=round(float(tpl_gray.std()), 2))
        return []

    hay_edge = _edge_map(hay_gray) if use_edges else None

    hits = []
    for target in sorted(set(int(round(s)) for s in target_sizes if s and s >= 16)):
        scale = target / float(max(tpl_h, tpl_w))
        new_w = max(8, int(round(tpl_w * scale)))
        new_h = max(8, int(round(tpl_h * scale)))
        if new_w >= hay_w or new_h >= hay_h:
            continue

        small = cv2.resize(tpl_gray, (new_w, new_h), interpolation=cv2.INTER_AREA)
        try:
            res = cv2.matchTemplate(hay_gray, small, cv2.TM_CCOEFF_NORMED)
        except Exception:
            warn("core/execution/wx_vision.py:1646 Exception", err=repr(__import__("sys").exc_info()[1]))
            continue
        _mn, mx, _ml, loc = cv2.minMaxLoc(res)
        detail = {"gray": round(float(mx), 4)}

        if use_edges:
            try:
                small_edge = _edge_map(small)
                res_e = cv2.matchTemplate(hay_edge, small_edge, cv2.TM_CCOEFF_NORMED)
                detail["edge"] = round(float(cv2.minMaxLoc(res_e)[1]), 4)
            except Exception:
                warn("core/execution/wx_vision.py:1656 Exception", err=repr(__import__("sys").exc_info()[1]))

        hits.append({"score": float(mx), "response": res, "loc": (int(loc[0]), int(loc[1])),
                     "size": (new_w, new_h), "target": target, "detail": detail})

    hits.sort(key=lambda h: -h["score"])
    return hits


def find_sticker_in_panel(arr, panel: Panel, template_paths, *,
                          cells: Optional[list] = None,
                          grid_meta: Optional[dict] = None,
                          min_score: float = 0.0) -> Optional[StickerMatch]:
    """Locate the best-matching sticker inside an opened popup.

    The template is a full-resolution source image while the popup paints a
    downscaled thumbnail, so the search scale is driven by the lattice pitch
    measured from the popup itself -- that gives both the render size and the
    set of legitimate landing spots.
    """
    np = _np()
    cv2 = _cv2()

    if isinstance(template_paths, (str, Path)):
        template_paths = [template_paths]

    x0, y0, x1, y1 = (int(v) for v in panel.content)
    x0 = max(0, x0); y0 = max(0, y0)
    x1 = min(int(arr.shape[1]), x1); y1 = min(int(arr.shape[0]), y1)
    if x1 - x0 < 60 or y1 - y0 < 60:
        return None
    hay = arr[y0:y1, x0:x1]

    pitch = 0.0
    if grid_meta:
        pitch = max(float(grid_meta.get("pitch_x") or 0), float(grid_meta.get("pitch_y") or 0))
    if pitch <= 0 and cells:
        sizes = [max(c.x1 - c.x0, c.y1 - c.y0) for c in cells]
        if sizes:
            pitch = float(np.median(sizes))

    targets = []
    if pitch > 0:
        targets += [pitch * f for f in (0.98, 0.92, 0.86, 0.80, 1.06)]
    if cells:
        for c in cells:
            m = max(c.x1 - c.x0, c.y1 - c.y0)
            targets += [m * 0.92, m * 0.80]
    if not targets:
        targets = [max(60.0, min(x1 - x0, y1 - y0) * 0.22)]

    hay_w = x1 - x0
    hay_h = y1 - y0

    # template path -> {cell_key -> best evidence for that pair}
    #
    # Keeping the templates apart is what makes the margin mean anything: it
    # asks whether *this* picture has one unambiguous landing spot.  Merging
    # them into a single ranking answers a different question, because a second
    # source image of the same mood legitimately lands on a different cell --
    # and that extra cell then became the runner-up and collapsed the margin.
    # Measured on the real registry: resolving a mood's second template (its
    # stored path had been considered missing, see Sticker.images) turned a
    # matching 调皮 send into "未找到足够匹配的表情包", score 0.55 and margin
    # under 0.08, with the wrong cell scoring just below the right one.
    per_template = {}

    def record(cell, score, point, size, path, detail):
        by_cell = per_template.setdefault(path, {})
        key = (cell.row, cell.col)
        prev = by_cell.get(key)
        if prev is None or score > prev["score"]:
            by_cell[key] = {"score": score, "point": point, "size": size,
                            "path": path, "detail": detail, "cell": cell}

    any_hit = False
    for path in template_paths:
        tpl = load_template_rgb(path)
        if tpl is None:
            continue
        hits = match_template_multiscale(hay, tpl, targets)
        if not hits:
            continue
        any_hit = True
        top = hits[0]
        detail = {"target": top["target"], **top["detail"]}

        if not cells:
            point = (x0 + top["loc"][0] + top["size"][0] // 2,
                     y0 + top["loc"][1] + top["size"][1] // 2)
            best = StickerMatch(score=top["score"], template=str(path), point=point,
                                size=top["size"], detail=detail)
            return best if best.score >= min_score else None

        # Attribute each cell the strongest response landing inside it.
        tw, th = top["size"]
        response = top["response"]
        for cell in cells:
            lo_x = max(0, int(cell.x0 - x0 - tw / 2))
            hi_x = min(response.shape[1] - 1, int(cell.x1 - x0 - tw / 2))
            lo_y = max(0, int(cell.y0 - y0 - th / 2))
            hi_y = min(response.shape[0] - 1, int(cell.y1 - y0 - th / 2))
            if hi_x < lo_x or hi_y < lo_y:
                continue
            window = response[lo_y:hi_y + 1, lo_x:hi_x + 1]
            _mn, mx, _ml, loc = cv2.minMaxLoc(window)
            point = (x0 + lo_x + int(loc[0]) + tw // 2, y0 + lo_y + int(loc[1]) + th // 2)
            record(cell, float(mx), point, (tw, th), str(path), detail)

    if not any_hit or not per_template:
        return None

    # Each template is ranked on its own; the winner is the template whose best
    # cell scored highest, and it brings its own margin along.
    best = None
    scored = 0
    for by_cell in per_template.values():
        ranked = sorted(by_cell.values(), key=lambda d: -d["score"])
        scored += len(ranked)
        top_of_template = ranked[0]
        runner_of_template = ranked[1]["score"] if len(ranked) > 1 else 0.0
        if best is None or top_of_template["score"] > best[0]["score"]:
            best = (top_of_template, runner_of_template)
    top, runner = best
    margin = top["score"] - runner

    match = StickerMatch(score=top["score"], template=top["path"], point=top["point"],
                         cell=top["cell"], size=top["size"],
                         detail={**top["detail"], "margin": round(margin, 4),
                                 "runner_up_cell": round(runner, 4),
                                 "cells_scored": scored,
                                 "templates_scored": len(per_template)})

    if match.score < min_score:
        log.debug("sticker_match_below_floor", score=round(match.score, 4),
                  floor=min_score, template=match.template)
        return None
    return match


def find_emoji_button(image):
    """Window-local centre of the input toolbar's emoji button, or None."""
    layout = detect_layout(image)
    return layout.emoji_point()


def find_custom_tab(image):
    """Window-local centre of the custom-sticker tab, or None.

    The popup's tab strip reads [search, emoji, custom] from left to right, so
    the custom collection is the rightmost slot.  When the strip cannot be
    segmented, fall back to the label WeChat draws on it.
    """
    layout = detect_layout(image)
    panel = detect_panel(image, layout)
    if panel is not None and panel.tabs:
        return (panel.tabs[-1].cx_i, panel.tabs[-1].cy_i)

    for text, cx, cy, *_rest in ocr_items(image):
        if "自定义" in text or "收藏" in text:
            return (cx, cy)
    return None


def analyze_image_file(path) -> dict:
    """Run the whole perception stack over a saved screenshot (diagnostics)."""
    from PIL import Image

    with Image.open(str(path)) as im:
        img = im.convert("RGB")
    layout = detect_layout(img)
    panel = detect_panel(img, layout)
    result = {"file": str(path), "size": [img.size[0], img.size[1]],
              "layout": layout.as_dict(), "panel": None, "grid": None, "cells": []}
    if panel is not None:
        cells, meta = detect_sticker_cells(_as_rgb(img), panel)
        result["panel"] = panel.as_dict()
        result["grid"] = meta
        result["cells"] = [c.as_dict() for c in cells]
    return result
