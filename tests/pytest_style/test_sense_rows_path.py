"""sense_rows 的账号是从 db 路径推出来的——路径给错时要出声，不许静默回 0 行。

2026-10-30 事故：探针把 `data/world.db` 传进去，账号被推成 "data"，
查回 0 行且无任何提示，我据此两次误判"库里没有这种行/这是死代码"。
"""

from __future__ import annotations

from db import world_db


def test_bad_path_says_something(monkeypatch):
    seen: list[tuple] = []
    monkeypatch.setattr(world_db, "warn", lambda ev, **kw: seen.append((ev, kw)))
    rows = world_db.sense_rows("data/world.db")
    assert rows == []
    assert [e for e, _ in seen] == ["sense_rows_db_path_looks_wrong"]
    assert seen[0][1]["acct"] == "data"


def test_proper_account_path_stays_quiet(monkeypatch, tmp_path):
    seen: list[tuple] = []
    monkeypatch.setattr(world_db, "warn", lambda ev, **kw: seen.append((ev, kw)))
    acct_dir = tmp_path / "wxid_abcdef012345"
    acct_dir.mkdir()
    world_db.sense_rows(str(acct_dir / "account.db"))
    assert seen == []
