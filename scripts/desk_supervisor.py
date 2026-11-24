"""desk 监管器：唯一有权拉起发送工（loop/desk.py）的进程。

为什么单独一个：`scripts/supervisor.py` 只管主程序（run.py scheduler）。
发送工（loop/desk.py）是**另一条常驻链**——没人拉它，她"说"的话就只会堆在
`outbound_queue` 里永远发不出去（2026-10-30 实测：id=102 卡了两小时）。

规矩与 supervisor 一致：命名互斥体保证单实例；子进程死了退避重起；认总闸。
"""
from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOGS = os.path.join(ROOT, "logs")
PY = sys.executable
MUTEX_NAME = "Local\\YinyueDeskSupervisorMutex"
STOP = os.path.join(LOGS, "all.stop")
MAX_BACKOFF_S = 30.0


def _log(msg: str) -> None:
    line = "[desk-supervisor] " + msg
    print(line, flush=True)
    try:
        os.makedirs(LOGS, exist_ok=True)
        with open(os.path.join(LOGS, "desk_supervisor.out"), "a", encoding="utf-8") as fh:
            fh.write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), line))
    except OSError:
        print("[desk-supervisor] 写日志失败： %r" % (__import__("sys").exc_info()[1],), file=__import__("sys").stderr)


def _single_instance() -> bool:
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    h = k32.CreateMutexW(None, False, MUTEX_NAME)
    if not h:
        return False
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        return False
    globals()["_MUTEX_HANDLE"] = h
    return True




def _exitlog():
    """退出原因账（2026-11-05，主人「要」）：把「谁退了、什么码」写成一行 JSON。

    账本本身坏了不许影响监管（所以这里 try 住、只 _log 一句）；ROOT 不在 sys.path 时
    先插进去 —— 计划任务/双击启动时的 sys.path 里没有仓库根。
    """
    try:
        if ROOT not in sys.path:
            sys.path.insert(0, ROOT)
        from core.infra import exitlog
        return exitlog
    except Exception as exc:  # noqa: BLE001 - 账本坏了也不许影响监管
        _log("退出账本不可用：%r" % (exc,))
        return None


def main() -> int:
    if not _single_instance():
        _log("已经有一个 desk 监管器在跑")
        return 0
    try:
        _el0 = _exitlog()
        if _el0 is not None:
            _el0.record_start("desk_supervisor")
    except Exception as _sexc:  # noqa: BLE001 - 记不下也不许拦住监管
        _log("exitlog.record_start 失败：%r" % (_sexc,))
    os.makedirs(LOGS, exist_ok=True)
    out = open(os.path.join(LOGS, "desk.out"), "a", encoding="utf-8", buffering=1)
    backoff = 1.0
    proc: subprocess.Popen | None = None
    _log("起来（pid=%d）" % os.getpid())
    while not os.path.exists(STOP):
        if proc is None or proc.poll() is not None:
            if proc is not None:
                _log("desk 退出 rc=%r，%.0f 秒后重起" % (proc.returncode, backoff))
                _el = _exitlog()
                if _el:
                    _el.record("desk", rc=proc.returncode, pid=proc.pid,
                               detail="desk 监管器看到退出")
                time.sleep(backoff)
                backoff = min(MAX_BACKOFF_S, backoff * 2.0)
            else:
                _log("启动 desk")
            proc = subprocess.Popen([PY, "-m", "loop.desk"], cwd=ROOT,
                                    stdout=out, stderr=out,
                                    creationflags=0x00000008 | 0x00000200)
            backoff = 1.0
        time.sleep(1.0)
    if proc is not None and proc.poll() is None:
        # 2026-11-05：先让 desk 自己看哨兵退（它现在会记一行 all_stop）；
        # 以前这里直接 terminate，发送工永远没机会说自己是怎么停的。
        _log("收到总闸：等 desk 自己退（最多 8 秒）")
        _deadline = time.monotonic() + 8.0
        while time.monotonic() < _deadline and proc.poll() is None:
            time.sleep(0.2)
        if proc.poll() is None:
            _log("desk 没自己退，terminate")
            proc.terminate()
            try:
                proc.wait(timeout=3.0)
            except subprocess.TimeoutExpired:
                _log("desk 还活着，kill")
                proc.kill()
    _log("看到 all.stop，退出")
    _el = _exitlog()
    if _el:
        _el.record("desk_supervisor", why="all_stop", detail="看到 all.stop")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
