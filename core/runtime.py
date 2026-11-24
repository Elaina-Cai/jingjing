from __future__ import annotations

import contextvars
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterator
from safelog import warn

# The active request/event context.  asyncio tasks created inside a handler
# inherit this, so legacy helpers that still call ``get_repo()`` can resolve
# the correct account without changing every call site at once.
_CURRENT_ACCOUNT: contextvars.ContextVar["AccountContext | None"] = contextvars.ContextVar(
    "yinyue_account_context", default=None,
)


def current_account_context() -> "AccountContext | None":
    return _CURRENT_ACCOUNT.get()


def set_current_account_context(ctx: "AccountContext | None"):
    return _CURRENT_ACCOUNT.set(ctx)


def reset_current_account_context(token) -> None:
    _CURRENT_ACCOUNT.reset(token)


@dataclass(frozen=True, order=True)
class AccountKey:
    platform: str
    account_id: str

    def __str__(self) -> str:
        return f"{self.platform}:{self.account_id}"


@dataclass
class AccountContext:
    """Everything the runtime needs to act for one platform account.

    ``repo`` is a ``ScopedRepository`` facade so existing agents/tools can keep
    using a single repository object while messages/contacts/analysis resolve
    to this account's database and personas/memories/sessions resolve to the
    shared meta database.
    """

    platform: str
    account_id: str
    role: str
    config: Any
    account_repo: Any
    meta_repo: Any
    provider: Any = None
    display_name: str = ""
    _repo: Any = field(default=None, repr=False)
    _tools: Any = field(default=None, repr=False)

    @property
    def key(self) -> AccountKey:
        return AccountKey(self.platform, self.account_id)

    @property
    def repo(self):
        if self._repo is None:
            from db.scoped_repo import ScopedRepository
            self._repo = ScopedRepository(
                self.account_repo,
                self.meta_repo,
                platform=self.platform,
                account_id=self.account_id,
            )
        return self._repo

    @property
    def is_yinyue(self) -> bool:
        return self.role == "yinyue"

    @property
    def tools(self):
        return self._tools

    def set_tools(self, registry):
        self._tools = registry
        return registry

    def __str__(self) -> str:
        return f"{self.platform}:{self.account_id}"


class RuntimeRegistry:
    """Owns all platform/account contexts for the process."""

    def __init__(
        self,
        contexts: list[AccountContext],
        *,
        meta_repo: Any,
        account_db_manager: Any = None,
        provider_manager: Any = None,
        llm: Any = None,
        embedding: Any = None,
        vector_store: Any = None,
        unified_query: Any = None,
        event_bus: Any = None,
        config: Any = None,
    ):
        self._contexts: dict[AccountKey, AccountContext] = {c.key: c for c in contexts}
        self.meta_repo = meta_repo
        self.account_db_manager = account_db_manager
        self.provider_manager = provider_manager
        self.llm = llm
        self.embedding = embedding
        self.vector_store = vector_store
        self.unified_query = unified_query
        self.event_bus = event_bus
        self.config = config
        self._default_key: AccountKey | None = None
        if self._contexts:
            yinyue = next((c for c in contexts if c.is_yinyue), contexts[0])
            self._default_key = yinyue.key
        self._tool_lock = threading.RLock()

    @classmethod
    def build(
        cls,
        *,
        config: Any,
        account_db_manager: Any,
        meta_repo: Any,
        provider_manager: Any = None,
        llm: Any = None,
        embedding: Any = None,
        vector_store: Any = None,
        unified_query: Any = None,
        event_bus: Any = None,
    ) -> "RuntimeRegistry":
        contexts: list[AccountContext] = []
        for ac in config.all_account_configs():
            account_repo = account_db_manager.get_repo(ac.platform, ac.account_id)
            if account_repo is None:
                account_repo = account_db_manager.register(
                    ac.platform, ac.account_id, ac.db_path,
                )
            provider = None
            if provider_manager is not None:
                provider = provider_manager.get_by_account(ac.platform, ac.account_id)
            contexts.append(AccountContext(
                platform=ac.platform,
                account_id=ac.account_id,
                role=getattr(ac, "role", "yinyue"),
                config=ac,
                account_repo=account_repo,
                meta_repo=meta_repo,
                provider=provider,
            ))
        return cls(
            contexts,
            meta_repo=meta_repo,
            account_db_manager=account_db_manager,
            provider_manager=provider_manager,
            llm=llm,
            embedding=embedding,
            vector_store=vector_store,
            unified_query=unified_query,
            event_bus=event_bus,
            config=config,
        )

    def _context_from_config(self, ac: Any) -> AccountContext:
        account_repo = self.account_db_manager.get_repo(ac.platform, ac.account_id)
        if account_repo is None:
            account_repo = self.account_db_manager.register(
                ac.platform, ac.account_id, ac.db_path,
            )
        provider = None
        if self.provider_manager is not None:
            provider = self.provider_manager.get_by_account(ac.platform, ac.account_id)
            if provider is None:
                from core.execution.provider import create_provider
                provider = create_provider(ac)
                if provider is not None:
                    self.provider_manager.register(provider)
        return AccountContext(
            platform=ac.platform,
            account_id=ac.account_id,
            role=getattr(ac, "role", "yinyue"),
            config=ac,
            account_repo=account_repo,
            meta_repo=self.meta_repo,
            provider=provider,
        )

    def upsert_account(self, ac: Any) -> AccountContext:
        """Add or replace one account context (hot-plug)."""
        ctx = self._context_from_config(ac)
        self._contexts[ctx.key] = ctx
        if self._default_key is None or ctx.is_yinyue:
            self._default_key = ctx.key
        return ctx

    def remove_account(self, platform: str, account_id: str) -> None:
        key = AccountKey(platform, account_id)
        self._contexts.pop(key, None)
        if self._default_key == key:
            first = next(iter(self._contexts.values()), None)
            self._default_key = first.key if first is not None else None

    def load_registered_accounts(self) -> "RuntimeRegistry":
        """Merge accounts persisted in MetaRegistry into the runtime."""
        try:
            rows = self.meta_repo.account_registry.list_all()
        except Exception:
            warn("core/runtime.py:213 Exception", err=repr(__import__("sys").exc_info()[1]))
            return self
        for row in rows:
            if self.get(row.platform, row.account_id) is not None:
                continue
            ac = None
            if self.config is not None:
                ac = self.config.get_account_config(row.platform, row.account_id)
            if ac is None:
                from core.config.settings import AccountConfig
                ac = AccountConfig(
                    account_id=row.account_id,
                    platform=row.platform,
                    role=getattr(row, "role", "yinyue") or "yinyue",
                )
                if self.config is not None:
                    ac.resolve_paths(self.config.paths)
            self.upsert_account(ac)
        return self

    def reload_from_config(self, config: Any = None) -> "RuntimeRegistry":
        """Rebuild contexts from config without restarting the process."""
        cfg = config or self.config
        if cfg is None:
            return self
        contexts = [self._context_from_config(ac) for ac in cfg.all_account_configs()]
        self._contexts = {c.key: c for c in contexts}
        if contexts:
            yinyue = next((c for c in contexts if c.is_yinyue), contexts[0])
            self._default_key = yinyue.key
        else:
            self._default_key = None
        return self.load_registered_accounts()

    def all_contexts(self) -> list[AccountContext]:
        return list(self._contexts.values())

    def by_platform(self, platform: str) -> list[AccountContext]:
        return [c for c in self._contexts.values() if c.platform == platform]

    def get(self, platform: str, account_id: str) -> AccountContext | None:
        if not platform or not account_id:
            return None
        return self._contexts.get(AccountKey(platform, account_id))

    def require(self, platform: str, account_id: str) -> AccountContext:
        ctx = self.get(platform, account_id)
        if ctx is None:
            raise KeyError(f"account context not registered: {platform}:{account_id}")
        return ctx

    @property
    def default(self) -> AccountContext | None:
        if self._default_key is None:
            return None
        return self._contexts.get(self._default_key)

    @property
    def default_key(self) -> AccountKey | None:
        return self._default_key

    def set_default(self, platform: str, account_id: str) -> None:
        self._default_key = AccountKey(platform, account_id)

    def context_for_event(self, event: Any) -> AccountContext | None:
        platform = getattr(event, "platform", "") or ""
        account_id = getattr(event, "account_id", "") or ""
        return self.get(platform, account_id) or self.default

    @contextmanager
    def activate(self, ctx: AccountContext | None) -> Iterator[AccountContext | None]:
        token = _CURRENT_ACCOUNT.set(ctx)
        try:
            yield ctx
        finally:
            _CURRENT_ACCOUNT.reset(token)

    def tools_for(self, ctx: AccountContext | None):
        """Return an account-scoped ToolRegistry, building it lazily."""
        if ctx is None:
            return None
        if ctx.tools is not None:
            return ctx.tools
        with self._tool_lock:
            if ctx.tools is not None:
                return ctx.tools
            from agents.tools import ToolRegistry
            try:
                send_enabled = bool(getattr(ctx.config, "send_enabled", False))
                registry = ToolRegistry.create_default(
                    repo=ctx.repo,
                    meta_repo=ctx.meta_repo or self.meta_repo,
                    vector_store=self.vector_store,
                    embedding_client=self.embedding,
                    event_bus=self.event_bus,
                    llm=self.llm,
                    provider_manager=self.provider_manager,
                    unified_query=self.unified_query,
                    send_enabled=send_enabled,
                    platform=ctx.platform,
                )
            except Exception:
                # Keep the runtime usable even when optional tool deps are
                # missing (e.g. embeddings or vector store not configured).
                warn("core/runtime.py:314 Exception", err=repr(__import__("sys").exc_info()[1]))
                registry = ToolRegistry.create_default(
                    repo=ctx.repo,
                    meta_repo=ctx.meta_repo or self.meta_repo,
                    llm=self.llm,
                    provider_manager=self.provider_manager,
                    unified_query=self.unified_query,
                    send_enabled=bool(getattr(ctx.config, "send_enabled", False)),
                    platform=ctx.platform,
                )
            return ctx.set_tools(registry)

    def __len__(self) -> int:
        return len(self._contexts)

    def __contains__(self, key: AccountKey) -> bool:
        return key in self._contexts


__all__ = [
    "AccountKey",
    "AccountContext",
    "RuntimeRegistry",
    "current_account_context",
    "set_current_account_context",
    "reset_current_account_context",
]
