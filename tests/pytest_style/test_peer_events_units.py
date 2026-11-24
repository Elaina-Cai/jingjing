# -*- coding: utf-8 -*-
"""毫秒 vs 秒：这一对单位错了三处，三处都静默（2026-10-31）。

为什么要有这几条：
- loop/run.py 把 duration.now_ms() 的 t0 直接喂给 ports.peer_events_since(ts)（契约是 epoch 秒），
  fromtimestamp 抛 OSError(22)，被 ports 里的 except 吞掉返回 []，于是「有人说话」恒为假、
  selfstate 永远记成没听见，而且每拍多建一次快照。
- console/checkup.py 把 uptime_ms 当秒减、又当小时除 1000，体检报「已跑 198 小时」。
"""
from __future__ import annotations

import inspect
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from loop import ports, run as loop_run
from console import checkup


def test_run_passes_epoch_seconds_not_millis():
    src = inspect.getsource(loop_run)
    assert "peer_events_since(t0" not in src, "又把毫秒 t0 直接喂进去了"
    assert src.count("peer_events_since(duration.epoch_s(t0)") >= 2


def test_peer_events_since_accepts_millis_without_raising(monkeypatch):
    seen = []
    monkeypatch.setattr(ports, "warn", lambda *a, **k: seen.append((a, k)))
    out = ports.peer_events_since(1793448465516, limit=1)   # 毫秒
    assert isinstance(out, list)
    assert any("ms_argument" in str(a[0]) for a in seen), "毫秒入参必须留下痕迹"


def test_incoming_since_accepts_millis_without_raising(monkeypatch):
    seen = []
    monkeypatch.setattr(ports, "warn", lambda *a, **k: seen.append((a, k)))
    n = ports.incoming_since("filehelper", 1793448465516)
    assert isinstance(n, int)
    assert any("ms_argument" in str(a[0]) for a in seen)


def test_check_engine_uptime_is_not_scaled_by_1000(monkeypatch):
    monkeypatch.setattr(checkup.liveness, "read",
                        lambda name: {"pid": 1, "uptime_ms": 7_200_000, "ticks": 3})
    monkeypatch.setattr(checkup.liveness, "alive", lambda name: True)
    it = checkup.check_engine()
    assert "2.0 小时" in it["line"], it["line"]


def test_check_code_fresh_no_name_error(monkeypatch):
    monkeypatch.setattr(checkup.liveness, "read", lambda name: {"pid": 1, "uptime_ms": 600_000})
    it = checkup.check_code_fresh()
    assert it["level"] in ("ok", "warn"), it
    assert "NameError" not in str(it.get("evidence"))
