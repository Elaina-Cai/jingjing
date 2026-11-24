"""微信 4.x 的 UIA 只读通道（2026-11-01）。

背景：4.x 的窗口类名是 ``mmui::MainWindow``（不是 Qt51514QWindowIcon）；会话列表在 UIA 里
是可读控件（``ListControl``「会话」+ 若干 ``ListItemControl``），每项 ``Name`` 是
「会话名\n预览\n时间」三段拼接，``SelectionItemPattern.IsSelected`` 可读 —— 于是
"现在打开的是哪个会话"有了不依赖 OCR 的硬判据。

但 Invoke / Select / SetFocus+Enter 全是空操作（实测，见 docs/交接.md），
所以这里只负责"看"，动作仍要用真实鼠标（``wx_chat._open_by_uia_once``）。

"""
from __future__ import annotations



try:
    import structlog
    log = structlog.get_logger("core.execution.wx_uia")
except Exception:  # pragma: no cover
    import logging
    log = logging.getLogger(__name__)

def _auto():
    try:
        import uiautomation as auto
        auto.SetGlobalSearchTimeout(2.5)
        return auto
    except Exception as exc:
        log.info("wx_uia_auto_unavailable", error=str(exc)[:120])
        return None


def window():
    auto = _auto()
    if auto is None:
        return None
    try:
        for w in auto.GetRootControl().GetChildren():
            try:
                if (w.ClassName or "") == "mmui::MainWindow":
                    return w
            except Exception as exc:
                log.info("wx_uia_children_failed", error=str(exc)[:120])
                continue
    except Exception as exc:
        log.info("wx_uia_auto_unavailable", error=str(exc)[:120])
        return None
    return None


def sessions() -> list:
    """返回 [{'title','name','rect':(l,t,w,h),'selected'}, ...]；UIA 不可用时返回 []。"""
    auto = _auto()
    win = window()
    if auto is None or win is None:
        return []
    try:
        lst = auto.ListControl(searchFromControl=win, searchDepth=14)
        if not lst:
            return []
        kids = lst.GetChildren() or []
    except Exception as exc:
        log.info("wx_uia_sessions_root_failed", error=str(exc)[:120])
        return []
    out = []
    for k in kids:
        try:
            nm = k.Name or ""
            r = k.BoundingRectangle
            sel = False
            try:
                sel = bool(k.GetSelectionItemPattern().IsSelected)
            except Exception as exc:
                log.info("wx_uia_session_pattern_failed", error=str(exc)[:120])
            out.append({
                "title": (nm.split("\n")[0] or "").strip(),
                "name": nm,
                "rect": (r.left, r.top, r.width(), r.height()),
                "selected": sel,
            })
        except Exception as exc:
            log.info("wx_uia_session_row_failed", error=str(exc)[:120])
            continue
    return out


def selected_title() -> str:
    for s in sessions():
        if s.get("selected"):
            return str(s.get("title") or "")
    return ""
