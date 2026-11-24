"""环境不支持时必须显式记跳过的结构锁（DSH-019）。

三条规矩：

1. 跳过的标记只能在一处定义（``tests/_skip.py::MARK``，与 runner 的 ``SKIP_MARK`` 一致）；
2. 不得再新增裸 ``print("SKIP ...")``——只允许下面 ``LEGACY`` 7 个存量文件；
3. 跳过必须带非空原因。
"""

import importlib.util
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
TESTS = ROOT / "tests"
if str(TESTS) not in sys.path:
    sys.path.insert(0, str(TESTS))

import _skip  # noqa: E402  (tests/ 已在 sys.path 里)

#: 存量裸跳过。**只减不增**：新文件要跳过就用 ``from _skip import skip``。
LEGACY = {
    "test_events_span_m61.py": "sqlite 版本没有 DROP COLUMN（旧库不支持迁移）",
    "test_local_embed.py": "本地 embedding 模型没缓存（离线机器）",
    "test_media_integrity.py": "缺真实素材（dev 资产）",
    "test_media_registry.py": "缺真实素材（dev 资产）",
    "test_repo_resolution.py": "本机还有 legacy 库（旧布局下路径不一样）",
    "test_sticker_candidates.py": "隔离环境里没有真实 sticker registry",
    "test_wx_vision.py": "缺真实截图（本机没开微信）",
}

#: 准成穗规则：``print(`` 里出现 ``SKIP``/``skip`` 这个词。
_SKIP_PRINT = re.compile(r"\b(?:SKIP|skip)\b")


def _scan_skip_prints():
    """返回 {文件名: [行号, ...]}，列出所有仍在用裸 print 记跳过的测试文件。"""
    hits = {}
    for path in sorted(TESTS.glob("*.py")):
        if path.name in {"_skip.py", Path(__file__).name}:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        lines = [
            i + 1
            for i, line in enumerate(text.split("\n"))
            if "print(" in line and _SKIP_PRINT.search(line)
        ]
        if lines:
            hits[path.name] = lines
    return hits


def _load_runner():
    """把 ``scripts/run_all_tests.py`` 当模块加载（它自己会把 scripts/ 塞进 sys.path）。"""
    scripts = ROOT / "scripts"
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    spec = importlib.util.spec_from_file_location("run_all_tests_for_lock", scripts / "run_all_tests.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_marker_lives_in_one_place():
    assert _skip.MARK == "\u23ed \u8df3\u8fc7"
    runner = (ROOT / "scripts" / "run_all_tests.py").read_text(encoding="utf-8", errors="replace")
    assert "SKIP_MARK" in runner, "runner 应以 SKIP_MARK 为唯一标记口径"
    assert "_count_skips(" in runner, "runner 应把跳过数出来并单独列状态"


def test_blank_reason_is_rejected():
    for bad in ("", "   ", None):
        try:
            _skip.skip(bad)
        except ValueError:
            continue
        raise AssertionError("跳过必须带原因：%r 没被拒" % (bad,))


def test_skip_prints_marker_and_returns_true(capsys):
    assert _skip.skip("缺素材") is True
    out = capsys.readouterr().out
    assert _skip.MARK in out and "缺素材" in out
    assert _skip.skipped()[-1] == "缺素材"


def test_runner_counts_both_styles():
    runner = _load_runner()
    sample = (
        "\u23ed \u8df3\u8fc7：缺 WSL\n"
        "⏭ 跳过：缺截图\n"
        "  SKIP (旧库不支持 DROP COLUMN)\n"
        "    skip: local embed missing\n"
        "跑了 3 条，并不是跳过。\n"
    )
    assert runner._count_skips(sample) == (2, 2)


def test_no_unsanctioned_skip_prints():
    hits = _scan_skip_prints()
    extra = sorted(set(hits) - set(LEGACY))
    assert not extra, (
        "新增了裸 print 跳过，请改用 tests/_skip.py 的 skip()：%s" % ", ".join(extra)
    )
    assert sorted(hits) == sorted(LEGACY), "存量名单与实测不一致：%s" % sorted(hits)


def test_legacy_allowlist_is_frozen():
    assert len(LEGACY) == 7, "这份名单只减不增；减了记得同步数字与踩坑档案 #22"
    for name, why in LEGACY.items():
        assert name.endswith(".py") and (TESTS / name).exists(), "名单里的文件不存在：%s" % name
        assert why.strip(), "%s 缺原因" % name
