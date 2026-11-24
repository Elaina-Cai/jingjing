"""模块里用到的名字必须真的能被解析（2026-10-30 事故：`_PEND_TRIES` 从未定义，每拍 NameError）。

与 `test_brain_names_resolve.py` 的分工：
- 那个文件钉 brain.py 的三条具体形状（别名导入、遮蔽、赋值+调用）；
- 这个文件是**通用**的：不靠手写白名单，直接算出"这个模块里被读、但哪里都没定义"的名字。
  手写白名单抓不住我没预料到的新名字——事故里的 `_PEND_TRIES` 就是这样漏的。

判据（保守，只报"读不到"）：
1. 收集模块级名字（import 别名、def/class、顶层赋值）；
2. 收集每个函数作用域里能绑定的名字（参数、赋值、for/with/except/comprehension/lambda 参数、
   函数内 import、global/nonlocal 声明）；
3. 所有 `ast.Name(ctx=Load)` 减去上面两集合，再减去内建名与模块口子（`__name__` 等）；
4. 剩下的就是"读了但没有定义"→ 运行到那一行必崩。
"""
from __future__ import annotations

import ast
import builtins
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TARGETS = (ROOT / "loop" / "run.py", ROOT / "loop" / "brain.py")
DUNDERS = {"__name__", "__file__", "__doc__", "__package__", "__spec__", "__loader__",
           "__builtins__", "__debug__", "__annotations__"}


def _module_names(body: list[ast.stmt]) -> set[str]:
    names: set[str] = set()
    for node in body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for a in node.names:
                names.add(a.asname or a.name.split(".")[0])
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for t in targets:
                for sub in ast.walk(t):
                    if isinstance(sub, ast.Name):
                        names.add(sub.id)
    return names


def _bound_in(node: ast.AST) -> set[str]:
    """一个函数/lambda 作用域里所有能绑定的名字。"""
    bound: set[str] = set()
    args = getattr(node, "args", None)
    if args is not None:
        for group in (args.posonlyargs, args.args, args.kwonlyargs):
            bound.update(a.arg for a in group)
        if args.vararg:
            bound.add(args.vararg.arg)
        if args.kwarg:
            bound.add(args.kwarg.arg)
    for sub in ast.walk(node):
        if isinstance(sub, ast.arg):
            bound.add(sub.arg)
        elif isinstance(sub, ast.Name) and not isinstance(sub.ctx, ast.Load):
            bound.add(sub.id)
        elif isinstance(sub, (ast.Import, ast.ImportFrom)):
            for a in sub.names:
                bound.add(a.asname or a.name.split(".")[0])
        elif isinstance(sub, ast.ExceptHandler) and sub.name:
            bound.add(sub.name)
        elif isinstance(sub, (ast.Global, ast.Nonlocal)):
            bound.update(sub.names)
        elif isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            # 嵌套 def/class 的名字也是本作用域里的绑定（2026-10-30：漏了这条，
            # `_start_accept_loop` 与内层 `_loop` 被误报成"读了未定义"）。
            bound.add(sub.name)
    return bound


def _loads(node: ast.AST) -> set[str]:
    return {s.id for s in ast.walk(node) if isinstance(s, ast.Name) and isinstance(s.ctx, ast.Load)}


def unresolved(path: Path) -> list[tuple[int, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8").lstrip("\ufeff"))
    module = _module_names(tree.body)
    known = module | set(dir(builtins)) | DUNDERS
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            known |= _bound_in(node)
    bad = [(n.lineno, n.id) for n in ast.walk(tree)
           if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load) and n.id not in known]
    return sorted(bad)


def test_no_unresolved_names() -> None:
    problems = []
    for path in TARGETS:
        rel = path.relative_to(ROOT).as_posix()
        for lineno, name in unresolved(path):
            problems.append("%s:%d 读了未定义的 %s" % (rel, lineno, name))
    assert not problems, "有读了但从未定义的名字：%s" % problems[:8]


def test_the_checker_would_have_caught_pend_tries() -> None:
    """反证：故意造一段"用了没定义"的代码，checker 必须报。"""
    import tempfile
    src = "def f():\n    return _NOT_DEFINED_ANYWHERE[0]\n"
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "x.py"
        p.write_text(src, encoding="utf-8", newline="\n")
        got = unresolved(p)
    assert got == [(2, "_NOT_DEFINED_ANYWHERE")], got

def test_the_two_modules_actually_import() -> None:
    """行为锚：静态锁的文件也要有一条真跑得起来的断言（避免"只读源码不跑代码"）。"""
    import loop.brain as brain_mod
    import loop.run as run_mod

    assert callable(run_mod.one_tick)
    assert isinstance(run_mod._PEND_TRIES, list) and len(run_mod._PEND_TRIES) == 1
    assert callable(brain_mod.run_loop)
