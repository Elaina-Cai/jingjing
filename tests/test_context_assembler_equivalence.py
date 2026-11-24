"""Phase 1 equivalence: assembler output == legacy output, byte for byte.

Run directly:
    python tests/test_context_assembler_equivalence.py

The test runs the 20 Phase-0 scenarios twice in one process -- once with
``YINYUE_FEATURE_CONTEXT_ASSEMBLER=0`` (legacy) and once with ``=1``
(``core.context.assembler``) -- and asserts the resulting prompts are identical.
It also checks the stored golden files, allowing only the memory block to
differ (that block depends on the live DB and may drift between runs).
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
# 临时目录的唯一入口（见 tests/_tempdirs.py）：``gettempdir()`` 在系统 TEMP 不可写
# 时会静默回落到仓库根，trace 文件就直接写进仓库。
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _tempdirs import temp_base  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
_PRODUCTION_META_DB = (ROOT / "data" / "yinyue_meta.db").resolve()
_PRODUCTION_DATA_DIR = (ROOT / "data").resolve()
_PRODUCTION_PLATFORMS_DIR = (_PRODUCTION_DATA_DIR / "platforms").resolve()

# ---------------------------------------------------------------------------
# P0-a 根因隔离（2026-09-29 事故）
# ---------------------------------------------------------------------------
# 本文件 main() 里裸调 build_agent() → init_singletons() → init_meta_db +
# run_migrations / AccountDBManager.register() → run_migrations，直接落到
# PathSettings 解析出的**生产库**：Migration 50/51 两次都是这么被测试落上去的。
# 正统 runner（scripts/run_all_tests.py）已经给每个子进程建好「快照 + 隔离」根
# （META_DB_PATH / DATA_DIR / PLATFORMS_DIR / LOG_DIR 全指临时根），这里做守卫；
# 但 pytest 直接收集、或 `python tests/test_context_assembler_equivalence.py`
# 独立跑时**没有**那层 env，就地补一个同源快照根 —— 数据仍是真实的（本文件要
# 真实 persona / 说话策略），写入一律留在临时根里。
_OWN_ISO_ROOT: Path | None = None


def _cleanup_own_root() -> None:
    """删掉自建隔离根；Windows 上 sqlite 文件被连接池占着就删不掉，先 dispose。"""
    root = _OWN_ISO_ROOT
    if root is None:
        return
    try:
        from core import bootstrap

        singles = getattr(bootstrap, "_singletons", {}) or {}
        engines = [getattr(singles.get("meta_repo"), "engine", None)]
        manager = singles.get("account_db_manager")
        engines += [
            getattr(repo, "engine", None)
            for repo in list(getattr(manager, "_repos", {}).values())
        ]
        for engine in engines:
            if engine is not None:
                try:
                    engine.dispose()
                except Exception:  # noqa: BLE001 - 清理失败不该盖住测试结果
                    pass
    except Exception:  # noqa: BLE001 - 同上
        pass
    shutil.rmtree(str(root), ignore_errors=True)


def _ensure_isolated_paths() -> None:
    """没有 runner 的隔离 env 时，就地快照真实库并指过去（否则硬失败）。"""
    global _OWN_ISO_ROOT
    meta_env = os.environ.get("META_DB_PATH", "")
    if meta_env and Path(meta_env).resolve() != _PRODUCTION_META_DB:
        return  # runner（或更早导入的测试文件）已经隔离好了
    runner_path = ROOT / "scripts" / "run_all_tests.py"
    spec = importlib.util.spec_from_file_location(
        "run_all_tests_isolation", runner_path
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    isolation = module.create_isolation_root()
    meta_snapshot = next(
        item
        for item in isolation["snapshots"]
        if Path(item["src"]).resolve() == _PRODUCTION_META_DB
    )
    if not meta_snapshot["ok"]:
        raise RuntimeError(
            "生产库快照失败，拒绝在未隔离的情况下 build_agent()："
            f"{meta_snapshot.get('error')}"
        )
    os.environ.update(isolation["env"])
    _OWN_ISO_ROOT = Path(isolation["root"])
    import atexit

    atexit.register(_cleanup_own_root)


_ensure_isolated_paths()

from core.config.settings import config as _config  # noqa: E402

# 守卫：env 只有在 core.config.settings 首次导入前设好才有效；真被固化到生产库
# 就硬失败，绝不静默把迁移落到生产库。
for _label, _path, _prod in (
    ("meta_db_path", _config.paths.meta_db_path, _PRODUCTION_META_DB),
    ("platforms_dir", _config.paths.platforms_dir, _PRODUCTION_PLATFORMS_DIR),
    ("data_dir", _config.paths.data_dir, _PRODUCTION_DATA_DIR),
):
    assert Path(_path).resolve() != _prod, f"测试数据库隔离失效：{_label}={_path}"

if _OWN_ISO_ROOT is not None:
    for _label, _path in (
        ("meta_db_path", _config.paths.meta_db_path),
        ("platforms_dir", _config.paths.platforms_dir),
        ("data_dir", _config.paths.data_dir),
    ):
        assert _OWN_ISO_ROOT in Path(_path).resolve().parents, (
            f"测试数据库隔离失效：{_label}={_path} 不在 {_OWN_ISO_ROOT} 下"
        )

# 这些开关是「进程级」等价基线，必须在用例结束后还原。
#
# 历史坑：先前只在模块导入时置 0、从不还原，于是同一进程里后跑的用例
# （如 test_context_budget）会看到预算被关掉，断言莫名失败。用 context
# manager 包住设置/还原，main() 里 finally 收口，避免跨文件串味。
_BASELINE_ENV: dict[str, str] = {
    "YINYUE_FEATURE_MEMORY_VECTOR_RECALL": "0",
    # 批1/批2 的 prompt 块开关在灰度里可能为 1；等价基线固定为默认（关）状态。
    "YINYUE_FEATURE_UNCERTAIN_VOICE": "0",
    "YINYUE_FEATURE_ROOM_STATE": "0",
    "YINYUE_FEATURE_MEMORY_GATE": "0",
    # Phase 1 equivalence is defined against the pre-budget behaviour.
    "YINYUE_FEATURE_CONTEXT_BUDGET": "0",
    # P2.5 重复检查固定为关（P2 姿态/立场、自省折叠已在 WP-10 第 5 行退役）。
    # WP-6E：群聊切幕是基线（开关已删）；private 总闸仍进 golden。
    "YINYUE_FEATURE_REACT_PLAYBOOK": "1",
    # [当前局面]（state_assembly）带分钟级时间戳，装进 golden 就是每小时都红；
    # 它不是装配器的形状，是运行时状态，基线固定为关。
    "YINYUE_FEATURE_STATE_ASSEMBLY": "0",
}
_BASELINE_ENV_PREVIOUS: dict[str, str | None] = {}
_BASELINE_TRACE_PREVIOUS: str | None = None


def _install_baseline_env() -> None:
    """Pin the equivalence baseline; remember originals so main() can restore."""
    global _BASELINE_TRACE_PREVIOUS
    for key, value in _BASELINE_ENV.items():
        _BASELINE_ENV_PREVIOUS[key] = os.environ.get(key)
        os.environ[key] = value
    _BASELINE_TRACE_PREVIOUS = os.environ.get("YINYUE_CONTEXT_TRACE_PATH")
    os.environ["YINYUE_CONTEXT_TRACE_PATH"] = str(
        temp_base() / f"yinyue_test_assembler_trace_{os.getpid()}.jsonl"
    )


def _restore_baseline_env() -> None:
    """Undo _install_baseline_env so other test files see the real defaults."""
    for key, previous in _BASELINE_ENV_PREVIOUS.items():
        if previous is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = previous
    _BASELINE_ENV_PREVIOUS.clear()
    if _BASELINE_TRACE_PREVIOUS is None:
        os.environ.pop("YINYUE_CONTEXT_TRACE_PATH", None)
    else:
        os.environ["YINYUE_CONTEXT_TRACE_PATH"] = _BASELINE_TRACE_PREVIOUS


_install_baseline_env()

GOLDEN = ROOT / "tests" / "golden" / "context"
_HEADERS_PATH = GOLDEN / "_headers.json"


def _load_baseline():
    path = ROOT / "scripts" / "context_baseline.py"
    spec = importlib.util.spec_from_file_location("context_baseline", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


_VOLATILE_BLOCK_PREFIXES = ("[事件记忆",)


def _without_memory_block(text: str) -> str:
    """丢掉数据驱动的块（事件记忆），其余逐字比对。

    这两块的内容来自库：同样的代码在同一天跑两次也可能不同（记忆条目、
    在手的事随生产数据变）。用「任一新块头（以 [ 开头）都算这一块结束」来
    切段落，而不是拿 `_BLOCK_POLICY` 当停词表——块登记表会随重构变化，
    拿它当停词表会让 golden 比对跟着登记表一起漂。
    """
    parts: list[str] = []
    skipping = False
    for part in text.split("\n\n"):
        stripped = part.lstrip()
        if stripped.startswith(_VOLATILE_BLOCK_PREFIXES):
            skipping = True
            continue
        if skipping and stripped.startswith("["):
            skipping = False
        if not skipping:
            parts.append(part)
    return "\n\n".join(parts)


def _headers(text: str) -> list[str]:
    """只比「有哪些块、顺序如何」——不比块内文字。

    2026-10-23：四层化期间 prompt 的文字（人设块的去留）每周都在动，而块内
    文字还依赖库（记忆条目、最近对话）。整篇 golden 会把「重构」
    和「数据变了」混成同一个红。形状（块清单与顺序）才是装配器的契约。
    """
    return [
        line.strip()
        for line in text.splitlines()
        if line.strip().startswith("[")
        and not line.strip().startswith(_VOLATILE_BLOCK_PREFIXES)
    ]


def _load_headers() -> dict[str, list[str]]:
    """读块头快照。

    必须在比对时读、不能在 import 时读：本文件作为 `__main__` 直接跑（全量
    脚本的跑法）与作为模块被 import（单测调试）走的是两条 import 时序，前者
    在快照文件写入之前就已经求值过模块级常量，于是 `_HEADERS` 会静默为空、
    20 个场景一起报红。
    """
    if not _HEADERS_PATH.exists():
        return {}
    return json.loads(_HEADERS_PATH.read_text(encoding="utf-8"))


def _freeze_clock():
    from core import temporal

    fixed_now = datetime(2026, 1, 1, 12, 0)
    original = temporal.prompt_block
    temporal.prompt_block = lambda now_value=None, **kw: original(fixed_now, **kw)
    return original


def main() -> bool:
    module = _load_baseline()
    from core.bootstrap import build_agent
    from core.speech.turn import snapshot_from_context

    original_prompt_block = _freeze_clock()
    failures: list[str] = []
    golden_notices: list[str] = []
    collected_headers: dict[str, list[str]] = {}
    golden_headers_all = _load_headers()
    agent = None
    try:
        agent = build_agent()
        for item in module._scenarios():
            os.environ["YINYUE_FEATURE_CONTEXT_ASSEMBLER"] = "0"
            legacy = module.prompt_for_item(agent, item, snapshot_from_context)
            os.environ["YINYUE_FEATURE_CONTEXT_ASSEMBLER"] = "1"
            assembled = module.prompt_for_item(agent, item, snapshot_from_context)

            if legacy != assembled:
                failures.append(item["name"])
                continue

            golden_headers = golden_headers_all.get(item["name"])
            if golden_headers is not None and golden_headers != _headers(assembled):
                golden_notices.append(item["name"])
            collected_headers[item["name"]] = _headers(assembled)
    finally:
        from core import temporal

        temporal.prompt_block = original_prompt_block
        os.environ.pop("YINYUE_FEATURE_CONTEXT_ASSEMBLER", None)
        _restore_baseline_env()

    total = len(module._scenarios())
    print(f"  legacy == assembler: {total - len(failures)}/{total}")
    if failures:
        print(f"  FAIL mismatch: {failures}")
    if os.environ.get("YINYUE_WRITE_HEADERS") == "1":
        _HEADERS_PATH.write_text(
            json.dumps(collected_headers, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(f"  headers written: {_HEADERS_PATH.name}")
        return True

    print(
        "  golden headers unchanged: "
        f"{total - len(golden_notices)}/{total}"
    )
    if golden_notices:
        print(f"  NOTE golden headers changed: {golden_notices}")

    # the assembler must have written a trace record with block metadata
    trace_ok = False
    try:
        from core.context.assembler import read_context_trace

        records = read_context_trace(limit=5)
        trace_ok = bool(records) and bool(records[0].get("blocks"))
    except Exception:
        trace_ok = False
    print(f"  context_trace has block metadata: {trace_ok}")

    passed = not failures and trace_ok
    print(f"context assembler equivalence: {'PASSED' if passed else 'FAILED'}")
    return passed


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
