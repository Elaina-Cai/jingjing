"""Unified LLM call accounting.

Owns the single ``llm_call`` row.  The legacy event names (``llm_tool_call`` /
``llm_vision_call`` / ``llm_stream_done``) were removed in WP4 after a consumer
grep, so this is now the only LLM accounting line (施工单 §4.5).

Discipline:
* one row per attempt (success / failure / skipped);
* missing usage stays ``None`` -- never an invented estimate;
* no api_key / Authorization / raw base64 ever reaches the event.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import structlog
from safelog import warn

LLM_CALL_EVENT = "llm_call"

log = structlog.get_logger()


def log_llm_call(
    *,
    call_kind: str,
    provider: str = "",
    model: str = "",
    agent: str = "",
    task: str = "",
    endpoint: str | None = None,
    provider_kind: str = "openai_compat",
    tokens: dict[str, Any] | None = None,
    duration_ms: int | None = None,
    attempt: int = 0,
    fallback_from: str | None = None,
    error: str | None = None,
    route_source: str = "legacy",
    skipped: bool = False,
    cost: dict[str, Any] | None = None,
) -> None:
    """Emit one unified ``llm_call`` row."""
    log.info(
        LLM_CALL_EVENT,
        endpoint=provider or None if endpoint is None else endpoint,
        provider=provider or None,
        provider_kind=provider_kind,
        model=model or None,
        agent=agent or None,
        task=task or call_kind,
        call_kind=call_kind,
        tokens=tokens,
        duration_ms=duration_ms,
        attempt=attempt,
        fallback_from=fallback_from,
        error=error,
        route_source=route_source,
        skipped=skipped,
        cost=cost,
    )



@dataclass(frozen=True)
class PriceRule:
    """One meter's unit price.  ``per_mtok`` counts per 1,000,000 tokens."""

    meter: str                      # input | output | cache_hit | cache_miss
    per_mtok: float | None = None
    unit: str = "mtok"


@dataclass(frozen=True)
class PriceSchedule:
    """Rules for one service tier.  Time windows stay TODO(owner)."""

    rules: tuple[PriceRule, ...] = ()
    time_windows: tuple[dict[str, Any], ...] = ()   # TODO(owner): 时段/星期计价

    def rule(self, meter: str) -> PriceRule | None:
        for rule in self.rules:
            if rule.meter == meter:
                return rule
        return None


@dataclass(frozen=True)
class PriceTable:
    schedules: dict[str, PriceSchedule] = field(default_factory=dict)
    default_tier: str = "standard"

    def schedule(self, tier: str | None = None) -> PriceSchedule | None:
        name = tier or self.default_tier
        return self.schedules.get(name) or self.schedules.get(self.default_tier)


@dataclass(frozen=True)
class PriceSnapshot:
    currency: str = ""
    table: PriceTable = field(default_factory=PriceTable)
    source: str = ""
    captured_at: str = ""
    basis: str = "marginal"


@dataclass(frozen=True)
class Charge:
    currency: str = ""
    amount: float | None = None
    known_amount: bool = False
    missing: tuple[str, ...] = ()
    lines: tuple[dict[str, Any], ...] = ()


def price_snapshot_from_dict(pricing: dict[str, Any] | None) -> PriceSnapshot | None:
    """Endpoint ``pricing`` dict -> snapshot.  Missing/empty pricing -> None."""
    if not pricing:
        return None
    currency = str(pricing.get("currency", "") or "")
    input_price = _as_price(pricing.get("input_per_mtok"))
    output_price = _as_price(pricing.get("output_per_mtok"))
    cache_hit_price = _as_price(pricing.get("cache_hit_per_mtok"))
    if cache_hit_price is None and input_price is not None:
        discount = _as_price(pricing.get("cache_discount"))
        if discount is not None:
            cache_hit_price = input_price * discount
    cache_miss_price = _as_price(pricing.get("cache_miss_per_mtok"))
    rules = []
    for meter, price in (
        ("input", input_price),
        ("output", output_price),
        ("cache_hit", cache_hit_price),
        ("cache_miss", cache_miss_price),
    ):
        if price is not None:
            rules.append(PriceRule(meter=meter, per_mtok=price))
    return PriceSnapshot(
        currency=currency,
        table=PriceTable(schedules={"standard": PriceSchedule(rules=tuple(rules))}),
        source="endpoint.pricing",
    )


def _as_price(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        warn("llm/meter.py:150 (TypeError, ValueError)", err=repr(__import__("sys").exc_info()[1]))
        return None


def charge_for_usage(usage: Any, snapshot: PriceSnapshot) -> Charge:
    """Simplified charge: input/output per-mtok + cache hit/miss.

    Missing usage or missing price never becomes 0.0: the whole ``amount`` stays
    ``None`` and every missing meter/price is listed in ``missing``.
    """
    schedule = snapshot.table.schedule()
    missing: list[str] = []
    lines: list[dict[str, Any]] = []
    total = 0.0

    def _bill(meter: str, tokens: int | None) -> None:
        nonlocal total
        if tokens is None:
            missing.append(f"{meter}_tokens")
            return
        if tokens <= 0:
            return
        rule = schedule.rule(meter) if schedule is not None else None
        if rule is None or rule.per_mtok is None:
            missing.append(f"{meter}_price")
            return
        amount = tokens / 1_000_000 * rule.per_mtok
        total += amount
        lines.append({"meter": meter, "tokens": tokens, "per_mtok": rule.per_mtok, "amount": amount})

    prompt = getattr(usage, "prompt_tokens", None)
    completion = getattr(usage, "completion_tokens", None)
    cache_hit = getattr(usage, "cache_hit_tokens", None)
    if prompt is None:
        missing.append("input_tokens")
    else:
        hit = cache_hit if isinstance(cache_hit, int) else 0
        hit = max(min(hit, prompt), 0)
        if hit > 0 and schedule is not None and schedule.rule("cache_hit") is not None:
            _bill("cache_hit", hit)
            rest = prompt - hit
        else:
            rest = prompt
        _bill("cache_miss" if (schedule is not None and schedule.rule("cache_miss") is not None) else "input", rest)
    _bill("output", completion)

    known = not missing
    return Charge(
        currency=snapshot.currency,
        amount=(round(total, 8) if known else None),
        known_amount=known,
        missing=tuple(missing),
        lines=tuple(lines),
    )


def compute_cost(usage: Any, pricing: dict[str, Any] | None) -> dict[str, Any] | None:
    """``None`` when pricing is absent; otherwise ``{currency, amount, missing, lines}``.

    Any missing usage meter or price keeps ``amount=None`` and lists the gap --
    never 0.0 (施工单 §4.5).
    """
    snapshot = price_snapshot_from_dict(pricing)
    if snapshot is None:
        return None
    charge = charge_for_usage(usage, snapshot)
    return {
        "currency": charge.currency,
        "amount": charge.amount,
        "missing": list(charge.missing),
        "lines": [dict(line) for line in charge.lines],
    }

__all__ = [
    "LLM_CALL_EVENT",
    "log_llm_call",
    "usage_to_dict",
    "PriceRule",
    "PriceSchedule",
    "PriceTable",
    "PriceSnapshot",
    "Charge",
    "price_snapshot_from_dict",
    "charge_for_usage",
    "compute_cost",
]

def usage_to_dict(usage: Any) -> dict[str, Any] | None:
    """``Usage`` dataclass (or SDK usage object) -> unified event tokens dict."""
    if usage is None:
        return None
    prompt = getattr(usage, "prompt_tokens", None)
    completion = getattr(usage, "completion_tokens", None)
    total = getattr(usage, "total_tokens", None)
    if total is None:
        resolver = getattr(usage, "resolved_total", None)
        if callable(resolver):
            total = resolver()
    return {
        "prompt": prompt,
        "completion": completion,
        "total": total,
        "reasoning": getattr(usage, "reasoning_tokens", None),
        "cache_hit": getattr(usage, "cache_hit_tokens", None),
        "cache_miss": getattr(usage, "cache_miss_tokens", None),
    }
