"""设备租约的硬要求：互斥、TTL 接管、hard_ttl 不可续、崩溃自愈、白名单。

跑法：.venv_it\\Scripts\\python.exe -X utf8 -u tests/test_device_lease.py
"""

from __future__ import annotations

import subprocess
import sys
import pathlib
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.execution.gui import lease as L  # noqa: E402

CHILD = """
import sys, time
sys.path.insert(0, r"{root}")
from core.execution.gui.lease import device_lease
lease = device_lease("input", "{owner}", wait_s=2.0, lock_dir=r"{lock_dir}")
print("HELD", flush=True)
time.sleep({secs})
"""

FAILS: list[str] = []
COUNT = [0]


def check(name: str, cond: bool, detail: str = "") -> None:
    COUNT[0] += 1
    if cond:
        print(f"  ok  {name}")
    else:
        FAILS.append(f"{name} :: {detail}")
        print(f"  FAIL {name}  {detail}")


def tmpdir() -> Path:
    base = ROOT / ".test_tmp" / "lease"
    base.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix="lease_", dir=str(base)))


def expect_busy(fn, *, holder_contains: str = "") -> tuple[bool, str]:
    try:
        fn()
    except L.UiBusy as exc:
        return True, exc.holder
    return False, ""


def spawn_child(lock_dir: Path, owner: str = "child", secs: float = 3.0) -> subprocess.Popen:
    code = CHILD.format(root=str(ROOT), lock_dir=str(lock_dir), owner=owner, secs=secs)
    proc = subprocess.Popen(
        [sys.executable, "-X", "utf8", "-c", code],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
    )
    line = proc.stdout.readline() if proc.stdout else ""
    assert "HELD" in line, f"子进程没拿到租约：{line!r} {proc.stderr.read() if proc.stderr else ''}"
    return proc


def main() -> int:
    print("[1] 同进程两线程互斥")
    d = tmpdir()
    got: list[str] = []

    def grab(tag: str) -> None:
        try:
            with L.Lease("input", tag, wait_s=0.3, lock_dir=d, heartbeat_s=0.2):
                got.append(tag)
                time.sleep(0.6)
        except L.UiBusy:
            got.append(f"{tag}:busy")

    t1 = threading.Thread(target=grab, args=("A",))
    t1.start()
    time.sleep(0.15)
    t2 = threading.Thread(target=grab, args=("B",))
    t2.start()
    t1.join()
    t2.join()
    check("A 拿到、B 报忙", got == ["A", "B:busy"], str(got))

    print("[2] 拿不到时报出持有者名字")
    d2 = tmpdir()
    with L.Lease("input", "主人", wait_s=1.0, lock_dir=d2, heartbeat_s=0.2):
        busy, holder = expect_busy(lambda: L.Lease("input", "路人", wait_s=0.2, lock_dir=d2).acquire())
    check("UiBusy 抛出", busy, "应为 UiBusy")
    check("holder 是持有者", holder == "主人", holder)

    print("[3] 两个进程互斥")
    d3 = tmpdir()
    proc = spawn_child(d3, secs=3.0)
    try:
        busy, holder = expect_busy(lambda: L.Lease("input", "parent", wait_s=0.3, lock_dir=d3).acquire())
        check("跨进程被挡住", busy, "应抛 UiBusy")
        check("跨进程读到 holder=child", holder == "child", holder)
    finally:
        proc.kill()
        proc.wait(timeout=5)

    print("[4] 持锁进程被 kill 后锁自动释放")
    d4 = tmpdir()
    proc = spawn_child(d4, secs=30.0)
    proc.kill()
    proc.wait(timeout=5)
    ok = True
    try:
        lease = L.Lease("input", "after-kill", wait_s=3.0, lock_dir=d4).acquire()
        lease.release()
    except L.UiBusy as exc:
        ok = False
        print("   ", exc)
    check("kill 后能重新拿到", ok)

    print("[5] ttl / hard_ttl 判定 is_expired（不参与抢锁）")
    d5 = tmpdir()
    now = time.time()
    fresh = {"resource": "input", "owner": "活人", "pid": 1, "token": "t",
             "created_at": now, "heartbeat_at": now, "ttl": 60.0, "hard_ttl": 120.0}
    stale = dict(fresh, heartbeat_at=now - 90.0)
    hard = dict(fresh, created_at=now - 300.0, heartbeat_at=now)
    L._write_state(Path(d5) / "input.json", fresh)
    check("心跳新鲜 -> 未过期", not L.Lease("input", "x", lock_dir=d5).is_expired())
    L._write_state(Path(d5) / "input.json", stale)
    check("心跳过期 -> 已过期", L.Lease("input", "x", lock_dir=d5).is_expired())
    L._write_state(Path(d5) / "input.json", hard)
    check("hard_ttl 到（心跳仍新）-> 已过期", L.Lease("input", "x", lock_dir=d5).is_expired())

    print("[6] hard_ttl 到期即使心跳还在也被接管（不可续）")
    d6 = tmpdir()
    now = time.time()
    L._write_state(Path(d6) / "input.json", {
        "resource": "input", "owner": "幽灵", "pid": 1, "token": "t",
        "created_at": now - 300.0, "heartbeat_at": now, "ttl": 60.0, "hard_ttl": 120.0,
    })
    took = False
    try:
        again = L.Lease("input", "接管2", wait_s=1.0, hard_ttl=120.0, lock_dir=d6).acquire()
        took = True
        check("自己的 is_expired 为假（刚拿到）", not again.is_expired())
        again.release()
    except L.UiBusy:
        took = False
    check("hard_ttl 过期被接管", took)

    print("[7] 资源名白名单")
    bad = 0
    for name in ("", "   ", "mouse", "input2", "page:", "页面:1"):
        try:
            L.check_resource(name)
        except ValueError:
            bad += 1
    check("六个非法名全部拒绝", bad == 6, str(bad))
    oknames = [L.check_resource(n) for n in ("input", "screen", "evidence", "page:abc")]
    check("四个合法名通过", oknames == ["input", "screen", "evidence", "page:abc"], str(oknames))

    print("[8] 状态新鲜但锁是空的 -> 接管（锁是唯一活证明）")
    d8 = tmpdir()
    now8 = time.time()
    L._write_state(Path(d8) / "input.json", {
        "resource": "input", "owner": "刚死的人", "pid": 999999, "token": "t",
        "created_at": now8, "heartbeat_at": now8, "ttl": 60.0, "hard_ttl": 120.0,
    })
    took8 = False
    try:
        leased = L.Lease("input", "接管者", wait_s=1.0, lock_dir=d8).acquire()
        took8 = True
        check("接管后状态换成自己", L.read_state("input", lock_dir=d8)["owner"] == "接管者")
        leased.release()
    except L.UiBusy:
        took8 = False
    check("新鲜的矛盾状态也接管", took8)
    print("[8b] with 退出后别人能拿到，重复 release 不抛")
    with L.Lease("input", "ctx", wait_s=1.0, lock_dir=d8):
        pass
    after = L.Lease("input", "next", wait_s=1.0, lock_dir=d8).acquire()
    after.release()
    after.release()
    check("with 释放 + 重复 release", True)

    print("[9] 心跳把 heartbeat_at 往后推")
    d9 = tmpdir()
    lease = L.Lease("input", "beat", wait_s=1.0, lock_dir=d9, auto_heartbeat=False)
    lease.acquire()
    before = L.read_state("input", lock_dir=d9)["heartbeat_at"]
    time.sleep(0.05)
    lease.heartbeat()
    after_beat = L.read_state("input", lock_dir=d9)["heartbeat_at"]
    lease.release()
    check("heartbeat_at 变大", after_beat > before, f"{before} -> {after_beat}")
    check("released_at 落盘", "released_at" in L.read_state("input", lock_dir=d9))

    print("[10] 不同资源名互不阻塞")
    d10 = tmpdir()
    a = L.Lease("input", "x", wait_s=0.5, lock_dir=d10).acquire()
    b_ok = True
    try:
        b = L.Lease("screen", "y", wait_s=0.5, lock_dir=d10).acquire()
        b.release()
    except L.UiBusy:
        b_ok = False
    a.release()
    check("input 与 screen 并行", b_ok)

    print("[11] describe_holder：谁占着、心跳多久没动")
    d11 = tmpdir()
    empty = L.describe_holder("input", lock_dir=d11)
    check("没状态时 holder 是空串", empty["holder"] == "", str(empty))
    check("没状态时心跳年龄是 -1（不知道，不是刚心跳）", empty["heartbeat_age_s"] == -1.0, str(empty))
    held11 = L.Lease("input", "turn-11", wait_s=0.5, lock_dir=d11).acquire()
    info = L.describe_holder("input", lock_dir=d11)
    check("holder 是持有者", info["holder"] == "turn-11", str(info))
    check("pid 是本进程", info["pid"] == __import__("os").getpid(), str(info))
    check("心跳是刚的", 0.0 <= info["heartbeat_age_s"] < 5.0, str(info["heartbeat_age_s"]))
    stale = L.describe_holder("input", lock_dir=d11, now=__import__("time").time() + 1000)
    check("时间往后推 1000s，心跳年龄跟着变老", stale["heartbeat_age_s"] > 900, str(stale["heartbeat_age_s"]))
    held11.release()
    check("坏资源名不抛（排障路径）", isinstance(L.describe_holder("no/such:thing", lock_dir=d11), dict))

    print("[12] 状态文件：瞬时占用要重试，临时名不许撞")
    d12 = tmpdir()
    p12 = d12 / "input.json"
    p12.write_text('{"owner": "主人"}', encoding="utf-8")
    real_read = pathlib.Path.read_text
    real_write = pathlib.Path.write_text
    calls = {"n": 0}

    def flaky_read(self, *a, **kw):
        calls["n"] += 1
        if calls["n"] <= 2:
            # 就是 2026-10-22 全量里那次假红的形态：读侧撞上 os.replace。
            raise PermissionError(32, "另一个进程正在替换这个文件")
        return real_read(self, *a, **kw)

    pathlib.Path.read_text = flaky_read
    try:
        got = L._read_state(p12)
    finally:
        pathlib.Path.read_text = real_read
    check("瞬时 PermissionError 会重试，不把持有者读成空串", got.get("owner") == "主人", str(got))

    names: list[str] = []

    def recording_write(self, *a, **kw):
        names.append(self.name)
        return real_write(self, *a, **kw)

    pathlib.Path.write_text = recording_write
    try:
        L._write_state(p12, {"owner": "A"})
        L._write_state(p12, {"owner": "B"})
    finally:
        pathlib.Path.write_text = real_write
    tmps = [n for n in names if n.endswith(".tmp")]
    check("两个写者用两个不同的临时名", len(tmps) == 2 and tmps[0] != tmps[1], str(tmps))
    check("没有残留的临时文件", not list(d12.glob("*.tmp")), str(list(d12.glob("*.tmp"))))
    check("后写的那份生效", L._read_state(p12).get("owner") == "B", str(L._read_state(p12)))

    print()
    print(f"RESULT {len(FAILS)} failed / {COUNT[0]} checks")
    for line in FAILS:
        print("  -", line)
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())
