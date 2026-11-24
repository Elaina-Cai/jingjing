# -*- coding: utf-8 -*-
"""三级授权：自己动手 / 长期授权 / 逐次点头。

跑法：``python -X utf8 -u tests/test_job_authorization.py``

钉三件事：
1. 档位由 `effects` 决定（换名字绕不开）；
2. `grant` 必须有凭据（`Policy.approved_by` 非空 + 未过期 + 在 scope 内 + 不超金额），
   否则退化成 `confirm` —— 默认拒绝；
3. 停下来的回执要说清是哪一档、卡在哪（`needs_confirm` / `no_grant` / `grant_expired` ...）。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.contracts.plan import Awaiting, Job, Plan, Policy, Step  # noqa: E402
from core.jobs import policy as pol  # noqa: E402
from core.jobs.runner import advance, pending_approval_receipts  # noqa: E402

FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"ok   {name}")
    else:
        print(f"FAIL {name} :: {detail}")
        FAILED.append(name)


class FakeStore:
    def __init__(self, *, approved: bool = False) -> None:
        self.events: list[tuple[str, str, dict]] = []
        self._approved = approved

    def append(self, job_id: str, kind: str, payload=None, *, actor: str = "") -> int:
        self.events.append((job_id, kind, dict(payload or {})))
        return len(self.events)

    def is_approved(self, job_id: str, step_n: int, action_hash: str) -> bool:
        return self._approved

    def last(self, kind: str) -> dict:
        rows = [p for _, k, p in self.events if k == kind]
        return rows[-1] if rows else {}


def _job(step: Step, *, policy: Policy | None = None) -> Job:
    return Job(
        job_id="job-a",
        chat_id="chat-a",
        goal="把行程写成文件",
        owner_person_id="p1",
        steps=list(Plan(goal="g", steps=[step]).steps),
        policy=policy or Policy(),
        awaiting=Awaiting(),
    )


def _ok_executor(step):
    return {"ok": True, "detail": "wrote", "state_before": "", "state_after": "h1",
            "changed": {"path": "data/workspace/notes/x.md", "after": "h1"}}


def main() -> int:
    # ---------- 1. 档位由 effects 决定 ----------
    check("read 是 auto", pol.effect_tier("read") == pol.TIER_AUTO, pol.effect_tier("read"))
    check("write_local 是 grant", pol.effect_tier("write_local") == pol.TIER_GRANT, "")
    check("ui_control 是 confirm", pol.effect_tier("ui_control") == pol.TIER_CONFIRM, "")
    check("external_send 是 confirm", pol.effect_tier("external_send") == pol.TIER_CONFIRM, "")
    check("未知 effect 最严", pol.effect_tier("whatever") == pol.TIER_CONFIRM, "")
    check("只有三档", pol.TIERS == ("auto", "grant", "confirm"), str(pol.TIERS))

    # ---------- 2. grant 的四个条件 ----------
    step = Step(n=1, action="write_note:行程", effects="write_local")
    check("没有 policy 就不算授权", pol.grant_allows(None, step=step) == (False, pol.REASON_NO_GRANT), "")
    check("没人批过就不算授权",
          pol.grant_allows(Policy(scope="write_local"), step=step) == (False, pol.REASON_NO_GRANT), "")
    ok_grant = Policy(scope="write_local", approved_by="私聊:主人")
    check("人批过 + 在 scope 内 = 放行", pol.grant_allows(ok_grant, step=step) == (True, ""), "")
    check("scope 写工具名也行",
          pol.grant_allows(Policy(scope="write_note", approved_by="私聊:主人"), step=step) == (True, ""), "")
    check("scope 通配 * 也行",
          pol.grant_allows(Policy(scope="*", approved_by="私聊:主人"), step=step) == (True, ""), "")
    check("空 scope 什么都不覆盖",
          pol.grant_allows(Policy(scope="", approved_by="私聊:主人"), step=step)
          == (False, pol.REASON_OUT_OF_SCOPE), "")
    check("过期了就不算",
          pol.grant_allows(Policy(scope="write_local", approved_by="私聊:主人",
                                  expires_at="2020-01-01T00:00:00+00:00"), step=step)
          == (False, pol.REASON_GRANT_EXPIRED), "")
    check("超金额就不算",
          pol.grant_allows(Policy(scope="write_local", approved_by="私聊:主人", max_amount=100),
                           step=step, amount=101) == (False, pol.REASON_OVER_AMOUNT), "")

    # ---------- 3. authorize_step ----------
    check("read 直接放行", pol.authorize_step(Step(n=1, action="web_search:x")) == (True, "auto", ""), "")
    check("write_local 没授权就停下",
          pol.authorize_step(step) == (False, "grant", pol.REASON_NO_GRANT), "")
    check("write_local 有授权就放行", pol.authorize_step(step, policy=ok_grant) == (True, "grant", ""), "")
    check("ui_control 永远逐次",
          pol.authorize_step(Step(n=1, action="click:确定", effects="ui_control"), policy=ok_grant)
          == (False, "confirm", pol.REASON_NEEDS_CONFIRM), "")
    check("needs_human 与 authorize 一致", pol.needs_human(step) is True and pol.needs_human(step, policy=ok_grant) is False, "")

    # ---------- 4. 推进器怎么用这三档 ----------
    store = FakeStore()
    run = advance(_job(step), execute=_ok_executor, store=store, allow_effects=("read",),
                  authorize=lambda s: pol.authorize_step(s)[:2])
    check("没授权：停 need_approval", run.stop == "need_approval", run.stop)
    check("没授权：tier 记成 grant", run.tier == "grant", run.tier)
    check("没授权：台账写明卡在 no_grant", store.last("awaiting").get("tier") == "grant", store.last("awaiting"))
    check("没授权：一步都没跑", run.ran and run.ran[0].status == "need_approval", run.ran)
    receipt = pending_approval_receipts(run)
    check("回执带 tier", receipt and receipt[0].get("tier") == "grant", receipt)
    check("回执 reason 说清卡在哪", receipt and receipt[0].get("reason") == "needs_grant", receipt)

    store2 = FakeStore()
    run2 = advance(_job(step, policy=ok_grant), execute=_ok_executor, store=store2,
                   allow_effects=("read",),
                   authorize=lambda s: pol.authorize_step(s, policy=ok_grant)[:2])
    check("有长期授权：自己就办了", run2.stop == "done", run2.stop)
    check("有长期授权：落 step_done", any(k == "step_done" for _, k, _ in store2.events),
          [k for _, k, _ in store2.events])

    ui_step = Step(n=1, action="click:确认下单", effects="ui_control", safe_point=False)
    store3 = FakeStore()
    run3 = advance(_job(ui_step, policy=ok_grant), execute=_ok_executor, store=store3,
                   allow_effects=("read",),
                   authorize=lambda s: pol.authorize_step(s, policy=ok_grant)[:2])
    check("ui_control 有长期授权也要点头", run3.stop == "need_approval", run3.stop)
    check("ui_control 的 tier 是 confirm", run3.tier == "confirm", run3.tier)

    # 人逐次批过这一步：比任何长期授权都硬
    store4 = FakeStore(approved=True)
    run4 = advance(_job(step), execute=_ok_executor, store=store4, allow_effects=("read",),
                   authorize=lambda s: (False, "confirm"))
    check("逐次批准压过授权闸的拒绝", run4.stop == "done", run4.stop)

    # 授权判定自己炸了：当没授权，停
    class Boom:
        def __call__(self, step_):
            raise RuntimeError("policy boom")

    store5 = FakeStore()
    run5 = advance(_job(step), execute=_ok_executor, store=store5, allow_effects=("read",), authorize=Boom())
    check("授权判定异常 = 没授权", run5.stop == "need_approval", run5.stop)
    check("异常也写进 tier", run5.tier.startswith("authorize_error:"), run5.tier)

    # ---------- 5. 行为接线：回合把 authorize 钩子交给推进器 ----------
    # 原来这里读源码找 `policy.authorize_step(` + `authorize=_authorize` 与
    # `allow_effects=(READ_EFFECT,)`——`_job_tick` 一改结构就红，而它们想保的
    # 「没人批过的写步必须拦、有长期授权才放行」在下面直接测得到。
    import types as _types

    from core import features
    from core.jobs import executor as ex_mod
    from core.jobs import runner as runner_mod
    from core.jobs import store as store_mod
    from core.routing import turn_pipeline as tp

    class _Registry:
        def get(self, tool):
            return None

        def act(self, target, action, params=None):
            return _types.SimpleNamespace(
                status="ok", content="写了", truncated=False,
                state_before="h0", state_after="h1", changed={"path": "x"},
            )

        def observe(self, target, what, params=None):
            return _types.SimpleNamespace(
                status="ok", content="ok", truncated=False,
                state_before="", state_after="", changed={},
            )

    class _Job:
        def __init__(self, policy=None):
            self.job_id = "job-a"
            self.policy = policy

    def _authorize_of(policy=None):
        """跑一次 `_job_tick`（on 档、台账空转），把推进器收到的 authorize 钩子捞回来。"""
        calls: list = []
        job = _Job(policy)

        class _Store:
            def enabled(self):
                return True

            def open_jobs(self, chat_id):
                return [job]

            def readouts(self, chat_id, limit=3):
                return []

        old = {
            "mode": features.mode,
            "default_store": store_mod.default_store,
            "advance": runner_mod.advance,
            "default_registry": ex_mod.default_registry,
            "external_sender": tp._external_sender,
            "correction_sender": tp._correction_sender,
        }
        features.mode = lambda name, default=None: (
            features.MODE_ON if name == features.JOB_LEDGER else default)
        store_mod.default_store = lambda *a, **k: _Store()
        runner_mod.advance = lambda j, **kw: (
            calls.append((j, kw)), _types.SimpleNamespace(stop="no_executor", ran=(), job=j))[1]
        ex_mod.default_registry = lambda: _Registry()
        tp._external_sender = lambda: None
        tp._correction_sender = lambda: None
        try:
            tp._job_tick(chat_id="chat-a", extra={"chat_id": "chat-a"})
        finally:
            features.mode = old["mode"]
            store_mod.default_store = old["default_store"]
            runner_mod.advance = old["advance"]
            ex_mod.default_registry = old["default_registry"]
            tp._external_sender = old["external_sender"]
            tp._correction_sender = old["correction_sender"]
        return (calls[0][1].get("authorize"), calls[0][1]) if calls else (None, {})

    auth, kw = _authorize_of()
    check("回合把授权钩子交给推进器", callable(auth), auth)
    check("免批准名单只有 read", tuple(kw.get("allow_effects") or ()) == ("read",),
          kw.get("allow_effects"))
    check("读步自动放行", auth(Step(n=1, action="web_search:x", effects="read")) == (True, pol.TIER_AUTO), "")
    check("写步没人批过就拦",
          auth(Step(n=1, action="write_note:x", effects="write_local"))[0] is False, "")
    auth2, _kw2 = _authorize_of(Policy(scope="write_local", approved_by="私聊:主人"))
    check("有长期授权就自己办",
          auth2(Step(n=1, action="write_note:x", effects="write_local")) == (True, pol.TIER_GRANT), "")

    print(f"RESULT {len(FAILED)} failed")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
