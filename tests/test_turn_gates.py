"""三道新闸：正向通过 + 合成违规必须被抓到。"""

from __future__ import annotations

import sys
import tempfile
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

sys.path.insert(0, str(ROOT / "scripts"))
import check_no_silent_return as C1   # noqa: E402
import check_single_catalog as C2     # noqa: E402
import check_turn_reachability as C3  # noqa: E402

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


def tree(files: dict[str, str]) -> Path:
    root = Path(tempfile.mkdtemp(prefix="turn_gates_", dir=str(ROOT / ".test_tmp")))
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(text), encoding="utf-8")
    return root


ENTRY_OK = """
    from __future__ import annotations
    import logging
    log = logging.getLogger(__name__)
    async def run_turn(ctx):
        if not ctx:
            log.info("empty ctx")
            return None
        return None
"""
INIT_OK = """
    from core.turn.entry import run_turn  # noqa: F401
"""


def t_reachability() -> None:
    root = tree({"core/turn/entry.py": ENTRY_OK, "core/turn/__init__.py": INIT_OK,
                 "core/turn/steps.py": "STEP_NAMES = ('ingest',)\n"})
    check("正向：干净入口通过", C3.scan(root) == [], str(C3.scan(root)))

    bad = tree({"core/turn/entry.py": ENTRY_OK + "\n    import core.speech.outbound\n",
                "core/turn/__init__.py": INIT_OK})
    problems = C3.scan(bad)
    check("负向：反向依赖被抓", any(p.startswith("A ") for p in problems), str(problems))

    orphan = tree({"core/turn/entry.py": ENTRY_OK,
                   "core/turn/__init__.py": INIT_OK,
                   "core/turn/util.py": "def _unused_helper():\n    return 1\n"})
    problems = C3.scan(orphan)
    check("负向：私有孤儿被抓", any(p.startswith("B ") for p in problems), str(problems))

    no_entry = tree({"core/turn/__init__.py": INIT_OK})
    problems = C3.scan(no_entry)
    check("负向：缺 entry 被抓", any(p.startswith("C ") for p in problems), str(problems))

    check("真仓通过", C3.scan(ROOT) == [], str(C3.scan(ROOT)))


def t_no_silent_return() -> None:
    root = tree({"core/turn/entry.py": """
        import logging
        log = logging.getLogger(__name__)
        def good(ctx):
            if not ctx:
                log.info("no ctx")
                return None
            return None
        def quiet(ctx):
            if not ctx:
                return None
            return 1
    """})
    problems = C1.scan(root)
    check("正向：有日志的 return None 不算静默", not any("good(" in p for p in problems), str(problems))
    check("负向：没日志的 return None 被抓", any("quiet(" in p for p in problems), str(problems))
    check("真仓通过", C1.scan(ROOT) == [], str(C1.scan(ROOT)))


def t_single_catalog() -> None:
    root = tree({
        "core/tools/registry.py": "class ToolSpec:\n    pass\n",
        "core/tools/builtin.py": "from core.tools.registry import ToolSpec\ns = ToolSpec()\n",
        "agents/tools/x.py": "from core.tools.registry import ToolSpec\nt = ToolSpec()\n",
    })
    problems = C2.scan(root)
    check("负向：core/tools 外造 spec 被抓", any(p.startswith("A ") for p in problems), str(problems))

    dup = tree({"core/tools/registry.py": "class ToolSpec:\n    pass\n",
                "core/tools/other.py": "class ToolSpec:\n    pass\n"})
    problems = C2.scan(dup)
    check("负向：第二份 class ToolSpec 被抓", any(p.startswith("B ") for p in problems), str(problems))

    check("真仓通过", C2.scan(ROOT) == [], str(C2.scan(ROOT)))


def main() -> int:
    for fn in (t_reachability, t_no_silent_return, t_single_catalog):
        print(fn.__name__)
        fn()
    print(f"RESULT {len(FAILS)} failed / {COUNT} checks")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
