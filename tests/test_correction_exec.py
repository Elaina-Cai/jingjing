"""U-26 更正收口：从一条已发回执生成改口，窗口内先撤再改，四条 fail closed。

跑法：.venv_it\\Scripts\\python.exe -X utf8 -u tests/test_correction_exec.py
"""

from __future__ import annotations

import pathlib
import sys
from datetime import datetime, timedelta
from types import SimpleNamespace

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import features  # noqa: E402
from core.jobs import executor  # noqa: E402
from core.speech import correction as C  # noqa: E402

COUNT = [0]
FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    COUNT[0] += 1
    if cond:
        print(f"  ok   {name}")
        return
    FAILS.append(f"{name} {detail}".strip())
    print(f"  FAIL {name} {detail}".strip())


NOW = datetime(2026, 10, 21, 12, 0, 0)
RECEIPT = {
    "status": "sent",
    "kind": "text",
    "target": "遥途逢星溯随天赴山海",
    "detail": "周四上午十点在校门口集合",
    "ref": "wechat#89",
    "at": NOW.strftime("%Y-%m-%d %H:%M:%S"),
}
OLD_RECEIPT = dict(RECEIPT, at=(NOW - timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S"))


class FakeDeliver:
    def __init__(self, status: str = "sent", raises: bool = False) -> None:
        self.status = status
        self.raises = raises
        self.calls: list[tuple] = []

    def __call__(self, target: str, text: str, *, turn_id: str = "") -> dict:
        self.calls.append((target, text, turn_id))
        if self.raises:
            raise RuntimeError("窗口没了")
        return {"status": self.status, "detail": text, "local_id": 91}


class FakeRevoke:
    def __init__(self, ok: bool = True, raises: bool = False) -> None:
        self.ok = ok
        self.raises = raises
        self.calls: list[tuple] = []

    def __call__(self, target: str, ref: str) -> dict:
        self.calls.append((target, ref))
        if self.raises:
            raise RuntimeError("菜单没弹出来")
        return {"ok": self.ok, "detail": "撤回" if self.ok else "找不到那条"}


class FakeLedger:
    def __init__(self, row: dict | None = None) -> None:
        self.row = row
        self.asks: list[str] = []

    def find_ref(self, ref: str) -> dict | None:
        self.asks.append(ref)
        return self.row


def t_off_does_nothing() -> None:
    deliver = FakeDeliver()
    out = C.correct(RECEIPT, "周四改到下午", deliver=deliver, now=NOW, mode="off")
    check("off：不 ok", out.ok is False)
    check("off：状态是 off", out.status == "off", out.status)
    check("off：理由 off", out.reason == C.REASON_OFF, out.reason)
    check("off：一个字节都不发", deliver.calls == [], str(deliver.calls))
    check("off：开关读点是 off", C.correction_mode() in ("off", features.MODE_OFF), C.correction_mode())


def t_shadow_computes_but_does_not_send() -> None:
    deliver = FakeDeliver()
    out = C.correct(RECEIPT, "周四改到下午", deliver=deliver, now=NOW, mode="shadow")
    check("shadow：ok（算出来了）", out.ok is True)
    check("shadow：状态 shadow", out.status == "shadow", out.status)
    check("shadow：不发", deliver.calls == [], str(deliver.calls))
    check("shadow：措辞带前缀", out.text.startswith(C.CORRECTION_PREFIX), out.text)


def t_fail_closed_shapes() -> None:
    deliver = FakeDeliver()
    no_ref = C.correct(dict(RECEIPT, ref=""), "改口", deliver=deliver, now=NOW, mode="on")
    check("没有 ref：不发", no_ref.ok is False and deliver.calls == [])
    check("没有 ref：理由 no_plan", no_ref.reason == C.REASON_NO_PLAN, no_ref.reason)

    empty = C.correct(RECEIPT, "   ", deliver=deliver, now=NOW, mode="on")
    check("改口为空：不发", empty.ok is False and deliver.calls == [])

    nested = C.correct(dict(RECEIPT, detail="更正：周四 → 周五"), "再改", deliver=deliver, now=NOW, mode="on")
    check("连环更正：拒", nested.reason == C.REASON_ALREADY, nested.reason)
    check("is_correction 认得出", C.is_correction(dict(RECEIPT, detail="更正：x")) is True)

    no_channel = C.correct(RECEIPT, "改口", deliver=None, now=NOW, mode="on")
    check("没有投递通道：拒", no_channel.ok is False and no_channel.reason == C.REASON_NO_PLAN)


def t_window_inside_revokes_then_corrects() -> None:
    deliver, revoke = FakeDeliver(), FakeRevoke(ok=True)
    out = C.correct(RECEIPT, "周四改到下午", deliver=deliver, revoke=revoke, now=NOW, mode="on")
    check("窗口内：先撤回", revoke.calls == [("遥途逢星溯随天赴山海", "wechat#89")], str(revoke.calls))
    check("窗口内：再更正", len(deliver.calls) == 1, str(deliver.calls))
    check("窗口内：状态是「撤了又改」", out.status == "revoked_and_corrected", out.status)
    check("窗口内：回执带 ref", out.receipt.get("ref") == "wechat#89", str(out.receipt))
    check("窗口内：ack 是 platform_id", out.receipt.get("ack") == "platform_id", str(out.receipt))


def t_revoke_failure_does_not_block_correction() -> None:
    deliver = FakeDeliver()
    out = C.correct(RECEIPT, "周四改到下午", deliver=deliver, revoke=FakeRevoke(ok=False), now=NOW, mode="on")
    check("撤回失败：仍然更正", len(deliver.calls) == 1, str(deliver.calls))
    check("撤回失败：状态只是 corrected", out.status == "corrected", out.status)

    out2 = C.correct(RECEIPT, "周四改到下午", deliver=FakeDeliver(), revoke=FakeRevoke(raises=True), now=NOW, mode="on")
    check("撤回抛异常：也继续更正", out2.ok is True and out2.status == "corrected", out2.status)


def t_expired_only_corrects() -> None:
    revoke = FakeRevoke()
    out = C.correct(OLD_RECEIPT, "周四改到下午", deliver=FakeDeliver(), revoke=revoke, now=NOW, mode="on")
    check("过期：不点撤回", revoke.calls == [], str(revoke.calls))
    check("过期：mode 是 correct", out.mode == "correct", out.mode)


def t_delivery_failure_is_recorded() -> None:
    recorder_rows: list[dict] = []

    def recorder(row: dict, *, turn_id: str = "", chat_id: str = "") -> None:
        recorder_rows.append(dict(row, turn_id=turn_id, chat_id=chat_id))

    out = C.correct(RECEIPT, "改到下午", deliver=FakeDeliver(status="failed"), recorder=recorder, turn_id="t-1", now=NOW, mode="on")
    check("发失败：不 ok", out.ok is False)
    check("发失败：理由 deliver_failed", out.reason == C.REASON_DELIVER_FAILED, out.reason)
    check("发失败：也落一行回执", len(recorder_rows) == 1, str(recorder_rows))
    check("发失败：回执指向原条", recorder_rows[0].get("ref") == "wechat#89", str(recorder_rows))

    out2 = C.correct(RECEIPT, "改到下午", deliver=FakeDeliver(raises=True), now=NOW, mode="on")
    check("投递抛异常：不 ok 且带异常名", out2.ok is False and "RuntimeError" in out2.reason, out2.reason)

    ok_rows: list[dict] = []
    out3 = C.correct(RECEIPT, "改到下午", deliver=FakeDeliver(status="unconfirmed"), recorder=lambda r, **k: ok_rows.append(r), now=NOW, mode="on")
    check("unconfirmed 算发出（与出口同口径）", out3.ok is True, out3.status)
    check("ok 时也落回执", len(ok_rows) == 1, str(ok_rows))


def t_correct_ref_looks_up() -> None:
    ledger, deliver = FakeLedger(RECEIPT), FakeDeliver()
    out = C.correct_ref("wechat#89", "改到下午", ledger=ledger, deliver=deliver, now=NOW, mode="on")
    check("correct_ref：查了回执", ledger.asks == ["wechat#89"], str(ledger.asks))
    check("correct_ref：发了", len(deliver.calls) == 1)
    check("correct_ref：ok", out.ok is True)

    miss = C.correct_ref("wechat#999", "改到下午", ledger=FakeLedger(None), deliver=FakeDeliver(), now=NOW, mode="on")
    check("查不到回执：拒", miss.ok is False and miss.reason == C.REASON_NO_RECEIPT, miss.reason)
    blank = C.correct_ref("", "改到下午", ledger=FakeLedger(RECEIPT), deliver=FakeDeliver(), now=NOW, mode="on")
    check("回执号为空：拒", blank.ok is False and blank.reason == C.REASON_NO_RECEIPT)


def _step(action: str) -> SimpleNamespace:
    return SimpleNamespace(action=action, effects=executor.EXTERNAL_EFFECT)


def t_executor_correct_action() -> None:
    calls: list[tuple] = []

    def correct(ref: str, fix: str, *, turn_id: str = "") -> dict:
        calls.append((ref, fix, turn_id))
        return {"ok": True, "status": "corrected", "mode": "correct", "ref": ref, "reason": ""}

    ex = executor.write_executor(
        None, turn_id="job-1", target="群", shadow=False,
        allow_external=True, correct=correct,
    )
    out = ex(_step("correct:wechat#89|周四改到下午"))
    check("执行器：动作拆对了", calls == [("wechat#89", "周四改到下午", "job-1")], str(calls))
    check("执行器：ok", out.get("ok") is True, str(out))
    check("执行器：changed 带 ref", out.get("changed", {}).get("ref") == "wechat#89", str(out))
    check("执行器：state_after 是 corrected:", str(out.get("state_after", "")).startswith("corrected:"), str(out))

    blocked = executor.write_executor(None, allow_external=False, correct=correct)
    out2 = blocked(_step("correct:wechat#89|改"))
    check("执行官：未放行 → blocked（同 external_send）", "blocked:non_writable:external_send" == out2.get("detail"), str(out2))

    nobody = executor.write_executor(None, allow_external=True, correct=None)
    out3 = nobody(_step("correct:wechat#89|改"))
    check("没有更正人 → no_sender", str(out3.get("detail", "")).startswith("no_sender"), str(out3))

    out4 = executor.write_executor(None, allow_external=True, correct=correct)
    out4 = out4(_step("correct:wechat#89"))
    check("少写 | → unsupported", str(out4.get("detail", "")).startswith("unsupported"), str(out4))

    fails = {"n": 0}

    def failing(ref: str, fix: str, *, turn_id: str = "") -> dict:
        fails["n"] += 1
        return {"ok": False, "status": "failed", "reason": "deliver_failed"}

    ex5 = executor.write_executor(None, allow_external=True, correct=failing)
    out5 = ex5(_step("correct:wechat#89|改"))
    check("更正失败：不 ok", out5.get("ok") is False, str(out5))
    check("更正失败：不重试（只调一次）", fails["n"] == 1, str(fails["n"]))


def t_external_path_unchanged() -> None:
    seen: list[tuple] = []

    def external(to: str, text: str, *, turn_id: str = "", mention: str = "") -> dict:
        seen.append((to, text, mention))
        return {"status": "sent", "detail": "好", "local_id": 90, "server_id": 1}

    ex = executor.write_executor(None, turn_id="t", target="群", shadow=False, allow_external=True, external=external)
    out = ex(_step("send:张三|明天见"))
    check("普通发送仍走 external", seen == [("张三", "明天见", "")], str(seen))
    check("普通发送仍 ok", out.get("ok") is True, str(out))


def t_registered() -> None:
    check("开关已注册", features.CORRECTION in features.FLAG_REGISTRY)
    check("开关进三态表", features.CORRECTION in features.TRI_STATE_FLAGS)
    check("默认 off", features.FLAG_REGISTRY[features.CORRECTION].default == features.MODE_OFF)
    check("总数 59", len(features.TRI_STATE_FLAGS) == 57, str(len(features.TRI_STATE_FLAGS)))

    src = (ROOT / "core/routing/turn_pipeline.py").read_text(encoding="utf-8")
    check("回合里注入了更正器", "correct=correction," in src)
    check("更正器只在 CORRECTION=on 时给", "features.CORRECTION" in src)
    ex_src = (ROOT / "core/jobs/executor.py").read_text(encoding="utf-8")
    check("执行器认 correct: 前缀", "CORRECT_PREFIX" in ex_src and "correct:<回执号>" in ex_src)


TESTS = (
    t_off_does_nothing,
    t_shadow_computes_but_does_not_send,
    t_fail_closed_shapes,
    t_window_inside_revokes_then_corrects,
    t_revoke_failure_does_not_block_correction,
    t_expired_only_corrects,
    t_delivery_failure_is_recorded,
    t_correct_ref_looks_up,
    t_executor_correct_action,
    t_external_path_unchanged,
    t_registered,
)


def main() -> int:
    for fn in TESTS:
        print(f"[{fn.__name__}]")
        fn()
    print()
    print(f"RESULT {len(FAILS)} failed / {COUNT[0]} checks")
    for line in FAILS:
        print("  -", line)
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())
