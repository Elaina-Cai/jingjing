import structlog
from datetime import datetime
from math import exp

from db.repository import RepositoryManager
from db.models import FrictionEvent, ThreadMsgRef
from db.thread_scope import require_same_source, resolve_thread_ref
from core.preprocess.relevance import ThreadPredictor, _extract_keywords, _semantic_text
from sqlalchemy import select

log = structlog.get_logger()


class FeedbackSystem:
    def __init__(self, repo: RepositoryManager, unified_query=None):
        self.repo = repo
        self._uq = unified_query

    def on_user_correction(self, msg_id: int, from_thread_id: int, to_thread_id: int) -> dict:
        self._move_message(msg_id, from_thread_id, to_thread_id)

        diagnosis = self._diagnose(msg_id, from_thread_id, to_thread_id)

        event_id = self.repo.friction.add_event(
            event_type="manual_correction",
            msg_id=msg_id,
            from_thread_id=from_thread_id,
            to_thread_id=to_thread_id,
            diagnosis_cause=diagnosis.get("cause"),
            diagnosis_detail=diagnosis.get("detail"),
        )

        if diagnosis:
            applied = self._apply_lesson(diagnosis, event_id)
            diagnosis["lesson_applied"] = applied
        else:
            diagnosis = {"cause": "unknown", "detail": "无法诊断原因"}

        log.info("user_correction",
                 msg_id=msg_id, src=from_thread_id, dst=to_thread_id,
                 cause=diagnosis.get("cause"))

        return diagnosis

    def _move_message(self, msg_id: int, from_thread_id: int, to_thread_id: int):
        if from_thread_id == to_thread_id:
            return
        src_thread = self.repo.threads.get_by_id(from_thread_id)
        tgt_thread = self.repo.threads.get_by_id(to_thread_id)
        existing = self.repo.thread_refs.get_by_msg(msg_id)
        source_refs = [r for r in existing if r.thread_id == from_thread_id]
        if not source_refs:
            raise ValueError("thread_source_ref_missing")
        target_refs = [r for r in existing if r.thread_id == to_thread_id]
        require_same_source(src_thread, tgt_thread, *source_refs, *target_refs)
        source_ref = source_refs[0]
        already_in_target = bool(target_refs)
        if not already_in_target:
            self.repo.thread_refs.add(ThreadMsgRef(
                platform=source_ref.platform, account_id=source_ref.account_id, chat_id=source_ref.chat_id,
                thread_id=to_thread_id,
                msg_id=msg_id,
                relevance=0.9,
                confidence=0.95,
                status="confirmed",
            ))

        self.repo.thread_refs.remove(from_thread_id, msg_id)
        if src_thread:
            new_count = max((src_thread.msg_count or 1) - 1, 0)
            self.repo.threads.update(from_thread_id, msg_count=new_count, updated_at=datetime.now())

        tgt_thread = self.repo.threads.get_by_id(to_thread_id)
        if tgt_thread:
            self.repo.threads.update(
                to_thread_id,
                msg_count=(tgt_thread.msg_count or 0) + (0 if already_in_target else 1),
                updated_at=datetime.now(),
            )

        tgt_thread2 = self.repo.threads.get_by_id(to_thread_id)
        if tgt_thread2:
            predictor = ThreadPredictor.from_snapshot(tgt_thread2.predictor_snapshot)
            msg = resolve_thread_ref(self._uq, src_thread, source_ref)
            if msg:
                fake_msg = {
                    "content": msg.content,
                    "msg_type": msg.msg_type,
                    "wxid": msg.wxid or "",
                    "created_at": msg.created_at,
                    "extra": msg.extra,
                }
                predictor.update(fake_msg)
                self.repo.threads.update(to_thread_id, predictor_snapshot=predictor.to_snapshot())

    def _diagnose(self, msg_id: int, from_thread_id: int, to_thread_id: int) -> dict | None:
        from_thread = self.repo.threads.get_by_id(from_thread_id)
        to_thread = self.repo.threads.get_by_id(to_thread_id)
        require_same_source(from_thread, to_thread)
        refs = [r for r in self.repo.thread_refs.get_by_msg(msg_id) if r.thread_id == to_thread_id]
        if not refs:
            return None
        msg = resolve_thread_ref(self._uq, to_thread, refs[0])
        if not msg:
            return None

        from_p = ThreadPredictor.from_snapshot(from_thread.predictor_snapshot)
        to_p = ThreadPredictor.from_snapshot(to_thread.predictor_snapshot)

        msg_keywords = _extract_keywords(msg.content or "")
        from_overlap = self._keyword_overlap(msg_keywords, set(from_p.recent_keywords.keys()))
        to_overlap = self._keyword_overlap(msg_keywords, set(to_p.recent_keywords.keys()))

        if from_overlap > to_overlap + 0.2:
            return {
                "cause": "predictor_misleading",
                "detail": f"原线程关键词重叠{from_overlap:.0%} > 目标线程{to_overlap:.0%}，但用户判断应移走",
                "suggestion": "关键词匹配权重过高，应更多考虑语义和对话结构",
            }

        if msg.reply_to:
            reply_refs = self.repo.thread_refs.get_by_msg(msg.reply_to)
            reply_thread_ids = {r.thread_id for r in reply_refs}
            if from_thread_id in reply_thread_ids and to_thread_id not in reply_thread_ids:
                return {
                    "cause": "reply_to_mismatch",
                    "detail": f"消息回复的父消息在线程{from_thread_id}，但用户移到了{to_thread_id}",
                    "suggestion": "reply_to不应作为唯一归属依据，可能存在跨线程回复",
                }

        if from_overlap < 0.1 and to_overlap > 0.3:
            return {
                "cause": "initial_misassignment",
                "detail": f"原线程关键词重叠仅{from_overlap:.0%}，目标线程{to_overlap:.0%}，初始分配就错了",
                "suggestion": "提高粗分配的关键词重叠阈值",
            }

        return {
            "cause": "ambiguous_context",
            "detail": f"原线程重叠{from_overlap:.0%}，目标线程重叠{to_overlap:.0%}，两者接近",
            "suggestion": "消息本身有歧义，可考虑M:N多引用",
        }

    def _apply_lesson(self, diagnosis: dict, event_id: int) -> bool:
        cause = diagnosis.get("cause", "")
        if cause == "predictor_misleading":
            similar = self.repo.friction.get_by_cause("predictor_misleading", limit=10)
            if len(similar) >= 3:
                log.info("lesson_learned", cause=cause, pattern_count=len(similar),
                         action="reduce_keyword_weight")
                for evt in similar:
                    self.repo.friction.mark_lesson_applied(evt.id)
                return True

        elif cause == "initial_misassignment":
            similar = self.repo.friction.get_by_cause("initial_misassignment", limit=10)
            if len(similar) >= 3:
                log.info("lesson_learned", cause=cause, pattern_count=len(similar),
                         action="raise_assignment_threshold")
                for evt in similar:
                    self.repo.friction.mark_lesson_applied(evt.id)
                return True

        return False

    def analyze_friction_patterns(self) -> list[dict]:
        cause_counts = self.repo.friction.count_by_cause()
        recent = self.repo.friction.get_recent(limit=100)

        patterns = []
        for cause, count in cause_counts.items():
            cause_events = [e for e in recent if e.diagnosis_cause == cause]
            unlearned = [e for e in cause_events if not e.lesson_applied]
            patterns.append({
                "cause": cause,
                "total_count": count,
                "recent_count": len(cause_events),
                "unlearned_count": len(unlearned),
                "severity": len(unlearned) / max(count, 1),
                "suggestion": self._suggest_action(cause, count),
            })

        patterns.sort(key=lambda p: p["recent_count"], reverse=True)
        return patterns

    def _suggest_action(self, cause: str, count: int) -> str:
        if cause == "predictor_misleading":
            return "降低关键词匹配权重，增加语义相似度权重" if count >= 3 else "继续观察"
        elif cause == "initial_misassignment":
            return "提高粗分配阈值，减少误分配" if count >= 3 else "继续观察"
        elif cause == "reply_to_mismatch":
            return "reply_to不应作为唯一归属依据" if count >= 2 else "继续观察"
        elif cause == "ambiguous_context":
            return "考虑M:N多引用而非1:1归属" if count >= 3 else "继续观察"
        return "继续观察"

    @staticmethod
    def _keyword_overlap(k1: set[str], k2: set[str]) -> float:
        if not k1 or not k2:
            return 0.0
        return len(k1 & k2) / len(k1 | k2)
