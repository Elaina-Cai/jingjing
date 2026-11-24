"""群名单标出自己，以及 myself 按群昵称绑定当前发言人。"""

import asyncio
from types import SimpleNamespace

from loop import brain, brief, ports, selfstate


def test_group_line_marks_self_by_exact_alias(monkeypatch):
    from core.config import settings

    monkeypatch.setattr(brief, "_roster_rows", lambda chat_id: [
        ("wxid_self", "小鲸鲸", 0),
        ("wxid_fy", "风语", 0),
    ])
    monkeypatch.setattr(brief, "_notes_by_person", lambda chat_id="": {})
    monkeypatch.setattr(brief, "_owner_ids", lambda: set())
    monkeypatch.setattr(settings, "config", SimpleNamespace(wechat=SimpleNamespace(get_self_wxid=lambda: "")))
    monkeypatch.setattr(ports, "me_aliases", lambda: ("小鲸鲸",))

    out = brief._group_line("room@chatroom", "")

    assert "小鲸鲸（我）" in out
    assert "我" not in next(line for line in out.splitlines() if line.startswith("- 风语"))


def test_myself_binds_group_speaker_display_name(tmp_path, monkeypatch):
    monkeypatch.setattr(selfstate, "STATE", tmp_path / "self_state.json")
    monkeypatch.setattr(brief, "_roster_rows", lambda chat_id: [
        ("wxid_self", "小鲸鲸", 0),
        ("wxid_fy", "风语", 0),
    ])
    token = ports._READ_SCOPE.set(ports.ReadScope("wechat", "acct", "room@chatroom"))
    try:
        observation, receipt = asyncio.run(brain._exec(
            "myself", {"about": "风语", "note": "他说先别劝"},
            db=str(tmp_path / "account.db"), dry_run=True, turn_id="t",
            stance_binding=(("wechat", "acct", "room@chatroom", "wxid_fy"), ("evt-1",)),
        ))
    finally:
        ports._READ_SCOPE.reset(token)

    stance = selfstate.load()["scoped_stance"]
    assert stance
    assert "他说先别劝" in next(iter(stance.values()))["text"]
    assert "没记下看法" not in observation
    assert receipt["why"] != "看法需要绑定到这一窗的唯一当前发言人"