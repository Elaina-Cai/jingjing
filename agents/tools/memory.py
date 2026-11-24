from __future__ import annotations

import asyncio

import structlog

from core import temporal
from agents.tools.base import BaseTool, ToolResult
from safelog import warn

log = structlog.get_logger()


def _explicit_visibility(principal):
    """Visibility for one explicit remember event, derived from identity."""
    try:
        from core.terminal.principal import VisibilityScope

        if principal.is_owner:
            return frozenset({VisibilityScope.OWNER_ONLY})
        if principal.is_group:
            return frozenset({VisibilityScope.GROUP_MEMBERS})
    except Exception:
        warn("agents/tools/memory.py:74 Exception", err=repr(__import__("sys").exc_info()[1]))
    try:
        from core.terminal.principal import VisibilityScope

        return frozenset({VisibilityScope.ISSUER_ONLY})
    except Exception:
        warn("agents/tools/memory.py:80 Exception", err=repr(__import__("sys").exc_info()[1]))
        return frozenset()


class RememberTool(BaseTool):
    name = "remember"
    description = "记住一条信息，存入长期记忆。用于保存重要发现、用户偏好、待办事项等"
    parameters = {
        "content": {"type": "string", "description": "要记住的内容", "required": True},
        "memory_type": {"type": "string", "description": "记忆类型: episodic(事件)/semantic(知识)/procedural(习惯)", "enum": ["episodic", "semantic", "procedural"]},
        "importance": {"type": "number", "description": "重要程度 0-1，默认0.5"},
        "why": {
            "type": "string",
            "description": "为什么值得记（来源/依据一句话）；可以不填，缺了不拦",
        },
    }

    def __init__(self, repo, vector_store, embedding_client, meta_repo=None):
        self.repo = repo
        self.vector_store = vector_store
        self.embedding_client = embedding_client
        self._meta_repo = meta_repo

    async def execute(self, **kwargs) -> ToolResult:
        content = kwargs.get("content", "")
        memory_type = kwargs.get("memory_type", "episodic")
        importance = kwargs.get("importance", 0.5)
        # 2026-10-23：记一条记忆时，"为什么值得记"是复查时唯一的线索。
        # **不要求**（缺了照样写），只是别再让它无处可写。
        why = " ".join(str(kwargs.get("why") or "").split())[:200]

        if not content:
            return ToolResult("", error="content 不能为空")

        # N4 收编点 ③（G1-13）：身份与"谁有资格写长期记忆"的判定只有一处 owner
        # （``core/rules/engine.py``）——本工具不再自己判 ``principal.is_owner``。
        from core.rules import engine as rules_engine

        principal = self.principal
        if principal is None and self.ctx is not None:
            principal = rules_engine.authority(context=self.ctx)
        if principal is None or not getattr(principal, "person_id", ""):
            return ToolResult(
                "",
                error="无法确定当前身份，未写入长期记忆",
            )
        from core.memory.explicit_write import remember_denial
        denied = remember_denial(principal, context=self.ctx, memory_type=memory_type)
        if denied:
            return ToolResult("", error=denied)

        try:
            importance_value = max(0.0, min(1.0, float(importance)))
        except (TypeError, ValueError):
            warn("agents/tools/memory.py:156 (TypeError, ValueError)", err=repr(__import__("sys").exc_info()[1]))
            importance_value = 0.5

        from core.terminal.event_model import Event, EventType
        from core.terminal.event_runtime import default_event_store

        chat_type = getattr(principal, "chat_type", "")
        chat_type = getattr(chat_type, "value", chat_type) or "private"
        event = Event.new(
            event_type=EventType.MEMORY_WRITE,
            platform=str(getattr(principal, "platform", "") or ""),
            bot_account_id=str(getattr(principal, "bot_account_id", "") or ""),
            chat_id=str(getattr(principal, "chat_id", "") or ""),
            chat_type=str(chat_type),
            actor_person_id=str(getattr(principal, "person_id", "") or ""),
            actor_account_id=str(
                ((getattr(self.ctx, "extra", {}) or {}).get("sender_id") or "")
            ),
            content=str(content),
            visibility_scope=_explicit_visibility(principal),
            metadata={
                "memory_type": str(memory_type or "semantic"),
                "importance": importance_value,
                "source": "tool_remember",
                "why": why,
                "explicit_remember": True,
                "principal": principal.as_dict(),
                "msg_id": "",
            },
        )

        def _write_event_and_project() -> str:
            store = default_event_store()
            if store is None:
                raise RuntimeError("terminal event store unavailable")
            from core.terminal.store_port import get_meta_repo
            from db.repo_memory_projections import MemoryProjectionRepository

            from core.memory.explicit_write import commit_explicit
            repository = MemoryProjectionRepository(get_meta_repo().engine)
            commit_explicit(event, store=store, repository=repository)
            return event.event_id

        try:
            event_id = await asyncio.to_thread(_write_event_and_project)
        except Exception as exc:
            log.warning("remember_event_write_failed", error=str(exc)[:180])
            return ToolResult("", error=f"长期记忆写入失败: {str(exc)[:120]}")

        # Phase 5 E 收口：显式 remember 只写 MEMORY_WRITE 事件 + 投影，
        # 旧 memories 表/旧向量库的兼容写已删除（旧 store 归档策略：删除）。
        return ToolResult(
            f"已记住: {content[:80]} "
            f"(类型={memory_type}, 重要度={importance_value}, event_id={event_id})"
        )


def _filter_by_date(hits, since: str = "", before: str = "") -> list:
    """按时间范围过滤 hits（翻旧账用）。坏日期/无时间的条目原样保留。"""

    def _parse(text: str, end: bool) -> "datetime | None":
        raw = str(text or "").strip()
        if not raw:
            return None
        try:
            from datetime import datetime

            value = datetime.fromisoformat(raw)
        except ValueError:
            warn("agents/tools/memory.py:231 ValueError", err=repr(__import__("sys").exc_info()[1]))
            return None
        if len(raw) == 10:  # 只给了日期 → 补足整天
            value = value.replace(
                hour=23 if end else 0, minute=59 if end else 0, second=59 if end else 0
            )
        return value

    from datetime import datetime

    low, high = _parse(since, False), _parse(before, True)
    if low is None and high is None:
        return list(hits)
    out = []
    for hit in hits:
        when = getattr(hit, "occurred_at", None)
        if when is None and isinstance(hit, dict):
            when = hit.get("occurred_at")
        if isinstance(when, str):
            try:
                when = datetime.fromisoformat(when)
            except ValueError:
                warn("agents/tools/memory.py:252 ValueError", err=repr(__import__("sys").exc_info()[1]))
                when = None
        if when is None:
            out.append(hit)  # 没时间的不过滤
            continue
        if getattr(when, "tzinfo", None) is not None:
            when = temporal.to_naive_local(when)
        if low is not None and when < low:
            continue
        if high is not None and when > high:
            continue
        out.append(hit)
    return out


class RecallMemoriesTool(BaseTool):
    name = "recall_memories"
    description = (
        "翻旧账：回放过去的对话与记忆（谁在什么时候说过什么）。"
        "当对方说「还记得…吗」「上次」「那天」「以前」「我们说好」时调用；"
        "也可以用 since/before 限定时间范围（如「上周」「三个月前」）。"
        "知识性问题用 web_search，查本地文档用 search_knowledge。"
    )
    parameters = {
        "query": {"type": "string", "description": "想回忆什么", "required": True},
        "top_k": {"type": "integer", "description": "返回多少条记忆，默认5"},
        "since": {"type": "string", "description": "只看这个日期之后的（YYYY-MM-DD，可选）"},
        "before": {"type": "string", "description": "只看这个日期之前的（YYYY-MM-DD，可选）"},
    }

    def __init__(self, repo, vector_store, embedding_client):
        self.repo = repo
        self.vector_store = vector_store
        self.embedding_client = embedding_client

    async def execute(self, **kwargs) -> ToolResult:
        query = str(kwargs.get("query") or "")
        try:
            top_k = max(1, int(kwargs.get("top_k", 5) or 5))
        except (TypeError, ValueError):
            warn("agents/tools/memory.py:291 (TypeError, ValueError)", err=repr(__import__("sys").exc_info()[1]))
            top_k = 5

        # Phase 5 D2: automatic recall is event-sourced only.  A missing
        # principal returns empty; the old vector store is never consulted.
        from core.execution.data_channels import recall_items_for_context

        hits = recall_items_for_context(
            self.ctx, query, kinds=["personal"], limit=max(top_k * 3, top_k)
        )
        hits = _filter_by_date(
            hits, str(kwargs.get("since") or ""), str(kwargs.get("before") or "")
        )[:top_k]
        if not hits:
            return ToolResult("没有找到相关记忆")
        from core.execution.data_channels import render_hits
        return ToolResult(render_hits(hits, header="相关记忆:"), data=[hit.as_dict() for hit in hits])



_NOTES_KIND = "notes"


def _notes_channel_visible() -> bool:
    """与 notes 开关同门：默认 off 时随手记通道对模型不可见。"""
    try:
        from core.memory.note_writer import worth_remembering_mode
    except Exception:  # noqa: BLE001 - 读不到开关按关闭处理
        warn("agents/tools/memory.py:325 Exception", err=repr(__import__("sys").exc_info()[1]))
        return False
    try:
        return worth_remembering_mode() == "on"
    except Exception:  # noqa: BLE001
        warn("agents/tools/memory.py:329 Exception", err=repr(__import__("sys").exc_info()[1]))
        return False


def _notes_scope_line(hits: list, context: object | None = None) -> str:
    """随手记返回时的范围说明：本群/本私聊 + 当前说话人，不跨会话不跨人。"""
    if not any(str(getattr(hit, "kind", "")) == _NOTES_KIND for hit in hits):
        return ""
    is_group = bool(getattr(context, "is_group", False))
    where = "本群" if is_group else "本私聊"
    sender = str(getattr(context, "sender_name", "") or "")
    who = f"当前说话人「{sender}」" if sender else "当前说话人"
    return (
        f"（随手记范围：{where}·{who} 的原话摘录；不跨会话、不跨人，"
        "别把别处的话当成这里说过。）"
    )


class MemoryRecallTool(BaseTool):
    name = "memory_recall"
    description = (
        "按需记忆：只读一次的召回。当当前窗口里没有你需要的"
        "事（事实 / 旧事 / 人 / 梗 / 原话）时调用；一轮最多一次，"
        "没查到就当没这回事，不要向对方复述你在查。"
    )
    parameters = {
        "query": {"type": "string", "description": "短检索句（你缺什么就写什么）", "required": True},
        "kinds": {
            "type": "string",
            "description": (
                "可选，逗号分隔：fact,episode,person,meme,quote；"
                "拿不准留空= 全查"
            ),
        },
        "top_k": {"type": "integer", "description": "返回多少条，默认 5"},
        "valid_at": {"type": "string", "description": "事实在哪个时间有效（本地 YYYY-MM-DD 或 YYYY-MM-DD HH:MM:SS；只写日期指当天00:00）。提供时只查 Belief 历史事实，kinds 只能为 fact 或留空；不是记录写入时间。"},
    }

    MAX_KINDS = 5

    def __init__(self):
        self.calls = 0
        if _notes_channel_visible():
            self.description = self.description + (
                "本群/本私聊里近期的原话碎片（随手记）也在可查范围内。"
            )
            kinds = dict(self.parameters["kinds"])
            kinds["description"] = (
                "可选，逗号分隔：fact,episode,person,meme,quote,notes；"
                "拿不准留空= 全查"
            )
            self.parameters = {**self.parameters, "kinds": kinds}

    def _principal(self):
        """身份解析走 ⑩ 通道：工具本身不碰身份判定（收编纪律）。

        已 attach 的身份直接用（``self.principal`` 转发引擎）；没挂就用本回合
        坐标现算，但**绝不 attach**（保 ? 的 shadow 零 diff）。
        """
        context = getattr(self, "ctx", None)
        try:
            principal = self.principal
        except Exception:  # noqa: BLE001 - 没挂身份按 None 处理
            warn("agents/tools/memory.py:390 Exception", err=repr(__import__("sys").exc_info()[1]))
            principal = None
        if principal is not None:
            return principal
        from core.execution.data_channels import principal_for_context

        platform, account_id = self._resolve_account()
        return principal_for_context(
            context,
            platform=str(platform or ""),
            account_id=str(account_id or ""),
            chat_id=str(getattr(context, "chat_id", "") or ""),
            chat_type="group" if getattr(context, "is_group", False) else "private",
            sender_id=str(getattr(context, "sender_id", "") or ""),
            sender_name=str(getattr(context, "sender_name", "") or ""),
        )

    async def execute(self, **kwargs) -> ToolResult:
        query = str(kwargs.get("query") or "").strip()
        if not query:
            return ToolResult("", error="query 不能为空")
        try:
            top_k = max(1, int(kwargs.get("top_k", 5) or 5))
        except (TypeError, ValueError):
            warn("agents/tools/memory.py:413 (TypeError, ValueError)", err=repr(__import__("sys").exc_info()[1]))
            top_k = 5
        requested_kinds = [
            k.strip()
            for k in str(kwargs.get("kinds") or "").replace(";", ",").split(",")
            if k.strip()
        ]
        kinds = requested_kinds[: self.MAX_KINDS]
        if not _notes_channel_visible():
            kinds = [kind for kind in kinds if kind != _NOTES_KIND]

        valid_at = str(kwargs.get("valid_at") or "").strip()
        if valid_at and any(kind not in {"fact", "belief"} for kind in requested_kinds):
            return ToolResult("", error="valid_at 只支持 Belief 事实，请将 kinds 留空或设为 fact；其他记忆尚无有效区间。")
        from core.memory.beliefs import query_context
        try:
            read_context = query_context(getattr(self, "ctx", None), valid_at)
        except (ValueError, TypeError):
            return ToolResult("", error="valid_at 必须是本地有效日期或日期时间，不能使用时区偏移或无效日期。")
        if valid_at:
            kinds = ["belief"]
        principal = self._principal()
        if principal is None:
            return ToolResult("没有找到相关记忆")
        self.calls += 1

        from core.execution.data_channels import recall_hub_items, render_hits

        detail: dict = {}
        hits = recall_hub_items(
            principal,
            query,
            kinds=kinds or None,
            limit=top_k,
            context=read_context,
            detail=detail,
        )
        hits = list(hits)[:top_k]
        if not hits:
            return ToolResult("该有效时间未找到可见的 Belief 事实；未查询其他旧记忆库。" if valid_at else "没有找到相关记忆")
        body = render_hits(hits, header="[按需记忆]") or "没有找到相关记忆"
        if valid_at:
            body = f"[事实有效时间：{valid_at}；仅查世界模型 Belief，下方日期为记录时间]\n{body}"
        scope = _notes_scope_line(hits, getattr(self, "ctx", None))
        if scope:
            body = f"{body}\n{scope}"
        return ToolResult(body, data=[h.as_dict() for h in hits])


class RecallThreadTool(BaseTool):
    name = "recall_thread"
    description = ("召回某个话题线程的完整消息内容。当你需要了解某个话题的具体讨论细节时调用。"
                   "先用search_threads找到线程ID，再用这个工具获取完整内容。")
    parameters = {
        "thread_id": {"type": "integer", "description": "话题线程ID（从search_threads获得）", "required": True},
        "limit": {"type": "integer", "description": "返回最近多少条消息，默认10"},
    }

    def __init__(self, repo, unified_query=None):
        self.repo = repo
        self._uq = unified_query

    async def execute(self, **kwargs) -> ToolResult:
        thread_id = kwargs.get("thread_id", 0)
        limit = kwargs.get("limit", 10)

        if not thread_id:
            return ToolResult("", error="thread_id 不能为空")

        from core.memory.providers import TopicMemoryProvider

        provider = TopicMemoryProvider(
            meta_repo=getattr(self.repo, "meta", None) or self.repo,
            unified_query=self._uq,
        )
        thread = provider.get_thread(int(thread_id), principal=self.principal)
        if not thread:
            return ToolResult("", error=f"找不到线程 {thread_id}")

        refs = provider.get_thread_refs(
            int(thread_id), principal=self.principal, status="confirmed", limit=limit
        )
        if not refs:
            refs = provider.get_thread_refs(int(thread_id), principal=self.principal, limit=limit)
        if not refs:
            return ToolResult(f"线程「{thread.scope}」没有消息")

        chat_name = self._chat_display(thread.chat_id)
        lines = [f"话题: 「{thread.scope}」 (群: {chat_name}, 共{len(refs)}条消息)"]
        lines.append("─" * 40)

        for ref in reversed(refs):
            msg = provider.resolve_ref(ref, self.principal)
            if not msg:
                continue
            text = msg.content or ""
            if isinstance(text, bytes):
                try:
                    text = text.decode("utf-8", errors="replace")
                except Exception:
                    warn("agents/tools/memory.py:504 Exception", err=repr(__import__("sys").exc_info()[1]))
                    text = "<二进制>"
            sender = (getattr(msg, "sender_name", "") or getattr(msg, "sender_id", "") or "").strip()
            if not sender:
                sender = "?"
            time_str = temporal.format_stamp(msg.created_at) if msg.created_at else ""
            lines.append(f"[{time_str}] {sender}: {text[:150]}")

        return ToolResult("\n".join(lines), data={
            "thread_id": thread_id,
            "scope": thread.scope,
            "chat_id": thread.chat_id,
            "msg_count": len(refs),
        })

    def _chat_display(self, chat_id: str) -> str:
        from core.memory.contact_scope import ScopedContacts
        return ScopedContacts(self._uq, self.principal).display(chat_id)
