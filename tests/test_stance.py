"""P2 姿态决策 + 立场卡测试。

Run directly:
    python tests/test_stance.py
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.context.assembler import _classify  # noqa: E402
from core.context.stance import (  # noqa: E402
    deactivate_card,
    list_cards,
    load_cards,
    render_cards_line,
    rollback_card,
    upsert_card,
)
from core.terminal.persona_brain import build_persona_blocks  # noqa: E402

BASE = datetime(2026, 9, 23, 20, 32, 0)


class _env:
    def __init__(self, **values: str | None):
        self._values = values

    def __enter__(self):
        self._saved = {key: os.environ.get(key) for key in self._values}
        for key, value in self._values.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def __exit__(self, *exc):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _m(speaker: str, content: str, *, seconds: int = 0, self_: bool = False,
       mention: bool = False) -> dict:
    return {
        "sender_name": speaker,
        "content": content,
        "created_at": BASE + timedelta(seconds=seconds),
        "is_self": self_,
        "mention_self": mention,
    }


# ------------------------------------------------------------ 纯规则：映射
def test_render_cards_line() -> None:
    assert render_cards_line([]) == ""
    line = render_cards_line(["不替人催债背锅"])
    assert line == "- 你的立场：不替人催债背锅", line
    print("  OK  立场退化行渲染")


# ------------------------------------------------------ 协议块（生成约束）
def _snapshot() -> SimpleNamespace:
    return SimpleNamespace(
        persona=SimpleNamespace(name="鲸鲸", identity=None, traits=None),
        turn=None,
        policy=None,
    )


def test_persona_block_group_only() -> None:
    """WP-6F：私聊（主人/非主人）和群聊都走 [遇事怎么反应]，stance 已退役。"""
    with _env(YINYUE_FEATURE_REACT_PLAYBOOK="1"):
        group_text = "\n".join(build_persona_blocks(_snapshot(), is_group=True))
        private_text = "\n".join(build_persona_blocks(_snapshot(), is_group=False))
        assert "[遇事怎么反应]" not in group_text  # 2026-10-23 退役（四层化）
        assert "[遇事怎么反应]" not in private_text
        assert "[姿态与分寸]" not in group_text
        assert "[姿态与分寸]" not in private_text
    print("  OK  persona：私聊/群聊都走幕块，stance 块已退役")



# ------------------------------------------------------ 立场卡 IO（隔离库）
_DDL = """
CREATE TABLE stance_cards (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scope TEXT NOT NULL DEFAULT 'group',
    platform TEXT NOT NULL DEFAULT '',
    account_id TEXT NOT NULL DEFAULT '',
    chat_id TEXT NOT NULL DEFAULT '',
    person_key TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'owner',
    version INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT ''
)
"""


def _repo():
    from sqlalchemy import create_engine, text as _sql
    from sqlalchemy.pool import StaticPool

    engine = create_engine("sqlite://", poolclass=StaticPool, future=True)
    with engine.begin() as conn:
        conn.execute(_sql(_DDL))
    return SimpleNamespace(engine=engine)


def test_card_versioning_and_rollback() -> None:
    repo = _repo()
    first = upsert_card(content="A 版", chat_id="g", meta_repo=repo)
    assert first["ok"] and first["version"] == 1 and first["changed"]
    again = upsert_card(content="A 版", chat_id="g", meta_repo=repo)
    assert again["changed"] is False and again["version"] == 1
    second = upsert_card(content="B 版", chat_id="g", meta_repo=repo)
    assert second["version"] == 2 and second["changed"]
    active = list_cards("g", meta_repo=repo)
    assert len(active) == 1 and active[0]["content"] == "B 版", active
    restored = rollback_card(int(first["id"]), meta_repo=repo)
    assert restored["ok"] and restored["version"] == 3, restored
    rows = list_cards("g", active_only=False, meta_repo=repo)
    assert [r["version"] for r in rows] == [1, 2, 3], rows
    assert [r["status"] for r in rows] == ["superseded", "superseded", "active"]
    assert load_cards("g", meta_repo=repo) == ["A 版"]
    print("  OK  版本化写入 + 回滚（append-only）")


def test_card_deactivate() -> None:
    repo = _repo()
    card = upsert_card(content="临时", chat_id="g", meta_repo=repo)
    assert deactivate_card(int(card["id"]), meta_repo=repo)["ok"]
    assert load_cards("g", meta_repo=repo) == []
    assert list_cards("g", meta_repo=repo) == []
    assert not deactivate_card(int(card["id"]), meta_repo=repo)["ok"]
    print("  OK  下架（可恢复，不删行）")


def test_card_scope_and_order() -> None:
    repo = _repo()
    upsert_card(content="全局卡", meta_repo=repo)
    upsert_card(content="群卡", chat_id="g", meta_repo=repo)
    upsert_card(content="人卡", scope="person", person_key="wxid_a", chat_id="g", meta_repo=repo)
    assert load_cards("g", meta_repo=repo) == ["群卡", "全局卡"]
    assert load_cards("g", person_keys=["wxid_a"], limit=5, meta_repo=repo) == [
        "群卡",
        "人卡",
        "全局卡",
    ]
    assert load_cards("other", meta_repo=repo) == ["全局卡"]
    print("  OK  群卡优先、全局兜底、分人卡按需")


def main() -> int:
    tests = [
        test_render_cards_line,
        test_persona_block_group_only,
                test_card_versioning_and_rollback,
        test_card_deactivate,
        test_card_scope_and_order,
    ]
    for test in tests:
        test()
    print(f"ALL PASSED ({len(tests)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
