# -*- coding: utf-8 -*-
"""群里「没有人在跟我说话」就不许自发开口（用户 2026-11-04 选的「松档」B）。

2026-11-04 之后她仍然会在群里主动开新话题（`spont_streak` 涨到 12 也不叫停，E-51）。
这里钉的是那条闸门：判据来自 `loop/addressing.py`（代码算），闸门在
`loop/spont.py::addressed_allows`，说法在 `loop/brain.py::_addressed_line`。

这些用例全是真的调函数、看返回值；不读源码文本。
"""
import sys
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from loop import brain, spont  # noqa: E402

GROUP = "10000000001@chatroom"
PRIVATE = "wxid_seed01"


def _wake(kind, *, chat=GROUP, why="这条 @ 了你", who="P5", must=False, **extra):
    w = {"addressed": {"chat_id": chat, "kind": kind, "why": why, "who": who,
                       "must_answer": must}}
    w.update(extra)
    return w


def test_group_small_talk_blocks_spontaneous_speaking():
    assert spont.addressed_allows("", is_group=True) is False


def test_group_small_talk_with_whitespace_is_still_nothing():
    assert spont.addressed_allows("   ", is_group=True) is False


def test_every_addressed_kind_allows_spontaneous_speaking():
    for kind in ("named", "called", "following", "questioned"):
        assert spont.addressed_allows(kind, is_group=True) is True, kind


def test_a_private_chat_is_never_blocked():
    assert spont.addressed_allows("", is_group=False) is True


def test_the_wake_block_says_who_is_talking_and_whether_an_answer_is_due():
    text = brain._wake_block(_wake("named", must=True))
    assert "[这句是不是在跟我说]" in text
    line = [ln for ln in text.split("\n") if ln.startswith("[这句是不是在跟我说]")][0]
    assert "P5" in line
    assert "这条 @ 了你" in line
    assert "必答" in line


def test_a_named_message_can_still_need_an_answer_after_a_quiet_poll():
    wake = _wake("named", must=True)
    previous = {"who": "P5", "text": "我去看看"}
    for extra in ({"peers": [{"chat": GROUP, "who": "P5", "text": "@鲸鲸 回来了吗？"}]},
                  {"mine": [previous]}):
        text = brain._wake_block({**wake, **extra})
        assert "必答" in text
        assert "没有人在等我回话" not in text
        assert "没有人来找我" not in text
        assert "可以回，也可以看过就算" not in text


def test_the_current_request_is_marked_as_prior_to_old_impressions():
    wake = _wake("named", must=True)
    wake["addressed"]["request_text"] = "今天这块展板只有 B 改过，你选一份适合今天的。"
    text = brain._wake_block(wake)
    assert "今天这块展板只有 B 改过" not in text
    assert brain._current_request_block(wake, "other@chatroom") == ""
    block = brain._current_request_block(wake, GROUP)
    assert "这句是本轮有人对我说、或接着我的话在说" in block
    assert "先照它做" in block
    assert "旧偏好只能帮忙选择仍合适的做法" in block
    assert "别把被排除的候选当成还能直接执行的选项" in block


def test_current_request_stays_after_original_turns():
    parts = [("其他现场事实", "world"), (brain._CURRENT_REQUEST_TITLE, "request")]
    _, user = brain._reshape("head", parts, "snapshot", "original")
    assert user.endswith("request")
    assert user.index("world") < user.index("snapshot") < user.index("original") < user.index("request")


def test_decision_guidance_already_made_actions_are_reported_as_done():
    text = brain._decision_guidance()
    assert "工具已经返回的内容，按已经做成的事说" in text
    assert "别再说「我先去打开」「我去看看」" in text
    assert "称呼、语气、回答重点、要不要追问" in text


def test_a_following_kind_is_not_marked_as_must_answer():
    text = brain._wake_block(_wake("following", why="接着你 26 秒前那句", who="P8"))
    line = [ln for ln in text.split("\n") if ln.startswith("[这句是不是在跟我说]")][0]
    assert "可答可不答" in line
    assert "必答" not in line


def test_group_small_talk_forbids_opening_a_new_topic():
    text = brain._wake_block(_wake(""))
    assert "不许开新话题" in text


def test_optional_addressing_also_keeps_the_exact_current_text():
    wake = _wake("following", why="刚才点过你名，又接着说", who="阿岚", must=False)
    wake["addressed"]["request_text"] = "刚才那句话我补充一下。"
    block = brain._current_request_block(wake, GROUP)
    assert '"刚才那句话我补充一下。"' in block
    assert "有人对我说、或接着我的话在说" in block


def test_private_small_talk_is_described_as_no_news():
    text = brain._wake_block(_wake("", chat=PRIVATE))
    line = [ln for ln in text.split("\n") if ln.startswith("[这句是不是在跟我说]")][0]
    assert "这一拍没有新话" in line
    assert "不许开新话题" not in line


def test_without_the_addressed_field_nothing_is_added():
    text = brain._wake_block({"peers": [], "mine": []})
    assert "[这句是不是在跟我说]" not in text


def test_a_spontaneous_tick_carries_the_addressing_line_too():
    text = brain._wake_block(_wake("following", why="接着你 26 秒前那句", spontaneous=True))
    assert "[这句是不是在跟我说]" in text
    assert "是我自己这会儿想说话" in text


def test_a_followup_tick_keeps_its_own_wording():
    text = brain._wake_block(_wake("named", must=True,
                                   followup={"age_s": 600, "text": "我先看看"}))
    assert "你刚说完一句（10 分钟前）" in text
    assert "我先看看" in text
    assert "[这句是不是在跟我说]" in text


def test_a_group_verdict_survives_into_the_wake_text_only_when_addressed():
    blocked = spont.addressed_allows("", is_group=True)
    allowed = spont.addressed_allows("named", is_group=True)
    assert (blocked, allowed) == (False, True)
    assert "不许开新话题" in brain._wake_block(_wake(""))
    assert "必答" in brain._wake_block(_wake("named", must=True))


@pytest.mark.parametrize("mode, title, body, expected", [
    ("owned", "旧鲸", "你刚才那句让我不舒服。", True),
    ("peer", "鲸鲸", "你刚才那句让我不舒服。", False),
    ("revoked", "鲸鲸", "你刚才那句让我不舒服。", False),
    ("missing", "鲸鲸", "你刚才那句让我不舒服。", False),
    ("peer", "鲸鲸", "@鲸鲸 你怎么看？", True),
    ("legacy", "鲸鲸", "你刚才那句让我不舒服。", True),
])
def test_group_quote_author_controls_current_request(monkeypatch, tmp_path, mode, title, body, expected):
    from core.terminal import shadow
    from core.terminal.event_model import Event, EventType
    from db import world_db
    from loop import ports, run, snapshot

    base = datetime.now().replace(microsecond=0) - timedelta(minutes=10)

    def row(num, speaker, author, text, quote=""):
        at = (base + timedelta(seconds=num)).strftime("%Y-%m-%d %H:%M:%S")
        return ports.Row(num, GROUP, "group", speaker, at, text,
                         raw=text, sender_id=author, quote_id=quote, platform_msg_id=f"ext-{num}")

    feedback = row(2, "小池", "u1", f"[引用 {title}: @鲸鲸 你太较真了]\n{body}",
                   "ext-1" if mode != "legacy" else "")
    bystander = row(3, "小林", "u2", "我刚到。")
    quote_author = "u3" if mode == "peer" else "acct"
    quote_kind = EventType.MESSAGE if mode == "peer" else EventType.REPLY

    def event(num, author, kind, text, parent=""):
        return Event(event_id=f"evt-{num}", event_type=kind, platform="wechat", bot_account_id="acct",
                     chat_id=GROUP, actor_account_id=author, occurred_at=base + timedelta(seconds=num),
                     content=text, parent_event_id=parent, external_msg_id=f"ext-{num}")

    quoted = event(1, quote_author, quote_kind, "@鲸鲸 你太较真了")
    incoming = event(2, "u1", EventType.MESSAGE, feedback.text, quoted.event_id)
    events = [incoming] if mode == "missing" else [incoming, quoted]
    terminal = SimpleNamespace(
        get={item.event_id: item for item in events}.get,
        get_by_external_msg_id=lambda **kw: {item.external_msg_id: item for item in events}.get(kw["external_msg_id"]),
        revocation_state=lambda ref: "revoked" if mode == "revoked" and ref == quoted.event_id else "active",
    )
    monkeypatch.setattr(shadow, "get_default_store", lambda: terminal)
    monkeypatch.setattr(world_db, "sync_accounts", lambda: None)
    monkeypatch.setattr(snapshot, "_mark_own_files", lambda rows: rows)
    monkeypatch.setattr(snapshot, "_mark_aliens_world", lambda rows: rows)
    for name, value in {"sense_sync": [feedback, bystander], "media_descriptions": {},
                        "me_aliases": ("鲸鲸",), "chat_label": GROUP, "is_self_chat": False}.items():
        monkeypatch.setattr(ports, name, lambda *args, _value=value, **kw: _value)

    cell = snapshot.build_snapshot(str(tmp_path / "wechat" / "acct" / "account.db"))["cells"][0]
    addressed = run._cell_addressing(cell)
    assert (cell["tier"], cell["hits"]) == (("ask", 1) if expected else ("", 0))
    assert (addressed["kind"], addressed["request_row_id"]) == (("named", "2") if expected else ("", ""))

# ---- 2026-11-24: 只读会话不再判「必答」，也不让她把整拍花在发不出去的 say 上 ----

def test_cell_addressing_keeps_must_answer_when_the_chat_can_speak():
    from loop import run

    rows = [SimpleNamespace(text="@鲸鲸 在吗？", sender_id="u1", msg_id=41, at=100, speaker="P5")]
    got = run._cell_addressing({"platform": "wechat", "chat_id": GROUP,
                                "chat_type": "group", "after_mine": rows})
    assert got["kind"] == "named"
    assert got["must_answer"] is True
    assert not got.get("read_only")


def test_cell_addressing_demotes_must_answer_in_a_read_only_chat(monkeypatch):
    from loop import run, speak_policy

    monkeypatch.setattr(speak_policy, "allowed",
                        lambda platform, chat: (False, "not_in_allowlist"))
    rows = [SimpleNamespace(text="@鲸鲸 在吗？", sender_id="u1", msg_id=41, at=100, speaker="P5")]
    got = run._cell_addressing({"platform": "wechat", "chat_id": GROUP,
                                "chat_type": "group", "after_mine": rows})
    assert got["kind"] == "named"
    assert got["must_answer"] is False
    assert got["read_only"] is True


def test_speak_allowed_fails_open_when_the_policy_cannot_be_read(monkeypatch):
    from loop import run, speak_policy

    def boom(platform, chat):
        raise RuntimeError("policy unreadable")

    monkeypatch.setattr(speak_policy, "allowed", boom)
    assert run._speak_allowed("wechat", GROUP) is True


def test_a_read_only_chat_tells_her_to_wait_instead_of_answering():
    w = _wake("named", must=True)
    w["addressed"]["read_only"] = True
    text = brain._wake_block(w)
    line = [ln for ln in text.split("\n") if ln.startswith("[这句是不是在跟我说]")][0]
    assert "只读" in line
    assert "必答" not in line
    assert "不要调 say" in line
