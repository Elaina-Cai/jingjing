# -*- coding: utf-8 -*-
"""Step 35 emotion layers (mood/attitude/tone) checks: standalone + pytest compatible."""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.context import emotion as em  # noqa: E402
from core.context.situation import WorldModel, note_emotion, render_situation  # noqa: E402

NOW = datetime(2026, 10, 14, 12, 0, 0)
ENV = "YINYUE_FEATURE_EMOTION_LAYERS"


def _model(*rows):
    model = WorldModel()
    for level, who, who_id, hours_ago in rows:
        at = (NOW - timedelta(hours=hours_ago)).strftime("%Y-%m-%d %H:%M")
        note_emotion(model, level=level, reason="r", who=who, who_id=who_id, at=at)
    return model


def test_mood_fold_with_half_life():
    mood = em.current_mood(_model((0.4, "P1", "p1", 0.0), (-0.6, "P2", "p2", 48.0)), now=NOW)
    assert abs(mood.level - 0.1) < 1e-6, mood.level
    assert mood.samples == 2 and mood.label == "平"


def test_mood_clamped():
    model = _model((1.0, "P1", "p1", 0.0), (1.0, "P1", "p1", 1.0))
    assert em.current_mood(model, now=NOW).level == 1.0


def test_attitude_is_per_person():
    model = _model((0.6, "P1", "p1", 0.0), (-0.8, "P2", "p2", 0.0))
    att1 = em.attitude_toward(model, who_id="p1", now=NOW)
    att2 = em.attitude_toward(model, who_id="p2", now=NOW)
    assert abs(att1.level - 0.6) < 1e-6 and att1.samples == 1
    assert abs(att2.level + 0.8) < 1e-6 and att2.samples == 1


def test_labels_and_tone():
    assert em._label_of(0.6) == "很暖" and em._label_of(0.2) == "偏暖"
    assert em._label_of(0.0) == "平" and em._label_of(-0.3) == "偏冷"
    assert em._label_of(-0.7) == "很冷"
    assert "收着点" in em.tone_hint(em.MoodState(level=-0.6), em.MoodState(level=-0.6), who="P1")
    assert "放松" in em.tone_hint(em.MoodState(level=0.4), em.MoodState(level=0.4), who="P1")
    assert "照常" in em.tone_hint(em.MoodState(), em.MoodState(), who="P1")


def test_half_life_env_and_clamp():
    old = os.environ.get("YINYUE_MOOD_HALF_LIFE_HOURS")
    try:
        os.environ["YINYUE_MOOD_HALF_LIFE_HOURS"] = "24"
        assert em.mood_half_life_hours() == 24.0
        os.environ["YINYUE_MOOD_HALF_LIFE_HOURS"] = "0.5"
        assert em.mood_half_life_hours() == 1.0
        os.environ["YINYUE_MOOD_HALF_LIFE_HOURS"] = "0"
        assert em.mood_half_life_hours() == em.MOOD_HALF_LIFE_HOURS
        os.environ["YINYUE_MOOD_HALF_LIFE_HOURS"] = "nope"
        assert em.mood_half_life_hours() == em.MOOD_HALF_LIFE_HOURS
        os.environ.pop("YINYUE_MOOD_HALF_LIFE_HOURS", None)
        assert em.mood_half_life_hours() == em.MOOD_HALF_LIFE_HOURS
    finally:
        if old is None:
            os.environ.pop("YINYUE_MOOD_HALF_LIFE_HOURS", None)
        else:
            os.environ["YINYUE_MOOD_HALF_LIFE_HOURS"] = old


def test_layers_mode_off_shadow_on():
    old = os.environ.get(ENV)
    try:
        os.environ.pop(ENV, None)
        assert em.layers_mode() == "off"
        os.environ[ENV] = "shadow"
        assert em.layers_mode() == "shadow"
        os.environ[ENV] = "on"
        assert em.layers_mode() == "on"
    finally:
        if old is None:
            os.environ.pop(ENV, None)
        else:
            os.environ[ENV] = old


def test_render_line_only_on():
    model = _model((0.4, "P1", "p1", 0.0))
    old = os.environ.get(ENV)
    try:
        os.environ.pop(ENV, None)
        assert "心境" not in render_situation(model, now=NOW)
        os.environ[ENV] = "shadow"
        assert "心境" not in render_situation(model, now=NOW)
        os.environ[ENV] = "on"
        text = render_situation(model, now=NOW)
        assert "心境" in text and "态度" in text
    finally:
        if old is None:
            os.environ.pop(ENV, None)
        else:
            os.environ[ENV] = old


def test_layers_digest_shape():
    digest = em.layers_digest(
        _model((0.4, "P1", "p1", 0.0), (-0.5, "P2", "p2", 0.0)),
        who_id="p1", who="P1", now=NOW,
    )
    assert set(digest) == {
        "mood_level", "mood_label", "mood_samples",
        "attitude_level", "attitude_label", "attitude_samples", "tone",
    }
    assert digest["attitude_samples"] == 1


def test_tone_prompt_block_and_neutral_skip() -> None:
    cold = em.tone_prompt_block(_model((-0.8, "P1", "p1", 0.0)), who_id="p1", who="P1", now=NOW)
    assert cold.startswith("[情绪语气]") and "收着点" in cold
    warm = em.tone_prompt_block(_model((0.6, "P1", "p1", 0.0)), who_id="p1", who="P1", now=NOW)
    assert "放松" in warm
    assert em.tone_prompt_block(WorldModel(), who_id="p1", who="P1", now=NOW) == ""


def main() -> bool:
    tests = [
        test_mood_fold_with_half_life, test_mood_clamped,
        test_attitude_is_per_person, test_labels_and_tone,
        test_half_life_env_and_clamp, test_layers_mode_off_shadow_on,
        test_render_line_only_on, test_layers_digest_shape,
        test_tone_prompt_block_and_neutral_skip,
    ]
    passed = 0
    for test in tests:
        try:
            test()
            print("PASS", test.__name__)
            passed += 1
        except AssertionError as exc:
            print("FAIL", test.__name__, exc)
    print("emotion-layers checks: %d/%d passed" % (passed, len(tests)))
    return passed == len(tests)


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
