from __future__ import annotations

import structlog
from abc import ABC
from sqlalchemy.orm import Session

log = structlog.get_logger()


class BaseRepository(ABC):
    """所有仓库的基类。

    它接的 ``engine`` 可能是**真 engine**，也可能是 ``db.write_guard.WriteHandle``
    （构造时不建库、写时才断言的那种句柄，见 ``db/meta_repo.py`` / ``db/repository.py``
    / ``db/account_db_manager.py``）。两种都要能用，所以这里**只有一处**知道怎么把
    句柄变成 SQLAlchemy 认得的东西：

    * :meth:`_session`（读为主）：:meth:`_resolved_engine` → ``materialize()``，
      **不过写守卫**——读不该被写守卫连坐（``db/write_guard.py`` 的"读也堵死 = 全仓
      测试与只读脚本全崩"）；
    * :meth:`write_engine`（真写）：先 ``assert_writable()`` 过守卫，再返回真 engine。

    为什么不直接把 ``WriteHandle`` 喂给 ``Session``：``Session.__init__`` 用
    ``isinstance(bind, Engine)`` 分流，句柄不是 Engine，于是静默走了另一条连接路径，
    在 SQLite 上表现为插入提交时 "**database is locked**"。所以句柄**不冒充** Engine
    （见 ``WriteHandle`` 的类 docstring），由本类这一处负责解引用。
    """

    def __init__(self, engine):
        self.engine = engine

    def _resolved_engine(self):
        """SQLAlchemy 能认的**真** engine（句柄走 ``materialize()``，真 engine 原样）。"""
        resolve = getattr(self.engine, "materialize", None)
        return resolve() if callable(resolve) else self.engine

    def read_engine(self):
        """读取侧统一取得实际 engine；调用方不自行解包受控句柄。"""
        return self._resolved_engine()

    def _write_session(self) -> Session:
        return Session(self.write_engine())

    def write_engine(self):
        """**要写**：过写守卫（生产路径 + 未声明 ⇒ ``ProductionWriteRefused``）后给真 engine。

        接的是真 engine 时守卫已在 manager 那一层过完了，这里原样返回。
        """
        guard = getattr(self.engine, "assert_writable", None)
        if callable(guard):
            guard()
        return self._resolved_engine()

    def _session(self) -> Session:
        return Session(self._resolved_engine())
