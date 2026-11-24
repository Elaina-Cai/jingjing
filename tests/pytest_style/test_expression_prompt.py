"""表达改造的装配守卫（2026-11-17，A1 后重写）。

锁的是「现役 run_loop 真的用了同一份表达原则」，不是文案本身：
- 两个 PROMPT_SHAPE_V2 布局都只注入一次；
- baseline 用**冻结旧卡**（fixtures.json 的路径+散列），不再跟着 active 卡漂；
- 候选卡与基线卡的差别只在表达句（真实人格资产的检查在缺文件时明确跳过）；
- 样本标了「虚构」，不落进世界/记忆；
- 旧固定台词（别等了 / 刚说了 / 7% / 十几二十字）确实被清掉，
  但保存、送达、授权、时间的事实边界还在；
- 工具 schema、温度、收件人不因表达改造变化；
- 试衣间不外发、不写业务状态。

接线测试一律用 `evals/expression/cards_synthetic/` 的合成卡，不依赖 .test_tmp
和被忽略的生产人设；真实人格只在内容断言里按 fixtures.json 的路径读，缺了就跳过。
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from core.contracts import limits
from core.policy import channel
from evals.expression import harness
from loop import expression, selfcard

ROOT = Path(__file__).resolve().parents[2]
SYNTH = ROOT / "evals" / "expression" / "cards_synthetic"


def _scenario(sid: str) -> dict:
    return next(s for s in harness.load_scenarios("dev") if s["id"] == sid)


def _run(sid: str, variant: str, monkeypatch, shape: str = "1", synthetic: bool = True) -> dict:
    if synthetic:
        monkeypatch.setenv(harness.SYNTH_ENV, str(SYNTH))
    else:
        monkeypatch.delenv(harness.SYNTH_ENV, raising=False)
    monkeypatch.setenv("YINYUE_EXPRESSION_SHAPE", shape)
    return asyncio.run(harness.assemble(_scenario(sid), variant))


def _text(rec: dict) -> str:
    return rec["system"] + "\n" + rec["user"]


def _real_pins_ready() -> bool:
    try:
        return all(harness._persona_pin(role)["path"].exists()
                   for role in ("baseline", "candidate"))
    except SystemExit:
        return False


def test_principles_live_in_exactly_one_module():
    """通用表达原则只能有一个源文件；别处再抄同义段落会在这里红。"""
    skip = {"tests", "backups", "output", ".test_tmp", ".venv", ".venv_it", ".venv_iso",
            ".venv_tts", "data", "logs", "napcat", "node_modules", "__pycache__"}

    def files():
        for top in ROOT.iterdir():
            if top.is_dir():
                if top.name not in skip:
                    yield from top.rglob("*.py")
            elif top.suffix == ".py":
                yield top

    hits = [str(p.relative_to(ROOT)).replace("\\", "/")
            for p in files()
            if "[怎么说话]" in p.read_text(encoding="utf-8", errors="replace")]
    assert hits == ["loop/expression.py"], hits


def test_principles_appear_once_in_both_layouts(monkeypatch):
    needle = expression.conversation_principles()
    for shape in ("1", "0"):
        rec = _run("d01", "full", monkeypatch, shape=shape)
        assert _text(rec).count(needle) == 1, (shape, _text(rec).count(needle))
        base = _run("d01", "baseline", monkeypatch, shape=shape)
        assert needle not in _text(base), shape


def test_baseline_is_pinned_and_reproducible(monkeypatch):
    """baseline 必须逐字来自冻结旧卡：同一输入两次结果相同，且不出现候选卡的字。

    2026-11-17 写卡后 baseline 跟着 active 卡漂，这条就是那次事故的回归。
    """
    one = _run("d01", "baseline", monkeypatch)
    two = _run("d01", "baseline", monkeypatch)
    assert one["sha256"] == two["sha256"], "同一冻结输入两次装配必须同一份 prompt"
    assert "十几二十字就够" in one["system"], "合成基线卡的表达句必须出现"
    assert "长度跟着要说的事走" not in one["system"], "baseline 不许读候选卡"
    assert one["frozen"].get("persona_baseline"), "运行记录必须带人设散列"


def test_persona_pin_refuses_missing_old_card(tmp_path, monkeypatch):
    """旧卡缺失时报错，不许静默回退到当前 active 卡。"""
    monkeypatch.setenv(harness.SYNTH_ENV, str(tmp_path))
    with pytest.raises(SystemExit) as err:
        harness.persona_body("baseline")
    assert "缺失" in str(err.value)


def test_candidate_and_expression_only_are_distinguishable(monkeypatch):
    base = _run("d01", "baseline", monkeypatch)
    expr = _run("d01", "expression", monkeypatch)
    full = _run("d01", "full", monkeypatch)
    assert "十几二十字就够" in base["system"]
    assert "别等了" in base["user"]
    # expression = 只换人设表达（候选卡 + 共同原则），旧规则/旧话术仍在。
    assert "长度跟着要说的事走" in expr["system"]
    assert expression.conversation_principles() in expr["system"]
    assert "别等了" in expr["user"], "expression 变体应保留旧拒绝话术"
    # full = 候选卡 + 共同原则 + 冲突清理。
    for old in ("十几二十字就够", "第一句先有反应", "别等了", "刚说了", "只有 7%"):
        assert old not in _text(full), old


@pytest.mark.skipif(not _real_pins_ready(), reason="真实人格原文不在本机（冻结路径缺失）")
def test_candidate_keeps_header_and_identity_sections():
    """身份、关系、声线、外观是资产：候选只改表达句。"""
    base_raw = harness._persona_pin("baseline")["path"].read_text(encoding="utf-8")
    cand_raw = harness._persona_pin("candidate")["path"].read_text(encoding="utf-8")
    for line in ("name: 鲸鲸", "voice: jingjing", "looks: jingjing"):
        assert line in base_raw and line in cand_raw
    a = selfcard._split_sections(selfcard._parse_md(base_raw)[1])
    c = selfcard._split_sections(selfcard._parse_md(cand_raw)[1])
    assert a["我是谁"] == c["我是谁"], "身份与关系设定必须逐字保留"
    assert a["我不知道"] == c["我不知道"], "事实边界段必须逐字保留"


@pytest.mark.skipif(not _real_pins_ready(), reason="真实人格原文不在本机（冻结路径缺失）")
def test_samples_are_marked_fictional_and_stay_out_of_the_world(monkeypatch):
    rec = _run("d01", "full", monkeypatch, synthetic=False)
    assert "虚构示范" in rec["system"]
    assert "不是我们之间发生过的事" in rec["system"]
    # 样本正文只该在 system；user 侧是世界与账，不能把示范当事实注入。
    assert "幂等就是同一个操作" in rec["system"]
    assert "幂等就是同一个操作" not in rec["user"]
    assert "下面都是虚构示范" not in rec["user"]


def test_samples_do_not_write_memory(monkeypatch):
    rec = _run("d01", "full", monkeypatch)
    acts = [str(r.get("act") or "") for t in rec["turns"] for r in t["receipts"]]
    assert "remember" not in acts and "promise" not in acts


def test_save_and_delivery_evidence_boundaries_survive():
    walls = (ROOT / "loop" / "brain.py").read_text(encoding="utf-8")
    for key in ("只有写入成功回执才说「记下了」", "只有写入失败回执才说「这次没记成」",
                "没有写入回执就说「还不能确认有没有记下」", "任务成功回执只证明回执对应的动作成功",
                "不能把重新授权当作核验手段", "只有 say 会真的发出去"):
        assert key in walls, key
    text = limits.refusal_text()
    assert "证据不足时不下「永久做不了」的结论" in text
    assert "别等了" not in text


def test_channel_ask_hint_has_no_quota_and_is_used(monkeypatch):
    assert "7%" not in channel.DEFAULT.ask_hint
    assert "需要了解的信息确实会改变回答时再问" in channel.DEFAULT.ask_hint
    rec = _run("d01", "full", monkeypatch)
    assert channel.DEFAULT.ask_hint in rec["user"]


def test_tools_and_temperature_do_not_change_between_variants(monkeypatch):
    base = _run("d01", "baseline", monkeypatch)
    full = _run("d01", "full", monkeypatch)
    assert base["tools"] == full["tools"]
    assert base["model"]["temperature"] == full["model"]["temperature"] == 0.7


class _SayOnce:
    def __init__(self, target: str, text: str):
        self.target, self.text, self.calls = target, text, 0

    async def chat_tools(self, *, messages, tools, **kwargs):
        self.calls += 1
        if self.calls > 1:
            return {"tool_calls": [], "content": "", "model": "say", "provider": "say",
                    "tokens": 0}
        return {"tool_calls": [{"id": "s1", "name": "say", "arguments": {
            "target": self.target, "text": self.text, "why": "test"}}],
            "content": "", "model": "say", "provider": "say", "tokens": 0}


def test_say_target_and_text_are_not_rewritten(monkeypatch):
    """表达改造不碰工具参数：收件人与原文逐字进回执。"""
    monkeypatch.setenv("YINYUE_EXPRESSION_SHAPE", "1")
    monkeypatch.setenv(harness.SYNTH_ENV, str(SYNTH))
    llm = _SayOnce("wxid_eval_owner", "收到，我在。")
    rec = asyncio.run(harness.assemble(_scenario("d01"), "full", llm=llm))
    receipts = [r for t in rec["turns"] for r in t["receipts"]]
    said = [r for r in receipts if r.get("act") == "say"]
    assert said and said[0]["target"] == "wxid_eval_owner"
    assert said[0]["text"] == "收到，我在。"
    assert said[0]["result"] == "rehearsal"


def test_rehearsal_never_sends_or_writes(monkeypatch):
    """脚本模型故意调 say/do/show/remember/promise：全部只留在试衣间。"""
    monkeypatch.setenv("YINYUE_EXPRESSION_SHAPE", "1")
    monkeypatch.setenv(harness.SYNTH_ENV, str(SYNTH))
    probe = harness.ProbeLLM("wxid_eval_owner")
    rec = asyncio.run(harness.assemble(_scenario("d08"), "full", llm=probe, max_steps=6))
    receipts = [r for t in rec["turns"] for r in t["receipts"]]
    acts = {str(r.get("act")): str(r.get("result")) for r in receipts}
    assert acts.get("say") == "rehearsal"
    assert acts.get("do") == "blocked"
    assert acts.get("remember") == "blocked"
    assert acts.get("promise") == "blocked"
    assert acts.get("show") == "rehearsal"
