"""重启整栈，**先给每个人一个自己退场的机会**（2026-11-05，§35）。

为什么有它：之前每次重启都是「写总闸 → 等 1.5 秒 → taskkill 剩下的」，于是退出账里一大片
`killed`（§33.4 那 6 条就是这么来的），而监管器本来能自己收尾、能自己记一行 `all_stop`。
真正该记成「被掐」的只有那些超时还不走的。

顺序：写总闸 → 等（默认 30 秒，端口放开 + 进程消失）→ 超时的才 taskkill（并记账）→
清哨兵 → 拉起 supervisor → 等端口 LISTENING。

用法：`python scripts/restart_stack.py [--grace 30] [--dry-run]`。数据源（进程/端口）都可注入，便于测试。
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:          # 让 _note 能 import core.infra.exitlog，否则 kill 记不进退出账
    sys.path.insert(0, str(ROOT))
LOGS = ROOT / "logs"
PORT = 39871
SENTINELS = ("all.stop", "scheduler.stop", "loop.stop", "watchdog.stop")
#: 这些命令行里的人要停；顺序无关，靠 psutil 扫。
WATCHED = ("scripts\\supervisor.py", "scripts/supervisor.py", "loop.run", "loop.desk",
           "run.py scheduler", "desk_supervisor.py")


def still_running(*, survivors: int, listening: bool) -> bool:
    """还有人在跑吗（进程活着，或者端口还在听）。纯函数，方便测。"""
    return bool(survivors) or bool(listening)


def should_escalate(*, waited_s: float, grace_s: float, survivors: int, listening: bool) -> bool:
    """**等够了**且**还有人没走** → 该动手了（taskkill）。

    纯函数。2026-11-05 第一版把这个语义写反了（`if not should_escalate: break` → 一秒不等就杀），
    结果账本里瞬间多出 10 条自己写的 `killed`，pid 还全记成了脚本自己的 —— 见 §35.4。
    """
    if not still_running(survivors=survivors, listening=listening):
        return False
    return float(waited_s) >= float(grace_s)


def _listening() -> set[int]:
    import psutil
    got: set[int] = set()
    for c in psutil.net_connections(kind="tcp"):
        if c.laddr and getattr(c.laddr, "port", 0) == PORT and c.status == psutil.CONN_LISTEN and c.pid:
            got.add(c.pid)
    return got


def _survivors() -> list[int]:
    import psutil
    got: list[int] = []
    for p in psutil.process_iter(["pid", "cmdline"]):
        cl = " ".join(p.info["cmdline"] or [])
        if any(k in cl for k in WATCHED):
            got.append(int(p.info["pid"]))
    return got


def _note(role: str, *, rc=None, why: str = "", pid=None, detail: str = "") -> None:
    try:
        from core.infra import exitlog
        exitlog.record(role, rc=rc, why=why, pid=pid, detail=detail)
    except Exception as exc:  # noqa: BLE001 - 账本坏了不许影响重启
        print("   （退出账没写成：%r）" % (exc,))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--grace", type=float, default=30.0, help="给它们自己退场的秒数")
    ap.add_argument("--wait-listen", type=float, default=90.0)
    ap.add_argument("--reason", default="restart_stack")
    ap.add_argument("--dry-run", action="store_true")
    got = ap.parse_args(argv)
    print("STEP0 listening=%s survivors=%s" % (sorted(_listening()), _survivors()))
    if got.dry_run:
        print("dry-run：不写总闸、不杀、不拉起")
        return 0
    LOGS.mkdir(parents=True, exist_ok=True)
    (LOGS / "all.stop").write_text(got.reason, encoding="utf-8")
    # 2026-11-05：总闸之外把另外两个哨兵也直接按下去 —— 否则引擎要等老监管器退出时才被动收到
    # loop.stop，而新监管器那时已经起来了，会把它当「孤儿」直接 taskkill（那就是 27 次引擎启动
    # 只有 9 条「子进程退出」的原因：它多数时候不是自己走的）。
    for _n in ("scheduler.stop", "loop.stop"):
        try:
            (LOGS / _n).write_text(got.reason, encoding="utf-8")
        except OSError as _exc:
            print("哨兵 %s 写不进去：%r" % (_n, _exc))
    t0 = time.time()
    while True:
        waited = time.time() - t0
        surv = _survivors()
        lis = _listening()
        if not still_running(survivors=len(surv), listening=bool(lis)):
            break                                             # 都自己走了
        if should_escalate(waited_s=waited, grace_s=got.grace, survivors=len(surv), listening=bool(lis)):
            break                                             # 等够了还不走
        time.sleep(0.5)
    waited = time.time() - t0
    surv = _survivors()
    print("STEP1 优雅等了 %.1fs，还剩 %d 个：%s" % (waited, len(surv), surv))
    import psutil
    for pid in surv:
        try:
            name = " ".join(psutil.Process(pid).cmdline() or [])[:80]
        except Exception:
            name = "?"
        try:
            psutil.Process(pid).kill()
            print("   kill", pid, name[:60])
            _note("supervisor", rc=None, why="killed", pid=pid, detail="restart_stack：等 %.0fs 没走，被掐掉｜%s" % (waited, name))
        except Exception as exc:  # noqa: BLE001
            print("   kill 失败", pid, repr(exc)[:80])
    time.sleep(1.0)
    for n in SENTINELS:
        f = LOGS / n
        if f.exists():
            try:
                f.unlink()
            except OSError as exc:
                print("   删哨兵失败", n, repr(exc)[:80])
    print("STEP2 哨兵剩余:", [n for n in SENTINELS if (LOGS / n).exists()])
    pyw = ROOT / ".venv_iso" / "Scripts" / "pythonw.exe"
    env = dict(os.environ)
    env["PROMPT_SHAPE_V2"] = "1"
    env["NO_PROXY"] = env["no_proxy"] = "127.0.0.1,localhost"
    out = open(LOGS / ("supervisor.%s.log" % got.reason), "wb")
    pid = subprocess.Popen([str(pyw), str(ROOT / "scripts" / "supervisor.py")], cwd=str(ROOT), env=env,
                           creationflags=0x00000008 | 0x00000200, stdin=subprocess.DEVNULL,
                           stdout=out, stderr=subprocess.STDOUT).pid
    print("STEP3 supervisor pid", pid)
    t0 = time.time()
    while time.time() - t0 < got.wait_listen:
        if _listening():
            break
        time.sleep(0.5)
    print("STEP4 LISTENING %.1fs pids=%s" % (time.time() - t0, sorted(_listening())))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
