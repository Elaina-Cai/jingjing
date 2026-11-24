"""E0 验收（U-01）：判决行留痕 ``turn_trace``。

跑法：python -X utf8 -u tests/test_turn_trace.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import create_engine, text  # noqa: E402

from core import features  # noqa: E402
from core.routing import turn_pipeline as TP  # noqa: E402
from core.terminal import turn_trace as TT  # noqa: E402

ENV = "YINYUE_FEATURE_TURN_TRACE"
FAILS: list[str] = []

# 与 tests/_tempdirs.py 的 FALLBACK_BASE 同一处：单文件直跑时目录可能不存在，
# 不能假设「跑过别的测试就会建好」（2026-10-20 清地基后第一次单跑即 FileNotFoundError）。
TMP_ROOT = ROOT / ".test_tmp"
TMP_ROOT.mkdir(parents=True, exist_ok=True)


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"PASS {name}")
    else:
        print(f"FAIL {name} {detail}")
        FAILS.append(name)


def fresh_engine(with_table: bool = True):
    d = tempfile.mkdtemp(prefix="turn_trace_", dir=str(TMP_ROOT))
    eng = create_engine(f"sqlite:///{Path(d) / 'x.db'}", future=True)
    if with_table:
        with eng.begin() as c:
            c.execute(text(TT.CREATE_TABLE_SQL))
            c.execute(text(TT.CREATE_INDEX_SQL))
    return eng


def rows(eng) -> list:
    with eng.connect() as c:
        return c.execute(
            text("SELECT turn_id, chat_id, decision, basis, mode FROM turn_trace ORDER BY turn_id")
        ).fetchall()


def main() -> int:
    os.environ.pop(ENV, None)
    check("开关已登记", features.TURN_TRACE in features.TRI_STATE_FLAGS)
    # 2026-10-24 读场/按需记忆整族退役 → 63
    check("开关总数 59", len(features.TRI_STATE_FLAGS) == 57, str(len(features.TRI_STATE_FLAGS)))
    check("默认档位 off", features.FLAG_REGISTRY[features.TURN_TRACE].default == features.MODE_OFF)

    eng = fresh_engine()
    TT.set_engine(eng)

    ok = TT.record_turn_trace(turn_id="t-off", chat_id="c", decision="silent")
    check("off 一行不写", ok is False and rows(eng) == [], str(rows(eng)))

    os.environ[ENV] = "shadow"
    ok = TT.record_turn_trace(turn_id="t1", chat_id="c1", decision="silent")
    got = rows(eng)
    check("shadow 写进去", ok is True and len(got) == 1)
    check("行上带 mode=shadow", got and got[0][4] == "shadow", str(got))

    ok2 = TT.record_turn_trace(turn_id="t1", chat_id="c1", decision="respond", basis=["w#1"])
    check("重复 turn_id 是 no-op", ok2 is False and len(rows(eng)) == 1)

    TT.record_turn_trace(turn_id="t2", chat_id="c2", decision="respond", basis=["w#3", "m#2"])
    r2 = [r for r in TT.iter_turn_trace(since=datetime.now() - timedelta(minutes=5)) if r.turn_id == "t2"]
    check("basis 往返成数组", bool(r2) and r2[0].basis == ("w#3", "m#2"), str(r2))

    old = TT.record_turn_trace(
        turn_id="t-old", chat_id="c3", decision="silent", ts=datetime.now() - timedelta(days=9)
    )
    window = TT.iter_turn_trace(since=datetime.now() - timedelta(days=1))
    check("时间窗过滤", old is True and all(r.turn_id != "t-old" for r in window))

    TT.set_engine(fresh_engine(with_table=False))
    try:
        ok3 = TT.record_turn_trace(turn_id="t3", chat_id="c", decision="silent")
        raised = False
    except Exception:  # noqa: BLE001
        ok3, raised = False, True
    check("写失败只返回 False、不抛", raised is False and ok3 is False)

    # 接线：decide_turn 只在 route 分支写一行（静态 + 功能）
    src = (ROOT / "core/routing/turn_pipeline.py").read_text(encoding="utf-8")
    check("decide_turn 调唯一收口", src.count("_record_turn_verdict(facts, decision)") == 1)
    # 2026-10-23：relevance 阶段整族删除，这条改成负锁（不许长回来）
    check("relevance 阶段已删除", 'stage == "relevance"' not in src)

    eng2 = fresh_engine()
    TT.set_engine(eng2)

    class _D:
        action = "reply_now"

    class _F:
        chat_id = "fake"
        trace_id = "t-hook"
        inbound = None

    TP._record_turn_verdict(_F(), _D())
    got2 = rows(eng2)
    check("钩子写 respond", len(got2) == 1 and got2[0][2] == "respond", str(got2))
    check("钩子带 chat_id", got2 and got2[0][1] == "fake")

    # ---- U-03 两行制：完整留痕只写开口/动手 ----
    full_dir = Path(tempfile.mkdtemp(prefix="turn_trace_full_", dir=str(TMP_ROOT)))
    os.environ["YINYUE_TURN_TRACE_DIR"] = str(full_dir)
    TT.record_turn_trace(turn_id="t5", chat_id="c5", decision="silent", full=True)
    check("沉默回合不写完整留痕", list(full_dir.glob("*.jsonl")) == [])
    TT.record_turn_trace(
        turn_id="t6",
        chat_id="c5",
        decision="respond",
        basis=["w#1"],
        full=True,
        detail={"reading": "P5问课表，鲸鲸答过第 8 周"},
    )
    lines = []
    for fp in sorted(full_dir.glob("*.jsonl")):
        lines.extend(
            json.loads(x) for x in fp.read_text(encoding="utf-8").splitlines() if x.strip()
        )
    check("开口回合写一行完整留痕", len(lines) == 1, str(len(lines)))
    check(
        "完整留痕带 reading/turn_id/basis",
        bool(lines)
        and lines[0].get("turn_id") == "t6"
        and lines[0].get("basis") == ["w#1"]
        and str(lines[0].get("detail", {}).get("reading", "")).startswith("P5问课表"),
        str(lines)[:160],
    )
    os.environ.pop("YINYUE_TURN_TRACE_DIR", None)

    # 报表：off 档必须打印固定那一行并 exit 0
    env = dict(os.environ)
    env.pop(ENV, None)
    proc = subprocess.run(
        [sys.executable, "-X", "utf8", "-u", str(ROOT / "scripts/report_turn_trace.py"), "--days", "1"],
        capture_output=True, text=True, env=env, cwd=str(ROOT), timeout=120,
    )
    check("报表 off 输出", proc.returncode == 0 and "TURN_TRACE=off, 0 rows" in proc.stdout,
          (proc.stdout + proc.stderr)[:200])

    os.environ.pop(ENV, None)
    failed = len(FAILS)
    print(f"RESULT {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
