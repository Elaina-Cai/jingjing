from __future__ import annotations

import json
import os
import re
import structlog
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from core.infra.event_bus import Event, EventType
from core.bootstrap import get_tools
from agents.tools import ToolRegistry, ToolResult, execute_tool_with_timeout
from safelog import warn
from core.infra.envflags import text

log = structlog.get_logger()

# A model that fails to call a tool can echo the invocation as plain text.  It
# must never reach the user as a chat message: the lines below are raw tool
# syntax, not something a person typed.
_TOOL_CALL_RE = re.compile(
    r"^\s*@?[^\s@]*\s*"
    r"(?:send_sticker|send_message|send_file|send_image|web_search|"
    r"search_messages|search_threads|search_knowledge|recall_memories|recall_thread|"
    r"create_reminder|notify_user|create_todo|complete_todo|get_contact_info|"
    r"analyze_contact_profile|get_today_analysis|fetch_url|analyze_image)\s*[\(\（]\s*[\{\[]?.*[\}\]]?\s*[\)\）]\s*$",
    re.IGNORECASE | re.DOTALL,
)
_TOOL_JSON_RE = re.compile(
    r'^\s*\{\s*"(?:action|tool|action_input)"\s*:.*\}\s*$',
    re.IGNORECASE | re.DOTALL,
)

_TOOL_LEAK_RE = re.compile(
    r"工具函数|工具调用|调用工具|跨会话发送|会话规则不允许|action_input|send_message|send_sticker"
)


def sanitize_tool_markup(reply: str) -> str:
    """Drop lines that are raw tool calls, keeping the human-readable reply."""
    if not reply:
        return reply
    kept = []
    removed = 0
    for line in reply.splitlines():
        if (
            _TOOL_CALL_RE.match(line)
            or _TOOL_JSON_RE.match(line)
            or _TOOL_LEAK_RE.search(line)
        ):
            removed += 1
            continue
        kept.append(line)
    cleaned = "\n".join(kept).strip()
    if removed:
        log.warning("tool_markup_stripped_from_reply", removed=removed,
                    original_len=len(reply), cleaned_len=len(cleaned))
    return cleaned


_CONTRACT_CACHE: frozenset[str] | None = None
_EXTRA_CONTRACT_VIOLATIONS: set[str] = set()


class ExtraContractViolation(KeyError):
    """Raised when an unknown extra key is written while mode is strict (default)."""


def _extra_contract_keys() -> frozenset[str] | None:
    global _CONTRACT_CACHE
    if _CONTRACT_CACHE is not None:
        return _CONTRACT_CACHE
    try:
        from core.routing.extra_contract import EXTRA_CONTRACT

        _CONTRACT_CACHE = frozenset(str(key) for key in EXTRA_CONTRACT)
    except Exception:  # noqa: BLE001 - import cycle / early boot: fail-open
        warn("agents/base.py:77 Exception", err=repr(__import__("sys").exc_info()[1]))
        return None
    return _CONTRACT_CACHE


def _extra_contract_mode() -> str:
    raw = (text("YINYUE_EXTRA_CONTRACT_MODE") or "strict").strip().lower()
    return raw if raw in {"off", "warn", "strict"} else "strict"


def _check_extra_key(key: object) -> None:
    mode = _extra_contract_mode()
    if mode == "off":
        return
    contract = _extra_contract_keys()
    if contract is None:
        return
    name = str(key)
    if name in contract:
        return
    _EXTRA_CONTRACT_VIOLATIONS.add(name)
    if mode == "strict":
        try:
            log.error(
                "extra_contract_violation",
                key=name,
                contract_size=len(contract),
            )
        except Exception:  # noqa: BLE001 - 守卫必须仍能抛出
            warn("agents/base.py:105 Exception", err=repr(__import__("sys").exc_info()[1]))
        raise ExtraContractViolation(
            f"unknown extra key {name!r} (contract size {len(contract)})"
        )
    try:
        log.warning(
            "extra_contract_violation",
            key=name,
            contract_size=len(contract),
        )
    except Exception:  # noqa: BLE001 - guard must never break the write
        warn("agents/base.py:116 Exception", err=repr(__import__("sys").exc_info()[1]))


class ExtraDict(dict):
    """AgentContext.extra 的运行期契约守卫（WP-08 8.2）。

    * ``off``    —— 不检查（仅回滚位）；
    * ``warn``   —— 未知键写 ``extra_contract_violation`` warning；
    * ``strict`` —— 未知键 ``log.error`` 后抛 :class:`ExtraContractViolation`（默认）；
      非法 ``YINYUE_EXTRA_CONTRACT_MODE`` 值 fail-safe 回落 strict。
    """

    def __init__(self, *args, **kwargs):
        super().__init__()
        if args or kwargs:
            self.update(*args, **kwargs)

    def __setitem__(self, key, value) -> None:  # noqa: D105
        _check_extra_key(key)
        super().__setitem__(key, value)

    def update(self, *args, **kwargs) -> None:  # noqa: D102
        incoming = dict(*args, **kwargs)
        for key in incoming:
            _check_extra_key(key)
        super().update(incoming)

    def setdefault(self, key, default=None):  # noqa: D102
        if key not in self:
            _check_extra_key(key)
        return super().setdefault(key, default)

    def __ior__(self, other):  # noqa: D105
        self.update(other)
        return self


@dataclass
class AgentContext:
    messages: list[dict] = field(default_factory=list)
    user_profile: dict = field(default_factory=dict)
    event: Event | None = None
    extra: dict[str, Any] = field(default_factory=ExtraDict)
    trigger: str = "user_command"
    trigger_msg_id: int | None = None
    thread_id: int | None = None
    accounts: dict[str, str] = field(default_factory=dict)
    owner_accounts: dict[str, list[str]] = field(default_factory=dict)
    # Optional runtime AccountContext.  Keeps account identity explicit for
    # agents/tools; legacy helpers can also resolve it through the contextvar
    # set by RuntimeRegistry.activate().
    account_context: Any = None
    # Deterministic identity context for this turn.  Populated by the router /
    # scheduler on every live turn; tools consume the same object instead of
    # re-deriving owner status from config ids or nicknames.
    principal: Any = None
    # Speech subsystem snapshot (core.speech.domain.SpeechSnapshot).
    # Kept as Any to avoid a domain<->agents import cycle while the
    # legacy extra-dict callers still transition.
    turn_context: Any = None
    speech_snapshot: Any = None
    #: WP-08 8.2：本轮 canonical typed decision（frozen TurnDecision）。两条生成
    #: 路径在 agent.execute 之前挂上；Web/主动工具等没走 decide_turn 的入口为 None。
    turn_decision: Any = None
    #: WP-08 8.2：本轮 outbound policy precheck 的结论（消费一次）。send_message
    #: 优先从这里读；extra["_policy_decision"] 仅作过渡兼容，删键后随之退役。
    policy_decision: Any = None
    memory_index_snapshot: Any = None  # 由记忆网关绑定的不可变全文索引
    #: WP-08 8.2b：N2 typed 结论（PlanResult）；仅 content_plan 投影写入时挂。
    plan_result: Any = None
    #: B2：本轮已附加的外层计划执行体（可变 Plan；仅 PLAN_LOOP=on 且规划成功时挂）。
    plan_object: Any = None
    #: B2：本轮计划执行结果（JSON 载荷，表达层渲染；非 None = 已有执行读数/失败读数）。
    plan_execution: Any = None
    #: B2：防双执行标记（同一 context 只触发一次计划执行）。
    plan_execution_started: bool = False
    #: WP-08 8.1：本回合 canonical trace id（route/generate 铸一次，子 trace 用 decision_trace_id 外键）。
    trace_id: str = ""

    #: 防乱接修复（2026-10-15）：本回合工具视图（只读白名单视图等）。树里 None =
    #: 全量工具；回复回合由 _run_agentic_loop_stream 挂只读视图，任务会话不设。
    tool_view: Any = None

    def __post_init__(self) -> None:
        if not isinstance(self.extra, ExtraDict):
            self.extra = ExtraDict(self.extra or {})

    def __setattr__(self, name: str, value: Any) -> None:
        if name == "extra" and not isinstance(value, ExtraDict):
            value = ExtraDict(value or {})
        super().__setattr__(name, value)

    def get(self, key: str, default: Any = None) -> Any:
        return self.extra.get(key, default)

    # ---- WP-08 8.2 typed decision 读口（过渡期兼容旧 extra 投影）--------
    def decision_value(self, name: str, default: Any = None) -> Any:
        """从 typed ``turn_decision`` 读一个判定字段；没有 decision 返回 default。

        ``None`` 表示本入口没走 ``decide_turn``（Web 聊天、主动工具直调），
        此时调用方按旧口径回退 ``extra``。
        """
        decision = getattr(self, "turn_decision", None)
        if decision is None:
            return default
        return getattr(decision, name, default)

    def take_policy_decision(self) -> Any:
        """消费一次 outbound policy precheck 结论（send_message 用）。

        typed 字段优先；旧 ``extra["_policy_decision"]`` 只作过渡兼容。
        两条路都保证**只被消费一次**（返回后清空）。
        """
        decision = getattr(self, "policy_decision", None)
        self.policy_decision = None
        extra = self.extra
        legacy = extra.pop("_policy_decision", None) if hasattr(extra, "pop") else None
        return decision if decision is not None else legacy


@dataclass
class ThoughtStep:
    thought: str
    action: str | None = None
    action_input: dict | None = None
    observation: str | None = None
    phase: str = "think"
    quality: dict | None = None


@dataclass
class AgentResult:
    agent_name: str
    output: dict = field(default_factory=dict)
    memories: list[str] = field(default_factory=list)
    confidence: float = 0.8
    events_to_emit: list[tuple[EventType, dict]] = field(default_factory=list)
    reasoning_trace: list[ThoughtStep] = field(default_factory=list)
    tools_used: list[str] = field(default_factory=list)
    plan_summary: str = ""


class BaseAgent(ABC):
    name: str = ""
    priority: int = 3
    description: str = ""
    max_iterations: int = 5

    def __init__(self, db_path: str = "", **deps):
        self.db_path = db_path
        self._deps = deps
        self._initialized = False
        self._tool_registry: ToolRegistry | None = None
        self._trace: list[ThoughtStep] = []
        self._tools_used: list[str] = []

    def inject(self, **deps):
        for key, value in deps.items():
            self._deps[key] = value
        return self

    def get_dep(self, key: str, default: Any = None) -> Any:
        return self._deps.get(key, default)

    def set_tool_registry(self, registry: ToolRegistry):
        self._tool_registry = registry

    @property
    def tools(self) -> ToolRegistry:
        if not self._tool_registry:
            self._tool_registry = self.get_dep("tools") or get_tools()
        return self._tool_registry

    def _tool_system_prompt(self) -> str:
        return f"""你是一个智能Agent，名叫{self.name}。你的职责: {self.description}

你可以调用工具。工具参数由原生 function calling schema 提供。

工具选择规则:
- 用户问某人画像、某人是什么样的人、是否记得某人、某人说过什么、最近的近况
  → 优先调用 get_person_profile，不要用 search_messages 逐条翻历史。
- 当前问题出现「他/她/它/这个人/那个人」时，先从最近上下文找到具体人名。
- 用户要求发表情包/表情 → 调用 send_sticker。
- 用户要求主动发消息给某人 → 调用 send_message。
- 有足够信息可以直接回答时，不要调用工具，直接给出最终回答。
- 工具失败时不要向用户提及工具、报错或调用过程；直接用你自己的话继续回答。"""

    def _tool_view(self, context: AgentContext | None):
        """本回合工具视图：``context.tool_view`` 优先（回复回合只读白名单）。"""
        view = getattr(context, "tool_view", None)
        return view if view is not None else self.tools

    async def think(self, prompt: str, context: AgentContext) -> ThoughtStep:
        llm = self.get_dep("llm")
        if not llm:
            return ThoughtStep(thought=prompt, action=None, action_input=None)

        tools = self._tool_view(context).all_tools()
        tool_schemas = [tool.schema() for tool in tools.values()] if tools else []

        # Preferred path: native function calling.  The model decides whether
        # to call a tool; we only translate its tool_calls into ThoughtStep.
        if tool_schemas and hasattr(llm, "chat_tools"):
            try:
                native = await llm.chat_tools(
                    system=self._tool_system_prompt(),
                    user=prompt,
                    tools=tool_schemas,
                    tool_choice="auto",
                    agent_name=self.name,
                    temperature=0.1,
                    max_tokens=1200,
                )
            except Exception as e:
                log.warning("agent_native_tool_call_failed", agent=self.name, error=str(e)[:120])
                native = None
            if native is not None and not native.get("error"):
                calls = native.get("tool_calls") or []
                if calls:
                    call = calls[0]
                    return ThoughtStep(
                        thought=native.get("content") or f"调用工具 {call.get('name', '')}",
                        action=call.get("name") or None,
                        action_input=call.get("arguments") or {},
                    )
                return ThoughtStep(
                    thought=native.get("content") or "",
                    action=None,
                    action_input=None,
                )

        # Fallback path for providers without native tool calling.  Keep the
        # JSON protocol but repair a bad first response instead of silently
        # treating it as "no action".
        tool_descriptions = ""
        for name, tool in tools.items():
            tool_descriptions += f"- {name}: {tool.description}\n"
            for pname, pinfo in tool.parameters.items():
                tool_descriptions += f"    参数 {pname}: {pinfo.get('description', '')}\n"

        system = f"""你是一个智能Agent，名叫{self.name}。你的职责: {self.description}

你可以使用以下工具:
{tool_descriptions}

请按以下格式思考和行动:
1. 先思考(Thought): 分析当前情况，决定下一步做什么
2. 如果需要使用工具(Action): 输出工具名和参数
3. 观察结果(Observation): 工具返回的结果
4. 重复以上步骤直到得出最终结论

重要：send_sticker、send_message 等动作工具，当用户请求对应动作时必须调用，不能跳过。
只有研究类工具（搜索、回忆等）在已有足够信息时可以跳过。
工具失败时不要向用户提及工具、报错或调用过程；直接用你自己的话继续回答。

如果你已经有足够信息得出结论，且不需要执行任何动作，直接在thought中给出最终判断。

请用JSON格式回复:
{{"thought": "你的思考", "action": "工具名或null", "action_input": {{参数}}或null}}"""

        response = await llm.chat(
            system=system,
            user=prompt,
            agent_name=self.name,
            temperature=0.1,
        )
        parsed = llm.parse_json(response, {})
        if not parsed or (
            "thought" not in parsed and "action" not in parsed
        ):
            repair_user = (
                f"原始任务:\n{prompt}\n\n"
                f"你上一次的输出不是合法 JSON:\n{response[:500]}\n\n"
                "请只输出一个 JSON 对象，不要 Markdown，不要解释。格式:\n"
                '{"thought": "你的思考", "action": "工具名或null", "action_input": {}}'
            )
            repaired = await llm.chat(
                system=system,
                user=repair_user,
                agent_name=self.name,
                temperature=0.0,
            )
            parsed = llm.parse_json(repaired, {
                "thought": repaired[:200],
                "action": None,
                "action_input": None,
            })

        return ThoughtStep(
            thought=parsed.get("thought", ""),
            action=parsed.get("action"),
            action_input=parsed.get("action_input"),
        )

    async def act(self, action: str, action_input: dict, context: AgentContext | None = None) -> ToolResult:
        try:  # M3 影子：模型侧工具名在统一目录里有没有实现（只记日志）
            # 注意：本模块 log 是 structlog —— 位置参数不会插值，必须先拼好字符串
            from core.tools.invoke import covered_by_catalog as _covered

            log.info(f"tool_catalog_shadow tool={action} covered={1 if _covered(action) else 0}")
        except Exception as _shadow_exc:  # 影子坏了绝不影响主路
            log.debug(f"tool_catalog_shadow_failed error={_shadow_exc!r}")
        tool = self._tool_view(context).get(action)
        if not tool:
            return ToolResult("", error=f"未知工具: {action}")

        self._tools_used.append(action)
        log.info("agent_tool_call", agent=self.name, tool=action, input=str(action_input)[:100])

        if context and hasattr(tool, "set_context"):
            tool.set_context(context)

        import time as _time
        start = _time.monotonic()

        try:
            result = await execute_tool_with_timeout(tool, **(action_input or {}))
            duration = int((_time.monotonic() - start) * 1000)
            # The error has to be logged too.  Logging only the output made a
            # tool that refused its arguments look identical to one that
            # succeeded quietly: send_sticker returned "" in 0.2ms because the
            # model had not passed a recipient, and nothing in the log said so.
            log.info("agent_tool_result", agent=self.name, tool=action,
                     output=result.output[:80],
                     error=getattr(result, "error", None))
            self._write_action_log(action, action_input, result, duration, context)
            self._trace_command_execution(
                action, result, duration, context, ok=not result.is_error
            )
            return result
        except Exception as e:
            duration = int((_time.monotonic() - start) * 1000)
            log.error("agent_tool_error", agent=self.name, tool=action, error=str(e))
            result = ToolResult("", error=str(e))
            self._write_action_log(action, action_input, result, duration, context)
            self._trace_command_execution(
                action, result, duration, context, ok=False
            )
            return result

    def _trace_command_execution(
        self,
        action: str,
        result: ToolResult,
        duration_ms: int,
        context: AgentContext | None,
        *,
        ok: bool,
    ) -> None:
        """A1-d：把这次**真实执行**接到命令链上（「谁下达 → 执行到哪」）。

        接线点就是上面那次 ``await tool.execute(...)``——工具真正被跑的那一处；
        **成功与失败两条路径都经过这里**（失败也留痕，否则"执行到哪"到不了失败那一步）。
        唯一读点与落点都在 ``core/terminal/cmd_trace.py``（三态，默认 ``off``）：
        ``off`` 时 ``record_tool_execution`` 第一行就返回，行为与接线前**逐字节一致**。

        全程 best-effort 且**不接返回值**：命令链接线永远不改工具结果、不改回复。
        """
        try:
            from core.terminal.cmd_trace import record_tool_execution

            record_tool_execution(
                context=context,
                action=action,
                ok=bool(ok),
                error=str(getattr(result, "error", "") or ""),
                duration_ms=int(duration_ms or 0),
            )
        except Exception as exc:  # noqa: BLE001 - 接线绝不阻断执行
            log.debug("cmd_trace_hook_failed", tool=action, error=str(exc)[:160])

    def _write_action_log(self, action: str, action_input: dict,
                          result: ToolResult, duration_ms: int,
                          context: AgentContext | None):
        self._shadow_record_action(action, action_input, result, duration_ms, context)
        try:
            from core.terminal.store_port import get_repo
            repo = get_repo()
            trigger = context.trigger if context else "unknown"
            trigger_msg_id = context.trigger_msg_id if context else None
            thread_id = context.thread_id if context else None
            platform = None
            account = None
            target = None
            related_send_log_id = None
            if action == "send_message" and action_input:
                platform = action_input.get("platform", "wechat")
                target = action_input.get("who")
                account_choice = action_input.get("account", "self")
                if context:
                    if account_choice == "self" and context.accounts:
                        account = context.accounts.get(platform, "")
                    elif account_choice == "owner" and context.owner_accounts:
                        ids = context.owner_accounts.get(platform, [])
                        account = ids[0] if ids else ""
                related_send_log_id = context.extra.get("_last_send_log_id") if context else None
            repo.action_log.add(
                tool_name=action,
                tool_args=json.dumps(action_input, ensure_ascii=False)[:2000] if action_input else None,
                result_summary=(result.output[:500] if not result.is_error else (result.error or ""))[:500],
                platform=platform,
                account=account,
                target=target,
                trigger=trigger,
                trigger_msg_id=trigger_msg_id,
                thread_id=thread_id,
                status="success" if not result.is_error else "failed",
                error=result.error if result.is_error else None,
                duration_ms=duration_ms,
                related_send_log_id=related_send_log_id,
            )
        except Exception as e:
            log.debug("action_log_write_failed", error=str(e))

    def _shadow_record_action(
        self,
        action: str,
        action_input: dict,
        result: ToolResult,
        duration_ms: int,
        context: AgentContext | None,
    ) -> None:
        try:
            from core.terminal.shadow import record_action_event
            extra = getattr(context, "extra", None) or {}
            platform = str(extra.get("platform") or "")
            account_id = str(extra.get("account_id") or "")
            chat_id = str(extra.get("chat_id") or "")
            chat_type = "group" if extra.get("is_group") else "private"
            principal = self._resolve_event_principal(context, extra)
            record_action_event(
                action=action,
                action_input=action_input or {},
                output=result.output if not result.is_error else "",
                error=result.error or "",
                ok=not result.is_error,
                duration_ms=duration_ms,
                platform=platform,
                bot_account_id=account_id,
                chat_id=chat_id,
                chat_type=chat_type,
                person_id=str(extra.get("sender_id") or ""),
                # A1-c：动作属于触发它的那一回合（turn_id + 触发消息事件 id）
                parent_event_id=str(extra.get("parent_event_id") or ""),
                turn_id=str(extra.get("turn_id") or ""),
                principal=principal,
            )
        except Exception:
            warn("agents/base.py:563 Exception", err=repr(__import__("sys").exc_info()[1]))

    @staticmethod
    def _resolve_event_principal(context: AgentContext | None, extra: dict) -> Any:
        """Use the already-resolved principal, else resolve read-only metadata."""
        try:
            from core.terminal.authority import context_principal

            principal = context_principal(context)
            if principal is not None:
                return principal
        except Exception:
            warn("agents/base.py:575 Exception", err=repr(__import__("sys").exc_info()[1]))
        try:
            from core.terminal.shadow_runtime import resolve_shadow_principal

            return resolve_shadow_principal(
                platform=str(extra.get("platform") or ""),
                account_id=str(extra.get("account_id") or ""),
                chat_id=str(extra.get("chat_id") or ""),
                chat_type="group" if extra.get("is_group") else "private",
                sender_id=str(extra.get("sender_id") or ""),
                sender_name=str(extra.get("sender_name") or ""),
                group_id=(
                    str(extra.get("chat_id") or "")
                    if extra.get("is_group")
                    else ""
                ),
            )
        except Exception:
            warn("agents/base.py:593 Exception", err=repr(__import__("sys").exc_info()[1]))
            return None

    def _shadow_record_thought(self, step: ThoughtStep, context: AgentContext | None) -> None:
        try:
            content = str(getattr(step, "thought", "") or "").strip()
            if not content:
                return
            from core.terminal.shadow import record_thought_event
            extra = getattr(context, "extra", None) or {}
            platform = str(extra.get("platform") or "")
            account_id = str(extra.get("account_id") or "")
            chat_id = str(extra.get("chat_id") or "")
            chat_type = "group" if extra.get("is_group") else "private"
            principal = self._resolve_event_principal(context, extra)
            record_thought_event(
                content=content,
                platform=platform,
                bot_account_id=account_id,
                chat_id=chat_id,
                chat_type=chat_type,
                person_id=str(extra.get("sender_id") or ""),
                # A1-c：与同一回合的 action/reply 共用 turn_id 和父事件 id
                parent_event_id=str(extra.get("parent_event_id") or ""),
                turn_id=str(extra.get("turn_id") or ""),
                principal=principal,
            )
        except Exception:
            warn("agents/base.py:620 Exception", err=repr(__import__("sys").exc_info()[1]))

    async def run(self, context: AgentContext) -> AgentResult:
        ...

    @abstractmethod
    def subscribed_events(self) -> list[EventType]:
        ...

    async def on_start(self):
        self._initialized = True
        self._trace = []
        self._tools_used = []

    async def on_finish(self, result: AgentResult):
        result.reasoning_trace = self._trace
        result.tools_used = self._tools_used

    async def on_error(self, error: Exception) -> AgentResult:
        return AgentResult(
            agent_name=self.name,
            output={"error": str(error)},
            confidence=0.0,
            reasoning_trace=self._trace,
            tools_used=self._tools_used,
        )

    async def execute(self, context: AgentContext) -> AgentResult:
        try:
            await self.on_start()
            result = await self.run(context)
            await self.on_finish(result)
            return result
        except Exception as e:
            warn("agents/base.py:654 Exception", err=repr(e))
            return await self.on_error(e)

    def __repr__(self):
        return f"<Agent {self.name} priority={self.priority}>"
