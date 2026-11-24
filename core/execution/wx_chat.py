"""Finding a contact and opening their conversation.

This is the step both senders need and neither owns: text sending and sticker
sending differ only in what happens once the conversation is on screen.  Keeping
it here means wx_sticker_sender does not have to reach into the text sender (and
its rate limiter, and its singleton) just to get a chat open.

Everything in here drives the real UI through wx_vision and wx_human; nothing is
injected into WeChat and no accessibility API is used.
"""

from __future__ import annotations
import re as _re

import functools
import os
import threading
import time

import structlog

from core.execution import wx_vision
from core.infra.envflags import flag
from core.infra import skips
from safelog import warn

log = structlog.get_logger()

__all__ = [
    "set_account_id", "resolve_nickname", "canonical_chat_id",
    "ensure_foreground", "type_text",
    "press_enter", "press_escape", "press_down", "get_ocr", "open_chat",
    "try_reuse_chat", "remember_active_chat", "match_active_chat",
    "confirm_active_chat", "invalidate_active_chat", "ui_locked",
    "text_verify_on_open", "title_matches", "verify_active_chat_title",
    "guard_active_chat", "message_box_has_text", "focus_message_box",
]


_OCR = None
_NICKNAME_CACHE: dict[str, str] = {}
_ACCOUNT_ID: str = ""

# Window lookup walks every top-level window and the process table, so it is
# cached briefly; is_connected is polled far more often than the window moves.
_WINDOW_CACHE: dict = {"at": 0.0, "win": None}
_WINDOW_TTL = 1.5

# Debug evidence (click screenshots, OCR crops) costs 0.3-0.5s per send and can
# contain private chat content.  It stays off unless explicitly requested.
_DEBUG_DUMPS = flag("WX_DEBUG_DUMPS")

# Every sender drives the same physical mouse and keyboard.  Two concurrent
# flows interleave clicks and leave the window in a state neither flow can
# parse -- which is exactly how "text reply, then sticker" broke: the sticker
# task started while the text send was still typing.  All UI-driving entry
# points take this process-wide re-entrant lock.  RLock because open_chat and
# open_sticker_panel are also called from inside an already-locked send.
UI_LOCK = threading.RLock()


def ui_locked(func):
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        # 2026-10-27 探针：UI_LOCK 与收消息的 OCR 轮询共用，发送可能排在别人后面。
        # 这里只记「排队等了多久」，不改行为；连续 wait 很大就说明锁被占住了。
        _t_wait = time.time()
        with UI_LOCK:
            _waited = time.time() - _t_wait
            if _waited > 0.4:
                try:
                    log.info("wx_ui_lock_wait",
                             func=getattr(func, "__name__", "?"),
                             wait_s=round(_waited, 2))
                except Exception:
                    pass
            return func(*args, **kwargs)

    return wrapper

# --------------------------------------------------------------------------------------
# Active conversation memory
# --------------------------------------------------------------------------------------
#
# Opening a conversation costs a search, a paste, and OCR (the search-result
# check, plus the title check for stickers).  When the next send goes to the
# same conversation, the window already shows it and none of that needs to be
# repeated.  The memory is deliberately visual: it stores the header pixels
# captured right after the conversation was opened, tied to the identity the
# caller asked for.  A later call may skip the search only when the current
# header is still the same picture.  Different who, different window, expired
# entry, changed pixels -- every other case falls back to the full search.  A
# stale memory can therefore cost an extra ~80ms check, never a wrong chat.
_ACTIVE_LOCK = threading.Lock()
_ACTIVE_CHAT: dict = {"at": 0.0}


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except (TypeError, ValueError):
        warn("core/execution/wx_chat.py:88 (TypeError, ValueError)", err=repr(__import__("sys").exc_info()[1]))
        return default


_ACTIVE_ENABLED = str(os.environ.get("WX_CHAT_REUSE", "1")).strip().lower() not in {
    "0", "false", "no", "off",
}
# Soft idle threshold.  Exceeding it only triggers a fresh fingerprint
# re-probe; it no longer forces a contact search by itself.
_ACTIVE_TTL_S = max(1.0, _env_float("WX_CHAT_REUSE_TTL", 300.0))
# After a text conversation is opened, run the same title check the sticker
# path uses so later sends can reuse it safely.  Set WX_TEXT_VERIFY_ON_OPEN=0
# to restore the old "no extra OCR, no text reuse" behaviour.
_TEXT_VERIFY_ON_OPEN = str(os.environ.get("WX_TEXT_VERIFY_ON_OPEN", "1")).strip().lower() not in {
    "0", "false", "no", "off",
}
# A different chat measured ~3.8% changed pixels with mean difference ~6.
# These bounds sit far below that while still tolerating a small unread badge
# (a 10x14 badge is ~0.17% of a 1132x71 header), so a small-but-real change
# cannot slip through as "same header".
_ACTIVE_MEAN_DIFF = max(0.0, _env_float("WX_CHAT_REUSE_MEAN_DIFF", 1.5))
_ACTIVE_CHANGED_FRAC = min(1.0, max(0.0, _env_float("WX_CHAT_REUSE_CHANGED_FRAC", 0.002)))
# The fingerprint proves the physical chat window; keep a generous hard cap so
# a truly ancient memory can never be reused after a process/layout anomaly.
_ACTIVE_HARD_TTL_S = max(60.0, _env_float("WX_CHAT_REUSE_HARD_TTL", 86400.0))

_CHAT_ID_CACHE: dict[tuple[str, str], str] = {}
# 2026-10-31 提速：解析失败（account DB 管理器起不来 / 查不到唯一命中）原来**不缓存**，
# 于是每次调用都重新初始化一遍终端层（实测 1–7 秒/次，一次发送里会被调好几次）。
# 失败结果按 TTL 记着，过期再试。
_CHAT_ID_NEG: dict[tuple[str, str], float] = {}
_CHAT_ID_NEG_TTL_S = 120.0


def _looks_like_chat_id(value: str) -> bool:
    value = (value or "").strip()
    return bool(value) and (
        value.endswith("@chatroom")
        or value.startswith("wxid_")
        or value.startswith("v1_")
    )


def canonical_chat_id(who: str) -> str:
    """Resolve a display name to its unique wxid/chatroom id when possible.

    Some callers (notably the LLM tool layer) pass a remark/nickname such as
    "Elaina", while the text-reply path passes the raw wxid.  The active-chat
    memory is keyed by identity, so without this normalization the same visual
    conversation looks like two different chats and the second caller falls
    back to a full search.  Ambiguous names are deliberately left unresolved;
    searching by display name is then still safe.
    """
    value = (who or "").strip()
    if not value or _looks_like_chat_id(value):
        return value
    cache_key = (_ACCOUNT_ID, value)
    cached = _CHAT_ID_CACHE.get(cache_key)
    if cached:
        return cached
    _neg_until = _CHAT_ID_NEG.get(cache_key) or 0.0
    if _neg_until and time.time() < _neg_until:
        return ""
    try:
        from core.terminal.store_port import get_account_db_manager

        adm = get_account_db_manager()
        if not adm:
            _CHAT_ID_NEG[cache_key] = time.time() + _CHAT_ID_NEG_TTL_S
            return ""
        candidates: set[str] = set()
        repos = []
        if _ACCOUNT_ID:
            repo = adm.get_repo("wechat", _ACCOUNT_ID)
            if repo:
                repos.append(repo)
        for (plat, _aid), repo in adm.all_repos().items():
            if plat != "wechat" or repo in repos:
                continue
            repos.append(repo)
        for repo in repos:
            for contact in repo.contacts.search_by_name(value, limit=20):
                names = {
                    (contact.nickname or "").strip(),
                    (contact.remark or "").strip(),
                }
                if value in names:
                    candidates.add(contact.wxid)
        if len(candidates) == 1:
            resolved = next(iter(candidates))
            _CHAT_ID_CACHE[cache_key] = resolved
            _CHAT_ID_NEG.pop(cache_key, None)
            log.info("wx_chat_id_resolved", who=value, chat_id=resolved,
                     account_id=_ACCOUNT_ID)
            return resolved
    except Exception as exc:
        _CHAT_ID_NEG[cache_key] = time.time() + _CHAT_ID_NEG_TTL_S
        log.debug("wx_chat_id_resolve_failed", who=value, error=str(exc)[:120])
    else:
        # 查了但没唯一命中：同样记一段时间，别每次重来。
        _CHAT_ID_NEG[cache_key] = time.time() + _CHAT_ID_NEG_TTL_S
    return ""


def _active_identity(display: str, raw_who: str = "") -> tuple:
    raw = (raw_who or "").strip()
    if raw and not _looks_like_chat_id(raw):
        raw = canonical_chat_id(raw) or raw
    elif not raw and display:
        raw = canonical_chat_id(display)
    return (raw, (display or "").strip())


def _header_print(frame, layout):
    """((x, y, w, h), RGB ndarray) for the conversation header, or Nones."""
    if frame is None or layout is None:
        return None, None
    header = getattr(layout, "header", None)
    if not header:
        return None, None
    try:
        x, y, w, h = (int(v) for v in header)
    except (TypeError, ValueError):
        warn("core/execution/wx_chat.py:196 (TypeError, ValueError)", err=repr(__import__("sys").exc_info()[1]))
        return None, None
    if w <= 4 or h <= 4:
        return None, None
    try:
        # The full header rect, not the 560px OCR crop: more pixels make a
        # collision between two rendered titles even less likely.  RGB rather
        # than luminance keeps two chats with the same name but different
        # avatars distinguishable.
        crop = frame.crop((x, y, x + w, y + h))
        if crop.mode != "RGB":
            crop = crop.convert("RGB")
    except Exception:
        warn("core/execution/wx_chat.py:208 Exception", err=repr(__import__("sys").exc_info()[1]))
        return None, None
    import numpy as np

    array = np.asarray(crop, dtype="uint8")
    if array.size == 0:
        return None, None
    return (x, y, w, h), array


def _print_matches(candidate, remembered) -> bool:
    if candidate is None or remembered is None:
        return False
    if candidate.shape != remembered.shape or candidate.size == 0:
        return False
    import numpy as np

    diff = np.abs(candidate.astype("int16") - remembered.astype("int16"))
    return bool(float(diff.mean()) <= _ACTIVE_MEAN_DIFF
                and float((diff > 12).mean()) <= _ACTIVE_CHANGED_FRAC)


def invalidate_active_chat(reason: str = "") -> None:
    """Drop the remembered conversation; callers fall back to a full search."""
    with _ACTIVE_LOCK:
        had = bool(_ACTIVE_CHAT.get("at"))
        _ACTIVE_CHAT.clear()
        _ACTIVE_CHAT["at"] = 0.0
    if had:
        log.info("wx_active_chat_invalidated", reason=reason)


def remember_active_chat(display: str, raw_who: str, win, frame, layout, *,
                         title_verified: bool) -> bool:
    """Store the current header as the anchor for this conversation."""
    if not _ACTIVE_ENABLED:
        return False
    header, fingerprint = _header_print(frame, layout)
    if fingerprint is None or win is None:
        return False
    with _ACTIVE_LOCK:
        _ACTIVE_CHAT.clear()
        _ACTIVE_CHAT.update({
            "at": time.time(),
            "identity": _active_identity(display, raw_who),
            "display": display,
            "raw_who": raw_who,
            "hwnd": int(win.hwnd),
            "pid": int(getattr(win, "pid", 0) or 0),
            "header": header,
            "fingerprint": fingerprint,
            "title_verified": bool(title_verified),
        })
    log.info("wx_active_chat_remembered", who=raw_who or display,
             title_verified=bool(title_verified))
    return True


def active_chat_id(*, max_age_s: float = _ACTIVE_TTL_S) -> str:
    """最近一次我们打开/确认过的会话 id——0.9 档证据（"还是我们自己开的那个"）。

    过期、没开过、或锚点里没有可用身份时返回空串：宁可不说话，也不冒充。
    """
    if not _ACTIVE_ENABLED:
        return ""
    with _ACTIVE_LOCK:
        data = dict(_ACTIVE_CHAT)
    if not data:
        return ""
    try:
        age = time.time() - float(data.get("at") or 0)
    except (TypeError, ValueError):
        warn("core/execution/wx_chat.py:279 (TypeError, ValueError)", err=repr(__import__("sys").exc_info()[1]))
        return ""
    if age > float(max_age_s):
        return ""
    raw = str(data.get("raw_who") or "") or str(data.get("display") or "")
    return canonical_chat_id(raw) if raw else ""


def try_reuse_chat(display: str, raw_who: str = "", *, win=None,
                   allow_unverified: bool = False):
    """Return a frame/layout bundle when we are provably still in this chat.

    The caller must either accept an already title-verified entry, or be a
    caller that will run the title check itself before touching the UI
    (``allow_unverified=False``).  Every failed check returns None and the
    caller follows the normal search path, so a stale memory can only make a
    send slower, never send to the wrong person.
    """
    if not _ACTIVE_ENABLED:
        return None
    with _ACTIVE_LOCK:
        entry = dict(_ACTIVE_CHAT)
    if not entry.get("at"):
        return None
    if entry.get("identity") != _active_identity(display, raw_who):
        log.debug("wx_active_chat_reuse_rejected", reason="identity",
                  who=raw_who or display)
        return None
    if not allow_unverified and not entry.get("title_verified"):
        log.debug("wx_active_chat_reuse_rejected", reason="unverified",
                  who=raw_who or display)
        return None
    age = time.time() - float(entry.get("at") or 0.0)
    if age > _ACTIVE_HARD_TTL_S:
        invalidate_active_chat("expired_hard")
        return None
    if age > _ACTIVE_TTL_S:
        # The TTL is only an idle hint.  The header fingerprint below is the
        # real proof, so re-probe instead of forcing an unnecessary search
        # just because the user left the same conversation open for a while.
        log.info("wx_active_chat_stale_probe", who=raw_who or display,
                 age=round(age, 1))

    if win is None:
        win = _wechat_window(force=True)
    if win is None:
        log.debug("wx_active_chat_reuse_rejected", reason="no_window",
                  who=raw_who or display)
        return None
    if (int(win.hwnd) != int(entry.get("hwnd") or 0)
            or int(getattr(win, "pid", 0) or 0) != int(entry.get("pid") or 0)):
        invalidate_active_chat("window_changed")
        return None

    try:
        if ensure_foreground() is None:
            return None  # transient; keep the memory for the next attempt
        frame, layout = wx_vision.claim_window_frame(win)
    except Exception as exc:
        log.debug("wx_active_chat_probe_failed", error=str(exc))
        return None

    if not wx_vision.frame_looks_like_wechat(layout):
        invalidate_active_chat("frame_not_wechat")
        return None
    # claim_window_frame refreshed the window just now; if its reported size
    # does not match the frame we got back, something moved under us.
    if (int(win.width), int(win.height)) != tuple(frame.size):
        invalidate_active_chat("reuse_geometry_changed")
        return None
    header, fingerprint = _header_print(frame, layout)
    if header is None or tuple(header) != tuple(entry.get("header") or ()):
        invalidate_active_chat("header_geometry_changed")
        return None
    if not _print_matches(fingerprint, entry.get("fingerprint")):
        invalidate_active_chat("header_pixels_changed")
        return None

    # This reuse just re-proved the header, so the TTL is an idle window rather
    # than a hard maximum age; otherwise a long burst of sends would fall back
    # to the search every TTL for no safety gain.
    with _ACTIVE_LOCK:
        if (_ACTIVE_CHAT.get("identity") == entry.get("identity")
                and int(_ACTIVE_CHAT.get("hwnd") or 0) == int(entry.get("hwnd") or 0)):
            _ACTIVE_CHAT["at"] = time.time()

    log.info("wx_active_chat_reused", who=raw_who or display, age=round(age, 2),
             title_verified=bool(entry.get("title_verified")))
    return {"ok": True, "reused": True, "win": win, "frame": frame,
            "layout": layout, "title_verified": bool(entry.get("title_verified")),
            "rect": tuple(win.rect), "size": tuple(frame.size)}


def confirm_active_chat(display: str, raw_who: str = "", *, win=None) -> bool:
    """Fresh fingerprint check before a reused send commits its input.

    True only when the remembered header for this exact conversation is still
    on screen.  False covers a changed frame, a missing memory (someone else
    may have replaced it), WeChat not being foreground, or a capture error --
    the caller must not send in any of those cases.
    """
    if not _ACTIVE_ENABLED:
        return False
    if win is None:
        win = _wechat_window(force=True)
    if win is None:
        return False
    if not win.is_foreground():
        # 2026-10-26：打完字后焦点可能被别的窗口抢走（终端/浏览器/输入法），
        # 但会话没变。先尝试把微信拉回前台一次，仍不行才放弃。
        try:
            wx_vision._force_foreground(int(getattr(win, "hwnd", 0) or 0))
            time.sleep(0.25)
        except Exception:  # noqa: BLE001
            pass
        if not win.is_foreground():
            log.warning("wx_guard_not_foreground", hwnd=int(getattr(win, "hwnd", 0) or 0))
            return False
    try:
        frame, _rect = wx_vision.capture_window(win)
        layout = wx_vision.detect_layout(frame)
    except Exception as exc:
        log.warning("wx_active_chat_confirm_failed", error=str(exc))
        return False
    return match_active_chat(display, raw_who, win, frame, layout) is True


def match_active_chat(display: str, raw_who: str, win, frame, layout):
    """Tri-state guard for the last moment before a click.

    None means there is no memory for this conversation, so the caller has
    nothing to enforce.  True means the frame still matches; False means the
    conversation changed and the caller must abort.
    """
    if not _ACTIVE_ENABLED:
        return None
    with _ACTIVE_LOCK:
        entry = dict(_ACTIVE_CHAT)
    if not entry.get("at") or entry.get("identity") != _active_identity(display, raw_who):
        return None
    if (win is None
            or int(win.hwnd) != int(entry.get("hwnd") or 0)
            or int(getattr(win, "pid", 0) or 0) != int(entry.get("pid") or 0)):
        return False
    header, fingerprint = _header_print(frame, layout)
    if header is None:
        invalidate_active_chat("header_unreadable")
        return False
    # 2026-11-01 修：这里以前把 _header_print() 返回的**矩形坐标**当成标题文字
    # 来比对（`(377, 48, 904, 71)` → got="3774890471"），于是每次校验都必然
    # mismatch、把刚建立的可信记忆作废，下一条又从 6–7 秒的 OCR 重来。
    # 现在的次序：像素指纹一致就直接过（0.1 秒）；只有像素真的变了（未读角标、
    # 消息滚动都会变，2026-10-26 的教训）才去 OCR 标题文字复核。
    if _print_matches(fingerprint, entry.get("fingerprint")):
        return True
    _got = ""
    try:
        _items = wx_vision.ocr_items(fingerprint)
        _got = "".join(str(_it.get("text") or "") for _it in (_items or [])).replace(" ", "").lower()
    except Exception as exc:
        warn("wx_confirm_title_ocr_failed", err=repr(exc))
    _exp = "".join(str(display or "").split()).lower()
    if _exp and _got and _exp not in _got and _got not in _exp:
        log.warning("wx_active_chat_title_mismatch", who=str(raw_who or "")[:64],
                    expected=str(display or "")[:60], got=str(_got)[:120])
        invalidate_active_chat("title_mismatch")
        return False
    log.info("wx_active_chat_pixels_changed_reconfirmed", who=str(raw_who or "")[:64],
             got=str(_got)[:60])
    if not _print_matches(fingerprint, entry.get("fingerprint")):
        log.info("wx_active_chat_pixels_changed", who=str(raw_who or "")[:64])
    return True


def text_verify_on_open() -> bool:
    return _TEXT_VERIFY_ON_OPEN


def title_matches(opened: str, expected: str) -> bool:
    """Fuzzy-but-conservative comparison of two rendered chat titles."""
    left = "".join((opened or "").split()).lower()
    right = "".join((expected or "").split()).lower()
    if not left or not right:
        return False
    if right in left or left in right:
        return True
    overlap = len(set(left) & set(right))
    return overlap >= max(1, int(len(right) * 0.6))


def title_matches_any(opened: str, expected: list[str]) -> str:
    """返回第一个和 ``opened`` 匹配的候选名；都不匹配返回空串。

    2026-10-27 起用于"改名"场景：同一个会话可能有新旧好几个名字，
    顶栏只要命中其中任意一个就算对，不必等于调用方手里那个可能已过期的名字。
    """
    if not str(opened or "").strip():
        return ""
    for name in expected or []:
        if not str(name or "").strip():
            continue
        if title_matches(opened, name):
            return str(name)
    return ""


def verify_active_chat_title(display: str, raw_who: str = "", *, win=None) -> tuple[bool, str]:
    """OCR the opened chat's header once and upgrade the memory to verified.

    Returns ``(verified, fatal_error)``.  An unreadable header is not fatal:
    the caller keeps the old behaviour and no cache upgrade happens.  A
    readable header that does not match is fatal and the caller must not send.
    """
    if not _ACTIVE_ENABLED or not _TEXT_VERIFY_ON_OPEN:
        return False, ""
    if win is None:
        win = _wechat_window(force=True)
    if win is None:
        log.info("wx_chat_title_verify_inconclusive", who=display, reason="no_window")
        return False, ""
    try:
        if ensure_foreground() is None:
            log.info("wx_chat_title_verify_inconclusive", who=display,
                     reason="not_foreground")
            return False, ""
        frame, layout = wx_vision.claim_window_frame(win)
    except Exception as exc:
        log.info("wx_chat_title_verify_inconclusive", who=display,
                 reason="frame_failed", error=str(exc)[:120])
        return False, ""
    if not wx_vision.frame_looks_like_wechat(layout) or not getattr(layout, "header", None):
        log.info("wx_chat_title_verify_inconclusive", who=display,
                 reason="not_wechat_or_no_header")
        return False, ""

    hx, hy, hw, hh = layout.header
    frame_size = tuple(frame.size)
    try:
        title = " ".join(text for text, *_rest in wx_vision.ocr_items(
            frame.crop((hx, hy, hx + min(hw, 560), hy + hh)))).strip()
    except Exception as exc:
        log.info("wx_chat_title_verify_inconclusive", who=display,
                 reason="ocr_failed", error=str(exc)[:120])
        return False, ""

    # 2026-11-01：窗口被拖得很宽时，detect_layout 会把"会话区起点"算歪
    # （实测 chat_left=1067 而其实是 377），于是 header 矩形落在空白处，
    # 标题读成空串、每次都判"校验不过"——用户看到的就是"页面点不动"。
    # 这里兜一手：整条顶栏 OCR 一遍，只要出现目标名字就算过。
    if not title:
        try:
            _strip = frame.crop((0, 0, int(frame.width), min(180, int(frame.height))))
            _tops = [str(t) for t, *_rest in wx_vision.ocr_items(_strip)]
            title = " ".join(_tops).strip()
            if title:
                log.info("wx_chat_title_verify_fallback", who=display, title=title[:70])
        except Exception as exc:
            log.info("wx_chat_aux_failed", where="open_by_session_list|verify", error=str(exc)[:120])

    # The OCR takes ~2.3s; if the window was resized during it, the frame and
    # layout no longer describe what is on screen, so do not cache them.
    try:
        win.refresh()
        if (int(win.width), int(win.height)) != frame_size:
            invalidate_active_chat("title_verify_geometry_changed")
            return False, ""
    except Exception:
        warn("core/execution/wx_chat.py:482 Exception", err=repr(__import__("sys").exc_info()[1]))
        return False, ""

    if not title:
        log.info("wx_chat_title_verify_inconclusive", who=display,
                 reason="empty_title")
        return False, ""
    # 名字会变（群改名 / 好友改昵称 / 备注被改），chat_id 不会。
    # 校验因此放宽为"命中这个会话的任意一个已知名字"，并把这次读到的真名
    # 记进别名簿——下次改名也就认得了（自愈，不靠人工维护白名单）。
    # 2026-10-31 提速：`chat_alias.aliases_for()` 要过一遍终端层（account manager），
    # 而它在"没声明可写生产库"的进程里每次初始化都会失败重试 —— 实测第一次 6 秒、
    # 之后也可能反复付。别名的语义只是**放宽**校验（标题等于目标名本来就够），
    # 所以它不可用/太慢的时候就先不用：记一个 10 分钟的"别名不可用"期，
    # 期间直接按标题判断，不再付这笔钱。
    _alias = None
    _known: list = []
    if time.time() >= _ALIAS_DOWN_UNTIL[0]:
        _t_alias = time.time()
        try:
            from core.execution import chat_alias as _alias_mod

            _known = list(_alias_mod.aliases_for(raw_who or display)) + \
                list(_alias_mod.aliases_for(display))
            _alias = _alias_mod
        except Exception as exc:  # noqa: BLE001 - 别名读不到只影响"改名也能认"，不影响发不发
            skips.skip("wx_chat.verify_active_chat_title:alias", "degraded",
                       "%s: %s" % (type(exc).__name__, exc))
            _alias = None
            _known = []
        _alias_ms = int((time.time() - _t_alias) * 1000)
        if _alias is None or _alias_ms > 1500:
            _ALIAS_DOWN_UNTIL[0] = time.time() + 600.0
            log.info("wx_chat_alias_disabled", who=display, ms=_alias_ms,
                     ok=_alias is not None)
    _hit = title_matches_any(title, [display] + _known)
    if not _hit:
        invalidate_active_chat("title_verify_mismatch")
        return False, ("当前打开的聊天是“%s”，与目标“%s”不符，已中止发送" % (title, display))
    if _alias is not None:
        if _hit != display:
            log.info("wx_chat_title_alias_ok", who=display, title=title, matched=_hit)
        try:
            _t_learn = time.time()
            _alias.learn(raw_who or display, title, source="title")
            _learn_ms = int((time.time() - _t_learn) * 1000)
            if _learn_ms > 1500:
                _ALIAS_DOWN_UNTIL[0] = time.time() + 600.0
                log.info("wx_chat_alias_disabled", who=display, phase="learn",
                         ms=_learn_ms)
        except Exception as exc:  # 学不到只是这次没记住，下次标题对不上会再学
            skips.skip("wx_chat.verify_active_chat_title:learn", "degraded",
                       "%s: %s" % (type(exc).__name__, exc))
            # 2026-10-31 提速：学习要写终端库，写不进去会先重试若干秒（实测 ~4.5 秒/次）。
            # 它只影响"改名后还认不认得"，不影响这次能不能发 —— 先关 10 分钟。
            _ALIAS_DOWN_UNTIL[0] = time.time() + 600.0
            log.info("wx_chat_alias_disabled", who=display, phase="learn_failed",
                     err=repr(exc)[:120])
    remember_active_chat(display, raw_who, win, frame, layout, title_verified=True)
    return True, ""


def guard_active_chat(display: str, raw_who: str = "", *, win=None):
    """Tri-state guard for any text/file send about to commit.

    None means there is no memory for this conversation to enforce; True means
    the remembered header still matches; False means the frame changed or
    WeChat is not safely in front, so the caller must not send.
    """
    if not _ACTIVE_ENABLED:
        return None
    if win is None:
        win = _wechat_window(force=True)
    if win is None:
        return False
    if not win.is_foreground():
        # 2026-10-26：打完字后焦点可能被别的窗口抢走（终端/浏览器/输入法），
        # 但会话没变。先尝试把微信拉回前台一次，仍不行才放弃。
        try:
            wx_vision._force_foreground(int(getattr(win, "hwnd", 0) or 0))
            time.sleep(0.25)
        except Exception:  # noqa: BLE001
            pass
        if not win.is_foreground():
            log.warning("wx_guard_not_foreground", hwnd=int(getattr(win, "hwnd", 0) or 0))
            return False
    try:
        frame, _rect = wx_vision.capture_window(win)
        layout = wx_vision.detect_layout(frame)
    except Exception as exc:
        log.warning("wx_active_chat_guard_failed", error=str(exc))
        return False
    _ok = match_active_chat(display, raw_who, win, frame, layout)
    if _ok:
        return True
    # 2026-10-26：判据太严会误杀——群里来条新消息、右侧多一个未读红点、输入框光标闪一下，
    # 整窗像素指纹就变了，于是"窗口被换过"→ 中止发送。实测（隔离实验）链路本身完全正常，
    # 所以这里降级处理：重截一帧再判一次，仍判不出就只告警、不中止（发前还有标题核对，
    # 发后还有 verified 校验兜底）。
    try:
        time.sleep(0.25)
        _frame2, _rect2 = wx_vision.capture_window(win)
        _layout2 = wx_vision.detect_layout(_frame2)
        if match_active_chat(display, raw_who, win, _frame2, _layout2):
            return True
    except Exception as exc:  # noqa: BLE001
        log.warning("wx_active_chat_guard_retry_failed", error=str(exc))
    log.warning("wx_active_chat_guard_downgraded", who=str(raw_who or display))
    return True


def _wechat_window(*, force: bool = False):
    """Current WeChat window, re-measured at most once every _WINDOW_TTL seconds.

    Returning a live WindowInfo (rather than a bare hwnd behind a hardcoded Qt
    class name) is what lets callers convert window-local points to screen
    points correctly after the user moves or resizes the window.
    """
    from core.execution import wx_vision

    now = time.time()
    if force or _WINDOW_CACHE["win"] is None or (now - _WINDOW_CACHE["at"]) > _WINDOW_TTL:
        _WINDOW_CACHE["win"] = wx_vision.find_wechat_window()
        _WINDOW_CACHE["at"] = now
    return _WINDOW_CACHE["win"]


def set_account_id(account_id: str):
    global _ACCOUNT_ID
    _ACCOUNT_ID = account_id


def resolve_nickname(who: str) -> str:
    # 2026-10-26：系统号不是 wxid_/…@chatroom，原来在下面的早退里被原样返回，
    # 结果搜索框里搜的是 "filehelper" 这种英文字母，当然搜不到「文件传输助手」，
    # 随后标题校验失败、被误判成"发送前检测到聊天窗口已变化"。
    _SYS = {
        "filehelper": "文件传输助手",
        "weixin": "微信团队",
        "notifymessage": "微信团队",
        "newsapp": "腾讯新闻",
        "qqmail": "QQ邮箱提醒",
        "medianote": "语音记事本",
        "floatbottle": "漂流瓶",
        "fmessage": "朋友推荐消息",
    }
    if who in _SYS:
        return _SYS[who]
    if who in _NICKNAME_CACHE:
        return _NICKNAME_CACHE[who]
    if not who.startswith("wxid_") and not who.endswith("@chatroom"):
        return who
    try:
        from core.terminal.store_port import get_account_db_manager
        adm = get_account_db_manager()
        if adm:
            if _ACCOUNT_ID:
                repo = adm.get_repo("wechat", _ACCOUNT_ID)
                if repo:
                    contact = repo.contacts.get_by_wxid(who)
                    if contact:
                        name = contact.remark or contact.nickname or ""
                        if name:
                            _NICKNAME_CACHE[who] = name
                            log.info("wxid_resolved", wxid=who, nickname=name, account_id=_ACCOUNT_ID)
                            return name
            for (plat, aid), repo in adm.all_repos().items():
                if plat != "wechat":
                    continue
                contact = repo.contacts.get_by_wxid(who)
                if contact:
                    name = contact.remark or contact.nickname or ""
                    if name:
                        _NICKNAME_CACHE[who] = name
                        log.info("wxid_resolved", wxid=who, nickname=name, account_id=aid)
                        return name
    except Exception:
        warn("core/execution/wx_chat.py:568 Exception", err=repr(__import__("sys").exc_info()[1]))
    return who


def get_ocr():
    """Kept for existing callers; the engine now lives in wx_vision."""
    from core.execution import wx_vision

    return wx_vision.get_ocr()


def _foreground_lock_timeout() -> int:
    """SPI_GETFOREGROUNDLOCKTIMEOUT（毫秒）；读不到返回 -1。只用于诊断。"""
    import ctypes

    value = ctypes.c_int(0)
    try:
        ok = ctypes.windll.user32.SystemParametersInfoW(0x2000, 0, ctypes.byref(value), 0)
    except Exception:
        return -1
    return int(value.value) if ok else -1


def ensure_foreground() -> int | None:
    """Bring WeChat to the foreground and return its handle, or None.

    Foreground really means foreground here: every keystroke and click this
    module sends goes to whatever window has focus, so proceeding while another
    application is on top would type the search query into that application.
    Failing is the only safe answer.
    """
    from core.execution import wx_vision

    win = _wechat_window(force=True)
    if win is None:
        return None
    # Foreground changes are sometimes refused while the user is actively
    # typing in another window.  One short retry covers the transient case;
    # failing still returns None so nothing can be typed into the wrong app.
    activated = False
    for attempt in range(2):
        if wx_vision.activate_window(win):
            activated = True
            break
        if attempt == 0:
            time.sleep(0.35)
    if not activated:
        # 2026-11-18：只记 minimized/visible 不够定位。把当时的前台窗口、标题和
        # SPI_GETFOREGROUNDLOCKTIMEOUT 一起留下，下次失败能分清是「前台锁」
        # 还是「窗口在别的虚拟桌面/被别的进程占着」。
        try:
            import win32gui

            _fg = wx_vision.foreground_hwnd()
            _fg_title = win32gui.GetWindowText(_fg) if _fg else ""
        except Exception:
            _fg, _fg_title = 0, ""
        log.warning("wx_activate_failed", hwnd=win.hwnd, visible=win.visible,
                    minimized=win.minimized, foreground=_fg, fg_title=_fg_title,
                    lock_timeout_ms=_foreground_lock_timeout())
        return None
    if not win.is_foreground():
        log.warning("wx_not_foreground", hwnd=win.hwnd,
                    foreground=wx_vision.foreground_hwnd())
        return None
    return win.hwnd


def _activate_wechat_search():
    """Open the search field; returns (ok, rect, window_size_at_probe)."""
    from core.execution import wx_human

    from core.execution import wx_vision

    win = _wechat_window(force=True)
    if ensure_foreground() is None:
        return False, None, None

    # No Escape here on purpose.  It was added to clear a leftover search
    # overlay, but in WeChat Escape closes the window: the window flashed shut,
    # the activation below dutifully restored it, and it came back as a
    # conversation-less placeholder that no longer passed the frame check --
    # which then pressed Escape again on the way out.  A window that looks like
    # WeChat and behaves like a static image is what that loop produces.
    # Leftovers are handled where they belong: by the verification below, which
    # reads the query back out of the search box before anything is clicked.

    # Confirm the screen really shows WeChat before typing a single character.
    # A grab returns whatever is painted at those coordinates, so an occluded
    # window yields a perfectly valid picture of some other application -- and
    # typing the contact's name into that is the worst possible outcome.
    _image, layout = wx_vision.claim_window_frame(win)
    # Deliberately the weaker test: a conversation being open is what searching
    # is for, so it cannot also be a precondition.  Using the strict check here
    # is what left WeChat stranded on its placeholder pane with no way back.
    if wx_vision.why_not_searchable(layout):
        log.warning("wx_search_frame_not_wechat",
                    reason=wx_vision.why_not_wechat(layout),
                    foreground=wx_vision.foreground_hwnd(), target=win.hwnd,
                    visible=win.visible, rect=win.rect,
                    detected={"chat_left": layout.chat_left, "emoji": layout.emoji_point(),
                              "toolbar_y": layout.toolbar_y, "header": layout.header,
                              "input_box": layout.input_box, "icons": len(layout.icons)})
        return False, None, None

    # Focus the search field with the mouse.  Ctrl+F is not dependable here --
    # measured against WeChat 4.x it frequently does nothing at all, and a
    # query typed into whatever happened to have focus is worse than useless.
    rect = _search_box_rect_in(_image, layout)
    if rect:
        cx = (rect[0] + rect[2]) // 2
        cy = (rect[1] + rect[3]) // 2
        wx_human.click(*win.to_screen(cx, cy), aim_jitter=1.0)
        log.debug("wx_search_field_clicked", local=(cx, cy), rect=rect)
    else:
        # 2026-11-01：这里原来按 ESC + Ctrl+F 兜底 —— 但 ESC 在微信 4.x 里是
        # "关窗口"（上面那大段注释就是在讲这个），按下之后窗口消失，后面打的字
        # 全落空：搜索框一直是占位符"搜索"，而"搜索结果 OCR"读到的其实是左边的
        # 会话列表，于是点/回车都打开原来那个会话，send_text 的标题闸只好中止。
        # 实测：detect_layout().search_box 在这台机器上恒为 None，所以这条兜底
        # 路径每次都会走。改成按窗口比例点搜索框（1299x1537 上的实测点是 170,68）。
        _w = float(win.width or 1299)
        _h = float(win.height or 1537)
        rect = (int(round(_w * 0.050)), int(round(_h * 0.020)),
                int(round(_w * 0.330)), int(round(_h * 0.075)))
        _fx, _fy = int(round(_w * 0.131)), int(round(_h * 0.044))
        wx_human.click(*win.to_screen(_fx, _fy), aim_jitter=0.0)
        log.warning("wx_search_field_not_found_used_proportional",
                    local=(_fx, _fy), rect=rect, size=(_w, _h))
    wx_human.sleep_human(0.5, 0.25)

    # Empty the field first: a query left behind by an earlier attempt would be
    # appended to, which silently turns the search into garbage.
    wx_human.hotkey(wx_human.VK_CONTROL, ord("A"))
    wx_human.key_press(0x2E)  # VK_DELETE
    wx_human.sleep_human(0.15, 0.3)
    return True, rect, (int(win.width), int(win.height))


VIA_CLIPBOARD = "clipboard"
VIA_UNICODE = "unicode"
VIAS = (VIA_CLIPBOARD, VIA_UNICODE)


def type_text(text: str, *, via: str = VIA_CLIPBOARD) -> bool:
    """打字只走这一个入口（SEC-006）。两条落地方式，由 `via` 决定用哪条。

    - `via="clipboard"`：剪贴板 + Ctrl-V。日常发消息走这条，中文不会被输入法吞。
      返回值 = 「粘贴动作有没有落下去」。
    - `via="unicode"`：SendInput 逐键敲（`wx_human.type_unicode`）。微信的「@ 成员
      名单」只对**键盘敲出来的 @** 弹，粘贴一行带 @ 的文本它不弹，所以 @ 必须走这条。
      逐键敲拿不到任何回执，返回 True 只代表「按键发出去了」，不代表字进了输入框 ——
      上层必须自己读回输入框复核（`_pick` 之后就是这么做的）。

    别在别处再写第三条路：两个入口会让「改一处漏一处」。
    """
    if via not in VIAS:
        raise ValueError("没登记的打字方式：%r（只能从 %r 里选）" % (via, VIAS))
    if not text:
        return False
    from core.execution import wx_human

    if via == VIA_UNICODE:
        wx_human.type_unicode(text)
        return True
    return bool(wx_human.paste_text(text))


def press_enter() -> None:
    from core.execution import wx_human

    wx_human.key_press(wx_human.VK_RETURN)


def press_ctrl_enter() -> None:
    """Ctrl+Enter：兼容微信「Enter=换行」/ 输入法吃掉 Enter 的场景。"""
    from core.execution import wx_human

    wx_human.hotkey(wx_human.VK_CONTROL, wx_human.VK_RETURN)


def press_escape() -> None:
    from core.execution import wx_human

    wx_human.key_press(wx_human.VK_ESCAPE)


def press_down(n: int = 1) -> None:
    from core.execution import wx_human

    for _ in range(n):
        wx_human.key_press(0x28)  # VK_DOWN


_SECTION_HEADERS = frozenset({"最常使用", "联系人", "群聊", "公众号", "功能", "聊天记录", "搜索"})


def _search_box_rect_in(image, layout):
    """Search-field rect measured from an existing frame; avoids a re-grab."""
    from core.execution import wx_vision

    try:
        limit = int(max(120, min(layout.chat_left - 4, layout.width * 0.6)))
        band = image.crop((0, 0, limit, max(60, int(layout.height * 0.18))))
        gray = wx_vision._to_gray(wx_vision._as_rgb(band))
        height, width = gray.shape
        blocks = wx_vision._white_blocks(gray, 0, height,
                                         min_h=12, min_w=max(60, width // 3))
        if not blocks:
            return None
        bx, by, bw, bh, _area = blocks[0]
        if by >= height * 0.7 or bh > height * 0.6:
            return None
        return (int(bx), int(by), int(bx + bw), int(by + bh))
    except Exception as exc:
        log.debug("search_box_probe_failed", error=str(exc))
        return None


def _search_box_rect(win):
    """Window-local rectangle of the chat-list search field, or None.

    The field is the only large white block in the chat-list column: the list
    itself is painted on a grey wash and the conversation pane is excluded by
    stopping at the pane boundary, which would otherwise dominate by area.
    """
    from core.execution import wx_vision

    try:
        image, _rect = wx_vision.capture_window(win)
        layout = wx_vision.detect_layout(image)
    except Exception as exc:
        log.debug("search_box_probe_failed", error=str(exc))
        return None
    return _search_box_rect_in(image, layout)


def _search_box_bottom(win) -> int:
    """Window-local bottom edge of the search field, or 0 if not found."""
    rect = _search_box_rect(win)
    return int(rect[3] + 6) if rect else 0


def _query_landed_in_message_box(win, text: str) -> bool:
    """Whether the search query ended up in the message input instead.

    This is the only outcome that actually needs blocking: text parked in the
    message box is one stray Enter away from being delivered to whoever happens
    to be open.  Checking for that positively avoids rejecting a perfectly good
    search just because OCR could not read the field back.
    """
    from core.execution import wx_vision

    try:
        image, _rect = wx_vision.capture_window(win)
        layout = wx_vision.detect_layout(image)
        box = layout.input_box
        if not box:
            return False
        x0, y0, x1, y1 = box
        # The icon toolbar occupies the lower part of the field; text sits above.
        crop = image.crop((x0, y0, x1, y0 + max(20, int((y1 - y0) * 0.55))))
        items = wx_vision.ocr_items(crop)
    except Exception as exc:
        log.debug("message_box_probe_failed", error=str(exc))
        return False

    if not items:
        return False
    seen = "".join(text for text, *_rest in items)
    wanted = "".join(text.split())
    if not wanted:
        return False
    return len(set(seen) & set(wanted)) >= max(2, len(wanted) // 2)


# 输入框为空时微信显示的占位提示（如"按住鼠标语音输入文字"）会被 OCR
# 读成一行"正文"；把它当成待发送文本会让"输入框里有没有文本"的判断失真
# （发送前的落笔检查、发送后的清空检查都会被它带偏）。
_MESSAGE_BOX_PLACEHOLDER_MARKERS = (
    "按住鼠标语音输入文字",
    "语音输入文字",
    "按住鼠标",
)


def focus_message_box(window=None) -> bool:
    """Click the message input so a following paste actually lands in it.

    Opening a conversation does not guarantee keyboard focus is in the input
    field: after a search-driven open the focus can stay on the search overlay
    or the session list, and a paste sent while nothing has focus goes nowhere
    -- exactly the failure mode that shows up as "消息文本未进入输入框".
    Clicks the upper part of the input box, clear of the icon toolbar that
    occupies its lower edge.
    """
    from core.execution import wx_human
    from core.execution import wx_vision

    win = window or _wechat_window(force=True)
    if win is None:
        return False
    try:
        image, _rect = wx_vision.capture_window(win)
        layout = wx_vision.detect_layout(image)
        box = layout.input_box
        if not box:
            return False
        x0, y0, x1, y1 = box
        cx = (x0 + x1) // 2
        cy = y0 + max(12, int((y1 - y0) * 0.22))
        wx_human.click(*win.to_screen(cx, cy), aim_jitter=1.0)
        wx_human.sleep_human(0.12, 0.25)
        return True
    except Exception as exc:
        log.debug("focus_message_box_failed", error=str(exc))
        return False


#: 2026-11-01 提速：一次发送只截一次屏。
#: 一条消息原来至少截 3 次窗、跑 3 次 detect_layout（每次约 0.2–0.9 秒，冷启动更贵）：
#: 读顶栏一次、判输入框两次、发前基线一次。它们看的是同一个窗口、同一个瞬间，
#: 各自重新截只是重复付钱。_view() 把「刚截过的这一帧」在很短的窗口内复用，
#: 谁需要"绝对新鲜"（比如按完回车之后）就先调 invalidate_view()。
_VIEW: dict = {"at": 0.0, "hwnd": 0, "frame": None, "layout": None}
_VIEW_MAX_AGE_S = 0.8


def invalidate_view() -> None:
    """丢掉缓存的窗口帧（发完/切完会话后调用，下一次取帧一定重新截）。"""
    _VIEW.update({"at": 0.0, "hwnd": 0, "frame": None, "layout": None})


def _view(window=None, *, max_age: float | None = None):
    """(win, frame, layout)；短时间内复用同一帧，返回的 frame 只读、别原地改。"""
    from core.execution import wx_vision

    limit = _VIEW_MAX_AGE_S if max_age is None else float(max_age)
    win = window or _wechat_window(force=True)
    if win is None:
        return None, None, None
    now = time.time()
    try:
        same = int(_VIEW.get("hwnd") or 0) == int(getattr(win, "hwnd", 0) or 0)
    except Exception:  # noqa: BLE001 - hwnd 读不出来就当不同
        same = False
    if same and _VIEW.get("frame") is not None and (now - float(_VIEW.get("at") or 0.0)) <= limit:
        return win, _VIEW["frame"], _VIEW["layout"]
    try:
        win.refresh()
        frame, _rect = wx_vision.capture_window(win)
        layout = wx_vision.detect_layout(frame)
    except Exception as exc:  # noqa: BLE001 - 截不到就当没有
        log.debug("wx_chat_view_failed", error=str(exc))
        return win, None, None
    _VIEW.update({"at": time.time(), "hwnd": int(getattr(win, "hwnd", 0) or 0),
                  "frame": frame, "layout": layout})
    return win, frame, layout


def message_box_has_text(window=None) -> bool | None:
    """Return whether the WeChat message input currently contains text.

    ``True`` means OCR can see text in the input, ``False`` means the field is
    empty, and ``None`` means the probe could not be completed (window/OCR
    failure).  Send verification only blocks on ``True``; an inconclusive probe
    must never break the send path.
    """
    from core.execution import wx_vision

    win = window or _wechat_window(force=True)
    if win is None:
        return None
    try:
        win, image, layout = _view(win)
        box = layout.input_box
        if not box:
            return None
        x0, y0, x1, y1 = box
        # The icon toolbar occupies the lower part of the field; text sits above.
        crop = image.crop((x0, y0, x1, y0 + max(20, int((y1 - y0) * 0.55))))
        # 2026-11-01 提速：判"框里有没有字"只需要知道**有没有碰到字**，不必把每个字读准。
        # 877x202 整幅 OCR 冷启动 2.67s、热 0.61s；缩到 480 宽冷启动 0.59s。
        # 短句本来就读不出（见下面 2026-10-28 注释），缩小的只是白等的秒数。
        try:
            _w0, _h0 = crop.size
            if _w0 > 480:
                crop = crop.resize((480, max(1, int(round(_h0 * (480.0 / float(_w0)))))))
        except Exception as exc:  # noqa: BLE001 - 缩不动就用原尺寸，慢一点但正确
            skips.skip("wx_chat.message_box_has_text:resize", "degraded", repr(exc)[:120])
        items = wx_vision.ocr_items(crop)
    except Exception as exc:
        log.debug("message_box_has_text_failed", error=str(exc))
        return None
    if not items:
        # 2026-10-28 事故「哦」：1~3 个字的短句 OCR 经常整条读不出来（空列表）。
        # 这里原先 return False —— 把“测不准”当成了“确定为空”，上游于是去清空输入框
        # 重贴，用户看到的就是“字打进去又被删掉”，最后整条消息被丢掉。
        # 空列表只能说明“没看见”，不能说明“没有”。
        return None
    seen = "".join(str(item[0]) for item in items if item)
    flat = "".join(seen.split())
    if any(marker in flat for marker in _MESSAGE_BOX_PLACEHOLDER_MARKERS):
        # 读到的只是输入框空态提示，不是待发送文本。
        return False
    return True if seen.strip() else None


_BOX_CHANGED_PX = 150

#: 2026-10-31 提速：最近一次"确认是空输入框"的像素快照（按框的宽高记）。
#: 判"框里有没有存量字"原来要 OCR（实测 2.4 秒/次），而框是同一个框：
#: 拿它跟缓存比一次像素差分（0.18 秒）就够；缓存没有或尺寸变了才回退 OCR。
_BOX_EMPTY: dict = {}

#: 2026-11-01：判空那一次 OCR 到底靠不靠得住（None=还不知道）。
#: 实测这台机器上它经常 5–12 秒返回 ``None``（"测不准"），而 ``None`` 在发送
#: 路径上等于"不拦"——花十几秒换一个不改变结果的答案。desk 启动预热会跑一次
#: 并调 mark_box_ocr_trust()：**只要那一次是 None，本进程就不再为判空付 OCR**，
#: 直接按"测不准"处理（语义与今天完全一致，只是不再白等）。
_BOX_OCR_TRUST: list = [None]


def mark_box_ocr_trust(ok: bool) -> None:
    """告诉判空那条路：这一次 OCR 靠谱吗（desk 预热用）。"""
    _BOX_OCR_TRUST[0] = bool(ok)


def box_looks_empty(window=None):
    """输入框是不是空的：``True`` / ``False`` / ``None``（测不准）。

    先比缓存（我们上次亲眼见过它是空的），比不出来才 OCR —— 但**只用 OCR 的
    "确定为空"结论去刷新缓存**，不确定就返回 ``None``，绝不当成"空"。
    """
    _t0 = time.time()
    snap = message_box_snapshot(window)
    _t1 = time.time()
    if snap is None:
        return None
    try:
        import numpy as _np

        arr = _np.asarray(snap, dtype="uint8")
        key = (int(arr.shape[0]), int(arr.shape[1]))
    except Exception as exc:  # noqa: BLE001 - 形状读不出来就走 OCR 老路
        skips.skip("wx_chat.box_looks_empty:shape", "malformed", repr(exc)[:120])
        key = None
    if key is not None:
        ref = _BOX_EMPTY.get(key)
        if ref is not None:
            changed = message_box_changed(ref, snap)
            if changed is not None:
                return not changed
    if _BOX_OCR_TRUST[0] is False:
        log.debug("box_empty_ocr_skipped", trusted=False, key=str(key))
        return None
    _t2 = time.time()
    text = message_box_has_text(window)
    if text is None:
        _BOX_OCR_TRUST[0] = False
    _t3 = time.time()
    log.debug("box_empty_timing", snap_ms=int((_t1 - _t0) * 1000),
             cache_miss_ms=int((_t2 - _t1) * 1000), ocr_ms=int((_t3 - _t2) * 1000),
             key=str(key), text=repr(text)[:20])
    if text is False and key is not None:
        _BOX_EMPTY[key] = snap
        return True
    return text

#: 别名校验（chat_alias）不可用/太慢时的"先不用"截止时刻（epoch 秒）。
#: 2026-10-31：它要过终端层，失败重试一次 6 秒，而它只是把校验**放宽**而已。
_ALIAS_DOWN_UNTIL = [0.0]


def message_box_snapshot(window=None):
    """输入框上半区的像素快照，用来判"贴字前后有没有多出墨"。"""
    from core.execution import wx_vision

    win = window or _wechat_window(force=True)
    if win is None:
        return None
    try:
        win, image, layout = _view(win)
        box = layout.input_box
        if not box:
            return None
        x0, y0, x1, y1 = box
        crop = image.crop((x0, y0, x1, y0 + max(20, int((y1 - y0) * 0.55))))
        np = wx_vision._np()
        return np.asarray(crop.convert("RGB")).astype("int16")
    except Exception as exc:
        log.debug("message_box_snapshot_failed", error=str(exc))
        return None


def message_box_changed(before, after, min_px: int = _BOX_CHANGED_PX) -> bool | None:
    """两帧输入框之间是否"新出现了字"（像素差分）。

    为什么不用 OCR 判空：1~3 个字的短句 OCR 经常读不出来（假阴性），而假阴性会
    触发"清空重贴"，用户看到的就是字打进去又被删掉。输入框底色本身有约 0.9%
    的噪点、拿绝对阈值分不出来，但两帧之间的**变化**只可能是字（或光标）。
    """
    if before is None or after is None:
        return None
    if getattr(before, "shape", None) != getattr(after, "shape", None):
        return None
    try:
        from core.execution import wx_vision

        np = wx_vision._np()
        diff = np.abs(before - after).sum(axis=2)
        return bool(int((diff > 60).sum()) >= int(min_px))
    except Exception as exc:
        log.debug("message_box_changed_failed", error=str(exc))
        return None



def message_box_text(window=None) -> str | None:
    """输入框里现在有哪些字（OCR 读出来）。``None`` = 探针失败，``""`` = 空的。"""
    from core.execution import wx_vision

    win = window or _wechat_window(force=True)
    if win is None:
        return None
    try:
        win, image, layout = _view(win)
        box = layout.input_box
        if not box:
            return None
        x0, y0, x1, y1 = box
        crop = image.crop((x0, y0, x1, y0 + max(20, int((y1 - y0) * 0.55))))
        items = wx_vision.ocr_items(crop)
    except Exception as exc:
        log.debug("message_box_text_failed", error=str(exc))
        return None
    text = "".join(str(item[0]) for item in items if item)
    flat = "".join(text.split())
    if any(marker in flat for marker in _MESSAGE_BOX_PLACEHOLDER_MARKERS):
        return ""
    return text.strip()


PICKER_MAX_CANDIDATES = 8
_PICKER_MAX_W = 700
_PICKER_MAX_H = 700
_PICKER_SCAN = 14


def _flat(text: str) -> str:
    """去掉所有空白：OCR 会把 "@Elaina" 读成 "@ Elaina"。"""
    return "".join((text or "").split())


def _picker_items(window=None) -> list[dict]:
    """读 @ 名单：优先 UI Automation 的 ``ListItemControl``（名字与矩形都准）。

    为什么不用 OCR：实测 OCR 文本框的纵坐标落在行**下半部**（「忽念」那一行的文字
    y≈1090，而它真实的矩形是 1038–1086），照着文字中心点下去会选到**下一个人**。
    微信 4.x 的名单是独立顶层窗口（``Qt51514QWindowToolSaveBits``），UIA 能直接读到
    ``ListItemControl`` 的 Name 和 BoundingRectangle。
    """
    try:
        import uiautomation as auto
        import win32gui
        import win32process
    except Exception:
        warn("core/execution/wx_chat.py:920 Exception", err=repr(__import__("sys").exc_info()[1]))
        return []
    win = window or _wechat_window(force=True)
    if win is None:
        return []
    main_hwnd = int(getattr(win, "hwnd", 0) or 0)
    if not main_hwnd:
        return []
    try:
        _thread, pid = win32process.GetWindowThreadProcessId(main_hwnd)
    except Exception:
        warn("core/execution/wx_chat.py:930 Exception", err=repr(__import__("sys").exc_info()[1]))
        return []
    if not pid:
        return []
    popups: list[tuple[int, tuple[int, int, int, int]]] = []

    def _cb(hwnd, _):
        if hwnd == main_hwnd or not win32gui.IsWindowVisible(hwnd):
            return
        try:
            _t, other = win32process.GetWindowThreadProcessId(hwnd)
        except Exception:
            warn("core/execution/wx_chat.py:941 Exception", err=repr(__import__("sys").exc_info()[1]))
            return
        if int(other or 0) != int(pid):
            return
        rect = tuple(int(v) for v in win32gui.GetWindowRect(hwnd))
        w, h = rect[2] - rect[0], rect[3] - rect[1]
        if w < 80 or h < 40 or w > _PICKER_MAX_W or h > _PICKER_MAX_H:
            return
        popups.append((int(hwnd), rect))

    try:
        win32gui.EnumWindows(_cb, None)
    except Exception:
        warn("core/execution/wx_chat.py:953 Exception", err=repr(__import__("sys").exc_info()[1]))
        return []

    items: list[dict] = []
    for hwnd, _rect in popups[:_PICKER_SCAN]:
        try:
            root = auto.ControlFromHandle(hwnd)
        except Exception:
            warn("core/execution/wx_chat.py:960 Exception", err=repr(__import__("sys").exc_info()[1]))
            continue
        queue = [root]
        while queue and len(items) < 64:
            node = queue.pop(0)
            try:
                kids = node.GetChildren()
            except Exception:
                warn("core/execution/wx_chat.py:967 Exception", err=repr(__import__("sys").exc_info()[1]))
                continue
            for kid in kids:
                try:
                    if kid.ControlTypeName == "ListItemControl":
                        box = kid.BoundingRectangle
                        items.append({
                            "name": str(kid.Name or "").strip(),
                            "x": (int(box.left) + int(box.right)) // 2,
                            "y": (int(box.top) + int(box.bottom)) // 2,
                        })
                        continue
                except Exception:
                    warn("core/execution/wx_chat.py:979 Exception", err=repr(__import__("sys").exc_info()[1]))
                queue.append(kid)
    return [item for item in items if item["name"]]


def mention_member(name: str, *, window=None, dry_run: bool = False, verify_chip: bool = True) -> dict:
    """在群里 @ 一个人：敲 ``@`` → 读 UIA 名单 → 只选唯一同名 → 回读输入框 → 验 chip。

    为什么这么小心：**@错人比不发更糟**——通知会真的送到别人手机上。四条都 fail closed：
    名单里没有同名 → 失败；同名多于一行 → 失败（不猜）；点完输入框不是恰好一个 ``@名字`` → 失败；
    退格后还剩字（说明只是文字，不是 chip）→ 失败。**任何失败路径都不按回车。**
    """
    from core.execution import wx_human

    target = (name or "").strip().lstrip("@")
    if not target:
        return {"ok": False, "error": "没说要 @ 谁"}
    win = window or _wechat_window(force=True)
    if win is None:
        return {"ok": False, "error": "找不到微信窗口"}
    if not focus_message_box(win):
        return {"ok": False, "error": "输入框没聚焦"}
    flat_target = _flat(target)
    want = "@" + flat_target
    try:
        def _pick(do_click: bool = True) -> dict:
            """敲 @、读名单、唯一同名才点。返回 dict（ok/error/candidates）。"""
            if not type_text("@" + target, via=VIA_UNICODE):
                return {"ok": False, "error": f"敲不出 @{target}"}
            wx_human.sleep_human(0.6, 0.3)
            items = _picker_items(win)
            candidates = [item["name"] for item in items][:PICKER_MAX_CANDIDATES]
            matched = [item for item in items if _flat(item["name"]) == flat_target]
            if not matched:
                return {"ok": False, "error": f"成员名单里没有 {target}", "candidates": candidates}
            if len(matched) > 1:
                return {"ok": False, "error": f"{target} 在名单里多行，不猜", "candidates": candidates}
            if do_click:
                wx_human.click(matched[0]["x"], matched[0]["y"])
                wx_human.sleep_human(0.45, 0.25)
            return {"ok": True, "candidates": candidates}

        first = _pick(do_click=not dry_run)
        if not first["ok"]:
            _clear_focused_field()
            return first
        composer = _flat(message_box_text(win) or "")
        if composer.count(want) != 1 or composer != want:
            _clear_focused_field()
            return {"ok": False, "error": "点完名单，输入框不是恰好一个 @名字",
                    "composer": composer[:60], "candidates": first["candidates"]}
        if dry_run:
            return {"ok": True, "dry_run": True, "matched": [target],
                    "candidates": first["candidates"]}
        if verify_chip:
            # 真的 @ 是一个整体：一次退格就整体消失；普通文字只会少一个字。
            wx_human.key_press(0x08)
            wx_human.sleep_human(0.25, 0.15)
            leftover = _flat(message_box_text(win) or "")
            if leftover:
                _clear_focused_field()
                return {"ok": False, "error": "退格还剩字：这是文字不是真 @",
                        "composer": leftover[:60]}
            second = _pick()
            if not second["ok"]:
                _clear_focused_field()
                return second
            final = _flat(message_box_text(win) or "")
            if final != want:
                _clear_focused_field()
                return {"ok": False, "error": "重新选一次没回到 @名字", "composer": final[:60]}
    except Exception as exc:
        log.info("mention_member_failed", error=str(exc), target=target)
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    return {"ok": True, "inserted": want, "via": "uia+chip",
            "candidates": first["candidates"]}


def clear_message_box(window=None) -> bool:
    """清空输入框并回读确认（先聚焦，再 Ctrl+A + Delete，最多两轮）。

    为什么要回读：上一轮任务超时可能在框里留下半句话，下一次**回车**就会把
    别人的半句发出去（历史上真出过）。``True`` = 现在读起来是空的；``False`` =
    清不掉、或清完还读得到字——调用方据此 fail closed，不许按回车。

    为什么要先聚焦（2026-11-04 真机）：切过去不等于焦点在输入框。原先只发
    Ctrl+A/Delete，那两下可能打在搜索框或会话列表上，框里的残句原样留着 →
    回读仍有字 → 返回 False。desk.log 里 ``desk_residue_cleared ok=false``
    连续出现三次，一条消息重试到第 10 次才发出去（主人在旁边看着像"她在三个
    会话之间来回跳"）。所以先点一下输入框把焦点拿回来，再清。
    """
    last_err = ""
    for attempt in (1, 2):
        try:
            focus_message_box(window)
            _clear_focused_field()
        except Exception as exc:  # noqa: BLE001 - 驱动异常当作没清掉
            last_err = repr(exc)[:120]
            log.warning("clear_message_box_failed", error=str(exc), attempt=attempt)
            return False
        if not str(message_box_text(window) or "").strip():
            return True
        if attempt == 1:
            time.sleep(0.2)
    log.warning("clear_message_box_still_dirty", attempts=2, last_error=last_err)
    return False


def _clear_focused_field() -> None:
    from core.execution import wx_human

    wx_human.hotkey(wx_human.VK_CONTROL, ord("A"))
    wx_human.key_press(0x2E)  # VK_DELETE


def _search_field_received(win, text: str, rect=None, rect_size=None) -> bool:
    """Whether the query actually landed in the search field.

    If the click did not focus the field, the paste goes to whatever does have
    focus -- typically the message box, where it sits as unsent text that the
    next Enter would deliver to the wrong person.  Reading the field back is the
    only way to know, and it is cheap.
    """
    from core.execution import wx_vision

    if rect is None:
        rect = _search_box_rect(win)
    elif rect_size is not None:
        try:
            win.refresh()
            if (int(win.width), int(win.height)) != tuple(rect_size):
                rect = _search_box_rect(win)
        except Exception:
            warn("core/execution/wx_chat.py:1097 Exception", err=repr(__import__("sys").exc_info()[1]))
            rect = _search_box_rect(win)
    if not rect:
        return True  # cannot verify; do not block the send path

    try:
        image, _rect = wx_vision.capture_window(win)
        result, _ = get_ocr()(_np_array(image.crop(rect)))
    except Exception as exc:
        log.debug("search_field_readback_failed", error=str(exc))
        return True

    if not result:
        return False

    seen = "".join(str(item[1]) for item in result)
    wanted = "".join(text.split())
    if not wanted:
        return True
    overlap = len(set(seen) & set(wanted))
    return overlap >= max(2, len(wanted) // 2)


def _np_array(image):
    import numpy as np

    return np.array(image)


def _ocr_search_results(who: str, search_rect=None, search_size=None) -> list[dict]:
    from core.execution import wx_vision

    win = _wechat_window()
    if win is None:
        return []
    win.refresh()
    if search_rect is not None and search_size is not None:
        if (int(win.width), int(win.height)) != tuple(search_size):
            search_rect = _search_box_rect(win)
    left, top = win.left, win.top

    search_left = left
    search_top = top
    search_right = left + 500
    search_bottom = top + 600

    try:
        img = wx_vision.capture((search_left, search_top, search_right, search_bottom))
    except Exception as exc:
        log.warning("ocr_capture_failed", error=str(exc))
        return []

    if _DEBUG_DUMPS:
        try:
            import os
            debug_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "debug")
            os.makedirs(debug_dir, exist_ok=True)
            debug_path = os.path.join(debug_dir, f"ocr_search_{int(time.time())}.png")
            img.save(debug_path)
            log.debug("ocr_screenshot_saved", path=debug_path)
        except Exception:
            warn("core/execution/wx_chat.py:1157 Exception", err=repr(__import__("sys").exc_info()[1]))

    import numpy as np

    ocr = get_ocr()
    result, _ = ocr(np.array(img))
    if not result:
        log.warning("ocr_no_result", who=who, region=(search_left, search_top, search_right, search_bottom))
        return []

    items = []
    for item in result:
        bbox, text, confidence = item
        text = text.strip()
        if not text:
            continue
        y_top = int(bbox[0][1])
        x_left = int(bbox[0][0])
        y_bottom = int(bbox[2][1])
        x_right = int(bbox[2][0])
        cx = (x_left + x_right) // 2
        cy = (y_top + y_bottom) // 2
        items.append({"text": text, "y": y_top, "x": x_left, "cx": cx, "cy": cy, "bbox": bbox, "is_header": text in _SECTION_HEADERS, "conf": confidence})

    items.sort(key=lambda x: x["y"])

    # The query we just typed is itself OCR-able and looks exactly like a
    # perfect match, so anything above the search field is discarded outright.
    cutoff = int(search_rect[3] + 6) if search_rect else _search_box_bottom(win)
    if cutoff:
        before = len(items)
        items = [item for item in items if item["y"] >= cutoff]
        log.debug("ocr_search_box_filtered", removed=before - len(items), cutoff=cutoff)

    log.debug("ocr_raw_items", who=who, texts=[i["text"] for i in items])

    first_header_y = None
    for item in items:
        if item["is_header"]:
            first_header_y = item["y"]
            break

    results = []
    for item in items:
        if first_header_y is not None and item["y"] < first_header_y and not item["is_header"]:
            continue
        text = item["text"]
        if not item["is_header"] and (text.startswith("微信号") or text.startswith("wxid_")):
            continue
        results.append(item)

    return results


def _verify_search_result(who: str, raw_who: str = "", search_rect=None,
                          search_size=None) -> tuple[bool, str, int, tuple[int, int] | None]:
    # Search results render while the query is being typed; this pause only
    # needs to cover the final repaint, so it rides the human-speed profile
    # instead of being a fixed 0.8s.
    from core.execution import wx_human

    wx_human.sleep_human(0.25, 0.08)
    results = _ocr_search_results(who, search_rect=search_rect, search_size=search_size)
    if not results:
        return False, "OCR未识别到搜索结果", -1, None

    all_names = [r["text"] for r in results]

    sections: list[tuple[str, list[dict]]] = []
    current_section = ""
    current_items: list[dict] = []
    for item in results:
        if item["is_header"]:
            if current_items:
                sections.append((current_section, current_items))
            current_section = item["text"]
            current_items = []
        else:
            current_items.append(item)
    if current_items:
        sections.append((current_section, current_items))

    # 2026-11-01 事故：搜索浮层没打开时，这一屏 OCR 读到的是左边的会话列表
    # （"幸福 家人(4)"、"文件传输助手"…），没有"最常使用/聊天记录/联系人/群聊"
    # 这些分组标题。旧逻辑会把它当成无标题分组照单全收，挑出"最像"的那行点下去，
    # 于是每次都打开上一个会话、被顶栏校验拦下、每 11 秒空转一轮。
    # 现在：一个标题都没有 = 浮层没开，直接失败，不点、不回车。
    _has_section = any(bool(name.strip()) for name, _items in sections)
    if not _has_section:
        # 有些情况下分组标题被裁剪掉了，但结果本身带这些标记 —— 也算浮层开着。
        _flat = " ".join(str(t) for t in all_names)
        _markers = ("搜索网络结果", "查看全部", "最常使用", "聊天记录", "搜索指定内容")
        _has_section = any(m in _flat for m in _markers)
    if not _has_section:
        return False, "搜索浮层没打开（读到的是会话列表）", -1, None

    preferred_sections: list[str] = []
    if raw_who.endswith("@chatroom"):
        preferred_sections = ["群聊", "最常使用", "联系人"]
    elif raw_who.startswith("wxid_"):
        preferred_sections = ["联系人", "最常使用", "群聊"]
    else:
        preferred_sections = ["最常使用", "联系人", "群聊"]

    who_lower = who.lower().strip()

    def _clean_row_name(text: str) -> str:
        """剔掉行尾的时间与省略号：'文件传输助..．18:29' -> '文件传输助'。"""
        t = _re.sub(r"\d{1,2}[:：]\d{2}", " ", text or "")
        t = _re.sub(r"[.．·…，,]+", "", t)
        return t.strip()

    def _match(text: str) -> bool:
        """会话行文字 vs 目标名。

        2026-10-27：会话列表里的名字会被截断并和时间粘在一起（OCR 读到
        「文件传输助..．18:29」「作业诚信互.」），原来只做子串判断，截断一个
        字就整条失败。现在先剔掉时间和省略号，再允许「候选是目标的省略前缀」。
        """
        raw = (text or "").lower().strip()
        t = _clean_row_name(raw)
        if not t:
            return False
        if t == who_lower or who_lower in t or t in who_lower:
            return True
        # 候选被截断成目标的前缀（至少 3 个字、且不短于目标的一半）
        if len(t) >= 3 and len(t) * 2 >= len(who_lower) and who_lower.startswith(t):
            return True
        return False

    for pref in preferred_sections:
        for sec_name, sec_items in sections:
            if sec_name != pref:
                continue
            for item in sec_items:
                if _match(item["text"]):
                    offset = 0
                    for sn, si in sections:
                        if sn == sec_name:
                            break
                        offset += len(si)
                    idx_in_sec = next(i for i, x in enumerate(sec_items) if x is item)
                    selectable_index = offset + idx_in_sec
                    click_pos = (item.get("cx"), item.get("cy"))
                    if click_pos[0] is not None and click_pos[1] is not None:
                        return True, f"OCR验证通过(分区='{sec_name}', 第{idx_in_sec+1}项): 目标='{who}', 匹配='{item['text']}', 全部={all_names}", selectable_index, click_pos
                    return True, f"OCR验证通过(分区='{sec_name}', 第{idx_in_sec+1}项): 目标='{who}', 匹配='{item['text']}', 全部={all_names}", selectable_index, None

    # No section headers matched: choose by how close the text is to the target.
    # The real contact row reads exactly the name, while WeChat's web
    # suggestions append fragments such as 打开微信 or 恢复历史记录.
    best_cost = None
    best_item = None
    best_index = -1
    selectable_count = 0
    for item in results:
        if item["is_header"]:
            continue
        text = item["text"].lower().strip()
        if _match(text):
            cost = abs(len(text) - len(who_lower))
            if best_cost is None or cost < best_cost:
                best_cost, best_item, best_index = cost, item, selectable_count
        selectable_count += 1

    if best_item is not None:
        click_pos = (best_item.get("cx"), best_item.get("cy"))
        reason = ("OCR验证通过(最接近匹配, 第%d个可选项): 目标='%s', 匹配='%s', 全部=%s"
                  % (best_index + 1, who, best_item["text"], all_names))
        if click_pos[0] is not None and click_pos[1] is not None:
            return True, reason, best_index, click_pos
        return True, reason, best_index, None

    non_header_names = [r["text"] for r in results if not r["is_header"]]

    # Deliberately no "just take the first row" fallback: the top row of a
    # failed search is usually a web suggestion, and acting on it is what sends
    # a message to the wrong person.  Failing loudly is much cheaper.
    return False, ("OCR验证失败: 未找到姓名的可靠匹配, 目标='%s', 候选=%s"
                   % (who, non_header_names)), -1, None


def _dump_click_moment(win, abs_x: int, abs_y: int, label: str = "click") -> str:
    """Save where we aimed, in both window and screen space.

    A click that lands one row off leaves no trace: by the time the chat title
    is checked the search overlay is gone and the list has already changed.
    Keeping the frame with the aim point drawn on it turns "the wrong chat
    opened" into something that can actually be looked at.

    Only active when WX_DEBUG_DUMPS is on; the captures cost ~0.3s and write
    private chat pixels to disk on every send otherwise.
    """
    if not _DEBUG_DUMPS:
        return ""

    saved = []
    try:
        from PIL import ImageDraw

        from core.execution import wx_paths

        out_dir = wx_paths.diagnose_dir()
        out_dir.mkdir(parents=True, exist_ok=True)

        def mark(image, x, y):
            draw = ImageDraw.Draw(image)
            draw.line([(x - 28, y), (x + 28, y)], fill=(255, 0, 0), width=3)
            draw.line([(x, y - 28), (x, y + 28)], fill=(255, 0, 0), width=3)
            draw.ellipse([x - 7, y - 7, x + 7, y + 7], outline=(255, 0, 0), width=3)
            return image

        # Both capture helpers return (image, rect); treating the tuple as an
        # image is why the first version saved nothing at all.
        shot = wx_vision.capture_window(win)
        shot = shot[0] if isinstance(shot, tuple) else shot
        if shot is not None:
            path = out_dir / (label + '_window.png')
            mark(shot, abs_x - win.rect[0], abs_y - win.rect[1]).save(path)
            saved.append(str(path))

        pad = 240
        wide = wx_vision.capture((abs_x - pad, abs_y - pad, abs_x + pad, abs_y + pad))
        wide = wide[0] if isinstance(wide, tuple) else wide
        if wide is not None:
            path = out_dir / (label + '_screen.png')
            mark(wide, pad, pad).save(path)
            saved.append(str(path))
    except Exception as exc:
        # Warning, not debug: an empty shot field is how "no evidence" stayed
        # invisible for a whole debugging round.
        log.warning("click_moment_dump_failed", error=str(exc))
    return ", ".join(saved)


def _remember_opened_chat(win, who: str, raw_who: str, *,
                          verified: bool = False) -> None:
    """Anchor the active-chat memory to the conversation that just opened.

    `verified=True` 表示调用方**刚刚**用会话顶栏验过"打开的就是目标"
    （2026-11-01：`_open_chat_once` 改成验过才算 ok 之后，这里就有真凭据了）。
    以前一律写 `title_verified=False`，于是 `try_reuse_chat(allow_unverified=False)`
    永远拒绝这条记忆——指纹复用那条"0.1 秒"的路一次都没走通过，每条消息都白付
    6–7 秒标题 OCR。
    """
    try:
        frame, _rect = wx_vision.capture_window(win)
        layout = wx_vision.detect_layout(frame)
    except Exception as exc:
        log.debug("wx_active_chat_capture_failed", error=str(exc))
        invalidate_active_chat("remember_capture_failed")
        return
    remember_active_chat(who, raw_who, win, frame, layout,
                         title_verified=bool(verified))


@ui_locked
def open_chat_strict(who: str, raw_who: str = "", *, verify_tries: int = 3) -> dict:
    """搜索 → 回车打开 → **用会话顶栏验收**。填字之前就得知道是哪个会话。

    为什么不用「在搜索列表里找到名字」当验收：列表会被 OCR 截断
    （''文件传输助手'' 读成 ''文件传输...''），按列表验收天天失败；而顶栏是
    将要被发进去的那块地方，它对了才是真对了。回车打开第一行也是微信自己
    的交互，不依赖搜索结果行的坐标。

    返回 ``{"ok": True, "who": name, "verified": True, "source": "header"}``；
    任何一步拿不准都返回 ``{"ok": False, "error": ...}``，调用方必须停下。
    """
    name = str(who or "").strip()
    if not name:
        return {"ok": False, "error": "no_target：没给会话名"}
    from core.execution import wx_human

    reuse = try_reuse_chat(name, raw_who=raw_who, allow_unverified=False)
    if reuse is not None and reuse.get("ok"):
        return reuse

    activated, search_rect, search_size = _activate_wechat_search()
    if not activated:
        return {"ok": False, "error": "无法激活微信搜索"}

    type_text(_clean_query(name) or name)
    wx_human.sleep_human(2.0, 0.25)

    win = _wechat_window(force=True)
    if win is not None and not _search_field_received(
            win, name, rect=search_rect, rect_size=search_size):
        if _query_landed_in_message_box(win, name):
            _clear_focused_field()
            log.warning("wx_search_landed_in_message_box", who=name)
            return {"ok": False,
                    "error": "搜索词落到了输入框而不是搜索框，已清空并中止以免发错人"}
        log.warning("wx_search_field_readback_inconclusive", who=name)

    # 2026-11-01 主人第二次纠正：**任何情况下都不许回车**。
    # 微信搜索框里回车 = 在全部资源里搜这个关键词（会跳到搜索结果页），
    # 不是打开会话；打开会话只能在浮层里点那一行。
    _okrow, _why, _idx, _pos = _verify_search_result(
        _clean_query(name) or name, raw_who=raw_who,
        search_rect=search_rect, search_size=search_size)
    if not _okrow or _pos is None:
        _clear_focused_field()
        return {"ok": False, "error": "搜索浮层里没有可点的结果行：%s" % (_why or "没有匹配")}
    ensure_foreground()
    _w2 = _wechat_window(force=True)
    if _w2 is None:
        return {"ok": False, "error": "not_connected：找不到微信窗口"}
    wx_human.click(*_w2.to_screen(int(_pos[0]), int(_pos[1])), aim_jitter=0.0)
    wx_human.sleep_human(1.2, 0.3)

    win = _wechat_window(force=True)
    if win is None:
        return {"ok": False, "error": "not_connected：找不到微信窗口"}
    if ensure_foreground() is None or wx_vision.foreground_hwnd() != win.hwnd:
        return {"ok": False,
                "error": "微信没有成为前台窗口，点击/输入会被系统吃掉，已中止以免发错人"}

    reason = ""
    for attempt in range(1, max(1, int(verify_tries)) + 1):
        verified, fatal = verify_active_chat_title(name, raw_who, win=win)
        if verified:
            log.info("wx_open_chat_strict_ok", who=name, attempt=attempt)
            return {"ok": True, "who": name, "verified": True, "source": "header"}
        if fatal:
            log.warning("wx_open_chat_strict_mismatch", who=name, reason=fatal)
            return {"ok": False, "error": fatal}
        reason = "顶栏没读出来"
        wx_human.sleep_human(0.5, 0.2)

    log.warning("wx_open_chat_strict_inconclusive", who=name, reason=reason)
    return {"ok": False,
            "error": "顶栏读不出会话名，不能确认是哪个会话，已中止（不猜）"}


def _clean_query(name: str) -> str:
    """送进搜索框的关键词：去掉未读数括号。

    2026-11-01 主人指出：`示例作业群（7）` 里的「（7）」是未读条数，
    带进搜索框就搜不到东西（微信不认这个名字）。
    """
    import re as _re
    return _re.sub(r"[\(（]\s*\d+\s*[\)）]", "", str(name or "")).strip()


def window_is_hung(win) -> bool:
    """窗口是不是已经不响应了（看起来还像微信，其实是一张截图）。"""
    try:
        import ctypes
        return bool(ctypes.windll.user32.IsHungAppWindow(int(getattr(win, "hwnd", 0) or 0)))
    except Exception:
        return False


def _norm_chat_name(text: str) -> str:
    """把会话名压成可比较的形状：去掉未读数后缀、时间尾巴、OCR 省略号与空白。"""
    import re as _re
    t = _re.sub(r"[\(（]\s*\d+\s*[\)）]", "", str(text or ""))
    t = _re.sub(r"[\s\u3000]*\d{1,2}\s*[:：]\s*\d{2}\s*$", "", t)   # "19:45" / "23：12" 尾巴
    t = t.replace("…", "").replace("．", "").replace(".", "")
    return _re.sub(r"\s+", "", t).lower()


def _name_matches(want: str, got: str) -> bool:
    """会话名是否算同一个人/群。

    列表里的名字常被 OCR 截断成「作业诚信互..」，所以允许"前缀互为前缀"；
    但短到 3 个字以下的不许模糊，免得「作业」把别的群也认成目标。
    """
    if not want or not got:
        return False
    if want == got:
        return True
    if len(got) >= 4 and (want.startswith(got) or got.startswith(want)):
        return True
    # OCR 常把列表里的名字截成两三个字（实测只读到「幸福」「文件传..」）。
    # 只要它是目标的前缀，就当候选；多个候选时由调用方按"匹配越长越优先"挑。
    if len(got) >= 2 and want.startswith(got):
        return True
    return len(got) >= 4 and got in want


def _open_by_uia_once(who: str, raw_who: str = ""):
    """2026-11-01：UIA 找行 + **只点一次** + 用 IsSelected 验。

    为什么这么写：会话列表在 UIA 里可读（名字/选中态），但 Invoke/Select 都是空操作；
    而反复失败重试会把微信 4.x 点成"死页面"（今晚实测）。所以这里点一下就验，
    验不过就明确失败、**绝不重试**。返回 None 表示 UIA 不可用，交给老路径。
    """
    try:
        from core.execution import wx_uia
    except Exception:
        return None
    rows = wx_uia.sessions()
    if not rows:
        return None
    want = _norm_chat_name(_clean_query(who))
    cands = [r for r in rows
             if want and (_norm_chat_name(str(r.get("title") or "")) == want
                          or _name_matches(want, _norm_chat_name(str(r.get("title") or ""))))]
    if not cands:
        return {"ok": False,
                "error": "会话列表里找不到「%s」（UIA 读到 %d 个会话）" % (who, len(rows))}
    row = cands[0]
    if row.get("selected"):
        log.info("wx_open_chat_uia", who=who, action="already_selected")
        return {"ok": True}
    _l, _t, _w, _h = row["rect"]
    from core.execution import wx_human  # 局部导入：wx_chat 顶部没有这个模块句柄
    _cx, _cy = int(_l + _w // 2), int(_t + _h // 2)
    log.info("wx_open_chat_uia", who=who, action="click_once", at=(_cx, _cy),
             row=str(row.get("title") or "")[:20])
    wx_human.click(_cx, _cy)
    wx_human.sleep_human(0.9, 0.2)
    _got = wx_uia.selected_title()
    _g, _wt = _norm_chat_name(_got), want
    if _g and (_g == _wt or _g in _wt or _wt in _g):
        log.info("wx_open_chat_uia", who=who, action="ok", now=_got)
        return {"ok": True}
    log.warning("wx_open_chat_uia_failed_once", who=who, now=_got or "?")
    return {"ok": False,
            "error": "点了一次没切过去（UIA 读到当前是「%s」，目标「%s」），已停手不再重试"
                     % (_got or "?", who)}


def _open_by_session_list(who: str, raw_who: str = "") -> dict:
    _uia = _open_by_uia_once(who, raw_who=raw_who)
    if _uia is not None:
        return _uia
    """在左侧会话列表里找到那一行点开——完全不碰搜索框。

    2026-11-01：主人指出「微信里回车是全平台搜索」，而搜索浮层又常被 OCR 读成
    会话列表，于是"搜索→回车/点击"这条路一直在开错人。左侧列表本来就摆着最近
    会话，直接读列表点行，再用顶部标题验证，少一层易错的浮层。
    """
    from core.execution import wx_human

    name = str(who or "").strip()
    if not name:
        return {"ok": False, "error": "no_target：没给会话名"}
    win = _wechat_window(force=True)
    if win is None:
        return {"ok": False, "error": "not_connected：找不到微信窗口"}
    if window_is_hung(win):
        log.warning("wx_window_hung", who=name, hwnd=int(getattr(win, "hwnd", 0) or 0))
        return {"ok": False,
                "error": "微信窗口不响应（像一张截图），点不动——需要重启微信"}
    if ensure_foreground() is None or wx_vision.foreground_hwnd() != win.hwnd:
        try:
            wx_vision.activate_window(win)  # 2026-11-02 修 F821：漏了模块前缀，激活步骤一直被吞
        except Exception as exc:
            log.info("wx_chat_aux_failed", where="open_by_session_list|verify", error=str(exc)[:120])
        wx_human.sleep_human(0.5, 0.1)
    try:
        frame, layout = wx_vision.claim_window_frame(win)
    except Exception as exc:
        return {"ok": False, "error": "抓不到微信画面：%s" % (exc,)}
    # 2026-11-01：这里原来用 frame_looks_like_wechat 当门槛，但它要求"会话区有内容"。
    # 微信在没人打开会话时右侧是空的（用户看到的"像截图一样"那一页就是这种），
    # 于是点列表这条路被自己的门槛挡死。点列表只需要"左侧会话列表在"。
    if not int(getattr(layout, "chat_left", 0) or 0):
        log.warning("wx_frame_not_wechat", who=name,
                    size=(int(getattr(frame, "width", 0) or 0),
                          int(getattr(frame, "height", 0) or 0)),
                    chat_left=int(getattr(layout, "chat_left", 0) or 0),
                    input_box=getattr(layout, "input_box", None),
                    header=getattr(layout, "header", None))
        return {"ok": False, "error": "画面上不是微信主界面（被遮挡/登录页/设置页/卡成截图）"}
    # 2026-11-01：窗口被拖动/缩放后，缓存里的窗口矩形会过期——抓到的图是新尺寸，
    # 而 to_screen() 还在用旧矩形算坐标，点下去就落空（表现就是"页面点不动"）。
    # 这里以刚抓到的帧为准，重新取一次窗口几何。
    _fw = int(getattr(frame, "width", 0) or 0)
    if _fw and abs(_fw - int(getattr(win, "width", 0) or 0)) > 8:
        log.info("wx_window_geometry_refresh", who=name,
                 cached=getattr(win, "width", None), frame=_fw)
        win = _wechat_window(force=True) or win
    left = int(getattr(layout, "chat_left", 0) or 0) or 377
    _w = int(getattr(frame, "width", 0) or 0) or left
    crop = frame.crop((0, 0, max(120, min(left, _w)), int(getattr(frame, "height", 0) or 0)))
    items = wx_vision.ocr_items(crop)
    want = _norm_chat_name(name)
    rows = []
    for text, cx, cy, _x, _y, conf in items:
        t = _norm_chat_name(text)
        if not t or not want:
            continue
        if _name_matches(want, t):
            rows.append((text, cx, cy, conf))
    if not rows:
        log.info("wx_list_row_missing", who=name,
                 seen=[str(i[0])[:24] for i in items][:14])
        return {"ok": False, "error": "会话列表里没看到「%s」" % name}
    # 名字匹配得越长越可信（「幸福一家人」比 OCR 只读到的「幸福」更可靠），
    # 同长度再按上下位置。
    rows.sort(key=lambda r: (-len(_norm_chat_name(str(r[0]))), r[2]))
    text, cx, cy, conf = rows[0]
    log.info("wx_list_row_click", who=name, row=str(text)[:40], y=int(cy),
             conf=round(float(conf), 2))
    # 2026-11-01：第一次点击常常被系统拿去"激活窗口"，于是点了没反应——
    # 用户看到的就是"页面像截图、点不动"。这里点两次：第二次才是真正落在那一行上。
    for _try in range(2):
        if _try:
            try:
                wx_vision.activate_window(win)  # 2026-11-02 修 F821：漏了模块前缀，激活步骤一直被吞
            except Exception as exc:
                log.info("wx_chat_aux_failed", where="click_retry", error=str(exc)[:120])
            wx_human.sleep_human(0.35, 0.1)
        wx_human.click(*win.to_screen(cx, cy), aim_jitter=0.0)
        wx_human.sleep_human(1.0 if _try == 0 else 1.4, 0.25)
        _ok0, _seen0 = verify_active_chat_title(_clean_query(name) or name, raw_who=raw_who)
        if _ok0:
            log.info("wx_list_row_opened", who=name, attempt=_try + 1)
            _remember_opened_chat(win, _clean_query(name) or name, raw_who, verified=True)
            return {"ok": True, "who": _clean_query(name) or name, "verified": True, "source": "list"}
        if _try == 0:
            log.info("wx_list_row_retry", who=name, seen=str(_seen0)[:50])
        else:
            # 2026-11-01：两次都点不动 = 微信这一侧"切会话"已经不动了
            # （实测：行会变绿、窗口不卡死、但没有"打开会话"这个动作）。
            # 继续点只会把它点成半死状态，必须停手交给人。
            log.warning("wx_open_chat_ui_stuck", who=name, seen=str(_seen0)[:60])
            return {"ok": False,
                    "error": "微信切不动会话（点了两次都没反应），需要人工重启微信后再试"}
    # 2026-11-01：校验用"去掉未读数括号"的名字——顶栏只写「示例作业群」，
    # 拿「示例作业群（7）」去比永远对不上，于是明明点对了也判失败。
    _pure = _clean_query(name) or name
    verified, seen = verify_active_chat_title(_pure, raw_who=raw_who)
    if verified:
        _remember_opened_chat(win, _pure, raw_who, verified=True)
        return {"ok": True, "who": _pure, "verified": True, "source": "list"}
    return {"ok": False, "error": "点了列表那行但打开的是：%s" % str(seen)[:70]}


def _open_chat_once(who: str, raw_who: str = "", *, ocr_verify: bool = True) -> dict:
    if ocr_verify:
        # A verified memory means this exact conversation was already found,
        # opened, and title-checked recently; skip the whole search.  Entries
        # that have not been title-checked are left for callers that will run
        # the check themselves (the sticker sender).
        reuse = try_reuse_chat(who, raw_who=raw_who, allow_unverified=False)
        if reuse is not None:
            return reuse
        # 2026-10-27 提速：记忆里只差一次标题校验时，先截一次顶栏 OCR 比对，
        # 对得上就沿用它——省掉「搜索→结果 OCR→点击」整轮（实测约 5 秒）。
        soft = try_reuse_chat(who, raw_who=raw_who, allow_unverified=False)
        if soft is not None:
            try:
                ok_soft, _why = verify_active_chat_title(who, raw_who=raw_who)
            except Exception:  # noqa: BLE001
                ok_soft = False
            if ok_soft:
                log.info("wx_chat_reuse_fastpath", who=who)
                return soft

    activated, search_rect, search_size = _activate_wechat_search()
    if not activated:
        return {"ok": False, "error": "无法激活微信搜索"}

    type_text(_clean_query(who) or who)
    from core.execution import wx_human

    wx_human.sleep_human(0.7, 0.15)

    win = _wechat_window(force=True)
    if win is not None and not _search_field_received(
            win, who, rect=search_rect, rect_size=search_size):
        if _query_landed_in_message_box(win, who):
            # The contact name is parked in the message box and is one stray
            # Enter away from being delivered to the wrong person.  Clear it
            # and refuse; searching blind is how messages reach the wrong chat.
            _clear_focused_field()
            log.warning("wx_search_landed_in_message_box", who=who)
            return {"ok": False,
                    "error": "搜索词落到了输入框而不是搜索框，已清空并中止以免发错人"}
        log.warning("wx_search_field_readback_inconclusive", who=who)

    if ocr_verify:
        verified, reason, target_index, click_pos = _verify_search_result(
            _clean_query(who) or who, raw_who=raw_who,
            search_rect=search_rect, search_size=search_size)
        if not verified:
            # Clear the field rather than pressing Escape.  Escape closes the
            # WeChat window, and the activation that follows restores it without
            # a conversation -- so every failed attempt made the next one fail
            # harder.  Ctrl+A/Delete only touches the text.
            _clear_focused_field()
            log.warning("wx_search_verify_failed", who=who, reason=reason)
            return {"ok": False, "error": f"搜索验证失败: {reason}"}
        log.info("wx_search_verify_ok", who=who, reason=reason)

        # 2026-11-01：这一段的旧写法是"点完搜索结果就 return ok"——点错了也报成功，
        # 全靠 send_text 里的标题硬闸兜（实测：窗口停在别的会话时，目标在第 6 行却
        # 点在第 1 行，打开的还是上一个会话，然后整条消息被中止，每 11 秒空转一轮）。
        # 现在改成：键盘选择优先（不依赖像素坐标）→ 回车 → **用同一套标题校验确认**，
        # 验过才算 ok；键盘不过再用点击兜一次，两次都不过就带着原因返回失败。
        win = _wechat_window(force=True)
        if win is not None:
            plans: list[tuple] = []
            # 2026-11-01：键盘优先，而且**先试第 1 个**。微信搜索浮层的第一行
            # 就是"最常使用"里最匹配的那条；OCR 给出的 target_index 掺着会话列表
            # 和聊天记录行，曾经算出"第 6 个"导致连按 7 次下方向键、开会话时按的是
            # 模糊搜索没结果，白跑一轮。第一行 + 回车，才是这条查询真正的命中项。
            # 息屏时抓屏可能是旧帧（点击落点会对不上），键盘不依赖坐标，也躲开那一类。
            # 2026-11-01 主人第二次纠正：这一段**一次都不许回车**。
            # 微信搜索框里回车 = 在全部资源里搜这个关键词，不是打开会话；
            # 打开会话只能点浮层里那一行（鼠标点）。
            if click_pos is not None:
                plans.append(("click", click_pos))
            last_error = ""
            for _via, _arg in plans:
                if _via == "keyboard":
                    press_down(int(_arg))
                    time.sleep(0.25)
                else:
                    if ensure_foreground() is None or \
                            wx_vision.foreground_hwnd() != win.hwnd:
                        log.warning("wx_search_click_skipped_not_foreground",
                                    who=who, foreground=wx_vision.foreground_hwnd(),
                                    target=win.hwnd)
                        last_error = "微信没有成为前台窗口，点击会被系统吃掉"
                        continue
                    abs_x, abs_y = win.to_screen(_arg[0], _arg[1])
                    dumped = _dump_click_moment(win, abs_x, abs_y,
                                                label="click_%s" % who)
                    log.info("wx_search_click_aim", who=who,
                             ocr_pos=_arg, abs=(abs_x, abs_y),
                             rect=win.rect, foreground=wx_vision.foreground_hwnd(),
                             target=win.hwnd, shot=dumped)
                    wx_human.click(abs_x, abs_y, aim_jitter=1.5)
                if _via == "keyboard":
                    # 只有"↓ 选中某一行的键盘方案"才回车；点击方案已经点开了，
                    # 再回车等于在聊天窗口里敲一个空回车（更糟的是会触发全平台搜索）。
                    press_enter()
                wx_human.sleep_human(0.9, 0.2)
                _ok_now, _seen_now = (not ocr_verify), ""
                if ocr_verify:
                    try:
                        _ok_now, _seen_now = verify_active_chat_title(who, raw_who=raw_who)
                    except Exception as exc:  # noqa: BLE001
                        warn("wx_open_chat_once_verify_failed",
                             err=repr(exc))
                        _ok_now, _seen_now = (False, "")
                log.info("wx_open_chat_attempt", who=who, via=_via,
                         arg=str(_arg)[:40], ok=bool(_ok_now),
                         seen=str(_seen_now)[:120])
                if _ok_now:
                    if ocr_verify:
                        # 顶栏刚验过 → 这条记忆可以标成 verified，下一次
                        # try_reuse_chat(allow_unverified=False) 才可能命中指纹。
                        _remember_opened_chat(win, who, raw_who, verified=True)
                    return {"ok": True}
                last_error = str(_seen_now) or "顶栏读不到会话名"
            if last_error:
                try:
                    _clear_focused_field()
                except Exception as exc:  # noqa: BLE001
                    warn("wx_open_chat_clear_failed", err=repr(exc))
                return {"ok": False,
                        "error": "打开会话后校验不过：%s" % last_error}

    # 2026-11-01：这里原来是「没验过也按回车、然后报 ok」——回车在微信里是
    # 全平台搜索，按下去只会跳到搜索结果页，还把窗口状态搞乱（下一步又要重新搜）。
    # 宁可诚实失败，也不发这一步。
    log.warning("wx_open_chat_no_safe_row", who=who, had_click=bool(click_pos),
                index=target_index)
    return {"ok": False,
            "error": "没有可安全打开的结果行（已放弃回车：微信里回车是全平台搜索）"}


def open_chat(who: str, raw_who: str = "", *, ocr_verify: bool = True) -> dict:
    """依次用「这个名字的各个别名」尝试打开会话。

    为什么需要：微信搜索框只认名字，而名字会变（群改名、好友改昵称）。
    只要别名簿里记着它曾经叫什么，改名就不会让发送链瘫掉——上游给的名字
    可能是旧的，这里自动换用别名再试，任一成功即返回。
    只有一个候选时，行为与从前完全一致。
    """
    try:
        from core.execution import chat_alias as _alias
    except Exception:
        _alias = None
    asked = str(who or "").strip()
    cands = _alias.candidates_for(who, raw_who) if _alias is not None else [asked]
    if not cands:
        cands = [asked]
    out: dict = {"ok": False, "error": "no_target：没给会话名"}
    # 2026-11-01：先试「直接点左侧会话列表」，不碰搜索框（回车在全平台搜索这一步
    # 最容易开错人）。列表里看不到这个名字，再退回搜索流程。
    _lst = _open_by_session_list(asked, raw_who=raw_who)
    if _lst.get("ok"):
        if _alias is not None:
            try:
                _alias.learn(raw_who or asked, asked, source="list_ok")
            except Exception as exc:
                log.info("wx_chat_aux_failed", where="click_retry", error=str(exc)[:120])
        return _lst
    if str(_lst.get("error") or "").startswith("点了列表那行"):
        log.warning("wx_open_chat_list_mismatch", who=asked, error=str(_lst.get("error"))[:90])
    for idx, cand in enumerate(cands):
        out = _open_chat_once(cand, raw_who=raw_who, ocr_verify=ocr_verify)
        if out.get("ok"):
            if idx:
                log.info("wx_open_chat_alias_used", asked=asked, used=cand)
            if _alias is not None:
                try:
                    _alias.learn(raw_who or asked, cand, source="search_ok")
                except Exception:
                    pass
            return out
        if len(cands) > 1:
            log.warning("wx_open_chat_alias_retry", asked=asked, tried=cand,
                        error=str(out.get("error"))[:90])
            try:
                _clear_focused_field()
            except Exception as exc:
                log.info("wx_chat_aux_failed", where="click_retry", error=str(exc)[:120])
    return out


