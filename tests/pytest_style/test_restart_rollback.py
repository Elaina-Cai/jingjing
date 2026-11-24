"""PIT-048：重启链路里「等释放」这一步失败，必须整体回滚，不许硬等之后照样往下走。

三层要分开验：
1. 等到了 → 三个 stop 文件都删、监管器被拉起；
2. 等不到（超时）→ 只撤销这次新写出来的 stop 文件，本来就有的不动，也不拉起监管器；
3. 停的途中写文件失败 → 半停状态也要撤回。

外加一条结构锁：svc_restart 里不许再自己写 while 等，等释放只许走 _wait_released。
"""
import ast
import importlib
import os
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

executor = importlib.import_module("console.executor")


def _sandbox(monkeypatch, tmp_path, existed=()):
    paths = {}
    for name in ("ALL_STOP", "WATCHDOG_STOP", "SCHED_STOP", "LOOP_STOP"):
        path = tmp_path / (name.lower() + ".stop")
        if name in existed:
            path.write_text("别人早先要求的停\n", encoding="utf-8")
        elif path.exists():
            path.unlink()  # 起点干净：只验本次写没写、撤没撤
        monkeypatch.setattr(executor, name, str(path))
        paths[name] = path
    monkeypatch.setattr(executor, "_audit_row", lambda row: None)
    # 2026-11-04：这个文件原来没隔离 SVC_LAST/LOGS_DIR，后台 daemon 线程会把结果写进
    # **生产的** logs/svc_last.json（实测被写成 svc_restart 那一行）。测试不许动生产状态。
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir(exist_ok=True)
    monkeypatch.setattr(executor, "LOGS_DIR", str(logs_dir))
    if hasattr(executor, "SVC_LAST"):
        monkeypatch.setattr(executor, "SVC_LAST", str(logs_dir / "svc_last.json"))
    # 同理：svc_stop 的核对线程默认 wait_s=45，不打桩会有 45 秒的线程活到别的用例里去写账本。
    if hasattr(executor, "_wait_gone"):
        monkeypatch.setattr(executor, "_wait_gone", lambda wait_s=0: (True, 0.1, []))
    return paths


def _wait_until(predicate, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        if predicate():
            return True
        time.sleep(0.02)
    return bool(predicate())


def test_wait_timeout_rolls_back_only_what_this_run_wrote(monkeypatch, tmp_path):
    paths = _sandbox(monkeypatch, tmp_path, existed=("SCHED_STOP",))
    before = {name: path.exists() for name, path in paths.items()}
    monkeypatch.setattr(executor, "_wait_released", lambda wait_s: (False, 90.0))

    def _never(cmd, **kw):
        raise AssertionError("等不到释放时不许拉起监管器")

    monkeypatch.setattr(executor.subprocess, "Popen", _never)
    out = executor.svc_restart(by="test", wait_s=0.01)
    assert out["ok"] is True
    # 撤回是逐个文件做的：先删 all.stop、再比 loop.stop。只用 all.stop 消失当"撤回完成"的信号，
    # 会在两步之间读到"一半撤回完"的状态（2026-10-30：5 次里红 1 次就是这个窗口）。
    # 所以等的是"整套状态回到开始前"，而不是"第一个文件消失"。
    assert _wait_until(
        lambda: not paths["ALL_STOP"].exists()
        and paths["LOOP_STOP"].exists() == before["LOOP_STOP"]
    ), "超时后没撤回这次新写的 stop（all.stop 还在，或 loop.stop 的存废与开始前不一致）"
    assert paths["SCHED_STOP"].exists(), "本来就存在的 stop 被误删了（等于替别人开机）"
    assert not paths["ALL_STOP"].exists() and paths["LOOP_STOP"].exists() == before["LOOP_STOP"]


def test_wait_success_removes_all_three_and_starts_supervisor(monkeypatch, tmp_path):
    paths = _sandbox(monkeypatch, tmp_path, existed=("ALL_STOP",))
    calls = []
    monkeypatch.setattr(executor, "_wait_released", lambda wait_s: (True, 1.2))
    monkeypatch.setattr(executor.subprocess, "Popen", lambda cmd, **kw: calls.append(cmd))
    executor.svc_restart(by="test", wait_s=0.01)
    # 2026-11-09：等"拉起 PYW"这一跳，而不是"calls 非空"——
    # monkeypatch 打的是全局 subprocess，宿主 shim 会先往 calls 里塞 node.exe。
    assert _wait_until(
        lambda: any(cmd and cmd[0] == executor.PYW for cmd in calls)
    ), "等到了却没拉起监管器：%r" % (calls,)
    # 2026-11-09：monkeypatch 打的是全局 subprocess，宿主 shim 也会进来
    # （实测 node.exe 抢到 calls[0]）。只要求"这次拉起用的是 PYW"，不假设它是第一个。
    assert any(cmd and cmd[0] == executor.PYW for cmd in calls), calls
    for name, path in paths.items():
        assert _wait_until(lambda path=path: not path.exists()), name + " 没被删掉"


def test_stop_write_failure_rolls_back_partial(monkeypatch, tmp_path):
    paths = _sandbox(monkeypatch, tmp_path)
    monkeypatch.setattr(executor, "LOOP_STOP", str(tmp_path))
    out = executor.svc_stop(by="test")
    assert out["ok"] is False
    assert out["rolled_back"] is True
    assert out["undone"], out
    assert not paths["ALL_STOP"].exists()
    assert not paths["SCHED_STOP"].exists()


def test_wait_released_tells_timeout_apart_from_success(monkeypatch):
    monkeypatch.setattr(executor, "_port_open", lambda port: True)
    monkeypatch.setattr(executor.liveness, "pid_alive", lambda pid: True)
    monkeypatch.setattr(executor.liveness, "read", lambda name: {"pid": 1})
    released, waited = executor._wait_released(0.3)
    assert released is False
    assert waited >= 0.3
    monkeypatch.setattr(executor, "_port_open", lambda port: False)
    monkeypatch.setattr(executor.liveness, "pid_alive", lambda pid: False)
    released, waited = executor._wait_released(5.0)
    assert released is True
    assert waited < 1.0


def _find_function(tree, name):
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError("找不到函数 " + name)


def test_restart_goes_through_the_two_state_wait():
    src = Path(executor.__file__).read_text(encoding="utf-8")
    fn = _find_function(ast.parse(src), "svc_restart")
    called = {n.func.id for n in ast.walk(fn) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert {"_wait_released", "_stop_rollback"} <= called, called
    assert not [n for n in ast.walk(fn) if isinstance(n, ast.While)], "等释放只许走 _wait_released"
