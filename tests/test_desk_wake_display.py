"""熄屏兜底：黑屏能发，但"激活窗口失败"时要点亮一次再试一次（2026-11-01）。

实测（docs/交接.md「熄屏自测」）：屏幕全黑时 desk 第一次 send 报
`无法激活微信窗口`，第二次就成功。所以策略是：**默认不亮屏**，只在识别到
"窗口没拿到前台"这类失败时，点亮显示器一次、重试一次。
"""

import os

# tests/xxx.py → dirname 两次才到仓库根
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

import sys  # noqa: E402

if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def _read(*parts) -> str:
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as f:
        return f.read()


def test_awake_never_requests_display_by_default():
    """常驻请求只能是 ES_SYSTEM_REQUIRED，绝不能带 ES_DISPLAY_REQUIRED（会不让熄屏）。"""
    src = _read("core", "infra", "awake.py")
    assert "_ES_SYSTEM_REQUIRED = 0x00000001" in src
    assert "ES_DISPLAY_REQUIRED(_ES_DISPLAY_REQUIRED" not in src
    # 常驻那一行只允许这两个位
    line = [l for l in src.splitlines() if "SetThreadExecutionState(_ES_CONTINUOUS" in l]
    assert line, "找不到常驻请求那一行"
    assert all("_ES_DISPLAY_REQUIRED" not in l for l in line)


def test_wake_uses_postmessage_not_sendmessage():
    """广播 SendMessage 会等所有窗口应答；半死的窗口能让它永久挂住（实测）。"""
    src = _read("core", "infra", "awake.py")
    assert "PostMessageW(0xFFFF, _WM_SYSCOMMAND, _SC_MONITORPOWER, _MONITOR_ON)" in src
    assert "SendMessageW(0xFFFF" not in src


def test_desk_residue_strikes_bound_retries():
    """2026-11-04 起：输入框残留不再靠「激活失败重试一次」，改成记笔画、到次数判死。

    为什么换了：残留清不干净时反复换窗口重试会把一条消息在三四个会话之间来回拖，
    40 秒才发出去。现在按消息 id 记笔画，到 RESIDUE_STRIKE_LIMIT 次就停，不再排回退避。
    """
    src = _read("loop", "desk.py")
    assert "_residue.RESIDUE_MARKER in why" in src
    assert "_residue.note_strike(" in src
    assert "desk_residue_blocked" in src
    assert 'wake_display_once("desk_activation_retry")' not in src

# ---- 2026-11-24：把「源码里有没有这行」补成真调用（行为断言） ----

class _Fn:
    restype = None

    def __init__(self, sink):
        self.sink = sink

    def __call__(self, *args):
        self.sink.append(args)
        return 1


class _K32:
    def __init__(self, sink):
        self.SetThreadExecutionState = _Fn(sink)


class _U32:
    def __init__(self, sink):
        self.PostMessageW = _Fn(sink)
        self.SendMessageW = _Fn(sink)


def test_keep_system_awake_calls_win32_without_display_bit():
    import ctypes as _ctypes
    from core.infra import awake

    calls = []
    saved_windll = awake.ctypes.windll
    saved_state = dict(awake._STATE)
    awake.ctypes.windll = type("_W", (), {"kernel32": _K32(calls)})()
    try:
        assert awake.keep_system_awake("test") is True
    finally:
        awake.ctypes.windll = saved_windll
        awake._STATE.update(saved_state)
    assert calls == [(awake._ES_CONTINUOUS | awake._ES_SYSTEM_REQUIRED,)]
    assert all(not (flags & awake._ES_DISPLAY_REQUIRED) for (flags,) in calls)


def test_wake_display_once_posts_and_never_sends():
    from core.infra import awake

    calls = []
    saved_windll = awake.ctypes.windll
    awake.ctypes.windll = type("_W", (), {"user32": _U32(calls)})()
    try:
        assert awake.wake_display_once("test") is True
    finally:
        awake.ctypes.windll = saved_windll
    assert calls == [(0xFFFF, awake._WM_SYSCOMMAND, awake._SC_MONITORPOWER, awake._MONITOR_ON)]


def main() -> bool:
    """2026-11-02 补：这个文件以前只有 def test_、没有运行入口（一直空转，一条都没跑过）。"""
    names = sorted(n for n in globals() if n.startswith("test_"))
    fails = []
    for n in names:
        fn = globals()[n]
        if not callable(fn):
            continue
        try:
            fn()
            print("  ok   %s" % n)
        except Exception as exc:  # noqa: BLE001 - 汇总用，单个用例炸了要继续跑完
            fails.append(n)
            print("  FAIL %s  %s" % (n, repr(exc)[:200]))
    print("RESULT %d failed / %d" % (len(fails), len(names)))
    return not fails


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
