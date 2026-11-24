from __future__ import annotations

import asyncio
import json
import structlog
from abc import ABC, abstractmethod
from datetime import datetime, timedelta
from typing import Any
from safelog import warn
from core.infra.envflags import text

log = structlog.get_logger()


class ToolResult:
    def __init__(self, output: str, data: Any = None, error: str = ""):
        self.output = output
        self.data = data
        self.error = error

    @property
    def is_error(self) -> bool:
        return bool(self.error)

    def __repr__(self):
        if self.error:
            return f"ToolResult(error={self.error})"
        return f"ToolResult(output={self.output[:80]}...)"


#: 单个工具 handler 的整体硬上限（秒）。只做兜底，不替代工具内部的 httpx timeout。
#:
#: 数据依据（2026-10-10 生产 action_log：data/platforms/*/*/account.db，n=417 次有耗时记录）：
#:   * 成功执行 342 次：P50=7ms、P95=11.9s、P99=23.7s、最长 38.85s（send_sticker 的
#:     UI 自动化长尾；web_search 最长 25.04s，本地/DB 类工具均在 1.7s 内）；
#:   * 全部耗时最长 49.70s，且 >38.85s 的唯一样本正是那次失败挂起（49.70s，
#:     表情面板未关闭）——成功上限与失败长尾之间存在空白带；
#:   * 取 45s：覆盖全部 342 次实测成功（对成功上限留约 16% 余量），且落在空白带内，
#:     零误杀、能截住实测到的最坏挂起。
#:
#: 这是硬兜底不是性能调优：45s 依然很长，数据积累（duration_ms 一直在记）后应重校；
#: 改这个值必须同步更新 tests/test_tool_execution_timeout.py 里的证据常量。
TOOL_EXECUTION_TIMEOUT_S: float = 600.0  # 2026-10-31：主人要求取消上限（原 45.0）


async def execute_tool_with_timeout(tool, timeout_s: float | None = None, **kwargs) -> ToolResult:
    """执行 ``tool.execute(**kwargs)``，但给**整个 handler** 加硬上限。

    与工具内部的 httpx timeout 不同：这里兜的是 DB 锁 / 文件 IO / 未知死循环——
    任何一直没有 ``await`` 返回的执行都会被截断。

    超时**转成失败 ``ToolResult``**（而不是向上抛 TimeoutError）：
    * 调用方拿到的是一条可回给模型的失败回执，回合继续而不是整个挂住；
    * ``act`` 的 action_log 仍按 failed 记录，并带有被截断后的真实耗时。
    """
    limit = TOOL_EXECUTION_TIMEOUT_S if timeout_s is None else float(timeout_s)
    tool_name = str(getattr(tool, "name", "") or type(tool).__name__)
    try:
        return await asyncio.wait_for(tool.execute(**kwargs), timeout=limit)
    except asyncio.TimeoutError:
        log.warning("tool_execution_timeout", tool=tool_name, timeout_s=limit)
        return ToolResult(
            "",
            error=f"工具执行超时: {tool_name} 超过 {limit:g}s（handler 未在期限内返回）",
        )


class BaseTool(ABC):
    name: str = ""
    description: str = ""
    parameters: dict = {}

    def set_context(self, ctx) -> None:
        from contextvars import ContextVar
        if not hasattr(self, "_context_var"):
            self._context_var = ContextVar("tool_context_" + str(id(self)), default=None)
        self._context_var.set(ctx)

    @property
    def ctx(self) -> Any:
        variable = getattr(self, "_context_var", None)
        return variable.get() if variable is not None else getattr(self, "_ctx", None)

    # ------------------------------------------------------------- authority
    @property
    def principal(self) -> Any:
        """The deterministic PrincipalContext attached to this turn, if any."""
        try:
            from core.terminal.authority import context_principal

            return context_principal(self.ctx)
        except Exception:
            warn("agents/tools/base.py:86 Exception", err=repr(__import__("sys").exc_info()[1]))
            return None

    def authoritative_is_owner(self) -> bool:
        """Owner decision from the **唯一**判定点（N4 规则引擎的身份维）。

        Authority is always active and fails closed: a missing/unresolvable
        principal is UNKNOWN, so this returns ``False`` rather than ``None``.

        2026-10-06（G1-13 收编）：本方法只**转发**到
        ``core.rules.engine.is_owner``，自己不再 import 身份服务、也不再写一份
        ``bool(...)`` 兜底——12 处动作前置校验点以前各写一份的 owner 判定，
        现在只有引擎那一处。它保留成**工具实例方法**是有意的：既有用例
        （``tests/test_reminder_delivery.py`` 等 15 处）在实例上替换它来构造
        "主人/非主人"两种世界，换成模块级函数会把那个测试接缝一起删掉。
        """
        try:
            from core.rules import engine as rules_engine

            return bool(rules_engine.is_owner(self.ctx))
        except Exception:
            warn("agents/tools/base.py:106 Exception", err=repr(__import__("sys").exc_info()[1]))
            return False

    def authoritative_has_capability(self, capability) -> bool:
        """Capability decision from the deterministic identity service.

        Authority is always active and fails closed: resolution failure means
        the capability is denied, never delegated to a legacy fallback.
        """
        try:
            from core.rules import engine as rules_engine

            return bool(rules_engine.has_capability(self.ctx, capability))
        except Exception:
            warn("agents/tools/base.py:119 Exception", err=repr(__import__("sys").exc_info()[1]))
            return False

    def _resolve_account(self) -> tuple[str | None, str | None]:
        # Prefer the explicit runtime AccountContext, then fall back to the
        # legacy extra dict used by older callers.
        account_ctx = getattr(self.ctx, "account_context", None) if self.ctx else None
        if account_ctx is not None:
            return (
                getattr(account_ctx, "platform", None),
                getattr(account_ctx, "account_id", None),
            )
        extra = {}
        if self.ctx:
            extra = getattr(self.ctx, "extra", {}) or {}
        platform = extra.get("platform") or None
        account_id = extra.get("account_id") or extra.get("reply_account") or None
        return platform, account_id

    @abstractmethod
    async def execute(self, **kwargs) -> ToolResult:
        ...

    def schema(self) -> dict:
        properties = {
            name: {
                key: value
                for key, value in (info or {}).items()
                if key != "required"
            }
            for name, info in (self.parameters or {}).items()
        }
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": properties,
                    "required": [
                        name for name, info in (self.parameters or {}).items()
                        if (info or {}).get("required", False)
                    ],
                },
            },
        }


class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, BaseTool] = {}

    def register(self, tool: BaseTool):
        self._tools[tool.name] = tool

    def remove(self, name: str) -> bool:
        """摘掉一个工具（供 YINYUE_TOOLS_DISABLED 用，可回滚）。"""
        return self._tools.pop(str(name), None) is not None

    def apply_disabled(self, disabled: set[str] | None = None) -> list[str]:
        """按 ``YINYUE_TOOLS_DISABLED``（逗号分隔）摘工具，返回被摘掉的名单。

        工具 schema 也是每轮 prompt 的一部分（2026-09-22 实测 24 个工具≈11.8k 字符，
        其中三分之一近两天从未被调用），所以给主人一个可回滚的瘦身开关。
        """
        import os

        names = disabled
        if names is None:
            raw = text("YINYUE_TOOLS_DISABLED")
            names = {part.strip() for part in raw.split(",") if part.strip()}
        removed = [name for name in sorted(names) if self.remove(name)]
        return removed

    def get(self, name: str) -> BaseTool | None:
        return self._tools.get(name)

    def all_schemas(self) -> list[dict]:
        return [t.schema() for t in self._tools.values()]

    def all_tools(self) -> dict[str, BaseTool]:
        return dict(self._tools)

    @classmethod
    def create_default(cls, repo=None, vector_store=None, embedding_client=None,
                       event_bus=None, llm=None, provider_manager=None,
                       meta_repo=None, unified_query=None,
                       send_enabled: bool | None = None,
                       platform: str = "") -> "ToolRegistry":
        from agents.tools.search import SearchMessagesTool, SearchThreadsTool, SearchVectorTool, SearchKnowledgeTool
        from agents.tools.contact import GetContactInfoTool
        from agents.tools.system import GetCurrentTimeTool, NotifyUserTool, EmitEventTool
        from agents.tools.reminder import CreateReminderTool
        from agents.tools.web import WebSearchTool, FetchUrlTool
        from agents.tools.analysis import GetTodayAnalysisTool, AnalyzeImageTool, DecisionHelperTool, WeatherTool
        from agents.tools.todo import CreateTodoTool, ListTodosTool, CompleteTodoTool
        from agents.tools.memory import (
        MemoryRecallTool,
        RecallMemoriesTool,
        RecallThreadTool,
        RememberTool,
    )
        from agents.tools.messaging import SendMessageTool
        from agents.tools.send_sticker import SendStickerTool
        from agents.tools.acg import AcgLookupTool
        from agents.tools.jobs import SendPlanTool, UiStateTool
        from agents.tools.emotion import ReadEmotionTool, GetStanceCardsTool
        from agents.tools.memory_index import ReadMemoryTool
        from agents.tools.local_files import ReadFileTool

        registry = cls()

        # Prefer the account-scoped repository.  ``meta_repo`` is only the
        # fallback for legacy/global construction; account data must never be
        # silently looked up in the shared meta database.
        effective_repo = repo or meta_repo
        effective_uq = unified_query

        registry.register(SearchMessagesTool(effective_repo, unified_query=effective_uq))
        registry.register(GetContactInfoTool(effective_repo, unified_query=effective_uq))
        registry.register(NotifyUserTool())
        registry.register(GetCurrentTimeTool())
        # 模型侧的「动手」壳：只声明、不发送（send_plan 停在等批准，gui_state 只读）
        registry.register(SendPlanTool())
        registry.register(UiStateTool())
        registry.register(CreateReminderTool(effective_repo, meta_repo=meta_repo))
        web_search = WebSearchTool(llm=llm, vector_store=vector_store, embedding_client=embedding_client, repo=effective_repo)
        registry.register(web_search)
        registry.register(GetTodayAnalysisTool(effective_repo, unified_query=effective_uq))
        # 情绪层 / 态度层：不进 prompt，模型自己填参数来取（2026-10-23）
        registry.register(ReadEmotionTool())
        registry.register(GetStanceCardsTool(meta_repo=meta_repo))
        # 记忆索引取值：提示词里只给 key，正文按 key 取（2026-10-23）
        registry.register(ReadMemoryTool())
        # 2026-11-18：只读本地文档。C 实验里她只能回「我翻不了项目里的文件」——
        # 经验教会她「先查索引」，却没有手去查。边界见 agents/tools/local_files.py。
        registry.register(ReadFileTool())
        registry.register(SearchThreadsTool(effective_repo, embedding_client, vector_store, unified_query=effective_uq))
        registry.register(RecallThreadTool(effective_repo, unified_query=effective_uq))
        registry.register(AnalyzeImageTool())
        registry.register(CreateTodoTool(effective_repo))
        registry.register(ListTodosTool(effective_repo))
        registry.register(CompleteTodoTool(effective_repo))
        # Phase 5 D1: the formal event stream is always on, so the
        # command-query projection is part of the default tool list.
        from agents.tools.command_query import QueryCommandsTool

        registry.register(QueryCommandsTool())
        # B3：⑩ 数据通道的只读工具口——⑥ 经它读持久人设（不写、不判断）。
        # **flag-gated 注册**：只有 PERSONA_CHANNEL=on 才注册——工具 schema 也是每轮
        # prompt 的一部分，off（默认）与 shadow（只记账）都必须与接线前逐字节一致；
        # on 档重启后进能力池，供⑥规划/工具循环使用。
        try:
            from core.execution.persona_channel import persona_channel_mode

            if persona_channel_mode() == "on":
                from agents.tools.persona import GetPersonaTool

                registry.register(GetPersonaTool(effective_repo))
        except Exception:  # noqa: BLE001 - flag 读不到按 off（不注册）
            warn("agents/tools/base.py:275 Exception", err=repr(__import__("sys").exc_info()[1]))
        # 2026-10-10 接线：本工具写了但从未 register（干装载用例 test_tool_dry_load.py 抓到）。
        # 它依赖的 emoji_recognition 子系统**活着且在跑**（db/sync_service.py:704 调度识别、
        # conversational.py:981 把 [表情包:md5] 换成可读描述、web/routes_api.py:1539 人工纠错口），
        # 所以是"该接线"而不是"该删"。补上之后，"描述识别错了"这件事除了主人去 Web 改，
        # 她也能自己记下来（execute 调 update_emoji_description）。
        from agents.tools.correct_emoji import CorrectEmojiDescTool

        registry.register(CorrectEmojiDescTool())
        from core import features as _features

        if _features.enabled(_features.PERSON_PROFILE_TOOLS):
            from agents.tools.person_profile import GetPersonProfileTool
            registry.register(GetPersonProfileTool(meta_repo=meta_repo))
        registry.register(WeatherTool())
        fetch_url = FetchUrlTool(repo=effective_repo, vector_store=vector_store,
                                       embedding_client=embedding_client, llm=llm)
        registry.register(fetch_url)
        web_search.set_fetch_tool(fetch_url)
        registry.register(AcgLookupTool())
        registry.register(DecisionHelperTool(llm=llm))

        if vector_store and embedding_client:
            registry.register(SearchVectorTool(vector_store, embedding_client))
            registry.register(RememberTool(effective_repo, vector_store, embedding_client, meta_repo=meta_repo))
            registry.register(RecallMemoriesTool(effective_repo, vector_store, embedding_client))
            registry.register(SearchKnowledgeTool(vector_store, embedding_client))

        # ⑥ 按需记忆工具：**永远注册**（2026-10-24）。原先是 NEEDS_MEMORY_INJECT
        # 三态门（仅 on 才注册）；该开关随 N1 按需记忆族退役，而生产一直是 on，
        # 所以这里固定注册 = 生产行为逐字节不变。要不要查由模型自己决定。
        try:
            registry.register(MemoryRecallTool())
        except Exception:  # noqa: BLE001 - 新后端未就绪不影响工具列表
            warn("agents/tools/base.py:309 Exception", err=repr(__import__("sys").exc_info()[1]))

        if event_bus:
            registry.register(EmitEventTool(event_bus))

        try:
            from core.config.settings import config
            enabled = config.wechat.send_enabled if send_enabled is None else send_enabled
            if enabled or provider_manager is not None:
                registry.register(SendMessageTool(
                    effective_repo, provider_manager, unified_query=effective_uq,
                ))
            # Register the sticker tool when the account/platform has a
            # provider advertising that capability.  WeChat advertises it via
            # WechatProvider; a future TelegramProvider can do the same.
            sticker_capable = (
                not platform
                or platform == "wechat"
                or (
                    provider_manager is not None
                    and provider_manager.supports(platform, "sticker")
                )
            )
            if enabled and sticker_capable:
                try:
                    from core.execution.intent_bus import get_intent_bus
                    intent_bus = get_intent_bus()
                    registry.register(SendStickerTool(
                        effective_repo, intent_bus=intent_bus, unified_query=effective_uq,
                    ))
                except Exception:
                    warn("agents/tools/base.py:340 Exception", err=repr(__import__("sys").exc_info()[1]))
                    registry.register(SendStickerTool(
                        effective_repo, unified_query=effective_uq,
                    ))
        except Exception:
            warn("agents/tools/base.py:344 Exception", err=repr(__import__("sys").exc_info()[1]))

        removed = registry.apply_disabled()
        if removed:
            log.info("tools_disabled", tools=removed)

        return registry
