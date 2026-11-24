"""闸：工具目录只许一处（M3 的前置）。

规则 A：`ToolSpec(` 的构造只许出现在 `core/tools/` 下。
规则 B：`class ToolSpec` 只许定义一次（`core/tools/registry.py`）。
规则 C：「模型看得见什么」的名单（`PLAN_LOOP_ALLOWED` / `PLAN_LOOP_READ_ONLY_TOOLS`）
  只许在 `core/tools/catalog.py` 定义，别处只许 import。

真机教训：仓里有两个工具目录（模型侧 `agents/tools/base.py` 18 个只读、
执行器侧 `core/tools/builtin.py` 10 个含 send_plan），工具造在后者、模型
永远看不见——她因此答「我没有往群里发消息的工具」。
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCAN_DIRS = ("core", "agents", "web", "db", "llm", "notify", "scheduler")
ALLOWED_SPEC_DIR = "core/tools/"
SPEC_CLASS_OWNER = "core/tools/registry.py"
CATALOG_OWNER = "core/tools/catalog.py"
LIST_NAMES = ("PLAN_LOOP_ALLOWED", "PLAN_LOOP_READ_ONLY_TOOLS")
# 声明的例外：只造 spec 交给同一个 registry，不自己建目录
ALLOWED_EXTRA = {
    "core/execution/mcp.py": "MCP 工具声明：spec 交给同一个 registry，不另建目录",
}
# 本检查器脚本本身不在扫描范围（SCAN_DIRS 不含 scripts），且用拼接避免自匹配
_NEEDLE_SPEC = "Tool" + "Spec("
_NEEDLE_CLASS = "class " + "ToolSpec"


def scan(root: Path = ROOT) -> list[str]:
    problems: list[str] = []
    for rel_dir in SCAN_DIRS:
        d = root / rel_dir
        if not d.exists():
            continue
        for path in sorted(d.rglob("*.py")):
            rel = path.relative_to(root).as_posix()
            src = path.read_text(encoding="utf-8-sig", errors="ignore")
            if _NEEDLE_SPEC in src and not rel.startswith(ALLOWED_SPEC_DIR) and rel not in ALLOWED_EXTRA:
                problems.append(f"A {rel}: 在 core/tools/ 之外构造 {_NEEDLE_SPEC}")
            if _NEEDLE_CLASS in src and rel != SPEC_CLASS_OWNER:
                problems.append(f"B {rel}: 第二份 class ToolSpec（唯一属主 {SPEC_CLASS_OWNER}）")
            if rel != CATALOG_OWNER:
                for name in LIST_NAMES:
                    for line in src.splitlines():
                        text = line.strip()
                        if not text.startswith(name):
                            continue
                        if "=" not in text:
                            continue
                        rhs = text.split("=", 1)[1].strip()
                        # 允许「别名 re-export」：只许指向目录属主自己的常量
                        if "_CATALOG." in rhs or "catalog." in rhs or "import" in rhs:
                            continue
                        problems.append(
                            f"C {rel}: 自己定义工具名单 {name}（唯一属主 {CATALOG_OWNER}）")
    return problems


def main() -> int:
    problems = scan()
    if problems:
        print(f"FAILED: {len(problems)} 个问题")
        for item in problems:
            print(" -", item)
        return 1
    print("PASSED: 工具目录唯一（ToolSpec 只在 core/tools/）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
