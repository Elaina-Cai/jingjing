"""Explicit task-session boundary for Phase 3.

Daily chat runs a PersonaBrain turn and never touches Planner.  A multi-step
task is represented by a ``TaskSession``: one session id, one objective, one
live Plan, and an explicit status.  ``ConversationalAgent`` consults this
object before entering the planner path instead of treating any stray
in-memory dict entry as an active task.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from agents.planner import Plan
from safelog import warn


def _utcnow() -> datetime:
    return datetime.now()


@dataclass
class TaskSession:
    session_id: int
    objective: str = ""
    plan: Plan | None = None
    status: str = "active"  # active / clarifying / completed / failed
    created_at: datetime = field(default_factory=_utcnow)
    updated_at: datetime = field(default_factory=_utcnow)
    turns: int = 1
    metadata: dict[str, Any] = field(default_factory=dict)

    def touch(self, *, objective: str = "", status: str | None = None) -> None:
        if objective and not self.objective:
            self.objective = str(objective)[:500]
        if status:
            self.status = str(status)
        self.updated_at = _utcnow()

    def update_plan(self, plan: Plan) -> None:
        self.plan = plan
        self.metadata["plan_id"] = str(getattr(plan, "plan_id", "") or "")
        self.metadata["all_done"] = bool(getattr(plan, "all_done", False))
        self.touch(
            status="completed" if getattr(plan, "all_done", False) else "active"
        )

    def mark_turn(self) -> None:
        self.turns += 1
        self.touch()

    def is_active(self) -> bool:
        if self.status in {"completed", "failed"}:
            return False
        plan = self.plan
        if plan is None:
            return False
        return not bool(getattr(plan, "all_done", True))

    def plan_summary(self) -> str:
        if self.plan is None:
            return ""
        try:
            import json

            return json.dumps(self.plan.to_dict(), ensure_ascii=False)
        except Exception:
            warn("agents/task_session.py:68 Exception", err=repr(__import__("sys").exc_info()[1]))
            return ""
