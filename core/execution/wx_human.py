"""Human-like mouse / keyboard simulation for WeChat automation.

Everything here goes out through the real Windows input queue (SendInput), which is
the exact path physical hardware uses.  Nothing is injected into the target process,
no window messages are posted, and no accessibility API is used -- so from WeChat's
point of view the input is indistinguishable from a person using the machine.

The "human" part exists to avoid automation-shaped input, which is what behavioural
heuristics actually look for:

* the cursor never teleports -- it travels a curved, speed-varying path
* clicks are never dead-centre and always carry a non-zero press duration
* keys carry scan codes and jittered hold times, like a real keyboard
* pacing is drawn from distributions rather than being a fixed sleep
"""

from __future__ import annotations

import ctypes
import math
import os
import random
import time
from ctypes import wintypes

import structlog
from safelog import warn

log = structlog.get_logger()

__all__ = [
    "cursor_pos", "move_to", "click", "double_click", "scroll",
    "key_press", "hotkey", "paste_text", "restore_clipboard",
    "sleep_human", "set_profile", "get_profile", "Profile", "SCREEN",
]

user32 = ctypes.WinDLL("user32", use_last_error=True)

INPUT_MOUSE = 0
INPUT_KEYBOARD = 1

MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_HWHEEL = 0x1000
MOUSEEVENTF_ABSOLUTE = 0x8000
MOUSEEVENTF_VIRTUALDESK = 0x4000

KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_SCANCODE = 0x0008

WHEEL_DELTA = 120
MAPVK_VK_TO_VSC = 0

VK_SHIFT = 0x10
VK_CONTROL = 0x11
VK_MENU = 0x12
VK_ESCAPE = 0x1B
VK_RETURN = 0x0D
VK_F = 0x46


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    ]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


user32.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
user32.SendInput.restype = wintypes.UINT
user32.MapVirtualKeyW.argtypes = (wintypes.UINT, wintypes.UINT)
user32.MapVirtualKeyW.restype = wintypes.UINT


def _send(*inputs: INPUT) -> int:
    if not inputs:
        return 0
    array = (INPUT * len(inputs))(*inputs)
    sent = int(user32.SendInput(len(inputs), array, ctypes.sizeof(INPUT)))
    if sent != len(inputs):
        log.warning("sendinput_incomplete", sent=sent, requested=len(inputs),
                    winerror=ctypes.get_last_error())
    return sent


def _mouse_input(flags: int, dx: int = 0, dy: int = 0, data: int = 0) -> INPUT:
    return INPUT(
        type=INPUT_MOUSE,
        u=_INPUTUNION(
            mi=MOUSEINPUT(dx=dx, dy=dy, mouseData=data & 0xFFFFFFFF,
                          dwFlags=flags, time=0, dwExtraInfo=None)
        ),
    )


def _key_input(vk: int, scan: int, flags: int) -> INPUT:
    return INPUT(
        type=INPUT_KEYBOARD,
        u=_INPUTUNION(ki=KEYBDINPUT(wVk=vk, wScan=scan, dwFlags=flags,
                                    time=0, dwExtraInfo=None)),
    )


class _Screen:
    """Cached virtual-desktop metrics used to normalise absolute mouse coordinates."""

    def __init__(self) -> None:
        self.refresh()

    def refresh(self) -> None:
        g = user32.GetSystemMetrics
        self.vx = int(g(76))
        self.vy = int(g(77))
        self.vw = int(g(78))
        self.vh = int(g(79))
        if self.vw <= 0 or self.vh <= 0:
            self.vx = self.vy = 0
            self.vw = int(g(0)) or 1920
            self.vh = int(g(1)) or 1080

    def to_absolute(self, x: int, y: int) -> tuple:
        nx = int(round((x - self.vx) * 65535.0 / max(1, self.vw - 1)))
        ny = int(round((y - self.vy) * 65535.0 / max(1, self.vh - 1)))
        return max(0, min(65535, nx)), max(0, min(65535, ny))


SCREEN = _Screen()


def cursor_pos() -> tuple:
    pt = wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    return int(pt.x), int(pt.y)


class Profile:
    """Tunable 'how human' knobs.  Defaults are deliberately unhurried."""

    def __init__(self) -> None:
        # How human the pacing is, as a divisor: 1.0 is the unhurried default,
        # higher is faster, and every delay in this module goes through it.
        # It reads the environment rather than the config because this module
        # is a leaf and must not import project code.  The env var is only an
        # override; set_profile(speed=...) is the normal way in.
        try:
            override = float(os.environ.get("WX_HUMAN_SPEED", "") or 0)
        except ValueError:
            warn("core/execution/wx_human.py:183 ValueError", err=repr(__import__("sys").exc_info()[1]))
            override = 0.0
        self.speed = override if override > 0 else 3.0
        self.jitter_px = 1.6
        self.curve_px = 60.0
        self.overshoot = 0.25
        self.pre_click_ms = (60, 190)
        self.click_hold_ms = (45, 130)
        self.key_hold_ms = (28, 85)
        self.think_ms = (0, 45)


_profile = Profile()


def set_profile(**kwargs) -> Profile:
    for key, value in kwargs.items():
        if not hasattr(_profile, key):
            raise AttributeError("unknown profile field: " + key)
        setattr(_profile, key, value)
    return _profile


def get_profile() -> Profile:
    return _profile


def _ms(low_high: tuple) -> float:
    low, high = low_high
    return random.uniform(low, high) / 1000.0 / max(0.05, _profile.speed)


def sleep_human(base: float, spread: float = 0.35) -> None:
    """Sleep for roughly base seconds, jittered proportionally."""
    delay = base * random.uniform(1.0 - spread, 1.0 + spread)
    time.sleep(max(0.0, delay) / max(0.05, _profile.speed))


def _think() -> None:
    time.sleep(_ms(_profile.think_ms))


def _bezier(p0, p1, p2, t: float) -> tuple:
    u = 1.0 - t
    return (u * u * p0[0] + 2 * u * t * p1[0] + t * t * p2[0],
            u * u * p0[1] + 2 * u * t * p1[1] + t * t * p2[1])


def _ease(t: float) -> float:
    """Smooth-step with a slight late acceleration, like a real arm."""
    t = min(1.0, max(0.0, t))
    return t * t * (3.0 - 2.0 * t) * 0.82 + 0.18 * (1.0 - (1.0 - t) ** 2)


def _emit_move(x: float, y: float) -> None:
    nx, ny = SCREEN.to_absolute(int(round(x)), int(round(y)))
    _send(_mouse_input(MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK, nx, ny))


def move_to(x: int, y: int, *, duration: float | None = None) -> None:
    """Glide the cursor to the target along a jittered bezier path."""
    SCREEN.refresh()
    sx, sy = cursor_pos()
    tx, ty = int(x), int(y)

    dx, dy = tx - sx, ty - sy
    dist = math.hypot(dx, dy)
    if dist < 1.5:
        _emit_move(tx, ty)
        return

    steps = int(max(8, min(48, dist / 16.0)))
    if duration is None:
        duration = random.uniform(0.16, 0.34) * (0.55 + min(1.6, dist / 900.0))
    # The cursor travel was the one delay that ignored _profile.speed, so
    # turning the pacing down still left every mouse move taking a third of a
    # second.
    duration = duration / max(0.05, _profile.speed)

    mx, my = (sx + tx) / 2.0, (sy + ty) / 2.0
    if dist > 1:
        px, py = -dy / dist, dx / dist
    else:
        px, py = 0.0, 0.0
    bend = random.gauss(0, 1) * min(_profile.curve_px, dist * 0.28)
    cx, cy = mx + px * bend, my + py * bend

    per_step = duration / steps
    for i in range(1, steps + 1):
        t = _ease(i / steps)
        fx, fy = _bezier((sx, sy), (cx, cy), (tx, ty), t)
        falloff = math.sin(math.pi * t)
        fx += random.gauss(0, _profile.jitter_px) * falloff
        fy += random.gauss(0, _profile.jitter_px) * falloff
        _emit_move(fx, fy)
        time.sleep(per_step * random.uniform(0.6, 1.5))

    if dist > 90 and random.random() < _profile.overshoot:
        ox = tx + random.gauss(0, 5)
        oy = ty + random.gauss(0, 5)
        _emit_move(ox, oy)
        time.sleep(random.uniform(0.02, 0.06))
        steps_back = random.randint(2, 4)
        for i in range(1, steps_back + 1):
            t = i / steps_back
            _emit_move(ox + (tx - ox) * t, oy + (ty - oy) * t)
            time.sleep(random.uniform(0.008, 0.022))
    else:
        _emit_move(tx, ty)


_BUTTON_DOWN = {"left": MOUSEEVENTF_LEFTDOWN, "right": MOUSEEVENTF_RIGHTDOWN, "middle": MOUSEEVENTF_MIDDLEDOWN}
_BUTTON_UP = {"left": MOUSEEVENTF_LEFTUP, "right": MOUSEEVENTF_RIGHTUP, "middle": MOUSEEVENTF_MIDDLEUP}


def click(x: int, y: int, *, button: str = "left", aim_jitter: float = 0.0,
          move: bool = True) -> tuple:
    """Move to the target and click.  Returns the coordinates actually clicked."""
    tx, ty = int(x), int(y)
    if aim_jitter:
        tx += int(round(random.gauss(0, aim_jitter)))
        ty += int(round(random.gauss(0, aim_jitter)))
    if move:
        move_to(tx, ty)
    time.sleep(_ms(_profile.pre_click_ms))
    _send(_mouse_input(_BUTTON_DOWN[button]))
    time.sleep(_ms(_profile.click_hold_ms))
    _send(_mouse_input(_BUTTON_UP[button]))
    _think()
    return tx, ty


def click_menu(x: int, y: int, *, hold_ms: int = 180) -> tuple:
    """右键打开 Qt 菜单：``SetCursorPos`` + ``mouse_event`` + **长按**。

    为什么不能复用 :func:`click`：微信 4.x 的右键菜单对 SendInput 的短按没反应
    （真机三次实测：右键前后窗口像素差 20/42/72，菜单根本没出来）；换成
    ``SetCursorPos`` + ``mouse_event`` 并把按住时间拉到 180 ms，菜单才稳定出现。
    这条只给「打开菜单」用，左键点击仍走 :func:`click`（它一直是可靠的）。
    """
    tx, ty = int(x), int(y)
    user32 = ctypes.windll.user32
    user32.SetCursorPos(tx, ty)
    time.sleep(0.12)
    # 必须用老的 mouse_event，不能用 SendInput：真机实测 SendInput 的右键
    # 微信 4.x 完全不认（菜单不出现），mouse_event + 180ms 长按才稳定。
    user32.mouse_event(MOUSEEVENTF_RIGHTDOWN, 0, 0, 0, 0)
    time.sleep(max(0.02, hold_ms / 1000.0))
    user32.mouse_event(MOUSEEVENTF_RIGHTUP, 0, 0, 0, 0)
    _think()
    return tx, ty


def double_click(x: int, y: int, *, button: str = "left") -> None:
    move_to(int(x), int(y))
    time.sleep(_ms(_profile.pre_click_ms))
    for _ in range(2):
        _send(_mouse_input(_BUTTON_DOWN[button]))
        time.sleep(random.uniform(0.02, 0.05))
        _send(_mouse_input(_BUTTON_UP[button]))
        time.sleep(random.uniform(0.04, 0.09))


def scroll(clicks: int, *, at: tuple | None = None, horizontal: bool = False) -> None:
    """Turn the wheel by N notches (positive = up / right)."""
    if at is not None:
        move_to(int(at[0]), int(at[1]))
        time.sleep(random.uniform(0.05, 0.14))

    remaining = abs(int(clicks))
    direction = 1 if clicks > 0 else -1
    flag = MOUSEEVENTF_HWHEEL if horizontal else MOUSEEVENTF_WHEEL

    while remaining > 0:
        burst = min(remaining, random.randint(1, 3))
        for _ in range(burst):
            _send(_mouse_input(flag, data=(WHEEL_DELTA * direction) & 0xFFFFFFFF))
            time.sleep(random.uniform(0.012, 0.045))
        remaining -= burst
        if remaining > 0:
            time.sleep(random.uniform(0.045, 0.16))


_EXTENDED_VKS = frozenset({
    0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28,
    0x2D, 0x2E, 0x5B, 0x5C, 0x5D, 0x6F, 0xA3, 0xA5,
})


def _scan_code(vk: int) -> int:
    return int(user32.MapVirtualKeyW(vk, MAPVK_VK_TO_VSC)) & 0xFF


def key_press(vk: int, *, hold_ms: tuple | None = None) -> None:
    """Press and release one key using hardware scan codes."""
    scan = _scan_code(vk)
    ext = KEYEVENTF_EXTENDEDKEY if vk in _EXTENDED_VKS else 0
    flags = (KEYEVENTF_SCANCODE if scan else 0) | ext
    hold = hold_ms or _profile.key_hold_ms
    _send(_key_input(vk, scan, flags))
    time.sleep(random.uniform(*hold) / 1000.0 / max(0.05, _profile.speed))
    _send(_key_input(vk, scan, flags | KEYEVENTF_KEYUP))
    time.sleep(random.uniform(0.012, 0.05))


KEYEVENTF_UNICODE = 0x0004


def type_unicode(text: str, *, per_char_ms: tuple = (18, 55)) -> None:
    """逐字符「真的敲」一遍（SendInput + KEYEVENTF_UNICODE）。

    为什么不用剪贴板：微信的「@ 成员名单」只对**键盘敲出来的 @** 弹出来，
    粘一行带 @ 的文本它是不会弹的——所以 @ 必须走这条路径。
    """
    for ch in str(text or ""):
        code = ord(ch)
        if code > 0xFFFF:  # 基本多文种平面之外，代理对不在本函数职责内
            continue
        _send(_key_input(0, code, KEYEVENTF_UNICODE))
        time.sleep(random.uniform(*per_char_ms) / 1000.0)
        _send(_key_input(0, code, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP))
        time.sleep(random.uniform(*per_char_ms) / 1000.0)


def hotkey(*vks: int) -> None:
    """Hold every key but the last, tap the last, then release in reverse."""
    if not vks:
        return
    held = []
    for vk in vks[:-1]:
        scan = _scan_code(vk)
        ext = KEYEVENTF_EXTENDEDKEY if vk in _EXTENDED_VKS else 0
        flags = (KEYEVENTF_SCANCODE if scan else 0) | ext
        _send(_key_input(vk, scan, flags))
        held.append((vk, scan, ext))
        time.sleep(random.uniform(0.018, 0.055))
    key_press(vks[-1])
    for vk, scan, ext in reversed(held):
        flags = (KEYEVENTF_SCANCODE if scan else 0) | ext | KEYEVENTF_KEYUP
        _send(_key_input(vk, scan, flags))
        time.sleep(random.uniform(0.014, 0.04))


def restore_clipboard(text: str) -> None:
    try:
        import pyperclip

        pyperclip.copy(text)
    except Exception:
        warn("core/execution/wx_human.py:431 Exception", err=repr(__import__("sys").exc_info()[1]))


def _clipboard_snapshot():
    try:
        import pyperclip

        return pyperclip.paste()
    except Exception:
        warn("core/execution/wx_human.py:440 Exception", err=repr(__import__("sys").exc_info()[1]))
        return None


def paste_text(text: str, *, via_clipboard: bool = True) -> bool:
    """Type text into the focused field.

    **唯一实现**（2026-10-28，SEC-006）：全仓只有这里真的碰剪贴板。
    上层一律调 `core.execution.wx_chat.type_text`（它管焦点、返回值与日志），
    不要直接调本函数——两个入口会让「改一处漏一处」。

    Clipboard + Ctrl-V is used because WeChat's search box does not accept
    synthesised Unicode keystrokes for CJK; on the wire this is exactly what a
    user pressing Ctrl-V looks like.
    """
    if not text:
        return False
    if not via_clipboard:
        return False

    previous = _clipboard_snapshot()
    try:
        import pyperclip

        pyperclip.copy(text)
    except Exception as exc:
        log.warning("clipboard_paste_failed", error=str(exc))
        return False

    time.sleep(random.uniform(0.05, 0.12))
    hotkey(VK_CONTROL, ord("V"))
    # The target reads the clipboard on its own schedule; keep a small floor so
    # a high speed setting cannot race the paste, but still honor the profile.
    time.sleep(max(0.15, random.uniform(0.30, 0.55) / max(0.05, _profile.speed)))

    if previous:
        restore_clipboard(previous)
    return True
