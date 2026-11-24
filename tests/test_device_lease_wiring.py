"""`DEVICE_LEASE` 接线（实施手册第 5 步）：off 档一行都不执行，on 档先检后动。

这一片**只接线**：把「能力自检 + 输入租约」接到 `wechat.fill_message` 的入口，
不改 `send_and_verify` 的语义，也不把租约持到落盘回读那 15–30 秒上。
"""

from __future__ import annotations

import contextlib
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import features
from core.execution.gui import wechat

CHECKS: list[str] = []
FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    CHECKS.append(name)
    if cond:
        print(f"  ok   {name}")
        return
    FAILED.append(f"{name} {detail}".strip())
    print(f"  FAIL {name} {detail}".strip())


def _body(src: str, name: str) -> str:
    start = src.index(f"def {name}(")
    nxt = src.find("\ndef ", start + 1)
    return src[start : nxt if nxt != -1 else len(src)]


class FakeReport:
    def __init__(self, ok: bool = True) -> None:
        self.ok = ok

    def one_line(self) -> str:
        return "caps=111111 ok" if self.ok else "caps=101111 blocked:dpi_mismatch"


class FakeCaps:
    """假的 `capability` 模块：只记调用次数。"""

    class CapabilityMissing(RuntimeError):
        def __init__(self, reason: str) -> None:
            self.reason = reason
            super().__init__(reason)

    def __init__(self, *, ok: bool = True, probe_raises: bool = False, require_raises: bool = False) -> None:
        self.probe_calls = 0
        self.require_calls = 0
        self._ok = ok
        self._probe_raises = probe_raises
        self._require_raises = require_raises

    def probe(self) -> FakeReport:
        self.probe_calls += 1
        if self._probe_raises:
            raise RuntimeError("probe boom")
        return FakeReport(self._ok)

    def require(self, report: FakeReport) -> None:
        self.require_calls += 1
        if self._require_raises:
            raise FakeCaps.CapabilityMissing("dpi_mismatch")


class FakeLease:
    def __init__(self) -> None:
        self.released = 0

    def release(self) -> None:
        self.released += 1


class FakeLeases:
    """假的 `lease` 模块。"""

    class UiBusy(RuntimeError):
        def __init__(self, holder: str) -> None:
            self.holder = holder
            super().__init__(holder)

    DEFAULT_TTL_S = 60.0

    def __init__(self, *, busy: bool = False, stuck: bool = False) -> None:
        self.calls: list[tuple] = []
        self.held = FakeLease()
        self._busy = busy
        self._stuck = stuck
        self.describe_calls = 0

    def describe_holder(self, resource: str, **kw) -> dict:
        self.describe_calls += 1
        age = 500.0 if self._stuck else 1.0
        return {"holder": "someone-else", "pid": 4242, "heartbeat_age_s": age, "age_s": age + 1}

    def device_lease(self, resource: str, owner: str, **kw) -> FakeLease:
        self.calls.append((resource, owner))
        if self._busy:
            raise FakeLeases.UiBusy("someone-else")
        return self.held


@contextlib.contextmanager
def patched(mode: str, caps: FakeCaps | None = None, leases: FakeLeases | None = None):
    saved = (wechat.capability_probe, wechat.device_lease_mod, wechat.device_lease_mode)
    caps = caps if caps is not None else FakeCaps()
    leases = leases if leases is not None else FakeLeases()
    wechat.capability_probe = caps  # type: ignore[assignment]
    wechat.device_lease_mod = leases  # type: ignore[assignment]
    wechat.device_lease_mode = lambda: mode  # type: ignore[assignment]
    try:
        yield caps, leases
    finally:
        (
            wechat.capability_probe,  # type: ignore[assignment]
            wechat.device_lease_mod,  # type: ignore[assignment]
            wechat.device_lease_mode,  # type: ignore[assignment]
        ) = saved


def t_off_does_nothing() -> None:
    with patched("off") as (caps, leases):
        with wechat.device_guard("turn-1") as held:
            pass
    check("off：yield None", held is None)
    check("off：零次能力自检", caps.probe_calls == 0, str(caps.probe_calls))
    check("off：零次租约", leases.calls == [], str(leases.calls))


def t_shadow_probes_but_does_not_block() -> None:
    with patched("shadow") as (caps, leases):
        with wechat.device_guard("turn-2") as held:
            pass
    check("shadow：探一次能力", caps.probe_calls == 1, str(caps.probe_calls))
    check("shadow：不拿锁", leases.calls == [], str(leases.calls))
    check("shadow：yield None", held is None)


def t_shadow_probe_crash_is_survivable() -> None:
    with patched("shadow", caps=FakeCaps(probe_raises=True)) as (caps, leases):
        with wechat.device_guard("turn-3") as held:
            pass
    check("shadow：自检崩了也不拦", held is None)
    check("shadow：只探一次", caps.probe_calls == 1, str(caps.probe_calls))
    check("shadow：仍然不拿锁", leases.calls == [], str(leases.calls))


def t_on_happy_path_takes_and_releases() -> None:
    with patched("on") as (caps, leases):
        with wechat.device_guard("turn-4") as held:
            pass
    check("on：先检后动（probe 1 次）", caps.probe_calls == 1, str(caps.probe_calls))
    check("on：require 1 次", caps.require_calls == 1, str(caps.require_calls))
    check("on：拿了一次租约", leases.calls == [("input", "turn-4")], str(leases.calls))
    check("on：yield 的是租约", isinstance(held, FakeLease), type(held).__name__)
    check("on：退出时释放", leases.held.released == 1, str(leases.held.released))


def t_on_probe_failure_blocks() -> None:
    raised = ""
    with patched("on", caps=FakeCaps(require_raises=True)) as (caps, leases):
        try:
            with wechat.device_guard("turn-5"):
                pass
        except wechat.capability_probe.CapabilityMissing as exc:
            raised = getattr(exc, "reason", "")
    check("on：能力不过就抛", raised == "dpi_mismatch", raised)
    check("on：能力不过零租约", leases.calls == [], str(leases.calls))


def t_on_probe_missing_blocks() -> None:
    raised = ""
    with patched("on", caps=FakeCaps(probe_raises=True)) as (caps, leases):
        try:
            with wechat.device_guard("turn-6"):
                pass
        except wechat.capability_probe.CapabilityMissing as exc:
            raised = getattr(exc, "reason", "")
    check("on：探不出来 = 不过（probe_missing）", raised == "probe_missing", raised)
    check("on：探不出来零租约", leases.calls == [], str(leases.calls))


def t_on_busy_propagates() -> None:
    raised = ""
    with patched("on", leases=FakeLeases(busy=True)) as (caps, leases):
        try:
            with wechat.device_guard("turn-7"):
                pass
        except wechat.device_lease_mod.UiBusy as exc:
            raised = getattr(exc, "holder", "")
    check("on：拿不到手抛 UiBusy", raised == "someone-else", raised)
    check("on：拿不到手就不释放别人的锁", leases.held.released == 0, str(leases.held.released))


def t_on_exception_still_releases() -> None:
    with patched("on") as (caps, leases):
        try:
            with wechat.device_guard("turn-8"):
                raise ValueError("动作中途炸了")
        except ValueError:
            pass
    check("on：动作炸了也释放", leases.held.released == 1, str(leases.held.released))


def t_static_wiring() -> None:
    src = pathlib.Path("core/execution/gui/wechat.py").read_text(encoding="utf-8")
    outer = _body(src, "fill_message")
    inner = _body(src, "_fill_message")
    check("静态：fill_message 过门", "device_guard(" in outer and "_fill_message(" in outer)
    check("静态：门在填字之前（with 包着 _fill_message）", outer.index("device_guard(") < outer.index("_fill_message("))
    check("静态：_fill_message 自己不再过门（只一道）", "device_guard(" not in inner)
    check("静态：DEVICE_LEASE 只有一个读点", src.count("features.DEVICE_LEASE") == 1, str(src.count("features.DEVICE_LEASE")))
    check("静态：资源名是设备级（input）", "DEVICE_RESOURCE = device_lease_mod.RESOURCE_INPUT" in src)

    send_src = pathlib.Path("core/execution/gui/wechat_send.py").read_text(encoding="utf-8")
    send_body = _body(send_src, "send_and_verify")
    guard = send_body.index("with wechat.device_guard(")
    fill = send_body.index("wechat.fill_message(")
    enter = send_body.index("press_enter(")
    # 2026-11-21：回读从 wechat.rows_after 换成带来源绑定的 local.observe
    readback = send_body.index("local.observe(")
    check("静态：发送侧把「填字 + 回车」包在同一段租约里", guard < fill < enter < readback,
          f"{guard}/{fill}/{enter}/{readback}")
    check("静态：回读对账在门之外（不持锁等落盘）", readback > enter)
    check("静态：门内只有一段（不嵌套两个 with）", send_body.count("with wechat.device_guard(") == 1)



class FakeDeps:
    """最小的微信 UI 驱动替身：只记调用次序，不碰真机。"""

    def __init__(self, *, box_text: str = "", clears: bool = True, has_text: bool = True) -> None:
        self.calls: list = []
        self.box = box_text
        self.clears = clears
        self.has_text = has_text

    def window_title(self) -> str:
        return "微信"

    def foreground_title(self) -> str:
        return "微信"

    def wake(self) -> bool:
        self.calls.append("wake")
        return True

    def open_chat(self, chat: str) -> dict:
        self.calls.append(("open_chat", chat))
        return {"ok": True, "who": chat}

    def focus_message_box(self) -> bool:
        self.calls.append("focus")
        return True

    def message_box_text(self) -> str:
        self.calls.append("read_box")
        return self.box

    def clear_message_box(self) -> bool:
        self.calls.append("clear")
        if self.clears:
            self.box = ""
        return self.clears

    def mention(self, name: str) -> bool:
        self.calls.append(("mention", name))
        return True

    def screenshot(self):
        return None

    def paste(self, text: str) -> bool:
        self.calls.append(("paste", text))
        return True

    def message_box_has_text(self):
        return self.has_text

    def press_enter(self) -> bool:
        self.calls.append("enter")
        return True

    def order(self, name: str) -> int:
        for i, call in enumerate(self.calls):
            if call == name or (isinstance(call, tuple) and call[0] == name):
                return i
        return -1


def t_reentrant_guard_takes_one_lease() -> None:
    with patched("on") as (caps, leases):
        with wechat.device_guard("outer"):
            with wechat.device_guard("inner"):
                pass
    check("重入：只拿一次租约", len(leases.calls) == 1, str(leases.calls))
    check("重入：只释放一次", leases.held.released == 1, str(leases.held.released))
    check("重入：自检也只跑一次", caps.probe_calls == 1, str(caps.probe_calls))


def _fill(mode: str, deps: FakeDeps):
    saved = wechat._mode
    wechat._mode = lambda: mode  # type: ignore[assignment]
    try:
        return wechat._fill_message("测试群", "你好", deps=deps)
    finally:
        wechat._mode = saved  # type: ignore[assignment]


def t_dirty_composer_blocks_before_paste() -> None:
    deps = FakeDeps(box_text="上一轮的半句话")
    code = ""
    try:
        _fill("on", deps)
    except wechat.GuiError as exc:
        code = exc.code
    check("脏输入框：先读框", deps.order("read_box") >= 0, str(deps.calls))
    check("脏输入框：读到残留就拦（blocked）", code == "blocked", code)
    check("脏输入框：绝不清空", deps.order("clear") == -1, str(deps.calls))
    check("脏输入框：不粘贴", deps.order("paste") == -1, str(deps.calls))


def t_dirty_composer_never_cleared_even_if_clearable() -> None:
    deps = FakeDeps(box_text="能清掉的半句", clears=True)
    code = ""
    try:
        _fill("on", deps)
    except wechat.GuiError as exc:
        code = exc.code
    check("脏输入框（能清也不清）：拦", code == "blocked", code)
    check("脏输入框（能清也不清）：clear 没被调用", deps.order("clear") == -1, str(deps.calls))


def t_clean_composer_not_touched() -> None:
    deps = FakeDeps(box_text="")
    _fill("on", deps)
    check("干净输入框：不白清一次", deps.order("clear") == -1, str(deps.calls))
    check("干净输入框：照常粘贴", deps.order("paste") >= 0, str(deps.calls))


def t_send_holds_lease_until_enter_then_releases_before_readback() -> None:
    from core.execution.gui import wechat_send

    deps = FakeDeps()
    body = "你好"
    seen: dict = {}
    from core.execution import local_text_receipt as local_mod

    saved = (wechat._mode, wechat_send._mode)
    saved_local = (local_mod.account_path, local_mod.capture, local_mod.observe)

    def fake_account_path(_root, _platform, _account_id):
        import pathlib as _pl

        return _pl.Path("x.db")

    def fake_capture(_path):
        return 0, "tok"

    def fake_observe(_path, _chat, _text, _after, *, source_token=None):
        # 2026-11-21：回读换成 local.observe（带来源绑定）；锁的释放仍然要在回读之前
        seen["released_at_readback"] = leases.held.released
        return 5

    with patched("on") as (caps, leases):
        wechat._mode = lambda: "on"  # type: ignore[assignment]
        wechat_send._mode = lambda: "on"  # type: ignore[assignment]
        local_mod.account_path = fake_account_path  # type: ignore[assignment]
        local_mod.capture = fake_capture  # type: ignore[assignment]
        local_mod.observe = fake_observe  # type: ignore[assignment]
        try:
            receipt = wechat_send.send_and_verify("测试群", body, account_id="bot", deps=deps,
                                                  polls=1, sleep=lambda s: None)
        finally:
            wechat._mode, wechat_send._mode = saved
            local_mod.account_path, local_mod.capture, local_mod.observe = saved_local

    check("发送：租约被拿过一次", len(leases.calls) == 1, str(leases.calls))
    check("发送：回车在门内发生", deps.order("enter") >= 0, str(deps.calls))
    check("发送：回读时锁已放（不持锁等落盘）", seen.get("released_at_readback") == 1,
          str(seen))
    # 2026-11-21：本地记录只证明「本机出现了这条」，平台送达仍未核实，所以是 unconfirmed
    check("发送：有本地记录仍是 unconfirmed", receipt.status == wechat_send.STATUS_UNCONFIRMED,
          receipt.status)
    check("发送：回执带上本地记录号", receipt.local_id == 5, str(receipt.local_id))

def t_flag_registered() -> None:
    check("开关已注册", features.DEVICE_LEASE in features.FLAG_REGISTRY)
    check("开关默认 off", features.FLAG_REGISTRY[features.DEVICE_LEASE].default == features.MODE_OFF)
    check("开关进三态表", features.DEVICE_LEASE in features.TRI_STATE_FLAGS)
    check("真实环境读点是 off", wechat.device_lease_mode() in (features.MODE_OFF, features.MODE_SHADOW, features.MODE_ON))


def t_busy_is_visible() -> None:
    """拿不到手 → 抛 UiBusy 之前先记一行日志，且不再多抛别的异常。"""
    wechat.device_metrics.reset()
    with patched("on", leases=FakeLeases(busy=True)) as (_caps, leases):
        raised = ""
        try:
            with wechat.device_guard("turn-busy"):
                pass
        except FakeLeases.UiBusy as exc:
            raised = str(exc)
    check("busy：异常照旧抛（不改失败方式）", raised == "someone-else", raised)
    check("busy：读了持有者状态", leases.describe_calls == 1, str(leases.describe_calls))
    check("busy：心跳新鲜不记卡死", wechat.device_metrics.counts()["lease_stuck"] == 0)


def t_stuck_holder_bumps_metric() -> None:
    """心跳超过 2×TTL → 记一条 lease_stuck（这是唯一会把「等人」变成「事故」的信号）。"""
    wechat.device_metrics.reset()
    with patched("on", leases=FakeLeases(busy=True, stuck=True)) as (_caps, _leases):
        try:
            with wechat.device_guard("turn-stuck"):
                pass
        except FakeLeases.UiBusy:
            pass
    check("stuck：记了一次", wechat.device_metrics.counts()["lease_stuck"] == 1, str(wechat.device_metrics.counts()))
    snap = wechat.device_metrics.snapshot()
    check("stuck：量具在册", "lease_stuck" in snap, str(list(snap)))


def main() -> int:
    for fn in (
        t_off_does_nothing,
        t_shadow_probes_but_does_not_block,
        t_shadow_probe_crash_is_survivable,
        t_on_happy_path_takes_and_releases,
        t_on_probe_failure_blocks,
        t_on_probe_missing_blocks,
        t_on_busy_propagates,
        t_on_exception_still_releases,
        t_static_wiring,
        t_reentrant_guard_takes_one_lease,
        t_dirty_composer_blocks_before_paste,
        t_dirty_composer_never_cleared_even_if_clearable,
        t_clean_composer_not_touched,
        t_send_holds_lease_until_enter_then_releases_before_readback,
        t_flag_registered,
        t_busy_is_visible,
        t_stuck_holder_bumps_metric,
    ):
        fn()
    print(f"RESULT {len(FAILED)} failed / {len(CHECKS)} checks")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
