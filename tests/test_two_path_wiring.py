# -*- coding: utf-8 -*-
"""WP-06 两路径接线变异锁（《当断则断_执行手册》§6.3 + task-4 补2）。

四条变异，每条都把 router 入口/判定接线真的删掉，跑对应路径的行为用例，必须
变红；还原后再跑必须绿：

* 直回 route 决策（``_route_one`` 里的 ``decide_turn``）删掉 -> TurnDecision 变 silent；
* 直回生成入口（``_invoke_conversational_inner`` 的 ``generate_turn``）删掉；
* 触发 route 决策（``_on_group_trigger`` 的 ``decide_turn`` 绑定）删掉；
* 触发生成入口（``_on_group_trigger`` 的 ``generate_turn``）删掉。

两条 relevance 阶段（``_generate_direct_turn`` / ``_generate_trigger_turn`` 对
``decide_turn`` 的调用）由 tests/test_turn_pipeline_single_assembly.py 的 AST 守卫
锁在场，不再各自引一条长行为用例。

Run directly: python tests/test_two_path_wiring.py
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ROUTER = ROOT / "core" / "routing" / "router.py"

# 2026-10-23：`_route_one` 现在先把判决收进变量（要记一行不回的原因，cd9afe7），
# 再 return。锚点必须跟着改，否则这里会以 "anchor missing" 假红——它守的不是
# 缩进，而是「route 真的调了 decide_turn 且真的把结果交出去了」。
DIRECT_ROUTE_ANCHOR = (
    "        decision = decide_turn(RouteFacts(\n"
    "            router=self, origin=\"direct\", stage=\"route\",\n"
    "            msg=msg, context=context,\n"
    "        ))\n"
    "        _ledger_silence(decision, msg, where=\"route\")\n"
    "        return decision\n"
)
DIRECT_GENERATE_ANCHOR = "        return await generate_turn(ctx)\n"
TRIGGER_ANCHOR = (
    "        ctx.decision = decide_turn(ctx)\n"
    "        _ledger_silence(\n"
    "            ctx.decision,\n"
    "            {\"chat_id\": chat_id, \"id\": getattr(question, \"msg_id\", \"\") or \"\"},\n"
    "            where=\"trigger\",\n"
    "        )\n"
    "        await generate_turn(ctx)\n"
)

MUTATIONS = (
    (
        "direct-drop-route-decide",
        DIRECT_ROUTE_ANCHOR,
        "        return TurnDecision(action=\"silent\")\n",
        "tests/test_pat_sync.py",
    ),
    (
        "direct-drop-generate",
        DIRECT_GENERATE_ANCHOR,
        '        return ""\n',
        "tests/test_turn_pipeline_single_assembly.py",
    ),
    (
        "trigger-drop-route-decide",
        TRIGGER_ANCHOR,
        "        await generate_turn(ctx)\n",
        "tests/test_group_context_source.py",
    ),
    (
        "trigger-drop-generate",
        TRIGGER_ANCHOR,
        "        ctx.decision = decide_turn(ctx)\n        return\n",
        "tests/test_group_context_source.py",
    ),
)

TARGETS = (
    "tests/test_pat_sync.py",
    "tests/test_turn_pipeline_single_assembly.py",
    "tests/test_group_context_source.py",
)


def _run(test: str) -> int:
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.run(
        [sys.executable, "-u", test],
        cwd=str(ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
    )
    return int(proc.returncode)


def _green_baseline() -> list:
    failures = []
    for test in TARGETS:
        code = _run(test)
        if code != 0:
            failures.append("%s baseline expected green, got exit=%d" % (test, code))
    return failures


def main() -> bool:
    print("=" * 66)
    print("two path wiring mutation checks (WP-06 / task-4)")
    print("=" * 66)
    original = ROUTER.read_bytes()
    text = ROUTER.read_text(encoding="utf-8")
    for anchor in (DIRECT_ROUTE_ANCHOR, DIRECT_GENERATE_ANCHOR, TRIGGER_ANCHOR):
        assert anchor in text, "anchor missing: %r" % anchor[:60]

    failures = _green_baseline()
    for name, anchor, replacement, test in MUTATIONS:
        mutated = text.replace(anchor, replacement, 1)
        assert mutated != text, "%s: mutation did not apply" % name
        try:
            ROUTER.write_text(mutated, encoding="utf-8")
            red_code = _run(test)
        finally:
            ROUTER.write_bytes(original)
        green_code = _run(test)
        ok = red_code != 0 and green_code == 0
        status = "PASS" if ok else "FAIL"
        print("[%s] %-26s red(!=0)=%d green(==0)=%d [%s]"
              % (status, name, red_code, green_code, test))
        if not ok:
            failures.append(
                "%s: red=%d green=%d (%s)" % (name, red_code, green_code, test)
            )

    failures.extend("cleanup: %s" % item for item in _green_baseline())
    print("mutations=%d failures=%d" % (len(MUTATIONS), len(failures)))
    for item in failures:
        print("  FAIL %s" % item)
    return not failures


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
