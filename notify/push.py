from __future__ import annotations
import subprocess

import json
import structlog
from pathlib import Path
from typing import Callable
from safelog import warn

log = structlog.get_logger()

_CHANNEL_REGISTRY: dict[str, Callable[[str, str], bool]] = {}


def register_channel(name: str, handler: Callable[[str, str], bool]):
    _CHANNEL_REGISTRY[name] = handler


def _load_push_config() -> dict:
    config_path = Path("data/push_config.json")
    if config_path.exists():
        try:
            return json.loads(config_path.read_text(encoding="utf-8"))
        except Exception as _:
            warn("notify/push.py:22 Exception", err=repr(_))
    return {}


def _init_default_channels():
    if _CHANNEL_REGISTRY:
        return

    def _powershell_balloon(title: str, message: str) -> bool:
        """2026-10-24：win10toast 与 plyer 在这台机器上都没装，原来的通知链是死的。
        PowerShell 气泡不需要任何第三方包，兜底用它。"""
        try:
            ps = (
                "Add-Type -AssemblyName System.Windows.Forms;"
                "$n=New-Object System.Windows.Forms.NotifyIcon;"
                "$n.Icon=[System.Drawing.SystemIcons]::Information;"
                "$n.Visible=$true;"
                f"$n.ShowBalloonTip(5000,'{title}','{message}',"
                "[System.Windows.Forms.ToolTipIcon]::Info);"
                "Start-Sleep -Seconds 5;$n.Dispose()"
            )
            subprocess.Popen(["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
        except Exception as _:
            warn("notify/push.py:balloon Exception", err=repr(_))
            return False

    def _windows_notify(title: str, message: str) -> bool:
        try:
            from win10toast import ToastNotifier
            toaster = ToastNotifier()
            toaster.show_toast(title, message, duration=5, threaded=True)
            return True
        except ImportError:
            # 2026-10-24：装不上不是异常情况，用 PowerShell 气泡兜底，别再刷 warn
            log.debug("win10toast_missing_fallback_powershell")
            return _fallback_notify(title, message) or _powershell_balloon(title, message)
        except Exception as e:
            log.warning("windows_notify_failed", error=str(e))
            return False

    def _fallback_notify(title: str, message: str) -> bool:
        try:
            from plyer import notification            # noqa: PLC0415 - 可选依赖
        except ImportError:
            return False                              # 没装就是没装，不是异常，也不写日志
        try:
            notification.notify(title=title, message=message, timeout=5)
            return True
        except Exception as _:                        # noqa: BLE001
            from core.infra.warn_guard import once
            if once("notify_fallback_failed", every_s=6 * 3600):
                warn("notify/push.py:fallback Exception", err=repr(_))
            return False

    def _bark_notify(title: str, message: str) -> bool:
        cfg = _load_push_config()
        bark_url = cfg.get("bark_url", "")
        if not bark_url:
            return False
        try:
            import httpx
            url = bark_url.rstrip("/")
            resp = httpx.post(f"{url}/push", json={
                "title": title,
                "body": message,
                "sound": "alarm",
                "group": "yinyue",
            }, timeout=10)
            return resp.status_code == 200
        except Exception as e:
            log.warning("bark_notify_failed", error=str(e))
            return False

    def _serverchan_notify(title: str, message: str) -> bool:
        cfg = _load_push_config()
        key = cfg.get("serverchan_key", "")
        if not key:
            return False
        try:
            import httpx
            url = f"https://sctapi.ftqq.com/{key}.send"
            resp = httpx.post(url, data={"title": title, "desp": message}, timeout=10)
            return resp.status_code == 200
        except Exception as e:
            log.warning("serverchan_notify_failed", error=str(e))
            return False

    def _pushplus_notify(title: str, message: str) -> bool:
        cfg = _load_push_config()
        token = cfg.get("pushplus_token", "")
        if not token:
            return False
        try:
            import httpx
            resp = httpx.post("http://www.pushplus.plus/send", json={
                "token": token,
                "title": title,
                "content": message,
                "template": "txt",
            }, timeout=10)
            return resp.status_code == 200
        except Exception as e:
            log.warning("pushplus_notify_failed", error=str(e))
            return False

    register_channel("windows", _windows_notify)
    register_channel("bark", _bark_notify)
    register_channel("serverchan", _serverchan_notify)
    register_channel("pushplus", _pushplus_notify)


def send_notification(title: str, message: str, channels: list[str] | None = None) -> bool:
    _init_default_channels()

    results = []
    targets = channels or list(_CHANNEL_REGISTRY.keys())

    for name in targets:
        handler = _CHANNEL_REGISTRY.get(name)
        if handler is None:
            continue
        try:
            ok = handler(title, message)
        except Exception as e:
            log.warning("channel_error", channel=name, error=str(e))
            ok = False
        results.append((name, ok))

    sent_any = any(ok for _, ok in results)
    log.info("notification_sent", title=title, channels=results)
    return sent_any


def list_channels() -> list[str]:
    _init_default_channels()
    return list(_CHANNEL_REGISTRY.keys())
