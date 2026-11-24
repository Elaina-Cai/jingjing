"""P2：记忆纠正——区分「时间变化」与「记错了」，旧条目保留可查。

方案 §6：明确更正生成 supersedes，旧条目标 superseded（不是删除）；
来源冲突两条并存。这里钉住三件事：
1. assert_fact 带新字段（subject_key/predicate/evidence_refs/scope/
   valid_from/valid_to/supersedes）能落进投影；
2. supersedes 把旧条目标为 superseded 并写 valid_to，但**不删**——历史可查；
3. 旧事件（不带新字段）读出来全部是默认值，不受影响。
"""
from core.context import situation
from core.context.situation import (
    Belief,
    assert_fact,
    assert_unknown,
    project,
)


def test_assert_fact_carries_the_full_contract():
    model = situation.WorldModel()
    assert_fact(
        model,
        "地址:主人",
        value="现在住在上海",
        at="2026-11-07 10:00",
        who="主人",
        subject_key="person:主人",
        predicate="lives_in",
        evidence_refs=("evt-1", "evt-2"),
        scope="wechat:bot:c1",
        valid_from="2026-11-07",
    )

    belief = project(model.events).beliefs["地址:主人"]
    assert belief.value == "现在住在上海"
    assert belief.subject_key == "person:主人"
    assert belief.predicate == "lives_in"
    assert belief.evidence_refs == ("evt-1", "evt-2")
    assert belief.scope == "wechat:bot:c1"
    assert belief.valid_from == "2026-11-07"
    assert belief.valid_to == ""
    assert belief.supersedes == ""
    assert belief.status == "active"


def test_supersedes_marks_old_as_superseded_not_deleted():
    model = situation.WorldModel()
    assert_fact(model, "地址:主人", value="住在深圳", at="2026-10-01")
    assert_fact(
        model, "地址:主人_v2", value="现在住在上海",
        at="2026-11-07", supersedes="地址:主人",
    )

    view = project(model.events)
    old = view.beliefs["地址:主人"]
    new = view.beliefs["地址:主人_v2"]

    # 旧条目：不删，标 superseded + 写 valid_to（历史可查）。
    assert old.status == "superseded"
    assert old.valid_to == "2026-11-07"
    assert old.value == "住在深圳"

    # 新条目：正常 active，supersedes 指回旧 key。
    assert new.status == "active"
    assert new.supersedes == "地址:主人"
    assert new.value == "现在住在上海"


def test_superseded_beliefs_do_not_appear_in_active_render():
    model = situation.WorldModel()
    assert_fact(model, "地址:主人", value="住在深圳", at="2026-10-01")
    assert_fact(
        model, "地址:主人_v2", value="现在住在上海",
        at="2026-11-07", supersedes="地址:主人",
    )

    rendered = situation.render_situation(model, max_lines=10)
    assert "住在深圳" not in rendered, "被替代的旧值不应再出现在活跃视图"
    assert "现在住在上海" in rendered


def test_revocation_sets_valid_to():
    model = situation.WorldModel()
    assert_fact(model, "爱好:主人", value="喜欢钓鱼", at="2026-10-01")
    model.add("fact_revoked", {"key": "爱好:主人", "reason": "记错了"}, at="2026-11-07")

    belief = project(model.events).beliefs["爱好:主人"]
    assert belief.status == "revoked"
    assert belief.valid_to == "2026-11-07"


def test_legacy_events_have_default_values():
    model = situation.WorldModel()
    model.add("fact_asserted", {"key": "旧事", "value": "旧值", "who": "某人"}, at="2026-01-01")

    belief = project(model.events).beliefs["旧事"]
    assert belief.subject_key == ""
    assert belief.predicate == ""
    assert belief.evidence_refs == ()
    assert belief.scope == ""
    assert belief.valid_from == ""
    assert belief.valid_to == ""
    assert belief.supersedes == ""
    assert belief.status == "active"


def test_self_supersedes_is_ignored():
    model = situation.WorldModel()
    assert_fact(
        model, "地址:主人", value="住在上海", at="2026-11-07",
        supersedes="地址:主人",
    )

    belief = project(model.events).beliefs["地址:主人"]
    assert belief.status == "active", "自己 supersede 自己等于没做"
    assert belief.supersedes == ""


def test_mem_ref_removed_from_look():
    """mem: 假能力已删——错误信息不再教模型用 mem:。"""
    from loop import ports

    # look 先查 read scope；scope 缺了走不到 ref 解析。这里只验静态面：
    # 错误文案和 brain 的工具描述不再包含 mem:。
    import inspect
    source = inspect.getsource(ports.look)
    import ast
    messages = [node.value for node in ast.walk(ast.parse(source)) if isinstance(node, ast.Constant) and isinstance(node.value, str)]
    assert all("mem:" not in value for value in messages), "ports.look 不应再声明 mem:"

    from loop import brain
    tool_desc = next(
        t["function"]["description"] for t in brain.SCHEMAS if t["function"].get("name") == "look"
    )
    assert "mem:" not in tool_desc, "look 工具描述不应再教模型用 mem:"
