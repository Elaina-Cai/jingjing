from core.infra import actions
from loop import ledger, ports
from datetime import datetime
from types import SimpleNamespace
import sqlite3


def _append(scope, *, target, why, turn_id, at=""):
    token = ports._READ_SCOPE.set(scope)
    try:
        return ledger.append_receipt(
            act="say", target=target, why=why, text=why,
            result="sent", turn_id=turn_id, ack="queued", at=at,
        )
    finally:
        ports._READ_SCOPE.reset(token)


def test_receipts_and_digest_stay_inside_the_current_conversation(tmp_path, monkeypatch):
    monkeypatch.setattr(actions, "DIR", str(tmp_path))
    monkeypatch.setattr(ledger, "_evidence", lambda: (set(), False))
    group = ports.ReadScope("wechat", "acct-a", "group@chatroom")
    same_chat_other_account = ports.ReadScope("wechat", "acct-b", "group@chatroom")
    private = ports.ReadScope("wechat", "acct-a", "private-person")
    _append(group, target=group.chat_id, why="群里的公开理由", turn_id="turn-group")
    _append(same_chat_other_account, target=group.chat_id, why="其他账号同群号的资料",
            turn_id="turn-other-account")
    _append(private, target=private.chat_id, why="私聊里的秘密理由", turn_id="turn-private")
    for number in range(25):
        _append(private, target=private.chat_id, why=f"更晚的私聊记录 {number}",
                turn_id=f"turn-private-later-{number}")
    _append(group, target=group.chat_id, why="群里的新公开理由", turn_id="turn-group-latest")

    rows = ledger.read_receipts(100)
    assert {(row["scope"]["platform"], row["scope"]["account_id"], row["scope"]["chat_id"])
            for row in rows} == {
        ("wechat", "acct-a", "group@chatroom"),
            ("wechat", "acct-b", "group@chatroom"),
        ("wechat", "acct-a", "private-person"),
    }

    token = ports._READ_SCOPE.set(group)
    try:
        group_digest = ledger.render_digest()
    finally:
        ports._READ_SCOPE.reset(token)
    assert "群里的公开理由" in group_digest
    assert "其他账号同群号的资料" not in group_digest
    assert "私聊里的秘密理由" not in group_digest

    token = ports._READ_SCOPE.set(group)
    try:
        assert ledger.speaking_facts(group.chat_id)["says"] == 2
        recent = ledger.recent_says_in(group.chat_id, limit=10)
    finally:
        ports._READ_SCOPE.reset(token)
    assert "其他账号同群号的资料" not in {row["text"] for row in recent}

    token = ports._READ_SCOPE.set(private)
    try:
        private_digest = ledger.render_digest()
    finally:
        ports._READ_SCOPE.reset(token)
    assert "更晚的私聊记录 24" in private_digest
    assert "群里的公开理由" not in private_digest

    unscoped = ledger.render_digest()
    assert "群里的新公开理由" in unscoped
    assert "更晚的私聊记录 24" in unscoped


def test_last_say_gates_do_not_mix_same_chat_across_accounts(tmp_path, monkeypatch):
    monkeypatch.setattr(actions, "DIR", str(tmp_path))
    first = ports.ReadScope("wechat", "acct-a", "same@chatroom")
    second = ports.ReadScope("wechat", "acct-b", "same@chatroom")
    _append(first, target=first.chat_id, why="账号 A 说过",
            turn_id="turn-a", at="2026-11-19 10:00:00")
    _append(second, target=second.chat_id, why="账号 B 说过",
            turn_id="turn-b", at="2026-11-19 11:00:00")
    for number in range(350):
        _append(second, target=second.chat_id, why=f"账号 B 后续记录 {number}",
                turn_id=f"turn-b-later-{number}", at="2026-11-19 11:00:01")

    token = ports._READ_SCOPE.set(first)
    try:
        assert ledger.last_say_at(first.chat_id) == datetime(
            2026, 11, 19, 10, 0, 0
        ).timestamp()
        assert ledger.said_this_turn(first.chat_id, "turn-a") is True
        assert ledger.said_this_turn(first.chat_id, "turn-b") is False
        assert ledger.quiet_ms(first.chat_id) >= 0
    finally:
        ports._READ_SCOPE.reset(token)


def test_proactive_cooldown_passes_the_explicit_account_scope(monkeypatch):
    from core.terminal.proactive_service import ProactivePolicyService

    target = SimpleNamespace(
        enabled=1, allow_sticker=1, quiet_hours_json="[]",
        cooldown_minutes=10, max_per_hour=100, max_per_day=100,
    )
    repo = SimpleNamespace(proactive=SimpleNamespace(
        get_target=lambda platform, account, chat: target,
        count_since=lambda **kwargs: 0,
        last_send_at=lambda *args: None,
    ))
    service = ProactivePolicyService(repo)
    monkeypatch.setattr(service, "_engine_reject", lambda **kwargs: None)
    calls = []
    monkeypatch.setattr(actions, "quiet_ms",
                        lambda chat, **kwargs: calls.append((chat, kwargs)) or None)

    decision = service.evaluate(
        platform="wechat", bot_account_id="acct-a",
        chat_id="same@chatroom", chat_type="group",
        now=datetime(2026, 11, 20, 1, 0, 0),
    )
    assert decision.allowed is True
    assert calls == [
        ("same@chatroom", {"scope": ("wechat", "acct-a", "same@chatroom")}),
    ]


def test_speech_receipt_writes_the_same_scope(monkeypatch):
    from core.speech.outbound import OutboundGateway

    captured = []
    monkeypatch.setattr(actions, "append_receipt",
                        lambda **kwargs: captured.append(kwargs) or kwargs)
    applied = SimpleNamespace(
        who="", content="语音内容", filepath="",
        target=SimpleNamespace(chat_id="same@chatroom", platform="wechat"),
    )
    gateway = object.__new__(OutboundGateway)
    gateway._record_action_receipt(applied, "acct-a")

    assert captured[0]["extra"]["scope"] == {
        "platform": "wechat",
        "account_id": "acct-a",
        "chat_id": "same@chatroom",
    }


def test_unscoped_legacy_receipts_are_not_guessed_into_a_conversation(tmp_path, monkeypatch):
    monkeypatch.setattr(actions, "DIR", str(tmp_path))
    monkeypatch.setattr(ledger, "_evidence", lambda: (set(), False))
    actions.append_receipt(
        act="say", target="same", why="旧账没有账号来源",
        text="旧账没有账号来源", result="sent",
        turn_id="legacy", ack="queued",
    )
    scope = ports.ReadScope("wechat", "acct-a", "same")
    token = ports._READ_SCOPE.set(scope)
    try:
        assert ledger.last_say_at("same") == 0.0
        assert ledger.recent_says_in("same") == []
        assert "旧账没有账号来源" not in ledger.render_digest()
    finally:
        ports._READ_SCOPE.reset(token)
    assert "旧账没有账号来源" in ledger.render_digest()


def test_account_database_is_the_current_turns_database(tmp_path):
    current = tmp_path / "acct-a" / "account.db"
    other = tmp_path / "acct-b" / "account.db"
    for path, count in ((current, 1), (other, 50)):
        path.parent.mkdir(parents=True)
        connection = sqlite3.connect(path)
        try:
            connection.execute("create table messages(id integer)")
            connection.executemany("insert into messages values(?)",
                                   [(number,) for number in range(count)])
            connection.commit()
        finally:
            connection.close()

    scope = ports.ReadScope(
        "wechat", "acct-a", "same", db_path=str(current),
    )
    token = ports._READ_SCOPE.set(scope)
    try:
        assert ports._account_db() == str(current)
    finally:
        ports._READ_SCOPE.reset(token)
