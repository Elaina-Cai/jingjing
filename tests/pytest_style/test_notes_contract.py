# -*- coding: utf-8 -*-
"""小本本写入契约：note_id / revision / status / evidence_refs / revoke。"""
from __future__ import annotations

import json

import pytest

from loop import notes


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(notes, "ROOT", str(tmp_path))
    monkeypatch.setattr(notes, "PATH", str(tmp_path / "notes.jsonl"))
    notes._CACHE.update({"mtime": 0.0, "rows": []})
    notes._LEGACY.update({"mtime": 0.0, "rows": []})
    yield tmp_path / "notes.jsonl"
    notes._CACHE.update({"mtime": 0.0, "rows": []})
    notes._LEGACY.update({"mtime": 0.0, "rows": []})


def _raw(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_add_persists_version_evidence_and_stable_id(store):
    row = notes.add("fact", "他住在杭州", target="chat-1", evidence_refs=["evt-1"])
    assert row["note_id"].startswith("note-")
    assert row["revision"] == 1 and row["status"] == "active"
    assert row["evidence_refs"] == ["evt-1"]
    raw = _raw(store)
    assert len(raw) == 1 and raw[0]["note_id"] == row["note_id"]


def test_revise_appends_and_latest_revision_wins(store):
    first = notes.add("fact", "他住在杭州", target="chat-1", evidence_refs=["evt-1"])
    second = notes.revise(first["note_id"], "他搬到上海", evidence_refs=["evt-2"])
    assert second["revision"] == 2
    assert second["evidence_refs"] == ["evt-1", "evt-2"]
    rows = notes.all_rows()
    assert len(rows) == 1
    assert rows[0]["text"] == "他搬到上海"
    assert len(_raw(store)) == 2


def test_revoke_hides_the_note_from_all_reads(store):
    row = notes.add("fact", "这条要撤销", target="chat-1")
    revoked = notes.revoke(row["note_id"], reason="说错了")
    assert revoked["status"] == "revoked"
    assert notes.all_rows() == []
    assert notes.residents("chat-1") == []
    assert len(_raw(store)) == 2
    assert _raw(store)[-1]["revoke_reason"] == "说错了"


def test_visible_rows_excludes_revoked_rows():
    rows = [
        {"kind": "note", "text": "还在", "target": "c", "status": "active"},
        {"kind": "note", "text": "没了", "target": "c", "status": "revoked"},
        {"kind": "note", "text": "旧行", "target": "c"},
    ]
    assert [row["text"] for row in notes.visible_rows(rows, "c")] == ["还在", "旧行"]


def test_legacy_row_without_note_id_is_still_readable(store):
    legacy = store.parent / "data" / "loop"
    legacy.mkdir(parents=True)
    (legacy / "memo.jsonl").write_text(
        json.dumps({"at": "2026-01-01 00:00:00", "kind": "fact", "text": "旧事实", "target": "c"},
                   ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    notes._LEGACY.update({"mtime": 0.0, "rows": []})
    assert [row["text"] for row in notes.all_rows()] == ["旧事实"]


def test_diary_rows_are_self_readable_without_target(store):
    row = notes.add("diary", "今天想明白了一件事", at="2026-11-08 03:10:00")
    assert notes.diaries(3, target="") == []
    assert [item["note_id"] for item in notes.diary_rows()] == [row["note_id"]]


def test_revise_or_revoke_unknown_note_is_a_noop(store):
    assert notes.revise("note-missing", "新内容") == {}
    assert notes.revoke("note-missing") == {}
    assert _raw(store) == [] if store.exists() else True


def test_empty_text_still_returns_empty_dict(store):
    assert notes.add("note", "   ") == {}
    assert not store.exists() or _raw(store) == []
