r"""Print the effective LLM route table / key status / budgets (no network).

Run:
    .\.venv\Scripts\python.exe scripts\doctor_llm.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.config.settings import config as app_config  # noqa: E402
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001 - 控制台编码不该挡住 doctor
        pass


from llm.config import (  # noqa: E402
    EndpointSpec,
    LLMConfig,
    LLMConfigError,
    load_llm_config,
)

_TICK = "\u2713"
_CROSS = "\u2717"


def _resolve_key(spec: EndpointSpec, settings) -> str:
    """Literal key > process env > LLMSettings field (legacy .env keys)."""
    if spec.api_key:
        return str(spec.api_key)
    if not spec.api_key_env:
        return ""
    value = str(os.environ.get(spec.api_key_env, "") or "")
    if not value:
        value = str(getattr(settings, spec.api_key_env.lower(), "") or "")
    return value


def _fingerprint(value: str) -> str:
    text = str(value or "")
    if not text:
        return ""
    return "****" + (text[-4:] if len(text) >= 4 else text)


def _fmt_endpoint(cfg: LLMConfig, name: str) -> str:
    spec = cfg.endpoints[name]
    caps = spec.capabilities
    tools = _TICK if caps.tools else _CROSS
    vision = _TICK if caps.vision else _CROSS
    ctx = caps.context_window if caps.context_window else "n/a"
    price = "n/a"
    if spec.pricing:
        currency = str(spec.pricing.get("currency", "") or "")
        input_price = spec.pricing.get("input_per_mtok")
        output_price = spec.pricing.get("output_per_mtok")
        if input_price is not None and output_price is not None:
            price = f"{currency}{input_price}/{output_price} per Mtok"
    return f"{name}/{spec.model}  [tools {tools} vision {vision} ctx {ctx}]  价格: {price}"


def _fmt_price(spec: EndpointSpec) -> str:
    """Per-endpoint unit prices; missing stays n/a (never 0.0)."""
    pricing = spec.pricing or {}
    if not pricing:
        return "n/a"
    currency = str(pricing.get("currency", "") or "")

    def _price(key: str) -> str:
        value = pricing.get(key)
        return "n/a" if value is None else f"{currency}{value}/Mtok"

    cache_hit = pricing.get("cache_hit_per_mtok")
    if cache_hit is None and pricing.get("cache_discount") is not None:
        input_price = pricing.get("input_per_mtok")
        if input_price is not None:
            cache_hit = float(input_price) * float(pricing["cache_discount"])
    cache_text = "n/a" if cache_hit is None else f"{currency}{cache_hit}/Mtok"
    return f"input {_price('input_per_mtok')}  output {_price('output_per_mtok')}  cache_hit {cache_text}"


def build_report(cfg: LLMConfig, settings=None) -> str:
    settings = settings if settings is not None else app_config.llm
    lines = ["LLM 路由表"]
    lines.append(f"  {'default':<16} → {_fmt_endpoint(cfg, cfg.default_endpoint)}")
    for agent in sorted(cfg.agent_endpoints):
        route = cfg.agent_endpoints[agent]
        extra = ""
        if route.only_kind or route.on_circuit_open != "error":
            extra = f"  (only_kind={route.only_kind or '*'}, on_circuit_open={route.on_circuit_open})"
        lines.append(f"  {agent:<16} → {_fmt_endpoint(cfg, route.endpoint)}{extra}")
    lines.append(f"  {'fallback':<16} → " + " → ".join(cfg.fallback))

    key_parts: list[str] = []
    missing: list[str] = []
    for name in sorted(cfg.endpoints):
        spec = cfg.endpoints[name]
        value = _resolve_key(spec, settings)
        if value:
            key_parts.append(f"{name} {_TICK}({_fingerprint(value)})")
        elif spec.api_key_env:
            key_parts.append(f"{name} {_CROSS} 未配置")
            missing.append(name)
        else:
            key_parts.append(f"{name} (免鉴权)")
    lines.append("密钥:  " + "   ".join(key_parts))

    budgets = cfg.budgets or {}
    lines.append(
        f"预算:  daily {budgets.get('daily_tokens')} tokens（已用 0，仅本次进程）；"
        f"per_agent {budgets.get('per_agent')}"
    )
    for name in cfg.fallback:
        if name in missing:
            lines.append(f"警告:  {name} 未配置，fallback 将跳过")
    for kind in sorted(cfg.kind_lead):
        lines.append(f"lead:  kind={kind} → {list(cfg.kind_lead[kind])}")

    lines.append("单价:")
    for name in sorted(cfg.endpoints):
        lines.append(f"  {name:<16} {_fmt_price(cfg.endpoints[name])}")
    return "\n".join(lines)


def main() -> int:
    try:
        cfg = load_llm_config()
    except LLMConfigError as exc:
        print(f"LLM 配置错误: {exc}")
        return 1
    print(build_report(cfg))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
