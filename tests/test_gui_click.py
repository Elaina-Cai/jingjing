# -*- coding: utf-8 -*-
"""点击：三条判据同时成立才动，动完量像素差（`软件适配器.md` §13.6）。

它证明五件事：① `gui_click` 默认不可调；② 前两条判据在动手**之前**判（不过就一步不动）；
③ 第三条只能事后量，量不到就是失败；④ 界面没变（`changed` 空）不算成功；
⑤ 证据截图落盘、路径进回执。
"""

from __future__ import annotations

import base64
import io as _io
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import features  # noqa: E402
from core.execution.gui import base as gb  # noqa: E402
from core.execution.gui import cdp as gc  # noqa: E402
from core.tools import builtin as tb  # noqa: E402
from core.tools import registry as tr  # noqa: E402

FAILED = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global FAILED
    if cond:
        print(f"PASS {name}")
    else:
        FAILED += 1
        print(f"FAIL {name} :: {detail}")


REAL_READER = gc.CdpReader
SPECS = {s.name: s for s in tb.all_specs()}
CLICK = SPECS["ui"]


def test_spec() -> None:
    check("规格存在", "ui" in SPECS)
    check("effects 只有 ui_control", CLICK.effects == frozenset({tr.Effect.UI_CONTROL}), str(CLICK.effects))
    check("不是幂等的", CLICK.idempotent is False)
    check("参数是 action/target/what/text", set(CLICK.args) == {"action", "target", "what", "text"}, str(CLICK.args))
    check("声明了窗口标题", bool(CLICK.window_title.strip()), CLICK.window_title)
    check("超时 20 秒", CLICK.limits.get("timeout_s") == 20, str(CLICK.limits))
    check(
        "归 ui 组",
        [s.name for s in tb.ui_specs()] == ["ui_state", "ui"],
        str([s.name for s in tb.ui_specs()]),
    )
    check(
        "像素门槛两处同值",
        gb.MIN_CHANGED_PIXELS == tr.UI_MIN_PIXELS_CHANGED == 100,
        f"{gb.MIN_CHANGED_PIXELS}/{tr.UI_MIN_PIXELS_CHANGED}",
    )
    check("no_change 是登记过的状态码", tr.is_known_status("no_change"))


def test_gate() -> None:
    off = tb.build_registry()
    ok, reason = off.check_call("ui", {"target": "wx", "what": "x"}, turn_id="t")
    check("默认不可调", ok is False and reason == "not_enabled", str((ok, reason)))
    on = tb.build_registry(enable_ui_control=True)
    check("人开了才可调", on.check_call("ui", {"target": "wx", "what": "x"}, turn_id="t")[0] is True)
    from core.tools import prompt as tp

    check("旧名不再出现在 prompt", "gui_click" not in tp.build_tool_block(off))
    check("开了才进 prompt", "ui" in tp.build_tool_block(on))
    check("读工具照旧", off.check_call("fetch_url", None, turn_id="t")[0] is True)


def test_pieces() -> None:
    check("选择器去空白", gb.check_selector("  #buy  ") == "#buy")
    for bad in ("", "   ", "x" * 201):
        try:
            gb.check_selector(bad)
            ok = False
        except gb.GuiError:
            ok = True
        check(f"拒绝选择器 {bad[:12]!r}", ok)
    check("标题包含（大小写无关）", gb.window_title_matches("edge", "订单页 - Microsoft Edge") is True)
    check("标题不含即 False", gb.window_title_matches("Edge", "记事本") is False)
    check("空声明 False", gb.window_title_matches("", "Microsoft Edge") is False)
    check("空实体 False", gb.window_title_matches("Edge", "") is False)

    from PIL import Image

    def png(color) -> bytes:
        buf = _io.BytesIO()
        Image.new("RGB", (40, 30), color).save(buf, "PNG")
        return buf.getvalue()

    white, black = png((255, 255, 255)), png((0, 0, 0))
    check("同一张图 0 像素", gb.count_changed_pixels(white, white) == 0)
    check("全变 = 宽×高", gb.count_changed_pixels(white, black) == 40 * 30)
    check("空输入 = -1", gb.count_changed_pixels(b"", black) == -1)


def test_verdict() -> None:
    from dataclasses import replace

    no_title = replace(CLICK, window_title="")
    ok, reason = tr.can_ui_control(no_title, foreground_ok=True, title_ok=True, pixels_changed=999)
    check("没声明标题一律不许", ok is False and reason == tr.UI_BLOCKED, str((ok, reason)))
    ok2, _ = tr.can_ui_control(CLICK, foreground_ok=True, title_ok=True, pixels_changed=99)
    check("像素差不够不许", ok2 is False)
    ok3, _ = tr.can_ui_control(CLICK, foreground_ok=False, title_ok=True, pixels_changed=999)
    check("不在前台不许", ok3 is False)
    ok4, reason4 = tr.can_ui_control(CLICK, foreground_ok=True, title_ok=True, pixels_changed=100)
    check("三条齐了才放行", ok4 is True and reason4 == "", str((ok4, reason4)))


class FakeSession:
    def __init__(self, *, rect, shots, state) -> None:
        self.rect = rect
        self.shots = list(shots)
        self.state = state
        self.sent: list[str] = []
        self.n = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def send(self, method, params=None):
        self.sent.append(method)
        if method == "Page.captureScreenshot":
            self.n += 1
            idx = min(self.n - 1, len(self.shots) - 1)
            return {"data": base64.b64encode(self.shots[idx]).decode()}
        if method == "Input.dispatchMouseEvent":
            return {}
        if method == "Runtime.evaluate":
            expr = str((params or {}).get("expression") or "")
            if "querySelector" in expr:
                return {"result": {"value": self.rect}}
            return {"result": {"value": json.dumps(self.state)}}
        return {}


def _png(color) -> bytes:
    from PIL import Image

    buf = _io.BytesIO()
    Image.new("RGB", (40, 30), color).save(buf, "PNG")
    return buf.getvalue()


PAGES = [
    {
        "id": "P1",
        "type": "page",
        "title": "t",
        "url": "https://e.com/",
        "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/P1",
    }
]


def _reader(sess, *, pages=None, taken="2026-10-21 13:00:00") -> object:
    targets = PAGES if pages is None else pages
    return REAL_READER(
        connect=lambda ws: sess,
        http_get=lambda url: targets,
        taken_at=lambda: taken,
    )


def _patch(monkeypatch, obj, name, value) -> None:
    monkeypatch.setattr(obj, name, value)


def test_driver(monkeypatch) -> None:
    _patch(monkeypatch, gc, "CdpReader", REAL_READER)
    _patch(monkeypatch, gc, "_mode", lambda: features.MODE_ON)
    _patch(monkeypatch, gc, "foreground_window_title", lambda: "订单页 - Microsoft Edge")

    state = {"title": "订单页", "url": "https://e.com/order", "text": "订单已创建"}
    sess = FakeSession(rect={"x": 10, "y": 20, "w": 5, "h": 5}, shots=[_png((255, 255, 255)), _png((0, 0, 0))], state=state)
    out = _reader(sess).click(
        "#buy",
        window_title="Edge",
        expect="订单已创建",
        evidence_dir=".test_tmp/evidence_click",
        turn_id="turn-9",
        pre_gate=lambda fg, title: tr.can_ui_control(CLICK, foreground_ok=fg, title_ok=title, pixels_changed=gb.MIN_CHANGED_PIXELS),
    )
    mouse = [m for m in sess.sent if m == "Input.dispatchMouseEvent"]
    check("派发三次鼠标事件", len(mouse) == 3, str(sess.sent))
    check("截图两次", len([m for m in sess.sent if m == "Page.captureScreenshot"]) == 2, str(sess.sent))
    check("像素差量到了", out.pixels_changed == 40 * 30, str(out.pixels_changed))
    check("三条判据成立", all(out.criteria().values()), str(out.criteria()))
    check("证据落盘", bool(out.evidence) and Path(out.evidence).exists(), out.evidence)
    check("证据名带 turn_id", "turn-9" in Path(out.evidence).name, out.evidence)
    check("目标出现", out.expect_found is True)
    check("回执含判据", set(out.as_receipt()["criteria"]) == {"foreground", "title", "pixels"}, str(out.as_receipt()))

    # 前台不过：一步都不许动
    _patch(monkeypatch, gc, "foreground_window_title", lambda: "记事本")
    sess2 = FakeSession(rect={"x": 1, "y": 2, "w": 3, "h": 4}, shots=[_png((1, 1, 1))], state=state)
    try:
        _reader(sess2).click(
            "#buy",
            window_title="Edge",
            pre_gate=lambda fg, title: tr.can_ui_control(CLICK, foreground_ok=fg, title_ok=title, pixels_changed=gb.MIN_CHANGED_PIXELS),
        )
        blocked = False
    except gb.GuiError as exc:
        blocked = exc.code == "blocked"
    check("前台不过 → blocked", blocked)
    check("前台不过 → 零鼠标事件", not [m for m in sess2.sent if m == "Input.dispatchMouseEvent"], str(sess2.sent))

    # 页面标题空 → 第二条判据不成立
    _patch(monkeypatch, gc, "foreground_window_title", lambda: "Microsoft Edge")
    sess3 = FakeSession(rect={"x": 1, "y": 2, "w": 3, "h": 4}, shots=[_png((1, 1, 1))], state={"title": "", "url": "", "text": ""})
    blank_page = [dict(PAGES[0], title="")]
    try:
        _reader(sess3, pages=blank_page).click(
            "#buy",
            window_title="Edge",
            pre_gate=lambda fg, title: tr.can_ui_control(CLICK, foreground_ok=fg, title_ok=title, pixels_changed=gb.MIN_CHANGED_PIXELS),
        )
        title_blocked = False
    except gb.GuiError as exc:
        title_blocked = exc.code == "blocked"
    check("页面没标题 → blocked", title_blocked)

    # 选择器选不到
    _patch(monkeypatch, gc, "foreground_window_title", lambda: "订单页 - Microsoft Edge")
    sess4 = FakeSession(rect=None, shots=[_png((1, 1, 1))], state=state)
    try:
        _reader(sess4).click(
            "#nope",
            window_title="Edge",
            pre_gate=lambda fg, title: tr.can_ui_control(CLICK, foreground_ok=fg, title_ok=title, pixels_changed=gb.MIN_CHANGED_PIXELS),
        )
        no_target = False
    except gb.GuiError as exc:
        no_target = exc.code == "no_target"
    check("选不到元素 → no_target", no_target)
    check("选不到 → 零鼠标事件", not [m for m in sess4.sent if m == "Input.dispatchMouseEvent"], str(sess4.sent))

    # 开关关着：连端口都不摸
    _patch(monkeypatch, gc, "_mode", lambda: features.MODE_OFF)
    sess5 = FakeSession(rect={"x": 1, "y": 2, "w": 3, "h": 4}, shots=[_png((1, 1, 1))], state=state)
    try:
        _reader(sess5).click("#buy", window_title="Edge")
        off_blocked = False
    except gb.GuiError as exc:
        off_blocked = exc.code == "blocked"
    check("off 档 → blocked", off_blocked)
    check("off 档 → 零 CDP 命令", sess5.sent == [], str(sess5.sent))


def test_handler(monkeypatch) -> None:
    def motion(**kw):
        base = dict(
            target="P1",
            selector="#buy",
            url_before="https://e.com/",
            url_after="https://e.com/order",
            title_after="订单页",
            state_before="aaa",
            state_after="bbb",
            changed={"url": {"before": "https://e.com/", "after": "https://e.com/order"}},
            foreground_title="订单页 - Microsoft Edge",
            foreground_ok=True,
            title_ok=True,
            pixels_changed=1200,
            evidence="data/evidence/turn-1_1.png",
            expect_found=True,
            at="2026-10-21 13:00:00",
        )
        base.update(kw)
        return gb.ClickResult(**base)

    class GoodReader:
        def __init__(self, *a, **k) -> None:
            pass

        def click(self, selector, **kw):
            return motion()

    _patch(monkeypatch, gc, "CdpReader", GoodReader)
    out = tb.gui_click_handler(params={"selector": "#buy", "turn_id": "turn-1"})
    check("handler ok", out.status == "ok", out.status)
    check("正文有点击对象", "#buy" in out.content, out.content)
    check("正文有像素差", "像素差=1200" in out.content, out.content)
    check("正文有证据", "data/evidence" in out.content, out.content)
    check("证据进回执字段", out.evidence == "data/evidence/turn-1_1.png", out.evidence)
    check("状态差异透传", out.state_before == "aaa" and out.state_after == "bbb", str(out))
    check("changed 透传", "url" in out.changed, str(out.changed))

    class LowPixels:
        def __init__(self, *a, **k) -> None:
            pass

        def click(self, selector, **kw):
            return motion(pixels_changed=3)

    _patch(monkeypatch, gc, "CdpReader", LowPixels)
    out2 = tb.gui_click_handler(params={"selector": "#buy"})
    check("像素不够 → ui_blocked", out2.status == "ui_blocked", out2.status)

    class NoChange:
        def __init__(self, *a, **k) -> None:
            pass

        def click(self, selector, **kw):
            return motion(changed={})

    _patch(monkeypatch, gc, "CdpReader", NoChange)
    out3 = tb.gui_click_handler(params={"selector": "#buy"})
    check("界面没变 → no_change", out3.status == "no_change", out3.status)
    check("界面没变说清楚", "没有变化" in out3.content, out3.content)

    class NotConn:
        def __init__(self, *a, **k) -> None:
            pass

        def click(self, selector, **kw):
            raise gb.GuiError("not_connected", "9222 没开")

    _patch(monkeypatch, gc, "CdpReader", NotConn)
    out4 = tb.gui_click_handler(params={"selector": "#buy"})
    check("没连上 → not_connected", out4.status == "not_connected", out4.status)

    check("默认声明 Edge", tb._click_spec().window_title == "Edge", tb._click_spec().window_title)
    check("可换浏览器（换一行）", tb._click_spec("Chrome").window_title == "Chrome")
    check("没声明就退回默认", tb._click_spec("").window_title == "Edge")


def main() -> int:
    test_spec()
    test_gate()
    test_pieces()
    test_verdict()

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
        test_driver(mp)
        test_handler(mp)
    finally:
        mp.undo()
    print(f"RESULT {FAILED} failed")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
