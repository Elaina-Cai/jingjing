"""六项能力自检：动手之前先确认「这台机器现在真能动手」。

为什么必须有它
--------------
我们在这台机器上真踩过的坑，全都不是「代码写错了」，而是「环境不具备而代码不知道」：

* ``.venv_it`` 被打上 Low 完整性标签 → ``SetForegroundWindow`` 全 ``err=5``，
  而 SendInput 对更高的进程**静默失败**（不报错，只是什么都没发生）。
* 锁屏 / 远程桌面断开 → 输入桌面不可写，同一段代码突然不生效。
* DPI 不一致 → 窗口内坐标换算出屏幕坐标差几十像素，点到了隔壁按钮。
* 顶栏读不出、输入框读不出、独立弹窗枚举不到 → 后面所有判定都建立在沙上。

所以：**宁可提前 blocked，也不要动手到一半才发现什么都没发生。**

判据（六项，任一不过就 blocked，且必须给出可读理由）
------------------------------------------------
``integrity`` 我们的进程与目标进程完整性级别相当（不是更低）
``session``   会话活跃、输入桌面可写（没锁屏、没断开）
``dpi``       进程 DPI 感知（坐标不需要外部换算）
``identity``  目标身份可确认（能读到会话名/顶栏指纹）
``writable``  输入框可读（说明聚焦与粘贴这条链路通）
``menu``      独立窗口可枚举（@ 名单、右键菜单靠它）

两条纪律
--------
1. **探不出来 = 不过**（fail closed）。任何探针抛异常都按 False 记，并留下 ``probe_error``。
2. **报告要能一行进日志**：``as_dict()`` / ``one_line()`` 都是给量具与排障用的，
   不许把整棵 UIA 树塞进去。
"""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass, field
from typing import Any, Callable

from core.execution.gui.lease import CapabilityMissing
from safelog import warn

log = logging.getLogger(__name__)

ITEMS: tuple[str, ...] = (
    "integrity",
    "session",
    "dpi",
    "identity",
    "writable",
    "menu",
)

REASONS: dict[str, str] = {
    "integrity": "integrity_too_low",
    "session": "session_locked",
    "dpi": "dpi_mismatch",
    "identity": "target_unknown",
    "writable": "composer_unwritable",
    "menu": "menu_unreadable",
}

_INTEGRITY_MEDIUM = 0x2000


@dataclass(frozen=True)
class CapabilityReport:
    integrity_ok: bool = False
    session_active: bool = False
    dpi_ok: bool = False
    identity_ok: bool = False
    writable_ok: bool = False
    menu_ok: bool = False
    detail: dict[str, str] = field(default_factory=dict)

    def _value(self, item: str) -> bool:
        return bool(getattr(self, _FIELD[item]))

    @property
    def ok(self) -> bool:
        return all(self._value(item) for item in ITEMS)

    def failures(self) -> list[str]:
        return [item for item in ITEMS if not self._value(item)]

    def missing(self) -> list[str]:
        return [REASONS[item] for item in self.failures()]

    @property
    def reason(self) -> str:
        fails = self.failures()
        return REASONS[fails[0]] if fails else ""

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "ok": self.ok,
            "failures": self.failures(),
            "reason": self.reason,
        }
        for item in ITEMS:
            out[item] = self._value(item)
        out["detail"] = dict(self.detail)
        return out

    def one_line(self) -> str:
        marks = "".join("1" if self._value(i) else "0" for i in ITEMS)
        if self.ok:
            return f"caps={marks} ok"
        return f"caps={marks} blocked:{self.reason}"


_FIELD = {
    "integrity": "integrity_ok",
    "session": "session_active",
    "dpi": "dpi_ok",
    "identity": "identity_ok",
    "writable": "writable_ok",
    "menu": "menu_ok",
}


# ---------------------------------------------------------------- 真机探针
def _user32() -> Any:
    import ctypes

    return ctypes.windll.user32


def _probe_integrity(*, target_pid: int = 0) -> bool:
    if not sys.platform.startswith("win"):
        return False
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.windll.kernel32
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    TOKEN_QUERY = 0x0008
    TokenIntegrityLevel = 25

    def _level(pid: int) -> int | None:
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return None
        try:
            token = wintypes.HANDLE()
            if not ctypes.windll.advapi32.OpenProcessToken(
                handle, TOKEN_QUERY, ctypes.byref(token)
            ):
                return None
            try:
                size = wintypes.DWORD(0)
                ctypes.windll.advapi32.GetTokenInformation(
                    token, TokenIntegrityLevel, None, 0, ctypes.byref(size)
                )
                buf = ctypes.create_string_buffer(size.value or 16)
                if not ctypes.windll.advapi32.GetTokenInformation(
                    token, TokenIntegrityLevel, buf, size, ctypes.byref(size)
                ):
                    return None
                # TOKEN_MANDATORY_LABEL -> SID 的最后一段就是完整性 RID
                sid = ctypes.cast(buf, ctypes.POINTER(ctypes.c_void_p))[0]
                sub_count = ctypes.cast(sid, ctypes.POINTER(ctypes.c_ubyte))[1]
                import struct

                raw = ctypes.string_at(sid, 8 + 4 * sub_count)
                rid = struct.unpack("<I", raw[-4:])[0]
                return int(rid) << 12
            finally:
                kernel32.CloseHandle(token)
        finally:
            kernel32.CloseHandle(handle)

    try:
        import os

        mine = _level(os.getpid())
        if mine is None:
            return False
        if target_pid:
            theirs = _level(int(target_pid))
            if theirs is None:
                return False  # 打不开目标 = 我们更低
            return mine >= theirs
        return mine >= _INTEGRITY_MEDIUM
    except Exception as exc:  # noqa: BLE001
        log.warning("caps_integrity_failed error=%r", exc)
        return False


def _probe_session() -> bool:
    try:
        user32 = _user32()
        DESKTOP_READOBJECTS = 0x0001
        DESKTOP_WRITEOBJECTS = 0x0002
        handle = user32.OpenInputDesktop(
            0, False, DESKTOP_READOBJECTS | DESKTOP_WRITEOBJECTS
        )
        if not handle:
            return False  # 锁屏 / 断开 / 别的会话占用输入桌面
        user32.CloseDesktop(handle)
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("caps_session_failed error=%r", exc)
        return False


def _probe_dpi() -> bool:
    """进程 DPI 感知。**先加载我们的坐标栈**，再读进程值。

    实测（2026-10-21）：裸 python 进程是 DPI-unaware（``val=0``）；真正把我们设成 aware 的
    是 ``wx_vision.ensure_dpi_aware()``（``SetProcessDpiAwareness(2)`` 优先，失败退
    ``SetProcessDPIAware``）。所以探针必须先调它、再读进程值——否则每次冷启动都误报
    ``dpi_mismatch``，而真到动手时其实是对的（第一次跑就踩到了）。
    """
    try:
        import ctypes

        try:
            from core.execution import wx_vision

            wx_vision.ensure_dpi_aware()  # 与坐标栈用同一段代码把我们设成 aware
        except Exception:  # noqa: BLE001 - 坐标栈不在，视为不具备
            warn("core/execution/gui/capability.py:219 Exception", err=repr(__import__("sys").exc_info()[1]))
            return False
        val = ctypes.c_int(0)
        hr = ctypes.windll.shcore.GetProcessDpiAwareness(None, ctypes.byref(val))
        if hr != 0:
            return True  # 老系统没有这个 API：不阻塞
        return val.value in (1, 2)  # 1=system aware, 2=per-monitor aware
    except Exception:  # noqa: BLE001
        warn("core/execution/gui/capability.py:226 Exception", err=repr(__import__("sys").exc_info()[1]))
        return False  # 探不出来 = 不过


def _probe_identity() -> bool:
    """能不能**确认**目标身份，而不是"现在已经确认过"。

    动手前我们通常还没切会话（正是要去打开它），所以这里判的是「窗口找得到、
    矩形读得到」——即身份可读的基础具备；具体是哪一位，由 ``target.check()``
    在动作前后用代次判定，不是这一步的事。
    """
    try:
        from core.execution import wx_chat

        win = wx_chat._wechat_window()
        if win is None:
            return False
        return bool(int(getattr(win, "hwnd", 0) or 0)) and bool(getattr(win, "rect", None))
    except Exception as exc:  # noqa: BLE001
        log.warning("caps_identity_failed error=%r", exc)
        return False


def _probe_writable() -> bool:
    try:
        from core.execution import wx_chat

        return wx_chat.message_box_text() is not None
    except Exception as exc:  # noqa: BLE001
        log.warning("caps_writable_failed error=%r", exc)
        return False


def _probe_menu() -> bool:
    try:
        from core.execution import wx_chat

        wx_chat._picker_items()
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("caps_menu_failed error=%r", exc)
        return False


DEFAULT_PROBES: dict[str, Callable[..., bool]] = {
    "integrity": _probe_integrity,
    "session": _probe_session,
    "dpi": _probe_dpi,
    "identity": _probe_identity,
    "writable": _probe_writable,
    "menu": _probe_menu,
}


def probe(
    *,
    probes: dict[str, Callable[..., bool]] | None = None,
    target_pid: int = 0,
) -> CapabilityReport:
    """跑六项自检。``probes`` 可注入（测试不碰真桌面）；给 ``None`` 表示该探针缺失。"""
    table = dict(DEFAULT_PROBES)
    if probes:
        table.update(probes)
    values: dict[str, bool] = {}
    detail: dict[str, str] = {}
    for item in ITEMS:
        fn = table.get(item)
        if fn is None:
            values[item] = False
            detail[item] = "probe_missing"
            continue
        try:
            ok = bool(fn(target_pid=target_pid)) if item == "integrity" else bool(fn())
        except Exception as exc:  # noqa: BLE001 - 探不出来 = 不过
            warn("core/execution/gui/capability.py:299 Exception", err=repr(exc))
            values[item] = False
            detail[item] = f"probe_error:{exc}"
            continue
        values[item] = ok
        if not ok:
            detail[item] = REASONS[item]
    report = CapabilityReport(
        integrity_ok=values["integrity"],
        session_active=values["session"],
        dpi_ok=values["dpi"],
        identity_ok=values["identity"],
        writable_ok=values["writable"],
        menu_ok=values["menu"],
        detail=detail,
    )
    log.info("caps_probe %s", report.one_line())
    return report


def require(report: CapabilityReport) -> None:
    """不过就抛 ``CapabilityMissing(reason)``，理由按固定顺序取第一条。"""
    if report.ok:
        return
    fails = report.failures()
    reason = REASONS[fails[0]] if fails else "capability_unknown"
    extra = report.detail.get(fails[0], "") if fails else ""
    raise CapabilityMissing(f"{reason}" + (f"（{extra}）" if extra and extra != reason else ""))
