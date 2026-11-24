"""`gui_snapshot`：把只读快照接进工具包（effects=read，不需要任何授权）。

对应 `docs/decisions/工具包.md` §6（失败码）、`docs/decisions/软件适配器.md` §13 与 U-11 切片。
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import features
from core.execution.gui import base as gb
from core.execution.gui import cdp as gc
from core.tools import builtin as bt
from core.tools.registry import STATUS_CODES, Effect, is_known_status


class FakeReader:
    def __init__(self, snap=None, error=None):
        self.snap = snap
        self.error = error
        self.calls: list[tuple[str, int]] = []

    def snapshot(self, target="", *, max_chars=4000):
        self.calls.append((target, max_chars))
        if self.error is not None:
            raise self.error
        return self.snap


def _snap(text="正文", *, title="订单页", url="https://e.com/a", truncated=False):
    return gb.GuiSnapshot(
        target="P1", url=url, title=title, text=text, taken_at="2026-10-21 12:00:00", truncated=truncated
    )


def main() -> int:
    failed = 0

    def check(name, cond, detail=""):
        nonlocal failed
        if not cond:
            failed += 1
        print(("PASS " if cond else "FAIL ") + name + ("" if not detail else "  :: " + str(detail)))

    # ---------- 规格 ----------
    spec = next((s for s in bt.read_specs() if s.name == "gui_snapshot"), None)
    check("登记了 gui_snapshot", spec is not None)
    check("族是 gui", spec is not None and spec.family == "gui", getattr(spec, "family", None))
    check("effects 只有 read（不需要授权）",
          spec is not None and spec.effects == frozenset({Effect.READ}), getattr(spec, "effects", None))
    check("只有一个参数（≤4 个）", spec is not None and list(spec.args) == ["target"], getattr(spec, "args", None))
    check("超时 8s / 上限 4000 字",
          spec is not None and spec.limits == {"timeout_s": 8, "max_chars": 4000}, getattr(spec, "limits", None))
    check("幂等（读没有副作用）", spec is not None and spec.idempotent is True)
    names = [s.name for s in bt.all_specs()]
    check("工具名唯一", len(names) == len(set(names)), names)

    # ---------- 装上就可用（默认启用） ----------
    registry = bt.build_registry()
    check("注册表里有它", registry.get("gui_snapshot") is not None)
    ok, reason = registry.check_call("gui_snapshot", None, turn_id="t0")
    check("read 默认启用", ok is True, reason)

    # ---------- 开关 off：ui_blocked，且连端口都不摸 ----------
    real_reader = gc.CdpReader
    real_mode = gc._mode
    real_http = gc._http_json

    def boom(*a, **k):
        raise AssertionError("off 档不许碰调试端口")

    gc._mode = lambda: features.MODE_OFF
    gc._http_json = boom
    res = registry.observe("", "", {"tool": "gui_snapshot", "turn_id": "t1"})
    check("开关 off → ui_blocked（不是 not_connected）", res.status == "ui_blocked", res.status)
    check("off 档没有去连端口（上面那条没抛 AssertionError）", res.content == "", res.content)
    check("off 档 content 为空", res.content == "")

    # ---------- 开关 on：真的读 ----------
    gc._mode = lambda: features.MODE_ON
    gc._http_json = real_http
    fake = FakeReader(_snap())
    gc.CdpReader = lambda **kw: fake
    res2 = registry.observe("", "", {"tool": "gui_snapshot", "turn_id": "t2"})
    check("on 档读成功", res2.status == "ok", res2.status)
    check("正文里带标题与地址", "订单页" in res2.content and "https://e.com/a" in res2.content, res2.content)
    check("正文带上正文本身", "正文" in res2.content, res2.content)
    check("source 是 URL", res2.source == "https://e.com/a", res2.source)
    check("没截断就不标 truncated", res2.truncated is False)
    check("at 用驱动给的时间", res2.at == "2026-10-21 12:00:00", res2.at)

    registry.observe("", "", {"tool": "gui_snapshot", "turn_id": "t3", "target": "第二页"})
    check("params.target 会被用上", fake.calls[-1][0] == "第二页", fake.calls[-1][0])
    res4 = registry.observe("会话 id", "", {"tool": "gui_snapshot", "turn_id": "t4", "target": "第二页"})
    check("params.target 优先于调用方传的 target", fake.calls[-1][0] == "第二页", fake.calls[-1][0])
    check(
        "为什么反过来：执行器的 target 是会话 id，不是读哪个窗口",
        not (res4.status == "ok" and "会话 id" in res4.source),
        res4.source,
    )
    registry.observe("", "第四页", {"tool": "gui_snapshot", "turn_id": "t5"})
    check("what 也能当 target（判可能只说一句话）", fake.calls[-1][0] == "第四页", fake.calls[-1][0])
    registry.observe("", "", {"tool": "gui_snapshot", "turn_id": "t6", "max_chars": 120})
    check("max_chars 透传给驱动", fake.calls[-1][1] == 120, fake.calls[-1][1])

    long_fake = FakeReader(_snap(text="y" * 4000, truncated=True))
    gc.CdpReader = lambda **kw: long_fake
    res5 = registry.observe("", "", {"tool": "gui_snapshot", "turn_id": "t7"})
    check("超长正文标 truncated", res5.truncated is True)
    check("正文段不超过上限", len(res5.content.split("\n", 1)[1]) <= 4000, len(res5.content))

    empty_fake = FakeReader(_snap(text=""))
    gc.CdpReader = lambda **kw: empty_fake
    res6 = registry.observe("", "", {"tool": "gui_snapshot", "turn_id": "t8"})
    check("读到页面但正文空 → no_result（不是 ok）", res6.status == "no_result", res6.status)
    check("no_result 时仍给出标题与地址", "订单页" in res6.content and "https://e.com/a" in res6.content, res6.content)

    # ---------- 错误码映射 ----------
    pairs = [
        ("blocked", "ui_blocked"),
        ("not_connected", "not_connected"),
        ("no_target", "no_target"),
        ("timeout", "timeout"),
        ("unsupported", "unsupported_type"),
    ]
    for code, want in pairs:
        def _reader(code=code):
            return lambda **kw: FakeReader(error=gb.GuiError(code, "x"))

        gc.CdpReader = _reader()
        got = registry.observe("", "", {"tool": "gui_snapshot", "turn_id": "e-" + code}).status
        check(f"{code} → {want}", got == want, got)
        check(f"{want} 是登记过的状态码", is_known_status(want), want)

    gc.CdpReader = lambda **kw: FakeReader(error=RuntimeError("驱动炸了"))
    res_err = registry.observe("", "", {"tool": "gui_snapshot", "turn_id": "boom"})
    check("驱动抛别的异常 → unsupported_type，不冒泡", res_err.status == "unsupported_type", res_err.status)

    # ---------- 计数：失败也算一次 ----------
    before = registry.calls_in_turn("count-me")
    registry.observe("", "", {"tool": "gui_snapshot", "turn_id": "count-me"})
    registry.observe("", "", {"tool": "gui_snapshot", "turn_id": "count-me"})
    check("每次调用都计数（含失败）", registry.calls_in_turn("count-me") - before == 2,
          registry.calls_in_turn("count-me") - before)

    gc.CdpReader = real_reader
    gc._mode = real_mode

    # ---------- 静态纪律 ----------
    src = io.open(ROOT / "core/tools/builtin.py", encoding="utf-8").read()
    reg_src = io.open(ROOT / "core/tools/registry.py", encoding="utf-8").read()
    check("external_send 只能走出口（注册表里写着理由）", "external_send_via_exit" in reg_src)
    check("gui_snapshot 的 effects 不含 external_send", Effect.EXTERNAL_SEND not in spec.effects)
    # U-21 起驱动不再由工具直接 new，而是走 backend 注册表；纪律不变：
    # 仍然是**函数体内**延迟导入，不把 GUI 依赖带进工具包顶层。
    check("驱动是延迟导入（不把 GUI 依赖带进工具包顶层）",
          "    from core.execution.gui import backends" in src)
    check("映射表写死在代码里（封闭集合）", "GUI_STATUS_MAP" in src)
    check("新状态码进了封闭集合", {"not_connected", "no_target"} <= set(STATUS_CODES))

    total = 41
    print(f"RESULT {failed} failed / {total - failed} checked")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
