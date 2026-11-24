"""只读任务的推进器（U-10 第一步）。

一件事怎么往前走，只有这一个函数
--------------------------------
判决定「下一步做什么」（`Plan.steps`），这个函数按顺序把它们跑掉，跑不动就停，
并把停的原因交回给判。**它自己不认识出口、不认识工具包**：动手的能力由调用点注入
（`execute=...`），所以 U-10 可以先只接只读工具，行为面一行都没改。

三条不许越过的线
----------------
1. **没有批准，不许跑 `effects != "read"` 的步骤**（`external_send` / `ui_control` /
   `write_local`）。停下来的形状是 `stop="need_approval"`，同时往台账写一条
   `awaiting{kind: "approval"}` —— 出口那侧的 `pending_approval` 才有东西可挂。
2. **只在 `Step.safe_point` 上停**。`safe_point=False` 的步骤表示「跑起来就不许中断」
   （提交表单、扣款），预算到点也不许把它掰成两半——这是「半个订单」的唯一防线。
3. **预算一本账**：`TOOL_CALLS_PER_TURN` 管判这一回合，`TOOL_CALLS_PER_JOB` 管这件事
   从生到死的累计；两个都记在台账上，谁都改不了别人的账。
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, replace
from typing import Any, Callable, Iterable, Mapping, Sequence

from core.contracts.plan import (
    MAX_TURNS_PER_JOB,
    TOOL_CALLS_PER_JOB,
    Job,
    Step,
)
from safelog import warn

log = logging.getLogger(__name__)

#: 停下来的原因。新增只许追加（报表与用例按字面值比对）。
STOP_REASONS: tuple[str, ...] = (
    "done",          # 没有下一步了
    "need_approval",  # 下一步要人批
    "awaiting",      # 本来就在等人（输入/审批）
    "budget",        # 这件事的工具预算用完
    "turns",         # 这件事推进的回合数用完
    "state",         # 已经 done / cancelled / expired
    "failed",        # 某一步执行失败，且停在安全点上
    "no_executor",   # 调用点没给 execute，只能空转
    "shadow",        # 写操作在影子档：判定「该写」，一个字节都没真写
    "safe_point",    # 长任务在安全点交还注意力，下一步留在台账
    "goal_closed",   # 它关联的目标已被取消/关闭，执行前复查拦下（2026-11-18）
    "goal_source_revoked",  # 目标所依据的用户请求已撤回，执行前复查拦下
)

#: 推进器自己"办不下去"的停因：一出现就该把这件事记成 abandoned（半途而废），
#: 不许无声无息地留在 open，也不许跟"主人叫停的 cancelled"混成一类
#: （方案文档 §12.5 / E-26：有词表 ≠ 统一，得有人真去写）。
GIVE_UP_STOPS: tuple[str, ...] = ("budget", "turns")

StepStatus = str  # done | failed | need_approval | shadow


@dataclass(frozen=True)
class StepRun:
    n: int
    action: str
    status: StepStatus
    detail: str = ""
    #: 这一步**真拿到**的凭据（产物路径 / 队列号 / 回执号）。执行器没回就是空。
    refs: tuple[str, ...] = ()


@dataclass(frozen=True)
class AdvanceResult:
    job: Job
    ran: tuple[StepRun, ...] = ()
    stop: str = "done"
    tool_calls: int = 0
    #: 这次停在哪一档（`auto` / `grant` / `confirm`）；没停就不填。
    tier: str = ""

    @property
    def progressed(self) -> bool:
        return bool(self.ran) and any(r.status == "done" for r in self.ran)

    def summary(self) -> str:
        """给留痕/日志的一行，不带情绪词。"""
        done = sum(1 for r in self.ran if r.status == "done")
        return f"job {self.job.job_id} 第{self.job.step_n}步 stop={self.stop} ran={done} tools={self.tool_calls}"


def step_action_hash(job_id: str, step: Step) -> str:
    """批的是「哪一个动作」：job + 步号 + 动作原文，不含语气词。"""
    raw = f"{job_id}:{step.n}:{step.action}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


def _refs_of(value: Any) -> tuple[str, ...]:
    """执行器回的「这一步留下了什么凭据」：字符串或序列，去空、去重、保序。"""
    if isinstance(value, str):
        items: tuple[Any, ...] = (value,)
    elif isinstance(value, (list, tuple, set)):
        items = tuple(value)
    else:
        items = ()
    out: list[str] = []
    for item in items:
        text = str(item or "").strip()
        if text and text not in out:
            out.append(text)
    return tuple(out)


def _as_result(out: Any) -> tuple[bool, str, dict[str, Any]]:
    """`execute` 的返回值归一：True/False、或 (ok, detail)。

    第三个返回值是**状态差异**（`state_before` / `state_after` / `changed`）：
    写操作没有幂等键，能不能算「办成了」全靠它，台账里必须留痕。
    """
    if isinstance(out, tuple) and out:
        ok = bool(out[0])
        detail = str(out[1]) if len(out) > 1 else ""
        return ok, detail, {}
    if isinstance(out, Mapping):
        extra = {
            k: out[k]
            for k in ("state_before", "state_after", "changed")
            if out.get(k) is not None
        }
        refs = _refs_of(out.get("evidence_refs"))
        if refs:
            extra["evidence_refs"] = refs
        return bool(out.get("ok")), str(out.get("detail", "")), extra
    return bool(out), "", {}


def advance(
    job: Job,
    *,
    execute: Callable[[Step], Any] | None = None,
    store: Any = None,
    actor: str = "judge",
    budget: int = TOOL_CALLS_PER_JOB,
    allow_effects: Sequence[str] = ("read",),
    approved: Callable[[Step], bool] | None = None,
    authorize: Callable[[Step], Any] | None = None,
    yield_at_safe_point: bool = False,
    abort_if: Callable[[Job], Any] | None = None,
) -> AdvanceResult:
    """把 `job` 的下一步往前推，能推几步推几步，推不动就带着原因回来。"""
    if job.state in ("done", "cancelled", "expired"):
        return AdvanceResult(job=job, stop="state")

    def _abort_reason(current: Job) -> str:
        """执行前复查：调用方的闸门（台账已叫停 / 目标已关闭）压过这份内存快照。

        `job` 是调用方早先读出来的一行；这一拍到执行之前，别人可能已经取消。
        只信快照就会照旧动手，所以每一步动手前都要再问一次。
        """
        if abort_if is None:
            return ""
        try:
            return str(abort_if(current) or "").strip()
        except Exception as exc:  # noqa: BLE001 - 复查炸了按未取消继续，但要留痕
            log.warning(f"job_abort_recheck_failed: err={repr(exc)[:120]}")
            return ""

    _early = _abort_reason(job)
    if _early:
        return AdvanceResult(job=job, stop=_early)
    if job.awaiting.kind not in ("", "none"):
        return AdvanceResult(job=job, stop="awaiting")
    if job.turns_total >= MAX_TURNS_PER_JOB:
        # 2026-11-03 P2-3：回合用尽 ≡ 办不下去，必须落一条 abandoned。
        # 这条是**早退**（一步都没跑），所以落盘要写在这里，不能只写在循环末尾。
        if store is not None:
            store.append(
                job.job_id,
                "abandoned",
                payload={"stop": "turns", "step_n": job.step_n, "tool_calls": job.tool_calls_total},
                actor=actor,
            )
        return AdvanceResult(job=replace(job, state="abandoned"), stop="turns")

    def _is_approved(step: Step) -> bool:
        if step.approved:
            return True
        if approved is not None:
            return bool(approved(step))
        if store is not None:
            return bool(
                store.is_approved(job.job_id, step.n, step_action_hash(job.job_id, step))
            )
        return False

    def _as_auth(out: Any) -> tuple[bool, str]:
        """`authorize` 的返回值归一：bool / (bool, tier) / {ok, tier}。"""
        if isinstance(out, tuple) and out:
            return bool(out[0]), str(out[1]) if len(out) > 1 else ""
        if isinstance(out, Mapping):
            return bool(out.get("ok")), str(out.get("tier", ""))
        return bool(out), ""

    def _gate(step: Step) -> tuple[bool, str]:
        """这一步过不过：免批准名单 -> 人的逐次批准 -> 三级授权。"""
        if step.effects in allow_effects:
            return True, ""
        if _is_approved(step):
            # 人已经逐个批过这一步：比任何长期授权都硬。
            return True, "approved"
        if authorize is not None:
            try:
                ok, tier = _as_auth(authorize(step))
            except Exception as exc:  # noqa: BLE001 - 授权判定炸了只能当没授权
                warn("core/jobs/runner.py:150 Exception", err=repr(exc))
                return False, f"authorize_error:{type(exc).__name__}"
            return ok, tier
        return False, "confirm"

    def _await_approval(
        current: Job,
        step: Step,
        ran: list[StepRun] | None = None,
        calls: int = 0,
        tier: str = "",
    ) -> AdvanceResult:
        """停在「等人批准」。**与有没有执行器无关**：等批准是人的事，不是工具的事。"""
        if store is not None:
            store.append(
                current.job_id,
                "awaiting",
                payload={"kind": "approval", "who": current.owner_person_id, "tier": tier},
                actor=actor,
            )
        waiting = replace(
            current,
            state="awaiting",
            awaiting=type(current.awaiting)(kind="approval", who=current.owner_person_id),
        )
        runs = list(ran or [])
        runs.append(StepRun(n=step.n, action=step.action, status="need_approval"))
        return AdvanceResult(
            job=waiting, ran=runs, stop="need_approval", tool_calls=calls, tier=tier
        )

    first = job.next_step()
    if first is not None:
        ok, tier = _gate(first)
        if not ok:
            # 先问批准，再谈有没有执行器：否则 `on` 但没绑工具注册表时，
            # 「等你点头」这件事永远不会出现在回执里，判也就无从知道有一件事卡住了。
            return _await_approval(job, first, tier=tier)

    if execute is None:
        return AdvanceResult(job=job, stop="no_executor")

    ran: list[StepRun] = []
    calls = 0
    current = job
    stop = "done"

    while True:
        step = current.next_step()
        if step is None:
            if store is not None:
                store.append(current.job_id, "done", payload={}, actor=actor)
            current = replace(current, state="done")
            stop = "done"
            break

        _late = _abort_reason(current)
        if _late:
            return AdvanceResult(job=current, ran=tuple(ran), stop=_late, tool_calls=calls)

        ok_gate, tier = _gate(step)
        if not ok_gate:
            return _await_approval(current, step, ran, calls, tier=tier)

        # 预算按**台账里的累计**算（每跑一步就已累加），不再叠加本地 `calls`，
        # 否则同一次 advance 里会把同一步数两次，预算直接腰斩一半。
        if current.tool_calls_total >= budget:
            stop = "budget"
            break

        try:
            raw = execute(step)
            ok, detail, extra = _as_result(raw)
        except Exception as exc:  # noqa: BLE001 - 执行器的异常一律记为这一步失败
            warn("core/jobs/runner.py:219 Exception", err=repr(exc))
            raw, ok, detail, extra = None, False, f"{type(exc).__name__}: {exc}", {}

        if isinstance(raw, Mapping) and raw.get("shadow"):
            # 影子档：判定「该写」，但一个字节都不写。三条都不许做——
            # 不算失败（她没做错事）、不推进步号（下次还得从这一步起）、
            # 不写 step_done / 不算 done（否则台账会把「没办」记成「办完了」）。
            ran.append(
                StepRun(n=step.n, action=step.action, status="shadow", detail=detail)
            )
            stop = "shadow"
            break

        calls += 1
        if not ok:
            ran.append(StepRun(n=step.n, action=step.action, status="failed", detail=detail))
            stop = "failed"
            if store is not None:
                store.append(
                    current.job_id,
                    "step_failed",
                    payload={
                        "step_n": step.n,
                        "tool_calls": 1,
                        "detail": detail,
                        **extra,
                    },
                    actor=actor,
                )
            break

        if store is not None:
            store.append(
                current.job_id,
                "step_done",
                payload={"step_n": step.n, "tool_calls": 1, "detail": detail, **extra},
                actor=actor,
            )
        current = replace(
            current,
            step_n=max(current.step_n, step.n),
            tool_calls_total=current.tool_calls_total + 1,
        )
        ran.append(
            StepRun(
                n=step.n,
                action=step.action,
                status="done",
                detail=detail,
                refs=tuple(extra.get("evidence_refs") or ()),
            )
        )
        # 长任务在安全点交还注意力：本回合只完成到这一步，下一步留在台账里，
        # 下一拍从 `step_n` 继续。`safe_point=False` 的步骤不允许在半步中断。
        if yield_at_safe_point and step.safe_point and current.next_step() is not None:
            stop = "safe_point"
            break

    # 一次 advance = 判推进了一回合：`turn` 事件只记一次（预算分母，不重复计）。
    if store is not None:
        store.append(current.job_id, "turn", payload={}, actor=actor)
    final = replace(current, turns_total=current.turns_total + 1)

    # 2026-11-03 P2-3：把"办不下去"真的写成 abandoned。
    # 以前这里什么都不写——账本里就只剩 done 和"人叫停的 cancelled"，
    # 于是"半途而废率"永远是 0。不是没发生，是没人记（方案文档 §12.5 / E-26）。
    # 判据用 GIVE_UP_STOPS（预算/回合用尽），不含 no_executor（那是接线问题，不是她办不下去）
    # 也不含 state（已经 done/cancelled/expired，不该再盖一个 abandoned）。
    if store is not None and stop in GIVE_UP_STOPS:
        store.append(
            current.job_id,
            "abandoned",
            payload={
                "stop": stop,
                "step_n": current.step_n,
                "tool_calls": current.tool_calls_total,
            },
            actor=actor,
        )
        final = replace(final, state="abandoned")

    return AdvanceResult(job=final, ran=tuple(ran), stop=stop, tool_calls=calls)


#: 出口的第六态（`docs/decisions/出口.md` §5）：挂起等批准，**一个字节都没发**。
PENDING_APPROVAL: str = "pending_approval"


def approval_request(
    job: Job, step: Step | None = None, *, tier: str = "", reason: str = ""
) -> dict[str, Any]:
    """拼出口要的那一条回执：`status=pending_approval`，等私聊本人点头才动。

    回执形状与 `core.contracts.delivery.Receipt` 同名同义，只是这里是 dict——
    本模块不 import 出口（规则 11），出口自己把它包成 Receipt。
    """
    target = step if step is not None else next(
        (s for s in job.steps if s.n > job.step_n), None
    )
    n = target.n if target is not None else job.step_n
    action = target.action if target is not None else ""
    return {
        "status": PENDING_APPROVAL,
        "kind": "plan",
        "target": job.chat_id,
        "ack": "none",
        "reason": reason or (f"needs_{tier}" if tier else ""),
        "tier": tier,
        "platform_msg_id": "",
        "client_msg_id": "",
        "adapter": "",
        "detail": f"{job.job_id} 第 {n} 步需批准：{action}"[:200],
        "job_id": job.job_id,
        "step_n": n,
        "action_hash": step_action_hash(job.job_id, target) if target is not None else "",
        "who": job.owner_person_id or "",
        # 只有私聊本人才算授权：群里第三个人说「订吧」不算
        "channel": "private",
    }


def pending_approval_receipts(result: AdvanceResult) -> list[dict[str, Any]]:
    """这一回合要挂起等批准的回执；没停在 `need_approval` 就一条都没有。"""
    if result.stop != "need_approval":
        return []
    return [approval_request(result.job, tier=result.tier)]


def readouts_for_prompt(store: Any, chat_id: str) -> list[str]:
    """判的输入第 6 样。`off`/`shadow` 下恒空（shadow 只写不读）。"""
    if store is None:
        return []
    return list(store.readouts(chat_id))
