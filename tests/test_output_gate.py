# -*- coding: utf-8 -*-
"""输出闸门（MVP-2）回归：**两项机械检查 + 一名单拦截 + 沉默熔断兜底**。

对应 ``docs/需求契约.md`` §7 R-O1/R-O2/R-O3/R-O4 与 ``core/speech/guards.py`` 的
``run_output_gate`` / ``run_output_gate_async`` / ``apply_gate_text`` /
``gate_audit_fields`` / ``gate_scope``。

**2026-10-24 整族退役（主人 m14437 批）**：出站闸门里只剩**机械可判**的事项。
「内部语汇（禁语词表）」与「复读指纹」两条语义判据已整族删除——语域与复述是
会话里模型自己的事，代码不该替她判"这话像不像我说的"（2026-10-21 那次
974 字复述闸的前科即为此）。随之退役的还有：复述抑制（``REPEAT_SUPPRESS``）、
近重复闸（``REPEAT_GUARD``）、断言台账闸（``ASSERTION_GUARD``）。

现在只剩：

* 观测项 ② **群聊字数**（``group_max_chars``）→ 作用域恒 ``observe``：命中只写
  读数（before_len/after_len/hard/soft），**文本逐字不变**，``on`` 也一样
  （R-O2：群聊字数不是主人需求，只是代理的观察）。
* 拦截项 ⑤ **结构化名单泄漏**（``roster_leak``）→ 作用域 ``enforce``（R-V5 隐私
  硬禁区）：命中改写为**拒绝语**（``GateConfig.roster_refusal_text``），**不是沉默**。
* ⑤b **沉默熔断**（同会话 10 分钟 DROP ≥3 → LOG_ONLY + 告警）：结构性兜底。
  当前没有任何规则会 DROP（拦截项只改写），所以它常驻休眠——用例
  ``test_no_rule_produces_a_block_today`` 钉的就是这个事实。
* ⑦ **兜底**：改写后万一为空，用 ``lexicon_fallback`` 填一句，绝不把空文本交给上游。
* **flag 拆成三个轴**（不共用）：``YINYUE_FEATURE_OUTPUT_GATE`` 三态控制整体；
  ``..._OBSERVE`` / ``..._ENFORCE`` 各自声明观测项/拦截项的作用域
  （``observe|enforce``）。既有 ``on/off/shadow`` 读法一字不改。

样本取自 **2026-09-30 21:40–21:56 群 10000000001@chatroom 的真实事故**：

② 群聊字数：``LONG_GROUP_REPLY`` 实测 131 字，**旧实现按句边界截到 111 字**
   （``_truncate_at_sentence``，已退役）；现在只记 hard/soft 读数、一字不改；
⑤ 名单泄漏：``ROSTER_TEXT`` 两条人员记录（含单位/班级号）—— on + enforce 下
   改写成拒绝语，其它格只记不动；
⑤b 熔断：见上（今日无输入）；
⑥ 组合矩阵：两条规则 × off/shadow/on（含拦截项作用域 observe/enforce 关键格）；
⑦ 误杀回归：正常长回复、引用别人原话、私聊长文、元语汇（模型/上下文）不被拦；
⑧ 网关全链路：观测项经 ``gateway.send_text`` 发出的是**逐字原文**，审计一行为
   warn + ``hits=`` 计数；闸门 0 次模型调用；
⑨ 闸门是纯函数：同输入同输出、无 IO（审计写入与判定分离），且**无改写/无模型**路径。

Run directly:
    python tests/test_output_gate.py
"""

from __future__ import annotations

import asyncio
import inspect
import os
import re
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

ROOT = Path(__file__).resolve().parent.parent
# 隔离前导（照 tests/test_reminder_mapping.py:34-41）：本文件以纯函数为主，但
# ④⑤ 要走一次网关审计，所以必须先把 meta 库/平台目录/日志目录改道，
# 万一以后加了走 bootstrap 的断言也不会碰到生产库。
_TEST_LOG_DIR = str(Path("data") / "_test_logs_output_gate")
Path(_TEST_LOG_DIR).mkdir(parents=True, exist_ok=True)
os.environ.setdefault("LOG_DIR", _TEST_LOG_DIR)
_ISO_ROOT = (ROOT / "data" / "_output_gate_test" / "_iso").resolve()
_ISO_ROOT.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("META_DB_PATH", str(_ISO_ROOT / "meta.db"))
os.environ.setdefault("PLATFORMS_DIR", str(_ISO_ROOT / "platforms"))
os.environ.setdefault("DATA_DIR", str(_ISO_ROOT / "data"))

from sqlalchemy import select  # noqa: E402

from core import features  # noqa: E402
from core.speech.guards import (  # noqa: E402
    GATE_MODE_OFF,
    GATE_MODE_ON,
    GATE_MODE_SHADOW,
    GATE_SCOPE_ENFORCE,
    GATE_SCOPE_OBSERVE,
    OUTPUT_GATE_ENFORCE,
    OUTPUT_GATE_OBSERVE,
    GateConfig,
    GateResult,
    apply_gate_text,
    gate_audit_fields,
    gate_scope,
    gate_scopes,
    run_output_gate,
    run_output_gate_async,
)

# ---------------------------------------------------------------------------
# 真实样本（2026-09-30 21:40–21:56，群 10000000001@chatroom）
# ---------------------------------------------------------------------------
# 2026-10-24：「内部语汇」与「复读指纹」两条语义判据整族退役——语域与复述是
# 会话里模型自己的事，代码只留**机械可判**的形态。出站闸门现在只有两条规则：
#   * ``group_max_chars``（观测）：群聊字数超阈值只记读数，一个字都不改；
#   * ``roster_leak``（拦截）：结构化名单泄漏在 on + enforce 下改写为拒绝语。
# 21:45:20：把 P3 说成广东（她 21:44:59 刚自报"湖南长沙"）
CLAIM_LEAK = "P3 你是广东人吧，刚才报的籍贯里没有你。"
# 她那 25 条的口径：均 44 字；这条实测 **131 字**（> 群聊观测口径 120）
LONG_GROUP_REPLY = (
    "同学你好，关于报到这件事我再说一遍：学籍在湖南的按长沙校区走。"
    "学籍在广东的按广州校区走。港澳台的按国际学院走。剩下的按生源地就近安排。"
    "具体窗口明天早上八点开。带好身份证和录取通知书。"
    "别跑错了地方，跑错了要重新排队，队很长。窗口只开一个上午，来晚了就只能等下一批。"
)
# 旧实现（退役前）对上面这条的读数：`_truncate_at_sentence(text, 60, ceiling=120)`
# → 111 字；本次裁决后同一输入必须**一字不改**（用例 ② 守的就是这个差）。
LONG_GROUP_REPLY_LEN = 131
LONG_GROUP_REPLY_OLD_TRIM = 111
# 结构化名单泄漏样本：两条人员记录（含班级号），形态判据命中 >= 2 条即拦
ROSTER_TEXT = "张伟，男，计算机学院，2024级\n李娜，女，软件学院，2024级"
# 默认会话：真实事故群（网关全链路用例用）
DEFAULT_CHAT = "10000000001@chatroom"
# 纯函数用例专用会话：与网关用例的 DROP 计数彻底隔开（熔断按 chat_id 计数）
PURE_CHAT = "output_gate_pure@chatroom"


def _gate(text: str, **kwargs) -> GateResult:
    kwargs.setdefault("mode", GATE_MODE_SHADOW)
    # 每条用例自带的会话 id：沉默熔断是按 chat_id 计数的进程级状态，
    # 不隔离的话同一次测试进程里前面的 DROP 会把后面的判定降级成只记不动。
    kwargs.setdefault("chat_id", PURE_CHAT)
    return run_output_gate(text, **kwargs)


def _reset_breakers(*chat_ids: str) -> None:
    """清掉沉默熔断计数：它是进程级状态，用例之间必须各自干净。

    没有这一步，前面用例留下的 DROP 计数会把后面用例带偏——红绿取决于顺序。
    """
    from core.speech.guards import reset_drop_breaker

    for chat_id in chat_ids or (PURE_CHAT, DEFAULT_CHAT):
        reset_drop_breaker(chat_id)
    reset_drop_breaker("")  # 全局（含未指定会话的调用）


@contextmanager
def _gate_env(overall: str | None = None, observe: str | None = None,
              enforce: str | None = None):
    """临时设置闸门三个轴的环境变量，退出时连"未设置"状态一起还原。

    三个轴：``YINYUE_FEATURE_OUTPUT_GATE``（off|shadow|on）、
    ``..._OBSERVE`` / ``..._ENFORCE``（observe|enforce）。
    """
    keys = {
        "overall": features.env_key(features.OUTPUT_GATE),
        "observe": features.env_key(OUTPUT_GATE_OBSERVE),
        "enforce": features.env_key(OUTPUT_GATE_ENFORCE),
    }
    saved = {key: os.environ.get(key) for key in keys.values()}
    try:
        for name, key in keys.items():
            value = {"overall": overall, "observe": observe,
                     "enforce": enforce}[name]
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _rules(result: GateResult) -> set[str]:
    return set(result.rules)


def _code_without_docstring(func) -> str:
    """函数体源码**去掉 docstring**（结构守卫只该盯代码，不盯说明文字）。

    docstring 里必须留着"为什么不再用 content_preview"的记录——那是给后人的
    护栏；断言不能逼着后人删掉注释。

    **行内注释也算判据的一部分**（``ast.unparse`` 会把它们变成占位名）。这是
    **有意**的：断言放宽成"名字可以挪到别处"会让护栏失去意义（真正要防的是有人
    把 ``content_preview`` 换回来）。2026-10-24 起取窗口那层（``_gate_self_text_window``
    / ``SpeechStore.recent_self_texts``）已随复读指纹整族退役，本函数不再有窗口契约。
    """
    import ast
    import textwrap

    tree = ast.parse(textwrap.dedent(inspect.getsource(func)))
    body = tree.body[0].body
    if body and isinstance(body[0], ast.Expr) and isinstance(
            body[0].value, ast.Constant):
        body = body[1:]
    return "\n".join(ast.unparse(node) for node in body)


# ---------------------------------------------------------------------------
# ① 禁语 / 内部语汇（观测项：命中只记，**文本逐字不变**）
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# ② 群聊字数上限（观测项：不 trim、不 regen、不按句边界截断）
# ---------------------------------------------------------------------------
def test_group_length_on_never_trims_131_sample():
    """**主用例②**：超长命中 → 文本逐字不变（含旧实现 131→111 的样本）。

    ``LONG_GROUP_REPLY`` 实测 131 字；旧实现 ``_truncate_at_sentence(…, 60,
    ceiling=120)`` 把它截到 111 字（``LONG_GROUP_REPLY_OLD_TRIM``）。裁决后同一输入
    必须原样：``after_len == before_len == 131``、``action=warn``、``enforced=False``。
    """
    assert len(LONG_GROUP_REPLY) == LONG_GROUP_REPLY_LEN, len(LONG_GROUP_REPLY)
    assert LONG_GROUP_REPLY_OLD_TRIM < LONG_GROUP_REPLY_LEN > \
        GateConfig().hard_max_chars, "样本必须同时满足：超硬上限 且 旧实现会截短"
    result = _gate(LONG_GROUP_REPLY, chat_type="group", mode=GATE_MODE_ON)
    assert "group_max_chars" in _rules(result), result.rules
    assert result.applied_text == LONG_GROUP_REPLY, repr(result.applied_text)
    assert result.enforced is False, result
    assert result.allowed is True, result
    assert result.action == "warn", result.action
    assert result.before_len == result.after_len == LONG_GROUP_REPLY_LEN, result
    assert len(result.applied_text) != LONG_GROUP_REPLY_OLD_TRIM, "不许再截到 111 字"
    return True


def test_group_length_records_hard_soft_and_counts():
    """观测读数：detail 带 hard/soft，审计带 ``hits=`` 计数与相等的 before/after。"""
    result = _gate(LONG_GROUP_REPLY, chat_type="group", mode=GATE_MODE_ON)
    finding = next(f for f in result.findings if f.rule == "group_max_chars")
    assert f"hard={GateConfig().hard_max_chars}" in finding.detail, finding.detail
    assert f"soft={GateConfig().soft_target_chars}" in finding.detail, finding.detail
    assert str(len(LONG_GROUP_REPLY)) in finding.detail, finding.detail
    fields = gate_audit_fields(result, chat_type="group", chat_id=PURE_CHAT)
    preview = fields["content_preview"]
    assert "hits=group_max_chars:1" in preview, preview
    assert f"before_len={len(LONG_GROUP_REPLY)}" in preview, preview
    assert f"after_len={len(LONG_GROUP_REPLY)}" in preview, preview
    assert fields["decision"] == "warn", fields
    return True


def test_group_length_thresholds_are_observational():
    """阈值可配置：把观测口径压到 20，短句也**只记**（旧实现会截）。"""
    config = GateConfig(hard_max_chars=20, soft_target_chars=10)
    text = "在的，你说。报到明天早上八点开窗口，记得带身份证。"
    result = _gate(text, chat_type="group", mode=GATE_MODE_ON, config=config)
    assert "group_max_chars" in _rules(result), result.rules
    assert result.applied_text == text, result.applied_text
    assert result.after_len == result.before_len == len(text), result
    return True


def test_private_length_is_not_capped():
    """私聊放宽：同一段长文在私聊不触发（默认 private_max_chars=0）。"""
    group = _gate(LONG_GROUP_REPLY, chat_type="group", mode=GATE_MODE_ON)
    private = _gate(LONG_GROUP_REPLY, chat_type="private", mode=GATE_MODE_ON)
    assert "group_max_chars" in _rules(group), group.rules
    assert private.hit is False, private.findings
    assert private.applied_text == LONG_GROUP_REPLY, private
    return True


def test_private_max_chars_is_observational():
    """私聊口径可配置：显式给 private_max_chars 也只记（不截、不改）。"""
    config = GateConfig(private_max_chars=20)
    text = "在的，你说。报到明天早上八点开窗口，记得带身份证和录取通知书。"
    result = _gate(text, chat_type="private", mode=GATE_MODE_ON, config=config)
    assert "group_max_chars" in _rules(result), result.rules
    assert result.applied_text == text, result.applied_text
    assert result.after_len == result.before_len == len(text), result
    return True


# ---------------------------------------------------------------------------
# ④ 无证据断言：只记不拦、不改写
# ---------------------------------------------------------------------------
def test_unfounded_claim_rule_is_retired():
    """R0：`unfounded_claim` 执行路径已整段删除——无证据断言不是可判的硬事实。

    锁的是「这条规则不再存在」：同样的文本不再进 findings、不进 audit 的
    rule_ids；观测项作用域只剩内部语汇与字数两项。
    """
    import core.speech.guards as _g

    assert not hasattr(_g, "_unfounded_hits"), "静态形态路径必须已删除"
    assert not hasattr(_g, "_person_assert_hits"), "结构形态路径必须已删除"
    assert not hasattr(_g, "_claim_ledger_hits"), "台账执行路径必须已删除"
    for text in (CLAIM_LEAK, "你刚说的那批人", "他对我说过这事"):
        result = _gate(text, chat_type="group", mode=GATE_MODE_ON)
        assert "unfounded_claim" not in _rules(result), (text, result.rules)
        assert result.applied_text == text, (text, repr(result.applied_text))
    return True


def test_observation_items_never_produce_block_severity():
    """观测项的 findings 一律 watch：没有"待处置"的观测命中。"""
    result = _gate(LONG_GROUP_REPLY, chat_type="group", mode=GATE_MODE_ON)
    assert result.findings, result
    assert all(f.severity == "watch" for f in result.findings), result
    assert result.action == "warn", result.action
    assert result.applied_text == LONG_GROUP_REPLY, result.applied_text
    return True


# ---------------------------------------------------------------------------
# ⑤b 沉默熔断（结构性兜底：当前没有任何规则产出 block，熔断因此常驻休眠）
# ---------------------------------------------------------------------------
BREAKER_CHAT = "breaker@chatroom"


def test_no_rule_produces_a_block_today():
    """结构事实：三条规则里没有一条会 DROP——拦截项一律改写为拒绝语。

    熔断器（``record_gate_drop`` / ``drop_breaker_state``）是"万一将来真有 DROP"
    的结构性兜底。2026-10-24 复读/断言两条闸退役后它**没有输入**了：
    ``run_output_gate`` 里没有一处 ``action = "block"`` 赋值，名单泄漏走改写分支。
    所以这里钉两条：
      * 源码不许重新长出 DROP 赋值（那是把"她突然不说话"装回来）；
      * 名单泄漏在 on 下改写为拒绝语，连续命中也不会累计 DROP、不会熔断。
    """
    from core.speech.guards import drop_breaker_state

    code = inspect.getsource(run_output_gate)
    for assignment in ('action = "block"', 'action, rule = "block"'):
        assert assignment not in code, f"不许重新长出 DROP 赋值：{assignment}"

    _reset_breakers(BREAKER_CHAT)
    first = run_output_gate(ROSTER_TEXT, chat_type="group",
                            chat_id=BREAKER_CHAT, mode=GATE_MODE_ON)
    assert first.action == "rewrite", first
    assert first.rule == "roster_leak", first
    assert first.applied_text == GateConfig().roster_refusal_text, first
    assert first.enforced is True and first.allowed is True, first

    for _ in range(5):
        again = run_output_gate(ROSTER_TEXT, chat_type="group",
                                chat_id=BREAKER_CHAT, mode=GATE_MODE_ON)
        assert again.action == "rewrite", again
    state = drop_breaker_state(BREAKER_CHAT)
    assert state.drops == 0, state
    assert state.tripped is False, state
    return True


# ---------------------------------------------------------------------------
# ⑤c 数据通道：阈值/词表落数据（policy_rules），**改数据不重启即生效**
# ---------------------------------------------------------------------------
def _policy_rule(store, key: str, payload: dict, *, enabled: int = 1) -> None:
    """把一行闸门配置写进隔离库的 ``policy_rules``（模拟运维改数据）。

    key 是唯一键：同一 key 再写一次就是**改数据**（upsert），不是插入重复行。
    """
    import json as _json

    from db.meta_models import PolicyRule

    with store._Session() as session:
        row = session.execute(
            select(PolicyRule).where(PolicyRule.key == key)
        ).scalar_one_or_none()
        if row is None:
            row = PolicyRule(key=key, name=key, category="output_gate",
                             enforcement="hard")
            session.add(row)
        row.payload_json = _json.dumps(payload, ensure_ascii=False)
        row.enabled = enabled
        session.commit()


def test_gate_config_comes_from_data_rows():
    """``output_gate_payloads`` 读 policy_rules；白名单键覆盖代码默认值。"""
    gateway, store, _ = _isolated_gateway()
    _policy_rule(store, "output_gate", {
        "hard_max_chars": 33, "soft_target_chars": 40,
    })
    config = gateway._gate_config()
    assert config.hard_max_chars == 33, config
    assert config.soft_target_chars == 40, config
    # 数据行没写的键仍由代码默认值兜底
    assert config.private_max_chars == GateConfig().private_max_chars, config
    # 复读/内部语汇两个旋钮已随语义判据退役：配置里不该再有它们
    for gone in ("block_repeats", "repeat_confirm_text", "repeat_lookback",
                 "repeat_similarity", "blocked", "watch", "allowed"):
        assert not hasattr(config, gone), gone
    # 重生成参数已随 ② 观测化退役：配置里不该再有这个旋钮
    assert not hasattr(config, "regen_max"), "字数不再重生成，regen_max 已退役"
    return True


def test_gate_threshold_change_takes_effect_without_restart():
    """**不重启生效**：bump_revision 之后同一个网关对象立刻用新阈值。"""
    gateway, store, _ = _isolated_gateway()
    _policy_rule(store, "output_gate", {"hard_max_chars": 33})
    assert gateway._gate_config().hard_max_chars == 33
    _policy_rule(store, "output_gate", {"hard_max_chars": 44})
    # 配置缓存按 revision 失效：先确认"没有 revision 变化就沿用缓存"
    assert gateway._gate_config().hard_max_chars == 33, "同一 revision 应命中缓存"
    store.bump_revision("policy_write", "test", {"key": "output_gate"})
    assert gateway._gate_config().hard_max_chars == 44, \
        "revision 变了必须立刻读到新阈值（改数据不重启）"
    return True


def test_gate_config_fallbacks_are_code_defaults():
    """没有数据行 / payload 是垃圾 → 回落代码默认值，绝不炸。"""
    gateway, store, _ = _isolated_gateway()
    assert gateway._gate_config() == GateConfig()
    _policy_rule(store, "unrelated_rule", {"hard_max_chars": 7})
    assert gateway._gate_config() == GateConfig(), "无关数据行不许影响闸门"
    _policy_rule(store, "output_gate", {"hard_max_chars": 7}, enabled=0)
    assert gateway._gate_config() == GateConfig(), "enabled=0 的行不生效"
    from db.meta_models import PolicyRule

    with store._Session() as session:
        session.add(PolicyRule(key="output_gate_bad", payload_json="{not json",
                               enabled=1))
        session.commit()
    assert gateway._gate_config() == GateConfig(), "坏 payload 不许炸"
    return True


# ---------------------------------------------------------------------------
# ⑤ flag：整体三态（不改既有语义）+ 观测/拦截两个作用域声明
# ---------------------------------------------------------------------------
def test_flag_three_states_pure_gate():
    """off 不做事 / shadow 只记 / on 处置拦截项；三态读法正确。"""
    with _gate_env():
        assert features.mode(features.OUTPUT_GATE) == GATE_MODE_SHADOW, "默认 shadow"
        # WP-02 起"防止有人改回 enabled()"由硬拦截保证：三态 flag 用 enabled()
        # 读直接 raise，而不是返回 False。
        try:
            features.enabled(features.OUTPUT_GATE)  # tri-state-reader-ok: 负例断言，必须 raise
            raise AssertionError("三态 flag 不许用 enabled() 读")
        except ValueError:
            pass
        assert features.mode(features.OUTPUT_GATE, env_var=None) == GATE_MODE_SHADOW

    for raw, want in (("on", GATE_MODE_ON), ("1", GATE_MODE_ON),
                      ("off", GATE_MODE_OFF), ("shadow", GATE_MODE_SHADOW),
                      ("garbage", GATE_MODE_SHADOW)):
        with _gate_env(overall=raw):
            assert features.mode(features.OUTPUT_GATE) == want, raw

    off = _gate(LONG_GROUP_REPLY, chat_type="group", mode=GATE_MODE_OFF)
    assert off.hit is False and off.findings == (), off
    assert off.action == "allow" and off.mode == GATE_MODE_OFF, off
    assert off.applied_text == LONG_GROUP_REPLY, off

    shadow = _gate(LONG_GROUP_REPLY, chat_type="group", mode=GATE_MODE_SHADOW)
    assert shadow.hit is True and shadow.action == "warn", shadow
    assert shadow.applied_text == LONG_GROUP_REPLY, shadow

    # **on 也不改写观测项**：字数命中只记读数，文本逐字不变。
    on = _gate(LONG_GROUP_REPLY, chat_type="group", mode=GATE_MODE_ON)
    assert on.hit is True and on.action == "warn", on
    assert on.enforced is False, on
    assert on.applied_text == LONG_GROUP_REPLY, on
    assert all(f.severity == "watch" for f in on.findings), on.findings
    return True


def test_scope_flags_split_observation_and_enforcement():
    """**主用例⑤**：观测项/拦截项各有作用域声明，各组合格都验一遍（含名单泄漏）。

    ``YINYUE_FEATURE_OUTPUT_GATE`` 仍是整体三态（off/shadow/on）；新加的两个 flag
    只声明**各自那一类**检查的作用域，互不共用——这正是 G4-1 要拆的那件事：不再
    出现"切成 on 就顺带删禁语 / 截断字数"。
    """
    for scope, want in ((None, GATE_SCOPE_ENFORCE), ("", GATE_SCOPE_ENFORCE),
                        (GATE_SCOPE_ENFORCE, GATE_SCOPE_ENFORCE),
                        (GATE_SCOPE_OBSERVE, GATE_SCOPE_OBSERVE),
                        ("garbage", GATE_SCOPE_ENFORCE)):
        with _gate_env(enforce=scope):
            got = gate_scope("roster_leak")
            assert got == want, (scope, got)
            # 拦截项的开关永远不波及观测项
            assert gate_scope("group_max_chars") == GATE_SCOPE_OBSERVE, scope
            assert gate_scope("unfounded_claim") == GATE_SCOPE_OBSERVE, scope
    with _gate_env(enforce=GATE_SCOPE_OBSERVE):
        assert gate_scope("roster_leak") == GATE_SCOPE_OBSERVE
        assert gate_scopes() == {
            "group_max_chars": GATE_SCOPE_OBSERVE,
            "unfounded_claim": GATE_SCOPE_OBSERVE,
            "roster_leak": GATE_SCOPE_OBSERVE,
        }
    with _gate_env():
        assert gate_scopes() == {
            "group_max_chars": GATE_SCOPE_OBSERVE,
            "unfounded_claim": GATE_SCOPE_OBSERVE,
            # ⑤ 结构化名单泄漏：隐私 = 不可逆 → 拦截项（唯一一个 enforce 位，
            # 可被 OUTPUT_GATE_ENFORCE=observe 灰度降级）
            "roster_leak": GATE_SCOPE_ENFORCE,
        }, "默认：观测项 observe、拦截项 enforce"
    return True


def test_observe_scope_flag_cannot_rearm_rewriting():
    """观测项声明 ``enforce`` 被拒绝（告警一次），行为仍是 observe：不改文本。

    这条守的是"不许留一条与裁决相反的回滚通道"：flag 能被读到、能被看见（告警），
    但代码里已经没有"删子句/截断"的路径可以激活。
    """
    import core.speech.guards as guards_mod

    guards_mod._OBSERVE_SCOPE_WARNED = False
    with _gate_env(overall=GATE_MODE_ON, observe=GATE_SCOPE_ENFORCE):
        assert gate_scope("group_max_chars") == GATE_SCOPE_OBSERVE
        assert gate_scope("unfounded_claim") == GATE_SCOPE_OBSERVE
        length = _gate(LONG_GROUP_REPLY, chat_type="group", mode=GATE_MODE_ON)
    assert length.applied_text == LONG_GROUP_REPLY, length.applied_text
    assert length.enforced is False and length.action == "warn", length
    assert guards_mod._OBSERVE_SCOPE_WARNED is True, "被拒绝的声明必须留下告警痕迹"
    return True


def test_apply_gate_text_only_enforces_on():
    """``apply_gate_text`` 是唯一的"要不要改文本"开关（只对拦截项有意义）。"""
    _reset_breakers(PURE_CHAT)
    roster_shadow = _gate(ROSTER_TEXT, chat_type="group", mode=GATE_MODE_SHADOW)
    roster_on = _gate(ROSTER_TEXT, chat_type="group", mode=GATE_MODE_ON)
    length_on = _gate(LONG_GROUP_REPLY, chat_type="group", mode=GATE_MODE_ON)
    assert apply_gate_text(ROSTER_TEXT, roster_shadow) == ROSTER_TEXT
    assert apply_gate_text(ROSTER_TEXT, roster_on) == \
        GateConfig().roster_refusal_text, "拦截项在 on 下改写为拒绝语"
    assert apply_gate_text(LONG_GROUP_REPLY, length_on) == LONG_GROUP_REPLY, \
        "观测项永不被改"
    assert apply_gate_text(ROSTER_TEXT, None) == ROSTER_TEXT
    return True


def test_three_state_reader_is_shared_by_all_four_call_sites():
    """三态读法只有一处：四个调用点（闸门/位置门/准入/常量）对同一输入同结果。

    三种输入 × 两个 env 读法：``off`` / ``shadow`` / ``on`` / 垃圾值必须一一对应；
    未知值只允许回落到**调用方显式给出的默认值**（闸门=shadow，准入=off）。
    （位置门 ``pg`` 只做常量收敛断言：该开关已随 WP-04 退役、全仓无读点。）
    """
    from core.context import admission as ad
    from core.speech import guards as gd

    # 常量收敛：以前四处各写一份字面量
    assert (gd.GATE_MODE_OFF, gd.GATE_MODE_SHADOW, gd.GATE_MODE_ON) == (
        features.MODE_OFF, features.MODE_SHADOW, features.MODE_ON)
    assert (ad.MODE_OFF, ad.MODE_SHADOW, ad.MODE_ON) == (
        features.MODE_OFF, features.MODE_SHADOW, features.MODE_ON)
    # 新的作用域词表也是唯一一份（别在别处再写 "observe"/"enforce" 字面量）
    assert gd.GATE_SCOPE_CHOICES == (GATE_SCOPE_OBSERVE, GATE_SCOPE_ENFORCE)

    out_key = features.env_key(features.OUTPUT_GATE)
    # env_var 型三态读法由**仍活着**的 admission 覆盖（``YINYUE_ADMISSION_MODE``，
    # 默认 off）。WP-04 退役的 ``YINYUE_POSITION_GATE_MODE`` 原先只被本用例**写进
    # 环境、从不读取**（死键），task-14 随 shadow 口径收口一并移除，断言口径不变。
    ad_key = features.ADMISSION_MODE_ENV
    saved = {key: os.environ.get(key) for key in (out_key, ad_key)}
    try:
        expectations = {
            None: ("shadow", "off"),        # 未设置 → 各自的默认
            "": ("shadow", "off"),
            "off": ("off", "off"),
            "shadow": ("shadow", "shadow"),
            "on": ("on", "on"),
            "1": ("on", "on"),               # 布尔写法 = on
            "true": ("on", "on"),
            "TrUe": ("on", "on"),            # 大小写与空白
            " on ": ("on", "on"),
            "0": ("shadow", "off"),         # 0 走调用方默认
            "false": ("shadow", "off"),
            "garbage": ("shadow", "off"),   # 未知值 → 调用方默认，不猜
            "yes": ("on", "on"),
        }
        for raw, (want_gate, want_ad) in expectations.items():
            for key in (out_key, ad_key):
                if raw is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = raw
            got = (
                features.mode(features.OUTPUT_GATE, features.MODE_SHADOW),
                features.mode("admission_mode", features.MODE_OFF,
                              env_var=ad_key),
            )
            assert got == (want_gate, want_ad), (raw, got)
            # 模块自己的读法也必须一致（不是只有 features 对）
            assert gd.GATE_MODE_ON == features.MODE_ON
            assert ad.mode() == want_ad, (raw, ad.mode())
            assert features.mode(features.OUTPUT_GATE) == want_gate, raw
        # WP-02：三态 flag 的布尔读法已退役——任何取值都必须 raise，
        # 不再有"shadow 读成 False"的通道。
        for raw_value in ("on", "shadow", "off", "garbage"):
            os.environ[out_key] = raw_value
            try:
                features.enabled(features.OUTPUT_GATE)  # tri-state-reader-ok: 负例断言，必须 raise
                raise AssertionError(f"三态 flag 不许用 enabled() 读（raw={raw_value}）")
            except ValueError:
                pass
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    return True


def test_observation_enforcement_matrix():
    """**主用例⑥**：组合矩阵——两条规则 × off/shadow/on × 拦截项作用域。

    每格只断言"看得见的结果"：处置 / 文本是否逐字不变 / 是否拦。观测项任何格子都
    不许改文本；拦截项只有 ``on`` + ``enforce`` 才动（且只改写为拒绝语，不沉默）。
    """
    cases = [
        # (mode, enforce_scope, rule, 输入, 期望 action, 期望文本逐字不变)
        (GATE_MODE_OFF, None, "group_max_chars", LONG_GROUP_REPLY, "allow", True),
        (GATE_MODE_SHADOW, None, "group_max_chars", LONG_GROUP_REPLY, "warn", True),
        (GATE_MODE_ON, None, "group_max_chars", LONG_GROUP_REPLY, "warn", True),
        (GATE_MODE_ON, GATE_SCOPE_OBSERVE, "group_max_chars", LONG_GROUP_REPLY,
         "warn", True),
        (GATE_MODE_OFF, None, "roster_leak", ROSTER_TEXT, "allow", True),
        (GATE_MODE_SHADOW, None, "roster_leak", ROSTER_TEXT, "warn", True),
        (GATE_MODE_ON, GATE_SCOPE_OBSERVE, "roster_leak", ROSTER_TEXT, "warn", True),
        (GATE_MODE_ON, GATE_SCOPE_ENFORCE, "roster_leak", ROSTER_TEXT,
         "rewrite", False),
    ]
    for mode, scope, rule, text, want_action, want_verbatim in cases:
        label = f"{mode}/{scope}/{rule}"
        _reset_breakers(PURE_CHAT)
        with _gate_env(enforce=scope):
            result = _gate(text, chat_type="group", mode=mode)
        assert result.action == want_action, (label, result.action)
        if mode == GATE_MODE_OFF:
            assert result.hit is False, (label, result.findings)
        else:
            assert rule in _rules(result), (label, result.rules)
        if want_verbatim:
            assert result.applied_text == text, (label, repr(result.applied_text))
            assert result.enforced is False, (label, result)
            assert result.before_len == result.after_len, (label, result)
            assert result.allowed is True, (label, result)
        else:
            assert result.applied_text != text, (label, result.applied_text)
            assert result.enforced is True and result.allowed is True, (label, result)
            assert result.applied_text == GateConfig().roster_refusal_text, label
    return True


def test_shadow_and_off_make_zero_changes():
    """shadow/off：同一批样本一个字符都不动（观测项与拦截项都不动）。"""
    texts = (LONG_GROUP_REPLY, CLAIM_LEAK, ROSTER_TEXT)
    for mode in (GATE_MODE_SHADOW, GATE_MODE_OFF):
        for text in texts:
            result = _gate(text, chat_type="group", mode=mode)
            assert result.applied_text == text, (mode, text, result.applied_text)
            assert result.enforced is False, (mode, text, result)
    return True


def test_gate_async_shell_matches_sync_verdict():
    """async 壳与同步判定同结果（壳里已无重生成，恒等于同步调用）。"""
    _reset_breakers(PURE_CHAT)
    for text in (LONG_GROUP_REPLY, ROSTER_TEXT):
        sync = _gate(text, chat_type="group", mode=GATE_MODE_ON)
        _reset_breakers(PURE_CHAT)
        shell = asyncio.run(run_output_gate_async(
            text, chat_type="group", chat_id=PURE_CHAT, mode=GATE_MODE_ON))
        assert shell == sync, (text, shell, sync)
    return True


# ---------------------------------------------------------------------------
# ⑥ 误杀回归
# ---------------------------------------------------------------------------
def test_normal_replies_not_touched():
    """正常回复（含"模型/上下文"这类元语汇的正常用法）不被改、不被拦。"""
    for text in (
        "在的，你说。",
        "报到明天早上八点开窗口，带好身份证和录取通知书，去长沙校区就行。",
        "这活儿我不接，闹钟得他自己定。",
        "你刚才说的那个校区，我记着了。",
        "他说的模型是本地跑的那种，我不太懂这个。",
        "刚看到有人问「我这边判定是串台了吗」，那是别人在群里说的原话。",
    ):
        result = _gate(text, chat_type="group", mode=GATE_MODE_ON)
        assert result.applied_text == text, (text, result.applied_text)
        assert result.allowed is True, (text, result)
    return True


def test_private_long_text_not_blocked():
    """私聊长文 + 正常聊天记录：既不截断也不误判重复。"""
    long_private = LONG_GROUP_REPLY + "另外你说的那个表格我明天再对一遍，今天先这样。"
    result = _gate(long_private, chat_type="private", mode=GATE_MODE_ON)
    assert result.applied_text == long_private, result
    assert result.allowed is True, result
    return True


# ---------------------------------------------------------------------------
# ⑦ 纯函数（同输入同输出、无 IO、无改写路径）
# ---------------------------------------------------------------------------
def test_gate_is_deterministic_and_pure():
    """同输入同输出；判定不碰 IO（审计由 gate_audit_fields 单独生成）。"""
    _reset_breakers(PURE_CHAT)
    kwargs = dict(chat_type="group", mode=GATE_MODE_ON,
                  config=GateConfig(), chat_id=PURE_CHAT)
    first = run_output_gate(LONG_GROUP_REPLY, **kwargs)
    second = run_output_gate(LONG_GROUP_REPLY, **kwargs)
    assert first == second, (first, second)
    assert first.findings == second.findings, (first, second)
    assert run_output_gate(LONG_GROUP_REPLY, **kwargs) == \
        run_output_gate(LONG_GROUP_REPLY, **kwargs)
    # 审计字段是纯函数（不落库、可重复）
    assert gate_audit_fields(first) == gate_audit_fields(second)
    return True


def test_audit_fields_carry_required_keys():
    """审计字段至少含 action/rule/evidence/before_len/after_len/mode（+hits）。"""
    result = _gate(LONG_GROUP_REPLY, chat_type="group", mode=GATE_MODE_ON)
    fields = gate_audit_fields(result, chat_type="group", chat_id=DEFAULT_CHAT)
    assert fields["decision"] in ("allow", "block", "rewrite", "warn"), fields
    preview = fields["content_preview"]
    for key in ("rule=", "evidence=", "before_len=", "after_len=", "mode=", "hits="):
        assert key in preview, (key, preview)
    assert fields["rule_ids"] and fields["rule_ids"][0].startswith("gate:"), fields
    assert len(preview) <= 200, len(preview)
    # 没有命中就不该写 hits= （allow 行仍与历史口径一致）
    clean = gate_audit_fields(_gate("在的，你说。", chat_type="group"))
    assert "hits=" not in clean["content_preview"], clean
    assert clean["decision"] == "allow", clean
    return True


def test_gate_write_has_no_io_in_pure_path():
    """结构守卫：同步判定函数体内不出现 IO 调用（审计落库只在网关）。"""
    src = inspect.getsource(run_output_gate)
    for forbidden in ("open(", "write_audit(", "get_speech_store(", "session",
                      "requests.", "sqlite3", "urlopen"):
        assert forbidden not in src, forbidden
    assert "asyncio" not in src, "纯函数不该 await 任何东西"
    # 本模块只出「审计字段」这个纯函数（gate_audit_fields），落库交给网关
    # （core/speech/outbound.py 的 store.write_audit）——所以这里既不该有 store 句柄，
    # 也不该有把结果直接写库的入口（write_gate_audit 已于 2026-09-30 删除：
    # 无生产调用者，Speculative Generality）。
    import core.speech.guards as guards_mod
    assert not hasattr(guards_mod, "write_gate_audit"), (
        "审计落库应唯一在网关，别再往本模块加写库入口"
    )
    fields_src = inspect.getsource(guards_mod.gate_audit_fields)
    assert "write_audit(" not in fields_src, "gate_audit_fields 必须是纯函数"
    return True


def test_gate_has_no_rewriting_or_model_paths():
    """结构守卫（裁决的回归防线）：闸门里**没有**删子句/截断/重生成的路径。

    旧实现的三条改写路径（`_strip_internal_clauses` 删禁语子句、
    `_truncate_at_sentence` 按句边界截断、`replacement`/`regenerate` 重生成）必须
    整体不存在——不是"关掉"，是删掉；否则一句 `flag=on` 就能把它们重新打开。
    """
    import core.speech.guards as guards_mod

    for gone in ("_strip_internal_clauses", "_truncate_at_sentence",
                 "_sentence_split", "_clause_split", "_strip_join",
                 "_GATE_SENTENCE_END", "_GATE_MIN_REMAINDER",
                 "gate_needs_regeneration", "GateRegenerator"):
        assert not hasattr(guards_mod, gone), f"{gone} 应已随裁决退役"
    params = set(inspect.signature(guards_mod.run_output_gate).parameters)
    assert "replacement" not in params, "重生成入口必须整条删掉"
    assert "regenerate" not in inspect.signature(
        guards_mod.run_output_gate_async).parameters
    assert "regen_attempted" not in {
        f.name for f in __import__("dataclasses").fields(GateResult)}
    assert "has_hard_finding" not in dir(GateResult)
    assert guards_mod.GATE_ACTIONS == ("block", "rewrite", "warn"), "trim 已退役"
    # 观测项的默认作用域是常量（不是可配置开关）：只有拦截项能灰度降级
    assert guards_mod.OBSERVE_SCOPE == GATE_SCOPE_OBSERVE
    assert guards_mod.DEFAULT_ENFORCE_SCOPE == GATE_SCOPE_ENFORCE
    return True


# ---------------------------------------------------------------------------
# ⑧ 网关接线（审计走 outbound_audit，一行一条）
# ---------------------------------------------------------------------------
def _isolated_gateway():
    """内存库上的 OutboundGateway：真实 send_text 全链路，不碰生产库。

    同时**关掉账号库解析**：历史实现的取窗口入口（已随复读指纹退役）曾会经
    ``core.bootstrap.get_account_db_manager`` 找 ``send_log``，而那个管理器指向
    真实 ``data/platforms/**``——pytest 里各测试文件共享进程环境变量（先 import
    的文件已经把 ``PLATFORMS_DIR`` 定了），不关掉就会读到**生产 send_log**，
    用例的判据源变成线上数据（判据随机命中）。关掉之后判据源只剩本用例自己的
    payload 与内存库，确定性可复现。
    """
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from core.speech.outbound import OutboundGateway
    from core.speech.store import SpeechStore
    from db.meta_models import MetaBase

    _block_account_db_resolution()
    engine = create_engine("sqlite+pysqlite:///:memory:")
    MetaBase.metadata.create_all(engine)
    store = SpeechStore(engine)
    return OutboundGateway(store), store, sessionmaker(bind=engine)


def _block_account_db_resolution() -> None:
    """让闸门读不到真实账号库（测试隔离；读不到=历史为空，不挡发送）。"""
    import core.bootstrap as bootstrap

    bootstrap.get_account_db_manager = lambda: None


def _fake_snapshot(chat_type: str = "group", chat_id: str = "10000000001@chatroom"):
    """最小但**类型正确**的 SpeechSnapshot（照 tests/test_social_cost_gate.py:103-131）。"""
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
    )
    turn = TurnContext(
        request_id="req-gate", target=target,
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
    from db.meta_models import OutboundAudit
    from sqlalchemy import select

    with store._Session() as session:
        return list(session.execute(
            select(OutboundAudit).order_by(OutboundAudit.id)
        ).scalars().all())


def _preview_len_pair(text: str) -> str:
    return f"before_len={len(text)}; after_len={len(text)}"


def _run_gateway_send(text: str, mode: str, chat_type: str = "group",
                      chat_id: str = DEFAULT_CHAT, extra: dict | None = None,
                      **env_flags):
    """真实 ``gateway.send_text``：返回 (结果, 实际发出的文本, 审计行)。"""
    from core.speech.domain import OutboundDraft

    _reset_breakers(chat_id)
    with _gate_env(overall=mode, **env_flags):
        gateway, store, _ = _isolated_gateway()
        provider, sent = _fake_provider()
        gateway._resolve_provider = lambda platform, account_id="": provider
        snapshot = _fake_snapshot(chat_type, chat_id=chat_id)
        draft = OutboundDraft(
            request_id="req-gate", kind="text", target=snapshot.turn.target,
            content=text, who=snapshot.turn.target.chat_id, extra=extra or {},
        )
        result = asyncio.run(gateway.send_text(draft, snapshot, None))
        return result, (sent[0] if sent else None), _audit_rows(store)


def test_gateway_shadow_only_adds_one_audit_row():
    """shadow 下网关只增一行审计（decision=warn+gate:group_max_chars），文本不改。"""
    result, sent_text, rows = _run_gateway_send(LONG_GROUP_REPLY, GATE_MODE_SHADOW)
    assert result.ok is True, result
    assert sent_text == LONG_GROUP_REPLY, repr(sent_text)
    assert len(rows) == 1, [(r.id, r.decision, r.content_preview) for r in rows]
    row = rows[0]
    assert row.decision == "warn", row.decision
    assert "gate:group_max_chars" in (row.rule_ids_json or ""), row.rule_ids_json
    preview = row.content_preview or ""
    for key in ("rule=", "evidence=", "before_len=", "after_len=", "mode=shadow",
                "hits=group_max_chars:"):
        assert key in preview, (key, preview)
    assert row.created_at is not None, row
    return True


def test_gateway_on_keeps_long_group_text_verbatim():
    """**主用例②（网关侧）**：131 字的群聊回复在 on 下逐字发出（旧实现会截到 111）。"""
    result, sent_text, rows = _run_gateway_send(LONG_GROUP_REPLY, GATE_MODE_ON)
    assert result.ok is True, result
    assert sent_text == LONG_GROUP_REPLY, repr(sent_text)
    assert len(sent_text) == LONG_GROUP_REPLY_LEN != LONG_GROUP_REPLY_OLD_TRIM
    assert len(rows) == 1, [(r.id, r.decision) for r in rows]
    row = rows[0]
    assert row.decision == "warn", row.decision
    assert "gate:group_max_chars" in (row.rule_ids_json or ""), row.rule_ids_json
    preview = row.content_preview or ""
    assert "mode=on" in preview and "hits=group_max_chars:1" in preview, preview
    assert _preview_len_pair(LONG_GROUP_REPLY) in preview, preview
    return True


def test_gateway_on_makes_zero_model_calls():
    """闸门不再重生成：``draft.extra["output_gate_regenerate"]`` 回调**不被读取**。"""
    calls: list[str] = []

    def regenerate(text: str) -> str:
        calls.append(text)
        return "压缩后的一句话。"

    result, sent_text, rows = _run_gateway_send(
        LONG_GROUP_REPLY, GATE_MODE_ON,
        extra={"output_gate_regenerate": regenerate})
    assert result.ok is True, result
    assert calls == [], calls
    assert sent_text == LONG_GROUP_REPLY, repr(sent_text)
    assert len(rows) == 1 and rows[0].decision == "warn", rows[0].decision
    return True


def test_gateway_off_is_a_no_op():
    """off：网关不判定、不改文本，审计行仍是惯例的 allow 一行。"""
    result, sent_text, rows = _run_gateway_send(LONG_GROUP_REPLY, GATE_MODE_OFF)
    assert result.ok is True, result
    assert sent_text == LONG_GROUP_REPLY, repr(sent_text)
    assert len(rows) == 1, len(rows)
    assert rows[0].decision == "allow", rows[0].decision
    assert "gate:" not in (rows[0].rule_ids_json or ""), rows[0].rule_ids_json
    return True


def test_gateway_audit_never_writes_production_db():
    """纪律：本用例只碰内存库；meta 库路径**不是生产库**。

    不断言"一定是本文件的隔离根"：pytest 会把三个文件放进同一进程，
    ``run_all_tests`` 又给所有子进程换了自己的隔离根（``setdefault`` 下先
    import 的文件已经把路径定好了，实测被 ``_scheduler_jobs_test`` 抢先；
    ``run_all_tests`` 的临时根甚至在仓库外）。要守的性质只有一个：
    **写出去的东西不落生产库**。
    """
    meta = Path(os.path.normpath(os.environ.get("META_DB_PATH", "")))
    production_dir = ROOT / "data"
    production = production_dir / "yinyue_meta.db"
    assert str(meta) and str(meta) != ".", meta
    assert meta != production, meta
    assert not (production_dir == meta.parent), f"meta 库直落生产 data/ 根: {meta}"
    if production_dir in meta.parents:
        # 在 data/ 下就必须在隔离目录（``_`` 开头的测试根）里
        inside = meta.relative_to(production_dir).parts[:-1]
        assert inside and inside[0].startswith("_"), \
            f"meta 库不在隔离目录里: {meta}"
    print(f"    meta 库 = {meta}")
    return True


# ---------------------------------------------------------------------------
# ⑨ 退役接口的桥（agents/router 侧；本闸门已无调用者）
# ---------------------------------------------------------------------------
class _FakeLLM:
    def __init__(self, reply: str = "", exc: Exception | None = None):
        self.reply = reply
        self.exc = exc
        self.calls: list[dict] = []

    async def chat(self, **kwargs):
        self.calls.append(kwargs)
        if self.exc is not None:
            raise self.exc
        return self.reply


def _gate_agent(reply: str = "", exc: Exception | None = None):
    """只挂 _llm 的 ConversationalAgent（照 tests/test_speak_contract.py:32-35）。"""
    from agents.conversational import ConversationalAgent

    agent = ConversationalAgent.__new__(ConversationalAgent)
    agent._llm = _FakeLLM(reply, exc)
    return agent


def main() -> bool:
    tests = [obj for name, obj in sorted(globals().items())
             if name.startswith("test_") and callable(obj)]
    print("=" * 60)
    print("输出闸门（MVP-2）回归：两项观测/拦截 + 沉默熔断兜底")
    print("=" * 60)
    failed: list[str] = []
    for test in tests:
        _reset_breakers()
        try:
            test()
            print(f"  ok   {test.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed.append(test.__name__)
            print(f"  FAIL {test.__name__}: {exc!r}")
            import traceback

            traceback.print_exc()
    print(f"\noutput-gate checks: {len(tests) - len(failed)}/{len(tests)} passed")
    return not failed


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
