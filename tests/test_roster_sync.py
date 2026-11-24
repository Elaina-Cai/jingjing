"""群名单自动同步：写成 person_notes **逐人卡**（2026-09-24 改；此前写群级备注）。"""

from __future__ import annotations

import os
import shutil
import sys
import uuid
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

ROOT = Path(__file__).resolve().parent.parent
_TMP = ROOT / "data" / "_roster_sync_test"

from core.quality.feedback import detect_signals  # noqa: E402
from core.quality.roster_sync import parse_roster, sync_roster  # noqa: E402
from db.meta_repo import MetaRepositoryManager  # noqa: E402
from db.migrations import run_migrations  # noqa: E402

ROSTER = (
    "[示例作业群·成员清单]（2026-09-22 主人确认唯一准版）：\n"
    "- P5，计算机25-4班，前学委（计算机25-4班，期末卸任），女\n"
    "- Elaina（主人），计算机25-4班，普通学生，男\n"
    "- P3，计算机25-1班，学委（计算机25-1班，现任），男\n"
    "- P6，物联网25-2班，女"
)

ROSTER_V2 = ROSTER.replace("P6，物联网25-2班，女", "P6，物联网25-1班，女")


def _tmp_db() -> Path:
    _TMP.mkdir(parents=True, exist_ok=True)
    path = _TMP / f"case_{uuid.uuid4().hex}"
    path.mkdir(parents=True, exist_ok=False)
    return path / "meta.db"


def test_parse() -> None:
    members = parse_roster(ROSTER)
    assert [m["nick"] for m in members] == ["P5", "Elaina", "P3", "P6"]
    assert members[0]["class"] == "计算机25-4班" and members[0]["role"] == "前学委"
    assert members[2]["role"] == "学委（现任）"
    assert "女" in members[0]["full"]  # 性别保留在 full（身份说明原文）
    print("  ok   parse_roster")


def _seed_members(meta, chat_id: str, nicks: list[str]) -> None:
    for nick in nicks:
        person = meta.people.resolve_identity(
            platform="wechat",
            account_id="bot_a",
            identity_type="wxid",
            identity_value=f"wxid_{nick}",
            display_name=nick,
        )
        meta.people.upsert_membership(
            person.person_id,
            platform="wechat",
            account_id="bot_a",
            chat_id=chat_id,
            group_nickname=nick,
        )


def test_sync_writes_per_person_cards_not_group_note() -> None:
    """2026-09-24：名册落点 = 逐人卡；群级备注（person_key='')不得再被名册污染。"""
    from sqlalchemy import text as _sql

    CHAT = "g1@chatroom"
    db = _tmp_db()
    meta = None
    try:
        run_migrations(str(db))
        meta = MetaRepositoryManager(str(db))
        _seed_members(meta, CHAT, ["P5", "Elaina", "P3", "P6"])

        first = sync_roster(
            meta, roster_text=ROSTER, chat_id=CHAT,
            platform="wechat", bot_account_id="bot_a", actor_person_id="p_owner",
        )
        assert first.get("person_notes_written") == 4, first
        assert first.get("unresolved_members") == [], first
        with meta.engine.connect() as conn:
            cards = conn.execute(
                _sql(
                    "select person_key, content from person_notes "
                    "where chat_id=:c and person_key != '' order by person_key"
                ),
                {"c": CHAT},
            ).fetchall()
            group_note = conn.execute(
                _sql("select count(*) from person_notes where chat_id=:c and person_key=''"),
                {"c": CHAT},
            ).fetchone()
        assert len(cards) == 4, cards
        assert any("女" in str(row[1]) and "计算机25-4班" in str(row[1]) for row in cards), cards
        assert group_note[0] == 0, "群级备注不该再写名册"

        # 更新（改P6班级）→ 逐人卡仍 4 条，内容更新（upsert 幂等）
        second = sync_roster(
            meta, roster_text=ROSTER_V2, chat_id=CHAT,
            platform="wechat", bot_account_id="bot_a", actor_person_id="p_owner",
        )
        assert second.get("person_notes_written") == 4, second
        with meta.engine.connect() as conn:
            cards = conn.execute(
                _sql(
                    "select content from person_notes "
                    "where chat_id=:c and person_key='wxid_P6'"
                ),
                {"c": CHAT},
            ).fetchall()
        assert len(cards) == 1 and "物联网25-1班" in str(cards[0][0]), cards
        print("  ok   sync writes per-person cards (idempotent), group note untouched")
    finally:
        if meta is not None:
            meta.engine.dispose()
        shutil.rmtree(db.parent, ignore_errors=True)


def test_roster_confirm_signal() -> None:
    signals = detect_signals("x", "现在对了，这就是最准确的版本", quote_content=ROSTER)
    assert signals and signals[0] == "roster_confirm", signals
    assert not detect_signals("x", "今天天气怎么样", quote_content=ROSTER)
    print("  ok   roster_confirm signal fires only on owner confirmation")


def main() -> int:
    tests = [
        test_parse,
        test_sync_writes_per_person_cards_not_group_note,
        test_roster_confirm_signal,
    ]
    failed = 0
    for test in tests:
        try:
            test()
        except Exception as exc:  # noqa: BLE001
            failed += 1
            import traceback

            print(f"FAIL {test.__name__}: {exc!r}")
            traceback.print_exc()
    print(f"roster sync checks: {len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
