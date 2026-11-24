r"""目标与代次的硬要求：不一致就停手、零副作用、证据档可判。

跑法：.venv_it\Scripts\python.exe -X utf8 -u tests/test_current_target.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.execution.gui import target as T  # noqa: E402
from core.execution.gui.lease import CapabilityMissing  # noqa: E402

FAILS: list[str] = []
COUNT = [0]


def check(name: str, cond: bool, detail: str = "") -> None:
    COUNT[0] += 1
    if cond:
        print(f"  ok  {name}")
    else:
        FAILS.append(f"{name} :: {detail}")
        print(f"  FAIL {name}  {detail}")


class Probe:
    """可变探针：测试里改 state 就是「世界变了」。"""

    def __init__(self, **state) -> None:
        self.state = {
            "handle": 527494,
            "pid": 36956,
            "exe": "weixin.exe",
            "title": "微信",
            "rect": (0, 0, 1289, 1537),
            "identity": "文件传输助手",
            "fingerprint": "abc123",
            "session_active": True,
        }
        self.state.update(state)
        self.calls = 0

    def __call__(self, **kwargs):
        self.calls += 1
        return dict(self.state)

    def set(self, **kwargs):
        self.state.update(kwargs)


def raises(fn, exc_type):
    try:
        fn()
    except exc_type as exc:
        return exc
    except Exception as exc:  # noqa: BLE001
        return exc
    return None


def main() -> int:
    T.reset_generations()

    print("[1] acquire 拿全字段 + 代次从 1 起")
    probe = Probe()
    t1 = T.acquire("wechat", chat="文件传输助手", owner="job-1", probe_fn=probe)
    check("handle/pid/exe 带出来", (t1.handle, t1.pid, t1.exe) == (527494, 36956, "weixin.exe"))
    check("identity 是会话", t1.identity == "文件传输助手", t1.identity)
    check("代次 = 1", t1.generation == 1, str(t1.generation))
    check("证据 explicit -> 1.0", t1.confidence == 1.0)
    check("usable", t1.usable)
    check("as_dict 可序列化", t1.as_dict()["handle"] == 527494)

    print("[2] 同一目标再 acquire -> 代次递增")
    t2 = T.acquire("wechat", chat="文件传输助手", probe_fn=probe)
    check("代次 = 2", t2.generation == 2, str(t2.generation))

    print("[3] 目标没变 -> check 通过，且只探不发")
    exc = raises(lambda: T.check(t2, probe_fn=probe), Exception)
    check("check 不抛", exc is None, repr(exc))
    check("check 只调用探针一次", probe.calls == 3, str(probe.calls))

    print("[4] 会话被切走 -> WindowChanged(identity)")
    probe.set(identity="鲸鲸的测试群")
    exc = raises(lambda: T.check(t2, probe_fn=probe), T.WindowChanged)
    check("抛 WindowChanged", isinstance(exc, T.WindowChanged), repr(exc))
    check("原因 identity", getattr(exc, "reason", "") == "identity", getattr(exc, "reason", ""))

    print("[5] 窗口换了 -> WindowChanged(handle)")
    probe2 = Probe(handle=111111)
    exc = raises(lambda: T.check(t2, probe_fn=probe2), T.WindowChanged)
    check("原因 handle", getattr(exc, "reason", "") == "handle", getattr(exc, "reason", ""))

    print("[6] 顶栏指纹变了（同名不同会话）-> WindowChanged(fingerprint)")
    probe3 = Probe(fingerprint="zzz999")
    exc = raises(lambda: T.check(t2, probe_fn=probe3), T.WindowChanged)
    check("原因 fingerprint", getattr(exc, "reason", "") == "fingerprint", getattr(exc, "reason", ""))

    print("[7] 窗口消失 -> WindowGone；锁屏 -> CapabilityMissing")
    gone = raises(lambda: T.check(t2, probe_fn=lambda **k: {}), T.WindowGone)
    check("空探针 -> WindowGone", isinstance(gone, T.WindowGone), repr(gone))
    locked = raises(
        lambda: T.check(t2, probe_fn=Probe(session_active=False)),
        CapabilityMissing,
    )
    check("锁屏 -> CapabilityMissing(session_locked)",
          isinstance(locked, CapabilityMissing) and locked.reason == "session_locked",
          repr(locked))

    print("[8] 代次被别的任务推进 -> WindowChanged(generation)")
    probe.set(identity="文件传输助手", fingerprint="abc123", handle=527494)
    t3 = T.acquire("wechat", chat="文件传输助手", probe_fn=probe)
    # 同一会话、同一指纹：世界没变，但代次被另一个任务推进过（它切走过又切回来了）
    t4 = T.advance(t3)
    check("advance 后代次 +1", t4.generation == t3.generation + 1, f"{t3.generation} -> {t4.generation}")
    check("advance 不动 identity", t4.identity == t3.identity, t4.identity)
    exc = raises(lambda: T.check(t3, probe_fn=probe), T.WindowChanged)
    check("拿旧代次去动手 -> 被拒", getattr(exc, "reason", "") == "generation", getattr(exc, "reason", ""))
    exc2 = raises(lambda: T.check(t4, probe_fn=probe), Exception)
    check("拿新代次 -> 通过", exc2 is None, repr(exc2))

    print("[9] 零副作用：判定失败不动世界")
    snapshot = dict(probe.state)
    raises(lambda: T.check(t3, probe_fn=probe), T.WindowChanged)
    check("探针状态没被改", probe.state == snapshot)

    print("[10] 证据档：recent_db 只有 0.3，不可逆动作要拒")
    t5 = T.acquire("wechat", chat="x", evidence="recent_db", probe_fn=probe)
    check("recent_db -> 0.3", t5.confidence == 0.3, str(t5.confidence))
    weak = raises(lambda: T.require_confidence(t5, 0.9), CapabilityMissing)
    check("弱证据过不了 strict", isinstance(weak, CapabilityMissing)
          and "target_evidence_weak" in weak.reason, repr(weak))
    check("explicit 过得了 strict", raises(lambda: T.require_confidence(t4, 0.9), Exception) is None)
    bad = raises(lambda: T.acquire("wechat", evidence="猜的", probe_fn=probe), ValueError)
    check("未知证据档 -> ValueError", isinstance(bad, ValueError))

    print("[11] 无窗口时 acquire 抛 WindowGone")
    none = raises(lambda: T.acquire("wechat", probe_fn=lambda **k: {}), T.WindowGone)
    check("acquire -> WindowGone", isinstance(none, T.WindowGone))

    print("[12] 平台白名单 + reset_generations")
    bad_platform = raises(lambda: T.acquire("telegram", probe_fn=probe), ValueError)
    check("未知平台 -> ValueError", isinstance(bad_platform, ValueError))
    T.reset_generations()
    t6 = T.acquire("wechat", chat="文件传输助手", probe_fn=probe)
    check("重置后代次回到 1", t6.generation == 1, str(t6.generation))

    print()
    print(f"RESULT {len(FAILS)} failed / {COUNT[0]} checks")
    for line in FAILS:
        print("  -", line)
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())
