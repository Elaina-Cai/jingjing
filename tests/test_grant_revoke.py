"""U-27 授权撤销：一句「以后都问我」把长期授权收回来，且立刻生效。

钉住的是**边界**：
- 只认私聊本人、≤40 字、必须真有收回语义，且**否定优先**（「不用撤回」不许撤）；
- 只有确实还有一张有效授权时才撤（撤一张不存在的票没有意义）；
- 两件事都还有效时一句都不撤（不许猜是哪一件）；
- 撤权只动 `policy`（`expires_at` 钉成此刻），不动「在等」的状态；
- `off` 档一行都不做。

跑法：.venv_it\\Scripts\\python.exe -X utf8 -u tests/test_grant_revoke.py
"""

from __future__ import annotations

import dataclasses
import pathlib
import sys
from datetime import datetime, timedelta

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import create_engine, text  # noqa: E402

from core.contracts.plan import Approval, Awaiting, Job, Policy, Step  # noqa: E402
from core.jobs import approval as A  # noqa: E402
from core.jobs import policy as P  # noqa: E402
from core.jobs import store as job_store  # noqa: E402

COUNT = [0]
FAILS: list[str] = []
NOW = datetime(2026, 10, 20, 12, 0, 0)


def check(name: str, cond: bool, detail: str = "") -> None:
    COUNT[0] += 1
    if cond:
        print(f"  ok   {name}")
        return
    FAILS.append(f"{name} {detail}".strip())
    print(f"  FAIL {name} {detail}".strip())


def _store(mode: str = "on") -> job_store.JobStore:
    engine = create_engine("sqlite://")
    job_store.ensure_schema(engine)
    return job_store.JobStore(engine, mode=mode)


def _job(job_id: str = "j1", *, scope: str = "write_local", approve: str = "私聊:u1") -> Job:
    return Job(
        job_id=job_id,
        chat_id="c1",
        goal="订下周去杭州",
        owner_person_id="u1",
        steps=[Step(n=1, action="write:行程.md", effects="write_local")],
        policy=Policy(
            scope=scope,
            expires_at=(NOW + timedelta(days=30)).isoformat(sep=" ", timespec="seconds"),
            revocable=True,
            approved_by=approve,
        ),
    )


def _with_policy(job: Job, **kw) -> Job:
    return dataclasses.replace(job, policy=dataclasses.replace(job.policy, **kw))


def with_mode(mode: str):
    old = A._mode
    A._mode = lambda: mode
    return old


def t_live_grant() -> None:
    check("没有 policy → 不算有票", A.live_grant(Job(job_id="x", chat_id="c", goal="x")) is None)
    check("没凭据 → 不算", A.live_grant(_job(approve="")) is None)
    check("scope 空 → 不算", A.live_grant(_job(scope="")) is None)
    expired = _with_policy(_job(), expires_at=(NOW - timedelta(days=1)).isoformat(sep=" ", timespec="seconds"))
    check("过期 → 不算", A.live_grant(expired, now=NOW) is None)
    check("有效 → 算", A.live_grant(_job(), now=NOW) is not None)


def t_parse() -> None:
    parse = A.parse_revoke_grant
    check("群里说 → 不撤", parse("撤回授权", is_private=False, speaker_id="u1", owner="u1") == "")
    check("别人说 → 不撤", parse("撤回授权", is_private=True, speaker_id="u2", owner="u1") == "")
    check("太长 → 不撤", parse("撤回授权" + "啊" * 60, is_private=True, speaker_id="u1") == "")
    check("否定优先：不用撤回授权了 → 不撤", parse("不用撤回授权了", is_private=True, speaker_id="u1") == "")
    check("否定优先：别取消 → 不撤", parse("别取消权限", is_private=True, speaker_id="u1") == "")
    check("无收回语义 → 不撤", parse("今天天气不错", is_private=True, speaker_id="u1") == "")
    got = parse("撤回授权", is_private=True, speaker_id="u1")
    check("撤回授权 → 撤", got == "撤回授权", got)
    check("以后别自己办了 → 撤", parse("以后别自己办了", is_private=True, speaker_id="u1") != "")
    check("还是问我吧 → 撤", parse("还是问我吧", is_private=True, speaker_id="u1") != "")
    check("以后都问我 → 撤", parse("以后都问我", is_private=True, speaker_id="u1") != "")
    check("给了 job 但没有票 → 不撤", parse("撤回授权", is_private=True, speaker_id="u1", job=_job(approve="")) == "")


def t_record() -> None:
    store = _store()
    job = _job()
    store.create(job)
    old = with_mode("off")
    try:
        check("off 档不落库", A.record_revoke(job, store=store) is False)
    finally:
        A._mode = old
    old = with_mode("on")
    try:
        check("store 关闭时不落库", A.record_revoke(job, store=_store("off")) is False)
        check("没有票时不落库", A.record_revoke(_job(approve=""), store=store) is False)
        check("有票时落库", A.record_revoke(job, store=store, who="u1", reason="撤回授权", now=NOW) is True)
        kinds = store.event_kinds(job.job_id)
        check("落的是 grant_revoked", "grant_revoked" in kinds, str(kinds))
    finally:
        A._mode = old


def t_projection_takes_effect() -> None:
    store = _store()
    job = _job()
    store.create(job)
    before = store.load(job.job_id)
    check("撤之前：有票", A.live_grant(before, now=NOW) is not None)
    ok, why = P.grant_allows(before.policy, step=job.steps[0], now=NOW)
    check("撤之前：grant 覆盖这一步", ok is True, why)

    old = with_mode("on")
    try:
        out = A.apply_revoke_grant("以后别自己办了", chat_id="c1", is_private=True, speaker_id="u1", store=store, now=NOW)
    finally:
        A._mode = old
    check("apply 返回被撤的那张票", len(out) == 1 and out[0].scope == "write_local", str(out))
    after = store.load(job.job_id)
    check("撤之后：没有票", A.live_grant(after, now=NOW) is None)
    ok2, why2 = P.grant_allows(after.policy, step=job.steps[0], now=NOW)
    check("撤之后：grant 不再覆盖", ok2 is False, why2)
    check("撤之后：理由是过期（撤回=钉成已过期）", why2 == P.REASON_GRANT_EXPIRED, why2)
    check("撤之后：scope 还留着（留痕）", after.policy.scope == "write_local", after.policy.scope)

    store.rebuild("c1")
    rebuilt = store.load(job.job_id)
    check("重放后仍是撤掉的票", A.live_grant(rebuilt, now=NOW) is None)


def t_apply_gates() -> None:
    store = _store()
    store.create(_job())
    old = with_mode("on")
    try:
        check("群里说 → 一句都不撤", A.apply_revoke_grant("撤回授权", chat_id="c1", is_private=False, speaker_id="u1", store=store, now=NOW) == [])
        check("别人说 → 一句都不撤", A.apply_revoke_grant("撤回授权", chat_id="c1", is_private=True, speaker_id="u2", store=store, now=NOW) == [])
        check("没语义 → 不撤", A.apply_revoke_grant("好的", chat_id="c1", is_private=True, speaker_id="u1", store=store, now=NOW) == [])
        check("空 chat → 不撤", A.apply_revoke_grant("撤回授权", chat_id="", is_private=True, speaker_id="u1", store=store, now=NOW) == [])
        check("空文本 → 不撤", A.apply_revoke_grant("  ", chat_id="c1", is_private=True, speaker_id="u1", store=store, now=NOW) == [])
    finally:
        A._mode = old

    old = with_mode("off")
    try:
        check("off 档 → 不撤", A.apply_revoke_grant("撤回授权", chat_id="c1", is_private=True, speaker_id="u1", store=store, now=NOW) == [])
    finally:
        A._mode = old

    old = with_mode("on")
    try:
        check("store 关闭 → 不撤", A.apply_revoke_grant("撤回授权", chat_id="c1", is_private=True, speaker_id="u1", store=_store("off"), now=NOW) == [])
        # 没有票的事：不撤
        store2 = _store()
        store2.create(_job("j2", approve=""))
        check("没有票 → 不撤", A.apply_revoke_grant("撤回授权", chat_id="c1", is_private=True, speaker_id="u1", store=store2, now=NOW) == [])
    finally:
        A._mode = old


def t_ambiguous_and_idempotent() -> None:
    store = _store()
    store.create(_job("jA"))
    store.create(_job("jB"))
    old = with_mode("on")
    try:
        check("两件都有票 → 一句都不撤", A.apply_revoke_grant("撤回授权", chat_id="c1", is_private=True, speaker_id="u1", store=store, now=NOW) == [])
        # 先把 jB 的票作废，再撤
        store.append("jB", "grant_revoked", payload={"revoked_policy": dict(A._expired_now(store.load("jB").policy, now=NOW))}, actor="human")
        out = A.apply_revoke_grant("撤回授权", chat_id="c1", is_private=True, speaker_id="u1", store=store, now=NOW)
        check("只剩一件有票 → 撤它", len(out) == 1, str(out))
        again = A.apply_revoke_grant("撤回授权", chat_id="c1", is_private=True, speaker_id="u1", store=store, now=NOW)
        check("再撤一次 → 空（幂等）", again == [], str(again))
    finally:
        A._mode = old


def t_leaves_awaiting_alone() -> None:
    store = _store()
    job = dataclasses.replace(_job(), state="awaiting", awaiting=Awaiting(kind="approval", who="u1"))
    store.create(job)
    store.append(job.job_id, "awaiting", payload={"kind": "approval", "who": "u1"}, actor="human")
    old = with_mode("on")
    try:
        A.apply_revoke_grant("撤回授权", chat_id="c1", is_private=True, speaker_id="u1", store=store, now=NOW)
    finally:
        A._mode = old
    after = store.load(job.job_id)
    check("撤权不动状态", after.state == "awaiting", after.state)
    check("撤权不动「在等谁」", after.awaiting.kind == "approval", after.awaiting.kind)


def t_shape_and_wiring() -> None:
    check("事件 kind 已注册", "grant_revoked" in job_store.EVENT_KINDS)
    check("DDL 仍是 5 条（不新增表）", len(job_store.ddl_statements()) == 5)
    src = (ROOT / "core/routing/turn_pipeline.py").read_text(encoding="utf-8")
    check("回合里接了撤回", "apply_revoke_grant(" in src)
    check("撤权结果写进 extra", 'extra["job_grant_revoked"]' in src)
    a_src = (ROOT / "core/jobs/approval.py").read_text(encoding="utf-8")
    check("否定优先在代码里", "KEEP_TOKENS" in a_src and "any(tok in line for tok in KEEP_TOKENS)" in a_src)
    check("撤回不改 scope", "scope=granted.scope" in a_src)


TESTS = (
    t_live_grant,
    t_parse,
    t_record,
    t_projection_takes_effect,
    t_apply_gates,
    t_ambiguous_and_idempotent,
    t_leaves_awaiting_alone,
    t_shape_and_wiring,
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
