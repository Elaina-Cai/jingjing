# -*- coding: utf-8 -*-
"""在网页里打字（`软件适配器.md` §13.19）：判据是**输入框的值变了没有**，不是像素差。

它证明五件事：① `gui_input` 默认不可调；② 前两关（前台/标题）动手之前判，不过就一步不动；
③ 元素找不到 / 聚焦失败 → `no_target`，一个字都不输；④ 值没变就是 `no_change`；
⑤ `Input.insertText` 只出现在 `insert_text` 里（读屏路径一个输入事件都没有）。
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
from core.tools import builtin as tb  # noqa: E402
from core.tools import prompt as tp  # noqa: E402
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
INPUT = SPECS["ui"]

PAGES = [
    {
        "id": "P1",
        "type": "page",
        "title": "搜索",
        "url": "https://e.com/",
        "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/P1",
    }
]


class FakeSession:
    """假 CDP：记得住输入框的值，`Input.insertText` 会改它。"""

    def __init__(self, *, value="", state=None, focus_ok=True, exists=True, insert_works=True) -> None:
        self.value = value
        self.state = state or {"title": "搜索", "url": "https://e.com/", "text": "搜索页"}
        self.focus_ok = focus_ok
        self.exists = exists
        self.insert_works = insert_works
        self.sent: list[str] = []
        self.inserted: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def send(self, method, params=None):
        self.sent.append(method)
        if method == "Input.insertText":
            text = str((params or {}).get("text") or "")
            self.inserted.append(text)
            if self.insert_works:
                self.value = text
            return {}
        if method == "Runtime.evaluate":
            expr = str((params or {}).get("expression") or "")
            if "activeElement" in expr:
                return {"result": {"value": bool(self.focus_ok)}}
            if "typeof e.value" in expr:
                if not self.exists:
                    return {"result": {"value": None}}
                return {"result": {"value": {"value": self.value, "tag": "INPUT"}}}
            if "innerText" in expr:
                return {"result": {"value": json.dumps(self.state)}}
            return {"result": {"value": None}}
        return {}


def _reader(sess) -> object:
    return REAL_READER(connect=lambda ws: sess, http_get=lambda url: PAGES, taken_at=lambda: "2026-10-21 14:00:00")


def _patch(monkeypatch, obj, name, value) -> None:
    monkeypatch.setattr(obj, name, value)


def test_spec() -> None:
    check("规格存在", "ui" in SPECS)
    check("effects 只有 ui_control", INPUT.effects == frozenset({tr.Effect.UI_CONTROL}), str(INPUT.effects))
    check("不是幂等的", INPUT.idempotent is False)
    check("参数是 action/target/what/text", set(INPUT.args) == {"action", "target", "what", "text"}, str(INPUT.args))
    check("声明了窗口标题", bool(INPUT.window_title.strip()), INPUT.window_title)
    check("超时 20 秒", INPUT.limits.get("timeout_s") == 20, str(INPUT.limits))
    check(
        "归 ui 组（三个都在）",
        [s.name for s in tb.ui_specs()] == ["ui_state", "ui"],
        str([s.name for s in tb.ui_specs()]),
    )
    check("换浏览器 = 换一行", tb._input_spec("Chrome").window_title == "Chrome")


def test_gate() -> None:
    off = tb.build_registry()
    ok, reason = off.check_call("ui", {"target": "wx", "what": "x"}, turn_id="t")
    check("默认不可调", ok is False and reason == "not_enabled", str((ok, reason)))
    on = tb.build_registry(enable_ui_control=True)
    check("人开了才可调", on.check_call("ui", {"target": "wx", "what": "x"}, turn_id="t")[0] is True)
    check("默认注册表不进 prompt", not any(ln.startswith("- ui ") for ln in tp.build_tool_block(off).splitlines()))
    check("开了才进 prompt", "ui" in tp.build_tool_block(on))


def test_check_text() -> None:
    check("原样返回（不抹空白）", gb.check_text("  hi  ") == "  hi  ")
    check("换行允许", gb.check_text("a\nb") == "a\nb")
    check("正好 200 字可以", len(gb.check_text("字" * 200)) == 200)
    for bad, why in (("", "空"), ("   ", "全空白"), ("x" * 201, "太长"), ("a\tb", "TAB"), ("a\rb", "CR"), ("a\x00b", "NUL")):
        try:
            gb.check_text(bad)
            ok = False
        except gb.GuiError:
            ok = True
        check(f"拒绝 {why}", ok)


def test_verdict() -> None:
    ok, reason = tr.can_ui_control(INPUT, foreground_ok=True, title_ok=True, pixels_changed=0, value_changed=True)
    check("值变了就放行（像素不参与）", ok is True and reason == "", str((ok, reason)))
    ok2, reason2 = tr.can_ui_control(INPUT, foreground_ok=True, title_ok=True, pixels_changed=9999, value_changed=False)
    check("值没变不许", ok2 is False and reason2 == tr.UI_BLOCKED, str((ok2, reason2)))
    ok3, _ = tr.can_ui_control(INPUT, foreground_ok=False, title_ok=True, pixels_changed=0, value_changed=True)
    check("不在前台不许", ok3 is False)
    ok4, _ = tr.can_ui_control(INPUT, foreground_ok=True, title_ok=True, pixels_changed=999)
    check("不给 value_changed 时仍按像素判", ok4 is True)
    from dataclasses import replace

    no_title = replace(INPUT, window_title="")
    ok5, reason5 = tr.can_ui_control(no_title, foreground_ok=True, title_ok=True, pixels_changed=0, value_changed=True)
    check("没声明标题一律不许", ok5 is False and reason5 == tr.UI_BLOCKED, str((ok5, reason5)))


def test_driver(monkeypatch) -> None:
    _patch(monkeypatch, gc, "_mode", lambda: features.MODE_ON)
    _patch(monkeypatch, gc, "foreground_window_title", lambda: "搜索 - Microsoft Edge")

    sess = FakeSession(value="")
    out = _reader(sess).insert_text("#q", "杭州 机票", window_title="Edge")
    check("真的发了一次 insertText", sess.inserted == ["杭州 机票"], str(sess.inserted))
    check("值确实进去了", out.value_after == "杭州 机票", out.value_after)
    check("changed 里有长度与 sha256", set(out.changed) == {"value_sha256", "value_len"}, str(out.changed))
    check("回执口径是值", out.criteria()["value"] is True, str(out.criteria()))
    check("回执里不写正文（只给长度）", "杭州" not in json.dumps(out.as_receipt(), ensure_ascii=False))

    # 前台不对：一步不动
    _patch(monkeypatch, gc, "foreground_window_title", lambda: "记事本")
    sess2 = FakeSession(value="")
    try:
        _reader(sess2).insert_text(
            "#q",
            "x",
            window_title="Edge",
            pre_gate=lambda fg, title: tr.can_ui_control(
                INPUT, foreground_ok=fg, title_ok=title, pixels_changed=tr.UI_MIN_PIXELS_CHANGED
            ),
        )
        blocked = False
    except gb.GuiError as exc:
        blocked = exc.code == "blocked"
    check("前台不对 → blocked", blocked)
    check("前台不对 → 一个字都没输", sess2.inserted == [], str(sess2.inserted))

    # 元素不存在
    _patch(monkeypatch, gc, "foreground_window_title", lambda: "搜索 - Microsoft Edge")
    sess3 = FakeSession(exists=False)
    try:
        _reader(sess3).insert_text("#q", "x", window_title="Edge")
        raised = ""
    except gb.GuiError as exc:
        raised = exc.code
    check("元素找不到 → no_target", raised == "no_target", raised)
    check("元素找不到 → 没输字", sess3.inserted == [], str(sess3.inserted))

    # 聚焦失败
    sess4 = FakeSession(focus_ok=False)
    try:
        _reader(sess4).insert_text("#q", "x", window_title="Edge")
        raised4 = ""
    except gb.GuiError as exc:
        raised4 = exc.code
    check("聚焦失败 → no_target", raised4 == "no_target", raised4)
    check("聚焦失败 → 没输字", sess4.inserted == [], str(sess4.inserted))

    # 输了但页面没吃进去 → changed 空
    sess5 = FakeSession(value="", insert_works=False)
    out5 = _reader(sess5).insert_text("#q", "x", window_title="Edge")
    check("页面没吃进去 → changed 空（交给 no_change）", out5.is_changed() is False, str(out5.changed))


def test_handler(monkeypatch) -> None:
    _patch(monkeypatch, gc, "_mode", lambda: features.MODE_ON)
    _patch(monkeypatch, gc, "foreground_window_title", lambda: "搜索 - Microsoft Edge")

    sess = FakeSession(value="")
    _patch(monkeypatch, gc, "CdpReader", lambda: _reader(sess))
    res = tb.gui_input_handler(params={"selector": "#q", "text": "杭州", "turn_id": "t1"})
    check("handler 成功 → ok", res.status == "ok", res.status)
    check("handler 带回状态差异", bool(res.state_before) and bool(res.state_after) and bool(res.changed), str(res.changed))
    check("正文里写判据", "前台=过" in res.content and "标题=过" in res.content, res.content)

    sess2 = FakeSession(value="杭州", insert_works=False)
    _patch(monkeypatch, gc, "CdpReader", lambda: _reader(sess2))
    res2 = tb.gui_input_handler(params={"selector": "#q", "text": "杭州"})
    check("值没变 → no_change", res2.status == "no_change", res2.status)

    _patch(monkeypatch, gc, "foreground_window_title", lambda: "记事本")
    sess3 = FakeSession(value="")
    _patch(monkeypatch, gc, "CdpReader", lambda: _reader(sess3))
    res3 = tb.gui_input_handler(params={"selector": "#q", "text": "杭州"})
    check("前台不对 → ui_blocked", res3.status == "ui_blocked", res3.status)

    _patch(monkeypatch, gc, "CdpReader", REAL_READER)
    _patch(monkeypatch, gc, "_mode", lambda: features.MODE_OFF)
    from unittest.mock import patch as _p

    with _p.object(gc, "CdpReader", lambda: _reader(FakeSession())):
        res4 = tb.gui_input_handler(params={"selector": "#q", "text": "x"})
    check("开关 off → ui_blocked（不是 not_connected）", res4.status == "ui_blocked", res4.status)


def test_static() -> None:
    src = Path("core/execution/gui/cdp.py").read_text(encoding="utf-8")
    head = src.index("    def insert_text(")
    tail = src.find("\n    def ", head + 10)
    tail = len(src) if tail < 0 else tail
    body = src[head:tail]
    check("insertText 在 insert_text 里", "Input.insertText" in body)
    outside = src[:head] + src[tail:]
    check("读屏路径没有 insertText", "Input.insertText" not in outside)
    check("没有 dispatchKeyEvent（不模拟按键）", "dispatchKeyEvent" not in src)
    check("没有剪贴板", "clipboard" not in src.lower())


def main() -> int:
    test_spec()
    test_gate()
    test_check_text()
    test_verdict()
    test_static()

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
