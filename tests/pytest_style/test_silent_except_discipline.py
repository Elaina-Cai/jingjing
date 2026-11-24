"""静默 except 的结构锁（PIT-071）。

判据只有一条：「这次不做」必须说清是哪一类（`core.infra.skips.KINDS`）、在哪、看得见。
不禁止 `except`，禁止的是吞掉之后系统状态变成「看起来没事」。

存量不逐个改（346 处，穷举式改法本身就是坑）——存量记在
`tests/silent_except_ledger.txt` 里，只准减：改好一处就在这里删一处，数字涨了就红。
判定路径（读配置、认人、找素材）上的几处必须已经归零，它们是「坏方向是空/无限制」的那类。
"""
import ast
import importlib.util
import os
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LEDGER = ROOT / "tests" / "silent_except_ledger.txt"
SCAN_DIRS = ("core", "loop", "agents", "scheduler", "console", "db", "scripts")

# 判定路径：这些函数里的静默异常方向是「没有限制 / 没有数据 / 认不出人」
JUDGEMENT = {
    "core/runtime_config.py::_pick": "读配置：吞掉 → 静默回默认，用户以为改了没生效",
    "core/execution/chat_alias.py::_load": "认人：吞掉 → 别名表空，群名匹配悄悄失效",
    "core/execution/wx_chat.py::verify_active_chat_title": "认人：吞掉 → 这次没学会，下次还得重学",
    "core/conversation/media_ref.py::_from_account_db": "找素材：吞掉 → 当她没发过图",
}


def _silent_body(nodes) -> bool:
    if not nodes:
        return False
    for n in nodes:
        if isinstance(n, (ast.Pass, ast.Continue)):
            continue
        if isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant):
            continue
        return False
    return True


def scan() -> Counter:
    """(相对路径::函数) -> 静默 except 数。"""
    hits: Counter = Counter()
    for d in SCAN_DIRS:
        for dirpath, _dirs, files in os.walk(ROOT / d):
            # 2026-11-02：scripts/_scratch 是 .gitignore 的堆放区（非项目代码），
            # 与 scripts/audit_silent_except.py 的 SKIP_DIRS 保持同一口径。
            if "_scratch" in dirpath or "__pycache__" in dirpath:
                continue
            for fn in files:
                if not fn.endswith(".py"):
                    continue
                fp = Path(dirpath) / fn
                rel = fp.relative_to(ROOT).as_posix()
                try:
                    tree = ast.parse(fp.read_text(encoding="utf-8"))
                except SyntaxError:
                    continue
                for node in ast.walk(tree):
                    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        continue
                    for sub in ast.walk(node):
                        if isinstance(sub, ast.ExceptHandler) and _silent_body(sub.body):
                            hits[f"{rel}::{node.name}"] += 1
    return hits


def ledger() -> dict:
    out = {}
    for line in LEDGER.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, _, num = line.partition("\t")
        out[key.strip()] = int(num)
    return out


def skips_module():
    spec = importlib.util.spec_from_file_location("_skips_probe", ROOT / "core" / "infra" / "skips.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_scan_is_not_vacuous():
    hits = scan()
    assert sum(hits.values()) >= 300, f"只扫到 {sum(hits.values())} 处静默 except，扫描规则大概坏了"
    assert len(hits) >= 250, f"只扫到 {len(hits)} 个键，扫描规则大概坏了"


def test_ledger_matches_reality():
    actual, recorded = scan(), ledger()
    new = {k: n for k, n in actual.items() if k not in recorded}
    grew = {k: (recorded[k], n) for k, n in actual.items() if k in recorded and n > recorded[k]}
    shrank = {k: (recorded[k], n) for k, n in actual.items() if k in recorded and n < recorded[k]}
    gone = {k: n for k, n in recorded.items() if k not in actual}
    assert not new, "\n".join(
        [f"新代码不许静默吞（{k} 有 {n} 处）：要么处理，要么 skips.skip(\"文件.函数:对象\", \"absent|unsupported|degraded|flutter|malformed\", 说明)" for k, n in sorted(new.items())])
    assert not grew, "\n".join([f"静默处变多了：{k} 台账 {a} → 实际 {b}" for k, (a, b) in sorted(grew.items())])
    assert not shrank, "\n".join(
        [f"改好了就把台账这条删掉（只准减）：{k} 台账 {a}，实际 {b}" for k, (a, b) in sorted(shrank.items())])
    assert not gone, "\n".join([f"台账里有、代码里没了：{k}（{n} 处），把这条删掉" for k, n in sorted(gone.items())])


def test_judgement_paths_are_silent_free():
    hits = scan()
    recorded = ledger()
    still = {k: hits.get(k, 0) for k in JUDGEMENT if hits.get(k) or k in recorded}
    assert not still, "\n".join([f"判定路径必须归零：{k} 还有 {n} 处（{JUDGEMENT[k]}）" for k, n in sorted(still.items())])
    for key in JUDGEMENT:
        rel, fn = key.split("::")
        src = (ROOT / rel).read_text(encoding="utf-8")
        body = src.split(f"def {fn}(", 1)[-1][:4000]
        assert "skips.skip(" in body, f"{key} 里没看到 skips.skip(，判定路径只说「跳过」还不够，得看得见"


def test_skip_kinds_are_the_only_vocabulary():
    mod = skips_module()
    assert set(mod.KINDS) == {"absent", "unsupported", "degraded", "flutter", "malformed"}, "类别表变了：先说明新类跟已有五类哪一类不同"
    assert all(isinstance(v, str) and v.strip() for v in mod.KINDS.values()), "每一类都要有一句人话解释"
    try:
        mod.skip("probe.test", "bogus")
    except ValueError as exc:
        assert "没登记的跳过类别" in str(exc)
    else:
        raise AssertionError("未知类别必须报错，否则自由发挥又会回来")
    try:
        mod.skip("", "absent")
    except ValueError as exc:
        assert "在哪跳" in str(exc)
    else:
        raise AssertionError("where 为空必须报错：不知道在哪跳，事后查不到")


def test_skip_is_visible():
    mod = skips_module()
    mod.reset()
    mod.skip("probe.test", "absent", "no such table")
    mod.skip("probe.test", "flutter", "locked")
    assert mod.counts() == {"absent": 1, "flutter": 1}, mod.counts()
    assert mod.total() == 2
    src = (ROOT / "core" / "infra" / "skips.py").read_text(encoding="utf-8")
    assert 'log.info("skipped_step"' in src, "跳过必须落一条事件，只改计数在进程退出后就没了"


def test_one_mark_one_place():
    mod = skips_module()
    assert mod.MARK == "\u23ed \u8df3\u8fc7"
    allow = {
        "core/infra/skips.py",                       # 定义处（唯一）
        "tests/_skip.py",                            # 独立脚本侧镜像，供没有 pytest 的文件用
        "scripts/run_all_tests.py",                  # 读者：把跳过行数进 RESULT
        "tests/pytest_style/test_skip_discipline.py",  # 断言两者同一个词
        "tests/pytest_style/test_silent_except_discipline.py",  # 本文件的自我断言
    }
    found = set()
    for d in SCAN_DIRS + ("tests",):
        for dirpath, _dirs, files in os.walk(ROOT / d):
            for fn in files:
                if not fn.endswith(".py"):
                    continue
                fp = Path(dirpath) / fn
                rel = fp.relative_to(ROOT).as_posix()
                if mod.MARK in fp.read_text(encoding="utf-8", errors="replace"):
                    found.add(rel)
    extra = found - allow
    assert not extra, "跳过的说法只能有一个（别再造第二套词汇）：" + ", ".join(sorted(extra))
