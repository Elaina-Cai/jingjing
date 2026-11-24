"""退出账本不许无限长（§35.6 第 1 条）。

`logs/exits.jsonl` 以前只追加、没有保留策略：一天几十行的东西，跑一年也就几十万行，
但「27 次启动 vs 9 条退出」那种账要对很久 —— 上限与份数只能来自 `log_rotate` 的策略表。
"""

from __future__ import annotations

import os

import pytest

from core.infra import exitlog, log_rotate


def test_the_exit_ledger_has_a_policy_at_all():
    """策略表里必须有它 —— 否则 `rotate_if_needed` 只会回 disabled，账本又长回去了。"""
    pol = log_rotate.policy_for("exits.jsonl")
    assert pol.max_bytes > 0
    assert pol.backups >= 1


def test_writing_past_the_cap_rotates_the_ledger(tmp_path):
    log = tmp_path / "exits.jsonl"
    cap = log_rotate.policy_for("exits.jsonl").max_bytes
    log.write_text("x" * (cap + 10), encoding="utf-8")
    ok = exitlog.record("engine", rc=0, why="clean", pid=4321, path=str(log))
    assert ok is True
    assert os.path.exists(str(log) + ".1")
    assert os.path.getsize(str(log) + ".1") > cap
    body = log.read_text(encoding="utf-8").strip().splitlines()
    assert len(body) == 1
    assert "engine" in body[0]


def test_a_small_ledger_is_not_rotated(tmp_path):
    log = tmp_path / "exits.jsonl"
    assert exitlog.record("desk", rc=0, why="clean", pid=9, path=str(log)) is True
    assert exitlog.record("desk", rc=0, why="all_stop", pid=9, path=str(log)) is True
    assert not os.path.exists(str(log) + ".1")
    assert len(log.read_text(encoding="utf-8").strip().splitlines()) == 2
