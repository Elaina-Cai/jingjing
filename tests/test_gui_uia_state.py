"""`gui_state`：原生窗口的控件树读屏（只读、有预算、只认唯一窗口）。

跑法：``python -X utf8 -u tests/test_gui_uia_state.py``
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import features  # noqa: E402
from core.execution.gui import uia  # noqa: E402
from core.execution.gui.base import GuiError, GuiSnapshot  # noqa: E402
from core.tools import builtin  # noqa: E402

_FAILED: list[str] = []
_TOTAL = 0


def check(name: str, cond: object, detail: str = "") -> None:
    global _TOTAL
    _TOTAL += 1
    if cond:
        print(f"  ok   {name}")
        return
    _FAILED.append(name)
    print(f"  FAIL {name}  {detail}")


WINDOWS = [
    {"title": "微信", "class": "mmui::MainWindow", "hwnd": 11, "rect": (1, 0, 1276, 1525), "active": False},
    {"title": "微信", "class": "Qt51514QWindowToolSaveBits", "hwnd": 12, "rect": (260, 996, 327, 180), "active": False},
    {"title": "控制台", "class": "TkTopLevel", "hwnd": 13, "rect": (156, 156, 984, 839), "active": True},
]

NODES = [
    {"role": "WindowControl", "name": "微信", "rect": (1, 0, 1276, 1525), "depth": 0},
    {"role": "ButtonControl", "name": "通讯录", "rect": (1, 213, 90, 54), "depth": 1},
    {"role": "EditControl", "name": "", "rect": (390, 1137, 877, 368), "depth": 2},
]


class FakeBackend:
    def __init__(self, windows=None, nodes=None):
        self._windows = list(WINDOWS if windows is None else windows)
        self._nodes = list(NODES if nodes is None else nodes)
        self.calls: list[str] = []

    def windows(self):
        self.calls.append("windows")
        return list(self._windows)

    def tree(self, hwnd, *, max_depth=12):
        self.calls.append(f"tree:{hwnd}")
        return list(self._nodes)


def with_mode(value: str):
    """临时改 `uia._mode`（不改 env，避免串档）。"""
    original = uia._mode
    uia._mode = lambda: value
    return original


def main() -> int:
    print("[1] pick_window：只认唯一命中")
    check("空 target 取前台", uia.pick_window(WINDOWS, "")["hwnd"] == 13, repr(uia.pick_window(WINDOWS, "")))
    check("没有前台就取第一个", uia.pick_window([dict(WINDOWS[0], active=False)], "")["hwnd"] == 11)
    check("空列表返回 None", uia.pick_window([], "微信") is None)
    check("两个都叫微信 → 不猜", uia.pick_window(WINDOWS, "微信") is None, repr(uia.pick_window(WINDOWS, "微信")))
    check("按类名唯一命中", (uia.pick_window(WINDOWS, "mmui") or {}).get("hwnd") == 11)
    check("按标题唯一命中", (uia.pick_window(WINDOWS, "控制台") or {}).get("hwnd") == 13)
    check("大小写不敏感", (uia.pick_window(WINDOWS, "tktoplevel") or {}).get("hwnd") == 13)

    print("[2] render_tree：形状与预算")
    text, truncated = uia.render_tree(NODES)
    check("不是截断", truncated is False, repr(truncated))
    check("有窗口行", 'WindowControl "微信"' in text, text[:120])
    check("可点的标出来", 'ButtonControl "通讯录" (1,213,90,54) *可动' in text, text)
    check("空名字也留结构", "[2] EditControl (390,1137,877,368) *可动" in text, text)
    check("缩进按深度", text.splitlines()[1].startswith("  [1]"), repr(text.splitlines()[1]))
    many = [dict(NODES[0], depth=0) for _ in range(10)]
    text2, cut2 = uia.render_tree(many, max_elements=3)
    check("节点预算生效", len(text2.splitlines()) == 3 and cut2 is True, f"{len(text2.splitlines())} {cut2}")
    text3, cut3 = uia.render_tree(many * 4, max_chars=200)
    check("字数预算生效", len(text3) <= 400 and cut3 is True, f"{len(text3)} {cut3}")

    print("[3] UiaReader：三档开关与错误码")
    backend = FakeBackend()
    original = with_mode(features.MODE_OFF)
    try:
        try:
            uia.UiaReader(backend=backend).snapshot()
            check("off 档必须抛 blocked", False, "没抛")
        except GuiError as exc:
            check("off 档抛 blocked", exc.code == "blocked", exc.code)
        check("off 档一次都没摸窗口", backend.calls == [], repr(backend.calls))
        uia._mode = lambda: features.MODE_SHADOW
        try:
            uia.UiaReader(backend=backend).snapshot()
            check("shadow 档也抛 blocked", False, "没抛")
        except GuiError as exc:
            check("shadow 档也抛 blocked", exc.code == "blocked", exc.code)
        check("shadow 档也没摸窗口", backend.calls == [], repr(backend.calls))
    finally:
        uia._mode = original

    original = with_mode(features.MODE_ON)
    try:
        snap = uia.UiaReader(backend=backend, taken_at="2026-10-20 19:50:00").snapshot("mmui")
        check("按类名唯一命中读到微信", snap.title == "微信", repr(snap))
        try:
            uia.UiaReader(backend=backend).snapshot("微信")
            check("同名两个窗口时不猜", False, "没抛")
        except GuiError as exc:
            check("同名两个窗口时不猜", exc.code == "no_target", exc.code)
        check("正文带窗口抬头", snap.text.startswith("窗口: 微信 [mmui::MainWindow]"), snap.text[:60])
        check("正文带控件行", "ButtonControl" in snap.text, snap.text[:200])
        check("url 是空的（桌面应用没有地址）", snap.url == "", repr(snap.url))
        check("taken_at 透传", snap.taken_at == "2026-10-20 19:50:00", snap.taken_at)
        called = list(backend.calls)
        check("列了两次窗口但只读了一棵树", called.count("windows") == 2 and called.count("tree:11") == 1, repr(called))
        try:
            uia.UiaReader(backend=FakeBackend()).snapshot("不存在的窗口")
            check("找不到窗口要抛 no_target", False, "没抛")
        except GuiError as exc:
            check("找不到窗口抛 no_target", exc.code == "no_target", exc.code)
        empty = uia.UiaReader(backend=FakeBackend(nodes=[])).snapshot("控制台")
        check("空树也能给抬头", empty.title == "控制台" and "窗口: 控制台" in empty.text, repr(empty))
    finally:
        uia._mode = original

    print("[4] 工具规格与调度")
    specs = {s.name: s for s in builtin.ui_specs()}
    check("ui_state 已登记", "ui_state" in specs, sorted(specs))
    check("它是只读", specs["ui_state"].effects == frozenset({"read"}), repr(specs["ui_state"].effects))
    check("它在只读组里默认启用", "ui_state" in {s.name for s in builtin.read_specs()} or "ui_state" in {s.name for s in builtin.ui_specs()})
    registry = builtin.build_registry()
    ok, reason = registry.check_call("ui_state")
    check("注册表里可调", ok is True, reason)

    real = uia.UiaReader
    try:
        class FakeReader:
            def __init__(self, *, taken_at=""):
                self.taken_at = taken_at

            def snapshot(self, target="", *, max_chars=None):
                return GuiSnapshot(target="微信", url="", title="微信", text="窗口: 微信\n[x]", taken_at=self.taken_at or "t")

        uia.UiaReader = FakeReader
        import core.execution.gui.uia as mod

        mod.UiaReader = FakeReader
        out = builtin.gui_state_handler(target="mmui")
        check("handler ok", out.status == "ok", repr(out))
        check("source 标明 uia", str(out.source).startswith("uia:"), repr(out.source))
        check("正文进 content", "窗口: 微信" in out.content, out.content[:60])

        class EmptyReader(FakeReader):
            def snapshot(self, target="", *, max_chars=None):
                return GuiSnapshot(target="微信", url="", title="微信", text="", taken_at="t")

        mod.UiaReader = EmptyReader
        out2 = builtin.gui_state_handler()
        check("空屏 → no_result", out2.status == "no_result", repr(out2))

        class BoomReader(FakeReader):
            def snapshot(self, target="", *, max_chars=None):
                raise GuiError("blocked", "GUI_CHANNEL=off")

        mod.UiaReader = BoomReader
        out3 = builtin.gui_state_handler()
        check("blocked 映射成 ui_blocked", out3.status == "ui_blocked", repr(out3))

        class CrashReader(FakeReader):
            def snapshot(self, target="", *, max_chars=None):
                raise RuntimeError("炸了")

        mod.UiaReader = CrashReader
        out4 = builtin.gui_state_handler()
        check("驱动炸了降级 unsupported_type", out4.status == "unsupported_type", repr(out4))
    finally:
        import core.execution.gui.uia as mod

        mod.UiaReader = real

    print("[5] 静态纪律")
    src = (ROOT / "core/execution/gui/uia.py").read_text(encoding="utf-8")
    for word in ("Invoke(", "SetValue(", "dispatchMouseEvent", "SendInput"):
        check(f"uia.py 里没有 {word}", word not in src, word)
    win = (ROOT / "core/execution/gui/uia_win.py").read_text(encoding="utf-8")
    check("写窗口只在一个入口里", win.count("def show(") == 1, str(win.count("def show(")))
    check("uia.py 不 import speech/routing", not any(m in src for m in ("core.speech", "core.routing")), "分层")

    print(f"RESULT {len(_FAILED)} failed")
    for name in _FAILED:
        print(f"  - {name}")
    return len(_FAILED)


if __name__ == "__main__":
    raise SystemExit(main())
