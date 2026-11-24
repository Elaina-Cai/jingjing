# -*- coding: utf-8 -*-
"""「**确实有 pending 迁移**才拒绝」的迁移守卫用例（收窄后的判据）。

背景（今晚真实事故 + 两次同类前科）
--------------------------------
``core/bootstrap.py::init_singletons`` 会一路跑到 ``init_meta_db`` + ``run_migrations``
（meta 库）与 ``AccountDBManager.register → run_migrations``（各账号库）。根因级隔离
只在 runner 一层做过（``scripts/run_all_tests.py`` 的「快照 + 隔离根」），所以**直接
import 的冒烟脚本**与"单跑一个测试文件"那条路仍然打得穿：2026-10-02 一次冒烟就把
``data/yinyue_meta.db`` 与 4 个账号库推到了 schema 55（前科见 `docs/archive/任务清单_2026-10-06.md:207`：
Migration 50 本地 2026-09-29 21:32、51 本地 22:58:19）。

第一版守卫的判据是"非声明进程 + 生产路径 → **一律**拒绝"，它把**不需要迁移**的合法
维护脚本一起堵死了（``scripts/reimport_speech_seed.py`` 这类走启动链的脚本，库已是最新
却照样抛错）。守卫要拦的是**迁移这个动作**：没有 pending 时 ``run_migrations`` 一个版本
都不会跑。现在 ``core/bootstrap.py::assert_migrations_allowed`` 的判据是**三件事一起**：

1. 目标库 == ``production_data_paths()`` 里的一条（**唯一**的路径来源，本文件不认第二份）；
2. 该库**确实还有 pending 迁移**（``applied_schema_version`` <
   ``migration_chain_target_version``，后者读 ``db/migrations.py`` 的 ``@migration``
   注册表，不写死版本号）；
3. 本进程没有声明（``allow_production=True`` 参数，或入口的
   ``init_singletons(allow_production=True)``）。

四格（本文件逐格实测，缺一格就是没测）
------------------------------------
* ① 生产路径 + **有 pending** + 无声明 → **拒绝**，且库未被改；
* ② 生产路径 + **无 pending**（库已最新）+ 无声明 → **放行**，且库未被改；
* ③ **声明**（参数 / 入口 / 子进程）→ 放行（有 pending 也放行：声明优先）；
* ④ **测试进程 + 隔离路径** → 照常迁移（**不许误伤**；隔离库真有 pending，版本从链目标
  之下回到链目标——不是只"调了一下 run_migrations"）；

外加两格取证：

* ⑤ ``scheduler/main.py`` 那一行声明在**子进程**里真的能把门打开（没有它 → 拒绝）；
* ⑥ ``scripts/reimport_speech_seed.py``（今晚被误伤的那类维护脚本）在"生产形状 + 已最新"
  的目标上**跑得完**，而生产库一个字节都没动。同一晚的**写侧**守卫
  （``db/write_guard.py``）与迁移门读**同一个路径来源**，所以这一格只给"允许写"的声明、
  **不给**迁移声明（给了就什么都没测到）——见该格与探针里的注释。

隔离（**本文件对生产库只读**）
----------------------------
* ``LOG_DIR`` / ``META_DB_PATH`` / ``PLATFORMS_DIR`` / ``DATA_DIR`` 全部指向仓库内隔离
  目录；隔离库用 ``sqlite3 Connection.backup()`` 从快照复制（源库**只读**打开）；
* 断言"库没被迁移"读的是 ``_schema_version`` 的**全部版本行** + 库文件 sha256
  （都是只读读数）；
* "有 pending"那一格由**种子库**（只写一行 ``_schema_version``）或"删掉高版本行的副本"
  造出来，绝不碰生产库；
* "声明 → 放行"与"跑得完"那两格把目标换成本地副本/隔离库（目标**路径身份**仍由
  ``production_data_paths`` 这一处来源给出），生产文件只被 ``mode=ro`` 读过。

测试卫生：本文件依赖的开关 import 期显式钉死（"关"一律显式 ``off``，不靠"env 未设"）；
本文件**不引入任何新开关**——守卫的判据不是 flag，``allow_production`` 是参数不是 env。

Run directly::

    .\\.venv\\Scripts\\python.exe tests\\test_bootstrap_migration_guard.py
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import subprocess
import sys
import traceback
from contextlib import contextmanager, nullcontext
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, str(Path(__file__).resolve().parent))

ROOT = Path(__file__).resolve().parent.parent
_TEST_LOG_DIR = str(Path("data") / "_test_logs_bootstrap_guard")
Path(_TEST_LOG_DIR).mkdir(parents=True, exist_ok=True)
os.environ["LOG_DIR"] = _TEST_LOG_DIR
_ISO_ROOT = (ROOT / "data" / "_bootstrap_guard_test" / "_iso").resolve()
_ISO_ROOT.mkdir(parents=True, exist_ok=True)
os.environ["META_DB_PATH"] = str(_ISO_ROOT / "meta.db")
os.environ["PLATFORMS_DIR"] = str(_ISO_ROOT / "platforms")
os.environ["DATA_DIR"] = str(_ISO_ROOT / "data")

import core.infra.logging as _logging  # noqa: E402

_logging.init_logging = lambda *args, **kwargs: None

#: 本文件依赖的开关一律显式钉死（"关"就是 ``off``，不靠"env 未设"）。
#: ``CLAIM_LEDGER`` 是迁移链末端（56）那张表的读侧总闸：本文件会真跑一次迁移链，
#: 显式钉 ``off`` 才能保证"链跑到 56"与"台账逻辑接管出口"两件事不混在一起。
_PINS = {
    "YINYUE_FEATURE_EXEC_RECEIPT": "off",
    "YINYUE_FEATURE_SLOT_FILL": "off",
    "YINYUE_FEATURE_PLAN_LOOP": "off",
    "YINYUE_FEATURE_CLAIM_LEDGER": "off",
}
os.environ.update(_PINS)

from core import bootstrap  # noqa: E402
from core.config.settings import config  # noqa: E402
from core.env import running_under_tests  # noqa: E402
from _tempdirs import make_db_dir  # noqa: E402

#: 生产库的只读打开口径（**绝不给生产文件写权**）。
_PROD_META = ROOT / "data" / "yinyue_meta.db"


def _pin() -> None:
    os.environ.update(_PINS)


# ---------------------------------------------------------------------------
# 只读读数：版本行 / 文件指纹 / 快照 / 造 pending
# ---------------------------------------------------------------------------
def _schema_version(path) -> int | None:
    """只读读出 ``_schema_version`` 的 max(version)（库不存在/无表 → ``None``）。"""
    p = Path(path)
    if not p.exists():
        return None
    con = sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True)
    try:
        row = con.execute("select max(version) from _schema_version").fetchone()
    except sqlite3.Error:
        return None
    finally:
        con.close()
    return int(row[0]) if row and row[0] is not None else None


def _schema_rows(path) -> tuple[int, ...]:
    """只读读出 ``_schema_version`` 的**全部**版本行（"库没被迁移"的强读数）。"""
    p = Path(path)
    if not p.exists():
        return ()
    con = sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True)
    try:
        try:
            rows = con.execute(
                "select version from _schema_version order by version"
            ).fetchall()
        except sqlite3.Error:
            return ()
    finally:
        con.close()
    return tuple(int(r[0]) for r in rows)


def _sha256(path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _tuple_line(text: str, prefix: str) -> tuple:
    """从子进程输出里取一行 ``prefix`` 的元组读数（``ast.literal_eval``，不 eval）。"""
    import ast

    line = [ln for ln in text.splitlines() if ln.startswith(prefix)]
    assert line, f"子进程输出里没有 {prefix} 那一行：\n{text}"
    value = ast.literal_eval(line[0][len(prefix):].strip())
    assert isinstance(value, tuple), value
    return value


def _fingerprint(path) -> tuple[str, tuple[int, ...]]:
    """隔离库的"一个字节都没动"指纹（文件 sha256 + 全部版本行）。"""
    p = Path(path)
    return (_sha256(p) if p.exists() else "", _schema_rows(p))


def _snapshot(src: Path, dest: Path) -> None:
    """一致性快照（源库 ``mode=ro`` 打开；目标在隔离目录里）。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(f"file:{src.as_posix()}?mode=ro", uri=True)
    try:
        target = sqlite3.connect(str(dest))
        try:
            source.backup(target)
        finally:
            target.close()
    finally:
        source.close()


def _seed_stale_db(path: Path, version: int) -> None:
    """建一个"停在旧版本"的库：只写一行 ``_schema_version``（守卫的判据只读它）。"""
    con = sqlite3.connect(str(path))
    try:
        con.execute(
            "CREATE TABLE _schema_version ("
            "version INTEGER PRIMARY KEY, description TEXT, applied_at DATETIME)"
        )
        con.execute(
            "INSERT INTO _schema_version (version, description) VALUES (?, 'guard seed')",
            (version,),
        )
        con.commit()
    finally:
        con.close()


def _stale_copy_of_prod(dest: Path) -> int:
    """生产 meta 库的**副本** + 删掉 ``_schema_version`` 里 >= 链目标的行（造 pending）。

    删的是隔离根里的副本；生产库只被 ``mode=ro`` 读过。返回副本现在的版本。
    """
    _snapshot(_PROD_META, dest)
    target = bootstrap.migration_chain_target_version()
    con = sqlite3.connect(str(dest))
    try:
        con.execute("delete from _schema_version where version >= ?", (target,))
        con.commit()
    finally:
        con.close()
    current = bootstrap.applied_schema_version(dest)
    assert current is not None and current < target, (current, target)
    return current


# ---------------------------------------------------------------------------
# 判据层的开关（都不改生产代码的行为：只改测试一侧的输入/进程身份）
# ---------------------------------------------------------------------------
@contextmanager
def _production_paths_are(*paths: object):
    """把**唯一**那份生产路径来源（``production_data_paths``）临时指向测试给的路径。

    判据的调用处（``production_paths_for`` → ``production_data_paths()``）一个字没改：
    本上下文只换"喂进去的路径"，不新增第二份路径判断；真实来源在退出时还原。
    """
    saved = bootstrap.production_data_paths
    bootstrap.production_data_paths = lambda: tuple(
        (str(p), "guard 用例注入的生产路径") for p in paths
    )
    try:
        yield
    finally:
        bootstrap.production_data_paths = saved


@contextmanager
def _isolated_paths(meta_db: Path, platforms_dir: Path, data_dir: Path):
    """把 bootstrap 与 config 的目标路径都改到隔离根（并还原）。

    ``config.paths`` 是 ``AccountConfig.resolve_paths`` 的来源：账号库路径在
    ``config.all_account_configs()`` 里**按调用时的 paths 现算**，所以这里改一处
    就够（不给它第二份口径）。
    """
    saved_meta = bootstrap.META_DB_PATH
    saved_paths = (config.paths.platforms_dir, config.paths.data_dir)
    bootstrap.META_DB_PATH = str(meta_db)
    config.paths.platforms_dir = Path(platforms_dir)
    config.paths.data_dir = Path(data_dir)
    try:
        yield
    finally:
        bootstrap.META_DB_PATH = saved_meta
        config.paths.platforms_dir, config.paths.data_dir = saved_paths


@contextmanager
def _not_under_tests():
    """假装"非测试进程"（只在本进程内；子进程另有对照）。"""
    saved = bootstrap.running_under_tests
    bootstrap.running_under_tests = lambda: False
    try:
        yield
    finally:
        bootstrap.running_under_tests = saved


class _MigrateSpy:
    """记录 ``run_migrations`` 的真调用（**仍然调用真的那个**，不替换行为）。"""

    def __init__(self):
        self.calls: list[str] = []
        self._real = bootstrap.run_migrations

    def __call__(self, db_path, *args, **kwargs):
        self.calls.append(str(db_path))
        return self._real(db_path, *args, **kwargs)


@contextmanager
def _spy_migrations():
    spy = _MigrateSpy()
    saved = bootstrap.run_migrations
    bootstrap.run_migrations = spy
    try:
        yield spy
    finally:
        bootstrap.run_migrations = saved


@contextmanager
def _quiet_init_dirs():
    """``init_dirs`` 会在仓库里建目录：跑这条用例时只留隔离库这一件事。"""
    saved = bootstrap.init_dirs
    bootstrap.init_dirs = lambda: None
    try:
        yield
    finally:
        bootstrap.init_dirs = saved


def _close_handles(singletons: dict) -> None:
    """尽力关掉用例里起过的库句柄（Windows 上句柄不关就删不掉隔离目录）。

    只服务**清理**：断言早已跑完，失败一律吞掉（清理不该改变任何判据）。
    """
    for value in list(singletons.values()):
        for holder in (value, getattr(value, "client", None)):
            for attr in ("engine", "client"):
                handle = getattr(holder, attr, None)
                closer = getattr(handle, "dispose", None) or getattr(handle, "close", None)
                if callable(closer):
                    try:
                        closer()
                    except Exception:  # noqa: BLE001 - 清理尽力而为
                        pass


@contextmanager
def _fresh_singletons():
    """本用例跑完把单例位复位（否则后面 import 的用例会拿到半初始化的实例）。"""
    saved = (dict(bootstrap._singletons), bootstrap._initialized)
    try:
        yield
    finally:
        _close_handles(bootstrap._singletons)
        bootstrap._singletons.clear()
        bootstrap._singletons.update(saved[0])
        bootstrap._initialized = saved[1]


@contextmanager
def _no_declaration():
    """临时撤掉本进程的"生产迁移已声明"位（判据层用例要测**未声明**那一格）。"""
    saved = bootstrap.production_migration_declared()
    bootstrap._reset_production_declaration()
    try:
        yield
    finally:
        bootstrap._reset_production_declaration()
        if saved:
            bootstrap._PRODUCTION_DECLARED = True


def _refusal(target, **kwargs) -> str:
    """跑一次守卫并**要求它拒绝**，返回错误消息（不拒绝 = 用例红）。"""
    try:
        bootstrap.assert_migrations_allowed(target, **kwargs)
    except RuntimeError as exc:
        return str(exc)
    raise AssertionError(f"守卫没有拒绝：target={target} kwargs={kwargs}")


def _assert_refusal_message(message: str, target, *, current=None, target_version=None) -> None:
    """拒绝消息必须带齐：目标路径、"这是生产库"、依赖 pending 的证据、声明写法、隔离做法、R-级理由。"""
    assert str(target) in message, message
    assert "生产库" in message, "消息要说清「这是生产库」"
    assert "pending" in message, message
    if current is not None:
        assert f"当前 schema 版本 {current}" in message, message
    if target_version is not None:
        assert f"迁移链目标 {target_version}" in message, message
    assert "allow_production=True" in message, "消息要给显式声明的写法"
    assert "同类事故已两次" in message, message
    assert "docs/archive/任务清单_2026-10-06.md:207" in message, "R-级理由要能被追到具体事故记录"
    assert "scripts/run_all_tests.py" in message and "tests/_tempdirs.py" in message, message


# ---------------------------------------------------------------------------
# ① 生产路径 + **有 pending** + 无声明 → 拒绝，且库未被改
# ---------------------------------------------------------------------------
def test_production_path_with_pending_is_refused() -> None:
    _pin()
    target_version = bootstrap.migration_chain_target_version()
    assert target_version > 1, "迁移链目标版本读不到（判据的另一半没了）"

    base = make_db_dir("bs_pending_")
    try:
        stale = base / "meta.db"
        _seed_stale_db(stale, target_version - 1)
        before = _fingerprint(stale)
        assert bootstrap.applied_schema_version(stale) == target_version - 1

        with _production_paths_are(stale):
            # (a) 判据层：这条路径**确实**算生产路径（来源只有 production_data_paths 一处）
            assert bootstrap.production_paths_for(stale) == (str(stale),)

            # (b) 非测试进程 + 无声明 → 拒绝（维护脚本的形状；旧实现这里被堵死）
            #     测试进程同样拒绝：判据里没有"是不是测试进程"这一项
            for ctx in (_not_under_tests(), nullcontext()):
                with ctx, _no_declaration():
                    message = _refusal(stale)
                    _assert_refusal_message(
                        message, stale,
                        current=target_version - 1, target_version=target_version,
                    )

            # (c) schema 版本读不到（库不存在 / 没有 _schema_version 表）→ 按有 pending 处理
            ghost = base / "ghost.db"
            with _production_paths_are(ghost):
                with _not_under_tests(), _no_declaration():
                    message = _refusal(ghost)
                assert "视作还有 pending" in message, message
            assert not ghost.exists(), "守卫把目标库建出来了（判据必须是只读的）"

        # (d) 库真的没被改（逐字节 + 全部版本行）
        assert _fingerprint(stale) == before, "拒绝路径上动了目标库"
    finally:
        shutil.rmtree(base, ignore_errors=True)


# ---------------------------------------------------------------------------
# ①（接线）拒绝发生在**碰任何库之前**：init_dirs / init_meta_db / run_migrations 都不进
# ---------------------------------------------------------------------------
def test_guard_is_the_first_gate_of_init_singletons() -> None:
    """目标 = 生产路径 + 有 pending 时，连 init_dirs / run_migrations 都不进。"""
    _pin()
    target_version = bootstrap.migration_chain_target_version()
    base = make_db_dir("bs_firstgate_")
    try:
        stale = base / "meta.db"
        _seed_stale_db(stale, target_version - 1)
        before = _fingerprint(stale)

        called: list[str] = []
        saved = (bootstrap.init_dirs, bootstrap.run_migrations, bootstrap.init_meta_db)
        bootstrap.init_dirs = lambda: called.append("init_dirs")
        bootstrap.run_migrations = lambda *a, **k: called.append("run_migrations")
        bootstrap.init_meta_db = lambda *a, **k: called.append("init_meta_db")
        try:
            with _fresh_singletons(), _isolated_paths(
                stale, base / "platforms", base / "data",
            ), _production_paths_are(stale), _not_under_tests(), _no_declaration():
                bootstrap._initialized = False
                bootstrap._singletons.clear()
                try:
                    bootstrap.init_singletons(validate=False)
                except RuntimeError as exc:
                    assert str(stale) in str(exc), exc
                else:
                    raise AssertionError("init_singletons 在「有 pending 的生产路径」上没有拒绝")
        finally:
            bootstrap.init_dirs, bootstrap.run_migrations, bootstrap.init_meta_db = saved
        assert called == [], f"拒绝之前就已经动了库/目录：{called}"
        assert _fingerprint(stale) == before, "门之前就写了目标库"
    finally:
        shutil.rmtree(base, ignore_errors=True)


# ---------------------------------------------------------------------------
# ② 生产路径 + **无 pending**（库已最新）+ 无声明 → 放行，且库未被改
# ---------------------------------------------------------------------------
#: 子进程：**非测试进程** + 真生产路径 + 无声明 → 门必须放行（库已最新）。
#: 只调判据，不调 init_singletons（用例绝不让启动链碰到生产文件）。
_PROD_GATE_PROBE = """\
import sys
sys.path.insert(0, %(root)r)
from core.env import running_under_tests
from core import bootstrap
target = %(prod)r
print('running_under_tests=', running_under_tests())
print('production_hits=', len(bootstrap.production_paths_for(target)))
print('applied=', bootstrap.applied_schema_version(target))
print('chain_target=', bootstrap.migration_chain_target_version())
bootstrap.assert_migrations_allowed(target)
print('allowed=True')
"""


def test_production_path_without_pending_is_allowed_and_untouched() -> None:
    """维护脚本那一格：库已是最新 → 放行，但不迁移、不改库。"""
    _pin()
    target_version = bootstrap.migration_chain_target_version()
    current = bootstrap.applied_schema_version(_PROD_META)
    assert current is not None, "前提：生产 meta 库存在（本用例只读它）"
    assert current >= target_version, (
        f"前提：真实生产库现在没有 pending（读到的版本 {current} < {target_version}）——"
        "若它真有 pending，本格测不到'无 pending 放行'，请先看 ①"
    )
    before = (_schema_version(_PROD_META), _schema_rows(_PROD_META))

    # (a) 判据层：真生产路径 + 无声明 → 放行（测试进程与非测试进程都一样放行）
    for ctx in (nullcontext(), _not_under_tests()):
        with ctx, _no_declaration():
            bootstrap.assert_migrations_allowed(_PROD_META)          # 不抛 = 放行
    # 声明位没有被"放行"这件事改掉（放行 ≠ 声明）
    with _no_declaration():
        assert bootstrap.production_migration_declared() is False

    # (b) 生产路径的**父目录/兄弟路径**照旧不算命中（隔离根就建在 data/ 下）
    with _no_declaration():
        bootstrap.assert_migrations_allowed(_ISO_ROOT / "meta.db")

    # (c) 判据本身不迁移：把 run_migrations 换成炸弹，放行路径一个字节都不许动
    def _boom(*a, **k):  # pragma: no cover - 只有判据越界去迁移才会响
        raise AssertionError("判据（assert_migrations_allowed）自己跑了 run_migrations")

    saved = bootstrap.run_migrations
    bootstrap.run_migrations = _boom
    try:
        with _no_declaration():
            bootstrap.assert_migrations_allowed(_PROD_META)
    finally:
        bootstrap.run_migrations = saved

    # (d) 库未变：版本行与 max(version) 都不动（生产库只被只读读过）
    assert (_schema_version(_PROD_META), _schema_rows(_PROD_META)) == before, \
        "放行路径上改了生产库"

    # (e) 子进程对照：非测试进程 + 真生产路径 + 无声明 → 放行（判据那一行就是启动链的第一件事）
    probe = _ISO_ROOT / "_bootstrap_guard_up_to_date_probe.py"
    env = dict(os.environ)
    env.update(_PINS)
    env.update({
        "META_DB_PATH": str(_ISO_ROOT / "meta.db"),
        "PLATFORMS_DIR": str(_ISO_ROOT / "platforms"),
        "DATA_DIR": str(_ISO_ROOT / "data"),
        "LOG_DIR": _TEST_LOG_DIR,
        # 只影响 .env 开关类键的注入（守卫判据不读任何 flag），与 allow_production 无关
        "YINYUE_TEST_NO_ENV_FLAGS": "1",
        "PYTHONIOENCODING": "utf-8",
    })
    env.pop("PYTEST_CURRENT_TEST", None)
    probe.write_text(
        _PROD_GATE_PROBE % {"root": str(ROOT), "prod": str(_PROD_META)},
        encoding="utf-8",
    )
    try:
        proc = subprocess.run(
            [sys.executable, str(probe)],
            cwd=str(ROOT), env=env, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=300,
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr
        out = proc.stdout
        assert "running_under_tests= False" in out, out
        assert "production_hits= 1" in out, f"真生产路径没被认出来：\n{out}"
        assert f"applied= {current}" in out and f"chain_target= {target_version}" in out, out
        assert "allowed=True" in out, out
    finally:
        probe.unlink(missing_ok=True)
    assert (_schema_version(_PROD_META), _schema_rows(_PROD_META)) == before, \
        "子进程放行路径上改了生产库"


# ---------------------------------------------------------------------------
# ③ 声明 → 放行（照旧；且**有 pending 也放行**：声明优先）
# ---------------------------------------------------------------------------
def test_explicit_declaration_allows_production_path() -> None:
    _pin()
    target_version = bootstrap.migration_chain_target_version()
    before = (_schema_version(_PROD_META), _schema_rows(_PROD_META))

    # (a) 参数声明：``allow_production=True``（服务入口的写法）
    with _not_under_tests(), _no_declaration():
        bootstrap.assert_migrations_allowed(_PROD_META, allow_production=True)

    # (b) **有 pending** + 声明 → 照样放行（声明优先：连版本都不用读）
    base = make_db_dir("bs_declared_")
    stale = base / "meta.db"
    _seed_stale_db(stale, target_version - 1)
    stale_before = _fingerprint(stale)
    with _production_paths_are(stale):
        assert bootstrap.production_paths_for(stale) == (str(stale),)
        for ctx in (_not_under_tests(), nullcontext()):
            with ctx, _no_declaration():
                _refusal(stale)                                  # 无声明 → 拒绝（①已实测）
                # 声明（参数）→ 放行：**有 pending 也放行**，声明优先
                bootstrap.assert_migrations_allowed(stale, allow_production=True)
        # 判据自己不许迁移（迁移是 init_singletons 后面的事）
        saved = bootstrap.run_migrations

        def _boom(*a, **k):  # pragma: no cover - 判据越界去迁移才会响
            raise AssertionError("判据（assert_migrations_allowed）自己跑了 run_migrations")

        bootstrap.run_migrations = _boom
        try:
            with _not_under_tests(), _no_declaration():
                bootstrap.assert_migrations_allowed(stale, allow_production=True)
        finally:
            bootstrap.run_migrations = saved
    assert _fingerprint(stale) == stale_before, "声明放行路径上动了目标库"

    # (c) **服务入口那一行**：``init_singletons(allow_production=True)`` 在"生产形状"的
    #     目标上不走拒绝分支，而且会把"本进程已声明"记下来供后续调用复用。
    #     用 spy 把"真的去迁移"换掉，目标是一个**副本**（绝不让启动链碰生产文件）。
    copy = base / "entry_meta.db"
    _stale_copy_of_prod(copy)
    called: list[str] = []
    saved = (bootstrap.init_dirs, bootstrap.run_migrations, bootstrap.init_meta_db)
    bootstrap.init_dirs = lambda: None
    bootstrap.run_migrations = lambda *a, **k: called.append("run_migrations")
    bootstrap.init_meta_db = lambda *a, **k: called.append("init_meta_db")
    try:
        with _fresh_singletons(), _isolated_paths(
            copy, base / "platforms", base / "data",
        ), _production_paths_are(copy), _not_under_tests():
            bootstrap._initialized = False
            bootstrap._singletons.clear()
            bootstrap._reset_production_declaration()
            bootstrap.init_singletons(validate=False, allow_production=True)
            assert bootstrap.production_migration_declared() is True, \
                "入口声明没有被记下来（后续 init_singletons 调用会又被拒）"
            assert called, "显式声明之后连迁移入口都没走到（服务会被堵死）"
            # 同进程后续调用（无参）复用地条声明 → 不再拒绝
            bootstrap._initialized = False
            bootstrap._singletons.clear()
            bootstrap.init_singletons(validate=False)
    finally:
        bootstrap.init_dirs, bootstrap.run_migrations, bootstrap.init_meta_db = saved
        bootstrap._reset_production_declaration()
    assert (_schema_version(_PROD_META), _schema_rows(_PROD_META)) == before, \
        "本用例不许改生产库"

    # (d) 子进程对照：真的不注册 pytest；无声明 → 拒绝，显式声明 → 放行
    probe = _ISO_ROOT / "_bootstrap_guard_pending_probe.py"
    declared = _ISO_ROOT / "_bootstrap_guard_pending_probe_declared.py"
    env = dict(os.environ)
    env.update(_PINS)
    env.update({
        "META_DB_PATH": str(_ISO_ROOT / "meta.db"),
        "PLATFORMS_DIR": str(_ISO_ROOT / "platforms"),
        "DATA_DIR": str(_ISO_ROOT / "data"),
        "LOG_DIR": _TEST_LOG_DIR,
        "YINYUE_TEST_NO_ENV_FLAGS": "1",
        "PYTHONIOENCODING": "utf-8",
    })
    env.pop("PYTEST_CURRENT_TEST", None)
    source = (
        "import sys\n"
        "sys.path.insert(0, %(root)r)\n"
        "from core.env import running_under_tests\n"
        "from core import bootstrap\n"
        "target = %(target)r\n"
        "bootstrap.production_data_paths = lambda: ((target, 'probe: 生产形状'),)\n"
        "print('running_under_tests=', running_under_tests())\n"
        "print('production_hit=', bootstrap.production_paths_for(target))\n"
        "print('pending_gap=', bootstrap.applied_schema_version(target),\n"
        "      bootstrap.migration_chain_target_version())\n"
        "bootstrap.assert_migrations_allowed(target%(declared)s)\n"
        "print('allowed=True')\n"
    )
    try:
        probe.write_text(
            source % {"root": str(ROOT), "target": str(stale), "declared": ""},
            encoding="utf-8",
        )
        proc = subprocess.run(
            [sys.executable, str(probe)],
            cwd=str(ROOT), env=env, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=180,
        )
        assert proc.returncode != 0, (
            f"非测试进程 + 有 pending + 无声明居然放行了：\n{proc.stdout}")
        assert "拒绝迁移" in (proc.stdout + proc.stderr), proc.stdout + proc.stderr
        assert "running_under_tests= False" in proc.stdout, proc.stdout
        assert "production_hit=" in proc.stdout and str(stale) in proc.stdout, proc.stdout

        declared.write_text(
            source % {"root": str(ROOT), "target": str(stale),
                      "declared": ", allow_production=True"},
            encoding="utf-8",
        )
        proc = subprocess.run(
            [sys.executable, str(declared)],
            cwd=str(ROOT), env=env, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=180,
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "allowed=True" in proc.stdout, proc.stdout
    finally:
        probe.unlink(missing_ok=True)
        declared.unlink(missing_ok=True)
        assert _fingerprint(stale) == stale_before, "子进程对照动了目标库"
        shutil.rmtree(base, ignore_errors=True)


# ---------------------------------------------------------------------------
# ④ 测试进程 + 隔离路径 → 照常迁移（不许误伤）
# ---------------------------------------------------------------------------
def test_tests_process_on_isolated_path_migrates_normally() -> None:
    """④ 测试进程 + 隔离路径 → 照常迁移（**真的迁移**：版本从链目标之下回到链目标）。"""
    _pin()
    base = make_db_dir("bs_guard_")
    try:
        meta = base / "data" / "yinyue_meta.db"
        meta.parent.mkdir(parents=True, exist_ok=True)
        # 快照一份真实库的形状（源库只读）+ 把版本行退到链目标之下 → 隔离库**真有 pending**，
        # 否则"照常迁移"这一格测不到任何迁移（run_migrations 会被调用但没有版本可跑）。
        before = _stale_copy_of_prod(meta)
        target_version = bootstrap.migration_chain_target_version()
        assert before < target_version, (before, target_version)

        # 判据层：隔离路径不是生产路径 → 放行（不必声明、不必看版本）
        assert bootstrap.production_paths_for(meta) == ()
        with _no_declaration():
            bootstrap.assert_migrations_allowed(meta)

        # 接线层：真跑一次迁移链（只碰隔离库）
        with _fresh_singletons(), _spy_migrations() as spy, _quiet_init_dirs():
            with _isolated_paths(meta, base / "data" / "platforms", base / "data"):
                bootstrap._initialized = False
                bootstrap._singletons.clear()
                bootstrap.init_singletons(validate=False)

        assert str(meta) in spy.calls, f"隔离路径没跑迁移：{spy.calls}"
        after = _schema_version(meta)
        assert after == target_version, f"隔离库没被迁到链目标：{before} → {after}"
        assert before < after, "隔离库没有真的被迁移（本格测的是'照常迁移'）"
    finally:
        shutil.rmtree(base, ignore_errors=True)


# ---------------------------------------------------------------------------
# ⑤ 服务入口放行证据：`scheduler/main.py` 的那一行声明**实测**能过
# ---------------------------------------------------------------------------
def test_service_entry_declaration_really_opens_the_gate() -> None:
    """**服务照常启动**的取证：入口那一行声明在"生产形状 + 有 pending"上真能过。

    做法（子进程里跑，避免污染本进程的单例与声明位）：

    * 先实测"**没有**那一行会怎样"——``init_singletons(validate=False)`` 在非测试
      进程 + 生产形状 + 有 pending 上抛 ``RuntimeError``（入口声明确实是必需的，不是装饰）；
    * 再实测"**有**那一行就怎样"——``init_singletons(validate=False,
      allow_production=True)`` 一路走到迁移入口（``init_dirs`` / ``init_meta_db``
      / ``run_migrations`` 换成记录桩）。目标一律是**本地副本**（``META_DB_PATH`` 指向
      它、``production_data_paths`` 指向它 = 生产形状），**绝不碰生产库**；
    * 最后核对 ``scheduler/main.py`` 里确实是按这个形状调用的（AST，不是 grep 文本）。
    """
    _pin()
    import ast as _ast

    main_src = (ROOT / "scheduler" / "main.py").read_text(encoding="utf-8-sig")
    main_tree = _ast.parse(main_src)
    declarations = []
    for node in _ast.walk(main_tree):
        if not isinstance(node, _ast.Call):
            continue
        if getattr(node.func, "id", "") != "init_singletons":
            continue
        if any(
            kw.arg == "allow_production"
            and isinstance(kw.value, _ast.Constant) and kw.value.value is True
            for kw in node.keywords
        ):
            declarations.append(node)
    assert declarations, (
        "服务入口没有显式声明生产迁移（scheduler/main.py 里找不到 "
        "init_singletons(allow_production=True)）")

    base = make_db_dir("bs_entry_")
    stale = base / "meta.db"
    _stale_copy_of_prod(stale)
    before = _fingerprint(stale)
    probe = base / "_bootstrap_guard_entry_probe.py"
    source = (
        "import sys\n"
        "sys.path.insert(0, %(root)r)\n"
        "from pathlib import Path\n"
        "from core.config.settings import config\n"
        "config.paths.platforms_dir = Path(%(platforms)r)\n"
        "config.paths.data_dir = Path(%(data)r)\n"
        "from core import bootstrap\n"
        "target = %(target)r\n"
        "bootstrap.META_DB_PATH = target\n"
        "bootstrap.production_data_paths = lambda: ((target, 'probe: 生产形状'),)\n"
        "bootstrap._initialized = False\n"
        "bootstrap._singletons.clear()\n"
        "bootstrap.init_dirs = lambda: None\n"
        "seen = []\n"
        "bootstrap.init_meta_db = lambda *a, **k: seen.append('init_meta_db')\n"
        "bootstrap.run_migrations = lambda *a, **k: seen.append('run_migrations')\n"
        "try:\n"
        "    bootstrap.init_singletons(validate=False)\n"
        "except RuntimeError as exc:\n"
        "    print('undeclared=RuntimeError')\n"
        "    print('message_ok=', 'allow_production=True' in str(exc))\n"
        "else:\n"
        "    print('undeclared=ALLOWED')\n"
        "bootstrap._initialized = False\n"
        "bootstrap._singletons.clear()\n"
        "# 真 init_dirs 会建 platforms_dir 与每个账号库的父目录（core/bootstrap.py:57-59）。\n"
        "# 这里把它换成桩，就得自己把这步补上——否则账号库父目录不存在，SQLite 报\n"
        "# 'unable to open database file'，本用例测不到它真正要测的那道门。\n"
        "for _ac in config.all_account_configs():\n"
        "    Path(_ac.db_path).parent.mkdir(parents=True, exist_ok=True)\n"
        "Path(config.paths.platforms_dir).mkdir(parents=True, exist_ok=True)\n"
        "bootstrap.init_singletons(validate=False, allow_production=True)\n"
        "print('declared_migrations=', seen.count('run_migrations') > 0)\n"
        "print('declared_flag=', bootstrap.production_migration_declared())\n"
        "print('ENTRY_OK')\n"
    )
    try:
        probe.write_text(
            source % {
                "root": str(ROOT), "platforms": str(base / "platforms"),
                "data": str(base / "data"), "target": str(stale),
            },
            encoding="utf-8",
        )
        env = dict(os.environ)
        env.update(_PINS)
        env.update({
            "META_DB_PATH": str(_ISO_ROOT / "meta.db"),
            "PLATFORMS_DIR": str(_ISO_ROOT / "platforms"),
            "DATA_DIR": str(_ISO_ROOT / "data"),
            "LOG_DIR": _TEST_LOG_DIR,
            "YINYUE_TEST_NO_ENV_FLAGS": "1",
            "PYTHONIOENCODING": "utf-8",
        })
        env.pop("PYTEST_CURRENT_TEST", None)
        proc = subprocess.run(
            [sys.executable, str(probe)],
            cwd=str(ROOT), env=env, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=600,
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr
        out = proc.stdout
        assert "ENTRY_OK" in out, out
        assert "undeclared=RuntimeError" in out, f"没有那一行竟然也能过：\n{out}"
        assert "message_ok= True" in out, out
        assert "declared_migrations= True" in out, f"声明之后没走到迁移入口：\n{out}"
        assert "declared_flag= True" in out, out
    finally:
        probe.unlink(missing_ok=True)
        shutil.rmtree(base, ignore_errors=True)


# ---------------------------------------------------------------------------
# ⑥ 今晚被误伤的那类脚本：`scripts/reimport_speech_seed.py` 在**生产形状 + 已最新**
#    的目标上跑得完，而生产库一个字节都没动
# ---------------------------------------------------------------------------
_REIMPORT_PROBE = """\
import runpy
import sqlite3
import sys
from pathlib import Path

ROOT = Path(%(root)r)
ISO = Path(%(iso)r)
sys.path.insert(0, str(ROOT))

from core.config.settings import config
config.paths.platforms_dir = ISO / 'platforms'
config.paths.data_dir = ISO / 'data'

from core import bootstrap

PROD = Path(%(prod)r)
COPY = ISO / 'meta.db'


def fingerprint(path):
    con = sqlite3.connect('file:' + Path(path).as_posix() + '?mode=ro', uri=True)
    try:
        rows = tuple(int(r[0]) for r in con.execute(
            'select version from _schema_version order by version').fetchall())
        counts = tuple(int(con.execute('select count(*) from ' + t).fetchone()[0])
                       for t in ('personas', 'persona_versions', 'policy_rules'))
    finally:
        con.close()
    return (rows, counts)


# 生产库 → 隔离副本（源库只读打开；脚本的写全部落在副本上）
src = sqlite3.connect('file:' + PROD.as_posix() + '?mode=ro', uri=True)
dst = sqlite3.connect(str(COPY))
src.backup(dst)
dst.close()
src.close()

bootstrap.META_DB_PATH = str(COPY)
# 「生产路径」这一个来源指向副本：脚本启动链里的目标就是"生产库"（判据调用处不变）
bootstrap.production_data_paths = lambda: ((str(COPY), 'probe: 生产库的隔离副本'),)

# 同一晚的**写侧**守卫（db/write_guard.py）读的是**同一个路径来源**：目标既被认成
# "生产库"，拿**可写**句柄就必须声明。本格要考的是**迁移门**——库已最新所以不必声明
# 迁移——所以这里只声明"允许写这个库"（操作者的决定），迁移声明一个字都不给。
try:
    from db.write_guard import declare_writes_allowed
except Exception:  # 写侧守卫不在（尚未落地 / 已改名）时本格照旧只考迁移门
    declare_writes_allowed = None
if declare_writes_allowed is not None:
    declare_writes_allowed(declared_by='reimport probe (isolated copy)')

print('hit=', bootstrap.production_paths_for(COPY))
print('gap=', bootstrap.applied_schema_version(COPY),
      bootstrap.migration_chain_target_version())

before_prod = fingerprint(PROD)
before_copy = fingerprint(COPY)
sys.argv = ['reimport_speech_seed.py', '--no-snapshot']
code = 0
try:
    runpy.run_path(str(ROOT / 'scripts' / 'reimport_speech_seed.py'),
                   run_name='__main__')
except SystemExit as exc:
    code = int(exc.code or 0)
after_copy = fingerprint(COPY)
print('script_exit=', code)
print('migration_declared=', bootstrap.production_migration_declared())
print('copy_counts_before=', before_copy[1])
print('copy_counts_after=', after_copy[1])
print('copy_changed=', after_copy != before_copy)
print('prod_unchanged=', fingerprint(PROD) == before_prod)
print('REIMPORT_PROBE_OK')
"""


def test_reimport_speech_seed_runs_on_up_to_date_production_target() -> None:
    """收窄后的实测格：维护脚本不再被守卫挡住，而且**生产库零写入**。

    ``scripts/reimport_speech_seed.py`` 没有 ``--dry-run``，它的正常工作是写人设/规则，
    所以这里把它的目标摆成"生产形状 + 已最新"的**隔离副本**（``META_DB_PATH`` 与
    ``production_data_paths`` 都指向副本）：脚本的启动链看到的仍然是"生产库"，
    但它写的是副本。生产库全程只有 ``mode=ro`` 的只读读数（前后比对指纹）。
    """
    _pin()
    prod_before = (_schema_version(_PROD_META), _schema_rows(_PROD_META))
    base = make_db_dir("bs_reimport_")
    probe = base / "_bootstrap_guard_reimport_probe.py"
    try:
        probe.write_text(
            _REIMPORT_PROBE % {"root": str(ROOT), "iso": str(base),
                               "prod": str(_PROD_META)},
            encoding="utf-8",
        )
        env = dict(os.environ)
        env.update(_PINS)
        env.update({
            "META_DB_PATH": str(base / "meta.db"),
            "PLATFORMS_DIR": str(base / "platforms"),
            "DATA_DIR": str(base / "data"),
            "LOG_DIR": _TEST_LOG_DIR,
            # 只影响 .env 开关类键的注入（本格要的是判据那一格，不是 flag 面）
            "YINYUE_TEST_NO_ENV_FLAGS": "1",
            "PYTHONIOENCODING": "utf-8",
        })
        env.pop("PYTEST_CURRENT_TEST", None)
        proc = subprocess.run(
            [sys.executable, str(probe)],
            cwd=str(ROOT), env=env, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=900,
        )
        out = proc.stdout + proc.stderr
        assert proc.returncode == 0, out
        assert "REIMPORT_PROBE_OK" in out, out
        assert "拒绝迁移" not in out, f"维护脚本又被守卫挡住了：\n{out}"
        assert "hit= (" in out, out
        # gap= <当前> <链目标>：脚本跑之前副本就已经是最新（没有 pending）
        gap_line = [ln for ln in out.splitlines() if ln.startswith("gap=")][0]
        numbers = [int(tok) for tok in gap_line.split("=", 1)[1].split()]
        assert len(numbers) == 2 and numbers[0] >= numbers[1], gap_line
        assert "script_exit= 0" in out, f"脚本没跑完：\n{out}"
        # 整趟跑完，**迁移声明一直是 False**：门是靠"没有 pending"自己放行的
        assert "migration_declared= False" in out, out
        # 脚本**真的做了它的事**：副本上的 persona_versions 多了一条（人设新版本）
        counts_before = _tuple_line(out, "copy_counts_before=")
        counts_after = _tuple_line(out, "copy_counts_after=")
        assert counts_after[1] > counts_before[1], (counts_before, counts_after)
        assert "copy_changed= True" in out, f"脚本什么都没写（跑的还是原来那条链吗）：\n{out}"
        assert "prod_unchanged= True" in out, f"生产库被动了：\n{out}"
        # 取证行留在正常输出里（报告与人工复核都用它，不必再翻子进程日志）
        for line in out.splitlines():
            if line.split("=", 1)[0] in (
                "hit", "gap", "script_exit", "migration_declared",
                "copy_counts_before", "copy_counts_after", "copy_changed",
                "prod_unchanged",
            ):
                print("      [reimport] " + line)
    finally:
        probe.unlink(missing_ok=True)
        shutil.rmtree(base, ignore_errors=True)
    assert (_schema_version(_PROD_META), _schema_rows(_PROD_META)) == prod_before, \
        "本用例不许改生产库"


# ---------------------------------------------------------------------------
# ⑦ 结构锁：路径判定与版本判定各只有一处来源
# ---------------------------------------------------------------------------
def _bootstrap_tree():
    import ast

    source = (ROOT / "core" / "bootstrap.py").read_text(encoding="utf-8-sig")
    return ast.parse(source), source


def test_production_path_source_is_single() -> None:
    _pin()
    import ast

    tree, source = _bootstrap_tree()

    # (a) 路径字面量：bootstrap 里**没有**第二份拼接（唯一来源是 PathSettings 的字段
    #     默认值）。文档字符串不算——所以先把 docstring 摘掉再看。
    docstrings = {
        id(node.value) for node in ast.walk(tree)
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    }
    literals = [
        node.value for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
        and id(node) not in docstrings
    ]
    for token in ("yinyue_meta.db", "data/platforms", "data\\platforms", "account.db"):
        assert not [lit for lit in literals if token in lit], (
            f"bootstrap 里出现了第二个路径来源：{token}"
        )

    # (b) 取默认值的调用只有一处（production_data_paths 里那一次），且读的是
    #     **字段默认值**而不是会被环境变量覆盖的 PathSettings()
    uses = [
        node.lineno for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name) and node.func.id == "PathSettings"
    ]
    assert uses == [], f"PathSettings() 会读环境变量，不能用来定生产路径：{uses}"
    assert "PathSettings.model_fields" in source, "必须从字段默认值取生产路径"

    # (c) pending 判据的**版本**来源也只有一处：链目标读 db/migrations.py 的
    #     ``@migration`` 注册表，不许在判据里写死版本号
    from db import migrations as _migrations

    target_version = bootstrap.migration_chain_target_version()
    assert target_version == max(m.version for m in _migrations._MIGRATIONS), \
        "链目标版本与 db/migrations.py 的注册表不一致"
    judged = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name in (
            "migration_chain_target_version", "applied_schema_version",
            "_pending_gap", "assert_migrations_allowed",
        )
    ]
    assert len(judged) == 4, [node.name for node in judged]
    hardcoded = sorted({
        node.value for fn in judged for node in ast.walk(fn)
        if isinstance(node, ast.Constant) and isinstance(node.value, int)
    })
    assert target_version not in hardcoded, (
        f"迁移链目标版本被写死在判据里（第二份版本台账）：{target_version} ∈ {hardcoded}"
    )

    # (d) 「当前版本」的读法是**只读**的：库不存在时不许被建出来
    ghost = _ISO_ROOT / "_guard_readonly_probe.db"
    ghost.unlink(missing_ok=True)
    assert bootstrap.applied_schema_version(ghost) is None
    assert not ghost.exists(), "pending 判据把目标库建出来了（判据必须是只读的）"
    assert bootstrap.applied_schema_version("") is None
    assert bootstrap.applied_schema_version(_PROD_META) == _schema_version(_PROD_META)

    # (e) 强制点只有一处，且在 init_singletons 的**第一件事**（碰任何库之前）：
    #     判据不用字面串（那会把"参数换行"也判红），改用 AST 读真实调用。
    calls = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and getattr(node.func, "id", "") == "assert_migrations_allowed"
    ]
    assert len(calls) == 1, [
        (node.lineno, ast.unparse(node)) for node in calls]
    assert "def assert_migrations_allowed" in source
    init = next(
        node for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "init_singletons"
    )
    # 门在 init_singletons 里必须是**第一件事**（前面只许有 global 声明、单例短路与
    # "记下入口声明"这三类无副作用语句——顺序即"碰库之前"的保证）。
    body = [node for node in init.body]
    gate_index = next(
        index for index, node in enumerate(body)
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
        and getattr(node.value.func, "id", "") == "assert_migrations_allowed"
    )
    earlier = body[:gate_index]
    for node in earlier:
        code = ast.unparse(node)
        assert code.startswith(("global ", "if _initialized")) or \
            code.startswith("if allow_production"), (
            f"门之前出现了别的语句（可能已经动了库/目录）：{code}")


def main() -> bool:
    print("=" * 62)
    print("bootstrap migration guard (refuse only when migrations are pending)")
    print("=" * 62)
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
