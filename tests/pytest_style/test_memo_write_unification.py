"""remember / 常驻板：notes.jsonl 是唯一现役写口，memo.jsonl 只读兼容。"""
import json

import pytest


@pytest.fixture
def mem(tmp_path, monkeypatch):
    from loop import memo, notes

    base = tmp_path / "data" / "loop"
    base.mkdir(parents=True)
    notes_path = base / "notes.jsonl"
    legacy_path = base / "memo.jsonl"
    monkeypatch.setattr(notes, "ROOT", str(tmp_path))
    monkeypatch.setattr(notes, "PATH", str(notes_path))
    monkeypatch.setattr(memo, "FILE", str(legacy_path))
    notes._CACHE.update({"mtime": 0.0, "rows": []})
    notes._LEGACY.update({"mtime": 0.0, "rows": []})
    return notes_path, legacy_path, notes


def _write_legacy(path, rows):
    path.write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
        encoding="utf-8",
    )


def test_memo_add_writes_notes_only(mem):
    notes_path, legacy_path, notes = mem
    from loop import memo

    _write_legacy(legacy_path, [
        {"at": "2026-01-01 00:00:00", "kind": "note", "target": "", "text": "旧行"},
    ])
    before = legacy_path.read_bytes()
    row = memo.add("  他住在杭州  ", kind="fact", target="wxid_a")
    assert row is not None
    assert row["kind"] == "fact" and row["target"] == "wxid_a" and row["text"] == "他住在杭州"
    assert legacy_path.read_bytes() == before, "memo.jsonl 必须停写"
    rows = [json.loads(x) for x in notes_path.read_text(encoding="utf-8").splitlines()]
    assert rows == [row]
    assert "他住在杭州" in memo.resident("wxid_a")


def test_memo_resident_reads_legacy_without_writing(mem):
    notes_path, legacy_path, notes = mem
    from loop import memo

    _write_legacy(legacy_path, [
        {"at": "2026-01-01 00:00:00", "kind": "promise", "target": "wxid_a", "text": "答应早点睡"},
    ])
    before = legacy_path.read_bytes()
    text = memo.resident("wxid_a")
    assert "答应过：答应早点睡" in text
    assert not notes_path.exists()
    assert legacy_path.read_bytes() == before


def test_memo_add_empty_writes_nothing(mem):
    notes_path, legacy_path, notes = mem
    from loop import memo

    assert memo.add("   ", kind="fact") is None
    assert not notes_path.exists()
    assert not legacy_path.exists()


def test_memo_add_keeps_200_char_cap(mem):
    notes_path, legacy_path, notes = mem
    from loop import memo

    row = memo.add("字" * 260, kind="note", target="wxid_a")
    assert row and len(row["text"]) == 200


def test_memo_unknown_target_is_not_global(mem):
    from loop import memo

    memo.add("来源不明的事", kind="fact", target="")
    assert "来源不明的事" not in memo.resident("wxid_a")
    assert "来源不明的事" not in memo.resident("")


def test_resident_dedupes_legacy_and_new(mem):
    notes_path, legacy_path, notes = mem
    from loop import memo

    _write_legacy(legacy_path, [
        {"at": "2026-01-01 00:00:00", "kind": "fact", "target": "wxid_a", "text": "同一句"},
    ])
    memo.add("同一句", kind="fact", target="wxid_a")
    assert memo.resident("wxid_a").count("同一句") == 1


def test_remember_is_visible_in_unified_note_block(mem):
    from loop import memo, notes

    memo.add("记住这条", kind="rule", target="wxid_a")
    assert any("记住这条" in line for line in notes.note_block("wxid_a", 300))


def test_memo_has_no_legacy_append_write():
    from pathlib import Path

    src = (Path(__file__).resolve().parents[2] / "loop" / "memo.py").read_text(encoding="utf-8")
    assert 'open(FILE, "a"' not in src
    assert "notes.add(" in src
