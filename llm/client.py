from __future__ import annotations
import json
import structlog
from typing import AsyncGenerator

from openai import AsyncOpenAI

from core.infra import promptlog  # 提示词形状改造 Step 2：把真正发出去的请求落盘

# openai SDK 的子模块是惰性加载：多线程（调度循环/to_thread 池）在服务启动
# 初期可能并发触发 openai.resources.* 的首次 import，Python 3.14 的模块锁会
# 检测到死锁/导入竞争（2026-09-23 起 13 次 embedding/tool 调用失败均源于此，
# 并曾导致降级到本地 qwen）。这里在单线程导入期预热，消除运行期竞态。
for _mod in ("openai.resources", "openai.resources.chat",
             "openai.resources.chat.completions", "openai.resources.embeddings"):
    try:
        __import__(_mod)
    except Exception as _exc:  # noqa: BLE001 - 预热失败不该拖住启动
        structlog.get_logger("llm.client").warning(
            "llm_client_prewarm_failed", module=_mod, err=repr(_exc))

from core.config.settings import config
from llm.adapters.base import ProviderError
from llm.circuit import CircuitBreaker
from llm.schema import expect_json, json_dumps, parse_json_payload
from safelog import warn

log = structlog.get_logger()

#: 工具参数修复请求（审计 G5-15 的"重试一次"）：把坏的那次 tool_call 原样回给模型，
#: 只让它重发参数。**不猜参数、不 _raw 兜底**——两次都坏就在信封里显式报错。
TOOL_REPAIR_SYSTEM = (
    "你上一次的工具调用参数不是合法 JSON。只重发这些调用，参数必须是合法 JSON 对象。\n"
    "只输出 JSON（不要 Markdown、不要解释）："
    '{"calls":[{"id":"原 id","name":"工具名","arguments":{…}}]}\n'
    "工具名与原 id 必须与上次完全一致，不要新增或删除调用。"
)

_PROVIDER_REGISTRY: dict[str, dict] = {}


def register_provider(
    name: str,
    api_key_fn,
    base_url_fn,
    model_fn,
    supports_vision: bool = False,
):
    _PROVIDER_REGISTRY[name] = {
        "api_key_fn": api_key_fn,
        "base_url_fn": base_url_fn,
        "model_fn": model_fn,
        "supports_vision": supports_vision,
    }


def _init_default_providers():
    if _PROVIDER_REGISTRY:
        return
    register_provider(
        "deepseek",
        api_key_fn=lambda: config.llm.deepseek_api_key,
        base_url_fn=lambda: config.llm.deepseek_base_url,
        model_fn=lambda: config.llm.deepseek_model,
        # 2026-10-25 复核（实测推翻了我早先的注释）：deepseek 的 OpenAI 兼容
        # 接口**接受** image_url content parts，并用 1×1 红图验证过能报出颜色
        # （`/api/service/llm/probe` 的 vision 检查 862ms 通过）。所以这里标 True。
        # 注意：能力判定还看模型名 token（llm/client.py::_model_supports_vision），
        # 换模型时以 probe 的实测结果为准，别只信名字。
        supports_vision=True,
    )
    register_provider(
        "openai",
        api_key_fn=lambda: config.llm.openai_api_key,
        base_url_fn=lambda: config.llm.openai_base_url,
        model_fn=lambda: config.llm.openai_model,
        supports_vision=True,
    )
    register_provider(
        "ollama",
        api_key_fn=lambda: "ollama",
        base_url_fn=lambda: f"{config.llm.ollama_base_url}/v1",
        model_fn=lambda: config.llm.ollama_model,
        supports_vision=True,
    )
    register_provider(
        "dashscope",
        api_key_fn=lambda: config.llm.dashscope_api_key,
        base_url_fn=lambda: config.llm.dashscope_base_url,
        model_fn=lambda: config.llm.dashscope_vision_model,
        supports_vision=True,
    )


def _model_supports_vision(model_name: str) -> bool:
    """Heuristic for providers whose model list mixes text-only and VL models."""
    name = str(model_name or "").strip().lower()
    if not name:
        return False
    return any(
        token in name
        for token in ("vl", "vision", "llava", "minicpm-v", "moondream")
    )


_FAST_CHAT_AGENTS = frozenset({"fast_chat", "fast_tool_format"})


def _usage_dict(usage) -> dict | None:
    """SDK usage -> unified ``llm_call`` tokens dict; missing stays None."""
    if usage is None:
        return None
    return {
        "prompt": getattr(usage, "prompt_tokens", None),
        "completion": getattr(usage, "completion_tokens", None),
        "total": getattr(usage, "total_tokens", None),
    }


class _RouterBudgetPort:
    """Adapter from the client's budget methods to the Router port names."""

    def __init__(self, client: "LLMClient") -> None:
        self._client = client

    def budget_exceeded(self, agent: str) -> bool:
        return self._client._budget_exceeded(agent)

    def record_usage(self, agent: str, tokens: int | None) -> None:
        self._client._record_usage(agent, tokens)


class LLMClient:
    def __init__(self):
        _init_default_providers()
        self._budget_used = 0
        self._daily_budget = config.llm.daily_token_budget
        self._per_agent_budget = config.llm.per_agent_budget
        self._agent_usage: dict[str, int] = {}
        self._circuit = CircuitBreaker(failure_threshold=3, recovery_timeout=60.0)
        self._client_pool: dict[str, AsyncOpenAI] = {}
        self._router = None

    def _filter_usable_providers(
        self, chain, *, vision_mode: bool = False,
    ) -> tuple[list[str], list[tuple[str, str]]]:
        """Drop providers that cannot serve the request before the chain runs.

        An unconfigured key costs a request, a retry cycle and a
        circuit-breaker failure, and it buries the real error (a wrong
        model, a timeout) under noise that looks the same.  In
        ``vision_mode`` an ollama model that is not multimodal is also
        dropped.  Returns ``(usable, skipped)`` where ``skipped`` holds
        ``(provider, reason)`` pairs for logging.
        """
        usable = []
        skipped = []
        for name in chain:
            if vision_mode and name == "ollama":
                try:
                    ollama_model = self._get_model("ollama")
                except Exception:
                    warn("llm/client.py:156 Exception", err=repr(__import__("sys").exc_info()[1]))
                    ollama_model = ""
                if not _model_supports_vision(ollama_model):
                    skipped.append((
                        name,
                        "模型 %r 不是多模态，已跳过" % ollama_model,
                    ))
                    continue
            key_fn = (_PROVIDER_REGISTRY.get(name) or {}).get("api_key_fn")
            if key_fn is not None:
                try:
                    if not key_fn():
                        skipped.append((name, "未配置 api_key"))
                        continue
                except Exception as exc:
                    warn("llm/client.py:170 Exception", err=repr(exc))
                    skipped.append((name, "api_key 读取失败: %s" % exc))
                    continue
            usable.append(name)
        return usable, skipped

    def _get_client(self, provider: str) -> AsyncOpenAI:
        if provider in self._client_pool:
            return self._client_pool[provider]
        entry = _PROVIDER_REGISTRY.get(provider)
        if not entry:
            raise ValueError(f"Unknown provider: {provider}. Registered: {list(_PROVIDER_REGISTRY)}")
        client = AsyncOpenAI(
            api_key=entry["api_key_fn"](),
            base_url=entry["base_url_fn"](),
        )
        self._client_pool[provider] = client
        return client

    def _get_model(self, provider: str) -> str:
        entry = _PROVIDER_REGISTRY.get(provider)
        if not entry:
            raise ValueError(f"Unknown provider: {provider}. Registered: {list(_PROVIDER_REGISTRY)}")
        return entry["model_fn"]()

    async def _repair_tool_arguments(
        self,
        request_messages: list[dict],
        broken: list[tuple[dict, str]],
        *,
        agent_name: str = "",
    ) -> list[dict] | None:
        """请模型**重发一次**参数（审计 G5-15 的"失败重试一次"）。

        协议与调用方无关：这里只发一条修正请求，把模型回的
        ``{"calls":[{"id","name","arguments"}]}`` 按 ``id``（缺失时按工具名）对回原来的
        调用。工具名以原调用为准（不让修复回话换工具）；仍不合法就返回 ``None``，
        由调用方在信封里显式报错。

        修复请求**不参与** provider 重试链：它是"模型打了错字"这一层的事，
        与网络/限流重试分开，免得一次坏参数把整条链重跑一遍。

        ``request_messages`` 只用于日志里对照（原请求长什么样），不重发——
        修正请求是独立的两条消息。
        """
        listing = [
            {
                "id": entry.get("id", ""),
                "name": entry.get("name", ""),
                "arguments_raw": entry.get("raw_text", ""),
                "error": reason[:200],
            }
            for entry, reason in broken
        ]
        repair_user = (
            "下面这些工具调用的 arguments 不是合法 JSON，请按原样重发一次参数：\n"
            + json_dumps(listing)
        )
        log.info("llm_tool_repair_requested", agent=agent_name,
                 broken=len(listing), messages=len(request_messages))
        try:
            raw = await self.chat(
                system=TOOL_REPAIR_SYSTEM,
                user=repair_user,
                agent_name=f"{agent_name or 'tool'}_repair",
                temperature=0.0,
                max_tokens=800,
            )
            if not raw:
                return None
            payload = expect_json(raw, required=("calls",), types={"calls": "list"})
        except Exception as exc:  # noqa: BLE001 - 修复失败 = 显式失败，不是静默兜底
            log.warning("llm_tool_repair_failed", agent=agent_name,
                        error=str(exc)[:160])
            return None

        by_id: dict[str, dict] = {}
        by_name: dict[str, dict] = {}
        for item in payload.get("calls") or []:
            if not isinstance(item, dict):
                continue
            args = item.get("arguments")
            if not isinstance(args, dict):
                continue
            by_id[str(item.get("id") or "")] = args
            by_name[str(item.get("name") or "")] = args
        repaired: list[dict] = []
        for entry, reason in broken:
            args = by_id.get(str(entry.get("id") or "")) or by_name.get(
                str(entry.get("name") or "")
            )
            if args is None:
                log.warning("llm_tool_repair_missing", agent=agent_name,
                            tool=entry.get("name", ""), error=reason[:120])
                return None
            fixed = dict(entry)
            fixed["arguments"] = args
            fixed.pop("error", None)
            fixed.pop("raw_text", None)
            repaired.append(fixed)
        log.info("llm_tool_arguments_repaired", agent=agent_name,
                 repaired=len(repaired))
        return repaired

    def _budget_exceeded(self, agent_name: str) -> bool:
        # 0 or negative means unlimited (local long-running test mode).
        if self._daily_budget > 0 and self._budget_used >= self._daily_budget:
            return True
        if (self._per_agent_budget > 0 and agent_name and self._agent_usage.get(agent_name, 0) >= self._per_agent_budget):
            return True
        return False

    def _record_usage(self, agent_name: str, tokens: int | None):
        """Record one successful call. ``None`` = provider gave no usage.

        ``None`` must not touch budget counters and must not invent an
        estimate; the unified event line is emitted by the caller.
        """
        counted = 0 if tokens is None else tokens
        if tokens is not None:
            self._budget_used += tokens
            if agent_name:
                self._agent_usage[agent_name] = self._agent_usage.get(agent_name, 0) + tokens
        # 回合账本（core/observability/turn_usage）：同一回合内的多次调用
        # （出稿/准入/话题线/视觉…）累在一起，供 turn_llm_usage 日志使用。
        # usage 缺失时仍记一次调用（tokens=0），预算不算。
        try:
            from core.observability import turn_usage

            turn_usage.record(agent_name, counted)
        except Exception:  # noqa: BLE001 - 记账失败不许影响主路径
            warn("llm/client.py:300 Exception", err=repr(__import__("sys").exc_info()[1]))

    @staticmethod
    def parse_json(response: str, fallback: dict) -> dict:
        if not response:
            return fallback
        text = response
        candidates: list[str] = []
        if "```json" in text:
            for part in text.split("```json")[1:]:
                candidates.append(part.split("```")[0])
        elif "```" in text:
            for part in text.split("```")[1:]:
                candidates.append(part)
        candidates.append(text)
        for candidate in candidates:
            cleaned = candidate.strip()
            try:
                return json.loads(cleaned)
            except json.JSONDecodeError:
                warn("llm/client.py:320 json.JSONDecodeError", err=repr(__import__("sys").exc_info()[1]))
            start = cleaned.find("{")
            end = cleaned.rfind("}")
            if start >= 0 and end > start:
                try:
                    return json.loads(cleaned[start:end + 1])
                except json.JSONDecodeError:
                    warn("llm/client.py:327 json.JSONDecodeError", err=repr(__import__("sys").exc_info()[1]))
        log.warning("llm_json_parse_failed", response_preview=text[:200])
        from core.context.degradation import note_degradation

        note_degradation(
            stage="llm",
            failure="json_parse",
            fallback="caller_fallback",
            detail=text[:80],
        )
        return fallback

    # ------------------------------------------------------------------
    # WP3: Router dispatch（legacy 路径已于 2026-10-11 删除，回滚 = git revert）
    # ------------------------------------------------------------------
    def _port_overridden(self, name: str) -> bool:
        """Router 端口是否被替换过：测试 / 跑分 seam 的判定入口。

        实例级打桩落在 ``self.__dict__``；子类覆写（如跑分脚本的
        ``_RecordingClient``）落在类上。两种都必须送进 Router，而普通
        client 保持不注入 —— 生产口径不变（Router 用内置 transport）。
        """
        if name in self.__dict__:
            return True
        return getattr(type(self), name, None) is not getattr(LLMClient, name, None)

    def _ensure_router(self):
        if self._router is None:
            from llm.router import LLMRouter

            transport = (
                self._get_client if self._port_overridden("_get_client") else None
            )
            resolver = self._get_model if self._port_overridden("_get_model") else None
            provider_filter = (
                self._filter_usable_providers
                if self._port_overridden("_filter_usable_providers") else None
            )
            self._router = LLMRouter(
                breaker=self._circuit,
                budget_port=_RouterBudgetPort(self),
                transport_factory=transport,
                model_resolver=resolver,
                provider_filter=provider_filter,
            )
        return self._router

    async def chat(
        self,
        system: str,
        user: str,
        agent_name: str = "",
        temperature: float = 0.3,
        max_tokens: int = 2000,
        messages: list[dict] | None = None,
    ) -> str:
        return await self._routed_chat(system, user, agent_name, temperature, max_tokens, messages)

    async def _routed_chat(self, system, user, agent_name, temperature, max_tokens, messages) -> str:
        request_messages = (
            list(messages) if messages is not None
            else [{"role": "system", "content": system}, {"role": "user", "content": user}]
        )
        try:
            result = await self._ensure_router().call(
                "chat", agent=agent_name, task="chat",
                messages=request_messages, temperature=temperature, max_tokens=max_tokens,
            )
        except ProviderError as exc:
            log.warning("llm_routed_failed", call_kind="chat", agent=agent_name,
                        error=exc.code, detail=str(exc)[:160])
            return ""
        return result.text

    async def chat_tools(
        self,
        *,
        system: str = "",
        user: str = "",
        messages: list[dict] | None = None,
        tools: list[dict],
        tool_choice: str | dict = "auto",
        agent_name: str = "",
        temperature: float = 0.1,
        max_tokens: int = 8192,  # 2026-10-31：主人要求取消上限（原 1200）
        model: str | None = None,
        provider: str | None = None,
    ) -> dict:
        """model / provider 只影响这一次调用（试衣间、对比实验用），不写任何配置。"""
        return await self._routed_chat_tools(
            system=system, user=user, messages=messages, tools=tools,
            tool_choice=tool_choice, agent_name=agent_name,
            temperature=temperature, max_tokens=max_tokens,
            model=model, provider=provider,
        )

    async def _routed_chat_tools(self, *, system, user, messages, tools, tool_choice,
                                 agent_name, temperature, max_tokens,
                                 model=None, provider=None) -> dict:
        request_messages = (
            list(messages) if messages is not None
            else [{"role": "system", "content": system}, {"role": "user", "content": user}]
        )
        # Step 2：先落盘再发。tools 为空也记一条（note=no_tools），不许静默省略。
        promptlog.record(
            provider=provider or "", model=model or "",
            params={"agent": agent_name, "tool_choice": tool_choice,
                    "temperature": temperature, "max_tokens": max_tokens},
            messages=request_messages, tools=tools,
        )
        if not tools:
            return {"content": "", "tool_calls": [], "error": "no_tools"}
        try:
            result = await self._ensure_router().call(
                "tools", agent=agent_name, task="tools", messages=request_messages,
                tools=tools, tool_choice=tool_choice,
                temperature=temperature, max_tokens=max_tokens,
                model_override=(str(model).strip() if model else None),
                provider_override=(str(provider).strip() if provider else None),
            )
        except ProviderError as exc:
            log.warning("llm_tool_call_failed", error=str(exc)[:180])
            error = "tool_unsupported" if exc.code == "tool_unsupported" else str(exc)[:180]
            return {"content": "", "tool_calls": [], "error": error}

        calls: list[dict] = []
        broken: list[tuple[dict, str]] = []
        for call in result.tool_calls:
            name = str(call.get("name", "") or "")
            raw_args = str(call.get("arguments", "") or "{}")
            outcome = parse_json_payload(raw_args, source=f"tool:{name or '?'}")
            entry = {
                "id": str(call.get("id", "") or ""),
                "name": name,
                "arguments": outcome.payload if outcome.ok else {},
            }
            if not outcome.ok:
                reason = "；".join(str(item) for item in outcome.problems) or "参数不是合法 JSON 对象"
                entry["error"] = reason[:200]
                entry["raw_text"] = raw_args[:500]
                broken.append((entry, reason))
            calls.append(entry)
        if broken:
            repaired = await self._repair_tool_arguments(
                request_messages, broken, agent_name=agent_name,
            )
            if repaired:
                calls = repaired
                broken = [entry for entry in repaired if entry.get("error")]

        resolved = result.usage.resolved_total()
        tokens = resolved if resolved is not None else 0
        provider = str(result.raw_meta.get("endpoint", "") or "")
        model = str(result.raw_meta.get("model", "") or "")
        envelope = {
            "content": result.text,
            "tool_calls": calls,
            "reasoning": "".join(getattr(b, "text", "") or "" for b in (result.reasoning_blocks or ())),
            "provider": provider,
            "model": model,
            "tokens": tokens,
        }
        if broken:
            envelope["error"] = (
                "tool_arguments_invalid: "
                + "；".join(reason for _, reason in broken)
            )[:200]
        return envelope

    async def chat_vision(
        self,
        system: str,
        user: str,
        images: list[str],
        agent_name: str = "",
        temperature: float = 0.3,
        max_tokens: int = 2000,
        return_result: bool = False,
    ):
        """问视觉模型「图里是什么」。

        ``return_result=True`` 时返回 ChatResult 而不是 text —— 思考型模型
        （deepseek-flash）常把观察写在 ``reasoning_blocks`` 里、且可能因为
        max_tokens 用尽而 text 为空（2026-10-27 实测：表情包 60 token 全被
        reasoning 吃掉，描述永久为空）。调用方需要自己取值时用它。
        """
        return await self._routed_chat_vision(
            system, user, images, agent_name, temperature, max_tokens,
            return_result=return_result,
        )

    async def _routed_chat_vision(self, system, user, images, agent_name,
                                  temperature, max_tokens, return_result=False):
        request_messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        try:
            result = await self._ensure_router().call(
                "vision", agent=agent_name, task="vision", messages=request_messages,
                images=images, temperature=temperature, max_tokens=max_tokens,
            )
        except ProviderError as exc:
            log.warning("llm_vision_routed_failed", error=exc.code, detail=str(exc)[:160])
            return None if return_result else ""
        return result if return_result else result.text

    async def stream_chat(
        self,
        system: str,
        user: str,
        agent_name: str = "",
        temperature: float = 0.7,
        max_tokens: int = 800,
        messages: list[dict] | None = None,
    ) -> AsyncGenerator[str, None]:
        async for chunk in self._routed_stream(
            system, user, agent_name, temperature, max_tokens, messages,
        ):
            yield chunk

    async def _routed_stream(self, system, user, agent_name, temperature,
                             max_tokens, messages):
        request_messages = (
            list(messages) if messages is not None
            else [{"role": "system", "content": system}, {"role": "user", "content": user}]
        )
        router = self._ensure_router()
        try:
            async for chunk in router.stream(
                agent=agent_name, task="stream", messages=request_messages,
                temperature=temperature, max_tokens=max_tokens,
            ):
                yield chunk.text
        except ProviderError as exc:
            log.warning("llm_stream_all_failed", error=exc.code, detail=str(exc)[:160])
            return

