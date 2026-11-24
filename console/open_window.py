# -*- coding: utf-8 -*-
"""开窗器：把「鲸鲸面板」用 Edge 的 app 模式打开（没有地址栏、没有标签页）。

为什么不让 bat 直接 start：
  * 目标是中文路径 + file:// 协议，cmd 的 start 在转义上很脆；
  * 需要一个地方判断"窗口已经开着就别再开一个"。
"""
from __future__ import annotations

import ctypes
import os
import subprocess
import time
import sys
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:   # 作为脚本直接跑时，sys.path[0] 是 console/
    sys.path.insert(0, ROOT)

from core.infra.envflags import text

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAGE = os.path.join(ROOT, "console", "panel.html")
PROFILE = os.path.join(ROOT, "data", "console", ".ui_profile")
TITLE = "鲸鲸 · 控制台"
CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]


from core.infra import egress


def edge() -> str | None:
    for p in CANDIDATES:
        if os.path.exists(p):
            return p
    return None


def window_open() -> bool:
    found = [False]

    def cb(hwnd, _lp):
        n = ctypes.windll.user32.GetWindowTextLengthW(hwnd)
        if n:
            buf = ctypes.create_unicode_buffer(n + 1)
            ctypes.windll.user32.GetWindowTextW(hwnd, buf, n + 1)
            if TITLE in buf.value:
                found[0] = True
                return False
        return True

    try:
        ctypes.windll.user32.EnumWindows(ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)(cb), 0)
    except Exception:
        return False
    return found[0]


def _api_up(timeout: float = 6.0) -> bool:
    """等执行器（collector）把 39872 上的 HTTP 端点拉起来。"""
    import socket as _socket
    end = time.time() + timeout
    while time.time() < end:
        try:
            with egress.connect("127.0.0.1", 39872, timeout_ms=600):
                return True
        except OSError:
            time.sleep(0.25)
    return False


def main() -> int:
    exe = edge()
    if not exe:
        return 2
    if window_open():
        return 0
    if not os.path.exists(PAGE):
        return 3
    # 带 mtime 版本号：Edge 会用 user-data-dir 的缓存，不加这一下改完面板还看旧的（2026-10-27）
    try:
        _v = int(os.path.getmtime(PAGE))
    except OSError:
        _v = 0
    url = "http://127.0.0.1:39872/app?v=%d" % _v
    if not _api_up():
        url = "file:///" + PAGE.replace("\\", "/")  # 兜底：执行器没起也能看快照
    os.makedirs(PROFILE, exist_ok=True)
    subprocess.Popen(
        [exe, "--app=" + url, f"--window-size={text("YINYUE_UI_SIZE", "1480,920")}", "--user-data-dir=" + PROFILE,
         "--allow-file-access-from-files", "--no-first-run", "--no-default-browser-check",
         "--disable-features=msEdgeSidebarV2"],
        creationflags=0x00000008 | 0x00000200,  # DETACHED_PROCESS | CREATE_NO_WINDOW
        close_fds=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
