"""P3 目标契约：Intention 不再只是一条「待办 key」。

方案 §8 要求目标带来源、动机、完成条件、下一步、复核时间与版本。这里钉住：
1. 字段能随 `intention_opened` 落进投影；
2. 关闭区分「完成」与「放弃（过期）」；
3. 版本随事件递增；
4. 渲染优先给「下一步」，没有才退回 key（旧数据逐字不变）。
"""
from core.context import situation


def _model(**overrides):
    values = {
        "kind": "act",
        "at": "2026-11-07 10:00",
        "evidence": "主人让找两篇资料",
        "origin": "user_request",
        "motivation": "想帮他省时间",
        "desired_outcome": "两篇资料及摘要",
        "done_when": "两篇资料已发出并得到确认",
        "next_action": "先找第一篇",
        "evidence_refs": ("evt-1", "evt-2"),
        "scope": "wechat:bot:c1",
        "review_at": "2026-11-07 12:00",
        "parent_id": "goal:parent",
    }
    values.update(overrides)
    model = situation.WorldModel()
    situation.open_intention(model, "goal:资料", **values)
    return model


def _goal(model, key="goal:资料"):
    return situation.project(model.events).intentions[key]


def test_goal_fields_are_projected():
    goal = _goal(_model())

    assert goal.goal_id == "goal:资料"
    assert goal.origin == "user_request"
    assert goal.motivation == "想帮他省时间"
    assert goal.desired_outcome == "两篇资料及摘要"
    assert goal.done_when == "两篇资料已发出并得到确认"
    assert goal.next_action == "先找第一篇"
    assert goal.evidence_refs == ("evt-1", "evt-2")
    assert goal.scope == "wechat:bot:c1"
    assert goal.review_at == "2026-11-07 12:00"
    assert goal.parent_id == "goal:parent"
    assert goal.state == "active"
    assert goal.revision == 1


def test_closing_as_completed_bumps_revision():
    model = _model()
    situation.close_intention(
        model, "goal:资料", at="2026-11-07 11:00", result="completed:已发出"
    )

    goal = _goal(model)
    assert goal.status == "closed"
    assert goal.state == "completed"
    assert goal.closed_at == "2026-11-07 11:00"
    assert goal.result == "completed:已发出"
    assert goal.revision == 2


def test_expiry_is_abandoned_not_completed():
    model = _model()
    situation.close_intention(
        model, "goal:资料", at="2026-11-07 12:30",
        result="expired（超时未完成）",
    )

    goal = _goal(model)
    assert goal.status == "closed"
    assert goal.state == "abandoned"
    assert goal.revision == 2


def test_explicit_terminal_state_is_respected():
    model = _model()
    situation.close_intention(
        model, "goal:资料", at="2026-11-07 11:00",
        result="用户取消", state="abandoned",
    )

    assert _goal(model).state == "abandoned"


def test_open_state_can_be_proposed_or_waiting():
    proposed = _goal(_model(state="proposed"))
    waiting = _goal(_model(state="waiting"))

    assert proposed.state == "proposed" and proposed.status == "open"
    assert waiting.state == "waiting" and waiting.status == "open"


def test_render_prefers_next_action_and_falls_back_to_key():
    model = _model()
    rendered = situation.render_situation(model, max_lines=10)
    assert "待办：act 先找第一篇" in rendered

    legacy = situation.WorldModel()
    situation.open_intention(legacy, "提醒:喝水", kind="remind", evidence="提醒我喝水")
    assert "待办：remind 提醒:喝水" in situation.render_situation(legacy, max_lines=10)


def test_act_view_uses_the_same_label():
    from core.context.act_view import render_act_view

    rendered = render_act_view(_model(), max_lines=10)
    assert "待办：act 先找第一篇" in rendered
