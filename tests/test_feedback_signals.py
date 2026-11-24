"""Layer-2 quality feedback: detection rules + collection on a temp DB.

Run directly:
    python tests/test_feedback_signals.py
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _tempdirs import make_db_dir  # noqa: E402

from core import quality  # noqa: E402
from db.meta_repo import MetaRepositoryManager  # noqa: E402
from db.migrations import run_migrations  # noqa: E402


class _FakeMessages:
    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows

    def get_by_chat(self, chat_id: str, limit: int = 12):
        return [row for row in self._rows if row.get("chat_id") == chat_id][-limit:]


class _FakeRepo:
    def __init__(self, rows: list[dict]) -> None:
        self.messages = _FakeMessages(rows)


def _row(msg_id, content, *, is_self=0, chat_id="wxid_a", chat_type="private"):
    return {
        "msg_id": msg_id,
        "chat_id": chat_id,
        "chat_type": chat_type,
        "content": content,
        "is_self": is_self,
    }


def test_correction_detection() -> None:
    assert quality.classify_correction("不对，我说的是另一个项目") is True
    assert quality.classify_correction("鲸鲸你记错了，我没说过这个") is True
    assert quality.classify_correction("这样不对吗？") is False  # question, not correction
    assert quality.classify_correction("好的，谢谢你") is False
    print("  ok   correction detector (with question guard)")


def test_repeat_detection() -> None:
    assert quality.is_repeat("明天天气怎么样", "明天天气怎么样") is True
    assert quality.is_repeat("帮我查一下本周的日程安排", "帮我查一下本周的日程安排呀") is True
    assert quality.is_repeat("今天心情不错", "明天要下雨") is False
    assert quality.is_repeat("在吗", "在吗") is False  # too short to count as retry
    assert quality.is_repeat("随便聊聊", "你再说一遍") is True  # explicit marker
    print("  ok   repeat detector (similarity + explicit marker)")


def test_collection_end_to_end() -> None:
    tmp = make_db_dir("feedback_")
    meta = None
    try:
        db_path = tmp / "feedback_meta.db"
        run_migrations(str(db_path))
        meta = MetaRepositoryManager(str(db_path))

        # keep the test hermetic: no live terminal-event lookup
        from core.quality import feedback as feedback_mod

        original = feedback_mod._find_reply_event_id
        feedback_mod._find_reply_event_id = lambda **kwargs: ""
        try:
            repo = _FakeRepo([
                _row("m1", "帮我记一下周五开会"),
                _row("m2", "好的，我记住了", is_self=1),
            ])
            batch = [_row("m3", "不对，不是周五，是周六")]
            recorded = quality.record_feedback_signals(
                batch, repo=repo, platform="wechat", account_id="bot_a", meta_repo=meta
            )
            assert recorded == 1, recorded
            rows = list(meta.feedback_samples.list_recent(limit=10))
            assert len(rows) == 1, rows
            assert rows[0].source == "correction"
            assert "周五开会" in rows[0].previous_user_text
            assert "记住了" in rows[0].reply_preview

            # same signal moments later must not be collected twice
            again = quality.record_feedback_signals(
                batch, repo=repo, platform="wechat", account_id="bot_a", meta_repo=meta
            )
            assert again == 0 and meta.feedback_samples.count() == 1

            # a repeated question is collected as source=repeat
            repo2 = _FakeRepo([
                _row("m4", "明天天气怎么样", chat_id="wxid_b"),
                _row("m5", "明天晴，26 度", is_self=1, chat_id="wxid_b"),
            ])
            batch2 = [_row("m6", "明天天气怎么样", chat_id="wxid_b")]
            recorded2 = quality.record_feedback_signals(
                batch2, repo=repo2, platform="wechat", account_id="bot_a", meta_repo=meta
            )
            assert recorded2 == 1, recorded2
            sources = {row.source for row in meta.feedback_samples.list_recent(limit=10)}
            assert sources == {"correction", "repeat"}, sources
        finally:
            feedback_mod._find_reply_event_id = original
    finally:
        if meta is not None:
            try:
                meta.engine.dispose()
            except Exception:
                pass
        shutil.rmtree(tmp, ignore_errors=True)
    print("  ok   end-to-end collection (correction + repeat + dedupe)")


def test_self_messages_ignored() -> None:
    tmp = make_db_dir("feedback_")
    meta = None
    try:
        db_path = tmp / "feedback_meta.db"
        run_migrations(str(db_path))
        meta = MetaRepositoryManager(str(db_path))
        from core.quality import feedback as feedback_mod

        original = feedback_mod._find_reply_event_id
        feedback_mod._find_reply_event_id = lambda **kwargs: ""
        try:
            batch = [_row("m9", "这是一条自己发的消息", is_self=1)]
            recorded = quality.record_feedback_signals(
                batch, repo=_FakeRepo([]), meta_repo=meta
            )
            assert recorded == 0 and meta.feedback_samples.count() == 0
        finally:
            feedback_mod._find_reply_event_id = original
    finally:
        if meta is not None:
            try:
                meta.engine.dispose()
            except Exception:
                pass
        shutil.rmtree(tmp, ignore_errors=True)
    print("  ok   assistant's own messages are ignored")


def main() -> bool:
    tests = [
        test_correction_detection,
        test_repeat_detection,
        test_collection_end_to_end,
        test_self_messages_ignored,
    ]
    failed: list[str] = []
    for test in tests:
        try:
            test()
        except Exception as exc:  # noqa: BLE001
            failed.append(test.__name__)
            print(f"  FAIL {test.__name__}: {exc!r}")
    print(f"feedback signal checks: {len(tests) - len(failed)}/{len(tests)} passed")
    return not failed


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
