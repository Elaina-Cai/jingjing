"""现场补充两段（施工 Step 7）：B5 在场的人 / J 旧事检索。

底线：**没有内容就整段不出现**——空标题会让她以为"我看过了"，也会让台账像有数据。
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from loop import brief  # noqa: E402


def test_no_chat_id_means_no_line() -> None:
    assert brief.room_line("") == ""


def test_room_line_counts_and_owner(monkeypatch) -> None:
    # 2026-11-02 群术语改造：主人判定改由 _owner_ids()（不再看名单第三列）。
    monkeypatch.setattr(brief, "_roster_rows",
                        lambda cid: [("wxid_a", "小明", 0), ("wxid_b", "阿 三", 0), ("wxid_c", "P6", 0)])
    monkeypatch.setattr(brief, "_owner_ids", lambda: {"wxid_a"})
    line = brief.room_line("123@chatroom", "小明")
    assert "3 个人" in line and "主人在" in line and "小明" in line


def test_room_line_says_when_the_name_is_missing(monkeypatch) -> None:
    # 2026-11-02：那句「名单里没有叫 X 的」被有意删除（调用方传的是群名，永远匹配不上）。
    monkeypatch.setattr(brief, "_roster_rows", lambda cid: [("wxid_a", "小明", 0)])
    monkeypatch.setattr(brief, "_owner_ids", lambda: set())
    line = brief.room_line("123@chatroom", "老王")
    assert "没有叫" not in line, line
    assert "小明" in line


def test_room_line_reads_person_notes_only_from_the_current_chat(monkeypatch) -> None:
    from core.context import person_notes
    from loop import ports

    monkeypatch.setattr(brief, "_roster_rows", lambda cid: [("wxid_a", "小明", 0)])
    monkeypatch.setattr(brief, "_owner_ids", lambda: set())
    seen = []

    def notes(chat_id="", active_only=True):
        seen.append((chat_id, active_only))
        return [
            {"person_key": "wxid_a", "content": "这是当前群里的资料",
             "chat_id": "group@chatroom", "platform": "wechat", "account_id": "acct"},
            {"person_key": "wxid_a", "content": "这是别处私聊的秘密",
             "chat_id": "private", "platform": "wechat", "account_id": "acct"},
            {"person_key": "wxid_a", "content": "这是同号其他账号的资料",
             "chat_id": "group@chatroom", "platform": "wechat", "account_id": "other"},
        ]

    monkeypatch.setattr(person_notes, "list_notes", notes)
    token = ports._READ_SCOPE.set(ports.ReadScope("wechat", "acct", "group@chatroom"))
    try:
        line = brief.room_line("group@chatroom", "小明")
    finally:
        ports._READ_SCOPE.reset(token)
    assert seen == [("group@chatroom", True)]
    assert "当前群里的资料" in line
    assert "别处私聊的秘密" not in line
    assert "其他账号的资料" not in line


def test_chat_note_is_scoped_to_the_current_account(monkeypatch) -> None:
    from core.context import person_notes
    from loop import ports

    monkeypatch.setattr(person_notes, "notes_enabled_for", lambda chat_id: True)
    monkeypatch.setattr(person_notes, "list_notes", lambda **kwargs: [
        {"person_key": "", "content": "当前账号的群背景", "chat_id": "group@chatroom",
         "platform": "wechat", "account_id": "acct"},
        {"person_key": "", "content": "其他账号的群背景", "chat_id": "group@chatroom",
         "platform": "wechat", "account_id": "other"},
    ])
    token = ports._READ_SCOPE.set(ports.ReadScope("wechat", "acct", "group@chatroom"))
    try:
        line = brief.chat_note_line("group@chatroom", "wechat")
    finally:
        ports._READ_SCOPE.reset(token)
    assert "当前账号的群背景" in line
    assert "其他账号的群背景" not in line


def test_recall_line_is_empty_when_nothing_is_hit() -> None:
    rows = [{"text": "别的话"}]
    assert brief.recall_line("天气不错", "", memo_rows=rows, history_rows=[]) == ""
    assert brief.recall_line("", "", memo_rows=rows, history_rows=[]) == ""


def test_recall_line_finds_a_memo() -> None:
    line = brief.recall_line("他今天又加班", "", target="wxid_a", memo_rows=[{"target": "wxid_a", "text": "10-27 他又加班到十点"}], history_rows=[])
    assert "我还记得" in line and "加班" in line


def test_brain_only_adds_them_when_v2_is_on() -> None:
    src = (ROOT / "loop" / "brain.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    calls = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
             and n.func.attr in ("room_line", "recall_line")]
    assert len(calls) == 2
    assert "if brief.enabled():" in src
# ---- 2026-11-03「未完的事」：读世界模型里 status='open' 的 Intention（只读，不写）----

def _model(*specs):
    from core.context.situation import WorldModel, open_intention
    m = WorldModel()
    for kw in specs:
        open_intention(m, **kw)
    return m


def test_open_loop_line_needs_a_chat_id() -> None:
    assert brief.open_loop_line("") == ""
    assert brief.open_loop_line("   ") == ""


def test_open_loop_line_is_empty_when_nothing_is_open() -> None:
    from core.context.situation import WorldModel, close_intention, open_intention
    assert brief.open_loop_line("c1", model=WorldModel()) == ""
    m = WorldModel()
    open_intention(m, "问一下老王搬没搬", kind="remind", due_at="2026-11-04 09:00")
    close_intention(m, "问一下老王搬没搬", result="done")
    assert brief.open_loop_line("c1", model=m) == ""


def test_open_loop_line_shows_a_pending_reminder() -> None:
    model = _model({"key": "问一下老王搬没搬", "kind": "remind", "due_at": "2026-11-04 09:00"})
    line = brief.open_loop_line("c1", model=model)
    assert "还没到点" in line
    assert "2026-11-04 09:00" in line
    assert "问一下老王搬没搬" in line


def test_open_loop_line_says_what_she_is_waiting_for() -> None:
    model = _model({"key": "等他把报价发来", "kind": "observe", "condition": "老王"})
    line = brief.open_loop_line("c1", model=model)
    assert "在等" in line and "老王" in line


def test_open_loop_line_caps_at_two_items() -> None:
    model = _model(*[{"key": "第%d件事" % i, "kind": "remind",
                      "due_at": "2026-11-%02d 09:00" % i} for i in (1, 2, 3)])
    line = brief.open_loop_line("c1", model=model)
    lines = line.split("\n")
    assert lines[0] == brief.OPEN_LOOP_HEAD, line
    assert len(lines) == 1 + 2, line          # 提示行 + 最多 2 条


def test_open_loop_line_clips_one_long_item() -> None:
    model = _model({"key": "很长的" * 60, "kind": "remind", "due_at": "2026-11-04 09:00"})
    line = brief.open_loop_line("c1", model=model)
    lines = line.split("\n")
    assert lines[0] == brief.OPEN_LOOP_HEAD, line
    assert "…" in lines[1], line
    assert len(lines[1]) <= brief.OPEN_LOOP_ITEM, len(lines[1])
    assert len(line) <= brief.OPEN_LOOP_CHARS, len(line)


def test_open_loop_line_carries_its_own_label() -> None:
    """块的标题在设计上不进 prompt（见方案文档 E-16），所以内容必须自带一行提示。"""
    model = _model({"key": "问一下老王搬没搬", "kind": "remind", "due_at": "2026-11-04 09:00"})
    line = brief.open_loop_line("c1", model=model)
    assert line.split("\n")[0] == brief.OPEN_LOOP_HEAD, line
    assert len(lines := line.split("\n")) == 2, lines


def test_open_loop_filters_revoked_and_labels_unknown(monkeypatch) -> None:
    from core.context import situation
    from core.memory import belief_commit

    model = situation.WorldModel(scope={
        "platform": "wechat",
        "account_id": "bot",
        "chat_id": "c1",
    })
    situation.open_intention(
        model,
        "dead request",
        kind="act",
        due_at="08:00",
        evidence_refs=("dead-event",),
    )
    situation.open_intention(
        model,
        "unknown request",
        kind="act",
        due_at="08:00",
        evidence_refs=("missing-event",),
    )
    monkeypatch.setattr(
        belief_commit,
        "evidence_state",
        lambda ref, scope: "dead" if ref == "dead-event" else "unknown",
    )

    line = brief.open_loop_line("c1", model=model)

    assert "dead request" not in line
    assert "unknown request" in line
    assert "依据未核实" in line


def test_prompt_stats_counts_the_rendered_header() -> None:
    """台账靠**文本匹配**认块（`prompt_stats.load_turns` 里 `t in sysmsg or t in user1`）。

    标题 `未完的事（还欠着什么）` 从不进 prompt（E-16），所以 `EXTRA` 里必须是内容自带的
    那一行提示；写成账本名 `"未完的事"` 会永远匹配 0 次，S3 的读数会整片假阴。
    """
    src = (ROOT / "scripts" / "prompt_stats.py").read_text(encoding="utf-8")
    needle = brief.OPEN_LOOP_HEAD.rstrip("：: ")
    assert needle in src, needle
    assert '"未完的事"' not in src, "EXTRA 里放账本名匹配不到任何 prompt"
