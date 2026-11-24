from __future__ import annotations

import json
import structlog
import time
from dataclasses import dataclass, field
from typing import Any

from db.repository import RepositoryManager

log = structlog.get_logger()


@dataclass
class ToolExperience:
    tool_name: str
    query_pattern: str
    success_count: int = 0
    fail_count: int = 0
    avg_latency_ms: float = 0.0
    last_used_at: float = 0.0

    @property
    def success_rate(self) -> float:
        total = self.success_count + self.fail_count
        return self.success_count / total if total > 0 else 0.5

    @property
    def total_uses(self) -> int:
        return self.success_count + self.fail_count


@dataclass
class ComplexityCorrection:
    original_msg: str
    classified_as: str
    should_be: str
    timestamp: float = 0.0


@dataclass
class PlanOutcome:
    goal_pattern: str
    plan_id: str
    subtask_count: int
    completed_count: int = 0
    failed_count: int = 0
    quality_score: float = 0.0
    total_latency_ms: float = 0.0
    timestamp: float = 0.0

    @property
    def success_rate(self) -> float:
        return self.completed_count / self.subtask_count if self.subtask_count > 0 else 0.0


class SelfEvolve:
    def __init__(self, repo: RepositoryManager):
        self.repo = repo
        self._tool_experiences: dict[str, ToolExperience] = {}
        self._complexity_corrections: list[ComplexityCorrection] = []
        self._plan_outcomes: dict[str, list[PlanOutcome]] = {}
        self.portrait = UserPortrait()
        self._load_state()

    def _load_state(self):
        try:
            # UserProfile is a key/value table and the repository already
            # exposes JSON helpers; the get_or_create/extra API this used to
            # call was never implemented, so this never once succeeded.
            state = self.repo.user_profile.get_json("self_evolve_state", {})
            if isinstance(state, str):
                state = json.loads(state or "{}")

            for key, val in state.get("tool_experiences", {}).items():
                self._tool_experiences[key] = ToolExperience(
                    tool_name=val.get("tool_name", ""),
                    query_pattern=val.get("query_pattern", ""),
                    success_count=val.get("success_count", 0),
                    fail_count=val.get("fail_count", 0),
                    avg_latency_ms=val.get("avg_latency_ms", 0.0),
                    last_used_at=val.get("last_used_at", 0.0),
                )

            for val in state.get("complexity_corrections", []):
                self._complexity_corrections.append(ComplexityCorrection(
                    original_msg=val.get("original_msg", ""),
                    classified_as=val.get("classified_as", ""),
                    should_be=val.get("should_be", ""),
                    timestamp=val.get("timestamp", 0.0),
                ))

            for key, outcomes in state.get("plan_outcomes", {}).items():
                self._plan_outcomes[key] = [
                    PlanOutcome(
                        goal_pattern=o.get("goal_pattern", ""),
                        plan_id=o.get("plan_id", ""),
                        subtask_count=o.get("subtask_count", 0),
                        completed_count=o.get("completed_count", 0),
                        failed_count=o.get("failed_count", 0),
                        quality_score=o.get("quality_score", 0.0),
                        total_latency_ms=o.get("total_latency_ms", 0.0),
                        timestamp=o.get("timestamp", 0.0),
                    )
                    for o in outcomes
                ]

            log.info("self_evolve_loaded",
                     tools=len(self._tool_experiences),
                     corrections=len(self._complexity_corrections),
                     patterns=len(self._plan_outcomes))

            portrait_data = state.get("portrait")
            if portrait_data:
                self.portrait = UserPortrait.from_dict(portrait_data)
                log.info("portrait_loaded", style=self.portrait.interaction_style,
                         interactions=self.portrait.total_interactions)
        except Exception as e:
            log.warning("self_evolve_load_failed", error=str(e))

    def save_state(self):
        try:
            state = {
                "tool_experiences": {
                    k: {
                        "tool_name": v.tool_name,
                        "query_pattern": v.query_pattern,
                        "success_count": v.success_count,
                        "fail_count": v.fail_count,
                        "avg_latency_ms": v.avg_latency_ms,
                        "last_used_at": v.last_used_at,
                    }
                    for k, v in self._tool_experiences.items()
                },
                "complexity_corrections": [
                    {
                        "original_msg": c.original_msg[:100],
                        "classified_as": c.classified_as,
                        "should_be": c.should_be,
                        "timestamp": c.timestamp,
                    }
                    for c in self._complexity_corrections[-50:]
                ],
                "plan_outcomes": {
                    k: [
                        {
                            "goal_pattern": o.goal_pattern,
                            "plan_id": o.plan_id,
                            "subtask_count": o.subtask_count,
                            "completed_count": o.completed_count,
                            "failed_count": o.failed_count,
                            "quality_score": o.quality_score,
                            "total_latency_ms": o.total_latency_ms,
                            "timestamp": o.timestamp,
                        }
                        for o in outcomes[-20:]
                    ]
                    for k, outcomes in self._plan_outcomes.items()
                },
                "portrait": self.portrait.to_dict(),
            }
            self.repo.user_profile.set_json("self_evolve_state", state)
        except Exception as e:
            log.warning("self_evolve_save_failed", error=str(e))

    def record_tool_result(
        self, tool_name: str, query_pattern: str,
        success: bool, latency_ms: float = 0.0,
    ):
        key = f"{tool_name}|{query_pattern}"
        exp = self._tool_experiences.get(key)
        if not exp:
            exp = ToolExperience(tool_name=tool_name, query_pattern=query_pattern)
            self._tool_experiences[key] = exp

        if success:
            exp.success_count += 1
        else:
            exp.fail_count += 1

        if exp.avg_latency_ms == 0:
            exp.avg_latency_ms = latency_ms
        else:
            exp.avg_latency_ms = 0.9 * exp.avg_latency_ms + 0.1 * latency_ms
        exp.last_used_at = time.time()

    def record_plan_outcome(self, plan: Any, quality: dict, latency_ms: float = 0.0):
        from agents.planner import Plan, TaskStatus

        if not isinstance(plan, Plan):
            return

        goal_pattern = self._extract_pattern(plan.goal)
        completed = sum(1 for t in plan.subtasks if t.status == TaskStatus.DONE)
        failed = sum(1 for t in plan.subtasks if t.status == TaskStatus.FAILED)

        outcome = PlanOutcome(
            goal_pattern=goal_pattern,
            plan_id=plan.plan_id,
            subtask_count=len(plan.subtasks),
            completed_count=completed,
            failed_count=failed,
            quality_score=quality.get("completeness", 0.0),
            total_latency_ms=latency_ms,
            timestamp=time.time(),
        )

        if goal_pattern not in self._plan_outcomes:
            self._plan_outcomes[goal_pattern] = []
        self._plan_outcomes[goal_pattern].append(outcome)

        for t in plan.subtasks:
            if t.tool:
                self.record_tool_result(
                    t.tool, goal_pattern,
                    success=t.status == TaskStatus.DONE,
                )

    def record_complexity_misclassification(
        self, user_message: str, classified_as: str, should_be: str,
    ):
        self._complexity_corrections.append(ComplexityCorrection(
            original_msg=user_message[:100],
            classified_as=classified_as,
            should_be=should_be,
            timestamp=time.time(),
        ))
        log.info("complexity_correction_recorded",
                 msg=user_message[:30], was=classified_as, should=should_be)

    def suggest_retry_policy(self, tool_name: str) -> dict:
        relevant = [
            exp for exp in self._tool_experiences.values()
            if exp.tool_name == tool_name and exp.total_uses >= 3
        ]
        if not relevant:
            return {"max_retries": 2, "should_cache": True}

        avg_success = sum(e.success_rate for e in relevant) / len(relevant)
        total_uses = sum(e.total_uses for e in relevant)

        if avg_success > 0.9:
            return {"max_retries": 1, "should_cache": True}
        elif avg_success > 0.7:
            return {"max_retries": 2, "should_cache": True}
        elif avg_success > 0.5:
            return {"max_retries": 3, "should_cache": True}
        else:
            return {"max_retries": 1, "should_cache": False}

    def suggest_tools_for_pattern(self, goal_pattern: str) -> list[tuple[str, float]]:
        pattern = self._extract_pattern(goal_pattern)
        outcomes = self._plan_outcomes.get(pattern, [])
        if not outcomes:
            return []

        tool_scores: dict[str, list[float]] = {}
        for outcome in outcomes:
            if outcome.quality_score < 0.3:
                continue
            for key, exp in self._tool_experiences.items():
                if exp.query_pattern == pattern and exp.success_rate > 0.5:
                    tool_scores.setdefault(exp.tool_name, []).append(exp.success_rate)

        ranked = []
        for tool, scores in tool_scores.items():
            avg = sum(scores) / len(scores)
            ranked.append((tool, avg))
        ranked.sort(key=lambda x: x[1], reverse=True)
        return ranked[:5]

    def suggest_complexity_override(self, user_message: str) -> str | None:
        msg_lower = user_message.lower().strip()
        recent_corrections = [
            c for c in self._complexity_corrections
            if time.time() - c.timestamp < 86400 * 7
        ]

        for correction in recent_corrections:
            if self._pattern_match(msg_lower, correction.original_msg.lower()):
                return correction.should_be

        return None

    def get_evolution_report(self) -> dict:
        tool_stats = {}
        for key, exp in self._tool_experiences.items():
            if exp.total_uses < 2:
                continue
            tool_stats.setdefault(exp.tool_name, {"success": 0, "fail": 0, "uses": 0})
            tool_stats[exp.tool_name]["success"] += exp.success_count
            tool_stats[exp.tool_name]["fail"] += exp.fail_count
            tool_stats[exp.tool_name]["uses"] += exp.total_uses

        plan_stats = {
            pattern: {
                "count": len(outcomes),
                "avg_quality": sum(o.quality_score for o in outcomes) / len(outcomes),
                "avg_success_rate": sum(o.success_rate for o in outcomes) / len(outcomes),
            }
            for pattern, outcomes in self._plan_outcomes.items()
            if outcomes
        }

        return {
            "tool_experiences": tool_stats,
            "plan_patterns": plan_stats,
            "complexity_corrections_7d": len([
                c for c in self._complexity_corrections
                if time.time() - c.timestamp < 86400 * 7
            ]),
            "total_lessons": len(self._complexity_corrections),
        }

    @staticmethod
    def _extract_pattern(text: str) -> str:
        import re
        text = re.sub(r'\d+', 'N', text)
        text = re.sub(r'[\w\.-]+@[\w\.-]+', 'EMAIL', text)
        text = re.sub(r'\S+chatroom', 'GROUP', text)
        words = text.strip().split()
        if len(words) <= 3:
            return text.strip()
        return " ".join(words[:3]) + "..."

    @staticmethod
    def _pattern_match(msg: str, pattern: str, threshold: float = 0.6) -> bool:
        msg_words = set(msg.split())
        pattern_words = set(pattern.split())
        if not msg_words or not pattern_words:
            return False
        overlap = len(msg_words & pattern_words) / len(msg_words | pattern_words)
        return overlap >= threshold


@dataclass
class InteractionSignal:
    msg_length: int = 0
    has_question: bool = False
    has_imperative: bool = False
    has_emotion: bool = False
    is_short_reply: bool = False
    is_correction: bool = False
    topic_category: str = ""
    timestamp: float = 0.0


@dataclass
class UserPortrait:
    avg_msg_length: float = 0.0
    msg_length_variance: float = 0.0
    question_ratio: float = 0.0
    imperative_ratio: float = 0.0
    emotion_ratio: float = 0.0
    short_reply_ratio: float = 0.0
    correction_ratio: float = 0.0
    preferred_detail_level: str = "moderate"
    frequent_topics: dict[str, int] = field(default_factory=dict)
    interaction_style: str = ""
    active_hours: list[int] = field(default_factory=list)
    total_interactions: int = 0
    last_updated: float = 0.0

    _IMPERATIVE_WORDS = frozenset([
        "帮我", "给我", "告诉我", "查一下", "搜一下", "整理", "分析",
        "提醒", "记得", "别忘了", "通知", "发", "创建", "删除",
    ])
    _EMOTION_WORDS = frozenset([
        "哈哈", "嘿嘿", "呜", "😭", "😂", "🥺", "生气", "开心",
        "烦", "累", "困", "饿", "好烦", "好累", "太好了", "无语",
    ])
    _SHORT_REPLIES = frozenset([
        "嗯", "哦", "好", "行", "是", "对", "嗯嗯", "好的", "好吧",
        "哈哈", "谢谢", "不了", "不用",
    ])
    _CORRECTION_WORDS = frozenset([
        "不对", "不是", "错了", "搞错", "别搞", "不是这个", "换一个",
    ])
    _TOPIC_KEYWORDS = {
        "工作": ["工作", "项目", "会议", "进度", "汇报", "deadline", "加班", "任务"],
        "社交": ["群里", "谁说", "讨论", "聊天", "微信", "消息"],
        "生活": ["吃", "喝", "睡", "天气", "周末", "放假", "出去玩"],
        "学习": ["学", "看", "读", "教程", "文档", "怎么用", "如何"],
        "情感": ["开心", "烦", "累", "难过", "想", "喜欢", "讨厌"],
        "时间管理": ["提醒", "记得", "别忘了", "几号", "几点", "明天", "下周"],
    }

    def observe(self, user_message: str, timestamp: float | None = None):
        ts = timestamp or time.time()
        msg = user_message.strip()
        n = self.total_interactions

        new_len = len(msg)
        if n == 0:
            self.avg_msg_length = float(new_len)
            self.msg_length_variance = 0.0
        else:
            old_avg = self.avg_msg_length
            self.avg_msg_length = (old_avg * n + new_len) / (n + 1)
            diff = new_len - self.avg_msg_length
            self.msg_length_variance = (self.msg_length_variance * n + diff * diff) / (n + 1)

        has_q = "？" in msg or "?" in msg or any(w in msg for w in ["吗", "呢", "什么", "怎么", "如何", "哪"])
        has_imp = any(w in msg for w in self._IMPERATIVE_WORDS)
        has_emo = any(w in msg for w in self._EMOTION_WORDS)
        is_short = msg in self._SHORT_REPLIES or len(msg) <= 2
        is_corr = any(w in msg for w in self._CORRECTION_WORDS)

        alpha = 1.0 / min(n + 1, 20)
        self.question_ratio += alpha * (float(has_q) - self.question_ratio)
        self.imperative_ratio += alpha * (float(has_imp) - self.imperative_ratio)
        self.emotion_ratio += alpha * (float(has_emo) - self.emotion_ratio)
        self.short_reply_ratio += alpha * (float(is_short) - self.short_reply_ratio)
        self.correction_ratio += alpha * (float(is_corr) - self.correction_ratio)

        for topic, keywords in self._TOPIC_KEYWORDS.items():
            if any(k in msg for k in keywords):
                self.frequent_topics[topic] = self.frequent_topics.get(topic, 0) + 1

        hour = int(time.localtime(ts).tm_hour)
        if hour not in self.active_hours:
            self.active_hours.append(hour)
            self.active_hours.sort()

        self.total_interactions += 1
        self.last_updated = ts

        self._update_style()

    def _update_style(self):
        parts = []
        if self.avg_msg_length < 8:
            parts.append("话少简洁")
        elif self.avg_msg_length > 30:
            parts.append("表达详细")
        else:
            parts.append("表达适中")

        if self.imperative_ratio > 0.4:
            parts.append("直接下指令")
        elif self.question_ratio > 0.5:
            parts.append("爱提问")
        else:
            parts.append("闲聊为主")

        if self.emotion_ratio > 0.3:
            parts.append("情绪外露")
        else:
            parts.append("情绪内敛")

        if self.correction_ratio > 0.1:
            parts.append("要求严格")
        else:
            parts.append("包容度高")

        if self.short_reply_ratio > 0.4:
            parts.append("回复简短")
        elif self.short_reply_ratio < 0.1:
            parts.append("回复认真")

        self.interaction_style = "、".join(parts)

    def suggest_reply_style(self) -> dict:
        style = {"detail": "moderate", "tone": "warm", "proactive": False}

        if self.avg_msg_length < 8 and self.short_reply_ratio > 0.3:
            style["detail"] = "brief"
        elif self.avg_msg_length > 25:
            style["detail"] = "detailed"

        if self.emotion_ratio > 0.3:
            style["tone"] = "empathetic"
        elif self.imperative_ratio > 0.4:
            style["tone"] = "efficient"

        if self.correction_ratio > 0.1:
            style["proactive"] = True

        return style

    def get_top_topics(self, limit: int = 5) -> list[tuple[str, int]]:
        sorted_topics = sorted(
            self.frequent_topics.items(), key=lambda x: x[1], reverse=True
        )
        return sorted_topics[:limit]

    def to_dict(self) -> dict:
        return {
            "avg_msg_length": round(self.avg_msg_length, 1),
            "msg_length_variance": round(self.msg_length_variance, 1),
            "question_ratio": round(self.question_ratio, 3),
            "imperative_ratio": round(self.imperative_ratio, 3),
            "emotion_ratio": round(self.emotion_ratio, 3),
            "short_reply_ratio": round(self.short_reply_ratio, 3),
            "correction_ratio": round(self.correction_ratio, 3),
            "preferred_detail_level": self.preferred_detail_level,
            "frequent_topics": dict(sorted(
                self.frequent_topics.items(), key=lambda x: x[1], reverse=True
            )[:10]),
            "interaction_style": self.interaction_style,
            "active_hours": self.active_hours,
            "total_interactions": self.total_interactions,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "UserPortrait":
        p = cls()
        p.avg_msg_length = data.get("avg_msg_length", 0.0)
        p.msg_length_variance = data.get("msg_length_variance", 0.0)
        p.question_ratio = data.get("question_ratio", 0.0)
        p.imperative_ratio = data.get("imperative_ratio", 0.0)
        p.emotion_ratio = data.get("emotion_ratio", 0.0)
        p.short_reply_ratio = data.get("short_reply_ratio", 0.0)
        p.correction_ratio = data.get("correction_ratio", 0.0)
        p.preferred_detail_level = data.get("preferred_detail_level", "moderate")
        p.frequent_topics = data.get("frequent_topics", {})
        p.interaction_style = data.get("interaction_style", "")
        p.active_hours = data.get("active_hours", [])
        p.total_interactions = data.get("total_interactions", 0)
        p.last_updated = data.get("last_updated", 0.0)
        return p
