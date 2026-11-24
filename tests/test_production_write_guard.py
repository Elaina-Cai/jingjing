# -*- coding: utf-8 -*-
"""写库守卫（``db/write_guard.py``）：**默认拒绝，显式声明**——写侧那一半。

对应 2026-10-02 **第三次**生产库事故（`docs/任务清单.md` 那类记录的同一形状）：
迁移那两次（04:57 的 55、18:12 的 56）由 ``core.bootstrap.assert_migrations_allowed``
收窄，而 22:5x 那次**根本不需要迁移**——冒烟脚本调写侧入口时没传 ``engine``，写口内部
回落到 ``core.bootstrap.get_meta_repo()``，于是 1 行测试数据落进**生产**
``claim_ledger``。迁移那道门拦不住"不需要迁移的写"，本文件的四格就是写那道门。

四格（**必测**）
----------------
① **未声明 + 生产路径 → 拒绝**：拿一个"生产形状"的隔离副本当生产路径，构造可写句柄
   被拒（``ProductionWriteRefused``），并断言库里 ``claim_ledger`` 的**行数未变**
   （2 → 2）且文件指纹未变；
② **未声明 + 隔离路径 → 放行**：同一条调用在隔离库上照常建句柄、照常写（现有全仓测试
   即证据，这里再钉一次"不许误伤"）；
③ **已声明 + 生产路径 → 放行**：跑 `scheduler/main.py` 里**真实的那一行入口代码**
   （AST 取出来 exec，不是抄一份），用生产形状的目标证明服务入口这条路走得通，
   并对照"没有那一行就抛错"；
④ **只读访问生产库 → 放行**：``read_only_engine`` / 只读连接在**未声明**的进程里读真
   生产库的 ``claim_ledger`` 行数，成功且**不建文件、不改库**（读也堵死会让全仓测试与
   只读脚本全崩）。

外加两格（同一条判据的**收窄**与**不软化**，见 ⑦）
--------------------------------------------------
2026-10-02 实测的误杀：``PLATFORMS_DIR`` 一改道到隔离根，``core.bootstrap``
``production_data_paths()`` 的**账号库那一段**（按**当前生效**的
``config.paths.platforms_dir`` 现算 ``db_path``）就把**隔离**账号库算成了生产路径 ⇒
``init_singletons() → AccountDBManager.load_from_config()`` 在未声明的进程里抛
``ProductionWriteRefused``（测试与隔离 runner 一起红）。⑦ 格把这条链钉死：改道账号根
下的账号库**不算**生产（但**默认**账号根之下的账号库照旧算生产——收窄不是"账号库一律
放行"）。

测试卫生与边界
--------------
* 断言"关"必须显式表态：本文件依赖的开关在 import 期一律钉成显式值（``off``），不靠
  "env 未设"；出站闸门只剩 ``OUTPUT_GATE``（``ADDRESSEE_GUARD`` 随 R0 删门移除），仍钉死；
* **不对生产库做任何写操作**：生产库只被 ``mode=ro`` 读过；"生产形状"的写目标一律是
  ``data/_write_guard_test/`` 下的**副本**；"生产路径"这份身份**只有一处来源**
  （``core.bootstrap.production_data_paths``），用例只临时替换"喂进去的路径"；
* 本文件**不引入任何新开关**：守卫的判据是路径 + 参数声明，不是 flag。

Run directly::

    .\\.venv\\Scripts\\python.exe tests\\test_production_write_guard.py
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import traceback
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, str(Path(__file__).resolve().parent))

ROOT = Path(__file__).resolve().parent.parent

# --- import 期钉死：本文件依赖的开关一律显式表态（"env 未设"≠"关"）----------
ROOT_ENV = {
    "LOG_DIR": str(Path("data") / "_write_guard_test_logs"),
    "META_DB_PATH": str(ROOT / "data" / "_write_guard_test" / "_iso" / "meta.db"),
    "PLATFORMS_DIR": str(ROOT / "data" / "_write_guard_test" / "_iso" / "platforms"),
    "DATA_DIR": str(ROOT / "data" / "_write_guard_test" / "_iso" / "data"),
}
os.environ.update(ROOT_ENV)
#: 出站闸门只剩 ``OUTPUT_GATE``（``ADDRESSEE_GUARD`` 随 R0 删门移除；tests/test_env_flag_isolation.py §③）。
#: 台账总闸也钉死：本文件不测台账行为，只借它的表证明"行数未变"。
_PINS = {
    "YINYUE_FEATURE_OUTPUT_GATE": "off",
    "YINYUE_FEATURE_CLAIM_LEDGER": "off",
}
os.environ.update(_PINS)
Path(ROOT_ENV["LOG_DIR"]).mkdir(parents=True, exist_ok=True)
_ISO_ROOT = (ROOT / "data" / "_write_guard_test" / "_iso").resolve()
_ISO_ROOT.mkdir(parents=True, exist_ok=True)

from sqlalchemy import create_engine, text as _sql  # noqa: E402

from core import bootstrap  # noqa: E402
from core.config.settings import config  # noqa: E402
from db import write_guard as wg  # noqa: E402
from db.meta_models import ClaimLedgerRow, MetaBase  # noqa: E402
from db.write_guard import ProductionWriteRefused  # noqa: E402
from _tempdirs import make_db_dir  # noqa: E402

#: 真生产库（**只读**访问；本文件绝不给它写权）。
_PROD_META = (ROOT / "data" / "yinyue_meta.db").resolve()
#: 真生产账号库目录（只在结构锁里被提到，不打开）。
_PROD_PLATFORMS = (ROOT / "data" / "platforms").resolve()

#: 子进程探针：走**真** ``init_singletons``（只替身掉网络/语音零部件），目标由参数给。
#: ``%(env)s`` 是 ``json.dumps(dict)`` —— 模板用 ``%`` 格式化，dict 的 repr 不是合法 Python。
#: ``%(entry)s`` = 从 ``scheduler/main.py`` AST 取出来的**入口声明那几行真源码**。
_CHILD = r'''
import json, os, sys
sys.path.insert(0, %(root)r)
os.environ.update(json.loads(%(env)r))
from core import bootstrap
from db.meta_repo import MetaRepositoryManager
from db.account_db_manager import AccountDBManager
from db import write_guard as wg

target = %(target)r
bootstrap.META_DB_PATH = target
bootstrap.production_data_paths = lambda: ((target, "probe: 生产形状"),)
#: 账号库目录也一并改道（probe 的 config 由 env 决定；这里再钉一次，避免碰生产账号库）
paths = bootstrap.config.paths
paths.platforms_dir = __import__("pathlib").Path(json.loads(%(env)r)["PLATFORMS_DIR"])
paths.data_dir = __import__("pathlib").Path(json.loads(%(env)r)["DATA_DIR"])


def _stub(*a, **k):
    return None


bootstrap.init_dirs = _stub
bootstrap.LLMClient = _stub
bootstrap.EmbeddingClient = _stub
bootstrap.VectorStore = _stub
bootstrap.SpeechStore = _stub
bootstrap.build_speech_facade = _stub
bootstrap.build_outbound_gateway = _stub
bootstrap.ToolRegistry = type("ToolRegistry", (), {"create_default": staticmethod(_stub)})


class _Runtime:
    def __init__(self):
        self.default = None

    def load_registered_accounts(self):
        pass


bootstrap.RuntimeRegistry = type(
    "RuntimeRegistry", (), {"build": staticmethod(lambda *a, **k: _Runtime())})
_real = MetaRepositoryManager
bootstrap.MetaRepositoryManager = lambda path: (
    _real(path), print("meta_repo_built=", path))[0]


class _RecordingAccountDBManager(AccountDBManager):
    """真 ``AccountDBManager``（注册口本身不过守卫、不碰盘）——只记一笔"账号库被启动了"。"""

    def load_from_config(self):
        print("account_repos_registered=", len(self.all_repos()))


bootstrap.AccountDBManager = _RecordingAccountDBManager

%(entry)s

try:
    bootstrap._initialized = False
    bootstrap._singletons.clear()
    bootstrap.init_singletons(validate=False)
except Exception as exc:
    print("boot_failed=", type(exc).__name__)
    print("boot_failed_msg=", str(exc)[:400].replace("\n", " "))
    raise SystemExit(3)

#: 启动链把可写句柄留成了**惰性**（构造 ≠ 要写），所以这里补上"真的拿句柄"那一步：
#: 写声明是否承重就看这一下——声明过就通，没声明就 ProductionWriteRefused。
try:
    _meta_repo = bootstrap._singletons["meta_repo"]
    _ = _meta_repo.write_engine().connect().close()
    print("write_handle=", "ok")
except Exception as exc:
    print("write_handle_failed=", type(exc).__name__)
    print("boot_failed=", type(exc).__name__)
    print("boot_failed_msg=", str(exc)[:400].replace("\n", " "))
    raise SystemExit(3)

print("writes_declared=", wg.writes_declared())
print("declaration_reason=", wg.declaration_reason())
print("production_hit=", bool(wg.production_path_hits(target)))
_ro = wg.read_only_engine(target)
with _ro.connect() as _conn:
    print("schema_after=", _conn.exec_driver_sql(
        "select max(version) from _schema_version").scalar())
_ro.dispose()
print("CHILD_EXIT_OK")
'''


# ---------------------------------------------------------------------------
# 只读读数 / 隔离库 / "生产路径"注入（唯一来源仍是 core.bootstrap）
# ---------------------------------------------------------------------------
def _row_count(path) -> int:
    """只读读出 ``claim_ledger`` 的行数（"写没写进去"的直接读数）。"""
    engine = wg.read_only_engine(path)
    try:
        with engine.connect() as conn:
            return int(conn.execute(_sql("select count(*) from claim_ledger")).scalar() or 0)
    finally:
        engine.dispose()


def _fingerprint(path) -> tuple[int, str]:
    """（文件字节数, 文件 mtime_ns）——"一个字节都没动"的旁证（只读 stat）。"""
    stat = Path(path).stat()
    return (int(stat.st_size), str(stat.st_mtime_ns))


def _meta_mtime_ns(path) -> int:
    return int(Path(path).stat().st_mtime_ns)


def _prod_db(prefix: str) -> tuple[Path, Path]:
    """一个**生产形状**的隔离库（含 2 行 ``claim_ledger``）；返回 (路径, 基座)。

    为什么要有这 2 行：事故的形状是"1 行测试数据写进生产 claim_ledger"，所以拒绝的
    证据必须是**行数**（2 → 2），而不只是"抛了异常"。
    """
    base = make_db_dir(prefix)
    path = base / "meta.db"
    engine = create_engine(f"sqlite:///{path}")
    try:
        MetaBase.metadata.create_all(engine)
        with engine.begin() as conn:
            for index in (1, 2):
                conn.execute(
                    _sql(
                        "insert into claim_ledger (platform, account_id, chat_id,"
                        " person_key, field_key, value, content_key, authority,"
                        " source_kind, source_actor_id, source_chat_id,"
                        " source_message_id, source_claim_id, observed_at, created_at)"
                        " values (:p, :a, :c, :pk, :fk, :v, :ck, :au, 'self_report',"
                        " :pk, :c, '', 0, :now, :now)"
                    ),
                    {
                        "p": "probe", "a": "probe", "c": "probe@chatroom",
                        "pk": f"wxid_probe_{index}", "fk": "hometown",
                        "v": f"隔离副本第 {index} 行", "ck": f"key-{index}",
                        "au": "self_report", "now": "2026-10-02 22:50:00",
                    },
                )
    finally:
        engine.dispose()
    return path, base


@contextmanager
def _production_paths_are(*paths: object):
    """把**唯一**那份生产路径来源临时指向用例给的路径（判据调用处一个字没改）。

    与 ``tests/test_bootstrap_migration_guard.py`` 同一手法：本上下文只换"喂进去的
    路径"，不在 ``db/`` 里新增第二份路径判断；退出时还原真实来源。
    """
    saved = bootstrap.production_data_paths
    bootstrap.production_data_paths = lambda: tuple(
        (str(p), "write_guard 用例注入的生产路径") for p in paths
    )
    try:
        yield
    finally:
        bootstrap.production_data_paths = saved


@contextmanager
def _undeclared():
    """回到默认拒绝（用例必须能测"未声明"那一格），退出时恢复原声明状态。"""
    was_declared = wg.writes_declared()
    wg.reset_writes_declaration()
    try:
        yield
    finally:
        wg.reset_writes_declaration()
        if was_declared:  # pragma: no cover - 本文件从不声明，留作防御
            wg.declare_writes_allowed(declared_by="tests/test_production_write_guard")


def _refusal(target, *, what="可写句柄") -> str:
    try:
        wg.assert_writes_allowed(target, what=what)
    except ProductionWriteRefused as exc:
        return str(exc)
    raise AssertionError(f"守卫没有拒绝：target={target}")


@contextmanager
def _isolated_db(name: str):
    """一个"隔离路径"的库位（**非生产路径** ⇒ 守卫一律放行）；退出时连目录一起清掉。"""
    base = make_db_dir("wg_iso_")
    yield base / f"{name}.db"
    shutil.rmtree(base, ignore_errors=True)


# ---------------------------------------------------------------------------
# ① 未声明 + 生产路径 → 拒绝（断言生产库行数未变）
# ---------------------------------------------------------------------------
def test_undeclared_production_path_is_refused_and_untouched() -> None:
    prod_like, base = _prod_db("wg_refuse_")
    account_style = base / "account.db"
    from db.models import Base as _AccountBase

    engine = create_engine(f"sqlite:///{account_style}")
    try:
        _AccountBase.metadata.create_all(engine)  # 与 isolate 无关的新库，先建空表
    finally:
        engine.dispose()

    from db.account_db_manager import AccountDBManager
    from db.meta_repo import MetaRepositoryManager
    from db.repository import RepositoryManager

    try:
        with _undeclared(), _production_paths_are(prod_like, account_style):
            assert wg.writes_allowed(prod_like) is False
            assert wg.production_path_hits(prod_like) == (str(prod_like),), \
                "生产路径身份必须来自 core.bootstrap.production_data_paths 这一处"

            before_rows = _row_count(prod_like)
            before_fp = _fingerprint(prod_like)
            account_fp = _fingerprint(account_style)
            assert before_rows == 2, before_rows

            message = _refusal(prod_like, what="meta 库的可写 engine")
            for token in ("生产库", "没有声明", "隔离", "declare_writes_allowed",
                          "read_only_engine", "run_all_tests.py", "22:5x"):
                assert token in message, f"拒绝消息缺 {token!r}：{message}"

            # 写句柄层（meta 库 / 单库）都在守卫后面：抛错 = 连 engine 都没建出来。
            # **构造不再断言**（构造 ≠ 要写，见 db/write_guard.py 的"读也堵死"那条）：
            # 拒绝发生在"真的拿可写句柄"那一刻——``write_engine()`` / ``.engine``
            # 第一次真访问（惰性 WriteHandle 在 factory 之前断言）。
            for factory, path in (
                (MetaRepositoryManager, prod_like),
                (RepositoryManager, account_style),
            ):
                built = factory(str(path))  # 构造：不许抛
                try:
                    built.write_engine()
                except ProductionWriteRefused:
                    pass
                else:
                    raise AssertionError(f"{factory.__name__} 在未声明的生产路径上放行了")

            # 账号库注册口：注册本身不过守卫（不碰盘），**拿可写句柄**时才拒
            manager = AccountDBManager()
            registered = manager.register("probe", "probe", str(account_style))
            try:
                registered.write_engine()  # 拿可写句柄 = 要写
            except ProductionWriteRefused:
                pass
            else:
                raise AssertionError("AccountDBManager 的可写句柄在未声明的生产路径上放行了")
            assert manager.all_repos() == {("probe", "probe"): registered}, \
                "注册口只该记名、不该在拒绝路径上留下半个真仓库"

            # **生产库行数未变**（事故形状：1 行测试数据写进去）
            assert _row_count(prod_like) == before_rows, \
                f"生产形状的库被写了：{_row_count(prod_like)} != {before_rows}"
            assert _fingerprint(prod_like) == before_fp, "库文件被动过（size/mtime 变了）"
            assert _fingerprint(account_style) == account_fp, "账号库被动了"
            wal = Path(str(prod_like) + "-wal")
            assert not wal.exists() or wal.stat().st_size == 0, "拒绝路径写了 WAL"
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_real_production_path_is_refused_without_declaration() -> None:
    """真生产库路径（``data/yinyue_meta.db``）本身就是判据命中的那一格。

    只调判据（纯路径比较 + 进程内布尔），**不碰库**：连读都不需要，所以这里对生产库
    唯一的要求就是"路径字符串对得上"。真库内容另在 ④ 里只读读出。
    """
    with _undeclared():
        hits = wg.production_path_hits(_PROD_META)
        assert hits, f"真生产库没被判成生产路径：{_PROD_META}"
        assert wg.writes_allowed(_PROD_META) is False
        message = _refusal(_PROD_META)
        assert str(_PROD_META) in message and "生产库" in message, message


# ---------------------------------------------------------------------------
# ②b 构造 ≠ 要写（本用例就是"守卫位置放错"那个 bug 的回归格）
# ---------------------------------------------------------------------------
def test_construction_does_not_assert_and_reads_still_work() -> None:
    """**构造仓库不等于要写**：未声明 + 生产路径上，构造必须成功，只读查询必须能跑。

    这正是修掉的 bug 的形状：守卫曾挂在 ``MetaRepositoryManager.__init__`` 上，于是
    "只想读"的调用方连 new 都 new 不出来（``db/write_guard.py`` 的"读也堵死 = 全仓测试
    与只读脚本全崩"）。本格把"构造不断言 + 构造不碰盘 + 只读查询照常 + 真写才拒"
    四件事一次钉死——**用一份生产形状的库副本**（不碰真生产库）。
    """
    from db.meta_repo import MetaRepositoryManager

    prod_like, base = _prod_db("wg_construct_")
    before_fp = _fingerprint(prod_like)
    try:
        with _undeclared(), _production_paths_are(prod_like):
            assert wg.production_path_hits(prod_like) != (), "前提：目标算生产路径"

            manager = MetaRepositoryManager(str(prod_like))  # 构造：不许抛
            assert _fingerprint(prod_like) == before_fp, "构造就把库动了（构造 ≠ 要写）"

            # 只读查询照常：走只读句柄，守卫一个字都不拦
            rows = _row_count(prod_like)
            assert rows == 2, rows
            with wg.read_only_connection(prod_like) as conn:
                assert int(conn.execute(
                    _sql("select count(*) from claim_ledger")).scalar() or 0) == 2
            assert _fingerprint(prod_like) == before_fp, "只读访问改了库文件"

            # 而"真拿可写句柄"仍然被拒（读通 ≠ 写通）
            try:
                manager.write_engine()
            except ProductionWriteRefused:
                pass
            else:
                raise AssertionError("未声明的生产路径上拿到了可写句柄")
            assert _fingerprint(prod_like) == before_fp, "拒绝路径动了库"
    finally:
        shutil.rmtree(base, ignore_errors=True)


# ---------------------------------------------------------------------------
# ② 未声明 + 隔离路径 → 放行（不许误伤测试）
# ---------------------------------------------------------------------------
def test_undeclared_isolated_path_is_allowed() -> None:
    with _isolated_db("isolated_allowed") as path:
        with _undeclared(), _production_paths_are(_PROD_META, _PROD_PLATFORMS):
            assert wg.production_path_hits(path) == (), "隔离路径不该命中生产路径"
            assert wg.writes_allowed(path) is True

            # 真建可写句柄（隔离路径一律放行）并真写一行 —— 全仓测试即这一格的证据
            from db.meta_repo import MetaRepositoryManager

            manager = MetaRepositoryManager(str(path))
            with manager.engine.begin() as conn:
                conn.execute(_sql(
                    "insert into claim_ledger (chat_id, person_key, field_key, value,"
                    " content_key, authority, observed_at, created_at)"
                    " values ('iso@chatroom', 'wxid_iso', 'hometown', '隔离库',"
                    " 'iso-key', 'self_report', '2026-10-02 22:51:00',"
                    " '2026-10-02 22:51:00')"
                ))
            assert _row_count(path) == 1, "隔离路径应当真能写"
            manager.engine.dispose()


# ---------------------------------------------------------------------------
# ③ 已声明 + 生产路径 → 放行（服务入口那**一行真代码**，替身/子进程证明）
# ---------------------------------------------------------------------------
def _entry_declarations():
    """从 ``scheduler/main.py`` 的 ``main()`` 取出"入口声明"那几行（AST，**不是抄一份文本**）。

    抄一份文本会漂移：入口改了、用例还在跑旧的字符串。这里用 ``ast.walk`` 读**真**调用，
    ``ast.unparse`` 回codetext，于是子进程跑的就是入口那一行。
    """
    import ast

    source = (ROOT / "scheduler" / "main.py").read_text(encoding="utf-8-sig")
    tree = ast.parse(source)
    entry = next(
        (node for node in tree.body
         if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "main"),
        None,
    )
    assert entry is not None, "scheduler/main.py 里没有 main()"
    found = []
    for node in ast.walk(entry):
        if not isinstance(node, ast.Call):
            continue
        name = getattr(node.func, "id", "")
        if name == "declare_writes_allowed":
            found.append(("writes", node.lineno, ast.unparse(node)))
        elif name == "init_singletons" and any(
            kw.arg == "allow_production"
            and isinstance(kw.value, ast.Constant) and kw.value.value is True
            for kw in node.keywords
        ):
            found.append(("migrations", node.lineno, ast.unparse(node)))
    found.sort(key=lambda item: item[1])
    assert [kind for kind, _, _ in found] == ["writes", "migrations"], (
        "scheduler/main.py 的入口声明形状变了（必须先是写库声明，再是迁移声明）："
        f"{[(k, l) for k, l, _ in found]}"
    )
    return [code for _, _, code in found]


def _entry_imports() -> list[str]:
    """入口声明那几行依赖的 import（也取自 ``scheduler/main.py``，不手写）。"""
    import ast

    source = (ROOT / "scheduler" / "main.py").read_text(encoding="utf-8-sig")
    found = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom) and node.module in (
            "db.write_guard", "core.bootstrap",
        ):
            found.append(ast.unparse(node))
    assert any("declare_writes_allowed" in line for line in found), (
        "scheduler/main.py 没有 import declare_writes_allowed（入口那一行跑不起来）")
    assert any("init_singletons" in line for line in found), (
        "scheduler/main.py 没有 import init_singletons")
    return found


def _spawn_probe(
    name: str, target: Path, base: Path, *, entry: bool,
) -> subprocess.CompletedProcess:
    """跑一个子进程：生产形状的目标 + （可选）**入口那几行真源码**。"""
    probe = base / f"_write_guard_probe_{name}.py"
    code = "\n".join(_entry_imports() + _entry_declarations()) if entry else ""
    probe.write_text(
        _CHILD % {
            "root": str(ROOT), "env": json.dumps(ROOT_ENV), "target": str(target),
            "entry": code,
        },
        encoding="utf-8",
    )
    env = dict(os.environ)
    env.update(_PINS)
    env.update(ROOT_ENV)
    env.setdefault("YINYUE_TEST_NO_ENV_FLAGS", "1")
    env["PYTHONIOENCODING"] = "utf-8"
    env.pop("PYTEST_CURRENT_TEST", None)
    try:
        return subprocess.run(
            [sys.executable, str(probe)], cwd=str(ROOT), env=env,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=600,
        )
    finally:
        probe.unlink(missing_ok=True)


def _copy_of_prod(dest: Path) -> None:
    """生产 meta 库的一致性快照（源库**只读**打开；只复制，不改一个字）。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(f"file:{_PROD_META.as_posix()}?mode=ro", uri=True)
    try:
        target = sqlite3.connect(str(dest))
        try:
            source.backup(target)
        finally:
            target.close()
    finally:
        source.close()


def _stale_copy_of_prod(dest: Path) -> None:
    """上面那份快照 + **推回 pending**（"有 pending 迁移"是服务启动的真实形状之一，
    也正是 18:12 那次事故的形状）——删掉高版本行即可：迁移链的 pending 判定按
    "缺列/缺表/缺版本行"走（与 ``tests/test_bootstrap_migration_guard.py`` 同一手法）。
    """
    _copy_of_prod(dest)
    con = sqlite3.connect(str(dest))
    try:
        con.execute("delete from _schema_version where version >= 50")
        con.commit()
    finally:
        con.close()


def _schema_version(path) -> int | None:
    engine = wg.read_only_engine(path)
    try:
        with engine.connect() as conn:
            value = conn.execute(_sql("select max(version) from _schema_version")).scalar()
        return int(value) if value is not None else None
    finally:
        engine.dispose()


def test_service_entry_declaration_opens_the_write_gate() -> None:
    """入口那两行声明在子进程里**真跑**（目标 = 生产形状），且证明**写**那一行是承重的。

    四件事一起证：

    * **服务不受影响**：入口那两行（AST 取的真源码）跑起来之后，
      ``init_singletons(allow_production=True)`` 在"看着像生产"且有 **pending 迁移**的
      目标上走得通——meta 库的**可写句柄真建出来了**（``meta_repo_built=``），迁移链也
      真从 50 以下爬到 56；
    * **写声明是承重的**（否则它只是装饰）：库**已是最新**时，迁移那道门放行
      （``applied_schema_version >= 链目标`` ⇒ ``assert_migrations_allowed`` 不拦），
      此时若没有 ``declare_writes_allowed``，服务会在 ``MetaRepositoryManager`` 上被
      写守卫拒掉（``boot_failed= ProductionWriteRefused``）——这一格就是"删掉入口那一行
      服务会起不来"的证据；
    * ``declaration_reason()`` 给得出依据（哪一行声明的）；
    * 这三格用的都是 AST 出来的真源码，不是抄一份文本（抄的那份会漂移）。
    """
    stale, base = _prod_db("wg_entry_")
    fresh, fresh_base = _prod_db("wg_fresh_")
    try:
        _stale_copy_of_prod(stale)
        before_pending = _schema_version(stale)
        assert before_pending is not None and before_pending < 56, \
            f"快照没被推回 pending（schema={before_pending}）：本用例就测不到'有 pending'"
        #: "已是最新"那一格：生产 meta 库的**只读快照**（它就是当前 schema 版本），
        #: 没有任何 pending 迁移可做 ⇒ 迁移那道门会放行（`applied >= 链目标`）。
        _copy_of_prod(fresh)
        fresh_version = _schema_version(fresh)
        assert fresh_version is not None, "快照读不到 _schema_version"

        proc = _spawn_probe("entry", stale, base, entry=True)
        out = proc.stdout + proc.stderr
        assert proc.returncode == 0, out
        for token in ("meta_repo_built=", "writes_declared= True",
                      "declaration_reason= declare_writes_allowed(declared_by=scheduler.main)",
                      "production_hit= True", "write_handle= ok",
                      "account_repos_registered=", "CHILD_EXIT_OK"):
            assert token in proc.stdout, f"子进程输出缺 {token!r}：\n{out}"
        assert "boot_failed=" not in proc.stdout, out
        after = _schema_version(stale)
        assert after is not None and after > before_pending, (
            f"启动链没把目标库迁上去（{before_pending} → {after}）："
            "'服务照常启动'的证据不成立")

        # ① 承重证据：库已最新（迁移门放行）+ 有写声明 ⇒ 起得来
        ok = _spawn_probe("fresh_declared", fresh, fresh_base, entry=True)
        ok_out = ok.stdout + ok.stderr
        assert ok.returncode == 0 and "meta_repo_built=" in ok.stdout, ok_out
        assert "boot_failed=" not in ok.stdout, ok_out
        assert _schema_version(fresh) == fresh_version, "已最新的库不该被再迁移一次"

        # ② 承重证据的反面：同样"已最新"，但**只声明迁移、不声明写** ⇒ 被写守卫拒。
        #    拒绝点在**启动链拿可写句柄**那一刻（core/bootstrap.py 的 SpeechStore(...)），
        #    而 meta_repo 的**构造已经过去了**——正是"构造 ≠ 要写"的直接读数。
        lone = _spawn_probe("fresh_writes_undeclared", fresh, fresh_base, entry=False)
        lone_out = lone.stdout + lone.stderr
        assert lone.returncode == 3, lone_out
        assert "boot_failed= ProductionWriteRefused" in lone.stdout, lone_out
        assert "meta_repo_built=" in lone.stdout, (
            "拒绝必须发生在拿句柄那一刻、**不是**构造时（构造已经成功过一回）："
            f"\n{lone_out}")
        assert "write_handle= ok" not in lone.stdout, lone_out
    finally:
        shutil.rmtree(base, ignore_errors=True)
        shutil.rmtree(fresh_base, ignore_errors=True)


# ---------------------------------------------------------------------------
# ④ 只读访问生产库 → 放行（不许把读也堵死）
# ---------------------------------------------------------------------------
def test_read_only_access_to_production_is_allowed() -> None:
    """未声明进程里读**真生产库**：``read_only_engine`` 成功、行数读得出、库不动。"""
    with _undeclared():
        assert wg.writes_allowed(_PROD_META) is False, "前提：未声明 + 真生产路径"
        before_mtime = _meta_mtime_ns(_PROD_META)
        rows = _row_count(_PROD_META)  # 走 read_only_engine（本守卫一个字都不拦）
        assert rows >= 0, rows

        # 只读连接真读一行数据（证明"只读路径确实通"，不是拿空库糊弄）
        with wg.read_only_connection(_PROD_META) as conn:
            head = conn.execute(_sql("select id, chat_id from claim_ledger order by id limit 1")
                                ).fetchone()
            assert head is None or head[1] is not None, head
            versions = conn.execute(
                _sql("select max(version) from _schema_version")).scalar()
        assert versions is not None and int(versions) >= 1, versions

        assert _meta_mtime_ns(_PROD_META) == before_mtime, \
            "只读访问改了生产库文件（mtime 变了）——只读路径必须真的只读"
        assert wg.writes_allowed(_PROD_META) is False, "读不该顺手把写也放行"

        # 只读口对不存在的库**不会**把它建出来（mode=ro）
        ghost = _ISO_ROOT / "_readonly_ghost.db"
        ghost.unlink(missing_ok=True)
        try:
            _row_count(ghost)
        except Exception:
            pass
        assert not ghost.exists(), "只读口把不存在的库建出来了"


# ---------------------------------------------------------------------------
# ⑤ 结构锁：判据只有一处来源 / 声明只有一处 / 读侧没有经过守卫
# ---------------------------------------------------------------------------
def _project_py_files() -> list[Path]:
    out = []
    for package in ("db", "core", "agents", "web", "scheduler", "llm", "notify", "scripts"):
        base = ROOT / package
        if not base.is_dir():
            continue
        for path in base.rglob("*.py"):
            if "__pycache__" in path.parts or "_scratch" in path.parts:
                continue
            out.append(path)
    return out


def _call_lines(path: Path, name: str) -> list[int]:
    """``name(`` 出现在哪些行（grep 级，够用：注释里的写法会一起算，宁可严格）。"""
    return [
        lineno for lineno, line in enumerate(
            path.read_text(encoding="utf-8-sig").splitlines(), 1)
        if f"{name}(" in line
    ]


def _write_handle_calls(path: Path) -> list[tuple[int, str]]:
    """``(lineno, ast.unparse(调用))`` of every ``WriteHandle(...)`` in *path*.

    守卫的位置锁在**写句柄入口**上（``WriteHandle``），不再锁在 ``__init__``：
    "构造时断言"正是被修掉的那个 bug 的形状。
    """
    import ast

    tree = ast.parse(path.read_text(encoding="utf-8-sig"))
    root = ast.Module(body=tree.body, type_ignores=[])
    found = []
    for node in _walk_out_of_lambdas(root):
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "WriteHandle":
            found.append((node.lineno, ast.unparse(node)))
    return found


def _walk_out_of_lambdas(node: Any):
    """遍历 ``node``，但**不进入 lambda 体**——lambda 体是"以后才跑"的那部分。

    为什么需要它：``factory=lambda: init_meta_db(db_path)`` 里的 ``init_meta_db`` 是
    **推迟到写时才兑现**的那一下，它出现在 ``__init__`` 的**文本**里，但在**执行**上
    不属于构造。"构造里到底做了什么"必须按执行语义读，否则这个检查会把"把写推迟"
    误判成"构造时写"——正是这个 bug 的两个方向。
    """
    import ast

    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        for child in ast.iter_child_nodes(current):
            if isinstance(child, ast.Lambda):
                yield child  # lambda 节点本身算（它的 body 不算）
                continue
            stack.append(child)


def _init_reads_the_handle_only(path: Path, class_name: str) -> tuple[bool, list[str], str]:
    """``class_name.__init__`` 到底有没有**真建可写句柄**。

    返回 ``(干不干净, 真写调用, 源码)``：干净 = 只接上一个惰性的 ``WriteHandle``
    （**构造 ≠ 要写**），既没有 ``init_*_db(...)``（建库/建表）也没有
    ``run_migrations(...)``——那两样才是写。判据按**执行语义**走（不进 lambda 体）。
    """
    import ast

    text = path.read_text(encoding="utf-8-sig")
    cls = next(
        node for node in ast.walk(ast.parse(text))
        if isinstance(node, ast.ClassDef) and node.name == class_name
    )
    init = next(
        node for node in cls.body
        if isinstance(node, ast.FunctionDef) and node.name == "__init__"
    )
    source = ast.get_source_segment(text, init) or ""
    writes = [
        ast.unparse(node) for node in _walk_out_of_lambdas(init)
        if isinstance(node, ast.Call)
        and (
            getattr(node.func, "id", "") in ("init_db", "init_meta_db", "init_account_db")
            or getattr(node.func, "attr", "") in ("run_migrations", "run_meta_migrations")
        )
    ]
    handles = [
        node.lineno for node in _walk_out_of_lambdas(init)
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "WriteHandle"
    ]
    return (not writes and len(handles) == 1, writes, source)


def test_guard_sources_are_single_and_reads_untouched() -> None:
    import ast

    guard = ROOT / "db" / "write_guard.py"
    source = guard.read_text(encoding="utf-8-sig")
    tree = ast.parse(source)

    # (a) 生产路径判据只有一处来源：import core.bootstrap，不在 db/ 里写第二份。
    #     先摘掉注释/文档字符串再看字面量（docstring 里提路径是解释，不是第二份来源）。
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
    assert "from core.bootstrap import production_paths_for" in source, \
        "守卫必须 import core.bootstrap 的生产路径判据"
    for token in ("yinyue_meta.db", "data/platforms", "data\\platforms", "account.db"):
        assert not [lit for lit in literals if token in lit], \
            f"db/write_guard.py 里出现了第二份路径来源：{token}"
    calls = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and getattr(node.func, "id", "") == "production_data_paths"
    ]
    assert calls == [], \
        "守卫不许自己拼生产路径（唯一来源是 core.bootstrap.production_data_paths）"

    # (b) 声明点只有**入口**：服务启动 + 管理脚本入口各一处，其余任何文件声明都算漂移。
    #     2026-10-11（3eda591）：scripts/reimport_speech_seed.py 是"改生产人设"的管理
    #     入口，job 就是写生产库；db.write_guard 要求写生产库的进程显式声明，脚本内
    #     157-161 行有同样理由（此前实测被 ProductionWriteRefused 挡死）。
    # 2026-10-21：scripts/job_drive.py 是操作员手动跑的真机驱动（--allow-production
    #     才放行，脚本 84-94 行写明理由，与 reimport_speech_seed.py 同一类）。
    allowed = {
        ROOT / "scheduler" / "main.py",
        ROOT / "scripts" / "reimport_speech_seed.py",
        ROOT / "scripts" / "job_drive.py",
        guard,
    }
    declarers = [
        str(path.relative_to(ROOT)) for path in _project_py_files()
        if path not in allowed and _call_lines(path, "declare_writes_allowed")
    ]
    assert declarers == [], f"除已登记的入口外还有人声明可写生产库：{declarers}"
    assert _call_lines(ROOT / "scheduler" / "main.py", "declare_writes_allowed"), \
        "服务入口没有声明可写生产库"
    assert _call_lines(ROOT / "scripts" / "reimport_speech_seed.py",
                       "declare_writes_allowed"), \
        "管理脚本入口的写声明没了（3eda591 的理由仍在：删了它这条维护路会被守卫挡死）"

    # (c) 守卫接在**写句柄层**：三个 manager 各一处 WriteHandle，写句柄的 factory 就是
    #     建可写 engine 那一处，守卫先于 factory；**__init__ 里一个字都不许写**
    #     （构造 ≠ 要写——那正是被修掉的 bug 的形状）。
    for relative, class_name, factory_call in (
        ("db/meta_repo.py", "MetaRepositoryManager", "init_meta_db("),
        ("db/repository.py", "RepositoryManager", "init_db("),
        ("db/account_db_manager.py", "_LazyAccountRepo", "AccountRepositoryManager("),
    ):
        path = ROOT / relative
        handles = _write_handle_calls(path)
        assert len(handles) == 1, (
            f"{relative} 的写句柄入口必须只有一处（{class_name}）：{handles}")
        _, handle_src = handles[0]
        assert factory_call in handle_src, (
            f"{relative}: 写句柄的 factory 必须是建可写 engine 那一处：{handle_src}")
        clean, writes, init_src = _init_reads_the_handle_only(path, class_name)
        assert clean, (
            f"{relative}: {class_name}.__init__ 里还在**真建**可写句柄"
            f"（构造时断言/构造时建库）：{writes}\n{init_src}")

    # (c2) 守卫**在写时**、不在构造时：`handle()` 先断言再建 engine，
    #      所以"拒绝"发生时目标库一个字节都不会变（用例①的指纹断言是行为侧证据）。
    guard_source = (ROOT / "db" / "write_guard.py").read_text(encoding="utf-8-sig")
    assert "class WriteHandle" in guard_source, "惰性写句柄必须存在于守卫模块里"
    for relative in ("db/meta_repo.py", "db/repository.py", "db/account_db_manager.py"):
        text = (ROOT / relative).read_text(encoding="utf-8-sig")
        assert "def write_engine(" in text, \
            f"{relative} 必须显式给出'拿可写句柄'那一处（write_engine）"
    import ast as _ast

    guard_tree = _ast.parse(guard_source)
    handle_fn = next(
        node for node in _ast.walk(guard_tree)
        if isinstance(node, _ast.FunctionDef) and node.name == "handle"
    )
    assert_line = next(
        node.lineno for node in _ast.walk(handle_fn)
        if isinstance(node, _ast.Call)
        and getattr(node.func, "attr", "") == "assert_writable"
    )
    materialize_line = next(
        node.lineno for node in _ast.walk(handle_fn)
        if isinstance(node, _ast.Call) and getattr(node.func, "attr", "") == "materialize"
    )
    assert assert_line < materialize_line, (
        "守卫必须**先于**建 engine：拒绝时目标库一个字节都不许变"
        f"（assert@{assert_line} vs materialize@{materialize_line}）")

    # (c3) 句柄**不冒充 Engine**：`Session(bind)` 用 isinstance(bind, Engine) 分流，
    #      句柄转发属性会静默走另一条连接路径（SQLite 上表现为 "database is locked"）。
    #      仓库层必须有一处显式解引用，而不是把句柄直接喂给 SQLAlchemy。
    handle_class = next(
        node for node in _ast.walk(guard_tree)
        if isinstance(node, _ast.ClassDef) and node.name == "WriteHandle"
    )
    assert "__getattr__" not in {
        node.name for node in handle_class.body if isinstance(node, _ast.FunctionDef)
    }, "WriteHandle 不许靠 __getattr__ 冒充 Engine（Session 会认错 bind）"
    repo_base = (ROOT / "db" / "repo_base.py").read_text(encoding="utf-8-sig")
    assert "def _resolved_engine(" in repo_base and "def write_engine(" in repo_base, \
        "仓库层必须有唯一一处把句柄解引用成真 engine（读 materialize / 写先过守卫）"

    # (d) 读侧没有经过守卫：只读口显式存在，且它**不**在守卫的主路径上
    assert "def read_only_engine(" in source and "def read_only_connection(" in source
    assert "assert_writes_allowed(" not in source.split("def read_only_engine(", 1)[1], \
        "只读口里出现了写守卫（读会被连坐堵死）"
    assert "ProductionWriteRefused" in source

    # (e) 服务入口声明**先于**迁移声明（否则 init_singletons 会先建 meta 句柄）
    entry = _entry_declarations()
    assert entry[0].startswith("declare_writes_allowed("), entry


# ---------------------------------------------------------------------------
# ⑥ 账号库那条路也要拦（事故是 meta 库，账号库是同一形状的第二处）
# ---------------------------------------------------------------------------
def test_account_db_manager_refuses_production_account_path() -> None:
    from db.account_db_manager import AccountDBManager

    _, base = _prod_db("wg_account_")
    account_style = base / "account.db"
    from db.models import Base as _AccountBase

    engine = create_engine(f"sqlite:///{account_style}")
    try:
        _AccountBase.metadata.create_all(engine)
    finally:
        engine.dispose()
    before = _fingerprint(account_style)

    try:
        with _undeclared(), _production_paths_are(account_style):
            manager = AccountDBManager()
            registered = manager.register("probe", "probe", str(account_style))
            try:
                registered.write_engine()  # 拿可写句柄那一步才过守卫（注册不碰盘）
            except ProductionWriteRefused as exc:
                assert "生产库" in str(exc) and str(account_style) in str(exc), exc
            else:
                raise AssertionError("账号库的可写句柄在未声明的生产路径上放行了")
            assert manager.all_repos() == {("probe", "probe"): registered}, \
                "注册口只该记名；拒绝路径上不许留下真仓库"
            assert _fingerprint(account_style) == before, "拒绝路径动了账号库"
    finally:
        shutil.rmtree(base, ignore_errors=True)


def main() -> bool:
    print("=" * 64)
    print("production write guard (db/write_guard.py) — default deny, explicit declare")
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
