"""LLM endpoint / routing config: TOML + legacy env mapping (WP1).

Priority (low -> high):
    builtin defaults < configs/llm.toml < legacy env < YINYUE_LLM_* < explicit args

"Legacy env" means the fields LLMSettings already reads (DEEPSEEK_MODEL, ...).
Only fields explicitly set in the environment / .env (``model_fields_set``)
may override the TOML file; untouched defaults stay defaults. Every legacy
overlay emits one ``llm_legacy_env_deprecated`` warning.

See ``docs/多模型接入_施工单.md`` §4.2 and §6.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import structlog

from core.config.settings import config as _app_config
from llm.adapters import known_kinds
from llm.adapters.base import Capabilities

log = structlog.get_logger()

# 2026-11-01：旧 env 覆盖只报一次就够了。load_llm_config() 每个轮询都会被调，
# 原来每次刷一条 warning —— 17MB 日志里每分钟 15~40 条全是它，把真正要看的埋了。
_LEGACY_WARNED: set = set()

KNOWN_KINDS = frozenset({"openai_compat", "anthropic"})
LEGACY_AGENT_ROUTES = ("fast_chat", "fast_tool_format")
_VISION_HINTS = ("vl", "vision", "llava", "minicpm-v", "moondream")


class LLMConfigError(ValueError):
    """Human-readable config error (missing endpoint / unknown kind / ...)."""


@dataclass(frozen=True)
class AgentRoute:
    endpoint: str
    only_kind: str | None = None          # None = 所有 kind（chat/tools/vision/stream）
    on_circuit_open: str = "error"        # error | fallback


@dataclass(frozen=True)
class EndpointSpec:
    name: str
    kind: str
    base_url: str
    model: str
    api_key_env: str = ""                 # 与 api_key 二选一；两者都空 = 不要求鉴权（ollama）
    api_key: str | None = None            # 字面量密钥（ollama="ollama"）；优先于 api_key_env
    temperature: float | None = None
    max_tokens: int | None = None
    # 思考档（2026-10-16）：默认不传 = 随端点。reasoning_effort=low/high/max；
    # thinking=\"enabled\"/\"disabled\"。开了思考就不要 temperature（会削思考质量）。
    reasoning_effort: str = ""
    thinking: str = ""
    timeout: float | None = None          # 缺省用 config.llm.timeout_seconds
    capabilities: Capabilities = Capabilities()
    pricing: dict[str, Any] | None = None


@dataclass(frozen=True)
class LLMConfig:
    endpoints: dict[str, EndpointSpec]
    default_endpoint: str
    agent_endpoints: dict[str, AgentRoute]
    fallback: tuple[str, ...]
    on_missing_capability: str = "downgrade"
    max_retries: int = 2
    retry_on_rate_limit: bool = False
    legacy_provider: str = ""
    kind_lead: dict[str, tuple[str, ...]] = field(default_factory=dict)
    budgets: dict[str, Any] = field(default_factory=dict)


def _model_supports_vision(model_name: str) -> bool:
    """Legacy ollama heuristic; mirrors ``llm/client.py`` (pre-WP0 :95)."""
    name = str(model_name or "").strip().lower()
    if not name:
        return False
    return any(token in name for token in _VISION_HINTS)


def _legacy_caps(*, vision: bool, context_window: int | None = None) -> Capabilities:
    """Capabilities legacy env endpoints get (§6: tools/json/streaming 直通)."""
    return Capabilities(
        tools=True, vision=vision, json_mode=True, streaming=True,
        stream_usage=True, context_window=context_window,
    )


def _builtin_endpoints(st) -> tuple[dict[str, EndpointSpec], dict[str, tuple[str, ...]]]:
    timeout = float(getattr(st, "timeout_seconds", 30) or 30)
    endpoints = {
        "deepseek": EndpointSpec(
            name="deepseek", kind="openai_compat",
            base_url=str(st.deepseek_base_url), model=str(st.deepseek_model),
            api_key_env="DEEPSEEK_API_KEY", timeout=timeout,
            capabilities=_legacy_caps(vision=True, context_window=65536),
        ),
        "openai": EndpointSpec(
            name="openai", kind="openai_compat",
            base_url=str(st.openai_base_url), model=str(st.openai_model),
            api_key_env="OPENAI_API_KEY", timeout=timeout,
            capabilities=_legacy_caps(vision=True),
        ),
        "ollama": EndpointSpec(
            name="ollama", kind="openai_compat",
            base_url=f"{str(st.ollama_base_url).rstrip('/')}/v1", model=str(st.ollama_model),
            api_key="ollama", timeout=timeout,
            capabilities=_legacy_caps(vision=_model_supports_vision(st.ollama_model)),
        ),
    }
    kind_lead: dict[str, tuple[str, ...]] = {}
    if str(st.dashscope_api_key or ""):
        endpoints["dashscope"] = EndpointSpec(
            name="dashscope", kind="openai_compat",
            base_url=str(st.dashscope_base_url), model=str(st.dashscope_vision_model),
            api_key_env="DASHSCOPE_API_KEY", timeout=timeout,
            capabilities=_legacy_caps(vision=True),
        )
        kind_lead["vision"] = ("dashscope",)
    return endpoints, kind_lead


def _builtin_config(st) -> LLMConfig:
    endpoints, kind_lead = _builtin_endpoints(st)
    fallback = tuple(str(x) for x in (st.fallback_chain or ("deepseek",)))
    if not fallback:
        fallback = ("deepseek",)
    budgets = {
        "daily_tokens": getattr(st, "daily_token_budget", 0),
        "per_agent": {"*": getattr(st, "per_agent_budget", 0)},
    }
    agents = {
        name: AgentRoute(endpoint="ollama", only_kind="chat", on_circuit_open="fallback")
        for name in LEGACY_AGENT_ROUTES
    }
    return LLMConfig(
        endpoints=endpoints,
        default_endpoint=fallback[0],
        agent_endpoints=agents,
        fallback=fallback,
        # legacy 等价：无可用 vision 端点 -> 空串（§6）。TOML 存在时缺省 downgrade。
        on_missing_capability="fail",
        max_retries=int(getattr(st, "max_retries", 2) or 2),
        retry_on_rate_limit=False,
        legacy_provider="",
        kind_lead=kind_lead,
        budgets=budgets,
    )

def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    return float(value)


def _legacy_overlay(st, cfg: LLMConfig) -> tuple[LLMConfig, list[str]]:
    """Apply explicitly-set legacy env fields over ``cfg`` (legacy env > TOML)."""
    fields = set(getattr(st, "model_fields_set", set()) or set())
    if not fields:
        return cfg, []

    applied: list[str] = []
    endpoints = dict(cfg.endpoints)
    kind_lead = {k: tuple(v) for k, v in cfg.kind_lead.items()}
    budgets = dict(cfg.budgets)
    raw_per_agent = budgets.get("per_agent")
    per_agent = dict(raw_per_agent) if isinstance(raw_per_agent, dict) else {}
    fallback = cfg.fallback
    default = cfg.default_endpoint
    max_retries = cfg.max_retries
    legacy_provider = cfg.legacy_provider

    def _replace(name: str, **changes: Any) -> None:
        current = endpoints.get(name)
        if current is None:
            return
        endpoints[name] = replace(current, **changes)

    if "provider" in fields:
        legacy_provider = str(st.provider or "")
        applied.append("PROVIDER")

    if "fallback_chain" in fields and st.fallback_chain:
        fallback = tuple(str(x) for x in st.fallback_chain)
        default = fallback[0]
        applied.append("FALLBACK_CHAIN")

    if "deepseek_api_key" in fields:
        _replace("deepseek", api_key=None, api_key_env="DEEPSEEK_API_KEY")
        applied.append("DEEPSEEK_API_KEY")
    if "deepseek_base_url" in fields:
        _replace("deepseek", base_url=str(st.deepseek_base_url))
        applied.append("DEEPSEEK_BASE_URL")
    if "deepseek_model" in fields:
        _replace("deepseek", model=str(st.deepseek_model))
        applied.append("DEEPSEEK_MODEL")

    if "openai_api_key" in fields:
        _replace("openai", api_key=None, api_key_env="OPENAI_API_KEY")
        applied.append("OPENAI_API_KEY")
    if "openai_base_url" in fields:
        _replace("openai", base_url=str(st.openai_base_url))
        applied.append("OPENAI_BASE_URL")
    if "openai_model" in fields:
        _replace("openai", model=str(st.openai_model))
        applied.append("OPENAI_MODEL")

    ollama_changes: dict[str, Any] = {}
    if "ollama_base_url" in fields:
        ollama_changes["base_url"] = f"{str(st.ollama_base_url).rstrip('/')}/v1"
        applied.append("OLLAMA_BASE_URL")
    if "ollama_model" in fields:
        ollama_changes["model"] = str(st.ollama_model)
        current = endpoints.get("ollama")
        if current is not None:
            ollama_changes["capabilities"] = replace(
                current.capabilities,
                vision=_model_supports_vision(st.ollama_model),
            )
        applied.append("OLLAMA_MODEL")
    if ollama_changes:
        _replace("ollama", **ollama_changes)

    if "dashscope_api_key" in fields and str(st.dashscope_api_key or ""):
        if "dashscope" not in endpoints:
            endpoints["dashscope"] = EndpointSpec(
                name="dashscope", kind="openai_compat",
                base_url=str(st.dashscope_base_url), model=str(st.dashscope_vision_model),
                api_key_env="DASHSCOPE_API_KEY", timeout=float(st.timeout_seconds or 30),
                capabilities=_legacy_caps(vision=True),
            )
        kind_lead["vision"] = ("dashscope",)
        applied.append("DASHSCOPE_API_KEY")
    if "dashscope_base_url" in fields and "dashscope" in endpoints:
        _replace("dashscope", base_url=str(st.dashscope_base_url))
        applied.append("DASHSCOPE_BASE_URL")
    if "dashscope_vision_model" in fields and "dashscope" in endpoints:
        _replace("dashscope", model=str(st.dashscope_vision_model))
        applied.append("DASHSCOPE_VISION_MODEL")

    if "timeout_seconds" in fields:
        timeout = float(st.timeout_seconds)
        endpoints = {name: replace(spec, timeout=timeout) for name, spec in endpoints.items()}
        applied.append("TIMEOUT_SECONDS")
    if "max_retries" in fields:
        max_retries = int(st.max_retries)
        applied.append("MAX_RETRIES")
    if "daily_token_budget" in fields:
        budgets["daily_tokens"] = st.daily_token_budget
        applied.append("DAILY_TOKEN_BUDGET")
    if "per_agent_budget" in fields:
        per_agent["*"] = st.per_agent_budget
        applied.append("PER_AGENT_BUDGET")
    if per_agent:
        budgets["per_agent"] = per_agent

    return LLMConfig(
        endpoints=endpoints,
        default_endpoint=default,
        agent_endpoints=dict(cfg.agent_endpoints),
        fallback=fallback,
        on_missing_capability=cfg.on_missing_capability,
        max_retries=max_retries,
        retry_on_rate_limit=cfg.retry_on_rate_limit,
        legacy_provider=legacy_provider,
        kind_lead=kind_lead,
        budgets=budgets,
    ), applied

def _endpoint_from_toml(name: str, data: Any, base: EndpointSpec | None) -> EndpointSpec:
    if not isinstance(data, dict):
        raise LLMConfigError(f"endpoints.{name} 必须是表（table）")
    fallback_caps = base.capabilities if base is not None else Capabilities()
    cap_data = data.get("capabilities")
    caps = fallback_caps
    if isinstance(cap_data, dict):
        caps = Capabilities(
            tools=bool(cap_data.get("tools", fallback_caps.tools)),
            vision=bool(cap_data.get("vision", fallback_caps.vision)),
            json_mode=bool(cap_data.get("json_mode", fallback_caps.json_mode)),
            streaming=bool(cap_data.get("streaming", fallback_caps.streaming)),
            reasoning=bool(cap_data.get("reasoning", fallback_caps.reasoning)),
            context_window=cap_data.get("context_window", fallback_caps.context_window),
            stream_usage=bool(cap_data.get("stream_usage", fallback_caps.stream_usage)),
        )
    spec_data = data.get("spec") or {}
    if not isinstance(spec_data, dict):
        raise LLMConfigError(f"endpoints.{name}.spec 必须是表")
    return EndpointSpec(
        name=name,
        kind=str(data.get("kind", base.kind if base else "openai_compat")),
        base_url=str(data.get("base_url", base.base_url if base else "")),
        model=str(data.get("model", base.model if base else "")),
        api_key_env=str(data.get("api_key_env", base.api_key_env if base else "")),
        api_key=(data["api_key"] if "api_key" in data else (base.api_key if base else None)),
        temperature=_as_float(data.get(
            "temperature", spec_data.get("temperature", base.temperature if base else None),
        )),
        max_tokens=(data.get("max_tokens", spec_data.get("max_tokens", base.max_tokens if base else None))),
        reasoning_effort=str(data.get(
            "reasoning_effort",
            spec_data.get("reasoning_effort", base.reasoning_effort if base else ""),
        ) or ""),
        thinking=str(data.get(
            "thinking",
            spec_data.get("thinking", base.thinking if base else ""),
        ) or ""),
        timeout=_as_float(data.get("timeout", base.timeout if base else None)),
        capabilities=caps,
        pricing=(data.get("pricing", base.pricing if base else None)),
    )


def _agent_route_from_toml(name: str, value: Any) -> AgentRoute:
    if isinstance(value, str):
        return AgentRoute(endpoint=value)
    if isinstance(value, dict):
        endpoint = value.get("endpoint")
        if not endpoint:
            raise LLMConfigError(f"routing.agents.{name} 缺少 endpoint")
        only_kind = value.get("only_kind")
        if only_kind not in (None, "chat", "tools", "vision", "stream"):
            raise LLMConfigError(f"routing.agents.{name}.only_kind={only_kind!r} 非法")
        on_open = value.get("on_circuit_open", "error")
        if on_open not in ("error", "fallback"):
            raise LLMConfigError(f"routing.agents.{name}.on_circuit_open={on_open!r} 非法")
        return AgentRoute(endpoint=str(endpoint), only_kind=only_kind, on_circuit_open=str(on_open))
    raise LLMConfigError(f"routing.agents.{name} 必须是字符串或表")


def _merge_budgets(base: dict[str, Any], raw: Any) -> dict[str, Any]:
    out = dict(base)
    if raw is None:
        return out
    if not isinstance(raw, dict):
        raise LLMConfigError("budgets 必须是表")
    if "daily_tokens" in raw:
        out["daily_tokens"] = raw["daily_tokens"]
    if "per_agent" in raw:
        existing = out.get("per_agent")
        merged = dict(existing) if isinstance(existing, dict) else {}
        if not isinstance(raw["per_agent"], dict):
            raise LLMConfigError("budgets.per_agent 必须是表")
        merged.update(raw["per_agent"])
        out["per_agent"] = merged
    return out


def _merge_toml(cfg: LLMConfig, raw: dict) -> LLMConfig:
    if not isinstance(raw, dict):
        raise LLMConfigError("TOML 顶层必须是表")
    version = raw.get("version", 1)
    if version != 1:
        raise LLMConfigError(f"配置 version={version!r} 不支持（只支持 1）")

    endpoints = dict(cfg.endpoints)
    raw_endpoints = raw.get("endpoints") or {}
    if not isinstance(raw_endpoints, dict):
        raise LLMConfigError("endpoints 必须是表")
    for name, data in raw_endpoints.items():
        key = str(name)
        endpoints[key] = _endpoint_from_toml(key, data, endpoints.get(key))

    routing = raw.get("routing") or {}
    if not isinstance(routing, dict):
        raise LLMConfigError("routing 必须是表")
    fallback = tuple(str(x) for x in routing.get("fallback", cfg.fallback))
    if "default" in routing:
        default = str(routing["default"])
    elif "fallback" in routing and fallback:
        default = fallback[0]
    else:
        default = cfg.default_endpoint

    agents = dict(cfg.agent_endpoints)
    raw_agents = routing.get("agents") or {}
    if not isinstance(raw_agents, dict):
        raise LLMConfigError("routing.agents 必须是表")
    for name, value in raw_agents.items():
        agents[str(name)] = _agent_route_from_toml(str(name), value)

    kind_lead = {k: tuple(v) for k, v in cfg.kind_lead.items()}
    raw_kinds = routing.get("kinds") or {}
    if not isinstance(raw_kinds, dict):
        raise LLMConfigError("routing.kinds 必须是表")
    for kind, data in raw_kinds.items():
        if isinstance(data, dict) and data.get("lead") is not None:
            kind_lead[str(kind)] = tuple(str(x) for x in data["lead"])

    return LLMConfig(
        endpoints=endpoints,
        default_endpoint=default,
        agent_endpoints=agents,
        fallback=fallback,
        # TOML 是新配置：未写就按 §4.2 默认 downgrade（legacy-only 才是 fail）。
        on_missing_capability=str(routing.get("on_missing_capability", "downgrade")),
        max_retries=int(routing.get("max_retries", cfg.max_retries)),
        retry_on_rate_limit=bool(routing.get("retry_on_rate_limit", cfg.retry_on_rate_limit)),
        legacy_provider=str(routing.get("legacy_provider", cfg.legacy_provider)),
        kind_lead=kind_lead,
        budgets=_merge_budgets(cfg.budgets, raw.get("budgets")),
    )


def _apply_env_overrides(cfg: LLMConfig, env: dict[str, str]) -> LLMConfig:
    default = str(env.get("YINYUE_LLM_DEFAULT_ENDPOINT") or cfg.default_endpoint)
    agents = dict(cfg.agent_endpoints)
    prefix = "YINYUE_LLM_ROUTE_"
    for key, value in env.items():
        if not key.startswith(prefix) or not value:
            continue
        agent = key[len(prefix):].lower()
        current = agents.get(agent)
        agents[agent] = AgentRoute(
            endpoint=str(value),
            only_kind=current.only_kind if current else None,
            on_circuit_open=current.on_circuit_open if current else "error",
        )
    return replace(cfg, default_endpoint=default, agent_endpoints=agents)


def _validate(cfg: LLMConfig) -> None:
    if not cfg.endpoints:
        raise LLMConfigError("没有配置任何端点")
    kinds = known_kinds()
    for name, spec in sorted(cfg.endpoints.items()):
        if spec.kind not in kinds:
            raise LLMConfigError(
                f"端点 {name!r} 的 kind={spec.kind!r} 未知；已知 kind: {sorted(kinds)}"
            )
        if not spec.model:
            raise LLMConfigError(f"端点 {name!r} 缺少 model")
    if cfg.default_endpoint not in cfg.endpoints:
        raise LLMConfigError(
            f"默认端点 {cfg.default_endpoint!r} 不存在；可用端点: {sorted(cfg.endpoints)}"
        )
    for name in cfg.fallback:
        if name not in cfg.endpoints:
            raise LLMConfigError(
                f"fallback 端点 {name!r} 不存在；可用端点: {sorted(cfg.endpoints)}"
            )
    # 2026-10-25：设置页允许删卡片，删掉的端点可能还被 routing.agents 引用。
    # 以前这里直接抛 LLMConfigError，等于"删一张卡把整个引擎弄哑"，太难恢复。
    # 现在降级：丢掉这条 agent 覆盖（回落默认端点）+ 警告，配置仍然可用。
    _dead = [a for a, r in sorted(cfg.agent_endpoints.items()) if r.endpoint not in cfg.endpoints]
    if _dead:
        for _a in _dead:
            log.warning("llm_agent_endpoint_missing",
                        agent=_a, endpoint=cfg.agent_endpoints[_a].endpoint,
                        hint="已忽略这条覆盖，回落默认端点")
        cfg = replace(cfg, agent_endpoints={k: v for k, v in cfg.agent_endpoints.items()
                                            if k not in set(_dead)})
    for kind, leads in sorted(cfg.kind_lead.items()):
        for name in leads:
            if name not in cfg.endpoints:
                # kind lead 是"有就用"的偏好：缺端点只提示，不拒绝整份配置。
                log.warning("llm_kind_lead_endpoint_missing", kind=kind, endpoint=name)


def load_llm_config(
    path: str | None = None,
    *,
    settings=None,
    env: dict[str, str] | None = None,
) -> LLMConfig:
    """Load the effective LLM config.

    ``settings`` / ``env`` are test seams: pass an ``LLMSettings`` instance and
    an explicit environ mapping to avoid touching the process environment.
    Missing TOML falls back to the §6 legacy mapping; an explicitly provided
    path that does not exist only logs ``llm_config_missing``.
    """
    st = settings if settings is not None else _app_config.llm
    environ = dict(os.environ if env is None else env)

    cfg = _builtin_config(st)

    resolved = (
        path
        or environ.get("YINYUE_LLM_CONFIG")
        or getattr(st, "llm_config_path", "configs/llm.toml")
    )
    toml_path = Path(str(resolved))
    explicit_path = bool(path) or bool(environ.get("YINYUE_LLM_CONFIG"))
    if toml_path.is_file():
        with toml_path.open("rb") as fh:
            raw = tomllib.load(fh)
        cfg = _merge_toml(cfg, raw)
    elif explicit_path:
        log.warning("llm_config_missing", path=str(toml_path))

    cfg, applied = _legacy_overlay(st, cfg)
    _key = tuple(sorted(applied)) if applied else ()
    if _key and _key not in _LEGACY_WARNED:
        _LEGACY_WARNED.add(_key)
        log.warning("llm_legacy_env_deprecated", keys=list(_key))

    cfg = _apply_env_overrides(cfg, environ)
    _validate(cfg)
    return cfg
