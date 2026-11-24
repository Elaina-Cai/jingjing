import json
import os

import pytest

from core.infra import roles


@pytest.fixture()
def root(tmp_path, monkeypatch):
    (tmp_path / "logs").mkdir()
    monkeypatch.setattr(roles, "_root", lambda: str(tmp_path))
    return tmp_path


def test_read_pid_plain(root):
    (root / "logs" / "loop_engine.lock").write_text("12345\n", encoding="utf-8")
    assert roles.read_pid("engine") == 12345


def test_read_pid_json(root):
    (root / "logs" / "loop_engine.lock").write_text(json.dumps({"pid": 999}), encoding="utf-8")
    assert roles.read_pid("engine") == 999


def test_read_pid_garbage_and_missing(root):
    (root / "logs" / "loop_engine.lock").write_text("no digits here", encoding="utf-8")
    assert roles.read_pid("engine") is None
    os.remove(str(root / "logs" / "loop_engine.lock"))
    assert roles.read_pid("engine") is None


def test_owner_ignores_dead_pid(root, monkeypatch):
    (root / "logs" / "loop_engine.lock").write_text("4242", encoding="utf-8")
    monkeypatch.setattr(roles.liveness, "pid_alive", lambda pid: False)
    assert roles.owner("engine") is None


def test_unknown_role_raises(root):
    with pytest.raises(KeyError):
        roles.path("nope")


def test_shim_detection(tmp_path):
    venv = tmp_path / "venv"
    (venv / "Scripts").mkdir(parents=True)
    (venv / "pyvenv.cfg").write_text("home = x", encoding="utf-8")
    shim = venv / "Scripts" / "pythonw.exe"
    shim.write_bytes(b"")
    assert roles._is_shim(str(shim)) is True
    assert roles._is_shim(str(tmp_path / "plain" / "pythonw.exe")) is False


def test_status_covers_every_role(root):
    got = roles.status()
    assert set(got) == set(roles.ROLES)
    assert got["supervisor"]["pid"] is None


def test_stop_without_owner_is_noop(root):
    out = roles.stop("scheduler")
    assert out["stopped"] is False and out["pid"] is None


def test_supervisor_writes_its_lock():
    demo = open("scripts/supervisor.py", encoding="utf-8").read()
    assert 'os.path.join(LOGS, "supervisor.lock")' in demo
    assert "_write_lock()" in demo
    assert "_drop_lock()" in demo
