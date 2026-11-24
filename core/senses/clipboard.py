"""剪贴板（只读，纯 ctypes；只认文本，别的格式一律当没读到）。"""

from __future__ import annotations

import ctypes

from core.senses.base import CLIPBOARD_MAX_CHARS, SensesError
from safelog import warn

CF_UNICODETEXT = 13
_user32 = ctypes.windll.user32
_kernel32 = ctypes.windll.kernel32


def read_clipboard(*, max_chars: int = CLIPBOARD_MAX_CHARS) -> str:
    """读剪贴板文本；没有文本 / 打不开剪贴板 → 空串（不是异常）。

    为什么不抛：剪贴板被别的程序占着是常态，这不是故障，只是"这一秒没看见"。
    """
    opened = False
    try:
        opened = bool(_user32.OpenClipboard(None))
        if not opened:
            return ""
        handle = _user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return ""
        _kernel32.GlobalLock.restype = ctypes.c_void_p
        ptr = _kernel32.GlobalLock(ctypes.c_void_p(handle))
        if not ptr:
            return ""
        try:
            text = ctypes.wstring_at(ptr)
        finally:
            _kernel32.GlobalUnlock(ctypes.c_void_p(handle))
        return str(text or "")[: max(0, int(max_chars))]
    except SensesError:
        raise
    except Exception:  # noqa: BLE001 - 见上
        warn("core/senses/clipboard.py:38 Exception", err=repr(__import__("sys").exc_info()[1]))
        return ""
    finally:
        if opened:
            try:
                _user32.CloseClipboard()
            except Exception:  # noqa: BLE001
                warn("core/senses/clipboard.py:44 Exception", err=repr(__import__("sys").exc_info()[1]))
