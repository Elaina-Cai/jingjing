# -*- coding: utf-8 -*-
"""A2：同一句话，只换引用对象。

施工单要求「群里相同原句分别引用鲸鲸和其他人」。这一组对照只允许两个地方不同：
引用的是谁、以及那句话原本是谁说的；system 必须逐字相同，其余前文不变。

新场景集 `scenarios_ref.jsonl` 单独冻结，不动 dev/holdout 的散列（见 fixtures.json）。
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from evals.expression import harness

ROOT = Path(__file__).resolve().parents[2]
SYNTH = ROOT / "evals" / "expression" / "cards_synthetic"
SCEN = {item["id"]: item for item in harness.load_scenarios("ref")}


class _Capture:
    async def chat_tools(self, *, messages, tools, **kwargs):
        return {"tool_calls": [], "content": "", "model": "c", "provider": "c", "tokens": 0}


def _rec(sid: str, monkeypatch) -> dict:
    monkeypatch.setenv(harness.SYNTH_ENV, str(SYNTH))
    return asyncio.run(harness.assemble(SCEN[sid], "full", llm=_Capture()))


@pytest.mark.parametrize("me,other,quote", [("q1a", "q1b", "我把测试库清了"),
                                            ("q2a", "q2b", "今天不加班了")])
def test_only_the_quote_target_changes(me, other, quote, monkeypatch):
    a, b = _rec(me, monkeypatch), _rec(other, monkeypatch)
    assert a["system"] == b["system"], "换引用对象不许连表达原则/人设一起换"
    diff = [
        (left, right)
        for left, right in zip(a["user"].splitlines(), b["user"].splitlines())
        if left != right
    ]
    assert len(diff) == 2, "只该有两行不同（引用谁、那句原本谁说的）：%r" % (diff,)
    assert all(quote in left for left, _ in diff)


def test_quoting_her_and_quoting_someone_else_are_distinguishable(monkeypatch):
    mine, theirs = _rec("q1a", monkeypatch), _rec("q1b", monkeypatch)
    assert "↳ 鲸鲸 引用那句" in mine["user"]
    assert "↳ 阿岚 引用那句" not in mine["user"]
    assert "↳ 阿岚 引用那句" in theirs["user"]
    assert "↳ 鲸鲸 引用那句" not in theirs["user"]
    # 那句原本是我说的 / 是阿岚说的，也必须跟着对。
    assert "再往前 11-17 20:00 我: 我把测试库清了" in mine["user"]
    assert "再往前 11-17 20:00 阿岚: 我把测试库清了" in theirs["user"]


def test_group_speakers_are_not_flattened_into_one_person(monkeypatch):
    """以前三个非 assistant 行都会被写成「Elaina」，群里两个人看起来像一个人。"""
    rec = _rec("q1b", monkeypatch)
    assert "P6" in _rec("q2b", monkeypatch)["user"]
    assert "阿岚" in rec["user"]
    assert "Elaina" not in rec["user"]


def test_the_pair_is_a_quote_not_a_mention(monkeypatch):
    """两版都不该被当成「被点名」——否则差异就变成「叫没叫我」，不是「引用谁」。"""
    for sid in ("q1a", "q1b", "q2a", "q2b"):
        rec = _rec(sid, monkeypatch)
        cell = [line for line in rec["user"].splitlines() if "茶话会" in line]
        assert cell, rec["user"][:400]
        assert not any("★" in line for line in cell), "%s 被当成了被点名：%r" % (sid, cell)
