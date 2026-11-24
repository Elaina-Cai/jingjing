"""2026-10-31：启动/建快照期间的心跳补拍。

监管器只看 at_ms（beat_ms=99s × GRACE=3 ≈ 5 分钟），超时就 kill。
以前只有"进到某个窗口"和"一拍跑完"才写心跳，于是 build_snapshot 跑 vision 到 2-3 分钟时
引擎会被自己人杀掉，外面看起来就是"重启了没反应"（今天 09:19/09:25/09:32/12:19 各一次）。
"""
import json
import os

import pytest

from loop import run as R


@pytest.fixture()
def hb(tmp_path, monkeypatch):
    p = tmp_path / "loop_engine.json"
    monkeypatch.setattr(R, "HEARTBEAT", str(p))
    monkeypatch.setattr(R, "_HB_PHASE", {"phase": "starting"})
    return p


def _read(p):
    return json.loads(p.read_text(encoding="utf-8"))


def test_beat_once_creates_heartbeat(hb):
    assert R._hb_beat_once() is True
    d = _read(hb)
    assert d["pid"] == os.getpid()
    assert d["phase"] == "starting"
    assert d["at_ms"] > 0 and d["beat_ms"] > 0


def test_beat_once_keeps_busy_and_window(hb):
    """补拍只能盖"我还活着"，不能把 one_tick 写的忙到哪一步冲掉。"""
    hb.write_text(json.dumps({"pid": 999, "busy": True,
                              "window": {"i": 3, "n": 7, "name": "Elaina"},
                              "at_ms": 1}), encoding="utf-8")
    R._HB_PHASE["phase"] = "windows"
    assert R._hb_beat_once() is True
    d = _read(hb)
    assert d["busy"] is True
    assert d["window"] == {"i": 3, "n": 7, "name": "Elaina"}
    assert d["phase"] == "windows"
    assert d["pid"] == os.getpid()


def test_stale_rule(hb):
    assert R._hb_stale({}) is True                       # 没有 at_ms
    assert R._hb_stale({"at_ms": 0}) is True
    assert R._hb_stale({"at_ms": R.duration.now_ms()}) is False          # 刚写过
    assert R._hb_stale({"at_ms": R.duration.now_ms() - R._HB_EVERY_MS - 1}) is True
    assert R._hb_stale({"at_ms": "坏值"}) is True         # 坏值也算该补


def test_keepalive_thread_wired_in_main():
    """源码级：main 里要真的把线程起来，且 one_tick 建快照前要标 phase + 补一次。"""
    import inspect
    src = inspect.getsource(R.main)
    assert "_hb_keepalive" in src and "daemon=True" in src
    one = inspect.getsource(R.one_tick)
    assert 'building' in one and "_hb_beat_once()" in one
