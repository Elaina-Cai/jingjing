# -*- coding: utf-8 -*-
"""可靠重启：等旧进程退掉，再起新的。由已被提权的调度器派生，因此子进程仍具管理员权限。"""
from __future__ import annotations
import os, socket, subprocess, sys, time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOGS = ROOT / "logs"
LOG = LOGS / "restart_helper.log"
PORT = 8972
PY = Path(sys.executable)


def log(msg: str) -> None:
    try:
        LOGS.mkdir(exist_ok=True)
        with LOG.open("a", encoding="utf-8") as f:
            f.write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
    except OSError:
        pass


def listening() -> bool:
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", PORT)) == 0


def main() -> int:
    log("helper start pid=%d" % os.getpid())
    time.sleep(2.0)
    for n in ("watchdog.stop", "loop.stop"):
        try:
            (LOGS / n).unlink()
        except FileNotFoundError:
            pass
    (LOGS / "scheduler.stop").touch()
    log("sentinel placed, waiting for port to free")
    for _ in range(120):
        if not listening():
            break
        time.sleep(0.5)
    else:
        log("ABORT: port still busy after 60s")
        try:
            (LOGS / "scheduler.stop").unlink()
        except FileNotFoundError:
            pass
        return 1
    try:
        (LOGS / "scheduler.stop").unlink()
    except FileNotFoundError:
        pass
    log("port free, spawning scheduler")
    subprocess.Popen([str(PY), "run.py", "scheduler"], cwd=str(ROOT),
                     creationflags=0x00000008 | 0x00000200,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(180):
        if listening():
            log("scheduler back up")
            return 0
        time.sleep(0.5)
    log("ABORT: scheduler did not come back")
    return 1


if __name__ == "__main__":
    sys.exit(main())
