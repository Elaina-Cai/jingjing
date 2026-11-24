from __future__ import annotations

import sqlite3
import structlog
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping

from core.config.settings import PathSettings, config
from core.env import load_yinyue_env_defaults, running_under_tests
from db.meta_models import init_meta_db
from db.meta_repo import MetaRepositoryManager
from db.account_db_manager import AccountDBManager
from db.unified_query import UnifiedQuery
from db.migrations import run_migrations
from llm.client import LLMClient
# [去向量] 移除: from llm.embedding import EmbeddingClient
# [去向量] 移除: from db.vector import VectorStore
from agents.tools import ToolRegistry
from core.runtime import RuntimeRegistry, current_account_context
from core.speech.store import SpeechStore
from core.speech.bootstrap import SpeechBootstrap
from core.speech.facade import get_speech_facade as build_speech_facade
from core.speech.outbound import get_outbound_gateway as build_outbound_gateway
from safelog import warn

if TYPE_CHECKING:  # pragma: no cover - typing-only import
    from db.repository import RepositoryManager

log = structlog.get_logger()

#: 仓库根（本文件在 ``core/`` 下）：``PathSettings`` 的默认值是相对路径，
#: 相对谁解析成绝对路径需要一个锚点——这里用仓库根，**不是**当时的 CWD。
_REPO_ROOT = Path(__file__).resolve().parent.parent

META_DB_PATH = str(config.paths.meta_db_path)
LEGACY_DB_PATH = str(config.paths.data_dir / "yinyue.db")

_singletons: dict = {}
_initialized = False


def get_meta_db_path() -> str:
    return META_DB_PATH


def get_legacy_db_path() -> str:
    """Legacy single-DB path, or "" once the file has been retired."""
    path = Path(LEGACY_DB_PATH)
    return str(path) if path.exists() else ""


def init_dirs():
    load_yinyue_env_defaults()
    config.paths.data_dir.mkdir(parents=True, exist_ok=True)
    config.paths.decrypted_dir.mkdir(parents=True, exist_ok=True)
    config.paths.output_dir.mkdir(parents=True, exist_ok=True)
    config.paths.log_dir.mkdir(parents=True, exist_ok=True)
    config.paths.platforms_dir.mkdir(parents=True, exist_ok=True)
    for ac in config.all_account_configs():
        Path(ac.db_path).parent.mkdir(parents=True, exist_ok=True)
        if ac.emoji_dir:
            Path(ac.emoji_dir).mkdir(parents=True, exist_ok=True)
        if ac.media_dir:
            Path(ac.media_dir).mkdir(parents=True, exist_ok=True)


def _canonical(path: Any) -> str:
    """路径的规范化比较形式（绝对 + 大小写归一，Windows 盘符与 ``..`` 都不误伤）。

    相对路径按**仓库根**解析（本文件在 ``core/`` 下，所以根 = 它的上一级）：
    ``PathSettings`` 的默认值就是相对路径（``./data/yinyue_meta.db``），而"生产库"
    说的是**仓库里那个文件**，不是"当前工作目录下恰好同名的文件"。
    """
    raw = Path(str(path)).expanduser()
    if not raw.is_absolute():
        raw = _REPO_ROOT / raw
    try:
        return str(raw.resolve()).casefold()
    except (OSError, ValueError):  # pragma: no cover - 极端路径
        warn("core/bootstrap.py:78 (OSError, ValueError)", err=repr(__import__("sys").exc_info()[1]))
        return str(raw).casefold()


def production_data_paths() -> tuple[tuple[str, str], ...]:
    """**生产库路径**的唯一来源（路径 → 一句人话说明）。

    两个入口都来自路径解析的 owner ``core/config/settings.py``，**本模块不拼路径**：

    * **类默认值**（``PathSettings.model_fields[...].default``）—— 发版时的生产默认
      （``./data/yinyue_meta.db`` / ``./data/platforms``）。刻意读**字段默认值**而不是
      ``PathSettings()``：后者会被环境变量覆盖（测试进程里 ``META_DB_PATH`` 就指向
      隔离根），拿它当"生产"会让守卫在测试进程里失效——那正是本函数要防的那一格；
    * **当前生效的账号库解析**（``config.all_account_configs()``）—— 操作者显式配置过
      的账号库路径（``PLATFORMS_CONFIGS_JSON`` 里的 ``db_path`` 等）。

    为什么**不**把"当前生效的 ``config.paths.meta_db_path``"也算成生产：测试隔离根
    正是**通过它**指过去的（``META_DB_PATH=<隔离根>/meta.db``），把它算成生产会把
    隔离路径一起误杀——那正是本洞的另一条硬要求（不许误伤）。runner 把
    ``META_DB_PATH`` 改道之后，本判据的第一项（类默认值）仍然指着仓库里那个真库，
    所以守卫在隔离 runner 下**照旧有效**。

    调用方（:func:`assert_migrations_allowed`）只拿它当**判据的一部分**：命中这里
    列出的路径**且**目标库确实还有 pending 迁移（:func:`applied_schema_version` <
    :func:`migration_chain_target_version`）**且**本进程没有声明时才拒绝。库已经是
    最新时**不拦**（没有迁移可做，拦下来只会堵死合法的维护脚本）。
    """
    fields = PathSettings.model_fields
    targets: list[tuple[str, str]] = [
        (str(_default_of(fields, "meta_db_path")), "meta 库（PathSettings.meta_db_path 的默认值）"),
        (str(_default_of(fields, "platforms_dir")), "账号库目录（PathSettings.platforms_dir 的默认值）"),
    ]
    # 账号库这一段**必须与 meta 那一条同一个口径**：`config.all_account_configs()` 是按
    # **当前生效**的 `config.paths.platforms_dir` 现算的，所以只要有人把 `PLATFORMS_DIR`
    # 改道到隔离根（测试、探针、`scripts/run_all_tests.py` 的快照根），**隔离账号库就会被
    # 算成"生产路径"** → 写侧守卫误杀 → 全仓隔离测试与只读脚本一起崩（2026-10-02 实测：
    # 6 个文件红）。因此只在"解析后**落在默认账号根之下**"时才算生产；派生自改道环境的
    # 路径一律**不算**——这正是本函数对 `meta_db_path` 已经写死过的那条理由。
    default_platforms = Path(_canonical(_default_of(fields, "platforms_dir")))
    for ac in config.all_account_configs():
        db_path = str(ac.db_path)
        try:
            under_default_root = bool(str(_default_of(fields, "platforms_dir")).strip()) and (
                Path(_canonical(db_path)).is_relative_to(default_platforms)
            )
        except (OSError, ValueError):  # pragma: no cover - 极端路径
            warn("core/bootstrap.py:123 (OSError, ValueError)", err=repr(__import__("sys").exc_info()[1]))
            under_default_root = False
        if not under_default_root:
            continue
        targets.append(
            (db_path, f"账号库 {ac.platform}/{ac.account_id}（落在默认账号根下）")
        )
    return tuple(targets)


def _default_of(fields: Mapping[str, Any], name: str) -> Any:
    """``PathSettings`` 某个字段的**类默认值**（拿不到就当空，绝不用环境值兜底）。"""
    field = fields.get(name)
    return getattr(field, "default", "") if field is not None else ""


def production_paths_for(target: Any) -> tuple[str, ...]:
    """``target`` 如果落在**生产默认路径**上，返回命中的那些路径（否则空元组）。

    只报**确实命中**的：生产路径的**父目录**不算命中（隔离根就建在 ``data/`` 下，
    把它算成生产会把隔离路径一起误杀）。
    """
    if not str(target or "").strip():
        return ()
    canonical = _canonical(target)
    return tuple(
        path for path, _why in production_data_paths() if _canonical(path) == canonical
    )


#: 本进程是否已由**入口代码**显式声明"我要迁移生产库"（默认 False = 拒绝）。
#:
#: 为什么不走 env（依据：同类事故已两次 + 今晚 18:12:43 把生产库推到 schema 56）：
#: 环境变量会被**脚本随手带上**（`$env:YINYUE_ALLOW_PRODUCTION=1` 一设，隔离就没了），
#: 等于没拦；而"入口代码里那一行"没法被临时探测脚本继承——想放行就必须改入口本身，
#: 那是一个**要评审的改动**。唯一允许设置它的地方是 :func:`init_singletons` 的
#: ``allow_production=True`` 参数（服务入口 ``scheduler/main.py`` 显式传）。
#: 管理脚本入口（``scripts/reimport_speech_seed.py``）**不**传它：库有 pending 时按
#: 设计拒绝，库已最新时走 :func:`assert_migrations_allowed` 的"没有 pending 就放行"。
_PRODUCTION_DECLARED = False


def production_migration_declared() -> bool:
    """本进程有没有显式声明"允许迁移生产库"（只读；设置点只有 :func:`init_singletons`）。"""
    return bool(_PRODUCTION_DECLARED)


def _reset_production_declaration() -> None:
    """撤掉声明位（**只给用例用**：测"未声明"那一格时必须能回到默认拒绝）。

    产品代码里**没有**调用点——声明一旦由入口给出，本进程就该一直记得（否则服务
    启动之后第一次 ``get_meta_repo()`` 会被自己刚给过的声明反咬一口）。
    """
    global _PRODUCTION_DECLARED
    _PRODUCTION_DECLARED = False


def _allow_production_reason() -> str:
    """拒绝时给的那段"为什么 + 怎么办"（含 R-级理由、正确做法、怎么显式声明）。"""
    return (
        "R-级理由：迁移**生产库**是「程序启动的一部分」，不是测试/脚本/一次性探测的"
        "副产物（同类事故已两次，`docs/archive/任务清单_2026-10-06.md:207`——Migration 50 本地 "
        "2026-09-29 21:32、51 本地 22:58:19 被测试经 init_singletons() → run_migrations "
        "落到生产库；2026-10-02 18:12:43 又一次把生产库推到 schema 56）。"
        " 拦的判据是「**确实还有 pending 迁移**」（见上）：库已是最新时本门不拦，"
        "只用启动链的维护脚本照常跑（没有迁移可做，也就没有可写坏的东西）。"
        " 正确做法：脚本/探测/测试请把 META_DB_PATH / PLATFORMS_DIR / DATA_DIR 指到"
        "隔离根（scripts/run_all_tests.py 的快照根、tests/_tempdirs.py 的 .test_tmp）；"
        " 只有**服务入口**才该迁移生产库，它必须**显式声明**——在入口代码里写 "
        "`init_singletons(allow_production=True)`（默认 False；**不用 env**：env 会被"
        "脚本随手带上，等于没拦）。"
    )


def migration_chain_target_version() -> int:
    """迁移链的**目标版本** = ``db/migrations.py`` 里 ``@migration`` 注册表的 max(version)。

    刻意读注册表，不在本模块写死数字：写死就是第二份迁移版本台账，链上加了版本之后
    这里会静默落后，守卫随即退化成"永远没有 pending"（= 又把生产库放进来）。
    与 ``run_migrations``（``m.version > current`` 才是 pending）同一份口径。
    """
    from db.migrations import _MIGRATIONS

    return max((m.version for m in _MIGRATIONS), default=0)


def applied_schema_version(target: Any) -> int | None:
    """目标库 ``_schema_version`` 的 ``max(version)``（**只读**打开；读不到 → ``None``）。

    * ``mode=ro``：本函数是"碰任何库之前"的判据，绝不许建库/建表/写文件
      （库文件不存在也**不会**被它创建）；
    * 库不存在 / 没有 ``_schema_version`` 表 / 打不开 → ``None``（未知）。调用方按
      **有 pending** 处理（fail closed）：读不到就无法证明"没有迁移要做"。
    """
    raw = str(target or "").strip()
    if not raw:
        return None
    path = Path(raw)
    try:
        if not path.is_file():
            return None
    except OSError:  # pragma: no cover - 极端路径
        warn("core/bootstrap.py:224 OSError", err=repr(__import__("sys").exc_info()[1]))
        return None
    try:
        con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        try:
            row = con.execute("SELECT MAX(version) FROM _schema_version").fetchone()
        finally:
            con.close()
    except sqlite3.Error:
        warn("core/bootstrap.py:232 sqlite3.Error", err=repr(__import__("sys").exc_info()[1]))
        return None
    return int(row[0]) if row and row[0] is not None else None


def _pending_gap(target: Any) -> tuple[int | None, int]:
    """``target`` 的 ``(当前 schema 版本, 迁移链目标版本)``（当前版本读不到 → ``None``）。"""
    return applied_schema_version(target), migration_chain_target_version()


def assert_migrations_allowed(target: Any, *, allow_production: bool = False) -> None:
    """**默认拒绝**：迁移**生产库**必须由入口代码显式声明——**但只在确实有 pending 时**。

    为什么要这一条（`docs/archive/任务清单_2026-10-06.md:207`，同类事故已两次、今晚又一次）：测试/脚本经
    ``get_meta_repo() → init_singletons() → run_migrations`` 把 **Migration 50**
    （2026-09-29 21:32）与 **51**（22:58:19）落到了生产 ``data/yinyue_meta.db`` 与
    4 个账号库；根因只在 runner 一层修过（``scripts/run_all_tests.py`` 的快照 + 隔离根），
    所以**直接 import 的冒烟脚本 / 一次性探测 / 子代理**那条路仍然打得穿（2026-10-02
    又一次：迁移链把 meta 与 4 个账号库推到 schema 56）。

    为什么要**收窄**（2026-10-02 晚）：最初这一门是"非声明进程 + 生产路径 → 一律拒绝"，
    它把**不需要迁移**的合法维护脚本一起堵死了（`scripts/reimport_speech_seed.py` 这类
    走 ``init_singletons`` 启动链的脚本，库已是最新却照样抛错）。守卫要拦的是**迁移这个
    动作**，不是"用过启动链"；没有 pending 迁移时 ``run_migrations`` 一个版本都不会跑、
    库一个字节都不会变，拦下来只有误伤。

    判据（**次序有意**）：

    1. 目标库不在 :func:`production_data_paths` 里 → 放行（隔离/临时路径照常迁移，
       ``scripts/run_all_tests.py`` 的隔离根与 ``tests/_tempdirs.py`` 的 ``.test_tmp``
       都不受影响——**不许误伤**）；
    2. 命中生产路径、且本进程已经由入口声明过（``init_singletons(allow_production=True)``，
       见 :func:`production_migration_declared`，或是 ``allow_production=True`` 参数）
       → 放行（**服务照常启动**；声明优先，连库都不用读）；
    3. 命中生产路径、未声明、但**没有 pending 迁移**（``applied_schema_version(target)
       >= migration_chain_target_version()``）→ 放行：没有迁移可做，库不会被改
       （**不迁移、不改库**；维护脚本恢复正常）；
    4. 其余一律**拒绝**并抛错：命中生产路径 + **确实有 pending**（或 schema 版本读不到
       → 按有 pending 处理，fail closed）+ 未声明；**不论是不是测试进程**。旧实现只在
       "测试进程 + 生产路径"时拒绝，"非测试进程 + 生产路径"按设计放行——那正是缺口的形状：
       测试进程只是"最常踩的那个"，不是判据。

    ``allow_production`` 是**参数**而不是 env：env 会被脚本随手带上（
    ``$env:YINYUE_ALLOW_PRODUCTION=1``），等于没拦；参数只能由入口代码那一行给出。

    ``db/`` 侧的迁移入口（``AccountDBManager.register`` / ``db/sync_service.py`` 的
    ``run_migrations``）不归本文件管（写入域限制）；本函数是 ``init_singletons`` 在碰
    任何库之前的第一道门，也是唯一有"入口声明"这条信息的一道门。
    """
    hits = production_paths_for(target)
    if not hits:
        return
    if allow_production or production_migration_declared():
        log.info(
            "migration_production_declared",
            target=str(target),
            production_paths=list(hits),
            declared_by="init_singletons(allow_production=True)",
        )
        return

    current, target_version = _pending_gap(target)
    if current is not None and current >= target_version:
        log.info(
            "migration_production_up_to_date",
            target=str(target),
            production_paths=list(hits),
            schema_version=current,
            chain_target=target_version,
        )
        return

    gap = (
        f"当前 schema 版本 {current} < 迁移链目标 {target_version}"
        if current is not None
        else f"读不到 _schema_version（视作还有 pending；迁移链目标 {target_version}）"
    )
    log.error(
        "migration_blocked_production_path",
        target=str(target),
        production_paths=list(hits),
        schema_version=current,
        chain_target=target_version,
        under_tests=bool(running_under_tests()),
        reason=_allow_production_reason(),
    )
    raise RuntimeError(
        "拒绝迁移：目标库 {} 是**生产库**（命中 {}），且**还有 pending 迁移**（{}）。 {}".format(
            target, list(hits), gap, _allow_production_reason())
    )


import threading as _threading

_INIT_GUARD = _threading.Lock()
_INIT_OWNER = None
_INIT_WAIT_S = 90.0


def _init_enter() -> bool:
    """单飞闸：同一进程内只让一个线程真跑初始化，别的线程等它（有上限）。

    2026-10-25：网页线程（FastAPI lifespan 里的 ``init_singletons()``）和调度主线程
    （``init_singletons(allow_production=True)``）会同时走进来，而这里原先**一把锁都没有** ——
    两个线程同时跑 init_meta_db / run_migrations / 建 engine，网页线程就可能永远卡在 startup：
    端口已经 bind（uvicorn 在 lifespan 之前 bind）却永不应答，表现就是
    "前端打得开、所有接口都超时"。
    现在的语义：已有结果 → 复用；别人正在跑 → 最多等 _INIT_WAIT_S 秒；
    等满还没好 → 记 init_singletons_timeout 并**不再开第二条初始化**。
    """
    global _INIT_OWNER
    me = _threading.get_ident()
    if _initialized and _singletons:
        return False
    if _INIT_OWNER == me:                 # 递归（get_meta_repo 之类）别重跑
        return False
    if not _INIT_GUARD.acquire(timeout=_INIT_WAIT_S):
        log.error("init_singletons_timeout", waited_s=_INIT_WAIT_S,
                  hint="另一个线程还在初始化；这次跳过，避免并发迁移")
        return False
    if _initialized and _singletons:      # 等锁期间别人做完了
        _INIT_GUARD.release()
        return False
    _INIT_OWNER = me
    return True


def init_singletons(*, with_mcp: bool = False, with_rag: bool = False,
                    validate: bool = True,
                    allow_production: bool = False) -> dict:
    """幂等 + 线程安全的入口；真正的初始化在 :func:`_init_body`。"""
    global _INIT_OWNER
    if not _init_enter():
        return _singletons
    try:
        return _init_body(with_mcp=with_mcp, with_rag=with_rag,
                          validate=validate, allow_production=allow_production)
    finally:
        _INIT_OWNER = None
        try:
            _INIT_GUARD.release()
        except RuntimeError:
            pass


def _init_body(*, with_mcp: bool = False, with_rag: bool = False,
                    validate: bool = True,
                    allow_production: bool = False) -> dict:
    global _initialized, _PRODUCTION_DECLARED
    if _initialized and _singletons:
        return _singletons

    # 生产路径的迁移**必须由入口显式声明**（默认拒绝），判据在
    # :func:`assert_migrations_allowed`：非生产路径照常放行（隔离根/临时目录）；
    # 生产路径在"入口传了 allow_production=True / 本进程已经声明过"时放行，
    # 在**库已是最新（没有 pending 迁移）**时也放行（不迁移、不改库——只走启动链
    # 的维护脚本不再被误伤），只有"生产路径 + 确实还有 pending + 未声明"才拒绝。
    # 声明一次就对本进程后续所有 ``init_singletons()`` 调用生效——服务入口启动时声明
    # 一次；脚本/探测/测试进程不声明 → 若真有 pending 就在**碰任何库之前**抛错
    # （数据目录 mkdir / init_meta_db / run_migrations / 账号库注册全在它后面，
    # 迁移链一步都不会跑）。
    if allow_production:
        _PRODUCTION_DECLARED = True
    assert_migrations_allowed(
        META_DB_PATH, allow_production=bool(allow_production or _PRODUCTION_DECLARED))


    if validate:
        from core.config.validate import validate_and_report
        validate_and_report()

    init_dirs()

    meta_db_path = META_DB_PATH
    Path(meta_db_path).parent.mkdir(parents=True, exist_ok=True)
    init_meta_db(meta_db_path)
    run_migrations(meta_db_path)

    meta_repo = MetaRepositoryManager(meta_db_path)
    speech_store = SpeechStore(meta_repo.engine)
    try:
        speech_report = SpeechBootstrap(speech_store, meta_repo).ensure_seed()
        log.info("speech_seed_ready", **speech_report)
    except Exception as e:
        log.warning("speech_bootstrap_failed", error=str(e))

    account_db_manager = AccountDBManager()
    account_db_manager.load_from_config()
    account_db_manager.sync_to_registry(meta_repo)

    unified_query = UnifiedQuery(meta_repo, account_db_manager)

    llm = LLMClient()
    embedding = None  # [2026-10-25 去向量] EmbeddingClient 已移除
    vector_store = None  # [2026-10-25 去向量]

    provider_manager = None
    try:
        from core.execution.provider import ProviderManager, create_provider
        try:
            # 注册 QQ（OneBot/NapCat）Provider 工厂；模块导入失败不影响微信链路
            import core.execution.qq_provider  # noqa: F401
        except Exception as exc:  # noqa: BLE001
            log.warning("qq_provider_import_failed", error=str(exc)[:160])
        provider_manager = ProviderManager()
        for ac in config.all_account_configs():
            provider = create_provider(ac)
            if provider is not None:
                provider_manager.register(provider)
    except Exception as e:
        log.warning("provider_manager_init_failed", error=str(e))

    tools = ToolRegistry.create_default(
        repo=meta_repo,
        meta_repo=meta_repo,
        unified_query=unified_query,
        vector_store=vector_store,
        embedding_client=embedding,
        llm=llm,
        provider_manager=provider_manager,
    )

    runtime = RuntimeRegistry.build(
        config=config,
        account_db_manager=account_db_manager,
        meta_repo=meta_repo,
        provider_manager=provider_manager,
        llm=llm,
        embedding=embedding,
        vector_store=vector_store,
        unified_query=unified_query,
    )
    runtime.load_registered_accounts()

    intent_bus = None
    try:
        from core.execution.intent_bus import get_intent_bus
        from core.execution.wx_executor import WechatIntentExecutor
        intent_bus = get_intent_bus()
        intent_bus.register_executor("wechat", WechatIntentExecutor())
    except Exception as e:
        log.warning("intent_bus_init_failed", error=str(e))

    _singletons.update(
        meta_repo=meta_repo,
        account_db_manager=account_db_manager,
        unified_query=unified_query,
        repo=meta_repo,
        runtime=runtime,
        llm=llm,
        embedding=embedding,
        vector_store=vector_store,
        tools=tools,
        provider_manager=provider_manager,
        intent_bus=intent_bus,
        speech_store=speech_store,
        speech_facade=build_speech_facade(speech_store),
        outbound_gateway=build_outbound_gateway(speech_store),
    )

    if with_mcp:
        try:
            from core.mcp_bridge import MCPBridge
            _singletons["mcp_bridge"] = MCPBridge()
        except Exception as e:
            log.warning("mcp_init_failed", error=str(e))

    if with_rag:
        try:
            from core.rag import RAGEngine
            _singletons["rag"] = RAGEngine(repo=meta_repo, embedding=embedding, vector_store=vector_store)
        except Exception as e:
            log.warning("rag_init_failed", error=str(e))

    _initialized = True
    return _singletons


def get_meta_repo() -> MetaRepositoryManager:
    return init_singletons()["meta_repo"]


def get_account_db_manager() -> AccountDBManager:
    return init_singletons()["account_db_manager"]


def get_unified_query() -> UnifiedQuery:
    return init_singletons()["unified_query"]


_FALLBACK_WARNED = False


def get_repo() -> RepositoryManager | MetaRepositoryManager:
    """Return the active account repository, else the shared meta repository.

    The multi-account runtime sets a contextvar while handling an event; this
    keeps legacy helpers working without forcing every call site to accept an
    AccountContext parameter in the same commit.

    Account-scoped work should use :func:`get_account_repo` instead -- falling
    back to the meta repository can silently read the wrong tables.
    """
    # 组合根：把 core 侧声明的工具绑到 agents 侧实现（声明在 core、实现在 agents，
    # 规则 10 禁止 core import agents，所以只能在这一层反向注册）。失败不阻塞启动。
    try:
        from agents.tools.register import bind_declared as _bind_declared

        _bind_declared()
    except Exception as _bind_exc:  # noqa: BLE001
        import logging as _logging

        _logging.getLogger(__name__).info(
            "tool_bind_declared_failed error=%r", _bind_exc)

    ctx = current_account_context()
    if ctx is not None:
        return ctx.repo
    s = init_singletons()
    runtime = s.get("runtime")
    if runtime is not None and runtime.default is not None:
        return runtime.default.repo
    global _FALLBACK_WARNED
    if not _FALLBACK_WARNED:
        _FALLBACK_WARNED = True
        log.warning(
            "get_repo_fell_back_to_meta",
            hint="account-scoped callers should run inside an AccountContext; "
                 "use get_account_repo() to fail closed",
        )
    return s["repo"]


def get_account_repo():
    """Account-scoped repository; fails closed outside an AccountContext.

    Messages, contacts and account-level analysis live in per-account DBs.  A
    caller that needs them must run inside an AccountContext (or have a
    configured runtime default account); silently falling back to the meta DB
    would read the wrong store, so this raises instead.
    """
    ctx = current_account_context()
    if ctx is not None:
        return ctx.repo
    s = init_singletons()
    runtime = s.get("runtime")
    if runtime is not None and runtime.default is not None:
        return runtime.default.repo
    raise RuntimeError(
        "no account context available: account-scoped call refused "
        "(pass an AccountContext or configure a default account)"
    )


def get_runtime() -> RuntimeRegistry:
    return init_singletons()["runtime"]


def reload_runtime(config_obj=None) -> RuntimeRegistry:
    """Hot-reload platform/account contexts after registry changes."""
    runtime = get_runtime()
    runtime.reload_from_config(config_obj or config)
    return runtime


def get_account_context(platform: str, account_id: str):
    return get_runtime().get(platform, account_id)


def get_speech_store():
    return init_singletons()["speech_store"]


def get_speech_facade():
    return init_singletons()["speech_facade"]


def get_outbound_gateway():
    return init_singletons()["outbound_gateway"]


def get_llm() -> LLMClient:
    return init_singletons()["llm"]


def get_llm_router():
    """Lazy singleton Router bound to the singleton LLMClient (WP3)."""
    return get_llm()._ensure_router()


def get_embedding() -> None:
    # [2026-10-25 去向量] 向量能力已整体移除：这里保留函数名只为兼容调用点。
    # [2026-11-02 修 F821] 返回注解里的 EmbeddingClient 是幽灵名字（import 已删）。
    return None


def get_vector_store() -> None:
    # [2026-10-25 去向量] 向量能力已整体移除：这里保留函数名只为兼容调用点。
    # [2026-11-02 修 F821] 同上，注解换成 None。
    return None


def get_tools() -> ToolRegistry:
    ctx = current_account_context()
    s = init_singletons()
    runtime = s.get("runtime")
    if ctx is None and runtime is not None:
        ctx = runtime.default
    if ctx is not None and runtime is not None:
        scoped = runtime.tools_for(ctx)
        if scoped is not None:
            return scoped
    return s["tools"]


def get_provider_manager():
    return init_singletons().get("provider_manager")


def get_mcp_bridge():
    return init_singletons().get("mcp_bridge")


def get_rag():
    return init_singletons().get("rag")


def get_intent_bus():
    return init_singletons().get("intent_bus")


def build_agent(*args, **kwargs):
    """旧大脑构造器（2026-10-24 退役）。

    保留函数名是为了让历史脚本给出清晰错误，而不是 ImportError。
    """
    raise RuntimeError(
        "旧大脑 ConversationalAgent 已退役（2026-10-24）；"
        "生成由 loop/ 引擎（loop.brain.run_loop）负责。"
    )

def bind_tool_package(old_registry: Any = None) -> Any:
    """把已有的工具实现接成**工具包**认的形状（`core/tools/builtin.py`）。

    为什么要有这一步：`core/jobs/executor.py` 只认 `core/tools/registry.py` 的契约
    （同步 handler + `ToolResult`），而 `agents/tools/*` 是异步、面向模型的实现。
    接不上，「在手的事」就没有手——`_job_tick` 只能一直停在 `no_executor`。

    只接只读与写本地两类；`external_send` 一个都不许接（只走出口，R3）。
    接不上不许影响启动：绑失败只记一行日志，回合照常。
    """
    try:
        from core.tools import builtin

        base: dict[str, Any] = {}
        if old_registry is not None:
            for name in ("web_search", "fetch_url"):
                tool = None
                try:
                    tool = old_registry.get(name)
                except Exception:  # noqa: BLE001 - 旧表形状不确定，取不到就当没有
                    warn("core/bootstrap.py:635 Exception", err=repr(__import__("sys").exc_info()[1]))
                    tool = None
                if tool is not None:
                    base[name] = tool
        return builtin.bootstrap(base, enable_writes=True)
    except Exception as exc:  # noqa: BLE001 - 绑工具不许拖垮启动
        warn("core/bootstrap.py:640 Exception", err=repr(exc))
        try:
            log.warning("tool_package_bind_failed", error=repr(exc))
        except Exception:  # noqa: BLE001
            warn("core/bootstrap.py:643 Exception", err=repr(__import__("sys").exc_info()[1]))
        return None


def get_db_path() -> str:
    return META_DB_PATH
