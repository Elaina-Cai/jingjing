"""把主人一句吩咐变成能一步步办下去的事（2026-11-04 用户选的「全 C」）。

三条纪律，每一条都对应一次真事故：

1. **只认主人私聊里的一句祈使**：用 `core.jobs.ask.is_owner_private` 判定，群聊、别人、
   她自己的自述一律不开单 —— 后者是 E-48：把自述当成"主人吩咐过"。
2. **必须有真实来路的凭据**：`msg_id` 为空就不开单。没有真实入站消息，就没有"谁吩咐过"。
3. **开单就要真发出去**：执行器直接走 `loop/ports.say_many(..., reason="job")`，所以每一步
   都落在 `outbound_queue` 里、都要过 `loop/outbound_gate` 的去重闸门。2026-11-04 的读数：
   31 件历史 job 里 `outbound_queue` 中 `turn_id like 'job%'` 的行数是 **0** —— 台账写 done、
   消息从来没发出去（E-59）。这里就是为了不再重犯。

`core.jobs.runner.advance()` 是同步的，而发消息是 async，所以执行器起一个短线程自己
`asyncio.run`：引擎主循环本身是 asyncio（`loop/run.py`），在它里面直接 `asyncio.run`
会抛 "asyncio.run() cannot be called from a running event loop"。
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import threading
from typing import Any

log = logging.getLogger(__name__)

#: 全 C：外发不再停下来等点头。`read` 本来就不问，`external_send` 是这次放开的。
OPEN_EFFECTS: tuple[str, ...] = ("read", "external_send")

#: 动作写法与 `core/jobs/direct.py` 一致：``send:<目标>|<正文>``。
SEND_TOOL = "send"

#: 一次发消息最多等多久（秒）。端口/键鼠卡住时不许把一拍拖死。
SEND_TIMEOUT_S = 60.0

#: 归一化时要抹掉的尾巴：只用来判"是不是同一件事"，不参与发送。
_TAIL = "。.…、，,~～!！?？：: \"'「」『』"


def _store(store: Any = None) -> Any:
    """拿到台账。传进来就用传进来的（测试与调用点注入）。"""
    if store is not None:
        return store
    from core.jobs.store import default_store

    return default_store()


def _norm_goal(text: Any) -> str:
    t = " ".join(str(text or "").split())
    return t.rstrip(_TAIL)


def _is_owner_private(chat_id: str) -> bool:
    """这个会话是不是"主人私聊"。查不出来就当不是（宁可不开单）。"""
    try:
        from core.jobs.ask import is_owner_private

        return bool(is_owner_private(chat_id))
    except Exception as exc:  # noqa: BLE001 - 判定炸了只能当不是主人
        log.warning(f"job_bridge_owner_check_failed: err={repr(exc)[:120]}")
        return False


def open_from_owner(
    chat_id: str,
    text: str,
    *,
    msg_id: str = "",
    source_ref: str = "",
    scope: Any = None,
    at: str = "",
    store: Any = None,
    trace_id: str = "",
) -> dict:
    """主人私聊里一句「往…说…」→ 开一件事。

    返回 ``{"ok": bool, "why": str, "job_id": str, "target": str, "body": str, "goal": str}``。
    ``why`` 只会是这些词：``no_proof`` / ``not_owner_private`` / ``not_an_order`` /
    ``store_unreadable`` / ``already_open`` / ``open_failed`` / ``""``。
    """
    cid = str(chat_id or "")
    if not str(msg_id or "").strip():
        return {"ok": False, "why": "no_proof", "job_id": ""}
    if not _is_owner_private(cid):
        return {"ok": False, "why": "not_owner_private", "job_id": ""}
    from core.jobs.direct import open_direct, parse_order

    order = parse_order(text)
    if order is None:
        return {"ok": False, "why": "not_an_order", "job_id": ""}
    target, body = order
    goal = "往「%s」说一句：%s" % (target, body)
    st = _store(store)
    try:
        for job in st.open_jobs(cid) or []:
            if _norm_goal(getattr(job, "goal", "")) == _norm_goal(goal):
                return {
                    "ok": False,
                    "why": "already_open",
                    "job_id": str(getattr(job, "job_id", "") or ""),
                    "target": target,
                    "body": body,
                    "goal": goal,
                }
    except Exception as exc:  # noqa: BLE001 - 查不了重复就别开，免得同一件事排两遍
        log.warning(f"job_bridge_dup_check_failed: err={repr(exc)[:120]}")
        return {"ok": False, "why": "store_unreadable", "job_id": ""}
    goal_id = _direct_goal_id(cid, msg_id=msg_id, scope=scope)
    try:
        job_id = str(
            open_direct(
                text,
                chat_id=cid,
                store=st,
                trace_id=str(trace_id or "").strip() or ("owner-%s" % str(msg_id).strip()),
                goal_id=goal_id,
            )
            or ""
        )
    except Exception as exc:  # noqa: BLE001 - 开单失败就当没开，外面照常回话
        log.warning(f"job_bridge_open_failed: err={repr(exc)[:120]}")
        return {"ok": False, "why": "open_failed", "job_id": ""}
    if not job_id:
        return {"ok": False, "why": "open_failed", "job_id": "", "target": target, "body": body, "goal": goal}
    try:
        goal_id = _ensure_direct_goal(
            cid,
            text,
            goal,
            goal_id=goal_id,
            source_ref=str(source_ref or msg_id).strip(),
            scope=scope,
            at=at,
        )
    except Exception as exc:  # noqa: BLE001 - Job 没有可撤回来源就宁可取消
        log.warning(f"job_bridge_goal_bind_failed: job_id={job_id} err={repr(exc)[:120]}")
        try:
            st.append(
                job_id,
                "cancelled",
                payload={"stop": "goal_bind_failed"},
                actor="loop",
            )
        except Exception as cancel_exc:  # noqa: BLE001 - 下一拍仍会被无来源守卫看见
            log.warning(
                f"job_bridge_goal_bind_cancel_failed: job_id={job_id} "
                f"err={repr(cancel_exc)[:120]}"
            )
        return {"ok": False, "why": "open_failed", "job_id": "", "target": target, "body": body, "goal": goal}
    log.info("job_opened job_id=%s chat=%s target=%s chars=%d", job_id, cid, target, len(body))
    return {
        "ok": True,
        "why": "",
        "job_id": job_id,
        "goal_id": goal_id,
        "target": target,
        "body": body,
        "goal": goal,
    }


def _direct_goal_id(chat_id: str, *, msg_id: str, scope: Any = None) -> str:
    if isinstance(scope, dict):
        platform = str(scope.get("platform") or "")
        account = str(scope.get("account_id") or "")
    else:
        platform = str(getattr(scope, "platform", "") or "")
        account = str(getattr(scope, "account_id", "") or "")
    raw = "\x1f".join((platform, account, str(chat_id or ""), str(msg_id or "")))
    return "direct:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def _ensure_direct_goal(
    chat_id: str,
    text: str,
    goal: str,
    *,
    goal_id: str,
    source_ref: str,
    scope: Any = None,
    at: str = "",
) -> str:
    """Job 建好后补上它的请求目标；失败时由调用方取消 Job。"""
    from core.context.situation import note_evidence, open_intention, project
    from core.execution.data_channels import get_world, save_world

    model = get_world(chat_id, scope=scope)
    current = project(model.events).intentions.get(goal_id)
    if current is None:
        open_intention(
            model,
            goal_id,
            kind="act",
            at=str(at or ""),
            evidence=str(text or ""),
            origin="owner_request",
            motivation="主人直接吩咐",
            desired_outcome=str(goal or ""),
            done_when="目标动作拿到可核对的本机观察或平台回执",
            next_action="执行第一步发送",
            evidence_refs=(source_ref,),
            scope=f"chat:{chat_id}",
        )
    elif source_ref and source_ref not in current.evidence_refs:
        note_evidence(
            model,
            goal_id,
            source_ref,
            at=str(at or ""),
            source="owner_request",
        )
    if not save_world(chat_id, scope=model.scope):
        raise OSError("direct_goal_persist_failed")
    return goal_id


def _split_send(action: str) -> tuple[str, str] | None:
    """``send:银月的测试群|今天下雨记得带伞`` → ``("银月的测试群", "今天下雨记得带伞")``。"""
    tool, _, rest = str(action or "").strip().partition(":")
    if tool.strip() != SEND_TOOL:
        return None
    target, _, body = rest.partition("|")
    target, body = target.strip(), body.strip()
    if not target or not body:
        return None
    return target, body


async def _send_async(target: str, body: str) -> dict:
    from loop import ports

    chat, why = ports.resolve_say_target(target)
    if not chat:
        return {"ok": False, "detail": "target_rejected:%s" % str(why or "")[:40], "chat": ""}
    res = await ports.say_many(
        ports.split_bubbles(body),
        dry_run=False,
        chat_id=chat,
        reason="job",
    )
    data = dict(getattr(res, "data", {}) or {})
    ack = str(data.get("ack") or "")
    detail = ack if not data.get("why") else "%s:%s" % (ack, str(data.get("why")))
    # 2026-11-04（§24 地基②）：`ack="queued"` 只是「排进队列了」，不是「发出去了」——
    # 真库 67 条 sent 里 23 条在微信里查无此消息（E-59 同源）。所以这里把队列号带出去，
    # 由 `loop/receipts.py` 之后核对；`verified` 在入队这一瞬间**必然**是假的。
    return {"ok": ack in ("queued", "sent", "confirmed"), "detail": detail, "chat": chat,
            "queue_ids": list(data.get("queue_ids") or []), "verified": False}


def _send_sync(target: str, body: str) -> dict:
    """真的把这句话排进 `outbound_queue`。返回 ``{ok, detail, chat}``。"""
    box: dict[str, Any] = {}

    def _go() -> None:
        try:
            box["v"] = asyncio.run(_send_async(target, body))
        except BaseException as exc:  # noqa: BLE001 - 短线程里的异常必须带回主线程
            box["e"] = exc

    th = threading.Thread(target=_go, name="job-send", daemon=True)
    th.start()
    th.join(SEND_TIMEOUT_S)
    if th.is_alive():
        return {"ok": False, "detail": "send_timeout", "chat": ""}
    exc = box.get("e")
    if exc is not None:
        return {"ok": False, "detail": "send_error:%s" % type(exc).__name__, "chat": ""}
    return dict(box.get("v") or {"ok": False, "detail": "send_empty", "chat": ""})


def _execute(step: Any) -> dict:
    """推进一步里的那一步。认不出的动作一律不动手（只有 `send:` 这一种被实现）。"""
    action = str(getattr(step, "action", "") or "")
    pair = _split_send(action)
    if pair is None:
        return {"ok": False, "detail": "unknown_action"}
    out = _send_sync(pair[0], pair[1])
    if not out.get("ok"):
        log.info("job_send_failed target=%s detail=%s", pair[0], str(out.get("detail") or "")[:80])
    if out.get("ok"):
        # 2026-11-18 D-3：这一步真把消息排进了 outbound_queue，队列号就是「投递动作」
        # 的过程证据；`ack="queued"` 不是「已送达」，所以这里只记 queue:，平台送达
        # 仍等 receipts 核对后另记 ack:。执行器没回证据时字段不出现。
        queue_ids = [str(item) for item in (out.get("queue_ids") or []) if str(item)]
        if queue_ids:
            out = dict(out)
            out["evidence_refs"] = ["queue:%s" % item for item in queue_ids]
    return out


def _job_intentions(chat_id: str, scope: Any):
    from core.context import situation_store
    from core.context.situation import project

    with situation_store._LOCK:
        model = situation_store.get_world_model(chat_id, scope=scope)
        situation_store._apply_cognitive_closures(model, chat_id)
        return dict(model.scope), project(model.events).intentions


def _goal_abort(chat_id: str, store: Any, scope: Any = None):
    """执行前复查：这件事已被叫停、或它关联的目标已关闭，就不许再动手。

    2026-11-18 D-2：`advance_once` 拿到的是一件早先读出来的 job。从读到执行之间
    主人可能已经取消；只看内存快照就会照旧把动作发出去。所以每一步动手前重读
    台账状态，并按 `goal_id` 回世界模型看目标是否还开着。
    """

    def check(job: Any) -> str:
        job_id = str(getattr(job, "job_id", "") or "")
        fresh = None
        try:
            fresh = store.load(job_id) if hasattr(store, "load") else None
        except Exception as exc:  # noqa: BLE001 - 读不到就按快照继续，但要留痕
            log.warning(f"goal_abort_reload_failed: job_id={job_id} err={repr(exc)[:120]}")
        current = fresh or job
        if str(getattr(current, "state", "") or "") in ("done", "cancelled", "expired"):
            return "state"
        goal_id = str(getattr(current, "goal_id", "") or "")
        if not goal_id:
            return ""
        try:
            model_scope, intentions = _job_intentions(chat_id, scope)
            goal = intentions.get(goal_id)
        except Exception as exc:  # noqa: BLE001 - 判断不了就按未取消继续
            log.warning(f"goal_abort_check_failed: goal_id={goal_id} err={repr(exc)[:120]}")
            return ""
        if goal is not None and str(goal.status) != "open":
            return "goal_closed"
        if goal is not None and goal.evidence_refs:
            from core.context.cognitive_commit import Scope
            from core.context.goal_source import goal_source_state

            evidence_scope = Scope(
                str(model_scope.get("platform") or ""),
                str(model_scope.get("account_id") or ""),
                str(model_scope.get("chat_id") or ""),
            )
            if goal_source_state(goal, evidence_scope) == "dead":
                return "goal_source_revoked"
        return ""

    return check


def _settle_source_revoked(
    chat_id: str,
    store: Any,
    job: Any,
    *,
    scope: Any,
    actor: str,
) -> None:
    """Persist that a revoked request ended its job and intention."""
    job_id = str(getattr(job, "job_id", "") or "")
    try:
        store.append(
            job_id,
            "cancelled",
            payload={"stop": "goal_source_revoked", "reason": "source_revoked"},
            actor=actor,
        )
    except Exception as exc:  # noqa: BLE001 - 下一拍仍会重试守卫
        log.warning(
            f"goal_source_cancel_failed: job_id={job_id} err={repr(exc)[:120]}"
        )
    goal_id = str(getattr(job, "goal_id", "") or "")
    if not goal_id:
        return
    try:
        from core.context.situation import close_intention, project
        from core.execution.data_channels import get_world, save_world

        model = get_world(chat_id, scope=scope)
        goal = project(model.events).intentions.get(goal_id)
        if goal is None or str(goal.status) != "open":
            return
        close_intention(
            model,
            goal_id,
            result="来源请求已撤回",
            state="abandoned",
        )
        if not save_world(chat_id, scope=model.scope):
            raise OSError("goal_source_close_persist_failed")
    except Exception as exc:  # noqa: BLE001 - Job 已取消；目标下一次仍可复核
        log.warning(
            f"goal_source_close_failed: goal_id={goal_id} err={repr(exc)[:120]}"
        )


def _record_evidence(chat_id: str, goal_id: str, res: Any, scope: Any = None) -> tuple[str, ...]:
    """把执行器这一步真拿到的凭据补到目标上（产物 / 队列号 / 回执号）。

    2026-11-18 D-3：目标只在开的时候登记过一次证据引用，运行期的 ``artifact:`` /
    ``queue:`` / ``ack:`` 没有入口，最终报告因此永远缺「产物保存 / 投递动作 /
    平台送达」这几类。执行器回了什么就记什么，回空的什么都不写；写失败只留痕，
    不许把这一拍的推进带崩。
    """
    refs: list[str] = []
    for run in getattr(res, "ran", ()) or ():
        if str(getattr(run, "status", "")) != "done":
            continue
        for ref in getattr(run, "refs", ()) or ():
            text = str(ref or "").strip()
            if text and text not in refs:
                refs.append(text)
    goal = str(goal_id or "").strip()
    if not goal or not refs:
        return ()
    try:
        from core.context.situation import note_evidence
        from core.context.situation_store import _LOCK, get_world_model, persist_world_model

        with _LOCK:
            model = get_world_model(chat_id, scope=scope)
            before = list(model.events)
            added = [ref for ref in refs if note_evidence(model, goal, ref, source="job")]
            if not added:
                return ()
            try:
                saved = persist_world_model(chat_id, scope=scope)
            except Exception:
                model.events[:] = before
                raise
            if not saved:
                model.events[:] = before
                return ()
        log.info("goal_evidence goal_id=%s refs=%s", goal, ",".join(added))
        return tuple(added)
    except Exception as exc:  # noqa: BLE001 - 记不上证据不许带崩推进
        log.warning(f"goal_evidence_failed: goal_id={goal} err={repr(exc)[:120]}")
        return ()


def advance_once(
    chat_id: str,
    *,
    store: Any = None,
    execute: Any = None,
    actor: str = "loop",
    scope: Any = None,
) -> dict:
    """这一拍把某一件开着的事往前推一步。返回 ``{ran, stop, job_id, goal, step_n, steps, text}``。"""
    from core.jobs.runner import advance

    cid = str(chat_id or "")
    st = _store(store)
    try:
        jobs = list(st.open_jobs(cid) or [])
    except Exception as exc:  # noqa: BLE001 - 读不到台账就当这一拍没事
        log.warning(f"job_bridge_list_failed: err={repr(exc)[:120]}")
        return {"ran": False, "why": "store_unreadable"}
    fallback: dict | None = None
    for job in jobs:
        job_id = str(getattr(job, "job_id", "") or "")
        try:
            res = advance(
                job,
                execute=execute or _execute,
                store=st,
                allow_effects=OPEN_EFFECTS,
                actor=actor,
                yield_at_safe_point=True,
                abort_if=_goal_abort(cid, st, scope=scope),
            )
        except Exception as exc:  # noqa: BLE001 - 一件推进炸了不许带崩别的
            log.warning(f"job_bridge_advance_failed: job_id={job_id} err={repr(exc)[:120]}")
            continue
        stop = str(getattr(res, "stop", "") or "")
        ran = bool(getattr(res, "progressed", False))
        after = getattr(res, "job", job)
        if stop == "goal_source_revoked":
            _settle_source_revoked(
                cid,
                st,
                after,
                scope=scope,
                actor=actor,
            )
        log.info(
            "job_advance job_id=%s stop=%s ran=%s step_n=%s",
            job_id, stop, ran, int(getattr(after, "step_n", 0) or 0),
        )
        evidence = _record_evidence(cid, str(getattr(after, "goal", "") or ""), res, scope)
        if stop in ("budget", "turns"):
            log.info("job_abandoned job_id=%s stop=%s", job_id, stop)
            # 2026-11-04 ④ 主人抄送：一件事推到预算/回合用尽就停在这儿了，
            # 只写日志等于没人知道（P2-3 那天查出来「半途而废率 0%」就是这么来的）。
            try:
                from loop import owner_copy as _oc2
                _oc2.note("job_stuck", target=str(chat_id or ""),
                          detail=str(getattr(after, "goal", "") or ""))
            except Exception as _oexc2:  # noqa: BLE001
                log.warning(f"owner_copy.job_stuck_failed: err={repr(_oexc2)[:120]}")
        result = {
            "ran": ran,
            "stop": stop,
            "job_id": job_id,
            "goal": str(getattr(after, "goal", "") or ""),
            "step_n": int(getattr(after, "step_n", 0) or 0),
            "steps": len(getattr(after, "steps", []) or []),
            "text": str(getattr(res, "summary", lambda: "")() or ""),
            "evidence": list(evidence),
        }
        if stop == "goal_closed" and not ran:
            # 目标已关但 job 还躺在台账里：这一步没执行任何动作，换下一件试试。
            if fallback is None:
                fallback = result
            continue
        return result
    return fallback or {"ran": False, "why": "no_job"}


def readout(chat_id: str, *, store: Any = None, scope: Any = None) -> str:
    """给提示词的一行：手上还有一件事没办完，卡在第几步。没有就返回空串。

    `scope` 是（platform, account_id, chat_id）的完整会话范围，旧调用方可以不传。
    台账里的 open job 可能对应的目标已经关闭（关闭不会删 job）；这里按真实世界/销账
    投影把已关闭的滤掉，只报还能继续的那一件。
    """
    cid = str(chat_id or "")
    st = _store(store)
    try:
        jobs = list(st.open_jobs(cid) or [])
    except Exception as exc:  # noqa: BLE001 - 台账读不到就当没有，别把一拍搞坏
        log.warning(f"job_bridge_readout_failed: err={repr(exc)[:120]}")
        return ""
    if not jobs:
        return ""
    closed_goals: set[str] = set()
    if any(getattr(item, "goal_id", "") for item in jobs):
        try:
            _, intentions = _job_intentions(cid, scope)
            closed_goals = {key for key, goal in intentions.items() if goal.status != "open"}
        except Exception as exc:  # noqa: BLE001 - 投影读不到就按台账原样报
            warn("job_bridge_readout_goal_check_failed", err=repr(exc)[:120])
    job = next(
        (item for item in jobs if str(getattr(item, "goal_id", "") or "") not in closed_goals),
        None,
    )
    if job is None:
        return ""
    steps = list(getattr(job, "steps", []) or [])
    step_n = int(getattr(job, "step_n", 0) or 0)
    goal = " ".join(str(getattr(job, "goal", "") or "").split())[:80]
    if step_n < len(steps):
        where = "第 %d/%d 步：%s" % (step_n + 1, len(steps), str(getattr(steps[step_n], "action", ""))[:60])
    else:
        where = "全部 %d 步都做完了，等收尾" % len(steps)
    return "[我在办的事] %s（%s）" % (goal, where)


__all__ = ["OPEN_EFFECTS", "SEND_TIMEOUT_S", "advance_once", "open_from_owner", "readout"]
