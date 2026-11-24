"""直接下令：主人一句话里已经点名「往哪发、发什么」→ 不必等模型自觉调工具。

真机 2026-10-21：三个回合 `step=judge note=calls=0` —— 她一次 `send_plan` 都没调，
只在解释「我按不到发送键」。这不是措辞问题，是**把「要不要办事」交给模型自觉**
这件事本身不可靠。这里加一道程序化前置：认出这种句式就直接开一件事，
批准由 `policy.is_owner_ordered` 判（主人点名 = 授权），之后 `_job_tick` 正常推进。

纪律：
* 只认**显式点名**的三类句式（在…说一句 / 往…发一句 / 跟…说）；
* 正文少于 4 字不认（「好」「嗯」不构成指令）；
* 只在私聊里用（群里的指令先不猜）；
* 任何异常只记一行日志，绝不影响回合。
"""

from __future__ import annotations

import logging
import re
import uuid
from typing import Any

log = logging.getLogger(__name__)

MIN_BODY_CHARS = 4
MAX_TARGET_CHARS = 30

_PATTERNS = (
    re.compile(r"在\s*(?P<target>[^，。,：:\n]{2,30}?)\s*(?:里|中)?\s*说(?:一)?句(?:话)?\s*[：:，,]?\s*(?P<body>.+)"),
    re.compile(r"往\s*(?P<target>[^，。,：:\n]{2,30}?)\s*(?:里|中)?\s*发(?:一)?句(?:话)?\s*[：:，,]?\s*(?P<body>.+)"),
    re.compile(r"跟\s*(?P<target>[^，。,：:\n]{2,30}?)\s*说\s*[：:，,]?\s*(?P<body>.+)"),
)

_STRIP = "「」『』\"'”‘’ "


def parse_order(text: str) -> tuple[str, str] | None:
    """从一句话里认出（目标, 正文）；认不出返回 None。"""
    raw = str(text or "").strip()
    if not raw:
        return None
    for pattern in _PATTERNS:
        match = pattern.search(raw)
        if not match:
            continue
        target = match.group("target").strip(_STRIP)
        body = match.group("body").strip(_STRIP)
        if not target or len(target) > MAX_TARGET_CHARS:
            continue
        if len(body) < MIN_BODY_CHARS:
            continue
        return target, body
    return None


def open_direct(
    text: str,
    *,
    chat_id: str,
    store: Any = None,
    trace_id: str = "",
    goal_id: str = "",
) -> str:
    """认出「往…发…」就直接开一件事；返回 job_id（认不出或不成功返回空串）。"""
    order = parse_order(text)
    if order is None:
        return ""
    target, body = order
    # trace_id 必须每回合不同：job_id = hash(chat_id, trace_id, goal)，
    # 常量 trace 会让「同一句话第二次说」撞上**已经 done 的旧事**，
    # 于是 ingest_plan 返回既有的那件、推进器无事可做，
    # 外面看到的就是「她答应了，群里什么都没发」（真机 2026-10-21 16:13）。
    trace = str(trace_id or "").strip() or f"direct-{uuid.uuid4().hex}"
    try:
        from core.jobs.ingest import ingest_plan

        payload = {
            "plan": {
                "goal": f"往「{target}」说一句：{body}",
                "steps": [{"action": f"send:{target}|{body}", "effects": "external_send"}],
            }
        }
        job = ingest_plan(
            payload,
            chat_id=str(chat_id or ""),
            store=store,
            trace_id=trace,
            goal_id=goal_id,
        )
        if job is None:
            log.info("job_direct_order_dropped target=%s reason=ingest_none", target)
            return ""
        log.info(f"job_direct_order_opened job_id={job.job_id} target={target} chars={len(body)}")
        return str(job.job_id)
    except Exception as exc:  # noqa: BLE001 - 前置失败就当没认出来
        log.warning(f"job_direct_order_failed:{type(exc).__name__}")
        return ""


__all__ = ["parse_order", "open_direct"]
