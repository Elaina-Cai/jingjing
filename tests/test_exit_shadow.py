r"""出口影子档：旧路照发，新出口只 dry_run 走一遍并记一行比对日志。

为什么要有这一档：`LEGACY_VIA_EXIT` 只有 on/off 时，要把「新出口到底能不能发」
验清楚，只能逐站点改代码；影子档让它一次覆盖**所有**走网关的调用点——
旧路真发，新路只走闸门，谁也不许接管传输。

跑了：``.venv_it\Scripts\python.exe -X utf8 -u tests/test_exit_shadow.py``
"""

from __future__ import annotations

import io as _io
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.speech import legacy_bridge as br  # noqa: E402

FAILED: list[str] = []
COUNT = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global COUNT
    COUNT += 1
    if cond:
        print(f"  ok {name}")
    else:
        FAILED.append(f"{name} {detail}")
        print(f"  FAIL {name} {detail}")


class FakeCaps:
    kinds = frozenset({"say"})


class FakeAdapter:
    platform = "wechat"

    def capabilities(self):
        return FakeCaps()


class FakeRegistry:
    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled

    def get(self, name):
        return FakeAdapter()

    def is_enabled(self, name):
        return self.enabled


from core.contracts.delivery import Receipt as _ReceiptBase


class _Receipt(_ReceiptBase):
    """够 `receipt_outcome()` 用就行。传 [status, reason] 可定制。

    2026-11-22：`deliver_via_exit` 现在只认真正的 Receipt（`isinstance` 校验），
    替身必须继承它，否则会被当成「新出口未返回可核对回执」。
    """

    def __init__(self, pair=None) -> None:
        status, reason = pair if pair else ("sent", "")
        super().__init__(status=status, kind="text", reason=reason, platform_msg_id="wx-1")


class Recorder:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.args: list[tuple] = []

    def __call__(self, *a, **kw):
        self.args.append(a)
        self.calls.append(kw)
        return [_Receipt()]


def _src(path: str) -> str:
    return _io.open(path, encoding="utf-8-sig").read()


def t_shadow_dry_run_only(monkeypatch) -> None:
    rec = Recorder()
    fake_deliver = rec

    import core.speech.delivery as dl

    monkeypatch.setattr(br, "legacy_via_exit_mode", lambda: "shadow")
    monkeypatch.setattr(dl, "deliver", fake_deliver)

    got = br.deliver_via_exit("wechat", "张三", "在吗", registry=FakeRegistry())
    check("shadow + 非 dry_run：不接管", got.handled is False, str(got))
    check("shadow + 非 dry_run：一次都不调 deliver", rec.calls == [], str(rec.calls))

    got2 = br.deliver_via_exit(
        "wechat", "张三", "在吗", registry=FakeRegistry(), dry_run=True
    )
    check("shadow + dry_run：走闸门", got2.handled is True, str(got2))
    check("shadow + dry_run：deliver 收到 dry_run=True",
          rec.calls and rec.calls[0].get("dry_run") is True, str(rec.calls))
    items = rec.args[0][0] if rec.args and rec.args[0] else ()
    check("shadow + dry_run：正文原样", "在吗" in str(items), str(items))


def t_off_still_noop(monkeypatch) -> None:
    rec = Recorder()
    import core.speech.delivery as dl

    monkeypatch.setattr(br, "legacy_via_exit_mode", lambda: "off")
    monkeypatch.setattr(dl, "deliver", rec)
    got = br.deliver_via_exit("wechat", "张三", "在吗", registry=FakeRegistry(), dry_run=True)
    check("off 档：连 dry_run 都不走", got.handled is False and rec.calls == [], str(got))


def t_on_still_real(monkeypatch) -> None:
    rec = Recorder()
    import core.speech.delivery as dl

    monkeypatch.setattr(br, "legacy_via_exit_mode", lambda: "on")
    monkeypatch.setattr(dl, "deliver", rec)
    br.deliver_via_exit("wechat", "张三", "在吗", registry=FakeRegistry())
    check("on 档：不带 dry_run（真发）", rec.calls and rec.calls[0].get("dry_run") is False,
          str(rec.calls))


def t_default_registry_is_adapter_registry() -> None:
    """2026-10-21 真机 bug：不传 registry 时取到了工具注册表 → 每条日志都假报 no_adapter。"""
    import core.speech.legacy_bridge as lb

    saved_mode = lb.legacy_via_exit_mode
    lb.legacy_via_exit_mode = lambda: "shadow"
    try:
        got = lb.deliver_via_exit("wechat", "wxid_x", "hi", dry_run=True)
    finally:
        lb.legacy_via_exit_mode = saved_mode
    check("不传 registry 也能找到微信适配器（不再假报 no_adapter）",
          got.reason != "no_adapter", str(got))
    check("不传 registry 时说的是真话：adapter_disabled",
          (got.status, got.reason) == ("blocked", "adapter_disabled"), str(got))

    class Empty:
        def get(self, name):
            return None

    saved_mode2 = lb.legacy_via_exit_mode
    lb.legacy_via_exit_mode = lambda: "shadow"
    try:
        got2 = lb.deliver_via_exit("wechat", "wxid_x", "hi", dry_run=True, registry=Empty())
    finally:
        lb.legacy_via_exit_mode = saved_mode2
    check("真的没有适配器时才报 no_adapter",
          (got2.status, got2.reason) == ("blocked", "no_adapter"), str(got2))


def t_status_is_honest() -> None:
    """影子档必须说清「为什么没走下去」，且状态词要与 l0_glance 的判据一致。"""
    import core.speech.legacy_bridge as lb

    saved_mode = lb.legacy_via_exit_mode
    lb.legacy_via_exit_mode = lambda: "shadow"
    try:
        _status_cases(lb)
    finally:
        lb.legacy_via_exit_mode = saved_mode


def _status_cases(lb) -> None:
    # 1) 平台不认识
    got = lb.deliver_via_exit("telegram", "x", "hi", dry_run=True)
    check("未知平台：blocked/platform_not_bridged",
          (got.status, got.reason) == ("blocked", "platform_not_bridged"), str(got))
    # 2) 认得的平台但适配器没启用 —— 这正是一次真机影子档实际走到的分支
    got2 = lb.deliver_via_exit("wechat", "wxid_x", "hi", dry_run=True, registry=FakeRegistry(False))
    check("适配器未启用：blocked/adapter_disabled",
          (got2.status, got2.reason) == ("blocked", "adapter_disabled"), str(got2))
    check("适配器未启用：绝不接管（旧路照常）", got2.handled is False, str(got2))
    # 3) 启用 + dry_run → 闸门放行，状态取回执原话（skipped/dry_run）
    import core.speech.delivery as delivery_mod
    saved = delivery_mod.deliver
    delivery_mod.deliver = lambda items, **kw: [_Receipt(["skipped", "dry_run"])]
    try:
        got3 = lb.deliver_via_exit("wechat", "wxid_x", "hi", dry_run=True, registry=FakeRegistry(True))
    finally:
        delivery_mod.deliver = saved
    check("走到闸门：状态取回执原话", (got3.status, got3.reason) == ("skipped", "dry_run"), str(got3))
    check("走到闸门：handled=True", got3.handled is True, str(got3))


def t_static_log_shape() -> None:
    body = _src("core/speech/outbound.py")
    check("影子日志先拼好再交（不让 structlog 留下 %s）",
          'f"legacy_exit_shadow platform=' in body and "legacy_exit_shadow platform=%s" not in body)
    check("影子日志带 exit_reason", "exit_reason=" in body)


def t_static() -> None:
    src = _src("core/speech/outbound.py")
    check("网关里有影子日志", "legacy_exit_shadow" in src)
    start = src.index("mode = legacy_via_exit_mode()")
    body = src[start:start + 1800]
    check("影子档只认 on/shadow", 'mode not in ("on", "shadow")' in body)
    check("影子档 dry_run=True", "dry_run=True" in body)
    check("影子档一律 return None（绝不许接管传输）", "return None" in body)
    bridge = _src("core/speech/legacy_bridge.py")
    check("桥也认 dry_run", "dry_run: bool = False" in bridge)
    check("影子档不许真发（非 on 且非 dry_run 就退）",
          'if mode != "on" and not dry_run:' in bridge)
    check("桥默认取适配器注册表（不是工具注册表）",
          "from core.execution.wechat_adapter import default_registry as adapter_registry" in bridge)
    check("桥不再拿 core.jobs.executor 的工具注册表顶替适配器注册表",
          "from core.jobs.executor import default_registry" not in bridge)


class MonkeyPatch:
    def __init__(self) -> None:
        self.undo_list: list[tuple[object, str, object]] = []

    def setattr(self, obj: object, name: str, value: object) -> None:
        self.undo_list.append((obj, name, getattr(obj, name)))
        setattr(obj, name, value)

    def undo(self) -> None:
        for obj, name, old in reversed(self.undo_list):
            setattr(obj, name, old)
        self.undo_list.clear()


def with_patch(fn) -> None:
    mp = MonkeyPatch()
    try:
        fn(mp)
    finally:
        mp.undo()


def main() -> int:
    for fn in (t_shadow_dry_run_only, t_off_still_noop, t_on_still_real):
        with_patch(fn)
    t_static()
    t_status_is_honest()
    t_default_registry_is_adapter_registry()
    t_static_log_shape()
    print()
    print(f"RESULT {len(FAILED)} failed / {COUNT} checks")
    for f in FAILED:
        print("  -", f)
    return 0 if not FAILED else 1


if __name__ == "__main__":
    raise SystemExit(main())
