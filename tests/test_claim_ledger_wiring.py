# -*- coding: utf-8 -*-
"""M2-4 claim 台账**接线**（`docs/archive/任务清单_2026-10-06.md:71`）：台账在**真实出站路径**上生效。

M2-4 交付了台账本体（``core/context/message_facts.py``）与闸门接缝
（``core/speech/guards.py::claim_ledger_inputs`` / ``_claim_ledger_hits``），但
**零生产调用点**——台账是死的。本文件守的就是那一格：公网出口
``OutboundGateway.send_text`` 走完整链路时，台账结论必须真的参与判定。

接线点（两处，都在 ``core/speech/outbound.py``）：

* （已退役）``_gate_self_text_window``：原先是"去重前的原始窗口"，供台账复述读数与
  闸门指纹读数共用一份列表。2026-10-24 复读指纹与复述抑制整族退役（A6），窗口与
  进程内环形缓冲一并删除，本条只留档；
* ``_gate_person_keys``：本回合"该被判定的人"= 发言人规范 id
  （``ConversationTarget.sender_id``，不是昵称）；定不出人就空元组 →
  台账按"归属未知，无支撑"判（保守方向）；
* ``_stage_claim_ledger``：唯一接缝调用点（读库只在这里）→ 把结论折进
  ``GateConfig`` 交给 ``run_output_gate``。

三档读数（本文件 ①②③ 三条主用例）：

* ``on``     → 无支撑断言**降级**：``applied_text != 原文``，发出去的是「没把握」短句；
* ``shadow`` → **原文不变**（逐字节发出）但留痕：审计里带
  ``hits=unfounded_claim`` / ``gate:unfounded_claim``；
* ``off``    → **与现状逐字节一致**：连接缝都不进（不 import 台账、不查库、不构造
  台账对象）——用"接缝被调用就打红"的桩实测，不靠读码。

活体 fixture 用 18:01 那一轮的真实形态（"一天五块三"归给主人、本轮无任何支撑）。

**测试卫生**：断言"关"一律显式 ``off``（"env 未设"≠"关"：import ``core.*`` 时
``init_singletons`` 会把生产 `.env` 的值填进来），且本文件依赖的**每一个**开关都在
import 期钉死：``CLAIM_LEDGER`` × ``OUTPUT_GATE``（兄弟组，钉一个就得钉整组）。

Run directly::

    .\\.venv\\Scripts\\python.exe tests\\test_claim_ledger_wiring.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import traceback
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

ROOT = Path(__file__).resolve().parent.parent

# --- import 期钉死：本文件依赖的每一个开关都显式表态（"env 未设"≠"关"）--------
os.environ["YINYUE_FEATURE_OUTPUT_GATE"] = "off"
os.environ["YINYUE_FEATURE_CLAIM_LEDGER"] = "off"
# 出站闸门只剩 OUTPUT_GATE（ADDRESSEE_GUARD 随 R0 删门移除）；钉死，别让生产
# `.env` 的 shadow 默认值改写"关 = 旧行为"的断言。
# 隔离：库 / 日志 / 平台目录全部改道（本文件用内存库 + 注入式台账，绝不碰生产库）。
_ISO_ROOT = (ROOT / "data" / "_claim_ledger_wiring_test").resolve()
_ISO_ROOT.mkdir(parents=True, exist_ok=True)
# 2026-11-04：**账本流水也必须隔离**。这个文件原来只钉了三个 env（META_DB_PATH /
# PLATFORMS_DIR / DATA_DIR），回执流水仍写进生产 data/loop/ledger-<date>.jsonl ——
# 于是每跑一次就替她「说」一遍「同学你好，关于报到这件事我再说一遍…」，
# 她下一拍把这行当成自己的记忆（E-54）。这里把流水目录也钉进 _ISO_ROOT。
os.environ["META_DB_PATH"] = str(_ISO_ROOT / "meta.db")
os.environ["PLATFORMS_DIR"] = str(_ISO_ROOT / "platforms")
os.environ["DATA_DIR"] = str(_ISO_ROOT / "data")
os.environ["LOG_DIR"] = str(ROOT / "data" / "_test_logs_claim_ledger_wiring")

from core import features  # noqa: E402
from core.context import message_facts as facts  # noqa: E402
from core.speech import guards  # noqa: E402
from core.speech.domain import OutboundDraft  # noqa: E402
from core.speech.guards import (  # noqa: E402
    GATE_MODE_ON,
    GATE_MODE_OFF,
    GATE_MODE_SHADOW,
    GATE_OBSERVE_RULES,
    GATE_SCOPE_OBSERVE,
    GATE_SEVERITY_BLOCK,
    OBSERVE_SCOPE,
)
from core.speech.outbound import OutboundGateway  # noqa: E402
from core.speech.store import SpeechStore  # noqa: E402

from core.infra import actions as _actions  # noqa: E402

_actions.DIR = str(_ISO_ROOT / "loop")
from db.meta_models import MetaBase  # noqa: E402

DEFAULT_CHAT = "wiring_10000000001@chatroom"
#: 18:01:32 的活体样本（`tests/test_claim_ledger.py:94` 同一句）：她收到一个"拍"，
#: 没有检索、没有上一句、台账里什么都没有 → 必须判"无 claim 支撑"。
LIVE_1801 = "不过一天五块三的吃法你是真敢，@Elaina"
#: 21:45:20 的形态（值冲突）：台账里有权威自报「湖南长沙」，她说「广东」。
WRONG_2145 = "P3 你是广东人吧，刚才报的籍贯里没有你。"
SELFREPORT = "我是湖南长沙的。"
ANAHIDA = "wxid_seed03"


# ---------------------------------------------------------------------------
# 隔离设施（照 tests/test_output_gate.py ⑧，同一套口径）
# ---------------------------------------------------------------------------
def _pin(*, ledger: str = "off") -> None:
    os.environ["YINYUE_FEATURE_CLAIM_LEDGER"] = str(ledger)
    os.environ["YINYUE_FEATURE_OUTPUT_GATE"] = "off"


@contextmanager
def _env(**pairs):
    """临时设置三态开关（"off"/"shadow"/"on"），退出时连"未设置"状态一起还原。"""
    saved = {key: os.environ.get(key) for key in pairs}
    try:
        for key, value in pairs.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = str(value)
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _block_account_db_resolution() -> None:
    """让闸门读不到真实账号库（读不到 = 历史为空，绝不碰生产 send_log）。"""
    import core.bootstrap as bootstrap

    bootstrap.get_account_db_manager = lambda: None


def _isolated_gateway():
    """内存库上的 OutboundGateway：真实 ``send_text`` 全链路，不碰生产库。"""
    from sqlalchemy import create_engine

    _block_account_db_resolution()
    engine = create_engine("sqlite+pysqlite:///:memory:")
    MetaBase.metadata.create_all(engine)
    store = SpeechStore(engine)
    return OutboundGateway(store), store


def _fake_snapshot(chat_type: str = "group", chat_id: str = DEFAULT_CHAT,
                   sender_id: str = ANAHIDA):
    """最小但**类型正确**的 SpeechSnapshot（``sender_id`` = 台账 person_key 的来源）。"""
    from core.speech.domain import (
        AudienceKind,
        ConversationTarget,
        ResolvedPersona,
        SpeakingPolicy,
        SpeechSnapshot,
        TurnContext,
    )

    target = ConversationTarget(
        platform="wechat", account_id="wxid_bot", chat_id=chat_id,
        chat_type="group" if chat_type == "group" else "private",
        sender_id=sender_id, sender_name="P3",
    )
    turn = TurnContext(
        request_id="req-wiring", target=target,
        audience=AudienceKind.GROUP_MENTIONED, is_mentioned=True,
        trigger_reason="mention", msg_type="text",
    )
    persona = ResolvedPersona(persona_id=1, version=1, key="yinyue", name="鲸鲸")
    policy = SpeakingPolicy(
        revision="rev-1", target=target, audience=AudienceKind.GROUP_MENTIONED,
        persona=persona,
    )
    return SpeechSnapshot(turn=turn, audience=AudienceKind.GROUP_MENTIONED,
                          persona=persona, policy=policy)


def _fake_provider():
    sent: list[str] = []

    async def _send(target, content, filepath=None):
        sent.append(content)
        return SimpleNamespace(ok=True, msg_id="m1", error="")

    return SimpleNamespace(send=_send), sent


def _audit_rows(store):
    from sqlalchemy import select

    from db.meta_models import OutboundAudit

    with store._Session() as session:
        return list(session.execute(
            select(OutboundAudit).order_by(OutboundAudit.id)
        ).scalars().all())


def _records(items) -> facts.ClaimLedger:
    """注入式台账（读侧用例：不碰库、不依赖迁移）。"""
    rows = []
    for index, row in enumerate(items, start=1):
        rows.append(facts.ClaimRecord(
            id=index, platform="wechat", account_id="wxid_bot",
            chat_id=row.get("chat_id", DEFAULT_CHAT), person_key=row["person"],
            field_key=row["field"], value=row["value"],
            authority=row["authority"],
            source_actor_id=row.get("actor", row["person"]),
            created_at=row.get("at", "2026-09-30 21:44:59"),
        ))
    return facts.ClaimLedger(rows)


def _send(text: str, *, ledger_mode: str, gate_mode: str = GATE_MODE_ON,
          chat_id: str = DEFAULT_CHAT, roster=None, sender_id: str = ANAHIDA):
    """真实 ``gateway.send_text``；``roster`` 非空时把它当本次读到的台账（唯一 IO 点）。

    返回 ``(结果, 实际发出的文本, 审计行)``。
    """
    import core.speech.guards as guards_mod

    saved = guards_mod.claim_ledger_inputs
    if roster is not None:
        real = saved

        def _seam(content, *, chat_id="", person_keys=(), config=None):
            from core.context import message_facts as _facts
            from core.speech.guards import GateConfig
            import dataclasses as _dc

            base = config or GateConfig()
            ledger_mode = features.mode(features.CLAIM_LEDGER,
                                        features.MODE_OFF)
            if ledger_mode == features.MODE_OFF:
                return base
            probe = _facts.evaluate_claims(
                content, chat_id=str(chat_id or ""), person_keys=person_keys,
                ledger=roster, recent_keys=(),
            )
            unsupported = tuple({
                "field": verdict.field_key,
                "value": verdict.assertion.value,
                "evidence": verdict.assertion.evidence,
                "reason": verdict.reason,
            } for verdict in probe.unsupported)
            return _dc.replace(
                base, unsupported_claims=unsupported,
                # 与真接缝同形：只有台账 on 才给处置许可（2026-10-10 修）。
                claim_ledger_enforce=(ledger_mode == features.MODE_ON),
            )

        guards_mod.claim_ledger_inputs = _seam
    with _env(YINYUE_FEATURE_CLAIM_LEDGER=ledger_mode,
              YINYUE_FEATURE_OUTPUT_GATE=gate_mode):
        # 全程待在 `_env` 里：闸门与台账的三态是**每次读环境**的（同
        # core.features.mode 的口径），跑到 `with` 之外再发就会读到还原后的值——
        # 那正是"影子门悄悄变成不做事"的现成通道（已踩过一次）。
        try:
            gateway, store = _isolated_gateway()
            provider, sent = _fake_provider()
            gateway._resolve_provider = lambda platform, account_id="": provider
            snapshot = _fake_snapshot("group", chat_id=chat_id, sender_id=sender_id)
            draft = OutboundDraft(
                request_id="req-wiring", kind="text", target=snapshot.turn.target,
                content=text, who=snapshot.turn.target.chat_id,
            )
            result = asyncio.run(gateway.send_text(draft, snapshot, None))
            rows = _audit_rows(store)
        finally:
            guards_mod.claim_ledger_inputs = saved
        return result, (sent[0] if sent else None), rows


# ---------------------------------------------------------------------------
# ① 端到端 · on → 无支撑断言被降级（文本真的变了）
# ---------------------------------------------------------------------------




def test_wiring_off_never_touches_the_ledger_seam():
    """``off``：**不查库、不构造台账对象**——用"接缝被调用就打红"的桩实测。"""
    _pin()
    import core.speech.guards as guards_mod

    calls: list[str] = []

    def _boom(*_args, **_kwargs):  # pragma: no cover - off 下不该被调到
        calls.append("seam")
        raise AssertionError("off 下闸门不许进 claim 台账接缝")

    saved = guards_mod.claim_ledger_inputs
    guards_mod.claim_ledger_inputs = _boom
    try:
        result, sent_text, rows = _send(
            LIVE_1801, ledger_mode="off", gate_mode=GATE_MODE_OFF, roster=None)
    finally:
        guards_mod.claim_ledger_inputs = saved

    assert calls == [], calls
    assert result.ok is True, result
    assert sent_text == LIVE_1801, repr(sent_text)
    assert len(rows) == 1 and rows[0].decision == "allow", rows[0].decision
    preview = rows[0].content_preview or ""
    assert "gate:" not in (rows[0].rule_ids_json or ""), rows[0].rule_ids_json
    assert "hits=" not in preview, f"off 的闸门不该产生任何 finding：{preview}"
    print("  ok ③ off：接缝零调用、原文逐字节、零 finding（闸门不判定）")


def test_wiring_off_behavior_is_identical_to_pre_wiring():
    """``off`` 与"手工构造的 GateConfig()"逐字段相同（回滚位的硬读数）。"""
    _pin()
    with _env(YINYUE_FEATURE_CLAIM_LEDGER="off"):
        gateway, _store = _isolated_gateway()
        snapshot = _fake_snapshot()
        draft = OutboundDraft(
            request_id="req-wiring", kind="text", target=snapshot.turn.target,
            content=LIVE_1801, who=snapshot.turn.target.chat_id,
        )
        staged = gateway._stage_claim_ledger(draft, snapshot)
        config = staged[0]
        from core.speech.guards import GateConfig

        assert config == GateConfig(), config
        assert config.unsupported_claims == ()
        # 2026-10-24 A6：自述窗口已随复读指纹退役，接线只剩四元组本身
        assert len(staged) == 4

        # 台账关着时，同一份输入两次判定逐字段相等（纯函数语义没被接线破坏）
        from core.speech.guards import run_output_gate

        baseline = run_output_gate(LIVE_1801, chat_type="group",
                                   chat_id=DEFAULT_CHAT, config=GateConfig(),
                                   mode=GATE_MODE_ON)
        wired = run_output_gate(LIVE_1801, chat_type="group",
                                chat_id=DEFAULT_CHAT, config=config,
                                mode=GATE_MODE_ON)
        assert wired == baseline, (wired, baseline)
        assert wired.applied_text == LIVE_1801
    print("  ok ③ off：config 与 GateConfig() 全等、判定与接线前逐字段一致")


# ---------------------------------------------------------------------------
# ④ 保守方向：没有台账行 / 定不出人 → 按"无支撑"处理
# ---------------------------------------------------------------------------




def test_observation_scope_constants_are_untouched():
    """``GATE_OBSERVE_RULES`` / ``OBSERVE_SCOPE`` 与接线前逐字相同。"""
    _pin()
    assert GATE_OBSERVE_RULES == ("group_max_chars", "unfounded_claim")
    assert OBSERVE_SCOPE == GATE_SCOPE_OBSERVE
    # 2026-10-11 更新：4be8e6c（主人裁定）新增隐私硬禁区 R-V5「结构化名单泄漏」，
    # 把 roster_leak 同步加入 GATE_RULES / GATE_ENFORCE_RULES（真 rewrite，见
    # guards.py:1852-1869；依据 532 条真实回复中 40 条命中名单形态）。本锁只管
    # **claim-ledger 接线那一批**不许夹带新规则，后续经独立裁定的规则按事实更新。
    assert guards.GATE_ENFORCE_RULES == ("roster_leak",), \
        "接线不许把新规则塞进拦截项清单（独立裁定新增的规则见 4be8e6c，不属接线夹带）"
    for rule in ("group_max_chars", "unfounded_claim"):
        assert guards.gate_scope(rule) == GATE_SCOPE_OBSERVE, rule
    print("  ok ⑤ 结构锁：OBSERVE_RULES / OBSERVE_SCOPE 一字不动；"
          "拦截项 = 接线项 + 独立裁定新增的 roster_leak")


def test_gateway_does_not_widen_the_gate():
    """接线只加"读参数"：网关仍不读模型、不放宽观测项、不新增文本变换路径。"""
    _pin()
    import ast

    import inspect

    from core.speech.outbound import OutboundGateway

    # 联网/模型路径不许出现在接线点上（台账是纯库读）
    for required in ("_gate_person_keys", "_stage_claim_ledger"):
        assert hasattr(OutboundGateway, required), f"接线点缺失：{required}"
    stage_src = inspect.getsource(OutboundGateway._stage_claim_ledger)
    assert stage_src.count("claim_ledger_inputs(") == 1, \
        "唯一接缝必须在网关处被调用（且只调用一次）"
    # run_output_gate 仍是纯函数：闸门模块不许从台账模块 import 读库路径进判定体
    gate_source = (ROOT / "core" / "speech" / "guards.py").read_text(
        encoding="utf-8-sig")
    assert "def run_output_gate(" in gate_source
    assert "ClaimLedger(" not in gate_source, "闸门不许自己构造台账（IO 只在该模块外）"
    print("  ok ⑤ 网关只加读取参数（3 个接线点），闸门仍是纯函数")


# ---------------------------------------------------------------------------
# ⑥ 变异自检（红信号锁进用例里）：接线去掉 → 端到端**不降级**
# ---------------------------------------------------------------------------
def main() -> bool:
    _pin()
    tests = [fn for name, fn in sorted(globals().items()) if name.startswith("test_")]
    failed = 0
    print("=" * 68)
    print("M2-4 claim 台账**接线**（真实出站路径 / on 降级 · shadow 留痕 · off 逐字节）")
    print("=" * 68)
    for fn in tests:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception:
            failed += 1
            print(f"FAIL {fn.__name__}")
            traceback.print_exc()
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return failed == 0


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
