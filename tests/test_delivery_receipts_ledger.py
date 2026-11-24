# -*- coding: utf-8 -*-
"""出口回执落账（U-11 第二半）：`delivery_receipts` 一张旁注表。

钉住四条：
- **六种状态都写**（含 `pending_approval`）——"没发出去"也必须有行；
- **off 档一行不写**；`shadow` 也写（回执记的是"这次发成了什么"，与"谁负责传输"无关）；
- **同一回合 + 同一对象 + 同一 kind 只写一行**（唯一索引 + INSERT OR IGNORE），重放不灌双份；
- **写失败只 log**，不抛、不阻断回合。

跑法：`.venv_it\\Scripts\\python.exe -X utf8 -u tests\\test_delivery_receipts_ledger.py`
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import create_engine, text  # noqa: E402

from core import features  # noqa: E402
from core.contracts.delivery import Receipt  # noqa: E402
from core.speech import delivery  # noqa: E402
from core.terminal import delivery_receipts as receipts  # noqa: E402

PASSED = 0
FAILED = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print(f"PASS {name}")
    else:
        FAILED += 1
        print(f"FAIL {name}" + (f"  [{detail}]" if detail else ""))


class _Mode:
    def __init__(self, mode: str) -> None:
        self.mode = mode
        self.orig = receipts._mode

    def __enter__(self):
        receipts._mode = lambda: self.mode
        return self

    def __exit__(self, *a):
        receipts._mode = self.orig
        return False


class _DelMode:
    """临时把 `delivery._delivery_mode()` 钉成某一档（出口那一侧的开关）。"""

    def __init__(self, mode: str) -> None:
        self.mode = mode
        self.orig = delivery._delivery_mode

    def __enter__(self):
        delivery._delivery_mode = lambda: self.mode
        return self

    def __exit__(self, *a):
        delivery._delivery_mode = self.orig
        return False


def _fresh():
    engine = create_engine("sqlite://")
    for sql in receipts.ddl_statements():
        with engine.begin() as conn:
            conn.execute(text(sql))
    receipts.set_engine(engine)
    return engine


def _count(engine) -> int:
    with engine.connect() as conn:
        return int(conn.execute(text("SELECT COUNT(*) FROM delivery_receipts")).scalar() or 0)


def _receipt(**kw) -> dict:
    base = {
        "status": "sent",
        "kind": "say",
        "target": "c1",
        "ack": "platform_id",
        "reason": "",
        "platform_msg_id": "m-1",
        "client_msg_id": "cm-1",
        "adapter": "wechat",
        "detail": "在的",
        "ref": "job-abc#2",
    }
    base.update(kw)
    return base


def test_ddl_idempotent(engine) -> None:
    for sql in receipts.ddl_statements():
        with engine.begin() as conn:
            conn.execute(text(sql))
    check("DDL 幂等（跑第二遍不炸）", _count(engine) == 0, str(_count(engine)))


def test_gating(_engine) -> None:
    # 这一条自己造引擎：off 档要验"0 行"，shadow 档要验"真的写了"，两件事不能共用计数。
    engine = _fresh()
    receipts.set_engine(engine)
    with _Mode("off"):
        ok = receipts.record_receipt(_receipt(), turn_id="t-off", chat_id="c1")
    check("off 档不写（返回 False）", ok is False, str(ok))
    check("off 档表里 0 行", _count(engine) == 0, str(_count(engine)))

    with _Mode("shadow"):
        ok = receipts.record_receipt(_receipt(), turn_id="t-shadow", chat_id="c1")
    check("shadow 档也写（旁注与传输归属无关）", ok is True, str(ok))
    check("shadow 档表里 1 行", _count(engine) == 1, str(_count(engine)))
    receipts.set_engine(_engine)


def test_write_and_fields(engine) -> None:
    with _Mode("on"):
        ok = receipts.record_receipt(_receipt(), turn_id="t1", chat_id="c1")
    check("on 档真的写入", ok is True and _count(engine) == 1, f"{ok}/{_count(engine)}")
    with engine.connect() as conn:
        row = conn.execute(text("SELECT * FROM delivery_receipts")).mappings().first()
    check("字段逐一对上",
          row["turn_id"] == "t1" and row["chat_id"] == "c1" and row["status"] == "sent"
          and row["kind"] == "say" and row["target"] == "c1" and row["ack"] == "platform_id"
          and row["platform_msg_id"] == "m-1" and row["client_msg_id"] == "cm-1"
          and row["adapter"] == "wechat" and row["ref"] == "job-abc#2",
          str(dict(row)))
    check("ts 自动填了", row["ts"] is not None, str(row["ts"]))


def test_unique(engine) -> None:
    with _Mode("on"):
        again = receipts.record_receipt(_receipt(), turn_id="t1", chat_id="c1")
        other = receipts.record_receipt(_receipt(ref="job-abc#3"), turn_id="t1", chat_id="c1")
    check("同回合同对象同 kind 不重复写入", again is False and _count(engine) == 2, f"{again}/{_count(engine)}")
    check("换个 ref 就是新的一行", other is True, str(other))


def test_guard_and_failure(engine) -> None:
    with _Mode("on"):
        check("空 turn_id 不写", receipts.record_receipt(_receipt(), turn_id="") is False, "")

    class _Boom:
        def begin(self):
            raise RuntimeError("db gone")

    receipts.set_engine(_Boom())
    with _Mode("on"):
        check("写失败只返回 False，不抛", receipts.record_receipt(_receipt(ref="x#1"), turn_id="t2") is False, "")
    receipts.set_engine(engine)
    check("写失败没有脏行", _count(engine) == 2, str(_count(engine)))


def test_six_statuses(engine) -> None:
    with _Mode("on"):
        for i, status in enumerate(("sent", "skipped", "blocked", "failed", "partial", "pending_approval")):
            receipts.record_receipt(_receipt(status=status, ref=f"s#{i}"), turn_id="t3", chat_id="c2")
    counts = receipts.count_by_status(chat_id="c2")
    check("六种状态各一行", len(counts) == 6 and sum(counts.values()) == 6, str(counts))
    check("pending_approval 也在表里", counts.get("pending_approval") == 1, str(counts))
    rows = receipts.recent(chat_id="c2", limit=3)
    check("recent 倒序取最近三条", len(rows) == 3, str(len(rows)))


def test_ledger_hook(engine) -> None:
    before = _count(engine)
    with _Mode("on"):
        hook = receipts.make_ledger(turn_id="t4", chat_id="c3")
        hook(Receipt(status="pending_approval", kind="plan", target="c3", ack="none",
                     detail="job-x 第 1 步需批准：订票", ref="job-x#1"))
        hook(Receipt(status="pending_approval", kind="plan", target="c3", ack="none",
                     detail="job-x 第 2 步需批准：付钱", ref="job-x#2"))
    check("钩子逐条落行（回合/会话在闭包里绑定）", _count(engine) == before + 2, str(_count(engine)))
    rows = receipts.recent(chat_id="c3", limit=5)
    check("钩子写进去的回合号正确", all(r["turn_id"] == "t4" for r in rows), str([r["turn_id"] for r in rows]))


def test_end_to_end(engine) -> None:
    before = _count(engine)
    approval = {"status": "pending_approval", "kind": "plan", "target": "c9", "ack": "none",
                "detail": "job-abc 第 2 步需批准：订机票", "job_id": "job-abc", "step_n": 2}
    with _Mode("on"), _DelMode("on"):
        out = delivery.deliver_pending_approvals(
            [approval], turn_id="t5", ledger=receipts.make_ledger(turn_id="t5", chat_id="c9")
        )
    check("端到端：出口造一条第六态", len(out) == 1 and out[0].status == "pending_approval", str(len(out)))
    check("端到端：表里多一行", _count(engine) == before + 1, str(_count(engine)))
    rows = receipts.recent(chat_id="c9", limit=5)
    check("端到端：ref 指出是哪一件哪一步", rows and rows[0]["ref"] == "job-abc#2", str(rows[:1]))


def test_ref_passthrough() -> None:
    kept = delivery._with_context(
        Receipt(status="sent", kind="say", target="c1", ref="job-z#7"), "wechat"
    )
    check("_with_context 不丢 ref", kept.ref == "job-z#7", kept.ref)


def test_static() -> None:
    src = (ROOT / "core/speech/delivery.py").read_text(encoding="utf-8")
    check("出口不碰数据库（不 import sqlalchemy）", "sqlalchemy" not in src, "")
    pipe = (ROOT / "core/routing/turn_pipeline.py").read_text(encoding="utf-8")
    check("turn_pipeline 传的是 make_ledger", "make_ledger(" in pipe, "")
    from db import migrations as mig

    versions = [m.version for m in mig._MIGRATIONS]
    check("迁移 65 已登记", 65 in versions, str(sorted(versions)[-3:]))
    check("迁移 65 唯一", versions.count(65) == 1, str(versions.count(65)))


def main() -> int:
    engine = _fresh()
    try:
        test_ddl_idempotent(engine)
        test_gating(engine)
        test_write_and_fields(engine)
        test_unique(engine)
        test_guard_and_failure(engine)
        test_six_statuses(engine)
        test_ledger_hook(engine)
        test_end_to_end(engine)
        test_ref_passthrough()
        test_static()
    finally:
        receipts.set_engine(None)
    print(f"RESULT {FAILED} failed / {PASSED + FAILED} checked")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
