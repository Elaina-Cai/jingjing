"""U-14 `core/speech/legacy_bridge.py`：旧出口的最后一段换成新出口。

只测三件事：什么时候**不接**（必须退回旧路）、接上以后回执怎么翻译、
以及「不许重试」这条纪律在结构上成立。
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dataclasses import dataclass  # noqa: E402

from core.contracts.delivery import Receipt  # noqa: E402
from core.speech import legacy_bridge as lb  # noqa: E402

FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"PASS {name}")
    else:
        FAILED.append(name)
        print(f"FAIL {name} {detail}".rstrip())


@dataclass
class Caps:
    kinds: frozenset[str]
    targets: frozenset[str] = frozenset({"*"})


class FakeAdapter:
    platform = "wechat"

    def __init__(self, kinds=("say",)):
        self._kinds = frozenset(kinds)
        self.sent: list = []

    def capabilities(self):
        return Caps(self._kinds)

    def send(self, kind, target, payload):
        self.sent.append((kind, target, dict(payload)))
        return Receipt(status="sent", kind=kind, target=target,
                       platform_msg_id="42", ack="platform_id")


class FakeRegistry:
    def __init__(self, adapter=None, enabled=True):
        self._adapter = adapter
        self._enabled = enabled

    def get(self, platform):
        return self._adapter

    def is_enabled(self, platform):
        return self._enabled


class Env:
    """临时改模块里的开关与 deliver（仓库体例：不用 pytest 的 monkeypatch）。"""

    def __init__(self, *, mode="on", deliver=None, default_registry=None):
        self.mode = mode
        self.deliver = deliver
        self.default_registry = default_registry

    def __enter__(self):
        self._old_mode = lb.legacy_via_exit_mode
        lb.legacy_via_exit_mode = lambda: self.mode
        self._old_deliver = None
        if self.deliver is not None:
            import core.speech.delivery as d

            self._old_deliver = d.deliver
            d.deliver = self.deliver
        self._old_reg = None
        if self.default_registry is not None:
            import core.jobs.executor as ex

            self._old_reg = ex.default_registry
            ex.default_registry = self.default_registry
        return self

    def __exit__(self, *exc):
        lb.legacy_via_exit_mode = self._old_mode
        if self._old_deliver is not None:
            import core.speech.delivery as d

            d.deliver = self._old_deliver
        if self._old_reg is not None:
            import core.jobs.executor as ex

            ex.default_registry = self._old_reg
        return False


CALLS: list = []


def fake_deliver(receipt=None, *, items=None):
    def _deliver(deliver_items, *, turn_id, registry=None, platform=None,
                 ledger=None, **kwargs):
        CALLS.append({"items": list(deliver_items), "turn_id": turn_id,
                      "platform": platform, "ledger": ledger})
        row = receipt or Receipt(status="sent", kind="say", target="张三",
                                 platform_msg_id="99", ack="platform_id")
        if ledger is not None:
            ledger(row)
        return [row]

    return _deliver


def main() -> int:
    src = io.open(str(ROOT / "core/speech/legacy_bridge.py"), encoding="utf-8").read()
    out = io.open(str(ROOT / "core/speech/outbound.py"), encoding="utf-8").read()

    # ── 回执翻译（纯函数，七种状态一个不漏）───────────────────────────────
    cases = [
        ("sent", True, "42"),
        ("sent_unconfirmed", True, ""),
        ("skipped", False, ""),
        ("blocked", False, ""),
        ("failed", False, ""),
        ("partial", False, ""),
        ("pending_approval", False, ""),
    ]
    for status, want_ok, want_id in cases:
        # 只有 sent 才真的带平台消息号；其余状态带号是「不该发生」的输入
        r = Receipt(status=status, kind="say",
                    platform_msg_id=("42" if status == "sent" else ""),
                    reason="dry_run")
        got = lb.receipt_outcome(r)
        check(f"翻译：{status} → ok={want_ok}", got.ok is want_ok, str(got))
        check(f"翻译：{status} → msg_id={want_id!r}", got.msg_id == want_id, got.msg_id)
        if not want_ok:
            check(f"翻译：{status} 带原因", got.error.startswith(status), got.error)

    # ── 四种 fallback：不接，退回旧路 ──────────────────────────────────
    with Env(mode="off", deliver=fake_deliver()):
        CALLS.clear()
        got = lb.deliver_via_exit("wechat", "张三", "在吗", registry=FakeRegistry(FakeAdapter()))
        check("off：不接", got.handled is False, str(got))
        check("off：一次都不发", CALLS == [], str(CALLS))

    with Env(mode="shadow", deliver=fake_deliver()):
        CALLS.clear()
        got = lb.deliver_via_exit("wechat", "张三", "在吗", registry=FakeRegistry(FakeAdapter()))
        check("shadow：也不接（不发）", got.handled is False and CALLS == [], str(got))

    with Env(mode="on", deliver=fake_deliver()):
        CALLS.clear()
        got = lb.deliver_via_exit("qq", "张三", "在吗", registry=FakeRegistry(FakeAdapter()))
        check("别的平台：不接", got.handled is False and CALLS == [])

        CALLS.clear()
        got = lb.deliver_via_exit("wechat", "张三", "在吗", kind="sticker",
                                  registry=FakeRegistry(FakeAdapter()))
        check("未知种类：不接", got.handled is False and CALLS == [])

        CALLS.clear()
        got = lb.deliver_via_exit("wechat", "张三", "在吗", registry=None)
        check("没有注册表：不接", got.handled is False and CALLS == [])

        CALLS.clear()
        got = lb.deliver_via_exit("wechat", "张三", "在吗",
                                  registry=FakeRegistry(None))
        check("注册表里没这个平台：不接", got.handled is False and CALLS == [])

        CALLS.clear()
        got = lb.deliver_via_exit("wechat", "张三", "在吗",
                                  registry=FakeRegistry(FakeAdapter(), enabled=False))
        check("适配器没启用：不接（旧路还在）", got.handled is False and CALLS == [])

        CALLS.clear()
        got = lb.deliver_via_exit("wechat", "图片", "", kind="file", filepath="a.png",
                                  registry=FakeRegistry(FakeAdapter(kinds=("say",))))
        check("种类不被自报能力支持：不接", got.handled is False and CALLS == [])

    # ── 接上以后：一条投递、回执翻译、回合号透传 ──────────────────────────
    adapter = FakeAdapter()
    with Env(mode="on", deliver=fake_deliver()):
        CALLS.clear()
        got = lb.deliver_via_exit("wechat", "张三", "在吗", turn_id="turn-7",
                                  chat_id="c1", registry=FakeRegistry(adapter))
        check("接上：handled=True", got.handled is True, str(got))
        check("接上：ok 与消息号", got.outcome.ok and got.outcome.msg_id == "99",
              str(got.outcome))
        check("接上：只交一条", len(CALLS) == 1 and len(CALLS[0]["items"]) == 1, str(CALLS))
        check("接上：投递内容对", CALLS[0]["items"][0].text == "在吗"
              and CALLS[0]["items"][0].target == "张三", str(CALLS))
        check("接上：回合号透传", CALLS[0]["turn_id"] == "turn-7", CALLS[0]["turn_id"])
        check("接上：平台透传", CALLS[0]["platform"] == "wechat", str(CALLS[0]["platform"]))
        check("接上：落账钩子在", CALLS[0]["ledger"] is not None)

    # ── 新出口自己炸了：退回旧路，不把回复卡死 ───────────────────────────
    def boom(*args, **kwargs):
        raise RuntimeError("炸了")

    with Env(mode="on", deliver=boom):
        got = lb.deliver_via_exit("wechat", "张三", "在吗", registry=FakeRegistry(FakeAdapter()))
        check("新出口抛异常：保留接管", got.handled is True and got.reason == "execution_unknown", str(got))

    with Env(mode="on", deliver=lambda items, **kw: []):
        got = lb.deliver_via_exit("wechat", "张三", "在吗", registry=FakeRegistry(FakeAdapter()))
        check("新出口返回空：保留接管", got.handled is True and got.reason == "execution_unknown", str(got))

    # ── 结构纪律 ────────────────────────────────────────────────────
    check("静态：桥里没有重试/退避", "retry" not in src.lower() and "backoff" not in src.lower())
    check("静态：桥不写 audit/DB", "record(" not in src and "INSERT" not in src)
    check("静态：默认 off 由 features 决定", 'features.LEGACY_VIA_EXIT' in src)
    check("静态：出口先问桥再问 provider",
          out.index("_send_via_exit_bridge(") < out.index("await provider.send(target_id"))
    check("静态：桥在线程里跑（真发约 30 秒）", "asyncio.to_thread(" in out)
    check("静态：桥失败只 log 不抛", "legacy_bridge_failed" in out)

    print(f"RESULT {len(FAILED)} failed")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
