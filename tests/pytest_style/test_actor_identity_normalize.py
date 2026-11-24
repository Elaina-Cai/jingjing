r"""task-49：事件流 actor 身份归一（off 逐字 / on 分叉 / 解析不出保原值）。

跑法：``python scripts/run_pytest_suite.py --filter test_actor_identity_normalize``
（pytest 风格用例统一放 tests/pytest_style/，见该脚本文件头）
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from core import features  # noqa: E402
from core.terminal import event_store_db as store  # noqa: E402
from core.terminal.event_model import Event, EventType  # noqa: E402


FLAG_ENV = features.env_key(features.ACTOR_IDENTITY_NORMALIZE)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv(FLAG_ENV, raising=False)
    store.reset_actor_identity_state()
    yield
    store.reset_actor_identity_state()


def _event(**overrides) -> Event:
    base = dict(
        event_id="e1",
        occurred_at=datetime(2026, 10, 18, 12, 0, 0),
        platform="wechat",
        bot_account_id="wxid_bot",
        chat_id="c1",
        chat_type="group",
        actor_person_id="wxid_a",
        event_type=EventType.MESSAGE,
        content="hi",
        metadata={},
    )
    base.update(overrides)
    return Event(**base)


def _on(monkeypatch) -> None:
    monkeypatch.setenv(FLAG_ENV, "on")


def test_flag_registered_and_off_by_default():
    assert features.ACTOR_IDENTITY_NORMALIZE in features.TRI_STATE_FLAGS
    spec = features.FLAG_REGISTRY[features.ACTOR_IDENTITY_NORMALIZE]
    assert spec.default == features.MODE_OFF
    assert features.mode(features.ACTOR_IDENTITY_NORMALIZE, features.MODE_OFF) == features.MODE_OFF


def test_off_keeps_legacy_columns_byte_for_byte():
    values = store._event_to_values(_event(actor_account_id=""))
    assert values["actor_person_id"] == "wxid_a"
    assert values["actor_account_id"] == ""
    values = store._event_to_values(_event(actor_account_id="wxid_a"))
    assert values["actor_account_id"] == "wxid_a"


def test_on_writes_person_id_and_keeps_account(monkeypatch):
    _on(monkeypatch)
    store.override_actor_identity_map({("wechat", "wxid_bot", "wxid_a"): "p1"})
    values = store._event_to_values(_event(actor_account_id="wxid_a"))
    assert values["actor_person_id"] == "p1"
    assert values["actor_account_id"] == "wxid_a"


def test_on_fills_account_when_missing(monkeypatch):
    _on(monkeypatch)
    store.override_actor_identity_map({("wechat", "wxid_bot", "wxid_a"): "p1"})
    values = store._event_to_values(_event(actor_account_id=""))
    assert values["actor_person_id"] == "p1"
    assert values["actor_account_id"] == "wxid_a"


def test_on_unresolved_keeps_raw_and_counts(monkeypatch):
    _on(monkeypatch)
    store.override_actor_identity_map({})
    values = store._event_to_values(_event(actor_person_id="wxid_zzz", actor_account_id="wxid_zzz"))
    assert values["actor_person_id"] == "wxid_zzz"
    assert store.actor_identity_counters().get("unresolved") == 1


@pytest.mark.parametrize("raw", ["unknown", "self", "unresolved:qq:123"])
def test_on_placeholders_never_written(monkeypatch, raw):
    _on(monkeypatch)
    store.override_actor_identity_map({("wechat", "wxid_bot", raw): "p1"})
    values = store._event_to_values(_event(actor_person_id=raw, actor_account_id=raw))
    assert values["actor_person_id"] == raw


def test_on_ambiguous_value_is_not_resolved(monkeypatch):
    _on(monkeypatch)
    store.override_actor_identity_map({("wechat", "wxid_bot", "wxid_dup"): None})
    values = store._event_to_values(_event(actor_person_id="wxid_dup", actor_account_id="wxid_dup"))
    assert values["actor_person_id"] == "wxid_dup"


def test_shadow_writes_nothing(monkeypatch):
    monkeypatch.setenv(FLAG_ENV, "shadow")
    store.override_actor_identity_map({("wechat", "wxid_bot", "wxid_a"): "p1"})
    values = store._event_to_values(_event(actor_account_id="wxid_a"))
    assert values["actor_person_id"] == "wxid_a"
    assert values["actor_account_id"] == "wxid_a"
    assert store.actor_identity_counters().get("shadow") == 1


def test_speaker_prefers_account_id():
    from core.terminal.speaker import resolve_speaker

    assert (
        resolve_speaker("message", "p1", names={"wxid_a": "P6"}, actor_account_id="wxid_a")
        == "P6"
    )
    assert resolve_speaker("message", "p1", names={"p1": "P6"}) == "P6"


def test_load_names_covers_both_id_spaces(tmp_path):
    from sqlalchemy import create_engine

    from core.memory.recall_chain import load_names

    db = tmp_path / "meta.db"
    engine = create_engine("sqlite:///" + str(db))
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "create table person_entities (person_id text primary key, display_name text)"
        )
        conn.exec_driver_sql(
            "create table person_identities (id integer primary key, platform text, "
            "account_id text, identity_type text, identity_value text, person_id text)"
        )
        conn.exec_driver_sql("insert into person_entities values ('p1', 'P6')")
        conn.exec_driver_sql(
            "insert into person_identities (platform, account_id, identity_type, "
            "identity_value, person_id) values ('wechat','wxid_bot','wxid','wxid_a','p1')"
        )
    names = load_names(engine)
    assert names.get("wxid_a") == "P6"
    assert names.get("p1") == "P6"

# --- self（她自己）的单列语义：self ≠ unknown，能唯一定位才映射 -----------------


def test_self_event_maps_to_her_person(monkeypatch):
    _on(monkeypatch)
    store.override_actor_identity_map({("wechat", "wxid_bot", "wxid_bot"): "her"})
    values = store._event_to_values(
        _event(
            actor_person_id="self",
            actor_account_id="self",
            bot_account_id="wxid_bot",
            metadata={"is_self": True},
        )
    )
    assert values["actor_person_id"] == "her"
    assert values["actor_account_id"] == "wxid_bot"


def test_self_event_unmapped_keeps_self_and_counts(monkeypatch):
    _on(monkeypatch)
    store.override_actor_identity_map({})
    values = store._event_to_values(
        _event(actor_person_id="self", actor_account_id="self", metadata={"is_self": True})
    )
    assert values["actor_person_id"] == "self"
    assert values["actor_account_id"] == "self"
    assert store.actor_identity_counters().get("self_unmapped") == 1


def test_unknown_with_is_self_is_never_touched(monkeypatch):
    _on(monkeypatch)
    store.override_actor_identity_map({("wechat", "wxid_bot", "wxid_bot"): "her"})
    values = store._event_to_values(
        _event(
            actor_person_id="unknown",
            actor_account_id="unknown",
            metadata={"is_self": True},
        )
    )
    assert values["actor_person_id"] == "unknown"
    assert values["actor_account_id"] == "unknown"
    assert "self_unmapped" not in store.actor_identity_counters()
