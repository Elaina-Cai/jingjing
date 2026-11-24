"""Public API surface for the WeChat execution layer.

Why this module exists
----------------------

The web layer used to reach straight into execution-layer internals: ten
underscore-prefixed symbols pulled from two different modules, plus path
arithmetic written out by hand that had to keep agreeing with theirs by
convention.  That is a web -> implementation dependency.  Renaming a private
helper inside the execution layer would break an HTTP route at request time,
and nothing would catch it before a user clicked the button.

Everything here is public and intended to stay stable.  The modules behind it
remain free to reorganise.  Callers should import from here and nowhere else.

    from core.execution import wx_api

    rect = wx_api.wechat_window_rect()
    index = wx_api.sticker_index_path(account_id="wxid_...")
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import structlog

from core.execution import wx_human, wx_paths, wx_vision
from safelog import warn

log = structlog.get_logger()

__all__ = [
    # paths
    "DATA_ROOT", "platform_dir", "account_dir", "sticker_index_path",
    "sticker_templates_dir", "sticker_thumbs_dir", "ui_templates_dir",
    # screen
    "ensure_dpi_aware", "wechat_window", "wechat_status", "wechat_window_rect",
    "screenshot_region", "screenshot_window", "find_emoji_button",
    "find_custom_tab", "click_screen", "press_escape", "is_frame_wechat",
    # calibration
    "load_calibration", "save_calibration", "probe_emoji_button",
    # sticker collection
    "capture_panel_thumbs", "bind_thumbs",
    "load_sticker_index", "save_sticker_index", "list_moods",
    "list_sticker_accounts", "list_stickers",
    # sending
    "send_sticker",
]

DEFAULT_PLATFORM = wx_paths.DEFAULT_PLATFORM
DATA_ROOT = wx_paths.DATA_ROOT

# --------------------------------------------------------------------------------------
# Paths -- one owner, so the web layer never composes these itself
# --------------------------------------------------------------------------------------

platform_dir = wx_paths.platform_dir
account_dir = wx_paths.account_dir
sticker_index_path = wx_paths.sticker_index_path
sticker_templates_dir = wx_paths.sticker_templates_dir
sticker_thumbs_dir = wx_paths.sticker_thumbs_dir
ui_templates_dir = wx_paths.ui_templates_dir


# --------------------------------------------------------------------------------------
# Screen and window
# --------------------------------------------------------------------------------------

def ensure_dpi_aware() -> Optional[str]:
    """Make this process per-monitor DPI aware; safe to call repeatedly."""
    return wx_vision.ensure_dpi_aware()


def wechat_window():
    """The WeChat window, refreshed, or None when it is not running."""
    win = wx_vision.find_wechat_window()
    return win.refresh() if win is not None else None


def wechat_status() -> dict:
    """Window geometry plus whether the screen really shows WeChat.

    The web UI can use this to explain itself instead of silently doing nothing:
    a window parked in the notification area, or covered by another
    application, both mean a send will refuse to run.
    """
    win = wx_vision.find_wechat_window()
    if win is None:
        return {"ok": False, "running": False,
                "error": "未找到微信窗口，请确认微信 PC 客户端已启动"}

    win.refresh()
    status = {"ok": True, "running": True, "window": win.as_dict()}
    try:
        image, layout = wx_vision.capture_window(win)
        status["geometry"] = layout.as_dict()
        status["frame_is_wechat"] = wx_vision.frame_looks_like_wechat(layout)
        status["occluded"] = wx_vision.foreground_hwnd() != win.hwnd
    except Exception as exc:
        warn("core/execution/wx_api.py:100 Exception", err=repr(exc))
        status["frame_is_wechat"] = False
        status["error"] = str(exc)
    return status


def wechat_window_rect():
    """Screen rectangle of the WeChat window, or None."""
    win = wechat_window()
    return None if win is None else tuple(win.rect)


def screenshot_region(left: int, top: int, right: int, bottom: int):
    """Grab an arbitrary screen rectangle as an RGB PIL image."""
    return wx_vision.capture((int(left), int(top), int(right), int(bottom)))


def screenshot_window():
    """Grab the whole WeChat window; returns (image, rect) or (None, None)."""
    win = wechat_window()
    if win is None:
        return None, None
    return wx_vision.capture_window(win)


def is_frame_wechat(image) -> bool:
    """Whether an already-grabbed frame is really the WeChat conversation view."""
    return wx_vision.frame_looks_like_wechat(wx_vision.detect_layout(image))


def find_emoji_button(image):
    """Window-local centre of the input toolbar's emoji button, or None."""
    return wx_vision.find_emoji_button(image)


def find_custom_tab(image):
    """Window-local centre of the custom-sticker tab, or None."""
    return wx_vision.find_custom_tab(image)


def click_screen(x: int, y: int) -> None:
    """Click an absolute screen coordinate with simulated mouse input."""
    wx_human.click(int(x), int(y), aim_jitter=1.5)


def press_escape() -> None:
    """Send Escape, which dismisses the sticker popup."""
    wx_human.key_press(wx_human.VK_ESCAPE)


# --------------------------------------------------------------------------------------
# Calibration
# --------------------------------------------------------------------------------------

def _auto_setup():
    from core.execution import sticker_auto_setup

    return sticker_auto_setup


def load_calibration() -> dict:
    return _auto_setup().load_calibration()


def save_calibration(ratio_x: float, ratio_y: float) -> dict:
    return _auto_setup().save_calibration(ratio_x, ratio_y)


async def probe_emoji_button(ratio_x: float = 0.05, ratio_y: float = 0.15) -> dict:
    return await _auto_setup().probe_emoji_button(ratio_x, ratio_y)


# --------------------------------------------------------------------------------------
# Sticker collection
# --------------------------------------------------------------------------------------



def capture_panel_thumbs(platform: str = DEFAULT_PLATFORM, account_id: str = "",
                         max_pages: int = 12) -> dict:
    """Save every popup thumbnail; feeds bind_thumbs()."""
    from core.execution import wx_sticker_sender

    return wx_sticker_sender.capture_panel_thumbs(platform, account_id, max_pages=max_pages)


def bind_thumbs(platform: str = DEFAULT_PLATFORM, account_id: str = "",
                min_score: float = 0.5, write: bool = True) -> dict:
    """Point each mood at the learned crop that matches it best."""
    from core.execution import wx_sticker_sender

    return wx_sticker_sender.bind_thumbs(platform, account_id,
                                         min_score=min_score, write=write)


def load_sticker_index(platform: str = DEFAULT_PLATFORM, account_id: str = "") -> dict:
    import json

    path = wx_paths.sticker_index_path(platform, account_id)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        log.warning("sticker_index_unreadable", path=str(path), error=str(exc))
        return {}


def save_sticker_index(index: dict, platform: str = DEFAULT_PLATFORM,
                       account_id: str = "") -> Path:
    import json

    path = wx_paths.sticker_index_path(platform, account_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def list_moods(platform: str = DEFAULT_PLATFORM, account_id: str = "") -> list:
    return sorted(load_sticker_index(platform, account_id).keys())


def list_sticker_accounts(platform: Optional[str] = None) -> list:
    """Every account that has a sticker registry, across platforms.

    This is the read side of the per-account isolation design: the super
    terminal can see all accounts without knowing their ids in advance.
    """
    from db import sticker_store

    out = []
    for candidate in sticker_store.list_stores(platform):
        stickers = candidate.load()
        out.append({
            "platform": candidate.platform,
            "account_id": candidate.account_id,
            "count": len(stickers),
            "moods": sorted(stickers),
            "root": str(candidate.root),
        })
    return out


def list_stickers(platform: Optional[str] = None, account_id: Optional[str] = None) -> list:
    """Every sticker matching the filter, each tagged with platform + account."""
    from db import sticker_store

    return [
        {
            "emotion": s.emotion,
            "platform": s.platform,
            "account_id": s.account_id,
            "label": s.label,
            "aliases": list(s.aliases),
            "context": s.context,
            "scope": list(s.scope),
            "tab": s.tab,
            "images": s.images(sticker_store.StickerStore(s.platform, s.account_id).root),
        }
        for s in sticker_store.all_stickers(platform, account_id)
    ]


# --------------------------------------------------------------------------------------
# Sending
# --------------------------------------------------------------------------------------

def send_sticker(who: str, mood_id: str, emotion: str = "", platform: str = DEFAULT_PLATFORM,
                 account_id: str = "", dry_run: bool = False) -> dict:
    """Send a sticker using simulated mouse and keyboard input only.

    dry_run rehearses the whole flow without the final click, so nothing is
    delivered to the other side.
    """
    from core.execution import wx_sticker_sender

    return wx_sticker_sender.send_sticker(who, mood_id, emotion=emotion,
                                          platform=platform, account_id=account_id,
                                          dry_run=dry_run)
