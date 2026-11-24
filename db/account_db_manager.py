from __future__ import annotations

import structlog
from threading import Lock
from typing import Any

from db.account_repo import AccountRepositoryManager
from db.write_guard import WriteHandle
from safelog import warn

log = structlog.get_logger()


class _LazyAccountRepo:
    """账号库仓库的**惰性**句柄（``AccountRepositoryManager`` 的替身，**接口逐字同形**）。

    为什么需要它：``AccountRepositoryManager.__init__`` 会 ``init_account_db``
    （建库文件 + ``create_all`` + 跑迁移），**那才是写**。所以注册时先不发句柄，
    把"建 engine"推迟到第一次真访问——与 ``db/meta_repo.py`` / ``db/repository.py``
    同一处手法（见 ``db.write_guard.WriteHandle``）：

    * **注册 ≠ 要写**：只想问"这个账号库在哪"的调用方不再被守卫连坐；
    * 真写调用方：第一次碰 ``repo.engine`` / ``repo.messages`` …（走 ``_manager()`` →
      ``WriteHandle.handle``）时过守卫，生产路径 + 未声明 ⇒ ``ProductionWriteRefused``，
      且**一个字节都不会变**。

    2026-10-03 P0 修正：**替身必须真同形**（这一条是那次生产事故的形状）
    -------------------------------------------------------------------
    上一版把 ``__getattr__`` 转发给那个 ``WriteHandle``（它**刻意不转发属性**，因为句柄
    冒充 Engine 会让 ``Session`` 的 ``isinstance`` 分流走错路），于是替身只剩三个方法：
    ``engine`` 返回的是**工厂的返回值**（``AccountRepositoryManager``，不是 ``Engine``），
    而 ``messages`` / ``contacts`` / … / ``send_log`` 七个仓库属性在替身上根本不存在——
    生产代码一直在用它们（``ctx.repo.messages`` / ``ctx.repo.send_log`` /
    ``ctx.repo.engine.connect()``），五条 error 全是这一个根因。

    所以本类的形状改成"**惰性的只是**什么时候建那个真 manager**，建好之后逐字同形**"：
    ``_manager()`` 过守卫拿到真 ``AccountRepositoryManager`` 并缓存它；``engine`` 返回
    **它**的 engine（**真** ``sqlalchemy.engine.Engine``，不是本对象、也不是句柄——
    生产代码把它直接喂给 ``Session(bind)`` / 调 ``engine.connect()``），其余属性由
    ``__getattr__`` 委托给那个真 manager。

    **句柄仍然不冒充 Engine**（那条不变）：冒充发生在 ``WriteHandle`` 那一层，
    本类不转发 ``materialize`` / ``assert_writable`` 那种句柄内部接口——``_manager()``
    是显式的解引用动作。
    """

    def __init__(self, db_path: str, platform: str, account_id: str):
        self.db_path = db_path
        self.platform = platform
        self.account_id = account_id
        self._handle = WriteHandle(
            db_path, what="账号库的可写 engine",
            factory=lambda: AccountRepositoryManager(db_path, platform, account_id),
        )
        #: **真** ``AccountRepositoryManager``（第一次真访问才建；建了就缓存）。
        self._repo: AccountRepositoryManager | None = None
        # 非生产路径当场建好（注册完就是可用的库，既有语义逐字不变）；生产路径不建，
        # 留给第一次真用时的守卫（**注册 ≠ 要写**）。见 WriteHandle.schema_ready。
        self._handle.schema_ready()

    # ---------------------------------------------------------------- 解引用
    def _manager(self) -> AccountRepositoryManager:
        """真 manager（幂等）——**不过守卫**：读不该被写守卫连坐。

        为什么要分开两个口（P0 修正的第二半）
        ------------------------------------
        ``WriteHandle`` 里"读 / 写"本来就是两条路：``materialize()``（不过守卫，给读）与
        ``handle()``（先 ``assert_writable()`` 再物化，给写）。仓库层
        （``db/repo_base.py``）读走 ``materialize``、写走 ``assert_writable``，本类必须
        与它**同一口径**，否则会出现一个新的连坐形状：

        * ``_session()`` 里 ``Session(bind)`` 建会话时会 ``bind.connect()``（SQLAlchemy
          对非 Engine 的 bind 走的就是这条路）。若本方法过守卫，那么**只读**查询
          （``repo.messages.get_by_id(...)``）在"生产路径 + 未声明"的进程里会抛
          ``ProductionWriteRefused``——"读也堵死"，正是守卫模块自己写着不许发生的那一格；
        * 反过来，写入口（:attr:`engine` / :meth:`write_engine`）**照旧过守卫**，
          所以"未声明 + 生产 + 要写句柄 ⇒ 拒绝"这一格一个字都不变。

        判据来源仍然只有 ``core.bootstrap.production_data_paths`` 一处（守卫内部）。
        """
        if self._repo is None:
            self._repo = self._handle.materialize()
        return self._repo

    # ------------------------------------------------------- 与真 manager 同形的口
    def write_engine(self):
        """**拿可写 engine**——守卫就在这一步（生产路径 + 未声明 ⇒ 拒绝）。"""
        self._handle.assert_writable()
        return self._manager().engine

    def assert_writable(self) -> None:
        """**要写**：过写守卫（仓库层 ``db.repo_base.BaseRepository`` 从这里走）。"""
        self._handle.assert_writable()

    @property
    def engine(self):
        """**真** engine（``sqlalchemy.engine.Engine``）——与
        ``AccountRepositoryManager.engine`` 同一个对象。

        生产代码拿它做 ``engine.connect()`` / ``outbox_status_counts(engine)`` /
        ``resolve_pending(engine)`` / 直接喂 ``Session(bind)``，所以这里必须是 Engine，
        不能是 manager、更不能是那个惰性句柄。

        **拿它就是"要一个可写引擎"，所以先过守卫**（与 :meth:`write_engine` 同一处判据）：
        生产路径 + 未声明 ⇒ ``ProductionWriteRefused``，目标库零字节。
        """
        self._handle.assert_writable()
        return self._manager().engine

    def dispose_if_materialized(self) -> None:
        """注销时用：**没建过就不建**（注销不是写，不能顺手把库建出来）。"""
        if self._repo is not None:
            self._repo.engine.dispose()
            self._repo = None
        self._handle.dispose()

    def __getattr__(self, name: str) -> Any:
        """其余属性一律委托给**真 manager**（``messages`` / ``send_log`` / … 逐字同形）。

        ``engine`` / ``write_engine`` / ``assert_writable`` / ``dispose_if_materialized``
        都在本类上显式定义，所以不会走到这里（``__getattr__`` 只在常规查找失败时被调用）。

        为什么不是"转给那个 ``WriteHandle``"：它**刻意不转发属性**（见 ``WriteHandle``
        的类 docstring），转发过去只会得到 ``'WriteHandle' object has no attribute …``
        ——那正是 2026-10-03 的生产事故本身。

        **句柄内部接口必须"查不到"，不能"转发"**（这条是 P0 修正的第二半）
        ---------------------------------------------------------------
        ``getattr(obj, "materialize", None)`` 这种**探测**在仓库层是有的
        （``db/repo_base.py`` 的 ``_resolved_engine``：先探 ``materialize`` 判断"接到的是
        句柄还是真 engine"）。``getattr`` 的三参形式**只在 AttributeError 时**吃默认值：
        这里若把 ``materialize`` 转发给 manager（或让它走到守卫里抛
        ``ProductionWriteRefused``），探测会**抛出去**，读路径跟着炸——"读被写守卫连坐"
        的另一种形状。所以内部名一律 ``raise AttributeError(name)``：探测拿到 ``None``，
        仓库层照旧把这里当"没有句柄接口"处理，读继续走真 engine。
        """
        if name in self._NOT_FORWARDED or name.startswith("_"):
            raise AttributeError(name)
        return getattr(self._manager(), name)

    #: **不转发**的名字：句柄内部接口（探测语义见 :meth:`__getattr__`）。
    _NOT_FORWARDED = frozenset({
        "materialize", "handle", "schema_ready", "target", "what",
        "assert_writable", "write_engine", "dispose", "dispose_if_materialized",
    })

    def __repr__(self) -> str:  # pragma: no cover - 诊断用
        state = "已建" if self._repo is not None else "未建（还没人要）"
        return f"<LazyAccountRepo {self.platform}/{self.account_id} {self.db_path!r} {state}>"

    __str__ = __repr__


class AccountDBManager:
    """
    管理所有账号级数据库的连接池。

    - 按 (platform, account_id) 索引 AccountRepositoryManager
    - 启动时从 AccountRegistry 或配置加载所有账号
    - 支持运行时注册/注销账号

    **注册口不在构造/注册时断言可写**（``db/write_guard.py`` 的"读也堵死 = 全仓测试与
    只读脚本全崩"）：守卫挂在**真的拿可写句柄**那一处（:meth:`_LazyAccountRepo.engine`
    / ``write_engine()``），所以"注册一个账号库"本身不碰盘、不过守卫。
    """

    def __init__(self):
        self._repos: dict[tuple[str, str], AccountRepositoryManager] = {}
        self._lock = Lock()

    def register(self, platform: str, account_id: str, db_path: str) -> AccountRepositoryManager:
        """注册一个账号库并返回**惰性**可写仓库（接口同 ``AccountRepositoryManager``）。

        **注册时不过守卫、不建库**：``init_account_db``（建表 + 迁移）是写，它被推迟到
        第一次真访问句柄时——未声明的进程对**生产路径**（账号库目录
        ``data/platforms/<platform>/<account>/account.db`` 是 ``production_data_paths``
        的一项）动手会被拒；隔离路径——测试与 ``scripts/run_all_tests.py`` 用的隔离根
        ——照常放行。判据只有一处来源，见 ``db/write_guard``。

        父目录由 ``core.bootstrap.init_dirs()`` 建（它 ``mkdir`` 了 ``platforms_dir``
        与每个 ``ac.db_path`` 的父目录）——**本方法不建**：这里曾经 ``mkdir`` 过，但那
        正是"顺手往生产账号库里造东西"的形状，注册口不该有对目标动手的副作用。
        """
        key = (platform, account_id)
        with self._lock:
            if key in self._repos:
                return self._repos[key]
            repo = _LazyAccountRepo(db_path, platform, account_id)
            self._repos[key] = repo
            log.info("account_db_registered", platform=platform, account_id=account_id, db_path=db_path)
            return repo

    def unregister(self, platform: str, account_id: str) -> None:
        key = (platform, account_id)
        with self._lock:
            repo = self._repos.pop(key, None)
            if repo:
                try:
                    #: **没人要过句柄就别把它建出来**（注销不是写）：已建才 dispose，
                    #: 没建就直接丢弃。真 ``AccountRepositoryManager`` 没有这个方法，
                    #: 走 ``dispose`` 那条。
                    close = getattr(repo, "dispose_if_materialized", None) or (
                        lambda: repo.engine.dispose())
                    close()
                except Exception:
                    warn("db/account_db_manager.py:204 Exception", err=repr(__import__("sys").exc_info()[1]))
                log.info("account_db_unregistered", platform=platform, account_id=account_id)

    def get_repo(self, platform: str, account_id: str) -> AccountRepositoryManager | None:
        key = (platform, account_id)
        with self._lock:
            return self._repos.get(key)

    def get_or_raise(self, platform: str, account_id: str) -> AccountRepositoryManager:
        repo = self.get_repo(platform, account_id)
        if not repo:
            raise KeyError(f"账号 ({platform}, {account_id}) 未注册")
        return repo

    def all_repos(self) -> dict[tuple[str, str], AccountRepositoryManager]:
        with self._lock:
            return dict(self._repos)

    def list_accounts(self, platform: str | None = None) -> list[tuple[str, str]]:
        with self._lock:
            if platform:
                return [(p, a) for (p, a) in self._repos if p == platform]
            return list(self._repos.keys())

    def load_from_config(self) -> None:
        from core.config.settings import config
        for ac in config.all_account_configs():
            self.register(ac.platform, ac.account_id, ac.db_path)

    def load_from_registry(self, meta_repo) -> None:
        rows = meta_repo.account_registry.list_all(enabled_only=True)
        for row in rows:
            from core.config.settings import config
            db_path = str(config.paths.get_account_db_path(row.platform, row.account_id))
            self.register(row.platform, row.account_id, db_path)

    def sync_to_registry(self, meta_repo) -> None:
        for (platform, account_id), repo in self.all_repos().items():
            from core.config.settings import config
            ac = config.get_account_config(platform, account_id)
            role = ac.role if ac else "yinyue"
            data_dir = str(config.paths.get_account_dir(platform, account_id))
            meta_repo.account_registry.upsert(
                account_id=account_id,
                platform=platform,
                role=role,
                data_dir=data_dir,
            )
