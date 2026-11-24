# -*- coding: utf-8 -*-
"""出口对第六态 ``pending_approval`` 的消费（U-11 第一半）。

钉住三件事：
- **一个字节都不发**：这条回执不经过 `_resolve` / `_guard` / `_send_once`，
  函数签名里连 registry 都没有；`is_delivered()` 必须为 False；
- **不许静默**：`DELIVERY_CONTRACT=on` 时每条都交给 `ledger`；`off` 时连日志都不写；
- **不污染幂等**：不进 `_SENT` / `_SENT_PER_TURN`，否则会把后面真正的投递判成重复。

跑法：`.venv_it\\Scripts\\python.exe -X utf8 -u tests\\test_pending_approval_exit.py`
"""

from __future__ import annotations

import inspect
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import create_engine  # noqa: E402

from core import features  # noqa: E402
from core.contracts.delivery import PENDING_APPROVAL, Receipt  # noqa: E402
from core.contracts.plan import Job, Step  # noqa: E402
from core.jobs import runner as job_runner  # noqa: E402
from core.jobs import store as job_store  # noqa: E402
from core.speech import delivery  # noqa: E402

PASSED = 0
FAILED = 0

SRC = (ROOT / "core/speech/delivery.py").read_text(encoding="utf-8")
PIPE = (ROOT / "core/routing/turn_pipeline.py").read_text(encoding="utf-8")


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print(f"PASS {name}")
    else:
        FAILED += 1
        print(f"FAIL {name}" + (f"  [{detail}]" if detail else ""))


def _approval(**kw) -> dict:
    base = {
        "status": "pending_approval",
        "kind": "plan",
        "target": "c1",
        "ack": "none",
        "reason": "",
        "platform_msg_id": "",
        "client_msg_id": "",
        "adapter": "",
        "detail": "job-abc 第 2 步需批准：订机票",
        "job_id": "job-abc",
        "step_n": 2,
        "action_hash": "0123456789abcdef",
        "who": "p1",
        "channel": "private",
    }
    base.update(kw)
    return base


class _Mode:
    """临时把 `_delivery_mode()` 钉成某一档。"""

    def __init__(self, mode: str) -> None:
        self.mode = mode
        self.orig = delivery._delivery_mode

    def __enter__(self):
        delivery._delivery_mode = lambda: self.mode
        return self

    def __exit__(self, *a):
        delivery._delivery_mode = self.orig
        return False


def test_receipt_shape() -> None:
    r = delivery.pending_approval_receipt(_approval())
    check("回执状态恒为第六态", r.status == PENDING_APPROVAL, r.status)
    check("ack=none 且两个消息号都空",
          r.ack == "none" and r.platform_msg_id == "" and r.client_msg_id == "",
          f"{r.ack}/{r.platform_msg_id}/{r.client_msg_id}")
    check("is_delivered() 为 False（不许算已发出）", r.is_delivered() is False, str(r.is_delivered()))
    check("target/kind/detail 原样承接",
          r.target == "c1" and r.kind == "plan" and "订机票" in r.detail, f"{r.target}/{r.kind}")
    long = delivery.pending_approval_receipt(_approval(detail="x" * 500))
    check("detail 截到 200", len(long.detail) == 200, str(len(long.detail)))
    bare = delivery.pending_approval_receipt({})
    check("空 dict 也不炸，kind 缺省 plan", bare.kind == "plan" and bare.status == PENDING_APPROVAL, bare.kind)


def test_three_modes() -> None:
    seen: list[Receipt] = []

    with _Mode("off"):
        out = delivery.deliver_pending_approvals([_approval()], turn_id="t1", ledger=seen.append)
    check("off 档返回空", out == [], str(out))
    check("off 档一条账都不落", seen == [], str(len(seen)))

    with _Mode("shadow"):
        out = delivery.deliver_pending_approvals([_approval(), _approval(step_n=3)],
                                                 turn_id="t1", ledger=seen.append)
    check("shadow 档条数与输入等长", len(out) == 2, str(len(out)))
    check("shadow 档全是第六态", all(r.status == PENDING_APPROVAL for r in out), str([r.status for r in out]))
    check("shadow 档不落账", seen == [], str(len(seen)))

    with _Mode("on"):
        out = delivery.deliver_pending_approvals([_approval(), _approval(step_n=3)],
                                                 turn_id="t1", ledger=seen.append)
    check("on 档逐条交给 ledger", len(seen) == 2, str(len(seen)))
    check("on 档回执仍全是第六态", all(r.status == PENDING_APPROVAL for r in out), "")
    with _Mode("on"):
        check("on 档没有 ledger 也不炸",
              delivery.deliver_pending_approvals([_approval()], turn_id="t1") != [])


def test_never_sends() -> None:
    sig = inspect.signature(delivery.deliver_pending_approvals)
    params = set(sig.parameters)
    check("签名里没有 registry/adapter/platform", not (params & {"registry", "adapter", "platform"}), str(sorted(params)))

    tail = SRC[SRC.index("def pending_approval_receipt"):]
    banned = [n for n in ("_send_once(", "adapter.send(", "_resolve(", "_guard(") if n in tail]
    check("实现里没有发送路径", banned == [], str(banned))

    before = (dict(delivery._SENT), dict(delivery._SENT_PER_TURN))
    with _Mode("on"):
        delivery.deliver_pending_approvals([_approval()], turn_id="t9")
    check("不写 _SENT（幂等键）", dict(delivery._SENT) == before[0], str(delivery._SENT))
    check("不写 _SENT_PER_TURN（回合配额）", dict(delivery._SENT_PER_TURN) == before[1],
          str(delivery._SENT_PER_TURN))


def test_end_to_end() -> None:
    engine = create_engine("sqlite://")
    job_store.ensure_schema(engine)
    st = job_store.JobStore(engine, mode=features.MODE_ON)
    job = Job(job_id="j1", chat_id="c1", goal="订机票",
              steps=[Step(n=1, action="订机票", effects="external_send")])
    st.create(job)
    res = job_runner.advance(job, execute=None, store=st)
    check("写操作步停在 need_approval", res.stop == "need_approval", res.stop)
    receipts = job_runner.pending_approval_receipts(res)
    check("生产者给出一条挂起回执", len(receipts) == 1, str(len(receipts)))
    landed: list[Receipt] = []
    with _Mode("on"):
        out = delivery.deliver_pending_approvals(receipts, turn_id="t1", ledger=landed.append)
    check("端到端：出口落一条第六态", len(landed) == 1 and landed[0].status == PENDING_APPROVAL,
          str([r.status for r in landed]))
    check("端到端：没有任何东西被算成已发出", all(not r.is_delivered() for r in out), "")


def test_wiring() -> None:
    check("turn_pipeline 调了 deliver_pending_approvals(", "deliver_pending_approvals(" in PIPE, "")
    check("回执落在 extra[\"job_approval_receipts\"]", 'extra["job_approval_receipts"]' in PIPE, "")
    i = PIPE.index('extra["job_approval_receipts"]')
    head = PIPE[:i]
    cond = head.rfind("mode == features.MODE_ON")
    check("只在 on 档装配（同一分支内）", cond != -1 and i - cond < 900, f"distance={i - cond}")
    check("_delivery_mode 真读 features.DELIVERY_CONTRACT", "features.DELIVERY_CONTRACT" in SRC, "")
    check("模块文档串已改成六种结果", "六种结果" in SRC and "五种结果" not in SRC, "")
    check("DELIVERY_CONTRACT 已进注册表", "delivery_contract" in Path(ROOT / "core/features.py").read_text(encoding="utf-8"), "")


def main() -> int:
    test_receipt_shape()
    test_three_modes()
    test_never_sends()
    test_end_to_end()
    test_wiring()
    print(f"RESULT {FAILED} failed / {PASSED + FAILED} checked")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
