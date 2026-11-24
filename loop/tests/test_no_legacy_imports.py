"""不变式 6：loop/ 只许经过 ports。

这条测试红的时候，不要加白名单——说明有人把旧决策层又接回来了。
"""
import ast
import os

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FORBIDDEN = ("core", "agents", "db", "llm", "scheduler", "web")
ALLOWED = {"ports.py"}


def _imported_roots(path):
    tree = ast.parse(open(path, encoding="utf-8").read())
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                roots.add(a.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level:            # 相对 import 是本包内
                continue
            if node.module:
                roots.add(node.module.split(".")[0])
    return roots


def test_no_legacy_imports():
    bad = []
    for dirpath, _, files in os.walk(HERE):
        for fn in files:
            if not fn.endswith(".py") or fn in ALLOWED:
                continue
            p = os.path.join(dirpath, fn)
            hit = _imported_roots(p) & set(FORBIDDEN)
            if hit:
                bad.append((os.path.relpath(p, HERE), sorted(hit)))
    assert not bad, "loop/ 里出现了旧代码 import：%r" % (bad,)
