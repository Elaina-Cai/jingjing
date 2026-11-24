# -*- coding: utf-8 -*-
"""「怎么算做到了」这件事本身（第 6 件）的验收：`criteria` 从计划流到台账再读回来。

钉住的是**通路**而不是文案：
- 契约上 `Plan.criteria` / `Job.criteria` 缺省空串（既有生产者逐字不变）；
- `parse_plan` 读它（截 200）、`build_job` 传它、`send_plan` 工具能收它；
- 台账往返不丢：`create` 写进去、`load` 读出来、`rebuild` 由事件重放也还在；
- 迁移 67 幂等，老库（没有 criteria 列）读到空串而不崩。

跑法：`.venv_it\\Scripts\\python.exe -X utf8 -u tests\\test_job_criteria.py`
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import create_engine, text  # noqa: E402

from core import features  # noqa: E402
from core.contracts.plan import Job, Plan, Step  # noqa: E402
from core.jobs import ingest, store as job_store  # noqa: E402

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


def _job(criteria: str = "说出温度且带地名") -> Job:
    return Job(
        job_id="j-crit",
        chat_id="c1",
        goal="查天气",
        criteria=criteria,
        steps=[Step(n=1, action="查天气", effects="read")],
    )


def test_contract_defaults() -> None:
    plan = Plan(goal="g")
    check("Plan.criteria 缺省空串", plan.criteria == "")
    job = Job(job_id="j", chat_id="c", goal="g")
    check("Job.criteria 缺省空串", job.criteria == "")
    check("Plan 仍是纯数据契约（无方法）", not any(
        callable(getattr(Plan, n, None)) and n not in ("__init__",)
        for n in dir(Plan) if not n.startswith("__")
    ))


def test_parse_plan_reads_criteria() -> None:
    payload = {"plan": {"goal": "查天气", "criteria": "说出温度", "steps": [{"n": 1, "action": "查"}]}}
    plan = ingest.parse_plan(payload)
    check("parse_plan 读到 criteria", plan is not None and plan.criteria == "说出温度", str(plan))
    check("缺 criteria 时为空串",
          ingest.parse_plan({"plan": {"goal": "g", "steps": [{"n": 1, "action": "a"}]}}).criteria == "")
    long = "判" * 400
    check("criteria 截 200",
          len(ingest.parse_plan({"plan": {"goal": "g", "criteria": long,
                                          "steps": [{"n": 1, "action": "a"}]}}).criteria) == 200)
    check("criteria 非字符串不炸",
          ingest.parse_plan({"plan": {"goal": "g", "criteria": None,
                                      "steps": [{"n": 1, "action": "a"}]}}).criteria == "")


def test_build_job_carries_criteria() -> None:
    plan = ingest.parse_plan({"plan": {"goal": "查天气", "criteria": "说出温度",
                                       "steps": [{"n": 1, "action": "查"}]}})
    job = ingest.build_job(plan, chat_id="c1")
    check("build_job 把 criteria 带进 Job", job.criteria == "说出温度", job.criteria)
    check("build_job 是纯函数（不写库）", job.state == "open")


def test_store_roundtrip() -> None:
    engine = _engine()
    st = job_store.JobStore(engine, mode=features.MODE_ON)
    st.create(_job("说出温度"))
    got = st.load("j-crit")
    check("create→load 保住 criteria", got is not None and got.criteria == "说出温度",
          getattr(got, "criteria", None))
    with engine.begin() as conn:
        row = conn.execute(text("SELECT criteria FROM jobs WHERE job_id='j-crit'")).fetchone()
    check("投影表里真有这一列值", row is not None and row[0] == "说出温度", str(row))


def test_rebuild_from_events() -> None:
    engine = _engine()
    st = job_store.JobStore(engine, mode=features.MODE_ON)
    st.create(_job("说出温度"))
    st.rebuild()
    got = st.load("j-crit")
    check("事件重放后 criteria 还在（事件里存了，不是只靠投影）",
          got is not None and got.criteria == "说出温度", getattr(got, "criteria", None))


def test_planned_can_rewrite_criteria() -> None:
    engine = _engine()
    st = job_store.JobStore(engine, mode=features.MODE_ON)
    st.create(_job("旧判据"))
    st.append("j-crit", "planned", payload={"goal": "查天气", "steps": [{"n": 1, "action": "查"}]})
    got = st.load("j-crit")
    check("不带 criteria 的 planned 不改判据", got.criteria == "旧判据", got.criteria)
    st.append("j-crit", "planned", payload={"criteria": "新判据"})
    check("显式 criteria 可以改写", st.load("j-crit").criteria == "新判据")
    st.append("j-crit", "planned", payload={"criteria": ""})
    check("显式置空也照办（不是「非空才写」）", st.load("j-crit").criteria == "")


def test_migration_67_is_idempotent() -> None:
    from db import migrations as mig

    versions = [m.version for m in mig._MIGRATIONS]
    check("迁移 67 已登记", 67 in versions, str(sorted(versions)[-3:]))
    check("迁移号不重复", len(versions) == len(set(versions)))
    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE jobs (job_id TEXT PRIMARY KEY, chat_id TEXT, goal TEXT)"))
        mig._m067(conn, None)
        mig._m067(conn, None)  # 第二次必须无害
        cols = {str(c[1]) for c in conn.execute(text("PRAGMA table_info(jobs)")).fetchall()}
        conn.execute(text("INSERT INTO jobs (job_id, chat_id, goal) VALUES ('x','c','g')"))
        val = conn.execute(text("SELECT criteria FROM jobs WHERE job_id='x'")).fetchone()[0]
    check("老库补上 criteria 列", "criteria" in cols, str(cols))
    check("老行 criteria 为空串而不是 NULL", val == "", repr(val))


def test_old_db_without_column_does_not_crash() -> None:
    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE jobs (job_id TEXT PRIMARY KEY, chat_id TEXT, goal TEXT, owner_person_id TEXT,"
            " state TEXT, step_n INTEGER, awaiting_kind TEXT, awaiting_who TEXT, steps_json TEXT,"
            " trigger_json TEXT, policy_json TEXT, artifact_json TEXT, handoff_json TEXT,"
            " tool_calls_total INTEGER, turns_total INTEGER, seq INTEGER, updated_by TEXT, updated_at TEXT)"
        ))
        conn.execute(text(
            "INSERT INTO jobs (job_id, chat_id, goal, state, step_n, steps_json) "
            "VALUES ('old','c','g','open',0,'[]')"
        ))
    st = job_store.JobStore(engine, mode=features.MODE_ON)
    got = st.load("old")
    check("没迁移的老库也能 load（criteria 当空串）", got is not None and got.criteria == "",
          getattr(got, "criteria", None))


def test_send_plan_tool_accepts_criteria() -> None:
    from agents.tools.jobs import SendPlanTool

    params = SendPlanTool().parameters
    check("send_plan 有 criteria 参数", "criteria" in params, str(list(params)))
    check("criteria 不是必填", not params["criteria"].get("required"))


def main() -> int:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print(f"RESULT {PASSED}/{PASSED + FAILED} checks passed")
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
