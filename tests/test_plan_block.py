from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.jobs import plan_block as P
from core.speech.outbound import OutboundGateway

FAILS: list[str] = []
COUNT = 0


def check(name: str, cond: object, detail: str = "") -> None:
    global COUNT
    COUNT += 1
    if cond:
        print("  ok  " + name)
    else:
        FAILS.append(name)
        print("  FAIL " + name, detail)


def t_extract() -> None:
    text = "先说一句。\n<plan>{\"goal\":\"发一句\",\"steps\":[{\"action\":\"gui_state:微信\"}]}</plan>"
    clean, plan = P.extract(text)
    check("块被摘掉", "{" not in clean and clean.startswith("先说一句"))
    check("计划解出来了", isinstance(plan, dict) and plan.get("goal") == "发一句")
    same, none = P.extract("没有块的普通句子")
    check("没有块原样返回", same == "没有块的普通句子" and none is None)
    bad, none2 = P.extract("前<plan>{坏的</plan>后")
    check("坏块不拦正文", none2 is None and "前" in bad)


def t_consume() -> None:
    seen = {}

    class FakeStore:
        def load(self, job_id):
            return None

    import core.jobs.ingest as ingest

    class FakeJob:
        job_id = "job-x"
        steps = [1]

    orig = ingest.ingest_plan
    ingest.ingest_plan = lambda payload, **kw: (seen.update(payload) or FakeJob())
    try:
        import core.jobs.runner as runner

        orig_adv = runner.advance
        runner.advance = lambda job, **kw: type("R", (), {"stop": "need_approval"})()
        try:
            got = P.consume({"goal": "g", "steps": [{"action": "gui_state:微信"}]}, chat_id="c1", store=FakeStore())
        finally:
            runner.advance = orig_adv
    finally:
        ingest.ingest_plan = orig
    check("消费成功返回 job_id", got == "job-x")
    check("计划被传下去", seen.get("plan", {}).get("goal") == "g")
    src = Path(P.__file__).read_text(encoding="utf-8")
    check("丢事必须留日志", "plan_block_dropped" in src)
    out = Path(OutboundGateway.__module__.replace(".", "/") + ".py")
    body = (ROOT / out).read_text(encoding="utf-8")
    check("发送前摘块", "_consume_plan_block" in body and "plan_block_strip_failed" in body)


def main() -> int:
    print("== plan 块 ==")
    t_extract()
    t_consume()
    print(f"RESULT {len(FAILS)} failed / {COUNT} checks")
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())
