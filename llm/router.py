"""LLMRouter: deterministic route -> chain -> attempt loop (WP3).

Implements ``docs/多模型接入_施工单.md`` §4.3:

- ``for_agent`` / ``build_chain`` are pure (no network, no breaker slot consumed);
- one ``llm_call`` row per attempt (success / failure / skipped);
- last real error wins on chain exhaustion; capability misses raise their own code;
- stream may fail over only before the first chunk.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, replace
from typing import Any, AsyncIterator

try:  # 2026-10-25 按角色绑定模型（见 core/llm_roles.py）
    from contextvars import ContextVar as _ContextVar

    _ROLE = _ContextVar("llm_role", default="")
except Exception:  # noqa: BLE001 - 没有 contextvars 也不该让路由起不来
    _ROLE = None

import structlog

from core.config.settings import config as _app_config
from llm import meter
from llm.adapters import get_adapter_class
from llm.adapters.base import (
    BaseAdapter,
    Capabilities,
    ChatResult,
    ProviderError,
    StreamChunk,
    ToolsResult,
)
from llm.config import EndpointSpec, LLMConfig, load_llm_config
from safelog import warn

log = structlog.get_logger()

_CHARS_PER_TOKEN = 4
_SLEEPS = (1, 4, 10)


@dataclass(frozen=True)
class Route:
    endpoint: str
    spec: EndpointSpec
    adapter: BaseAdapter | None
    source: str
    on_circuit_open: str = "error"


def estimate_tokens(req: dict[str, Any]) -> int:
    """WP4 formula: message str values + tools schema, 4 chars ~ 1 token."""
    messages = req.get("messages") or []
    text = "".join(str(message) for message in messages)
    tools = req.get("tools")
    if tools:
        text += json.dumps(tools, ensure_ascii=False, default=str)
    return max(len(text) // _CHARS_PER_TOKEN, 0)


class _CapabilityMiss(ProviderError):
    """Internal: one endpoint lacks a capability (carries which one)."""

    def __init__(self, code: str, message: str, *, provider: str = "", capability: str = ""):
        super().__init__(code, message, provider=provider)  # type: ignore[arg-type]
        self.capability = capability


def messages_with_image_placeholders(messages: list[dict], images: list[str]) -> list[dict]:
    """§4.4 vision downgrade: replace images with [图片] at the tail of the last user message."""
    out = [dict(message) for message in messages]
    marker = " ".join("[图片]" for _ in images)
    if not marker:
        return out
    index = next(
        (i for i in range(len(out) - 1, -1, -1) if out[i].get("role") == "user"),
        None,
    )
    if index is None:
        out.append({"role": "user", "content": marker})
        return out
    content = out[index].get("content")
    if isinstance(content, list):
        out[index]["content"] = list(content) + [{"type": "text", "text": marker}]
    else:
        text_before = str(content or "").strip()
        out[index]["content"] = f"{text_before} {marker}".strip()
    return out


class LLMRouter:
    def __init__(
        self,
        config: LLMConfig | None = None,
        *,
        adapters: dict[str, BaseAdapter] | None = None,
        breaker=None,
        budget_port=None,
        transport_factory=None,
        model_resolver=None,
        provider_filter=None,
        settings=None,
    ) -> None:
        self.config = config if config is not None else load_llm_config()
        self._adapters: dict[str, BaseAdapter] = dict(adapters or {})
        self._breaker = breaker
        self._budget_port = budget_port
        self._transport_factory = transport_factory
        self._model_resolver = model_resolver
        self._provider_filter = provider_filter
        self._settings = settings

    # ------------------------------------------------------------------
    # adapters / availability / capability
    # ------------------------------------------------------------------
    def _spec_for(self, name: str) -> EndpointSpec:
        spec = self.config.endpoints.get(name)
        if spec is not None:
            return spec
        if self._provider_filter is None:
            raise ProviderError("provider_down", f"端点 {name!r} 不在配置中")
        # 显式注入的 provider_filter 是测试/兼容 seam：允许配置外的 stub provider。
        model = "seam-model"
        if self._model_resolver is not None:
            try:
                model = str(self._model_resolver(name) or model)
            except Exception:  # noqa: BLE001
                warn("llm/router.py:125 Exception", err=repr(__import__("sys").exc_info()[1]))
        return EndpointSpec(
            name=name,
            kind="openai_compat",
            base_url="",
            model=model,
            api_key="seam",
            capabilities=Capabilities(
                tools=True, vision=True, json_mode=True, streaming=True,
                context_window=None,
            ),
        )

    def _adapter_for(self, name: str) -> BaseAdapter:
        adapter = self._adapters.get(name)
        if adapter is None:
            spec = self._spec_for(name)
            adapter_cls = get_adapter_class(spec.kind)
            if adapter_cls is None:
                raise ProviderError("bad_response", f"端点 {name} 的 kind={spec.kind!r} 未注册", provider=name)
            factory = None
            if self._transport_factory is not None:

                def _named_factory(_name: str = name):
                    return self._transport_factory(_name)

                factory = _named_factory
            adapter = adapter_cls(spec, settings=self._settings, client_factory=factory)
            self._adapters[name] = adapter
        return adapter

    def _auth_ok(self, name: str) -> bool:
        spec = self.config.endpoints[name]
        if spec.api_key:
            return bool(str(spec.api_key))
        if not spec.api_key_env:
            return True
        import os

        if os.environ.get(spec.api_key_env):
            return True
        settings = self._settings if self._settings is not None else _app_config.llm
        return bool(getattr(settings, spec.api_key_env.lower(), ""))

    def _capability_ok(self, kind: str, req: dict[str, Any], spec: EndpointSpec) -> bool:
        caps = spec.capabilities
        if kind == "tools" and not caps.tools:
            return False
        if kind == "vision" and not caps.vision:
            return False
        if kind == "stream" and not caps.streaming:
            return False
        if caps.context_window:
            if estimate_tokens(req) > caps.context_window:
                return False
        return True

    def _capability_miss(self, kind: str, req: dict[str, Any], spec: EndpointSpec,
                         name: str) -> "_CapabilityMiss | None":
        caps = spec.capabilities
        if kind == "tools" and not caps.tools:
            return _CapabilityMiss("tool_unsupported", f"端点 {name} 缺 tools 能力",
                                   provider=name, capability="tools")
        if kind == "vision" and not caps.vision:
            return _CapabilityMiss("bad_response", f"端点 {name} 缺 vision 能力",
                                   provider=name, capability="vision")
        if kind == "stream" and not caps.streaming:
            return _CapabilityMiss("bad_response", f"端点 {name} 不支持流式",
                                   provider=name, capability="stream")
        if caps.context_window and estimate_tokens(req) > caps.context_window:
            return _CapabilityMiss("context_overflow", f"端点 {name} 上下文不足",
                                   provider=name, capability="context")
        return None

    def _is_open(self, name: str) -> bool:
        if self._breaker is None:
            return False
        return bool(self._breaker.is_open(name))

    def _allow(self, name: str) -> bool:
        if self._breaker is None:
            return True
        return bool(self._breaker.allow(name))

    # ------------------------------------------------------------------
    # routing (pure)
    # ------------------------------------------------------------------
    def for_agent(self, agent: str, task: str = "", kind: str = "chat") -> Route:
        rule = None
        source = "default"
        for key, candidate_source in ((f"{agent}:{task}", "task"), (agent, "agent")):
            candidate = self.config.agent_endpoints.get(key)
            if candidate is None:
                continue
            if candidate.only_kind not in (None, kind):
                continue
            rule = candidate
            source = candidate_source
            break
        if rule is None:
            endpoint = self.config.default_endpoint
            return Route(
                endpoint=endpoint,
                spec=self.config.endpoints[endpoint],
                adapter=self._adapter_for(endpoint),
                source="default",
                on_circuit_open="error",
            )
        if rule.endpoint not in self.config.endpoints:
            raise ProviderError(
                "bad_response",
                f"routing.agents.{agent} 指向不存在的端点 {rule.endpoint!r}",
            )
        return Route(
            endpoint=rule.endpoint,
            spec=self.config.endpoints[rule.endpoint],
            adapter=self._adapter_for(rule.endpoint),
            source=source,
            on_circuit_open=rule.on_circuit_open,
        )

    def build_chain(self, route: Route, kind: str, req: dict[str, Any]) -> list[str]:
        cfg = self.config
        leads = [
            name for name in cfg.kind_lead.get(kind, ())
            if name in cfg.endpoints
            and self._auth_ok(name)
            and self._capability_ok(kind, req, cfg.endpoints[name])
        ]
        if route.on_circuit_open == "fallback":
            # fast_chat：熔断打开 -> 整链回落；否则只走该端点，调用失败不回落。
            base = list(cfg.fallback) if self._is_open(route.endpoint) else [route.endpoint]
        else:
            base = [route.endpoint] + [name for name in cfg.fallback if name != route.endpoint]
        base = [name for name in base if name in cfg.endpoints and self._auth_ok(name)]
        chain = leads + [name for name in base if name not in leads]
        if self._provider_filter is not None:
            usable, _skipped = self._provider_filter(chain, vision_mode=(kind == "vision"))
            # 注入的 seam 可以给出配置外的 stub provider（测试兼容位）。
            chain = list(usable)
        return chain
    # ------------------------------------------------------------------
    # meters / breaker / budget ports
    # ------------------------------------------------------------------
    def _emit(self, *, call_kind, agent, task, endpoint=None, spec=None, tokens=None,
              duration_ms=None, attempt=0, fallback_from=None, error=None,
              skipped=False, route_source="legacy", cost=None) -> None:
        meter.log_llm_call(
            call_kind=call_kind,
            provider=endpoint or "",
            model=(spec.model if spec is not None else ""),
            agent=agent or "",
            task=task or call_kind,
            endpoint=endpoint,
            provider_kind=(spec.kind if spec is not None else "openai_compat"),
            tokens=tokens,
            duration_ms=duration_ms,
            attempt=attempt,
            fallback_from=fallback_from,
            error=error,
            route_source=route_source,
            skipped=skipped,
            cost=cost,
        )

    def _success(self, name: str) -> None:
        if self._breaker is not None:
            self._breaker.record_success(name)

    def _failure(self, name: str) -> None:
        if self._breaker is not None:
            self._breaker.record_failure(name)

    def _budget_exceeded(self, agent: str) -> bool:
        if self._budget_port is None:
            return False
        return bool(self._budget_port.budget_exceeded(agent))

    def _record_usage(self, agent: str, tokens: int | None) -> None:
        if self._budget_port is not None:
            self._budget_port.record_usage(agent, tokens)

    def _refresh(self, name: str) -> None:
        adapter = self._adapters.get(name)
        refresher = getattr(adapter, "refresh_credentials", None)
        if callable(refresher):
            refresher()

    def _retryable(self, code: str) -> bool:
        if code in ("timeout", "provider_down"):
            return True
        return code == "rate_limit" and self.config.retry_on_rate_limit

    def _model(self, name: str, spec: EndpointSpec) -> str:
        try:  # 2026-10-25 角色表里为这个 provider 指定了模型就用它
            from core.llm_roles import override_for
            _ov = override_for(_ROLE.get() if _ROLE is not None else "")
            if _ov:
                if name == _ov.get("provider") and _ov.get("model"):
                    return str(_ov["model"])
                for _f in (_ov.get("fallback") or []):
                    if name == _f.get("provider") and _f.get("model"):
                        return str(_f["model"])
        except Exception:  # noqa: BLE001 - 角色表坏了就按原样走
            pass
        if self._model_resolver is not None:
            try:
                resolved = self._model_resolver(name)
            except Exception:  # noqa: BLE001 - a seam must not break routing
                warn("llm/router.py:323 Exception", err=repr(__import__("sys").exc_info()[1]))
                resolved = None
            if resolved:
                return str(resolved)
        # 2026-10-25 默认值跟随卡片：没绑角色、也没有 resolver 时，用「说话」那张卡的模型。
        # 否则内置 spec.model（例如 deepseek-chat）在网关改名后会静默指向不存在的模型。
        try:
            from core.llm_roles import override_for as _ovf
            _chat = _ovf("chat") or {}
            if _chat.get("provider") == name and _chat.get("model"):
                return str(_chat["model"])
        except Exception:  # noqa: BLE001 - 角色表坏了就按原样走
            pass
        return spec.model

    def _tag(self, result, name: str, spec: EndpointSpec, model: str | None = None):
        meta = dict(result.raw_meta)
        meta.update({
            "endpoint": name,
            "provider_kind": spec.kind,
            # 2026-10-27 单次指定模型时，回执里要写"实际用的那个"，不能退回端点默认值
            "model": model or meta.get("model") or spec.model,
        })
        return replace(result, raw_meta=meta)

    def _dispatch(self, kind: str, name: str, spec: EndpointSpec, req: dict[str, Any]):
        adapter = self._adapter_for(name)
        # 2026-10-27 试衣间/实验用：允许单次调用指定模型（不影响默认路由）
        _forced = str(req.get("model_override") or "").strip()
        model = _forced or self._model(name, spec)
        req["_effective_model"] = model
        messages = req.get("messages") or []
        temperature = req.get("temperature", spec.temperature if spec.temperature is not None else 0.3)
        max_tokens = req.get("max_tokens", spec.max_tokens if spec.max_tokens is not None else 2000)
        if kind == "chat":
            return adapter.chat(
                model=model, messages=messages, temperature=temperature,
                max_tokens=max_tokens, reasoning_effort=req.get("reasoning_effort"),
                thinking_budget=req.get("thinking_budget"), timeout=req.get("timeout"),
            )
        if kind == "tools":
            return adapter.chat_tools(
                model=model, messages=messages, tools=req.get("tools") or [],
                tool_choice=req.get("tool_choice", "auto"), temperature=temperature,
                max_tokens=max_tokens, reasoning_effort=req.get("reasoning_effort"),
                thinking_budget=req.get("thinking_budget"), timeout=req.get("timeout"),
            )
        if kind == "vision":
            return adapter.chat_vision(
                model=model, messages=messages, images=req.get("images") or [],
                temperature=temperature, max_tokens=max_tokens, timeout=req.get("timeout"),
            )
        raise ProviderError("bad_response", f"unsupported kind {kind!r}")

    # ------------------------------------------------------------------
    # public call / stream
    # ------------------------------------------------------------------
    async def call(self, kind: str, *, agent: str, task: str | None = None, **req):
        task = task or kind
        if self._budget_exceeded(agent):
            self._emit(call_kind=kind, agent=agent, task=task, error="budget", skipped=True)
            raise ProviderError("budget", "预算已用尽")
        route = self.for_agent(agent, task, kind)
        chain = self.build_chain(route, kind, req)
        # 2026-10-27 单次调用指定 provider（把它顶到链首，其余仍作兜底）
        _prov = str(req.get("provider_override") or "").strip()
        if _prov and _prov in self.config.endpoints:
            chain = [_prov] + [c for c in chain if c != _prov]
        try:  # 2026-10-25 角色绑定：把该角色选的 provider（含兜底）顶到链首
            from core.llm_roles import override_for, role_for_call
            _role = role_for_call(kind=kind, agent=agent, task=task or "")
            if _ROLE is not None:
                _ROLE.set(_role)
            _ov = override_for(_role)
            if _ov:
                _head = []
                if _ov.get("provider"):
                    _head.append(str(_ov["provider"]))
                for _fb in (_ov.get("fallback") or []):
                    _p = str(_fb.get("provider") or "")
                    if _p:
                        _head.append(_p)
                if _head:
                    chain = _head + [c for c in chain if c not in _head]
        except Exception:  # noqa: BLE001 - 角色表出错就退回原链
            pass
        if kind == "vision" and self.config.on_missing_capability != "fail":
            try:
                return await self._run_call(
                    dispatch_kind="vision", event_kind="vision", capability_kind="vision",
                    agent=agent, task=task, req=req, route=route, chain=chain,
                )
            except _CapabilityMiss as miss:
                warn("llm/router.py:380 _CapabilityMiss", err=repr(miss))
                if miss.capability != "vision":
                    raise
                # §4.4 downgrade：没有 vision 端点 -> 图片换 [图片] 占位文本，走 chat。
                downgraded = dict(req)
                downgraded["messages"] = messages_with_image_placeholders(
                    req.get("messages") or [], req.get("images") or [],
                )
                downgraded.pop("images", None)
                return await self._run_call(
                    dispatch_kind="chat", event_kind="vision", capability_kind="chat",
                    agent=agent, task=task, req=downgraded, route=route, chain=chain,
                    seed_fallback_from=miss.provider or None,
                )
        return await self._run_call(
            dispatch_kind=kind, event_kind=kind, capability_kind=kind,
            agent=agent, task=task, req=req, route=route, chain=chain,
        )

    async def _run_call(
        self, *, dispatch_kind: str, event_kind: str, capability_kind: str,
        agent: str, task: str, req: dict[str, Any], route: Route, chain: list[str],
        seed_fallback_from: str | None = None,
    ):
        last_error: ProviderError | None = None
        last_capability: _CapabilityMiss | None = None
        fallback_from: str | None = seed_fallback_from
        for name in chain:
            spec = self._spec_for(name)
            miss = self._capability_miss(capability_kind, req, spec, name)
            if miss is not None:
                last_capability = miss
                self._emit(call_kind=event_kind, agent=agent, task=task, endpoint=name, spec=spec,
                           error=miss.code, skipped=True, route_source=route.source)
                if self.config.on_missing_capability == "fail":
                    raise miss
                fallback_from = name
                continue
            if not self._allow(name):
                self._emit(call_kind=event_kind, agent=agent, task=task, endpoint=name, spec=spec,
                           error="circuit_open", skipped=True, route_source=route.source)
                fallback_from = name
                continue
            refreshed = False
            for attempt in range(self.config.max_retries + 1):
                started = time.monotonic()
                try:
                    result = await self._dispatch(dispatch_kind, name, spec, req)
                except ProviderError as exc:
                    warn("llm/router.py:428 ProviderError", err=repr(exc))
                    duration_ms = int((time.monotonic() - started) * 1000)
                    self._failure(name)
                    last_error = exc
                    self._emit(call_kind=event_kind, agent=agent, task=task, endpoint=name, spec=spec,
                               error=exc.code, duration_ms=duration_ms, attempt=attempt,
                               route_source=route.source, fallback_from=fallback_from)
                    if exc.code == "auth" and not refreshed:
                        self._refresh(name)
                        refreshed = True
                        continue
                    if self._retryable(exc.code) and attempt < self.config.max_retries:
                        await asyncio.sleep(_SLEEPS[min(attempt, len(_SLEEPS) - 1)])
                        continue
                    break
                duration_ms = int((time.monotonic() - started) * 1000)
                self._success(name)
                usage_dict = meter.usage_to_dict(result.usage)
                # 思考档读数：把 thinking 的尾巴带进 llm_call 事件，方便回看它当时在想什么
                # （reasoning 字段在 tokens 里；正文永远不含它）。
                _blocks = getattr(result, "reasoning_blocks", ()) or ()
                if _blocks:
                    _tail = str(getattr(_blocks[0], "text", "") or "")
                    if usage_dict is not None:
                        usage_dict["reasoning_tail"] = _tail[-240:]
                        usage_dict["reasoning_blocks"] = len(_blocks)
                self._emit(call_kind=event_kind, agent=agent, task=task, endpoint=name, spec=spec,
                           tokens=usage_dict,
                           cost=meter.compute_cost(result.usage, spec.pricing),
                           duration_ms=duration_ms, attempt=attempt, route_source=route.source,
                           fallback_from=fallback_from)
                self._record_usage(agent, result.usage.resolved_total())
                return self._tag(result, name, spec, model=req.get("_effective_model"))
            fallback_from = name
        if last_error is not None:
            raise last_error
        if last_capability is not None:
            raise last_capability
        raise ProviderError("provider_down", "没有可用的端点（全熔断或无端点）")

    async def stream(self, *, agent: str, task: str | None = None, **req) -> AsyncIterator[StreamChunk]:
        task = task or "stream"
        if self._budget_exceeded(agent):
            self._emit(call_kind="stream", agent=agent, task=task, error="budget", skipped=True)
            raise ProviderError("budget", "预算已用尽")
        route = self.for_agent(agent, task, "stream")
        chain = self.build_chain(route, "stream", req)
        last_error: ProviderError | None = None
        last_capability: ProviderError | None = None
        fallback_from: str | None = None
        for name in chain:
            spec = self._spec_for(name)
            miss = self._capability_miss("stream", req, spec, name)
            if miss is not None:
                last_capability = miss
                self._emit(call_kind="stream", agent=agent, task=task, endpoint=name, spec=spec,
                           error=miss.code, skipped=True, route_source=route.source)
                if self.config.on_missing_capability == "fail":
                    raise miss
                fallback_from = name
                continue
            if not self._allow(name):
                self._emit(call_kind="stream", agent=agent, task=task, endpoint=name, spec=spec,
                           error="circuit_open", skipped=True, route_source=route.source)
                fallback_from = name
                continue
            refreshed = False
            for attempt in range(self.config.max_retries + 1):
                started = time.monotonic()
                usage = None
                committed = False
                try:
                    adapter = self._adapter_for(name)
                    stream = adapter.stream(
                        model=self._model(name, spec),
                        messages=req.get("messages") or [],
                        temperature=req.get("temperature", spec.temperature if spec.temperature is not None else 0.7),
                        max_tokens=req.get("max_tokens", spec.max_tokens if spec.max_tokens is not None else 800),
                        reasoning_effort=req.get("reasoning_effort"),
                        thinking_budget=req.get("thinking_budget"),
                        timeout=req.get("timeout"),
                    )
                    async for chunk in stream:
                        if chunk.usage is not None:
                            usage = chunk.usage
                        if chunk.text:
                            committed = True
                            yield chunk
                    duration_ms = int((time.monotonic() - started) * 1000)
                    self._success(name)
                    self._emit(call_kind="stream", agent=agent, task=task, endpoint=name, spec=spec,
                               tokens=meter.usage_to_dict(usage), duration_ms=duration_ms,
                               cost=(meter.compute_cost(usage, spec.pricing) if usage is not None else None),
                               attempt=attempt, route_source=route.source,
                               fallback_from=fallback_from)
                    if usage is not None:
                        self._record_usage(agent, usage.resolved_total())
                    return
                except ProviderError as exc:
                    warn("llm/router.py:526 ProviderError", err=repr(exc))
                    duration_ms = int((time.monotonic() - started) * 1000)
                    self._failure(name)
                    last_error = exc
                    self._emit(call_kind="stream", agent=agent, task=task, endpoint=name, spec=spec,
                               error=exc.code, duration_ms=duration_ms, attempt=attempt,
                               route_source=route.source,
                               fallback_from=fallback_from)
                    if committed:
                        # 已提交不可逆增量：结束，不换端、不重放（§4.3）。
                        return
                    if exc.code == "auth" and not refreshed:
                        self._refresh(name)
                        refreshed = True
                        continue
                    if self._retryable(exc.code) and attempt < self.config.max_retries:
                        await asyncio.sleep(_SLEEPS[min(attempt, len(_SLEEPS) - 1)])
                        continue
                    break
            fallback_from = name
        if last_error is not None:
            raise last_error
        if last_capability is not None:
            raise last_capability
        raise ProviderError("provider_down", "没有可用的端点（全熔断或无端点）")
