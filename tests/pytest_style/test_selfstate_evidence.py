"""她不许把没发生过的话记成「我说过」。

2026-11-04：`self_state.json` 里那条「把作业群里那几条自己编的报到安排撤回来了」
是模型自己写的，消息表和投递队列里都没有对应记录，却被当成事实喂回下一拍，
她于是去「撤回」一件没发生的事。这个文件锁两件事：
1)`touch_said` 必须先对账，对不上不写；2)没核对过的「手头在做」不进提示词。
"""
import sqlite3
import time

import pytest

from loop import selfstate


def _conn(rows=0, *, text="hello", ts=None):
    con = sqlite3.connect(":memory:")
    con.execute("create table outbound_queue (chat_id text, state text, payload text, created_ts real, updated_ts real)")
    now = float(ts if ts is not None else time.time())
    payload = '{"kind": "text", "text": "hello"}'
    for _ in range(int(rows)):
        con.execute("insert into outbound_queue values (?,?,?,?,?)",
                    ("c1", "sent", payload, now, now))
    return con


@pytest.fixture()
def state(tmp_path, monkeypatch):
    p = tmp_path / "self_state.json"
    monkeypatch.setattr(selfstate, "STATE", p)
    return p


def test_a_line_that_never_went_out_is_not_recorded(state):
    ok = selfstate.touch_said("c1", "我在撤回那几条", check=True, conn=_conn(0))
    assert ok is False
    assert "c1" not in (selfstate.load().get("said") or {})


def test_three_vision_lines_do_not_push_the_stance_out(state):
    scope = ("wechat", "acct", "room@chatroom", "u1")
    selfstate.update(mood="有点累，也有点想她多说两句")
    selfstate.update(note="她这会儿更想被听着，不是被给办法", who="u1",
                     stance_scope=scope, evidence_refs=())
    for i in range(3):
        selfstate.push_vision_hint("room@chatroom", "图里有一段说明文字" * 8)
    out = selfstate.render(stance_scope=scope)
    assert "我当时对当前发言人的看法" in out
    assert "更想被听着" in out


def test_a_line_that_is_in_the_queue_is_recorded(state):
    ok = selfstate.touch_said("c1", "hello", check=True, conn=_conn(1))
    assert ok is True
    assert (selfstate.load()["said"]["c1"]["text"]) == "hello"


def test_a_line_outside_the_window_does_not_count(state):
    old = float(time.time()) - float(selfstate.SAID_VERIFY_WINDOW_S) - 60.0
    ok = selfstate.touch_said("c1", "hello", check=True, conn=_conn(1, ts=old))
    assert ok is False
    assert "c1" not in (selfstate.load().get("said") or {})


def test_scoped_speech_requires_confirmation_from_the_same_account(state):
    con = _conn(1)
    con.execute("alter table outbound_queue add platform text default 'wechat'")
    con.execute("alter table outbound_queue add account_id text default 'acct'")
    con.execute("alter table outbound_queue add ack text default 'unconfirmed'")
    scope = ("wechat", "acct", "c1")

    assert not selfstate.touch_said("c1", "hello", conn=con, scope=scope)
    con.execute("update outbound_queue set ack='confirmed'")
    assert not selfstate.touch_said("c1", "hello", conn=con, scope=("wechat", "other", "c1"))
    assert selfstate.touch_said("c1", "hello", conn=con, scope=scope)
    assert "hello" in selfstate.render(stance_scope=(), conversation_scope=scope)
    assert "hello" not in selfstate.render(stance_scope=(), conversation_scope=())


def test_summary_lines_can_skip_the_check(state):
    ok = selfstate.touch_said("c1", "[表情] abc", check=False)
    assert ok is True
    assert (selfstate.load()["said"]["c1"]["text"]) == "[表情] abc"


def test_self_reported_doing_is_stored_but_not_shown(state):
    msg = selfstate.update(doing="把作业群里那几条自己编的报到安排撤回来了")
    assert "没核对过" in msg
    assert selfstate.load()["doing"]["verified"] is False
    body = selfstate.render()
    assert "撤回来了" not in body


def test_a_verified_doing_is_shown(state):
    selfstate.update(doing="在等P5回话")
    d = selfstate.load()
    d["doing"]["verified"] = True
    selfstate.save(d)
    assert "在等P5回话" in selfstate.render()


def test_render_keeps_said_and_vision_inside_the_current_chat(state):
    selfstate.touch_said("private", "私聊里的秘密", check=False)
    selfstate.touch_said("group", "群里说过的话", check=False)
    selfstate.push_vision_hint("private", "私聊截图里的内容")
    selfstate.push_vision_hint("group", "群聊图片里的内容")

    current = selfstate.render(stance_scope=("wechat", "acct", "group", "person-a"))
    assert "群里说过的话" in current
    assert "群聊图片里的内容" in current
    assert "私聊里的秘密" not in current
    assert "私聊截图里的内容" not in current

    unscoped = selfstate.render()
    assert "私聊里的秘密" in unscoped
    assert "群聊图片里的内容" in unscoped


def test_scoped_stance_follows_source_evidence(state, monkeypatch):
    from core.memory import belief_commit

    coords = ("wechat", "acct", "group", "person-a")
    monkeypatch.setattr(
        belief_commit,
        "evidence_state",
        lambda ref, scope: {
            "dead-event": "dead",
            "missing-event": "unknown",
            "live-event": "alive",
        }.get(ref, "unknown"),
    )

    selfstate.update(
        note="这条看法基于已撤回的话",
        who="person-a",
        stance_scope=coords,
        evidence_refs=("dead-event",),
    )
    assert "基于已撤回的话" not in selfstate.render(stance_scope=coords)

    selfstate.update(
        note="她这句话到底什么意思",
        who="person-a",
        stance_scope=coords,
        evidence_refs=("missing-event",),
    )
    detail = {}
    unknown = selfstate.render(stance_scope=coords, detail=detail)
    assert "她这句话到底什么意思" in unknown
    assert "[依据未核实]" in unknown
    assert detail["uncertain_memory_claims"] == [{
        "content": "她这句话到底什么意思",
        "state": "unknown",
        "source_event_ids": ["missing-event"],
    }]
    from loop import evidence_guard

    assert evidence_guard.uncertain_memory_claim(
        "我就是觉得她这句话到底什么意思。",
        detail["uncertain_memory_claims"],
    ) is not None

    selfstate.update(
        note="我愿意先听她说完",
        who="person-a",
        stance_scope=coords,
        evidence_refs=("live-event",),
    )
    alive_detail = {}
    alive = selfstate.render(stance_scope=coords, detail=alive_detail)
    assert "我愿意先听她说完" in alive
    assert "[依据未核实]" not in alive
    assert alive_detail["uncertain_memory_claims"] == []


def _stored_refs():
    entries = selfstate.load()["scoped_stance"]
    return list(entries.values())[0]["evidence_refs"]


def test_a_revised_note_drops_the_old_basis(state, monkeypatch):
    from core.memory import belief_commit

    coords = ("wechat", "acct", "group", "person-a")
    monkeypatch.setattr(
        belief_commit,
        "evidence_state",
        lambda ref, scope: {
            "old-live": "alive",
            "revision-dead": "dead",
            "independent": "alive",
        }.get(ref, "unknown"),
    )

    selfstate.update(
        note="她只是随口一说",
        who="person-a",
        stance_scope=coords,
        evidence_refs=("old-live",),
    )
    assert "她只是随口一说" in selfstate.render(stance_scope=coords)

    # 看法被改写，新依据随后被撤回：旧的活依据不许替这条新看法续命。
    selfstate.update(
        note="她其实是认真的",
        who="person-a",
        stance_scope=coords,
        evidence_refs=("revision-dead",),
    )
    assert _stored_refs() == ["revision-dead"]
    assert "她其实是认真的" not in selfstate.render(stance_scope=coords)

    # 内容没变时，独立的新依据仍可累积为佐证。
    selfstate.update(
        note="她其实是认真的",
        who="person-a",
        stance_scope=coords,
        evidence_refs=("independent",),
    )
    assert _stored_refs() == ["revision-dead", "independent"]
    assert "她其实是认真的" in selfstate.render(stance_scope=coords)


def test_long_notes_sharing_a_clipped_prefix_do_not_inherit_basis(state, monkeypatch):
    from core.memory import belief_commit

    coords = ("wechat", "acct", "group", "person-a")
    monkeypatch.setattr(
        belief_commit,
        "evidence_state",
        lambda ref, scope: {
            "prefix-old-live": "alive",
            "prefix-new-dead": "dead",
        }.get(ref, "unknown"),
    )

    prefix = "甲" * (selfstate._NOTE_MAX - 1)
    long_a = prefix + "第一种说法"
    long_b = prefix + "第二种说法"
    assert selfstate._clip(long_a, selfstate._NOTE_MAX) == selfstate._clip(long_b, selfstate._NOTE_MAX)

    selfstate.update(
        note=long_a, who="person-a", stance_scope=coords,
        evidence_refs=("prefix-old-live",),
    )
    selfstate.update(
        note=long_b, who="person-a", stance_scope=coords,
        evidence_refs=("prefix-new-dead",),
    )
    assert _stored_refs() == ["prefix-new-dead"]
    assert selfstate._clip(long_b, selfstate._NOTE_MAX) not in selfstate.render(stance_scope=coords)
