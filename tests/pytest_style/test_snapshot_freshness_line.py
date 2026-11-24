"""“视界里最新一条消息”必须取自消息时间，不是快照生成时间（2026-10-30）。"""

from __future__ import annotations

import inspect

from loop import snapshot as S


def _cell(last_at: str, chat_id: str = "a@chatroom") -> dict:
    return {"chat_id": chat_id, "last_at": last_at}


def test_newest_at_takes_the_latest_message():
    assert S.newest_at([_cell("2026-10-29 20:26"), _cell("2026-10-30 18:40", "b")]) == "2026-10-30 18:40"


def test_newest_at_ignores_order():
    cells = [_cell("2026-10-30 18:40", "b"), _cell("2026-10-29 20:26")]
    assert S.newest_at(cells) == "2026-10-30 18:40"


def test_newest_at_is_empty_without_times():
    assert S.newest_at([_cell(""), _cell("")]) == ""
    assert S.newest_at([]) == ""


def test_render_text_uses_the_message_clock():
    """锁定：这一句的取值必须走 newest_at，不许再读 snap["at"]（那是构建时刻）。"""
    src = inspect.getsource(S.render_text)
    assert "newest_at(" in src
    assert 'snap.get("at")' not in src


def test_epoch_of_accepts_seconds_and_minutes():
    """last_at 带秒（18:16:40），曾经因为只认 %H:%M 而整句消失。"""
    from loop.snapshot import _epoch_of
    assert _epoch_of("2026-10-29 18:16:40") is not None
    assert _epoch_of("2026-10-29 18:16") is not None
    assert _epoch_of("") is None
    assert _epoch_of("不是时间") is None
