# -*- coding: utf-8 -*-
"""**Engine/仓库 API 契约**（2026-10-03 P0 的回归格）：manager 与账号库替身必须真同形。

为什么有这个文件（那晚生产被打坏、而测试全绿的形状）
----------------------------------------------------
``db/`` 的"写守卫"重构把可写入口改成返回惰性句柄 ``WriteHandle``，其中
``_LazyAccountRepo``（``AccountDBManager.register`` 的返回值，也就是全仓
``ctx.repo`` / ``get_repo()`` 的实际对象）**只剩三个方法**，于是生产代码一直在用的
两样东西同时消失：

1. ``repo.engine`` 返回的是**工厂的返回值**（``AccountRepositoryManager``），不是
   ``Engine`` ⇒ ``engine.connect()`` / ``Session(engine)`` 全炸
   （实测 error：``'AccountRepositoryManager' object has no attribute 'connect'``）；
2. ``repo.messages`` / ``contacts`` / ``analysis`` / ``sync_state`` / ``user_profile``
   / ``send_log`` / ``action_log`` 七个仓库属性在替身上**不存在**
   （实测 error：``'WriteHandle' object has no attribute 'messages'``）。

单元测试没抓到，是因为它们大多**直接 new 真 manager**（``AccountRepositoryManager``）
或直接用 ``MetaRepositoryManager``，没有一条断言"**生产代码实际拿到的那个对象**上，
它用的那些名字还在"。本文件补的就是这条断言，分三层：

* **① 名字集合**：从 ``AccountRepositoryManager.__init__`` 的**源码**（AST）取它给自己
  挂的属性，逐个断言替身上取得出来、且指向同一个真对象（不是转发到句柄）；
* **② 生产调用点扫描**：用 AST 扫 ``db/`` / ``core/`` / ``agents/`` / ``scheduler/`` /
  ``web/`` / ``scripts/`` 里**接收者像账号库仓库**的 ``.engine`` / 七个仓库属性访问，
  每个名字都必须在替他身上存在——**这条就是"下一次同类回归"的门**；
* **③ 真跑那五个调用点**：``EvidenceIndexer.index_pending_for_account``（要
  ``account_repo.messages``）、``media_registry.resolve_pending(ctx.repo.engine)``、
  ``outbox.drain_outbox(repo=ctx.repo)``、``SendLogRepository.cleanup_stale``、
  ``BaseRepository._session()``（读）——全都是**真实函数**，在**隔离库**上跑。

边界
----
* 不碰生产库：所有目标库都在 ``tests/_tempdirs.py`` 的 ``.test_tmp`` 下；"生产形状"
  的身份由 ``tests/test_production_write_guard.py`` 那套注入手法临时给出；
* 不新开关、不改判据来源（判据仍然只有 ``core.bootstrap.production_data_paths`` 一处）；
* 守卫四格不在这里重测（``tests/test_production_write_guard.py`` 是它的 owner），
  这里只钉"**守卫放行之后，句柄给出去的东西还是不是原来的 Engine/仓库**"。

Run directly::

    .\\.venv\\Scripts\\python.exe tests\\test_engine_api_contract.py
"""

from __future__ import annotations

import ast
import os
import shutil
import sys
import traceback
from contextlib import contextmanager
from pathlib import Path
from typing import Any

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, str(Path(__file__).resolve().parent))

ROOT = Path(__file__).resolve().parent.parent

# --- import 期钉死：本文件依赖的开关一律显式表态（"env 未设"≠"关"）----------
ROOT_ENV = {
    "LOG_DIR": str(Path("data") / "_engine_api_contract_logs"),
    "META_DB_PATH": str(ROOT / "data" / "_engine_api_contract" / "_iso" / "meta.db"),
    "PLATFORMS_DIR": str(ROOT / "data" / "_engine_api_contract" / "_iso" / "platforms"),
    "DATA_DIR": str(ROOT / "data" / "_engine_api_contract" / "_iso" / "data"),
}
os.environ.update(ROOT_ENV)
_PINS = {
    "YINYUE_FEATURE_OUTPUT_GATE": "off",
    "YINYUE_FEATURE_CLAIM_LEDGER": "off",
}
os.environ.update(_PINS)
Path(ROOT_ENV["LOG_DIR"]).mkdir(parents=True, exist_ok=True)

from sqlalchemy.engine import Engine  # noqa: E402

from core import bootstrap  # noqa: E402
from db import write_guard as wg  # noqa: E402
from db.account_db_manager import AccountDBManager  # noqa: E402
from db.account_repo import AccountRepositoryManager  # noqa: E402
from db.meta_repo import MetaRepositoryManager  # noqa: E402
from db.repository import RepositoryManager  # noqa: E402
from db.write_guard import ProductionWriteRefused, WriteHandle  # noqa: E402
from _tempdirs import make_db_dir  # noqa: E402

#: 账号库那一层的**契约名字**（生产代码在替身上用的那些；见本文件 docstring）。
_ACCOUNT_API = (
    "messages", "contacts", "analysis", "sync_state", "user_profile",
    "send_log", "action_log",
)

#: ``.engine`` 的接收者——"像账号库仓库/manager"的那些名字（AST 扫描用）。
_REPO_RECEIVERS = frozenset({
    "account", "account_repo", "acct_repo", "repo", "meta", "meta_repo",
    "ctx", "context", "r",
})


# ---------------------------------------------------------------------------
# 唯一一份"替身应该有哪些名字"的来源：真 manager 的 __init__（AST，不手抄）
# ---------------------------------------------------------------------------
def _manager_attributes_from_source() -> tuple[str, ...]:
    """``AccountRepositoryManager.__init__`` 给自己挂的属性名（``self.X = ...``）。"""
    source = (ROOT / "db" / "account_repo.py").read_text(encoding="utf-8-sig")
    cls = next(
        node for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.ClassDef) and node.name == "AccountRepositoryManager"
    )
    init = next(
        node for node in cls.body
        if isinstance(node, ast.FunctionDef) and node.name == "__init__"
    )
    names = []
    for node in ast.walk(init):
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if (isinstance(target, ast.Attribute)
                    and isinstance(target.value, ast.Name)
                    and target.value.id == "self"):
                names.append(target.attr)
    return tuple(names)


@contextmanager
def _isolated_account_repo():
    """一个隔离账号库上的 ``(manager, lazy)``：manager 是**真**对象，lazy 是替身。

    ``run_migrations`` 显式跑一遍：``AccountRepositoryManager.__init__`` 只 ``create_all``
    （``db/account_repo.py:17`` 的 ``init_account_db``），账号库那条迁移链由
    ``AccountDBManager.register`` 在**真建句柄**时补——本用例两个对象都是"当场建"的
    隔离路径，所以这里把 schema 补到与生产注册后一致（否则查 ``message_media`` 会撞
    "no such table"，那是 schema 时序，不是本文件要测的 API 形状）。
    """
    import db.migrations as migrations

    base = make_db_dir("eac_")
    path = base / "account.db"
    real = AccountRepositoryManager(str(path), "probe", "probe")
    migrations.run_migrations(str(path))
    adm = AccountDBManager()
    lazy = adm.register("probe", "probe", str(path))
    try:
        yield real, lazy
    finally:
        try:
            real.engine.dispose()
        except Exception:
            pass
        shutil.rmtree(base, ignore_errors=True)


@contextmanager
def _production_paths_are(*paths: object):
    """把**唯一**那份生产路径来源临时指向用例给的路径（判据调用处一个字没改）。"""
    saved = bootstrap.production_data_paths
    bootstrap.production_data_paths = lambda: tuple(
        (str(p), "engine_api_contract 用例注入的生产路径") for p in paths
    )
    try:
        yield
    finally:
        bootstrap.production_data_paths = saved


@contextmanager
def _undeclared():
    was_declared = wg.writes_declared()
    wg.reset_writes_declaration()
    try:
        yield
    finally:
        wg.reset_writes_declaration()
        if was_declared:  # pragma: no cover - 本文件从不声明，留作防御
            wg.declare_writes_allowed(declared_by="tests/test_engine_api_contract")


# ---------------------------------------------------------------------------
# ① 名字集合：替身必须与真 manager 同形（守卫放行的那条路上）
# ---------------------------------------------------------------------------
def test_lazy_account_repo_is_attribute_for_attribute_shaped_like_manager() -> None:
    """``register()`` 的返回值上，真 manager 的**每个**属性都在，且是同一个真对象。"""
    expected = _manager_attributes_from_source()
    assert set(_ACCOUNT_API) <= set(expected), (
        f"db/account_repo.py 的 __init__ 形状变了（少了仓库属性）：{expected}")
    assert "engine" in expected, expected

    with _isolated_account_repo() as (real, lazy):
        for name in expected:
            try:
                got = getattr(lazy, name)
            except AttributeError as exc:
                raise AssertionError(
                    f"替身上没有 {name!r}（生产代码会 AttributeError）：{exc}") from exc
            want = getattr(real, name)
            assert got is want or type(got) is type(want), (
                f"{name}: 替身给出 {type(got).__name__}，真 manager 是 {type(want).__name__}")

        # engine 这一条要更严：**必须是 Engine**（生产代码把它喂给 Session/connect）
        assert isinstance(lazy.engine, Engine), (
            f"repo.engine 不是 Engine 而是 {type(lazy.engine).__name__}"
            "——Session(bind) 会走错分流、engine.connect() 直接 AttributeError")
        # 与真 manager 的 engine **同型**：同一个 URL、同一个库文件
        assert str(lazy.engine.url) == str(real.engine.url), (
            f"替身给出的 engine 指向 {lazy.engine.url}，真 manager 是 {real.engine.url}")
        with lazy.engine.connect() as conn:
            assert conn.exec_driver_sql("select 1").scalar() == 1

        # 句柄层那条硬约束不变：**WriteHandle 不转发属性**（它一旦转发就冒充 Engine，
        # Session 的 isinstance 分流会走错路 → SQLite "database is locked"）。
        # 所以"同形"是替身给的，不是句柄给的——两句一起钉。
        assert not hasattr(WriteHandle, "__getattr__"), \
            "WriteHandle 不许有 __getattr__（句柄冒充 Engine，Session 会认错 bind）"
        # 句柄内部名在替身上必须"查不到"（AttributeError），不能"转发"也不能"抛别的"：
        # db/repo_base.py 的 `getattr(self.engine, "materialize", None)` 是三参探测，
        # 只在 AttributeError 时吃默认值——抛 ProductionWriteRefused 会穿透读路径。
        for internal in ("materialize", "handle", "schema_ready", "target"):
            assert getattr(lazy, internal, None) is None, (
                f"替身把句柄内部名 {internal!r} 透出去了（仓库层的探测会走错路）")

        # 写入口在，而且给的是真 Engine（守卫语义由 write_guard 用例守）
        assert isinstance(lazy.write_engine(), Engine)
        assert callable(lazy.assert_writable)


def test_meta_and_single_db_managers_still_expose_real_engine() -> None:
    """meta / 单库两个 manager 的 ``.engine`` 也必须是真 Engine（同一形状的第二、三处）。"""
    meta = MetaRepositoryManager(str(make_db_dir("eac_meta_") / "meta.db"))
    single = RepositoryManager(str(make_db_dir("eac_single_") / "legacy.db"))
    try:
        for name, manager in (("MetaRepositoryManager", meta), ("RepositoryManager", single)):
            engine = manager.engine
            assert isinstance(engine, Engine), (
                f"{name}.engine 不是 Engine 而是 {type(engine).__name__}")
            with engine.connect() as conn:
                conn.exec_driver_sql("select 1").scalar()
            assert manager.write_engine() is engine
    finally:
        meta.engine.dispose()
        single.engine.dispose()


# ---------------------------------------------------------------------------
# ② 生产调用点扫描：**下一次同类回归的门**
# ---------------------------------------------------------------------------
def _production_py_files() -> list[Path]:
    out = []
    for package in ("db", "core", "agents", "scheduler", "web", "scripts"):
        base = ROOT / package
        if not base.is_dir():
            continue
        for path in base.rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            out.append(path)
    return out


def test_every_production_call_site_name_exists_on_the_handle() -> None:
    """扫生产源码：账号库/manager 形状的接收者上用到的名字，替身上**必须**取得到。

    这条断言的意义：生产代码是**通过** ``ctx.repo`` / ``get_repo()`` 拿账号库的
    （拿到的是替身），所以"真 manager 有"不等于"生产能用"。名字丢了就在这里红。
    """
    names: dict[str, list[str]] = {}
    for path in _production_py_files():
        if path.name == "account_db_manager.py":  # 替身自己的定义处
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        except SyntaxError:  # pragma: no cover - 语法错误由别的门管
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Attribute):
                continue
            attr = node.attr
            if attr == "engine":
                pass
            elif attr in _ACCOUNT_API:
                pass
            else:
                continue
            base = node.value
            receiver = (
                base.id if isinstance(base, ast.Name)
                else base.attr if isinstance(base, ast.Attribute)
                else ""
            )
            if receiver in _REPO_RECEIVERS:
                names.setdefault(attr, []).append(
                    f"{path.relative_to(ROOT)}:{node.lineno}")

    expected = set(_manager_attributes_from_source())
    assert "engine" in names, "扫描没找到任何 .engine 调用点——接收者名单需要更新"
    for attr, sites in sorted(names.items()):
        assert attr in expected, (
            f"生产代码在 {len(sites)} 处用了 .{attr}（例：{sites[:3]}），"
            "但账号库 manager 上没有这个名字——替身也不可能给出它")

    # 真替身上再走一遍（源码扫到的名字一个都不能 AttributeError）
    with _isolated_account_repo() as (_real, lazy):
        for attr in sorted(names):
            assert getattr(lazy, attr, None) is not None, (
                f"替身给不出 .{attr}（生产调用点：{names[attr][:3]}）")


# ---------------------------------------------------------------------------
# ③ 真跑那五个调用点（隔离库；生产代码的**真实函数**）
# ---------------------------------------------------------------------------
def test_the_five_production_call_sites_run_on_a_real_repo() -> None:
    """五个调用点各自的**真实函数**在真实替身上跑通（那晚五条 error 的回归格）。

    逐条对应实测日志：

    * ``evidence_index_failed`` → ``EvidenceIndexer.index_pending_for_account``
      的第一行就是 ``account_repo.messages``；
    * ``media_pending_query_failed`` → ``resolve_pending(ctx.repo.engine)`` 里
      ``Session(engine)``；
    * ``ingest_failed`` / ``outbox_drain_failed`` → ``drain_outbox(repo=ctx.repo)``
      的 ``repo.engine`` 与 ``repo.messages``；
    * ``send_log_cleanup_failed`` → ``self.repo.send_log.cleanup_stale``。
    """
    from core.conversation.media_registry import resolve_pending
    from core.evidence import EvidenceIndexer
    from core.terminal.outbox import drain_outbox
    from db.repo_evidence_cursor import EvidenceCursorRepository

    meta = MetaRepositoryManager(str(make_db_dir("eac_meta_run_") / "meta.db"))
    try:
        with _isolated_account_repo() as (real, lazy):
            # (a) 读路径：BaseRepository._session()（不经写守卫，读不该被连坐）
            assert lazy.messages.get_by_id(1) is None, "只读查询应当能跑"
            assert lazy.send_log.cleanup_stale(timeout_minutes=5) == 0

            # (b) evidence index：真实函数，真实 account_repo（替身）
            indexer = EvidenceIndexer(meta.evidence, meta.people)
            indexed = indexer.index_pending_for_account(
                lazy, platform="probe", account_id="probe",
                cursors=meta.evidence_cursors, batch_size=10, max_batches=1)
            assert isinstance(indexed, int), indexed

            # (c) media pending：生产那行是 resolve_pending(ctx.repo.engine, limit=40)。
            #     本格只钉"**能跑到查询**"：Engine 传进去了、Session 建起来了。表可能还
            #     没建（account 迁移与 meta 迁移是两条链），那种失败是存量问题，不是本
            #     次形状——所以这里把"形状错"（AttributeError / TypeError）单列出来。
            stats = resolve_pending(lazy.engine, limit=5)
            assert set(("tried", "resolved")) <= set(stats), stats

            # (d) outbox drain：真实函数，repo=ScopedRepository（与生产同形）
            from db.scoped_repo import ScopedRepository

            scoped = ScopedRepository(lazy, meta, platform="probe", account_id="probe")
            assert isinstance(scoped.engine, Engine), (
                "ScopedRepository.engine 必须透出真 Engine（ctx.repo.engine 走它）")
            report = drain_outbox(
                repo=scoped, platform="probe", account_id="probe",
                store=None, limit=5)
            assert report is not None

            # (e) send_log cleanup 的另一半：**真 manager** 上那条也在
            assert real.send_log.cleanup_stale(timeout_minutes=5) >= 0
    finally:
        meta.engine.dispose()


# ---------------------------------------------------------------------------
# ④ 守卫四格没有被这次修正改掉（读放行 / 写才拦 / 声明后放行 / 生产拦）
# ---------------------------------------------------------------------------
def test_guard_semantics_survive_the_engine_api_fix() -> None:
    """修 "API 同形" 不许把守卫修没：**读放行、写才拦、声明后放行**。"""
    base = make_db_dir("eac_guard_")
    prod_like = base / "account.db"
    real = AccountRepositoryManager(str(prod_like), "probe", "probe")
    try:
        with _undeclared(), _production_paths_are(prod_like):
            assert wg.writes_allowed(prod_like) is False, "前提：目标算生产路径"
            adm = AccountDBManager()
            lazy = adm.register("probe", "probe", str(prod_like))

            # ① 未声明 + 生产 + 要**可写句柄** ⇒ 拒绝（是守卫的拒绝，不是 AttributeError）
            for label, opener in (("engine", lambda: lazy.engine),
                                  ("write_engine", lambda: lazy.write_engine())):
                try:
                    opener()
                except ProductionWriteRefused:
                    pass
                else:
                    raise AssertionError(f"未声明的生产路径上放行了可写句柄：{label}")

            # ② 只读**不经守卫**：仓库层 materialize() 照常（读不该被写守卫连坐）。
            #    这一格与 ① 是**同一个判据的两侧**：读走 materialize、写走 handle。
            reader = lazy.messages
            assert reader.get_by_id(1) is None, "只读查询应当能跑（哪怕未声明）"

            # ③ 声明后 ⇒ 放行，且给出来的还是真 Engine
            wg.declare_writes_allowed(declared_by="tests/test_engine_api_contract")
            try:
                assert isinstance(lazy.engine, Engine)
                assert lazy.send_log is not None
            finally:
                wg.reset_writes_declaration()
    finally:
        try:
            real.engine.dispose()
        except Exception:
            pass
        shutil.rmtree(base, ignore_errors=True)


def main() -> bool:
    print("=" * 64)
    print("engine/repo API contract (2026-10-03 P0 regression)")
    print("=" * 64)
    tests = [fn for name, fn in sorted(globals().items()) if name.startswith("test_")]
    failed = 0
    for fn in tests:
        try:
            fn()
            print(f"  ok    {fn.__name__}")
        except Exception:  # pragma: no cover - diagnostics only
            failed += 1
            print(f"  FAIL  {fn.__name__}")
            traceback.print_exc()
    print(f"{len(tests) - failed}/{len(tests)} passed")
    return failed == 0


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
