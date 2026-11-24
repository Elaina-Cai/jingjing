"""U-10 第二刀：判给的 `plan` → 在手的事（台账里真有一行），以及 `pending_approval` 的生产者。

钉住的几条：
  * `JOB_LEDGER=off`（生产默认）**不读载荷、不碰台账、不开一件事**；
  * `shadow` 只校验 + 记日志，台账一个字节不写；
  * `on` 才开：步骤编号补全、effects 归一、**载荷里的 `approved` 一律抹成 False**
    （判不能自己批自己）；
  * 同一回合同一目标重复投递 = 同一件事（幂等），不会开两件；
  * 上限 / 坏载荷 / 台账异常都退化成「没开」，绝不把回合炸掉；
  * `pending_approval` 那条回执只在「停因 = need_approval」时产出。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.contracts.plan import MAX_JOBS_PER_CHAT, TOOL_CALLS_PER_JOB, Job  # noqa: E402
from core.jobs import ingest as I  # noqa: E402
from core.jobs import runner as R  # noqa: E402

FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"PASS {name}")
    else:
        FAILED.append(name)
        print(f"FAIL {name} {detail}")


class NoRead(dict):
    """一被读就炸：用来证明 off 档真的没碰载荷。"""

    def get(self, *args: object, **kwargs: object) -> object:  # noqa: D102
        raise AssertionError("off 档读了载荷")

    def __getitem__(self, key: object) -> object:  # noqa: D102
        raise AssertionError("off 档读了载荷")

    def __contains__(self, key: object) -> bool:  # noqa: D102
        raise AssertionError("off 档碰了载荷")


class FakeStore:
    def __init__(self, *, mode: str = "on", fail: BaseException | None = None) -> None:
        self._mode = mode
        self._fail = fail
        self.created: list[Job] = []
        self.loaded: list[str] = []
        self.jobs: dict[str, Job] = {}

    def enabled(self) -> bool:
        return self._mode != "off"

    def create(self, job: Job, *, actor: str = "judge") -> int:
        if self._fail is not None:
            raise self._fail
        self.created.append(job)
        self.jobs[job.job_id] = job
        return len(self.created)

    def load(self, job_id: str) -> Job | None:
        self.loaded.append(job_id)
        return self.jobs.get(job_id)

    def open_jobs(self, chat_id: str) -> list[Job]:
        return [j for j in self.jobs.values() if j.chat_id == chat_id]


def _payload(**plan: object) -> dict[str, object]:
    return {"decision": "respond", "plan": dict(plan)}


def main() -> int:
    # ---- 1. off：不读载荷、不碰台账 ----
    I._mode = lambda: "off"  # type: ignore[assignment]
    store = FakeStore()
    got = I.ingest_plan(NoRead({"plan": {"goal": "x"}}), chat_id="c1", store=store, trace_id="t1")
    check("off 档不开一件事", got is None, repr(got))
    check("off 档没写台账", store.created == [], str(store.created))

    # ---- 2. shadow：只校验，不写 ----
    I._mode = lambda: "shadow"  # type: ignore[assignment]
    store = FakeStore()
    got = I.ingest_plan(_payload(goal="订杭州的票", steps=[{"action": "web_search"}]),
                        chat_id="c1", store=store, trace_id="t1")
    check("shadow 档不开一件事", got is None, repr(got))
    check("shadow 档没写台账", store.created == [], str(store.created))

    # ---- 3. on：真开一件事 ----
    I._mode = lambda: "on"  # type: ignore[assignment]
    store = FakeStore()
    got = I.ingest_plan(
        _payload(goal="订杭州的票", steps=[
            {"action": "web_search", "effects": "read"},
            {"action": "buy", "effects": "external_send", "approved": True},
        ]),
        chat_id="c1", store=store, trace_id="t1",
    )
    check("on 档开出一件事", isinstance(got, Job), repr(got))
    check("on 档写了一条 created", len(store.created) == 1, str(len(store.created)))
    if isinstance(got, Job):
        check("步骤编号补成 1,2", [s.n for s in got.steps] == [1, 2], str([s.n for s in got.steps]))
        check("effects 保留", [s.effects for s in got.steps] == ["read", "external_send"],
              str([s.effects for s in got.steps]))
        check("载荷里的 approved 被抹成 False",
              all(s.approved is False for s in got.steps), str([s.approved for s in got.steps]))
        check("chat_id 落对", got.chat_id == "c1", got.chat_id)
        check("state 是 open", got.state == "open", got.state)
        check("job_id 带前缀", got.job_id.startswith("job-"), got.job_id)

    # ---- 4. 幂等：同一 (chat, trace, goal) 只有一件事 ----
    store = FakeStore()
    p = _payload(goal="订杭州的票", steps=[{"action": "web_search"}])
    a = I.ingest_plan(p, chat_id="c1", store=store, trace_id="t1")
    b = I.ingest_plan(p, chat_id="c1", store=store, trace_id="t1")
    check("幂等：只 create 一次", len(store.created) == 1, str(len(store.created)))
    check("幂等：两次拿到同一件", a is not None and b is not None and a.job_id == b.job_id,
          f"{getattr(a, 'job_id', None)} vs {getattr(b, 'job_id', None)}")

    # ---- 5. 坏载荷一律 None 且不写 ----
    store = FakeStore()
    for name, pl in [
        ("空目标", _payload(goal="   ", steps=[{"action": "x"}])),
        ("没有步骤", _payload(goal="目标")),
        ("未知 effects", _payload(goal="目标", steps=[{"action": "x", "effects": "抽卡"}])),
        ("动作空串", _payload(goal="目标", steps=[{"action": ""}])),
        ("步骤超上限", _payload(goal="目标", steps=[{"action": "x"}] * (TOOL_CALLS_PER_JOB + 1))),
        ("plan 不是 dict", {"plan": "订票"}),
        ("根本没 plan", {"decision": "respond"}),
    ]:
        got = I.ingest_plan(pl, chat_id="c1", store=store, trace_id="t1")
        check(f"坏载荷拒收：{name}", got is None, repr(got))
    check("坏载荷一条都没写", store.created == [], str(len(store.created)))

    # ---- 6. 上限 / 台账异常：退化成「没开」，不抛 ----
    I._mode = lambda: "on"  # type: ignore[assignment]
    from core.jobs.store import JobLimitExceeded

    got = I.ingest_plan(_payload(goal="第四件", steps=[{"action": "x"}]),
                        chat_id="c1", store=FakeStore(fail=JobLimitExceeded("满了")), trace_id="t9")
    check("超上限不抛、返回 None", got is None, repr(got))
    got = I.ingest_plan(_payload(goal="炸了", steps=[{"action": "x"}]),
                        chat_id="c1", store=FakeStore(fail=RuntimeError("boom")), trace_id="t8")
    check("台账异常不抛、返回 None", got is None, repr(got))
    check("上限常量没被改动", MAX_JOBS_PER_CHAT == 3, str(MAX_JOBS_PER_CHAT))

    # ---- 7. pending_approval 的生产者 ----
    job = Job(job_id="job-abc", chat_id="c1", goal="订票",
              steps=[R.Step(n=1, action="buy", effects="external_send")])
    need = R.AdvanceResult(job=job, stop="need_approval",
                           ran=(R.StepRun(n=1, action="buy", status="need_approval"),))
    done = R.AdvanceResult(job=job, stop="done")
    reqs = R.pending_approval_receipts(need)
    check("need_approval → 一条回执", len(reqs) == 1, str(len(reqs)))
    if reqs:
        r = reqs[0]
        check("回执 status=pending_approval", r.get("status") == "pending_approval", str(r.get("status")))
        check("回执带 job_id / step_n", r.get("job_id") == "job-abc" and r.get("step_n") == 1, str(r))
        check("回执带 action_hash", bool(r.get("action_hash")), str(r.get("action_hash")))
        check("回执 target 是会话", r.get("target") == "c1", str(r.get("target")))
        check("回执 ack=none（一个字节没发）", r.get("ack") == "none", str(r.get("ack")))
    check("done → 零条回执", R.pending_approval_receipts(done) == [], str(R.pending_approval_receipts(done)))
    check("回执里的 status 在出口六态里", reqs and reqs[0].get("status") in
          ("sent", "skipped", "blocked", "failed", "partial", "pending_approval"), "n/a")

    # ---- 8. 判的载荷落地：audit_verdict_contract 把载荷交给 ingester（行为层）----
    import os

    import agents.conversational as conv
    from core.jobs import ingest as ingest_mod

    def _run_contract(payload, turn_id="t-1", chat_id="c1"):
        """跑一次判决落地，返回 (返回值, ingester 收到的调用)。"""
        seen_inner = []
        saved = ingest_mod.ingest_plan
        try:
            ingest_mod.ingest_plan = lambda payload, **kw: seen_inner.append((payload, kw))
            returned = conv.audit_verdict_contract(
                payload=payload, turn_id=turn_id, chat_id=chat_id,
            )
        finally:
            ingest_mod.ingest_plan = saved
        return returned, seen_inner

    verdict_payload = {
        "decision": "respond",
        "deliver": [{"kind": "say", "text": "好"}],
        "basis": ["w#3"],
        "reading": "他们在聊作业",
    }
    saved_flag = os.environ.get("YINYUE_FEATURE_VERDICT_CONTRACT")
    os.environ["YINYUE_FEATURE_VERDICT_CONTRACT"] = "shadow"
    try:
        _returned, _seen = _run_contract(verdict_payload)
        check("判决载荷落地调了 ingest_plan", len(_seen) == 1, str(len(_seen)))
        if _seen:
            _payload_seen, _kw_seen = _seen[0]
            check("落地的是同一份载荷", _payload_seen is verdict_payload, "载荷对象被换了")
            check("落地带了会话与回合号",
                  _kw_seen.get("chat_id") == "c1" and _kw_seen.get("trace_id") == "t-1",
                  str(_kw_seen))

        # 开一件事失败不许影响判决（:5216 的 except 是契约，不是防御噪音）
        def _boom(payload, **kw):
            raise RuntimeError("ingest 炸了")

        saved_ingest = ingest_mod.ingest_plan
        try:
            ingest_mod.ingest_plan = _boom
            _survived = conv.audit_verdict_contract(
                payload=verdict_payload, turn_id="t-2", chat_id="c1",
            )
        finally:
            ingest_mod.ingest_plan = saved_ingest
        check("ingest 炸了也照常返回判决", getattr(_survived, "ok", False) is True, str(_survived))

        # 不合契约的载荷不开事；空载荷连档位都不读
        _r, _seen_bad = _run_contract({"decision": "???"}, turn_id="t-3")
        check("不合契约的载荷不开事", _seen_bad == [], str(_seen_bad))
        check("空载荷直接返回 None",
              conv.audit_verdict_contract(payload=None) is None, "空载荷返回了东西")
    finally:
        if saved_flag is None:
            os.environ.pop("YINYUE_FEATURE_VERDICT_CONTRACT", None)
        else:
            os.environ["YINYUE_FEATURE_VERDICT_CONTRACT"] = saved_flag

    # tick 侧仍是静态锁：`core/routing/turn_pipeline.py` 的手段不属本轮（N3 归出去）
    pipe = (ROOT / "core/routing/turn_pipeline.py").read_text(encoding="utf-8")
    check("tick 里有 job_approvals", "job_approvals" in pipe, "没找到")
    check("job_approvals 只在 on 档写",
          pipe.count('extra["job_approvals"]') >= 1 and "mode == features.MODE_ON" in pipe, "没拿到档位判断")

    print(f"RESULT {len(FAILED)} failed")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
