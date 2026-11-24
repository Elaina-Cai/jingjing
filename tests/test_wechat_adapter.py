"""微信适配器（`core/execution/wechat_adapter.py`）：把「真发一条」包成出口的形状。

钉住六件事：
1. 能力自报：只有 `say`、targets 不限制、需要账号态、**注册不等于启用**；
2. `send()` 四态翻译（sent / unconfirmed / failed / blocked）落成出口的 status × ack；
3. 只会发 `say`：文件、空正文、别的 kind 都 fail closed，一次不碰平台；
4. 出口必须**认 `_resolve` 的判决**：适配器没启用时 `blocked`，一个字节都不发
   （这是真机自检抓出来的 fail-open，`dry_run=False` 时必须拦住）；
5. 影子档配上已启用的适配器 → `skipped(dry_run)`，仍然一个字节不发；
6. 静态：适配器自己不含 `press_enter`（按回车只发生在 `wechat_send` 里）。
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.contracts.delivery import DeliverItem  # noqa: E402
from core.execution.wechat_adapter import (  # noqa: E402
    KIND_SAY,
    PLATFORM,
    WechatAdapter,
    build_registry,
    default_registry,
)
from core.speech import delivery as dv  # noqa: E402
from core.speech import exit_shadow as ex  # noqa: E402


class FakeResult:
    def __init__(self, status="sent", *, detail="", local_id=9, server_id=1234, at="2026-10-21 20:00:00"):
        self.status = status
        self.detail = detail
        self.local_id = local_id
        self.server_id = server_id
        self.at = at


class FakeGuiError(Exception):
    def __init__(self, code: str, message: str = "boom") -> None:
        super().__init__(message)
        self.code = code


def main() -> int:
    failed = 0

    def check(name: str, cond: bool, detail: str = "") -> None:
        nonlocal failed
        if cond:
            print(f"PASS {name}")
            return
        failed += 1
        print(f"FAIL {name} :: {detail}")

    calls: list[tuple[str, str]] = []

    def ok_send(chat, text, **kw):
        calls.append((chat, text))
        return FakeResult()

    # 1) 能力自报
    caps = WechatAdapter(ok_send).capabilities()
    check("platform=wechat", caps.platform == PLATFORM, caps.platform)
    check("只有 say 一种 kind", caps.kinds == frozenset({KIND_SAY}), str(caps.kinds))
    check("targets 不限制（空集）", caps.targets == frozenset(), str(caps.targets))
    check("需要账号态", caps.needs_auth is True, str(caps.needs_auth))
    check("限流是结构化数字", caps.rate_limits == {"per_turn": 2, "per_minute": 6}, str(caps.rate_limits))
    check("注册不等于启用", caps.enabled is False, str(caps.enabled))

    # 2) 注册表：register / enable / check
    reg = build_registry(adapter=WechatAdapter(ok_send))
    check("注册后 platforms 有 wechat", reg.platforms() == ("wechat",), str(reg.platforms()))
    check("注册后默认没启用", reg.is_enabled("wechat") is False, "enabled")
    check("没启用 check 拒绝", reg.check("wechat", "say", "c1") == (False, "adapter_disabled"),
          str(reg.check("wechat", "say", "c1")))
    reg.enable("wechat")
    check("启用后 check 放行", reg.check("wechat", "say", "c1") == (True, ""), str(reg.check("wechat", "say", "c1")))
    check("启用后别的 kind 仍拒", reg.check("wechat", "file", "c1") == (False, "kind_not_registered"),
          str(reg.check("wechat", "file", "c1")))
    check("能力没漂移", reg.capability_drift() == {}, str(reg.capability_drift()))
    check("默认表没启用", default_registry().is_enabled("wechat") is False, "enabled")

    # 3) send 四态翻译
    calls.clear()
    r = WechatAdapter(ok_send).send("say", "c1", {"text": "hi"})
    check("sent -> status=sent", r.status == "sent", r.status)
    check("sent -> ack=platform_id", r.ack == "platform_id", r.ack)
    check("sent -> 平台号是 server_id", r.platform_msg_id == "1234", r.platform_msg_id)
    check("sent -> ref=wechat#local", r.ref == "wechat#9", r.ref)
    check("sent -> client_msg_id=local:N", r.client_msg_id == "local:9", r.client_msg_id)
    check("sent -> adapter=wechat", r.adapter == "wechat", r.adapter)
    check("真发调了一次", calls == [("c1", "hi")], str(calls))

    r = WechatAdapter(lambda c, t, **k: FakeResult("unconfirmed")).send("say", "c1", {"text": "hi"})
    check("unconfirmed -> status=sent", r.status == "sent", r.status)
    check("unconfirmed -> ack=unconfirmed", r.ack == "unconfirmed", r.ack)
    check("unconfirmed -> 没有平台号", r.platform_msg_id == "", r.platform_msg_id)

    r = WechatAdapter(lambda c, t, **k: FakeResult("failed", detail="no_target: 没切到会话")).send(
        "say", "c1", {"text": "hi"})
    check("failed -> status=failed", r.status == "failed", r.status)
    check("failed -> reason 由前缀翻译", r.reason == "target_not_found", r.reason)
    r = WechatAdapter(lambda c, t, **k: FakeResult("failed", detail="fill_failed: 输入框没聚焦")).send(
        "say", "c1", {"text": "hi"})
    check("fill_failed -> platform_error", r.reason == "platform_error", r.reason)
    r = WechatAdapter(lambda c, t, **k: FakeResult("failed", detail="timeout: 8s 内没返回")).send(
        "say", "c1", {"text": "hi"})
    check("timeout -> network", r.reason == "network", r.reason)
    r = WechatAdapter(lambda c, t, **k: FakeResult("blocked", detail="开关关着")).send("say", "c1", {"text": "hi"})
    check("blocked -> status=blocked", r.status == "blocked", r.status)
    check("blocked -> reason=adapter_disabled", r.reason == "adapter_disabled", r.reason)

    # 4) fail closed 的几条
    calls.clear()
    r = WechatAdapter(ok_send).send("file", "c1", {"text": "hi", "file": "a.pdf"})
    check("file -> unsupported_kind", r.reason == "unsupported_kind", r.reason)
    check("file -> 一次都没发", calls == [], str(calls))
    r = WechatAdapter(ok_send).send("say", "c1", {"text": "   "})
    check("空正文 -> skipped(empty_content)", (r.status, r.reason) == ("skipped", "empty_content"),
          f"{r.status}/{r.reason}")
    check("空正文 -> 一次都没发", calls == [], str(calls))
    r = WechatAdapter(ok_send).send("say", "", {"text": "hi"})
    check("没目标 -> target_not_found", r.reason == "target_not_found", r.reason)

    def boom(chat, text, **kw):
        raise FakeGuiError("no_target", "找不到会话")
    r = WechatAdapter(boom).send("say", "c1", {"text": "hi"})
    check("异常 -> partial 不抛", r.status == "partial", r.status)
    check("异常不能证明未执行", r.reason == "execution_unknown", r.reason)
    def boom2(chat, text, **kw):
        raise RuntimeError("炸了")
    r = WechatAdapter(boom2).send("say", "c1", {"text": "hi"})
    check("未知异常保持未知", r.reason == "execution_unknown", r.reason)

    # 5) 出口必须认 _resolve 的判决（真机自检抓到的 fail-open）
    dv.reset_idempotency()
    calls.clear()
    items = [DeliverItem(kind="say", target="c1", text="hi", file="", basis="")]
    got = dv.deliver(items, turn_id="t-disabled", dry_run=False,
                     registry=build_registry(adapter=WechatAdapter(ok_send)), platform="wechat")
    check("未启用 + 真发档 -> blocked", got[0].status == "blocked", got[0].status)
    check("未启用 -> reason=adapter_disabled", got[0].reason == "adapter_disabled", got[0].reason)
    check("未启用 -> 一个字节都没发", calls == [], str(calls))

    dv.reset_idempotency()
    calls.clear()
    got = dv.deliver(items, turn_id="t-enabled", dry_run=False,
                     registry=build_registry(adapter=WechatAdapter(ok_send), enable=True), platform="wechat")
    check("已启用 + 真发档 -> sent", got[0].status == "sent", got[0].status)
    check("已启用 -> 真的发了一次", calls == [("c1", "hi")], str(calls))
    same = dv.deliver(items, turn_id="t-enabled", dry_run=False,
                      registry=build_registry(adapter=WechatAdapter(ok_send), enable=True), platform="wechat")
    check("同一回合重复 -> duplicate", same[0].reason == "duplicate", same[0].reason)

    # 顺序有讲究：额度用完的回合里，重复项要报 duplicate 而不是 over_limit
    # （「这条已经发过」与「这回合额度用完了」是两件事，后者会误导人去换回合重发）
    dv.reset_idempotency()
    calls.clear()
    reg2 = build_registry(adapter=WechatAdapter(ok_send), enable=True)
    dv.deliver(items, turn_id="t-order", max_items=1, dry_run=False, registry=reg2, platform="wechat")
    again = dv.deliver(items, turn_id="t-order", max_items=1, dry_run=False, registry=reg2, platform="wechat")
    check("满额回合里重复项 -> duplicate（不是 over_limit）", again[0].reason == "duplicate", again[0].reason)

    # 6) 影子档 + 已启用适配器 -> dry_run，不发
    real_mode = ex.exit_shadow_mode
    dv.reset_idempotency()
    calls.clear()
    try:
        ex.exit_shadow_mode = lambda: "shadow"
        got = ex.shadow_deliver({"deliver": [{"kind": "say", "text": "hi"}]}, turn_id="t-shadow",
                                chat_id="c1",
                                registry=build_registry(adapter=WechatAdapter(ok_send), enable=True))
    finally:
        ex.exit_shadow_mode = real_mode
    check("影子档 -> skipped(dry_run)", (got[0].status, got[0].reason) == ("skipped", "dry_run"),
          f"{got[0].status}/{got[0].reason}")
    check("影子档 -> 一个字节都没发", calls == [], str(calls))

    # 7) 静态纪律
    src = io.open(ROOT / "core/execution/wechat_adapter.py", encoding="utf-8").read()
    check("适配器自己不按回车", "press_enter" not in src, "found")
    check("适配器不 import 出口", "core.speech" not in src, "found")

    print(f"RESULT {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
