import os
import sys
from pathlib import Path
os.environ["YINYUE_FEATURE_JOB_LEDGER"] = "on"
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from sqlalchemy import create_engine  # noqa: E402
from core import features  # noqa: E402
from core.jobs import ingest, store as job_store  # noqa: E402

FAILS = []
def check(name, cond):
    print(("  ok   " if cond else "  FAIL ") + name)
    if not cond:
        FAILS.append(name)

def _store():
    engine = create_engine("sqlite://")
    job_store.ensure_schema(engine)
    return job_store.JobStore(engine, mode=features.MODE_ON)

PLAN = {"plan": {"goal": "往群里说一句", "steps": [{"n": 1, "action": "send:示例作业群|@Elaina，命令已执行"}]}}
OTHER = {"plan": {"goal": "另一件事", "steps": [{"n": 1, "action": "send:鲸鲸的测试群|别的话"}]}}

def main() -> int:
    st = _store()
    a = ingest.ingest_plan(PLAN, chat_id="c1", store=st, trace_id="direct-1")
    b = ingest.ingest_plan(PLAN, chat_id="c1", store=st, trace_id="send_plan_tool")
    check("同一句只开一件（复用）", a is not None and b is not None and a.job_id == b.job_id)
    check("台账只有一行", len(st.open_jobs("c1")) == 1)
    c = ingest.ingest_plan(OTHER, chat_id="c1", store=st, trace_id="t3")
    check("不同动作照旧开新件", c is not None and c.job_id != a.job_id)
    check("台账两行", len(st.open_jobs("c1")) == 2)
    d = ingest.ingest_plan(PLAN, chat_id="c2", store=st, trace_id="t4")
    check("跨会话不复用", d is not None and d.job_id != a.job_id)
    return 1 if FAILS else 0

if __name__ == "__main__":
    sys.exit(main())
