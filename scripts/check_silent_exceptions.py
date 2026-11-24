"""静默失败禁令：扫出「把错误吞掉、进程看着健康」的代码。

2026-10-24 立。一天里四个 bug 全是这个病根（参数写反 / reasoning_content 缺键 /
单实例判重失效 / 冷却窗口吞消息）。规矩只有两条：

1. 每个 except 体内必须至少有一行「留下痕迹」的调用（名字含 log / print / warn），
   或者显式 raise。
2. 每个裸露的 pass 必须在本行带注释，说明为什么可以忽略。

用法：
    python scripts/check_silent_exceptions.py loop            # 只查 loop/
    python scripts/check_silent_exceptions.py loop core agents  # 查多个目录
退出码：0 = 干净；1 = 有违规。
"""
from __future__ import annotations

import ast
import os
import sys

# 允许自指的模块（它自己就是「留痕」的最后一道）
ALLOW_FILES = {"loop/safelog.py"}
NOQA = "noqa: silent"


def _is_trace_call(node) -> bool:
    if not (isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)):
        return False
    name = ast.unparse(node.value.func)
    return any(k in name.lower() for k in ("log", "print", "warn"))


def check_file(path: str) -> list[str]:
    src = open(path, encoding="utf-8-sig").read()
    if NOQA in src.split("\n")[0] or path.replace("\\", "/") in ALLOW_FILES:
        return []
    try:
        tree = ast.parse(src)
    except SyntaxError as exc:
        return ["%s:%s 语法错误：%s" % (path, exc.lineno, exc.msg)]
    lines = src.split("\n")
    bad = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ExceptHandler):
            if any(isinstance(b, ast.Raise) for b in node.body):
                continue
            if any(_is_trace_call(b) for b in node.body):
                continue
            # 2026-10-24：允许在 handler 行或它的 body 里写 `# noqa: silent + 理由`。
            # 有些 except 是"试探/常态"（平台名探测、-wal 不存在），留警告纯属噪音，
            # 但也必须写出为什么忽略——不许无声无息地吞。
            _span = lines[node.lineno - 1 : (node.body[-1].end_lineno or node.lineno)]
            if any(NOQA in ln for ln in _span):
                continue
            bad.append("%s:%d 这个 except 把异常吞掉了，一行痕迹都没有" % (path, node.lineno))
        elif isinstance(node, ast.Pass):
            line = lines[node.lineno - 1] if node.lineno - 1 < len(lines) else ""
            if "#" not in line:
                bad.append("%s:%d 裸露的 pass，要么留注释说明、要么留一行痕迹" % (path, node.lineno))
    return bad


def main(argv: list[str]) -> int:
    roots = argv[1:] or ["loop"]
    problems: list[str] = []
    for root in roots:
        if os.path.isfile(root):
            problems += check_file(root)
            continue
        for dirpath, _dirnames, filenames in os.walk(root):
            for fn in sorted(filenames):
                if fn.endswith(".py"):
                    problems += check_file(os.path.join(dirpath, fn))
    for p in problems:
        print(p)
    print("检查 %s：%d 处违规" % (", ".join(roots), len(problems)))
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
