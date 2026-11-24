"""让机器别睡，但允许屏幕熄灭。

2026-11-01：主人要求「关屏但别睡眠」——关屏不影响发送（抓屏走 GDI，未锁屏时照样能截能点），
但睡眠/休眠醒来要走登录，等于把她锁在门外（2026-10-31 07:06–09:33 那 12 条 failed 就是这么来的）。

所以这里只请求 ES_SYSTEM_REQUIRED（系统不睡），**绝不**请求 ES_DISPLAY_REQUIRED（屏幕该怎么黑怎么黑）。
进程存活期间生效；进程退出后系统自动收回。
"""

import ctypes

try:                                    # 跟项目其它模块一样走 structlog（日志才会进 logs/*.out）
    import structlog
    log = structlog.get_logger("core.infra.awake")
except Exception:                       # pragma: no cover
    import logging
    log = logging.getLogger(__name__)

_ES_CONTINUOUS = 0x80000000
_ES_SYSTEM_REQUIRED = 0x00000001
_ES_DISPLAY_REQUIRED = 0x00000002

_STATE = {"on": False, "reason": ""}


def keep_system_awake(reason: str = "") -> bool:
    """请求「系统别睡」（不阻止熄屏）。重复调用无副作用。"""
    try:
        kernel32 = ctypes.windll.kernel32
        kernel32.SetThreadExecutionState.restype = ctypes.c_ulong
        prev = kernel32.SetThreadExecutionState(_ES_CONTINUOUS | _ES_SYSTEM_REQUIRED)
        if not prev:
            log.warning("keep_system_awake_failed", reason=reason)
            return False
        _STATE["on"] = True
        _STATE["reason"] = reason
        log.info("keep_system_awake", reason=reason, prev=hex(int(prev)))
        return True
    except Exception as exc:              # pragma: no cover - 非 Windows 或权限异常
        log.warning("keep_system_awake_error", reason=reason, err=repr(exc))
        return False


def release(reason: str = "") -> None:
    """撤销请求（进程退出时系统也会自动撤销）。"""
    try:
        kernel32 = ctypes.windll.kernel32
        kernel32.SetThreadExecutionState.restype = ctypes.c_ulong
        kernel32.SetThreadExecutionState(_ES_CONTINUOUS)
        _STATE["on"] = False
        log.info("keep_system_awake_release", reason=reason or _STATE.get("reason", ""))
    except Exception as exc:              # pragma: no cover
        log.warning("keep_system_awake_release_error", err=repr(exc))


_WM_SYSCOMMAND = 0x0112
_SC_MONITORPOWER = 0xF170
_MONITOR_ON = -1
_MONITOR_OFF = 2


def wake_display_once(reason: str = "") -> bool:
    """点亮显示器一次（熄屏/后台时激活窗口失败，用它兜底）。

    用 PostMessage 广播而不是 SendMessage：广播版 SendMessage 要逐个等所有顶层窗口应答，
    只要有一个窗口不泵消息（半死的微信就是），调用就永远不返回（2026-11-01 实测挂死）。
    """
    try:
        user32 = ctypes.windll.user32
        posted = bool(user32.PostMessageW(0xFFFF, _WM_SYSCOMMAND, _SC_MONITORPOWER, _MONITOR_ON))
        log.info("wake_display_once", reason=reason, posted=posted)
        return posted
    except Exception as exc:          # pragma: no cover
        log.warning("wake_display_once_failed", reason=reason, err=repr(exc))
        return False


def is_on() -> bool:
    return bool(_STATE["on"])
