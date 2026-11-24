"""U-32 开机一行：L0 还差几天（24 项）。"""

from __future__ import annotations

import sys
import tempfile
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from core.observability import startup as S

FAILS: list[str] = []
COUNT = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global COUNT
    COUNT += 1
    if ok:
        print(f"  ok {name}")
    else:
        FAILS.append(f"{name} {detail}")
        print(f"  FAIL {name} {detail}")


def tmp_marker() -> Path:
    d = Path(tempfile.mkdtemp(prefix="dn_", dir=str(ROOT / ".test_tmp")))
    return d / ".daily_note"


def t_days_left() -> None:
    print("[t_days_left]")
    due = "2026-10-27"
    check("还有 7 天", S.days_left(due, today=date(2026, 10, 20)) == 7)
    check("今天到期是 0", S.days_left(due, today=date(2026, 10, 27)) == 0)
    check("过期是负", S.days_left(due, today=date(2026, 10, 30)) == -3)


def t_note_text() -> None:
    print("[t_note_text]")
    text = S.note_text(today=date(2026, 10, 22))
    check("还没到说还剩几天", "还剩 5 天" in text, text)
    check("带上到期日", "2026-10-27" in text, text)
    check("给出跑哪条命令", "stage_gate" in text, text)
    text = S.note_text(today=date(2026, 10, 27))
    check("当天说今天到期", "今天到期" in text, text)
    text = S.note_text(today=date(2026, 10, 30))
    check("过期说过期几天", "过期 3 天" in text, text)
    check("过期也催人看", "别等了" in text, text)


def t_marker() -> None:
    print("[t_marker]")
    m = tmp_marker()
    check("没写过读成空串", S.read_marker(m) == "")
    check("写成功", S.mark(m, today=date(2026, 10, 21)) is True)
    check("读回日期", S.read_marker(m) == "2026-10-21")
    check("父目录自动建", m.parent.exists())


def t_once_per_day() -> None:
    print("[t_once_per_day]")
    m = tmp_marker()
    lines: list[str] = []
    first = S.daily_note(today=date(2026, 10, 21), marker=m, log_fn=lines.append)
    check("第一次写出来", "还剩 6 天" in first, first)
    check("日志只一条", len(lines) == 1, str(lines))
    second = S.daily_note(today=date(2026, 10, 21), marker=m, log_fn=lines.append)
    check("同一天第二次空", second == "")
    check("同一天不再写日志", len(lines) == 1, str(lines))
    third = S.daily_note(today=date(2026, 10, 22), marker=m, log_fn=lines.append)
    check("第二天又写", "还剩 5 天" in third, third)
    check("日志变两条", len(lines) == 2, str(lines))


def t_never_raises() -> None:
    print("[t_never_raises]")
    m = tmp_marker()
    def boom(_line: str) -> None:
        raise RuntimeError("log 挂了")
    text = S.daily_note(today=date(2026, 10, 21), marker=m, log_fn=boom)
    check("日志抛异常仍有字", "还剩 6 天" in text, text)
    def bad_write(_line: str) -> None:
        raise OSError("磁盘满")
    text = S.daily_note(today=date(2026, 10, 21), marker=tmp_marker(), write=bad_write, log_fn=lambda _l: None)
    check("写盘抛异常不炸", bool(text))
    check("startup_note 吞一切", isinstance(S.startup_note(today=date(2026, 10, 21), marker=tmp_marker()), str))


def t_parity_with_gate() -> None:
    print("[t_parity_with_gate]")
    import l0_glance

    check("到期日与 l0_glance 一致", S.DUE_DATE == l0_glance.DUE_DATE, f"{S.DUE_DATE} vs {l0_glance.DUE_DATE}")
    check("最小天数一致", S.MIN_DAYS == l0_glance.MIN_DAYS, f"{S.MIN_DAYS} vs {l0_glance.MIN_DAYS}")
    src = (ROOT / "scheduler/main.py").read_text(encoding="utf-8")
    check("启动处真的调了", "startup_note()" in src)
    check("包在 try 里（启动不被阻断）", "startup_note" in src and "except Exception" in src)
    gate = (ROOT / "scripts/stage_gate.py").read_text(encoding="utf-8")
    check("闸脚本仍复用 l0_glance.verdict", "l0_glance.verdict" in gate)


def main() -> int:
    t_days_left()
    t_note_text()
    t_marker()
    t_once_per_day()
    t_never_raises()
    t_parity_with_gate()
    print()
    print(f"RESULT {len(FAILS)} failed / {COUNT} checks")
    for f in FAILS:
        print("  -", f)
    return 0 if not FAILS else 1


if __name__ == "__main__":
    raise SystemExit(main())
