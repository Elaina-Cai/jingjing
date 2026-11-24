"""审批入口：人回一句「可以」→ Approval 落库 → 写操作解冻。

对应 `docs/decisions/办_在手的事.md` §17 与 U-10 切片里的
`apply_approval(...) / parse_approval(...) / record_approval(...)`。
"""

from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import create_engine

from core.contracts.plan import Approval, Awaiting, Job, Step
from core.jobs import approval as ap
from core.jobs.runner import step_action_hash
from core.jobs.store import JobStore, ensure_schema


def _engine():
    engine = create_engine("sqlite://", future=True)
    ensure_schema(engine)
    return engine


def _job(job_id="job-aaa", chat_id="c1", *, owner=None):
    return Job(
        job_id=job_id,
        chat_id=chat_id,
        goal="帮我订下周三去杭州的机票",
        owner_person_id=owner,
        steps=[Step(n=1, action="web_search:杭州机票", effects="read")],
    )


def _waiting_store(engine, *jobs):
    store = JobStore(engine, mode="on")
    for job in jobs:
        store.create(job)
        store.append(job.job_id, "awaiting", payload={"kind": "approval"}, actor="judge")
    return store


def main() -> int:
    failed = 0

    def check(name, cond, detail=""):
        nonlocal failed
        if not cond:
            failed += 1
        print(("PASS " if cond else "FAIL ") + name + ("" if not detail else "  :: " + str(detail)))

    ap._mode = lambda: "on"  # 单测里直接开 on 档；env 三档另有用例

    engine = _engine()
    store = _waiting_store(engine, _job())
    job = store.load("job-aaa")
    check("等批准的状态读回来", job.state == "awaiting" and job.awaiting.kind == "approval",
          (job.state, job.awaiting.kind))

    step = job.next_step()
    approval = ap.parse_approval("可以", job, is_private=True)
    check("私聊一句「可以」→ 一行批准", isinstance(approval, Approval))
    check("批的是动作不是句子",
          approval is not None and approval.action_hash == step_action_hash(job.job_id, step),
          getattr(approval, "action_hash", None))
    check("步号就是下一步的步号", approval is not None and approval.step_n == step.n)
    check("通道写 private", approval is not None and approval.channel == "private")
    check("批准带过期时间", approval is not None and bool(approval.expires_at))

    check("群里说「可以」不算授权", ap.parse_approval("可以", job, is_private=False) is None)
    check("「不用了」不批", ap.parse_approval("不用了", job, is_private=True) is None)
    check("「可以，但是先别订」不批（否定优先）",
          ap.parse_approval("可以，但是先别订", job, is_private=True) is None)
    check("长句不批（>40 字）",
          ap.parse_approval("可以是可以不过我下周三才放假而且机票我已经看好了你就别管了", job, is_private=True) is None)
    check("没有明确同意词不批",
          ap.parse_approval("帮我规划下周去杭州", job, is_private=True) is None)
    check("空文本不批", ap.parse_approval("", job, is_private=True) is None)
    check("单字「好」算批", isinstance(ap.parse_approval("好", job, is_private=True), Approval))

    other_owner = replace(
        _job("job-bbb", owner="p-owner"), state="awaiting", awaiting=Awaiting(kind="approval")
    )
    check("不是本人不批",
          ap.parse_approval("可以", other_owner, is_private=True, speaker_id="p-else") is None)
    check("是本人就批",
          isinstance(ap.parse_approval("可以", other_owner, is_private=True, speaker_id="p-owner"), Approval))

    open_job = _job("job-ccc")
    check("没在等人的事不批", ap.parse_approval("可以", open_job, is_private=True) is None)

    ok = ap.record_approval(job, approval, store=store)
    check("record_approval 落库成功", ok is True)
    check("is_approved 认这一行",
          store.is_approved(job.job_id, approval.step_n, approval.action_hash) is True)
    thawed = store.load(job.job_id)
    check("批准后「在等」被解开", thawed.state == "open" and thawed.awaiting.kind == "none",
          (thawed.state, thawed.awaiting.kind))
    check("留了一条 approved 事件", "approved" in store.event_kinds(job.job_id),
          store.event_kinds(job.job_id))
    check("重复批不造第二行", ap.record_approval(job, approval, store=store) is False)

    # apply_approval：唯一一件在等 → 批；两件在等 → 谁都不批
    engine2 = _engine()
    store2 = _waiting_store(engine2, _job("job-ddd", "c2"))
    got = ap.apply_approval("可以", chat_id="c2", is_private=True, store=store2)
    check("apply_approval 批了一件", len(got) == 1, len(got))
    check("apply_approval 之后解冻", store2.load("job-ddd").state == "open")

    engine3 = _engine()
    store3 = _waiting_store(engine3, _job("job-e1", "c3"), _job("job-e2", "c3"))
    got3 = ap.apply_approval("可以", chat_id="c3", is_private=True, store=store3)
    check("两件在等 → 一句不批（fail closed）", got3 == [], got3)
    check("两件都没被批",
          not store3.is_approved("job-e1", 1, step_action_hash("job-e1", store3.load("job-e1").steps[0]))
          and not store3.is_approved("job-e2", 1, step_action_hash("job-e2", store3.load("job-e2").steps[0])))

    got4 = ap.apply_approval("可以", chat_id="nowhere", is_private=True, store=store3)
    check("没有在等的事 → 不批", got4 == [])
    check("空文本 → 不批",
          ap.apply_approval("   ", chat_id="c2", is_private=True, store=store2) == [])

    saved = ap._mode
    ap._mode = lambda: "off"
    engine4 = _engine()
    store4 = _waiting_store(engine4, _job("job-fff", "c4"))
    check("off 档一行都不写",
          ap.apply_approval("可以", chat_id="c4", is_private=True, store=store4) == [])
    check("off 档台账没被动过",
          store4.load("job-fff").awaiting.kind == "approval")
    ap._mode = saved

    # 行为接线：`_job_tick` 必须把原话与私聊标记喂给 `apply_approval`
    # （`core/routing/turn_pipeline.py:420-426`）。原来这里读源码找
    # `apply_approval(` / `approval_text=` / `is_private=` 三个字面量——后两个
    # 就写在 `_job_tick` 的签名上，把整段审批分支删了它们仍然是绿的。
    from types import SimpleNamespace

    from core import features
    from core.jobs import runner as runner_mod
    from core.jobs import store as store_mod
    from core.routing import turn_pipeline as tp

    class _NullStore:
        def enabled(self):
            return True

        def open_jobs(self, chat_id):
            return []

        def readouts(self, chat_id, limit=3):
            return []

    def _tick(**kw):
        seen: list = []
        old = {
            "mode": features.mode,
            "default_store": store_mod.default_store,
            "advance": runner_mod.advance,
            "apply_approval": ap.apply_approval,
            "apply_grant": ap.apply_grant,
        }
        features.mode = lambda name, default=None: (
            features.MODE_ON if name == features.JOB_LEDGER else default)
        store_mod.default_store = lambda *a, **k: _NullStore()
        runner_mod.advance = lambda job, **a: SimpleNamespace(
            stop="no_executor", ran=(), job=job)
        ap.apply_approval = lambda text, **a: (seen.append((text, a)), [])[1]
        ap.apply_grant = lambda text, **a: []
        extra: dict = {}
        try:
            tp._job_tick(chat_id="c1", extra=extra, **kw)
        finally:
            features.mode = old["mode"]
            store_mod.default_store = old["default_store"]
            runner_mod.advance = old["advance"]
            ap.apply_approval = old["apply_approval"]
            ap.apply_grant = old["apply_grant"]
        return extra, seen

    extra, seen = _tick(approval_text="可以", is_private=True, speaker_id="p1")
    check("_job_tick 把原话喂给 apply_approval", len(seen) == 1, seen)
    check("原话原样传到", bool(seen) and seen[0][0] == "可以",
          seen[0][0] if seen else None)
    passed = seen[0][1] if seen else {}
    check("私聊标记传到", passed.get("is_private") is True, passed.get("is_private"))
    check("说话人传到", passed.get("speaker_id") == "p1", passed.get("speaker_id"))
    check("台账用的是 default_store()", passed.get("store") is not None,
          passed.get("store"))
    check("没有原话就不碰审批", _tick(trace_id="t1")[1] == [], seen)

    print(f"RESULT {failed} failed / {39 - failed} checked")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
