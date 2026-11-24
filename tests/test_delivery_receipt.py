# -*- coding: utf-8 -*-
"""E3 验收（U-04）：出口回执与幂等 —— ``deliver`` → ``Receipt``。

跑法：``python -X utf8 -u tests/test_delivery_receipt.py``
现在应为 **红**：``core/speech/delivery.py`` 还不存在（这是设计，不是缺陷）。

本文件是**可执行的规格**：下面每一条断言都对应 `docs/decisions/出口.md` 的一行。
实现要满足的是这些断言，而不是「跑过 run_all_tests」。
"""

from __future__ import annotations

import itertools
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.contracts.delivery import (  # noqa: E402
    ACKS,
    BLOCK_REASONS,
    SKIP_REASONS,
    STATUSES,
    DeliverItem,
    Receipt,
)

FAILS: list[str] = []
_TURNS = itertools.count(1)


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"PASS {name}")
    else:
        print(f"FAIL {name} {detail}")
        FAILS.append(name)


try:
    from core.speech import delivery as D
except Exception as exc:  # noqa: BLE001 - 未实现时 import 失败是预期红
    D = None
    IMPORT_ERR = f"{type(exc).__name__}: {exc}"


class FakeAdapter:
    """最小适配器：记录调用、按队列返回回执或抛错。"""

    platform = "fake"

    def __init__(self, mode: str = "ok"):
        self.mode = mode          # ok / no_id / network / partial
        self.calls: list[tuple[str, str, str]] = []

    def capabilities(self):
        from core.execution.adapters import AdapterCapabilities

        return AdapterCapabilities(
            platform=self.platform,
            kinds=frozenset({"say", "file"}),
            # 2b 要测「不同槽位」，所以适配器得认这几个 target（否则先被
            # target_not_found 拦下，测不到额度）。
            targets=frozenset({"chat:1", "chat:2", "chat:3"}),
            needs_auth=False,
            enabled=True,
        )

    def send(self, kind: str, target: str, payload) -> Receipt:
        self.calls.append((kind, target, str(payload.get("text", ""))))
        if self.mode == "network":
            from core.execution.adapters import ErrorCode

            return Receipt(status="failed", kind=kind, target=target,
                           ack="error", reason=ErrorCode.NETWORK.value)
        if self.mode == "no_id":
            # 平台接受了，但没给消息号（出口.md §14-2 的 sent_unconfirmed）
            return Receipt(status="sent", kind=kind, target=target, ack="unconfirmed")
        if self.mode == "partial":
            return Receipt(status="partial", kind=kind, target=target, ack="platform_id",
                           platform_msg_id="m1", detail="第二部分失败")
        return Receipt(status="sent", kind=kind, target=target, ack="platform_id",
                       platform_msg_id="m1")


def main() -> int:
    # ── 契约形状（出口.md §5）────────────────────────────────────────
    check("六种结果齐全且只有六种（含 pending_approval）", len(STATUSES) == 6 and set(STATUSES) == {
        "sent", "skipped", "blocked", "failed", "partial", "pending_approval"})
    check("回执与结果正交（四值）", len(ACKS) == 4 and "unconfirmed" in ACKS)
    check("skipped 的 reason 是名字不是句子", "dry_run" in SKIP_REASONS)
    check("blocked 的 reason 是名字不是句子", "kind_not_registered" in BLOCK_REASONS)

    if D is None:
        for name in (
            "deliver 存在", "dry-run 留一行 skipped", "无 target 记 blocked",
            "无号无错记 sent+unconfirmed", "network 只重试一次", "同回合同一条只发一次",
            "五条进五条出（不许静默丢弃）", "未知 kind 记 blocked", "文件+说明部分失败记 partial",
            "出口不改写正文",
        ):
            check(name, False, f"core.speech.delivery 未实现（{IMPORT_ERR}）")
        print(f"RESULT {len(FAILS)} failed")
        return 1

    reg = {}

    def run(items, *, dry_run=False, adapter=None, turn_id=None):
        # 每个场景一个独立 turn：单回合 ≤2 条是按 turn_id 计的（出口.md §9）
        turn_id = turn_id or f"t{next(_TURNS)}"
        a = adapter or FakeAdapter()
        reg["fake"] = a
        return D.deliver(items, turn_id=turn_id, dry_run=dry_run,
                         registry={"fake": a}, platform="fake"), a

    # ── 1. dry-run 也必须留痕（出口.md §15-4）────────────────────────
    receipts, a = run([DeliverItem(kind="say", target="chat:1", text="在的")], dry_run=True)
    check("dry-run 留一行 skipped",
          len(receipts) == 1 and receipts[0].status == "skipped"
          and receipts[0].reason == "dry_run",
          f"got={receipts}")
    check("dry-run 不真的调用适配器", a.calls == [])

    # ── 2. 没 target / 空内容 / 未知 kind → blocked 或 skipped，绝不静默 ──
    receipts, _ = run([
        DeliverItem(kind="say", target="", text="没有收件人"),
        DeliverItem(kind="say", target="chat:1", text=""),
        DeliverItem(kind="不存在的kind", target="chat:1", text="x"),
    ])
    check("三条进三条出（不许静默丢弃）", len(receipts) == 3, f"got={len(receipts)}")
    check("无 target 记 blocked(no_target)",
          receipts[0].status == "blocked" and receipts[0].reason == "no_target",
          f"got={receipts[0]}")
    check("未知 kind 记 blocked(kind_not_registered)",
          receipts[2].status == "blocked" and receipts[2].reason == "kind_not_registered",
          f"got={receipts[2]}")

    # ── 2b. 单回合 ≤2 个**槽位**：第三个不同的 target 必须被拦（不静默丢弃）──
    #     2026-10-21 起按 (kind,target) 槽位记账：真机一条长回复会被拆成多个气泡，
    #     每个气泡都是一次 deliver；按"次数"记账会让第 3 个气泡就 over_limit，
    #     用户看到的是"她没回"。
    receipts, a = run([DeliverItem(kind="say", target="chat:1", text="第一句"),
                       DeliverItem(kind="say", target="chat:2", text="第二句"),
                       DeliverItem(kind="say", target="chat:3", text="第三句")])
    check("单回合第三个槽位 → blocked(over_limit)",
          len(receipts) == 3 and [r.status for r in receipts][:2] == ["sent", "sent"]
          and receipts[2].status == "blocked" and receipts[2].reason == "over_limit",
          f"got={[r.status for r in receipts]}")
    check("超限的那条没有真的调用适配器", len(a.calls) == 2, f"calls={len(a.calls)}")

    # ── 2c. 同一个 target 的多段气泡不算超限（正是 2026-10-21 那条真机 bug）──
    receipts, a = run([DeliverItem(kind="say", target="chat:1", text="长回复第一段"),
                       DeliverItem(kind="say", target="chat:1", text="长回复第二段"),
                       DeliverItem(kind="say", target="chat:1", text="长回复第三段")])
    check("同一 target 的多段气泡不触发 over_limit",
          [r.status for r in receipts] == ["sent", "sent", "sent"],
          f"got={[r.status for r in receipts]}")

    # ── 3. 无号无错 = sent + unconfirmed；这算「已发」 ────────────────
    receipts, _ = run([DeliverItem(kind="say", target="chat:1", text="嗯")],
                      adapter=FakeAdapter("no_id"))
    check("无号无错记 sent+unconfirmed",
          receipts[0].status == "sent" and receipts[0].ack == "unconfirmed",
          f"got={receipts[0]}")
    check("unconfirmed 也算已发出（重复发送率口径）", receipts[0].is_delivered())

    # ── 4. 网络错 → failed(network)，最多重试 1 次 ────────────────────
    receipts, a = run([DeliverItem(kind="say", target="chat:1", text="喂")],
                      adapter=FakeAdapter("network"))
    check("network 记 failed", receipts[0].status == "failed"
          and receipts[0].reason == "network", f"got={receipts[0]}")
    check("重试不超过 1 次（共 ≤2 次调用）", len(a.calls) <= 2, f"calls={len(a.calls)}")

    # ── 5. 幂等：同一回合同一条只发一次（出口.md §8）─────────────────
    items = [DeliverItem(kind="say", target="chat:1", text="我来了 @张三")]
    r1, a = run(items, turn_id="t9")
    try:
        r2 = D.deliver(items, turn_id="t9", dry_run=False,
                       registry={"fake": a}, platform="fake")
        dup_ok = (len(a.calls) == 1 and len(r2) == 1
                  and r2[0].status in ("skipped", "blocked")
                  and r2[0].reason == "duplicate")
    except Exception:  # noqa: BLE001
        dup_ok = False
    check("同回合同一条只发一次", dup_ok, f"calls={len(a.calls)}")

    # ── 6. 文件 + 说明算一条投递，部分失败记 partial ─────────────────
    receipts, _ = run([DeliverItem(kind="file", target="chat:1", file="out/a.txt",
                                   text="说明")], adapter=FakeAdapter("partial"))
    check("文件+说明部分失败记 partial", receipts[0].status == "partial",
          f"got={receipts[0]}")

    # ── 7. 出口只执行、不改写（出口.md §2）──────────────────────────
    text = "  你好 @张三 \u200b"
    receipts, a = run([DeliverItem(kind="say", target="chat:1", text=text)])
    check("出口不改写正文", a.calls and a.calls[0][2] == text,
          f"got={a.calls[:1]}")

    print(f"RESULT {len(FAILS)} failed")
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())
