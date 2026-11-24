"""控制台四个按钮的边界（2026-10-31）。

四个按钮以前的问题：写三个哨兵、清两个；停止不等结果；重启失败只写日志；
重启引擎只递纸条不确认。这些测试钉住"新行为"，不碰真进程。

跑法（从仓库根，必须显式列文件，见 pyproject 的 python_files/norecursedirs）：
  .venv/Scripts/python.exe -m pytest tests/test_svc_buttons.py -q
"""
import json
import os
import time

import pytest

from console import executor as E


@pytest.fixture()
def logs(tmp_path, monkeypatch):
    d = tmp_path / "logs"
    d.mkdir()
    monkeypatch.setattr(E, "LOGS_DIR", str(d))
    for name in ("ALL_STOP", "WATCHDOG_STOP", "SCHED_STOP", "LOOP_STOP",
                 "DESK_LOCK", "SUPERVISOR_LOCK", "SVC_LAST"):
        if hasattr(E, name):
            monkeypatch.setattr(E, name, str(d / (name.lower().replace("_", "."))))
    # 2026-11-03：svc_stop/svc_restart 会在后台起一个 daemon 线程去"核对有没有退干净"，
    # 默认 wait_s=45。这个线程活 45 秒、跑完还要写 SVC_LAST —— 它会跨到别的用例里去写，
    # 于是 test_stop_verifies_and_reports_survivors 只在全量里偶发红（单跑全绿）。
    # 这里给它一个立刻返回的桩；需要真跑这条路径的用例自己再 monkeypatch 覆盖。
    monkeypatch.setattr(E, "_wait_gone", lambda wait_s=0: (True, 0.1, []))
    yield d
    # 后台线程会在 monkeypatch 撤销后继续跑 —— 必须在这里等它们收尾，
    # 否则会把结果写进真实的 logs/svc_last.json（2026-10-31 踩过）。
    import threading
    for th in list(threading.enumerate()):
        if th.name.startswith(("svc-", "restart-engine")):
            th.join(5.0)


def test_all_stops_covers_four_sentinels():
    names = [os.path.basename(p) for p in E._all_stops()]
    assert names == ["all.stop", "watchdog.stop", "scheduler.stop", "loop.stop"]


def test_clear_stops_removes_all_four(logs):
    for p in E._all_stops():
        with open(p, "w", encoding="utf-8") as f:
            f.write("x")
    removed, errors = E._clear_stops()
    assert errors == []
    assert len(removed) == 4
    assert all(not os.path.exists(p) for p in E._all_stops())


def test_stop_writes_four_including_watchdog(logs, monkeypatch):
    monkeypatch.setattr(E, "_wait_gone", lambda s: (True, 0.1, []))
    out = E.svc_stop(by="test")
    assert out["ok"] is True
    assert os.path.exists(E.WATCHDOG_STOP), "停止必须连外部看门狗一起关"
    assert os.path.exists(E.LOOP_STOP) and os.path.exists(E.SCHED_STOP)
    assert "watchdog.stop" in out["note"]


def test_stop_verifies_and_reports_survivors(logs, monkeypatch):
    import threading
    seen = {}

    def _fake_wait(wait_s):
        seen["wait_s"] = wait_s
        return False, wait_s, ["发送工 desk"]

    monkeypatch.setattr(E, "_wait_gone", _fake_wait)
    E.svc_stop(by="test", wait_s=1.0)
    for t in threading.enumerate():
        if t.name == "svc-stop-verify":
            t.join(3.0)

    # 2026-11-04：原来 join 完就直接读一次文件 —— 线程可能还没真正落盘，
    # 全量跑时就会读到"上一次/别人的"那一行（P2-2 起偶发红，单跑全绿）。
    # 这里轮询到「这条结果真的写进来了」为止；等不到就把文件原文带进报错。
    def _note() -> dict:
        try:
            return json.load(open(E.SVC_LAST, encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    ok = False
    for _ in range(150):          # 150 × 0.02s = 3s
        cur = _note()
        if cur and cur.get("cmd") == "svc_stop" and "发送工 desk" in str(cur.get("note") or ""):
            ok = True
            break
        time.sleep(0.02)
    last = _note()
    assert ok, "核对结果没按预期落盘：%s" % (last,)
    assert last["ok"] is False


def test_start_is_noop_when_supervisor_alive(logs, monkeypatch):
    monkeypatch.setattr(E, "_supervisor_pid", lambda: 4242)
    monkeypatch.setattr(E.liveness, "pid_alive", lambda pid: True)
    monkeypatch.setattr(E, "_port_open", lambda port, timeout=0.5: True)
    out = E.svc_start(by="test")
    assert out["ok"] and out.get("noop") is True


def test_start_clears_stale_loop_stop_even_when_noop(logs, monkeypatch):
    with open(E.LOOP_STOP, "w", encoding="utf-8") as f:
        f.write("毒哨兵")
    monkeypatch.setattr(E, "_supervisor_pid", lambda: 4242)
    monkeypatch.setattr(E.liveness, "pid_alive", lambda pid: True)
    monkeypatch.setattr(E, "_port_open", lambda port, timeout=0.5: True)
    out = E.svc_start(by="test")
    assert out["ok"]
    assert not os.path.exists(E.LOOP_STOP), "启动必须清掉上一轮留下的 loop.stop"
    assert any("loop.stop" in x for x in out["removed"])


def test_start_reports_missing_interpreter(logs, monkeypatch):
    monkeypatch.setattr(E, "_supervisor_pid", lambda: 0)
    monkeypatch.setattr(E, "_port_open", lambda port, timeout=0.5: False)
    monkeypatch.setattr(E, "PYW", os.path.join(str(logs), "nope.exe"))
    out = E.svc_start(by="test")
    assert out["ok"] is False and "解释器" in out["error"]


def test_restart_engine_refuses_when_scheduler_dead(logs, monkeypatch):
    monkeypatch.setattr(E.liveness, "pid_alive", lambda pid: False)
    out = E.restart_engine(by="test")
    assert out["ok"] is False and "启动全部" in out["error"]
    assert not os.path.exists(E.LOOP_STOP), "不能留毒哨兵"


def test_restart_engine_writes_sentinel_when_scheduler_alive(logs, monkeypatch):
    import threading
    monkeypatch.setattr(E.liveness, "pid_alive", lambda pid: True)
    monkeypatch.setattr(E, "_engine_pid", lambda: 111)
    monkeypatch.setattr(E, "_port_open", lambda port, timeout=0.5: False)
    out = E.restart_engine(by="test", wait_s=0.1)
    assert out["ok"] is True
    assert os.path.exists(E.LOOP_STOP)
    for t in threading.enumerate():
        if t.name == "restart-engine-verify":
            t.join(6.0)
    # 等不到新引擎 → 必须把毒哨兵撤掉，并如实记失败
    assert not os.path.exists(E.LOOP_STOP)
    last = json.load(open(E.SVC_LAST, encoding="utf-8"))
    assert last["ok"] is False and last["cmd"] == "restart_engine"


def test_restart_clears_all_sentinels_before_spawn(logs, monkeypatch):
    import threading
    monkeypatch.setattr(E, "_wait_released", lambda wait_s: (True, 0.1))
    monkeypatch.setattr(E, "_port_open", lambda port, timeout=0.5: True)
    monkeypatch.setattr(E, "liveness", type("L", (), {"read": staticmethod(lambda n: {"ticks": 3, "pid": 1})}))
    monkeypatch.setattr(E.subprocess if hasattr(E, "subprocess") else os, "x", None, raising=False)

    class _P:
        pid = 9999

    import subprocess as _sp
    monkeypatch.setattr(_sp, "Popen", lambda *a, **k: _P())
    out = E.svc_restart(by="test")
    assert out["ok"]
    for t in threading.enumerate():
        if t.name == "svc-restart":
            t.join(6.0)
    assert not any(os.path.exists(p) for p in E._all_stops())
    last = json.load(open(E.SVC_LAST, encoding="utf-8"))
    assert last["ok"] is True and "重启完成" in last["note"]


def test_restart_reports_timeout_instead_of_silence(logs, monkeypatch):
    import threading
    monkeypatch.setattr(E, "_wait_released", lambda wait_s: (False, 9.0))
    E.svc_restart(by="test")
    for t in threading.enumerate():
        if t.name == "svc-restart":
            t.join(6.0)
    last = json.load(open(E.SVC_LAST, encoding="utf-8"))
    assert last["ok"] is False and "重启失败" in last["note"]


def test_status_has_desk_light_and_port_based_engine(logs, monkeypatch):
    monkeypatch.setattr(E, "_port_open", lambda port, timeout=0.5: False)
    monkeypatch.setattr(E.liveness, "pid_alive", lambda pid: True)
    monkeypatch.setattr(E, "_desk_pid", lambda: 0)
    monkeypatch.setattr(E, "liveness", type("L", (), {
        "read": staticmethod(lambda n: {"pid": 123, "ticks": 1}),
        "age_ms": staticmethod(lambda n: 5),
        "pid_alive": staticmethod(lambda pid: True)}))
    st = E.svc_status()
    keys = {l["key"]: l for l in st["lights"]}
    assert "desk" in keys and keys["desk"]["alive"] is False
    assert keys["engine"]["alive"] is False, "端口不通就不能报绿灯"

def test_svc_start_ensures_collector(monkeypatch):
    """2026-11-01：采集器不归 supervisor 管，svc_start/restart 必须顺手确保它在。

    现象：重启服务后页面还在、顶栏却红成「连不上采集器」(39872)。
    """
    calls = {}

    monkeypatch.setattr(E, "_port_open", lambda port=E.ENGINE_PORT: False)
    monkeypatch.setattr(E, "_stop_files", [])
    monkeypatch.setattr(E, "_clear_stops", lambda: (["logs\\all.stop"], []))
    monkeypatch.setattr(E, "_svc_note", lambda *a, **k: None)
    monkeypatch.setattr(E, "_audit_row", lambda row: row.update({"id": "t"}) or row)

    class _P:
        pid = 4242

    import subprocess as _sp

    monkeypatch.setattr(_sp, "Popen", lambda *a, **k: _P())
    monkeypatch.setattr(E, "_ensure_collector", lambda *a, **k: calls.setdefault("called", {"ok": True, "started": True, "pid": 777}))

    res = E.svc_start(by="test")
    assert calls.get("called"), "svc_start 没有确保采集器"
    assert res.get("collector", {}).get("started") is True
    assert "面板后端" in res.get("note", "")


def test_ensure_collector_noop_when_port_open(monkeypatch):
    monkeypatch.setattr(E, "_port_open", lambda port=E.COLLECTOR_PORT: True)
    out = E._ensure_collector()
    assert out == {"ok": True, "started": False, "pid": 0}
