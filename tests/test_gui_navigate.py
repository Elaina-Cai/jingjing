# -*- coding: utf-8 -*-
"""act 的第一小步：`gui_navigate`（只跳转 + 状态差异回执）。

出处：`docs/decisions/软件适配器.md` §13.5、`docs/decisions/工具包.md` §7。
它证明三件事：① `ui_control` 档默认**不可调**（连 prompt 都不进去）；
② 动别人的界面要人显式开；③ 写动作靠**状态差异**回执，不靠内容哈希。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import features  # noqa: E402
from core.execution.gui import base as gb  # noqa: E402
from core.execution.gui import cdp as gc  # noqa: E402
from core.jobs import policy as jp  # noqa: E402
from core.tools import builtin as tb  # noqa: E402
from core.tools.registry import Effect  # noqa: E402

FAILED = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global FAILED
    if cond:
        print(f"PASS {name}")
    else:
        FAILED += 1
        print(f"FAIL {name} :: {detail}")


SPEC = {s.name: s for s in tb.all_specs()}
REAL_READER = gc.CdpReader
NAV = SPEC["ui"]


def test_spec() -> None:
    check("规格存在", "ui" in SPEC)
    check("effects 只有 ui_control", NAV.effects == frozenset({Effect.UI_CONTROL}), str(NAV.effects))
    check("不是幂等的", NAV.idempotent is False, str(NAV.idempotent))
    check("参数是 action/target/what/text", set(NAV.args) == {"action", "target", "what", "text"}, str(NAV.args))
    check("超时 20 秒", NAV.limits.get("timeout_s") == 20, str(NAV.limits))
    check("不属 read 组", "ui" not in [s.name for s in tb.read_specs()])
    check("不属 write 组", "ui" not in [s.name for s in tb.write_specs()])
    check("属 ui 组", "ui" in [s.name for s in tb.ui_specs()], str([s.name for s in tb.ui_specs()]))


def test_gate() -> None:
    off = tb.build_registry()
    ok, reason = off.check_call("ui", {"target": "wx", "what": "x"}, turn_id="t")
    check("默认不可调", ok is False, str((ok, reason)))
    check("原因是 not_enabled", reason == "not_enabled", reason)

    on = tb.build_registry(enable_ui_control=True)
    ok2, reason2 = on.check_call("ui", {"target": "wx", "what": "x"}, turn_id="t")
    check("人开了才可调", ok2 is True, str((ok2, reason2)))

    from core.tools import prompt as tp

    check("旧名不再出现在 prompt", "gui_click" not in tp.build_tool_block(off))
    check("开了才进 prompt", "ui" in tp.build_tool_block(on))
    check("read 工具照旧可调", off.check_call("web_search", None, turn_id="t")[0] is True)


def test_url_guard() -> None:
    check("http 放行", gb.check_nav_url("http://e.com/a") == "http://e.com/a")
    check("https 放行", gb.check_nav_url("  https://e.com/a  ") == "https://e.com/a")
    for bad in ("javascript:alert(1)", "file:///C:/x", "data:text/html,x", "about:blank", "e.com"):
        try:
            gb.check_nav_url(bad)
            ok = False
        except gb.GuiError as exc:
            ok = exc.code == "blocked"
        check(f"拒绝 {bad!r}", ok)
    try:
        gb.check_nav_url("")
        empty_ok = False
    except gb.GuiError as exc:
        empty_ok = exc.code in ("blocked", "unsupported")
    check("拒绝空地址", empty_ok)


def test_diff() -> None:
    before = gb.GuiSnapshot(title="A", url="https://e.com/a", text="同一段")
    after = gb.GuiSnapshot(title="B", url="https://e.com/b", text="同一段")
    changed = gb.diff_snapshots(before, after)
    check("差异含 url/title", set(changed) == {"url", "title", "digest"}, str(sorted(changed)))
    check("没变的字段不出现", "text" not in changed, str(sorted(changed)))
    check("相同快照无差异", gb.diff_snapshots(before, before) == {})
    check("expect 命中", gb.expect_found(gb.GuiSnapshot(text="订单 已创建"), "订单") is True)
    check("expect 未命中", gb.expect_found(gb.GuiSnapshot(text="首页"), "订单") is False)
    check("空 expect 不算命中", gb.expect_found(gb.GuiSnapshot(text="订单"), "") is False)


def _motion(**kw):
    base = dict(
        target="P1",
        action="navigate",
        url_before="https://e.com/",
        url_after="https://e.com/order",
        title_after="订单",
        state_before="aaa",
        state_after="bbb",
        changed={"url": ["https://e.com/", "https://e.com/order"]},
        expect_found=True,
        at="2026-10-21 13:00:00",
    )
    base.update(kw)
    return gb.GuiMotion(**base)


def test_handler(monkeypatch) -> None:
    class FakeReader:
        def __init__(self, *a, **k) -> None:
            self.args = (a, k)

        def navigate(self, url, *, target="", expect="", max_chars=4000, polls=None):
            self.url = url
            return _motion()

    monkeypatch.setattr(gc, "CdpReader", FakeReader)
    out = tb.gui_navigate_handler(what="https://e.com/order")
    check("handler ok", out.status == "ok", out.status)
    check("正文有地址", "https://e.com/order" in out.content, out.content)
    check("正文说清变化", "已跳转" in out.content, out.content)
    check("带状态差异", (out.state_before, out.state_after) == ("aaa", "bbb"), str(out))
    check("changed 透传", "url" in out.changed, str(out.changed))
    check("可逆", out.reversible is True, str(out.reversible))

    class ErrReader:
        def __init__(self, *a, **k) -> None:
            pass

        def navigate(self, *a, **k):
            raise gb.GuiError("not_connected", "9222 没开")

    monkeypatch.setattr(gc, "CdpReader", ErrReader)
    out2 = tb.gui_navigate_handler(what="https://e.com/order")
    check("没连上是 not_connected", out2.status == "not_connected", out2.status)

    class BlockedReader:
        def __init__(self, *a, **k) -> None:
            pass

        def navigate(self, *a, **k):
            raise gb.GuiError("blocked", "GUI_CHANNEL=off")

    monkeypatch.setattr(gc, "CdpReader", BlockedReader)
    out3 = tb.gui_navigate_handler(what="https://e.com/order")
    check("开关没开是 ui_blocked", out3.status == "ui_blocked", out3.status)


class FakeSession:
    def __init__(self, states) -> None:
        self.states = list(states)
        self.sent: list[str] = []
        self.i = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def send(self, method, params=None):
        self.sent.append(method)
        if method == "Page.navigate":
            return {}
        state = self.states[min(self.i, len(self.states) - 1)]
        self.i += 1
        return {"result": {"value": json.dumps(state)}}


def test_driver(monkeypatch) -> None:
    monkeypatch.setattr(gc, "CdpReader", REAL_READER)
    monkeypatch.setattr(gc, "_mode", lambda: features.MODE_ON)
    pages = [
        {
            "id": "P1",
            "type": "page",
            "title": "t",
            "url": "https://e.com/",
            "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/P1",
        }
    ]
    states = [
        {"title": "首页", "url": "https://e.com/", "text": "首页正文"},
        {"title": "订单", "url": "https://e.com/order", "text": "订单正文"},
    ]
    sess = FakeSession(states)
    reader = gc.CdpReader(
        connect=lambda ws: sess,
        http_get=lambda url: pages,
        taken_at=lambda: "2026-10-21 13:00:00",
    )
    motion = reader.navigate("https://e.com/order", expect="订单")
    check("只发出一次 Page.navigate", sess.sent.count("Page.navigate") == 1, str(sess.sent))
    check("读屏两次（前后各一次）", len([m for m in sess.sent if m == "Runtime.evaluate"]) >= 2, str(sess.sent))
    check("跳转前 URL 记下了", motion.url_before == "https://e.com/", motion.url_before)
    check("跳转后 URL 记下了", motion.url_after == "https://e.com/order", motion.url_after)
    check("状态指纹变了", motion.state_before != motion.state_after)
    check("差异含 url", "url" in motion.changed, str(motion.changed))
    check("目标出现", motion.expect_found is True)

    off = gc.CdpReader(
        connect=lambda ws: (_ for _ in ()).throw(AssertionError("开关没开不该连")),
        http_get=lambda url: (_ for _ in ()).throw(AssertionError("开关没开不该摸端口")),
    )
    monkeypatch.setattr(gc, "_mode", lambda: features.MODE_OFF)
    try:
        off.navigate("https://e.com/order")
        blocked = False
    except gb.GuiError as exc:
        blocked = exc.code == "blocked"
    check("off 档直接 blocked 且不连端口", blocked)

    monkeypatch.setattr(gc, "_mode", lambda: features.MODE_ON)
    try:
        gc.CdpReader(http_get=lambda url: pages).navigate("javascript:x")
        guarded = False
    except gb.GuiError as exc:
        guarded = exc.code == "blocked"
    check("坏 scheme 在驱动层也拦", guarded)


def test_tiers() -> None:
    check("ui_control 是 confirm 档", jp.effect_tier("ui_control") == jp.TIER_CONFIRM, jp.effect_tier("ui_control"))
    check("read 是 auto 档", jp.effect_tier("read") == jp.TIER_AUTO, jp.effect_tier("read"))
    check("write_local 是 grant 档", jp.effect_tier("write_local") == jp.TIER_GRANT, jp.effect_tier("write_local"))
    check("external_send 是 confirm 档", jp.effect_tier("external_send") == jp.TIER_CONFIRM)
    check("未知 effect 取最严", jp.effect_tier("whatever") == jp.TIER_CONFIRM)


def main() -> int:
    test_spec()
    test_gate()
    test_url_guard()
    test_diff()
    import types

    class MP:
        def __init__(self) -> None:
            self.saved = []

        def setattr(self, obj, name, value):
            self.saved.append((obj, name, getattr(obj, name)))
            setattr(obj, name, value)

        def undo(self) -> None:
            for obj, name, value in reversed(self.saved):
                setattr(obj, name, value)

    mp = MP()
    try:
        test_handler(mp)
        test_driver(mp)
    finally:
        mp.undo()
    test_tiers()
    print(f"RESULT {FAILED} failed")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
