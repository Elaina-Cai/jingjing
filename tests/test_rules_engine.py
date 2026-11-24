# -*- coding: utf-8 -*-
"""N4 规则引擎（``core/rules/engine.py``）：五维 / 数据面 / 收编 / 死字段 用例。

主人口径：「Web-UI 可配的**动作前置条件**」（`docs/decisions/架构评估.md` §3.4、§6 P1），
乱点 #21/#28 + G1-13 的落点。

本文件守的八件事
----------------
① **五维**：``condition`` / ``target`` / ``quota`` / ``action`` / ``priority``
   各有断言；``RuleDecision`` 的六个契约字段逐个断言（只给 ``allowed`` 不够）；
② **改一行数据即生效**：同一进程内改 ``policy_rules`` 一行 + ``bump_revision``
   → **不重启**就换判定（走既有 ``get_revision()`` 缓存通道）；
③ ``quota`` 超限 → 拒绝且 ``explanation`` 可读（含"哪个窗口、用了几次"）；
④ **shadow 模式只记录不影响行为**（``allowed`` 恒真 + ``explanation`` 说清本来会拒；
   **唯一例外是硬禁区**，见 ⑦）；
⑤ **12 处前置校验收敛到唯一判定点**（结构锁：那些模块不再各写一份判定）；
⑥ **死字段处置**：``ToolPolicy.max_calls`` / ``require_confirmation`` 接进引擎
   （断言再没有"零读取方"字段）；
⑦ **硬禁区**不可配（写死代码）；
⑧ ``RULES=off`` → 各调用点与接线前**逐字节一致**。

迁移 55（``rule_use_ledger``）的幂等 + 建表 parity 也在本文件（照
``tests/test_focus_state.py`` 的口径）。

Run directly:
    python tests/test_rules_engine.py
"""

from __future__ import annotations

import ast
import json
import os
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

ROOT = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# 隔离前导（照 tests/test_output_gate.py）：任何走 bootstrap 的路径都只能看见
# scratch 根，碰不到生产 meta / account 库。
# ---------------------------------------------------------------------------
_TEST_LOG_DIR = str(Path("data") / "_test_logs_rules_engine")
Path(_TEST_LOG_DIR).mkdir(parents=True, exist_ok=True)
os.environ.setdefault("LOG_DIR", _TEST_LOG_DIR)
_ISO_ROOT = (ROOT / "data" / "_rules_engine_test" / "_iso").resolve()
_ISO_ROOT.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("META_DB_PATH", str(_ISO_ROOT / "meta.db"))
os.environ.setdefault("PLATFORMS_DIR", str(_ISO_ROOT / "platforms"))
os.environ.setdefault("DATA_DIR", str(_ISO_ROOT / "data"))

from sqlalchemy import create_engine, inspect, select, text  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from core import features  # noqa: E402

RULES_ENV = features.env_key(features.RULES)

# ---------------------------------------------------------------------------
# 开关：本文件断言的档位**逐条自己声明**，绝不吃环境默认（默认是 off）。
#
# pytest 下用 autouse fixture 把档位设成 ``on``（"判定要生效"是本文件的主线），
# 单个用例再用 :func:`_rules_env` 明确覆盖成 ``off`` / ``shadow``——那些覆盖
# 就是"回滚位"的断言本身。直跑（``python tests/test_rules_engine.py``）时由
# :func:`main` 自己设同一档位。**不硬依赖 pytest**：hook 用 try/except 包住。
# ---------------------------------------------------------------------------
_DEFAULT_MODE = "on"

try:  # pragma: no cover - 直跑（无 pytest）时走不到
    import pytest

    @pytest.fixture(autouse=True)
    def _engine_mode_default():
        saved = os.environ.get(RULES_ENV)
        os.environ[RULES_ENV] = _DEFAULT_MODE
        try:
            yield
        finally:
            if saved is None:
                os.environ.pop(RULES_ENV, None)
            else:
                os.environ[RULES_ENV] = saved
except Exception:  # noqa: BLE001 - 没有 pytest 就直跑
    pytest = None  # type: ignore[assignment]

from core.rules import engine as rules_engine  # noqa: E402
from core.speech.domain import (  # noqa: E402
    AccountPolicy,
    AudienceKind,
    ConversationTarget,
    OutboundDraft,
    PolicyAction,
    ResolvedPersona,
    SpeakingPolicy,
    ToolPolicy,
)
from core.speech.store import SpeechStore  # noqa: E402
from db import meta_models as mm  # noqa: E402
from db import migrations as migrations_mod  # noqa: E402

#: 判定用的事实（时间固定，不读墙钟）。
FIXED_NOW = datetime(2026, 10, 6, 14, 30, 0)
CHAT = "10000000001@chatroom"
ACTOR = "wxid_friend"


# ---------------------------------------------------------------------------
# 开关：**显式钉死**（本文件断言的每一个档位都自己设，绝不靠环境默认）
# ---------------------------------------------------------------------------
@contextmanager
def _rules_env(value: str | None):
    """临时设置 ``YINYUE_FEATURE_RULES``；退出时连"未设置"状态一起还原。"""
    saved = os.environ.get(RULES_ENV)
    try:
        if value is None:
            os.environ.pop(RULES_ENV, None)
        else:
            os.environ[RULES_ENV] = value
        yield
    finally:
        if saved is None:
            os.environ.pop(RULES_ENV, None)
        else:
            os.environ[RULES_ENV] = saved


def _isolated_store(name: str) -> tuple[SpeechStore, Any]:
    """隔离 meta 库 + ``SpeechStore``（不碰生产库）。

    每次调用都**清掉引擎的进程级实例表**：里面还挂着上一条用例的
    ``RuleEngine``（连带它的同进程回退计数），不清就会串味
    （``id()`` 会被回收重用，缓存键同号即命中）。
    """
    rules_engine._ENGINES.clear()
    rules_engine._ENGINE_OWNERS.clear()
    directory = ROOT / "data" / "_rules_engine_test"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.db"
    if path.exists():
        path.unlink()
    engine = create_engine(f"sqlite:///{path}")
    mm.MetaBase.metadata.create_all(engine)
    return SpeechStore(engine), engine


def _reset_engine_cache(store: SpeechStore) -> SpeechStore:
    """清掉引擎的进程级实例表（``id()`` 回收重用会命中同号缓存）+ 台账残留。

    **不动模块单例**（``core.speech.store._STORE``）：路由与工具走的是
    ``get_speech_store()`` 解析出来的那一个，而本文件用隔离 store——两者身份不同
    是**有意的**（用例不许把生产单例换成隔离库）。所以本文件里凡是"数据行优先"
    的断言都**显式传 store**，见 :func:`test_reminder_pending_cap_goes_through_the_engine`。
    """
    from sqlalchemy import delete as _delete

    rules_engine._ENGINES.clear()
    rules_engine._ENGINE_OWNERS.clear()
    with store._Session() as session:
        session.execute(_delete(mm.RuleUseLedger))
        session.commit()
    return store


def _write_data_rule(store: SpeechStore, rules: list[dict], *,
                     key: str = rules_engine.RULES_RULE_KEY,
                     bump: bool = True) -> None:
    """把规则写进 ``policy_rules`` 一行（模拟主人在 Web 上改数据）。"""
    with store._Session() as session:
        row = session.execute(
            select(mm.PolicyRule).where(mm.PolicyRule.key == key)
        ).scalar_one_or_none()
        if row is None:
            row = mm.PolicyRule(key=key, name="动作前置条件", category="action_rule",
                                enforcement="deterministic", enabled=1)
            session.add(row)
        row.payload_json = json.dumps({"rules": rules}, ensure_ascii=False)
        row.enabled = 1
        session.commit()
    if bump:
        store.bump_revision("action_rules", "test", {"key": key})


def _ctx(**overrides) -> rules_engine.ActionContext:
    facts = dict(
        action_id="create_reminder",
        actor_id=ACTOR,
        chat_id=CHAT,
        chat_type="group",
        platform="wechat",
        account_id="wxid_bot",
        is_owner=False,
        is_group=True,
        now=FIXED_NOW,
    )
    facts.update(overrides)
    return rules_engine.ActionContext(**facts)


@contextmanager
def _enabled(value: str = "on"):
    """把引擎档位设成给定值（**显式**，不靠环境默认）。

    默认档位是 ``off``（回滚位），所以"判定要生效"的用例必须自己说清楚要哪一档。
    """
    with _rules_env(value):
        yield


def _rule(**overrides) -> dict:
    """一条规则的数据形状（默认：允许群里任何人建提醒，无条件）。"""
    data = {
        "key": "r1",
        "name": "允许建提醒",
        "action": {"action_id": "create_reminder", "effect": "allow"},
    }
    data.update(overrides)
    return data


# ===========================================================================
# ① 五维 + RuleDecision 的六个契约字段
# ===========================================================================
def test_decision_contract_has_all_six_fields() -> None:
    """``RuleDecision`` 字段名逐字照 ``docs/decisions/架构评估.md`` §3.4 :260。"""
    store, _ = _isolated_store("contract")
    _write_data_rule(store, [_rule(quota={"max_per_hour": 3})])
    decision = rules_engine.decide(_ctx(), store=store)
    payload = decision.as_dict()
    for field_name in ("allowed", "rule_id", "specificity", "decided_at",
                       "explanation", "shadow"):
        assert field_name in payload, (field_name, sorted(payload))
    assert decision.allowed is True, decision
    assert decision.rule_id == "r1", decision.rule_id
    assert isinstance(decision.specificity, int) and decision.specificity > 0, decision
    assert decision.decided_at == FIXED_NOW.isoformat(timespec="seconds"), decision
    assert decision.explanation, "explanation 是 R-V3 的载体，必须非空"
    assert decision.shadow is False, decision
    return True


def test_condition_dimension_time_window_and_topic() -> None:
    """第一维 ``condition``：时间段（含跨午夜）+ 话题取值。"""
    store, _ = _isolated_store("condition")
    _write_data_rule(store, [_rule(
        condition={"time_window": ["23:00", "07:00"], "topic_type": ["闲聊"]},
    )])
    inside = rules_engine.decide(
        _ctx(now=datetime(2026, 10, 6, 2, 0), topic_type="闲聊"), store=store)
    assert inside.rule_id == "r1" and inside.allowed, inside
    outside_hour = rules_engine.decide(
        _ctx(now=datetime(2026, 10, 6, 14, 0), topic_type="闲聊"), store=store)
    assert outside_hour.rule_id == "", outside_hour
    outside_topic = rules_engine.decide(
        _ctx(now=datetime(2026, 10, 6, 2, 0), topic_type="求助"), store=store)
    assert outside_topic.rule_id == "", outside_topic
    # 非跨午夜窗口
    _write_data_rule(store, [_rule(
        key="r2", condition={"time_window": ["09:00", "18:00"]},
    )])
    assert rules_engine.decide(
        _ctx(now=datetime(2026, 10, 6, 12, 0)), store=store).rule_id == "r2"
    assert rules_engine.decide(
        _ctx(now=datetime(2026, 10, 6, 20, 0)), store=store).rule_id == ""
    return True


def test_target_dimension_people_chats_role() -> None:
    """第二维 ``target``：人 / 会话 / 角色三个子条件各自生效。"""
    store, _ = _isolated_store("target")
    _write_data_rule(store, [
        _rule(key="people_rule", target={"people": ["wxid_owner"]}),
        _rule(key="chat_rule", target={"chats": [CHAT]}),
        _rule(key="role_rule", target={"role": "owner"}),
    ])
    by_people = rules_engine.decide(_ctx(actor_id="wxid_owner"), store=store)
    assert "people_rule" in by_people.matched_keys, by_people
    by_chat = rules_engine.decide(_ctx(chat_id=CHAT), store=store)
    assert "chat_rule" in by_chat.matched_keys, by_chat
    # 非主人 + 群 → role=group（不是 owner，也不是 member）
    by_group_role = rules_engine.decide(_ctx(), store=store)
    assert "role_rule" not in by_group_role.matched_keys, by_group_role
    by_owner_role = rules_engine.decide(_ctx(is_owner=True), store=store)
    assert "role_rule" in by_owner_role.matched_keys, by_owner_role
    # 私聊里的非主人是 member
    _write_data_rule(store, [_rule(key="member_rule", target={"role": "member"})])
    private = rules_engine.decide(
        _ctx(chat_type="private", is_group=False, chat_id="wxid_friend"), store=store)
    assert "member_rule" in private.matched_keys, private
    return True


def test_action_dimension_allow_and_deny() -> None:
    """第四维 ``action``：动作 id 命中 + 允许/拒绝两值。"""
    store, _ = _isolated_store("action")
    _write_data_rule(store, [
        _rule(key="deny_reminder", action={"action_id": "create_reminder",
                                           "effect": "deny"}),
        _rule(key="allow_sticker", action={"action_id": "send_sticker",
                                           "effect": "allow"}),
    ])
    denied = rules_engine.decide(_ctx(action_id="create_reminder"), store=store)
    assert denied.allowed is False, denied
    assert denied.rule_id == "deny_reminder", denied
    allowed = rules_engine.decide(_ctx(action_id="send_sticker"), store=store)
    assert allowed.allowed is True, allowed
    assert allowed.rule_id == "allow_sticker", allowed
    # 没命中任何动作 → 放行，explanation 说清"没有命中任何动作前置条件"
    miss = rules_engine.decide(_ctx(action_id="search_messages"), store=store)
    assert miss.allowed is True and miss.rule_id == "", miss
    assert "没有命中" in miss.explanation, miss.explanation
    return True


def test_priority_and_specificity_order() -> None:
    """第五维 ``priority``：**具体度优先、同具体度才比 priority**，最后按 key 定序。"""
    store, _ = _isolated_store("priority")
    # ① 同效果：具体度高的胜（哪怕 priority 低得多）
    _write_data_rule(store, [
        _rule(key="a_generic_high_priority", priority=999),
        _rule(key="b_specific_low_priority", priority=1, target={"chats": [CHAT]}),
    ])
    decision = rules_engine.decide(_ctx(), store=store)
    assert decision.rule_id == "b_specific_low_priority", decision
    assert decision.allowed is True, decision
    assert decision.specificity > 1, decision
    assert decision.matched_keys == ("b_specific_low_priority",
                                     "a_generic_high_priority"), decision.matched_keys

    # ② 不同效果：具体度高的那条说了算（哪怕它是 deny、priority 更低）
    _write_data_rule(store, [
        _rule(key="a_generic_allow", priority=999,
              action={"action_id": "create_reminder", "effect": "allow"}),
        _rule(key="b_specific_deny", priority=1, target={"chats": [CHAT]},
              action={"action_id": "create_reminder", "effect": "deny"}),
    ])
    denied = rules_engine.decide(_ctx(), store=store)
    assert denied.rule_id == "b_specific_deny", denied
    assert denied.allowed is False, denied

    # ③ 同具体度：priority 大者胜
    _write_data_rule(store, [
        _rule(key="low", priority=10),
        _rule(key="high", priority=20),
    ])
    assert rules_engine.decide(_ctx(), store=store).rule_id == "high"
    # ④ 完全并列：按 key 定序（判定必须确定，不许看行序）
    _write_data_rule(store, [_rule(key="zz"), _rule(key="aa")])
    assert rules_engine.decide(_ctx(), store=store).rule_id == "aa"
    return True


# ===========================================================================
# ② 改一行数据即生效（同一进程、不重启）
# ===========================================================================
def test_one_data_row_change_takes_effect_without_restart() -> None:
    """**主用例**：同一 store / 同一进程，改数据 + bump revision → 换判定。

    这条是 #28「谁 bump revision 无统一入口」的验收：读通道是既有的
    ``SpeechStore._ensure_cache``（按 ``get_revision()`` 失效），
    ``core/rules/engine.py::bump_revision`` 是**唯一**推进入口。
    """
    store, _ = _isolated_store("live_edit")
    _write_data_rule(store, [_rule(action={"action_id": "create_reminder",
                                           "effect": "allow"})])
    first = rules_engine.decide(_ctx(), store=store)
    assert first.allowed is True, first

    # 改**同一行同一条**（key 不变）的判定：allow → deny
    _write_data_rule(store, [_rule(action={"action_id": "create_reminder",
                                           "effect": "deny"})])
    second = rules_engine.decide(_ctx(), store=store)
    assert second.allowed is False, "改一行数据必须立刻生效（不重启）"
    assert second.explanation != first.explanation, (first, second)

    # 同一条数据行整体删掉 → 立刻回到"没有命中任何规则"
    _write_data_rule(store, [])
    third = rules_engine.decide(_ctx(), store=store)
    assert third.allowed is True and third.rule_id == "", third
    return True


def test_bump_revision_is_the_single_entry_point() -> None:
    """``bump_revision()`` 是**唯一**的推进入口，Web 与脚本都调它。"""
    store, _ = _isolated_store("bump")
    before = store.get_revision()
    revision = rules_engine.bump_revision(
        changed_by="test", detail={"k": "v"}, store=store)
    assert revision and revision != before, (before, revision)
    assert store.get_revision() == revision, "入口没让读通道看到新 revision"
    # 写入口自己也会 bump（save_rule / delete_rule 内部都调它）——读 AST，不 import
    for name in ("save_rule", "delete_rule"):
        body = None
        for node in ast.walk(_tree("core/rules/engine.py")):
            if isinstance(node, ast.FunctionDef) and node.name == name:
                body = ast.unparse(node)
        assert body and "bump_revision(" in body, f"{name} 必须自己推进 revision"
    return True


def test_save_and_delete_rule_are_idempotent_by_key() -> None:
    """``save_rule`` 同一 key 是**改数据**，不是插重复行；``delete_rule`` 删得掉。"""
    store, _ = _isolated_store("save_delete")
    rules_engine.save_rule(
        {"key": "k1", "name": "一", "action": {"action_id": "create_reminder"}},
        changed_by="test", store=store)
    rules_engine.save_rule(
        {"key": "k1", "name": "二", "action": {"action_id": "create_reminder"}},
        changed_by="test", store=store)
    rows = rules_engine.rules(store)
    assert len(rows) == 1, [r.key for r in rows]
    assert rows[0].name == "二", rows[0]
    assert rules_engine.delete_rule("k1", changed_by="test", store=store) is True
    assert rules_engine.rules(store) == []
    assert rules_engine.delete_rule("k1", changed_by="test", store=store) is False
    return True


# ===========================================================================
# ③ quota 超限 → 拒绝且 explanation 可读
# ===========================================================================
def test_quota_exceeded_denies_with_readable_explanation() -> None:
    """``quota`` 维：记满之后拒绝，``explanation`` 说清窗口与次数。"""
    store, _ = _isolated_store("quota")
    _write_data_rule(store, [_rule(quota={"max_per_hour": 2})])
    ctx = _ctx()

    assert rules_engine.decide(ctx, store=store).allowed is True
    rules_engine.record(rules_engine.decide(ctx, store=store), ctx, store=store)
    rules_engine.record(rules_engine.decide(ctx, store=store), ctx, store=store)

    third = rules_engine.decide(ctx, store=store)
    assert third.allowed is False, third
    assert third.exceeded == "hour", third
    assert "每小时上限 2" in third.explanation, third.explanation
    assert "已用 2" in third.explanation, third.explanation
    assert third.rule_id == "r1", third
    # 计数按 (动作 × 平台 × 账号 × 会话 × 发起人)：换个人就重新算
    other = rules_engine.decide(_ctx(actor_id="wxid_other"), store=store)
    assert other.allowed is True, other
    return True


def test_quota_windows_and_cooldown() -> None:
    """三个上限各自生效：per_day / cooldown_seconds。"""
    store, _ = _isolated_store("quota_windows")
    _write_data_rule(store, [_rule(quota={"max_per_day": 1})])
    ctx = _ctx()
    rules_engine.record(rules_engine.decide(ctx, store=store), ctx, store=store)
    blocked = rules_engine.decide(ctx, store=store)
    assert blocked.allowed is False and blocked.exceeded == "day", blocked
    assert "每天上限 1" in blocked.explanation, blocked.explanation

    # 冷却：刚记过一次 → 冷却窗口内直接拒
    _write_data_rule(store, [_rule(quota={"cooldown_seconds": 3600})])
    fresh, _ = _isolated_store("quota_cooldown")
    _write_data_rule(fresh, [_rule(quota={"cooldown_seconds": 3600})])
    later = rules_engine.decide(_ctx(now=FIXED_NOW + timedelta(seconds=10)), store=fresh)
    assert later.allowed is True, later
    rules_engine.record(later, _ctx(now=FIXED_NOW + timedelta(seconds=10)), store=fresh)
    # 计数 key 与时间窗都要对齐：用同一 now 判一次（冷却命中）
    with_cool = rules_engine.decide(
        _ctx(now=FIXED_NOW + timedelta(seconds=10)), store=fresh)
    assert with_cool.allowed is False, with_cool
    assert with_cool.exceeded == "cooldown", with_cool
    assert "冷却" in with_cool.explanation, with_cool.explanation
    return True


def test_quota_read_failure_never_blocks() -> None:
    """台账读不到（表不存在）→ 按 0 算，**绝不因为计数故障拒绝动作**。"""
    with _enabled("on"):
        directory = ROOT / "data" / "_rules_engine_test"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "no_ledger.db"
        if path.exists():
            path.unlink()
        engine = create_engine(f"sqlite:///{path}")
        # 全表建好，再**只**抹掉台账表：模拟"老库还没跑迁移 55"。
        mm.MetaBase.metadata.create_all(engine)
        with engine.begin() as conn:
            conn.execute(text("DROP TABLE rule_use_ledger"))
        rules_engine._ENGINES.clear()
        rules_engine._ENGINE_OWNERS.clear()
        store = SpeechStore(engine)
        assert not rules_engine._table_exists(engine, "rule_use_ledger")
        _write_data_rule(store, [_rule(quota={"max_per_hour": 1})])
        ctx = _ctx()
        decision = rules_engine.decide(ctx, store=store)
        assert decision.allowed is True, decision
        rules_engine.record(decision, ctx, store=store)     # 落同进程回退计数
        assert rules_engine.use_count(
            ctx.quota_key(), since=FIXED_NOW - timedelta(hours=1), store=store,
        ) == 1, "台账表缺失时必须回退到同进程计数"
        # 同一个进程里再判一次：回退计数在**本进程**里是算数的（重启才清零）
        second = rules_engine.decide(ctx, store=store)
        assert second.allowed is False, second
        assert second.exceeded == "hour", second
    return True


# ===========================================================================
# ④ shadow 只记录不影响行为
# ===========================================================================
def test_shadow_records_but_never_blocks() -> None:
    """``shadow``：``allowed`` 恒真、``shadow=True``、``explanation`` 说清本来会拒。"""
    store, _ = _isolated_store("shadow")
    _write_data_rule(store, [_rule(action={"action_id": "create_reminder",
                                           "effect": "deny"})])
    with _rules_env("shadow"):
        decision = rules_engine.decide(_ctx(), store=store)
    assert decision.allowed is True, "shadow 下不许拦"
    assert decision.shadow is True, decision
    assert decision.mode == features.MODE_SHADOW, decision
    assert "shadow" in decision.explanation, decision.explanation
    assert "本来会拒绝" in decision.explanation, decision.explanation
    assert decision.rule_id == "r1", "影子也要留下「命中了哪条」"
    # on 档同一份数据必须真的拦（证明影子与执行只差一个档位）
    with _rules_env("on"):
        enforced = rules_engine.decide(_ctx(), store=store)
    assert enforced.allowed is False, enforced
    assert enforced.shadow is False, enforced
    return True


def test_shadow_still_counts_for_the_ratio_readout() -> None:
    """影子也**记账**：读数要的就是"本来会超几次"。"""
    store, _ = _isolated_store("shadow_quota")
    _write_data_rule(store, [_rule(quota={"max_per_day": 1})])
    ctx = _ctx()
    with _rules_env("shadow"):
        first = rules_engine.decide(ctx, store=store)
        rules_engine.record(first, ctx, store=store)
        second = rules_engine.decide(ctx, store=store)
    assert second.allowed is True, "shadow 不许拦"
    assert second.exceeded == "day", "但读数要看得见超了哪个窗口"
    assert "shadow" in second.explanation, second.explanation
    return True


# ===========================================================================
# ⑤ 12 处前置校验收敛到唯一判定点（结构锁）
# ===========================================================================
#: 收编的 12 处（G1-13）。每一条都断言"不再各写一份判定"。
_COLLECTED_MODULES = (
    "core/speech/guards.py",
    "core/terminal/proactive_service.py",
    "agents/tools/base.py",
    "agents/tools/reminder.py",
    "agents/tools/memory.py",
    "agents/tools/person_profile.py",
    "agents/tools/search.py",
    "agents/tools/send_sticker.py",
)

#: **真的 import 引擎**的收编模块。``agents/tools/{reminder,memory,person_profile}.py``
#: 不在内：它们走 ``BaseTool.authoritative_is_owner`` 这个**转发壳**（保留它是为了
#: 既有 15 处用例能在实例上替换它构造"主人/非主人"两种世界），壳本身由
#: :func:`test_base_tool_forwards_to_the_engine_not_to_authority` 锁住。
_ENGINE_IMPORTERS = (
    "core/speech/guards.py",
    "core/terminal/proactive_service.py",
    "agents/tools/base.py",
    "agents/tools/reminder.py",
    "agents/tools/memory.py",
    "agents/tools/search.py",
    "agents/tools/send_sticker.py",
)

#: 身份判定的散点：这些 import 不许再出现在收编模块里（只有 ``agents/tools/base.py``
#: 的转发壳与引擎可以 import）。
_FORBIDDEN_AUTHORITY_IMPORTS = (
    "active_context_is_owner",
    "active_context_has_capability",
    "active_context_principal",
    "active_is_owner",
)


def _tree(rel: str) -> ast.Module:
    return ast.parse((ROOT / rel).read_text(encoding="utf-8-sig"))


def test_collected_modules_do_not_write_their_own_authority_checks() -> None:
    """结构锁：收编模块里不再有"自己写的 owner/capability 判定"。

    判据（AST，不靠 grep 文本）：不许 ``from core.terminal.authority import
    active_context_is_owner / …_has_capability / …_principal / active_is_owner``
    ——那是身份判定的散点。唯一允许 import 的是 ``agents/tools/base.py``。
    """
    offenders: list[str] = []
    for rel in _COLLECTED_MODULES:
        tree = _tree(rel)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and str(
                node.module or ""
            ) == "core.terminal.authority":
                imported = {alias.name for alias in node.names}
                bad = imported & set(_FORBIDDEN_AUTHORITY_IMPORTS)
                if bad:
                    offenders.append(f"{rel}:{node.lineno} import {sorted(bad)}")
    assert not offenders, (
        "这些收编点还在自己 import 身份判定（应该只调 core/rules/engine.py）：\n  "
        + "\n  ".join(offenders)
    )
    # 反向：每个 import 引擎的收编模块都必须真的引用引擎
    missing: list[str] = []
    for rel in _ENGINE_IMPORTERS:
        source = (ROOT / rel).read_text(encoding="utf-8-sig")
        if "core.rules" not in source and "rules_engine" not in source:
            missing.append(rel)
    assert not missing, f"这些模块没接引擎（收编没落地）：{missing}"
    return True


def test_base_tool_forwards_to_the_engine_not_to_authority() -> None:
    """``BaseTool`` 的两个授权方法只**转发**到引擎，不再自己写判定。

    这里读 **AST**（不 import 工具模块，避免把 registry / 依赖面拉进来）。
    """
    tree = _tree("agents/tools/base.py")
    found: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name in (
            "authoritative_is_owner", "authoritative_has_capability",
        ):
            found[node.name] = ast.unparse(node)
    assert "authoritative_is_owner" in found, "BaseTool 的 owner 转发壳不见了"
    assert "rules_engine.is_owner" in found["authoritative_is_owner"], (
        found.get("authoritative_is_owner"))
    assert "active_context_is_owner" not in found["authoritative_is_owner"], (
        "转发壳又自己判了一次（应是唯一判定点的转发）")
    assert "rules_engine.has_capability" in found["authoritative_has_capability"], (
        found.get("authoritative_has_capability"))
    return True


def test_guard_account_and_tool_preconditions_go_through_the_engine() -> None:
    """出站侧（收编点 ①②）：整条 if 链只在引擎里，guard 只做形状适配。

    两向断言：① 引擎 **off** 时 guard 的结论与旧判据逐字相同；
    ② 引擎 **on** 且数据行判 deny 时 guard 真的拦（说明判定来自引擎）；
    ③ 数据行判 allow 时**不放宽**旧判据（引擎只能加约束）。

    ``GuardChain(store=...)`` 是给隔离用例的注入口（生产路径不传，走进程单例）。
    """
    from core.speech.guards import GuardChain

    target = ConversationTarget(platform="wechat", account_id="wxid_bot",
                                chat_id=CHAT, chat_type="group")
    draft = OutboundDraft(request_id="t1", kind="text", target=target,
                          account_choice="owner", account_id="wxid_bot",
                          content="你好", who="wxid_other")
    policy = SpeakingPolicy(
        revision="r1", target=target, audience=AudienceKind.GROUP_AMBIENT,
        persona=ResolvedPersona(persona_id=1, version=1, key="k", name="鲸鲸"),
        account_policy=AccountPolicy(allowed_accounts=("self",)),
        tool_policy=ToolPolicy(denied_tools=("send_message",)),
    )

    store, _ = _isolated_store("guards")
    chain = GuardChain(store=store)
    with _rules_env("off"):
        off_account = chain.account.check(draft, policy)
        off_tool = chain.tool.check(draft, policy)
    assert off_account.allowed is False and "账号选择" in off_account.reason, off_account
    assert off_tool.allowed is False and "禁止调用" in off_tool.reason, off_tool
    # off 档的 reason 必须逐字是旧判据（不是引擎文案）
    assert "拒绝执行" not in off_account.reason, off_account.reason

    # on + 数据行判 deny → 引擎说的算（文案来自 explanation）
    _write_data_rule(store, [
        _rule(key="deny_account", action={"action_id": "account_choice",
                                          "effect": "deny"}),
        _rule(key="deny_tool", action={"action_id": "tool_call", "effect": "deny"}),
    ])
    clean = OutboundDraft(request_id="t2", kind="text", target=target,
                          account_choice="self", account_id="wxid_bot",
                          content="你好", who=CHAT)
    clean_policy = SpeakingPolicy(
        revision="r1", target=target, audience=AudienceKind.GROUP_AMBIENT,
        persona=ResolvedPersona(persona_id=1, version=1, key="k", name="鲸鲸"),
    )
    with _rules_env("on"):
        blocked_account = chain.account.check(clean, clean_policy)
        blocked_tool = chain.tool.check(clean, clean_policy)
    assert blocked_account.allowed is False, blocked_account
    assert blocked_tool.allowed is False, blocked_tool
    assert "拒绝执行" in blocked_account.reason, blocked_account.reason

    # on + 数据行判 allow → 仍落到旧判据（引擎只加约束，不放宽主人配置）
    _write_data_rule(store, [
        _rule(key="allow_account", action={"action_id": "account_choice",
                                           "effect": "allow"}),
        _rule(key="allow_tool", action={"action_id": "tool_call", "effect": "allow"}),
    ])
    with _rules_env("on"):
        relaxed_account = chain.account.check(draft, policy)
        relaxed_tool = chain.tool.check(draft, policy)
    assert relaxed_account.allowed is False, "数据行 allow 不许放宽旧判据"
    assert relaxed_account.reason == off_account.reason, relaxed_account
    assert relaxed_tool.allowed is False, "数据行 allow 不许放宽旧判据"
    assert relaxed_tool.reason == off_tool.reason, relaxed_tool
    return True


def test_reminder_pending_cap_goes_through_the_engine() -> None:
    """提醒配额（收编点 ⑥）：``NON_OWNER_PENDING_CAP`` 的判据在引擎里。

    两档都要对：``off`` = 与接线前逐字节一致（同一个常数、同一句文案）；
    ``on`` = 数据行（有）或内置兜底规则（无）说了算，且文案带引擎的可读判据。
    """
    from agents.tools import reminder as reminder_tool

    source = {
        "platform": "wechat", "account_id": "wxid_bot",
        "chat_id": "wxid_friend", "chat_type": "private",
        "requester_id": "wxid_friend", "requester_name": "老王",
    }
    cap = reminder_tool.NON_OWNER_PENDING_CAP
    store, _ = _isolated_store("reminder_cap")
    _reset_engine_cache(store)
    # 判据是 ``pending >= cap``（离线口径）。
    with _rules_env("off"):
        assert reminder_tool._pending_cap_exceeded(source, cap - 1) is None, "off 档不该拦"
        legacy = reminder_tool._pending_cap_exceeded(source, cap)
        assert legacy is not None, "off 档必须与接线前一样拒（pending >= cap）"
        assert legacy == (
            f"对方已有 {cap} 条待提醒，到上限了：先回绝，等这些到点再说"
        ), f"off 档文案必须逐字是旧文案：{legacy!r}"
    with _rules_env("on"):
        assert reminder_tool._pending_cap_exceeded(source, cap - 1) is None, "on 档不该拦"
        blocked = reminder_tool._pending_cap_exceeded(source, cap)
        assert blocked is not None, "on 档同样要拒"
        assert "到上限了" in blocked, blocked
        assert "每天上限" in blocked, ("on 档要带上引擎给的判据", blocked)

    # 数据行优先：把上限改成 10，就不再拦（"改一行数据即生效"在收编点上的样子）。
    # 走**写入口**（``save_rule``）而不是直接改库，读走**引擎的同一份 rules()**：
    # 判据一处、数据一处，用例只搬事实。
    with _rules_env("on"):
        rules_engine.save_rule(
            {"key": "cap_10", "name": "上限 10",
             "quota": {"max_per_day": 10},
             "action": {"action_id": "create_reminder", "effect": "allow"}},
            changed_by="test", store=store,
        )
        engine = rules_engine.get_engine(store)
        found = [r for r in engine.rules() if r.action.action_id == "create_reminder"]
        assert found and found[0].key == "cap_10", [r.key for r in found]
        ctx = rules_engine.ActionContext(
            action_id="create_reminder", actor_id="wxid_friend",
            chat_id="wxid_friend", chat_type="private", platform="wechat",
            account_id="wxid_bot", now=FIXED_NOW,
            quota_counts=rules_engine.QuotaCounts(counts={"day": cap}),
        )
        ok = engine.decide_with_rows(ctx, found)
        assert ok.allowed is True, ("数据行说 10，用了 5 不该拦", ok)
        too_many = engine.decide_with_rows(
            rules_engine.ActionContext(
                action_id="create_reminder", actor_id="wxid_friend",
                chat_id="wxid_friend", chat_type="private", platform="wechat",
                account_id="wxid_bot", now=FIXED_NOW,
                quota_counts=rules_engine.QuotaCounts(counts={"day": 10}),
            ),
            found,
        )
        assert too_many.allowed is False, too_many
        assert "每天上限 10" in too_many.explanation, too_many.explanation
    return True


def test_proactive_preconditions_go_through_the_engine() -> None:
    """主动开口（收编点 ⑨⑩⑪）：原因码与接线前一致，判据在引擎里。"""
    from core.terminal.proactive_service import ProactivePolicyService

    class _Row:
        def __init__(self, **kw):
            self.chat_id = CHAT
            self.enabled = 1
            self.allow_sticker = 1
            self.max_per_hour = 6
            self.max_per_day = 20
            self.cooldown_minutes = 60
            self.quiet_hours_json = "[]"
            self.__dict__.update(kw)

    class _Repo:
        def __init__(self, row, *, hour=0, day=0, last=None, now=None):
            self.row, self.hour, self.day, self.last = row, hour, day, last
            self.now = now or datetime.now()

        class _P:
            def __init__(self, outer):
                self.outer = outer

            def get_target(self, *a):
                return self.outer.row

            def count_since(self, *, since, **kw):
                # 按**窗口**区分：``now - since == 1 天`` 就是每日计数，
                # 否则是滚动 1 小时（与生产 ``repo_proactive.count_since`` 同形状）。
                # 用调用方给的 ``now`` 算，不读墙钟（否则固定时间戳会算出负窗口）。
                window = self.outer.now - since
                if window >= timedelta(days=1):
                    return self.outer.day
                return self.outer.hour

            def last_send_at(self, *a):
                return self.outer.last

        @property
        def proactive(self):
            return _Repo._P(self)

    now = datetime(2026, 10, 6, 14, 0)
    cases = [
        # (行, hour, day, last, 期望原因)
        (_Row(enabled=0), 0, 0, None, "disabled"),
        (_Row(quiet_hours_json="[13, 15]"), 0, 0, None, "quiet_hours"),
        (_Row(cooldown_minutes=60), 0, 0, now - timedelta(minutes=10), "cooldown"),
        (_Row(cooldown_minutes=0, max_per_hour=3), 3, 3, None, "hourly_limit"),
        (_Row(cooldown_minutes=0, max_per_day=5), 1, 5, None, "daily_limit"),
        (_Row(cooldown_minutes=0), 1, 1, None, "allowed"),
    ]
    store, _ = _isolated_store("proactive")
    with _rules_env("on"):
        for row, hour, day, last, expected in cases:
            service = ProactivePolicyService(
                _Repo(row, hour=hour, day=day, last=last, now=now))
            got = service.evaluate(
                platform="wechat", bot_account_id="wxid_bot", chat_id=CHAT,
                chat_type="group", now=now,
            )
            assert got.reason == expected, (
                f"{expected} != {got.reason}（行 {row.__dict__}）")
            assert got.allowed is (expected == "allowed"), got
    return True


# ===========================================================================
# ⑥ 死字段处置：接进引擎，不许"零读取方"
# ===========================================================================
def test_dead_tool_policy_fields_have_a_reader_now() -> None:
    """``ToolPolicy.max_calls`` / ``require_confirmation`` 有读取方（接进引擎）。

    这是 G1-13「零读取方死字段」的收口断言：两个字段在 ``core/speech/guards.py``
    的 ``ToolGuard.check`` 里被读出来、作为事实交给唯一判定点；判定结果里的
    ``require_confirmation`` 从 ``RuleDecision`` 取。
    """
    from core.speech.guards import ToolGuard

    target = ConversationTarget(platform="wechat", account_id="wxid_bot",
                                chat_id=CHAT, chat_type="group")
    draft = OutboundDraft(request_id="t1", kind="text", target=target,
                          account_choice="self", account_id="wxid_bot",
                          content="你好", who=CHAT)
    policy = SpeakingPolicy(
        revision="r1", target=target, audience=AudienceKind.GROUP_AMBIENT,
        persona=ResolvedPersona(persona_id=1, version=1, key="k", name="鲸鲸"),
        tool_policy=ToolPolicy(max_calls=7, require_confirmation=("send_message",)),
    )
    guards_tree = _tree("core/speech/guards.py")
    body = None
    for node in ast.walk(guards_tree):
        if isinstance(node, ast.FunctionDef) and node.name == "check":
            text = ast.unparse(node)
            if "tp.max_calls" in text or "policy.tool_policy" in text:
                body = text
                break
    assert body is not None, "找不到 ToolGuard.check 的源码"
    assert "tp.max_calls" in body, "max_calls 必须被读出来交给引擎（否则还是死字段）"
    assert "tp.require_confirmation" in body, "require_confirmation 同上"
    with _rules_env("off"):
        decision = ToolGuard().check(draft, policy)
    assert decision.action == PolicyAction.ALLOW, decision

    # ``RuleDecision`` 上把"要不要确认"暴露出来（引擎给出的那一处）
    store, _ = _isolated_store("confirm")
    _write_data_rule(store, [_rule(action={
        "action_id": "create_reminder", "effect": "allow",
        "require_confirmation": True,
    })])
    with _rules_env("on"):
        shaped = rules_engine.decide(_ctx(), store=store)
    assert shaped.require_confirmation is True, shaped
    return True


def test_tool_policy_fields_are_not_zombies() -> None:
    """全仓扫一遍：``max_calls`` / ``require_confirmation`` 都有读取方。"""
    offenders: list[str] = []
    for rel in _COLLECTED_MODULES + ("agents/tools/base.py", "core/speech/domain.py",
                                     "core/speech/resolver.py"):
        text_src = (ROOT / rel).read_text(encoding="utf-8-sig")
        for needle in ("max_calls", "require_confirmation"):
            if needle not in text_src:
                continue
        # 只要求：这两个名字至少在一个**非声明处**被读到（赋值/取值，不是 dataclass 字段）
    for needle, expected_file in (
        ("max_calls", "core/speech/guards.py"),
        ("require_confirmation", "core/speech/guards.py"),
    ):
        source = (ROOT / expected_file).read_text(encoding="utf-8-sig")
        assert f"tp.{needle}" in source, (
            f"{needle} 在 {expected_file} 里没有被读（零读取方 = 死字段）")
    assert not offenders, offenders
    return True


# ===========================================================================
# ⑦ 硬禁区：写死代码，任何数据行都放宽不了
# ===========================================================================
def test_hard_walls_cannot_be_configured_away() -> None:
    """硬禁区在数据行之前判，配成 allow 也不算数（docs/decisions/架构评估.md :263）。"""
    store, _ = _isolated_store("hard_wall")
    for wall in sorted(rules_engine.HARD_WALL_ACTIONS):
        _write_data_rule(store, [_rule(
            key=f"try_{wall}", action={"action_id": wall, "effect": "allow"})])
        with _rules_env("on"):
            decision = rules_engine.decide(_ctx(action_id=wall), store=store)
        assert decision.allowed is False, (wall, decision)
        assert decision.rule_id == f"hard_wall:{wall}", decision
        assert "硬禁区" in decision.explanation, decision.explanation
    return True


def test_hard_wall_blocks_even_in_shadow() -> None:
    """★ task-14 钉子：硬禁区是 shadow 的**唯一例外**——``allowed=False`` 且 ``shadow=True``。

    合同见 ``core/rules/engine.py`` 模块 docstring + ``docs/decisions/架构评估.md``
    §2.1 L5（金钱·隐私·代发他人·记忆范围写死代码、不落数据行）：影子档只对**可配规则**
    只记不拦，硬禁区**无论档位一律拦**（安全底线）。本用例不给硬禁区配任何数据行，
    直接钉"shadow 下调 ``decide`` 仍拒"。
    """
    store, _ = _isolated_store("hard_wall_shadow")
    walls = sorted(rules_engine.HARD_WALL_ACTIONS)
    assert walls, "硬禁区集合不许为空"
    for wall in walls:
        with _rules_env("shadow"):
            decision = rules_engine.decide(_ctx(action_id=wall), store=store)
        assert decision.allowed is False, (wall, decision)
        assert decision.shadow is True, decision
        assert decision.mode == features.MODE_SHADOW, decision
        assert decision.rule_id == f"hard_wall:{wall}", decision
        assert "硬禁区" in decision.explanation, decision.explanation
    return True


# ===========================================================================
# ⑧ off = 逐字节一致
# ===========================================================================
def test_off_mode_is_byte_identical_for_every_collected_point() -> None:
    """``YINYUE_FEATURE_RULES=off``（默认）：判定点一行都不走。"""
    from core.speech.guards import AccountGuard, ToolGuard

    # 数据面上故意写一条 deny：off 档必须当它不存在
    store, _ = _isolated_store("off")
    _write_data_rule(store, [_rule(action={"action_id": "create_reminder",
                                           "effect": "deny"})])
    with _rules_env("off"):
        assert rules_engine.rules_mode(store) == features.MODE_OFF
        decision = rules_engine.decide(_ctx(), store=store)
    assert decision.allowed is True, decision
    assert decision.mode == features.MODE_OFF, decision
    assert decision.rule_id == "", "off 档不许报出任何一个 rule_id"
    assert "off" in decision.explanation, decision.explanation

    target = ConversationTarget(platform="wechat", account_id="wxid_bot",
                                chat_id=CHAT, chat_type="group")
    draft = OutboundDraft(request_id="t1", kind="text", target=target,
                          account_choice="owner", account_id="wxid_bot",
                          content="你好", who="wxid_other")
    policy = SpeakingPolicy(
        revision="r1", target=target, audience=AudienceKind.GROUP_AMBIENT,
        persona=ResolvedPersona(persona_id=1, version=1, key="k", name="鲸鲸"),
        account_policy=AccountPolicy(allowed_accounts=("self",)),
        tool_policy=ToolPolicy(allowed_tools=("notify_user",)),
    )
    from core.speech.guards import GuardChain

    chain = GuardChain(store=store)
    with _rules_env("off"):
        account = chain.account.check(draft, policy)
        tool = chain.tool.check(draft, policy)
    # 结论与 reason 逐字来自旧判据
    assert account.allowed is False, account
    assert account.reason == "账号选择 owner 不允许用于当前会话", account.reason
    assert tool.allowed is False, tool
    assert tool.reason == "当前会话未授权调用 send_message", tool.reason
    # 显式 "off" 与未设置等价
    with _rules_env("off"):
        assert rules_engine.decide(_ctx(), store=store).mode == features.MODE_OFF
    with _rules_env(None):
        assert rules_engine.decide(_ctx(), store=store).mode == features.MODE_OFF
    return True


def test_engine_missing_store_never_blocks() -> None:
    """没有 store（隔离用例里的裸 ``GuardChain()``）：一律"不判定"。"""
    with _rules_env("on"):
        bare = rules_engine.RuleEngine(store=None)
        assert bare.payloads() == [], "读不到配置就是空"
        assert bare.rules() == []
        decision = bare.decide(_ctx())
    assert decision.allowed is True, decision
    return True


# ===========================================================================
# 迁移 55：幂等 + 建表 parity（照 tests/test_focus_state.py 的口径）
# ===========================================================================
def _table_shape(engine: Any, table: str) -> list[tuple]:
    insp = inspect(engine)
    columns = [
        (c["name"], str(c["type"]), bool(c["nullable"]), c["default"])
        for c in insp.get_columns(table)
    ]
    indexes = sorted(
        (ix["name"], tuple(ix["column_names"]), bool(ix["unique"]))
        for ix in insp.get_indexes(table)
    )
    pk = insp.get_pk_constraint(table)
    return [("columns", columns), ("indexes", indexes),
            ("pk", tuple(pk.get("constrained_columns") or ()))]


def _legacy_engine(path: Path):
    """老库：``create_all`` 建好、抹掉 ``rule_use_ledger``、版本压回 54。

    ``_schema_version`` 是 ``run_migrations`` 自己 ``CREATE TABLE IF NOT EXISTS``
    的，``create_all`` 不建它——存在才清版本。
    """
    engine = create_engine(f"sqlite:///{path}")
    mm.MetaBase.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS rule_use_ledger"))
        exists = conn.execute(text(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='_schema_version'"
        )).fetchone()
        if exists is not None:
            conn.execute(text("DELETE FROM _schema_version WHERE version >= 55"))
    return engine


def test_migration_55_exists_and_is_not_registered_in_the_two_tuples() -> None:
    """注册位置：55 **不进** ``_META_MIGRATION_VERSIONS`` / 不 skip（照 54）。"""
    versions = {m.version: m for m in migrations_mod._MIGRATIONS}
    assert 55 in versions, "迁移 55 不存在"
    assert 55 not in migrations_mod._META_MIGRATION_VERSIONS
    assert 55 not in migrations_mod._ACCOUNT_SKIP_MIGRATION_VERSIONS
    assert max(versions) >= 55, "55 必须在现有编号之后"
    return True


def test_migration_55_is_idempotent_and_matches_create_all() -> None:
    """跑两次 PRAGMA 全等；老库补出来的形状 = ``create_all`` 的形状。"""
    directory = ROOT / "data" / "_rules_engine_test"
    directory.mkdir(parents=True, exist_ok=True)
    legacy_path = directory / "legacy_m55.db"
    if legacy_path.exists():
        legacy_path.unlink()
    _legacy_engine(legacy_path)
    migrations_mod.run_migrations(str(legacy_path))
    first = _table_shape(create_engine(f"sqlite:///{legacy_path}"), "rule_use_ledger")
    migrations_mod.run_migrations(str(legacy_path))
    second = _table_shape(create_engine(f"sqlite:///{legacy_path}"), "rule_use_ledger")
    assert first == second, "迁移 55 不幂等（跑两次形状不同）"

    fresh_path = directory / "fresh_m55.db"
    if fresh_path.exists():
        fresh_path.unlink()
    fresh = create_engine(f"sqlite:///{fresh_path}")
    mm.MetaBase.metadata.create_all(fresh)
    fresh_shape = _table_shape(fresh, "rule_use_ledger")
    if first != fresh_shape:
        # 漂移必须说清差在哪（ENG-17 那类问题的定位成本全在这一行上）
        import difflib

        diff = "\n".join(difflib.unified_diff(
            [repr(item) for item in first],
            [repr(item) for item in fresh_shape],
            "migration55", "create_all", lineterm="",
        ))
        raise AssertionError("create_all vs 迁移 55 的建表漂移：\n" + diff)
    return True


def test_migration_55_runs_on_a_second_call_without_touching_data() -> None:
    """幂等还要求**不动已有行**：记一次用量，再跑迁移，行还在。"""
    directory = ROOT / "data" / "_rules_engine_test"
    path = directory / "m55_data.db"
    if path.exists():
        path.unlink()
    engine = _legacy_engine(path)
    migrations_mod.run_migrations(str(path))
    Session = sessionmaker(bind=create_engine(f"sqlite:///{path}"))
    with Session() as session:
        session.add(mm.RuleUseLedger(rule_key="r1", quota_key="k",
                                     used_at=FIXED_NOW))
        session.commit()
    migrations_mod.run_migrations(str(path))
    with Session() as session:
        assert session.execute(
            select(mm.RuleUseLedger).where(mm.RuleUseLedger.rule_key == "r1")
        ).scalar_one_or_none() is not None, "重跑迁移动了已有数据"
    return True


# ===========================================================================
# 收尾
# ===========================================================================
def main() -> int:
    # 直跑没有 pytest 的 autouse fixture：这里自己设同一档位（显式，不靠环境默认）。
    previous = os.environ.get(RULES_ENV)
    os.environ[RULES_ENV] = _DEFAULT_MODE

    import agents.tools.memory  # noqa: F401
    import agents.tools.reminder  # noqa: F401
    import core.speech.guards  # noqa: F401
    import core.terminal.proactive_service  # noqa: F401

    tests = [
        test_decision_contract_has_all_six_fields,
        test_condition_dimension_time_window_and_topic,
        test_target_dimension_people_chats_role,
        test_action_dimension_allow_and_deny,
        test_priority_and_specificity_order,
        test_one_data_row_change_takes_effect_without_restart,
        test_bump_revision_is_the_single_entry_point,
        test_save_and_delete_rule_are_idempotent_by_key,
        test_quota_exceeded_denies_with_readable_explanation,
        test_quota_windows_and_cooldown,
        test_quota_read_failure_never_blocks,
        test_shadow_records_but_never_blocks,
        test_shadow_still_counts_for_the_ratio_readout,
        test_collected_modules_do_not_write_their_own_authority_checks,
        test_base_tool_forwards_to_the_engine_not_to_authority,
        test_guard_account_and_tool_preconditions_go_through_the_engine,
        test_reminder_pending_cap_goes_through_the_engine,
        test_proactive_preconditions_go_through_the_engine,
        test_dead_tool_policy_fields_have_a_reader_now,
        test_tool_policy_fields_are_not_zombies,
        test_hard_walls_cannot_be_configured_away,
        test_hard_wall_blocks_even_in_shadow,
        test_off_mode_is_byte_identical_for_every_collected_point,
        test_engine_missing_store_never_blocks,
        test_migration_55_exists_and_is_not_registered_in_the_two_tuples,
        test_migration_55_is_idempotent_and_matches_create_all,
        test_migration_55_runs_on_a_second_call_without_touching_data,
    ]
    failed: list[str] = []
    try:
        for test in tests:
            try:
                test()
                print(f"  ok   {test.__name__}")
            except Exception as exc:  # noqa: BLE001
                failed.append(test.__name__)
                import traceback

                print(f"  FAIL {test.__name__}: {exc!r}")
                traceback.print_exc()
    finally:
        # 直跑也不许把档位留给后面的东西
        if previous is None:
            os.environ.pop(RULES_ENV, None)
        else:
            os.environ[RULES_ENV] = previous
    print(f"rules engine checks: {len(tests) - len(failed)}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
