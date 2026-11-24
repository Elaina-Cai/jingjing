"""MemoryHub: the unified automatic memory-read entry for Phase 5.

Physical stores stay split by access pattern, but all recall kinds go through
one API and one PrincipalContext filter:

    hub.recall(principal, query, kinds=["personal", "history", "meme", ...])

**One kind may own several providers**（2026-10-16，梗进按需记忆的接线）：
``meme`` 这一格同时有 ②（世界模型投影，:class:`MemeMemoryProvider`）与
层3（web 梗库索引，:class:`WebMemeMemoryProvider`）。旧实现是 ``kind -> provider``
的单值字典，后注册的把先注册的**静默顶掉**（``WebMemeMemoryProvider`` 一上线，
② 的梗就再也召不回来，而且没有任何读数能看出这件事）。现在注册表是
``kind -> [provider, ...]``：同 kind 多 provider **全部作答**，结果合并后统一排序、
统一截断；``limit`` 与 ``detail`` 的口径见 :meth:`MemoryHub.recall`。
"""

from __future__ import annotations

import threading
from dataclasses import replace
from typing import Any, Iterable

from core.memory.contracts import MemoryItem
from core.memory.beliefs import BeliefMemoryProvider
from core.memory.providers import (
    CoreMemoryProvider,
    MemeMemoryProvider,
    HistoryMemoryProvider,
    KnowledgeMemoryProvider,
    PersonMemoryProvider,
    PersonalMemoryProvider,
    TopicMemoryProvider,
    WorkingMemoryProvider,
    ClaimMemoryProvider,
    WebMemeMemoryProvider,
)
from core.memory.terminal_explicit import TerminalExplicitMemoryProvider
from safelog import warn

#: 取数明细（hits/errors）的默认上限；调用方记 trace 时按这个数截断。
RECALL_DETAIL_LIMIT: int = 20

#: 同 kind 有 N 家 provider 时，每家至少取回这么多条再合并——
#: 免得 provider A 的 8 条把 provider B 挤到候选池外（② 与层3 的 importance
#: 分布不同，按 limit 均分会把其中一家系统性饿死）。
PER_PROVIDER_MIN: int = 5

#: ⑤ 的 kind 闭集（core/context/scene_read.py::MEMORY_KINDS）→ hub 的 provider kind。
#: N1 说的是**用户语义**（fact/episode/person/meme/quote），hub 注册的是
#: **物理取数口**（personal/person/history/meme/...）：两者不是一套词。
#: 这张表是那一次翻译的**唯一**处；没进表的 kind（未接线的）查询直接空手而归，
#: **不回落**到 personal——回落会把“这一格没接线”伪装成“查过了没有”。
#: **不替换任何既有 kind 名**：kinds=["personal"] 之类的旧调用照旧只查自己那一格。
KIND_SOURCES: dict[str, tuple[str, ...]] = {
    # 2026-10-17：knowledge_docs（web 检索落库）也是「长期事实」的一种读法，
    # N1 说 fact 时既查每日事实库也查知识库；只加不改（personal 仍在第一位）。
    "fact": ("belief", "personal", "knowledge"),
    "episode": ("history", "recall_chain"),  # 对话回放 + P3 召回链窗口（同一批行的两种读法）
    "person": ("person", "claim"),      # 逐人卡 + 人设记忆
    # 台账里的格子就是“某人的某个属性”（籍贯/年龄/学校…），所以跟 `person` 同路。
    # "不归 fact" 是刻意的：台账是**谁说的**（信任级），不是**已验证的事实**，
    # 与 personal（每日事实库）混一个 kind 会把两种信任度混成一坑。
    "meme": ("meme",),          # ② 世界模型投影 + 层3 web 梗库（两家都答）
    "quote": ("history",),      # 原话回放：与 episode 同源（现状口径）
    # ①³ 写侧随手记（task-15）：note_recall.NoteRecallProvider 只答这一个 kind。
    # 只加不改：既有取值一字未动（见 core/memory/note_recall.py 模块头）。
    "notes": ("notes",),        # 原话 + span 的随手记（verbatim，带出处）
}


def _rank_key(query: str):
    """排序键：有 query 时相关命中优先，其余桶沿用旧的 importance→confidence→时间。

    2026-11-17（B2）：以前所有条目只按 importance 排，一条「重要但和这句话无关」
    的记忆可以把真正相关的那条挤在截断线外。query 为空时行为与旧版逐字一致。
    """
    if not query:
        return lambda item: (
            float(item.importance or 0.0),
            float(item.confidence or 0.0),
            item.occurred_at.timestamp() if item.occurred_at else 0.0,
        )
    from core.memory.providers import _match

    return lambda item: (
        1 if _match(query, item.content) else 0,
        float(item.importance or 0.0),
        float(item.confidence or 0.0),
        item.occurred_at.timestamp() if item.occurred_at else 0.0,
    )


class MemoryHub:

    def __init__(
        self,
        providers: Any = None,
        *,
        verify_evidence: bool = False,
    ):
        """``providers`` 收三种形态：``None`` / 单个 provider / 映射。

        映射的**值**也收单 provider 或 provider 序列（同 kind 多 provider），
        这样 ``MemoryHub({"meme": [a, b]})`` 与 ``hub.register`` 走同一条路。
        """
        self.verify_evidence = bool(verify_evidence)
        self._providers: dict[str, list[Any]] = {}
        if providers is None:
            return
        if isinstance(providers, dict):
            for kind, value in providers.items():
                for provider in self._iter_values(value):
                    self._attach(str(kind), provider)
            return
        for provider in self._iter_values(providers):
            self.register(provider)

    def _with_evidence_state(
        self,
        item: MemoryItem,
        principal: Any,
    ) -> MemoryItem | None:
        refs = tuple(str(ref) for ref in item.source_event_ids if str(ref))
        if not refs or not self.verify_evidence:
            return item
        platform = str(item.platform or getattr(principal, "platform", "") or "")
        account_id = str(
            item.bot_account_id
            or getattr(principal, "bot_account_id", "")
            or ""
        )
        chat_id = str(item.chat_id or getattr(principal, "chat_id", "") or "")
        if not all((platform, account_id, chat_id)):
            quality = "unknown"
        else:
            from core.context.cognitive_commit import Scope
            from core.memory import belief_commit

            scope = Scope(platform, account_id, chat_id)
            states = []
            for ref in refs:
                try:
                    states.append(belief_commit.evidence_state(ref, scope))
                except Exception:
                    states.append("unknown")
            if states and all(state == "dead" for state in states):
                return None
            quality = "alive" if "alive" in states else "unknown"
        return replace(
            item,
            metadata={**item.metadata, "evidence_state": quality},
        )

    # ------------------------------------------------------------- 注册表
    @staticmethod
    def _iter_values(value: Any) -> list[Any]:
        if value is None:
            return []
        if isinstance(value, (list, tuple, set, frozenset)):
            return [item for item in value if item is not None]
        return [value]

    @staticmethod
    def _kind_of(provider: Any, fallback: str = "") -> str:
        return str(getattr(provider, "kind", "") or fallback or "")

    def _attach(self, kind: str, provider: Any) -> None:
        if not kind:
            raise ValueError("memory provider requires a kind")
        self._providers.setdefault(kind, []).append(provider)

    def register(self, provider: Any) -> None:
        """Append one provider; a second provider for the same kind is kept."""
        self._attach(self._kind_of(provider), provider)

    def register_many(self, providers: Iterable[Any]) -> None:
        for provider in providers or ():
            self.register(provider)

    def providers_for(self, kind: str) -> tuple[Any, ...]:
        return tuple(self._providers.get(str(kind), ()))

    def provider_of(self, kind: str) -> Any:
        """第一个 provider（兼容旧读法）。"""
        found = self.providers_for(kind)
        return found[0] if found else None

    def kinds(self) -> tuple[str, ...]:
        return tuple(self._providers)

    # --------------------------------------------------------------- 召回
    def _resolve_requests(
        self,
        kinds: Iterable[str] | None,
        *,
        max_items: int,
        overrides: dict[str, int],
    ) -> list[tuple[str, str, int]]:
        """把调用方的 ``kinds`` 展开成 ``(N1 的 kind, provider kind, 取数上限)``。

        ``kinds=None`` ⇒ 所有注册过的 provider kind 各问一次（旧语义）。N1 的 kind 闭集
        （``KIND_SOURCES`` 里有）⇒ 翻成 provider kind；其中也注册过同名的（meme）不重复问。
        没登记的 kind 也进候选表——空手而归是**有读数**的事实（``hits[kind] = 0``），
        比静默丢掉更能暴露“这一格没接线”。
        """
        if kinds is None:
            wanted = list(self._providers)
        else:
            wanted = [str(kind) for kind in kinds]
        out: list[tuple[str, str, int]] = []
        for kind_asked in wanted:
            order: list[str] = []
            for provider_kind in (kind_asked, *self.kind_sources(kind_asked)):
                provider_kind = str(provider_kind)
                if provider_kind and provider_kind not in order:
                    order.append(provider_kind)
            for provider_kind in order:
                providers = self._providers.get(provider_kind) or ()
                if not providers:
                    continue
                ask = self._ask_for(
                    kind_asked,
                    provider_kind,
                    len(providers),
                    max_items=max_items,
                    per_kind=overrides,
                )
                out.append((kind_asked, provider_kind, ask))
        return out

    def _ask_for(
        self,
        kind: str,
        provider_kind: str,
        providers: int,
        *,
        max_items: int,
        per_kind: dict[str, int],
    ) -> int:
        """该 provider 取回多少条。

        ``per_kind[kind]`` 或 ``per_kind[provider kind]`` 给了就按它；否则取
        ``max(max_items, PER_PROVIDER_MIN * 这个 kind 的家数)``——同 kind 多家时每家都吃得到
        一份，不会因为排在后面而被前一家的 8 条挤出候选池。
        """
        override = per_kind.get(str(kind))
        if override is None:
            override = per_kind.get(str(provider_kind))
        if override is not None:
            return max(1, int(override))
        return max(int(max_items), PER_PROVIDER_MIN * max(1, int(providers)))


    def kind_sources(self, kind: str) -> tuple[str, ...]:
        """N1 \u7684 kind \u95ed\u96c6 \u2192 \u672c hub \u7684 provider kind\uff08\u672a\u767b\u8bb0 = \u7a7a\u5143\u7ec4\uff0c\u4e0d\u56de\u843d\uff09\u3002"""
        return tuple(KIND_SOURCES.get(str(kind), ()))

    def recall(
        self,
        principal: Any,
        query: str = "",
        *,
        kinds: Iterable[str] | None = None,
        limit: int = 8,
        per_kind: dict[str, int] | None = None,
        context: Any = None,
        detail: dict[str, Any] | None = None,
    ) -> list[MemoryItem]:
        """Gather items from selected providers and apply a stable rank.

        调用口径：
        * ``kinds`` 给的是 **N1 的 kind 闭集**（fact/episode/person/meme/quote）时，
          经 ``KIND_SOURCES`` 翻成 provider kind 后再取数（一个 kind 可以映射到多家
          provider，都答）。没登记的 kind 空手而归，不回落 ``personal``；
        * ``kinds`` 给的是 provider kind（personal/history/...）时照旧直接取，
          并且**尊重调用方给的种类数**（N1 说 meme 就只问 meme，不会因为
          fact 恰好也映射到同一个 provider 就多问一份）。

        Limit 口径（同 kind 多 provider 时不静默截断某一家）：
        * ``per_kind`` 给了该 kind 的条数就按它取；没给的 kind 取
          ``max(limit, PER_PROVIDER_MIN * 家数)``——每家都取这个数，保证
          ``limit`` 截断前每一家都有机会进候选（② 与层3 的 importance
          分布不同，按 limit 均分会把其中一家系统性饿死）；
        * 同一个 provider 在一次召回里**只问一次**（以防一个 provider 被多个
          kind 同时命中而重复取数）；
        * 合并去重（``(kind, memory_id)`` 取 importance 高者）后统一按
          ``importance → confidence → 时间`` 排序，最后 ``[:limit]`` 收口。

        ``detail`` 是可选的审计出参（dict）：写 ``hits``（N1 的 kind → 条数）、
        ``errors``（provider 抛错的 ``kind/类名: 类型: msg``）与 ``queried``
        （实际问到的 provider kind）。**失败不再静默**——旧实现
        ``except Exception: items = []`` 把“provider 崩了”和“没查到”混成一件
        事，影子期看不出谁在掉数据。
        """
        if principal is None:
            return []
        try:
            max_items = max(1, int(limit or 1))
        except (TypeError, ValueError):
            warn("core/memory/hub.py:230 (TypeError, ValueError)", err=repr(__import__("sys").exc_info()[1]))
            max_items = 1
        overrides = {
            str(k): int(v)
            for k, v in dict(per_kind or {}).items()
            if str(k)
        }

        # (N1 的 kind, 要问的 provider kind) 两列；kinds 给的就是 provider kind
        # 时两列相同（旧调用零变化）。
        requests = self._resolve_requests(
            kinds,
            max_items=max_items,
            overrides=overrides,
        )

        gathered: list[MemoryItem] = []
        hits_detail: dict[str, int] = {}
        errors: list[str] = []
        seen: set[tuple[str, int]] = set()
        asked_kinds: list[str] = []
        for kind_asked, provider_kind, ask in requests:
            providers = self._providers.get(provider_kind) or ()
            for index, provider in enumerate(providers):
                token = (provider_kind, index)
                if token in seen:
                    continue
                seen.add(token)
                asked_kinds.append(provider_kind)
                try:
                    items = provider.recall(
                        principal,
                        query,
                        limit=ask,
                        context=context,
                    )
                except Exception as exc:  # noqa: BLE001 - 单 provider 失败不拖垮整次召回
                    warn("core/memory/hub.py:266 Exception", err=repr(exc))
                    errors.append(
                        f"{provider_kind}/{type(provider).__name__}: "
                        f"{type(exc).__name__}: {str(exc)[:120]}"
                    )
                    continue
                count = 0
                for item in items or ():
                    if isinstance(item, MemoryItem):
                        checked = self._with_evidence_state(item, principal)
                        if checked is None:
                            continue
                        gathered.append(checked)
                        count += 1
                hits_detail[kind_asked] = hits_detail.get(kind_asked, 0) + count
        if isinstance(detail, dict):
            detail["hits"] = dict(hits_detail)
            detail["errors"] = errors[:RECALL_DETAIL_LIMIT]
            detail["queried"] = asked_kinds[:RECALL_DETAIL_LIMIT]

        deduped: dict[tuple[str, str], MemoryItem] = {}
        for item in gathered:
            key = (item.kind, item.memory_id)
            previous = deduped.get(key)
            if previous is None or item.importance > previous.importance:
                deduped[key] = item

        return sorted(deduped.values(), key=_rank_key(query), reverse=True)[:max_items]


def build_default_memory_hub() -> MemoryHub:
    """Build all providers lazily; no DB is touched until recall()."""
    hub = MemoryHub(verify_evidence=True)
    hub.register(PersonalMemoryProvider())
    hub.register(HistoryMemoryProvider())
    hub.register(TerminalExplicitMemoryProvider())
    from core.memory.recall_chain import RecallChainProvider  # P3：默认 off 时零 IO

    hub.register(RecallChainProvider(verify_evidence=True))
    from core.memory.note_recall import NoteRecallProvider  # ①³：默认 off 时零 IO

    hub.register(NoteRecallProvider(verify_evidence=True))
    hub.register(TopicMemoryProvider())
    hub.register(CoreMemoryProvider())
    hub.register(PersonMemoryProvider())
    hub.register(ClaimMemoryProvider())
    hub.register(BeliefMemoryProvider(verify_evidence=True))
    hub.register(WorkingMemoryProvider())
    hub.register(KnowledgeMemoryProvider())
    # kind="meme" 两家：② 世界模型投影 + 层3 web 梗库索引（都要答，见模块头）。
    hub.register(MemeMemoryProvider())
    hub.register(WebMemeMemoryProvider())
    return hub


_hub: MemoryHub | None = None
_lock = threading.RLock()


def get_memory_hub() -> MemoryHub:
    global _hub
    with _lock:
        if _hub is None:
            _hub = build_default_memory_hub()
        return _hub


def install_memory_hub(hub: MemoryHub | None) -> None:
    global _hub
    with _lock:
        _hub = hub


def reset_memory_hub() -> None:
    global _hub
    with _lock:
        _hub = None


__all__ = [
    "MemoryHub",
    "RECALL_DETAIL_LIMIT",
    "build_default_memory_hub",
    "get_memory_hub",
    "install_memory_hub",
    "reset_memory_hub",
]
