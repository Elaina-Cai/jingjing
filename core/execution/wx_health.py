# -*- coding: utf-8 -*-
"""微信客户端健康：分清「没开」「没窗口」「最小化」「在后台」「在前台」。

为什么单独一层：发送失败以前只剩一句“无法激活微信窗口”，
分不清是客户端没启动、窗口被最小化，还是前台锁。2026-11-18 实际就是没开微信。
只读探测，不激活、不发送。
"""
from __future__ import annotations

import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PATH = ROOT / "logs" / "wechat_health.json"
_CACHE: dict = {"at": 0.0, "value": {}}
_CACHE_S = 5.0


def _client_pids() -> list[int]:
    try:
        import psutil
    except Exception:
        return []
    out = []
    for proc in psutil.process_iter(["pid", "name"]):
        name = str((proc.info or {}).get("name") or "").lower()
        if name in {"weixin.exe", "wechat.exe"}:
            out.append(int(proc.info["pid"]))
    return out


def probe(*, now: float | None = None) -> dict:
    """返回一份快照；不改变任何窗口状态。"""
    at = time.time() if now is None else float(now)
    pids = _client_pids()
    win = None
    try:
        from core.execution.wx_sender import _wechat_window

        win = _wechat_window(force=True)
    except Exception as exc:  # noqa: BLE001 - 探测失败也要给出原因
        return {"at": at, "state": "probe_failed", "error": repr(exc)[:160],
                "client_pids": pids}
    if not pids and win is None:
        return {"at": at, "state": "client_down", "client_pids": []}
    if win is None:
        return {"at": at, "state": "window_missing", "client_pids": pids}
    foreground = False
    try:
        from core.execution import wx_vision

        foreground = wx_vision.foreground_hwnd() == win.hwnd
    except Exception:
        foreground = False
    if getattr(win, "minimized", False):
        state = "minimized"
    elif not getattr(win, "visible", True):
        state = "hidden"
    elif foreground:
        state = "foreground"
    else:
        state = "background"
    return {"at": at, "state": state, "client_pids": pids, "pid": int(win.pid),
            "hwnd": int(win.hwnd), "visible": bool(win.visible),
            "minimized": bool(win.minimized), "foreground": foreground}


def read(*, fresh_s: float = 30.0) -> dict:
    """读最近一次快照；太旧就现测一次。"""
    from core.infra import jsonio

    data = jsonio.read_json(PATH, {})
    if isinstance(data, dict) and time.time() - float(data.get("at") or 0) <= fresh_s:
        return data
    return probe()


def write() -> dict:
    data = probe()
    from core.infra import jsonio

    jsonio.atomic_write_json(PATH, data)
    _CACHE.update({"at": time.time(), "value": data})
    return data


def client_down(fresh_s: float = 60.0) -> bool:
    """客户端是否确实没开；探测失败时返回 False（宁可照常排队）。"""
    data = read(fresh_s=fresh_s)
    return str(data.get("state") or "") == "client_down"
