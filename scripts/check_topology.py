"""Enforce the arrows of the 3D graph on the code.

`scripts/check_docs_consistency.py` proves 文档 ↔ 图，`scripts/check_3d_textures.py`
proves 贴图 ↔ 文档。没有人证明 **代码 ↔ 图**：一个模块可以悄悄绕过箭头（办自己开口
说话、适配器被第二个地方调用、账本多出第二个写入口），而所有检查器仍然是绿的。
这份脚本把图上的箭头变成断言。

Rules enforced
--------------
1. 每块板都有落点：12 个实体 → 代码文件必须存在（外部世界例外：必须没有）。
2. 办与工具不自启：``core/jobs`` / ``core/tools`` / ``core/execution/adapters.py``
   不许 import ``core.speech`` / ``core.routing`` / ``agents`` / ``scheduler`` /
   ``web`` / ``llm``；也不许调用 ``.deliver(``——说话只能走 判 → 出口。
3. 契约是叶子：``core/contracts/plan.py`` / ``core/contracts/delivery.py`` 只许
   import ``core.contracts.*``，不许摸实现。
4. 适配器只有一个调用方：``core/`` 下 import ``core.execution.adapters`` 的模块必须
   在白名单；多一个就是多一张嘴。
5. 账本只有一个写入口：``TerminalEventStore(`` 的构造点必须在白名单；新出现一处
   就是出现了第二条写路径。
6. 接线必须带开关：``core/routing/turn_pipeline.py`` 里一旦出现 ``JobStore`` /
   ``job_readout`` / ``advance(``，同文件必须出现 ``JOB_LEDGER``；一旦出现
   ``core.speech.delivery`` / ``pending_approval``，必须出现 ``DELIVERY_CONTRACT`` 或 ``JOB_LEDGER``
（第六态有两段路：出口消费时挂前者，推进器产出时挂后者）。
7. 开关都在册：``turn_trace`` / ``verdict_contract`` / ``job_ledger`` 必须在
   ``core/features.py::FLAG_REGISTRY`` 里；``refactor_plan.yaml`` 声明但未注册的
   开关只给 WARN（那些片还没开工）。
8. 按回车只有几处：``core/`` 下出现 ``press_enter(`` 或 ``VK_RETURN`` 的文件必须
   在 ``ENTER_SITES`` 里登记理由。按回车就是把一句话真的送到人眼前——它是 R3 在
   微信上的最后一厘米，多一处就是多一张没登记的嘴。

Run it from the repository root:

    python scripts/check_topology.py
"""

from __future__ import annotations

import ast
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# --- 图上的 12 个实体 → 代码落点（外部世界不是我方组件，必须没有）------------
ENTITY_CODE = {
    "外部世界": (),
    "感知层": ("core/terminal/event_store_db.py", "core/terminal/event_identity.py"),
    "账本 · 事件流": ("core/terminal/event_store_db.py",),
    "世界窗口": ("core/context/chat_window.py",),
    "记忆立方体": ("core/memory/service.py",),
    "自我立方体": ("core/context/state_assembly.py",),
    "判 / 投 / 记": ("core/routing/turn_pipeline.py",),
    "留痕条": ("core/terminal/turn_trace.py",),
    "出口": ("core/speech/delivery.py", "core/contracts/delivery.py"),
    "软件 · 适配器": ("core/execution/adapters.py",),
    "工具包": ("core/tools/registry.py",),
    "在手的事": (
        "core/jobs/store.py",
        "core/jobs/runner.py",
        "core/context/job_readout.py",
        "core/contracts/plan.py",
    ),
}

# --- 规则 2：不自启的包与禁止的方向 -----------------------------------------
NO_MOUTH_DIRS = ("core/jobs", "core/tools")
NO_MOUTH_FILES = ("core/execution/adapters.py",)
FORBIDDEN_IMPORTS = (
    "core.speech",
    "core.routing",
    "agents",
    "scheduler",
    "web",
    "llm",
)
FORBIDDEN_CALLS = ("deliver",)

# --- 规则 3：契约叶子 -------------------------------------------------------
LEAF_CONTRACTS = ("core/contracts/plan.py", "core/contracts/delivery.py")
LEAF_ALLOWED = ("core.contracts",)

# --- 规则 4：适配器的调用方（出口）------------------------------------------
ADAPTER_MODULE = "core.execution.adapters"
#: 规则 4 的白名单：**调用**适配器的地方只有出口。
#: 适配器实现自己当然要 import 契约（`AdapterCapabilities` / `AdapterRegistry`），
#: 那是「实现」不是「多一张嘴」——所以实现文件登记在册并写明理由。
ADAPTER_IMPORTERS_ALLOWED = (
    "core/speech/delivery.py",        # 出口：唯一调用 send() 的地方
    "core/execution/wechat_adapter.py",  # 实现：微信适配器（实现 Protocol，不调用别人）
)

# --- 规则 5：账本的写入口（历史四个 + 自身）--------------------------------
LEDGER_CTOR = "TerminalEventStore("
LEDGER_WRITERS_ALLOWED = (
    "core/terminal/event_store_db.py",
    "core/terminal/event_runtime.py",
    "core/terminal/command_service.py",
    "core/terminal/audit_export.py",
    "core/tasks/store.py",
)

# --- 规则 6：接线与开关的共存条件 -------------------------------------------
TURN_PIPELINE = "core/routing/turn_pipeline.py"
COUPLINGS = (
    (("JobStore", "job_readout", "advance("), ("JOB_LEDGER",)),
    # 第六态 `pending_approval` 有两段路：出口消费时挂 DELIVERY_CONTRACT；
    # U-10 期间由推进器产出、挂在 JOB_LEDGER 之后（消费方尚未接）——任一即可。
    (("core.speech.delivery", "pending_approval"), ("DELIVERY_CONTRACT", "JOB_LEDGER")),
)

# --- 规则 7：必须在册的开关 -------------------------------------------------
REQUIRED_FLAGS = ("turn_trace", "verdict_contract", "job_ledger")

problems: list[str] = []
warns: list[str] = []


LEASE_MODULE = "core/execution/gui/lease.py"
DEVICE_GATE = "core/execution/gui/wechat.py"
METRICS_MODULE = "core/execution/gui/metrics.py"
CAPABILITY_MODULE = "core/execution/gui/capability.py"
ALLOWED_LEASE_CALLERS = (LEASE_MODULE, DEVICE_GATE)
ALLOWED_PROBE_CALLERS = (CAPABILITY_MODULE, DEVICE_GATE)
LOCK_PRIMITIVE = "msvcrt.locking"
LEGACY_LOCK_MODULES = ("core/execution/wx_chat.py",)  # UI_LOCK 是线程锁，不是跨进程锁：登记例外


def rel(path: str) -> str:
    return os.path.relpath(path, ROOT).replace(os.sep, "/")


def read(path: str) -> str:
    with open(path, encoding="utf-8-sig") as fh:
        return fh.read()


def py_files(prefix: str) -> list[str]:
    out: list[str] = []
    if os.path.isfile(prefix):
        return [prefix]
    for base, _dirs, files in os.walk(prefix):
        if "__pycache__" in base:
            continue
        for name in files:
            if name.endswith(".py"):
                out.append(os.path.join(base, name))
    return sorted(out)


def read_imports(path: str) -> list[str]:
    """Top-level import targets of one module (absolute only)."""
    try:
        tree = ast.parse(read(path))
    except SyntaxError as exc:  # a syntax error is not this script's problem
        warns.append(f"[W] 解析失败 {rel(path)}: {exc}")
        return []
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.append(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.append(node.module)
    return found


def attr_calls(path: str) -> list[str]:
    """.name(...) — attribute calls only, to avoid catching a local ``deliver``."""
    try:
        tree = ast.parse(read(path))
    except SyntaxError:
        return []
    out: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            out.append(node.func.attr)
    return out


def hits(imports: list[str], targets: tuple[str, ...]) -> list[str]:
    return [i for i in imports if any(i == t or i.startswith(t + ".") for t in targets)]


def rule_entity_landing() -> None:
    for name, files in ENTITY_CODE.items():
        if not files:
            continue
        for f in files:
            if not os.path.isfile(os.path.join(ROOT, f)):
                problems.append(f"板「{name}」的落点不存在：{f}")
    if ENTITY_CODE.get("外部世界"):
        problems.append("外部世界不该有代码落点（它不是我方组件）")


def rule_no_second_mouth() -> None:
    targets: list[str] = []
    for d in NO_MOUTH_DIRS:
        targets += py_files(os.path.join(ROOT, d))
    for f in NO_MOUTH_FILES:
        p = os.path.join(ROOT, f)
        if os.path.isfile(p):
            targets.append(p)
    for path in targets:
        bad = hits(read_imports(path), FORBIDDEN_IMPORTS)
        if bad:
            problems.append(f"{rel(path)} import 了 {bad}（办与工具不许自己开口）")
        called = [c for c in attr_calls(path) if c in FORBIDDEN_CALLS]
        if called:
            problems.append(f"{rel(path)} 调用了 .{called[0]}()（说话只能走 判 → 出口）")


#: 「按回车」的真实调用点白名单。新增一处必须在下面登记理由。
ENTER_SITES: dict[str, str] = {
    "core/execution/wx_human.py": "VK_RETURN 的定义处（SendInput 唯一实现）",
    "core/execution/wx_chat.py": "press_enter() 的定义处 + 内部那一次",
    "core/execution/wx_sender.py": "旧写侧的发送器（收敛前允许；SEND_TEXT 已由 wx_executor 拒绝）",
    "core/execution/gui/wechat.py": "_Deps.press_enter 的默认真实实现（只给 wechat_send 用）",
    "core/execution/gui/wechat_send.py": "impl.press_enter()（新路径唯一调用点）",
}


def rule_single_enter() -> None:
    """按回车 = 把一句话真的送到人眼前：全仓只许有这么几处。"""
    pattern = re.compile(r"press_enter\(")
    for path in py_files("core"):
        text = read(path)
        if not (pattern.search(text) or "VK_RETURN" in text):
            continue
        name = rel(path)
        if name not in ENTER_SITES:
            problems.append(
                f"{name} 里出现了按回车/回车键，但没在 scripts/check_topology.py::ENTER_SITES "
                "登记理由（对外说话只许走出口那一处）"
            )
    for name in ENTER_SITES:
        if not os.path.isfile(os.path.join(ROOT, name)):
            problems.append(f"ENTER_SITES 登记的 {name} 不存在（清掉这一条）")


def rule_leaf_contracts() -> None:
    for f in LEAF_CONTRACTS:
        path = os.path.join(ROOT, f)
        if not os.path.isfile(path):
            continue
        bad = [
            i
            for i in read_imports(path)
            if (i.startswith("core") or i.startswith("agents"))
            and not any(i == a or i.startswith(a + ".") for a in LEAF_ALLOWED)
        ]
        if bad:
            problems.append(f"{f} 不是叶子，import 了 {bad}")


def rule_single_adapter_importer() -> None:
    for path in py_files(os.path.join(ROOT, "core")):
        if rel(path) in ADAPTER_IMPORTERS_ALLOWED:
            continue
        if ADAPTER_MODULE in read_imports(path):
            problems.append(
                f"{rel(path)} import 了 {ADAPTER_MODULE}（适配器只许出口调用）"
            )


def rule_ledger_writer() -> None:
    for path in py_files(os.path.join(ROOT, "core")):
        if rel(path) in LEDGER_WRITERS_ALLOWED:
            continue
        if LEDGER_CTOR in read(path):
            problems.append(
                f"{rel(path)} 构造了 TerminalEventStore（账本只许 eventize 一处写）"
            )


def rule_flag_before_wiring() -> None:
    path = os.path.join(ROOT, TURN_PIPELINE)
    if not os.path.isfile(path):
        problems.append(f"回合层不见了：{TURN_PIPELINE}")
        return
    text = read(path)
    for markers, flags in COUPLINGS:
        present = [m for m in markers if m in text]
        if present and not any(f in text for f in flags):
            problems.append(
                f"{TURN_PIPELINE} 里出现了 {present} 却没有 {' / '.join(flags)}（先挂开关再接线）"
            )


def rule_flags_registered() -> None:
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    try:
        from core import features  # noqa: PLC0415 — 故意延迟，UI 不需要它
    except Exception as exc:  # pragma: no cover — 报告而不是崩
        warns.append(f"[W] 无法 import core.features：{exc}")
        return
    registry = getattr(features, "FLAG_REGISTRY", {})
    for name in REQUIRED_FLAGS:
        if name not in registry:
            problems.append(f"开关 {name} 不在 core/features.py::FLAG_REGISTRY")
    plan = os.path.join(ROOT, "refactor_plan.yaml")
    if not os.path.isfile(plan):
        return
    try:
        import yaml  # noqa: PLC0415
    except Exception:
        return
    data = yaml.safe_load(read(plan)) or {}
    for key, _val in data.items():
        if not isinstance(_val, dict) or "flags" not in _val:
            continue
        raw = str(_val["flags"])
        for name in re.findall(r"([A-Z][A-Z0-9_]{2,})\s*:", raw):
            if name.lower() not in registry:
                warns.append(f"[W] {key} 声明的开关 {name} 尚未注册（该片未开工）")


def rule_single_lock_factory() -> None:
    """I1：跨进程锁只许在 lease.py 里造——别处抄一份，必然两把锁各锁各的。"""
    for path in py_files(os.path.join(ROOT, "core")):
        name = rel(path)
        if name == LEASE_MODULE or name in LEGACY_LOCK_MODULES:
            continue
        if LOCK_PRIMITIVE in read(path):
            problems.append(f"I1 {name} 自己造跨进程锁（只许 {LEASE_MODULE}）")


def rule_lease_behind_gate() -> None:
    """I2：拿租约只许在门里（wechat.device_guard）——别处直接拿会绕过能力自检。"""
    for path in py_files(os.path.join(ROOT, "core")):
        name = rel(path)
        if name in ALLOWED_LEASE_CALLERS:
            continue
        if "device_lease(" in read(path):
            problems.append(f"I2 {name} 直接拿租约（只许门内：{' / '.join(ALLOWED_LEASE_CALLERS)}）")


def rule_probe_behind_gate() -> None:
    """I3：能力自检只许在门里调——别处调了也不会拦。"""
    for path in py_files(os.path.join(ROOT, "core")):
        name = rel(path)
        if name in ALLOWED_PROBE_CALLERS:
            continue
        text = read(path)
        if "capability_probe.probe(" in text or "capability.probe(" in text:
            problems.append(f"I3 {name} 自己调能力自检（只许门内：{' / '.join(ALLOWED_PROBE_CALLERS)}）")


def rule_metric_names() -> None:
    """I4：量具名必须是白名单里的字面量——打错字等于这门量具静默为零。"""
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    try:
        from core.execution.gui import metrics  # noqa: PLC0415
    except Exception as exc:  # pragma: no cover
        warns.append(f"[W] 无法 import 量具：{exc}")
        return
    allowed = set(metrics.METRIC_NAMES)
    for path in py_files(os.path.join(ROOT, "core")):
        text = read(path)
        for name in re.findall(r"bump\(\s*[\"']([^\"']+)[\"']", text):
            if name not in allowed:
                problems.append(f"I4 {rel(path)} 用了未登记的量具名 {name!r}")


def rule_metrics_not_in_ledger() -> None:
    """I5：量具不进账本正行（R1 不动）。"""
    terminal = os.path.join(ROOT, "core", "terminal")
    if not os.path.isdir(terminal):
        return
    for path in py_files(terminal):
        text = read(path)
        if "metrics.bump(" in text or "from core.execution.gui import metrics" in text:
            problems.append(f"I5 {rel(path)} 在账本层用设备量具（量具不进账本）")


def main() -> int:
    rule_entity_landing()
    rule_no_second_mouth()
    rule_leaf_contracts()
    rule_single_adapter_importer()
    rule_ledger_writer()
    rule_flag_before_wiring()
    rule_flags_registered()
    rule_single_enter()
    rule_single_lock_factory()
    rule_lease_behind_gate()
    rule_probe_behind_gate()
    rule_metric_names()
    rule_metrics_not_in_ledger()
    print(f"图：{len(ENTITY_CODE)} 个实体 → 代码落点 {sum(len(v) for v in ENTITY_CODE.values())} 处")
    for w in warns:
        print("WARN", w)
    for p in problems:
        print("FAIL", p)
    print(f"\n{'PASSED' if not problems else f'FAILED: {len(problems)} problem(s)'}")
    return 0 if not problems else 1


if __name__ == "__main__":
    raise SystemExit(main())
