from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

FAILS: list[str] = []
COUNT = 0


def check(name, cond, detail=""):
    global COUNT
    COUNT += 1
    if cond:
        print("  ok  " + name)
    else:
        FAILS.append(name)
        print("  FAIL " + name, detail)


def main() -> int:
    print("== 问批准接线 ==")
    # 2026-10-23：开事与「问批准」只剩一个 owner —— 出口（core/speech/plan_ingest.py）。
    # 路由侧原来有三份并行实现（一份 <plan> 开事 + 两份补问句，其中一份不记账）。
    ingest = (ROOT / "core/speech/plan_ingest.py").read_text(encoding="utf-8")
    outbound = (ROOT / "core/speech/outbound.py").read_text(encoding="utf-8")
    router = (ROOT / "core/routing/turn_pipeline.py").read_text(encoding="utf-8")
    check("开完事就问（同一处摘块 + 开事 + 补问句）",
          "plan_block.consume(" in ingest and "plan_block_consumed" in ingest
          and "pending_asks(" in ingest and "asked_refs(" in ingest)
    check("问的话并进真正发出去的那条",
          'merged = (text + "\\n\\n" + str(item["text"])).strip()' in ingest
          and outbound.index("self._append_pending_ask(") < outbound.index("await self.evaluate_async("))
    # 只问一条：取待问列表的头一件（不再用 break 跳出三处并行循环）
    check("只问一条", "items[0]" in ingest and ingest.count("ledger.append(") == 1
          and "job_ask_appended" in ingest)
    check("问不出口不拦消息", "job_ask_append_failed" in ingest)
    check("日志带 ref", 'job_id=item["job_id"], ref=item["ref"]' in ingest)
    check("路由侧不再自己开事/追问",
          "plan_block.consume(" not in router and "pending_asks(" not in router
          and "_had_plan" in router)
    ask_src = (ROOT / "core/jobs/ask.py").read_text(encoding="utf-8")
    check("ref 绑步", 'return f"{job_id}#{step_n}"' in ask_src)
    check("只算不发", "def pending_asks(" in ask_src and "send" not in ask_src.split("def pending_asks(")[1].split("def looks_like_done_claim")[0])
    print(f"RESULT {len(FAILS)} failed / {COUNT} checks")
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())
