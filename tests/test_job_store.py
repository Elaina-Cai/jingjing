# -*- coding: utf-8 -*-
"""「在手的事」台账（U-10）的验收：append-only 事件、可重建投影、三档开关、批准幂等。

钉住的是**形状**而不是语气：
- `job_events` 是唯一事实源，`jobs` 必须能由它整表重放（`rebuild()` 后逐字段相同）；
- `JOB_LEDGER=off` 时写读全 no-op（既有行为逐字不变），`shadow` 只写不读，`on` 才读；
- 批准以 `(job_id, step_n, action_hash)` 为键，重复批幂等，过期不算批；
- 同一会话在手的事 ≤ `MAX_JOBS_PER_CHAT`。

跑法：`.venv_it\\Scripts\\python.exe -X utf8 -u tests\\test_job_store.py`
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import create_engine, text  # noqa: E402

from core import features  # noqa: E402
from core.contracts.plan import (  # noqa: E402
    MAX_JOBS_PER_CHAT,
    READOUT_MAX_CHARS,
    READOUT_MAX_ITEMS,
    Approval,
    Awaiting,
    Job,
    Step,
)
from core.jobs import store as job_store  # noqa: E402

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


def _engine():
    engine = create_engine("sqlite://")
    job_store.ensure_schema(engine)
    return engine


def _job(job_id: str = "j1", chat_id: str = "c1", *, steps: int = 2) -> Job:
    return Job(
        job_id=job_id,
        chat_id=chat_id,
        goal="规划下周去杭州",
        steps=[Step(n=i + 1, action=f"第{i + 1}步") for i in range(steps)],
    )


def test_schema_idempotent() -> None:
    engine = _engine()
    job_store.ensure_schema(engine)  # 第二次必须无害
    with engine.begin() as conn:
        names = {
            r[0]
            for r in conn.execute(
                text("SELECT name FROM sqlite_master WHERE type='table'")
            ).fetchall()
        }
    check("三张表都建出来了", {"job_events", "jobs", "job_approvals"} <= names, str(names))
    check("DDL 语句 5 条（2 表 + 1 表 + 2 索引）", len(job_store.ddl_statements()) == 5)


def test_off_is_noop() -> None:
    engine = _engine()
    st = job_store.JobStore(engine, mode=features.MODE_OFF)
    seq = st.create(_job())
    check("off: create 返回 0（没写）", seq == 0, str(seq))
    with engine.begin() as conn:
        n = conn.execute(text("SELECT COUNT(*) FROM job_events")).fetchone()[0]
    check("off: 事件表 0 行", n == 0, str(n))
    check("off: 读侧全空", st.load("j1") is None and st.readouts("c1") == [])


def test_shadow_writes_not_reads() -> None:
    engine = _engine()
    st = job_store.JobStore(engine, mode=features.MODE_SHADOW)
    st.create(_job())
    st.append("j1", "step_done", payload={"step_n": 1, "tool_calls": 1})
    kinds = st.event_kinds("j1")
    check("shadow: 事件照写", kinds == ["created", "step_done"], str(kinds))
    check("shadow: readouts 恒空（不进 prompt）", st.readouts("c1") == [])
    job = st.load("j1")
    check("shadow: 投影可读且 step_n 已推进", job is not None and job.step_n == 1,
          "" if job is None else str(job.step_n))


def test_on_readouts() -> None:
    engine = _engine()
    st = job_store.JobStore(engine, mode=features.MODE_ON)
    for i in range(MAX_JOBS_PER_CHAT):
        st.create(_job(job_id=f"j{i}"))
    reads = st.readouts("c1")
    check("on: 读数条数 ≤ 上限", len(reads) <= READOUT_MAX_ITEMS, str(len(reads)))
    check("on: 每条 ≤ 60 字", all(len(r) <= READOUT_MAX_CHARS for r in reads),
          str([len(r) for r in reads]))
    check("on: 读数带 job_id", all(r.split(" · ")[0].startswith("j") for r in reads), str(reads))
    check("on: 别的会话读不到", st.readouts("c2") == [])


def test_append_only_and_rebuild() -> None:
    engine = _engine()
    st = job_store.JobStore(engine, mode=features.MODE_ON)
    st.create(_job())
    st.append("j1", "step_done", payload={"step_n": 1, "tool_calls": 1})
    st.append("j1", "step_done", payload={"step_n": 2, "tool_calls": 1})
    before = st.load("j1")
    assert before is not None
    with engine.begin() as conn:
        seqs = [r[0] for r in conn.execute(
            text("SELECT seq FROM job_events WHERE job_id='j1' ORDER BY seq")
        ).fetchall()]
    check("事件 seq 从 1 起连续", seqs == [1, 2, 3], str(seqs))
    replayed = st.rebuild()
    after = st.load("j1")
    check("rebuild 重放了全部事件", replayed == 3, str(replayed))
    check("rebuild 后投影逐字段相同",
          after is not None and (after.step_n, after.state, after.tool_calls_total)
          == (before.step_n, before.state, before.tool_calls_total),
          "" if after is None else f"{after.step_n}/{after.state}/{after.tool_calls_total}")


def test_approval_idempotent_and_expiry() -> None:
    engine = _engine()
    st = job_store.JobStore(engine, mode=features.MODE_ON)
    st.create(_job())
    check("批准前 is_approved=False", st.is_approved("j1", 1, "h1") is False)
    ok = st.approve(Approval(job_id="j1", step_n=1, action_hash="h1", who="P5",
                             channel="wechat", at=datetime.now().isoformat(" ", "seconds")))
    check("第一次批准返回 True", ok is True)
    check("批了之后 is_approved=True", st.is_approved("j1", 1, "h1") is True)
    again = st.approve(Approval(job_id="j1", step_n=1, action_hash="h1", who="P5",
                                channel="wechat", at=datetime.now().isoformat(" ", "seconds")))
    check("重复批幂等（False）", again is False)
    past = (datetime.now() - timedelta(hours=1)).isoformat(" ", "seconds")
    st.approve(Approval(job_id="j1", step_n=2, action_hash="h2", who="P5",
                        channel="wechat", at=past, expires_at=past))
    check("过期批准不算批", st.is_approved("j1", 2, "h2") is False)
    check("批准的 actor 记到事件里", "approved" in st.event_kinds("j1"), str(st.event_kinds("j1")))


def test_job_limit() -> None:
    engine = _engine()
    st = job_store.JobStore(engine, mode=features.MODE_ON)
    raised = False
    for i in range(MAX_JOBS_PER_CHAT + 1):
        try:
            st.create(_job(job_id=f"k{i}"))
        except job_store.JobLimitExceeded:
            raised = True
    check("同会话超上限抛 JobLimitExceeded", raised)
    check("上限内都在", len(st.open_jobs("c1")) == MAX_JOBS_PER_CHAT,
          str(len(st.open_jobs("c1"))))


def test_unknown_kind_rejected() -> None:
    engine = _engine()
    st = job_store.JobStore(engine, mode=features.MODE_ON)
    st.create(_job())
    raised = False
    try:
        st.append("j1", "made_up_kind")
    except ValueError:
        raised = True
    check("未注册 kind 一律拒", raised)
    check("kind 表有 created 与 turn", "created" in job_store.EVENT_KINDS
          and "turn" in job_store.EVENT_KINDS)


def test_awaiting_transition() -> None:
    engine = _engine()
    st = job_store.JobStore(engine, mode=features.MODE_ON)
    st.create(_job())
    st.append("j1", "awaiting", payload={"kind": "input", "who": "p1"})
    job = st.load("j1")
    check("awaiting 事件把 state 推进 awaiting",
          job is not None and job.state == "awaiting" and job.awaiting.kind == "input",
          "" if job is None else f"{job.state}/{job.awaiting.kind}")
    st.append("j1", "approved", payload={"step_n": 1, "action_hash": "h1"})
    job2 = st.load("j1")
    check("批准后回到 open 且不再等谁",
          job2 is not None and job2.state == "open" and job2.awaiting.kind == "none",
          "" if job2 is None else f"{job2.state}/{job2.awaiting.kind}")
    st.append("j1", "done", payload={})
    job3 = st.load("j1")
    check("done 后不再出现在 open_jobs", job3 is not None and job3.state == "done"
          and st.open_jobs("c1") == [])


def test_migration_64() -> None:
    """迁移 64 是这条切片的落库入口：自己带 DDL，不在 meta 链、不被跳过。"""
    from sqlalchemy import text as _text

    import db.migrations as migrations

    versions = {m.version: m for m in migrations._MIGRATIONS}
    check("迁移 64 已注册", 64 in versions, str(max(versions)))
    check("64 不在 meta 链（上限 39）", 64 not in migrations._META_MIGRATION_VERSIONS)
    check("64 不在只盖章名单", 64 not in migrations._ACCOUNT_SKIP_MIGRATION_VERSIONS)

    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        conn.execute(_text("CREATE TABLE terminal_events (id INTEGER PRIMARY KEY)"))
        migrations._m064(conn, None)
        names = {
            r[0]
            for r in conn.execute(
                _text("SELECT name FROM sqlite_master WHERE type='table'")
            ).fetchall()
        }
    check("迁移 64 建出三张表",
          {"job_events", "jobs", "job_approvals"} <= names, str(names))

    empty = create_engine("sqlite://")
    with empty.begin() as conn:
        migrations._m064(conn, None)  # 没有 terminal_events：守卫直接 return
        names2 = {
            r[0]
            for r in conn.execute(
                _text("SELECT name FROM sqlite_master WHERE type='table'")
            ).fetchall()
        }
    check("纯账号库上不建表（守卫生效）", names2 == set(), str(names2))


TESTS = [
    ("schema", test_schema_idempotent),
    ("migration64", test_migration_64),
    ("off", test_off_is_noop),
    ("shadow", test_shadow_writes_not_reads),
    ("on", test_on_readouts),
    ("rebuild", test_append_only_and_rebuild),
    ("approval", test_approval_idempotent_and_expiry),
    ("limit", test_job_limit),
    ("kinds", test_unknown_kind_rejected),
    ("awaiting", test_awaiting_transition),
]


def main() -> int:
    for name, fn in TESTS:
        print(f"--- {name} ---")
        fn()
    print(f"RESULT {FAILED} failed / {PASSED} passed")
    return FAILED


if __name__ == "__main__":
    raise SystemExit(main())
