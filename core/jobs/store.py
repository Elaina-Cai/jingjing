"""「在手的事」的台账：append-only 事件 + 可重建投影。

两张表，一条纪律
----------------
`job_events` 是**唯一事实源**（append-only，主键 `(job_id, seq)`，一次状态变化一行，
永不改旧行——与账本同一纪律）。`jobs` 只是当前状态的**投影**：删掉整表也能由事件
重放回来（`JobStore.rebuild()` 就是干这个的，用例里拿它当断言）。
`job_approvals` 单独一张 append-only 表：批准是一次外部授权，不是 job 的状态，
主键 `(job_id, step_n, action_hash)` 保证「同一动作批两次」幂等。

三档 `JOB_LEDGER`
----------------
- `off`（默认）：写读全 no-op。判拿不到读数，行为与没有这个切片时逐字相同。
- `shadow`：**写**事件与投影，但 `readouts()` 一律返回 `[]`（不进 prompt）。
  写失败只 log——影子期不许因为旁注把回合搞崩。
- `on`：写读都开。写失败**抛**——因为读侧依赖它，静默丢事件等于让判在假状态上推进。

为什么 `off` 时 `readouts()` 返回空而不是报错：调用点不该写 `if mode != off`，
唯一读点在这里，调用点永远只问「有哪些在手的事」。
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict
from datetime import datetime, timedelta
from typing import Any, Iterable, Sequence

from core import features
from core.contracts.plan import (
    MAX_JOBS_PER_CHAT,
    READOUT_MAX_ITEMS,
    Artifact,
    Approval,
    Awaiting,
    Handoff,
    Job,
    Plan,
    Policy,
    Step,
    Trigger,
)
from safelog import warn

log = logging.getLogger(__name__)


def _sql(statement: str):
    """参数化 SQL 的统一包装（sqlalchemy.text；没有就用原串）。"""
    try:
        from sqlalchemy import text as _text

        return _text(statement)
    except Exception:  # noqa: BLE001 - 测试替身没有 sqlalchemy 也能跑
        warn("core/jobs/store.py:54 Exception", err=repr(__import__("sys").exc_info()[1]))
        return statement

TABLE_EVENTS = "job_events"
TABLE_JOBS = "jobs"
TABLE_APPROVALS = "job_approvals"

#: 一次状态变化一行。新增 kind 只许**追加**，不许复用旧名的语义。
EVENT_KINDS: tuple[str, ...] = (
    "created",     # 判决定开一件事：payload = 整份 Job
    "planned",     # 步骤表定了：payload = {"goal": str, "steps": [...]}
    "turn",        # 判推进了一回合：只涨 turns_total（预算分母）
    "step_done",   # 第 n 步做完：payload = {"step_n": int, "detail": str}
    "step_failed",  # 第 n 步跑了但没成：payload = {"step_n": int, "detail": str}
    "awaiting",    # 挂起等人：payload = {"kind": str, "who": str | None}
    "approved",    # 人批了某一步：payload = {"step_n": int, "action_hash": str, ...}
    "granted",     # 人授权「这类事以后不用问」：payload = {"policy": {...}}
    "grant_revoked",  # 人把授权收回来：payload = {"revoked_policy": {...}, "who", "reason"}
    "handed_off",  # 办不下去、交给人接手：payload = {"reason", "handoff", "steps_left", "text"}
    "done",        # 做完
    "ask",         # 已经把「在等批准」问出口了：payload = {"ref": str, "step_n": int}
                   #   —— 同一 ref 只问一次，之后安静等（别每回合催）
    "abandoned",   # 人/判放弃了这件事（图纸状态机里的终态之一）
    "cancelled",   # 人叫停（主人不要了，正常收尾）
    "abandoned",   # 办不下去 / 没人再管（半途而废）；写入侧 kind="abandoned" 早已支持，这里是状态词表补齐
    "expired",     # 等太久过期（APPROVAL_TTL_HOURS）
)

CREATE_EVENTS_SQL = (
    "CREATE TABLE IF NOT EXISTS job_events ("
    " job_id TEXT NOT NULL,"
    " seq INTEGER NOT NULL,"
    " chat_id TEXT DEFAULT '' NOT NULL,"
    " kind TEXT NOT NULL,"
    " payload TEXT DEFAULT '{}' NOT NULL,"
    " actor TEXT DEFAULT '' NOT NULL,"
    " ts DATETIME NOT NULL,"
    " PRIMARY KEY (job_id, seq))"
)
CREATE_EVENTS_INDEX_SQL = (
    "CREATE INDEX IF NOT EXISTS ix_job_events_chat_ts ON job_events (chat_id, ts)"
)
CREATE_JOBS_SQL = (
    "CREATE TABLE IF NOT EXISTS jobs ("
    " job_id TEXT NOT NULL PRIMARY KEY,"
    " chat_id TEXT DEFAULT '' NOT NULL,"
    " goal TEXT DEFAULT '' NOT NULL,"
    " criteria TEXT DEFAULT '' NOT NULL,"
    " owner_person_id TEXT,"
    " state TEXT DEFAULT 'open' NOT NULL,"
    " step_n INTEGER DEFAULT 0 NOT NULL,"
    " awaiting_kind TEXT DEFAULT 'none' NOT NULL,"
    " awaiting_who TEXT,"
    " steps_json TEXT DEFAULT '[]' NOT NULL,"
    " trigger_json TEXT DEFAULT '{}' NOT NULL,"
    " policy_json TEXT DEFAULT '{}' NOT NULL,"
    " artifact_json TEXT DEFAULT '{}' NOT NULL,"
    " handoff_json TEXT DEFAULT '{}' NOT NULL,"
    " tool_calls_total INTEGER DEFAULT 0 NOT NULL,"
    " turns_total INTEGER DEFAULT 0 NOT NULL,"
    " seq INTEGER DEFAULT 0 NOT NULL,"
    " updated_by TEXT DEFAULT '' NOT NULL,"
    " updated_at DATETIME NOT NULL)"
)
CREATE_JOBS_INDEX_SQL = (
    "CREATE INDEX IF NOT EXISTS ix_jobs_chat_state ON jobs (chat_id, state)"
)
CREATE_APPROVALS_SQL = (
    "CREATE TABLE IF NOT EXISTS job_approvals ("
    " job_id TEXT NOT NULL,"
    " step_n INTEGER NOT NULL,"
    " action_hash TEXT NOT NULL,"
    " who TEXT DEFAULT '' NOT NULL,"
    " channel TEXT DEFAULT '' NOT NULL,"
    " at DATETIME NOT NULL,"
    " expires_at DATETIME,"
    " PRIMARY KEY (job_id, step_n, action_hash))"
)

_TABLE_DDL: tuple[tuple[str, str], ...] = (
    (TABLE_EVENTS, CREATE_EVENTS_SQL),
    (TABLE_EVENTS, CREATE_EVENTS_INDEX_SQL),
    (TABLE_JOBS, CREATE_JOBS_SQL),
    (TABLE_JOBS, CREATE_JOBS_INDEX_SQL),
    (TABLE_APPROVALS, CREATE_APPROVALS_SQL),
)

_engine: Any = None


class JobStoreError(RuntimeError):
    """台账写失败（`on` 档才抛给调用点）。"""


class JobLimitExceeded(JobStoreError):
    """同一会话在手的事超过 `MAX_JOBS_PER_CHAT`。"""


def ddl_statements() -> tuple[str, ...]:
    """建表语句，按顺序执行一次即幂等（迁移 64 与用例共用同一份）。"""
    return tuple(sql for _, sql in _TABLE_DDL)


def set_engine(engine: Any) -> None:
    """注入引擎（用例用内存 sqlite）。"""
    global _engine
    _engine = engine


def _get_engine() -> Any:
    global _engine
    if _engine is None:  # 惰性：只有真要写读时才碰库
        from core.terminal.store_port import get_meta_repo

        _engine = get_meta_repo().engine
    return _engine


def _mode() -> str:
    """唯一读点（`tests/test_flag_reader_discipline.py` 看着）。"""
    return str(features.mode(features.JOB_LEDGER, features.MODE_OFF) or features.MODE_OFF)


def _now() -> str:
    return datetime.now().isoformat(sep=" ", timespec="seconds")


def ensure_schema(engine: Any) -> None:
    from sqlalchemy import text

    with engine.begin() as conn:
        for sql in ddl_statements():
            conn.execute(text(sql))


def _dump(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True)


def _loads(raw: str | None) -> Any:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):  # 坏 JSON 当空处理，不崩读侧
        log.warning("job 台账里有一段坏 JSON，已当空处理：%r", raw[:80])
        return None


def job_to_dict(job: Job) -> dict[str, Any]:
    return asdict(job)


def job_from_dict(data: dict[str, Any]) -> Job:
    steps = [
        Step(
            n=int(s.get("n", 0)),
            action=str(s.get("action", "")),
            effects=str(s.get("effects", "read")),
            safe_point=bool(s.get("safe_point", True)),
            approved=bool(s.get("approved", False)),
        )
        for s in data.get("steps", ())
    ]
    return Job(
        job_id=str(data.get("job_id", "")),
        chat_id=str(data.get("chat_id", "")),
        goal=str(data.get("goal", "")),
        criteria=str(data.get("criteria", "")),
        owner_person_id=data.get("owner_person_id"),
        state=str(data.get("state", "open")),
        step_n=int(data.get("step_n", 0)),
        steps=steps,
        awaiting=_awaiting_from_dict(data.get("awaiting")),
        trigger=_trigger_from_dict(data.get("trigger")),
        policy=_policy_from_dict(data.get("policy")),
        artifact=_artifact_from_dict(data.get("artifact")),
        handoff=_handoff_from_dict(data.get("handoff")),
        tool_calls_total=int(data.get("tool_calls_total", 0)),
        turns_total=int(data.get("turns_total", 0)),
        updated_by=str(data.get("updated_by", "")),
        updated_at=str(data.get("updated_at", "")),
        goal_id=str(data.get("goal_id", "")),
    )


def _awaiting_from_dict(data: dict[str, Any] | None) -> Awaiting:
    data = data or {}
    return Awaiting(kind=str(data.get("kind", "none")), who=data.get("who"))


def _trigger_from_dict(data: dict[str, Any] | None) -> Trigger:
    data = data or {}
    return Trigger(
        kind=str(data.get("kind", "once")),
        expr=str(data.get("expr", "")),
        next_fire_at=data.get("next_fire_at"),
        enabled=bool(data.get("enabled", True)),
    )


def _policy_from_dict(data: dict[str, Any] | None) -> Policy:
    data = data or {}
    return Policy(
        scope=str(data.get("scope", "")),
        condition=str(data.get("condition", "")),
        max_amount=data.get("max_amount"),
        expires_at=data.get("expires_at"),
        revocable=bool(data.get("revocable", True)),
        approved_by=data.get("approved_by"),
    )


def _artifact_from_dict(data: dict[str, Any] | None) -> Artifact:
    data = data or {}
    return Artifact(
        kind=str(data.get("kind", "text")),
        renderer=str(data.get("renderer", "")),
        ttl_s=(int(data["ttl_s"]) if data.get("ttl_s") is not None else None),
    )


def _handoff_from_dict(data: dict[str, Any] | None) -> Handoff:
    data = data or {}
    return Handoff(
        url=str(data.get("url", "")),
        prefilled=tuple(data.get("prefilled") or ()),
        screenshots=tuple(data.get("screenshots") or ()),
        note=str(data.get("note", "")),
    )


def _row_text(row: Any, key: str) -> str:
    """投影行里的可缺列（老库还没跑迁移时不许整表读崩）。"""
    try:
        return str(row[key] or "")
    except Exception:  # noqa: BLE001 - 列不存在/取值失败都当空串
        warn("core/jobs/store.py:288 Exception", err=repr(__import__("sys").exc_info()[1]))
        return ""


def _row_to_job(row: Any) -> Job:
    """投影行 → Job（投影是派生物，字段缺了就当默认，不抛）。"""
    return Job(
        job_id=row["job_id"],
        chat_id=row["chat_id"] or "",
        goal=row["goal"] or "",
        criteria=_row_text(row, "criteria"),
        owner_person_id=row["owner_person_id"],
        state=row["state"] or "open",
        step_n=int(row["step_n"] or 0),
        steps=[
            Step(
                n=int(s.get("n", 0)),
                action=str(s.get("action", "")),
                effects=str(s.get("effects", "read")),
                safe_point=bool(s.get("safe_point", True)),
                approved=bool(s.get("approved", False)),
            )
            for s in (_loads(row["steps_json"]) or ())
        ],
        awaiting=Awaiting(
            kind=str(row["awaiting_kind"] or "none"),
            who=row["awaiting_who"],
        ),
        trigger=_trigger_from_dict(_loads(row["trigger_json"])),
        policy=_policy_from_dict(_loads(row["policy_json"])),
        artifact=_artifact_from_dict(_loads(row["artifact_json"])),
        handoff=_handoff_from_dict(_loads(row["handoff_json"])),
        tool_calls_total=int(row["tool_calls_total"] or 0),
        turns_total=int(row["turns_total"] or 0),
        updated_by=row["updated_by"] or "",
        updated_at=row["updated_at"] or "",
    )


class JobStore:
    """一件事的台账。所有写都走 `append()`，投影只由事件演化出来。"""

    def __init__(self, engine: Any = None, *, mode: str | None = None) -> None:
        self._engine = engine
        self._mode_override = mode

    # ---- 基础设施 -------------------------------------------------
    def _engine_or_default(self) -> Any:
        return self._engine if self._engine is not None else _get_engine()

    def mode(self) -> str:
        return self._mode_override or _mode()

    def enabled(self) -> bool:
        """要不要写（`off` 之外都写）。"""
        return self.mode() != features.MODE_OFF

    def visible(self) -> bool:
        """要不要读给判（只有 `on`）。"""
        return self.mode() == features.MODE_ON

    def _on_write_failure(self, exc: BaseException, what: str) -> None:
        if self.mode() == features.MODE_ON:
            raise JobStoreError(f"{what} 写失败：{exc}") from exc
        log.warning("job 台账%s 写失败（%s 档只记日志）：%s", what, self.mode(), exc)

    # ---- 写 -------------------------------------------------------
    def create(self, job: Job, *, actor: str = "judge") -> int:
        """开一件事。返回 seq；`off` 档返回 0（没写）。"""
        if not self.enabled():
            return 0
        try:
            if self._open_count(job.chat_id) >= MAX_JOBS_PER_CHAT:
                raise JobLimitExceeded(
                    f"会话 {job.chat_id} 在手的事已达上限 {MAX_JOBS_PER_CHAT}"
                )
            seq = self.append(job.job_id, "created", payload=job_to_dict(job), actor=actor)
            if seq and job.goal_id:
                self._link_job_goal(job)
            return seq
        except JobLimitExceeded:
            raise
        except Exception as exc:  # noqa: BLE001 - 统一按档位决定抛还是记
            warn("core/jobs/store.py:367 Exception", err=repr(exc))
            self._on_write_failure(exc, "created")
            return 0

    def _link_job_goal(self, job: Job) -> None:
        """把已建 job 关联到 Intention；表未迁移时静默跳过。

        `goal_links` 只能由显式迁移/测试创建，这里绝不自动建表——
        否则一次普通开 job 就会在生产库上改 schema。
        """
        try:
            from sqlalchemy import text

            engine = self._engine_or_default()
            with engine.connect() as conn:
                exists = conn.execute(
                    text(
                        "SELECT 1 FROM sqlite_master "
                        "WHERE type='table' AND name='goal_links'"
                    )
                ).first()
            if not exists:
                return
            from core.context.goal_links import link_goal

            link_goal(
                engine,
                subject_kind="job",
                subject_id=job.job_id,
                goal_id=job.goal_id,
                source="job_created",
            )
        except Exception as exc:  # noqa: BLE001 - 关联失败不能拖垮开 job
            log.warning("job_goal_link_failed", extra={"err": repr(exc)})

    def asked_refs(self, chat_id: str) -> set[str]:
        """已经问出口过的「在等批准」ref（ask 事件的 payload.ref）。

        用途：同一件事只问一次。真机事故（2026-10-21）：4 件陈旧 awaiting
        任务每回合被催一遍，一回合发了 4 条「想做的事…回「可以」我就动手」。
        """
        if not self.enabled():
            return set()
        from sqlalchemy import text

        try:
            with self._engine_or_default().connect() as conn:
                rows = conn.execute(
                    text(
                        "SELECT payload FROM job_events"
                        " WHERE chat_id = :c AND kind = 'ask'"
                    ),
                    {"c": str(chat_id or "")},
                ).fetchall()
        except Exception as exc:  # noqa: BLE001
            warn("core/jobs/store.py:390 Exception", err=repr(exc))
            self._on_write_failure(exc, "asked_refs")
            return set()
        out: set[str] = set()
        for (raw,) in rows:
            try:
                payload = json.loads(raw) if isinstance(raw, str) else (raw or {})
            except Exception:  # noqa: BLE001
                warn("core/jobs/store.py:397 Exception", err=repr(__import__("sys").exc_info()[1]))
                continue
            ref = str((payload or {}).get("ref") or "")
            if ref:
                out.add(ref)
        return out

    def append(
        self,
        job_id: str,
        kind: str,
        *,
        payload: dict[str, Any] | None = None,
        actor: str = "judge",
    ) -> int:
        """追加一行事件并推进投影。返回这一行的 seq。"""
        if not self.enabled():
            return 0
        if kind not in EVENT_KINDS:
            raise ValueError(f"未注册的事件 kind：{kind!r}（EVENT_KINDS 里没有）")
        try:
            seq = self._append_locked(job_id, kind, payload or {}, actor)
        except Exception as exc:  # noqa: BLE001
            warn("core/jobs/store.py:419 Exception", err=repr(exc))
            self._on_write_failure(exc, f"{kind}")
            return 0
        if seq and kind == "done":
            try:
                from core.context.goal_receipts import emit_job_receipt

                emit_job_receipt(
                    self._engine_or_default(),
                    job_id=job_id,
                    outcome="done",
                    at=datetime.now().isoformat(sep=" ", timespec="seconds"),
                )
            except Exception as exc:  # noqa: BLE001 - 回执不能影响状态推进
                log.warning("job_goal_receipt_failed", extra={"err": repr(exc)})
        return seq

    def _append_locked(
        self, job_id: str, kind: str, payload: dict[str, Any], actor: str
    ) -> int:
        from sqlalchemy import text

        ts = _now()
        with self._engine_or_default().begin() as conn:
            row = conn.execute(
                text("SELECT COALESCE(MAX(seq), 0) FROM job_events WHERE job_id = :j"),
                {"j": job_id},
            ).fetchone()
            seq = int(row[0]) + 1
            chat_id = self._chat_id_of(conn, job_id, payload)
            conn.execute(
                text(
                    "INSERT INTO job_events (job_id, seq, chat_id, kind, payload, actor, ts)"
                    " VALUES (:j, :s, :c, :k, :p, :a, :t)"
                ),
                {
                    "j": job_id,
                    "s": seq,
                    "c": chat_id,
                    "k": kind,
                    "p": _dump(payload),
                    "a": actor,
                    "t": ts,
                },
            )
            self._apply_projection(conn, job_id, kind, payload, seq=seq, actor=actor, ts=ts)
        return seq

    def _chat_id_of(self, conn: Any, job_id: str, payload: dict[str, Any]) -> str:
        if payload.get("chat_id"):
            return str(payload["chat_id"])
        from sqlalchemy import text

        row = conn.execute(
            text("SELECT chat_id FROM jobs WHERE job_id = :j"), {"j": job_id}
        ).fetchone()
        if row is not None:
            return str(row[0] or "")
        return ""

    def _apply_projection(
        self,
        conn: Any,
        job_id: str,
        kind: str,
        payload: dict[str, Any],
        *,
        seq: int,
        actor: str,
        ts: str,
    ) -> None:
        from sqlalchemy import text

        if kind == "created":
            job = job_from_dict(payload | {"job_id": job_id})
            conn.execute(
                text(
                    "INSERT OR REPLACE INTO jobs (job_id, chat_id, goal, criteria,"
                    " owner_person_id,"
                    " state, step_n, awaiting_kind, awaiting_who, steps_json, trigger_json,"
                    " policy_json, artifact_json, handoff_json, tool_calls_total,"
                    " turns_total, seq, updated_by, updated_at)"
                    " VALUES (:job_id, :chat_id, :goal, :criteria, :owner, :state, :step_n,"
                    " :aw_kind, :aw_who, :steps, :trigger, :policy, :artifact, :handoff,"
                    " :tools, :turns, :seq, :by, :at)"
                ),
                {
                    "job_id": job.job_id or job_id,
                    "chat_id": job.chat_id,
                    "goal": job.goal,
                    "criteria": job.criteria,
                    "owner": job.owner_person_id,
                    "state": job.state,
                    "step_n": job.step_n,
                    "aw_kind": job.awaiting.kind,
                    "aw_who": job.awaiting.who,
                    "steps": _dump([asdict(s) for s in job.steps]),
                    "trigger": _dump(asdict(job.trigger)),
                    "policy": _dump(asdict(job.policy)),
                    "artifact": _dump(asdict(job.artifact)),
                    "handoff": _dump(asdict(job.handoff)),
                    "tools": job.tool_calls_total,
                    "turns": job.turns_total,
                    "seq": seq,
                    "by": actor,
                    "at": ts,
                },
            )
            return

        sets: list[str] = ["seq = :seq", "updated_by = :by", "updated_at = :at"]
        params: dict[str, Any] = {"j": job_id, "seq": seq, "by": actor, "at": ts}
        if kind == "planned":
            sets.append("steps_json = :steps")
            params["steps"] = _dump(payload.get("steps") or [])
            if payload.get("goal"):
                sets.append("goal = :goal")
                params["goal"] = str(payload["goal"])
            # 判据可以由人的后续指令改写（planned 事件是唯一的改写通道）：
            # 允许显式置空，所以判据「有 key」而不是「非空」就写。
            if "criteria" in payload:
                sets.append("criteria = :criteria")
                params["criteria"] = str(payload["criteria"] or "")
        elif kind == "turn":
            sets.append("turns_total = turns_total + 1")
        elif kind == "step_done":
            sets.append("step_n = :step_n")
            params["step_n"] = int(payload.get("step_n") or 0)
            if payload.get("tool_calls"):
                sets.append("tool_calls_total = tool_calls_total + :tc")
                params["tc"] = int(payload["tool_calls"])
        elif kind == "awaiting":
            sets.append("awaiting_kind = :aw_kind")
            sets.append("awaiting_who = :aw_who")
            params["aw_kind"] = str(payload.get("kind") or "none")
            params["aw_who"] = payload.get("who")
            sets.append("state = :state")
            params["state"] = "awaiting" if payload.get("kind") not in (None, "", "none") else "open"
        elif kind in ("cancelled", "abandoned", "failed"):
            # 终态：清掉"在等什么"，否则会永远被当作待批准的事反复念出来
            # （2026-10-21 真机事故：4 件陈旧 awaiting 每回合催批准，取消还改不动状态）
            sets.append("awaiting_kind = 'none'")
            sets.append("awaiting_who = NULL")
            sets.append("state = :state")
            params["state"] = "cancelled" if kind == "cancelled" else str(kind)
        elif kind == "approved":
            sets.append("awaiting_kind = 'none'")
            sets.append("awaiting_who = NULL")
            sets.append("state = 'open'")
        elif kind == "granted":
            # 长期授权不改变「在等」的状态：只换 policy，
            # 让下一次 advance() 走到那一步时 authorize_step 能自己放行。
            sets.append("policy_json = :policy")
            params["policy"] = _dump(payload.get("policy") or {})
        elif kind == "grant_revoked":
            # 收回授权：把 policy 的 `expires_at` 钉到此刻之前——`grant_allows()` 会
            # 返回 `grant_expired`，下一次 authorize_step 重新要求人点头。
            # 不动 scope（留痕能看出撤的是哪一类），也不动「在等」的状态。
            sets.append("policy_json = :policy")
            params["policy"] = _dump(payload.get("revoked_policy") or {})
        elif kind == "handed_off":
            # 交接单只是「球交给谁」的事实，不改变 job 的推进状态：
            # 人接手了由下一次 advance() 自己往前走，人不管它由 expires 收尾。
            sets.append("handoff_json = :handoff")
            params["handoff"] = _dump(payload.get("handoff") or {})
        elif kind in ("done", "cancelled", "expired"):
            sets.append("state = :state")
            params["state"] = {"done": "done", "cancelled": "cancelled", "expired": "expired"}[kind]
            sets.append("awaiting_kind = 'none'")
        conn.execute(
            text(f"UPDATE jobs SET {', '.join(sets)} WHERE job_id = :j"), params
        )

    def _open_count(self, chat_id: str) -> int:
        from sqlalchemy import text

        with self._engine_or_default().begin() as conn:
            row = conn.execute(
                text(
                    "SELECT COUNT(*) FROM jobs WHERE chat_id = :c AND state IN ('open', 'awaiting')"
                ),
                {"c": chat_id},
            ).fetchone()
        return int(row[0] or 0)

    def approve(self, approval: Approval) -> bool:
        """记一次人批准。同 (job, step, action_hash) 重复批 → False（幂等）。"""
        if not self.enabled():
            return False
        from sqlalchemy import text

        try:
            with self._engine_or_default().begin() as conn:
                result = conn.execute(
                    text(
                        "INSERT OR IGNORE INTO job_approvals"
                        " (job_id, step_n, action_hash, who, channel, at, expires_at)"
                        " VALUES (:j, :n, :h, :w, :ch, :at, :exp)"
                    ),
                    {
                        "j": approval.job_id,
                        "n": approval.step_n,
                        "h": approval.action_hash,
                        "w": approval.who,
                        "ch": approval.channel,
                        "at": approval.at or _now(),
                        "exp": approval.expires_at,
                    },
                )
                inserted = int(result.rowcount or 0) > 0
            if inserted:
                self.append(
                    approval.job_id,
                    "approved",
                    payload={
                        "step_n": approval.step_n,
                        "action_hash": approval.action_hash,
                        "who": approval.who,
                        "channel": approval.channel,
                    },
                    actor=f"human:{approval.who}",
                )
            return inserted
        except Exception as exc:  # noqa: BLE001
            warn("core/jobs/store.py:629 Exception", err=repr(exc))
            self._on_write_failure(exc, "approved")
            return False

    # ---- 读 -------------------------------------------------------
    def _goal_id_for_job(self, job_id: str) -> str:
        """从隔离关联或首条 created 事件恢复 Job → Intention 关系。"""
        job = str(job_id or "").strip()
        if not job:
            return ""
        engine = self._engine_or_default()
        try:
            from core.context.goal_links import goal_for

            linked = goal_for(engine, subject_kind="job", subject_id=job)
            if linked:
                return linked
        except Exception as exc:  # noqa: BLE001 - 表未迁移时继续读 created
            warn("job_goal_link_lookup_failed", job=job, err=repr(exc)[:120])
        try:
            from sqlalchemy import text

            with engine.connect() as conn:
                row = conn.execute(
                    text(
                        "SELECT payload FROM job_events "
                        "WHERE job_id = :job AND kind = 'created' "
                        "ORDER BY seq LIMIT 1"
                    ),
                    {"job": job},
                ).first()
            data = _loads(row[0]) if row is not None else None
            return str((data or {}).get("goal_id") or "")
        except Exception as exc:  # noqa: BLE001 - 老库缺列时维持未关联
            warn("job_goal_payload_lookup_failed", job=job, err=repr(exc)[:120])
            return ""

    def load(self, job_id: str) -> Job | None:
        if not self.enabled():
            return None
        from sqlalchemy import text

        with self._engine_or_default().begin() as conn:
            row = conn.execute(
                text("SELECT * FROM jobs WHERE job_id = :j"), {"j": job_id}
            ).mappings().fetchone()
        if row is None:
            return None
        job = _row_to_job(row)
        job.goal_id = self._goal_id_for_job(job.job_id)
        return job

    def open_jobs(self, chat_id: str) -> list[Job]:
        if not self.enabled():
            return []
        from sqlalchemy import text

        with self._engine_or_default().begin() as conn:
            rows = conn.execute(
                text(
                    "SELECT * FROM jobs WHERE chat_id = :c AND state IN ('open', 'awaiting')"
                    " ORDER BY updated_at DESC, job_id"
                ),
                {"c": chat_id},
            ).mappings().fetchall()
        jobs = [_row_to_job(r) for r in rows]
        for job in jobs:
            job.goal_id = self._goal_id_for_job(job.job_id)
        return jobs

    def readouts(self, chat_id: str, limit: int = READOUT_MAX_ITEMS) -> list[str]:
        """进 prompt 的「在手的事读数」：只有 `on` 档给，最多 `limit` 条。"""
        if not self.visible():
            return []
        out: list[str] = []
        for job in self.open_jobs(chat_id):
            text_out = job.readout()
            if text_out:
                out.append(text_out)
            if len(out) >= limit:
                break
        return out


    def recent_done(self, chat_id: str, *, window_s: float = 300.0, limit: int = 2) -> list[dict]:
        """最近办完的事（含平台消息号）。

        为什么需要：事一 ``done`` 就离开 ``open_jobs``，读数里什么都不剩，
        她就顺着旧印象说「我发不出去」（真机 2026-10-21 原话）。成功必须在
        她自己的读数里留一句，否则她会一直否认自己做成过。
        """
        if not self.enabled():
            return []
        rows: list[dict] = []
        try:
            import time as _time

            cutoff = str(_time.strftime("%Y-%m-%d %H:%M:%S", _time.localtime(_time.time() - max(0.0, float(window_s)))))
            with self._engine_or_default().connect() as conn:
                jobs = conn.execute(
                    _sql(
                        f"select job_id, goal, updated_at from {TABLE_JOBS}"
                        " where chat_id = :chat and state = 'done' and updated_at >= :cut"
                        " order by updated_at desc limit :lim"
                    ),
                    {"chat": str(chat_id or ""), "cut": cutoff, "lim": int(limit)},
                ).fetchall()
                for job_id, goal, _when in jobs:
                    payload = conn.execute(
                        _sql(
                            f"select payload from {TABLE_EVENTS}"
                            " where job_id = :jid and kind = 'step_done' order by seq desc limit 1"
                        ),
                        {"jid": job_id},
                    ).fetchone()
                    changed: dict = {}
                    try:
                        changed = dict((json.loads(payload[0]) or {}).get("changed") or {}) if payload else {}
                    except Exception:  # noqa: BLE001
                        warn("core/jobs/store.py:708 Exception", err=repr(__import__("sys").exc_info()[1]))
                        changed = {}
                    rows.append(
                        {
                            "job_id": str(job_id),
                            "goal": str(goal or ""),
                            "server_id": str(changed.get("server_id") or ""),
                            "local_id": changed.get("local_id") or "",
                        }
                    )
        except Exception as exc:  # noqa: BLE001 - 读不到就当没有
            log.warning("job_recent_done_failed", error=type(exc).__name__)
            return []
        return rows

    def approvals(self, job_id: str) -> list[Approval]:
        if not self.enabled():
            return []
        from sqlalchemy import text

        with self._engine_or_default().begin() as conn:
            rows = conn.execute(
                text(
                    "SELECT * FROM job_approvals WHERE job_id = :j"
                    " ORDER BY step_n, action_hash"
                ),
                {"j": job_id},
            ).mappings().fetchall()
        return [
            Approval(
                job_id=r["job_id"],
                step_n=int(r["step_n"]),
                action_hash=r["action_hash"],
                who=r["who"] or "",
                channel=r["channel"] or "",
                at=r["at"] or "",
                expires_at=r["expires_at"],
            )
            for r in rows
        ]

    def is_approved(
        self,
        job_id: str,
        step_n: int,
        action_hash: str,
        *,
        now: str | None = None,
    ) -> bool:
        """这一步这个动作批过没有（过期不算）。"""
        if not self.enabled():
            return False
        from sqlalchemy import text

        with self._engine_or_default().begin() as conn:
            row = conn.execute(
                text(
                    "SELECT expires_at FROM job_approvals"
                    " WHERE job_id = :j AND step_n = :n AND action_hash = :h"
                ),
                {"j": job_id, "n": step_n, "h": action_hash},
            ).fetchone()
        if row is None:
            return False
        expires_at = row[0]
        if not expires_at:
            return True
        moment = now or _now()
        return str(expires_at) >= moment

    # ---- 重放 -----------------------------------------------------
    def rebuild(self, chat_id: str | None = None) -> int:
        """把投影整表重放一遍（证明 `jobs` 是派生物）。返回重放的事数。"""
        from sqlalchemy import text

        engine = self._engine_or_default()
        with engine.begin() as conn:
            if chat_id is None:
                conn.execute(text("DELETE FROM jobs"))
                rows = conn.execute(
                    text("SELECT job_id, seq, kind, payload, actor, ts FROM job_events ORDER BY job_id, seq")
                ).mappings().fetchall()
            else:
                conn.execute(text("DELETE FROM jobs WHERE chat_id = :c"), {"c": chat_id})
                rows = conn.execute(
                    text(
                        "SELECT job_id, seq, kind, payload, actor, ts FROM job_events"
                        " WHERE chat_id = :c ORDER BY job_id, seq"
                    ),
                    {"c": chat_id},
                ).mappings().fetchall()
            count = 0
            for r in rows:
                self._apply_projection(
                    conn,
                    r["job_id"],
                    r["kind"],
                    _loads(r["payload"]) or {},
                    seq=int(r["seq"]),
                    actor=r["actor"] or "",
                    ts=r["ts"] or _now(),
                )
                count += 1
        return count

    def recent_failures(
        self, chat_id: str, *, limit: int = 3, within_hours: int = 72
    ) -> list[dict]:
        """最近没成的事（只读；`chat_id` 留空 = 跨会话，自检用）。给她看的：失败必须进她的下一轮上下文，
        否则她既不知道自己失败过，也不会改口（真机 `job_advance stop=failed`
        却没有 detail，她就一直说"发不了"）。"""
        if not self.enabled() or limit <= 0:
            return []
        import json as _json

        from sqlalchemy import text

        rows = []
        where = "kind = 'step_failed' AND ts >= datetime('now', :window)"
        params: dict = {"window": f"-{int(within_hours)} hours", "limit": int(limit)}
        if chat_id:
            where += " AND chat_id = :chat_id"
            params["chat_id"] = chat_id
        try:
            with self._engine.connect() as conn:
                rows = conn.execute(
                    text(
                        "SELECT job_id, payload, ts FROM job_events WHERE "
                        + where
                        + " ORDER BY ts DESC LIMIT :limit"
                    ),
                    params,
                ).fetchall()
        except Exception as exc:  # noqa: BLE001 - 读失败不许影响回合
            log.info("job_recent_failures_failed", chat_id=chat_id, error=repr(exc))
            return []
        out: list[dict] = []
        for row in rows:
            try:
                payload = _json.loads(row[1] or "{}")
            except Exception:  # noqa: BLE001
                warn("core/jobs/store.py:848 Exception", err=repr(__import__("sys").exc_info()[1]))
                payload = {}
            out.append(
                {
                    "job_id": str(row[0]),
                    "detail": str(payload.get("detail", ""))[:120],
                    "step_n": int(payload.get("step_n", 0) or 0),
                    "ts": str(row[2]),
                }
            )
        return out

    def event_kinds(self, job_id: str) -> list[str]:
        from sqlalchemy import text

        with self._engine_or_default().begin() as conn:
            rows = conn.execute(
                text("SELECT kind FROM job_events WHERE job_id = :j ORDER BY seq"),
                {"j": job_id},
            ).fetchall()
        return [str(r[0]) for r in rows]


def default_store() -> JobStore:
    return JobStore()


def approval_ttl_expiry(now: datetime | None = None) -> str:
    from core.contracts.plan import APPROVAL_TTL_HOURS

    base = now or datetime.now()
    return (base + timedelta(hours=APPROVAL_TTL_HOURS)).isoformat(sep=" ", timespec="seconds")
