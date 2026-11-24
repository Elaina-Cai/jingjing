"""M3 第一刀：模型可见工具名单只有一个属主。"""

from __future__ import annotations

import sys
import tempfile
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import check_single_catalog as G   # noqa: E402
from core.tools import catalog as C  # noqa: E402

FAILS: list[str] = []
COUNT = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global COUNT
    COUNT += 1
    if cond:
        print(f"  ok  {name}")
    else:
        FAILS.append(f"{name} {detail}")
        print(f"  FAIL {name} {detail}")


def t_catalog() -> None:
    check("目录含 send_plan", "send_plan" in C.PLAN_LOOP_ALLOWED)
    check("目录含 gui_state", "gui_state" in C.PLAN_LOOP_ALLOWED)
    check("目录含只读工具", "web_search" in C.PLAN_LOOP_ALLOWED)
    check("只读视图不含 send_plan", "send_plan" not in C.PLAN_LOOP_READ_ONLY)
    check("allowed_in_plan 认识 send_plan", C.allowed_in_plan("send_plan"))
    check("allowed_in_plan 拒绝瞎写", not C.allowed_in_plan("rm_rf"))
    # frozenset 不可变，共享同一对象是安全的（CPython 的 frozenset(fs) 会返回原对象）
    check("read_only_in_plan 与常量一致", C.read_only_in_plan() == C.PLAN_LOOP_READ_ONLY)
    check("只读视图是 frozenset", isinstance(C.read_only_in_plan(), frozenset))
    import agents.planner as P
    check("planner 别名指向同一对象", P.PLAN_LOOP_READ_ONLY_TOOLS is C.PLAN_LOOP_ALLOWED)
    check("planner 名单没被自造", len(P.PLAN_LOOP_READ_ONLY_TOOLS) == len(C.PLAN_LOOP_ALLOWED))


def t_rule_c() -> None:
    root = Path(tempfile.mkdtemp(prefix="cat_c_", dir=str(ROOT / ".test_tmp")))
    for rel, body in {
        "core/tools/catalog.py": "PLAN_LOOP_ALLOWED = frozenset({'web_search'})\n",
        "agents/bad.py": textwrap.dedent("""
            PLAN_LOOP_READ_ONLY_TOOLS = frozenset({'web_search'})
        """),
        "agents/ok.py": textwrap.dedent("""
            from core.tools import catalog as _CATALOG
            PLAN_LOOP_READ_ONLY_TOOLS = _CATALOG.PLAN_LOOP_ALLOWED
        """),
    }.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
    problems = G.scan(root)
    check("负向：自造名单被抓", any(p.startswith("C agents/bad.py") for p in problems), str(problems))
    check("正向：别名 re-export 放行", not any("agents/ok.py" in p for p in problems), str(problems))
    check("真仓通过", G.scan(ROOT) == [], str(G.scan(ROOT)))


def main() -> int:
    for fn in (t_catalog, t_rule_c):
        print(fn.__name__)
        fn()
    print(f"RESULT {len(FAILS)} failed / {COUNT} checks")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
