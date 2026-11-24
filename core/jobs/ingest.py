"""U-10 第二刀：判给的 `plan` → 在手的事里真开一件事。

判说「我要办这件事」，这一段把它变成台账里的一行 `created`。**它不是执行器**：
开完仍是 `open`，下一步由 :func:`core.jobs.runner.advance` 推，写操作要人批。

三档（开关 `JOB_LEDGER`）：

* ``off``    —— **不读载荷、不碰台账**（生产默认，逐字节等同重构前）；
* ``shadow`` —— 只校验 + 记 ``job_plan_shadow``：本来会开成什么样只进日志；
* ``on``     —— 校验通过就 ``store.create()``；上限/台账异常一律退化成「没开」。

纪律（`docs/decisions/办_在手的事.md`）：

* 判的载荷**不能**把自己的步骤标成已批准（``approved`` 一律抹成 ``False``）——
  只有人给的 :class:`core.contracts.plan.Approval` 能置真；
* 同一 ``(chat_id, trace_id, goal)`` 重复投递 = **同一件事**（幂等键），不开第二件；
* 本模块不 I/O、不 import ``core.speech`` / ``core.routing`` / ``agents`` /
  ``scheduler`` / ``web``（``scripts/check_layering.py`` 规则 11）。
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Mapping
from typing import Any

import structlog

from core.contracts.plan import (
    TOOL_CALLS_PER_JOB,
    Awaiting,
    Job,
    Plan,
    Step,
)

log = structlog.get_logger(__name__)

#: 步骤的四种 effects（与工具包同名同义）；``external_send`` 一律走出口注册表。
EFFECTS: tuple[str, ...] = ("read", "write_local", "ui_control", "external_send")

#: 模型常常不写 ``effects``：按动作前缀推一个**偏保守**的默认值。
#: 发消息一律 ``external_send``（收不回），点/输/跳一律 ``ui_control``；认不出来才算只读。
_TOOL_EFFECTS: dict[str, str] = {
    "send": "external_send",
    "send_message": "external_send",
    "gui_click": "ui_control",
    "gui_input": "ui_control",
    "gui_navigate": "ui_control",
}


def _infer_effects(action: str) -> str:
    """按「工具:参数」的工具名推 effects（模型的载荷里缺这一项时用）。"""
    tool = str(action or "").partition(":")[0].strip().lower()
    return _TOOL_EFFECTS.get(tool, "read")

#: 一件事最多几步。超过 ``TOOL_CALLS_PER_JOB`` 的步数没有意义（预算早用完了）。
MAX_PLAN_STEPS: int = TOOL_CALLS_PER_JOB

GOAL_MAX_CHARS: int = 200
ACTION_MAX_CHARS: int = 120


def _mode() -> str:
    """唯一读点：``JOB_LEDGER`` 三态（off / shadow / on）。"""
    from core import features

    return features.mode(features.JOB_LEDGER, features.MODE_OFF)


def plan_job_id(chat_id: str, trace_id: str, goal: str) -> str:
    """幂等键 → job_id。同一回合同一目标永远同一个 id（重复投递不开第二件）。"""
    raw = f"{chat_id}\x1f{trace_id}\x1f{goal}"
    return "job-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def parse_plan(payload: Any) -> Plan | None:
    """把判的载荷里的 ``plan`` 解析成 :class:`Plan`；不合格一律 ``None``（不猜）。"""
    if not isinstance(payload, Mapping):
        return None
    raw = payload.get("plan")
    if not isinstance(raw, Mapping):
        return None
    goal = str(raw.get("goal") or "").strip()
    if not goal or len(goal) > GOAL_MAX_CHARS:
        return None
    steps_raw = raw.get("steps")
    if not isinstance(steps_raw, (list, tuple)) or not steps_raw:
        return None
    if len(steps_raw) > MAX_PLAN_STEPS:
        return None
    steps: list[Step] = []
    for index, item in enumerate(steps_raw, 1):
        if not isinstance(item, Mapping):
            return None
        action = str(item.get("action") or "").strip()
        if not action or len(action) > ACTION_MAX_CHARS:
            return None
        effects = str(item.get("effects") or "").strip() or _infer_effects(action)
        if effects not in EFFECTS:
            return None
        n = item.get("n", index)
        if not isinstance(n, int) or isinstance(n, bool) or n < 1:
            return None
        steps.append(
            Step(
                n=n,
                action=action,
                effects=effects,
                safe_point=bool(item.get("safe_point", True)),
                approved=False,  # 判不能自己批自己：批次只从人来
            )
        )
    if len({s.n for s in steps}) != len(steps):
        return None
    steps.sort(key=lambda s: s.n)
    return Plan(
        goal=goal,
        criteria=str(raw.get("criteria") or "").strip()[:200],
        steps=tuple(steps),
        awaiting=Awaiting(),
        job_id=_opt_str(raw.get("job_id")),
    )


def _opt_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def build_job(
    plan: Plan,
    *,
    chat_id: str,
    trace_id: str = "",
    owner_person_id: str | None = None,
    goal_id: str = "",
) -> Job:
    """`Plan` → 台账里那件 ``open`` 的事（纯函数，不写库）。"""
    return Job(
        job_id=plan.job_id or plan_job_id(chat_id, trace_id, plan.goal),
        chat_id=chat_id,
        goal=plan.goal,
        criteria=str(getattr(plan, "criteria", "") or ""),
        owner_person_id=owner_person_id,
        goal_id=str(goal_id or ""),
        state="open",
        steps=list(plan.steps),
        awaiting=Awaiting(),
    )


def ingest_plan(
    payload: Any,
    *,
    chat_id: str,
    store: Any = None,
    trace_id: str = "",
    owner_person_id: str | None = None,
    goal_id: str = "",
) -> Job | None:
    """判的一段 ``plan`` → 开一件事。返回那件事；没开成一律 ``None``。"""
    mode = _mode()
    if mode == _mode_off() or not chat_id:
        return None
    plan = parse_plan(payload)
    if plan is None:
        return None
    job = build_job(
        plan,
        chat_id=chat_id,
        trace_id=trace_id,
        owner_person_id=owner_person_id,
        goal_id=goal_id,
    )
    if mode == _mode_shadow():
        log.info(
            "job_plan_shadow",
            chat_id=chat_id,
            job_id=job.job_id,
            steps=len(job.steps),
            effects=[s.effects for s in job.steps],
            trace_id=trace_id,
        )
        return None
    if store is None:
        from core.jobs.store import default_store

        store = default_store()
    existing = store.load(job.job_id)
    if existing is not None:
        # 幂等键撞上一个「已经死了」的件（cancelled/done/expired）时不能原样返回：
        # 那件永远推不动、也永远批不了，于是同一句话再也开不出新事
        # （真机 2026-10-21 13:08：清理过的 job-e20bc55… 被反复复用，stop=state，
        #  回「可以」也没人听）。给目标加一个短随机后缀，另开一件新的。
        if str(getattr(existing, "state", "")) in ("cancelled", "done", "expired", "abandoned"):
            job.job_id = plan_job_id(chat_id, f"{trace_id}#{int(time.time() * 1000)}", job.goal)
            log.info(
                f"job_ingest_revived chat_id={chat_id} dead_id={existing.job_id} "
                f"new_id={job.job_id} state={getattr(existing, 'state', '')}"
            )
        else:
            return existing
    # 同一句话只该有一件事。direct 前置（core/jobs/direct.py）与模型的 send_plan
    # 是两个生产者，trace_id 不同就会造出两个 job_id，于是同一句命令开出两件
    # （真机 2026-10-21 16:38：job-71326f6… 与 job-f10dd73…，你批的是后一件）。
    # 判据：同会话 + 还活着的件 + 下一步动作逐字相同 → 复用最早的那件。
    try:
        _first_action = job.steps[0].action if job.steps else ""
        if _first_action:
            for _live in store.open_jobs(chat_id):
                if str(getattr(_live, "state", "")) not in ("open", "awaiting"):
                    continue
                _next = _live.next_step()
                if _next is not None and _next.action == _first_action:
                    log.info(
                        "job_ingest_deduped",
                        chat_id=chat_id,
                        job_id=job.job_id,
                        reused=_live.job_id,
                        action=_first_action[:80],
                        trace_id=trace_id,
                    )
                    return _live
    except Exception as exc:  # noqa: BLE001 - 去重失败不许拖垮开事
        log.info("job_ingest_dedup_failed", chat_id=chat_id, error=repr(exc))
    from core.jobs.store import JobLimitExceeded

    try:
        store.create(job)
    except JobLimitExceeded as exc:
        log.info("job_plan_rejected", chat_id=chat_id, reason="limit", error=str(exc))
        return None
    except Exception as exc:  # noqa: BLE001 - 开一件事失败不许拖垮回合
        log.warning("job_plan_failed", chat_id=chat_id, error=repr(exc))
        return None
    log.info(
        "job_plan_created",
        chat_id=chat_id,
        job_id=job.job_id,
        steps=len(job.steps),
        trace_id=trace_id,
    )
    return job


def _mode_off() -> str:
    from core import features

    return features.MODE_OFF


def _mode_shadow() -> str:
    from core import features

    return features.MODE_SHADOW
