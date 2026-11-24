# -*- coding: utf-8 -*-
"""C · 有经验 / 无经验 的真实模型对照：她第一次抓哪个工具。

只在显式授权时跑（烧预算）：

    set YINYUE_EXPRESSION_REAL=1
    pytest tests/pytest_style/test_experience_choice_real.py -q

走的是评测装配里的**真实主循环**（`harness.assemble` → `brain.run_loop`），
只把模型换成「记录型包装」：它记下每次请求的工具名，再转给真实客户端。
经验写进的是这一条沙箱自己的认知库（`assemble(seed=...)`），不是 mock 字符串。

三组：

- A 本地资料问题、库里没有经验 → 她本来怎么选；
- B 同一问题、库里有一条 `search_knowledge` 成功经验 → 会不会换方法；
- C 同一份经验在场、问的是公共资料 → 会不会照搬。

模型选择只**记录**、不硬断言（温度 0.7，单次不是回归）；断言的是接线：
经验只在该出现的时候出现在提示词里。
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

from core.context import cognitive_commit as cc
from core.context import reflection
from evals.expression import harness

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "output" / "实验C-有经验对照-20261118.json"
REAL = os.environ.get("YINYUE_EXPRESSION_REAL") == "1"
SCOPE = ("wechat", "wxid_eval_001", "private")
LESSON = ("再有人问项目里的资料（比如部署说明），先 do(name=\"read_file\", args={\"path\":\"docs/README.md\"}) 读索引，别凭印象猜路径。")

pytestmark = pytest.mark.skipif(not REAL, reason="需要真实模型授权（YINYUE_EXPRESSION_REAL=1）")

INDEX = "# docs 索引\n- 部署说明：docs/deploy.md\n- 取名规范：docs/naming.md\n"
CATALOG = "查项目文档：1 个 · read_file\n翻旧账：3 个 · search_messages、memory_recall"

LOCAL = {
    "id": "c-local", "kind": "single", "chat": "private",
    "history": [
        {"role": "user", "text": "上次让你找项目里那份部署说明，你翻半天没找到。"},
        {"role": "user", "text": "其实 docs 里有个索引，路径都写着呢。"},
    ],
    "current": "再帮我找一下这个项目的部署说明在哪。",
    "tool_stub": {"catalog": {"text": CATALOG}, "read_file": {"text": INDEX}},
}
PUBLIC = {
    "id": "c-public", "kind": "single", "chat": "private",
    "history": [{"role": "user", "text": "问个别的。"}],
    "current": "2026 年诺贝尔文学奖是谁拿的？",
    "tool_stub": {"catalog": {"text": CATALOG}, "read_file": {"text": INDEX}},
}


def _name_of(call) -> str:
    return str(call.get("name") or "") if isinstance(call, dict) else str(call or "")


class Recording:
    """记下每次请求的工具名，其余原样转给真实客户端。"""

    def __init__(self, inner):
        self.inner = inner
        self.seen: list[list[str]] = []

    async def chat_tools(self, **kwargs):
        out = await self.inner.chat_tools(**kwargs)
        self.seen.append([str(c.get("name") or "") for c in (out.get("tool_calls") or [])])
        return out

    async def aclose(self):
        closer = getattr(self.inner, "aclose", None)
        if closer is not None:
            await closer()


def install_lesson() -> None:
    """在**当前沙箱**的认知库里写一条方法经验（由 assemble 在沙箱建好后调用）。"""
    conn = cc.connect()
    try:
        result = reflection.record_lesson(
            conn, scope=cc.Scope(*SCOPE), method="read_file", outcome="success",
            evidence_refs=("evt:doc-index",), lesson=LESSON, confidence=0.8,
            occurrence="c-lesson-1", source="experience_tool",
        )
        assert result.ok, list(result.rejected)
    finally:
        conn.close()


def _run(scenario: dict, repeat: int, *, with_lesson: bool) -> dict:
    llm = Recording(harness.RealLLM(provider="deepseek"))
    rec = asyncio.run(harness.assemble(
        scenario, "full", llm=llm, repeat=repeat, split="dev", run_id="c-exp",
        recall_query=str(scenario["current"]),
        seed=install_lesson if with_lesson else None,
    ))
    first = (rec.get("request_log") or [{}])[0]
    prompt = str(rec.get("system") or "") + "\n" + str(rec.get("user") or "")
    return {
        "scenario": scenario["id"],
        "repeat": repeat,
        "lesson_in_prompt": "用过的办法" in prompt,
        "background_marked": "不一定适用" in prompt,
        "first_tools": list(first.get("tool_calls") or []),
        "called_read_file": any(_name_of(c) == "read_file"
                                for c in (first.get("tool_calls") or [])),
        "all_tools": [sorted(set(call)) for call in llm.seen],
        "reply": (rec.get("replies") or [""])[-1][:80],
    }


def test_experience_changes_tool_choice():
    os.environ.setdefault(harness.SYNTH_ENV, str(ROOT / "evals" / "expression" / "cards_synthetic"))
    rounds = int(os.environ.get("C_REPEATS") or 2)

    plain = [_run(LOCAL, i, with_lesson=False) for i in range(rounds)]
    with_lesson = [_run(LOCAL, i, with_lesson=True) for i in range(rounds)]
    public = [_run(PUBLIC, i, with_lesson=True) for i in range(rounds)]

    rows = ([{"arm": "A 无经验·本地资料", **r} for r in plain]
            + [{"arm": "B 有经验·本地资料", **r} for r in with_lesson]
            + [{"arm": "C 有经验·公共资料", **r} for r in public])
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")

    # 接线：没有经验时不出现，有经验时出现。
    # 是否被降级成「不一定适用」取决于 query 与经验用词的重合度——这是被测对象，
    # 不是断言对象：写死它会掩盖「同义不同词就退化成背景」这个真问题。
    assert not any(r["lesson_in_prompt"] for r in plain)
    assert all(r["lesson_in_prompt"] for r in with_lesson)
    assert all(r["lesson_in_prompt"] for r in public)
    # 模型选择只要求有记录，不许这里断言谁更好。
    assert all(r["all_tools"] for r in rows)
