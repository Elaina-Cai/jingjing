from __future__ import annotations

import asyncio
import base64
import json
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import structlog

from core.execution import wx_paths as _wx_paths
from safelog import warn

log = structlog.get_logger()

_DATA_ROOT = _wx_paths.DATA_ROOT


def _sticker_data_dir(platform: str = "wechat", account_id: str = "") -> Path:
    return _wx_paths.account_dir(platform, account_id)


def _sticker_templates_dir(platform: str = "wechat", account_id: str = "") -> Path:
    return _wx_paths.sticker_templates_dir(platform, account_id)


def _sticker_index_path(platform: str = "wechat", account_id: str = "") -> Path:
    return _wx_paths.sticker_index_path(platform, account_id)


def _ensure_dpi_aware() -> None:
    from core.execution import wx_vision

    wx_vision.ensure_dpi_aware()


_dpi_aware_done = False


def _restore_wechat_if_minimized(hwnd) -> None:
    import ctypes
    SW_RESTORE = 9
    if ctypes.windll.user32.IsIconic(hwnd):
        ctypes.windll.user32.ShowWindow(hwnd, SW_RESTORE)
        time.sleep(0.5)
        log.info("wechat_restored_from_minimized")


def _get_wechat_rect() -> tuple[int, int, int, int] | None:
    """Screen rectangle of the WeChat window, in physical pixels."""
    from core.execution import wx_vision

    win = wx_vision.find_wechat_window()
    if win is None:
        return None
    if win.minimized:
        _restore_wechat_if_minimized(win.hwnd)
        win.refresh()
    return win.rect


def _screenshot_region(left: int, top: int, right: int, bottom: int):
    from core.execution import wx_vision

    return wx_vision.capture((left, top, right, bottom))


def _click_at(x: int, y: int) -> None:
    from core.execution import wx_human

    wx_human.click(x, y, aim_jitter=1.5)






def _press_escape() -> None:
    from core.execution import wx_human

    wx_human.key_press(wx_human.VK_ESCAPE)


def _ui_templates_dir() -> Path:
    return _wx_paths.ui_templates_dir()


def _emoji_button_template_path() -> Path:
    return _ui_templates_dir() / "emoji_button.png"


def _find_by_template(img, template_path: Path, threshold: float = 0.7) -> tuple[int, int] | None:
    import numpy as np
    try:
        import cv2
    except ImportError:
        warn("core/execution/sticker_auto_setup.py:97 ImportError", err=repr(__import__("sys").exc_info()[1]))
        return None
    if not template_path.exists():
        return None
    from PIL import Image
    template = Image.open(str(template_path))
    arr = np.array(img)
    tpl = np.array(template)
    if arr.ndim != 3 or tpl.ndim != 3:
        return None
    arr_gray = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)
    tpl_gray = cv2.cvtColor(tpl, cv2.COLOR_RGB2GRAY)
    if tpl_gray.shape[0] > arr_gray.shape[0] or tpl_gray.shape[1] > arr_gray.shape[1]:
        return None
    result = cv2.matchTemplate(arr_gray, tpl_gray, cv2.TM_CCOEFF_NORMED)
    _, max_val, _, max_loc = cv2.minMaxLoc(result)
    if max_val >= threshold:
        cx = max_loc[0] + tpl_gray.shape[1] // 2
        cy = max_loc[1] + tpl_gray.shape[0] // 2
        log.debug("template_matched", template=str(template_path), confidence=round(max_val, 3), pos=(cx, cy))
        return (cx, cy)
    log.debug("template_not_matched", template=str(template_path), best_confidence=round(max_val, 3))
    return None


def _find_emoji_button(img) -> tuple[int, int] | None:
    """Locate the emoji button on the input toolbar.

    Structural detection comes first: it reads the toolbar glyph run out of the
    screenshot, so it keeps working after the window is moved or resized, which
    a stored pixel template cannot do.  The template is kept as a fallback.
    """
    try:
        from core.execution import wx_vision

        found = wx_vision.find_emoji_button(img)
        if found:
            return found
    except Exception as exc:
        log.debug("emoji_button_structural_failed", error=str(exc))
    return _find_by_template(img, _emoji_button_template_path(), threshold=0.65)


def _find_custom_tab(img) -> tuple[int, int] | None:
    from core.execution import wx_vision

    try:
        found = wx_vision.find_custom_tab(img)
        if found:
            return found
    except Exception as exc:
        log.debug("custom_tab_structural_failed", error=str(exc))

    custom_tab_path = _ui_templates_dir() / "custom_tab.png"
    pos = _find_by_template(img, custom_tab_path, threshold=0.65)
    if pos:
        return pos
    import numpy as np
    try:
        from rapidocr_onnxruntime import RapidOCR
        ocr = RapidOCR()
        result, _ = ocr(np.array(img))
        if result:
            for item in result:
                bbox, text, confidence = item
                if "自定义" in text.strip():
                    x = int((bbox[0][0] + bbox[2][0]) / 2)
                    y = int((bbox[0][1] + bbox[2][1]) / 2)
                    return (x, y)
    except Exception:
        warn("core/execution/sticker_auto_setup.py:166 Exception", err=repr(__import__("sys").exc_info()[1]))
    return None






_CALIBRATION_PATH = _DATA_ROOT / "wechat" / "ui_calibration.json"


def load_calibration() -> dict:
    """Read the stored calibration record, if any.

    The record is now informational: the emoji button is located structurally on
    every frame, so a previously saved ratio is only used to sanity-check what
    detection reports.
    """
    try:
        if _CALIBRATION_PATH.exists():
            return json.loads(_CALIBRATION_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        log.warning("calibration_read_failed", error=str(exc))
    return {}


def save_calibration(ratio_x: float, ratio_y: float) -> dict:
    record = {
        "emoji_button_ratio_x": round(float(ratio_x), 4),
        "emoji_button_ratio_y": round(float(ratio_y), 4),
        "saved_at": datetime.now().isoformat(timespec="seconds"),
        "note": "informational only; the sender re-detects the button every frame",
    }
    try:
        _CALIBRATION_PATH.parent.mkdir(parents=True, exist_ok=True)
        _CALIBRATION_PATH.write_text(json.dumps(record, ensure_ascii=False, indent=2),
                                     encoding="utf-8")
    except Exception as exc:
        log.warning("calibration_write_failed", error=str(exc))
    return record


async def probe_emoji_button(ratio_x: float = 0.05, ratio_y: float = 0.15) -> dict:
    """Detect the emoji button live and report where it actually is.

    This replaces the old blind "sweep candidate ratios until the user hits
    Ctrl+C" workflow: the button is found from the toolbar glyph run, and the
    supplied ratio is only reported as a distance so the UI can show whether the
    stored calibration still describes reality.
    """
    from core.execution import wx_vision

    def _probe() -> dict:
        win = wx_vision.find_wechat_window()
        if win is None:
            return {"ok": False, "error": "未找到微信窗口，请确认微信已启动并登录"}
        image, rect = wx_vision.capture_window(win)
        layout = wx_vision.detect_layout(image)
        if layout.emoji is None:
            return {"ok": False, "error": "未能在窗口中定位表情按钮（请确认微信停在聊天界面）",
                    "layout": layout.as_dict()}

        point = layout.emoji_point()
        actual_x = point[0] / max(1, layout.width)
        actual_y = point[1] / max(1, layout.height)

        try:
            from PIL import ImageDraw

            debug = image.copy()
            draw = ImageDraw.Draw(debug)
            draw.rectangle([layout.emoji.x - 6, layout.emoji.y - 6,
                            layout.emoji.x + layout.emoji.w + 6,
                            layout.emoji.y + layout.emoji.h + 6], outline=(255, 0, 0), width=3)
            for icon in layout.icons:
                draw.rectangle([icon.x, icon.y, icon.x + icon.w, icon.y + icon.h],
                               outline=(0, 128, 255), width=2)
            draw.line([layout.chat_left, 0, layout.chat_left, layout.height],
                      fill=(0, 200, 0), width=2)
            out_dir = _DATA_ROOT / "wechat" / "ui_templates"
            out_dir.mkdir(parents=True, exist_ok=True)
            debug.save(str(out_dir / "emoji_button_probe.png"))
        except Exception as exc:
            log.debug("probe_debug_save_failed", error=str(exc))

        return {
            "ok": True,
            "found": True,
            "emoji_button": {"x": point[0], "y": point[1],
                             "ratio_x": round(actual_x, 4), "ratio_y": round(actual_y, 4)},
            "requested": {"ratio_x": ratio_x, "ratio_y": ratio_y},
            "delta": {"ratio_x": round(actual_x - ratio_x, 4),
                      "ratio_y": round(actual_y - ratio_y, 4)},
            "window": {"size": [layout.width, layout.height], "rect": list(rect)},
            "toolbar": {"icons": len(layout.icons), "y": layout.toolbar_y,
                        "pitch": layout.notes.get("toolbar_pitch")},
            "notes": layout.notes,
        }

    return await asyncio.get_event_loop().run_in_executor(None, _probe)
