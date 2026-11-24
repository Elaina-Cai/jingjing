"""谁在跑哪一份角色：唯一权威是锁文件里写的 pid。

为什么需要这个文件：.venv_it/Scripts/pythonw.exe 是启动器，它会再起一个真正的
解释器子进程，命令行一模一样。于是每个角色在进程表里都表现为两个 pid（启动器 + 真身）。
用「命令行相同就是重复」去清理，会杀掉真身、启动器随之退出，整棵进程树全灭。
所以：判「谁是这一份」只看锁文件里的 pid；判「怎么停」只杀真身。
"""

from __future__ import annotations

import json
import os
import re
import sys

from core.infra import duration, liveness

# 角色 -> 锁文件（相对仓库根）。supervisor 的锁由 supervisor.py 自己写。
ROLES = {
    "supervisor": os.path.join("logs", "supervisor.lock"),
    "scheduler": os.path.join("logs", "scheduler.pid"),
    "engine": os.path.join("logs", "loop_engine.lock"),
}

_PID = re.compile(r"\d+")


def _root() -> str:
    if os.path.isdir(os.path.join(os.getcwd(), "logs")):
        return os.getcwd()
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def path(role: str) -> str:
    try:
        rel = ROLES[role]
    except KeyError:
        raise KeyError("未知角色: %s；可用: %s" % (role, ", ".join(ROLES))) from None
    return os.path.join(_root(), rel)


def read_pid(role: str) -> int | None:
    try:
        raw = open(path(role), encoding="utf-8", errors="replace").read()
    except OSError:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        data = None
    if isinstance(data, dict):
        for key in ("pid", "PID"):
            value = data.get(key)
            if isinstance(value, int):
                return value
    match = _PID.search(raw)
    return int(match.group()) if match else None


def owner(role: str) -> int | None:
    pid = read_pid(role)
    return pid if pid and liveness.pid_alive(pid) else None


def _is_shim(exe: str) -> bool:
    scripts = os.path.dirname(exe)
    venv = os.path.dirname(scripts)
    return os.path.basename(scripts).lower() == "scripts" and os.path.exists(
        os.path.join(venv, "pyvenv.cfg")
    )


def launcher_of(pid: int) -> int | None:
    try:
        import psutil

        proc = psutil.Process(pid)
        parent = proc.parent()
        if parent is None:
            return None
        if not _is_shim(parent.exe()):
            return None
        if " ".join(parent.cmdline() or []) != " ".join(proc.cmdline() or []):
            return None
        return parent.pid
    except Exception:
        return None


def stop(role: str, *, wait_ms: int = 8000) -> dict:
    pid = owner(role)
    if pid is None:
        return {"role": role, "pid": None, "stopped": False, "why": "没在跑"}
    launcher = launcher_of(pid)
    try:
        import psutil

        psutil.Process(pid).terminate()
    except Exception as exc:
        return {"role": role, "pid": pid, "launcher": launcher, "stopped": False, "why": repr(exc)[:120]}
    deadline = duration.now_ms() + int(wait_ms)
    while duration.now_ms() < deadline:
        if not liveness.pid_alive(pid):
            return {"role": role, "pid": pid, "launcher": launcher, "stopped": True}
        duration.sleep_s(200)
    return {"role": role, "pid": pid, "launcher": launcher, "stopped": False, "why": "超时仍在跑"}


def status() -> dict:
    out = {}
    for role in ROLES:
        pid = read_pid(role)
        alive = bool(pid and liveness.pid_alive(pid))
        out[role] = {
            "pid": pid,
            "alive": alive,
            "launcher": launcher_of(pid) if pid and alive else None,
            "lock": path(role),
        }
    return out


def main(argv: list[str] | None = None) -> int:
    av = list(sys.argv[1:] if argv is None else argv)
    if not av or av[0] == "status":
        print(json.dumps(status(), ensure_ascii=False, indent=2))
        return 0
    if av[0] == "stop" and len(av) >= 2:
        result = stop(av[1])
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result.get("stopped") or result.get("pid") is None else 1
    print("usage: python -m core.infra.roles [status | stop <role>]", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
