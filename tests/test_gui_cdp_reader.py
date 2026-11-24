"""GUI 只读驱动（CDP 后端）：读一屏 → `GuiSnapshot`，一次点击都没有。

对应 `docs/decisions/软件适配器.md` §18 与 U-11 切片。
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import features
from core.execution.gui import base as gb
from core.execution.gui import cdp as gc


def _page(url="https://example.com/x", title="订单页", ws="ws://127.0.0.1:9222/devtools/page/1", id_="P1"):
    return {"id": id_, "type": "page", "title": title, "url": url, "webSocketDebuggerUrl": ws}


class FakeSession:
    def __init__(self, payload, *, enter_ok=True):
        self.payload = payload
        self.calls: list[tuple[str, dict]] = []
        self.entered = 0
        self.exited = 0
        self.enter_ok = enter_ok

    def __enter__(self):
        self.entered += 1
        if not self.enter_ok:
            raise RuntimeError("boom")
        return self

    def __exit__(self, *exc):
        self.exited += 1

    def send(self, method, params=None):
        self.calls.append((method, params or {}))
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


def _eval_result(payload: dict) -> dict:
    return {"result": {"type": "string", "value": json.dumps(payload, ensure_ascii=False)}}


def main() -> int:
    failed = 0

    def check(name, cond, detail=""):
        nonlocal failed
        if not cond:
            failed += 1
        print(("PASS " if cond else "FAIL ") + name + ("" if not detail else "  :: " + str(detail)))

    # ---------- 纯函数：normalize_text ----------
    text, truncated = gb.normalize_text("第一行   \n\n\n\n第二行\r\n")
    check("去行尾空白 + 连续空行压成一个", text == "第一行\n\n第二行", repr(text))
    check("没截断就不标 truncated", truncated is False)
    long_text, truncated2 = gb.normalize_text("x" * 50, max_chars=10)
    check("超长按字截断并标记", long_text == "x" * 10 and truncated2 is True, (len(long_text), truncated2))
    check("空正文 → 空串", gb.normalize_text(None) == ("", False))
    check("max_chars 传 0 回落默认上限（不当成「截 0 字」）",
          gb.normalize_text("a" * 5, max_chars=0)[0] == "aaaaa")

    snap = gb.GuiSnapshot(target="P1", url="https://e.com/a", title="订单页", text="内容")
    check("digest 稳定", snap.digest() == gb.GuiSnapshot(target="P1", url="https://e.com/a", title="订单页", text="内容").digest())
    check("正文变了 digest 就变", snap.digest() != gb.GuiSnapshot(target="P1", url="https://e.com/a", title="订单页", text="内容2").digest())
    check("空正文算 is_empty", gb.GuiSnapshot().is_empty() is True)
    check("as_dict 带 digest", snap.as_dict()["digest"] == snap.digest())

    check("错误码是封闭集合", gb.GUI_ERROR_CODES == ("not_connected", "no_target", "timeout", "unsupported", "blocked"))
    err = gb.GuiError("啥也不是", "detail")
    check("未知错误码归 unsupported", err.code == "unsupported", err.code)
    check("GuiError 的字符串带码", str(gb.GuiError("no_target", "x")) == "no_target: x")

    # ---------- list_targets ----------
    def http_ok(pages):
        return lambda url: pages

    pages = [_page(), {"type": "service_worker", "url": "https://x"}, {"type": "page", "url": "file:///c:/x"}]
    got = gc.list_targets(http_get=http_ok(pages))
    check("只要 type=page 且是 http(s)", len(got) == 1 and got[0]["url"].startswith("https://"), got)
    check("ws 地址被带出来", got[0]["ws"].endswith("/devtools/page/1"))

    try:
        gc.list_targets("ws://127.0.0.1:9222", http_get=http_ok(pages))
        check("非 http(s) 端点被挡", False)
    except gb.GuiError as exc:
        check("非 http(s) 端点被挡", exc.code == "blocked", exc.code)

    def http_boom(url):
        raise OSError("connection refused")

    try:
        gc.list_targets(http_get=http_boom)
        check("端口没开 → not_connected", False)
    except gb.GuiError as exc:
        check("端口没开 → not_connected", exc.code == "not_connected", exc.code)

    try:
        gc.list_targets(http_get=lambda url: {"a": 1})
        check("回包不是列表 → not_connected", False)
    except gb.GuiError as exc:
        check("回包不是列表 → not_connected", exc.code == "not_connected")

    # ---------- pick_target ----------
    two = [_page(id_="P1"), _page(url="https://b.com/order", title="第二页", ws="ws2", id_="P2")]
    check("空 target 取第一个", gc.pick_target(two)["id"] == "P1")
    check("数字下标选页", gc.pick_target(two, "1")["id"] == "P2")
    check("URL 子串选页", gc.pick_target(two, "order")["id"] == "P2")
    check("标题子串选页", gc.pick_target(two, "第二页")["id"] == "P2")
    for bad in ("9", "nope"):
        try:
            gc.pick_target(two, bad)
            check(f"选不到 → no_target（{bad}）", False)
        except gb.GuiError as exc:
            check(f"选不到 → no_target（{bad}）", exc.code == "no_target", exc.code)
    try:
        gc.pick_target([])
        check("一个页面都没有 → no_target", False)
    except gb.GuiError as exc:
        check("一个页面都没有 → no_target", exc.code == "no_target")

    # ---------- CdpReader ----------
    calls = {"http": 0}

    def counting_http(url):
        calls["http"] += 1
        return [_page()]

    session = FakeSession(_eval_result({"title": "订单页", "url": "https://e.com/a", "text": "第一行\n\n\n第二行"}))
    reader = gc.CdpReader(
        http_get=counting_http,
        connect=lambda ws: session,
        taken_at=lambda: "2026-10-21 12:00:00",
    )

    saved = gc._mode
    gc._mode = lambda: features.MODE_OFF
    try:
        reader.snapshot()
        check("开关 off → blocked", False)
    except gb.GuiError as exc:
        check("开关 off → blocked", exc.code == "blocked", exc.code)
    check("off 档连端口都不摸", calls["http"] == 0, calls["http"])

    gc._mode = lambda: features.MODE_ON
    snap2 = reader.snapshot()
    check("on 档读得到标题/URL/正文", (snap2.title, snap2.url, snap2.text) == ("订单页", "https://e.com/a", "第一行\n\n第二行"), snap2)
    check("读了 /json 一次", calls["http"] == 1, calls["http"])
    check("会话被打开并关闭", session.entered == 1 and session.exited == 1, (session.entered, session.exited))
    check("只发了一条 Runtime.evaluate", [c[0] for c in session.calls] == ["Runtime.evaluate"], session.calls)
    expr = session.calls[0][1]["expression"]
    check("表达式只读（没有 click / dispatchMouseEvent）",
          "innerText" in expr and "click" not in expr and "dispatchMouseEvent" not in expr, expr)
    check("表达式拿 returnByValue", session.calls[0][1]["returnByValue"] is True)
    check("时间戳来自注入的时钟", snap2.taken_at == "2026-10-21 12:00:00")

    big = gc.CdpReader(
        http_get=counting_http,
        connect=lambda ws: FakeSession(_eval_result({"title": "t", "url": "https://e.com", "text": "y" * 5000})),
    )
    big_snap = big.snapshot(max_chars=100)
    check("超长正文被截断", len(big_snap.text) == 100 and big_snap.truncated is True, (len(big_snap.text), big_snap.truncated))

    no_ws = gc.CdpReader(http_get=lambda url: [_page(ws="")], connect=lambda ws: FakeSession({}))
    try:
        no_ws.snapshot()
        check("页面没有 ws 地址 → not_connected", False)
    except gb.GuiError as exc:
        check("页面没有 ws 地址 → not_connected", exc.code == "not_connected", exc.code)

    exc_snap = gc.CdpReader(
        http_get=counting_http,
        connect=lambda ws: FakeSession({"exceptionDetails": {"text": "ReferenceError"}}),
    )
    try:
        exc_snap.snapshot()
        check("页面里报错 → unsupported", False)
    except gb.GuiError as exc:
        check("页面里报错 → unsupported", exc.code == "unsupported", exc.code)

    bad_json = gc.CdpReader(
        http_get=counting_http,
        connect=lambda ws: FakeSession({"result": {"type": "string", "value": "not json"}}),
    )
    try:
        bad_json.snapshot()
        check("返回值不是 JSON → unsupported", False)
    except gb.GuiError as exc:
        check("返回值不是 JSON → unsupported", exc.code == "unsupported")

    timeout_reader = gc.CdpReader(
        http_get=counting_http,
        connect=lambda ws: FakeSession(TimeoutError("no reply")),
    )
    try:
        timeout_reader.snapshot()
        check("命令没回来 → timeout", False)
    except gb.GuiError as exc:
        check("命令没回来 → timeout", exc.code == "timeout", exc.code)
    gc._mode = saved

    # ---------- 静态纪律 ----------
    src = io.open(ROOT / "core/execution/gui/cdp.py", encoding="utf-8").read()

    def _body(name: str) -> str:
        start = src.index(f"def {name}(")
        end = src.find("\n    def ", start + 1)
        return src[start:end if end > 0 else len(src)]

    _read_path = _body("snapshot") + _body("_read_page")
    for forbidden in ("Input.dispatchMouseEvent", "Input.insertText", "Page.navigate"):
        check(f"读屏路径里没有 {forbidden}", forbidden not in _read_path)
    check("点击才派发鼠标事件", "Input.dispatchMouseEvent" in _body("click"))
    check("打字才输入文字（就在 insert_text 里）", "Input.insertText" in _body("insert_text"))
    check(
        "输入文字只在 insert_text 里",
        "Input.insertText" not in (_body("snapshot") + _body("_read_page") + _body("click") + _body("navigate")),
    )
    check("整个驱动仍不模拟按键", "dispatchKeyEvent" not in src)
    check(
        "只有 navigate 会 Page.navigate",
        "Page.navigate" in _body("navigate") and "Page.navigate" not in _body("click"),
    )
    check("只连本机 http(s)", 'ALLOWED_SCHEMES: tuple[str, ...] = ("http://", "https://")' in src)
    for mod in ("core.speech", "core.routing", "agents", "scheduler", "web"):
        check(f"驱动层不 import {mod}", f"import {mod}" not in src)

    check("开关已注册", "gui_channel" in features.FLAG_REGISTRY)
    check("开关默认 off", features.mode(features.GUI_CHANNEL, features.MODE_OFF) == features.MODE_OFF)
    check("开关是三态", features.FLAG_REGISTRY["gui_channel"].kind == "tri")

    total = 44
    print(f"RESULT {failed} failed / {total - failed} checked")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
