#!/usr/bin/env python
"""审计「静默吞异常」并按影响面分级，产出一份可逐条确认的清单。

背景：项目里 `except Exception` 有 1200+ 处，其中约三成后面只跟
pass / return 空值 / continue —— 故障时调用方无法区分「确实没有」与「挂了」。
一次性全改不现实（不是所有 except 都该抛），所以先分级，再按级别处理。

分级依据（AST 分析，不靠正则猜）：
  A 必须修   吞掉后把「空/假值」返回给调用方，且调用方拿它继续决策。
             最典型：记忆召回挂了却返回 []，助手就"没有记忆"地作答。
  B 应记日志 吞掉但不外泄假值（循环里跳过单条、局部兜底），至少该留一条日志。
  C 可接受   清理/收尾/可选增强/工具脚本，吞掉是合理设计。

用法：
    python scripts/audit_silent_except.py              # 打印分级汇总
    python scripts/audit_silent_except.py --write      # 写 docs/静默异常分级清单.md
    python scripts/audit_silent_except.py --level A    # 只看 A 类的明细
"""
from __future__ import annotations

import argparse
import ast
import os
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

SKIP_DIRS = {
    ".git", ".venv", ".venv_it", "__pycache__", ".pycache_prefix", ".ruff_cache",
    "backups", "data", "logs", "output", "napcat", "node_modules", ".idea",
    ".dsh-meow", "_scratch", "migrations",
}

# 处于清理/收尾语义的函数名 —— 这里的吞异常通常是刻意的
CLEANUP_HINTS = (
    "close", "cleanup", "clean_up", "shutdown", "teardown", "stop", "flush",
    "__del__", "atexit", "dispose", "release", "unlink", "rmtree", "kill",
)

# 这些模块的吞异常直接影响用户可见行为，优先处理
CRITICAL_PREFIXES = (
    "core/terminal/", "core/routing/", "core/memory/", "core/context/",
    "core/conversation/", "core/ingestion/", "agents/", "web/",
)

EMPTY_RETURNS = {"None", "[]", "{}", '""', "''", "()", "set()", "False", "0"}


def _enclosing(tree: ast.AST) -> dict[int, str]:
    """{行号: 最近的函数限定名}"""
    out: dict[int, str] = {}

    def walk(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            name = prefix
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                name = f"{prefix}.{child.name}" if prefix else child.name
            elif isinstance(child, ast.ClassDef):
                name = f"{prefix}.{child.name}" if prefix else child.name
            if hasattr(child, "lineno"):
                out[child.lineno] = name
            walk(child, name)

    walk(tree, "")
    return out


def _returns_empty(body: list[ast.stmt]) -> bool:
    """handler 体里是否 return 了空/假值（或 return 无值）。"""
    for stmt in body:
        if isinstance(stmt, ast.Return):
            if stmt.value is None:
                return True
            if isinstance(stmt.value, ast.Constant) and stmt.value.value in (None, "", 0, False, (), []):
                return True
            if isinstance(stmt.value, (ast.List, ast.Dict, ast.Tuple, ast.Set)) and not getattr(stmt.value, "elts", getattr(stmt.value, "keys", [])):
                return True
            if isinstance(stmt.value, ast.Call):
                fn = ast.unparse(stmt.value.func) if hasattr(ast, "unparse") else ""
                if fn in {"set", "list", "dict", "tuple"}:
                    return True
        # 递归看 if/else 里的 return
        if isinstance(stmt, ast.If):
            if _returns_empty(stmt.body) or _returns_empty(stmt.orelse):
                return True
    return False


def _body_kind(body: list[ast.stmt]) -> str:
    """描述 handler 体做了什么。关键是 return 的**具体值** ——
    返回 None 与返回 [] 对调用方含义不同，是分级判断的依据。"""
    kinds = []
    for stmt in body:
        if isinstance(stmt, ast.Pass):
            kinds.append("pass")
        elif isinstance(stmt, ast.Continue):
            kinds.append("continue")
        elif isinstance(stmt, ast.Break):
            kinds.append("break")
        elif isinstance(stmt, ast.Return):
            if stmt.value is None:
                kinds.append("return（无值 -> None）")
            else:
                try:
                    expr = ast.unparse(stmt.value)
                except Exception:  # noqa: BLE001
                    expr = "?"
                if len(expr) > 40:
                    expr = expr[:37] + "..."
                kinds.append(f"return {expr}")
        elif isinstance(stmt, ast.Raise):
            kinds.append("raise")
        else:
            kinds.append("其它语句")
    return " / ".join(kinds) or "空"


def classify(path: str, func: str, body: list[ast.stmt]) -> tuple[str, str]:
    """返回 (级别, 理由)。"""
    low = func.lower()
    if any(h in low for h in CLEANUP_HINTS):
        return "C", "清理/收尾语义函数，吞掉通常是刻意设计"
    if path.startswith("scripts/"):
        return "C", "工具脚本，不影响常驻服务"
    if path.startswith("tests/"):
        return "C", "测试代码"
    if _returns_empty(body):
        if any(path.startswith(p) for p in CRITICAL_PREFIXES):
            return "A", "吞掉后把空/假值返回给调用方，调用方无法区分「没有」与「挂了」"
        return "B", "吞掉后返回空/假值（非关键路径）"
    if any(isinstance(s, ast.Continue) for s in body):
        return "B", "循环内跳过单条，建议补 debug 日志"
    return "B", "静默 pass，建议补 debug/warning 日志"


def collect() -> list[dict]:
    rows: list[dict] = []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in filenames:
            if not fn.endswith(".py"):
                continue
            p = Path(dirpath) / fn
            rel = p.relative_to(ROOT).as_posix()
            try:
                tree = ast.parse(p.read_text(encoding="utf-8", errors="replace"))
            except SyntaxError:
                continue
            enclosing = _enclosing(tree)
            for node in ast.walk(tree):
                if not isinstance(node, ast.Try):
                    continue
                for handler in node.handlers:
                    if handler.type is None:
                        kind = "bare except"
                    else:
                        tname = ast.unparse(handler.type) if hasattr(ast, "unparse") else ""
                        if "Exception" not in tname and "BaseException" not in tname:
                            continue
                        kind = tname
                    body = handler.body
                    # 只看「静默」的：全部语句都是 pass/return空/continue/break/日志之外的
                    silent = all(
                        isinstance(s, (ast.Pass, ast.Continue, ast.Break, ast.Return))
                        for s in body
                    )
                    if not silent:
                        continue
                    func = enclosing.get(node.lineno, "<module>")
                    level, why = classify(rel, func, body)
                    rows.append({
                        "file": rel,
                        "line": handler.lineno,
                        "func": func,
                        "kind": kind,
                        "body": _body_kind(body),
                        "level": level,
                        "why": why,
                    })
    return rows


def summarize(rows: list[dict]) -> None:
    by_level = defaultdict(list)
    for r in rows:
        by_level[r["level"]].append(r)
    print(f"静默吞异常总计: {len(rows)}\n")
    for lvl in ("A", "B", "C"):
        print(f"  {lvl}: {len(by_level[lvl])} 处")

    print("\n=== A 类热点文件（优先处理）===")
    cnt: dict[str, int] = defaultdict(int)
    for r in by_level["A"]:
        cnt[r["file"]] += 1
    for f, n in sorted(cnt.items(), key=lambda x: -x[1])[:15]:
        print(f"  {n:>3}  {f}")


def write_doc(rows: list[dict]) -> Path:
    by_level = defaultdict(list)
    for r in rows:
        by_level[r["level"]].append(r)
    cnt: dict[str, int] = defaultdict(int)
    for r in by_level["A"]:
        cnt[r["file"]] += 1

    L: list[str] = []
    L.append("# 静默异常分级清单")
    L.append("")
    L.append("> 自动生成，勿手改。重新生成：`python scripts/audit_silent_except.py --write`")
    L.append("")
    L.append("## 这是什么")
    L.append("")
    L.append("项目里 `except Exception` 超过 1200 处，其中一部分后面只跟 "
             "`pass` / 返回空值 / `continue`。这类写法让**调用方无法区分"
             "「确实没有」与「内部挂了」**，是最常见的隐性故障源。")
    L.append("")
    L.append("一次性全改不现实——**不是所有 except 都该抛**。所以先分级再处理。")
    L.append("")
    L.append("## 分级口径")
    L.append("")
    L.append("| 级别 | 含义 | 处理建议 |")
    L.append("|---|---|---|")
    L.append("| **A** | 吞掉后把**空/假值返回给调用方**，且处于核心链路 | 必须修：至少要能区分「没有」与「挂了」（记日志 + 让调用方知情） |")
    L.append("| **B** | 吞掉但不外泄假值（循环跳过单条、局部兜底） | 补一条 debug/warning 日志即可 |")
    L.append("| **C** | 清理/收尾/可选增强/工具脚本 | 保持现状，属合理设计 |")
    L.append("")
    L.append("## 怎么读 A 类 —— 它里面有两种性质，别一概而论")
    L.append("")
    L.append("**① fail-closed（`return False`）**：集中在权限与准入门，例如")
    L.append("`BaseTool.authoritative_is_owner` / `authoritative_has_capability`、")
    L.append("`core/context/admission.py::enabled_for`。判定失败即「不放行」，")
    L.append("从安全角度这通常是**刻意**的，不要为了「报错更清楚」改成抛异常。")
    L.append("但仍应补一条日志 —— 否则主人权限被误判为非主人时，现场没有任何线索。")
    L.append("")
    L.append("**② 静默降级（`return []` / `None` / `\"\"`）**：集中在数据读取与上下文装配，")
    L.append("例如 `core/context/chat_window.py::load_messages`、")
    L.append("`core/context/assembler.py::read_context_trace`、记忆召回链路。")
    L.append("**这一类最危险**：调用方拿到的空值与「确实为空」完全无法区分，")
    L.append("故障表现为「助手忽然不记得了 / 群里的上下文丢了」这类玄学现象。")
    L.append("建议**优先修这一类**。")
    L.append("")
    L.append("## 汇总")
    L.append("")
    L.append("| 级别 | 数量 |")
    L.append("|---|---|")
    for lvl in ("A", "B", "C"):
        L.append(f"| {lvl} | {len(by_level[lvl])} |")
    L.append(f"| **合计** | **{len(rows)}** |")
    L.append("")

    L.append("## A 类热点文件")
    L.append("")
    L.append("| 处数 | 文件 |")
    L.append("|---|---|")
    for f, n in sorted(cnt.items(), key=lambda x: (-x[1], x[0]))[:25]:
        L.append(f"| {n} | `{f}` |")
    L.append("")

    L.append("## A 类全部明细")
    L.append("")
    L.append("按文件排序。`吞掉后` 列是该 handler 体里做了什么。")
    L.append("")
    L.append("| 文件:行 | 函数 | 吞掉后 |")
    L.append("|---|---|---|")
    for r in sorted(by_level["A"], key=lambda x: (x["file"], x["line"])):
        L.append(f"| `{r['file']}:{r['line']}` | `{r['func']}` | {r['body']} |")
    L.append("")

    L.append("## B 类明细（补日志即可，量大，折叠看）")
    L.append("")
    L.append("<details><summary>展开 B 类 " + str(len(by_level["B"])) + " 处</summary>")
    L.append("")
    L.append("| 文件:行 | 函数 | 吞掉后 | 理由 |")
    L.append("|---|---|---|---|")
    for r in sorted(by_level["B"], key=lambda x: (x["file"], x["line"])):
        L.append(f"| `{r['file']}:{r['line']}` | `{r['func']}` | {r['body']} | {r['why']} |")
    L.append("")
    L.append("</details>")
    L.append("")

    target = ROOT / "docs" / "静默异常分级清单.md"
    target.write_text("\n".join(L), encoding="utf-8", newline="\n")
    return target


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true", help="写入 docs/静默异常分级清单.md")
    ap.add_argument("--level", choices=["A", "B", "C"], help="打印指定级别明细")
    args = ap.parse_args()

    rows = collect()
    if args.level:
        sel = [r for r in rows if r["level"] == args.level]
        for r in sorted(sel, key=lambda x: (x["file"], x["line"])):
            print(f"{r['file']}:{r['line']}  [{r['func']}]  {r['body']}  -- {r['why']}")
        print(f"\n{args.level} 类共 {len(sel)} 处")
        return 0

    summarize(rows)
    if args.write:
        p = write_doc(rows)
        print(f"\n[OK] 已写入 {p.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
