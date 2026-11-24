"""Phase 5 E2: query-side vector recall over memory projections.

Run directly:
    python tests/test_memory_vector_recall.py
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# ---------------------------------------------------------------------------
# 数据库隔离（照 tests/test_event_chain_links.py / test_command_service_db.py）：
# 本文件会经 core.bootstrap.init_singletons() 触发 init_meta_db + run_migrations
# （bootstrap 兜底、runtime 访问器等），不隔离就把 pending 迁移落到**真实**
# meta / account 库。2026-09-29 实测：Migration 51 被本套测试落到了生产库
# （data/yinyue_meta.db + 4 个账号库，applied_at 14:58:19Z）。必须在 import core.*
# 之前设好：PathSettings 在 core.config.settings **首次**导入时就取值。
# ---------------------------------------------------------------------------
_ISO_ROOT = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "data", "_test_memory_vector_recall_iso"
)
os.makedirs(_ISO_ROOT, exist_ok=True)
os.environ["META_DB_PATH"] = os.path.join(_ISO_ROOT, "meta.db")
os.environ["PLATFORMS_DIR"] = os.path.join(_ISO_ROOT, "platforms")
os.environ["DATA_DIR"] = os.path.join(_ISO_ROOT, "data")

from core.config.settings import config as _config  # noqa: E402

# 守卫：env 只在 core.config.settings 首次导入前设好才有效，否则真实路径已经
# 固化进 config。这里硬失败，绝不静默把迁移/写操作落到生产库。
assert os.path.realpath(_config.paths.meta_db_path) != os.path.realpath(
    os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "data", "yinyue_meta.db"
    )
), f"测试数据库隔离失效：meta_db_path={_config.paths.meta_db_path}"


from core.terminal.memory_gateway import MemoryGateway  # noqa: E402
from core.terminal.memory_projection import MemoryProjectionItem  # noqa: E402
from core.terminal.principal import (  # noqa: E402
    ChatType,
    PrincipalContext,
    RelationLevel,
    VisibilityScope,
)

UTC = timezone.utc
NOW = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)


def _item(
    memory_id: str,
    content: str,
    *,
    scope: tuple[str, ...] = ("owner_only",),
    minutes: int = 0,
    memory_type: str = "fact",
) -> MemoryProjectionItem:
    return MemoryProjectionItem(
        memory_id=memory_id,
        memory_type=memory_type,
        content=content,
        platform="wechat",
        bot_account_id="bot_a",
        occurred_at=NOW - timedelta(minutes=minutes),
        confidence=0.6,
        visibility_scope=scope,
        source_event_ids=(f"evt_{memory_id}",),
    )


class _FakeRepo:
    """Only exposes what the gateway calls; ``visible`` models scan truncation."""

    def __init__(self, items: list[MemoryProjectionItem], visible: list[str] | None = None):
        self._items = {item.memory_id: item for item in items}
        self._visible = visible

    def query_candidates(self, limit: int = 400, accounts=None, component_id=""):
        ids = self._visible if self._visible is not None else list(self._items)
        return [self._items[i] for i in ids if i in self._items][:limit]

    def get(self, memory_id: str):
        return self._items.get(memory_id)


def _owner() -> PrincipalContext:
    return PrincipalContext(
        platform="wechat",
        bot_account_id="bot_a",
        chat_id="owner_private",
        chat_type=ChatType.PRIVATE,
        person_id="p_owner",
        display_name="主人",
        relation_to_bot=RelationLevel.OWNER,
    )


def _group_member() -> PrincipalContext:
    return PrincipalContext(
        platform="wechat",
        bot_account_id="bot_a",
        chat_id="group_x@chatroom",
        chat_type=ChatType.GROUP,
        person_id="p_member",
        display_name="群成员",
        relation_to_bot=RelationLevel.UNKNOWN,
        group_id="group_x@chatroom",
        memory_scopes=frozenset({VisibilityScope.GROUP_MEMBERS}),
    )


def test_vector_widens_candidate_pool() -> None:
    lexical = _item("proj:lexical", "yinyue 项目进度记录")
    semantic = _item("proj:semantic", "代号紫水晶的调研计划")
    repo = _FakeRepo([lexical, semantic], visible=["proj:lexical"])
    query = "上次说的那个计划"

    plain = MemoryGateway(
        projection_repository=repo, vector_recaller=lambda q, limit: {}
    )
    plain_ids = {hit.memory_id for hit in plain.recall(_owner(), query=query, limit=10)}
    assert "proj:semantic" not in plain_ids, plain_ids

    widened = MemoryGateway(
        projection_repository=repo,
        vector_recaller=lambda q, limit: {"proj:semantic": 0.92},
    )
    widened_ids = {hit.memory_id for hit in widened.recall(_owner(), query=query, limit=10)}
    assert "proj:semantic" in widened_ids, widened_ids
    print("  ok   vector recall widens the candidate pool")


def test_vector_improves_ranking() -> None:
    lexical = [
        _item(f"proj:lex{i}", f"项目 相关 记录 {i}", minutes=i)
        for i in range(20)
    ]
    # deliberately shares no token with the query, so only the vector path
    # can lift it
    semantic = _item("proj:semantic", "代号紫水晶的调研", minutes=30)
    repo = _FakeRepo(lexical + [semantic])
    query = "上次说的那个计划"

    plain = MemoryGateway(
        projection_repository=repo, vector_recaller=lambda q, limit: {}
    )
    plain_ids = [hit.memory_id for hit in plain.recall(_owner(), query=query, limit=25)]

    boosted = MemoryGateway(
        projection_repository=repo,
        vector_recaller=lambda q, limit: {"proj:semantic": 0.95},
    )
    boosted_ids = [hit.memory_id for hit in boosted.recall(_owner(), query=query, limit=25)]

    assert "proj:semantic" in plain_ids and "proj:semantic" in boosted_ids
    assert boosted_ids.index("proj:semantic") < plain_ids.index("proj:semantic"), (
        boosted_ids.index("proj:semantic"),
        plain_ids.index("proj:semantic"),
    )
    print("  ok   vector recall lifts semantic matches in the ranking")


def test_vector_respects_acl() -> None:
    secret = _item("proj:secret", "主人才知道的暗号：紫水晶")
    repo = _FakeRepo([secret], visible=[])
    gateway = MemoryGateway(
        projection_repository=repo,
        vector_recaller=lambda q, limit: {"proj:secret": 0.99},
    )
    hits = gateway.recall(_group_member(), query="暗号", limit=10)
    assert hits == [], [hit.memory_id for hit in hits]
    print("  ok   vector-only candidates still pass the ACL")


def test_vector_failure_degrades_to_lexical() -> None:
    lexical = _item("proj:lexical", "yinyue 项目进度记录")
    repo = _FakeRepo([lexical])

    def _boom(query, limit):
        raise RuntimeError("embedding service down")

    gateway = MemoryGateway(projection_repository=repo, vector_recaller=_boom)
    hits = gateway.recall(_owner(), query="项目", limit=5)
    assert {hit.memory_id for hit in hits} == {"proj:lexical"}
    print("  ok   vector failure degrades to lexical recall")


def test_gateway_passes_wait_to_recaller() -> None:
    calls: list[float] = []

    def _recaller(query, limit, wait=0.0):
        calls.append(wait)
        return {}

    gateway = MemoryGateway(
        projection_repository=_FakeRepo([_item("proj:x", "内容")]),
        vector_recaller=_recaller,
    )
    gateway.recall(_owner(), query="问题", limit=5, vector_wait=0.25)
    assert calls == [0.25], calls
    gateway.recall(_owner(), query="问题2", limit=5)  # default 0
    assert calls[-1] == 0.0, calls
    print("  ok   gateway forwards the wait budget to the recaller")


def main() -> bool:
    tests = [
        test_vector_widens_candidate_pool,
        test_vector_improves_ranking,
        test_vector_respects_acl,
        test_vector_failure_degrades_to_lexical,
        test_gateway_passes_wait_to_recaller,
    ]
    failed: list[str] = []
    for test in tests:
        try:
            test()
        except Exception as exc:  # noqa: BLE001
            failed.append(test.__name__)
            print(f"  FAIL {test.__name__}: {exc!r}")
    print(f"vector recall checks: {len(tests) - len(failed)}/{len(tests)} passed")
    return not failed


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
