# -*- coding: utf-8 -*-
"""WP-06 §6.2 步骤3：``AgentContext.extra`` 键契约审计。

扫描现役路由与出站装配域里所有 ``X.extra[...]`` / ``X.extra.get("...")`` /
``extra[...]`` / ``source.get("...")`` 键名，以及 ``AgentContext(extra={...})``
字面量键，输出排序后的键集；再和 ``core.routing.extra_contract.EXTRA_CONTRACT``
比对：**出现未登记键 → 退出码 1**。

契约纪律（手册 §6.2 步骤3）：初次登记以两条路径现有键的并集为准；登记后只允许
删除，新增必须同 PR 说明谁读。

    python scripts/audit_extra_contract.py
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

TARGETS = (
    ROOT / "core" / "routing" / "extra_contract.py",
    #: 外部 AgentContext 构造入口（WP-08 步骤 4）：
    #: proactive/reminder 与 scheduler 的 extra 投影同样受契约约束。
    ROOT / "agents" / "proactive_trigger.py",
    ROOT / "scheduler" / "main.py",
    #: 2026-10-21 补：这两处也在写 ``AgentContext.extra``，此前没被审计到，
    #: 于是 ``job_readouts`` / ``senses_block`` 一直撞 strict 契约（异常被 fail-closed 吞掉，
    #: 表现为「块没进 prompt」而日志只有一条 extra_contract_violation）。
    ROOT / "core" / "context" / "job_readout.py",
    ROOT / "core" / "senses" / "snapshot.py",
    #: 共享出站层会写记忆一致性审计状态。
    ROOT / "core" / "speech" / "outbound.py",
)

#: 这些局部变量名承载的就是 ``context.extra`` 映射本身（历史代码的两种叫法）。
_EXTRA_NAMES = frozenset({"extra", "source"})
_CONTEXT_ONLY_FILES = frozenset({"core/speech/outbound.py"})


def _string(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def scan_source(source: str, *, context_only: bool = False) -> set[str]:
    """一个文件里出现的 extra 键名（写 + 读）。"""
    tree = ast.parse(source)
    keys: set[str] = set()
    for node in ast.walk(tree):
        # extra["key"] = ... / context.extra["key"] = ...
        if isinstance(node, ast.Subscript):
            receiver = node.value
            if isinstance(receiver, ast.Attribute) and receiver.attr == "extra":
                if context_only and not (
                    isinstance(receiver.value, ast.Name)
                    and receiver.value.id == "context"
                ):
                    continue
                key = _string(node.slice)
                if key:
                    keys.add(key)
            elif (
                not context_only
                and isinstance(receiver, ast.Name)
                and receiver.id in _EXTRA_NAMES
            ):
                key = _string(node.slice)
                if key:
                    keys.add(key)
        # context.extra.get("key") / extra.get("key") / source.get("key")
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and node.args
        ):
            receiver = node.func.value
            is_extra = (
                isinstance(receiver, ast.Attribute)
                and receiver.attr == "extra"
                and (
                    not context_only
                    or (
                        isinstance(receiver.value, ast.Name)
                        and receiver.value.id == "context"
                    )
                )
            ) or (
                not context_only
                and isinstance(receiver, ast.Name)
                and receiver.id in _EXTRA_NAMES
            )
            if is_extra:
                key = _string(node.args[0])
                if key:
                    keys.add(key)
        # AgentContext(..., extra={"key": ...})
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "AgentContext"
        ):
            for keyword in node.keywords:
                if keyword.arg != "extra" or not isinstance(keyword.value, ast.Dict):
                    continue
                for item in keyword.value.keys:
                    key = _string(item)
                    if key:
                        keys.add(key)
    return keys


def scan_file(path) -> set[str]:
    relative = Path(path).resolve().relative_to(ROOT).as_posix()
    return scan_source(
        Path(path).read_text(encoding="utf-8-sig"),
        context_only=relative in _CONTEXT_ONLY_FILES,
    )


def scan_files(paths=TARGETS) -> set[str]:
    keys: set[str] = set()
    for path in paths:
        keys |= scan_file(path)
    return keys


def audit(paths=TARGETS):
    """返回 ``(sorted_written, unregistered, contract)``。"""
    from core.routing.extra_contract import EXTRA_CONTRACT

    contract = set(EXTRA_CONTRACT)
    written = scan_files(paths)
    return sorted(written), sorted(written - contract), contract


def main() -> int:
    written, unregistered, contract = audit()
    print("audit_extra_contract: scanned %d files" % len(TARGETS))
    print("written keys (%d):" % len(written))
    for key in written:
        print("    %s" % key)
    print("contract keys: %d" % len(contract))
    if unregistered:
        print()
        for key in unregistered:
            print("UNREGISTERED extra key: %s" % key)
        print()
        print("FAILED: %d unregistered key(s); 登记后只允许删除" % len(unregistered))
        return 1
    print()
    print("PASSED: 所有 extra 键都已登记")
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    raise SystemExit(main())
