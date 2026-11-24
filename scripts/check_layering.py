"""Enforce the layering described in docs/system_architecture.md.

That document defines a decision layer, an intent boundary and a per-platform
execution layer, but nothing checked it, so drift was invisible.  This script
turns the rules into assertions.

Rules enforced
--------------

1. No import cycles anywhere in the project.
2. The decision layer (agents/) may only depend on the execution layer's
   *abstractions* -- the intent bus and the provider/platform registry -- never
   on a concrete platform sender.
3. wx_vision and wx_human are leaf primitives: they must not import any other
   project module, so they stay reusable and testable in isolation.
4. Only the WeChat execution modules may mention the WeChat window class, so the
   platform detail stays in one place.
5. Layers outside core/execution must import only public names from it.  Reaching
   for an underscore-prefixed helper couples an HTTP route to an implementation
   detail that can be renamed without anything noticing until a request fails.
6. The platform senders are peers.  Each owns one kind of message and they share
   the wx_chat capability for getting a conversation open; neither may import the
   other, which would drag a singleton and a rate limiter across for no reason.
7. Analysis results are read through ``core.analysis.service``.  Reaching for the
   legacy ``analysis`` table (or its repository) directly is how the memory chain
   ended up with a second read path; that path is closed here.
8. System prompts are assembled through ``core.context.assembler``.  Only the
   assembler (and its documented fallback in ``agents/conversational.py``) may
   call the persona prompt builders; a new call site means the block/budget
   layer is being bypassed.
  9. Route assembly lives in ``core/routing/turn_pipeline.py`` (one call site per
     busy function); the retired ``RouteDecision`` may not come back.
 10. The new packages only depend downward: ``core/contracts/plan.py`` and
     ``core/contracts/delivery.py`` are leaves (types only, no ``core.*``
     implementation), and ``core/jobs`` / ``core/tools`` /
     ``core/execution/adapters.py`` may not import ``core.speech`` /
     ``core.routing`` / ``agents`` / ``scheduler`` / ``web`` -- 办与工具都不许
     自己开口说话，说话只能走判 -> 出口。

Run it from the repository root:

    python scripts/check_layering.py
"""

from __future__ import annotations

import ast
import os
import sys
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PACKAGES = ("core", "agents", "web", "scheduler", "db", "llm", "notify", "scripts", "tests", "tools")

# The execution layer's abstraction surface; the decision layer may use these.
ABSTRACTION = (
    "core.execution.intent_bus",
    "core.execution.provider",
    "core.execution.platform_registry",
)

# Platform-specific execution modules that are allowed to know about WeChat's UI.
WECHAT_MODULES = {
    "core/execution/wx_vision.py",
    "core/execution/wx_human.py",
    "core/execution/wx_sender.py",
    "core/execution/wx_sticker_sender.py",
    "core/execution/sticker_auto_setup.py",
    "core/execution/wx_diagnose.py",
}

LEAF_MODULES = ("core/execution/wx_vision.py", "core/execution/wx_human.py")

# Peer senders: one kind of message each, sharing wx_chat for contact opening.
PEER_SENDERS = ("core/execution/wx_sender.py", "core/execution/wx_sticker_sender.py")

# The window-class rule applies to shipped code only: tests may construct a fake
# window, and scripts/ holds standalone exploration tools that predate wx_vision.
PLATFORM_RULE_ROOTS = ("core/", "agents/", "web/", "scheduler/", "db/", "llm/", "notify/")
PLATFORM_RULE_SKIP = {"scripts/check_layering.py"}

# 这些目录不是项目源码，不参与分层规则检查。
# `_scratch` = scripts/_scratch/，存放 2026-09-27 从仓库根目录移入的一次性脚本与产物。
# 若把它算进来，历史垃圾脚本会让本检查误报失败（例如 .tmp_ref_audit.py 的未定义名）。
NON_SOURCE_DIRS = ("__pycache__", "build", "site-packages", "_scratch")


def project_files():
    out = []
    for base in PACKAGES:
        for dirpath, dirnames, filenames in os.walk(os.path.join(ROOT, base)):
            dirnames[:] = [d for d in dirnames if d not in NON_SOURCE_DIRS]
            for name in filenames:
                if name.endswith(".py"):
                    out.append(os.path.relpath(os.path.join(dirpath, name), ROOT).replace(os.sep, "/"))
    return sorted(out)


def read_imports(path):
    """Return (module_level, function_level, source) project imports."""
    # utf-8-sig so a leading BOM does not break parsing; several files in this
    # repository carry one.
    source = open(os.path.join(ROOT, path), encoding="utf-8-sig").read()
    tree = ast.parse(source)

    def collect(node):
        found = set()
        for n in ast.walk(node):
            if isinstance(n, ast.Import):
                for alias in n.names:
                    found.add(alias.name)
            elif isinstance(n, ast.ImportFrom) and n.module and n.level == 0:
                found.add(n.module)
        return found

    top = set()
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            top |= collect(node)

    inner = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            inner |= collect(node)

    def keep(names):
        return {n for n in names if n.split(".")[0] in PACKAGES}

    return keep(top), keep(inner) - keep(top), source


def symbol_imports(path):
    """(module, name) pairs imported from core.execution by one file."""
    source = open(os.path.join(ROOT, path), encoding="utf-8-sig").read()
    found = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            if node.module == "core.execution" or node.module.startswith("core.execution."):
                for alias in node.names:
                    found.append((node.module, alias.name))
    return found


def undefined_names(path):
    """Names that are read but never bound -- a NameError waiting to happen.

    A refactor that moves a function to another module and forgets one alias
    produces exactly this, and it only explodes on the code path that uses it.
    That is how a renamed helper can silently kill text sending while every
    sticker test still passes, so it is checked statically instead.
    """
    import builtins

    try:
        source = open(os.path.join(ROOT, path), encoding="utf-8-sig").read()
        tree = ast.parse(source)
    except SyntaxError:
        return {}

    bound = set(dir(builtins)) | {"__file__", "__name__", "__doc__", "__spec__"}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(node.name)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            bound.add(node.id)
        elif isinstance(node, ast.arg):
            bound.add(node.arg)
        elif isinstance(node, ast.alias):
            bound.add((node.asname or node.name).split(".")[0])
        elif isinstance(node, ast.ExceptHandler) and node.name:
            bound.add(node.name)
        elif isinstance(node, ast.Global):
            bound.update(node.names)

    # Known blind spot: a name imported inside one function is treated as bound
    # everywhere, so a module-level use of a locally imported helper is not
    # caught here.  Two attempts at real scope analysis both drowned in false
    # positives -- parameters and same-function local imports -- and a check
    # that cries wolf gets ignored, which is worse than one that misses a case.
    out = {}
    for node in ast.walk(tree):
        if (isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
                and node.id not in bound):
            out.setdefault(node.id, node.lineno)
    return out


# ---------------------------------------------------------------------------
# WP-06：路由装配必须收口到 core.routing.turn_pipeline
# ---------------------------------------------------------------------------
ROUTING_PIPELINE = "core/routing/turn_pipeline.py"
ROUTING_ROUTER = "core/routing/router.py"
#: 五个装配名字（《当断则断_执行手册》§6.3）：core/routing 下各只许一个 call site。
ROUTING_SINGLE_ASSEMBLY = (
    "run_admission_result", "build_turn_world",
    "_prepare_reply_with_policy", "_send_reply",
)
ROUTING_ENTRY_SHELLS = ("_invoke_conversational_inner", "_on_group_trigger")
#: 一旦在入口壳里直接出现，就说明装配又漏回 router 了。
ROUTING_SHELL_FORBIDDEN = frozenset({
    "run_admission_result", "build_turn_world",
    "_prepare_reply_with_policy", "_send_reply", "_attach_speech",
    "_attach_content_plan", "_apply_admission",
    "build_agent",
})


def routing_call_sites():
    """[(file, 宿主函数, callee 名)]——扫 router + turn_pipeline 的全部 Call。"""
    out = []
    for rel in (ROUTING_ROUTER, ROUTING_PIPELINE):
        path = os.path.join(ROOT, rel)
        if not os.path.exists(path):
            continue
        tree = ast.parse(open(path, encoding="utf-8-sig").read())

        def walk(node, scope):
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    walk(child, child.name)
                elif isinstance(child, ast.ClassDef):
                    walk(child, scope)
                else:
                    if isinstance(child, ast.Call):
                        func = child.func
                        name = (
                            func.attr if isinstance(func, ast.Attribute)
                            else getattr(func, "id", "")
                        )
                        out.append((rel, scope, name))
                    walk(child, scope)

        walk(tree, "<module>")
    return out


def routing_shell_imports(shell):
    """入口壳函数体里的 project imports（模块级 import 不算）。"""
    path = os.path.join(ROOT, ROUTING_ROUTER)
    tree = ast.parse(open(path, encoding="utf-8-sig").read())
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name != shell:
            continue
        for inner in ast.walk(node):
            if isinstance(inner, ast.Import):
                found.extend(alias.name for alias in inner.names)
            elif isinstance(inner, ast.ImportFrom) and inner.module:
                found.append(inner.module)
    return found


def main() -> int:
    problems = []
    top_imports, local_imports, sources = {}, {}, {}

    for path in project_files():
        try:
            top, local, source = read_imports(path)
        except SyntaxError as exc:
            problems.append("SYNTAX  %s: %s" % (path, exc))
            continue
        top_imports[path] = top
        local_imports[path] = local
        sources[path] = source

    # ---- rule 1: no cycles -------------------------------------------------
    edges = defaultdict(set)
    for path, imports in top_imports.items():
        for name in imports:
            parts = name.split(".")
            for depth in (2, 3):
                if len(parts) >= depth:
                    edges[path].add("/".join(parts[:depth]).replace(".", "/"))
    # map module paths back onto files
    by_module = {}
    for path in top_imports:
        by_module[path[:-3]] = path
        by_module[path[:-3].replace("/", ".")] = path

    color, stack = {}, []

    def visit(node):
        color[node] = 1
        stack.append(node)
        for target in sorted(edges.get(node, ())):
            resolved = by_module.get(target)
            if resolved is None:
                continue
            if resolved not in color:
                visit(resolved)
            elif color.get(resolved) == 1:
                cycle = stack[stack.index(resolved):] + [resolved]
                problems.append("CYCLE   " + " -> ".join(cycle))
        stack.pop()
        color[node] = 2

    for node in list(edges):
        if node not in color:
            visit(node)

    # ---- rule 2: agents/ uses abstractions only ----------------------------
    for path, imports in top_imports.items():
        if not path.startswith("agents/"):
            continue
        for name in imports:
            if not name.startswith("core.execution"):
                continue
            if not any(name == a or name.startswith(a + ".") for a in ABSTRACTION):
                problems.append(
                    "LAYER   %s imports concrete execution module %s; "
                    "the decision layer must use the intent bus or provider only"
                    % (path, name))

    # ---- rule 3: leaves import nothing -------------------------------------
    for leaf in LEAF_MODULES:
        imports = top_imports.get(leaf, set()) | local_imports.get(leaf, set())
        if imports:
            problems.append("LEAF    %s imports project modules %s" % (leaf, sorted(imports)))

    # ---- rule 4: WeChat window class stays in wx_vision --------------------
    for path, source in sources.items():
        if path in WECHAT_MODULES or path in PLATFORM_RULE_SKIP:
            continue
        if not path.startswith(PLATFORM_RULE_ROOTS):
            continue
        if "QWindowIcon" in source:
            problems.append("PLATFORM %s mentions a Qt window class; "
                            "window discovery belongs in wx_vision" % path)

    # ---- rule 5: outsiders use public names only ---------------------------
    for path in top_imports:
        if path.startswith("core/execution/"):
            continue
        for module, name in symbol_imports(path):
            if name.startswith("_"):
                problems.append(
                    "PRIVATE %s imports private %s.%s; use core.execution.wx_api instead"
                    % (path, module.rsplit(".", 1)[-1], name))

    # ---- rule 6: senders are peers -----------------------------------------
    for sender in PEER_SENDERS:
        others = [p for p in PEER_SENDERS if p != sender]
        imported = top_imports.get(sender, set()) | local_imports.get(sender, set())
        for other in others:
            module = other[:-3].replace("/", ".").replace("core.execution.", "core.execution.")
            if any(name == module for name in imported):
                problems.append(
                    "PEER    %s imports %s; move shared work into wx_chat"
                    % (sender, other))

    # ---- rule 7: no name is used before it is bound -------------------------
    for path in top_imports:
        if path.startswith("tools/"):  # vendored third-party code
            continue
        for name, line in sorted(undefined_names(path).items()):
            problems.append(
                "UNDEF   %s:%d uses undefined name %r -- import it or alias it"
                % (path, line, name))

    # ---- rule 8: analysis reads go through the service ----------------------
    ANALYSIS_ALLOW = {
        "core/analysis/service.py",
        "db/repo_analysis.py",
        "scripts/check_layering.py",
    }
    ANALYSIS_CALLS = (
        ".analysis.get_recent(",
        ".analysis.get_daily_digest(",
        ".analysis.add(",
        ".analysis.add_from_dict(",
    )
    for path, source in sources.items():
        if path in ANALYSIS_ALLOW or path.startswith(("scripts/", "tests/", "backups/")):
            continue
        for lineno, line in enumerate(source.splitlines(), 1):
            if any(token in line for token in ANALYSIS_CALLS):
                problems.append(
                    "ANALYSIS %s:%d reads/writes the analysis table directly; "
                    "use core.analysis.service.AnalysisService" % (path, lineno)
                )

    # ---- rule 9: system prompts go through the context assembler -----------
    PROMPT_ALLOW = {
        "core/context/assembler.py",
        "core/terminal/persona_brain.py",
        "agents/conversational.py",  # documented fallback when the assembler fails
    }
    PROMPT_CALLS = ("build_persona_blocks(", "build_persona_system_prompt(")
    for path, source in sources.items():
        if path in PROMPT_ALLOW or path.startswith(("tests/", "scripts/", "backups/")):
            continue
        for lineno, line in enumerate(source.splitlines(), 1):
            if any(token in line for token in PROMPT_CALLS):
                problems.append(
                    "PROMPT  %s:%d builds a system prompt directly; "
                    "use core.context.assembler.build_system_prompt" % (path, lineno)
                )

    # ---- rule 10: 路由装配收口到 turn_pipeline，且 RouteDecision 已退役 ----
    for path, source in sources.items():
        if path.startswith(("tests/", "scripts/", "backups/", "tools/")):
            continue
        if "RouteDecision" in source:
            problems.append(
                "ROUTING %s still mentions retired RouteDecision; use TurnDecision"
                % path
            )

    if not os.path.exists(os.path.join(ROOT, ROUTING_PIPELINE)):
        problems.append(
            "ROUTING %s missing; route assembly must live there" % ROUTING_PIPELINE
        )
    else:
        calls = routing_call_sites()
        for name in ROUTING_SINGLE_ASSEMBLY:
            hits = [(rel, scope) for rel, scope, callee in calls if callee == name]
            if len(hits) != 1:
                problems.append(
                    "ROUTING %s has %d call sites (want exactly 1): %s"
                    % (name, len(hits), hits)
                )
            elif hits[0][0] != ROUTING_PIPELINE:
                problems.append(
                    "ROUTING %s must be called from %s, got %s"
                    % (name, ROUTING_PIPELINE, hits[0])
                )
        #: task-4：壳 -> {callee: 期望次数}。直回 route 决策在 _route_one；
        #: 触发 route 决策在 _on_group_trigger；两条 relevance 阶段在各自生成函数。
        shell_expectations = {
            "_invoke_conversational_inner": {"generate_turn": 1},
            "_on_group_trigger": {"decide_turn": 1, "generate_turn": 1},
            "_route_one": {"decide_turn": 1},
        }
        shell_calls = {shell: [] for shell in shell_expectations}
        for rel, scope, callee in calls:
            if rel == ROUTING_ROUTER and scope in shell_calls:
                shell_calls[scope].append(callee)
        for shell, expected in shell_expectations.items():
            names = shell_calls[shell]
            for callee, want in expected.items():
                if names.count(callee) != want:
                    problems.append(
                        "ROUTING shell %s must call %s %d time(s), got %s"
                        % (shell, callee, want, names)
                    )
            leaked = sorted(set(names) & ROUTING_SHELL_FORBIDDEN)
            if leaked:
                problems.append(
                    "ROUTING shell %s still assembles directly: %s" % (shell, leaked)
                )
            bad_imports = [
                name for name in routing_shell_imports(shell)
                if name.startswith(("core.context", "core.bootstrap", "agents."))
            ]
            if bad_imports:
                problems.append(
                    "ROUTING shell %s imports assembly modules directly: %s"
                    % (shell, sorted(bad_imports))
                )

    # ---- rule 11: 新包只许朝下依赖（阶段 0/1 的方向表）---------------------
    DIRECTION_LEAVES = ("core/contracts/plan.py", "core/contracts/delivery.py")
    DIRECTION_FORBIDDEN = {
        "core/jobs/": ("core.speech", "core.routing", "agents", "scheduler", "web"),
        "core/tools/": ("core.speech", "core.routing", "agents", "scheduler", "web"),
        "core/execution/adapters.py": (
            "core.speech", "core.routing", "agents", "scheduler", "web",
        ),
    }
    for leaf in DIRECTION_LEAVES:
        if leaf not in top_imports:
            continue
        leaked = sorted(
            name
            for name in top_imports[leaf]
            if name.startswith("core.") and not name.startswith("core.contracts")
        )
        if leaked:
            problems.append(
                "DIRECTION %s imports %s; contracts hold types only "
                "(no core.* implementation)" % (leaf, leaked)
            )
    for prefix, forbidden in DIRECTION_FORBIDDEN.items():
        for path, imports in top_imports.items():
            if not path.startswith(prefix):
                continue
            leaked = sorted(
                name
                for name in imports
                if any(name == f or name.startswith(f + ".") for f in forbidden)
            )
            if leaked:
                problems.append(
                    "DIRECTION %s imports %s; %s must not reach upward (only 判 说话)"
                    % (path, leaked, prefix)
                )

    print("scanned %d modules" % len(top_imports))
    if problems:
        print()
        for problem in problems:
            print("  " + problem)
        print()
        print("FAILED: %d problem(s)" % len(problems))
        return 1

    print("  ok  no import cycles")
    print("  ok  agents/ depends only on intent_bus / provider / platform_registry")
    print("  ok  %s are leaf modules" % ", ".join(LEAF_MODULES))
    print("  ok  Qt window class confined to the WeChat execution modules")
    print("  ok  no private core.execution symbols imported from outside")
    print("  ok  text and sticker senders are peers (shared work lives in wx_chat)")
    print(
        "  ok  core/jobs & core/tools & execution/adapters 不 import "
        "speech/routing/agents/scheduler/web；contracts 是叶子"
    )
    print("  ok  no module uses a name it never bound")
    print("  ok  analysis reads go through core.analysis.service")
    print("  ok  system prompts go through core.context.assembler")
    print("  ok  route assembly goes through core.routing.turn_pipeline")
    print()
    print("PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
