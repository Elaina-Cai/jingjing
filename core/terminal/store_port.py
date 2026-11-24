"""⑨ 统一读写口：数据层多库 facade（纯转发，不迁移数据、不改行为）。

目标（施工单 Step D2）：把散落各处的 `from core.bootstrap import get_meta_repo ...`
收口到一个口子，任何模块经由此端口访问数据层（账号库/共享库/多账号管理器/统一查询层）。
端口只做惰性转发：`core.bootstrap` 在函数调用时才被导入，异常原样穿透，
不吞不包、不缓存、不落盘、不做业务判断。

四个面（surfaces）：
    meta                -> core.bootstrap.get_meta_repo       (db.meta_repo.MetaRepositoryManager)
    account             -> core.bootstrap.get_repo            (db.repository.RepositoryManager；无账号上下文时回落 meta)
    account_db_manager  -> core.bootstrap.get_account_db_manager (db.account_manager.AccountDBManager)
    unified             -> core.bootstrap.get_unified_query   (db.unified_query.UnifiedQuery)

未收口的直连（有意保留）：core/bootstrap.py 本体、本端口自身、scripts/、tests/、backups/。
"""
from __future__ import annotations

from typing import Any

__all__ = [
    "STORE_OWNERS",
    "STORE_SURFACES",
    "get_account_db_manager",
    "get_meta_repo",
    "get_repo",
    "get_unified_query",
    "store_digest",
]

STORE_SURFACES = ("meta", "account", "account_db_manager", "unified")

STORE_OWNERS = {
    "meta": "db.meta_repo.MetaRepositoryManager",
    "account": "db.repository.RepositoryManager",
    "account_db_manager": "db.account_manager.AccountDBManager",
    "unified": "db.unified_query.UnifiedQuery",
}


def get_meta_repo():
    """共享库管理器；等价 core.bootstrap.get_meta_repo()。"""
    from core.bootstrap import get_meta_repo as _get

    return _get()


def get_repo():
    """账号库管理器（无账号上下文时回落共享库）；等价 core.bootstrap.get_repo()。"""
    from core.bootstrap import get_repo as _get

    return _get()


def get_account_db_manager():
    """多账号管理器；等价 core.bootstrap.get_account_db_manager()。"""
    from core.bootstrap import get_account_db_manager as _get

    return _get()


def get_unified_query():
    """跨账号统一查询层；等价 core.bootstrap.get_unified_query()。"""
    from core.bootstrap import get_unified_query as _get

    return _get()


def store_digest() -> dict:
    """审计用摘要：面清单与各自 owner（不含句柄，避免触 IO）。"""
    data: dict[str, Any] = {
        "surfaces": list(STORE_SURFACES),
        "owners": dict(STORE_OWNERS),
        "surfaces_count": len(STORE_SURFACES),
    }
    return data
