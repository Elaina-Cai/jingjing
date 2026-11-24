"""Adapter registry + entry-point plugins (WP6).

Entry point group: ``yinyue.llm_providers``.  A plugin target is an adapter
class (or factory) declaring ``KIND`` (or module-level ``ADAPTER_KIND``) and
``ADAPTER_CONTRACT_VERSION``.  A version mismatch is rejected with a
human-readable message and never registered.

Import-light on purpose: importing ``llm.adapters.base`` (config.py does) must
not pull the OpenAI SDK.  Built-in ``openai_compat`` is imported lazily.
"""

from __future__ import annotations

import structlog
from importlib.metadata import entry_points

from llm.adapters.contract import (
    ADAPTER_CONTRACT_VERSION,
    AdapterContractError,
    ContractTransport,
    FakeTransport,
    assert_adapter_contract,
    run_adapter_contract,
)
from safelog import warn

log = structlog.get_logger()

BUILTIN_KINDS = ("openai_compat", "anthropic", "openai_responses")


class AdapterPluginError(RuntimeError):
    """Raised when a plugin cannot be registered (shape / kind problem)."""


_ADAPTERS: dict[str, type] = {}
_PLUGINS_LOADED = False
_LOADED: list[dict] = []
_REJECTED: list[dict] = []


def register_adapter(kind, adapter_cls, *, contract_version=None, source: str = "builtin") -> None:
    """Register one adapter class under ``kind`` (contract-checked)."""
    if not isinstance(kind, str) or not kind:
        raise AdapterPluginError("适配器 kind 必须是非空字符串")
    version = contract_version
    if version is None:
        version = getattr(adapter_cls, "ADAPTER_CONTRACT_VERSION", None)
    if version != ADAPTER_CONTRACT_VERSION:
        raise AdapterContractError(
            f"适配器 {kind!r} 契约版本 {version!r} 与当前 {ADAPTER_CONTRACT_VERSION} 不匹配，拒绝加载"
        )
    _ADAPTERS[kind] = adapter_cls


def _iter_entry_points():
    try:
        return list(entry_points(group="yinyue.llm_providers"))
    except TypeError:  # pragma: no cover - Python < 3.10
        warn("llm/adapters/__init__.py:58 TypeError", err=repr(__import__("sys").exc_info()[1]))
        return list(entry_points().get("yinyue.llm_providers", []))


def load_plugins(*, force: bool = False) -> list[dict]:
    """Discover ``yinyue.llm_providers`` entry points; idempotent by default."""
    global _PLUGINS_LOADED
    if _PLUGINS_LOADED and not force:
        return list(_LOADED)
    _PLUGINS_LOADED = True
    if force:
        _LOADED.clear()
    for entry_point in _iter_entry_points():
        try:
            target = entry_point.load()
            kind = (
                getattr(target, "KIND", None)
                or getattr(target, "ADAPTER_KIND", None)
                or entry_point.name
            )
            register_adapter(str(kind), target, source=f"entry_point:{entry_point.name}")
            _LOADED.append({"entry_point": entry_point.name, "kind": str(kind)})
        except Exception as exc:  # noqa: BLE001 - 插件不许拖垮主进程
            _REJECTED.append({"entry_point": entry_point.name, "error": str(exc)[:200]})
            log.warning("llm_plugin_rejected", entry_point=entry_point.name, error=str(exc)[:200])
    return list(_LOADED)


def known_kinds() -> frozenset[str]:
    load_plugins()
    return frozenset(BUILTIN_KINDS) | frozenset(_ADAPTERS)


def get_adapter_class(kind: str):
    """Return the adapter class for a config ``kind`` (None when unknown)."""
    load_plugins()
    adapter_cls = _ADAPTERS.get(kind)
    if adapter_cls is not None:
        return adapter_cls
    if kind == "openai_compat":
        from llm.adapters.openai_compat import OpenAICompatAdapter

        return OpenAICompatAdapter
    if kind == "anthropic":
        from llm.adapters.anthropic import AnthropicAdapter

        return AnthropicAdapter
    if kind == "openai_responses":
        from llm.adapters.openai_responses import OpenAIResponsesAdapter

        return OpenAIResponsesAdapter
    return None


def loaded_plugins() -> list[dict]:
    return list(_LOADED)


def rejected_plugins() -> list[dict]:
    return list(_REJECTED)


def reset_plugins() -> None:
    """Test seam: forget everything (including previously loaded plugins)."""
    global _PLUGINS_LOADED
    _ADAPTERS.clear()
    _LOADED.clear()
    _REJECTED.clear()
    _PLUGINS_LOADED = False


__all__ = [
    "ADAPTER_CONTRACT_VERSION",
    "AdapterContractError",
    "AdapterPluginError",
    "BUILTIN_KINDS",
    "ContractTransport",
    "FakeTransport",
    "assert_adapter_contract",
    "get_adapter_class",
    "known_kinds",
    "load_plugins",
    "loaded_plugins",
    "register_adapter",
    "rejected_plugins",
    "reset_plugins",
    "run_adapter_contract",
]
