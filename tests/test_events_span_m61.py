# -*- coding: utf-8 -*-
"""terminal_events 的 span 三列（m061）：写时不解释语义，只记「这一段原文在哪」。

判据：`content_start` / `content_end` / `media_span_json` 是**原文的地址**，不是对内容的
解释；`-1` = 未知（与 `core/contracts/events.py` 的 `EvidenceRef` 同一约定），老行永远是 -1，
读侧不许把 -1 当成"开头"或"没有媒体"。

跑法：
    .venv_it\\Scripts\\python.exe tests\\test_events_span_m61.py
    scripts\\run_all_tests.py --filter events_span
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

ROOT = Path(__file__).resolve().parent.parent
_ISO_ROOT = (ROOT / "data" / "_events_span_test").resolve()
_ISO_ROOT.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("META_DB_PATH", str(_ISO_ROOT / "meta.db"))

from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from core.terminal import event_store_db as store_mod  # noqa: E402
from core.terminal.event_model import Event, EventType  # noqa: E402
from db.meta_models import MetaBase  # noqa: E402


def _fresh_path(name):
    path = _ISO_ROOT / ("%s.db" % name)
    for suffix in ("", "-wal", "-shm"):
        Path(str(path) + suffix).unlink(missing_ok=True)
    return path


def _engine_for(path):
    engine = create_engine("sqlite:///%s" % path)
    MetaBase.metadata.create_all(engine)
    return engine


def _message(content, event_type=EventType.MESSAGE):
    return Event.new(
        event_type=event_type,
        content=content,
        platform="wechat",
        bot_account_id="bot",
        chat_id="span@chatroom",
    )

SPAN_KEYS = ("content_start", "content_end", "media_span_json")


def test_text_content_span_is_whole_text():
    content = "看这个[图片]好家伙"
    values = store_mod._event_to_values(_message(content), span_ok=True)
    assert values["content_start"] == 0, values
    assert values["content_end"] == len(content), values


def test_media_span_offsets_point_at_raw_text():
    content = "看这个[图片]和[表情包:abc]"
    spans = store_mod._media_spans(content)
    assert [s["kind"] for s in spans] == ["image", "sticker"], spans
    for span in spans:
        assert content[span["start"]:span["end"]] == span["raw"], span
    assert spans[0]["raw"] == "[图片]", spans


def test_thought_and_action_carry_no_media_span():
    for event_type in (EventType.THOUGHT, EventType.ACTION):
        values = store_mod._event_to_values(
            _message("[图片]想法", event_type=event_type), span_ok=True
        )
        assert values["content_start"] == 0, values
        assert json.loads(values["media_span_json"]) == [], values


def test_empty_content_is_unknown_not_zero():
    values = store_mod._event_to_values(_message(""), span_ok=True)
    assert values["content_start"] == -1 and values["content_end"] == -1, values
    assert values["media_span_json"] == "[]", values


def test_off_shape_omits_span_keys_entirely():
    values = store_mod._event_to_values(_message("[图片]看图"))
    assert not any(key in values for key in SPAN_KEYS), sorted(values)

def _drop_span_columns(engine):
    with engine.begin() as conn:
        for column in SPAN_KEYS:
            conn.execute(text("ALTER TABLE terminal_events DROP COLUMN %s" % column))


def test_old_shape_db_still_appends_without_span_columns():
    if sqlite3.sqlite_version_info < (3, 35, 0):
        print("  - SKIP old-shape db: sqlite %s 没有 DROP COLUMN" % sqlite3.sqlite_version)
        return
    path = _fresh_path("legacy_shape")
    engine = _engine_for(path)
    _drop_span_columns(engine)
    session_factory = sessionmaker(bind=engine)
    event = _message("[图片]看图")
    with session_factory() as session:
        store_mod.append_in_session(session, event)
        session.commit()
        stored = session.execute(
            text("select content from terminal_events where event_id = :e"),
            {"e": event.event_id},
        ).scalar()
    assert stored == "[图片]看图", stored
    assert store_mod._SPAN_SUPPORT.get(engine) is False, store_mod._SPAN_SUPPORT


def test_new_shape_db_writes_span_columns():
    path = _fresh_path("fresh_shape")
    engine = _engine_for(path)
    session_factory = sessionmaker(bind=engine)
    content = "看这个[图片]好家伙"
    event = _message(content)
    with session_factory() as session:
        store_mod.append_in_session(session, event)
        session.commit()
        row = session.execute(
            text(
                "select content_start, content_end, media_span_json "
                "from terminal_events where event_id = :e"
            ),
            {"e": event.event_id},
        ).first()
    assert row is not None, "row not written"
    assert row[0] == 0 and row[1] == len(content), row
    spans = json.loads(row[2])
    assert spans and spans[0]["kind"] == "image" and spans[0]["raw"] == "[图片]", spans
    assert store_mod._SPAN_SUPPORT.get(engine) is True, store_mod._SPAN_SUPPORT


CASES = (
    test_text_content_span_is_whole_text,
    test_media_span_offsets_point_at_raw_text,
    test_thought_and_action_carry_no_media_span,
    test_empty_content_is_unknown_not_zero,
    test_off_shape_omits_span_keys_entirely,
    test_old_shape_db_still_appends_without_span_columns,
    test_new_shape_db_writes_span_columns,
)


def main():
    failed = 0
    for case in CASES:
        try:
            case()
        except Exception as exc:  # noqa: BLE001 - 自跑脚本，逐例汇报
            failed += 1
            print("  x %s: %s: %s" % (case.__name__, type(exc).__name__, exc))
        else:
            print("  ok %s" % case.__name__)
    print("%d/%d passed" % (len(CASES) - failed, len(CASES)))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
