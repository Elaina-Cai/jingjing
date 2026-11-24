import os
import sys
from pathlib import Path
os.environ["YINYUE_FEATURE_JOB_LEDGER"] = "on"
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from sqlalchemy import create_engine  # noqa: E402
from core import features  # noqa: E402
from core.jobs import ingest, runner, store as job_store  # noqa: E402
from core.jobs.executor import split_mention  # noqa: E402

FAILS = []
def check(name, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + name + ("" if cond else f"  {detail}"))
    if not cond:
        FAILS.append(name)

def main() -> int:
    # 1) @ 后面没空格：不许吞正文
    check("无空格不当 mention", split_mention("@Elaina，命令已执行") == ("", "@Elaina，命令已执行"))
    check("有空格才拆", split_mention("@小奇 在吗") == ("小奇", "在吗"))
    check("只有 @名字", split_mention("@小奇") == ("", "@小奇"))
    check("普通正文", split_mention("明天见") == ("", "明天见"))

    # 2) 跑了但没成，不许记成 step_done
    engine = create_engine("sqlite://")
    job_store.ensure_schema(engine)
    st = job_store.JobStore(engine, mode=features.MODE_ON)
    job = ingest.ingest_plan(
        {"plan": {"goal": "g", "steps": [{"n": 1, "action": "web_search:杭州天气"}]}},
        chat_id="c1", store=st, trace_id="t",
    )
    check("开成一件", job is not None)
    res = runner.advance(job, execute=lambda step: (False, "blocked:unsupported：空文本不发送"), store=st)
    kinds = [k for k, _ in st.events(job.job_id)] if hasattr(st, "events") else []
    if not kinds:
        rows = st.event_kinds(job.job_id)
        kinds = list(rows) if not isinstance(rows, dict) else [k for k in rows]
    check("停 failed", res.stop == "failed", res.stop)
    check("台账里是 step_failed 不是 step_done", "step_failed" in kinds and "step_done" not in kinds, kinds)
    return 1 if FAILS else 0

if __name__ == "__main__":
    sys.exit(main())

