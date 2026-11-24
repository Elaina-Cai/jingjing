"""GUI 驱动层的 backend 注册表（U-21）。

阶段 6 的四只手（CDP / ADB / UIA / Vision）不该各长各的：判看到的是同一批工具
（`gui_snapshot` / `gui_state` / `gui_click` / `gui_input` / `gui_navigate`），
**换手不该换工具**。所以这里只做一件事：把「工具要做的动词」与「哪只手能做」对上。

三条纪律：

1. **不认识的 backend 名 = `unsupported`**，绝不回退到默认那只手（回退 = 静默点错地方）；
2. **开关 `GUI_BACKENDS=off` 时只放行已经在跑的两只**（`cdp` / `uia`）——
   将来加 Vision / ADB 时，它们只能在 `on` 档被选中，否则一个参数名就能绕过整片自检；
3. **动词不在 `verbs` 里 = `unsupported`**，不是「试试看」：`uia` 今天只读不点，
   让 `gui_click` 走到它头上必须当场失败，而不是退化成坐标点击。

这里**不建连接、不读屏幕**：`reader()` 只构造驱动对象（`CdpReader()` 构造时不发请求），
真正动手仍由驱动自己按 `GUI_CHANNEL` / `DEVICE_LEASE` 判。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable

log = logging.getLogger(__name__)

VERBS: tuple[str, ...] = ("snapshot", "state", "click", "insert_text", "navigate")
# 动词别叫 type_text：那是微信 GUI 那条链的入口（SEC-006），两套同名会认错。

SAFE_DEFAULTS: tuple[str, ...] = ("cdp", "uia")
"""`GUI_BACKENDS=off` 时仍可用的两只：它们今天已经在线（CDP 走 `gui_*`，UIA 走 `gui_state`）。"""


@dataclass(frozen=True)
class BackendInfo:
    """一只手的说明书：叫什么、会哪几个动词、需要什么、备注。"""

    name: str
    verbs: frozenset[str]
    needs: tuple[str, ...] = ()
    note: str = ""


def _info(name: str, verbs: tuple[str, ...] | set[str], needs: tuple[str, ...] = (), note: str = "") -> BackendInfo:
    return BackendInfo(name=name, verbs=frozenset(verbs), needs=tuple(needs), note=note)


BACKENDS: dict[str, BackendInfo] = {
    "cdp": _info("cdp", VERBS, ("本机调试端口",), "Chromium / Electron 页面（读 DOM，不读像素）"),
    "uia": _info("uia", ("state",), ("前台窗口",), "Win32 / UWP 控件树（今天只读，不做点击）"),
}

def _now_iso() -> str:
    from datetime import datetime

    return datetime.now().isoformat(timespec="seconds")


_FACTORIES: dict[str, Callable[[], Any]] = {
    "cdp": lambda: __import__(
        "core.execution.gui.cdp", fromlist=["CdpReader"]
    ).CdpReader(),
    "uia": lambda: __import__(
        "core.execution.gui.uia", fromlist=["UiaReader"]
    ).UiaReader(taken_at=_now_iso()),
}


def _mode() -> str:
    from core import features

    return features.mode(features.GUI_BACKENDS, features.MODE_OFF)


def _gui_error(code: str, detail: str):
    from core.execution.gui.base import GuiError

    return GuiError(code, detail)


def register(info: BackendInfo, factory: Callable[[], Any]) -> None:
    """登记一只手（代码级，不是配置级：**不允许**从 YAML / 参数里动态加 backend）。"""
    BACKENDS[info.name] = info
    _FACTORIES[info.name] = factory
    log.info("gui_backend_registered name=%s verbs=%s", info.name, sorted(info.verbs))


def unregister(name: str) -> None:
    BACKENDS.pop(name, None)
    _FACTORIES.pop(name, None)


def infos() -> list[BackendInfo]:
    return [BACKENDS[n] for n in sorted(BACKENDS)]


def describe(name: str = "") -> BackendInfo | list[BackendInfo] | None:
    if not name:
        return infos()
    return BACKENDS.get(name)


def supports(name: str, verb: str) -> bool:
    info = BACKENDS.get(name)
    return bool(info and verb in info.verbs)


def resolve(requested: str = "", *, verb: str = "", default: str = "cdp") -> str:
    """参数/配方里写的名字 → 真正要用的那只手。不认识、没放行、不会这个动词都当场失败。"""
    name = (requested or default or "").strip()
    if not name:
        raise _gui_error("unsupported", "没给 backend 名，也没有默认值")
    if name not in BACKENDS:
        raise _gui_error("unsupported", f"没有这个 backend：{name}（有的：{', '.join(sorted(BACKENDS))}）")
    if _mode() != "on" and name not in SAFE_DEFAULTS:
        raise _gui_error(
            "blocked",
            f"backend {name} 还没放行（GUI_BACKENDS={_mode()}，只放行 {', '.join(SAFE_DEFAULTS)}）",
        )
    if verb and verb not in BACKENDS[name].verbs:
        raise _gui_error(
            "unsupported",
            f"backend {name} 不做 {verb}（会的是：{', '.join(sorted(BACKENDS[name].verbs)) or '无'}）",
        )
    return name


def reader(requested: str = "", *, verb: str = "", default: str = "cdp") -> Any:
    """按名字取驱动实例。只构造，不连接。"""
    name = resolve(requested, verb=verb, default=default)
    factory = _FACTORIES.get(name)
    if factory is None:  # pragma: no cover - register 保证成对
        raise _gui_error("unsupported", f"backend {name} 登记了说明书但没有实现")
    return factory()
