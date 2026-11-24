"""**写库守卫**：默认拒绝——"默认就是生产库"这条路的写侧那一半。

为什么有这个文件（2026-10-02 第三次生产库事故）
------------------------------------------------
同一天三次事故，形状是同一个：**"不写路径"被当成了默认值，而默认值恰好指着生产库**。

1. 04:57 迁移 55 落到生产库（代理 import 冒烟）；
2. 18:12 迁移 56 落到生产库（代理探测）—— 这两次由 ``core/bootstrap.py`` 的
   ``assert_migrations_allowed`` 收窄（**迁移**那一半，A 号改动）；
3. 22:5x **1 行测试数据写进生产 ``claim_ledger``**：冒烟脚本调用写侧入口时**没传
   ``engine``**，写口内部回落到 ``core.bootstrap.get_meta_repo()``，于是"我手上没有
   句柄"被翻译成了"那就写生产库"。迁移那道门**拦不住它**——这次根本不需要迁移。

所以本模块管的是**写入**那一半，判据与迁移那道门同构：

* **非声明上下文 + 目标是生产路径 → 拒绝**（抛 :class:`ProductionWriteRefused`）；
* **声明上下文 → 放行**（入口代码 :func:`declare_writes_allowed`（服务启动 / 管理脚本），或入口已声明的
  "生产迁移"，见 :func:`declaration_reason`）；
* **非生产路径（测试隔离库 / 临时库）→ 一律放行**（不许误伤测试：全仓测试的库都在
  隔离根里，见 ``tests/_tempdirs.py`` 与 ``scripts/run_all_tests.py``）；
* **只读访问 → 走 :func:`read_only_engine`，本模块一个字都不拦**：读错最多是"没有
  台账/没有数据"，写错是**事实污染**，方向不同（同一句判断在
  ``core/context/message_facts.record_message_claims`` 的 docstring 里已经写过一次）。

判据只有一处来源
----------------
"哪些路径算生产"**不在这里**：唯一来源是 ``core.bootstrap.production_data_paths()``
（它自己又只读 ``PathSettings`` 的字段默认值与当前生效的账号库解析）。本模块
**import 它**，不复制、不拼接、不缓存第二份路径字面量——``db/`` 里出现第二个
"生产路径判断"就是下一次漂移的起点。

为什么 import 写在函数里（重要）
--------------------------------
``db/meta_repo.py`` → 本模块 → ``core.bootstrap`` → ``db.meta_repo`` 是一个**真环**：
模块级 import 会在 ``import core.bootstrap`` 的第一秒炸掉（部分初始化的模块）。所以
``from core.bootstrap import production_paths_for`` 一律写在函数体内——懒 import 断开
这条环，判据来源仍然只有那一处。

声明是**参数**，不是 env
------------------------
同 ``allow_production`` 的理由：env 会被 ``$env:YINYUE_ALLOW_WRITES=1`` 随手带上，
等于没拦；写入口代码里那一行就要求改**入口本身**（一个要评审的改动）。声明状态存在
本模块的进程内状态里（:data:`_WRITES_DECLARED`），只由
:func:`declare_writes_allowed` 设置。调用它的都是**入口代码**：服务启动
:mod:`scheduler.main`，以及管理脚本入口 ``scripts/reimport_speech_seed.py``
（2026-10-11 / 3eda591 起，理由见该脚本 157-161 行）。

本模块**不碰任何库**：判据是纯路径比较 + 进程内布尔，被拒绝时目标库一个字节都不会变。
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator

import structlog

log = structlog.get_logger()


class ProductionWriteRefused(RuntimeError):
    """拒绝"在未声明的进程里拿生产库的可写句柄"（``RuntimeError`` 的子类，兼容旧捕获）。"""


#: 本进程有没有**显式声明**"我要写生产库"。默认 ``False`` = 拒绝。
#:
#: 唯一设置点是 :func:`declare_writes_allowed`（调用它的只有入口代码：服务启动
#: ``scheduler/main.py`` 与管理脚本 ``scripts/reimport_speech_seed.py`` 两处）。
_WRITES_DECLARED: bool = False

#: 声明者的自述（只进日志与拒绝消息；不参与判据）。
_DECLARED_BY: str = ""

#: 已记过 ``production_write_allowed`` 的 ``(target, 声明者)`` 集合（进程内去重，
#: 理由见 :func:`assert_writes_allowed` 的日志注释）；重新声明时清空，保证每个
#: 入口进程仍留下一条可审计的"谁开了生产库"。
_LOGGED_ALLOWED: set[tuple[str, str]] = set()


# ---------------------------------------------------------------------------
# 声明（写侧入口：`scheduler/main.py` 与 `scripts/reimport_speech_seed.py` 两处；
# 测试用 reset/setter 回到默认拒绝）
# ---------------------------------------------------------------------------
def declare_writes_allowed(*, declared_by: str = "scheduler.main") -> None:
    """声明"本进程可写生产库"（**入口代码调一次**；参数，不用 env）。

    为什么在入口附近声明而不是藏在写口里：判据必须是**调用方**给的意图。写口自己
    声明就等于"谁都能写"——那正是缺口的形状。当前只有两个入口声明：服务启动
    ``scheduler/main.py`` 与"改生产人设"的管理脚本 ``scripts/reimport_speech_seed.py``
    （2026-10-11 / 3eda591 起，理由见该脚本 157-161 行）；其余脚本/探测/测试不声明，
    于是拿不到生产库的可写句柄（它们的库是隔离的，见 :func:`writes_allowed` 的次序）。
    """
    global _WRITES_DECLARED, _DECLARED_BY
    _WRITES_DECLARED = True
    _DECLARED_BY = str(declared_by or "")
    _LOGGED_ALLOWED.clear()  # 新入口进程/重新声明 → 每个 target 再留一条审计行
    log.info("production_writes_declared", declared_by=_DECLARED_BY)


def writes_declared() -> bool:
    """本进程有没有显式声明"可写生产库"（只读；设置点只有 :func:`declare_writes_allowed`）。"""
    return bool(_WRITES_DECLARED)


def reset_writes_declaration() -> None:
    """撤掉声明位（**只给用例用**：测"未声明 ⇒ 拒绝"那一格必须能回到默认拒绝）。

    产品代码里没有调用点——声明一旦由入口给出，本进程就该一直记得（服务启动之后任何
    一次"半路撤声明"都只会让本来合法的写开始抛错）。
    """
    global _WRITES_DECLARED, _DECLARED_BY
    _WRITES_DECLARED = False
    _DECLARED_BY = ""


def declaration_reason() -> str:
    """本进程为什么算"已声明"（拒绝消息与日志要给得出依据，不能只说"未声明"）。

    两个来源，**都是入口代码里的显式动作**：

    1. :func:`declare_writes_allowed`（本模块；入口代码调：``scheduler/main.py`` 或
       管理脚本 ``scripts/reimport_speech_seed.py``）；
    2. ``core.bootstrap.production_migration_declared()``：入口调过
       ``init_singletons(allow_production=True)``。同一次启动给出的**同一个意图**
       （"这个进程是服务本身，可以碰生产"），迁移那道门与写这道门读同一句话；也可以
       理解成"本进程不是临时探测脚本"的**那一处**进程级证据——
       ``tests/test_bootstrap_migration_guard.py`` 用"生产形状 + 只放行迁移声明"的替身
       进程证明服务照常启动，靠的就是这条桥。它读不出来就退化成"只认 (1)"（更严）。
    """
    if _WRITES_DECLARED:
        return f"declare_writes_allowed(declared_by={_DECLARED_BY or '<未注明>'})"
    try:
        from core.bootstrap import production_migration_declared
    except Exception as exc:  # pragma: no cover - 判据来源拿不到时退化成更严的一档
        log.warning("write_guard_declaration_probe_failed", error=str(exc)[:160])
        return ""
    return (
        "init_singletons(allow_production=True)（core.bootstrap 的生产迁移声明）"
        if production_migration_declared()
        else ""
    )


# ---------------------------------------------------------------------------
# 判据（唯一来源：core.bootstrap.production_data_paths）
# ---------------------------------------------------------------------------
def production_path_hits(target: Any) -> tuple[str, ...]:
    """``target`` 如果落在**生产路径**上，返回命中的那些路径（否则空元组）。

    转发 ``core.bootstrap.production_paths_for``——**不在这里实现路径判断**。懒 import
    的理由见模块 docstring（真 import 环）。
    """
    from core.bootstrap import production_paths_for

    return tuple(production_paths_for(target))


def writes_allowed(target: Any) -> bool:
    """``target`` 能不能拿**可写**句柄（判据次序有意，与迁移那道门同构）。

    1. **不是生产路径** → ``True``（隔离库 / 临时库 / 内存库一律放行）——
       全仓测试的库都在 ``data/_*_test/`` 与 ``.test_tmp/`` 里，**不许误伤**；
    2. 是生产路径 + **本进程已声明**（:func:`declaration_reason` 非空）→ ``True``；
    3. 是生产路径 + 未声明 → ``False``（:func:`assert_writes_allowed` 负责抛错）。

    只读**不经过这里**：生产库的读走 :func:`read_only_engine` / 各仓库的只读查询，
    本函数一个字都不拦。
    """
    if not production_path_hits(target):
        return True
    return bool(declaration_reason())


def assert_writes_allowed(target: Any, *, what: str = "可写句柄") -> None:
    """在建**可写**句柄之前过守卫：拒绝时抛 :class:`ProductionWriteRefused`。

    拒绝消息必须自解释（与迁移那道门同构）：目标路径、"**这是生产库**"、正确做法
    （隔离库 / 显式声明）都要在里面——看到报错的人应该不用读源码就知道下一步做什么。
    """
    hits = production_path_hits(target)
    if not hits:
        return
    reason = declaration_reason()
    if reason:
        # 每个 (target, 声明者) 进程内只记一次：生产上每开一次句柄都会走到这里，
        # 逐次落 info 会把日志淹掉（2026-10-17 实测 40 分钟 8086 行、占全日志 94%）。
        key = (str(target), reason)
        if key not in _LOGGED_ALLOWED:
            _LOGGED_ALLOWED.add(key)
            log.info("production_write_allowed", target=str(target), what=what,
                     production_paths=list(hits), declared_by=reason)
        return
    log.error("production_write_blocked", target=str(target), what=what,
              production_paths=list(hits))
    raise ProductionWriteRefused(_refusal_message(target, hits, what))


class WriteHandle:
    """**写时断言**的可写句柄：构造时什么都不做，第一次真用它才过守卫 + 建 engine。

    为什么要有这个类（本类的存在理由就是"守卫位置放错"这个 bug 的形状）
    ----------------------------------------------------------------
    守卫曾经挂在 ``MetaRepositoryManager.__init__`` 上：``init_meta_db`` 会
    ``create_all`` + 跑迁移，看起来"构造 = 建可写句柄"，所以构造时拦很自然。但那是
    **位置放错**：构造一个仓库不等于这个调用方要写——只想读的调用方（只读脚本、只想
    ``list_active()`` 的测试、只想问一句"这个账号库在哪"的诊断）连 new 都 new 不出来，
    于是"读"被"写"的守卫连坐堵死，与本模块 :func:`read_only_engine` 那条注释
    （"读也堵死 = 全仓测试与只读脚本全崩"）自相矛盾。

    ``init_meta_db`` / ``init_db`` / ``init_account_db`` **本身就是写**（建库文件、
    ``create_all``、跑迁移），所以断言不能只是"挪个地方"：它必须挡住**真正的第一次
    写**。做法是把"建 engine"这一步也一起推迟到那一刻——

    * 构造 :class:`WriteHandle` 不碰盘、不建库、不过守卫（**构造 ≠ 要写**）；
    * **拿可写句柄**（:meth:`handle`）那一刻先过 :func:`assert_writes_allowed`，
      再调 ``factory()`` 真建 engine —— 被拒绝时目标库**一个字节都不会变**；
    * 非生产路径（隔离库 / 临时库）由 :meth:`schema_ready` 当场建好，构造语义与从前
      逐字一致（见下）。

    **它不是 Engine**（这条是硬约束，踩过坑）
    ----------------------------------------
    ``Session(bind)`` 在 ``__init__`` 里对 bind 做 ``isinstance(bind, Engine)``：真 Engine
    走 ``_connection_cls`` 那条路，**别的对象**走 ``getattr(bind, "connect")`` 那条路。
    "看起来像"不够——本类一开始就是靠 ``__getattr__`` 转发冒充 Engine 的，结果
    ``Session(handle)`` 静默走了另一条路，在 SQLite 上表现为 "**database is locked**"
    （插入提交时连接换了一条，锁没释放），而 ``Session(real_engine)`` 一切正常。
    所以本类**不冒充** Engine：谁要真句柄谁先 :meth:`materialize`。仓库层因此统一从
    ``db.repo_base.BaseRepository`` 那**一处**取真 engine（读）或走 ``write_engine()``
    （写），而不是把本对象直接喂给 SQLAlchemy。

    用法（各 manager 的唯一可写句柄入口）::

        self._write = WriteHandle(db_path, what="meta 库的可写 engine",
                                  factory=lambda: init_meta_db(db_path))
    """

    def __init__(self, target: Any, *, what: str, factory: Any):
        self.target = target
        self.what = what
        self._factory = factory
        self._real: Any = None

    def materialize(self) -> Any:
        """建**真** engine（幂等）——**不过守卫**：只给"读"与已获准的写用。

        守卫在 :meth:`handle` / :meth:`assert_writable` 那两处；本方法刻意不拦，
        因为读路径不该被写守卫连坐（模块 docstring 与 :func:`read_only_engine` 那条）。
        """
        if self._real is None:
            self._real = self._factory()
        return self._real

    def assert_writable(self) -> None:
        """**要写**：当场过守卫（生产路径 + 未声明 ⇒ :class:`ProductionWriteRefused`）。

        拒绝时目标库一个字节都不会变——断言在 :meth:`materialize` 之前。
        """
        assert_writes_allowed(self.target, what=self.what)

    def handle(self) -> Any:
        """**拿可写句柄**：过守卫 + 建 engine（"要写"是一次显式动作，不是属性访问）。

        各 manager 的 ``engine`` / ``write_engine()`` 调本方法，守卫就在那一刻。
        """
        self.assert_writable()
        return self.materialize()

    def schema_ready(self) -> None:
        """**只在非生产路径上**当场把句柄建出来（生产路径仍然只建在第一次真用）。

        为什么需要这一步（不是"把断言挪回去"）：``init_meta_db`` / ``init_db`` /
        ``init_account_db`` 除了"开一个写句柄"之外还**顺带把 schema 建好/迁移到最新**
        （``create_all`` + ``run_migrations``）。全仓大量调用方（与直接跑用例）依赖的正是
        "**构造完就已经是可用的库**"这条既有语义；把建句柄一律推迟到第一次访问，会把这些
        调用方从"schema 已就绪"变成"schema 还没建"，是**静默的行为回归**。

        所以这里把"要紧的那一半"分开：

        * **非生产路径**（隔离库 / 临时库）：建它**不需要任何声明**（判据次序第 1 条），
          这条路本来就不该被拦——当场建好，构造语义与从前逐字一致；
        * **生产路径**：**不在这里建**。构造永远是安全的（不碰盘），第一次真用
          （:meth:`handle`）才过守卫——这正是"断言从构造时挪到写时"。

        注意它**不是**"构造时断言"的残留：判据仍然只有一处来源
        （``core.bootstrap.production_paths_for``），本方法在**生产路径上什么都不做**
        （既不建、也不抛），拒绝仍然只发生在 :meth:`handle` 那一刻。
        """
        if not production_path_hits(self.target):
            self.materialize()

    def dispose(self) -> None:
        """没人要过句柄时是 no-op（**不因为"清理"就把库建出来**）。"""
        if self._real is not None:
            self._real.dispose()

    def __repr__(self) -> str:  # pragma: no cover - 诊断用
        state = "已建" if self._real is not None else "未建（还没人要写）"
        return f"<WriteHandle {self.target!r} {state}>"

    __str__ = __repr__


def _refusal_message(target: Any, hits: tuple[str, ...], what: str) -> str:
    """拒绝消息（"为什么" + "正确做法"，含 R-级理由与可追的事故记录）。"""
    return (
        "拒绝写入：目标 {} **这是生产库**（命中生产路径 {}），本进程**没有声明**"
        "可写生产库，所以不给 {}。".format(target, list(hits), what)
        + " R-级理由：往生产库写字是**事实污染**（读错最多是'没有数据'，写错改不回来）"
        "——2026-10-02 22:5x 的第三次事故就是 1 行测试数据写进生产 claim_ledger："
        "冒烟脚本调用写侧入口时没传 engine，写口内部回落到 core.bootstrap.get_meta_repo()，"
        "于是'手上没有句柄'被翻译成了'那就写生产库'（迁移那道门拦不住它，这次不需要迁移）。"
        " 正确做法：① 脚本/测试/探测把 META_DB_PATH / PLATFORMS_DIR / DATA_DIR 指到隔离库"
        "（scripts/run_all_tests.py 的隔离根、tests/_tempdirs.py 的 .test_tmp）——非生产路径"
        "一律放行，不需要任何声明；②只有**服务入口**才该写生产库，它必须显式声明——在入口"
        "代码里的 init_singletons(allow_production=True) 旁边写 db.write_guard."
        "declare_writes_allowed(declared_by=...)，默认 False，**不用 env**（env 会被脚本随手"
        "带上，等于没拦）；③只想读生产库就**不要**拿可写句柄——用 "
        "db.write_guard.read_only_engine(path)（只读连接，本守卫不拦）。"
    )


# ---------------------------------------------------------------------------
# 只读：**不经过守卫**的那条路（读也堵死 = 全仓测试与只读脚本全崩）
# ---------------------------------------------------------------------------
def read_only_engine(target: Any):
    """生产库的**只读** engine（``mode=ro``；本模块不拦——读不是污染）。

    给"确实要读生产库"的只读脚本/诊断/取证用：SQLite 的 ``mode=ro`` 打开一个不存在的
    文件会直接报错而不是**把它建出来**，所以这个口子不会顺手在生产上造库。
    """
    from sqlalchemy import create_engine

    path = str(target or "").strip()
    if not path:
        raise ValueError("read_only_engine 需要一个库路径")
    return create_engine(
        f"sqlite:///file:{path}?mode=ro&uri=true",
        connect_args={"check_same_thread": False, "timeout": 30},
    )


@contextmanager
def read_only_connection(target: Any) -> Iterator[Any]:
    """``read_only_engine`` 的短命连接版本（用完就关，判据/读数用）。"""
    engine = read_only_engine(target)
    try:
        with engine.connect() as conn:
            yield conn
    finally:
        engine.dispose()


__all__ = [
    "ProductionWriteRefused",
    "WriteHandle",
    "assert_writes_allowed",
    "declaration_reason",
    "declare_writes_allowed",
    "production_path_hits",
    "read_only_connection",
    "read_only_engine",
    "reset_writes_declaration",
    "writes_allowed",
    "writes_declared",
]
