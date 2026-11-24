"""群里过期的 @ 不能一直算本轮必答（loop/addressing.py）。"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

from loop.addressing import verdict

NAMES = ["鲸鲸"]


def _stamp(ago_s: float) -> str:
    at = datetime.now().timestamp() - ago_s
    return datetime.fromtimestamp(at).strftime("%Y-%m-%d %H:%M:%S")


def _row(text: str, at, *, sender_id: str = "u1", msg_id: str = "m1"):
    return SimpleNamespace(sender_id=sender_id, text=text, at=at, is_self=0, msg_id=msg_id)


def test_an_at_four_days_old_is_not_current():
    v = verdict([_row("@鲸鲸 在吗", _stamp(4 * 24 * 3600))], names=NAMES)
    assert v.kind == ""
    assert v.must_answer is False


def test_an_at_eight_hours_old_is_still_current():
    v = verdict([_row("@鲸鲸 在吗", _stamp(8 * 3600))], names=NAMES)
    assert v.kind == "named"
    assert v.must_answer is True


def test_an_at_pushed_away_by_newer_group_chatter_is_not_current():
    rows = [_row("@鲸鲸 在吗", _stamp(10 * 3600), msg_id="m1"),
            _row("今天天气不错", _stamp(1 * 3600), sender_id="u2", msg_id="m2")]
    v = verdict(rows, names=NAMES)
    assert v.kind == ""


def test_an_at_with_unreadable_time_is_still_current():
    v = verdict([_row("@鲸鲸 在吗", 0)], names=NAMES)
    assert v.kind == "named"
    assert v.must_answer is True
