"""长期授权入口：人说「这类事以后你自己办」→ 落一行 `granted` → 同档步骤不再问人。

对应 `docs/decisions/办_在手的事.md` §8.1（三级授权）与 §8.2（授权从哪来），
以及 U-10 切片里的 `parse_grant(...) / record_grant(...) / apply_grant(...)`。
"""

from __future__ import annotations

import sys
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import create_engine

from core.contracts.plan import Awaiting, Job, Policy, Step
from core.jobs import approval as ap
from core.jobs import policy as pol
from core.jobs.runner import AdvanceResult, advance
from core.jobs.store import JobStore, ensure_schema


def _engine():
    engine = create_engine("sqlite://", future=True)
    ensure_schema(engine)
    return engine


def _write_job(job_id="job-w1", chat_id="c1", *, owner=None, effects="write_local", action=None):
    return Job(
        job_id=job_id,
        chat_id=chat_id,
        goal="把旅行计划写成文件",
        owner_person_id=owner,
        steps=[
            Step(
                n=1,
                action=action or "write_note:旅行计划",
                effects=effects,
            )
        ],
    )


def _store(engine, *jobs):
    store = JobStore(engine, mode="on")
    for job in jobs:
        store.create(job)
    return store


class _NullStore:
    """`_job_tick` 只用到这里三样：开没开、开着的活、读数。"""

    def enabled(self):
        return True

    def open_jobs(self, chat_id):
        return []

    def readouts(self, chat_id, limit=3):
        return []


def _tick_grant(line, *, chat_id="c1", is_private=True, speaker_id="p1"):
    """跑一次 `_job_tick`（on 档、台账空转），返回 ``(extra, 喂给 apply_grant 的实参)``。

    `apply_grant` 换成假实现，所以这里验的是**接线**：原话与私聊标记有没有传进去、
    返回的 scope 有没有写进 ``extra["job_grant_scope"]``。
    """

    from core import features
    from core.jobs import approval as ap_mod
    from core.jobs import runner as runner_mod
    from core.jobs import store as store_mod
    from core.routing import turn_pipeline as tp

    seen: list = []
    orig = {
        "mode": features.mode,
        "default_store": store_mod.default_store,
        "advance": runner_mod.advance,
        "apply_approval": ap_mod.apply_approval,
        "apply_grant": ap_mod.apply_grant,
    }
    features.mode = lambda name, default=None: (
        features.MODE_ON if name == features.JOB_LEDGER else default
    )
    store_mod.default_store = lambda *a, **k: _NullStore()
    runner_mod.advance = lambda job, **kw: SimpleNamespace(
        stop="no_executor", ran=(), job=job
    )
    ap_mod.apply_approval = lambda text, **kw: []  # 这句不是点头某一步
    ap_mod.apply_grant = lambda text, **kw: (
        seen.append((text, kw)),
        [Policy(scope="write_local", approved_by="私聊:p1")],
    )[1]
    extra: dict = {}
    try:
        tp._job_tick(
            chat_id=chat_id,
            extra=extra,
            approval_text=line,
            is_private=is_private,
            speaker_id=speaker_id,
        )
    finally:
        features.mode = orig["mode"]
        store_mod.default_store = orig["default_store"]
        runner_mod.advance = orig["advance"]
        ap_mod.apply_approval = orig["apply_approval"]
        ap_mod.apply_grant = orig["apply_grant"]
    return extra, seen


def main() -> int:
    failed = 0

    def check(name, cond, detail=""):
        nonlocal failed
        if not cond:
            failed += 1
        print(("PASS " if cond else "FAIL ") + name + ("" if not detail else "  :: " + str(detail)))

    ap._mode = lambda: "on"  # 单测直接开 on 档

    engine = _engine()
    store = _store(engine, _write_job())
    job = store.load("job-w1")

    line = "以后这类事你自己办"
    granted = ap.parse_grant(line, job, is_private=True, speaker_id="p1")
    check("私聊说「以后这类事你自己办」→ 一行长期授权", isinstance(granted, Policy), granted)
    check("scope 就是在等那一步的 effect",
          granted is not None and granted.scope == "write_local", getattr(granted, "scope", None))
    check("凭据写清是谁给的",
          granted is not None and granted.approved_by == "私聊:p1", getattr(granted, "approved_by", None))
    check("可撤销", granted is not None and granted.revocable is True)
    check("默认 30 天过期",
          granted is not None
          and abs(
              datetime.fromisoformat(granted.expires_at)
              - (datetime.now() + timedelta(days=ap.GRANT_TTL_DAYS))
          ).total_seconds()
          < 120,
          getattr(granted, "expires_at", None))
    check("没有金额上限（写本地文件不花钱）", granted is not None and granted.max_amount is None)

    check("群里说「以后你自己办」不算授权",
          ap.parse_grant(line, job, is_private=False) is None)
    check("否定优先：「以后这类事不用问我」不算授权",
          ap.parse_grant("以后这类事不用问我", job, is_private=True) is None)
    check("否定优先：「以后别问我」不算授权",
          ap.parse_grant("以后别问我", job, is_private=True) is None)
    check("没有长期语义（「可以」）不算授权",
          ap.parse_grant("可以", job, is_private=True) is None)
    check("长句不算（>40 字）",
          ap.parse_grant(
              "以后这类事你自己办就行了我这段时间要出差可能一周都不在你有事直接处理别等我回来再问",
              job,
              is_private=True,
          )
          is None)
    check("空文本不算", ap.parse_grant("", job, is_private=True) is None)

    owner_job = _write_job("job-w2", owner="p-owner")
    check("不是本人不许授权",
          ap.parse_grant(line, owner_job, is_private=True, speaker_id="p-else") is None)
    check("是本人可以授权",
          isinstance(ap.parse_grant(line, owner_job, is_private=True, speaker_id="p-owner"), Policy))

    ui_job = _write_job("job-w3", effects="ui_control", action="ui_click:下单")
    check("confirm 档（动别人界面）不接受一次性升级",
          ap.parse_grant(line, ui_job, is_private=True) is None)
    send_job = _write_job("job-w4", effects="external_send", action="send:发给张三")
    check("external_send 永不授权", ap.parse_grant(line, send_job, is_private=True) is None)
    read_job = _write_job("job-w5", effects="read", action="web_search:杭州")
    check("read 本来就免批准，不需要授权", ap.parse_grant(line, read_job, is_private=True) is None)
    empty_job = replace(_write_job("job-w6"), steps=[])
    check("没有步骤就不存在「这类事」", ap.parse_grant(line, empty_job, is_private=True) is None)

    ok = ap.record_grant(job, granted, store=store)
    check("record_grant 落库成功", ok is True)
    check("留了一条 granted 事件", "granted" in store.event_kinds("job-w1"),
          store.event_kinds("job-w1"))
    g_job = store.load("job-w1")
    check("投影把 policy 换成了长期授权",
          g_job.policy.approved_by == "私聊:p1" and g_job.policy.scope == "write_local",
          (g_job.policy.approved_by, g_job.policy.scope))
    check("授权不改「在等」的状态（它只决定要不要问）",
          g_job.state == "open" and g_job.awaiting.kind in ("", "none"),
          (g_job.state, g_job.awaiting.kind))
    check("authorize_step 认这份授权",
          pol.authorize_step(g_job.steps[0], policy=g_job.policy)[:2] == (True, pol.TIER_GRANT))

    # apply_grant：唯一候选 → 落库；两个候选 → 谁都不给
    engine2 = _engine()
    store2 = _store(engine2, _write_job("job-g1", "c2"))
    got = ap.apply_grant(line, chat_id="c2", is_private=True, speaker_id="p1", store=store2)
    check("apply_grant 落一行并返回", len(got) == 1 and got[0].scope == "write_local", got)
    check("落库后读得回来", store2.load("job-g1").policy.approved_by == "私聊:p1")

    engine3 = _engine()
    store3 = _store(engine3, _write_job("job-h1", "c3"), _write_job("job-h2", "c3"))
    got3 = ap.apply_grant(line, chat_id="c3", is_private=True, speaker_id="p1", store=store3)
    check("两件事在等 → 一句话谁都不授权", got3 == [], got3)
    check("两件事的 policy 都没被改",
          store3.load("job-h1").policy.approved_by is None
          and store3.load("job-h2").policy.approved_by is None)

    got4 = ap.apply_grant(line, chat_id="nowhere", is_private=True, speaker_id="p1", store=store3)
    check("没有在手的事 → 不授权", got4 == [])
    check("空文本 → 不授权",
          ap.apply_grant("   ", chat_id="c2", is_private=True, store=store2) == [])

    saved = ap._mode
    ap._mode = lambda: "off"
    engine5 = _engine()
    store5 = _store(engine5, _write_job("job-off", "c5"))
    check("off 档一行都不写",
          ap.apply_grant(line, chat_id="c5", is_private=True, store=store5) == [])
    check("off 档 policy 没被动", store5.load("job-off").policy.approved_by is None)
    ap._mode = lambda: "shadow"
    check("shadow 档也不写（只写档才落库）",
          ap.apply_grant(line, chat_id="c5", is_private=True, store=store5) == [])
    ap._mode = saved

    # 端到端：授权前停在等人，授权后同一步自己跑完
    engine6 = _engine()
    store6 = _store(engine6, _write_job("job-e1", "c6"))
    bare = store6.load("job-e1")
    ran: list[str] = []

    def execute(step):
        ran.append(step.action)
        return {"ok": True, "detail": "写了 1 个文件", "changed": {"path": "notes/x.md"}}

    blocked = advance(
        bare,
        execute=execute,
        store=store6,
        authorize=lambda step: pol.authorize_step(step, policy=bare.policy),
    )
    check("没有授权 → 停在等人点头", isinstance(blocked, AdvanceResult) and blocked.stop == "need_approval", blocked.stop)
    check("没授权时执行器一次都没跑", ran == [], ran)

    ap.apply_grant(line, chat_id="c6", is_private=True, speaker_id="p1", store=store6)
    granted_job = store6.load("job-e1")
    check("授权同时把正在等的那一步解开",
          granted_job.state == "open" and granted_job.awaiting.kind in ("", "none"),
          (granted_job.state, granted_job.awaiting.kind))
    check("解冻也是留了证据的（approved 事件）",
          "approved" in store6.event_kinds("job-e1"), store6.event_kinds("job-e1"))
    done = advance(
        granted_job,
        execute=execute,
        store=store6,
        authorize=lambda step: pol.authorize_step(step, policy=granted_job.policy),
    )
    check("授权后同一步自己跑完", done.stop == "done" and done.job.state == "done", (done.stop, done.job.state))
    check("执行器真的跑了一次", ran == ["write_note:旅行计划"], ran)
    check("台账留了 step_done 与 done",
          "step_done" in store6.event_kinds("job-e1") and "done" in store6.event_kinds("job-e1"),
          store6.event_kinds("job-e1"))

    # 投影真落库：新开连接再读一次，证明 policy 进了库、不是只活在内存投影里
    engine_re = _engine()
    store_re = _store(engine_re, _write_job("job-r1", "c7"))
    ap.apply_grant(line, chat_id="c7", is_private=True, speaker_id="p1", store=store_re)
    reopened = JobStore(engine_re, mode="on")
    check("重开连接后长期授权还在",
          reopened.load("job-r1").policy.approved_by == "私聊:p1",
          reopened.load("job-r1").policy.approved_by)
    check("重开连接后 granted 事件还在", "granted" in reopened.event_kinds("job-r1"),
          reopened.event_kinds("job-r1"))

    # 行为接线：`_job_tick` 必须把「以后这类事你自己办」喂给 `apply_grant`，
    # 并把 scope 写成 extra 里的回执（`core/routing/turn_pipeline.py:434-442`）。
    extra, seen = _tick_grant(line)
    check("_job_tick 把原话喂给 apply_grant", len(seen) == 1, seen)
    check("长期授权拿到了私聊标记",
          bool(seen) and seen[0][1].get("is_private") is True,
          seen[0][1] if seen else None)
    check("授权范围写进 extra",
          extra.get("job_grant_scope") == ["write_local"],
          extra.get("job_grant_scope"))

    total = 47
    print(f"RESULT {failed} failed / {total - failed} checked")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
