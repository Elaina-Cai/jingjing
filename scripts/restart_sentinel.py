"""重启同步哨兵（唯一正确姿势）。

为什么需要这个脚本
------------------
`scripts/run_sentinel.vbs` 里用 `sh.Run(..., 0, False)` 起进程，子进程**和计划任务的
进程树脱钩**（父进程是 explorer / 任务宿主，不是 taskeng）。所以 `schtasks /End` 只能
结束"任务自己的那个实例"，管不到真正在 `watch` 的那个 pythonw ——
只做 End + Run 会变成**两个哨兵同时监听**（2026-10-28 实测踩到）。

本脚本的次序（每步都可单独跑）
-----------------------------
1. `--only-kill`   只清理：按 cmdline 匹配 `sync_sentinel` 的 pythonw 全杀（成对：venv 外壳 + 真身）
2. `--only-start`  只启一个：`schtasks /Run`
3. 默认            清理 → 等端口/进程消失 → 启动 → 校验只剩一对

用法
----
    python scripts\\restart_sentinel.py            # 重启（默认）
    python scripts\\restart_sentinel.py --status   # 只看现在有几个
    python scripts\\restart_sentinel.py --only-kill
    python scripts\\restart_sentinel.py --only-start
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time

TASK = "YinyueSyncSentinel"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _ps(cmd: str) -> str:
    r = subprocess.run(["powershell", "-NoProfile", "-Command", cmd],
                       capture_output=True, timeout=120)
    return r.stdout.decode("mbcs", "replace").strip()


def procs() -> list[dict]:
    """当前带 sync_sentinel 的 pythonw 进程（含 venv 外壳与真身两层）。"""
    out = _ps(
        "Get-CimInstance Win32_Process -Filter \"Name='pythonw.exe'\" | "
        "Where-Object { $_.CommandLine -like '*sync_sentinel*' } | "
        "ForEach-Object { \"$($_.ProcessId)|$($_.ParentProcessId)|$($_.CreationDate)\" }"
    )
    rows = []
    for line in out.splitlines():
        parts = [p.strip() for p in line.split("|")]
        if len(parts) >= 2 and parts[0].isdigit():
            rows.append({"pid": int(parts[0]), "ppid": int(parts[1]),
                         "started": parts[2] if len(parts) > 2 else ""})
    return rows


def kill_all() -> int:
    rows = procs()
    if not rows:
        print("  （没有在跑的哨兵）")
        return 0
    for r in rows:
        print("  kill %d（父 %d，起于 %s）" % (r["pid"], r["ppid"], r["started"]))
        _ps("taskkill /F /PID %d 2>&1" % r["pid"])
    return len(rows)


def wait_empty(timeout_s: float = 10.0) -> bool:
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        if not procs():
            return True
        time.sleep(0.5)
    return not procs()


def start() -> None:
    print("  schtasks /Run:", _ps("schtasks /Run /TN %s" % TASK)[:100])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--status", action="store_true", help="只看现在有几个")
    ap.add_argument("--only-kill", action="store_true", help="只清理旧进程")
    ap.add_argument("--only-start", action="store_true", help="只启一个")
    args = ap.parse_args()

    if args.status:
        rows = procs()
        print("哨兵进程 %d 个：" % len(rows))
        for r in rows:
            print("  pid=%-6d ppid=%-6d 起于 %s" % (r["pid"], r["ppid"], r["started"]))
        print("结论：" + ("正常（一对＝一个逻辑实例）" if len(rows) == 2 else
                        ("没有在跑" if not rows else "异常：预期一对，实际 %d 个" % len(rows))))
        return 0

    if args.only_kill:
        kill_all()
        print("只清理：还剩 %d 个" % len(procs()))
        return 0

    if args.only_start:
        start()
        time.sleep(4)
        print("只启动：现在 %d 个" % len(procs()))
        return 0

    print("1) 清理旧进程（schtasks /End 管不到脱钩的子进程，必须按 pid 杀）")
    kill_all()
    if not wait_empty():
        print("！仍有进程未退出，请手工检查：")
        for r in procs():
            print("  ", r)
        return 1

    print("2) 启动")
    start()
    time.sleep(4)

    print("3) 校验")
    rows = procs()
    for r in rows:
        print("  pid=%-6d ppid=%-6d 起于 %s" % (r["pid"], r["ppid"], r["started"]))
    if len(rows) == 2:
        print("OK：只剩一对（venv 外壳 + 真身），单实例在跑")
        return 0
    print("！预期 2 个，实际 %d 个 —— 可能有两份 run_sentinel.vbs 在抢" % len(rows))
    return 1


if __name__ == "__main__":
    sys.exit(main())
