"""心境有过时：昨天写下的心情不能被当成此刻的心情。

线上 data/loop/self_state.json 里「安静，不堵了」写了 15 小时，仍以
「- 心境：…（15 小时前）」喂进每一拍 —— 她带着昨天的心情开口，而且看不出
这条心情是因为什么来的。这里锁三件事：

1. 新鲜心境照旧写成「心境」，老的改成「之前记下的心情」并说明不一定还是现在；
2. 老心境要能带出当时那句话（可核对来源），不能只是一个孤立形容词；
3. 同一句心境再写一次 = 「我现在还是这样」，时间要前移，不能永远停在第一次。
"""

import json
import os
import time

from loop import selfstate


def _state(tmp_path, monkeypatch, mood=None):
    path = tmp_path / "self_state.json"
    monkeypatch.setattr(selfstate, "STATE", path)
    data = selfstate._blank()
    if mood:
        data["mood"] = dict(mood)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


def _read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_fresh_mood_is_current_mood(tmp_path, monkeypatch):
    _state(tmp_path, monkeypatch, {"text": "有点累", "at": time.time() - 600})
    out = selfstate.render()
    assert "- 心境：有点累" in out
    assert "已经过去" not in out


def test_stale_mood_is_not_the_current_mood(tmp_path, monkeypatch):
    _state(tmp_path, monkeypatch, {"text": "安静，不堵了", "at": time.time() - 15 * 3600})
    out = selfstate.render()
    assert "- 心境：" not in out
    assert "之前记下的心情：安静，不堵了" in out
    assert "不一定还是这样" in out


def test_stale_mood_carries_what_it_was_about(tmp_path, monkeypatch):
    _state(tmp_path, monkeypatch, {
        "text": "烦躁", "at": time.time() - 10 * 3600, "evidence_refs": ["ev1"],
    })
    monkeypatch.setattr(selfstate, "_mood_source", lambda refs, **kw: "P6：你到底行不行啊")
    out = selfstate.render()
    assert "那会儿是因为" in out
    assert "你到底行不行啊" in out


def test_fresh_mood_keeps_its_cause_too(tmp_path, monkeypatch):
    _state(tmp_path, monkeypatch, {
        "text": "有点闷", "at": time.time() - 300, "evidence_refs": ["ev2"],
    })
    monkeypatch.setattr(selfstate, "_mood_source", lambda refs, **kw: "P5：算了不说了")
    out = selfstate.render()
    assert "- 心境：有点闷" in out
    assert "P5：算了不说了" in out


def test_rewriting_the_same_mood_moves_the_clock(tmp_path, monkeypatch):
    path = _state(tmp_path, monkeypatch,
                  {"text": "还是有点烦", "at": time.time() - 15 * 3600})
    selfstate.update(mood="还是有点烦", evidence_refs=("ev9",))
    mood = _read(path)["mood"]
    assert time.time() - float(mood["at"]) < 60
    assert mood.get("evidence_refs") == ["ev9"]


def test_mood_refs_are_capped(tmp_path, monkeypatch):
    path = _state(tmp_path, monkeypatch)
    selfstate.update(mood="还行", evidence_refs=("a", "b", "c", "d", "e"))
    refs = _read(path)["mood"].get("evidence_refs") or []
    assert 0 < len(refs) <= 3


def test_source_is_readable_words_not_a_placeholder(monkeypatch):
    from core.terminal import shadow as shadow_mod

    class _Event:
        platform = "wechat"
        bot_account_id = "acct"
        chat_id = "wxid_a"
        actor_account_id = "u1"
        actor_person_id = ""
        content = "你到底行不行啊"
        occurred_at = type("T", (), {"isoformat": lambda self: "2026-11-20T23:10:00"})()

    class _Store:
        def get(self, ref):
            return _Event() if ref == "ev1" else None

    monkeypatch.setattr(shadow_mod, "get_default_store", lambda: _Store())
    assert selfstate._mood_source(("ev1",)) == "你到底行不行啊"
    assert selfstate._mood_source(()) == ""


def test_render_and_brain_use_the_freshness_boundary():
    source = open(selfstate.__file__, encoding="utf-8").read()
    assert "MOOD_FRESH_S" in source
    assert "之前记下的心情" in source
    assert "_mood_source(" in source
    brain = os.path.join(os.path.dirname(selfstate.__file__), "brain.py")
    assert "evidence_refs=_source_refs" in open(brain, encoding="utf-8").read()

# ---- 2026-11-24：评价里的「我的情绪」折进此刻心境 ----

def _offer(ref="ev9"):
    from types import SimpleNamespace
    return SimpleNamespace(reference=ref)


def test_committed_appraisal_feeling_becomes_the_current_mood(tmp_path, monkeypatch):
    from loop import brain

    path = _state(tmp_path, monkeypatch)
    brain._fold_appraisal_mood({"feeling_text": "有点担心"}, _offer("ev9"),
                               {"result": "committed"}, turn_id="t1")
    data = _read(path)
    assert data["mood"]["text"] == "有点担心"
    assert data["mood"]["evidence_refs"] == ["ev9"]
    out = selfstate.render()
    assert "- 心境：有点担心" in out


def test_say_style_feeling_key_is_folded_too(tmp_path, monkeypatch):
    from loop import brain

    path = _state(tmp_path, monkeypatch)
    brain._fold_appraisal_mood({"feeling": "心里有点在意"}, _offer("ev10"),
                               {"result": "committed"}, turn_id="t2")
    assert _read(path)["mood"]["text"] == "心里有点在意"


def test_rejected_appraisal_leaves_the_mood_alone(tmp_path, monkeypatch):
    from loop import brain

    path = _state(tmp_path, monkeypatch, {"text": "本来就这样", "at": time.time() - 60})
    brain._fold_appraisal_mood({"feeling_text": "不该写进去"}, _offer("ev9"),
                               {"result": "rejected"}, turn_id="t3")
    assert _read(path)["mood"]["text"] == "本来就这样"


def test_empty_feeling_or_reference_is_ignored(tmp_path, monkeypatch):
    from loop import brain

    path = _state(tmp_path, monkeypatch)
    brain._fold_appraisal_mood({"feeling_text": "   "}, _offer("ev9"),
                               {"result": "committed"}, turn_id="t4")
    brain._fold_appraisal_mood({"feeling_text": "有情绪"}, _offer(""),
                               {"result": "committed"}, turn_id="t5")
    assert _read(path)["mood"]["text"] == ""


def test_say_carried_appraisal_writes_the_mood(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from core.context import cognitive_commit as cc
    from core.context import experience_commit
    from loop import brain

    path = _state(tmp_path, monkeypatch)
    monkeypatch.setattr(experience_commit, "submit", lambda *args, **kwargs: {"result": "committed"})
    monkeypatch.setattr(brain.ledger, "append_receipt", lambda **kwargs: kwargs)
    offer = SimpleNamespace(reference="ev11")
    ctx = {"scope": cc.Scope("wechat", "acct", "c1"), "offers": {"ev11": offer},
           "visible": frozenset({"ev11"})}
    brain._commit_say_appraisal({"meaning": "他在等我的答复", "feeling": "有点紧张"},
                                {"result": "sent"}, ctx, turn_id="t6", dry_run=False)
    # 这个 offer 没带会话坐标（手搓的），所以落回全局 mood。
    assert _read(path)["mood"]["text"] == "有点紧张"

def test_mood_evidence_refs_survive_a_reload(tmp_path, monkeypatch):
    _state(tmp_path, monkeypatch, {"text": "有点闷", "at": time.time(), "evidence_refs": ["ev1"]})
    seen = []
    monkeypatch.setattr(selfstate, "_mood_source",
                        lambda refs, **kw: seen.append(tuple(refs)) or "P6：你到底行不行啊")
    out = selfstate.render()
    assert seen == [("ev1",)]
    assert "那会儿是因为" in out and "你到底行不行啊" in out


# ---- 2026-11-24：心境按会话存（设计口径：mood 是会话维度） ----

def test_mood_is_kept_per_conversation(tmp_path, monkeypatch):
    _state(tmp_path, monkeypatch)
    selfstate.update(mood="群里的烦躁", mood_scope=("wechat", "acct", "g1"))
    selfstate.update(mood="私聊的轻松", mood_scope=("wechat", "acct", "p1"))
    group = selfstate.render(conversation_scope=("wechat", "acct", "g1"))
    private = selfstate.render(conversation_scope=("wechat", "acct", "p1"))
    assert "心境：群里的烦躁" in group and "私聊的轻松" not in group
    assert "心境：私聊的轻松" in private and "群里的烦躁" not in private


def test_legacy_global_mood_still_shows_without_a_scoped_entry(tmp_path, monkeypatch):
    _state(tmp_path, monkeypatch, {"text": "旧全局心情", "at": time.time() - 60})
    out = selfstate.render(conversation_scope=("wechat", "acct", "new-chat"))
    assert "心境：旧全局心情" in out


def test_scoped_mood_wins_over_the_global_one(tmp_path, monkeypatch):
    _state(tmp_path, monkeypatch, {"text": "旧全局心情", "at": time.time() - 60})
    selfstate.update(mood="这个会话此刻", mood_scope=("wechat", "acct", "g1"))
    out = selfstate.render(conversation_scope=("wechat", "acct", "g1"))
    assert "心境：这个会话此刻" in out and "旧全局心情" not in out
