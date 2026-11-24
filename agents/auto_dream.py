import asyncio
import json
import structlog
from collections import Counter
from datetime import datetime, timedelta
from math import sqrt

from db.repository import RepositoryManager
from db.models import Thread, ThreadMsgRef
from db.thread_scope import require_same_source, resolve_thread_ref, source_key
from core.preprocess.relevance import ThreadPredictor, _extract_keywords, _semantic_text
from sqlalchemy import select
from safelog import warn

log = structlog.get_logger()


class AutoDream:
    MERGE_KEYWORD_OVERLAP = 0.6
    MERGE_MEMBER_OVERLAP = 0.5
    MERGE_TIME_GAP_MINUTES = 30
    SPLIT_KEYWORD_DIVERGENCE = 0.8
    SPLIT_MIN_MSG_COUNT = 10
    ORPHAN_MAX_MSG_COUNT = 2
    ORPHAN_MIN_KW_OVERLAP = 0.3
    SUMMARY_ROUGH_MAX_CHARS = 200
    BUDGET_BASE = 4096
    BUDGET_MIN = 1024
    BUDGET_MAX = 8192

    def __init__(self, repo: RepositoryManager, llm=None, unified_query=None):
        # [2026-11-02 去向量收尾] embedding/vector_store 两个参数随向量能力移除
        # （赋了值没人读）。
        self.repo = repo
        self._llm = llm
        self._uq = unified_query

    def detect_merge_candidates(self, chat_id: str) -> list[dict]:
        threads = self.repo.threads.get_active(chat_id, limit=30)
        if len(threads) < 2:
            return []

        predictors = {}
        for t in threads:
            predictors[t.id] = ThreadPredictor.from_snapshot(t.predictor_snapshot)

        candidates = []
        for i in range(len(threads)):
            for j in range(i + 1, len(threads)):
                t1, t2 = threads[i], threads[j]
                p1, p2 = predictors[t1.id], predictors[t2.id]

                kw_overlap = self._keyword_overlap(p1, p2)
                member_overlap = self._member_overlap(p1, p2)
                time_gap = self._time_gap(t1, t2)

                if kw_overlap >= self.MERGE_KEYWORD_OVERLAP:
                    score = kw_overlap * 0.6 + member_overlap * 0.3 + (1 - min(time_gap / 60, 1)) * 0.1
                    candidates.append({
                        "action": "merge",
                        "source_id": t2.id,
                        "target_id": t1.id,
                        "source_scope": t2.scope or "",
                        "target_scope": t1.scope or "",
                        "score": round(score, 3),
                        "reason": f"关键词重叠{kw_overlap:.0%}, 成员重叠{member_overlap:.0%}, 时间间隔{time_gap:.0f}分钟",
                    })

        candidates.sort(key=lambda x: x["score"], reverse=True)
        return candidates[:5]

    def detect_split_candidates(self, chat_id: str) -> list[dict]:
        threads = self.repo.threads.get_active(chat_id, limit=30)
        candidates = []

        for t in threads:
            if (t.msg_count or 0) < self.SPLIT_MIN_MSG_COUNT:
                continue

            refs = self.repo.thread_refs.get_by_thread(t.id, limit=50)
            if len(refs) < self.SPLIT_MIN_MSG_COUNT:
                continue

            keyword_sets = []
            for ref in refs:
                try:
                    msg = resolve_thread_ref(self._uq, t, ref)
                    if msg and msg.content:
                        keyword_sets.append(_extract_keywords(msg.content))
                except Exception as _:
                    warn("agents/auto_dream.py:95 Exception", err=repr(_))

            if len(keyword_sets) < 5:
                continue

            divergence = self._keyword_divergence(keyword_sets)
            if divergence > self.SPLIT_KEYWORD_DIVERGENCE:
                candidates.append({
                    "action": "split",
                    "thread_id": t.id,
                    "scope": t.scope or "",
                    "score": round(divergence, 3),
                    "msg_count": t.msg_count,
                    "reason": f"关键词分歧度{divergence:.0%}, {len(keyword_sets)}条消息",
                })

        candidates.sort(key=lambda x: x["score"], reverse=True)
        return candidates[:3]

    def execute_merge(self, source_id: int, target_id: int) -> bool:
        if source_id == target_id:
            raise ValueError("thread_merge_same_target")
        src_thread = self.repo.threads.get_by_id(source_id)
        tgt_thread = self.repo.threads.get_by_id(target_id)
        require_same_source(src_thread, tgt_thread)
        src_refs = self.repo.thread_refs.get_by_thread(source_id)
        require_same_source(src_thread, *src_refs)
        moved = 0
        for ref in src_refs:
            existing = self.repo.thread_refs.get_by_msg(ref.msg_id)
            already_in_target = any(r.thread_id == target_id for r in existing)
            if not already_in_target:
                self.repo.thread_refs.add(ThreadMsgRef(
                    platform=ref.platform, account_id=ref.account_id, chat_id=ref.chat_id,
                    thread_id=target_id,
                    msg_id=ref.msg_id,
                    relevance=ref.relevance * 0.9,
                    confidence=ref.confidence * 0.9,
                    status="confirmed",
                ))
                moved += 1
            self.repo.thread_refs.remove(source_id, ref.msg_id)

        src_thread = self.repo.threads.get_by_id(source_id)
        tgt_thread = self.repo.threads.get_by_id(target_id)
        if src_thread and tgt_thread:
            tgt_predictor = ThreadPredictor.from_snapshot(tgt_thread.predictor_snapshot)
            src_predictor = ThreadPredictor.from_snapshot(src_thread.predictor_snapshot)
            for kw, cnt in src_predictor.recent_keywords.items():
                tgt_predictor.recent_keywords[kw] += cnt * 0.5
            tgt_predictor.active_members.update(src_predictor.active_members)
            self.repo.threads.update(
                target_id,
                predictor_snapshot=tgt_predictor.to_snapshot(),
                msg_count=(tgt_thread.msg_count or 0) + moved,
                updated_at=datetime.now(),
            )
            self.repo.threads.update(source_id, status="merged")

        log.info("auto_dream_merge", source_id=source_id, target_id=target_id, moved=moved)
        return True

    def execute_split(self, thread_id: int, split_point: int = 0) -> tuple[int, int] | None:
        thread = self.repo.threads.get_by_id(thread_id)
        if not thread:
            return None

        refs = self.repo.thread_refs.get_by_thread(thread_id, limit=100)
        require_same_source(thread, *refs)
        if len(refs) < self.SPLIT_MIN_MSG_COUNT:
            return None

        mid_idx = len(refs) // 2 if split_point == 0 else min(split_point, len(refs) - 1)

        now = datetime.now()
        new_thread = self.repo.threads.create(Thread(
            platform=thread.platform, account_id=thread.account_id,
            chat_id=thread.chat_id,
            scope=(thread.scope or "分裂") + " (2)",
            status="active",
            parent_id=thread_id,
            last_active_at=now,
            msg_count=0,
            created_at=now,
            updated_at=now,
        ))

        moved = 0
        for ref in refs[mid_idx:]:
            self.repo.thread_refs.add(ThreadMsgRef(
                platform=ref.platform, account_id=ref.account_id, chat_id=ref.chat_id,
                thread_id=new_thread.id,
                msg_id=ref.msg_id,
                relevance=ref.relevance,
                confidence=ref.confidence,
                status="confirmed",
            ))
            self.repo.thread_refs.remove(thread_id, ref.msg_id)
            moved += 1

        self.repo.threads.update(
            thread_id,
            msg_count=max((thread.msg_count or 0) - moved, 0),
            updated_at=now,
        )
        self.repo.threads.update(
            new_thread.id,
            msg_count=moved,
        )

        log.info("auto_dream_split", thread_id=thread_id, new_thread_id=new_thread.id, moved=moved)
        return (thread_id, new_thread.id)

    def dream(self, chat_id: str) -> dict:
        merge_candidates = self.detect_merge_candidates(chat_id)
        split_candidates = self.detect_split_candidates(chat_id)
        return {
            "chat_id": chat_id,
            "merge_candidates": merge_candidates,
            "split_candidates": split_candidates,
        }

    def dream_all(self) -> list[dict]:
        if self._uq:
            chat_list = self._uq.get_chat_list()
            chat_ids = [r.data.wxid for r in chat_list if r.data.is_group]
        else:
            chat_ids = self.repo.messages.get_chat_ids(chat_type="group")
        results = []
        for chat_id in chat_ids:
            try:
                result = self.dream(chat_id)
                if result.get("merge_candidates") or result.get("split_candidates"):
                    results.append(result)
            except Exception as e:
                log.warning("dream_chat_failed", chat_id=chat_id, error=str(e))
        return results

    def _keyword_overlap(self, p1: ThreadPredictor, p2: ThreadPredictor) -> float:
        if not p1.recent_keywords or not p2.recent_keywords:
            return 0.0
        k1 = set(p1.recent_keywords.keys())
        k2 = set(p2.recent_keywords.keys())
        if not k1 or not k2:
            return 0.0
        intersection = k1 & k2
        union = k1 | k2
        return len(intersection) / len(union)

    def _member_overlap(self, p1: ThreadPredictor, p2: ThreadPredictor) -> float:
        if not p1.active_members or not p2.active_members:
            return 0.0
        intersection = p1.active_members & p2.active_members
        union = p1.active_members | p2.active_members
        return len(intersection) / len(union)

    def _time_gap(self, t1: Thread, t2: Thread) -> float:
        if not t1.last_active_at or not t2.last_active_at:
            return 999.0
        return abs((t1.last_active_at - t2.last_active_at).total_seconds()) / 60

    def _keyword_divergence(self, keyword_sets: list[set[str]]) -> float:
        if len(keyword_sets) < 2:
            return 0.0
        n = len(keyword_sets)
        total_overlap = 0
        comparisons = 0
        half = n // 2
        first_half_keywords = Counter()
        second_half_keywords = Counter()
        for i, kws in enumerate(keyword_sets):
            if i < half:
                for kw in kws:
                    first_half_keywords[kw] += 1
            else:
                for kw in kws:
                    second_half_keywords[kw] += 1

        if not first_half_keywords or not second_half_keywords:
            return 0.0

        k1 = set(first_half_keywords.keys())
        k2 = set(second_half_keywords.keys())
        intersection = k1 & k2
        union = k1 | k2
        if not union:
            return 0.0

        overlap_ratio = len(intersection) / len(union)
        return 1.0 - overlap_ratio

    def detect_orphan_candidates(self, chat_id: str) -> list[dict]:
        threads = self.repo.threads.get_active(chat_id, limit=50)
        orphans = []
        for t in threads:
            if (t.msg_count or 0) <= self.ORPHAN_MAX_MSG_COUNT:
                orphans.append(t)

        if not orphans:
            return []

        siblings = [t for t in threads if (t.msg_count or 0) > self.ORPHAN_MAX_MSG_COUNT]
        if not siblings:
            return []

        candidates = []
        for orphan in orphans:
            p_orphan = ThreadPredictor.from_snapshot(orphan.predictor_snapshot)
            if not p_orphan.recent_keywords:
                continue

            best_sibling = None
            best_overlap = 0.0
            for sib in siblings:
                p_sib = ThreadPredictor.from_snapshot(sib.predictor_snapshot)
                overlap = self._keyword_overlap(p_orphan, p_sib)
                time_gap = self._time_gap(orphan, sib)
                time_bonus = max(0, 1 - time_gap / 120) * 0.2
                adjusted = overlap + time_bonus
                if adjusted > best_overlap:
                    best_overlap = adjusted
                    best_sibling = sib

            if best_sibling and best_overlap >= self.ORPHAN_MIN_KW_OVERLAP:
                candidates.append({
                    "action": "adopt",
                    "orphan_id": orphan.id,
                    "orphan_scope": orphan.scope or "",
                    "orphan_msg_count": orphan.msg_count or 0,
                    "target_id": best_sibling.id,
                    "target_scope": best_sibling.scope or "",
                    "score": round(best_overlap, 3),
                    "reason": f"孤儿线程{orphan.msg_count}条消息, 关键词重叠{best_overlap:.0%}",
                })

        candidates.sort(key=lambda x: x["score"], reverse=True)
        return candidates[:5]

    def execute_adopt(self, orphan_id: int, target_id: int) -> bool:
        orphan = self.repo.threads.get_by_id(orphan_id)
        target = self.repo.threads.get_by_id(target_id)
        if not orphan or not target:
            log.warning("adopt_missing_thread", orphan_id=orphan_id, target_id=target_id)
            return False

        if orphan_id == target_id:
            return False

        return self.execute_merge(orphan_id, target_id)

    async def detect_summary_upgrade_candidates(self, chat_id: str) -> list[dict]:
        threads = self.repo.threads.get_active(chat_id, limit=50)
        candidates = []
        for t in threads:
            if t.summary_quality != "rough":
                continue
            if (t.msg_count or 0) < 3:
                continue

            refs = self.repo.thread_refs.get_by_thread(t.id, limit=20)
            if not refs:
                continue

            msg_contents = []
            for ref in refs[:10]:
                msg = resolve_thread_ref(self._uq, t, ref)
                if msg and msg.content:
                    msg_contents.append(msg.content[:200])

            if not msg_contents:
                continue

            candidates.append({
                "action": "upgrade_summary",
                "thread_id": t.id,
                "scope": t.scope or "",
                "msg_count": t.msg_count or 0,
                "msg_sample_count": len(msg_contents),
            })

        return candidates[:5]

    async def execute_summary_upgrade(self, thread_id: int) -> dict:
        thread = self.repo.threads.get_by_id(thread_id)
        if not thread:
            return {"ok": False, "error": "线程不存在"}

        if not self._llm:
            return {"ok": False, "error": "LLM客户端未配置"}

        refs = self.repo.thread_refs.get_by_thread(thread_id, limit=20)
        msg_contents = []
        for ref in refs:
            msg = resolve_thread_ref(self._uq, thread, ref)
            if msg and msg.content:
                sender = "自己" if msg.is_self else (msg.sender_id or "对方")
                msg_contents.append(f"[{sender}]: {msg.content[:200]}")

        if not msg_contents:
            return {"ok": False, "error": "线程无消息内容"}

        conversation_text = "\n".join(msg_contents[:15])

        system = (
            "你是一个对话摘要助手。请根据以下对话内容，生成一段精炼的摘要，"
            "包含：1)讨论的主要话题 2)关键结论或决定 3)待办事项（如有）。"
            "摘要应简洁但信息完整，150字以内。"
        )
        user = f"线程主题：{thread.scope or '未知'}\n\n对话内容：\n{conversation_text}"

        try:
            new_summary = await self._llm.chat(
                system=system,
                user=user,
                agent_name="dream_summary_upgrade",
                temperature=0.3,
                max_tokens=300,
            )
        except Exception as e:
            log.warning("summary_upgrade_llm_failed", thread_id=thread_id, error=str(e))
            return {"ok": False, "error": f"LLM调用失败: {e}"}

        if not new_summary or not new_summary.strip():
            return {"ok": False, "error": "LLM返回空摘要"}

        self.repo.threads.update(
            thread_id,
            summary=new_summary.strip(),
            summary_quality="refined",
            updated_at=datetime.now(),
        )

        log.info("summary_upgraded", thread_id=thread_id, summary_len=len(new_summary))
        return {"ok": True, "thread_id": thread_id, "summary": new_summary.strip()}

    def detect_budget_candidates(self, chat_id: str) -> list[dict]:
        threads = self.repo.threads.get_active(chat_id, limit=50)
        if not threads:
            return []

        now = datetime.now()
        window = timedelta(hours=6)
        window_hours = window.total_seconds() / 3600

        keys = {t.id: require_same_source(t) for t in threads}
        if self._uq is None:
            raise ValueError("thread_budget_source_query_unavailable")
        recent_by_source = {}
        for key in set(keys.values()):
            rows = self._uq.search_messages(platform=key[0], account_id=key[1], chat_id=key[2],
                                            since=now - window, until=now, limit=500)
            recent_by_source[key] = {
                r.data.id for r in rows
                if (str(getattr(r, "platform", "") or ""), str(getattr(r, "account_id", "") or ""),
                    str(getattr(r.data, "chat_id", "") or "")) == key
                and getattr(r.data, "created_at", None) is not None
                and now - window <= r.data.created_at <= now
            }

        msg_rate_map: dict[int, float] = {}
        for t in threads:
            refs = self.repo.thread_refs.get_by_thread(t.id, limit=100)
            require_same_source(t, *refs)
            recent_count = len({ref.msg_id for ref in refs if ref.thread_id == t.id
                                and ref.msg_id in recent_by_source[keys[t.id]]})
            msg_rate_map[t.id] = recent_count / max(window_hours, 1)

        total_rates = Counter()
        thread_counts = Counter(keys.values())
        for t in threads:
            total_rates[keys[t.id]] += msg_rate_map[t.id]

        candidates = []
        for t in threads:
            rate = msg_rate_map.get(t.id, 0)
            share = rate / (total_rates[keys[t.id]] or 1.0)
            new_budget = int(self.BUDGET_BASE * (0.5 + share * thread_counts[keys[t.id]]))
            new_budget = max(self.BUDGET_MIN, min(self.BUDGET_MAX, new_budget))

            old_budget = t.context_budget or self.BUDGET_BASE
            if new_budget != old_budget:
                candidates.append({
                    "action": "reallocate_budget",
                    "thread_id": t.id,
                    "scope": t.scope or "",
                    "old_budget": old_budget,
                    "new_budget": new_budget,
                    "msg_rate": round(rate, 2),
                    "share": round(share, 3),
                })

        return candidates[:10]

    def execute_budget_reallocation(self, chat_id: str) -> dict:
        candidates = self.detect_budget_candidates(chat_id)
        applied = 0
        for c in candidates:
            self.repo.threads.update(
                c["thread_id"],
                context_budget=c["new_budget"],
                updated_at=datetime.now(),
            )
            applied += 1

        log.info("budget_reallocation", chat_id=chat_id, applied=applied)
        return {"chat_id": chat_id, "applied": applied, "candidates": candidates}

    async def full_dream(self, chat_id: str) -> dict:
        result = self.dream(chat_id)

        result["orphan_candidates"] = self.detect_orphan_candidates(chat_id)
        result["summary_candidates"] = await self.detect_summary_upgrade_candidates(chat_id)
        result["budget_candidates"] = self.detect_budget_candidates(chat_id)

        return result
