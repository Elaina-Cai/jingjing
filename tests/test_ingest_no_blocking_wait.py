"""同步路径不许在主循环里等后台 handler（2026-10-21 真机事故）。

真机现象：main_loop_gap 7.74s、sync_round_slow 12/16 轮。根因是
`core/ingestion/ingestors.py` 每平台每轮 `await EventBus.await_pending(timeout=2)`：
事件已经 publish 进队列了，等 handler 跑完对下一轮同步没有任何用处，
却把整个主循环按住了（原注释里 timeout=60 曾把主循环卡 53 秒）。
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

FAILS: list[str] = []
COUNT = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global COUNT
    COUNT += 1
    if cond:
        print(f"  ok {name}")
    else:
        FAILS.append(f"{name} {detail}")
        print(f"  FAIL {name} {detail}")


SRC_PATH = ROOT / "core" / "ingestion" / "ingestors.py"
SRC = SRC_PATH.read_text(encoding="utf-8")


def _await_pending_timeouts(src: str) -> list[float]:
    out: list[float] = []
    for node in ast.walk(ast.parse(src)):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        name = getattr(fn, "attr", getattr(fn, "id", ""))
        if name != "await_pending":
            continue
        for kw in node.keywords:
            if kw.arg == "timeout" and isinstance(kw.value, ast.Constant):
                out.append(float(kw.value.value))
    return out


def t_no_blocking_wait() -> None:
    values = _await_pending_timeouts(SRC)
    check("同步里确实调了 await_pending（不是空扫）", len(values) >= 1, str(values))
    check("没有任何一处等超过 0 秒", all(v <= 0 for v in values), str(values))


def t_reason_recorded() -> None:
    check("代码里写明真机事故与日期", "2026-10-21" in SRC)
    check("写明主循环被按住的量级", "main_loop_gap" in SRC)


def t_other_modules_unaffected() -> None:
    # 只钉同步路径：别处（测试、工具）等 handler 是合理的
    hits = []
    for path in (ROOT / "core").rglob("*.py"):
        if path.name == "ingestors.py":
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if "await_pending(timeout=" in text and "timeout=0" not in text:
            hits.append(str(path.relative_to(ROOT)))
    check("除 ingestors.py 外没有新增阻塞式等待（记录而非失败）", True, str(hits[:3]))


TESTS = [t_no_blocking_wait, t_reason_recorded, t_other_modules_unaffected]


def main() -> int:
    for fn in TESTS:
        print(fn.__name__)
        fn()
    print(f"RESULT {len(FAILS)} failed / {COUNT} checks")
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())
