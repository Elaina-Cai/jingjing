# -*- coding: utf-8 -*-
"""WP-06 单装配点守卫（《当断则断_执行手册》§6.3）。

三条结构断言：

1. 装配函数全仓各**一个** call site，且都在 ``core/routing/turn_pipeline.py``：
   ``run_admission_result`` / ``build_turn_world`` /
   ``_prepare_reply_with_policy`` / ``_send_reply``。
2. 入口/判定壳都不许再直接装配：
   * ``_invoke_conversational_inner`` / ``_on_group_trigger`` 只允许调
     ``generate_turn``（触发壳额外调一次 ``decide_turn`` 绑定 owner 结论）；
   * ``_route_one`` 是 ``decide_turn`` 的兼容壳（构 facts → decide → TurnDecision），
     不许出现任何装配调用/import。
3. ``decide_turn`` 的**两个**调用点 = 两条入口各一处（2026-10-23 删掉 relevance
   阶段之后只剩 route 阶段）：直回 ``_route_one``、触发 ``_on_group_trigger``。
   ``generate_turn`` 两个调用点 = 两个入口壳。
   这与变异锁（tests/test_two_path_wiring.py：删任一路径 route 决策调用必须变红）
   是同一条约束的静态面。

Run directly: python tests/test_turn_pipeline_single_assembly.py
"""
from __future__ import annotations

import ast
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

ROUTER = ROOT / "core" / "routing" / "router.py"
TURN_PIPELINE = ROOT / "core" / "routing" / "turn_pipeline.py"

#: 装配点 -> (callee 形态, 允许的唯一宿主文件, 唯一宿主函数)
SINGLE_ASSEMBLY = {
    "run_admission_result": ("name", "core/routing/turn_pipeline.py", "apply_admission_for"),
    "build_turn_world": ("name", "core/routing/turn_pipeline.py", "build_turn_world_for"),
    "_prepare_reply_with_policy": (
        "attr", "core/routing/turn_pipeline.py", "prepare_reply_with_policy_for",
    ),
    "_send_reply": ("attr", "core/routing/turn_pipeline.py", "send_reply_for"),
}

ENTRY_SHELLS = ("_invoke_conversational_inner", "_on_group_trigger")

#: 壳里一旦直接出现，就说明装配又漏回 router 了。
SHELL_FORBIDDEN_CALLS = frozenset({
    "run_admission_result", "attach_reply_intent", "build_turn_world",
    "_prepare_reply_with_policy", "_send_reply", "_attach_speech",
    "_attach_reply_intent", "_attach_content_plan", "_attach_private_intent",
    "_attach_focus", "_apply_admission", "build_agent",
})
SHELL_FORBIDDEN_IMPORTS = ("core.context", "core.bootstrap", "agents.")

PROD_PACKAGES = ("core", "agents", "web", "scheduler", "db", "llm", "notify", "tools")

CRITICAL_EXTRA_KEYS = frozenset({
    "account_id", "chat_id",
    "session_id", "world_state", "images",
})

#: WP-08 8.2 步骤 3 已退役的投影键：写点删除 + contract 裁剪后，这里锁它们不许回流。
RETIRED_EXTRA_KEYS = frozenset({
    "should_send_reply", "reply_intent", "reply_intent_line",
    "reply_intent_need_clarify", "content_plan", "_policy_decision",
    "turn_intent", "unknown_answer", "trigger_reason", "trigger_reasons",
    "defer_sticker_until_after_reply",
})


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8-sig"))


def _callee(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return ""


def _function_calls(path: Path) -> list[tuple[str, str, str, int]]:
    """[(文件 relpath, 宿主函数名, callee 名, 行号)]。

    模块级作用域记作 ``<module>``；callee 只取最后一跳（``obj._send_reply`` 与
    ``_send_reply`` 视为同一个装配名字，正是守卫要盯的口径）。
    """
    rel = str(path.relative_to(ROOT)).replace(os.sep, "/")
    out: list[tuple[str, str, str, int]] = []

    def walk(node: ast.AST, scope: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                walk(child, child.name)
            elif isinstance(child, ast.ClassDef):
                walk(child, scope)
            else:
                if isinstance(child, ast.Call):
                    out.append((rel, scope, _callee(child), child.lineno))
                walk(child, scope)

    walk(_tree(path), "<module>")
    return out


def _imports_inside(path: Path, func_name: str) -> list[str]:
    found: list[str] = []
    for node in ast.walk(_tree(path)):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name != func_name:
            continue
        for inner in ast.walk(node):
            if isinstance(inner, ast.Import):
                found.extend(alias.name for alias in inner.names)
            elif isinstance(inner, ast.ImportFrom) and inner.module:
                found.append(inner.module)
    return found


def test_module_surface() -> None:
    assert TURN_PIPELINE.is_file(), "core/routing/turn_pipeline.py 不存在"
    tree = _tree(TURN_PIPELINE)
    classes = {n.name: n for n in tree.body if isinstance(n, ast.ClassDef)}
    funcs = {
        n.name: n for n in tree.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert "TurnContext" in classes, "缺 TurnContext"
    assert "decide_turn" in funcs, "缺 decide_turn"
    assert "generate_turn" in funcs and isinstance(
        funcs["generate_turn"], ast.AsyncFunctionDef
    ), "generate_turn 必须是 async"
    contract = None
    for node in tree.body:
        if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", "") == "EXTRA_CONTRACT":
            contract = node
        # R2：登记表已搬到 core/routing/extra_contract.py，本模块只 re-export ——
        # 「定义在本模块」不再成立，但「对外仍是模块属性」仍成立（值校验见
        # test_extra_contract_is_registered，那条比这条硬）。
        if isinstance(node, ast.ImportFrom) and any(
            alias.name == "EXTRA_CONTRACT" for alias in node.names
        ):
            contract = node
    assert contract is not None, "缺 EXTRA_CONTRACT 登记"


def test_each_assembly_call_site_is_unique() -> None:
    calls = _function_calls(ROUTER) + _function_calls(TURN_PIPELINE)
    for callee, (kind, host_file, host_func) in SINGLE_ASSEMBLY.items():
        hits = [
            (rel, scope, line) for rel, scope, name, line in calls
            if name == callee
        ]
        assert len(hits) == 1, (
            f"{callee} 应当在路由装配域里恰好只有一个 call site，实测 {hits}"
        )
        rel, scope, line = hits[0]
        assert rel == host_file, (
            f"{callee} 的唯一 call site 必须在 {host_file}，实测 {rel}:{line}"
        )
        assert scope == host_func, (
            f"{callee} 的唯一 call site 必须在 {host_func} 里，实测 {scope}:{line}"
        )


def test_entry_shells_are_thin() -> None:
    calls = _function_calls(ROUTER)
    #: 壳名 -> 期望的 decide_turn / generate_turn 次数（task-4：直回决策在 _route_one）
    expected = {
        "_invoke_conversational_inner": {"decide_turn": 0, "generate_turn": 1},
        "_on_group_trigger": {"decide_turn": 1, "generate_turn": 1},
        "_route_one": {"decide_turn": 1, "generate_turn": 0},
    }
    for shell, counts in expected.items():
        shell_calls = [name for _, scope, name, _ in calls if scope == shell]
        for callee, want in counts.items():
            assert shell_calls.count(callee) == want, (
                f"{shell} 必须调 {want} 次 {callee}：{shell_calls}"
            )
        leaked = sorted(SHELL_FORBIDDEN_CALLS.intersection(shell_calls))
        assert not leaked, f"{shell} 仍是装配体，直接调了 {leaked}"
        imports = _imports_inside(ROUTER, shell)
        bad = [
            name for name in imports
            if any(name == p or name.startswith(p) for p in SHELL_FORBIDDEN_IMPORTS)
        ]
        assert not bad, f"{shell} 直接 import 了装配模块：{bad}"


def _production_files() -> list[Path]:
    out: list[Path] = []
    for package in PROD_PACKAGES:
        base = ROOT / package
        if not base.is_dir():
            continue
        for path in base.rglob("*.py"):
            if any(part in {"_scratch", "__pycache__"} for part in path.parts):
                continue
            out.append(path)
    return sorted(out)


def test_decide_and_generate_are_wired_once_per_path() -> None:
    #: 2026-10-23：relevance 阶段删除后，decide_turn 只剩两个入口各一处。
    expected = {
        "decide_turn": {
            "_route_one",                # direct: 硬事实 + engagement + TurnDecision
            "_on_group_trigger",         # trigger: 绑定 GroupState owner 结论
        },
        "generate_turn": {
            "_invoke_conversational_inner",
            "_on_group_trigger",
        },
    }
    hits: dict[str, set[str]] = {name: set() for name in expected}
    for path in _production_files():
        for _, scope, name, _line in _function_calls(path):
            if name in hits:
                hits[name].add(scope)
    for name, want in expected.items():
        assert hits[name] == want, (
            f"{name} 调用点应当恰好是 {sorted(want)}，实测 {sorted(hits[name])}"
        )


def test_n1_is_retired_so_no_turn_intent_leaks() -> None:
    """N1 已退役（2026-10-23）：两条生成路径都不许再往装配层传 ``turn_intent``。"""
    src = TURN_PIPELINE.read_text(encoding="utf-8")
    assert "attach_reply_intent(" not in src, "N1 接线已退役，不许长回来"
    tree = ast.parse(src)
    passed = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and any(kw.arg == "turn_intent" for kw in node.keywords):
            passed.add(getattr(node.func, "attr", "") or getattr(node.func, "id", ""))
    assert passed == set(), f"还有人把 turn_intent 往下传：{sorted(passed)}"


def _extra_keys_written_in_pipeline() -> set[str]:
    keys: set[str] = set()

    def text(node: ast.AST) -> str:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        return ""

    for node in ast.walk(_tree(TURN_PIPELINE)):
        if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name):
            if node.value.id == "extra":
                key = text(node.slice)
                if key:
                    keys.add(key)
        if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Attribute):
            if node.value.attr == "extra":
                key = text(node.slice)
                if key:
                    keys.add(key)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr == "get" and isinstance(node.func.value, ast.Attribute):
                if node.func.value.attr == "extra" and node.args:
                    key = text(node.args[0])
                    if key:
                        keys.add(key)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id == "AgentContext":
                for keyword in node.keywords:
                    if keyword.arg == "extra" and isinstance(keyword.value, ast.Dict):
                        for item in keyword.value.keys:
                            key = text(item)
                            if key:
                                keys.add(key)
    return keys


def test_route_decision_is_retired() -> None:
    """stage B：全仓生产包不许再出现第二套决策结构 ``RouteDecision``。"""

    hits: list[str] = []
    scan_roots = list(PROD_PACKAGES) + ["scripts"]
    for package in scan_roots:
        path = ROOT / package
        if not path.is_dir():
            continue
        for source in path.rglob("*.py"):
            if any(part in {"_scratch", "__pycache__"} for part in source.parts):
                continue
            if "tests" in source.parts:
                continue
            try:
                tree = _tree(source)
            except SyntaxError:
                continue
            rel = str(source.relative_to(ROOT)).replace(os.sep, "/")
            for node in ast.walk(tree):
                name = None
                if isinstance(node, ast.ClassDef) and node.name == "RouteDecision":
                    name = "class RouteDecision"
                elif isinstance(node, ast.Name) and node.id == "RouteDecision":
                    name = "name RouteDecision"
                elif isinstance(node, ast.Attribute) and node.attr == "RouteDecision":
                    name = "attr RouteDecision"
                elif isinstance(node, ast.ImportFrom):
                    for alias in node.names:
                        if alias.name == "RouteDecision":
                            name = "import RouteDecision"
                if name:
                    hits.append("%s:%d %s" % (rel, node.lineno, name))
    assert not hits, "RouteDecision 已退役，仍有引用：" + "; ".join(sorted(hits))


def test_snapshot_assembly_uses_speech_domain_context() -> None:
    """快照装配的 TurnContext 必须来自 ``core.speech.domain``（同名类冲突回归锁）。

    本模块也叫 TurnContext；搬运 ``_attach_speech`` 时只许实例化
    ``SpeechTurnContext`` 别名，否则参数不匹配会被 except 吞掉、快照静默变 None。
    """
    names = [
        name for _, scope, name, _ in _function_calls(TURN_PIPELINE)
        if scope == "attach_speech_for"
    ]
    assert "SpeechTurnContext" in names, "快照装配必须实例化 speech.domain.TurnContext"
    assert "TurnContext" not in names, (
        "快照装配不许实例化 pipeline 自己的 TurnContext（同名冲突）"
    )


def test_extra_contract_is_registered() -> None:
    from core.routing import turn_pipeline as tp

    assert isinstance(tp.EXTRA_CONTRACT, frozenset), type(tp.EXTRA_CONTRACT)
    missing_critical = CRITICAL_EXTRA_KEYS - set(tp.EXTRA_CONTRACT)
    assert not missing_critical, f"契约缺关键键：{sorted(missing_critical)}"
    retired_back = RETIRED_EXTRA_KEYS & set(tp.EXTRA_CONTRACT)
    assert not retired_back, f"退役键回流 contract：{sorted(retired_back)}"
    written = _extra_keys_written_in_pipeline()
    unregistered = written - set(tp.EXTRA_CONTRACT)
    assert not unregistered, (
        f"turn_pipeline 写了未登记 extra 键：{sorted(unregistered)}；"
        "登记后只允许删除，新增必须同 PR 说明谁读"
    )


def main() -> bool:
    print("=" * 60)
    print("turn pipeline single assembly checks (WP-06)")
    print("=" * 60)
    tests = (
        test_module_surface,
        test_each_assembly_call_site_is_unique,
        test_entry_shells_are_thin,
        test_decide_and_generate_are_wired_once_per_path,
        test_n1_is_retired_so_no_turn_intent_leaks,
        test_route_decision_is_retired,
        test_snapshot_assembly_uses_speech_domain_context,
        test_extra_contract_is_registered,
    )
    failed = 0
    for test in tests:
        try:
            test()
            print(f"  PASS {test.__name__}")
        except AssertionError as exc:
            failed += 1
            print(f"  FAIL {test.__name__}: {exc}")
        except Exception as exc:  # noqa: BLE001 - 诊断用
            failed += 1
            import traceback

            traceback.print_exc()
            print(f"  FAIL {test.__name__}: {type(exc).__name__}: {exc}")
    print(f"{len(tests) - failed}/{len(tests)} passed")
    return failed == 0


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
