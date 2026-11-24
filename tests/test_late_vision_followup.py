"""晚到的图：事后补一句（hint 版）的行为约束。"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

from sqlalchemy import create_engine

from agents import late_vision_followup as lvf


def _engine(tmp_path):
    db = tmp_path / "account.db"
    con = sqlite3.connect(db)
    con.execute(
        "create table message_media (id integer primary key, chat_id text, kind text, "
        "local_path text, description text, resolved_at text, meta_json text, status text)"
    )
    con.execute(
        "create table messages (id integer primary key, chat_id text, "
        "created_at text, is_self integer)"
    )
    con.commit()
    con.close()
    return create_engine("sqlite:///" + str(db).replace("\\", "/")), db


def _seed(db, *, resolved_at, last_self_at, description="蓝发女孩头像，在问吃没吃饭",
          chat_id="123@chatroom"):
    con = sqlite3.connect(db)
    con.execute(
        "insert into message_media (chat_id, kind, local_path, description, resolved_at, "
        "meta_json, status) values (?,?,?,?,?,?,?)",
        (chat_id, "image", r"C:\x\a.jpg", description, resolved_at, "{}", "resolved"),
    )
    if last_self_at:
        con.execute(
            "insert into messages (chat_id, created_at, is_self) values (?,?,1)",
            (chat_id, last_self_at),
        )
    con.commit()
    con.close()


def _fmt(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%d %H:%M:%S.%f")


def test_hint_is_narration_to_her_not_about_her():
    hint = lvf.build_late_followup_hint(description="蓝发女孩在问吃饭没", minutes_ago=6)
    assert "你终于看清了" in hint
    assert "蓝发女孩在问吃饭没" in hint
    assert "6 分钟前" in hint
    assert "她" not in hint


def test_hint_says_gang_nei_for_group():
    hint = lvf.build_late_followup_hint(description="一张图", minutes_ago=1, chat_type="group")
    assert "在群里" in hint
    assert "刚才" in hint


def test_enabled_reads_env(monkeypatch):
    monkeypatch.setenv(lvf.FOLLOWUP_ENV, "off")
    assert lvf.enabled() is False
    monkeypatch.setenv(lvf.FOLLOWUP_ENV, "1")
    assert lvf.enabled() is True


def test_publishes_once_when_she_spoke_before_the_file_was_readable(tmp_path, monkeypatch):
    eng, db = _engine(tmp_path)
    now = datetime.now()
    _seed(
        db,
        resolved_at=_fmt(now - timedelta(minutes=1)),
        last_self_at=(now - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S"),
    )
    sent = []
    monkeypatch.setattr(lvf, "_publish", lambda **kw: sent.append(kw) or True)

    first = lvf.maybe_publish_late_followup(eng, platform="wechat", account_id="wxid_x", now=now)
    second = lvf.maybe_publish_late_followup(eng, platform="wechat", account_id="wxid_x", now=now)

    assert first["published"] == 1
    assert second["published"] == 0
    assert len(sent) == 1
    assert sent[0]["chat_type"] == "group"
    assert "蓝发女孩头像" in sent[0]["hint"]


def test_skips_when_she_already_spoke_after_the_file_appeared(tmp_path, monkeypatch):
    eng, db = _engine(tmp_path)
    now = datetime.now()
    _seed(
        db,
        resolved_at=_fmt(now - timedelta(minutes=6)),
        last_self_at=(now - timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S"),
    )
    monkeypatch.setattr(lvf, "_publish", lambda **kw: True)
    out = lvf.maybe_publish_late_followup(eng, platform="wechat", account_id="wxid_x", now=now)
    assert out["published"] == 0


def test_skips_old_resolutions_outside_the_window(tmp_path, monkeypatch):
    eng, db = _engine(tmp_path)
    now = datetime.now()
    _seed(
        db,
        resolved_at=_fmt(now - timedelta(hours=3)),
        last_self_at=(now - timedelta(hours=4)).strftime("%Y-%m-%d %H:%M:%S"),
    )
    monkeypatch.setattr(lvf, "_publish", lambda **kw: True)
    out = lvf.maybe_publish_late_followup(eng, platform="wechat", account_id="wxid_x", now=now)
    assert out["published"] == 0


def test_skips_rows_without_description(tmp_path, monkeypatch):
    eng, db = _engine(tmp_path)
    now = datetime.now()
    _seed(
        db,
        resolved_at=_fmt(now - timedelta(minutes=1)),
        last_self_at=(now - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S"),
        description="",
    )
    monkeypatch.setattr(lvf, "_publish", lambda **kw: True)
    out = lvf.maybe_publish_late_followup(eng, platform="wechat", account_id="wxid_x", now=now)
    assert out["published"] == 0


if __name__ == "__main__":
    import sys as _sys
    from pathlib import Path as _Path

    for _p in (str(_Path(__file__).resolve().parent.parent), str(_Path(__file__).resolve().parent)):
        if _p not in _sys.path:
            _sys.path.insert(0, _p)
    from _mini_pytest import run_all as _run_all

    _sys.exit(0 if _run_all(globals()) else 1)
