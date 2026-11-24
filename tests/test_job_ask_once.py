"""「同一件事只问一次」的独立单测（2026-10-21 真机事故的回归）。

事故：4 件陈旧 awaiting 任务每回合被催一遍，一回合发出 4 条
「想做的事…回「可以」我就动手」，而且取消事件写进去后 state 不动。
本文件钉三件事：
  ① ``ask`` 是已登记的事件 kind（否则 append 直接 ValueError）；
  ② ``asked_refs`` 只认 ask 事件的 payload.ref，且按 chat_id 隔离；
  ③ 终态事件（cancelled/failed/abandoned）必须真的把 state 改掉并清空 awaiting。
"""
from __future__ import annotations

import dataclasses
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import create_engine  # noqa: E402

from core import features  # noqa: E402
from core.contracts.plan import Awaiting, Job, Step  # noqa: E402
from core.jobs import store as job_store  # noqa: E402

PASSED = FAILED = 0


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


def _job(job_id: str = "j1", chat_id: str = "c1") -> Job:
    return Job(
        job_id=job_id,
        chat_id=chat_id,
        goal="往群里说一句",
        steps=[Step(n=1, action="send:测试群|下雨带伞", effects=("external_send",))],
        awaiting=Awaiting(kind="approval", who="c1"),
        state="awaiting",
    )


def test_ask_is_registered_kind() -> None:
    check("ask 已登记进 EVENT_KINDS", "ask" in job_store.EVENT_KINDS)


def test_asked_refs_by_chat() -> None:
    st = job_store.JobStore(_engine(), mode=features.MODE_ON)
    st.append("j1", "created", payload=dataclasses.asdict(_job()), actor="judge")
    st.append("j1", "ask", payload={"ref": "j1#1", "step_n": 1}, actor="judge")
    st.append("j2", "created", payload=dataclasses.asdict(_job("j2", "c2")), actor="judge")
    st.append("j2", "ask", payload={"ref": "j2#1", "step_n": 1}, actor="judge")
    check("c1 只看到自己的 ref", st.asked_refs("c1") == {"j1#1"}, str(st.asked_refs("c1")))
    check("c2 不会串", st.asked_refs("c2") == {"j2#1"}, str(st.asked_refs("c2")))
    check("没问过的会话是空集", st.asked_refs("c3") == set(), str(st.asked_refs("c3")))
    check("off 档不读库（返回空集）",
          job_store.JobStore(_engine(), mode=features.MODE_OFF).asked_refs("c1") == set())


def test_terminal_clears_awaiting() -> None:
    # 注意：job 级没有 failed（只有 step_failed），job 级终态用 cancelled/abandoned
    for kind, want in (("cancelled", "cancelled"), ("abandoned", "abandoned")):
        st = job_store.JobStore(_engine(), mode=features.MODE_ON)
        st.append("j1", "created", payload=dataclasses.asdict(_job()), actor="judge")
        st.append("j1", "awaiting", payload={"kind": "approval", "who": "c1"})
        st.append("j1", kind, payload={"reason": "test"}, actor="operator")
        got = st.load("j1")
        check(f"{kind} 后 state={want}", got is not None and got.state == want,
              "" if got is None else got.state)
        check(f"{kind} 后 awaiting 清空",
              got is not None and got.awaiting.kind in ("", "none"),
              "" if got is None else got.awaiting.kind)


TESTS = [test_ask_is_registered_kind, test_asked_refs_by_chat, test_terminal_clears_awaiting]


def main() -> int:
    for fn in TESTS:
        fn()
    total = PASSED + FAILED
    print(f"RESULT {FAILED} failed / {total} checks")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
