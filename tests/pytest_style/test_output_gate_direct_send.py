"""键鼠线（loop/desk.py）出口闸门：``gate_direct_text`` 的三态与作用域。

背景：2026-11-16 生产切换前，output_gate 只挂在 OutboundGateway 上，而唯一在用的
发送入口 loop/desk.py:deliver 直接调 wx_sender，完全绕过 guards。这里锁住补上的
那条路径：shadow 只观察、on 只改写名单泄漏且不改字数、off 全空转、作用域互相独立。
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

ROSTER = "张小三，1班，学委，女\n李四，2班，委员，男"
NORMAL = "吃了没"


def _gate(text, *, chat_type="private", chat_id="c1", monkeypatch=None, env="shadow", enforce=None):
    from core.speech.outbound import gate_direct_text

    monkeypatch.setenv("YINYUE_FEATURE_OUTPUT_GATE", env)
    if enforce is None:
        monkeypatch.delenv("YINYUE_FEATURE_OUTPUT_GATE_ENFORCE", raising=False)
    else:
        monkeypatch.setenv("YINYUE_FEATURE_OUTPUT_GATE_ENFORCE", enforce)
    return gate_direct_text(text, chat_type=chat_type, chat_id=chat_id)


def test_shadow_only_observes_roster(monkeypatch):
    got = _gate(ROSTER, env="shadow", monkeypatch=monkeypatch)
    assert got["mode"] == "shadow"
    assert got["text"] == ROSTER  # 一个字都不改
    assert got["blocked"] is False
    assert got["action"] == "warn"
    assert got["gate"] is not None
    assert [f.rule for f in got["gate"].findings] == ["roster_leak"]


def test_on_rewrites_roster_with_refusal(monkeypatch):
    got = _gate(ROSTER, env="on", monkeypatch=monkeypatch)
    assert got["mode"] == "on"
    assert got["blocked"] is False
    assert got["action"] == "rewrite"
    assert got["text"] == "这个我不往外发。"
    assert got["gate"].enforced is True


def test_on_private_also_rewrites_roster(monkeypatch):
    got = _gate(ROSTER, chat_type="private", env="on", monkeypatch=monkeypatch)
    assert got["text"] == "这个我不往外发。"


def test_on_never_trims_long_group_text(monkeypatch):
    long_text = "今天" * 110
    got = _gate(long_text, chat_type="group", env="on", monkeypatch=monkeypatch)
    assert got["text"] == long_text  # 字数只观察，不压短（主人裁决 2026-10-23）
    assert got["action"] == "warn"
    assert "group_max_chars" in [f.rule for f in got["gate"].findings]


def test_off_passes_everything_through(monkeypatch):
    got = _gate(ROSTER, env="off", monkeypatch=monkeypatch)
    assert got["mode"] == "off"
    assert got["text"] == ROSTER
    assert got["gate"] is None
    assert got["blocked"] is False


def test_on_normal_text_allows(monkeypatch):
    got = _gate(NORMAL, env="on", monkeypatch=monkeypatch)
    assert got["text"] == NORMAL
    assert got["action"] == "allow"


def test_enforce_scope_observe_downgrades_roster(monkeypatch):
    got = _gate(ROSTER, env="on", enforce="observe", monkeypatch=monkeypatch)
    assert got["text"] == ROSTER
    assert got["action"] == "warn"
    assert got["gate"].enforced is False


def test_desk_deliver_calls_gate_before_send():
    desk = (ROOT / "loop" / "desk.py").read_text(encoding="utf-8")
    send_at = desk.index('r = s.send_text(who, str(_gate.get("text") or ""), **options)')
    gate_at = desk.index("_gate = gate_direct_text(")
    assert gate_at < send_at, "闸门必须在发送之前"
    assert 'return False, "blocked", "output_gate:%s"' in desk
    assert "record_direct_gate_audit(" in desk


def test_audit_writer_uses_outbound_audit_table():
    out = (ROOT / "core" / "speech" / "outbound.py").read_text(encoding="utf-8")
    assert "def gate_direct_text(" in out
    assert "def record_direct_gate_audit(" in out
    assert "OutboundAudit(" in out and "get_meta_repo().engine" in out


def test_desk_chat_type_uses_chat_id_not_queue_group_id():
    # desk 的 chat_type 由会话语义判定：queue.group_id 对私聊同样非空（去重/批次键）。
    from db.models import is_group_chat_id

    assert is_group_chat_id('10000000002@chatroom') is True
    assert is_group_chat_id('wxid_seed01') is False
    assert is_group_chat_id('group:demo') is True

    desk = Path(__file__).resolve().parents[2].joinpath('loop/desk.py').read_text(encoding='utf-8')
    assert '_gate_type = "group" if is_group_chat_id(chat_id) else "private"' in desk
