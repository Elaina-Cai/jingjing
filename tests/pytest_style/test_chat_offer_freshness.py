"""空闲唤醒没新话时，旧聊天原话不再被重新递成评价候选。"""
from datetime import datetime
from types import SimpleNamespace

from core.terminal import shadow
from core.terminal.event_model import Event, EventType
from test_appraisal_prompt_integration import Model, drive, runtime


CHAT = "writers@chatroom"
OLD = "evt-old"
OLD_WORDS = "先领着，暂时用不到，学到了再说"
OFFER_LINE = "当这句话改变了我对局面的理解"


def _turn(brain, tmp, monkeypatch, *, wake):
    events = {OLD: Event(event_id=OLD, event_type=EventType.MESSAGE, platform="wechat",
                        bot_account_id="acct", chat_id=CHAT, actor_account_id="u1",
                        occurred_at=datetime(2026, 1, 2), content=OLD_WORDS)}
    monkeypatch.setattr(shadow, "get_default_store", lambda: SimpleNamespace(get=events.get))
    model = Model([{"tool_calls": [{"id": "idle", "name": "rest", "arguments": {"why": "看过就算"}}]}])
    drive(brain.run_loop(str(tmp / "wechat" / "acct" / "account.db"), "这一窗最近没有新话",
                         llm=model, max_steps=1, dry_run=True, platform="wechat", chat_id=CHAT,
                         visible_event_ids=(OLD,), wake=wake))
    names = [tool["function"]["name"] for tool in model.tool_schemas[0]]
    return names, model.prompts[0][-1]["content"]


def _offered(names, prompt):
    return "evaluate_experience" in names and OFFER_LINE in prompt


def test_idle_wake_does_not_reoffering_old_chat_words(runtime, monkeypatch):
    brain, tmp = runtime
    names, prompt = _turn(brain, tmp, monkeypatch, wake={
        "last_tick_ms": 1_767_000_000_000, "addressed": {}, "goal_reviews": [],
    })
    assert "evaluate_experience" not in names
    assert OLD_WORDS not in prompt


def test_new_peer_message_still_offers_appraisal(runtime, monkeypatch):
    brain, tmp = runtime
    names, prompt = _turn(brain, tmp, monkeypatch, wake={
        "last_tick_ms": 1_767_000_000_000,
        "peers": [{"at": "2026-01-02 10:00:00", "chat": CHAT, "who": "u1", "text": OLD_WORDS}],
        "addressed": {}, "goal_reviews": [],
    })
    assert _offered(names, prompt)


def test_direct_address_offers_appraisal_without_peer_facts(runtime, monkeypatch):
    brain, tmp = runtime
    names, prompt = _turn(brain, tmp, monkeypatch, wake={
        "last_tick_ms": 1_767_000_000_000,
        "addressed": {"kind": "named", "chat_id": CHAT, "must_answer": True,
                      "request_row_id": "42", "request_text": "@鲸鲸 你还在吗"},
        "goal_reviews": [],
    })
    assert _offered(names, prompt)
