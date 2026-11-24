# -*- coding: utf-8 -*-
"""日记写入统一：notes.jsonl 单一现役写口，self_history.jsonl 只读兼容。"""
from __future__ import annotations

import asyncio
import json

import pytest

from loop import history, notes


class _LLM:
    def __init__(self, text):
        self.text = text

    async def chat(self, **kwargs):
        return self.text


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(notes, "ROOT", str(tmp_path))
    monkeypatch.setattr(notes, "PATH", str(tmp_path / "notes.jsonl"))
    monkeypatch.setattr(history, "PATH", str(tmp_path / "self_history.jsonl"))
    monkeypatch.setattr(history, "_WRITE_HOUR", 0)
    monkeypatch.setattr(history, "_materials", lambda day: ["材料一", "材料二"])
    notes._CACHE.update({"mtime": 0.0, "rows": []})
    notes._LEGACY.update({"mtime": 0.0, "rows": []})
    yield tmp_path
    notes._CACHE.update({"mtime": 0.0, "rows": []})
    notes._LEGACY.update({"mtime": 0.0, "rows": []})


def test_maybe_write_goes_to_notes_only(store):
    text = "今天我想明白了一件事，值得写下来，也值得以后反复看看。"
    assert asyncio.run(history.maybe_write(llm=_LLM(text))) == text
    rows = [json.loads(line) for line in (store / "notes.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1 and rows[0]["kind"] == "diary" and rows[0]["text"] == text
    assert not (store / "self_history.jsonl").exists()


def test_legacy_self_history_is_still_readable(store):
    legacy = store / "data" / "loop"
    legacy.mkdir(parents=True)
    (legacy / "self_history.jsonl").write_text(
        json.dumps({"day": "2026-01-02", "at": "2026-01-02 03:10:00", "text": "旧日记内容"},
                   ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    notes._LEGACY.update({"mtime": 0.0, "rows": []})
    assert history.has("2026-01-02") is True
    assert "旧日记内容" in history.block()


def test_short_text_is_not_written(store):
    assert asyncio.run(history.maybe_write(llm=_LLM("太短"))) == ""
    assert not (store / "notes.jsonl").exists()


def test_rows_derive_day_from_notes_timestamp(store):
    notes.add("diary", "统一后的日记", at="2026-11-08 03:10:00")
    rows = history._rows()
    assert rows == [{"day": "2026-11-08", "at": "2026-11-08 03:10:00", "text": "统一后的日记"}]


def test_write_failure_returns_empty(store, monkeypatch):
    def fail(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(notes, "add", fail)
    assert asyncio.run(history.maybe_write(llm=_LLM("今天我想明白了一件事，值得写下来，也值得以后反复看看。"))) == ""
    assert not (store / "self_history.jsonl").exists()


def test_source_has_no_legacy_append_or_second_maybe_write():
    import inspect

    source = inspect.getsource(history)
    assert 'io.open(PATH, "a"' not in source
    assert source.count("async def maybe_write") == 1
