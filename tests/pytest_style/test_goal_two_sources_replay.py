# -*- coding: utf-8 -*-
"""D · 固定回放：找两篇资料，中途重启、第二篇先失败后补回，最后按证据报告。

只替换两处边界：执行器（`execute` 换成记录器）和时钟。任务调度用的是真
`JobStore` + `runner.advance` + `job_bridge.advance_once`，目标是世界模型真源，
落盘/重启走真实的 `save_world` / `reset_world_models`。

回放顺序照施工单 §5：

1. 开任务 → 2. 第一篇成功 → 3. 一条无关聊天 → 4. 结束进程并从磁盘恢复 →
5. 第二篇第一次失败 → 6. 后续有效回包 → 7. 查两篇证据 → 8. 正确报告完成。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from core.context import situation, situation_store
from core.context.goal_report import line as report_line, missing_classes
from core.context.goal_wake import note_goal_signal, pending_wakes
from core.contracts.plan import Job, Step
from core.execution.data_channels import get_world, save_world
from core.jobs.runner import STOP_REASONS
from core.jobs.store import JobStore, ensure_schema as job_schema
from core.terminal.event_model import Event, EventType
from core.terminal.principal import VisibilityScope
from loop import job_bridge

from test_wechat_e2e_chain import db  # noqa: F401 - 复用真实的 meta 引擎夹具

SCOPE = {"platform": "wechat", "account_id": "a", "chat_id": "same", "chat_type": "private"}
KEY = "整理两篇资料"
JOB_ID = "job-two-sources"


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("YINYUE_WORLD_MODEL_DIR", str(tmp_path / "world"))
    monkeypatch.setenv("YINYUE_COGNITIVE_DB", str(tmp_path / "cognitive.db"))
    situation_store.reset_world_models()
    yield
    situation_store.reset_world_models()


class _Runner:
    """执行器替身：按脚本回「成功 / 失败」，并记下每一步真的做了什么。"""

    def __init__(self, script):
        self.script = list(script)
        self.calls: list[str] = []

    def __call__(self, step):
        self.calls.append(step.action)
        ok = self.script.pop(0) if self.script else True
        return {"ok": ok, "detail": "done" if ok else "资料站 503"}


def _open(refs=("msg:req-1",), next_action="先找第一篇"):
    model = get_world("same", scope=SCOPE)
    situation.open_intention(
        model, KEY, kind="act", at="2026-11-18 09:00",
        evidence="你帮我整理两篇符合条件的资料",
        desired_outcome="两篇资料都整理好、各自保存",
        done_when="两份独立结果都符合条件且已保存",
        next_action=next_action, review_at="2026-11-18 10:00",
        evidence_refs=tuple(refs), scope="chat:same", origin="owner_request",
    )
    assert save_world("same", scope=model.scope)
    return model


def _job(store: JobStore, engine) -> Job:
    job_schema(engine)
    job = Job(
        JOB_ID, "same", KEY, goal_id=KEY,
        steps=[Step(1, "send:群|第一篇", effects="external_send"),
               Step(2, "send:群|第二篇", effects="external_send")],
    )
    store.create(job)
    return job


def _reload():
    """等价于进程重启：内存缓存清空，只剩盘上的世界模型。"""
    situation_store.reset_world_models()
    return situation_store.get_world_model("same", scope=SCOPE)


def _goal(model):
    return next(g for g in situation.open_intentions(model) if g.key == KEY)


def test_two_sources_survive_restart_and_failed_retry(db):  # noqa: F811
    store = JobStore(db.engine, mode="on")
    model = _open()
    _job(store, db.engine)
    run = _Runner([True, True])

    # 1→2：第一篇成功。长任务在安全点交还注意力，目标继续开着。
    first = job_bridge.advance_once("same", store=store, execute=run)
    assert run.calls == ["send:群|第一篇"]
    assert first["stop"] == "safe_point" and first["step_n"] == 1
    assert note_goal_signal(model, KEY, source="evidence", ref="artifact:1",
                            at="2026-11-18 09:20", detail="第一篇已保存") is True
    assert _goal(model).next_action and [g.key for g in situation.open_intentions(model)] == [KEY]
    assert save_world("same", scope=model.scope)

    # 3：一条无关聊天进来，不改目标、不推进步骤。
    db.store.append(Event.new(
        event_type=EventType.MESSAGE, platform="wechat", bot_account_id="a", chat_id="same",
        actor_person_id="person-a", external_msg_id="chatter-1",
        content="今天好热", visibility_scope=frozenset({VisibilityScope.OWNER_ONLY}),
    ))
    assert [g.key for g in situation.open_intentions(get_world("same", scope=SCOPE))] == [KEY]

    # 4：结束进程 → 从磁盘恢复。字段和待处理信号都还在。
    reloaded = _reload()
    goal = _goal(reloaded)
    assert goal.done_when == "两份独立结果都符合条件且已保存"
    assert goal.evidence_refs == ("msg:req-1",)
    assert any(w.reason == "evidence" for w in pending_wakes(reloaded))

    # 5：第二篇第一次失败——不推进步号、不关目标、不留「办完了」的账。
    run.script = [False, True]
    failed = job_bridge.advance_once("same", store=store, execute=run)
    assert failed["stop"] == "failed"
    assert run.calls == ["send:群|第一篇", "send:群|第二篇"]
    reloaded = _reload()
    assert [g.key for g in situation.open_intentions(reloaded)] == [KEY]
    assert [g for g in situation.open_intentions(reloaded) if g.key == KEY][0].state == "active"

    # 6：重试，第二篇成功并落定。
    retried = job_bridge.advance_once("same", store=store, execute=run)
    assert retried["stop"] == "done" and retried["job_id"] == JOB_ID
    assert run.calls[-1] == "send:群|第二篇"
    assert note_goal_signal(reloaded, KEY, source="evidence", ref="artifact:2",
                            at="2026-11-18 10:05", detail="第二篇已保存") is True
    # 台账写 done 的时候，回执也要落到目标上——「台账 done、目标不知道」是 E-59 的老坑。
    assert any(e.kind == "goal_signal" and (e.payload or {}).get("source") == "receipt"
               for e in reloaded.events), "job 完成必须留下回执信号"
    # 同一篇重复上报不算第二次进展。
    assert note_goal_signal(reloaded, KEY, source="evidence", ref="artifact:2",
                            at="2026-11-18 10:06") is False
    assert save_world("same", scope=reloaded.scope)

    # 7：两篇都找齐，但「找齐」不等于「送达」——四类证据要各自交代。
    reloaded = _reload()
    registered = _open(refs=("msg:req-1", "artifact:1", "artifact:2"),
                       next_action="核对两篇并报告")
    text = report_line(_goal(registered))
    assert "两份独立结果都符合条件且已保存" in text
    assert "artifact:1" in text and "artifact:2" in text
    assert "尚未取得证据" in text and "平台送达" in text
    assert missing_classes(("artifact:1", "artifact:2")) == ("投递动作", "本机观察", "平台送达")


def test_report_stops_naming_missing_evidence_once_all_five_are_in(db):  # noqa: F811
    _open()
    full = _open(refs=("artifact:1", "artifact:2", "queue:9", "work_item:r1", "ack:wx-9"),
                 next_action="收尾")
    text = report_line(_goal(full))
    assert "尚未取得证据" not in text
    assert "已登记证据" in text
    # 任务台账本身不该因为「目标写全了」就被当成办完：状态仍是 open。
    assert [g.key for g in situation.open_intentions(full)] == [KEY]


def test_stop_reasons_are_registered():
    """新停下来的理由必须进 STOP_REASONS，否则报表按字面比对会漏。"""
    assert {"state", "goal_closed", "failed", "safe_point", "done"} <= set(STOP_REASONS)
