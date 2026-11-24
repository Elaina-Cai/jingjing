# -*- coding: utf-8 -*-
"""[L2 准入层] 决定“这一回合她能看到什么”（设计：docs/archive/架构文档_2026-09-24.md §三）。

只处理强指向回合（@她 / 引用她 / 拍她 / 点名）；弱指向返回 None，调用方保持现状。

流程：
  候选池（最近 N 行，去 self-echo）→ judge（独立小调用，逐行 A/B/C + 场面备注）
  → 折叠（A 原文 ≤6 行；B 折成场面一句话；C 丢弃）
  → 兜底（judge 失败 → T2 规则：当前消息+被引用+与发送人 30min 往返 → 全窗）

模式（YINYUE_ADMISSION_MODE）：
  off    不跑（默认）
  shadow 每回合判定 + 写审计，不改线上生成（run_admission 返回 None）
  on     返回准入文本，调用方用它替换全窗

模式读法统一走 ``core.features.mode``（唯一三态入口）；**默认与未知值都回落
本门的 off**（=不做事），显式写在 ``mode()`` 里，不再各处自写一份读法。

**本层不再独立成层**（``docs/全链路架构审计与目标架构.md`` §2.2 的 L2 行、§3.1 的
"必须收编的现有近似实现"①）：逐行相关性（A/B/C + 场面备注）**收编但降级**——它继续
当"这一回合她能看到什么"的**窗口过滤**，产物**不再当决策**。

**2026-10-23 起这里不再调模型**：judge（agent_name 曾为 admission-judge）与 N1
（`core/context/intent.py::run_intent`）都已退役——「要不要说、对谁说、他要什么」
交给会话里的模型自己判。本模块只剩**规则窗口**（候选池 → T2 最近的与发送人往返），
它的产物只是「她能看到哪几行」，`fallback` 从 `"t2"`/`"full"` 里取。

实测依据：output/abc_接梗实验_20260924.md（J 实验：路过型 5/5 干净、引用依赖 5/5 接得住）；
消融：不碰 [局况]/[姿态与分寸]（另有分寸职责）。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from threading import Lock
from typing import Any, Callable, Sequence

import structlog

from core import features
from safelog import warn
from core.infra.envflags import text
from core.infra import tempo

log = structlog.get_logger()

CANDIDATE_LIMIT = 18      # 候选池行数（实验口径）
A_CAP = 6                 # A 堆原文上限
#: 「原文没给你」那句声明，只在给原文的行数 **≤ 这个数** 时才出现。
#:
#: 为什么不按"裁了就说"（2026-10-11 实测 191 条真实准入记录后收窄）：
#:   * 候选池恒 ``CANDIDATE_LIMIT``＝18 行，而 judge 判 A 的中位数只有 **2** 行
#:     （A=0 1.1% / A=1 28% / A=2 40% / A=3 21% / A≥4 8%）；
#:   * 按"shown < total"来判，声明会在 **98.9%** 的回合出现——那不是"例外声明"，
#:     是每轮 45 字的固定噪音，还会诱导她拿"我信息不全"当免责话术；
#:   * 裁 B/C 行本来就是准入的设计语义（A＝"回应这条消息**最少需要**的行"），
#:     不是缺陷，"裁了就说"等于把她自己系统的常规工作方式报成异常。
#: 收窄到 0 只命中 1.1%，且那两条真实回合都是**一行原文都没给**（空 @ / 纯图片），
#: 正是她唯一可能凭空编的场合。
#: judge 单次上限（秒）。2026-10-10 日志实测（n=185）：成功 p50=1.40s / p99=2.73s /
#: max=2.83s，超时率 2.7%（3.0s 距最大成功只剩 0.17s，几乎没有余量）。
#: 取 6s = 2.2×p99，与 N1 判官的 10s 同一算法；改值同步 tests/test_judge_timeout_budget.py。
EXCHANGE_WINDOW_SECONDS = tempo.s("admission.exchange")  # T2 兜底：与发送人最近 30 分钟

TRACE_PATH = Path("logs") / "decision_trace.jsonl"  # 8.1d：默认并入 canonical
TRACE_PATH_ENV = "YINYUE_ADMISSION_TRACE_PATH"
_TRACE_MAX_BYTES = 4_000_000
_TRACE_KEEP_LINES = 4000
_TRACE_LOCK = Lock()


def trace_path() -> Path:
    """审计落盘路径：env 可改道；默认并入 canonical decision_trace（8.1d）。"""
    raw = (os.environ.get(TRACE_PATH_ENV) or "").strip()
    if raw:
        return Path(raw)
    from core.observability.decision_trace import decision_trace_path

    return decision_trace_path()

MODE_OFF = features.MODE_OFF
MODE_SHADOW = features.MODE_SHADOW
MODE_ON = features.MODE_ON

# 弱指向 / 不指她的 reason 名单：这些不跑准入
_WEAK_REASONS = frozenset({
    "pat_not_self", "other_addressed", "broadcast", "self_sent",
    "group_deferred_to_state", "acknowledgement", "closing",
})
# 强指向标记（子串匹配 decision.reason）
_STRONG_MARKERS = (
    "mentioned", "name_call", "patted_in_group", "patted_in_private",
    "quoted", "reply_to_bot", "explicitly_addressed",
)



# ------------------------------------------------------------------ mode / 开关
def mode() -> str:
    """YINYUE_ADMISSION_MODE：off（默认）/ shadow / on。

    唯一三态入口 ``core.features.mode``；**默认与未知值都回落 off**（=不做事，
    行为与现状一致）——显式写在这里，不靠读法自己猜。
    """
    return features.mode(
        "admission_mode", MODE_OFF, env_var=features.ADMISSION_MODE_ENV,
    )


def enabled_for(chat_id: str) -> bool:
    """模式非 off + 会话在窗口白名单（准入与窗口同源）+ 可选 ADMISSION_CHATS 收紧。"""
    if mode() == MODE_OFF or not str(chat_id or "").strip():
        return False
    try:
        from core.context.chat_window import window_enabled_for

        if not window_enabled_for(str(chat_id)):
            return False
    except Exception:
        warn("core/context/admission.py:124 Exception", err=repr(__import__("sys").exc_info()[1]))
        return False
    raw = text("YINYUE_ADMISSION_CHATS")
    if raw:
        chats = {part.strip() for part in raw.split(",") if part.strip()}
        if str(chat_id) not in chats:
            return False
    return True


def strong_addressed(reason: str) -> bool:
    """decision.reason 是否属于强指向（@/引用/拍/点名）。"""
    text = str(reason or "").strip().lower()
    if not text or text in _WEAK_REASONS:
        return False
    return any(marker in text for marker in _STRONG_MARKERS)


def should_ask_admission(chat_id: str, reason: str) -> bool:
    """这一回合**本来就会**跑准入 judge 吗（= 会花一次 LLM 调用）。

    给 N1 合并用：``True`` 时 N1 把意图判定折进这次 judge（净增 0 调用），``False``
    时准入不在管辖范围、N1 只能并列调用（+1 次）。判据与 ``evaluate`` 的门控同源
    （``enabled_for`` + ``strong_addressed``），不另起一套口径。
    """
    return bool(enabled_for(chat_id) and strong_addressed(reason))


def _is_group_chat(chat_id: str) -> bool:
    """群聊判据（与 ``core/context/chat_window`` 同源：``db.models.is_group_chat_id``）。

    S8.2：私聊不判受话——一对一里收件人恒为对方，那一问没有信息增量。认不出形态时
    **按群聊处理**：群里漏判收件人正是历史事故的来源，多问一次的代价只是几个 token。
    """
    try:
        from db.models import is_group_chat_id

        return bool(is_group_chat_id(str(chat_id or "")))
    except Exception:  # noqa: BLE001 - 判据不可用 -> 当群聊（宁可多问）
        warn("core/context/admission.py:162 Exception", err=repr(__import__("sys").exc_info()[1]))
        text = str(chat_id or "")
        return text.endswith("@chatroom") or text.startswith("group:")








#: N1 载荷的字段名：合并解析靠它认出"这一段就是意图段"（不靠键名猜）。




# ------------------------------------------------------------------ 解析 / 折叠








def _summary(text: Any, limit: int = 40) -> str:
    flat = " ".join(str(text or "").split())
    return flat[:limit] + ("…" if len(flat) > limit else "")


def _sha12(text: Any) -> str:
    return hashlib.sha1(str(text or "").encode("utf-8")).hexdigest()[:12]


# ------------------------------------------------------------------ 候选池
def _fmt_line(row: Any) -> str:
    """候选行文本：与窗口逐行同一渲染（含 canonical 归属后缀）。

    2026-09-30 收口：这里不再自己拼行——``chat_window._fmt_full`` 已经是唯一
    的逐行渲染入口，归属后缀（``（→ @Elaina）`` / `` [归属不明]``）随之一致，
    judge 看到的"对谁说"和生成侧看到的是同一份事实。
    """
    try:
        from core.context.chat_window import _fmt_full

        return str(_fmt_full(row))
    except Exception:
        warn("core/context/admission.py:208 Exception", err=repr(__import__("sys").exc_info()[1]))
        stamp = getattr(row, "occurred_at", None)
        clock = stamp.strftime("%H:%M") if hasattr(stamp, "strftime") else "--:--"
        return f"[{clock}] {getattr(row, 'speaker', '?')}: {getattr(row, 'content', '')}"


def _dedupe_self_echoes(rows: Sequence[Any]) -> list[Any]:
    try:
        from core.context.chat_window import _dedupe_self_echoes as _dedupe

        return list(_dedupe(rows))
    except Exception:
        warn("core/context/admission.py:219 Exception", err=repr(__import__("sys").exc_info()[1]))
        return list(rows)


def candidate_pairs(
    rows: Sequence[Any], *, now: datetime, current: str, sender_name: str = "",
) -> list[tuple[str, Any | None]]:
    """最近 N 行（去 self-echo）+ 当前消息（若尚未入库则补上）：行 + 对应 raw row。

    第二个元素供轮次通道复用（``row is None`` = 合成的当前消息行，没有原始行）。
    """
    moment = now or datetime.now()
    kept = []
    for row in _dedupe_self_echoes(list(rows)):
        stamp = getattr(row, "occurred_at", None)
        try:
            if stamp is not None and stamp > moment:
                continue
        except Exception:
            warn("core/context/admission.py:237 Exception", err=repr(__import__("sys").exc_info()[1]))
        kept.append(row)
    from core.context.answer_links import annotate_candidate_line, links_by_request

    # 链用**去重前**的行算：self-echo 去重可能丢掉带 parent 的 reply 分身。
    links = links_by_request(list(rows))
    pairs = [
        (annotate_candidate_line(_fmt_line(row), row, links), row)
        for row in kept[-CANDIDATE_LIMIT:]
    ]
    current_text = " ".join(str(current or "").split())
    if current_text and not any(current_text in line for line, _ in pairs[-3:]):
        pairs.append(
            (f"[{moment:%H:%M}] {sender_name or '?'}: {current_text}", None)
        )
    return pairs[-CANDIDATE_LIMIT:]


def candidate_lines(
    rows: Sequence[Any], *, now: datetime, current: str, sender_name: str = "",
) -> list[str]:
    """最近 N 行（去 self-echo）+ 当前消息（若尚未入库则补上），供 judge 编号。"""
    return [
        line
        for line, _ in candidate_pairs(
            rows, now=now, current=current, sender_name=sender_name
        )
    ]


def t2_pairs(
    rows: Sequence[Any], *, now: datetime, current: str, sender_name: str = "",
) -> list[tuple[str, Any | None]]:
    """T2 兜底候选：当前消息 + 与发送人 30 分钟内往返（≤4 条），带原始行。"""
    moment = now or datetime.now()
    recent: list[Any] = []
    for row in rows:
        speaker = str(getattr(row, "speaker", "") or "")
        is_self = bool(getattr(row, "is_self", False))
        if not (speaker == sender_name or (is_self and sender_name and sender_name in str(getattr(row, "content", "") or ""))):
            continue
        stamp = getattr(row, "occurred_at", None)
        try:
            if stamp is not None and (moment - stamp).total_seconds() > EXCHANGE_WINDOW_SECONDS:
                continue
        except Exception:
            warn("core/context/admission.py:283 Exception", err=repr(__import__("sys").exc_info()[1]))
        recent.append(row)
    pairs = [(_fmt_line(row), row) for row in recent[-4:]]
    current_text = " ".join(str(current or "").split())
    if current_text and not any(current_text in line for line, _ in pairs[-3:]):
        pairs.append(
            (f"[{moment:%H:%M}] {sender_name or '?'}: {current_text}", None)
        )
    return pairs[-4:]


def t2_render(pairs: Sequence[tuple[str, Any | None]]) -> str:
    lines = [line for line, _ in pairs]
    if not lines:
        return ""
    return "【最近对话·原文·仅直接相关】\n" + "\n".join(lines)


def t2_text(
    rows: Sequence[Any], *, now: datetime, current: str, sender_name: str = "",
) -> str:
    """T2 规则兜底：当前消息 + 与发送人 30 分钟内往返（≤4 条）。空 = ''。"""
    return t2_render(
        t2_pairs(rows, now=now, current=current, sender_name=sender_name)
    )


@dataclass
class AdmissionResult:
    """一次准入的结果（审计与调用方共用）。"""

    text: str = ""                 # 准入后的窗口块内容（"" = 退回全窗）
    fallback: str = "judge"        # judge | t2 | full
    labels: dict[str, str] = field(default_factory=dict)
    scene: str = ""
    judge_ms: int = 0
    candidate_n: int = 0
    reason: str = ""
    chat_id: str = ""
    candidates: list[str] = field(default_factory=list)  # judge 实际看到的候选池（证据）
    rows: list[Any] = field(default_factory=list)        # 轮次通道可复用的原始行（judge=A 行 / t2 行）
    scene_text: str = ""                                 # 轮次模式下的文本块（只留【场面备注】）
    # N1 合并通道：judge 同一次回答里给出的意图载荷 + 原始文本（交给 llm.schema 校验）。
    raw: str = ""

    @property
    def label_counts(self) -> dict[str, int]:
        counts = {"A": 0, "B": 0, "C": 0}
        for value in self.labels.values():
            if value in counts:
                counts[value] += 1
        return counts


async def evaluate(
    *,
    chat_id: str,
    reason: str,
    current: str,
    sender_name: str = "",
    now: datetime | None = None,
    rows: Sequence[Any] | None = None,
    loader: Callable[[], Sequence[Any]] | None = None,
    llm: Any = None,
    is_group: bool | None = None,
    no_model: bool = False,
) -> AdmissionResult | None:
    """跑一次准入窗口（不做模式门控，供测试/影子复用）。None = 不在管辖范围。

    只走规则：候选池（``candidate_pairs``）→ T2（与发送人最近 30 分钟的往返）
    → ``fallback`` 记 ``"t2"`` 或 ``"full"``。**不调模型**，`scene_text` 恒空。
    """
    if not enabled_for(chat_id) or not strong_addressed(reason):
        return None
    # S8.2：私聊不判受话——一对一里收件人恒为对方（``turn_addressee`` 第 1 格取
    # 触发者），那一问没有信息增量。没显式给形态时按 chat_id 认。
    group = is_group if is_group is not None else _is_group_chat(chat_id)
    moment = now or datetime.now()
    if rows is None:
        if loader is None:
            from core.context.chat_window import load_messages

            loader = lambda: load_messages(chat_id, days=2)  # noqa: E731
        try:
            rows = list(loader())
        except Exception:
            warn("core/context/admission.py:369 Exception", err=repr(__import__("sys").exc_info()[1]))
            rows = []
    pairs = candidate_pairs(rows or [], now=moment, current=current, sender_name=sender_name)
    lines = [line for line, _ in pairs]
    result = AdmissionResult(
        chat_id=str(chat_id), reason=str(reason), candidate_n=len(lines),
        candidates=list(lines),
    )
    # 2026-10-23：准入 judge 已退役——「要不要说 / 对谁说」由模型在会话里自己判
    # （perceive + 生成回合），代码这里只剩一条机械事实：当前消息 + 与发送人
    # 30 分钟内的往返（``t2_pairs``），有就用它当窗口，没有就退回全窗。
    fallback_pairs = t2_pairs(
        rows or [], now=moment, current=current, sender_name=sender_name,
    )
    fallback_text = t2_render(fallback_pairs)
    if fallback_text:
        result.text, result.fallback = fallback_text, "t2"
        result.rows = [row for _, row in fallback_pairs if row is not None]
    else:
        result.text, result.fallback = "", "full"
        result.rows = []
    result.scene_text = ""
    log.info(
        "admission_rules_only", chat_id=chat_id,
        candidates=len(lines), fallback=result.fallback,
    )
    return result


async def run_admission_result(
    *,
    chat_id: str,
    reason: str,
    current: str,
    sender_name: str = "",
    now: datetime | None = None,
    rows: Sequence[Any] | None = None,
    loader: Callable[[], Sequence[Any]] | None = None,
    llm: Any = None,
    is_group: bool | None = None,
    no_model: bool = False,
) -> AdmissionResult | None:
    """路由调用入口（返回完整结果，供轮次通道复用）。

    off/弱指向 → None；shadow → 只写审计返回 None；on → 结果（text 为空也返回，
    调用方按「rows 为空 → 保持全窗」处理）。

    shadow 返回 ``None`` 是刻意的：N1 的**调用方**因此看不到这次结果，也就不会被它
    改变行为（``applied=False``）——这正是"shadow 不改任何行为"的强制点。
    """
    result = await evaluate(
        chat_id=chat_id, reason=reason, current=current, sender_name=sender_name,
        now=now, rows=rows, loader=loader, llm=llm,
        is_group=is_group,
        no_model=no_model,
    )
    if result is None:
        return None
    _write_trace(result)
    if mode() != MODE_ON:
        return None
    return result


async def run_admission(
    *,
    chat_id: str,
    reason: str,
    current: str,
    sender_name: str = "",
    now: datetime | None = None,
    rows: Sequence[Any] | None = None,
    loader: Callable[[], Sequence[Any]] | None = None,
    llm: Any = None,
) -> str | None:
    """路由调用入口：off/弱指向 → None；shadow → 只写审计返回 None；on → 准入文本。

    on 模式下 text 为空（judge 与 T2 都失败）时也返回 None，调用方保持全窗。
    """
    result = await run_admission_result(
        chat_id=chat_id, reason=reason, current=current, sender_name=sender_name,
        now=now, rows=rows, loader=loader, llm=llm,
    )
    if result is None:
        return None
    return result.text or None


def _current_trace_id() -> str:
    """WP-08 8.1：当前回合并发 trace_id；trace 模块不可用时返回空串。"""
    try:
        from core.observability.decision_trace import current_trace_id

        return str(current_trace_id() or "")
    except Exception:  # noqa: BLE001 - 审计绝不拖垮回合
        warn("core/context/admission.py:463 Exception", err=repr(__import__("sys").exc_info()[1]))
        return ""

def _mark_projection(name: str) -> None:
    """WP-08 8.1b：标记本回合写过哪套子 trace（outcome 行的 projections 用）。"""
    try:
        from core.observability.decision_trace import mark_projection

        mark_projection(name)
    except Exception:  # noqa: BLE001 - 审计绝不拖垮回合
        warn("core/context/admission.py:472 Exception", err=repr(__import__("sys").exc_info()[1]))

def _write_trace(result: AdmissionResult) -> None:
    """审计落盘：判断 / 证据 / 结果 三段（B5c），附兼容旧读法的扁平键。

    * **判断** ``judgment``——这轮为什么进入准入、judge 怎么判的；
    * **证据** ``evidence``——judge 实际看到的候选池 + 折叠进 prompt 的 A 行，
      每行带 ``sha12``（可对账、可回放，不落原文以防隐私）；
    * **结果** ``result``——准入产出了什么、是否真的替换了全窗。

    顶层继续保留 ``fallback`` / ``labels`` / ``scene`` 等扁平键，供
    ``scripts/admission_board.py`` 与历史行统一读取。

    只记规则窗口这一件事：候选几条、兜底到哪一档、替换了没有。
    """
    _mark_projection("admission_trace.jsonl")
    labels = result.label_counts
    scene = _summary(result.scene, 60)
    a_lines = [
        {"text": _summary(line, 40), "sha12": _sha12(line)}
        for line in (result.text or "").splitlines() if line.startswith("[")
    ][:A_CAP] if result.fallback == "judge" else []
    # 候选池逐行留痕：**标签必须逐行记**——只记 A/B/C 的计数时，"被删掉的是哪 12 行"
    # 无从复核，判断层就成了不可校准的黑箱（2026-09-24 加）。
    labels_by_index = {str(k): str(v) for k, v in (result.labels or {}).items()}
    evidence_lines = [
        {
            "i": index + 1,
            "label": labels_by_index.get(str(index + 1), ""),
            "text": _summary(line, 40),
            "sha12": _sha12(line),
        }
        for index, line in enumerate(result.candidates or ())
    ]
    admitted = bool(result.text)
    stamp = datetime.now().isoformat()
    record = {
        "at": stamp,
        "decision_trace_id": _current_trace_id(),
        "trace_id": _sha12(f"{result.chat_id}|{result.reason}|{stamp}"),
        # 判断
        "judgment": {
            "reason": result.reason,
            "strong_addressed": strong_addressed(result.reason),
            "fallback": result.fallback,
            "labels": labels,
            "scene": scene,
            "judge_ms": result.judge_ms,
        },
        # 证据
        "evidence": {
            "candidate_n": result.candidate_n,
            "candidates": evidence_lines,
            "a_kept": a_lines,
        },
        # 结果
        "result": {
            "text_chars": len(result.text or ""),
            "admitted": admitted,
            "applied": admitted and mode() == MODE_ON,
        },
        # ---- 兼容旧读法的扁平键 ----
        "mode": mode(),
        "chat_id": result.chat_id,
        "reason": result.reason,
        "fallback": result.fallback,
        "candidate_n": result.candidate_n,
        "labels": labels,
        "scene": scene,
        "a_lines": a_lines,
        "judge_ms": result.judge_ms,
        "chars": len(result.text or ""),
        # ---- N1 合并通道（净增 0 调用的对账列）----
    }
    try:
        from core.observability.decision_trace import record_detail

        record_detail("admission_detail", record, path=trace_path())
    except Exception as exc:  # noqa: BLE001 - 审计永远不能拖垮主路径
        log.debug("admission_trace_failed", error=str(exc)[:120])

__all__ = [
    "A_CAP", "CANDIDATE_LIMIT", "MODE_OFF", "MODE_ON", "MODE_SHADOW", "AdmissionResult",
    "candidate_lines", "enabled_for", "evaluate", "mode",
    "run_admission", "run_admission_result", "should_ask_admission",
    "strong_addressed", "t2_text", "trace_path",
]
