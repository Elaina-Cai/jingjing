"""Send a WeChat sticker by driving the real mouse and keyboard.

Design notes
------------

Every action in this module leaves through the Windows input queue via
SendInput, exactly as physical hardware would.  Nothing is injected into the
WeChat process, no window messages are posted, and no accessibility API is
consulted -- the client sees a person moving a mouse and pressing keys.

Robustness against a moving or resizing window comes from never trusting a
remembered coordinate.  Each send re-measures the window and re-derives every
position from what is currently on screen:

1. the conversation header and the white input field bracket the pane;
2. the toolbar glyph run yields the emoji button;
3. the popup is found by differencing the window before and after the click;
4. the sticker lattice inside the popup yields the candidate click targets.

The public entry point keeps the signature the rest of the project depends on.
"""

from __future__ import annotations

import functools
import json
import os
import time
from pathlib import Path
from typing import Optional

import structlog

from core.execution import wx_human as human
from core.execution import wx_paths as _wx_paths
from core.execution import wx_vision as vision
from safelog import warn

log = structlog.get_logger()

__all__ = ["send_sticker", "resolve_sticker_entry", "capture_panel_thumbs",
           "bind_thumbs", "open_sticker_panel", "assign_thumbs"]

_DATA_ROOT = _wx_paths.DATA_ROOT


def _ui_locked(func):
    """Serialize this UI-driving entry point with every other sender.

    wx_chat owns the process-wide lock; importing it lazily keeps the
    execution package's import order free of cycles.
    """
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        from core.execution import wx_chat

        with wx_chat.UI_LOCK:
            return func(*args, **kwargs)

    return wrapper

# A cell must both score well in absolute terms and clearly beat the runner-up
# cell.  Measured on real captures, correct stickers scored 0.50-0.99 with a
# margin of 0.11-0.52, so these floors reject the "this sticker is not on this
# page" case without endangering a genuine match.
_MIN_MATCH_SCORE = 0.45
_MIN_MATCH_MARGIN = 0.08

# A mood may legitimately own several source images, and when more than one of
# them is visible at once the runner-up cell is a genuine second answer, which
# collapses the margin.  Evidence this strong is unambiguous on its own.
_STRONG_MATCH_SCORE = 0.80


def _match_accepted(score: float, margin: float) -> bool:
    if score < _MIN_MATCH_SCORE:
        return False
    return margin >= _MIN_MATCH_MARGIN or score >= _STRONG_MATCH_SCORE

_MAX_SCROLL_PAGES = 10
_MAX_TAB_TRIES = 3
_PANEL_TIMEOUT_S = 3.0
# A structurally-detected panel without a tab bar is already usable: _tab_order
# falls back to the active tab.  Waiting the whole timeout only to receive the
# same tab-less panel was costing ~3s on every send (confirmed against two real
# logs where panel.tabs stayed empty).  Keep polling briefly in case the tab bar
# is detected a frame later, then accept the fallback.
try:
    _PANEL_TAB_GRACE_S = max(0.0, float(os.environ.get("WX_PANEL_TAB_GRACE", "") or 0.45))
except ValueError:
    warn("core/execution/wx_sticker_sender.py:89 ValueError", err=repr(__import__("sys").exc_info()[1]))
    _PANEL_TAB_GRACE_S = 0.45
_MAX_CHAT_ATTEMPTS = 3


def _sticker_candidates_mode() -> str:
    """off / shadow / on for the free-text candidate lens (core/features.py).

    Three-state on purpose: ``shadow`` counts and logs what the lens *would*
    decide without changing which sticker goes out, ``on`` lets it decide and
    refuse.  ``enabled()`` would read shadow as False and quietly become an off
    switch.
    """
    try:
        from core import features

        return features.mode(features.STICKER_CANDIDATES, features.MODE_OFF)
    except Exception:
        warn("core/execution/wx_sticker_sender.py:106 Exception", err=repr(__import__("sys").exc_info()[1]))
        return "off"


def _hunt_outcome(hunt: dict, panel=None) -> tuple:
    """Why the sweep ended: ``found`` / ``exhausted`` / ``incomplete``.

    "Not on this page" and "the sweep never covered this page" are different
    facts, and one error string for both is how a bounded search gets quoted as
    proof of absence.  ``incomplete`` means something was never really read:
    the panel was lost, the lattice was not detected on some page, or the tab
    cap stopped us with tabs still unvisited.
    """
    if hunt.get("ok"):
        match = hunt.get("match")
        return "found", ("score=%.3f" % match.score) if match is not None else ""
    trace = list(hunt.get("trace") or ())
    if any(step.get("result") == "panel_lost" for step in trace):
        return "incomplete", "panel_lost"
    if not trace:
        return "incomplete", "trace_empty"
    tabs_total = len(list(getattr(panel, "tabs", ()) or ()))
    tabs_read = len({step.get("tab") for step in trace if "tab" in step})
    if tabs_total > _MAX_TAB_TRIES and tabs_read >= _MAX_TAB_TRIES:
        return "incomplete", "tab_cap_hit(%d/%d)" % (tabs_read, tabs_total)
    if any(step.get("cells") == 0 for step in trace):
        return "incomplete", "no_lattice"
    pages = len([step for step in trace if "page" in step])
    return "exhausted", "pages_read=%d" % pages


def _hunt_failure_text(hunt: dict, panel, outcome: str, why: str, detail: str) -> str:
    """on 档的措辞：搜索没走完时不许说成"没有这张表情"。"""
    if outcome == "incomplete":
        return ("表情搜索没走完（%s），这不等于没有这张表情；本轮不发，免得发错。（%s）"
                % (why, detail))
    tabs = len(list(getattr(panel, "tabs", ()) or ())) or 1
    return ("在表情面板里翻遍了 %d 个 tab、每个最多 %d 页，仍没有足够匹配的表情包（%s）。"
            % (tabs, _MAX_SCROLL_PAGES, detail))


# --------------------------------------------------------------------------------------
# Sticker index
# --------------------------------------------------------------------------------------

# Path composition lives in wx_paths; these remain as the module's local names.
def _sticker_data_dir(platform: str = "wechat", account_id: str = "") -> Path:
    return _wx_paths.account_dir(platform, account_id)


def _sticker_index_path(platform: str = "wechat", account_id: str = "") -> Path:
    return _wx_paths.sticker_index_path(platform, account_id)


def _sticker_templates_dir(platform: str = "wechat", account_id: str = "") -> Path:
    return _wx_paths.sticker_templates_dir(platform, account_id)


def _sticker_thumbs_dir(platform: str = "wechat", account_id: str = "") -> Path:
    return _wx_paths.sticker_thumbs_dir(platform, account_id)


def _is_bound_thumb(path: str) -> bool:
    """Whether an image path is a learned popup crop rather than a source image."""
    return "/thumbs/" in path.replace("\\", "/") or "sticker_thumbs" in path


def _entry_images(entry: dict, mood_id: str, templates_dir: Path | None = None,
                  root: Path | None = None) -> list:
    """Candidate images for an entry, thumbnails (exact panel crops) first.

    A panel thumbnail is a pixel-for-pixel copy of what WeChat paints, so it
    matches at around 0.95 while a full-resolution source image typically lands
    between 0.5 and 0.9.  Whenever a thumbnail has been learned it is tried
    first for that reason.
    """
    images = []

    def add(value):
        if isinstance(value, str) and value:
            path = Path(value)
            # Registry entries store paths relative to the sticker root so the
            # collection survives the checkout moving.
            if root is not None and not path.is_absolute() and not path.exists():
                path = root / path
            if path.exists() and str(path) not in images:
                images.append(str(path))
        elif isinstance(value, (list, tuple)):
            for item in value:
                add(item)

    if entry:
        add(entry.get("thumb"))
        add(entry.get("thumbs"))
        add(entry.get("template"))

    if mood_id:
        derived = templates_dir / (mood_id + ".png")
        if derived.exists() and str(derived) not in images:
            images.append(str(derived))

    return images


def _entry_dict(sticker, images: list) -> dict:
    """The shape the sender consumes, from a resolved sticker + its images."""
    return {"key": sticker.emotion, "images": images, "template": images[0],
            "label": sticker.label or sticker.emotion, "tab": sticker.tab,
            "grid_pos": None, "sticker": sticker}


def _entries_with_images(store, candidates, key: str) -> list:
    """Candidates that can actually be sent, i.e. that have real image files."""
    usable = []
    for candidate in candidates:
        images = candidate.sticker.images(store.root)
        if images:
            usable.append((candidate, images))
        else:
            log.warning("sticker_has_no_images", emotion=candidate.emotion, key=key,
                        account_id=store.account_id)
    return usable


def _candidate_resolution(store, keys) -> tuple:
    """(entry, report): the candidate lens over the model's own words.

    An entry comes back only when the evidence names *one* sendable sticker.
    Two entries claiming the same words produce a report with no entry, so the
    caller refuses the send instead of shipping whichever one came first.
    """
    report = {"keys": [], "decision": "no_match", "chosen": None, "key": ""}
    for key in keys:
        if not key:
            continue
        candidates = store.candidates(key)
        usable = _entries_with_images(store, candidates, key)
        report["keys"].append({
            "key": key,
            "matched_by": [candidate.matched_by for candidate in candidates],
            "candidates": [candidate.as_dict() for candidate, _ in usable]
                          or [candidate.as_dict() for candidate in candidates],
        })
        if len(usable) == 1:
            candidate, images = usable[0]
            report.update(decision=candidate.matched_by, chosen=candidate.emotion,
                          key=key)
            return _entry_dict(candidate.sticker, images), report
        if len(usable) > 1:
            report.update(decision="ambiguous", chosen=None, key=key,
                          siblings=[candidate.emotion for candidate, _ in usable])
            return {}, report
    return {}, report


def resolve_sticker_entry(mood_id: str, emotion: str = "", platform: str = "wechat",
                          account_id: str = "") -> dict:
    """Resolve a mood or emotion to a concrete sticker entry.

    Resolution itself lives in db.sticker_store, which owns the per-account
    registry; this only adapts the result to the shape the sender wants, namely
    a list of image paths with the learned thumbnail first.
    """
    from db import sticker_store

    store = sticker_store.StickerStore(platform, account_id)
    mode = _sticker_candidates_mode()
    if mode != "off":
        resolved, report = _candidate_resolution(store, (mood_id, emotion))
        log.info("sticker_candidate_resolution", mode=mode,
                 decision=report.get("decision"), chosen=report.get("chosen"),
                 key=report.get("key"), siblings=report.get("siblings"),
                 candidates=report.get("keys"))
        if mode == "on":
            if resolved:
                return resolved
            if report.get("decision") == "ambiguous":
                names = "、".join(report.get("siblings") or ())
                return {"error": ("「%s」对上不止一个表情（%s），没法替你挑；"
                                  "换一个更明确的名字，或直接说想发什么。本轮不发。"
                                  % (report.get("key"), names)),
                        "candidates": (report.get("keys") or [{}])[0].get("candidates")}
            # 没有候选：交给下面的精确路径，错误口径不变

    for key in (mood_id, emotion):
        if not key:
            continue
        found = store.resolve(key)
        if found is None:
            continue
        images = found.images(store.root)
        if not images:
            log.warning("sticker_has_no_images", emotion=found.emotion, key=key,
                        account_id=account_id)
            continue
        return _entry_dict(found, images)

    return {}


def _entry_templates(entry: dict, mood_id: str, platform: str, account_id: str) -> list:
    """Images to match against, already ordered thumbnail-first by the store."""
    return list(entry.get("images") or [])


# --------------------------------------------------------------------------------------
# Session helpers
# --------------------------------------------------------------------------------------

class _Session:
    """A live view of one WeChat window; caches the latest grab and layout."""

    def __init__(self, win: vision.WindowInfo, *, frame=None, layout=None):
        # Callers that already hold a freshly captured frame (the active-chat
        # reuse path, or open_sticker_panel's own claim) pass it in so the
        # constructor does not immediately pay for a second screen grab.
        self.win = win
        self.frame = None
        self._layout = layout
        if frame is None:
            self.grab()
        else:
            self.frame = frame

    def grab(self):
        self.frame, _rect = vision.capture_window(self.win)
        # The window can be moved or resized mid-flow; if the frame geometry no
        # longer matches the cached layout, throw the layout away so nothing
        # downstream converts a point using stale dimensions.
        if self._layout is not None:
            if (self.frame.size[0] != self._layout.width
                    or self.frame.size[1] != self._layout.height):
                log.info("sticker_window_geometry_changed",
                         was=(self._layout.width, self._layout.height),
                         now=self.frame.size)
                self._layout = None
        return self.frame

    def layout(self, *, refresh: bool = False) -> vision.Layout:
        if refresh or self._layout is None:
            if self.frame is None:
                self.grab()
            self._layout = vision.detect_layout(self.frame)
        return self._layout

    def to_screen(self, point) -> tuple:
        return self.win.to_screen(point[0], point[1])


def _enter_chat(win: vision.WindowInfo, who: str) -> tuple:
    """Search the contact list and open the conversation.

    Delegates to wx_chat, the capability both senders share.  Reaching into the
    text sender for this used to drag its singleton and its rate limiter along
    for no reason.
    """
    from core.execution import wx_chat

    try:
        display = wx_chat.resolve_nickname(who)
        result = wx_chat.open_chat(display, raw_who=who)
        if not result.get("ok"):
            return False, result.get("error") or ("无法打开与 %s 的聊天" % who)
        return True, ""
    except Exception as exc:
        log.warning("enter_chat_failed", who=who, error=str(exc), exc_info=True)
        return False, "打开聊天失败: %s" % exc


def _chat_region(layout: vision.Layout) -> tuple:
    """The message area of the conversation pane, used for before/after checks."""
    if layout.header:
        top = layout.header[1] + layout.header[3] + 4
    else:
        top = int(layout.height * 0.10)
    if layout.input_box:
        bottom = layout.input_box[1] - 8
    else:
        bottom = int(layout.height * 0.82)
    left = min(layout.width - 60, layout.chat_left + 6)
    right = layout.width - 8
    if bottom - top < 80:
        bottom = min(layout.height - 12, top + 240)
    return (left, top, max(left + 40, right), max(top + 40, bottom))


def _np():
    import numpy as np

    return np


def _cv2():
    import cv2

    return cv2


def _region_changed(before, after, rect, *, pixel_delta: int = 18) -> float:
    np = _np()
    cv2 = _cv2()
    x0, y0, x1, y1 = (int(v) for v in rect)
    a = np.asarray(before.convert("L"))[y0:y1, x0:x1]
    b = np.asarray(after.convert("L"))[y0:y1, x0:x1]
    if a.shape != b.shape or a.size == 0:
        return 0.0
    return float((cv2.absdiff(a, b) > pixel_delta).mean())


def _close_panel_if_open(sess: _Session, layout: vision.Layout) -> None:
    """Dismiss a popup left open by an earlier attempt, and wait for it to go.

    Clicking the emoji button toggles the popup, so clicking while one is still
    closing closes it again and the whole run fails for no good reason.
    """
    if vision.detect_panel(sess.frame, layout) is None:
        return

    log.info("sticker_panel_already_open_closing")
    deadline = time.time() + 2.5
    while time.time() < deadline:
        human.key_press(human.VK_ESCAPE)
        human.sleep_human(0.4, 0.3)
        sess.grab()
        if vision.detect_panel(sess.frame, layout) is None:
            log.info("sticker_panel_closed")
            break
    else:
        log.warning("sticker_panel_would_not_close")
    sess.layout(refresh=True)


def _await_panel(sess: _Session, layout: vision.Layout, before, *, timeout: float = _PANEL_TIMEOUT_S):
    """Poll until the popup shows up, preferring a detection that has a tab bar.

    A tab-less structural panel is usable -- _hunt falls back to the active tab
    -- so it is accepted after a short grace instead of waiting out the whole
    timeout.  The grace still gives a late tab bar a chance to appear.
    """
    deadline = time.time() + timeout
    fallback = None
    fallback_at = None
    while True:
        frame = sess.frame
        # Structural detection goes first: it reads the popup off the frame and
        # does not care whether the window shifted, whereas the difference
        # approach degrades badly the moment anything moves.
        panel = vision.detect_panel(frame, layout)
        if panel is not None and panel.tabs:
            return panel
        if panel is not None:
            fallback = panel
            if fallback_at is None:
                fallback_at = time.time()
        else:
            by_diff = vision.detect_panel_by_diff(before, frame, layout)
            if by_diff is not None and by_diff.tabs:
                return by_diff
            if by_diff is not None and fallback is None:
                fallback = by_diff
                fallback_at = time.time()
        now = time.time()
        if fallback is not None and fallback_at is not None and now - fallback_at >= _PANEL_TAB_GRACE_S:
            log.info("sticker_panel_accepted_without_tabs",
                     age=round(now - fallback_at, 3), timeout=timeout)
            return fallback
        if now >= deadline:
            return fallback
        human.sleep_human(0.28, 0.25)
        sess.grab()


def _scroll_panel(sess: _Session, panel: vision.Panel, *, up: bool, notches: int = 3) -> None:
    cx = (panel.content[0] + panel.content[2]) // 2
    cy = (panel.content[1] + panel.content[3]) // 2
    ax, ay = sess.to_screen((cx, cy))
    human.scroll(notches if up else -notches, at=(ax, ay))


def _tab_order(panel: vision.Panel) -> list:
    """Which tabs to try, most likely first.

    Tab 0 is always the search magnifier and is never clicked; the custom
    sticker collection lives at the right-hand end and is usually the active
    one, so the active tab is tried first and the rest follow right to left.
    """
    count = len(panel.tabs)
    if count == 0:
        return [None]
    order = []
    if 0 < panel.active_tab < count:
        order.append(panel.active_tab)
    for index in range(count - 1, 0, -1):
        if index not in order:
            order.append(index)
    return order[:_MAX_TAB_TRIES]


def _select_tab(sess: _Session, layout: vision.Layout, panel: vision.Panel, index):
    if index is None or not panel.tabs or index >= len(panel.tabs):
        return panel
    if index == panel.active_tab:
        return panel
    tab = panel.tabs[index]
    ax, ay = sess.to_screen((tab.cx_i, tab.cy_i))
    log.debug("sticker_tab_click", index=index, point=(tab.cx_i, tab.cy_i))
    human.click(ax, ay, aim_jitter=1.2)
    human.sleep_human(0.55)
    sess.grab()
    refreshed = vision.detect_panel(sess.frame, layout)
    return refreshed or panel


def _hunt(sess: _Session, layout: vision.Layout, panel: vision.Panel, templates: list) -> dict:
    """Search every tab and page of the opened popup for the wanted sticker."""
    trace = []
    best_seen = None

    for tab_index in _tab_order(panel):
        panel = _select_tab(sess, layout, panel, tab_index)
        if panel is None:
            trace.append({"tab": tab_index, "result": "panel_lost"})
            break

        _scroll_panel(sess, panel, up=True)
        previous = None

        for page in range(_MAX_SCROLL_PAGES):
            frame = sess.frame
            refreshed = vision.detect_panel(frame, layout)
            if refreshed is not None:
                panel = refreshed

            cells, meta = vision.detect_sticker_cells(_as_array(frame), panel)
            if cells:
                match = vision.find_sticker_in_panel(_as_array(frame), panel, templates,
                                                     cells=cells, grid_meta=meta)
                if match is not None:
                    margin = float(match.detail.get("margin") or 0.0)
                    trace.append({"tab": tab_index, "page": page,
                                  "score": round(match.score, 3), "margin": round(margin, 3)})
                    if best_seen is None or match.score > best_seen[0].score:
                        best_seen = (match, panel, cells, meta)
                    if _match_accepted(match.score, margin):
                        return {"ok": True, "match": match, "panel": panel,
                                "cells": cells, "grid": meta, "trace": trace}
            else:
                trace.append({"tab": tab_index, "page": page,
                              "cells": 0, "reason": meta.get("reason")})

            _scroll_panel(sess, panel, up=False)
            current = sess.frame
            if previous is not None and vision.images_similar(previous, current, threshold=0.995):
                break
            previous = current

    return {"ok": False, "match": best_seen[0] if best_seen else None,
            "panel": best_seen[1] if best_seen else panel,
            "cells": best_seen[2] if best_seen else [],
            "grid": best_seen[3] if best_seen else {},
            "trace": trace}


_HUNT_CACHE: dict = {"key": None, "cell": None}


def _hunt_geometry_key(panel, layout, frame_size) -> tuple:
    """Everything a cached cell depends on; any change forces a full hunt."""
    return (
        int(panel.x0), int(panel.y0), int(panel.x1), int(panel.y1),
        tuple(int(v) for v in panel.content),
        int(getattr(panel, "active_tab", -1) or -1),
        int(getattr(panel, "tab_y", 0) or 0),
        int(layout.chat_left),
        int(layout.toolbar_y),
        int(frame_size[0]), int(frame_size[1]),
    )


def _hunt_cached(sess, layout: vision.Layout, panel: vision.Panel, templates: list):
    """Try the remembered cell before the full tab/page sweep.

    The panel is scrolled back to the top exactly like the full hunt, then the
    remembered cell is matched against the template by itself.  Any geometry
    change, missing cell, or weak score returns None and the caller runs the
    full hunt, so a stale cache can only cost time, never click a wrong cell.
    """
    entry = _HUNT_CACHE
    if not entry.get("key") or entry["key"] != _hunt_geometry_key(panel, layout, sess.frame.size):
        return None
    target_row, target_col = entry["cell"]
    _scroll_panel(sess, panel, up=True)
    frame = sess.frame
    refreshed = vision.detect_panel(frame, layout)
    if refreshed is None:
        return None
    cells, meta = vision.detect_sticker_cells(_as_array(frame), refreshed)
    target = next((c for c in cells if (c.row, c.col) == (target_row, target_col)), None)
    if target is None:
        return None
    match = vision.find_sticker_in_panel(_as_array(frame), refreshed, templates,
                                         cells=[target], grid_meta=meta)
    if match is None:
        return None
    margin = float(match.detail.get("margin") or 0.0)
    if not _match_accepted(match.score, margin) or match.score < max(_MIN_MATCH_SCORE, 0.65):
        return None
    return {"ok": True, "match": match, "panel": refreshed, "cells": cells,
            "grid": meta, "trace": [{"cached": True, "score": round(match.score, 3)}]}


def _as_array(image):
    return vision._as_rgb(image)


def _read_chat_title(sess: "_Session", layout: vision.Layout) -> str:
    """OCR the conversation header, which names whoever's chat is open."""
    if not layout.header:
        return ""
    hx, hy, hw, hh = layout.header
    try:
        crop = sess.frame.crop((hx, hy, hx + min(hw, 560), hy + hh))
        items = vision.ocr_items(crop)
    except Exception as exc:
        log.debug("chat_title_ocr_failed", error=str(exc))
        return ""
    return " ".join(text for text, *_rest in items).strip()


def _title_matches(opened: str, expected: str) -> bool:
    from core.execution import wx_chat

    return wx_chat.title_matches(opened, expected)


@_ui_locked
def open_sticker_panel(win: Optional[vision.WindowInfo] = None, *,
                       expect_chat: str = "", raw_who: str = "",
                       frame=None, layout=None, title_verified: bool = False,
                       reused: bool = False) -> dict:
    """Bring WeChat forward, click the emoji button and describe the popup.

    Callers that already hold a freshly captured frame (the active-chat reuse
    path) may pass it in.  When ``title_verified`` is true that exact frame was
    already title-checked, so the OCR is skipped; false simply means the check
    still has to run here, before anything is clicked.

    Shared by the sender and the diagnostics CLI so both exercise exactly the
    same code path.  Returns a dict with ok, session, layout, panel, before.
    """
    from core.execution import wx_chat

    if win is None:
        win = vision.find_wechat_window()
    if win is None:
        return {"ok": False, "error": "未找到微信窗口，请确认微信 PC 客户端已启动并登录"}

    vision.activate_window(win)
    if frame is None or layout is None:
        frame, layout = vision.claim_window_frame(win)
    else:
        # A caller-supplied frame is only valid while this window is still in
        # front and still the same size.  A move is harmless (window-local
        # coordinates stay valid), a resize is not, so re-claim on any doubt.
        try:
            win.refresh()
        except Exception:
            warn("core/execution/wx_sticker_sender.py:672 Exception", err=repr(__import__("sys").exc_info()[1]))
        if (not win.is_foreground()
                or (int(win.width), int(win.height)) != (int(frame.size[0]), int(frame.size[1]))):
            frame, layout = vision.claim_window_frame(win)

    if not vision.frame_looks_like_wechat(layout):
        wx_chat.invalidate_active_chat("panel_frame_not_wechat")
        return {"ok": False, "error":
                "无法确认画面是微信聊天窗口（可能被其他窗口遮挡）。"
                "当前前台窗口句柄 %s，微信窗口句柄 %s。"
                % (vision.foreground_hwnd(), win.hwnd)}
    if layout.emoji is None:
        wx_chat.invalidate_active_chat("panel_emoji_missing")
        return {"ok": False, "error":
                "未能在微信窗口中定位输入栏的表情按钮（请确认微信停在聊天界面）"}

    sess = _Session(win, frame=frame, layout=layout)

    # Confirm who we are actually talking to before touching anything.  The
    # contact search can land on the wrong row, and a sticker sent to the wrong
    # person cannot be taken back.  Only a remembered entry that was already
    # checked against this exact header may skip the OCR.
    if expect_chat and not title_verified:
        title = _read_chat_title(sess, layout)
        if title and not _title_matches(title, expect_chat):
            wx_chat.invalidate_active_chat("title_mismatch")
            return {"ok": False, "session": sess, "layout": layout, "retry_search": True,
                    "error": "当前打开的聊天是“%s”，与目标“%s”不符，已中止发送"
                             % (title, expect_chat)}
        if not title:
            log.warning("sticker_chat_title_unreadable", expect=expect_chat, reused=reused)
            if reused:
                # Skipping the search is only acceptable when the title can be
                # read back; refuse and let the caller run the full search.
                wx_chat.invalidate_active_chat("reused_title_unreadable")
                return {"ok": False, "session": sess, "layout": layout, "retry_search": True,
                        "error": "复用的聊天标题无法读取，已退回完整搜索"}
        else:
            wx_chat.remember_active_chat(expect_chat, raw_who, win, sess.frame, layout,
                                         title_verified=True)

    _close_panel_if_open(sess, layout)
    before = sess.grab()
    layout = sess.layout(refresh=True)
    log.info("sticker_layout_ready", chat_left=layout.chat_left,
             emoji=layout.emoji_point(), notes=layout.notes)

    ax, ay = sess.to_screen(layout.emoji_point())
    human.click(ax, ay, aim_jitter=1.5)
    panel = _await_panel(sess, layout, before)
    if panel is None:
        wx_chat.invalidate_active_chat("panel_not_opened")
        return {"ok": False, "session": sess, "layout": layout, "before": before,
                "error": "点击表情按钮后没有出现表情面板（可能点击位置不准）"}

    log.info("sticker_panel_opened", panel=panel.as_dict())
    return {"ok": True, "session": sess, "layout": layout, "panel": panel, "before": before}


# --------------------------------------------------------------------------------------
# Public entry point
# --------------------------------------------------------------------------------------

@_ui_locked
def send_sticker(who: str, mood_id: str, emotion: str = "", platform: str = "wechat",
                 account_id: str = "", dry_run: bool = False) -> dict:
    """Send a sticker to the given contact using simulated mouse and keyboard only.

    With dry_run the whole flow runs -- the conversation is opened, the title is
    verified, the popup is opened and the sticker is located -- but the final
    click is skipped and the popup is dismissed.  Nothing reaches the other
    side, which is what makes it safe to rehearse against a real person or a
    real group before trusting the automation with them.
    """
    from core import features as _features

    if not dry_run and _features.dry_run_send(str(who or "")):
        log.info("dry_run_send", platform=platform, who=who, kind="sticker",
                 mood_id=mood_id, emotion=emotion)
        return {"ok": True, "dry_run": True}
    started = time.time()
    log.info("wx_send_sticker_start", who=who, mood_id=mood_id, emotion=emotion,
             platform=platform, account_id=account_id)

    entry = resolve_sticker_entry(mood_id, emotion, platform=platform, account_id=account_id)
    if entry and entry.get("error"):
        log.info("sticker_entry_refused", who=who, mood_id=mood_id, emotion=emotion,
                 error=entry["error"])
        return {"ok": False, "error": entry["error"],
                "candidates": entry.get("candidates")}
    if not entry:
        data_dir = _sticker_data_dir(platform, account_id)
        return {"ok": False, "error":
                "未找到表情包配置: mood_id=%s, emotion=%s。请在 %s 中配置，并把图片放进 %s"
                % (mood_id, emotion, data_dir / "sticker_index.json",
                   data_dir / "sticker_templates")}

    templates = _entry_templates(entry, mood_id, platform, account_id)
    log.info("sticker_entry_resolved", key=entry.get("key"),
             images=[Path(t).name for t in templates],
             using_thumb=any(_is_bound_thumb(t) for t in templates))

    win = vision.find_wechat_window()
    if win is None:
        return {"ok": False, "error": "未找到微信窗口，请确认微信 PC 客户端已启动并登录"}
    vision.activate_window(win)

    from core.execution import wx_chat

    if account_id:
        wx_chat.set_account_id(account_id)

    expect_name = who
    try:
        expect_name = wx_chat.resolve_nickname(who) or who
    except Exception:
        warn("core/execution/wx_sticker_sender.py:788 Exception", err=repr(__import__("sys").exc_info()[1]))

    # A title-verified active-chat memory skips the search and the title OCR
    # entirely; an unverified entry may skip only the search, because
    # open_sticker_panel still runs the title check before the first click.
    # Any doubt falls through to the full search, so a stale memory can never
    # send a sticker to the wrong conversation.
    reuse = None
    try:
        reuse = wx_chat.try_reuse_chat(expect_name, raw_who=who, win=win,
                                       allow_unverified=True)
    except Exception:
        log.warning("sticker_chat_reuse_failed", who=who, exc_info=True)
        wx_chat.invalidate_active_chat("reuse_exception")

    opened = None
    if reuse is not None:
        opened = open_sticker_panel(win, expect_chat=expect_name, raw_who=who,
                                    frame=reuse.get("frame"),
                                    layout=reuse.get("layout"),
                                    title_verified=bool(reuse.get("title_verified")),
                                    reused=True)
        if opened.get("ok"):
            pass  # noqa: silent（有意忽略，见 CONTRACT 第八条）
        elif opened.get("retry_search"):
            log.warning("sticker_reuse_rejected", who=who, error=opened.get("error"))
            wx_chat.invalidate_active_chat("reuse_rejected")
            opened = None
        else:
            wx_chat.invalidate_active_chat("reuse_panel_failed")
            return {"ok": False, "error": opened.get("error") or "打开表情面板失败"}

    if opened is None:
        # Opening a contact is a two-step guess: OCR reads the result list, then
        # a click lands on a row that is still being redrawn.  Retrying the whole
        # search is far more reliable than trying to make one attempt perfect,
        # and the title check makes each retry safe.
        for attempt in range(_MAX_CHAT_ATTEMPTS):
            ok, err = _enter_chat(win, who)
            if not ok:
                wx_chat.invalidate_active_chat("enter_chat_failed")
                return {"ok": False, "error": err}
            human.sleep_human(0.55)

            opened = open_sticker_panel(win, expect_chat=expect_name, raw_who=who)
            if opened["ok"]:
                break

            error = str(opened.get("error") or "")
            if not opened.get("retry_search") and "不符" not in error:
                wx_chat.invalidate_active_chat("panel_failed")
                return {"ok": False, "error": error}

            log.warning("sticker_wrong_chat_retry", attempt=attempt + 1, error=error)
            wx_chat.invalidate_active_chat("title_mismatch")
            human.key_press(human.VK_ESCAPE)
            human.sleep_human(0.4, 0.3)
        else:
            wx_chat.invalidate_active_chat("open_chat_retries_exhausted")
            return {"ok": False,
                    "error": (opened or {}).get("error") or "多次尝试后仍无法打开目标聊天"}

    sess = opened["session"]
    layout = opened["layout"]
    panel = opened["panel"]
    before_frame = opened["before"]
    chat_rect = _chat_region(layout)

    hunt = _hunt_cached(sess, layout, panel, templates)
    if hunt is None:
        hunt = _hunt(sess, layout, panel, templates)
    if not hunt["ok"]:
        human.key_press(human.VK_ESCAPE)
        best = hunt.get("match")
        detail = ("最佳相似度 %.2f，低于阈值 %.2f" % (best.score, _MIN_MATCH_SCORE)) if best \
            else "没有可比对的候选格"
        mode = _sticker_candidates_mode()
        outcome, why = ("", "")
        if mode != "off":
            outcome, why = _hunt_outcome(hunt, panel)
            log.info("sticker_hunt_outcome", mode=mode, outcome=outcome, why=why,
                     who=who, mood_id=mood_id, emotion=emotion,
                     best_score=round(best.score, 3) if best is not None else None,
                     pages=len([s for s in (hunt.get("trace") or []) if "page" in s]),
                     tabs=len(list(getattr(panel, "tabs", ()) or ())))
        if mode == "on":
            return {"ok": False,
                    "error": _hunt_failure_text(hunt, panel, outcome, why, detail),
                    "hunt_outcome": outcome, "trace": hunt.get("trace"),
                    "panel": panel.as_dict()}
        return {"ok": False, "error": "在表情面板中未找到足够匹配的表情包（%s）" % detail,
                "trace": hunt.get("trace"), "panel": panel.as_dict()}

    match = hunt["match"]
    cell = match.cell
    point = match.point
    jitter = max(2.0, min(cell.size) * 0.06) if cell else 3.0
    if cell is not None:
        # Remember this cell only with the exact geometry it was found in; the
        # key is re-checked before any cached hunt so a resize invalidates it.
        _HUNT_CACHE["key"] = _hunt_geometry_key(hunt.get("panel") or panel, layout,
                                                sess.frame.size)
        _HUNT_CACHE["cell"] = (cell.row, cell.col)

    log.info("sticker_click", point=point, score=round(match.score, 3),
             margin=round(float(match.detail.get("margin") or 0), 3),
             cell=(cell.row, cell.col) if cell else None)

    if dry_run:
        click_x, click_y = sess.to_screen(point)
        human.key_press(human.VK_ESCAPE)
        human.sleep_human(0.4)
        log.info("wx_sticker_dry_run", who=who, mood_id=mood_id,
                 score=round(match.score, 3), point=point)
        return {
            "ok": True,
            "dry_run": True,
            "who": who,
            "mood_id": mood_id,
            "label": entry.get("label", ""),
            "chat_title": expect_name,
            "score": round(match.score, 3),
            "margin": round(float(match.detail.get("margin") or 0), 3),
            "cell": [cell.row, cell.col] if cell else None,
            "would_click_screen": [click_x, click_y],
            "elapsed": round(time.time() - started, 2),
        }

    # Last-moment defence: the user (or another automation) can switch the
    # conversation after the title check but before this click.  The fresh grab
    # also refreshes win.rect, so a moved window gets fresh screen coordinates;
    # a resized window aborts because the cell refers to the old layout.
    try:
        guard_frame = sess.grab()
        guard_layout = vision.detect_layout(guard_frame)
        guard = wx_chat.match_active_chat(expect_name, who, win, guard_frame, guard_layout)
    except Exception as exc:
        log.warning("sticker_chat_guard_failed", who=who, error=str(exc))
        wx_chat.invalidate_active_chat("guard_capture_failed")
        human.key_press(human.VK_ESCAPE)
        return {"ok": False, "error": "发送前无法重新确认聊天窗口，已中止发送"}
    if guard is False:
        human.key_press(human.VK_ESCAPE)
        log.warning("sticker_chat_changed_before_click", who=who)
        return {"ok": False, "error": "发送前检测到聊天窗口已切换，已中止发送"}
    if (int(layout.width), int(layout.height)) != (int(guard_frame.size[0]),
                                                  int(guard_frame.size[1])):
        human.key_press(human.VK_ESCAPE)
        log.warning("sticker_window_resized_before_click", who=who,
                    was=(layout.width, layout.height), now=guard_frame.size)
        return {"ok": False, "error": "发送前窗口尺寸变化，已中止发送"}
    click_x, click_y = sess.to_screen(point)

    human.click(click_x, click_y, aim_jitter=jitter)
    human.sleep_human(0.75)

    after_frame = sess.grab()   # must be a fresh grab: the cached frame predates the click
    still_open = vision.detect_panel(after_frame, layout) is not None
    changed = _region_changed(before_frame, after_frame, chat_rect)

    if still_open:
        log.warning("sticker_send_panel_still_open", point=point)
        human.key_press(human.VK_ESCAPE)
        return {"ok": False,
                "error": "点击表情后表情面板没有关闭，可能没有点中（相似度 %.2f）" % match.score,
                "trace": hunt.get("trace")}

    elapsed = round(time.time() - started, 2)
    log.info("wx_sticker_sent", who=who, mood_id=mood_id, label=entry.get("label"),
             score=round(match.score, 3), chat_changed=round(changed, 4), elapsed=elapsed)

    return {
        "ok": True,
        "mood_id": mood_id,
        "label": entry.get("label", ""),
        "template": match.template,
        "score": round(match.score, 3),
        "margin": round(float(match.detail.get("margin") or 0), 3),
        "cell": [cell.row, cell.col] if cell else None,
        "chat_changed": round(changed, 4),
        "elapsed": elapsed,
        "who": who,
    }


# --------------------------------------------------------------------------------------
# Thumbnail learning
# --------------------------------------------------------------------------------------

@_ui_locked
def capture_panel_thumbs(platform: str = "wechat", account_id: str = "", *,
                         max_pages: int = 12) -> dict:
    """Sweep the sticker popup and save every thumbnail as a future template.

    A learned thumbnail is a pixel-exact copy of what WeChat draws, so matching
    against it is dramatically more reliable than matching a full-resolution
    source image.  This is what turns one successful send into a dependable
    lookup for every send after it.
    """
    from PIL import Image

    opened = open_sticker_panel()
    if not opened["ok"]:
        return {"ok": False, "error": opened["error"]}
    sess = opened["session"]
    layout = opened["layout"]
    panel = opened["panel"]

    out_dir = _sticker_thumbs_dir(platform, account_id)
    out_dir.mkdir(parents=True, exist_ok=True)

    saved = []
    for tab_index in _tab_order(panel):
        panel = _select_tab(sess, layout, panel, tab_index)
        if panel is None:
            break
        _scroll_panel(sess, panel, up=True)
        previous = None
        for page in range(max_pages):
            frame = sess.frame
            refreshed = vision.detect_panel(frame, layout)
            if refreshed is not None:
                panel = refreshed
            arr = _as_array(frame)
            cells, _meta = vision.detect_sticker_cells(arr, panel)
            for cell in cells:
                crop = arr[cell.y0:cell.y1, cell.x0:cell.x1]
                if crop.size == 0:
                    continue
                digest = "%08x" % (abs(hash(crop.tobytes())) & 0xFFFFFFFF)
                name = "t%d_p%d_r%dc%d_%s.png" % (tab_index, page, cell.row, cell.col, digest)
                path = out_dir / name
                Image.fromarray(crop).save(str(path))
                saved.append({"path": str(path), "tab": tab_index, "page": page,
                              "row": cell.row, "col": cell.col})
            _scroll_panel(sess, panel, up=False)
            current = sess.frame
            if previous is not None and vision.images_similar(previous, current, threshold=0.995):
                break
            previous = current

    human.key_press(human.VK_ESCAPE)
    (out_dir / "gallery.json").write_text(
        json.dumps({"thumbs": saved}, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("sticker_thumbs_captured", count=len(saved), dir=str(out_dir))
    return {"ok": True, "count": len(saved), "dir": str(out_dir), "thumbs": saved}


# --------------------------------------------------------------------------------------
# Binding learned thumbnails to moods
# --------------------------------------------------------------------------------------

def _crop_match_score(crop_rgb, template_rgb, *, pad_fraction: float = 0.30) -> float:
    """Best correlation between one saved crop and one source template.

    A crop is a single cell, so a template resized to the wrong pixel or two
    would have almost nowhere to slide and the score would collapse.  Padding
    the crop with the popup's white and sweeping a small scale range restores
    the search room the real panel provides.
    """
    np = _np()
    h, w = int(crop_rgb.shape[0]), int(crop_rgb.shape[1])
    if h < 8 or w < 8:
        return 0.0
    pad = max(6, int(max(h, w) * pad_fraction))
    canvas = np.full((h + 2 * pad, w + 2 * pad, 3), 255, np.uint8)
    canvas[pad:pad + h, pad:pad + w] = crop_rgb
    targets = [max(h, w) * factor for factor in (1.02, 0.95, 0.88, 0.81, 0.74)]
    hits = vision.match_template_multiscale(canvas, template_rgb, targets)
    return float(hits[0]["score"]) if hits else 0.0


def assign_thumbs(index: dict, gallery: list, *, min_score: float = 0.5,
                  min_margin: float = 0.06, root: Path | None = None) -> dict:
    """Greedily pair each mood with the panel crop it matches best.

    Implemented as a global greedy assignment rather than per-mood argmax: two
    look-alike stickers from the same series would otherwise both claim the same
    crop, and the second one would silently point at the wrong picture.
    """
    from PIL import Image

    _np()
    _cv2()
    templates_dir_hint = None
    scores = []

    for mood in index:
        images = _entry_images(index[mood], mood, Path("."), root=root)
        templates_dir_hint = templates_dir_hint or images
        for path in images:
            if _is_bound_thumb(path):
                continue  # already bound
            template = vision.load_template_rgb(path)
            if template is None:
                continue
            for position, crop_entry in enumerate(gallery):
                crop = crop_entry.get("_array")
                if crop is None:
                    continue
                score = _crop_match_score(crop, template)
                if score <= 0.0:
                    continue
                scores.append({"mood": mood, "template": path, "gallery": position,
                               "score": score, "margin": 0.0})

    # Margin is measured against the next best crop *for the same mood*, which
    # is what tells a confident pairing apart from a guess between look-alikes.
    by_mood = {}
    for item in scores:
        by_mood.setdefault(item["mood"], []).append(item)
    for items in by_mood.values():
        items.sort(key=lambda entry: -entry["score"])
        for rank, entry in enumerate(items):
            entry["margin"] = entry["score"] - (items[rank + 1]["score"] if rank + 1 < len(items) else 0.0)

    scores.sort(key=lambda item: -item["score"])
    used_moods = set()
    used_gallery = set()
    assignment = {}
    for item in scores:
        if item["mood"] in used_moods or item["gallery"] in used_gallery:
            continue
        if item["score"] < min_score or item["margin"] < min_margin:
            continue
        used_moods.add(item["mood"])
        used_gallery.add(item["gallery"])
        assignment[item["mood"]] = item
    return assignment


def bind_thumbs(platform: str = "wechat", account_id: str = "", *,
                min_score: float = 0.55, write: bool = True) -> dict:
    """Point each mood at the learned panel crop that matches it best.

    Matching a full-resolution source image against WeChat's downscaled
    thumbnail is workable but marginal (measured 0.50-0.99 with the worst cases
    close to the rejection floor).  A learned crop is the same pixels WeChat
    painted, so it matches at roughly 0.95 and lifts the whole pipeline out of
    the ambiguous band.
    """
    from PIL import Image

    from db import sticker_store

    store = sticker_store.StickerStore(platform, account_id)
    if not store.exists():
        return {"ok": False, "error": "找不到 %s" % store.registry_path}

    stickers = store.load()
    if not stickers:
        return {"ok": False, "error": "%s 里没有任何表情" % store.registry_path}
    index = {emotion: s.to_registry() for emotion, s in stickers.items()}

    thumbs_dir = _sticker_thumbs_dir(platform, account_id)
    gallery_path = thumbs_dir / "gallery.json"
    if not gallery_path.exists():
        return {"ok": False, "error": "还没有采集过缩略图，先运行 thumbs 命令"}

    try:
        gallery_meta = json.loads(gallery_path.read_text(encoding="utf-8"))
    except Exception as exc:
        warn("core/execution/wx_sticker_sender.py:1150 Exception", err=repr(exc))
        return {"ok": False, "error": "读取 gallery.json 失败: %s" % exc}

    gallery = []
    for entry in gallery_meta.get("thumbs", []):
        path = Path(entry.get("path", ""))
        if not path.exists():
            continue
        try:
            with Image.open(str(path)) as handle:
                entry = dict(entry)
                entry["_array"] = vision._as_rgb(handle.convert("RGB"))
                gallery.append(entry)
        except Exception:
            warn("core/execution/wx_sticker_sender.py:1163 Exception", err=repr(__import__("sys").exc_info()[1]))
            continue

    if not gallery:
        return {"ok": False, "error": "缩略图目录里没有可用的图片"}

    assignment = assign_thumbs(index, gallery, min_score=min_score, root=store.root)
    applied = []
    for mood, item in assignment.items():
        # Stored relative to the sticker root, like every other path here.
        relative = "thumbs/" + Path(gallery[item["gallery"]]["path"]).name
        if write:
            store.upsert(mood, thumb=relative)
        applied.append({"mood": mood, "thumb": relative,
                        "score": round(float(item["score"]), 3)})

    unbound = [m for m in index if m not in assignment]

    log.info("sticker_thumbs_bound", bound=len(applied), unbound=len(unbound),
             gallery=len(gallery))
    return {"ok": bool(applied), "bound": len(applied), "unbound": unbound,
            "gallery": len(gallery), "applied": applied,
            "index": str(store.registry_path)}


# --------------------------------------------------------------------------------------
# Compatibility shims for older debug scripts
# --------------------------------------------------------------------------------------

def _get_wechat_rect():
    win = vision.find_wechat_window()
    return None if win is None else win.rect


def _screenshot_region(left, top, right, bottom):
    return vision.capture((left, top, right, bottom))


def _get_wechat_hwnd() -> int:
    win = vision.find_wechat_window()
    return 0 if win is None else win.hwnd


def _click_at(x: int, y: int) -> None:
    human.click(x, y)


def _find_emoji_button(*_args, **_kwargs):
    win = vision.find_wechat_window()
    if win is None:
        return None
    image, _rect = vision.capture_window(win)
    return vision.detect_layout(image).emoji_point()
