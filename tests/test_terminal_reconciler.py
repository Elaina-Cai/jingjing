"""对账器测试（core/terminal/reconciler.py，任务模型 §7.1 ④）。

覆盖：
- dry-run 只报告不写；
- apply 幂等补写、二跑零缺口；
- 分页读取后按「旧→新」顺序追加（sequence 顺序贴近发生顺序）；
- 单条补写失败如实计数（不把失败算成成功）；
- 没有 msg_id 的消息跳过并计数。

Run directly:
    python tests/test_terminal_reconciler.py
"""

from __future__ import annotations

import os
import shutil
import sys
import uuid
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

ROOT = Path(__file__).resolve().parent.parent
_TMP_ROOT = ROOT / "data" / "_terminal_reconciler_test"
W = "wechat"
ACCOUNT = "bot_a"


def _make_temp_dir() -> Path:
    _TMP_ROOT.mkdir(parents=True, exist_ok=True)
    path = _TMP_ROOT / f"case_{uuid.uuid4().hex}"
    path.mkdir(parents=True, exist_ok=False)
    return path


def _dt(hour: int = 10, minute: int = 0) -> datetime:
    return datetime(2026, 9, 28, hour, minute, 0)


def _build_meta(tmp: Path):
    from db.meta_repo import MetaRepositoryManager
    from db.migrations import run_migrations

    db_path = tmp / "reconciler_meta.db"
    meta = MetaRepositoryManager(str(db_path))
    run_migrations(str(db_path))
    return meta, db_path


def _build_account(tmp: Path):
    from db.account_repo import AccountRepositoryManager

    db_path = tmp / "account.db"
    repo = AccountRepositoryManager(str(db_path), W, ACCOUNT)
    return repo, db_path


def _add_message(
    repo,
    msg_id: int,
    *,
    chat: str = "friend_raw",
    text: str = "在吗",
    created: datetime | None = None,
    is_self: int = 0,
    sender: str = "",
):
    from db.models import Message

    chat_type = "group" if chat.endswith("@chatroom") else "private"
    repo.messages.add(
        Message(
            msg_id=int(msg_id),
            chat_id=chat,
            chat_type=chat_type,
            sender_id=sender or chat,
            sender_name="朋友",
            content=text,
            created_at=created or _dt(),
            is_self=int(is_self),
            source=W,
            account_id=ACCOUNT,
            msg_type_name="text",
        )
    )


def _cleanup(meta, tmp: Path) -> None:
    if meta is not None:
        try:
            meta.engine.dispose()
        except Exception:  # noqa: BLE001
            pass
    shutil.rmtree(tmp, ignore_errors=True)


class _FlakyStore:
    """append 对指定 external_msg_id 抛错；查询照常代理给 inner。"""

    def __init__(self, inner, fail_msg_id: str) -> None:
        self.inner = inner
        self.fail_msg_id = fail_msg_id

    def recorded_message_keys(self, **kwargs):
        return self.inner.recorded_message_keys(**kwargs)

    def backfill_external_msg_ids(self, **kwargs):
        return self.inner.backfill_external_msg_ids(**kwargs)

    def get_by_external_msg_id(self, **kwargs):
        return self.inner.get_by_external_msg_id(**kwargs)

    def append(self, event):
        if event.external_msg_id == self.fail_msg_id:
            raise RuntimeError("simulated append failure")
        return self.inner.append(event)

    def count(self) -> int:
        return self.inner.count()


class _FakeMessagesRepo:
    """给「形状不对的消息」用的最小 repo（get_recent/to_dict）。"""

    def __init__(self, rows):
        self._rows = list(rows)

    def get_recent(self, limit=100, offset=0, chat_id=None, since=None):
        return self._rows[offset:offset + limit]

    def to_dict(self, row):
        return dict(row)


class _FakeRepo:
    def __init__(self, rows):
        self.messages = _FakeMessagesRepo(rows)


def _seed_one_event(repo, store, msg_id: int) -> None:
    from core.terminal.shadow import record_message_events

    row = repo.messages.find_by_platform_id(int(msg_id))
    assert row is not None
    record_message_events(
        [repo.messages.to_dict(row)],
        platform=W,
        account_id=ACCOUNT,
        store=store,
    )


def test_dry_run_reports_only() -> None:
    from core.terminal.event_store_db import TerminalEventStore
    from core.terminal.reconciler import reconcile_account

    tmp = _make_temp_dir()
    meta = None
    try:
        meta, _ = _build_meta(tmp)
        repo, _ = _build_account(tmp)
        store = TerminalEventStore(meta.engine)
        _add_message(repo, 1001, text="已配对")
        _add_message(repo, 1002, text="缺一")
        _add_message(repo, 1003, text="缺二")
        _seed_one_event(repo, store, 1001)

        report = reconcile_account(
            repo=repo, platform=W, account_id=ACCOUNT, store=store, apply=False
        )
        assert report.scanned == 3, report.scanned
        assert report.matched == 1, report.matched
        assert report.missing == 2, report.missing
        assert report.inserted == 0 and report.applied is False
        assert store.count() == 1
        assert report.by_chat.get("friend_raw") == 2
        assert len(report.samples) == 2
    finally:
        _cleanup(meta, tmp)


def test_apply_backfills_and_is_idempotent() -> None:
    from core.terminal.event_store_db import TerminalEventStore
    from core.terminal.reconciler import reconcile_account

    tmp = _make_temp_dir()
    meta = None
    try:
        meta, _ = _build_meta(tmp)
        repo, _ = _build_account(tmp)
        store = TerminalEventStore(meta.engine)
        _add_message(repo, 2001, text="已配对")
        _add_message(repo, 2002, text="缺一")
        _add_message(repo, 2003, text="缺二")
        _seed_one_event(repo, store, 2001)

        first = reconcile_account(
            repo=repo, platform=W, account_id=ACCOUNT, store=store, apply=True
        )
        assert first.applied is True
        assert first.missing == 2 and first.inserted == 2 and first.failed == 0
        assert store.count() == 3

        second = reconcile_account(
            repo=repo, platform=W, account_id=ACCOUNT, store=store, apply=True
        )
        assert second.missing == 0 and second.inserted == 0 and second.failed == 0
        assert store.count() == 3
    finally:
        _cleanup(meta, tmp)


def test_paging_appends_oldest_first() -> None:
    from core.terminal.event_store_db import TerminalEventStore
    from core.terminal.reconciler import reconcile_account

    tmp = _make_temp_dir()
    meta = None
    try:
        meta, _ = _build_meta(tmp)
        repo, _ = _build_account(tmp)
        store = TerminalEventStore(meta.engine)
        for index in range(5):
            _add_message(
                repo,
                3000 + index,
                text=f"m{index}",
                created=_dt(10) + timedelta(minutes=index),
            )

        report = reconcile_account(
            repo=repo,
            platform=W,
            account_id=ACCOUNT,
            store=store,
            apply=True,
            page_size=2,
        )
        assert report.missing == 5 and report.inserted == 5, report.as_dict()
        contents = [event.content for event in store.recent(10)]
        assert contents == ["m0", "m1", "m2", "m3", "m4"], contents
    finally:
        _cleanup(meta, tmp)


def test_failed_insert_is_counted_not_swallowed() -> None:
    from core.terminal.event_store_db import TerminalEventStore
    from core.terminal.reconciler import reconcile_account

    tmp = _make_temp_dir()
    meta = None
    try:
        meta, _ = _build_meta(tmp)
        repo, _ = _build_account(tmp)
        inner = TerminalEventStore(meta.engine)
        store = _FlakyStore(inner, "4002")
        _add_message(repo, 4001, text="好一")
        _add_message(repo, 4002, text="坏一")

        report = reconcile_account(
            repo=repo, platform=W, account_id=ACCOUNT, store=store, apply=True
        )
        assert report.applied is True
        assert report.inserted == 1 and report.failed == 1, report.as_dict()
        assert inner.count() == 1
        assert inner.get_by_external_msg_id(
            platform=W, bot_account_id=ACCOUNT, chat_id="friend_raw", external_msg_id="4002"
        ) is None
    finally:
        _cleanup(meta, tmp)


def test_old_code_rows_fall_back_to_metadata() -> None:
    """旧代码写的事件（实列为空、JSON 有 msg_id）：不误报缺口，apply 时补实列。"""
    from sqlalchemy import text

    from core.terminal.event_store_db import TerminalEventStore
    from core.terminal.reconciler import reconcile_account

    tmp = _make_temp_dir()
    meta = None
    try:
        meta, _ = _build_meta(tmp)
        repo, _ = _build_account(tmp)
        store = TerminalEventStore(meta.engine)
        _add_message(repo, 6001, text="旧代码写的")
        _seed_one_event(repo, store, 6001)
        # 模拟旧代码（Migration 46 之后、restart 之前）落的事件
        with meta.engine.begin() as conn:
            conn.execute(text("UPDATE terminal_events SET external_msg_id=''"))

        dry = reconcile_account(
            repo=repo, platform=W, account_id=ACCOUNT, store=store, apply=False
        )
        assert dry.missing == 0 and dry.matched == 1, dry.as_dict()

        applied = reconcile_account(
            repo=repo, platform=W, account_id=ACCOUNT, store=store, apply=True
        )
        assert applied.inserted == 0 and applied.failed == 0
        assert store.count() == 1
        repaired = store.get_by_external_msg_id(
            platform=W,
            bot_account_id=ACCOUNT,
            chat_id="friend_raw",
            external_msg_id="6001",
        )
        assert repaired is not None
    finally:
        _cleanup(meta, tmp)


def test_message_without_msg_id_is_skipped() -> None:
    from core.terminal.event_store_db import TerminalEventStore
    from core.terminal.reconciler import reconcile_account, reconcile_messages

    tmp = _make_temp_dir()
    meta = None
    try:
        meta, _ = _build_meta(tmp)
        store = TerminalEventStore(meta.engine)
        rows = [
            {"chat_id": "friend_raw", "msg_id": "5001", "content": "有 id"},
            {"chat_id": "friend_raw", "msg_id": "", "content": "没有 id"},
        ]
        report = reconcile_messages(
            rows, platform=W, account_id=ACCOUNT, store=store, apply=False
        )
        assert report.scanned == 2
        assert report.skipped_no_msg_id == 1
        assert report.missing == 1

        fake = _FakeRepo(rows + [{"chat_id": "friend_raw"}])
        report2 = reconcile_account(
            repo=fake, platform=W, account_id=ACCOUNT, store=store, apply=False
        )
        assert report2.scanned == 3
        assert report2.skipped_no_msg_id == 2
        assert report2.missing == 1
        assert store.count() == 0
    finally:
        _cleanup(meta, tmp)


def test_cli_no_matching_account_exits_2_and_json_stdout_is_pure() -> None:
    """P2-1 CLI：无命中账号 → 退出码 2；``--json`` 时 stdout 只有 JSON。

    旧行为两处不对：默认 exit 0（把「过滤条件写错」谎报成「一切正常」），
    ``--json`` 还把中文提示行混进 stdout，``json.loads`` 直接失败。
    """
    import io
    import json
    from contextlib import redirect_stderr, redirect_stdout
    from unittest.mock import patch

    import scripts.reconcile_terminal_events as cli

    def _run(argv):
        out, err = io.StringIO(), io.StringIO()
        with patch.object(sys, "argv", argv), patch.object(
            cli, "list_accounts", lambda platform, account: []
        ), patch.object(
            cli, "build_store", side_effect=AssertionError("无命中账号不该碰 meta 库")
        ):
            with redirect_stdout(out), redirect_stderr(err):
                code = cli.main()
        return code, out.getvalue(), err.getvalue()

    code, out, err = _run(["reconcile_terminal_events.py", "--account", "nope", "--json"])
    assert code == 2, code
    payload = json.loads(out)  # 混进中文提示行 / 多余文本都会在这里红
    assert payload["accounts"] == [] and payload["sequences"] is None
    assert "没有找到" in err  # 提示改走 stderr，不污染 stdout

    code, out, err = _run(["reconcile_terminal_events.py", "--account", "nope"])
    assert code == 2, code
    assert "没有找到" in out  # 非 --json：人读输出照旧在 stdout


def test_cli_json_stdout_stays_parseable_with_accounts() -> None:
    """P2-1 CLI：正常路径同样只在 stdout 出 JSON（人读报告一律不上 stdout）。"""
    import io
    import json
    from contextlib import redirect_stderr, redirect_stdout
    from types import SimpleNamespace
    from unittest.mock import patch

    import scripts.reconcile_terminal_events as cli
    from core.terminal.reconciler import ReconcileReport, SequenceReport

    fake_repo = SimpleNamespace(engine=SimpleNamespace(dispose=lambda: None))
    report = ReconcileReport(platform=W, account_id=ACCOUNT, scanned=2, matched=2)

    out, err = io.StringIO(), io.StringIO()
    with patch.object(
        sys, "argv", ["reconcile_terminal_events.py", "--json", "--no-identity"]
    ), patch.object(
        cli, "list_accounts",
        lambda platform, account: [(W, ACCOUNT, Path("account.db"))],
    ), patch.object(
        cli, "build_store", lambda: (None, object())
    ), patch(
        "db.account_repo.AccountRepositoryManager", lambda *a, **k: fake_repo
    ), patch(
        "core.terminal.reconciler.reconcile_sequences",
        lambda store, sample_limit=10: SequenceReport(rows=3, max_sequence=7),
    ), patch(
        "core.terminal.reconciler.reconcile_account", lambda **kwargs: report
    ):
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main()

    assert code == 0, (code, err.getvalue())
    payload = json.loads(out.getvalue())
    assert payload["accounts"][0]["account_id"] == ACCOUNT
    assert payload["sequences"]["rows"] == 3
    assert out.getvalue().lstrip().startswith("{")


def main() -> int:
    tests = [
        test_dry_run_reports_only,
        test_apply_backfills_and_is_idempotent,
        test_paging_appends_oldest_first,
        test_failed_insert_is_counted_not_swallowed,
        test_old_code_rows_fall_back_to_metadata,
        test_message_without_msg_id_is_skipped,
        test_cli_no_matching_account_exits_2_and_json_stdout_is_pure,
        test_cli_json_stdout_stays_parseable_with_accounts,
    ]
    failed = 0
    for test in tests:
        try:
            test()
            print(f"PASS {test.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            import traceback

            print(f"FAIL {test.__name__}: {exc!r}")
            traceback.print_exc()
    print(f"{len(tests) - failed}/{len(tests)} tests passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
