# -*- coding: utf-8 -*-
"""Step 10：意图执行半环（due/observe/expire/persist）。"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.context.intention_runner import (  # noqa: E402
    IntentionExecution,
    expire_intentions,
    match_observe,
    run_due,
    run_due_retryable,
)
from core.context.situation import (  # noqa: E402
    WorldModel,
    apply_world_updates,
    open_intention,
    open_intentions,
    project,
)
from core.context.situation_store import (  # noqa: E402
    get_world_model,
    persist_world_model,
    reset_world_models,
)


def test_run_due_executes_and_closes() -> None:
    from core.context import goal_close

    model = WorldModel()
    open_intention(model, "提醒:喝水", kind="remind", at="08:00", due_at="08:00")
    original = goal_close._verified_ref
    goal_close._verified_ref = lambda *args, **kwargs: True
    try:
        runs = run_due(
            model,
            "08:01",
            lambda intent: IntentionExecution(
                ok=True,
                result="已提醒",
                evidence_ref="reminder:1",
            ),
        )
    finally:
        goal_close._verified_ref = original
    assert len(runs) == 1 and runs[0].ok is True
    view = project(model.events)
    assert view.intentions["提醒:喝水"].status == "closed"
    assert view.intentions["提醒:喝水"].result == "已提醒"


def test_run_due_waits_until_due() -> None:
    model = WorldModel()
    open_intention(model, "提醒:喝水", kind="remind", at="08:00", due_at="09:00")
    assert run_due(model, "08:30", lambda intent: "x") == ()
    assert open_intentions(model, kind="remind")[0].status == "open"


def test_run_due_does_not_execute_a_revoked_source(monkeypatch) -> None:
    from core.memory import belief_commit

    model = WorldModel(scope={
        "platform": "wechat",
        "account_id": "bot",
        "chat_id": "c1",
    })
    open_intention(
        model,
        "提醒:撤回",
        kind="remind",
        at="08:00",
        due_at="08:00",
        evidence_refs=("revoked-request",),
    )
    called = []
    monkeypatch.setattr(
        belief_commit,
        "evidence_state",
        lambda ref, scope: "dead" if ref == "revoked-request" else "unknown",
    )

    runs = run_due(model, "08:01", lambda intent: called.append(intent.key))

    assert called == []
    assert runs[0].ok is False and runs[0].closed is True
    assert "未执行" in runs[0].result
    assert project(model.events).intentions["提醒:撤回"].status == "closed"


def test_run_due_closes_on_failure() -> None:
    model = WorldModel()
    open_intention(model, "提醒:喝水", kind="remind", at="08:00", due_at="08:00")
    def boom(intent):
        raise RuntimeError("send failed")
    runs = run_due(model, "08:01", boom)
    assert runs[0].ok is False
    assert "send failed" in runs[0].result
    assert project(model.events).intentions["提醒:喝水"].status == "closed"


def test_match_observe_closes_when_awaited_speaker_replies() -> None:
    model = WorldModel()
    open_intention(model, "observe:在干啥", kind="observe", condition="P5")
    closed = match_observe(model, text="在弄文件", speaker="P5", at="08:11")
    assert closed == ("observe:在干啥",)
    assert project(model.events).intentions["observe:在干啥"].status == "closed"


def test_expire_closes_overdue() -> None:
    model = WorldModel()
    open_intention(model, "提醒:喝水", kind="remind", at="08:00", due_at="08:00")
    expired = expire_intentions(model, "08:30")
    assert expired == ("提醒:喝水",)
    assert "expired" in project(model.events).intentions["提醒:喝水"].result


def test_persist_and_restore_roundtrip(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("YINYUE_WORLD_MODEL_DIR", str(tmp_path))
    reset_world_models()
    model = get_world_model("c1")
    apply_world_updates(model, {"observe": [
        {"key": "P5.location", "value": "到了", "source": "self_reported"},
    ]}, at="08:10")
    open_intention(model, "提醒:喝水", kind="remind", at="08:00", due_at="08:00")
    assert persist_world_model("c1") is True
    reset_world_models()
    restored = get_world_model("c1")
    view = project(restored.events)
    assert view.beliefs["P5.location"].value == "到了"
    assert view.intentions["提醒:喝水"].due_at == "08:00"


def test_run_due_retryable_keeps_open_for_next_tick() -> None:
    """可重试失败不关闭：留给下一 tick（Step 12 的失败重试语义）。"""
    model = WorldModel()
    open_intention(model, "提醒:喝水", kind="remind", at="08:00", due_at="08:00")

    async def flaky(intent):
        return IntentionExecution(ok=False, result="send failed", retryable=True, attempts=1)

    runs = asyncio.run(run_due_retryable(model, "08:01", flaky))
    assert len(runs) == 1
    assert runs[0].ok is False and runs[0].closed is False
    assert runs[0].result.startswith("retry 1/3:")
    assert project(model.events).intentions["提醒:喝水"].status == "open"


def test_run_due_retryable_closes_at_attempt_limit() -> None:
    """attempts 到上限即使执行器仍说 retryable 也关闭，不许无限重试。"""
    model = WorldModel()
    open_intention(model, "提醒:喝水", kind="remind", at="08:00", due_at="08:00")

    async def still_failing(intent):
        return IntentionExecution(ok=False, result="send failed", retryable=True, attempts=3)

    runs = asyncio.run(run_due_retryable(model, "08:01", still_failing))
    assert runs[0].ok is False and runs[0].closed is True
    assert runs[0].result == "send failed"
    view = project(model.events)
    assert view.intentions["提醒:喝水"].status == "closed"
    assert view.intentions["提醒:喝水"].result == "send failed"


def test_run_due_retryable_does_not_execute_a_revoked_source(monkeypatch) -> None:
    from core.memory import belief_commit

    model = WorldModel(scope={
        "platform": "wechat",
        "account_id": "bot",
        "chat_id": "c1",
    })
    open_intention(
        model,
        "提醒:撤回",
        kind="remind",
        at="08:00",
        due_at="08:00",
        evidence_refs=("revoked-request",),
    )
    called = []
    monkeypatch.setattr(
        belief_commit,
        "evidence_state",
        lambda ref, scope: "dead" if ref == "revoked-request" else "unknown",
    )

    async def execute(intent):
        called.append(intent.key)
        return IntentionExecution(ok=True, result="不应执行")

    runs = asyncio.run(run_due_retryable(model, "08:01", execute))

    assert called == []
    assert runs[0].ok is False and runs[0].closed is True
    assert project(model.events).intentions["提醒:撤回"].status == "closed"


def test_revoked_source_keeps_claim_when_authority_cancel_fails(monkeypatch) -> None:
    from core.memory import belief_commit

    model = WorldModel(scope={
        "platform": "wechat",
        "account_id": "bot",
        "chat_id": "c1",
    })
    open_intention(
        model,
        "提醒:撤回",
        kind="remind",
        at="08:00",
        due_at="08:00",
        evidence_refs=("revoked-request",),
    )
    monkeypatch.setattr(
        belief_commit,
        "evidence_state",
        lambda ref, scope: "dead",
    )

    async def execute(intent):
        raise AssertionError("撤回来源不得执行")

    async def cancel(intent):
        return False

    runs = asyncio.run(
        run_due_retryable(
            model,
            "08:01",
            execute,
            on_source_revoked=cancel,
        )
    )

    assert runs[0].closed is False
    assert "等待取消权威任务" in runs[0].result
    assert project(model.events).intentions["提醒:撤回"].status == "open"


def test_revoked_source_closes_after_authority_cancel_succeeds(monkeypatch) -> None:
    from core.memory import belief_commit

    model = WorldModel(scope={
        "platform": "wechat",
        "account_id": "bot",
        "chat_id": "c1",
    })
    open_intention(
        model,
        "提醒:撤回",
        kind="remind",
        at="08:00",
        due_at="08:00",
        evidence_refs=("revoked-request",),
    )
    monkeypatch.setattr(
        belief_commit,
        "evidence_state",
        lambda ref, scope: "dead",
    )
    cancelled = []

    async def execute(intent):
        raise AssertionError("撤回来源不得执行")

    async def cancel(intent):
        cancelled.append(intent.key)
        return True

    runs = asyncio.run(
        run_due_retryable(
            model,
            "08:01",
            execute,
            on_source_revoked=cancel,
        )
    )

    assert cancelled == ["提醒:撤回"]
    assert runs[0].closed is True
    assert project(model.events).intentions["提醒:撤回"].status == "closed"


def test_run_due_retryable_closes_on_executor_exception() -> None:
    """终局异常（未翻译成 retryable 的）按 run_due 同口径关闭。"""
    model = WorldModel()
    open_intention(model, "提醒:喝水", kind="remind", at="08:00", due_at="08:00")

    def boom(intent):
        raise RuntimeError("send failed")

    runs = asyncio.run(run_due_retryable(model, "08:01", boom))
    assert runs[0].ok is False and runs[0].closed is True
    assert "send failed" in runs[0].result
    assert project(model.events).intentions["提醒:喝水"].status == "closed"

if __name__ == "__main__":
    import sys as _sys
    from pathlib import Path as _Path

    for _p in (str(_Path(__file__).resolve().parent.parent), str(_Path(__file__).resolve().parent)):
        if _p not in _sys.path:
            _sys.path.insert(0, _p)
    from _mini_pytest import run_all as _run_all

    _sys.exit(0 if _run_all(globals()) else 1)
