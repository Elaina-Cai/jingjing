#!/usr/bin/env python
"""批1 场景回放（上下文级，不调 LLM）。

检查批 1「别硬编」在链路里应就位的上下文元素：
  * [拿不准时] 块（UNCERTAIN_VOICE=1；关 = 回滚干净）
  * 名单「没对上号」规则（同上）
  * 负知识块（问句 + 空召回）

S2/S3 是批 1 的主验收场景：这里只验证「规则与信息在场」；
真正的输出级回放（把场景喂给模型看回复）留待灰度观察。

用法：
  python scripts/replay_understanding_scenarios.py
  python scripts/replay_understanding_scenarios.py --json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SCENARIOS = ROOT / "configs" / "understanding_scenarios.json"
BATCH1_SCENARIOS = {"S2_unknown_slang", "S3_unknown_person"}


class _env:
    """Temporarily set/clear environment flags."""

    def __init__(self, **values: str | None):
        self._values = values

    def __enter__(self):
        self._saved = {key: os.environ.get(key) for key in self._values}
        for key, value in self._values.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def __exit__(self, *exc):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _prompt_text() -> str:
    from core.terminal.persona_brain import build_persona_blocks

    snapshot = SimpleNamespace(
        persona=SimpleNamespace(name="鲸鲸", identity=None, traits=None),
        turn=None,
        policy=None,
    )
    return "\n".join(build_persona_blocks(snapshot))


def check_on_state() -> list[str]:
    """Return a list of problems for the flags-on state (empty = all good)."""
    from agents.conversational import group_members_unknown_rule
    from core.terminal.memory_gateway import (
        looks_like_question,
        negative_knowledge_block,
    )

    problems: list[str] = []
    with _env(
        YINYUE_FEATURE_UNCERTAIN_VOICE="1",
        YINYUE_FEATURE_MEMORY_GATE="1",
    ):
        text = _prompt_text()
        if "[拿不准时]" not in text:
            problems.append("missing [拿不准时] block")
        if "没对上号" not in text:
            problems.append("[拿不准时] missing 没对上号 wording")
        if "不要猜" not in text or "硬编" not in text:
            problems.append("[拿不准时] missing no-guessing rule")

        rule = group_members_unknown_rule()
        if "没对上号" not in rule:
            problems.append("members rule missing 没对上号")
        if "不要说" not in rule or "没这号人" not in rule:
            problems.append("members rule missing 没这号人 downgrade")

        if not looks_like_question("明天fkxqs浅尝一下吗"):
            problems.append("slang question not detected (S2 trigger)")
        if not looks_like_question("钟凯宇是谁"):
            problems.append("identity question not detected (S3 trigger)")
        if "没查到" not in negative_knowledge_block():
            problems.append("negative knowledge block wording missing")
    return problems


def check_off_state() -> list[str]:
    """Regression: with flags off nothing new should leak in."""
    from agents.conversational import group_members_unknown_rule

    problems: list[str] = []
    with _env(
        YINYUE_FEATURE_UNCERTAIN_VOICE=None,
        YINYUE_FEATURE_MEMORY_GATE=None,
    ):
        text = _prompt_text()
        if "[拿不准时]" in text:
            problems.append("block leaked while flag off")
        if group_members_unknown_rule() != "":
            problems.append("members rule leaked while flag off")
    return problems


def load_scenarios() -> list[dict]:
    with SCENARIOS.open(encoding="utf-8") as fh:
        payload = json.load(fh)
    return list(payload.get("scenarios") or [])


def main() -> int:
    parser = argparse.ArgumentParser(description="批1 场景回放（上下文级）")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    scenarios = load_scenarios()
    on_problems = check_on_state()
    off_problems = check_off_state()

    rows = []
    failed = 0
    for scenario in scenarios:
        sid = scenario.get("id", "?")
        if sid in BATCH1_SCENARIOS:
            problems = list(on_problems)
            if sid == "S2_unknown_slang":
                problems = [p for p in problems if "S2" in p or "拿不准" in p or "no-guessing" in p]
            if sid == "S3_unknown_person":
                problems = [p for p in problems if "S3" in p or "members" in p or "拿不准" in p]
            status = "PASS" if not problems else "FAIL"
            failed += 0 if not problems else 1
            rows.append({"id": sid, "status": status, "problems": problems})
        else:
            rows.append({"id": sid, "status": "SKIP", "problems": ["见 scripts/replay_log_cases.py（真实日志级回放）"]})

    regression = [f"off-state: {p}" for p in off_problems]
    if regression:
        failed += 1

    if args.json:
        print(json.dumps(
            {
                "on_state_problems": on_problems,
                "off_state_problems": off_problems,
                "scenarios": rows,
            },
            ensure_ascii=False,
            indent=2,
        ))
    else:
        print("[replay] 批1 上下文级回放")
        for row in rows:
            note = ""
            if row["status"] == "FAIL":
                note = "  " + "; ".join(row["problems"])
            elif row["status"] == "SKIP":
                note = "  （" + "; ".join(row["problems"]) + "）"
            print(f"  {row['id']:<22} {row['status']}{note}")
        print()
        print(f"  on-state checks : {'OK' if not on_problems else on_problems}")
        print(f"  off-state checks: {'OK' if not off_problems else off_problems}")
        print(f"  summary: {sum(1 for r in rows if r['status'] == 'PASS')} PASS / "
              f"{failed} FAIL / {sum(1 for r in rows if r['status'] == 'SKIP')} SKIP")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
