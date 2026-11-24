"""A1 评测基础设施的验收：隔离、冻结、逐请求证据、配对与计分。

这些用例证明的是**评测本身可信**，不是鲸鲸说得好不好：
- 模型必须在沙箱之后构造，真实模型不走生产 bootstrap；
- 每一次请求都留 messages/tools/参数/状态/用量，回落与失败不被当成沉默；
- 冻结输入变了要报错，旧卡缺失要报错；
- 场景标签和模型看到的内容不能相反；
- 盲评拒绝重复票、错配和关键错误。
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from evals.expression import harness, review

ROOT = Path(__file__).resolve().parents[2]
SYNTH = ROOT / "evals" / "expression" / "cards_synthetic"


def _scenario(sid: str) -> dict:
    return next(s for s in harness.load_scenarios("dev") if s["id"] == sid)


class _TwoStep:
    """第一轮调一次 look，第二轮沉默——用来验证逐请求记录与沉默/失败之分。"""

    def __init__(self, *, model="fake-model", provider="fake-provider"):
        self.calls = 0
        self.model = model
        self.provider = provider

    async def chat_tools(self, *, messages, tools, **kwargs):
        self.calls += 1
        if self.calls == 1:
            return {"tool_calls": [{"id": "1", "name": "look",
                                    "arguments": {"ref": "win:Elaina"}}],
                    "content": "", "model": self.model, "provider": self.provider,
                    "tokens": 7, "usage": {"prompt_tokens": 5, "completion_tokens": 2}}
        return {"tool_calls": [], "content": "", "model": self.model,
                "provider": self.provider, "tokens": 3}


def test_sandbox_is_ready_before_the_model_is_built(monkeypatch):
    """模型构造时，库/日志/账本路径必须已经在临时目录里。"""
    monkeypatch.setenv(harness.SYNTH_ENV, str(SYNTH))
    seen = {}

    def factory():
        import os

        from db import world_db
        from core.infra import actions

        seen["env_dir"] = os.environ.get("YINYUE_WORLD_MODEL_DIR", "")
        seen["world_db"] = str(world_db.PATH)
        seen["actions"] = str(actions.DIR)
        return Capture()

    class Capture:
        async def chat_tools(self, *, messages, tools, **kwargs):
            return {"tool_calls": [], "content": "", "model": "c", "provider": "c",
                    "tokens": 0}

    rec = asyncio.run(harness.assemble(_scenario("d01"), "full", llm_factory=factory))
    for key, value in seen.items():
        assert value and ".test_tmp" in value.replace("\\", "/"), (key, value)
    assert rec["status"] == "ok"


def test_real_llm_does_not_take_the_production_bootstrap(monkeypatch):
    """构造真实客户端不许调用 ports.get_llm（它会 init_singletons(allow_production=True)）。"""
    from loop import ports

    def boom():
        raise AssertionError("评测客户端调用了生产 bootstrap")

    monkeypatch.setattr(ports, "get_llm", boom)
    llm = harness.RealLLM()
    assert type(llm.inner).__name__ == "LLMClient"


def test_every_request_is_recorded_with_status_and_usage(monkeypatch):
    monkeypatch.setenv(harness.SYNTH_ENV, str(SYNTH))
    llm = _TwoStep()
    rec = asyncio.run(harness.assemble(_scenario("d01"), "full", llm=llm))
    log = rec["request_log"]
    assert [r["seq"] for r in log] == [1, 2]
    assert [r["status"] for r in log] == ["ok", "silent"]
    assert log[0]["tool_calls"] == ["look"]
    assert log[0]["messages"] and log[0]["tools"], "请求必须带实际 messages/tools"
    assert log[0]["params"]["temperature"] == 0.7
    assert log[1]["returned"]["tokens"] == 3
    assert rec["outcome"] == "silent" and rec["valid"] is True
    assert rec["run_id"] == "" or isinstance(rec["run_id"], str)


def test_fallback_and_error_are_not_counted_as_silence():
    assert harness.request_status({"model": "qwen2.5:7b", "provider": "ollama"},
                                  requested_model="deepseek-chat",
                                  requested_provider="deepseek") == "fallback"
    assert harness.request_status({"error": "402 Payment Required"}) == "error"
    assert harness.request_status({"tool_calls": [], "content": ""}) == "silent"
    assert harness.request_status({"tool_calls": [{"name": "say"}]}) == "ok"


def test_scenario_premises_reach_the_model(monkeypatch):
    """d15 群聊没点名 → 不许出现 ★；d16 点名 → 必须有 ★；d11 有先前失败回执。"""
    monkeypatch.setenv(harness.SYNTH_ENV, str(SYNTH))

    class Capture:
        async def chat_tools(self, *, messages, tools, **kwargs):
            return {"tool_calls": [], "content": "", "model": "c", "provider": "c",
                    "tokens": 0}

    free = asyncio.run(harness.assemble(_scenario("d15"), "full", llm=Capture()))
    assert not free["premise_problems"], free["premise_problems"]
    assert "★1 群「茶话会」" not in free["user"]
    assert "群「茶话会」" in free["user"]

    called = asyncio.run(harness.assemble(_scenario("d16"), "full", llm=Capture()))
    assert not called["premise_problems"], called["premise_problems"]
    assert "★1 群「茶话会」" in called["user"]

    failed = asyncio.run(harness.assemble(_scenario("d11"), "full", llm=Capture()))
    assert not failed["premise_problems"], failed["premise_problems"]
    assert "database is locked" in failed["system"] + failed["user"]
    assert not (_scenario("d11").get("tool_stub") or {}).get("remember"), \
        "失败不能靠这一拍再存一次来制造"


def _fake_rec(sid, variant, *, model="m1", provider="p1", repeat=0, replies=("嗯",),
              valid=True, kind="single"):
    return {
        "scenario": sid, "variant": variant, "repeat": repeat, "kind": kind,
        "replies": list(replies), "valid": valid, "status": "ok" if valid else "failed",
        "invalid_reason": "" if valid else "error:x",
        "model": {"model": model, "provider": provider, "temperature": 0.7,
                  "adapter": "RealLLM"},
        "frozen": {"fixtures_version": "t", "scenarios_dev": "s", "prompt_baseline": "p",
                   "persona_baseline": "b", "persona_candidate": "c"},
        "split": "dev",
    }


def test_review_pairs_by_frozen_input_and_rejects_bad_cases(tmp_path, monkeypatch):
    runs = tmp_path / "runs"
    blind = tmp_path / "blind"
    rd = runs / "t1"
    rd.mkdir(parents=True)
    files = {
        "d01.baseline.r0.json": _fake_rec("d01", "baseline"),
        "d01.full.r0.json": _fake_rec("d01", "full"),
        # 同一案例两条 baseline → duplicate
        "d02.baseline.r0.json": _fake_rec("d02", "baseline"),
        "d02.baseline.r0b.json": _fake_rec("d02", "baseline"),
        "d02.full.r0.json": _fake_rec("d02", "full"),
        # 模型不一致 → not_paired
        "d03.baseline.r0.json": _fake_rec("d03", "baseline"),
        "d03.full.r0.json": _fake_rec("d03", "full", model="m2"),
        # 运行失败 → invalid_run
        "d04.baseline.r0.json": _fake_rec("d04", "baseline"),
        "d04.full.r0.json": _fake_rec("d04", "full", valid=False),
        # 多轮：保留轮次与沉默
        "d19.baseline.r0.json": _fake_rec("d19", "baseline", kind="multi",
                                          replies=("a", "", "c")),
        "d19.full.r0.json": _fake_rec("d19", "full", kind="multi",
                                      replies=("a", "", "d")),
    }
    for name, payload in files.items():
        (rd / name).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    monkeypatch.setattr(review, "RUNS", runs)
    monkeypatch.setattr(review, "BLIND", blind)
    args = type("A", (), {"split": "dev", "seed": 3, "run_id": "t1"})()
    assert review.cmd_build(args) == 0

    meta = json.loads((blind / "dev-key.json").read_text(encoding="utf-8"))["_meta"]
    reasons = {x["id"]: x["reason"] for x in meta["skipped"]}
    assert reasons["d02#r0"] == "duplicate"
    assert reasons["d03#r0"] == "not_paired"
    assert reasons["d04#r0"] == "invalid_run"
    assert meta["paired"] == 2 and meta["expected"] >= 5
    assert meta["coverage"] < 1.0

    packet = [json.loads(l) for l in (blind / "dev.jsonl").read_text(encoding="utf-8").splitlines()]
    multi = next(x for x in packet if x["id"].startswith("d19"))
    assert "（未开口）" in multi["A"] or "（未开口）" in multi["B"]

    key = json.loads((blind / "dev-key.json").read_text(encoding="utf-8"))["items"]
    item = next(iter(key))
    votes = tmp_path / "votes.jsonl"
    rows = [
        {"id": item, "reviewer": "r1", "prefer": "a",
         "scores": {"A": {d: 5 for d in review.DIMENSIONS},
                    "B": {d: 3 for d in review.DIMENSIONS}},
         "critical": {"A": {"事实": 0}, "B": {"事实": 1}}},
        {"id": item, "reviewer": "r1", "prefer": "b"},  # 同一评审人重复票
        {"id": "nope", "reviewer": "r1", "prefer": "a"},
    ]
    votes.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows), encoding="utf-8")
    import io
    from contextlib import redirect_stdout

    buf = io.StringIO()
    with redirect_stdout(buf):
        review.cmd_score(type("A", (), {"split": "dev", "votes": str(votes),
                                        "min_decided": 1, "min_coverage": 0.0})())
    out = json.loads(buf.getvalue())
    assert out["duplicate_votes"] and out["deduped_votes"] == 1
    assert out["invalid_votes"][0]["id"] == "nope"
    assert out["critical_errors"]["full"]["事实"] == 1
    assert out["gate"]["no_critical_errors"] is False


class _StickerOnce:
    async def chat_tools(self, *, messages, tools, **kwargs):
        if any(msg["role"] == "tool" for msg in messages):
            return {"content": "", "tool_calls": []}
        return {"tool_calls": [{"id": "k1", "name": "sticker",
                                "arguments": {"key": "开心", "why": "测试"}}],
                "content": "", "model": "sticker", "provider": "sticker", "tokens": 0}


def test_eval_exposes_stickers_and_records_the_choice(monkeypatch):
    """夹具必须真的给她表情包，否则“0% 用表情”只是没给，不是不用。"""
    monkeypatch.setenv(harness.SYNTH_ENV, str(SYNTH))
    rec = asyncio.run(harness.assemble(_scenario("d01"), "full", llm=_StickerOnce()))
    prompt = rec["system"] + "\n" + rec["user"]
    assert "我手上有这几张表情" in prompt
    assert "开心" in prompt and "晚安" in prompt
    receipts = [r for t in rec["turns"] for r in t["receipts"]]
    sticker = next(r for r in receipts if r.get("act") == "sticker")
    assert sticker["result"] == "rehearsal" and sticker["text"] == "开心"
    assert rec["replies"][0] == "（表情：开心）"
    assert rec["outcome"] == "said"



class _SeveralMessages:
    async def chat_tools(self, *, messages, tools, **kwargs):
        history = "\n".join(msg.get("content") or "" for msg in messages)
        if "下一轮的问题" in history or any(msg["role"] == "tool" for msg in messages):
            return {"content": "只有自己看见的草稿", "tool_calls": []}
        return {"content": "只有自己看见的思考", "tool_calls": [
            {"id": "s1", "name": "say", "arguments": {"text": "第一条可见消息"}},
            {"id": "k1", "name": "sticker", "arguments": {"key": "开心"}},
            {"id": "s2", "name": "say", "arguments": {"text": "第二条可见消息"}},
        ]}


class _OneSayPerTurn:
    """一拍只执行第一个工具，成功 say 后本回合收工；这条替身每轮只说一句。"""

    def __init__(self, texts):
        self.texts = list(texts)
        self.turns = 0

    async def chat_tools(self, *, messages, tools, **kwargs):
        if any(msg["role"] == "tool" for msg in messages):
            return {"content": "只有自己看见的草稿", "tool_calls": []}
        text = self.texts[min(self.turns, len(self.texts) - 1)]
        self.turns += 1
        return {"content": "只有自己看见的思考", "tool_calls": [
            {"id": "s%d" % self.turns, "name": "say", "arguments": {"text": text}}]}


def test_eval_preserves_each_visible_message_in_review_and_next_prompt(monkeypatch):
    monkeypatch.setenv(harness.SYNTH_ENV, str(SYNTH))
    sc = {**_scenario("d01"), "kind": "multi", "turns": ["先问一下", "下一轮的问题"]}
    rec = asyncio.run(harness.assemble(sc, "full", llm=_OneSayPerTurn(
        ["第一条可见消息", "第二条可见消息"])))
    assert rec["turns"][0]["said"] == ["第一条可见消息"]
    assert rec["turns"][1]["said"] == ["第二条可见消息"]
    assert rec["replies"] == ["第一条可见消息", "第二条可见消息"]
    assert review._variant_text(rec) == ("第1轮：第一条可见消息\n"
                                         "第2轮：第二条可见消息")
    prompt = "\n".join(msg["content"] for msg in rec["request_log"][1]["messages"])
    assert "我: 第一条可见消息" in prompt
    assert "只有自己看见的" not in prompt


def test_eval_does_not_report_unexecuted_calls_as_spoken(monkeypatch):
    """同一批里第二个 say 没有执行，回放必须留白，不能假装已经发出。"""
    monkeypatch.setenv(harness.SYNTH_ENV, str(SYNTH))
    rec = asyncio.run(harness.assemble(_scenario("d01"), "full", llm=_SeveralMessages()))
    said_receipts = [r.get("text") for r in rec["turns"][0]["receipts"]
                     if r.get("act") == "say"]
    assert said_receipts == ["第一条可见消息"]
    assert rec["replies"] == ["第一条可见消息"]
    assert "（表情：开心）" not in rec["replies"][0]


def test_eval_excludes_blocked_messages_from_speech_and_review(monkeypatch):
    monkeypatch.setenv(harness.SYNTH_ENV, str(SYNTH))
    sc = {**_scenario("d01"), "tool_stub": {"say": {"result": "blocked"},
                                               "sticker": {"result": "error"}}}
    rec = asyncio.run(harness.assemble(sc, "full", llm=_SeveralMessages()))
    assert rec["turns"][0]["said"] == []
    assert rec["replies"] == [""]
    assert rec["outcome"] == "failed"
    assert review._variant_text(rec) == "（未开口）"

    old_summary = {**rec, "replies": ["第一条可见消息"]}
    old_summary["turns"][0]["said"] = ["第一条可见消息", "第二条可见消息"]
    assert review._variant_text(old_summary) == "（未开口）"


def test_review_recovers_both_legacy_say_texts_when_receipts_are_missing():
    rec = {"kind": "single", "turns": [{"said": ["第一句", "第二句"]}], "replies": ["第一句"]}
    assert review._variant_text(rec) == "第一句\n第二句"



def test_new_decision_guidance_is_absent_from_frozen_comparison_variants(monkeypatch):
    from loop import brain

    monkeypatch.setenv(harness.SYNTH_ENV, str(SYNTH))
    guidance = brain._decision_guidance()
    assert "不机械报成固定标签或三段式" in guidance
    for variant in harness.VARIANTS:
        rec = asyncio.run(harness.assemble(_scenario("d01"), variant))
        assert rec["system"].count(guidance) == (1 if variant == "full" else 0)
