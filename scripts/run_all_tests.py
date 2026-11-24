#!/usr/bin/env python
"""Run every tests/test_*.py directly and write a machine-readable report.

The repository's tests are standalone scripts (``python tests/test_x.py``),
not a pytest suite.  This runner gives one command that executes all of them,
keeps a full log per test, and prints a compact failure summary.

    python scripts/run_all_tests.py
    python scripts/run_all_tests.py --filter terminal
    python scripts/run_all_tests.py --timeout 600
    python scripts/run_all_tests.py --keep

Every child process runs against a **snapshot + isolation root** (see below):
the real databases are never touched, and test logs never reach the
production log file.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import uuid
from datetime import datetime
from pathlib import Path

# Windows 控制台常是 GBK，子进程输出经 errors="replace" 后仍可能含 U+FFFD。
# 测试入口统一用 UTF-8 输出，避免收尾报告自己因编码失败。
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
# 测试侧唯一的临时目录入口（见 tests/_tempdirs.py 的模块注释）。runner 与测试
# 必须用**同一个**基座，否则隔离根和用例目录会在两处各建一套。
sys.path.insert(0, str(ROOT / "tests"))
from _tempdirs import temp_base  # noqa: E402

# 基座目录是「代码建的、且被 .gitignore 忽略」的：清理工作区后它不存在，
# 而部分用例直接 tempfile.mkdtemp(dir=ROOT/".test_tmp")，会 FileNotFoundError。
(ROOT / ".test_tmp").mkdir(parents=True, exist_ok=True)

DEFAULT_TIMEOUT = 600

# ---------------------------------------------------------------------------
# DSH-019：环境不支持时**显式记跳过**，不要输出假失败把真失败淹掉
# ---------------------------------------------------------------------------
# 独立脚本套件只看 exit code，于是「真验过了」和「缺素材/缺 WSL 所以没验」
# 在汇总里一模一样——后者只是没证据，不是证据。约定：
#   * 新写法：`tests/_skip.py::skip("缺什么")`，打印 `⏭ 跳过：缺什么`；
#   * 旧写法：各文件自己 print 的 `SKIP (...)` / `skip: ...`，这里兼容识别，
#     只用于判定「这个文件里有东西没验」，不逐文件改（41 处，改是穷举）。
# 判定：exit 0 且有跳过行 ⇒ 状态 SKIP（既不算 PASS 也不算失败，不进 exit code）。
SKIP_MARK = "\u23ed \u8df3\u8fc7"
LEGACY_SKIP_RE = re.compile(r"^(?:-\s*)?(?:SKIP|skip)\b")

# ---------------------------------------------------------------------------
# P0-a 根因隔离（2026-09-29 事故）
# ---------------------------------------------------------------------------
# tests/ 下是「独立脚本」，但不少文件会经
#   core.bootstrap.init_singletons() → init_meta_db + run_migrations
#   db.account_db_manager.AccountDBManager.register() → run_migrations
# 直接落到 PathSettings 解析出的**生产库**：Migration 50/51 就是这么被测试落到
# data/yinyue_meta.db 与 data/platforms/*/*/account.db 的（同类事故已发生两次）。
# 给单个测试文件打补丁只能挡住已知的那几个，所以正统 runner 必须给**所有子进
# 程**先建一个「快照 + 隔离」根：
#   * 真实库用 sqlite3 的 ``Connection.backup()`` 拍一致性快照（服务可能在跑，
#     不能拷文件）；
#   * ``META_DB_PATH`` / ``DATA_DIR`` / ``PLATFORMS_DIR`` / ``LOG_DIR`` 全部指向
#     临时根（``LOG_DIR`` 一并改道：测试日志不再漏进 logs/yinyue.log，ENG-12）；
#   * 测试照旧读到**等价数据**（test_context_assembler_equivalence 要真实
#     persona / 说话策略），写入一律留在临时根里；
#   * 某个库不存在/快照失败只告警，不让整套变红。
PRODUCTION_DATA_DIR = ROOT / "data"
PRODUCTION_META_DB = PRODUCTION_DATA_DIR / "yinyue_meta.db"
PRODUCTION_PLATFORMS_DIR = PRODUCTION_DATA_DIR / "platforms"
ISOLATION_ENV_KEYS = (
    "META_DB_PATH", "PLATFORMS_DIR", "DATA_DIR", "LOG_DIR",
    # 2026-10-10：canonical/旧 intent trace 也要随隔离根改道，
    # 否则测试的 detail 行会混进生产 decision_trace.jsonl。
    "YINYUE_DECISION_TRACE_PATH", "YINYUE_INTENT_TRACE_PATH", "YINYUE_LEDGER_DIR",
)

# ---------------------------------------------------------------------------
# P0-b 开关隔离（2026-10-02，同类事故第三次：`.env` 的 feature 开关变成测试的
# 隐式输入，见 core/env.py 模块 docstring）
# ---------------------------------------------------------------------------
# 每个子进程都拿到这个标记：`.env` 里的**开关类**键一律不注入（资料类键照旧）。
# 测试要哪个开关就自己显式设——"flag 关 = 旧行为"的断言不许被生产配置改写。
# 名字来源是 core/env.py 的常量（唯一声明处），这里只做转发。
from core.env import TEST_NO_ENV_FLAGS as TEST_NO_ENV_FLAGS_ENV  # noqa: E402

# 子进程的临时目录也必须改道（2026-10-01 23:14 事故）：本机沙箱下子进程写不进
# 系统 TEMP，而 SQLite 的 win32 VFS 用 GetTempPath()（读 TMP/TEMP）放临时文件，
# 路径不可写时直接 SQLITE_CANTOPEN，报成
# ``OperationalError: unable to open database file``——18 个测试同时红。
# 只把目录换个地方建不够，必须把环境变量一起给子进程，原生代码才跟着走。
TEMP_ENV_KEYS = ("TEMP", "TMP", "TMPDIR", "SQLITE_TMPDIR")

# 测试要读的不只是库：sticker_index.json / sticker_templates / emoji registry
# 这些资源文件也在 data/ 下，只快照 .db 会让用例在隔离环境里读到空目录
# （实测 test_owner_sticker_description、test_wx_vision 因此变红）。所以把
# 「输入型」资源一起镜像过去；大体积派生目录（decrypted / chroma / 各种
# cache）不镜像——它们由代码在临时根里重建，拷几百 MB 只会拖慢每次跑测试。
_SKIP_DATA_DIRS = frozenset({
    "archive", "chroma", "debug", "decrypted",
    "embedding_cache", "emoji_cache", "image_cache",
})
_DB_SUFFIXES = (".db", ".db-wal", ".db-shm")


def _is_db_artifact(name: str) -> bool:
    lowered = name.lower()
    return lowered.endswith(_DB_SUFFIXES) or ".db.bak" in lowered


def _ignore_for_mirror(directory: str, names: list[str]) -> set[str]:
    ignored: set[str] = set()
    for name in names:
        if name.startswith("_"):
            ignored.add(name)          # data/_* 是测试 scratch
        elif name in _SKIP_DATA_DIRS:
            ignored.add(name)
        elif _is_db_artifact(name):
            ignored.add(name)          # 库由 backup() 单独拍一致性快照
    return ignored


def mirror_assets(root: Path) -> dict:
    """把 data/ 下的输入型资源镜像进隔离根（库文件留给 ``snapshot_sqlite``）。"""
    dest = root / "data"
    try:
        shutil.copytree(
            PRODUCTION_DATA_DIR,
            dest,
            ignore=_ignore_for_mirror,
            dirs_exist_ok=True,
        )
    except Exception as exc:  # noqa: BLE001 - 资源缺失只告警，不让整套变红
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:200]}
    files = sum(1 for path in dest.rglob("*") if path.is_file())
    return {"ok": True, "files": files, "dest": str(dest)}


def production_dbs() -> list[Path]:
    """要快照的库：meta 库 + 每个 ``data/platforms/<p>/<a>/account.db``。"""
    dbs = [PRODUCTION_META_DB]
    if PRODUCTION_PLATFORMS_DIR.is_dir():
        dbs.extend(sorted(PRODUCTION_PLATFORMS_DIR.glob("*/*/account.db")))
    return dbs


def _connect_readonly(path: Path) -> sqlite3.Connection:
    """只读打开（不产生 -wal/-shm 副作用）；WAL 下失败再退回普通连接。

    退回的那次连接只用于 ``backup()``（读），不会写源库。
    """
    try:
        return sqlite3.connect(
            f"file:{path.as_posix()}?mode=ro", uri=True, timeout=30
        )
    except sqlite3.Error:
        return sqlite3.connect(str(path), timeout=30)


def snapshot_sqlite(src: Path, dest: Path) -> dict:
    """把 ``src`` 一致性快照到 ``dest``；失败只记录，不抛错。"""
    entry: dict = {"src": str(src), "dest": str(dest), "ok": False}
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            dest.unlink()
        source = _connect_readonly(src)
        try:
            target = sqlite3.connect(str(dest), timeout=30)
            try:
                source.backup(target)
            finally:
                target.close()
        finally:
            source.close()
        entry["ok"] = True
        entry["bytes"] = dest.stat().st_size
    except Exception as exc:  # noqa: BLE001 - 缺库/锁住都不该让整套变红
        entry["error"] = f"{type(exc).__name__}: {exc}"[:200]
    return entry


def create_isolation_root(root: Path | None = None) -> dict:
    """建「快照 + 隔离」根；返回 ``{root, base, tmp, env, snapshots}``。

    基座来自 ``tests/_tempdirs.py`` 的唯一入口（``YINYUE_TEST_TMP`` →
    ``ROOT/.test_tmp``），**不再**用 ``tempfile.gettempdir()``：后者在本机沙箱下
    会回落到仓库根，隔离根就跟着进了仓库（2026-10-01 23:14 实测
    ``isolation_root=C:\\...\\yinyue\\yinyue_test_root_20261001_231419_794fd7``）。

    也不用 ``tempfile.mkdtemp``：它用 0o700 建目录，受限环境里 sqlite 反而写不
    进去（2026-09-29 实测：mkdtemp 目录内 connect 直接
    ``unable to open database file``）。``Path.mkdir`` 用继承的默认 ACL，正常。
    """
    base = temp_base()
    if root is None:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        root = base / f"yinyue_test_root_{stamp}_{uuid.uuid4().hex[:6]}"
    root.mkdir(parents=True, exist_ok=True)
    # 子进程的临时目录：隔离根内的 tmp/，随隔离根一起删，跑完零残留。
    tmp = root / "tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    data_dir = root / "data"
    env = {
        "META_DB_PATH": str(data_dir / "yinyue_meta.db"),
        "DATA_DIR": str(data_dir),
        "PLATFORMS_DIR": str(data_dir / "platforms"),
        "LOG_DIR": str(root / "logs"),
        "YINYUE_DECISION_TRACE_PATH": str(root / "logs" / "decision_trace.jsonl"),
        "YINYUE_INTENT_TRACE_PATH": str(root / "logs" / "intent_trace.jsonl"),
        # 流水账本也随隔离根改道：否则测试里的 say/remember 会写进她的真实账本。
        "YINYUE_LEDGER_DIR": str(data_dir / "loop"),
    }
    assets = mirror_assets(root)
    snapshots = [
        snapshot_sqlite(src, root / src.relative_to(ROOT)) for src in production_dbs()
    ]
    return {
        "root": str(root),
        "base": str(base),
        "tmp": str(tmp),
        "env": env,
        "assets": assets,
        "snapshots": snapshots,
    }


def remove_isolation_root(root: str) -> None:
    shutil.rmtree(root, ignore_errors=True)


def child_env(isolation: dict, out_dir: Path) -> dict:
    """子进程环境（隔离根 + 临时目录改道 + 开关隔离标记）。

    单独成函数是为了可被守卫用例读到：``tests/test_env_flag_isolation.py``
    直接调它，断言标记真的进了子进程环境（改掉这一行 → 守卫红）。
    """
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    env.update({key: isolation["env"][key] for key in ISOLATION_ENV_KEYS})
    # 临时目录改道：子进程继承的 TEMP 可能不可写（本机沙箱就是这样），原生代码
    # （SQLite/jieba）拿它放临时文件时会直接失败。指向隔离根内的 tmp/，既保证
    # 可写，又随隔离根一起清理。YINYUE_TEST_TMP 一并给，让子进程里的
    # tests/_tempdirs.py 认同一个基座（否则它会退回 ROOT/.test_tmp）。
    env["YINYUE_TEST_TMP"] = isolation["tmp"]
    for key in TEMP_ENV_KEYS:
        env[key] = isolation["tmp"]
    # 召回 trace 之前没有测试隔离：owner_raw/bot_a 等夹具会污染
    # logs/memory_recall_trace.jsonl，分析真实流量前还得先剔除。跑测试统一改道。
    env["YINYUE_MEMORY_RECALL_TRACE_PATH"] = str(
        out_dir / "memory_recall_trace_test.jsonl"
    )
    # 准入审计同样没有测试隔离：夹具会写进 logs/admission_trace.jsonl，
    # 污染校准回放凭据。跑测试统一改道。
    env["YINYUE_ADMISSION_TRACE_PATH"] = str(
        out_dir / "admission_trace_test.jsonl"
    )
    # P0-b 开关隔离：`.env` 的开关类键不许进测试进程（core/env.py 是唯一读法）。
    # 默认打开；操作者**显式**给了假值（`YINYUE_TEST_NO_ENV_FLAGS=0`，对照/变异
    # 自检位）就照他的来——否则没法做"机制关掉 vs 打开"的 A/B。
    env.setdefault(TEST_NO_ENV_FLAGS_ENV, "1")
    return env


KNOWN_RED_PATH = ROOT / "tests" / "known_red.yaml"


def load_known_red() -> dict:
    """读 ``tests/known_red.yaml``：在册的已知红（阶段 1 立的门禁）。

    缺席 = 空表（门禁照旧严格，不因为文件丢了就放行）。
    键用文件名（``test_x.py``），与 runner 里的 ``test.name`` 对齐。
    """
    if not KNOWN_RED_PATH.exists():
        return {}
    try:
        import yaml  # 局部导入：缺 PyYAML 时不该让全量回归起不来
    except Exception:
        return {}
    data = yaml.safe_load(KNOWN_RED_PATH.read_text(encoding="utf-8")) or {}
    table: dict = {}
    for item in data.get("entries") or []:
        name = Path(str(item.get("file", ""))).name
        if name:
            table[name] = item
    return table


def _count_skips(output: str) -> tuple[int, int]:
    """数一个测试文件输出里的跳过行，返回 ``(新写法条数, 旧写法条数)``。

    ``⏭ 跳过：…`` 是 ``tests/_skip.py`` 的唯一记法；``SKIP (...)`` / ``skip: …``
    是 2026-10-29 之前各文件自己的写法，只为兼容存量、不鼓励新增。
    """
    new = legacy = 0
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if SKIP_MARK in line:
            new += 1
        elif LEGACY_SKIP_RE.match(line):
            legacy += 1
    return new, legacy


ENTRY_MARKERS = ("__main__", "run_all(")


def _has_entry_point(path: Path) -> bool:  # 2026-11-02 修 F821：pathlib 没以这个名字导入
    """`python <file>` 跑起来会不会真执行用例？

    独立脚本套件里「没有入口」= 模块导入完就退出：exit 0、零输出，全量里等于
    凭空 PASS（DSH-019 要治的假绿就是这个的近亲）。判据只有这一处；`--filter`
    想单跑某个文件时也一样吃它，别在调用点另写一套。

    `scripts/audit_vacuous_tests.py` 的静态名单比这里更严（还要求有 `def test_`），
    两者刻意不同：门禁只问「跑起来有没有东西执行」，审计问「是不是 pytest 风格的尸体」。
    """
    text = path.read_text(encoding="utf-8", errors="replace")
    return any(marker in text for marker in ENTRY_MARKERS)


def _side_reports() -> None:
    """收尾体检（report-only，刻意不参与 exit code）。

    两项都是「不跑测试、不读活体目录」的静态检查，跑过几轮已经稳定：

      * ``MAP-CHECK``  ``docs/现状地图.md`` 是否落后于当前代码
        （``scripts/gen_repo_map.py --check``。「生成时间」「HEAD」与 §9 的目录计数
        本来就不参与比对，§9 现在直接不写数）。
      * ``VACUOUS``    有没有「写了 ``def test_`` 却没有 ``__main__`` 运行器」的文件。
        独立脚本套件下那种文件跑起来 exit 0、零输出，在全量里等于凭空 PASS
        （``scripts/audit_vacuous_tests.py``：默认只静态扫，不加 ``--run`` 不起子进程）。

    以后要翻成门禁，**只改这一个函数**：把下面的结果交给调用点、让 ``main()`` 的
    return 吃它，别在调用点复制判据。
    """
    checks = (
        ("MAP-CHECK", ["scripts/gen_repo_map.py", "--check"]),
        ("VACUOUS", ["scripts/audit_vacuous_tests.py"]),
    )
    print()
    for label, argv in checks:
        try:
            proc = subprocess.run(
                [sys.executable, *argv],
                cwd=str(ROOT),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=300,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            print(f"{label} 跳过（{type(exc).__name__}），不挡提交")
            continue
        # 类型归一：`text=True` 之后本应全是 str，但这里**曾经**在实跑中拿到过 str
        # 却被当 bytes 拼（TypeError 把整段收尾报告吃掉了，见 4f8de71 那次）。既然后来
        # 复现不出原因，就不把正确性押在「参数写对了」上——两种都认。
        out, err = proc.stdout or "", proc.stderr or ""
        out = out.decode("utf-8", "replace") if isinstance(out, bytes) else out
        err = err.decode("utf-8", "replace") if isinstance(err, bytes) else err
        text = out + err
        lines = [ln.rstrip() for ln in text.splitlines() if ln.strip()]
        head = lines[0].strip() if lines else "(无输出)"
        if proc.returncode == 0:
            print(f"{label} ok（不参与 exit code）: {head}")
        else:
            print(f"{label} 需要处理（不挡提交）: {head}")
            for line in lines[1:5]:
                print(f"    {line.strip()}")
    print("（MAP-CHECK / VACUOUS 是 report-only：要它们挡提交得自己翻门禁，见 _side_reports 的注释）")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run all repository tests")
    parser.add_argument("--filter", default="", help="substring filter on file name")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    parser.add_argument(
        "--keep",
        action="store_true",
        help="保留「快照 + 隔离」根（排查用；默认跑完清理）",
    )
    args = parser.parse_args()

    tests = sorted((ROOT / "tests").glob("test_*.py"))
    if args.filter:
        tests = [p for p in tests if args.filter in p.name]
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = ROOT / "output" / f"test_run_{stamp}"
    out_dir.mkdir(parents=True, exist_ok=True)

    # P0-a：先把真实库快照到临时根，再让所有子进程只看见临时根。
    isolation = create_isolation_root()
    isolation_root = isolation["root"]
    snapshot_ok = [item for item in isolation["snapshots"] if item["ok"]]
    snapshot_bad = [item for item in isolation["snapshots"] if not item["ok"]]

    env = child_env(isolation, out_dir)
    known_red = load_known_red()

    results: list[dict] = []
    assets = isolation.get("assets") or {}
    assets_note = (
        f"files={assets.get('files')}" if assets.get("ok") else
        f"FAILED({assets.get('error')})"
    )
    print(
        f"test_run={out_dir} tests={len(tests)} timeout={args.timeout}s\n"
        f"tmp_base={isolation['base']}\n"
        f"isolation_root={isolation_root}\n"
        f"child_tmp={isolation['tmp']}\n"
        f"snapshots={len(snapshot_ok)}/{len(isolation['snapshots'])} ok"
        f" assets={assets_note}"
    )
    for item in snapshot_bad:
        print(f"    snapshot skipped: {item['src']} ({item.get('error')})")
    print(
        "    env META_DB_PATH={META_DB_PATH} DATA_DIR={DATA_DIR} "
        "PLATFORMS_DIR={PLATFORMS_DIR} LOG_DIR={LOG_DIR} "
        "DECISION_TRACE={YINYUE_DECISION_TRACE_PATH}".format(**isolation["env"])
    )
    print(
        f"    env {TEST_NO_ENV_FLAGS_ENV}={env.get(TEST_NO_ENV_FLAGS_ENV)}"
        "（1=测试进程不继承 .env 的开关；0=对照位，照旧全注入）"
    )
    try:
        for index, test in enumerate(tests, 1):
            log_path = out_dir / f"{test.stem}.log"
            started = datetime.now()
            try:
                proc = subprocess.run(
                    [sys.executable, str(test)],
                    cwd=str(ROOT),
                    env=env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=max(10, int(args.timeout)),
                )
                output = proc.stdout or ""
                exit_code = int(proc.returncode)
                timed_out = False
            except subprocess.TimeoutExpired as exc:
                output = (
                    (exc.stdout if isinstance(exc.stdout, str) else "")
                    or ""
                ) + f"\n[TIMEOUT after {args.timeout}s]\n"
                exit_code = 124
                timed_out = True

            log_path.write_text(output, encoding="utf-8")
            tail = "\n".join(output.strip().splitlines()[-6:])
            # 「exit 0」有两种：真验过了，或者因为环境不支持一条都没验。
            skip_hits, legacy_hits = _count_skips(output)
            entry = _has_entry_point(test)
            result = {
                "test": test.name,
                "ok": exit_code == 0,
                "entry": entry,
                "exit_code": exit_code,
                "timed_out": timed_out,
                "seconds": round((datetime.now() - started).total_seconds(), 1),
                "log": str(log_path),
                "tail": tail,
                "skips": skip_hits,
                "legacy_skips": legacy_hits,
            }
            results.append(result)
            known = known_red.get(test.name)
            skip_n = result["skips"] + result["legacy_skips"]
            if not entry:
                # 没入口：`python xxx.py` 导入完就退出，exit 0 也没执行任何用例。
                status = "NO-RUN"
            elif result["ok"]:
                if skip_n:
                    # 有东西没验：这不是 PASS，更不该记 FIXED（FIXED 会诱导把它出表）。
                    status = "SKIP"
                else:
                    status = "FIXED" if known else "PASS"
            elif known:
                status = "KNOWN-RED"
            else:
                status = "TIMEOUT" if timed_out else "FAIL"
            print(
                f"[{index:02d}/{len(tests)}] {status:7} {test.name:44} "
                f"{result['seconds']:6.1f}s"
            )
            if not result["ok"]:
                if known:
                    print(
                        "    known_red[{}]: {}".format(
                            known.get("root_cause", "?"), known.get("reason", "")
                        )
                    )
                else:
                    print(f"    {tail.splitlines()[-1] if tail else '(no output)'}")
            elif not result["entry"]:
                print(
                    "    没有 __main__ / run_all(...)：跑了等于没跑；"
                    "pytest 风格请放 tests/pytest_style/（由 scripts/run_pytest_suite.py 跑）"
                )
            elif skip_n:
                print(
                    "    {} {} 条（{}）{}".format(
                        SKIP_MARK,
                        skip_n,
                        "旧写法，建议改用 tests/_skip.py 的 skip()"
                        if result["legacy_skips"]
                        else "新写法",
                        "；在册且不再失败，但没验完，别急着出表" if known else "",
                    )
                )

        no_run = [r for r in results if not r["entry"]]
        passed = [r for r in results if r["ok"] and r["entry"]]
        failed = [r for r in results if not r["ok"] and r["entry"]]
        known_failed = [r for r in failed if r["test"] in known_red]
        new_failed = [r for r in failed if r["test"] not in known_red]
        fixed = [r for r in passed if r["test"] in known_red]
        skipped = [r for r in passed if r["skips"] or r["legacy_skips"]]
        summary = {
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "total": len(results),
            "passed": len(passed),
            "failed": len(failed),
            "new_red": len(new_failed),
            "known_red": len(known_failed),
            "fixed": len(fixed),
            "no_run": len(no_run),
            "skipped": len(skipped),
            "skip_lines": sum(r["skips"] + r["legacy_skips"] for r in skipped),
            "isolation": isolation,
            "results": results,
        }
        summary_path = out_dir / "summary.json"
        summary_path.write_text(
            json.dumps(summary, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print()
        print(
            f"RESULT {len(passed)}/{len(results)} passed"
            f" · NEW-RED {len(new_failed)}"
            f" · KNOWN-RED {len(known_failed)}"
            f" · FIXED {len(fixed)}"
            f" · SKIP {len(skipped)}"
            f" · NO-RUN {len(no_run)}"
        )
        print(f"summary={summary_path}")
        if skipped:
            print(f"{SKIP_MARK} 有 {len(skipped)} 个文件里有没验的用例（不算失败，也不算 PASS）：")
            for item in skipped[:12]:
                print(
                    "    {}  {} 条（新 {} / 旧 {}）".format(
                        item["test"], item["skips"] + item["legacy_skips"], item["skips"], item["legacy_skips"]
                    )
                )
            if len(skipped) > 12:
                print(f"    … 还有 {len(skipped) - 12} 个，见 summary.json 的 results[].skips")
        if no_run:
            print("NO-RUN（不算通过，挡提交；要么补入口，要么搬去 tests/pytest_style/）:")
            for result in no_run[:12]:
                print(f"  - {result['test']}")
            if len(no_run) > 12:
                print(f"    … 还有 {len(no_run) - 12} 个")
        if new_failed:
            print("NEW-RED（挡提交）:")
            for result in new_failed:
                print(f"  - {result['test']} exit={result['exit_code']}")
        if known_failed:
            print("KNOWN-RED（在册，不挡提交；修好请从 tests/known_red.yaml 删掉）:")
            for result in known_failed:
                meta = known_red.get(result["test"], {})
                print(
                    "  - {} [{}] {}".format(
                        result["test"],
                        meta.get("root_cause", "?"),
                        meta.get("owner", "?"),
                    )
                )
        if fixed:
            print("FIXED（在册但已通过，请从 tests/known_red.yaml 删掉）:")
            for result in fixed:
                print(f"  - {result['test']}")
        try:
            _side_reports()
        except Exception as exc:  # noqa: BLE001 - 收尾报告是量具：坏了要吵，但不许吞掉主判定
            print(f"SIDE-REPORTS failed: {type(exc).__name__}: {exc}")
    finally:
        if args.keep:
            print(f"isolation_kept={isolation_root}")
        else:
            remove_isolation_root(isolation_root)
    return 0 if not (new_failed or no_run) else 1


if __name__ == "__main__":
    raise SystemExit(main())
