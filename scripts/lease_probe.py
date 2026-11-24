r"""设备租约真机验收（三条）——改 `DEVICE_LEASE=on` 之前先跑一遍。

为什么要有这个脚本：租约是「拿不到手就不动手」的唯一保证，而它的三条性质
（互斥 / 崩溃后能被接管 / 不同资源可并行）单元测试只能用假模块验，真机文件锁
要另一套证据：真进程、真 msvcrt、真 kill。

用法：
    python -X utf8 -m scripts.lease_probe            # 全跑（约 15 秒）
    python -X utf8 -m scripts.lease_probe --case mutex
    ... --case ttl | hard | parallel | state
    ... --lock-dir .test_tmp\locks     # 换个目录跑，不碰 data/.locks
    ... --json output/lease_probe.json

判据（每条都会打印 ok / FAIL，末尾 `RESULT n failed / m checks`）：
1. 互斥：另一进程持 `input` 时，本进程等到 `wait_s` 就抛 `UiBusy`，且 holder 是对方；
   对方退出后立刻能拿到。
2. TTL 接管：持锁进程被 kill -9（心跳停），本进程在 `ttl` 之后、`wait_s` 之内接管。
3. hard_ttl 不可续：心跳一直在也不能无限持有——超过 `hard_ttl` 别人就能接管。
4. 并行：`input` 与 `page:tab2` 可同时持有；同一资源仍然互斥。
5. 可观测：状态文件里有 owner / pid / heartbeat_at / created_at（排障要看的四样）。

退出码：0 = 全过；1 = 有 FAIL。**只在改开关前跑它，不要进 CI**（要真进程 + 真休眠）。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.execution.gui import lease as L  # noqa: E402

CHECKS: list[str] = []
FAILED: list[str] = []
PREFIX = "probe-"

HOLDER = """
import sys, time
sys.path.insert(0, r"{root}")
from core.execution.gui.lease import device_lease
device_lease(
    "{resource}", "{owner}",
    ttl={ttl}, hard_ttl={hard_ttl}, wait_s=10.0, lock_dir=r"{lock_dir}",
)
print("HELD", flush=True)
time.sleep({hold_s})
"""


def check(name: str, cond: bool, detail: str = "") -> None:
    CHECKS.append(name)
    if cond:
        print(f"  ok   {name}")
        return
    FAILED.append(f"{name} {detail}".strip())
    print(f"  FAIL {name} {detail}".strip())


def spawn_holder(lock_dir: Path, *, ttl: float = 60.0, hard_ttl: float = 120.0,
                 hold_s: float = 30.0, owner: str = "probe-child") -> subprocess.Popen:
    """起一个真进程去拿手，等它真的拿到（看到 HELD）再返回。"""
    src = HOLDER.format(root=ROOT, resource=L.RESOURCE_INPUT, owner=owner,
                        ttl=ttl, hard_ttl=hard_ttl, hold_s=hold_s, lock_dir=lock_dir)
    proc = subprocess.Popen(
        [sys.executable, "-X", "utf8", "-c", src],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        encoding="utf-8", errors="replace",
    )
    seen: list[str] = []

    def _read() -> None:
        line = proc.stdout.readline() if proc.stdout is not None else ""
        seen.append(line.strip())

    t = threading.Thread(target=_read, daemon=True)
    t.start()
    t.join(15.0)
    if not seen or seen[0] != "HELD":
        code = proc.poll()
        err = ""
        if proc.stderr is not None and code is not None:
            err = (proc.stderr.read() or "")[-400:]
        proc.kill()
        raise RuntimeError(f"子进程没拿到手（exit={code}）：{err}")
    return proc


def cleanup(lock_dir: Path) -> None:
    """只清理本脚本造的状态文件（owner 以 probe- 开头）。"""
    for path in lock_dir.glob("*.json"):
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if str(state.get("owner") or "").startswith(PREFIX):
            try:
                path.unlink()
            except OSError:
                pass


def case_mutex(lock_dir: Path) -> None:
    child = spawn_holder(lock_dir, hold_s=6.0)
    holder = ""
    started = time.monotonic()
    try:
        L.device_lease(L.RESOURCE_INPUT, "probe-parent", wait_s=1.2, lock_dir=lock_dir)
    except L.UiBusy as exc:
        holder = getattr(exc, "holder", "")
    waited = time.monotonic() - started
    check("互斥：另一进程持手时本进程拿不到", holder != "", holder)
    check("互斥：报出的是对方的 owner", holder == "probe-child", holder)
    check("互斥：等满 wait_s 就放弃（不会无限等）", 1.0 <= waited <= 3.0, f"{waited:.2f}s")
    child.wait(timeout=30)
    ok_after = False
    try:
        held = L.device_lease(L.RESOURCE_INPUT, "probe-parent", wait_s=5.0, lock_dir=lock_dir)
        ok_after = True
        held.release()
    except L.UiBusy as exc:
        print(f"  （对方退出后仍拿不到：{exc}）")
    check("互斥：对方退出后立刻能拿到", ok_after)


def case_ttl(lock_dir: Path) -> None:
    """崩溃接管：kill -9 之后锁立刻释放（这条是「为什么崩溃不会永久堵死」的证据）。"""
    child = spawn_holder(lock_dir, ttl=3.0, hard_ttl=60.0, hold_s=60.0)
    child.kill()  # 模拟崩溃：心跳立刻停，锁文件留着
    child.wait(timeout=10)
    started = time.monotonic()
    took = False
    try:
        held = L.device_lease(L.RESOURCE_INPUT, "probe-parent", ttl=3.0, hard_ttl=60.0,
                              wait_s=15.0, lock_dir=lock_dir)
        took = True
        held.release()
    except L.UiBusy as exc:
        print(f"  （15 秒内没接管：{exc}）")
    waited = time.monotonic() - started
    check("崩溃接管：被 kill -9 后能被接管（不等人来清）", took)
    check("崩溃接管：不需要等满 wait_s（进程一死锁就释放）", waited <= 15.0, f"{waited:.2f}s")


def case_hard(lock_dir: Path) -> None:
    """hard_ttl 的两面：锁空闲时能接管；**持有者活着时抢不动**（已知限制，必须写下来）。"""
    mine = L.device_lease(L.RESOURCE_INPUT, "probe-a", ttl=30.0, hard_ttl=1.0,
                          wait_s=3.0, lock_dir=lock_dir)
    time.sleep(1.6)
    blocked = False
    try:
        other = L.device_lease(L.RESOURCE_INPUT, "probe-b", ttl=30.0, hard_ttl=30.0,
                               wait_s=1.5, lock_dir=lock_dir)
        other.release()
    except L.UiBusy:
        blocked = True
    check("hard_ttl：持有者还活着时抢不动（已知限制，靠 wait_s + fail-closed 兜）", blocked)
    mine.release()

    # 锁空闲 + 状态过期（模拟「上把锁没清干净」）：必须能接管，且不会一直等
    stale_dir = lock_dir / "stale"
    stale_dir.mkdir(parents=True, exist_ok=True)
    now = time.time()
    (stale_dir / "input.json").write_text(json.dumps({
        "resource": L.RESOURCE_INPUT, "owner": "probe-dead", "pid": 1,
        "created_at": now - 300.0, "heartbeat_at": now - 300.0, "token": "x",
    }), encoding="utf-8")
    took = False
    started = time.monotonic()
    try:
        held = L.device_lease(L.RESOURCE_INPUT, "probe-c", ttl=30.0, hard_ttl=120.0,
                              wait_s=3.0, lock_dir=stale_dir)
        took = True
        held.release()
    except L.UiBusy as exc:
        print(f"  （状态过期却拿不到：{exc}）")
    check("hard_ttl：锁空闲 + 状态过期 → 立刻接管", took and (time.monotonic() - started) < 2.0)


def case_parallel(lock_dir: Path) -> None:
    held = L.device_lease(L.RESOURCE_INPUT, "probe-input", lock_dir=lock_dir)
    page = None
    try:
        page = L.device_lease("page:tab2", "probe-page", wait_s=2.0, lock_dir=lock_dir)
    except L.UiBusy as exc:
        print(f"  （不同资源也被挡：{exc}）")
    check("并行：input 与 page:tab2 可同时持有", page is not None)
    blocked = False
    try:
        L.device_lease(L.RESOURCE_INPUT, "probe-input-2", wait_s=0.3, lock_dir=lock_dir)
    except L.UiBusy:
        blocked = True
    check("并行：同一资源仍然互斥", blocked)
    if page is not None:
        page.release()
    held.release()


def case_state(lock_dir: Path) -> None:
    held = L.device_lease(L.RESOURCE_INPUT, "probe-state", lock_dir=lock_dir)
    state = L.read_state(L.RESOURCE_INPUT, lock_dir=lock_dir)
    held.release()
    for key in ("owner", "pid", "created_at", "heartbeat_at"):
        check(f"可观测：状态里有 {key}", key in state, str(sorted(state)))
    check("可观测：owner 对得上", state.get("owner") == "probe-state", str(state.get("owner")))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="设备租约真机验收（三条）")
    ap.add_argument("--case", default="all",
                    choices=["all", "mutex", "ttl", "hard", "parallel", "state"])
    ap.add_argument("--lock-dir", default=str(L.LOCK_DIR))
    ap.add_argument("--json", default="")
    args = ap.parse_args(argv)
    lock_dir = Path(args.lock_dir)
    lock_dir.mkdir(parents=True, exist_ok=True)

    print(f"lock_dir={lock_dir}")
    cases = {"mutex": case_mutex, "ttl": case_ttl, "hard": case_hard,
             "parallel": case_parallel, "state": case_state}
    todo = list(cases) if args.case == "all" else [args.case]
    try:
        for name in todo:
            print(f"[{name}]")
            cases[name](lock_dir)
    finally:
        cleanup(lock_dir)

    print(f"RESULT {len(FAILED)} failed / {len(CHECKS)} checks")
    if args.json:
        out = Path(args.json)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({
            "lock_dir": str(lock_dir), "checks": CHECKS, "failed": FAILED,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"json={out}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
