r"""给 bat 循环写一行退出账（§33.5 第 1 条，2026-11-05 主人批的 ①）。

bat 里没法拼 JSON（引号、中文、日期都要转义），所以让 bat 调这一句：
`python scripts\exit_note.py --role bat --rc %CODE% --detail "..."`。

两条纪律：**永远 exit 0**（账本坏了不许把 bat 循环搞死）、写不进去只在 stderr 说一句。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("--role", default="bat")
    ap.add_argument("--rc", default="")
    ap.add_argument("--why", default="")
    ap.add_argument("--pid", default="")
    ap.add_argument("--child", default="")
    ap.add_argument("--detail", default="")
    ap.add_argument("--log", default="", help="账本路径（默认 logs/exits.jsonl；测试用）")
    got = ap.parse_args(argv)
    rc: int | None
    try:
        rc = int(got.rc) if str(got.rc).strip() != "" else None
    except ValueError:
        rc = None
    try:
        pid = int(got.pid) if str(got.pid).strip() else None
    except ValueError:
        pid = None
    try:
        from core.infra import exitlog
        ok = exitlog.record(got.role, rc=rc, why=got.why, pid=pid, child=got.child,
                            detail=got.detail, **({"path": got.log} if got.log else {}))
        if not ok:
            print("退出账没写成（不影响主流程）", file=sys.stderr)
    except Exception as exc:  # noqa: BLE001 - 账本坏了不许把 bat 循环搞死
        print("退出账不可用：%r" % (exc,), file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
