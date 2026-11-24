"""静默告警日志也要有上限。

盘满那次 `silent_warnings.log` 已经 5.5MB，而且只增不切。每进程 2000 条挡不住进程
重启次数。这里锁住：超过上限就切成 .1，旧份依次后移，且截断/改名失败不影响写日志。
"""
from __future__ import annotations

import safelog


def test_warns_rotate_when_file_is_big(monkeypatch, tmp_path):
    log = tmp_path / "silent_warnings.log"
    log.write_text("x" * 200 + "\n", encoding="utf-8")
    monkeypatch.setattr(safelog, "LOG", str(log))
    monkeypatch.setattr(safelog, "_MAX_BYTES", 100)
    monkeypatch.setattr(safelog, "_BACKUPS", 2)
    monkeypatch.setattr(safelog, "_seen", {})
    monkeypatch.setattr(safelog, "_total", 0)
    safelog.warn("rotation_probe")
    assert (tmp_path / "silent_warnings.log.1").exists()
    assert "rotation_probe" in log.read_text(encoding="utf-8")
    old = (tmp_path / "silent_warnings.log.1").read_text(encoding="utf-8")
    assert old.startswith("x")


def test_rotation_failure_never_raises(monkeypatch, tmp_path):
    monkeypatch.setattr(safelog, "LOG", str(tmp_path / "nope" / "silent_warnings.log"))
    monkeypatch.setattr(safelog, "_MAX_BYTES", 1)
    monkeypatch.setattr(safelog, "_seen", {})
    monkeypatch.setattr(safelog, "_total", 0)
    safelog.warn("no_dir_probe")   # 目录不在：_rotate_if_big 放过，写日志照旧失败但不抛
