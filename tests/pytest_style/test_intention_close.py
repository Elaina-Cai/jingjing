# -*- coding: utf-8 -*-
"""对话侧销账：错关比不关更坏，所以两个条件必须同时成立。

P3 起关闭写成**认知提交**（唯一真源）：世界模型文件只保留开启与旧事件，读侧在
加载时从认知库派生关闭状态。因此这里的断言分两层：
- 写：``out["closed"]`` 与认知库里的最后一条事件；
- 读：重载世界模型后 ``open_intentions`` 不再包含它（不是靠调用方手里的模型对象）。
"""
import pytest

from core.context import intention_close as ic
from core.context import situation
from core.context.cognitive_commit import (
    Scope,
    closed_subjects_for_chat,
    connect,
    invalidate_closed_cache,
    read_state,
)
from core.context.situation_store import (
    get_world_model,
    persist_world_model,
    reset_world_models,
)

SCOPE = Scope("wechat", "bot", "c1", "private", "")


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("YINYUE_COGNITIVE_DB", str(tmp_path / "cognitive.db"))
    monkeypatch.setenv("YINYUE_WORLD_MODEL_DIR", str(tmp_path / "world"))
    from types import SimpleNamespace
    from core.terminal import shadow
    facts = {ref: SimpleNamespace(platform="wechat", bot_account_id="bot", chat_id=chat) for ref, chat in (("evt-1", "c1"), ("evt-2", "c2"), ("evt-3", "c1"))}
    monkeypatch.setattr(shadow, "get_default_store", lambda: SimpleNamespace(get=lambda ref: facts.get(ref)))
    invalidate_closed_cache()
    reset_world_models()
    yield
    reset_world_models()
    invalidate_closed_cache()


def _model(key="提醒:喝水", **kw):
    m = situation.WorldModel()
    kw.setdefault("kind", "remind")          # 默认是 observe，这里要的是"她欠着的事"
    kw.setdefault("evidence", "提醒我喝水")
    situation.open_intention(m, key, **kw)
    return m


def _msg(text, at="2026-11-03 10:06:00", speaker="阿蓝", is_self=False,
         msg_id=1001, event_id="evt-1"):
    return {
        "text": text,
        "at": at,
        "speaker": speaker,
        "is_self": is_self,
        "msg_id": msg_id,
        "event_id": event_id,
    }


def _keys(model):
    return tuple(i.key for i in situation.open_intentions(model))


def test_nothing_open_matches_nothing():
    assert ic.match_completion(situation.WorldModel(), messages=[_msg("我喝过了")]) == ()


def test_marker_plus_subject_closes():
    m = _model()
    got = ic.match_completion(m, messages=[_msg("我喝过了")])
    assert [k for k, _t, _a in got] == ["提醒:喝水"]
    assert got[0][1] == "我喝过了"
    assert _keys(m) == ("提醒:喝水",)          # 匹配器只读，不改状态


def test_subject_without_marker_does_not_close():
    m = _model()
    assert ic.match_completion(m, messages=[_msg("水在哪")]) == ()


def test_marker_without_subject_does_not_close():
    m = _model("提醒:写周报", evidence="提醒我写周报")
    assert ic.match_completion(m, messages=[_msg("我办完了")]) == ()


def test_observe_is_left_to_match_observe():
    m = _model("等阿蓝回话", kind="observe", evidence="", condition="阿蓝")
    assert ic.match_completion(m, messages=[_msg("阿蓝办完了")]) == ()


def test_own_message_never_closes():
    m = _model()
    assert ic.match_completion(m, messages=[_msg("我喝过了", is_self=True)]) == ()


def test_off_mode_is_a_no_op():
    m = _model()
    out = ic.close_from_rows("c1", [_msg("我喝过了")], model=m, mode="off", scope=SCOPE)
    assert out["matched"] == () and out["closed"] == ()
    assert _keys(m) == ("提醒:喝水",)


def test_shadow_does_not_change_state():
    m = _model()
    out = ic.close_from_rows("c1", [_msg("我喝过了")], model=m, mode="shadow", scope=SCOPE)
    assert [k for k, _t, _a in out["matched"]] == ["提醒:喝水"]
    assert out["closed"] == ()
    assert closed_subjects_for_chat("c1") == frozenset()


def test_on_writes_a_cognitive_close():
    m = _model()
    out = ic.close_from_rows(
        "c1", [_msg("我喝过了")], model=m, mode="on", scope=SCOPE
    )

    assert out["closed"] == ("提醒:喝水",)
    conn = connect()
    try:
        state = read_state(conn, "提醒:喝水")
    finally:
        conn.close()
    assert state["operation"] == "close"
    assert state["payload"]["trigger"] == "我喝过了"
    assert state["payload"]["result"].startswith("completed:我喝过了")
    # 调用方给的模型不被改写；关闭靠读侧派生，不靠偷偷改内存。
    assert _keys(m) == ("提醒:喝水",)


def test_closure_survives_reload_and_is_derived_from_the_log(tmp_path):
    base = get_world_model("c1", scope=SCOPE)
    situation.open_intention(base, "提醒:喝水", kind="remind", evidence="提醒我喝水")
    assert persist_world_model("c1")

    out = ic.close_from_rows("c1", [_msg("我喝过了")], mode="on", scope=SCOPE)
    assert out["closed"] == ("提醒:喝水",)

    reset_world_models()                     # 模拟重启：内存全丢，只剩盘上文件
    reloaded = get_world_model("c1")
    assert _keys(reloaded) == ()
    derived = [e for e in reloaded.events if e.payload.get("_derived")]
    assert derived, "关闭必须由认知库派生，而不是写回文件"

    # 派生事件落盘时必须被过滤：同一条关闭只能有一个真源。
    assert persist_world_model("c1")
    files = list((tmp_path / "world").glob("*.json"))
    assert files
    assert "_derived" not in files[0].read_text(encoding="utf-8")


def test_on_without_scope_refuses_to_close():
    m = _model()
    out = ic.close_from_rows("c1", [_msg("我喝过了")], model=m, mode="on")

    assert out["matched"] and out["closed"] == ()
    assert closed_subjects_for_chat("c1") == frozenset()


def test_missing_evidence_does_not_close(monkeypatch):
    m = _model()
    monkeypatch.setattr(ic, "_evidence_ref", lambda row, scope: "")
    out = ic.close_from_rows(
        "c1", [_msg("我喝过了")], model=m, mode="on", scope=SCOPE
    )

    assert out["closed"] == ()
    assert closed_subjects_for_chat("c1") == frozenset()


def test_close_is_scoped_to_one_chat():
    m = _model()
    ic.close_from_rows("c1", [_msg("我喝过了")], model=m, mode="on", scope=SCOPE)

    assert closed_subjects_for_chat("c1") == frozenset({"提醒:喝水"})
    assert closed_subjects_for_chat("c2") == frozenset()


def test_later_close_in_another_chat_does_not_hide_this_chat():
    """同名 goal 的两个会话各关各的：别处更晚的 close 不能把这里顶成「未关闭」。"""
    m1, m2 = _model(), _model()
    ic.close_from_rows("c1", [_msg("我喝过了")], model=m1, mode="on", scope=SCOPE)
    ic.close_from_rows(
        "c2",
        [_msg("我喝过了", msg_id=2002, event_id="evt-2")],
        model=m2,
        mode="on",
        scope=Scope("wechat", "bot", "c2", "private", ""),
    )

    assert closed_subjects_for_chat("c1") == frozenset({"提醒:喝水"})
    assert closed_subjects_for_chat("c2") == frozenset({"提醒:喝水"})


def test_retry_does_not_add_a_second_close():
    m = _model()
    ic.close_from_rows("c1", [_msg("我喝过了")], model=m, mode="on", scope=SCOPE)
    ic.close_from_rows("c1", [_msg("我喝过了")], model=m, mode="on", scope=SCOPE)

    conn = connect()
    try:
        count = conn.execute(
            "SELECT COUNT(*) AS n FROM cognitive_events WHERE subject_key = '提醒:喝水'"
        ).fetchone()["n"]
    finally:
        conn.close()
    assert int(count) == 1


def test_two_intents_one_sentence_closes_only_the_matching_one():
    m = situation.WorldModel()
    situation.open_intention(m, "提醒:喝水", kind="remind", evidence="提醒我喝水")
    situation.open_intention(m, "提醒:写周报", kind="remind", evidence="提醒我写周报")

    out = ic.close_from_rows(
        "c1", [_msg("水我喝过了", msg_id=2002, event_id="evt-3")],
        model=m, mode="on", scope=SCOPE,
    )

    assert out["closed"] == ("提醒:喝水",)
    assert closed_subjects_for_chat("c1") == frozenset({"提醒:喝水"})
