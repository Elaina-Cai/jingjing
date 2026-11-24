# 时长只存毫秒（2026-10-28，UI-066）。
# 规矩：存储与传递只用毫秒整数；to_ms 是唯一入口，human 是唯一出口。
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from core.infra import duration  # noqa: E402


@pytest.mark.parametrize("value, want", [
    (1500, 1500),
    ("400ms", 400),
    ("45s", 45_000),
    ("2m", 120_000),
    ("1.5s", 1500),
    ("500MS", 500),
    (" 2 m ", 120_000),
    ("3h", 10_800_000),
])
def test_to_ms_reads_int_and_unit_strings(value, want):
    assert duration.to_ms(value) == want


@pytest.mark.parametrize("bad", ["", "abc", "-5s", -1, None, "1w"])
def test_to_ms_rejects_junk(bad):
    with pytest.raises((ValueError, TypeError)):
        duration.to_ms(bad)


def test_to_ms_refuses_float_seconds_and_bool():
    with pytest.raises(TypeError):
        duration.to_ms(1.5)
    with pytest.raises(TypeError):
        duration.to_ms(True)


def test_human_is_the_only_render_exit():
    assert duration.human(7) == "7 毫秒"
    assert duration.human(1500) == "1.5 秒"
    assert duration.human(-1500).startswith("-")
    assert duration.human("x") == "未知"


def test_deadline_ms_is_the_only_death_formula():
    assert duration.deadline_ms(1_000, 10_000) == 31_000
    assert duration.deadline_ms(1_000, 10_000, grace=2.0) == 21_000


def test_epoch_round_trip():
    assert duration.epoch_s(1_500) == 1.5
    assert duration.from_epoch_s(1793199302.7439) == 1793199302744
    assert duration.human_s(1.5) == duration.human(1500)
