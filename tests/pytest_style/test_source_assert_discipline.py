"""源码断言纪律：可以守装配，但不能只有存在性（HO-013）。

背景（`docs/踩坑档案.md` #2x）：`tests/test_tool_leak_guard.py` 曾经这样守「回复回合装上只读工具视图」:

    assert "_TOOL_LEAK_RE" in (ROOT / "agents/base.py").read_text()
    assert inspect.getsource(ConversationalAgent).count("PlanExecToolView(self.tools)") >= 1

它问的是「源码里有没有这行字」。换个装法（比如工厂函数里装）、换个 agent 类，
断言照样绿；而 `agents.conversational` 一退役，它还把整份测试拖成
`ModuleNotFoundError` —— 存在性断言既没守住行为，又制造了假红。

所以本仓的规矩不是「禁止源码断言」（装配本身常常没有可观察行为，只能看源码），
而是：**同一份测试文件里，行为断言（真的调用函数、比较返回值）不得少于源码断言**。
只有存在性的文件一律红；真需要只看源码的（变异测试这种），进 `MUTATION_HARNESS` 并写理由。

改红的方法：先写一条真调用（拿返回值比较），再留那句源码断言；或者把那句源码断言删掉。
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parents[1]
ROOT = TESTS_DIR.parent

# 变异测试：把锚点从生产源码里删掉，跑目标用例，靠 subprocess 退出码判红/绿。
# 它的「源码断言」是变异的前置条件（锚点必须在），判红靠的是行为，不是这行字。
MUTATION_HARNESS = {
    "tests/test_two_path_wiring.py": "变异测试：删 DSH 锚点后跑目标用例，靠 subprocess 退出码判红/绿",
}

_SRC_ASSIGN = re.compile(r"^\s*(\w+)\s*=.*\b(?:read_text|getsource)\(", re.M)
LEDGER = TESTS_DIR / "source_assert_ledger.txt"

_SRC_READER = re.compile(r"\bopen\(|read_text\(|getsource\(")
_DEF = re.compile(r"^def (\w+)\([^)]*\)[^:]*:\n((?:[ \t].*\n|\n)+)", re.M)


def source_vars(text: str) -> set[str]:
    """这个测试文件里，哪些变量装的是「源码文本」。

    两种来源都算：直接 `read_text()/getsource()`，以及调本地 helper（比如 `_src()`，
    它内部 `open(...).read()`）——只认前一种，等于给「把读源码包一层 helper」开口子。
    """
    names = set(_SRC_ASSIGN.findall(text))
    for m in _DEF.finditer(text):
        helper, body = m.group(1), m.group(2)
        if _SRC_READER.search(body):
            names |= set(re.findall(r"^\s*(\w+)\s*=\s*%s\(" % re.escape(helper), text, re.M))
    return names


def source_asserts(text: str) -> int:
    """数「断言源码里有没有某段字」的行：断言里提到装源码的那些变量。

    `src.index("a") < src.index("b")` 也算——它问的还是源码长什么样，不是代码做了什么。
    """
    names = source_vars(text)
    if not names:
        return 0
    pat = re.compile(r"\b(?:%s)\b" % "|".join(map(re.escape, sorted(names))))
    return sum(
        1
        for line in text.splitlines()
        if line.strip().startswith("assert ") and pat.search(line)
    )


_METHOD_CALL = re.compile(r"\b(\w+)\.(\w+)\(")
_FUNC_CALL = re.compile(r"(?<![\w.])(\w+)\(")


def behavior_asserts(text: str) -> int:
    """数「真的调了东西」的断言。

    对源码文本的查询（`src.index(...)` / `src.count(...)`）不算行为——那只是换了个
    写法看源码；否则「第 40 行在第 80 行前面」会被当成一条行为断言，把闸门骗过去。
    """
    names = source_vars(text)
    n = 0
    for line in text.splitlines():
        s = line.strip()
        if not s.startswith("assert "):
            continue
        methods = _METHOD_CALL.findall(s)
        calls_on_other = [m for obj, m in methods if obj not in names]
        rest = _METHOD_CALL.sub("OBJ(", s)
        funcs = [f for f in _FUNC_CALL.findall(rest) if f != "OBJ"]
        if calls_on_other or funcs:
            n += 1
    return n


def _test_files() -> list[Path]:
    files = sorted(TESTS_DIR.glob("test_*.py")) + sorted(TESTS_DIR.glob("pytest_style/test_*.py"))
    return files


def test_counters_are_not_vacuous() -> None:
    sample = (
        "src = (ROOT / 'a.py').read_text(encoding='utf-8')\n"
        "assert 'foo' in src\n"
        "assert src.count('bar') >= 1\n"
        "assert f(1) == 2\n"
        "assert g() is None\n"
    )
    assert source_asserts(sample) == 2
    # `src.count('bar')` 是对源码的查询，不是行为；只有 f()/g() 算
    assert behavior_asserts(sample) == 2


def _ledger() -> dict[str, tuple[int, int]]:
    """读存量台账：每行 `路径 src上限 beh下限`。"""
    out: dict[str, tuple[int, int]] = {}
    for line in LEDGER.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        rel, src_cap, beh_floor = line.split()
        out[rel] = (int(src_cap), int(beh_floor))
    return out


def test_no_file_is_source_assert_only() -> None:
    """新文件不许「只说源码长什么样」；存量台账只准朝好的方向改。"""
    ledger = _ledger()
    offenders, stale = [], []
    for path in _test_files():
        text = path.read_text(encoding="utf-8", errors="replace")
        src_n = source_asserts(text)
        rel = path.relative_to(ROOT).as_posix()
        if not src_n or rel in MUTATION_HARNESS:
            continue
        beh_n = behavior_asserts(text)
        cap = ledger.get(rel)
        if cap is None:
            if beh_n < src_n:
                offenders.append(
                    f"{rel}: 源码断言 {src_n} 条，行为断言只有 {beh_n} 条（新文件不许这样）")
            continue
        if src_n > cap[0] or beh_n < cap[1]:
            offenders.append(
                f"{rel}: {src_n} 源码 / {beh_n} 行为，超过存量 {cap[0]}/{cap[1]}")
        elif src_n < cap[0] or beh_n > cap[1]:
            stale.append(f"{rel}: 台账 {cap[0]}/{cap[1]} → 实测 {src_n}/{beh_n}")
    assert not offenders, (
        "只有存在性、没有行为断言（先写一条真调用，或删掉源码断言）：\n" + "\n".join(offenders))
    assert not stale, (
        "台账过期了（改好了就顺手把这行改成实测值，别让台账追上代码）：\n" + "\n".join(stale))


def test_ledger_entries_are_real_tests() -> None:
    for rel in _ledger():
        assert rel.startswith("tests/"), rel
        assert (ROOT / rel).exists(), rel


def test_mutation_harness_really_runs_behaviour() -> None:
    """豁免必须自证：变异测试得真的起子进程跑用例，否则就是找借口。"""
    for rel, _reason in MUTATION_HARNESS.items():
        text = (ROOT / rel).read_text(encoding="utf-8", errors="replace")
        assert "subprocess." in text, rel


def test_mutation_harness_entries_exist() -> None:
    for rel in MUTATION_HARNESS:
        assert (ROOT / rel).exists(), rel


def test_scan_covers_the_repo() -> None:
    """防「扫了个寂寞」：含源码断言的文件数量级不能塌。"""
    with_src = [
        p for p in _test_files()
        if source_asserts(p.read_text(encoding="utf-8", errors="replace"))
    ]
    assert len(with_src) >= 30, f"只扫到 {len(with_src)} 个含源码断言的测试文件"


def test_tool_leak_guard_is_upgraded() -> None:
    """HO-013 的样板：这份测试不许再靠读源码判装配（注释里提到名字不算，看真调用）。"""
    text = (TESTS_DIR / "test_tool_leak_guard.py").read_text(encoding="utf-8", errors="replace")
    calls = []
    for node in ast.walk(ast.parse(text)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr in {"read_text", "getsource"}:
            calls.append(func.attr)
        elif isinstance(func, ast.Name) and func.id == "getsource":
            calls.append("getsource")
    assert not calls, f"装配要用实例行为证明，不要读源码：{calls}"
