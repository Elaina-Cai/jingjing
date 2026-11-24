from __future__ import annotations

import time
import structlog
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any
from safelog import warn

log = structlog.get_logger()


class PipelineAction(Enum):
    CONTINUE = auto()
    ABORT = auto()


@dataclass
class MessageContext:
    user_id: str = ""
    chat_id: str = ""
    message: str = ""
    platform: str = "wechat"
    is_group: bool = False
    is_admin: bool = False
    complexity: str = "moderate"
    hints: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    _action: PipelineAction = PipelineAction.CONTINUE
    _abort_reason: str = ""
    account_id: str = ""

    @property
    def should_continue(self) -> bool:
        return self._action == PipelineAction.CONTINUE

    def abort(self, reason: str):
        self._action = PipelineAction.ABORT
        self._abort_reason = reason

    def add_hint(self, hint: str):
        self.hints.append(hint)

    @classmethod
    def from_inbound(cls, msg: Any) -> MessageContext:
        from core.execution.provider import InboundMessage
        if not isinstance(msg, InboundMessage):
            raise TypeError(f"Expected InboundMessage, got {type(msg)}")
        ctx = cls(
            user_id=msg.user_id,
            chat_id=msg.chat_id,
            message=msg.content,
            platform=msg.platform.value,
            is_group=msg.is_group,
            account_id=getattr(msg, "account_id", ""),
        )
        ctx.metadata["msg_type"] = msg.msg_type.value
        ctx.metadata["msg_id"] = msg.msg_id
        ctx.metadata["user_name"] = msg.user_name
        if msg.secondary_types:
            ctx.metadata["secondary_types"] = [t.value for t in msg.secondary_types]
        if msg.mentions:
            ctx.metadata["mentions"] = [{"user_id": m.user_id, "user_name": m.user_name} for m in msg.mentions]
        if msg.media.path or msg.media.url or msg.media.file_id:
            ctx.metadata["has_media"] = True
        if msg.link.url:
            ctx.metadata["link_url"] = msg.link.url
        if msg.quote.reply_to_id:
            ctx.metadata["quote_reply_to"] = msg.quote.reply_to_id
        return ctx


class Stage(ABC):
    name: str = ""
    order: int = 50

    @abstractmethod
    async def process(self, ctx: MessageContext) -> MessageContext:
        ...


class WhitelistStage(Stage):
    name = "whitelist"
    order = 10

    def __init__(self, allowed_users: set[str] | None = None,
                 allowed_groups: set[str] | None = None):
        self.allowed_users = allowed_users
        self.allowed_groups = allowed_groups

    async def process(self, ctx: MessageContext) -> MessageContext:
        if self.allowed_users is None and self.allowed_groups is None:
            return ctx
        if ctx.is_admin:
            return ctx
        if self.allowed_users and ctx.user_id in self.allowed_users:
            return ctx
        if self.allowed_groups and ctx.is_group and ctx.chat_id in self.allowed_groups:
            return ctx
        ctx.abort("whitelist_rejected")
        return ctx


class RateLimitStage(Stage):
    name = "rate_limit"
    order = 20

    def __init__(self, max_per_minute: int = 30, per_user: bool = True):
        self.max_per_minute = max_per_minute
        self.per_user = per_user
        self._windows: dict[str, list[float]] = {}

    def _allow(self, key: str) -> bool:
        now = time.time()
        cutoff = now - 60
        timestamps = [t for t in self._windows.get(key, []) if t > cutoff]
        self._windows[key] = timestamps
        if len(timestamps) >= self.max_per_minute:
            return False
        timestamps.append(now)
        return True

    async def process(self, ctx: MessageContext) -> MessageContext:
        key = (ctx.user_id or ctx.chat_id) if self.per_user else "_global"
        if not self._allow(key):
            ctx.abort("rate_limit_exceeded")
            log.warning("pipeline_rate_limited", key=key)
        return ctx


class ContentSafetyStage(Stage):
    name = "content_safety"
    order = 30

    def __init__(self, blocked_words: list[str] | None = None,
                 max_length: int = 10000):
        self.blocked_words = blocked_words or []
        self.max_length = max_length

    async def process(self, ctx: MessageContext) -> MessageContext:
        if len(ctx.message) > self.max_length:
            ctx.abort("message_too_long")
            return ctx
        for word in self.blocked_words:
            if word in ctx.message:
                ctx.abort("blocked_content")
                return ctx
        return ctx


class ClassifyStage(Stage):
    name = "classify"
    order = 40

    _COMPLEX = frozenset({
        "讨论什么", "进展", "进度", "怎么样了", "说了什么",
        "群里", "谁说的", "聊了什么", "最新消息",
        "整理微信", "今日分析", "群聊摘要", "日报", "谁找了我",
        "搜索", "搜一下", "查一下", "天气",
        "提醒", "别忘了", "待办",
        "帮我", "帮我查", "帮我找", "帮我整理",
        "对比", "比较", "为什么", "怎么", "如何",
        "总结", "汇总", "分析一下",
    })

    # B1 收编：时间压力词表唯一 owner = core.routing.serious_signals。
    from core.routing.serious_signals import URGENT_MARKERS as _URGENT

    _EMOTIONAL = frozenset({
        "难过", "崩溃", "烦", "累", "气死", "受不了", "郁闷", "焦虑",
        "谢谢", "感谢", "辛苦", "开心", "恭喜", "加油",
    })

    async def process(self, ctx: MessageContext) -> MessageContext:
        msg = ctx.message.strip()

        if len(msg) <= 4 and not any(p in msg for p in self._COMPLEX):
            ctx.complexity = "simple"
        elif any(p in msg for p in self._COMPLEX):
            ctx.complexity = "complex"
        elif len(msg) > 30 and any(w in msg for w in ["和", "与", "然后", "接着", "另外", "同时", "再"]):
            ctx.complexity = "complex"
        else:
            ctx.complexity = "moderate"

        if any(k in msg for k in self._URGENT):
            ctx.metadata["urgency"] = "high"
        elif any(k in msg for k in self._EMOTIONAL):
            ctx.metadata["urgency"] = "emotional"
        else:
            ctx.metadata["urgency"] = "normal"

        ctx.metadata["complexity"] = ctx.complexity
        return ctx


class Pipeline:
    def __init__(self, stages: list[Stage] | None = None):
        self._stages: list[Stage] = sorted(stages or [], key=lambda s: s.order)

    def add(self, stage: Stage):
        self._stages.append(stage)
        self._stages.sort(key=lambda s: s.order)

    def remove(self, name: str):
        self._stages = [s for s in self._stages if s.name != name]

    @property
    def stage_names(self) -> list[str]:
        return [s.name for s in self._stages]

    async def execute(self, ctx: MessageContext) -> MessageContext:
        for stage in self._stages:
            if not ctx.should_continue:
                break
            try:
                ctx = await stage.process(ctx)
            except Exception as e:
                log.warning("pipeline_stage_error", stage=stage.name, error=str(e))
        return ctx

    @classmethod
    def from_config(cls) -> Pipeline:
        try:
            from core.config.settings import config
            rate = getattr(config, 'pipeline_rate_limit', 30)
            max_len = getattr(config, 'pipeline_max_msg_length', 10000)
            blocked = getattr(config, 'pipeline_blocked_words', [])
        except Exception as _:
            warn("core/routing/pipeline.py:229 Exception", err=repr(_))
            rate, max_len, blocked = 30, 10000, []

        return cls([
            WhitelistStage(),
            RateLimitStage(max_per_minute=rate),
            ContentSafetyStage(blocked_words=blocked, max_length=max_len),
            ClassifyStage(),
        ])
