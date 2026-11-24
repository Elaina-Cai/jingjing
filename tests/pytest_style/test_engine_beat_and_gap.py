"""引擎心跳里的「空白」与 watchdog 读心跳（2026-11-06 A）。

背景：11-06 凌晨 02:54 机器进入 Modern Standby，14:34 才醒。`logs/exits.jsonl` 把这段
记成 `orphan_cleaned`，`quiet_alert` 说「她已经 19.1 小时没有自己说过一句话了…（或她自己
不肯开口）」，`down_alert` 因为服务日志 mtime 被刷新而算成 0 小时 —— 「她不在」和
「她不想说」在账本里长得一模一样。

这一组守两件事：
1. 引擎把「刚才那段空白」写进心跳（只跟同一个 pid 比，跨重启的空白归启动账）；
2. watchdog 读引擎心跳：空白 ≥ 1 小时记一条 `engine_gap`，心跳陈旧 > 15 分钟记一条
   `engine_stale`；主人按了「全关」时不喊心跳陈旧（那是她自己关的）。
"""

import json
import os
import time

import pytest

from loop import run as R

import scripts.watchdog_yinyue as W

HOUR = 3600.0


def _beat(path, **kw):
    data = {"pid": os.getpid(), "at_ms": int(time.time() * 1000), "beat_ms": 1000,
            "at_text": "2026-11-06 14:34:00"}
    data.update(kw)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(json.dumps(data).encode("utf-8"))
    return data


def _rows(path):
    if not path.exists():
        return []
    raw = path.read_bytes().decode("utf-8")
    return [json.loads(line) for line in raw.splitlines() if line.strip()]


def test_a_suspend_shows_up_as_a_gap():
    prev = {"pid": 42, "at_ms": 1_000_000, "at_text": "2026-11-06 02:54:37"}
    got = R._heartbeat_gap(prev, 1_000_000 + int(11.7 * HOUR * 1000), 42)
    assert got["gap_ms"] == int(11.7 * HOUR * 1000)
    assert got["gap_prev_text"] == "2026-11-06 02:54:37"
    assert got["gap_at_ms"] == 1_000_000 + int(11.7 * HOUR * 1000)


def test_a_short_pause_is_not_a_gap():
    prev = {"pid": 42, "at_ms": 1_000_000, "at_text": "x"}
    assert R._heartbeat_gap(prev, 1_000_000 + 5 * 60 * 1000, 42) == {}


def test_a_restart_is_not_a_gap():
    """换了 pid 就是重启：那段空白由启动账记，心跳里再记一次会被数两遍。"""
    prev = {"pid": 41, "at_ms": 1_000_000, "at_text": "x"}
    assert R._heartbeat_gap(prev, 1_000_000 + int(12 * HOUR * 1000), 42) == {}


def test_the_heartbeat_is_read_back_with_its_gap(tmp_path):
    beat = _beat(tmp_path / "loop_engine.json", gap_ms=int(2.5 * HOUR * 1000),
                 gap_at_ms=int(time.time() * 1000), gap_prev_text="2026-11-06 02:54:37")
    got = W.engine_beat(path=tmp_path / "loop_engine.json")
    assert got["ok"] is True
    assert got["gap_hours"] == pytest.approx(2.5, abs=0.01)
    assert got["gap_prev_text"] == "2026-11-06 02:54:37"
    assert got["age_s"] is not None and got["age_s"] < 60
    assert got["pid"] == beat["pid"]


def test_a_missing_heartbeat_is_not_an_alarm(tmp_path):
    alerts = tmp_path / "watchdog.jsonl"
    got = W._check_engine(alerts, beat_path=tmp_path / "nope.json")
    assert got["ok"] is False
    assert _rows(alerts) == []


def test_a_long_gap_is_reported_once(tmp_path):
    now = time.time()
    beat = tmp_path / "loop_engine.json"
    _beat(beat, gap_ms=int(11.7 * HOUR * 1000), gap_at_ms=int(now * 1000),
          gap_prev_text="2026-11-06 02:54:37")
    alerts = tmp_path / "watchdog.jsonl"
    W._check_engine(alerts, now=now, beat_path=beat)
    (row,) = _rows(alerts)
    assert row["action"] == "engine_gap"
    assert row["gap_hours"] == pytest.approx(11.7, abs=0.1)
    assert "不是她不肯开口" in row["note2"]
    assert "02:54:37" in row["note2"]
    W._check_engine(alerts, now=now + 300, beat_path=beat)
    assert len(_rows(alerts)) == 1, "同一段空白只该记一条"


def test_a_stale_heartbeat_is_reported(tmp_path):
    now = time.time()
    beat = tmp_path / "loop_engine.json"
    _beat(beat, at_ms=int((now - 20 * 60) * 1000), at_text="2026-11-06 19:30:26")
    alerts = tmp_path / "watchdog.jsonl"
    W._check_engine(alerts, now=now, beat_path=beat)
    (row,) = _rows(alerts)
    assert row["action"] == "engine_stale"
    assert row["heartbeat_age_s"] >= 19 * 60
    assert "心跳" in row["note2"]


def test_a_paused_stack_does_not_shout_about_a_stale_heartbeat(tmp_path):
    now = time.time()
    beat = tmp_path / "loop_engine.json"
    _beat(beat, at_ms=int((now - 3 * 3600) * 1000))
    pause = tmp_path / "watchdog.stop"
    pause.write_bytes(b"console 2026-11-06 19:30:28 by panel\n")
    alerts = tmp_path / "watchdog.jsonl"
    W._check_engine(alerts, now=now, beat_path=beat, pause_file=pause)
    assert _rows(alerts) == [], "主人按了全关：心跳停了不是故障"
