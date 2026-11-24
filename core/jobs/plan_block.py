"""把回复里的 ``<plan>{...}</plan>`` 摘出来消费掉（B 路径的桥）。

为什么在这里：模型会把计划写在正文里，而正文会直接发出去——不摘就是让 JSON 泄漏到聊天里；
摘了却不消费，就成了「说写进计划了、其实什么都没有」的空话。两件事必须一起做。
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Mapping

log = logging.getLogger(__name__)

PLAN_RE = re.compile(r"<plan>\s*(\{.*?\})\s*</plan>", re.S)

#: 判的输出契约（`办_在手的事.md` §20）。**必须存在**：`agents/conversational.py::_plan_hint_block`
#: 读的就是这个名字，缺了会被 try/except 吞掉，于是提示词一个字符都进不去（真机踩过：
#: 模型连着三轮没附计划，人看到的是「我手里没有发消息的口」）。
PLAN_HINT = (
    "要真办就附 "
    '<plan>{"goal":"一句话","steps":[{"n":1,"action":"工具:参数"}]}</plan>'
    "。要往别处发消息写 send:目标|正文——那是你先开一件事、等主人批准，不是你自己发；所以别答「我发不了」。"
)


def extract(text: str) -> tuple[str, dict[str, Any] | None]:
    """返回（干净正文, 计划）。没有块就原样返回。块坏了也不拦正文。"""
    raw = str(text or "")
    match = PLAN_RE.search(raw)
    if match is None:
        return raw, None
    clean = (raw[: match.start()] + raw[match.end() :]).strip()
    try:
        plan = json.loads(match.group(1))
    except Exception as exc:  # noqa: BLE001 - 块坏了当没有，但要说一声
        log.warning("plan_block_unparsable error=%s", repr(exc))
        return clean, None
    return clean, plan if isinstance(plan, Mapping) else None


extract_plan = extract


def consume(plan: Mapping[str, Any], *, chat_id: str, store: Any = None, trace_id: str = "") -> str:
    """把计划变成一件事并推一次。返回 job_id；不成返回空串（但一定留日志）。"""
    try:
        from core.jobs.capability_gate import gate as _cap_gate
        from core.jobs.executor import READ_EFFECT, read_only_executor
        from core.jobs.ingest import ingest_plan
        from core.jobs.runner import advance
        from core.jobs.store import default_store

        ledger = store or default_store()
        # 能力闸：这件事我们到底有没有口？没有就别开（开了也办不成，只会消耗信任）。
        _actions = " ".join(str(step.get("action") or "") for step in (plan.get("steps") or []) if isinstance(step, Mapping))
        _verdict = _cap_gate(f"{plan.get('goal') or ''} {_actions}", action=_actions)
        if not _verdict.get("ok"):
            log.info("plan_block_refused chat_id=%s reason=%s", chat_id, _verdict.get("kind"))
            return ""
        from core.jobs import slots as _slots

        _miss = _slots.missing_slots(" ".join([str(plan.get("goal") or ""), _actions]))
        if _miss:
            # 缺参数不开事：开了也办不成，只会让她反复问同一句。
            log.info("plan_block_need_slot chat_id=%s slots=%s", chat_id, ",".join(_miss))
            return ""
        job = ingest_plan({"plan": dict(plan)}, chat_id=chat_id, trace_id=trace_id, store=ledger)
        if job is None:
            log.warning("plan_block_dropped chat_id=%s reason=no_job plan=%r", chat_id, dict(plan))
            return ""
        log.info("plan_block_opened chat_id=%s job_id=%s steps=%d", chat_id, job.job_id, len(job.steps))
        result = advance(job, execute=read_only_executor(), store=ledger, allow_effects=(READ_EFFECT,))
        log.info("plan_block_primed chat_id=%s job_id=%s stop=%s", chat_id, job.job_id, result.stop)
        return job.job_id
    except Exception as exc:  # noqa: BLE001 - 摘块消费不许拖垮回合
        log.warning("plan_block_consume_failed error=%s", repr(exc))
        return ""
