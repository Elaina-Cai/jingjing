# -*- coding: utf-8 -*-
"""E2 验收（U-05）：平台适配器能力声明。

跑法：``python -X utf8 -u tests/test_adapter_capabilities.py``
现在应为 **红**：``AdapterRegistry`` 的方法体还没写（``registry.py`` 只有签名）。

规格出处：`docs/decisions/软件适配器.md` §2 / §5 / §6，`docs/decisions/出口.md` §4。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.contracts.delivery import BLOCK_REASONS, Receipt  # noqa: E402
from core.execution.adapters import (  # noqa: E402
    RETRYABLE,
    AdapterCapabilities,
    AdapterRegistry,
    ErrorCode,
)

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"PASS {name}")
    else:
        print(f"FAIL {name} {detail}")
        FAILS.append(name)


class FakeAdapter:
    platform = "fake"

    def __init__(self, kinds=("say",), targets=("chat:1",)):
        self._kinds = frozenset(kinds)
        self._targets = frozenset(targets)
        self.sent: list[str] = []

    def capabilities(self) -> AdapterCapabilities:
        return AdapterCapabilities(
            platform=self.platform,
            kinds=self._kinds,
            targets=self._targets,
            needs_auth=True,
            rate_limits={"say_per_min": 10},
            enabled=False,
        )

    def send(self, kind: str, target: str, payload) -> Receipt:
        self.sent.append(kind)
        return Receipt(status="sent", kind=kind, target=target, ack="platform_id",
                       platform_msg_id="m1", adapter=self.platform)


def main() -> int:
    fields = AdapterCapabilities.__dataclass_fields__
    check("能力声明含 targets（出口校验①要按 target 判定）", "targets" in fields)
    check("能力声明含结构化 rate_limits（不是人话 hint）", "rate_limits" in fields)
    check("新适配器默认未启用", AdapterCapabilities(platform="x").enabled is False)

    check("六类错误码齐全且只有六类", len(ErrorCode) == 6)
    check("可重试只有三类", set(RETRYABLE) == {
        ErrorCode.RATE_LIMITED, ErrorCode.PLATFORM_ERROR, ErrorCode.NETWORK})
    check("auth 过期不重试", ErrorCode.AUTH_EXPIRED not in RETRYABLE)

    reg = AdapterRegistry()
    a = FakeAdapter()
    ok = True
    try:
        reg.register(a)
    except NotImplementedError:
        ok = False
    check("register 可注册", ok)

    if not ok:
        for name in ("注册后默认 disabled", "人工 enable 后可发", "未声明 kind 被拦",
                     "未声明 target 被拦", "未知平台被拦", "能力漂移可发现",
                     "send 必须回 Receipt（无回执率 0）"):
            check(name, False, "AdapterRegistry 方法体未实现")
        print(f"RESULT {len(FAILS)} failed")
        return 1

    check("注册后默认 disabled", reg.get("fake") is not None)
    blocked, reason = reg.check("fake", "say", "chat:1")
    check("注册后默认 disabled", blocked is False and reason == "adapter_disabled",
          f"got=({blocked},{reason})")
    check("blocked 的 reason 在 BLOCK_REASONS 里", reason in BLOCK_REASONS)

    reg.enable("fake")
    okc, reason = reg.check("fake", "say", "chat:1")
    check("人工 enable 后可发", okc is True, f"got=({okc},{reason})")

    okc, reason = reg.check("fake", "不存在的kind", "chat:1")
    check("未声明 kind 被拦", okc is False and reason in BLOCK_REASONS, f"got=({okc},{reason})")

    okc, reason = reg.check("fake", "say", "chat:999")
    check("未声明 target 被拦", okc is False and reason in BLOCK_REASONS, f"got=({okc},{reason})")

    okc, reason = reg.check("nope", "say", "chat:1")
    check("未知平台被拦", okc is False and reason in BLOCK_REASONS, f"got=({okc},{reason})")

    # 能力漂移：注册后自报的 kinds 变了 → 必须被人看见（软件适配器.md §7）
    a._kinds = frozenset({"say", "file"})
    drift = reg.capability_drift()
    check("能力漂移可发现", "fake" in drift, f"got={drift}")

    r = a.send("say", "chat:1", {"text": "hi"})
    check("send 必须回 Receipt（无回执率 0）", isinstance(r, Receipt), f"got={type(r)}")

    check("一次投递只调一个 adapter", len(a.sent) == 1, f"got={len(a.sent)}")

    print(f"RESULT {len(FAILS)} failed")
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())
