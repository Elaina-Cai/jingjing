"""WP-08 8.1 canonical decision trace v2 单测。

Run directly:
    python tests/test_trace_canonical_v2.py
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import uuid
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

import _tempdirs  # noqa: E402,F401 - 把 TEMP 重定向到仓库内可写基座

from agents.base import AgentContext  # noqa: E402
from core.observability.decision_trace import (  # noqa: E402
    SCHEMA_VERSION,
    TRACE_SINKS,
    build_decision_trace_row,
    ensure_trace_id,
    mark_projection,
    new_trace_id,
    record_decision,
    record_detail,
    trace_route_decisions,
    trace_turn_outcome,
    trace_trigger_decision,
    use_trace_id,
)
from core.routing.turn_decision import TurnDecision  # noqa: E402


ROOT = Path(__file__).resolve().parent.parent


@contextmanager
def _tmpdir():
    path = ROOT / ".tmp_test" / f"trace_v2_{os.getpid()}_{uuid.uuid4().hex[:8]}"
    path.mkdir(parents=True, exist_ok=True)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


@contextmanager
def _env(**values):
    saved = {key: os.environ.get(key) for key in values}
    for key, value in values.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    try:
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _context() -> AgentContext:
    return AgentContext(
        extra={
            "platform": "wechat",
            "account_id": "bot1",
            "chat_id": "c1",
            "is_group": False,
        },
        trigger="user_command",
    )


def _message() -> dict:
    return {
        "chat_id": "c1",
        "sender_id": "u1",
        "sender_name": "owner",
        "content": "hello",
        "source": "wechat",
        "account_id": "bot1",
        "is_self": 0,
        "msg_type_name": "text",
        "chat_type": "private",
        "msg_id": "m1",
        "created_at": "2026-10-09T12:00:00",
    }


def _read_one(path: Path) -> dict:
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert lines, f"no trace rows: {path}"
    return json.loads(lines[-1])


def test_route_row_is_v2_with_trace_id() -> bool:
    ctx = _context()
    decision = TurnDecision(
        action="reply_now",
        reason="mentioned_in_group",
        trigger_reasons=("mentioned_in_group",),
        reply_platform="wechat",
        reply_account="self",
        is_group=False,
    )
    with _tmpdir() as tmp:
        path = Path(tmp) / "decision_trace.jsonl"
        written = trace_route_decisions([_message()], [decision], ctx, force=True, path=path)
        assert written == 1, written
        row = _read_one(path)
    assert row["schema"] == SCHEMA_VERSION == 2, row.get("schema")
    assert ctx.trace_id and row["trace_id"] == ctx.trace_id, (ctx.trace_id, row.get("trace_id"))
    assert row["turn_id"] == ctx.trace_id
    assert row["source"] == "direct"
    assert row["decision"]["action"] == "reply_now"
    assert row["decision"]["reason"] == "mentioned_in_group"
    assert row["decision"]["chain"] == ["mentioned_in_group"]
    assert row["decision"]["reason_text"] == "有人在群里 @ 了她", row["decision"]
    assert row["decision"]["should_reply"] is True
    for section in ("facts", "decision", "relevance", "understanding", "generation", "outbound", "projections"):
        assert section in row, section
    assert isinstance(row["event"], dict)
    return True


def test_trigger_row_is_v2_and_uses_given_trace_id() -> bool:
    decision = TurnDecision(
        action="reply_now",
        reason="ambient",
        trigger_reasons=("ambient",),
        is_group=True,
    )
    question = SimpleNamespace(
        msg_id="m9", sender_id="u9", sender_name="friend",
        content="hello group", created_at="2026-10-09T12:01:00",
    )
    with _tmpdir() as tmp:
        path = Path(tmp) / "decision_trace.jsonl"
        ok = trace_trigger_decision(
            decision,
            question=question,
            chat_id="g1@chatroom",
            trace_id="tid-trigger-1",
            force=True,
            path=path,
        )
        assert ok is True
        row = _read_one(path)
    assert row["schema"] == SCHEMA_VERSION
    assert row["trace_id"] == "tid-trigger-1"
    assert row["source"] == "trigger"
    assert row["chat_id"] == "g1@chatroom"
    assert row["decision"]["chain"] == ["ambient"]
    assert row["facts"]["is_group"] is True
    return True


def test_bound_trace_id_lands_on_canonical_row() -> bool:
    with _tmpdir() as tmp:
        canonical_path = Path(tmp) / "decision_trace.jsonl"
        with use_trace_id("tid-shared"):
            recorded = record_decision(
                {"kind": "probe", "note": "bound trace"},
                force=True,
                path=canonical_path,
            )
        assert recorded is True
        canonical_row = _read_one(canonical_path)
    assert canonical_row["trace_id"] == "tid-shared", canonical_row
    return True

def test_registry_covers_five_sinks() -> bool:
    expected = {
        "decision_trace.jsonl",
        "admission_trace.jsonl",
        "intent_trace.jsonl",
        "context_trace.jsonl",
        "turns_trace.jsonl",
    }
    assert set(TRACE_SINKS) == expected, sorted(TRACE_SINKS)
    assert TRACE_SINKS["decision_trace.jsonl"]["canonical"] is True
    # R0：position_gate 整模块已删，该汇点不再登记
    assert "position_gate_trace.jsonl" not in TRACE_SINKS
    assert TRACE_SINKS["turns_trace.jsonl"]["writer"] is None
    assert TRACE_SINKS["turns_trace.jsonl"]["deletion_ready"] is True
    for merged in ("admission_trace.jsonl", "intent_trace.jsonl", "context_trace.jsonl"):
        assert TRACE_SINKS[merged]["deletion_ready"] is True, merged
        assert TRACE_SINKS[merged]["status"] == "merged_into_canonical", merged
    for name, spec in TRACE_SINKS.items():
        assert "status" in spec, name
        assert "readers" in spec, name
        assert "deletion_ready" in spec, name
    return True


def test_ensure_trace_id_is_stable() -> bool:
    ctx = AgentContext(extra={})
    first = ensure_trace_id(ctx)
    second = ensure_trace_id(ctx)
    assert first and second == first == ctx.trace_id
    assert new_trace_id() != first
    return True


def test_build_row_keeps_legacy_decision_fields() -> bool:
    row = build_decision_trace_row(
        trace_id="tid-x",
        decision=TurnDecision(action="silent", reason="", is_group=False),
        event={"chat_id": "c1", "platform": "wechat", "account_id": "bot1"},
        source="direct",
    )
    assert row["decision"]["should_reply"] is False
    assert row["decision"]["action"] == "silent"
    assert row["generation"]["silent"] is True
    assert row["outbound"] == {"sent": None, "length": None}
    return True


def test_outcome_row_fills_sections_and_projections() -> bool:
    decision = TurnDecision(
        action="reply_now",
        reason="owner_private_message",
        trigger_reasons=("owner_private_message",),
    )
    policy_decision = SimpleNamespace(action=SimpleNamespace(value="allow"))
    context = SimpleNamespace(
        extra={
            "chat_id": "c1",
            "platform": "wechat",
            "account_id": "bot1",
            "is_group": False,
            "_last_sent_ok": True,
        },
        policy_decision=policy_decision,
    )
    ctx = SimpleNamespace(
        decision=decision,
        context=context,
        outcome_context=context,
        trace_id="tid-out",
        origin="direct",
        chat_id="c1",
        question=None,
        outcome={"turns": {"n": 2, "chars": 10, "hash": "abc", "roles": "UU"}},
    )
    with _tmpdir() as tmp:
        path = Path(tmp) / "decision_trace.jsonl"
        with use_trace_id("tid-out"):
            mark_projection("intent_trace.jsonl")
            assert trace_turn_outcome(ctx, reply="hello", force=True, path=path)
        row = _read_one(path)
    assert row["kind"] == "turn_outcome", row.get("kind")
    assert row["trace_id"] == "tid-out"
    assert row["understanding"]["intent"] == ""
    assert row["understanding"]["addressee"] == ""
    assert row["generation"]["policy_action"] == "allow"
    assert row["outbound"] == {"sent": True, "length": 5}, row["outbound"]
    assert row["projections"] == {"intent_trace.jsonl": "tid-out"}, row["projections"]
    assert row["facts"]["turns"]["n"] == 2, row["facts"].get("turns")
    assert row["facts"]["turns"]["hash"] == "abc"
    return True


def test_generate_turn_wires_trigger_and_outcome_trace() -> bool:
    import ast

    source = (ROOT / "core" / "routing" / "turn_pipeline.py").read_text(encoding="utf-8-sig")
    tree = ast.parse(source)
    fn = None
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "generate_turn":
            fn = node
            break
    assert fn is not None, "generate_turn 不存在"
    calls = {
        getattr(node.func, "id", None) or getattr(node.func, "attr", None)
        for node in ast.walk(fn)
        if isinstance(node, ast.Call)
    }
    assert "trace_trigger_decision" in calls, sorted(c for c in calls if c)
    assert "trace_turn_outcome" in calls, sorted(c for c in calls if c)
    assert "use_trace_id" in calls, sorted(c for c in calls if c)
    return True


def test_record_detail_merges_into_canonical_file() -> bool:
    with _tmpdir() as tmp:
        path = Path(tmp) / "decision_trace.jsonl"
        with use_trace_id("tid-detail"):
            ok = record_detail(
                "intent_detail",
                {"intent": "question", "evidence": ["x"]},
                path=path,
            )
        assert ok is True
        row = _read_one(path)
    assert row["kind"] == "intent_detail", row.get("kind")
    assert row["trace_id"] == "tid-detail"
    assert row["intent"] == "question"
    return True


def test_trace_rotates_by_size() -> bool:
    with _tmpdir() as tmp:
        path = Path(tmp) / "decision_trace.jsonl"
        keys = ("YINYUE_DECISION_TRACE_MAX_MB", "YINYUE_DECISION_TRACE_BACKUPS")
        saved = {key: os.environ.get(key) for key in keys}
        os.environ["YINYUE_DECISION_TRACE_MAX_MB"] = "0.001"
        os.environ["YINYUE_DECISION_TRACE_BACKUPS"] = "2"
        try:
            for index in range(12):
                assert record_decision(
                    {"kind": "probe", "index": index, "blob": "x" * 200},
                    force=True,
                    path=path,
                )
            current = path.read_text(encoding="utf-8").splitlines()
            backup1 = Path(str(path) + ".1")
            backup2 = Path(str(path) + ".2")
            assert backup1.exists(), "缺 .1 备份"
            assert backup2.exists(), "缺 .2 备份"
            assert current, "当前文件为空"
            assert len(current) < 12, len(current)
            assert not Path(str(path) + ".3").exists(), "备份数超过配置"
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
    return True


def main() -> bool:
    tests = [
        test_route_row_is_v2_with_trace_id,
        test_trigger_row_is_v2_and_uses_given_trace_id,
        test_bound_trace_id_lands_on_canonical_row,
        test_registry_covers_five_sinks,
        test_ensure_trace_id_is_stable,
        test_build_row_keeps_legacy_decision_fields,
        test_outcome_row_fills_sections_and_projections,
        test_generate_turn_wires_trigger_and_outcome_trace,
        test_record_detail_merges_into_canonical_file,
        test_trace_rotates_by_size,
    ]
    failed = 0
    for fn in tests:
        try:
            fn()
            print(f"  ok   {fn.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"  FAIL {fn.__name__}: {exc!r}")
    print(f"\ntrace canonical v2: {len(tests) - failed}/{len(tests)} passed")
    return failed == 0


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
