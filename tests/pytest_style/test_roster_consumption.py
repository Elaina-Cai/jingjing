"""名单确认反馈的消费契约：源版本 CAS、效果/收据同事务、最老优先。"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from core.memory.feedback_commit import claim_feedback, sample_version
from core.quality import corrections as corr
from core.quality import roster_sync
from db.meta_repo import MetaRepositoryManager
from db.migrations import run_migrations

CHAT = "g1@chatroom"

ROSTER = (
    "[示例作业群·成员清单]（2026-09-22 主人确认唯一准版）：\n"
    "- P5，计算机25-4班，前学委（计算机25-4班，期末卸任），女\n"
    "- Elaina（主人），计算机25-4班，普通学生，男\n"
    "- P3，计算机25-1班，学委（计算机25-1班，现任），男\n"
    "- P6，物联网25-2班，女"
)


def roster_with(klass: str) -> str:
    return ROSTER.replace("P6，物联网25-2班，女", f"P6，{klass}，女")


@pytest.fixture()
def case(tmp_path, monkeypatch):
    db_path = tmp_path / "meta.db"
    run_migrations(str(db_path))
    meta = MetaRepositoryManager(str(db_path))
    for nick in ["P5", "Elaina", "P3", "P6"]:
        person = meta.people.resolve_identity(
            platform="wechat", account_id="bot_a", identity_type="wxid",
            identity_value=f"wxid_{nick}", display_name=nick,
        )
        meta.people.upsert_membership(
            person.person_id, platform="wechat", account_id="bot_a",
            chat_id=CHAT, group_nickname=nick,
        )
    sample = meta.feedback_samples.add(
        source="roster_confirm", platform="wechat", bot_account_id="bot_a",
        chat_id="owner_private", chat_type="private", person_id="p_owner",
        sample_text="现在对了，这是唯一准版",
        metadata={"quote_content": roster_with("物联网25-1班")},
    )
    monkeypatch.setattr(corr, "_owner_ids", lambda platform: {"p_owner"})
    monkeypatch.setattr(corr, "_rate_ok", lambda key: True)
    try:
        yield SimpleNamespace(meta=meta, sample=sample)
    finally:
        meta.engine.dispose()


def run(case, limit=3):
    return asyncio.run(
        roster_sync.process_pending_roster_confirms(limit=limit, meta_repo=case.meta)
    )


def add_sample(case, quote, *, created_at=None):
    return case.meta.feedback_samples.add(
        source="roster_confirm", platform="wechat", bot_account_id="bot_a",
        chat_id="owner_private", chat_type="private", person_id="p_owner",
        sample_text="这版对了", metadata={"quote_content": quote},
        created_at=created_at,
    )


def sample_status(case, sample=None):
    target = sample or case.sample
    with case.meta.engine.connect() as conn:
        return conn.execute(
            text("select status from feedback_samples where id=:i"), {"i": target.id}
        ).scalar()


def note(case, nick="P6"):
    with case.meta.engine.connect() as conn:
        return conn.execute(
            text("select content from person_notes where chat_id=:c and person_key=:p"),
            {"c": CHAT, "p": f"wxid_{nick}"},
        ).scalar()


def receipt(case):
    claim = claim_feedback(
        case.sample, case.meta.feedback_samples,
        source="roster_confirm", namespace="roster-confirm",
    )
    with case.meta.engine.connect() as conn:
        return conn.execute(
            text("select event_type, metadata_json from terminal_events where event_id=:e"),
            {"e": claim.event_id},
        ).first()


def test_confirmation_writes_notes_and_system_receipt_together(case):
    result = run(case)
    assert result["synced"] == 1
    assert sample_status(case) == "learned"
    assert "物联网25-1班" in note(case)
    row = receipt(case)
    assert row is not None and row[0] == "system"
    metadata = json.loads(row[1])
    assert metadata["type"] == "feedback_consumption"
    assert metadata["source"] == "roster_confirm"
    assert metadata["feedback"]["version"] == sample_version(case.sample)
    assert run(case)["scanned"] == 0


def test_partial_write_crash_rolls_back_notes_status_and_receipt(case, monkeypatch):
    from core.context.person_notes import upsert_note

    def crash(meta_repo, *, roster_text, conn=None, **kwargs):
        upsert_note(
            person_key="wxid_P6", content="半次写入", chat_id=CHAT,
            platform="wechat", account_id="bot_a", meta_repo=meta_repo, conn=conn,
        )
        raise OSError("crash after partial write")

    with monkeypatch.context() as patch:
        patch.setattr(roster_sync, "sync_roster", crash)
        with pytest.raises(OSError):
            run(case)
    assert sample_status(case) == "new"
    assert note(case) is None
    assert receipt(case) is None
    assert run(case)["synced"] == 1
    assert "物联网25-1班" in note(case)


def test_source_change_after_consumption_is_a_conflict_not_a_replay(case):
    assert run(case)["synced"] == 1
    with case.meta.engine.begin() as conn:
        conn.execute(
            text("update feedback_samples set metadata_json=:m, status='new' where id=:i"),
            {
                "m": json.dumps({"quote_content": roster_with("物联网25-3班")}, ensure_ascii=False),
                "i": case.sample.id,
            },
        )
    result = run(case)
    assert result["conflicts"] == 1 and result["synced"] == 0
    assert "物联网25-1班" in note(case)
    assert sample_status(case) == "new"
    assert receipt(case) is not None


def test_budget_processes_oldest_confirmation_first_across_runs(case):
    v2 = add_sample(case, roster_with("物联网25-2班"), created_at=case.sample.created_at + timedelta(minutes=1))
    v3 = add_sample(case, roster_with("物联网25-3班"), created_at=case.sample.created_at + timedelta(minutes=2))
    assert run(case, limit=2)["synced"] == 2
    assert sample_status(case) == "learned" and sample_status(case, v2) == "learned"
    assert sample_status(case, v3) == "new"
    assert "物联网25-2班" in note(case)
    assert run(case, limit=2)["synced"] == 1
    assert "物联网25-3班" in note(case)


def test_non_owner_confirmation_is_cas_skipped_without_notes(case):
    with case.meta.engine.begin() as conn:
        conn.execute(
            text("update feedback_samples set person_id='ordinary' where id=:i"),
            {"i": case.sample.id},
        )
    result = run(case)
    assert result["skipped"] == 1
    assert sample_status(case) == "skipped_non_owner"
    assert note(case) is None


def test_non_roster_text_is_reviewed_not_synced(case):
    with case.meta.engine.begin() as conn:
        conn.execute(
            text("update feedback_samples set metadata_json='{}' where id=:i"),
            {"i": case.sample.id},
        )
    result = run(case)
    assert result["reviewed"] == 1 and result["synced"] == 0
    assert sample_status(case) == "reviewed"
    assert note(case) is None
