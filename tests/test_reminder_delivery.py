"""Reminder delivery: address capture -> due scan -> send / retry / stop.

The bug these lock down (2026-09-28): create_reminder could store a reminder
and *promise* "到点提醒你", but the row had no target conversation and the
scheduler had no job — so the promise was unfulfillable.  The fix is a
three-part contract:

1. the tool freezes the source conversation into the row (and refuses when
   there is no address, instead of promising);
2. the gateway must not classify the reminder send as proactive outreach,
   otherwise the whitelist/cooldown gates would silently swallow it;
3. a per-minute scheduler job delivers, retries a bounded number of times,
   then fails loudly (desktop notification) instead of retrying forever.

Standalone script (repo convention): ``python tests/test_reminder_delivery.py``.
No real LLM/WeChat is touched; the turn runner is faked at the module seam.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import sys
import traceback
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

_TEST_LOG_DIR = str(Path("data") / "_test_logs_reminder_delivery")
Path(_TEST_LOG_DIR).mkdir(parents=True, exist_ok=True)
os.environ["LOG_DIR"] = _TEST_LOG_DIR

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


ROOT = Path(__file__).resolve().parent.parent
# 隔离前导（照 tests/test_reminder_mapping.py:34-41）：本文件以纯函数 + 替身为主，
# 但纪律一致——万一以后走 bootstrap，也绝不碰生产库。
_ISO_ROOT = (ROOT / "data" / "_reminder_delivery_test" / "_iso").resolve()
_ISO_ROOT.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("META_DB_PATH", str(_ISO_ROOT / "meta.db"))
os.environ.setdefault("PLATFORMS_DIR", str(_ISO_ROOT / "platforms"))
os.environ.setdefault("DATA_DIR", str(_ISO_ROOT / "data"))

import core.infra.logging as _logging

_logging.init_logging = lambda *args, **kwargs: None

# scheduler.main is imported below; patch logging before the import.
import scheduler.main as sm
import agents.reminder_trigger as rt
from agents.proactive_trigger import apply_reply_prefix
from agents.tools.reminder import CreateReminderTool
from core.speech.outbound import OutboundGateway


@contextmanager
def patch(obj, name, value):
    original = getattr(obj, name)
    setattr(obj, name, value)
    try:
        yield
    finally:
        setattr(obj, name, original)


def _future_at(hours: int = 1) -> str:
    return (datetime.now() + timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M")


class _FakePersonas:
    def list_active(self):
        return [SimpleNamespace(id=7)]


class _FakeReminders:
    def __init__(self):
        self.added = []

    def count_pending_for_requester(self, requester_id, status="pending"):
        return 0

    def add(self, reminder):
        reminder.id = len(self.added) + 1
        self.added.append(reminder)
        return reminder


class _FakeRepo:
    def __init__(self):
        self.personas = _FakePersonas()
        self.reminders = _FakeReminders()


class _ScanRepo:
    """Minimal repo surface used by run_reminder_delivery."""

    def __init__(self, due):
        self._due = list(due)
        self.attempts: dict[int, int] = {}
        self.done: list[int] = []
        self.failed: list[int] = []
        self.reminders = SimpleNamespace(
            list_pending=self.list_pending,
            record_attempt=self.record_attempt,
            mark_done=self.mark_done,
            mark_failed=self.mark_failed,
        )

    def list_pending(self):
        return list(self._due)

    def record_attempt(self, reminder_id: int) -> int:
        self.attempts[reminder_id] = self.attempts.get(reminder_id, 0) + 1
        return self.attempts[reminder_id]

    def mark_done(self, reminder_id: int):
        self.done.append(reminder_id)

    def mark_failed(self, reminder_id: int):
        self.failed.append(reminder_id)


def _due(**overrides):
    row = {
        "id": 11,
        "content": "该开会了",
        "source_platform": "wechat",
        "source_account_id": "wxid_bot",
        "source_chat_id": "wxid_friend",
        "source_chat_type": "private",
        "requester_id": "wxid_friend",
        "requester_name": "老王",
        "requester_is_owner": 0,
        "mention_name": None,
        "mention_names": None,
        "purpose": "remind",
    }
    row.update(overrides)
    return SimpleNamespace(**row)


def _fresh_db_file(name: str) -> Path:
    d = Path("data") / "_reminder_delivery_test"
    d.mkdir(parents=True, exist_ok=True)
    db_path = d / f"{name}.db"
    if db_path.exists():
        db_path.unlink()
    return db_path

# --------------------------------------------------------------- tool contract
def test_create_reminder_captures_source_address():
    repo = _FakeRepo()
    tool = CreateReminderTool(repo)
    tool.set_context(SimpleNamespace(
        extra={
            "platform": "wechat",
            "account_id": "wxid_bot",
            "chat_id": "wxid_friend",
            "is_group": False,
        },
        account_context=None,
        accounts={"wechat": "wxid_bot"},
        owner_accounts={},
    ))
    tool.authoritative_is_owner = lambda: True

    result = asyncio.run(tool.execute(content="开会", remind_at=_future_at()))

    assert not result.is_error, result.error
    saved = repo.reminders.added[0]
    assert saved.source_platform == "wechat"
    assert saved.source_account_id == "wxid_bot"
    assert saved.source_chat_id == "wxid_friend"
    assert saved.source_chat_type == "private"
    assert saved.attempts == 0


def test_create_reminder_resolves_choice_word_to_account_id():
    repo = _FakeRepo()
    tool = CreateReminderTool(repo)
    tool.set_context(SimpleNamespace(
        extra={
            "platform": "wechat",
            # 老路径会把"用哪个账号回"的选择词写进 extra，它不是地址。
            "account_id": "self",
            "chat_id": "wxid_friend",
            "is_group": True,
        },
        account_context=None,
        accounts={"wechat": "wxid_bot"},
        owner_accounts={},
    ))
    tool.authoritative_is_owner = lambda: True

    result = asyncio.run(tool.execute(
        content="开会", remind_at=_future_at(), mention=["小美"],
    ))

    assert not result.is_error, result.error
    saved = repo.reminders.added[0]
    assert saved.source_account_id == "wxid_bot"
    assert saved.source_chat_type == "group"


def test_create_reminder_without_chat_refuses_and_stores_nothing():
    repo = _FakeRepo()
    tool = CreateReminderTool(repo)
    tool.set_context(SimpleNamespace(
        extra={"platform": "wechat", "account_id": "wxid_bot"},
        account_context=None,
        accounts={},
        owner_accounts={},
    ))
    tool.authoritative_is_owner = lambda: True

    result = asyncio.run(tool.execute(content="开会", remind_at=_future_at()))

    assert result.is_error
    assert repo.reminders.added == []


# ------------------------------------------------------------ delivery runner
def test_reminder_turn_uses_non_proactive_trigger():
    captured = {}

    async def fake_run_proactive_turn(**kwargs):
        captured.update(kwargs)
        return rt.ProactiveTurnResult(True, sent=True)

    with patch(rt, "run_proactive_turn", fake_run_proactive_turn):
        result = asyncio.run(rt.run_reminder_turn(
            platform="wechat", bot_account_id="wxid_bot",
            chat_id="wxid_friend", content="该开会了",
        ))

    assert result.sent
    assert captured["trigger_kind"] == "reminder"
    assert captured["enforce_proactive_policy"] is False
    assert captured["fallback_text"]
    assert "该开会了" in captured["hint"]


def test_gateway_treats_reminder_send_as_normal_not_proactive():
    draft = SimpleNamespace(extra={"proactive": False, "trigger": "reminder"})
    snapshot = SimpleNamespace(
        turn=SimpleNamespace(trigger_reason="reminder"),
        audience=SimpleNamespace(value="web_chat"),
    )
    context = SimpleNamespace(trigger="reminder", extra={})

    assert OutboundGateway._is_proactive_send(draft, snapshot, context) is False

    proactive_draft = SimpleNamespace(extra={"trigger": "proactive"})
    proactive_snapshot = SimpleNamespace(
        turn=SimpleNamespace(trigger_reason="proactive"),
        audience=SimpleNamespace(value="web_chat"),
    )
    proactive_context = SimpleNamespace(trigger="proactive", extra={})
    assert OutboundGateway._is_proactive_send(
        proactive_draft, proactive_snapshot, proactive_context
    ) is True




# ------------------------------------------------------------ scheduler job
def test_reminder_delivery_sends_and_marks_done():
    repo = _ScanRepo(due=[_due()])
    sent = {}

    async def fake_turn(**kwargs):
        sent.update(kwargs)
        return rt.ProactiveTurnResult(True, sent=True, reason="")

    with patch(sm, "get_repo", lambda: repo), \
            patch(rt, "run_reminder_turn", fake_turn):
        asyncio.run(sm.run_reminder_delivery())

    assert repo.done == [11]
    assert repo.failed == []
    assert sent["platform"] == "wechat"
    assert sent["bot_account_id"] == "wxid_bot"
    assert sent["chat_id"] == "wxid_friend"
    assert sent["chat_type"] == "private"
    assert sent["content"] == "该开会了"


def test_reminder_delivery_retries_then_stops():
    repo = _ScanRepo(due=[_due()])

    async def failing_turn(**kwargs):
        return rt.ProactiveTurnResult(False, sent=False, reason="send_failed")

    notifications: list[tuple] = []

    with patch(sm, "get_repo", lambda: repo), \
            patch(rt, "run_reminder_turn", failing_turn), \
            patch(sm, "send_notification",
                  lambda *a, **k: notifications.append(a)):
        asyncio.run(sm.run_reminder_delivery())
        assert repo.attempts[11] == 1
        assert repo.failed == []          # 第一次失败先重试，不放弃
        asyncio.run(sm.run_reminder_delivery())
        assert repo.attempts[11] == 2
        assert repo.failed == []
        asyncio.run(sm.run_reminder_delivery())
        assert repo.attempts[11] == 3

    assert repo.failed == [11]            # 到止损线停手并告警
    assert len(notifications) == 1
    assert repo.done == []


def test_reminder_delivery_legacy_row_without_source_fails_loudly():
    repo = _ScanRepo(due=[_due(source_chat_id=None)])

    async def must_not_run(**kwargs):  # pragma: no cover - regression guard
        raise AssertionError("delivery must not run without a target chat")

    notifications: list[tuple] = []

    with patch(sm, "get_repo", lambda: repo), \
            patch(rt, "run_reminder_turn", must_not_run), \
            patch(sm, "send_notification",
                  lambda *a, **k: notifications.append(a)):
        asyncio.run(sm.run_reminder_delivery())

    assert repo.failed == [11]
    assert repo.attempts == {}
    assert len(notifications) == 1


# ------------------------------------------------------------------ migration
def test_migration_43_adds_columns_to_legacy_reminders():
    from db.migrations import run_migrations

    db_path = _fresh_db_file("legacy_migration")
    con = sqlite3.connect(str(db_path))
    con.execute(
        "CREATE TABLE _schema_version (version INTEGER PRIMARY KEY, "
        "description TEXT, applied_at DATETIME DEFAULT CURRENT_TIMESTAMP)"
    )
    con.execute(
        "INSERT INTO _schema_version (version, description) VALUES (42, 'test')"
    )
    con.execute(
        """
        CREATE TABLE reminders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            persona_id INTEGER, content TEXT, remind_at DATETIME,
            status TEXT DEFAULT 'pending', repeat_rule TEXT, created_at DATETIME
        )
        """
    )
    con.commit()
    con.close()

    run_migrations(str(db_path))

    con = sqlite3.connect(str(db_path))
    cols = {r[1] for r in con.execute("PRAGMA table_info(reminders)")}
    version = con.execute("SELECT MAX(version) FROM _schema_version").fetchone()[0]
    con.close()
    assert {
        "source_platform", "source_account_id", "source_chat_id",
        "source_chat_type", "attempts",
        "requester_id", "requester_name", "requester_is_owner",
        "mention_name", "mention_names", "purpose",
    } <= cols
    assert version >= 45  # 不锁死最新迁移版本号，避免新增迁移时误报


def test_repo_attempt_counter_and_repeat_reset():
    from db.models import Reminder, init_db
    from db.repo_reminder import ReminderRepository

    db_path = _fresh_db_file("repo_repeat")
    repo = ReminderRepository(init_db(str(db_path)))
    now = datetime.now()
    repo.add(Reminder(
        persona_id=1, content="开会", remind_at=now - timedelta(minutes=1),
        status="pending", repeat_rule="daily", attempts=0, created_at=now,
        source_platform="wechat", source_account_id="wxid_bot",
        source_chat_id="wxid_friend", source_chat_type="private",
    ))

    pending = repo.list_pending()
    assert len(pending) == 1
    rid = pending[0].id
    assert repo.record_attempt(rid) == 1
    assert repo.record_attempt(rid) == 2

    repo.mark_done(rid)  # 重复提醒：顺延到下一次，尝试次数清零
    row = repo.list_all(limit=1)[0]
    assert row.status == "pending"
    assert row.attempts == 0
    assert row.remind_at > now

    repo.mark_failed(rid)
    assert repo.list_all(limit=1)[0].status == "failed"
    assert repo.list_pending() == []


def _fake_meta(enabled=True):
    row = SimpleNamespace(enabled=1) if enabled else None
    return SimpleNamespace(
        proactive=SimpleNamespace(get_target=lambda *a, **k: row),
    )


def _recording_meta(enabled=False):
    """白名单替身（记录是否被查过）：群聊路径不该查它。"""
    calls: list[tuple] = []

    def get_target(*args, **kwargs):
        calls.append(args)
        return SimpleNamespace(enabled=1) if enabled else None

    return SimpleNamespace(proactive=SimpleNamespace(get_target=get_target)), calls


def _non_owner_ctx():
    return SimpleNamespace(
        extra={
            "platform": "wechat",
            "account_id": "wxid_bot",
            "chat_id": "wxid_friend",
            "sender_id": "wxid_friend",
            "sender_name": "老王",
            "is_group": False,
        },
        account_context=None,
        accounts={"wechat": "wxid_bot"},
        owner_accounts={},
    )


def test_non_owner_repeat_request_is_refused():
    repo = _FakeRepo()
    tool = CreateReminderTool(repo, meta_repo=_fake_meta(True))
    tool.authoritative_is_owner = lambda: False
    tool.set_context(_non_owner_ctx())

    result = asyncio.run(tool.execute(
        content="每天提醒我喝水", remind_at=_future_at(), repeat="daily",
    ))

    assert result.is_error
    assert "循环" in result.error
    assert repo.reminders.added == []


def test_non_owner_without_whitelist_is_refused():
    repo = _FakeRepo()
    tool = CreateReminderTool(repo, meta_repo=_fake_meta(False))
    tool.authoritative_is_owner = lambda: False
    tool.set_context(_non_owner_ctx())

    result = asyncio.run(tool.execute(content="开会", remind_at=_future_at()))

    assert result.is_error
    assert "授权" in result.error
    assert repo.reminders.added == []


def test_non_owner_whitelisted_one_shot_is_stored_with_requester():
    repo = _FakeRepo()
    tool = CreateReminderTool(repo, meta_repo=_fake_meta(True))
    tool.authoritative_is_owner = lambda: False
    tool.set_context(_non_owner_ctx())

    result = asyncio.run(tool.execute(content="开会", remind_at=_future_at()))

    assert not result.is_error, result.error
    saved = repo.reminders.added[0]
    assert saved.requester_id == "wxid_friend"
    assert saved.requester_name == "老王"
    assert saved.requester_is_owner == 0
    assert saved.mention_name is None
    assert result.data.get("one_shot") is True


def test_non_owner_pending_cap_blocks_creation():
    repo = _FakeRepo()
    repo.reminders.count_pending_for_requester = lambda *a, **k: 5
    tool = CreateReminderTool(repo, meta_repo=_fake_meta(True))
    tool.authoritative_is_owner = lambda: False
    tool.set_context(_non_owner_ctx())

    result = asyncio.run(tool.execute(content="开会", remind_at=_future_at()))

    assert result.is_error
    assert "上限" in result.error
    assert repo.reminders.added == []


def test_group_mention_decided_at_creation():
    repo = _FakeRepo()
    tool = CreateReminderTool(repo, meta_repo=_fake_meta(True))
    tool.authoritative_is_owner = lambda: False
    ctx = _non_owner_ctx()
    ctx.extra["is_group"] = True
    ctx.extra["chat_id"] = "123@chatroom"
    tool.set_context(ctx)

    # 缺@对象 → 不建，先问
    missing = asyncio.run(tool.execute(content="开会", remind_at=_future_at()))
    assert missing.is_error
    assert "先问" in missing.error
    assert repo.reminders.added == []

    # "提醒我" → 模型传 ["我"]，创建时解析成 @请求人
    asyncio.run(tool.execute(content="开会", remind_at=_future_at(), mention=["我"]))
    assert json.loads(repo.reminders.added[-1].mention_names) == ["老王"]

    # 多选
    asyncio.run(tool.execute(
        content="开会", remind_at=_future_at(), mention=["小美", "小雨"],
    ))
    assert json.loads(repo.reminders.added[-1].mention_names) == ["小美", "小雨"]

    # 明确不 @
    asyncio.run(tool.execute(content="开会", remind_at=_future_at(), mention=[]))
    assert repo.reminders.added[-1].mention_names is None


def test_group_guest_reminder_is_created_without_whitelist():
    """2026-10 主人拍板：群里任何人（非主人）请她建提醒都要真的建。

    回归点：群聊不再要求 ``proactive_targets`` 白名单（旧门槛会让群友的
    提醒被回绝），但到点@谁、requester 身份、一次性口径照旧。
    """
    repo = _FakeRepo()
    meta, calls = _recording_meta(enabled=False)   # 白名单里没有这个群
    tool = CreateReminderTool(repo, meta_repo=meta)
    tool.authoritative_is_owner = lambda: False
    ctx = _non_owner_ctx()
    ctx.extra["is_group"] = True
    ctx.extra["chat_id"] = "123@chatroom"
    tool.set_context(ctx)

    result = asyncio.run(tool.execute(
        content="提醒我 8 点开会", remind_at=_future_at(), mention=["我"],
    ))

    assert not result.is_error, result.error
    assert calls == []                      # 群聊路径根本不查白名单
    saved = repo.reminders.added[0]
    assert saved.requester_id == "wxid_friend"
    assert saved.requester_name == "老王"
    assert saved.requester_is_owner == 0
    assert saved.source_chat_id == "123@chatroom"
    assert saved.source_chat_type == "group"
    assert json.loads(saved.mention_names) == ["老王"]   # ["我"] → 请求者本人
    assert saved.repeat_rule is None
    assert result.data.get("one_shot") is True


def test_group_guest_still_asks_for_mention_object():
    """放宽白名单不等于放宽@对象：群聊缺 mention 仍先问、不建。"""
    repo = _FakeRepo()
    tool = CreateReminderTool(repo, meta_repo=_fake_meta(False))
    tool.authoritative_is_owner = lambda: False
    ctx = _non_owner_ctx()
    ctx.extra["is_group"] = True
    ctx.extra["chat_id"] = "123@chatroom"
    tool.set_context(ctx)

    result = asyncio.run(tool.execute(content="开会", remind_at=_future_at()))

    assert result.is_error
    assert "先问" in result.error
    assert repo.reminders.added == []


def test_group_guest_repeat_request_still_refused():
    """群聊放宽的是「谁可以托」，不是「托什么」：循环提醒仍明确回绝。"""
    repo = _FakeRepo()
    tool = CreateReminderTool(repo, meta_repo=_fake_meta(False))
    tool.authoritative_is_owner = lambda: False
    ctx = _non_owner_ctx()
    ctx.extra["is_group"] = True
    ctx.extra["chat_id"] = "123@chatroom"
    tool.set_context(ctx)

    result = asyncio.run(tool.execute(
        content="每天提醒我喝水", remind_at=_future_at(),
        repeat="daily", mention=["我"],
    ))

    assert result.is_error
    assert "循环" in result.error
    assert repo.reminders.added == []


def test_non_owner_private_whitelist_gate_unchanged():
    """私聊仍是私人通道：非主人没白名单照样回绝（这次放宽只针对群聊）。"""
    repo = _FakeRepo()
    tool = CreateReminderTool(repo, meta_repo=_fake_meta(False))
    tool.authoritative_is_owner = lambda: False
    tool.set_context(_non_owner_ctx())      # is_group=False

    result = asyncio.run(tool.execute(content="开会", remind_at=_future_at()))

    assert result.is_error
    assert "授权" in result.error
    assert repo.reminders.added == []


# ------------------------------------------- 规则数据 / 工具说明 / 正向义务
_SEED_PATH = ROOT / "configs" / "speech_seed" / "policy_seed.json"


def _seed_rule(key: str) -> dict:
    for rule in json.loads(_SEED_PATH.read_text(encoding="utf-8")):
        if str(rule.get("key")) == key:
            return rule
    raise AssertionError(f"missing seed rule {key}")


def test_group_rule_accepts_reminder_requests_but_keeps_relay_refusal():
    """规则数据（根因①）：退回的边界保留，明确托的提醒要接、且先落库再回话。"""
    rule = _seed_rule("legacy_group_rules")
    assert rule["enabled"] is True
    assert rule["scope_type"] == "chat_type" and rule["scope_id"] == "group"
    hint = rule["payload"]["prompt_hint"]

    # 边界仍在：传话/替人做决定/掺和群友的事 → 退回
    assert "被让传话" in hint and "替人做决定" in hint
    assert "掺和群友之间的事" in hint
    assert "活别接、决定别替做" in hint
    # 正向义务：明确托的提醒/待办要接，流程写死
    assert "建提醒" in hint and "先调用工具落库、再回话" in hint
    assert "没接就明说不接，不许只在嘴上答应" in hint
    # 退回在前、例外在后：不是「什么活都接」
    assert hint.index("把话头退回去") < hint.index("先调用工具落库、再回话")


def test_tool_description_requires_store_before_promise():
    """工具说明（根因③）：不再写「只有你决定答应时才调用」这种可被读成拒接的话。"""
    desc = CreateReminderTool.description
    assert "先调用本工具落库、再回话" in desc
    assert "不许只在嘴上答应" in desc
    assert "只有你决定答应时才调用本工具" not in desc


def test_group_persona_block_carries_reminder_obligation():
    """正向义务（防再犯）：群聊 prompt 里必须出现「先落库再回话」。"""
    from core.terminal.persona_brain import build_persona_blocks

    snapshot = SimpleNamespace(
        persona=SimpleNamespace(name="鲸鲸", identity=None, traits=None),
        turn=None,
        policy=None,
    )
    group_text = "\n".join(build_persona_blocks(snapshot, is_group=True))
    private_text = "\n".join(build_persona_blocks(snapshot, is_group=False))

    assert "先调用工具落库、再回话" in group_text
    assert "只答应不落库算失职" in group_text
    assert "传话、替人做决定、掺和群友之间的事，照旧退回" in group_text
    assert "先调用工具落库、再回话" not in private_text


def test_apply_reply_prefix_is_idempotent():
    assert apply_reply_prefix("到点啦", "@老王") == "@老王 到点啦"
    assert apply_reply_prefix("@老王 到点啦", "@老王") == "@老王 到点啦"
    assert apply_reply_prefix("到点啦", "") == "到点啦"
    assert apply_reply_prefix("", "@老王") == ""


def test_reminder_turn_passes_group_mention_prefix():
    captured = {}

    async def fake_run_proactive_turn(**kwargs):
        captured.update(kwargs)
        return rt.ProactiveTurnResult(True, sent=True)

    with patch(rt, "run_proactive_turn", fake_run_proactive_turn):
        asyncio.run(rt.run_reminder_turn(
            platform="wechat", bot_account_id="wxid_bot",
            chat_id="123@chatroom", chat_type="group",
            content="开会", mention_names=["老王", "小雨"],
        ))

    assert captured["reply_prefix"] == "@老王 @小雨"
    assert "老王" in captured["hint"] and "小雨" in captured["hint"]


def test_scheduler_passes_mention_names():
    repo = _ScanRepo(due=[_due(
        source_chat_id="123@chatroom", source_chat_type="group",
        mention_names='["老王", "小雨"]',
    )])
    sent = {}

    async def fake_turn(**kwargs):
        sent.update(kwargs)
        return rt.ProactiveTurnResult(True, sent=True, reason="")

    with patch(sm, "get_repo", lambda: repo), patch(rt, "run_reminder_turn", fake_turn):
        asyncio.run(sm.run_reminder_delivery())

    assert sent["mention_names"] == ["老王", "小雨"]

    # 44 版单值旧列兜底
    repo2 = _ScanRepo(due=[_due(
        source_chat_id="123@chatroom", source_chat_type="group",
        mention_names=None, mention_name="老王",
    )])
    sent2 = {}

    async def fake_turn2(**kwargs):
        sent2.update(kwargs)
        return rt.ProactiveTurnResult(True, sent=True, reason="")

    with patch(sm, "get_repo", lambda: repo2), patch(rt, "run_reminder_turn", fake_turn2):
        asyncio.run(sm.run_reminder_delivery())

    assert sent2["mention_names"] == ["老王"]

    # 44 版遗留的非 JSON 名单：按分隔符切（老实现把整串当一个人 → 到点 @ 不到人）。
    # 语义 owner = core.tasks.mapping.split_mention_names，与迁移侧同一答案。
    repo3 = _ScanRepo(due=[_due(
        source_chat_id="123@chatroom", source_chat_type="group",
        mention_names="老王,小雨",
    )])
    sent3 = {}

    async def fake_turn3(**kwargs):
        sent3.update(kwargs)
        return rt.ProactiveTurnResult(True, sent=True, reason="")

    with patch(sm, "get_repo", lambda: repo3), patch(rt, "run_reminder_turn", fake_turn3):
        asyncio.run(sm.run_reminder_delivery())

    assert sent3["mention_names"] == ["老王", "小雨"]


def test_owner_one_shot_and_repeat_both_allowed():
    """主人不受新门槛影响：一次性/循环都能建，连白名单都不需要。"""
    repo = _FakeRepo()
    tool = CreateReminderTool(repo)          # meta_repo=None：主人不查白名单
    tool.authoritative_is_owner = lambda: True
    tool.set_context(_non_owner_ctx())       # 同一个会话上下文，仅身份不同

    one_shot = asyncio.run(tool.execute(content="开会", remind_at=_future_at()))
    assert not one_shot.is_error, one_shot.error
    saved = repo.reminders.added[-1]
    assert saved.repeat_rule is None          # 不传 repeat = 一次性
    assert saved.requester_is_owner == 1
    assert one_shot.data.get("one_shot") is False

    repeated = asyncio.run(tool.execute(
        content="每天开会", remind_at=_future_at(), repeat="daily",
    ))
    assert not repeated.is_error, repeated.error
    assert repo.reminders.added[-1].repeat_rule == "daily"


def test_act_purpose_reserved_but_refused():
    repo = _FakeRepo()
    tool = CreateReminderTool(repo)
    tool.authoritative_is_owner = lambda: True
    tool.set_context(_non_owner_ctx())

    result = asyncio.run(tool.execute(
        content="到点帮我查天气再告诉我", remind_at=_future_at(), purpose="act",
    ))

    assert result.is_error
    assert "执行器" in result.error
    assert repo.reminders.added == []


def main() -> bool:
    tests = [fn for name, fn in globals().items() if name.startswith("test_")]
    failed = 0
    for fn in tests:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception:
            failed += 1
            print(f"FAIL {fn.__name__}")
            traceback.print_exc()
    print(f"{len(tests) - failed}/{len(tests)} passed")
    return failed == 0


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
