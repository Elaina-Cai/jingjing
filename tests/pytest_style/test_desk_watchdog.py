"""发送工看门狗的锁：别让「出口悄悄关掉」再发生一次。

2026-10-30 23:01 `all.stop` 把 `desk_supervisor` 关掉，然后就没有任何东西负责拉起它了
—— 那一夜到第二天中午，她「说」的 12 句话全堆在 `outbound_queue` 里，各试 3 次、
全部 `failed / 无法激活微信窗口`。用户看到的是「她不回我」，其实是没人在送。
"""
from __future__ import annotations

import os
import subprocess
import time

import pytest

from scheduler import main as sch


@pytest.fixture()
def lock(tmp_path, monkeypatch):
    p = tmp_path / "desk.lock"
    monkeypatch.setattr(sch, "_DESK_LOCK", str(p))
    return p


def test_no_lock_means_not_alive(lock):
    assert sch._desk_alive() is False


def test_lock_with_our_own_pid_is_alive(lock):
    lock.write_text(str(os.getpid()), encoding="utf-8")
    assert sch._desk_alive() is True


def test_dead_pid_is_not_alive(lock):
    lock.write_text("999999", encoding="utf-8")
    assert sch._desk_alive() is False


def test_garbage_lock_is_not_alive_and_does_not_raise(lock):
    lock.write_text("not-a-pid", encoding="utf-8")
    assert sch._desk_alive() is False


def test_ensure_desk_respects_the_master_stop(lock, monkeypatch):
    """总闸在场时不许拉起 —— 否则会跟 all.stop 打架：拉一个、它退出、再拉一个。"""
    spawned = []
    monkeypatch.setattr(sch, "_DESK_LAST_CHECK", 0.0)
    monkeypatch.setattr(sch, "stop_requested", lambda: True)
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: spawned.append(a))
    sch._ensure_desk()
    assert spawned == []


def test_ensure_desk_is_throttled_to_sixty_seconds(lock, monkeypatch):
    spawned = []
    monkeypatch.setattr(sch, "stop_requested", lambda: False)
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: spawned.append(a))
    monkeypatch.setattr(sch, "_DESK_LAST_CHECK", time.monotonic())
    sch._ensure_desk()
    assert spawned == [], "刚查过就不许再查（60 秒一次）"


def test_ensure_desk_does_nothing_when_desk_is_alive(lock, monkeypatch):
    lock.write_text(str(os.getpid()), encoding="utf-8")
    spawned = []
    monkeypatch.setattr(sch, "_DESK_LAST_CHECK", 0.0)
    monkeypatch.setattr(sch, "stop_requested", lambda: False)
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: spawned.append(a))
    sch._ensure_desk()
    assert spawned == []


def test_ensure_desk_launches_the_supervisor_when_desk_is_gone(lock, monkeypatch):
    """拉的是监管器（重起/退避/单实例都在它里面），不是裸的 desk。"""
    spawned = []
    monkeypatch.setattr(sch, "_DESK_LAST_CHECK", 0.0)
    monkeypatch.setattr(sch, "stop_requested", lambda: False)
    monkeypatch.setattr(subprocess, "Popen", lambda cmd, *a, **k: spawned.append(cmd))
    sch._ensure_desk()
    assert len(spawned) == 1
    assert spawned[0][-1].endswith(os.path.join("scripts", "desk_supervisor.py"))
