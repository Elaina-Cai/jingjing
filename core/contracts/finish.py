"""一件事「最后算不算完」的统一词表（P2-1，2026-11-03）。

问题不是"谁算错了"，是**三种方言各说各话**，所以"半途而废率"根本算不出来：

- 台账 `core/tasks/store.py:95`：`RUN_OUTCOMES = ("sent", "silent", "failed", "blocked")`，
  其中只有 `sent` 算办好了（`core/tasks/slot_fill.py:158` `COMPLETED_OUTCOMES = ("sent",)`）；
- 任务推进器 `core/jobs/runner.py:35`：`STOP_REASONS` 九个（停下来的**原因**），
  而"这件事完了没"在 `jobs.state`（`open` / `awaiting` / `done` / `cancelled` / `expired`）；
- 意图账本 `core/context/situation.py`：只有 `status` = `open` / `closed`，
  结论塞在 `result` 的**文本前缀**里（`completed:` / `matched:` / `expired（超时未完成）` / `cleared_by_owner（…）`）。

本模块只做一件事：**把三种方言翻译成同一组词**。它是纯函数、无 IO、不碰数据库，
**不改变任何现有写路径**——让三处真的写同一个词，是下一步（P2-2）的事。

词表（5 个，多一个都不要）：

============  ==========================================================
``pending``   还没结：开着、在等人批、预算/回合用完但没放弃、卡在闸上
``done``      办成了：有对外可见的结果
``failed``    办砸了：试过，失败了
``expired``   过期没做：到点没办、超时关闭
``abandoned`` 主动放弃：主人取消、主人清账
============  ==========================================================

为什么取消 `blocked` 当终态：它在本仓的意思是「这一步被闸挡住了」（`core/jobs/handoff.py:42`
的注释原话），挡住的下一步通常是**开开关/给权限就能继续**，所以它是 `pending` 的一种，
不是"办完了"。同理 `silent`（`core/observability/decision_trace.py:267`：「判定不说话」）
在台账侧被判为「不是完成」——主人那边什么都没发生，所以记 `failed` 而不是 `done`。
"""

from __future__ import annotations

import structlog

from typing import Any, Iterable, Mapping

log = structlog.get_logger()

__all__ = [
    "PENDING",
    "DONE",
    "FAILED",
    "EXPIRED",
    "ABANDONED",
    "FINISH_CODES",
    "FINISHED_CODES",
    "from_job_state",
    "from_job_stop",
    "from_receipt",
    "from_intention",
    "from_work_item_status",
    "normalise",
    "is_finished",
    "describe",
    "count_by_finish",
    "abandoned_rate",
]

PENDING = "pending"
DONE = "done"
FAILED = "failed"
EXPIRED = "expired"
ABANDONED = "abandoned"

FINISH_CODES: tuple[str, ...] = (PENDING, DONE, FAILED, EXPIRED, ABANDONED)
FINISHED_CODES: tuple[str, ...] = (DONE, FAILED, EXPIRED, ABANDONED)

_DESCRIBE = {
    PENDING: "还没结",
    DONE: "办成了",
    FAILED: "办砸了",
    EXPIRED: "过期没做",
    ABANDONED: "主动放弃",
}

# --- jobs.state（core/jobs/store.py）→ 词表 ---------------------------------
_JOB_STATE = {
    "open": PENDING,
    "awaiting": PENDING,
    "done": DONE,
    "cancelled": ABANDONED,
    "abandoned": ABANDONED,
    "expired": EXPIRED,
}

# --- runner.STOP_REASONS（停下**原因**，不是终态）→ 词表 -------------------
# 只有 done / failed 是终态结论；其余都是"停在这儿，还能再推进"。
_JOB_STOP = {
    "done": DONE,
    "failed": FAILED,
    "need_approval": PENDING,
    "awaiting": PENDING,
    "budget": PENDING,
    "turns": PENDING,
    "state": PENDING,
    "no_executor": PENDING,
    "shadow": PENDING,
}

# --- 台账 RUN_OUTCOMES（一次执行的结果）→ 词表 -----------------------------
_RECEIPT = {
    "sent": DONE,
    "silent": FAILED,
    "failed": FAILED,
    "blocked": PENDING,
}

# --- 意图 result 前缀 → 词表 ----------------------------------------------
_INTENTION_PREFIX = {
    "completed": DONE,
    "matched": DONE,
    "expired": EXPIRED,
    "cleared_by_owner": ABANDONED,
    "failed": FAILED,
}


def _norm(value: Any) -> str:
    return str(value or "").strip().lower()


def from_job_state(state: Any) -> str:
    """`jobs.state` → 词表。未知值按 `pending` 处理（宁可漏算终态，不要误判办成）。"""
    key = _norm(state)
    code = _JOB_STATE.get(key)
    if code is None:
        log.warning("finish_unknown_job_state", state=key)
        return PENDING
    return code


def from_job_stop(stop: Any) -> str:
    """`AdvanceResult.stop` → 词表。注意这是**停下来的原因**，不是终态本身。"""
    key = _norm(stop)
    code = _JOB_STOP.get(key)
    if code is None:
        log.warning("finish_unknown_job_stop", stop=key)
        return PENDING
    return code


def from_receipt(outcome: Any) -> str:
    """台账 `RUN_OUTCOMES` → 词表。只有 `sent` 算办成（`COMPLETED_OUTCOMES`）。"""
    key = _norm(outcome)
    code = _RECEIPT.get(key)
    if code is None:
        log.warning("finish_unknown_receipt", outcome=key)
        return PENDING
    return code


def from_intention(status: Any, result: Any = "") -> str:
    """意图（`core/context/situation.py`）→ 词表。

    `status` 只有 open / closed；closed 的结论在 `result` 的文本前缀里
    （`completed:<触发句>` / `matched:<谁>` / `expired（超时未完成）` /
    `cleared_by_owner（…）`）。前缀认不出来就按 `failed` 处理：账已经关了，
    又不承认办成，那就是没办成——**宁可算失败，不要谎报办成**。
    """
    if _norm(status) != "closed":
        return PENDING
    text = str(result or "")
    head = text.split(":", 1)[0].split("（", 1)[0].strip().lower()
    code = _INTENTION_PREFIX.get(head)
    if code is None:
        log.warning("finish_unknown_intention_result", result=text[:40])
        return FAILED
    return code


def from_work_item_status(status: Any) -> str:
    """`core/tasks/store.WORK_ITEM_STATUSES` → 词表。

    `open` / `waiting_user` 还没结；`done` 办成；`failed` 办砸；
    `cancelled` 主动放弃；`superseded` 被新的顶掉——**也归 `abandoned`**
    （没人再管它了，但它不是失败，也不该记成办成）。
    """
    key = _norm(status)
    if key in ("open", "waiting_user", ""):
        return PENDING
    if key == "done":
        return DONE
    if key == "failed":
        return FAILED
    if key in ("cancelled", "superseded"):
        return ABANDONED
    log.warning("finish_unknown_work_item_status", status=key)
    return PENDING


def normalise(code: Any) -> str:
    """把任意一侧的词收敛到词表；不认识就用 `pending`。"""
    key = _norm(code)
    if key in FINISH_CODES:
        return key
    for table in (_JOB_STATE, _JOB_STOP, _RECEIPT, _INTENTION_PREFIX):
        if key in table:
            return table[key]
    log.warning("finish_unknown_code", code=key)
    return PENDING


def is_finished(code: Any) -> bool:
    """这个词是不是终态（`pending` 之外都算终态？不是——只有四种终态算）。"""
    return normalise(code) in FINISHED_CODES


def describe(code: Any) -> str:
    """给人看的中文短语（日志/面板用，不参与判定）。

    注意这里**不借道 `normalise()`**：`normalise()` 对不认识的词一律给 `pending`，
    是"宁可漏算终态"的策略；但人看的短语不该把垃圾说成"还没结"——认不出来就说未知。
    """
    key = _norm(code)
    if key in _DESCRIBE:
        return _DESCRIBE[key]
    for table in (_JOB_STATE, _JOB_STOP, _RECEIPT, _INTENTION_PREFIX):
        if key in table:
            return _DESCRIBE.get(table[key], "未知")
    return "未知"


def count_by_finish(codes: Iterable[Any]) -> dict[str, int]:
    """一批结论码 → 计数表（五个键永远都在，缺的是 0）。"""
    out = {code: 0 for code in FINISH_CODES}
    for raw in codes:
        out[normalise(raw)] += 1
    return out


def abandoned_rate(counts: Mapping[str, int]) -> float:
    """半途而废率 = （主动放弃 + 过期没做）/ 已结的事。

    分母**只算已结**（`done` + `failed` + `expired` + `abandoned`）：
    还没结的事算进分母，会把"正在办"办成"半途而废"，那是另一种谎报。
    没有已结的事 → 0.0（不是 1.0，也不是除零）。
    """
    finished = sum(int(counts.get(code, 0) or 0) for code in FINISHED_CODES)
    if finished <= 0:
        return 0.0
    lost = sum(int(counts.get(code, 0) or 0) for code in (ABANDONED, EXPIRED))
    return round(lost / finished, 4)


# ---------------------------------------------------------------- 放弃的原因
#: 「放弃」这一个词里挤着两件完全不同的事，必须分开：
#: ``owner``  主人说不要了（正常收尾，他心里有数）
#: ``stuck``  办不下去 / 没人再管（**这才是"半途而废"**）
#: ``superseded`` 被新的一件顶掉（不是失败，只是换了一件事做）
ABANDON_CAUSES: tuple[str, ...] = ("owner", "stuck", "superseded")


def abandon_cause(thing: Any, *, updated_by: Any = "") -> str:
    """从**原始词**反推放弃原因；不是放弃类的一律 ``""``。

    ``thing`` 可以是 ``jobs.state`` 的值，也可以是 ``finish`` 码——两条路都能问。

    ``updated_by`` 是 ``jobs`` 表里"这一行最后是谁改的"（`core/jobs/store.py:117`）。
    它是老数据**唯一**能区分两种放弃的证据：``operator`` = 主人/操作员叫停（正常收尾），
    其余（``judge`` / 计时器 / ingest …）= 系统自己收摊，也就是**办不下去**。
    有它就以它为准；没有它才退回看词面（``cancelled`` 保守算 owner，``abandoned`` 算 stuck）。
    """
    key = str(thing or "").strip().lower()
    who = str(updated_by or "").strip().lower()
    if key in ("abandoned", "stuck", "failed_gave_up"):
        return "stuck"
    if key in ("cancelled", "canceled", "cancelled_by_owner", "owner"):
        if who == "operator":
            return "owner"
        if who:
            return "stuck"          # 不是人叫停的 cancelled = 系统自己收摊 = 办不下去
        return "owner"              # 没有证据时保守算人叫停（不许凭空说人家半途而废）
    if key == "superseded":
        return "superseded"
    return ""


def count_by_abandon_cause(pairs: Iterable[Any]) -> dict[str, int]:
    """按原因数一遍放弃的件（只数放弃类，别的词忽略）。

    每项可以是 ``"cancelled"`` 这样的词，也可以是 ``("cancelled", "operator")``
    这样的 ``(状态, updated_by)`` 二元组——后者才能分出"人叫停"与"办不下去"。
    """
    out = {c: 0 for c in ABANDON_CAUSES}
    for item in pairs:
        if isinstance(item, (tuple, list)) and len(item) == 2:
            cause = abandon_cause(item[0], updated_by=item[1])
        else:
            cause = abandon_cause(item)
        if cause:
            out[cause] = out.get(cause, 0) + 1
    return out


def stuck_rate(counts: Mapping[str, int], causes: Iterable[Any] = ()) -> float:
    """**半途而废率** = 办不下去 / 已了结的件。

    与 :func:`abandoned_rate` 的区别：那个把"主人说不要了"也算进去（口径偏大），
    这个只用 ``cause == "stuck"``（口径偏严）。两个都给出来，报数时都要写清楚是哪一个。
    """
    finished = sum(int(counts.get(c, 0)) for c in FINISHED_CODES)
    if not finished:
        return 0.0
    stuck = 0
    for item in causes:
        if isinstance(item, (tuple, list)) and len(item) == 2:
            cause = abandon_cause(item[0], updated_by=item[1])
        else:
            cause = abandon_cause(item)
        if cause == "stuck":
            stuck += 1
    return round(stuck / finished, 4)
