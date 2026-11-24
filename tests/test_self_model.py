# -*- coding: utf-8 -*-
"""Step 36 self model + salience checks: standalone + pytest compatible."""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.context import salience as sa  # noqa: E402
from core.context.situation import (  # noqa: E402
    WorldModel,
    note_self_message,
    project,
    render_situation,
)

NOW = datetime(2026, 10, 14, 12, 0, 0)
AT = NOW.strftime("%Y-%m-%d %H:%M")


def _salience_env(value):
    old = os.environ.get("YINYUE_FEATURE_SALIENCE")
    if value is None:
        os.environ.pop("YINYUE_FEATURE_SALIENCE", None)
    else:
        os.environ["YINYUE_FEATURE_SALIENCE"] = value
    return old


def _restore_salience(old):
    if old is None:
        os.environ.pop("YINYUE_FEATURE_SALIENCE", None)
    else:
        os.environ["YINYUE_FEATURE_SALIENCE"] = old


def test_note_self_message_projection_and_tail():
    model = WorldModel()
    for i in range(55):
        note_self_message(model, content="m%d" % i, at=AT)
    view = project(model.events)
    assert len(view.self_messages) == 50, len(view.self_messages)
    assert view.self_messages[-1].content == "m54"
    assert view.self_messages[-1].kind == "reply"


def test_note_self_message_ignores_empty_and_truncates():
    model = WorldModel()
    note_self_message(model, content="   ", at=AT)
    assert project(model.events).self_messages == ()
    note_self_message(model, content="x" * 600, at=AT)
    item = project(model.events).self_messages[-1]
    assert len(item.content) == 500 and item.content.endswith("…")


def test_salience_mode_tri():
    old = _salience_env(None)
    try:
        assert sa.salience_mode() == "off"
        os.environ["YINYUE_FEATURE_SALIENCE"] = "shadow"
        assert sa.salience_mode() == "shadow"
        os.environ["YINYUE_FEATURE_SALIENCE"] = "on"
        assert sa.salience_mode() == "on"
    finally:
        _restore_salience(old)


def test_event_salience_decay():
    model = WorldModel()
    model.add("emotion_observed", {"level": 0.4, "reason": "defended"}, at=AT)
    fresh = model.events[-1]
    assert abs(sa.event_salience(fresh, now=NOW) - 1.0) < 1e-9
    old_at = (NOW - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M")
    model.add("emotion_observed", {"level": 0.1, "reason": "left_out"}, at=old_at)
    aged = model.events[-1]
    assert abs(sa.event_salience(aged, now=NOW) - 0.5) < 1e-9
    unparsed = sa.event_salience(fresh, now=None)
    assert abs(unparsed - 1.0) < 1e-9


def test_top_salient_orders_and_labels():
    model = WorldModel()
    model.add("self_message", {"content": "在吗"}, at=AT)
    model.add("emotion_observed", {"level": 0.4, "reason": "defended"}, at=AT)
    model.add("message_observed", {"who": "P1", "text": "聊聊"}, at=AT)
    items = sa.top_salient(model.events, now=NOW)
    assert items and items[0].kind == "emotion_observed"
    assert items[0].label == "defended"
    assert items[1].kind == "self_message" and items[1].label == "在吗"
    digest = sa.salience_digest(model, now=NOW)
    assert set(digest) == {"top", "count"}
    assert digest["count"] == 3 and digest["top"][0]["kind"] == "emotion_observed"


def test_render_situation_salience_line_only_on():
    model = WorldModel()
    note_self_message(model, content="在吗", at=AT)
    old = _salience_env(None)
    try:
        assert "要紧" not in render_situation(model, now=NOW)
        os.environ["YINYUE_FEATURE_SALIENCE"] = "shadow"
        assert "要紧" not in render_situation(model, now=NOW)
        os.environ["YINYUE_FEATURE_SALIENCE"] = "on"
        text = render_situation(model, now=NOW)
        assert "要紧" in text and "在吗" in text
    finally:
        _restore_salience(old)


def test_static_wiring():
    import inspect

    from core.speech.outbound import OutboundGateway

    send_src = inspect.getsource(OutboundGateway._send_text_or_file)
    assert send_src.count("self._note_self_message(applied, snapshot)") == 1, \
        "出稿链没把说过的话投影给自我模型（或重复投影）"
    readings = (ROOT / "core/speech/tone_readings.py").read_text(encoding="utf-8")
    assert "self_model_shadow" in readings and "self_model_write" in readings
    assert not (ROOT / "core/context/scene_effects.py").exists(), \
        "场景运行段随 N1 退役（显著度影子读数不再由它记）"
    situation = (ROOT / "core/context/situation.py").read_text(encoding="utf-8")
    assert "def note_self_message(" in situation and "SELF_MESSAGE_TAIL_MAX = 50" in situation


def main() -> bool:
    tests = [
        test_note_self_message_projection_and_tail,
        test_note_self_message_ignores_empty_and_truncates,
        test_salience_mode_tri,
        test_event_salience_decay,
        test_top_salient_orders_and_labels,
        test_render_situation_salience_line_only_on,
        test_static_wiring,
    ]
    passed = 0
    for test in tests:
        try:
            test()
            print("PASS", test.__name__)
            passed += 1
        except AssertionError as exc:
            print("FAIL", test.__name__, exc)
    print("self-model checks: %d/%d passed" % (passed, len(tests)))
    return passed == len(tests)


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
