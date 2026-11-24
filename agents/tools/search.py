from __future__ import annotations
import os
import re

import structlog
from datetime import datetime, timedelta

from core import temporal
from core.rules import engine as rules_engine
from agents.tools.base import BaseTool, ToolResult
from safelog import warn
from core.infra.envflags import text

log = structlog.get_logger()


SIM_FLOOR = float(text("YINYUE_SIM_FLOOR_MESSAGES", "0.30"))  # 向量集合 messages 的相关度下限
# persona_knowledge 与 messages 的分布不同（KB 内容稀、命中更稀疏）：
# scripts/calibrate_sim_floor.py 实测（2026-10-24，20 问 × 5 条）
#   messages: 只有 1/20 问有真命中（top1=0.469），0.30 保留 3 行
#   KB      : 只有 1/20 问有真命中（top1=0.399），0.30~0.39 只差一条同题次级命中
# 0.40 会把 KB 唯一的真命中整条丢掉，所以 KB 默认 0.35 而不是跟随 messages 的 0.30。
SIM_FLOOR_KB = float(text("YINYUE_SIM_FLOOR_KB", "0.35"))  # persona_knowledge 集合


class SearchMessagesTool(BaseTool):
    name = "search_messages"
    description = (
        "搜索历史消息，可按联系人、群聊、时间范围、关键词过滤。"
        "chat_id 可以传 wxid / 群 id，也可以传联系人昵称或备注名，"
        "工具会自动解析成正确的会话 id；查私聊历史时把 chat_type 设为 private。"
        "群聊或普通联系人上下文只能查当前会话；owner 私聊/Web 才能跨会话查询。"
        "可跨平台查询(传platform参数，如wechat/qq)。"
        "默认只搜最近 SEARCH_DEFAULT_HOURS_BACK 小时（默认24）；"
        "问更早的历史必须传 start_time/end_time 或更大的 hours_back；"
        "start_time 优先于 hours_back，只传 end_time 时窗口为 end_time 往前 hours_back。"
    )
    parameters = {
        "chat_id": {"type": "string", "description": "联系人或群聊ID，空字符串表示不限"},
        "chat_type": {"type": "string", "description": "private 或 group", "enum": ["private", "group"]},
        "keyword": {"type": "string", "description": "在消息内容中搜索的关键词"},
        "sender": {"type": "string", "description": "按发送人过滤：wxid/昵称/备注，空表示不限"},
        "hours_back": {
            "type": "integer",
            "description": (
                "往前搜索多少小时；默认读取 SEARCH_DEFAULT_HOURS_BACK（24），"
                "0=不限；传了 start_time 时忽略"
            ),
        },
        "start_time": {
            "type": "string",
            "description": (
                "起始时间，支持 YYYY-MM-DD / YYYY-MM-DD HH:MM / MM-DD HH:MM / "
                "昨天 / 上周三 / 3天前 等"
            ),
        },
        "end_time": {
            "type": "string",
            "description": (
                "结束时间，格式同 start_time；只给日期时含当天 23:59；"
                "不传 start_time 时窗口从 end_time 往前 hours_back 小时"
            ),
        },
        "limit": {"type": "integer", "description": "最多返回多少条，默认20"},
        "platform": {"type": "string", "description": "平台过滤: wechat/qq/telegram，空表示所有平台"},
        "account_id": {"type": "string", "description": "账号ID过滤，空表示自动解析"},
    }

    def __init__(self, repo, unified_query=None):
        self.repo = repo
        self._uq = unified_query

    @staticmethod
    def _default_hours_back() -> int:
        """search_messages 默认时间窗口，可通过 SEARCH_DEFAULT_HOURS_BACK 配置。"""
        try:
            from core.config.settings import config

            value = getattr(config.search, "default_hours_back", 24)
        except Exception:
            warn("agents/tools/search.py:78 Exception", err=repr(__import__("sys").exc_info()[1]))
            return 24
        try:
            return int(value)
        except (TypeError, ValueError):
            warn("agents/tools/search.py:82 (TypeError, ValueError)", err=repr(__import__("sys").exc_info()[1]))
            return 24

    async def execute(self, **kwargs) -> ToolResult:
        chat_id = kwargs.get("chat_id", "")
        chat_type = kwargs.get("chat_type", "")
        keyword = kwargs.get("keyword", "")
        sender = kwargs.get("sender", "") or ""
        hours_raw = kwargs.get("hours_back")
        default_hours = self._default_hours_back()
        if hours_raw is None or hours_raw == "":
            window_hours = default_hours
        else:
            try:
                window_hours = int(hours_raw)
            except (TypeError, ValueError):
                warn("agents/tools/search.py:97 (TypeError, ValueError)", err=repr(__import__("sys").exc_info()[1]))
                window_hours = default_hours
        unlimited_window = window_hours <= 0
        limit = kwargs.get("limit", 20)
        platform = kwargs.get("platform", "") or None
        account_id = kwargs.get("account_id", "") or None
        start_raw = kwargs.get("start_time") or kwargs.get("start") or ""
        end_raw = kwargs.get("end_time") or kwargs.get("end") or ""
        start_dt = temporal.parse_datetime(start_raw)
        if start_dt is not None and ":" not in str(start_raw):
            start_dt = temporal.start_of_day(start_dt)
        end_dt = temporal.parse_datetime(end_raw)
        if end_dt is not None and ":" not in str(end_raw):
            end_dt = temporal.end_of_day(end_dt)

        ctx_platform, ctx_account_id = self._resolve_account()
        scope = self._context_scope()
        # The active AgentContext is authoritative.  A model-supplied
        # platform/account must not be able to pivot to another tenant.
        platform = scope["platform"] or ctx_platform or platform
        account_id = scope["account_id"] or ctx_account_id or account_id
        # 2026-10-24 跨账号扇出（用户批准：检索两件之一）
        # 以前 platform/account_id 一律锁成当前账号，UnifiedQuery 必走「单账号」
        # 分支（db/unified_query.py:43），别的账号库永远查不到 —— 她会自己脑补
        # 「可能是另一个号那边的事」。owner 在私聊/Web 检索时把 account_id 留空，
        # 让 UnifiedQuery 扇出到同一平台的所有账号；群聊保持单账号（群里不该看见
        # 别的会话），非 owner 保持单账号（租户隔离，见上方 why-comment）。
        _scope_sender = str(scope['sender_id'] or '')
        cross_account = bool(
            _scope_sender
            and not scope['is_group']
            and (platform or account_id)
            and self._is_owner(platform or '', account_id or '', _scope_sender)
        )
        _acct_txt = '全部账号' if cross_account else (account_id or '未标注')

        requested_chat_id = str(chat_id or "").strip()
        resolved_chat_id = self._resolve_chat_id(
            requested_chat_id, platform or "", account_id or "",
        )

        # Context-scope guard: group chats may only search the current group;
        # ordinary private contacts may only search the current private chat.
        # Owner private/Web contexts keep cross-chat lookup.  Internal callers
        # without a concrete chat_id (old behavior) are left unchanged.
        scope_chat_id = scope["chat_id"]
        if scope_chat_id:
            if scope["is_group"]:
                if resolved_chat_id and resolved_chat_id != scope_chat_id:
                    return ToolResult(
                        "",
                        error="搜索历史消息仅限当前群聊，已拒绝跨会话查询",
                    )
                chat_id = scope_chat_id
                chat_type = "group"
            elif not self._is_owner(
                scope["platform"] or platform or "",
                scope["account_id"] or account_id or "",
                scope["sender_id"],
            ):
                if resolved_chat_id and resolved_chat_id != scope_chat_id:
                    return ToolResult(
                        "",
                        error="普通联系人只能查询当前私聊会话，已拒绝跨会话查询",
                    )
                chat_id = scope_chat_id
                chat_type = "private"
            else:
                chat_id = resolved_chat_id
        else:
            chat_id = resolved_chat_id

        if start_dt is not None:
            # 显式起点最优先，hours_back 完全忽略。
            since = start_dt
        elif unlimited_window:
            # hours_back <= 0 表示不限时间。
            since = None
        elif end_dt is not None:
            # 只给 end_time：以 end_time 为终点往前回推 window_hours。
            since = end_dt - timedelta(hours=window_hours)
        else:
            since = temporal.now() - timedelta(hours=window_hours)

        # 2026-10-24 用户拍板：跨平台只在「明确问 QQ 那边」时放开。
        # 其它场合 platform 保持当前平台（微信里查不到 QQ 的库，避免串台）。
        _cur_msg_txt = str((getattr(self.ctx, "extra", None) or {}).get("user_message") or "")
        if cross_account and re.search(r"(?i)(qq|扣扣|企鹅号|qq群)", _cur_msg_txt):
            platform = ""
            account_id = ""
            chat_id = ""
            chat_type = ""
            _acct_txt = "全部账号·含 QQ（不限会话）"
            log.info("search_cross_platform", keyword=keyword)

        if self._uq:
            from core.memory.providers import HistoryMemoryProvider

            uq_results = HistoryMemoryProvider(
                unified_query=self._uq
            ).search_raw(
                chat_id=chat_id or "",
                chat_type=chat_type or "",
                sender_id=sender,
                since=since,
                until=end_dt,
                limit=limit,
                platform=platform or "",
                account_id="" if cross_account else (account_id or ""),
                keyword=keyword or "",
            )
            msgs = [r.data for r in uq_results]
            msg_platforms = [r.platform for r in uq_results]
            msg_account_ids = [r.account_id for r in uq_results]
        else:
            msgs = self.repo.messages.get_recent(
                chat_id=chat_id or None,
                chat_type=chat_type or None,
                sender_id=sender,
                since=since,
                until=end_dt,
                limit=limit,
            )
            msg_platforms = [None] * len(msgs)
            msg_account_ids = [None] * len(msgs)

        # 2026-10-24 自指假阳性：刚进来的这条消息本身也在结果集里，关键词一命中
        # 它就变成「找到 1 条消息: 你昨天说的那个记账表还有吗」，比 0 条更糟
        # （她会当成证据）。这里把「本轮当前消息」剔掉。
        _extra = getattr(self.ctx, "extra", None) or {}
        _cur_text = str(_extra.get("user_message") or "").strip()
        _cur_at = temporal.parse_datetime(_extra.get("message_time")) if _extra.get("message_time") else None
        _self_hits = 0

        def _is_current_message(m, text):
            if not _cur_text or text.strip() != _cur_text:
                return False
            if _cur_at is None:
                return True
            try:
                return abs((m.created_at - _cur_at).total_seconds()) <= 180
            except Exception:
                warn("agents/tools/search.py:238 Exception", err=repr(__import__("sys").exc_info()[1]))
                return True

        results = []
        for m in msgs:
            text = m.content or ""
            if isinstance(text, bytes):
                try:
                    text = text.decode("utf-8", errors="replace")
                except Exception:
                    warn("agents/tools/search.py:247 Exception", err=repr(__import__("sys").exc_info()[1]))
                    text = ""
            if keyword and keyword.lower() not in text.lower():
                continue
            if _is_current_message(m, text):
                _self_hits += 1
                continue
            if self._uq:
                chat_name = self._uq.get_contact_display_name(m.chat_id)
            else:
                chat_name = self.repo.contacts.get_display_name(m.chat_id)
            speaker = (m.sender_name or m.sender_id or "").strip()
            if getattr(m, "chat_type", "") == "group" and speaker:
                label = f"{speaker}@{chat_name}"
            else:
                label = speaker or chat_name
            results.append(f"[{temporal.format_stamp(m.created_at)}] {label}: {text[:200]}")

        _self_note = ("·已排除本次提问 %d 条" % _self_hits) if _self_hits else ""

        if not results:
            return ToolResult(f"[search_messages] 已检索·关键词「{keyword}」·时间 {temporal.format_stamp(since)} ~ {temporal.format_stamp(end_dt or temporal.now())}·上限 {limit} 条·账号 {_acct_txt}{_self_note} → 命中 0 条。（这次检索真跑过了，范围内确实没有；别据此说「从来没发生过」。）")

        _scope_line = f"[search_messages] 已检索·关键词「{keyword}」·时间 {temporal.format_stamp(since)} ~ {temporal.format_stamp(end_dt or temporal.now())}·上限 {limit} 条·账号 {_acct_txt}{_self_note} → 命中 {len(results)} 条。"
        main_text = _scope_line + "\n" + f"找到 {len(results)} 条消息:" + "\n".join(results)

        relation_parts = []
        for i, m in enumerate(msgs[:3]):
            if m.quote_reply_id:
                try:
                    if self._uq:
                        chain_results = self._uq.get_reply_chain(
                            m.msg_id, max_depth=3,
                            platform=msg_platforms[i], account_id=msg_account_ids[i],
                        )
                        chain = [r.data for r in chain_results]
                    else:
                        chain = self.repo.messages.get_reply_chain(m.msg_id, max_depth=3)
                    if len(chain) > 1:
                        relation_parts.append("引用链:")
                        for cm in chain:
                            sender = cm.sender_name or cm.sender_id or "?"
                            short = (cm.content or "")[:80]
                            relation_parts.append(f"  [{temporal.format_stamp(cm.created_at)}] {sender}: {short}")
                except Exception:
                    warn("agents/tools/search.py:291 Exception", err=repr(__import__("sys").exc_info()[1]))

        if relation_parts:
            main_text += "\n\n── 关联上下文 ──\n" + "\n".join(relation_parts)

        msg_dicts = []
        for i, m in enumerate(msgs[:limit]):
            if self._uq:
                plat = msg_platforms[i]
                aid = msg_account_ids[i]
                account_repo = self._uq.get_account_repo(plat, aid) if plat and aid else None
                if account_repo:
                    msg_dicts.append(account_repo.messages.to_dict(m))
                else:
                    msg_dicts.append(self.repo.messages.to_dict(m))
            else:
                msg_dicts.append(self.repo.messages.to_dict(m))

        return ToolResult(main_text, data=msg_dicts)

    def _context_scope(self) -> dict:
        """Return the active conversation scope from AgentContext.extra."""
        ctx = self.ctx
        extra = getattr(ctx, "extra", None) or {}
        return {
            "chat_id": str(extra.get("chat_id") or ""),
            "is_group": bool(extra.get("is_group")),
            "sender_id": str(extra.get("sender_id") or ""),
            "platform": str(extra.get("platform") or ""),
            "account_id": str(extra.get("account_id") or ""),
        }

    def _is_owner(
        self, platform: str, account_id: str, sender_id: str,
    ) -> bool:
        """Resolve whether this sender is an owner for the current tenant.

        N4 收编点 ④（G1-13）：判定 owner 是 ``core/rules/engine.py``
        （``rules_engine.is_owner`` 是 ``core.terminal.authority`` 的**唯一**转发点）
        ——本文件不再自己 import 身份服务、也不再自己写一份 ``bool(...)`` 兜底。
        Authority is always active and fails closed.  Model-supplied names and
        legacy config owner-id sets are never consulted here.
        """
        resolved_sender = str(sender_id or "").strip()
        if not resolved_sender:
            return False
        return bool(rules_engine.is_owner(
            self.ctx, platform=platform, account_id=account_id,
            sender_id=resolved_sender,
        ))

    def _resolve_chat_id(
        self, chat_id: str, platform: str, account_id: str,
    ) -> str:
        """Resolve a display name / remark to the platform chat id.

        The agent often says ``search_messages(chat_id="P6")``; the raw
        column stores ``wxid_...``.  Without this resolution the search
        silently returns "没有找到匹配的消息" even though the private history
        exists.
        """
        query = str(chat_id or "").strip()
        if not query:
            return ""
        try:
            if self._uq is not None:
                contacts = self._uq.search_contacts(
                    query,
                    platform=platform or None,
                    account_id=account_id or None,
                    limit=5,
                )
                if contacts:
                    for result in contacts:
                        data = getattr(result, "data", result)
                        wxid = str(getattr(data, "wxid", "") or "")
                        if wxid and wxid == query:
                            return query
                    data = getattr(contacts[0], "data", contacts[0])
                    resolved = str(getattr(data, "wxid", "") or "")
                    if resolved:
                        log.info(
                            "search_messages_chat_id_resolved",
                            query=query, resolved=resolved,
                        )
                        return resolved
                return query

            if self.repo is not None:
                contact = self.repo.contacts.get_by_wxid(query)
                if contact is not None:
                    return query
                matches = self.repo.contacts.search_by_name(query, limit=5)
                if matches:
                    resolved = str(getattr(matches[0], "wxid", "") or "")
                    if resolved:
                        log.info(
                            "search_messages_chat_id_resolved",
                            query=query, resolved=resolved,
                        )
                        return resolved
        except Exception as exc:
            log.warning("search_messages_chat_id_resolve_failed",
                        query=query, error=str(exc)[:120])
        return query


class SearchVectorTool(BaseTool):
    name = "search_vector"
    description = "语义搜索：用自然语言描述你想找的内容，返回语义最相关的历史消息"
    parameters = {
        "query": {"type": "string", "description": "用自然语言描述你想搜索的内容", "required": True},
        "top_k": {"type": "integer", "description": "返回最相关的K条结果，默认5"},
        "collection": {"type": "string", "description": "搜索哪个集合: messages/summaries", "enum": ["messages", "summaries"]},
    }

    def __init__(self, vector_store, embedding_client):
        self.vector_store = vector_store
        self.embedding_client = embedding_client

    async def execute(self, **kwargs) -> ToolResult:
        query = kwargs.get("query", "")
        top_k = kwargs.get("top_k", 5)
        collection = kwargs.get("collection", "messages")
        if collection == "memories":
            # Phase 5 E2: personal memories must go through MemoryGateway.
            return ToolResult("", error="个人记忆请使用 recall_memories / search_knowledge")

        if not query:
            return ToolResult("", error="query 不能为空")

        embedding = await self.embedding_client.embed(query)
        if embedding == [0.0] * len(embedding):
            return ToolResult("", error="embedding 生成失败")

        results = self.vector_store.search(embedding, top_k=top_k, collection=collection)
        _best = max([max(0.0, 1.0 - float(_x.get('distance', 0) or 0)) for _x in results], default=0.0)
        results = [_r for _r in results if 1.0 - float(_r.get('distance', 0) or 0) >= SIM_FLOOR]

        if not results:
            return ToolResult(f"[search_vector] 已检索·集合 {collection}·query「{query}」·top_k={top_k} → 0 条命中（最高相似度 {_best:.2f} < 阈值 {SIM_FLOOR:.2f}，判为不相关；这次真跑过了，别据此说「从来没发生过」。）")

        lines = []
        for r in results:
            dist = r.get("distance", 0)
            doc = r.get("document", "")[:200]
            sim = max(0.0, 1.0 - dist)
            lines.append(f"[相似度={sim:.2f}] {doc}")

        return ToolResult(
            "语义搜索结果:\n" + "\n".join(lines),
            data=results,
        )


class SearchKnowledgeTool(BaseTool):
    name = "search_knowledge"
    description = "搜索本地知识库：在已上传的文档、资料中查找信息。不联网。需要联网搜索实时信息时请用 web_search。当用户明确要求结合记忆查资料（如「根据以往记忆」）时，设 include_memory=true，此时记忆为主、知识库补充。"
    parameters = {
        "query": {"type": "string", "description": "搜索内容", "required": True},
        "top_k": {"type": "integer", "description": "返回最相关的K条结果，默认5"},
        "include_memory": {"type": "boolean", "description": "是否联合搜索个人记忆。仅当用户明确要求结合记忆（如「根据以往记忆」「结合记忆查一下」）时设为true，默认false"},
    }

    def __init__(self, vector_store, embedding_client):
        self.vector_store = vector_store
        self.embedding_client = embedding_client

    async def execute(self, **kwargs) -> ToolResult:
        query = kwargs.get("query", "")
        top_k = kwargs.get("top_k", 5)
        include_memory = kwargs.get("include_memory", False)

        if not query:
            return ToolResult("", error="query 不能为空")

        embedding = await self.embedding_client.embed(query)
        if not any(embedding):
            return ToolResult("", error="embedding 生成失败")

        knowledge_lines = []
        _best_k = 0.0
        memory_lines = []
        all_results = []

        if include_memory:
            # Phase 5 D2: automatic memory reads are event-sourced only.  A
            # missing principal returns no memory rows; the old vector store is
            # never queried for "memories".
            try:
                from core.execution.data_channels import recall_items_for_context

                hits = recall_items_for_context(
                    self.ctx, query, kinds=["personal"], limit=top_k
                )
                all_results.extend(hit.as_dict() for hit in hits)
                for hit in hits:
                    speaker = f"{hit.speaker}: " if hit.speaker else ""
                    memory_lines.append(
                        f"[记忆|来源=event, 相关度={float(getattr(hit, 'importance', 0.0) or 0.0):.2f}]\n"
                        f"{speaker}{hit.content[:300]}"
                    )
            except Exception as e:
                log.warning("knowledge_gateway_search_failed", error=str(e)[:100])

            try:
                kb_results = self.vector_store.search(embedding, top_k=min(top_k, 3), collection="persona_knowledge")
                _best_k = max(_best_k, max([max(0.0, 1.0 - float(_x.get('distance', 0) or 0)) for _x in kb_results], default=0.0))
                kb_results = [_r for _r in kb_results if 1.0 - float(_r.get('distance', 0) or 0) >= SIM_FLOOR_KB]
                all_results.extend(kb_results)
                for r in kb_results:
                    dist = r.get("distance", 0)
                    doc = r.get("document", "")[:200]
                    meta = r.get("metadata", {})
                    source = meta.get("source_file", "未知来源")
                    sim = max(0.0, 1.0 - dist)
                    knowledge_lines.append(f"[来源: {source}, 相关度={sim:.2f}]\n{doc}")
            except Exception as e:
                log.warning("knowledge_kb_search_failed", error=str(e)[:100])
        else:
            results = self.vector_store.search(embedding, top_k=top_k, collection="persona_knowledge")
            _best_k = max(_best_k, max([max(0.0, 1.0 - float(_x.get('distance', 0) or 0)) for _x in results], default=0.0))
            results = [_r for _r in results if 1.0 - float(_r.get('distance', 0) or 0) >= SIM_FLOOR_KB]
            all_results.extend(results)
            for r in results:
                dist = r.get("distance", 0)
                doc = r.get("document", "")[:400]
                meta = r.get("metadata", {})
                source = meta.get("source_file", "未知来源")
                sim = max(0.0, 1.0 - dist)
                knowledge_lines.append(f"[来源: {source}, 相关度={sim:.2f}]\n{doc}")

        if not knowledge_lines and not memory_lines:
            return ToolResult(f"[search_knowledge] 已检索·query「{query}」·top_k={top_k}·include_memory={include_memory} → 知识库与个人记忆都 0 条命中（最高相似度 {_best_k:.2f} < 阈值 {SIM_FLOOR_KB:.2f}）。（这次真跑过了；说「知识库里没有」可以，别说「从来没有」。）")

        parts = []
        if memory_lines:
            parts.append("个人记忆搜索结果（主要）:\n" + "\n---\n".join(memory_lines))
        if knowledge_lines:
            label = "知识库补充结果" if memory_lines else "知识库搜索结果"
            parts.append(f"{label}:\n" + "\n---\n".join(knowledge_lines))

        return ToolResult(
            "\n\n".join(parts),
            data=all_results,
        )


class SearchThreadsTool(BaseTool):
    name = "search_threads"
    description = ("搜索群聊中的话题线程。当你需要了解某个群在讨论什么、某个项目/事件的讨论进展时调用。"
                   "返回匹配的线程摘要(scope)和最近消息预览，可用recall_thread获取完整内容。")
    parameters = {
        "query": {"type": "string", "description": "想找什么话题，如'项目A进度'、'明天的会议'、'午餐'", "required": True},
        "chat_id": {"type": "string", "description": "限定在哪个群聊中搜索，空字符串表示所有群"},
        "limit": {"type": "integer", "description": "最多返回多少个线程，默认5"},
    }

    def __init__(self, repo, embedding_client=None, vector_store=None, unified_query=None):
        self.repo = repo
        self.embedding_client = embedding_client
        self.vector_store = vector_store
        self._uq = unified_query

    async def execute(self, **kwargs) -> ToolResult:
        query = kwargs.get("query", "")
        chat_id = kwargs.get("chat_id", "")
        limit = kwargs.get("limit", 5)

        if not query:
            return ToolResult("", error="query 不能为空")

        requested_chat = chat_id
        try:
            chat_id = self._resolve_chat_id(chat_id)
        except ValueError as exc:
            return ToolResult("", error=str(exc))
        if requested_chat and not chat_id:
            return ToolResult("", error="contact_not_found: 指定会话无法在当前账号内确定")

        from core.memory.providers import TopicMemoryProvider

        provider = TopicMemoryProvider(
            meta_repo=getattr(self.repo, "meta", None) or self.repo,
            unified_query=self._uq,
        )
        candidate_threads = []
        if chat_id:
            candidate_threads.extend(
                provider.find_threads(self.principal, chat_id=chat_id, limit=40)
            )
        else:
            all_threads = provider.find_threads(
                self.principal, limit=200
            )
            chats = sorted({t.chat_id for t in all_threads if t.chat_id})
            for cid in chats:
                candidate_threads.extend(
                    provider.find_threads(self.principal, chat_id=cid, limit=20)
                )

        if not candidate_threads:
            _scope_txt = "指定会话" if chat_id else "全部会话"
            return ToolResult(f"[search_threads] 已检索·query「{query}」·范围 {_scope_txt}·limit={limit} → 0 条活跃线程。（这次真跑过了，别据此说「从来没聊过」。）")

        scored = []
        query_lower = query.lower()
        for t in candidate_threads:
            score = 0.0
            scope_lower = (t.scope or "").lower()
            if query_lower in scope_lower:
                score += 3.0
            for kw in query_lower.split():
                if kw in scope_lower:
                    score += 1.0
            if t.last_active_at:
                hours_ago = (datetime.now() - t.last_active_at).total_seconds() / 3600
                if hours_ago < 1:
                    score += 1.0
                elif hours_ago < 6:
                    score += 0.5
            if score > 0:
                scored.append((t, score))

        if self.embedding_client and self.vector_store:
            try:
                q_emb = await self.embedding_client.embed(query)
                if q_emb and any(q_emb):
                    for t, s in scored:
                        thread_emb = self._get_thread_embedding(t)
                        if thread_emb:
                            sim = self._cosine_sim(q_emb, thread_emb)
                            scored[scored.index((t, s))] = (t, s + sim * 2.0)
            except Exception:
                warn("agents/tools/search.py:619 Exception", err=repr(__import__("sys").exc_info()[1]))

        scored.sort(key=lambda x: x[1], reverse=True)
        top = scored[:limit]

        if not top:
            return self._format_grouped_summary(candidate_threads[:limit * 2], "没有精确匹配的话题，以下是各群最近活跃的讨论:")

        if not chat_id and len(chats) > 1:
            return self._format_grouped_summary(top, f"找到 {len(top)} 个相关话题:")

        lines = [f"找到 {len(top)} 个相关话题:"]
        for t, score in top:
            name = self._chat_display(t.chat_id)
            preview = self._thread_preview(t)
            lines.append(f"  [{t.id}] (相关度={score:.1f}) {name}「{t.scope}」{preview}")

        return ToolResult("\n".join(lines), data=[{"thread_id": t.id, "scope": t.scope, "score": s} for t, s in top])

    def _format_grouped_summary(self, threads_or_scored, header: str) -> ToolResult:
        by_group: dict[str, list] = {}
        for item in threads_or_scored:
            if isinstance(item, tuple):
                t, s = item
            else:
                t, s = item, 0
            name = self._chat_display(t.chat_id)
            by_group.setdefault(name, []).append((t, s))

        lines = [header]
        for group_name, items in by_group.items():
            lines.append(f"\n📌 {group_name}")
            for t, s in items[:3]:
                preview = self._thread_preview(t)
                score_tag = f"(相关度={s:.1f}) " if s > 0 else ""
                lines.append(f"  [{t.id}] {score_tag}「{t.scope}」{preview}")

        data = []
        for items in by_group.values():
            for t, s in items:
                data.append({"thread_id": t.id, "scope": t.scope, "score": s})
        return ToolResult("\n".join(lines), data=data)

    def _resolve_chat_id(self, chat_id_or_name: str) -> str:
        if not chat_id_or_name:
            return ""
        from db.models import is_group_chat_id
        from core.memory.contact_scope import ScopedContacts
        if is_group_chat_id(chat_id_or_name):
            return chat_id_or_name
        contact = ScopedContacts(self._uq, self.principal).resolve(chat_id_or_name, group_only=True)
        return str(contact.wxid) if contact is not None else ""

    def _chat_display(self, chat_id: str) -> str:
        from core.memory.contact_scope import ScopedContacts
        return ScopedContacts(self._uq, self.principal).display(chat_id)

    def _thread_preview(self, thread) -> str:
        from core.memory.providers import TopicMemoryProvider
        provider = TopicMemoryProvider(meta_repo=getattr(self.repo, "meta", None) or self.repo, unified_query=self._uq)
        refs = provider.get_thread_refs(thread.id, principal=self.principal, status="confirmed", limit=2)
        parts = []
        for ref in refs:
            message = provider.resolve_ref(ref, self.principal)
            if message is not None and message.content:
                parts.append("[%s] %s" % (temporal.format_stamp(message.created_at), str(message.content)[:80]))
        return " | ".join(parts)

    def _get_thread_embedding(self, thread):
        try:
            results = self.vector_store.search_by_id(f"thread_{thread.id}", collection="threads")
            if results:
                return results[0].get("embedding")
        except Exception:
            warn("agents/tools/search.py:728 Exception", err=repr(__import__("sys").exc_info()[1]))
        return None

    @staticmethod
    def _cosine_sim(a, b):
        dot = sum(x * y for x, y in zip(a, b))
        na = sum(x * x for x in a) ** 0.5
        nb = sum(x * x for x in b) ** 0.5
        if na < 1e-9 or nb < 1e-9:
            return 0.0
        return dot / (na * nb)
