# -*- coding: utf-8 -*-
"""Step 5 锁：`snapshot.render_turns()` 只出原文，一行一句，不带元注释。

对照物是 render_text()——那边是"她看到的世界"，`｜ 这条没提到我的名字`、
`（前一条 …）`、`（离现在 68.4 小时）` 都是有意留的注释。两个函数必须分开：
原文干净，世界带注释。见 docs/提示词形状改造施工.md Step 5。
"""
from datetime import datetime, timedelta

import pytest

import sys as _sys
from pathlib import Path as _Path

_ROOT = str(_Path(__file__).resolve().parents[1])
if _ROOT not in _sys.path:
    _sys.path.insert(0, _ROOT)

from loop import snapshot  # noqa: E402

CELL_PRIV = {
    "chat_id": "wxid_a", "chat_type": "private", "label": "Elaina",
    "last_at": "2026-10-30 23:02", "last_speaker": "Elaina", "last_is_self": False,
    "preview": "在吗", "tier": "ask", "hits": 2,
    "toward": "这条没提到我的名字",
    "peer_last": None,
    "prev": [
        {"at": "2026-10-30 21:37", "speaker": "我", "text": "什么事 你说"},
        {"at": "2026-10-30 23:01", "speaker": "Elaina", "text": "没啥事，今天你有看到好玩的事情吗"},
    ],
}

_Q = "[引用我 22:50 说的（前一条 忽念 22:49「绷」）: 好] 行吧"
CELL_GROUP = {
    "chat_id": "123@chatroom", "chat_type": "group", "label": "作业群",
    "last_at": "2026-10-30 23:03", "last_speaker": "忽念", "last_is_self": False,
    "preview": "绷不住了", "toward": "",
    # peer_last 与 prev 里是同一条 —— 不许出两遍
    "peer_last": {"at": "2026-10-30 22:59", "speaker": "P4", "text": _Q},
    "prev": [
        {"at": "2026-10-30 22:59", "speaker": "P4", "text": _Q},
        {"at": "2026-10-30 22:40", "speaker": "忽念", "text": "[表情包]"},
    ],
}

SNAP = {"at": "2026-10-31 12:50", "week": "周六", "db": "x.db",
        "cells": [CELL_GROUP, CELL_PRIV], "total": 2}


def test_one_line_per_turn_no_duplicates():
    lines = snapshot.render_turns(SNAP, limit=0).splitlines()
    # 私聊 3 条（2 prev + 末条）+ 群聊 3 条（2 prev + 末条），peer_last 与 prev 同条 → 只出一次
    assert len(lines) == 6
    for ln in lines:
        assert "\n" not in ln


def test_order_is_chronological():
    lines = snapshot.render_turns(SNAP, limit=0).splitlines()
    stamps = [ln[:11] for ln in lines]
    assert stamps == sorted(stamps)
    assert lines[-1].startswith("10-30 23:03")


def test_speaker_prefix():
    txt = snapshot.render_turns(SNAP, limit=0)
    assert "Elaina：在吗" in txt
    assert "我：什么事 你说" in txt
    assert "P4：" in txt
    assert "忽念：绷不住了" in txt


@pytest.mark.parametrize("own_last", [False, True])
def test_real_snapshot_keeps_same_second_messages_in_id_order(tmp_path, monkeypatch, own_last):
    from db import world_db

    monkeypatch.setattr(world_db, "PATH", str(tmp_path / "world.db"))
    monkeypatch.setattr(world_db, "_READY", [False])
    monkeypatch.setattr(world_db, "sync_accounts", lambda: None)
    monkeypatch.setattr(snapshot.ports, "chat_label", lambda db, chat: "合成群聊")
    monkeypatch.setattr(snapshot.ports, "me_aliases", lambda: ())
    account, chat = "wxid_snapshot_fixture", "fixture@chatroom"
    db = str(tmp_path / "wechat" / account / "account.db")
    at = (datetime.now() - timedelta(minutes=3)).strftime("%Y-%m-%d %H:%M:%S")
    messages = [
        (9, "甲", "好"),
        (10, "乙", "好"),
        (11, "甲", "好"),
        (12, "丙", "收到"),
    ]
    if own_last:
        messages.append((13, "我", "那就这样"))
    for msg_id, speaker, text in reversed(messages):
        assert world_db.record_inbound("wechat", account, chat, f"wechat:{account}:{msg_id}", at,
                                       who=speaker, text=text, is_self=int(speaker == "我"))

    snap = snapshot.build_snapshot(db)
    expected = [f"{at[5:16]} {speaker}：{text}" for _, speaker, text in messages]
    assert snapshot.render_turns(snap, chat, limit=0).splitlines() == expected
    assert snapshot.render_turns(snap, chat, limit=2).splitlines() == expected[-2:]
    if own_last:
        assert snap["cells"][0]["my_last"] == {"at": at, "text": "那就这样"}
        assert snap["cells"][0]["peer_before_mine"].msg_id == 12
        assert snapshot.render_text(snap).count("丙: 收到") == 1


def test_no_meta_annotations():
    txt = snapshot.render_turns(SNAP, limit=0)
    for bad in ("（前一条", "离现在", "｜", "↳", "这条没提到我的名字"):
        assert bad not in txt, bad


def test_quote_annotation_collapsed_to_plain():
    txt = snapshot.render_turns(SNAP, limit=0)
    assert "（引用「好」）行吧" in txt
    assert "22:50 说的" not in txt


def test_limit_keeps_newest():
    lines = snapshot.render_turns(SNAP, limit=2).splitlines()
    assert len(lines) == 2
    assert lines[-1].startswith("10-30 23:03")


def test_label_filter():
    txt = snapshot.render_turns(SNAP, "Elaina", limit=0)
    assert txt.count("\n") == 2
    assert "作业群" not in txt and "忽念" not in txt
    assert snapshot.render_turns(SNAP, "wxid_a", limit=0) == txt


def test_empty_snap():
    assert snapshot.render_turns({}, limit=0) == ""
    assert snapshot.render_turns({"cells": []}) == ""


def test_render_text_still_annotated():
    """语义未变：世界那一路的注释还在（这是刻意的，别一起清掉）。"""
    world = snapshot.render_text(SNAP)
    assert "｜ 这条没提到我的名字" in world
    assert "★2 私「Elaina」" in world  # 世界那边仍是 ★2 私「Elaina」 23:02 Elaina: 在吗 的排布


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))

def test_render_text_back_false_drops_lookback():
    """2026-10-31 ①：回看行不进世界段（交给 [原文] 段），但最新那条的注释要留。"""
    slim = snapshot.render_text(SNAP, back=False)
    full = snapshot.render_text(SNAP)
    assert "↳ 再往前" not in slim and "↳ 上一条" not in slim
    assert "↳ 再往前" in full          # 默认行为不变（look 那条路还要历史）
    assert "｜ 这条没提到我的名字" in slim   # 最新一条的注释保留
    assert "私「Elaina」" in slim and "群「作业群」" in slim  # 每个会话仍各有一行


def test_strip_meta_keeps_bracket_closed():
    """2026-10-31：切掉「｜原文：…」不能把 `]` 一起带走，否则她会看到没说完的话。"""
    from loop import snapshot as S
    assert S._strip_meta("[图片: 一只猫｜原文：abc]") == "[图片: 一只猫]"
    assert S._strip_meta("[表情包: 喔｜原文：x]") == "[表情包: 喔]"


def test_prev_not_truncated_in_source():
    """私聊「最近 6 条」的文字上限已拆（10-29 12:30 那条引用+提问曾被挤掉）。"""
    import inspect
    from loop import snapshot as S
    src = inspect.getsource(S.build_snapshot)
    assert "_txt_rows[:" not in src
    assert "prev = list(_txt_rows)" in src


def _mk_snap(n=40, chars=100, label="Elaina", chat_id="wxid_x"):
    prev = [{"at": "2026-10-29 %02d:%02d:00" % (10 + i // 60, i % 60),
             "speaker": "Elaina", "text": "说" * chars} for i in range(n)]
    return {"cells": [{"label": label, "chat_id": chat_id, "chat_type": "private",
                       "tier": "ask", "prev": prev, "peer_last": {},
                       "last_at": "2026-10-29 23:59:00", "last_speaker": "Elaina",
                       "preview": "最后一句", "last_is_self": False}]}


def test_budget_caps_chars_not_lines():
    """2026-10-31：原文按字数自适应——40 条各 100 字，budget=350 只留最近几条。"""
    from loop import snapshot as S
    snap = _mk_snap(n=40, chars=100)
    txt = S.render_turns(snap, "Elaina", limit=0, budget=350)
    assert len(txt) <= 350 + 120        # 至少一条也留，所以给一条的余量
    assert txt.count("\n") + 1 <= 4     # 不会还是 15 条


def test_budget_keeps_newest_even_if_too_long():
    from loop import snapshot as S
    snap = _mk_snap(n=5, chars=2000)
    txt = S.render_turns(snap, "Elaina", limit=0, budget=100)
    assert txt.strip()                       # 最新一条仍然在
    assert "最后一句" in txt                  # 而且是最新的那句（last 单元格）


def test_budget_keeps_chronological_order():
    from loop import snapshot as S
    snap = _mk_snap(n=30, chars=50)
    txt = S.render_turns(snap, "Elaina", limit=0, budget=400)
    ats = [ln[:11] for ln in txt.splitlines() if ln]
    assert ats == sorted(ats)


def test_budget_none_means_no_char_cap():
    from loop import snapshot as S
    snap = _mk_snap(n=40, chars=100)
    assert len(S.render_turns(snap, "Elaina", limit=0, budget=None)) > 3000


def test_run_py_uses_budget():
    import inspect
    from loop import run as R
    src = inspect.getsource(R.one_tick)
    assert "render_turns(snap, _w, limit=0)" in src   # 条数不卡，字数由 TURNS_BUDGET_CHARS 管
    from loop import snapshot as S
    assert S.TURNS_BUDGET_CHARS == 1500
