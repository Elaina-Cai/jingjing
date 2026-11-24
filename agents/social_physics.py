import structlog
from datetime import datetime, timedelta
from math import log as math_log, exp as math_exp

from db.repository import RepositoryManager
from db.models import Message
from sqlalchemy import select
from core.infra.cache import TTLCache

logger = structlog.get_logger()


def _sigmoid(x: float) -> float:
    if x >= 0:
        return 1.0 / (1.0 + math_exp(-x))
    ex = math_exp(x)
    return ex / (1.0 + ex)


class GroupDynamics:
    def __init__(self, chat_id: str):
        self.chat_id = chat_id
        self.temperature: float = 1.0
        self.msg_rate: float = 0.0
        self.participant_count: int = 0
        self.last_updated: datetime | None = None

    def update(self, recent_msgs: list):
        if not recent_msgs:
            return
        timestamps = []
        senders = set()
        for m in recent_msgs:
            ts = m.created_at if hasattr(m, "created_at") else None
            if ts:
                timestamps.append(ts)
            sender = m.wxid if hasattr(m, "wxid") else None
            if sender:
                senders.add(sender)

        if len(timestamps) >= 2:
            sorted_ts = sorted(timestamps)
            time_span = (sorted_ts[-1] - sorted_ts[0]).total_seconds()
            self.msg_rate = len(recent_msgs) / max(time_span / 60.0, 1.0)
        else:
            self.msg_rate = 0.0

        self.participant_count = len(senders) if senders else 1
        self.temperature = (
            1.0
            + _sigmoid(self.msg_rate - 0.5)
            + 0.1 * math_log(max(self.participant_count, 1))
        )
        self.last_updated = datetime.now()

    @property
    def merge_threshold(self) -> float:
        return 0.8 * self.temperature

    @property
    def split_sensitivity(self) -> float:
        return 0.5 / self.temperature

    @property
    def time_window(self) -> timedelta:
        return timedelta(minutes=2.0 / self.temperature)

    @property
    def context_budget_multiplier(self) -> float:
        return 1.0 / self.temperature

    def to_dict(self) -> dict:
        return {
            "chat_id": self.chat_id,
            "temperature": round(self.temperature, 3),
            "msg_rate": round(self.msg_rate, 3),
            "participant_count": self.participant_count,
            "merge_threshold": round(self.merge_threshold, 3),
            "split_sensitivity": round(self.split_sensitivity, 3),
            "time_window_minutes": round(self.time_window.total_seconds() / 60, 2),
            "context_budget_multiplier": round(self.context_budget_multiplier, 3),
            "last_updated": str(self.last_updated) if self.last_updated else None,
        }


class SocialPhysics:
    def __init__(self, repo: RepositoryManager, unified_query=None):
        self.repo = repo
        self._uq = unified_query
        self._cache: TTLCache[GroupDynamics] = TTLCache(ttl_seconds=300.0, max_entries=100, label="social_physics")

    def get_dynamics(self, chat_id: str, window_minutes: int = 60) -> GroupDynamics:
        cached = self._cache.get(chat_id)
        if cached is not None:
            return cached

        gd = GroupDynamics(chat_id)
        since = datetime.now() - timedelta(minutes=window_minutes)
        if self._uq:
            uq_results = self._uq.search_messages(chat_id=chat_id, since=since, limit=200)
            msgs = [r.data for r in uq_results]
        else:
            with self.repo.messages._session() as s:
                msgs = list(s.execute(
                    select(Message)
                    .where(Message.chat_id == chat_id, Message.created_at >= since)
                    .order_by(Message.created_at.desc())
                    .limit(200)
                ).scalars().all())
        gd.update(list(msgs))
        self._cache.put(chat_id, gd)
        return gd

    def get_all_dynamics(self) -> list[GroupDynamics]:
        if self._uq:
            chat_list = self._uq.get_chat_list()
            chat_ids = list({r.data.wxid for r in chat_list if r.data.is_group})
        else:
            with self.repo.messages._session() as s:
                from sqlalchemy import func
                chat_ids = list(set(s.execute(
                    select(Message.chat_id).where(Message.chat_type == "group")
                ).scalars().all()))
        results = []
        for cid in chat_ids:
            results.append(self.get_dynamics(cid))
        return results

    def apply_to_threads(self, chat_id: str):
        gd = self.get_dynamics(chat_id)
        threads = self.repo.threads.get_active(chat_id, limit=50)
        for t in threads:
            new_budget = int(4096 * gd.context_budget_multiplier)
            new_budget = max(1024, min(new_budget, 16384))
            if t.context_budget != new_budget or abs(t.temperature - gd.temperature) > 0.1:
                self.repo.threads.update(
                    t.id,
                    temperature=gd.temperature,
                    context_budget=new_budget,
                )
        logger.info("social_physics_applied",
                 chat_id=chat_id,
                 temperature=gd.temperature,
                 msg_rate=gd.msg_rate,
                 participants=gd.participant_count,
                 threads_updated=len(threads))
