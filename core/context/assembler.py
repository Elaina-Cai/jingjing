"""Context assembler (Phase 1 equivalence + Phase 2 budgets).

Phase 1: single entry for the system prompt, byte-identical to the legacy path
(``render`` joins exactly like the old wrapper).
Phase 2: when ``YINYUE_FEATURE_CONTEXT_BUDGET`` is on, every block carries a
priority + budget and over-budget blocks are truncated/dropped from the lowest
priority up, with one decision record per block in ``context_trace``.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

import structlog

from core.context.budget import (
    AUX,
    DEFAULT_BUDGETS,
    HISTORY,
    MEMORY,
    PERSONA,
    RELATION,
    BudgetDecision,
    apply_budget,
    estimate_tokens,
)

log = structlog.get_logger()
from core.context.contracts import ContextBlock, ContextBundle
from safelog import warn
from core.infra.envflags import text

_TRACE_LOCK = threading.RLock()
_TRACE_PATH: Path | None = None
_TRACE_MAX_BYTES = 4 * 1024 * 1024
_TRACE_KEEP_LINES = 2000

# prefix -> (name, source, priority, budget)
_BLOCK_POLICY: tuple[tuple[str, str, str, int, int | None], ...] = (
    ("[人格]", "[人格]", "persona.identity", PERSONA, None),
    ("[关系事实]", "[关系事实]", "principal.relation", RELATION, None),
    ("[群成员名单", "[群成员名单·权威]", "context.group_members", RELATION, None),
    # 时间块是基础时间锚，必须常驻（2026-09-24 修复：原来 priority=AUX，budget 默认
    # 400 字，而时间块 ~472 字会触发 "priority>=AUX → dropped" 被整块裁掉——
    # 改动1 把时间块从体外追加迁进 assembler 后，反而从"双份"变"零份"了）。
    # 改成 PERSONA（budget=None 永不裁），跟 persona 一样永远保留。
    ("[时间感知]", "[时间感知]", "context.time", PERSONA, None),
    ("[场外补充]", "[场外补充]", "context.chat_notes", RELATION, None),
    ("[局况]", "[局况]", "context.room_state", RELATION, None),
    ("[群聊最近消息", "[群聊最近消息·仅作上下文]", "session.group_recent", HISTORY, None),
    ("[最近对话", "[最近对话]", "session.recent", HISTORY, None),
    ("[事件记忆", "[事件记忆]", "memory.gateway", MEMORY, None),
    # 2026-09-30 指代事实（群聊纯文本代词 → 具体人）。PERSONA + 不裁：它是本轮
    # "谁在说谁"的事实，被预算裁掉就等于把可判定的事实又交回给模型猜。
    ("[指代事实]", "[指代事实]", "context.pronoun_facts", PERSONA, None),
    # 2026-09-30 消息归属（谁说给谁）。同样 PERSONA + 不裁：归属事实被裁掉，
    # 模型就只剩"最近说话人"可猜——那正是 19:08 事故的形态。
    ("[消息归属]", "[消息归属]", "context.attribution", PERSONA, None),
    # 群聊位置投影（Step 7）：GROUP_SCENE=on 且群聊/作用域内才入列；off 不追加。
    ("[位置]", "[位置]", "persona.position", PERSONA, None),
    ("[世界窗口]", "[世界窗口]", "context.act_view", PERSONA, None),
    # 2026-10-21：三个「新写的块」补登记。漏登记的后果不是"少一条注释"——`_classify`
    # 会把它们标成 `undeclared` + 告警，且**整块按 PERSONA 永不裁**，预算表对它们失效。
    # `[在手的事]` 是"这一轮手里有什么"的事实（≤3 条×60 字），与未完成的事同级；
    # `[现在这一屏]` 与 `[本轮可用的工具]` 曾经是 AUX（预算压力下**整块**丢），
    # 真机 2026-10-21 的后果：她答「我这边没有往群里发消息的工具，只有查东西、记笔记
    # 这些」——工具清单被整块丢掉，她看不见自己会什么。AUX 的定义是「丢了不影响
    # 正确性」，而「我会做什么」恰恰影响正确性，故改 RELATION（按比例裁，不整块没）。
    ("[在手的事]", "[在手的事]", "context.job_readout", RELATION, None),
    ("[现在这一屏]", "[现在这一屏]", "context.senses", RELATION, None),
    ("[本轮可用的工具]", "[本轮可用的工具]", "tool.prompt", RELATION, None),
    # `[要真办就附一段计划]` 是判的输出契约（怎么附 `<plan>` 块），
    # 由 `core/jobs/plan_block.py::PLAN_HINT` 渲染、`JOB_LEDGER=off` 时根本不入列。
    # AUX + 不裁：它是"怎么写"的说明，预算吃紧时它最先可以丢（丢了只是本轮不附计划）。
    # 前缀必须是块**真实**的头几个字符：`core/jobs/plan_block.py::PLAN_HINT` 以
    # 「要真办就附 <plan>…」开头，写成完整句会让 `_classify` 永远匹配不上 ⇒
    # 该块在 context_trace 里被挂到上一块（那类 trace 冒充 2026-09-24 实测过）。
    # `_classify` 用 `first.startswith(prefix)` 匹配块**第一行**；这一块没有方括号头，
    # 真实首行是「要真办就附 <plan>…」⇒ 前缀必须原样写「要真办就附」。
    ("要真办就附", "[要真办就附一段计划]", "jobs.plan_hint", RELATION, None),
    # N3 焦点状态（P1「焦点状态注入 [话题簇与锚点] 块」，`docs/decisions/架构评估.md` §3.3
    # :250 明写"**必须登记进** _BLOCK_POLICY，否则 _classify 会把它标 undeclared
    # 并告警"）。块头是 `core/context/threads.py::BLOCK_HEADER` 的**同一字面量**。
    # PERSONA + 不裁（规格 §八【已定 7】：独立常驻块、≤6 行）：它是"这一轮在聊谁
    # 的事"的事实，被预算裁掉就等于把"主角是谁"又交回给模型猜。
    # 开关 `YINYUE_FEATURE_THREADS` 默认 off → 该块**根本不入列**（现状逐字节一致）。
    ("[话题簇与锚点]", "[话题簇与锚点]", "context.threads", PERSONA, None),
    # **S6 的 `[回合归属]` 事实块已随方案 B 一并退役（S7）**：S7 起
    # ``clarify`` 也不起回合（``should_take=False`` 一律不接），那个块的唯一注入
    # 条件（``extra["continuation"] == "clarify"``）因此**永不可达** —— 登记行、
    # 渲染函数、唯一注入点整段删除（不留死代码、不留死测试）。将来 N3 达标、真要做
    # 方案 C（"只允许回应别人对她说的话"）时再从 git 取回：见 ``docs/需求契约.md``
    # §2.9「S6 退役」那一段与 commit ``db08f48``。
    ("[主动触达规则]", "[主动触达规则]", "context.proactive", AUX, None),
    ("[纠错确认]", "[纠错确认]", "context.correction", RELATION, None),
    # N2 规划-执行进 prompt（2026-10-06 第二期，R-C1 的「可读的步骤序列」）。
    # 块头由 ``agents/planner.py::CONTENT_PLAN_HEADER`` 逐字给出；内容由
    # ``agents/planner.py::content_plan_hint`` 从 ``context.extra["content_plan"]``
    # 渲染（**唯一写者**：``Planner.attach_content_plan``，路由两处装配点）。
    # **必须登记**：`_classify` 对认不出的前缀会把它挂到上一块的 source 上
    # （2026-09-24 实测过那类 trace 冒充），漏登记就等于"她看到了什么"查不到 owner。
    # PERSONA + 不裁：本轮的「分几步」被预算裁掉，外层规划就白做了。
    ("[本轮步骤]", "[本轮步骤]", "context.content_plan", PERSONA, None),
    ("[当前局面]", "[当前局面]", "context.state_assembly", PERSONA, None),
    ("[强制规则]", "[强制规则]", "context.blind_vision", RELATION, None),
    # 2026-09-27 解耦接线：反应清单（数据渲染块；默认关，见 features.REACT_PLAYBOOK）。
    # ⑥ 按需记忆（⑨→③→⑥）：**system prompt 侧已无产者**（2026-10-24 读场/按需记忆
    # 整族退役，原 ``agents/conversational.py::_needs_memory_block`` 已删）。记忆入口改走
    # 工具——模型自己调 ``recall_memories``，结果作为工具回执进对话。
    # 登记保留：工具回执的块头也叫 ``[按需记忆]``，万一它落进 system 文本，
    # ``_classify`` 不能把它当 undeclared 挂到上一块的 source 上（那类 trace 冒充
    # 2026-09-24 实测过）。
    ("[按需记忆]", "[按需记忆]", "memory.needs_memory", MEMORY, None),
    # ③→⑥ 读场（2026-10-16 四刀 → 2026-10-24 整族退役）：块头曾在
    # ``agents/conversational.py::_read_block``，该函数与 READ_BLOCK 开关都已删，
    # 这里不再登记读场块头（`test_act_view` / `test_block_policy_producers`
    # 钉着「不许再登记」）。
)

_DEFAULT_POLICY = ("(preamble)", "unknown", PERSONA, None)

# ---------------------------------------------------------------------------
# **S6 的「回合归属事实注入」已退役（S7，方案 B）—— 这里曾经是它的落点**
# ---------------------------------------------------------------------------
# 曾经有的东西（要取回就从 commit ``db08f48`` 拿，别凭记忆重写）：
# 那一族 ``TURN_OWNERSHIP_*`` 常量、``_fact_word``、渲染那个块的函数，
# 以及 ``_build`` 里那唯一一个注入点。
#
# **为什么删（不是"暂时关掉"）**：那个块的唯一门是
# ``extra["continuation"] == "clarify"``（判据不足 ⇒ 只许问）。S7 起
# **``should_take=False`` 一律不接** —— ``clarify`` 那一档在
# ``core/routing/router.py::_on_group_trigger`` 的回合归属门就被拦下了
# （``reason=ownership_clarify_not_taken``），**根本走不到装配器**；而
# ``extra["continuation"]`` 的**唯一**写点（同一函数里 ``if continuation:`` 那一段）
# 也在这门之后 ⇒ ``clarify`` 这个值**再也不会**出现在 ``extra`` 里。
# 于是这个块**永不可达**（不可达的注入 = 熵）：按 anti-entropy 的
# ``code-retirement`` 处置 —— ``delete-first``，登记行 / 渲染函数 / 唯一注入点
# 三处一起删，那个块的专用测试文件同步删（S6 的用例已随本片删除）。
#
# **保留的**：``extra["continuation"]`` 一族键照旧由路由写出（审计 + trace 用），
# 只是**没有 prompt 侧的消费者**了 —— 与 S6 之前的状态一致。
# 将来 N3 达标、真要做方案 C 时再从这个 commit 取回。


def _classify(texts: list[str]) -> list[ContextBlock]:
    """Turn rendered texts into blocks, carrying the last header's policy so
    continuation paragraphs inside ``[人格]`` keep the persona priority."""
    blocks: list[ContextBlock] = []
    current = _DEFAULT_POLICY
    for text in texts:
        first = text.strip().splitlines()[0] if text.strip() else ""
        matched = None
        for prefix, name, source, priority, budget in _BLOCK_POLICY:
            if first.startswith(prefix):
                matched = (name, source, priority, budget)
                break
        if matched is None and first.startswith("你是"):
            matched = ("你是{name}。", "persona.name", PERSONA, None)
        if matched is None and first.startswith("["):
            # 未登记的块：不能沿用上一块的 name/source（2026-09-24 实测出现过
            # 块名顶错的事故），也不能静默丢/砍。
            # 显式取块名 + source=undeclared + PERSONA，并留一条告警。
            header = first[:24]
            matched = (header, "undeclared", PERSONA, None)
            log.warning("context_block_undeclared", header=header)
        if matched is not None:
            current = matched
        name, source, priority, budget = current
        blocks.append(
            ContextBlock(
                name=name,
                content=text,
                source=source,
                priority=priority,
                budget=budget,
            )
        )
    return blocks


def budget_enabled() -> bool:
    try:
        from core import features

        return bool(features.enabled(features.CONTEXT_BUDGET, True))
    except Exception:
        warn("core/context/assembler.py:193 Exception", err=repr(__import__("sys").exc_info()[1]))
        return False

def _looks_like_persona(text: str) -> bool:
    """判断一段块文本是不是"旧 DB 人格块"，用于让卡接管人格位。

    这里只认两种最稳的形态：块头 ``[人格]``，或以 ``你是…。`` 开头的身份首行。
    宁可漏判（多留一块旧人格）也不要误判（把记忆/名单当人格删掉）。
    """
    t = (text or "").lstrip()
    if not t:
        return False
    if t.startswith("[人格]") or "\n[人格]" in t[:40]:
        return True
    return t.startswith("你是") and "。" in t[:24]




def _build(
    snapshot: Any,
    *,
    is_group: bool = False,
    terminal_memory_block: str = "",
    group_recent_context: str = "",
    group_members_block: str = "",
    chat_context_block: str = "",
    room_situation_block: str = "",
    principal: Any = None,
    blind_vision_rule: str = "",
    proactive_rule: str = "",
    correction_rule: str = "",
    time_block: str = "",
    pronoun_rows: Sequence[Any] = (),
    pronoun_text: str = "",
    content_plan_block: str = "",
    state_block: str = "",
    context: Any = None,
    apply: bool | None = None,
) -> tuple[ContextBundle, list[BudgetDecision]]:
    from core.terminal.persona_brain import build_persona_blocks

    texts = build_persona_blocks(
        snapshot,
        is_group=is_group,
        terminal_memory_block=terminal_memory_block,
        group_recent_context=group_recent_context,
        group_members_block=group_members_block,
        chat_context_block=chat_context_block,
        room_situation_block=room_situation_block,
        principal=principal,
        blind_vision_rule=blind_vision_rule,
        proactive_rule=proactive_rule,
        correction_rule=correction_rule,
        time_block=time_block,
        pronoun_rows=pronoun_rows,
        pronoun_text=pronoun_text,
    )
    # 人格块改由人格卡提供（loop/selfcard.py）：有卡时丢掉旧的 DB 人格块，
    # 换成卡渲染的判断面 + 表达面；其余块（记忆/名单/局面/时间/位置）原样保留。
    # 这样"语音快照"和"生产回复"读的是同一张卡（2026-10-24 人格两张皮的收尾）。
    try:
        from loop import selfcard as _sc

        if _sc.active_card():
            _scene = "group" if is_group else "private"
            _card_txt = _sc.judgment(_scene)
            _exp = _sc.expression(_scene)
            if _exp:
                _card_txt = _card_txt + "\n\n[我说话的样子]\n" + _exp
            if _card_txt:
                texts = [t for t in texts if not _looks_like_persona(t)]
                texts.insert(0, _card_txt)
    except Exception as exc:  # noqa: BLE001 - 卡坏了就退回 DB 人格，不能打断装配
        warn("assembler:card_persona_failed", err=repr(exc))
    # 群聊位置投影（Step 7）：Position/Desires 的渲染；off 返回空串 -> 不入列。
    try:
        from core.context.position import render_position_block

        # 会话 id 解析顺序（Step 12/TODO 3 修复）：principal（身份服务解析过）→
        # ``snapshot.turn.target.chat_id``（SpeechSnapshot.turn 是 TurnContext，**没有**
        # 裸 ``chat_id``，旧写法在这里恒为空）→ 兼容替身的裸 ``turn.chat_id`` →
        # context.chat_id。身份服务不可用时也要能出块，否则 [位置] 会静默消失。
        chat_id = str(
            getattr(principal, "chat_id", "")
            or getattr(
                getattr(getattr(snapshot, "turn", None), "target", None),
                "chat_id", "",
            )
            or getattr(getattr(snapshot, "turn", None), "chat_id", "")
            or getattr(context, "chat_id", "")
            or ""
        )
        extra_lines = ()
        try:
            from core.context.situation import position_lines
            from core.execution.data_channels import get_world

            extra_lines = position_lines(get_world(chat_id))
        except Exception:  # noqa: BLE001 - 位置证据读不到就不带
            warn("core/context/assembler.py:260 Exception", err=repr(__import__("sys").exc_info()[1]))
            extra_lines = ()
        position_block = render_position_block(chat_id, is_group, extra_lines=extra_lines)
    except Exception:  # noqa: BLE001 - 位置渲染失败不阻断装配
        warn("core/context/assembler.py:263 Exception", err=repr(__import__("sys").exc_info()[1]))
        position_block = ""
    if position_block:
        texts.append(position_block)
    try:
        from core.context.act_view import act_view_enabled, render_act_view
        from core.execution.data_channels import get_world

        if act_view_enabled(chat_id, is_group):
            act_block = render_act_view(
                get_world(chat_id),
                verify_evidence=True,
            )
            if act_block:
                texts.append(act_block)
    except Exception:  # noqa: BLE001 - 行动视图读不到就不追加
        warn("core/context/assembler.py:275 Exception", err=repr(__import__("sys").exc_info()[1]))
    # N2 的一次性块（``agents/planner.py::content_plan_hint``）：同一条纪律——
    # 调用方渲染好传进来，**只在这里入列**，于是它和别的块一样进 ``_classify``
    # 的登记表、进预算、进 ``context_trace``。空串（`PLAN_LOOP=off`，或本轮
    # 走快路径没有计划）= 不入列，逐字节退回现状。
    if str(content_plan_block or "").strip():
        texts.append(str(content_plan_block).strip())
    # WP-STATE 状态装配：调用方渲染好的 [当前局面]（off/shadow 为空串=不入列）。
    if str(state_block or "").strip():
        texts.append(str(state_block).strip())
    # N3 焦点块（``core/context/threads.py::focus_block_from_context``）：**只在这里
    # 入列**，于是它和别的块一样进 ``_classify`` 的登记表、进预算、进
    # ``context_trace``（"她看到了什么"要能查得到）。开关关 / 取数失败 → 空串 →
    # 不入列（**整块不注入，不半开半关**，规格 §六）。
    if context is not None:
        try:
            from core.context.threads import focus_block_from_context

            focus_block = focus_block_from_context(context)
        except Exception:  # noqa: BLE001 - 焦点块失败 = 这一轮没有它，不弄坏组装
            warn("core/context/assembler.py:295 Exception", err=repr(__import__("sys").exc_info()[1]))
            focus_block = ""
        if str(focus_block or "").strip():
            texts.append(str(focus_block).strip())
    # **S6 的 `[回合归属]` 事实块已随方案 B 退役（S7）**：它的唯一门
    # ``extra["continuation"] == "clarify"`` 永不可达（那一段 ``if continuation:``
    # 写点在路由的回合归属门**之后**，而 S7 起 ``clarify`` 在那里就被 return 了）
    # ⇒ 唯一注入点一并删除，不留死代码。详见本文件 ``_DEFAULT_POLICY``
    # 后面那段说明与 ``docs/需求契约.md`` §2.9。
    blocks = _classify(texts)
    should_apply = budget_enabled() if apply is None else bool(apply)
    if not should_apply:
        return ContextBundle(blocks=tuple(blocks)), []
    return apply_budget(blocks)


def build_bundle(
    snapshot: Any,
    *,
    is_group: bool = False,
    terminal_memory_block: str = "",
    group_recent_context: str = "",
    group_members_block: str = "",
    chat_context_block: str = "",
    room_situation_block: str = "",
    principal: Any = None,
    blind_vision_rule: str = "",
    proactive_rule: str = "",
    correction_rule: str = "",
    time_block: str = "",
    pronoun_rows: Sequence[Any] = (),
    pronoun_text: str = "",
    content_plan_block: str = "",
    state_block: str = "",
    context: Any = None,
    apply: bool | None = None,
) -> ContextBundle:
    bundle, _ = _build(
        snapshot,
        is_group=is_group,
        terminal_memory_block=terminal_memory_block,
        group_recent_context=group_recent_context,
        group_members_block=group_members_block,
        chat_context_block=chat_context_block,
        room_situation_block=room_situation_block,
        principal=principal,
        blind_vision_rule=blind_vision_rule,
        proactive_rule=proactive_rule,
        correction_rule=correction_rule,
        time_block=time_block,
        pronoun_rows=pronoun_rows,
        pronoun_text=pronoun_text,
        content_plan_block=content_plan_block,
        state_block=state_block,
        context=context,
        apply=apply,
    )
    return bundle


def _trace_path() -> Path:
    global _TRACE_PATH
    if _TRACE_PATH is None:
        override = text("YINYUE_CONTEXT_TRACE_PATH").strip()
        if override:
            _TRACE_PATH = Path(override)
        else:
            from core.observability.decision_trace import decision_trace_path

            _TRACE_PATH = decision_trace_path()
    return _TRACE_PATH


def _current_trace_id() -> str:
    """WP-08 8.1：当前回合并发 trace_id；trace 模块不可用时返回空串。"""
    try:
        from core.observability.decision_trace import current_trace_id

        return str(current_trace_id() or "")
    except Exception:  # noqa: BLE001 - 审计绝不拖垮回合
        warn("core/context/assembler.py:374 Exception", err=repr(__import__("sys").exc_info()[1]))
        return ""

def _mark_projection(name: str) -> None:
    """WP-08 8.1b：标记本回合写过哪套子 trace（outcome 行的 projections 用）。"""
    try:
        from core.observability.decision_trace import mark_projection

        mark_projection(name)
    except Exception:  # noqa: BLE001 - 审计绝不拖垮回合
        warn("core/context/assembler.py:383 Exception", err=repr(__import__("sys").exc_info()[1]))

def trace_bundle(
    bundle: ContextBundle,
    *,
    section: str = "system",
    context: Any = None,
    decisions: list = (),
) -> None:
    """Append one record per turn/section: what went in, at what size, what was cut."""
    _mark_projection("context_trace.jsonl")
    try:
        extra = getattr(context, "extra", None) or {}
        platform = str(extra.get("platform") or "")
        chat_id = str(extra.get("chat_id") or "")
        principal = getattr(context, "principal", None)
        if principal is not None:
            platform = platform or str(getattr(principal, "platform", "") or "")
            chat_id = chat_id or str(getattr(principal, "chat_id", "") or "")
        world = extra.get("world_state") if isinstance(extra, dict) else None
        record = {
            "at": datetime.now().isoformat(),
            "decision_trace_id": _current_trace_id(),
            "section": str(section or "system"),
            "world": world if isinstance(world, dict) else {},
            "platform": platform,
            "chat_id": chat_id,
            "total_chars": bundle.total_chars,
            "approx_tokens": sum(
                estimate_tokens(block.content) for block in bundle.blocks
            ),
            "blocks": [
                {
                    "name": block.name,
                    "source": block.source,
                    "priority": block.priority,
                    "chars": block.chars,
                    "approx_tokens": estimate_tokens(block.content),
                    "hash": hashlib.sha1(
                        (block.content or "").encode("utf-8")
                    ).hexdigest()[:12],
                }
                for block in bundle.blocks
            ],
            "decisions": [
                decision.as_dict() if hasattr(decision, "as_dict") else dict(decision)
                for decision in (decisions or ())
            ],
        }
        from core.observability.decision_trace import record_detail

        record_detail("context_detail", record, path=_trace_path())
    except Exception:
        warn("core/context/assembler.py:436 Exception", err=repr(__import__("sys").exc_info()[1]))

def _trace(
    bundle: ContextBundle,
    decisions: list[BudgetDecision],
    *,
    context: Any = None,
) -> None:
    trace_bundle(bundle, section="system", context=context, decisions=decisions)


def read_context_trace(*, chat_id: str = "", limit: int = 20) -> list[dict]:
    """Read recent context-trace records (for debugging "what did she see")."""
    try:
        path = _trace_path()
        if not path.exists():
            return []
        out: list[dict] = []
        with _TRACE_LOCK:
            lines = path.read_text(encoding="utf-8").splitlines()
        for line in reversed(lines):
            try:
                record = json.loads(line)
            except Exception:
                warn("core/context/assembler.py:460 Exception", err=repr(__import__("sys").exc_info()[1]))
                continue
            if str(record.get("kind") or "") != "context_detail":
                continue
            if chat_id and str(record.get("chat_id") or "") != str(chat_id):
                continue
            out.append(record)
            if len(out) >= max(1, int(limit)):
                break
        return out
    except Exception:
        warn("core/context/assembler.py:470 Exception", err=repr(__import__("sys").exc_info()[1]))
        return []


def build_system_prompt(
    snapshot: Any,
    *,
    is_group: bool = False,
    terminal_memory_block: str = "",
    group_recent_context: str = "",
    group_members_block: str = "",
    chat_context_block: str = "",
    room_situation_block: str = "",
    principal: Any = None,
    blind_vision_rule: str = "",
    proactive_rule: str = "",
    correction_rule: str = "",
    time_block: str = "",
    pronoun_rows: Sequence[Any] = (),
    pronoun_text: str = "",
    content_plan_block: str = "",
    state_block: str = "",
    context: Any = None,
    apply: bool | None = None,
    trace: bool = True,
) -> str:
    """Single entry for the system prompt (Phase 1 equivalence + Phase 2 budget)."""
    bundle, decisions = _build(
        snapshot,
        is_group=is_group,
        terminal_memory_block=terminal_memory_block,
        group_recent_context=group_recent_context,
        group_members_block=group_members_block,
        chat_context_block=chat_context_block,
        room_situation_block=room_situation_block,
        principal=principal,
        blind_vision_rule=blind_vision_rule,
        proactive_rule=proactive_rule,
        correction_rule=correction_rule,
        time_block=time_block,
        pronoun_rows=pronoun_rows,
        pronoun_text=pronoun_text,
        content_plan_block=content_plan_block,
        state_block=state_block,
        context=context,
        apply=apply,
    )
    if trace:
        _trace(bundle, decisions, context=context)
    return bundle.render()


__all__ = [
    "DEFAULT_BUDGETS",
    "trace_bundle",
    "build_bundle",
    "build_system_prompt",
    "budget_enabled",
    "read_context_trace",
]
