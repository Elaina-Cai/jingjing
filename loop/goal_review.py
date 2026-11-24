"""目标复核与回合之间的适配：候选可重试，明确结论才持久化消费。"""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from pathlib import Path
import json
import time

from core.context.goal_wake import GoalWake, acknowledge_reviews, due_wakes, goal_occurrence, pending_wakes
from core.context import goal_report
from core.context.goal_source import source_label
from core.context import situation_store as store
from core.context.situation import open_intentions
from core.infra.envflags import flag
from safelog import warn


def database_scope(db: str) -> tuple[str, str]:
    path = Path(db)
    return path.parent.parent.name, path.parent.name


class ReviewQueue:
    """扫描缓存和失败退避都是进程内提示；真正消费只由持久化标记决定。"""
    def __init__(self):
        self._scan_at = 0.0
        self._cached = []
        self._retry = {}
        self._db = None

    def poll(self, db: str, *, now: str, allowed: bool, clock: float | None = None) -> list[dict]:
        if not allowed or not db or not flag("YINYUE_GOAL_REVIEW_WAKE", False):
            return []
        clock = time.monotonic() if clock is None else clock
        if db != self._db or clock >= self._scan_at:
            self._db = db
            self._scan_at = clock + 5.0
            try:
                platform, account = database_scope(db)
                models = store.all_world_models()
                for model in models.values():
                    store._apply_cognitive_closures(model, model.scope.get("chat_id", ""))
                self._cached = [asdict(item) for item in due_wakes(models, now=now) if item.scope[:2] == (platform, account)]
                live = {item["token"] for item in self._cached}
                self._retry = {key: value for key, value in self._retry.items() if key in live}
            except Exception as exc:
                self._cached = []
                warn("goal_review_scan_failed", db=db, err=repr(exc)[:120])
        return [item for item in self._cached if clock >= self._retry.get(item["token"], 0.0)]

    def started(self, items: list[dict], *, clock: float | None = None) -> None:
        clock = time.monotonic() if clock is None else clock
        for item in items:
            self._retry[item["token"]] = clock + 60.0
        self._scan_at = 0.0


class ReviewDispatcher:
    def __init__(self):
        self._queues = {}
        # 轮转游标：某个库持续有候选时，不能让后面的库永远轮不到。
        self._cursor = 0

    def poll(self, databases: list[str], *, now: str, allowed: bool) -> tuple[str, list[dict]]:
        total = len(databases)
        for offset in range(total):
            index = (self._cursor + offset) % total
            db = databases[index]
            queue = self._queues.setdefault(db, ReviewQueue())
            items = queue.poll(db, now=now, allowed=allowed)
            if items:
                self._cursor = (index + 1) % total
                return db, items
        self._cursor = 0
        return "", []

    def started(self, db: str, items: list[dict]) -> None:
        if items:
            self._queues[db].started(items)


def for_window(items: list[dict], *, db: str, platform: str, chat_id: str) -> list[dict]:
    db_platform, account = database_scope(db)
    if platform != db_platform:
        return []
    return [item for item in items if tuple(item.get("scope") or ()) == (platform, account, chat_id)]


def prompt(items: list[dict], *, related_goals: dict | None = None,
           shown_items: list[dict] | None = None) -> str:
    groups: dict[tuple, dict] = {}
    scoped_models: dict[tuple, tuple] = {}
    for item in items:
        candidate = GoalWake(**{**item, "scope": tuple(item["scope"])})
        if candidate.scope not in scoped_models:
            model = store.get_world_model(candidate.scope)
            with store._LOCK:
                store._apply_cognitive_closures(model, candidate.scope[2])
                scoped_models[candidate.scope] = (
                    model, set(pending_wakes(model)), {goal.key: goal for goal in open_intentions(model)},
                )
        model, pending, goals = scoped_models[candidate.scope]
        if candidate not in pending:
            continue
        goal = goals[candidate.goal_id]
        group_key = (candidate.scope, candidate.goal_id)
        if group_key not in groups and related_goals is not None:
            related_goals[goal.key] = goal, goal_occurrence(model, goal.key)
        if shown_items is not None:
            shown_items.append(item)
        group = groups.setdefault(group_key, {"goal": goal, "signals": []})
        group["signals"].append(candidate)
    lines = []
    for group in groups.values():
        goal = group["goal"]
        lines.append(f"- {goal.key}｜{goal.desired_outcome or goal.evidence or goal.key}｜下一步：{goal.next_action or '待判断'}｜阻碍：{goal.blocked_on or '未记录'}")
        for candidate in sorted(group["signals"], key=lambda item: datetime.fromisoformat(item.at)):
            label = {"review": "复核点", "due": "到期复核", "condition": "等待条件可能满足", "evidence": "有新证据", "receipt": "动作回执变化"}.get(candidate.reason, "复核点")
            source = source_label(candidate.evidence_state)
            source_part = f"｜{source}" if source else ""
            lines.append(f"  - {label} {candidate.at}{source_part}")
            if candidate.receipt_ref:
                lines.append("    唤醒回执（仅作本次执行的依据，目标完成和消息送达需另核对）：" + json.dumps({
                    "引用": candidate.receipt_ref,
                    "执行状态": candidate.receipt_outcome or "未记录",
                    "摘要": candidate.receipt_detail,
                }, ensure_ascii=False))
        # 2026-11-18 D-3：完成条件与证据分开写，免得把「登记过引用」说成「已经送到」。
        progress = goal_report.line(goal)
        if progress:
            lines.append("  " + progress)
    if not lines:
        return ""
    return "[目标到了复核点]\n" + "\n".join(lines) + "\n请检查现在是否能推进或仍需等待，再用 review_goal 写明结论。复核不等于完成，也不要求对外发言；外发仍遵守原有场合与授权规则。"


def query_hint(items: list[dict]) -> str:
    """没有新消息的复核回合用目标标题/下一步当检索线索（只读，不推进目标）。

    B1：复核回合没有「他刚说的那句话」，但也不能因此不过记忆——用同一个
    世界模型里的目标文本当线索，读到的还是同一权限范围内的条目。
    """
    parts: list[str] = []
    for item in (items or [])[:2]:
        try:
            candidate = GoalWake(**{**item, "scope": tuple(item["scope"])})
            model = store.get_world_model(candidate.scope)
            goal = next((g for g in open_intentions(model) if g.key == candidate.goal_id), None)
            if goal is None:
                continue
            for value in (goal.desired_outcome, goal.next_action or goal.blocked_on):
                text = str(value or "").strip()
                if text:
                    parts.append(text)
        except Exception as exc:  # noqa: BLE001 - 线索取不到就当没有，不影响复核
            warn("goal_review.query_hint_failed", err=repr(exc)[:120])
            continue
    return " ".join(parts)[:160]


def prepare(args: dict, items: list[dict]) -> tuple[str, dict]:
    key = str(args.get("goal_id") or "").strip()
    conclusion = str(args.get("conclusion") or "").strip()
    item = next((item for item in items if item.get("goal_id") == key), None)
    if item is None or not conclusion:
        return "未记录：只能复核本回合列出的目标，且需要具体结论。", {"act": "review_goal", "result": "rejected"}
    scope = tuple(item.get("scope") or ())
    tokens = list(dict.fromkeys(
        other["token"]
        for other in items
        if other.get("goal_id") == key and tuple(other.get("scope") or ()) == scope
    ))
    return "复核结论已暂存，回合正常结束后确认；目标仍未完成。", {
        "act": "review_goal", "result": "prepared", "goal_id": key,
        "token": tokens[0], "tokens": tokens, "why": conclusion,
    }


def finish(items: list[dict], receipts: list[dict], *, dry_run: bool, at: str) -> int:
    if dry_run or any(row.get("act") in {"error", "timeout"} for row in receipts):
        return 0
    dispatched = {item["token"]: item for item in items}
    count = 0
    for row in receipts:
        if row.get("act") != "review_goal" or row.get("result") != "reviewed":
            continue
        conclusion = str(row.get("why") or "").strip()
        key = row.get("goal_id")
        base = dispatched.get(row.get("token"))
        if not conclusion or base is None or base.get("goal_id") != key:
            continue
        scope = tuple(base["scope"])
        tokens = row.get("tokens", [row["token"]])
        if not isinstance(tokens, list) or not all(isinstance(token, str) for token in tokens):
            continue
        batch = [dispatched.get(token) for token in tokens]
        if row["token"] not in tokens or any(
            item is None or item.get("goal_id") != key or tuple(item["scope"]) != scope
            for item in batch
        ):
            continue
        candidates = [GoalWake(**{**item, "scope": scope}) for item in batch]
        count += acknowledge_reviews(candidates, at=at, conclusion=conclusion)
    return count


def complete_receipts(receipts: list[dict], *, completed: bool, dry_run: bool, rehearse: bool) -> None:
    for row in receipts:
        if row.get("act") == "review_goal" and row.get("result") == "prepared":
            row["result"] = "reviewed" if completed and not dry_run and not rehearse else "uncommitted"


def prioritize_messages(db: str, review_db: str, items: list[dict], *, changed: bool) -> tuple[str, list[dict]]:
    """已有新消息时不切换到别的账号，候选留待下一拍。"""
    if changed and db != review_db:
        return "", []
    return review_db, items
