"""判想动手时的「计划器」：把「要办什么」问成一段严格 JSON（U-18 姊妹件）。

为什么需要它：让模型在聊天正文里自己夹一段 ``<plan>`` 块这条路，真机验过**不可靠**
（提示词确实进了 prompt——``plan_hint_injected chars=217``——但她连着三轮都没附）。
所以改成**结构性**做法：判断这句话「要不要动手」由代码决定（动词表，便宜且可解释），
真要动手时再用一次很小、很硬的模型调用问她「哪几步」；模型只输出 JSON，解析失败就当没这回事。

三条纪律：
1. 门是 ``JOB_LEDGER``：``off`` 档连动词表都不查、一次模型都不调，逐字节不变。
2. 计划器**只产出步骤**，不执行、不发消息、不碰台账之外的东西；开事仍走 ``ingest_plan``。
3. 失败一律静默降级（记一行日志）：问不出计划，等于这一轮只是聊天。
"""
from __future__ import annotations

import json
import os
import logging
from typing import Any, Awaitable, Callable, Mapping

from core import features
from safelog import warn
from core.infra.envflags import text

log = logging.getLogger(__name__)

PLANNER_MAX_CHARS = 3000
STEP_MAX_CHARS = 120
STEPS_MAX = 12
ACTION_VERBS = (
    "发", "查", "搜", "订", "买", "改", "删", "打开", "点一下", "填", "提交",
    "找", "看一下", "写", "记", "提醒", "安排", "规划", "约", "叫", "转告",
    "通知", "下单", "预约", "撤回", "翻译", "总结", "说一句", "说句话", "说一声",
    "告诉", "喊", "念", "读一下", "回复",
)
PLANNER_SYSTEM = "你是执行计划器。只输出一行 JSON，不要解释、不要代码块围栏。"
CLASSIFY_SYSTEM = "只回答一个词：yes 或 no。不要解释。"


def planner_mode() -> str:
    """唯一读点：计划器跟着「在手的事」同一把开关。"""
    return features.mode(features.JOB_LEDGER, features.MODE_OFF)


def looks_like_task(text: str) -> bool:
    """这句话是不是要她动手。只认动词，不猜意图——便宜的确定性触发。"""
    body = " ".join(str(text or "").split())
    if len(body) < 4:
        return False
    return any(word in body for word in ACTION_VERBS)


def build_prompt(*, user_text: str, reply_text: str = "", chat: str = "") -> str:
    return (
        "对方说：" + " ".join(str(user_text or "").split())[:600] + "\n"
        + ("她已经打算这样回：" + " ".join(str(reply_text or "").split())[:300] + "\n" if reply_text else "")
        + (f"所在会话：{chat}\n" if chat else "")
        + "判断：这句话要求她去办一件事（用工具或动手），还是只是聊天？\n"
        + "只输出 JSON：{\"act\": true/false, \"goal\": \"一句话目标\", \"steps\": [{\"action\": \"做什么\", \"effects\": \"read\"}]}\n"
        + "action 写法：工具名:参数；对外发消息写 send:会话|正文；effects 只能是 read / write_local / ui_control / external_send，默认 read。\n"
        + f"最多 {STEPS_MAX} 步；只是聊天就输出 {{\"act\": false}}。"
    )


def build_classify_prompt(*, user_text: str) -> str:
    return (
        "对方说：" + " ".join(str(user_text or "").split())[:400] + "\n"
        "这句话是在要求助手去动手办事（用工具、查东西、操作界面、对外发消息），还是只是聊天？\n"
        "只是聊天就答 no；要求办事就答 yes。只输出 yes 或 no。"
    )


async def classify_task(*, user_text: str, send: Callable[..., Awaitable[str]]) -> bool:
    """L2 语义兜底：一次极小的调用，只回 yes/no。判不出来一律当「不办」（不猜）。"""
    raw = str(await send(CLASSIFY_SYSTEM, build_classify_prompt(user_text=user_text)) or "")
    head = raw.strip().lower()[:8]
    return head.startswith("yes") or head.startswith("y")


def _first_object(text: str) -> dict | None:
    raw = str(text or "").strip()
    if raw.startswith("```"):
        parts = raw.split("```")
        raw = parts[1] if len(parts) > 1 else raw
        raw = raw.lstrip("json").strip()
    start = raw.find("{")
    if start < 0:
        return None
    depth = 0
    for i in range(start, len(raw)):
        if raw[i] == "{":
            depth += 1
        elif raw[i] == "}":
            depth -= 1
            if depth == 0:
                try:
                    data = json.loads(raw[start:i + 1])
                except Exception:
                    warn("core/jobs/planner.py:95 Exception", err=repr(__import__("sys").exc_info()[1]))
                    return None
                return data if isinstance(data, dict) else None
    return None


def parse_planner_json(text: str) -> dict | None:
    """模型输出 → 可交给 ``ingest_plan`` 的 ``plan``；不合格一律 ``None``（不猜）。"""
    data = _first_object(text)
    if not data:
        return None
    if data.get("act") is False:
        return None
    goal = " ".join(str(data.get("goal") or "").split())[:200]
    steps_raw = data.get("steps")
    if not goal or not isinstance(steps_raw, (list, tuple)) or not steps_raw:
        return None
    steps: list[dict[str, Any]] = []
    for index, item in enumerate(steps_raw[:STEPS_MAX], 1):
        if not isinstance(item, Mapping):
            return None
        action = " ".join(str(item.get("action") or "").split())[:STEP_MAX_CHARS]
        if not action:
            return None
        effects = str(item.get("effects") or "read").strip()
        if effects not in ("read", "write_local", "ui_control", "external_send"):
            return None
        steps.append({"n": index, "action": action, "effects": effects})
    return {"goal": goal, "steps": steps}


def _default_send() -> Callable[..., Awaitable[str]]:
    from core.bootstrap import get_llm

    llm = get_llm()

    async def send(system: str, user: str) -> str:
        return await llm.chat(
            system=system,
            user=user,
            agent_name="planner",
            temperature=0.0,
            max_tokens=400,
        )

    return send


def single_call_plan() -> bool:
    """B 路径开关：``YINYUE_SINGLE_CALL_PLAN``（默认开）。

    开 = 判一次输出同时给「话」与 ``<plan>``（规划器不再单独调用模型）；
    关 = 退回旧的两调用路径（留一档可回退，不必改代码）。
    """
    raw = str(text("YINYUE_SINGLE_CALL_PLAN", "1") or "").strip().lower()
    return raw not in ("0", "false", "off", "no")

async def maybe_open_job(
    *,
    user_text: str,
    reply_text: str = "",
    chat_id: str,
    turn_id: str = "",
    chat: str = "",
    send: Callable[..., Awaitable[str]] | None = None,
    store: Any = None,
    prime: bool = True,
) -> Any | None:
    """要动手就问一次「哪几步」，问到了就开一件事。任何失败都不影响这一句话。"""
    if single_call_plan():
        # B 路径：判在一次输出里同时给「话」和 ``<plan>``，出口那边（outbound 的
        # ``_consume_plan_block``）负责摘块与开事。这里再问一次计划器就是**每回合两次模型点**
        # （破 R2），而且两套事实各说一句——真机症状：事开成了，她嘴上还说「我发不了」。
        log.info("job_planner_skipped reason=single_call_plan")
        return None
    if planner_mode() == features.MODE_OFF or not chat_id:
        return None
    if store is None:
        try:
            from core.jobs.store import default_store

            store = default_store()
        except Exception as exc:  # noqa: BLE001
            log.warning("job_planner_store_unavailable error=%s", repr(exc))
            return None
    caller = send or _default_send()
    by_verbs = looks_like_task(user_text)
    if by_verbs:
        log.info("planner_gate source=verbs hit=True")
    else:
        try:
            ok = await classify_task(user_text=user_text, send=caller)
        except Exception as exc:  # noqa: BLE001 - 兜底判不出来就当聊天
            log.warning("planner_gate_failed error=%s", repr(exc))
            return None
        log.info("planner_gate source=semantic hit=%s", ok)
        if not ok:
            return None
    try:
        raw = await caller(PLANNER_SYSTEM, build_prompt(user_text=user_text, reply_text=reply_text, chat=chat or chat_id))
    except Exception as exc:  # noqa: BLE001 - 计划器不许拖垮回合
        log.warning("job_planner_failed error=%s", repr(exc))
        return None
    plan = parse_planner_json(raw)
    if plan is None:
        log.info("job_planner_no_plan chat_id=%s chars=%d", chat_id, len(str(raw or "")))
        return None
    try:
        from core.jobs.ingest import ingest_plan

        job = ingest_plan({"plan": plan}, chat_id=chat_id, trace_id=turn_id, store=store)
    except Exception as exc:  # noqa: BLE001
        log.warning("job_planner_ingest_failed error=%s", repr(exc))
        return None
    if job is None:
        # 同会话同目标会算出同一个 job_id：重复说同一件事时 ingest 会返回 None。
        # 静默丢掉是错的（人看到的是「她答应了但什么也没发生」），要把它接回原来那件、继续推进。
        try:
            from core.jobs.ingest import plan_job_id

            from core.jobs.store import default_store

            ledger = store or default_store()
            again = plan_job_id(chat_id, turn_id, str(plan.get("goal", "")))
            existing = ledger.load(again)
        except Exception as exc:  # noqa: BLE001
            log.warning("job_planner_reuse_failed error=%s", repr(exc))
            existing = None
        if existing is None:
            log.warning("job_planner_dropped chat_id=%s reason=no_job", chat_id)
            return None
        log.info("job_planner_reused chat_id=%s job_id=%s state=%s", chat_id, existing.job_id, existing.state)
        job = existing
    if not prime:
        return job
    log.info("job_planner_opened chat_id=%s job_id=%s steps=%d", chat_id, job.job_id, len(job.steps))
    # 开完立刻推一次：不然这件事会停在 state=open、`awaiting_kind=none`，
    # 而人看不到「在等你批准」——下一次 `_job_tick` 才推的话，要等下一句话。
    # 只放行 read：写/界面/对外发一律停在 need_approval（这正是我们要的那一行回执）。
    try:
        from core.jobs.runner import advance
        from core.jobs.executor import READ_EFFECT, read_only_executor

        result = advance(
            job,
            execute=read_only_executor(),
            store=store,
            allow_effects=(READ_EFFECT,),
        )
        log.info(
            "job_planner_primed chat_id=%s job_id=%s stop=%s step_n=%d",
            chat_id,
            job.job_id,
            result.stop,
            result.job.step_n,
        )
        return result.job
    except Exception as exc:  # noqa: BLE001 - 推不动也不许影响这一句话
        log.warning("job_planner_prime_failed error=%s", repr(exc))
        return job
