from __future__ import annotations

from datetime import datetime, timedelta
from typing import Sequence

from sqlalchemy import select, and_, func

from db.models import Reminder, Relationship
from db.repo_base import BaseRepository
from safelog import warn

#: 只有这个状态参与「同一件事已经在册」的复用（见 :meth:`ReminderRepository.find_pending_dedup`）。
#: done / failed / 任何取消态一律不参与：它们已经把话送出去过了。
PENDING_STATUS = "pending"


def normalize_dedup_text(raw) -> str:
    """幂等键里的「规范化文本」：去首尾空白 + 合并内部空白，**仅此而已**。

    不做语义改写、不做模糊/相似度匹配（``docs/任务清单.md`` ENG-25 拍板口径）：
    「叫 Elaina 起床」与「起床啦，八点整了」是两条不同的文本，本函数**不许**
    把它们算成同一个键——只有真正只差空白的两次请求才算同一件事。
    存储用原文（``content`` 列逐字不变），规范化结果只用于比较与留痕。
    """
    return " ".join(str(raw or "").split())


class ReminderRepository(BaseRepository):
    def list_pending(self, persona_id: int | None = None) -> Sequence[Reminder]:
        with self._session() as s:
            q = select(Reminder).where(
                Reminder.status == "pending",
                Reminder.remind_at <= datetime.now(),
            )
            if persona_id:
                q = q.where(Reminder.persona_id == persona_id)
            return s.execute(q.order_by(Reminder.remind_at)).scalars().all()

    def list_all(self, persona_id: int | None = None, limit: int = 50) -> Sequence[Reminder]:
        with self._session() as s:
            q = select(Reminder)
            if persona_id:
                q = q.where(Reminder.persona_id == persona_id)
            return s.execute(q.order_by(Reminder.remind_at.desc()).limit(limit)).scalars().all()

    def get(self, reminder_id: int) -> Reminder | None:
        """按 id 读一条（意图 tick 执行前查权威状态；不做任何写）。"""
        with self._session() as s:
            return s.get(Reminder, reminder_id)

    def add(self, reminder: Reminder) -> Reminder:
        with self._session() as s:
            s.add(reminder)
            s.commit()
            s.refresh(reminder)
            return reminder

    def add_from_dict(self, d: dict) -> Reminder:
        with self._session() as s:
            r = Reminder()
            for k, v in d.items():
                if hasattr(r, k):
                    setattr(r, k, v)
            s.add(r)
            s.commit()
            s.refresh(r)
            return r

    def find_pending_dedup(self, persona_id: int, chat_id: str,
                           due_at: datetime, content: str) -> Reminder | None:
        """同一件事是否已经在册（幂等键 = 会话 × 到点时刻 × 规范化文本）。

        只回答「有没有」与「是哪一条」，写不写库由调用方（``agents/tools/reminder.py``）
        决定：读取进仓储、决定留在创建点，避免第二个创建者出现时两边判据漂移。

        四条纪律（2026-10-02 事故「同一句话她说了两遍」）：

        * **只有 pending 参与**——``done`` / ``failed``（含人工取消）已经把话送出去过
          或明确作废，复用它们等于把提醒吞掉；
        * **时刻必须逐值相等**——不同 ``remind_at`` 是两件合法的事，绝不合并；
        * **文本两侧都走 :func:`normalize_dedup_text`**——只抹空白，不做语义/模糊匹配。
          比较必须"归一化 vs 归一化"：库里存的是**原文**（``content`` 逐字不变），
          直接拿归一化串去比原文会把「只差空白」判成两件事；
        * **``persona_id`` 一起比**——人格之间不共用提醒表。

        候选集按 ``(persona, chat, due)`` 收窄（正常情况下就是 0～2 行），文本比对在
        Python 侧做，避免在 SQL 里再写一套"合并空白"的规则（两份规则 = 等着漂移）。
        返回按 ``id`` 升序的第一条：同一键上已经堆了历史重复行时，永远复用最早那条，
        防止「越修越多」。
        """
        key = str(chat_id or "").strip()
        want = normalize_dedup_text(content)
        if not key or due_at is None or not want:
            return None
        with self._session() as s:
            candidates = s.execute(
                select(Reminder).where(
                    Reminder.persona_id == persona_id,
                    Reminder.source_chat_id == key,
                    Reminder.remind_at == due_at,
                    Reminder.status == PENDING_STATUS,
                ).order_by(Reminder.id)
            ).scalars().all()
        for row in candidates:
            if normalize_dedup_text(row.content) == want:
                return row
        return None

    def mark_done(self, reminder_id: int):
        with self._session() as s:
            r = s.get(Reminder, reminder_id)
            if r:
                if r.repeat_rule:
                    next_at = self._calc_next_repeat(r.remind_at, r.repeat_rule)
                    if next_at:
                        r.remind_at = next_at
                        r.status = "pending"
                        r.attempts = 0
                        s.commit()
                        return
                r.status = "done"
                s.commit()

    def record_attempt(self, reminder_id: int) -> int:
        """记一次投递尝试并返回累计次数（投递失败的止损依据）。"""
        with self._session() as s:
            r = s.get(Reminder, reminder_id)
            if r is None:
                return 0
            r.attempts = int(r.attempts or 0) + 1
            s.commit()
            return r.attempts

    def mark_failed(self, reminder_id: int, reason: str = ""):
        """投递失败到达止损线：停手并保留现场（不再每分钟重扫）。

        reason 只供调用方打日志，不落库；这里只负责让 list_pending 不再命中。
        """
        with self._session() as s:
            r = s.get(Reminder, reminder_id)
            if r:
                r.status = "failed"
                s.commit()

    def cancel(self, reminder_id: int) -> bool:
        """请求依据被撤回后取消权威提醒；已终局的行保持原状态。"""
        with self._session() as s:
            r = s.get(Reminder, reminder_id)
            if r is None:
                return False
            if str(r.status or "") in {"done", "failed", "cancelled"}:
                return str(r.status or "") == "cancelled"
            r.status = "cancelled"
            s.commit()
            return True

    def count_pending_for_requester(self, requester_id: str,
                                    status: str = "pending") -> int:
        """某委托人的待提醒条数（非主人创建上限的依据）。"""
        key = str(requester_id or "").strip()
        if not key:
            return 0
        with self._session() as s:
            return int(s.execute(
                select(func.count()).select_from(Reminder).where(
                    Reminder.requester_id == key,
                    Reminder.status == status,
                )
            ).scalar() or 0)

    @staticmethod
    def _calc_next_repeat(current: datetime | None, rule: str) -> datetime | None:
        if not current or not rule:
            return None
        rule = rule.strip().lower()
        try:
            if rule in ("daily", "每天", "每日"):
                return current + timedelta(days=1)
            elif rule in ("weekly", "每周", "每星期"):
                return current + timedelta(weeks=1)
            elif rule in ("monthly", "每月", "每个月"):
                from dateutil.relativedelta import relativedelta
                return current + relativedelta(months=1)
            elif rule.endswith("h") or rule.endswith("小时"):
                hours = int("".join(c for c in rule if c.isdigit()))
                return current + timedelta(hours=hours)
            elif rule.endswith("d") or rule.endswith("天"):
                days = int("".join(c for c in rule if c.isdigit()))
                return current + timedelta(days=days)
        except Exception as _:
            warn("db/repo_reminder.py:177 Exception", err=repr(_))
        return None

    def delete(self, reminder_id: int):
        with self._session() as s:
            r = s.get(Reminder, reminder_id)
            if r:
                s.delete(r)
                s.commit()


class RelationshipRepository(BaseRepository):
    def get(self, persona_id: int, target_wxid: str) -> Relationship | None:
        with self._session() as s:
            return s.execute(
                select(Relationship).where(
                    Relationship.persona_id == persona_id,
                    Relationship.target_wxid == target_wxid,
                )
            ).scalar_one_or_none()

    def get_by_persona(self, persona_id: int) -> Sequence[Relationship]:
        with self._session() as s:
            return s.execute(
                select(Relationship)
                .where(Relationship.persona_id == persona_id)
                .order_by(Relationship.intimacy.desc())
            ).scalars().all()

    def upsert(self, rel: Relationship) -> Relationship:
        with self._session() as s:
            existing = s.execute(
                select(Relationship).where(
                    Relationship.persona_id == rel.persona_id,
                    Relationship.target_wxid == rel.target_wxid,
                )
            ).scalar_one_or_none()
            if existing:
                existing.relation_type = rel.relation_type
                existing.intimacy = rel.intimacy
                existing.notes = rel.notes
                existing.updated_at = datetime.now()
                s.commit()
                s.refresh(existing)
                return existing
            else:
                s.add(rel)
                s.commit()
                s.refresh(rel)
                return rel

    def set_affect(self, persona_id: int, target_wxid: str, *,
                   warmth: float, reason: str = "",
                   at: datetime | None = None) -> bool:
        """Write only the transient affect columns of an existing row.

        Deliberately separate from ``upsert``: that one owns the structural
        relationship (type / intimacy / notes) and is called after every reply
        by ``RelationshipManager``, so letting it carry affect would mean two
        writers with different clocks fighting over one row.

        Never inserts.  A stranger who has no relationship row yet must not
        get one created just because she felt something, and returning False
        lets the caller fall back to no-affect behaviour.
        """
        with self._session() as s:
            row = s.execute(
                select(Relationship).where(
                    Relationship.persona_id == persona_id,
                    Relationship.target_wxid == target_wxid,
                )
            ).scalar_one_or_none()
            if row is None:
                return False
            if not hasattr(row, "warmth"):
                return False
            row.warmth = float(warmth)
            row.warmth_reason = reason or ""
            row.warmth_updated_at = at or datetime.now()
            s.commit()
            return True
