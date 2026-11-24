"""回合账本（core/observability/turn_usage）：把一回合内多次 LLM 调用累在一起。

背景（2026-09-24 成本账本）：群聊一个回合的调用来源已经不止一个——
speak_render（出稿）+ admission judge（准入 on 时每群回合一次）+ topic_line
+ vision…，但只有逐条 `llm_call` 日志和 per-agent 累计；没有"这一回合花了
几次调用、多少 token、多少毫秒"的账。

Run directly:
    python tests/test_turn_usage.py
"""

from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

ROOT = os.path.join(os.path.dirname(__file__), "..")


def test_ledger_counts_calls_tokens_and_agents() -> None:
    from core.observability import turn_usage

    turn_usage.begin()
    turn_usage.record("speak_render", 100)
    turn_usage.record("speak_render", 50)
    turn_usage.record("admission_judge", 200)
    snap = turn_usage.snapshot()
    assert snap == {
        "calls": 3,
        "tokens": 350,
        "agents": {"speak_render": 2, "admission_judge": 1},
    }, snap
    assert turn_usage.end() == snap
    # end 之后账本清空
    assert turn_usage.snapshot() == {}


def test_record_without_turn_is_noop() -> None:
    from core.observability import turn_usage

    turn_usage.end()  # 确保没有活动账本
    turn_usage.record("whatever", 999)
    assert turn_usage.snapshot() == {}, turn_usage.snapshot()


def test_ledger_is_isolated_per_async_task() -> None:
    """contextvar：并发 task 各自记账，互不串（后台 task 不影响父回合）。"""
    from core.observability import turn_usage

    async def worker(name: str, tokens: int, wait: float):
        turn_usage.begin()
        turn_usage.record(name, tokens)
        await asyncio.sleep(wait)
        return turn_usage.snapshot()

    async def main():
        a, b = await asyncio.gather(worker("a", 10, 0.02), worker("b", 30, 0.0))
        return a, b

    a, b = asyncio.run(main())
    assert a["tokens"] == 10 and a["agents"] == {"a": 1}, a
    assert b["tokens"] == 30 and b["agents"] == {"b": 1}, b


def test_client_records_into_the_ledger() -> None:
    """静态守卫：LLM 客户端必须记账（否则账本永远是空）。"""
    src = open(os.path.join(ROOT, "llm", "client.py"), encoding="utf-8").read()
    assert "turn_usage.record(" in src, "llm/client.py 没有调用 turn_usage.record"


def test_router_emits_turn_usage_for_both_paths() -> None:
    """静态守卫：直回与群触发两条路径都要 begin/end 并落 turn_llm_usage。"""
    src = open(os.path.join(ROOT, "core", "routing", "router.py"), encoding="utf-8").read()
    assert src.count('"turn_llm_usage"') >= 2, "router 里 turn_llm_usage 应覆盖直回+群触发两条路径"
    assert "turn_usage.begin()" in src and "turn_usage.end()" in src
    assert "self._on_group_trigger_traced" in src, "群触发必须走账本外壳"
    assert "_on_group_trigger(" in src, "原 _on_group_trigger 不能被改名（有 AST/源码守卫依赖）"


def main() -> int:
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    failed = 0
    for test in tests:
        try:
            test()
            print(f"PASS {test.__name__}")
        except AssertionError as exc:
            failed += 1
            print(f"FAIL {test.__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
