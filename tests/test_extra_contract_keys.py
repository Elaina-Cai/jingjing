from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

FAILS: list[str] = []
COUNT = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global COUNT
    COUNT += 1
    if ok:
        print(f"  ok  {name}")
    else:
        FAILS.append(name)
        print(f"  FAIL {name} {detail}")


def main() -> int:
    from core.routing.extra_contract import EXTRA_CONTRACT
    from scripts.audit_extra_contract import TARGETS, scan_file, scan_source

    # 1) 这次补登记的 9 个键
    wanted = (
        "job_readouts",
        "job_readout_shadow",
        "job_approvals",
        "job_approval_receipts",
        "job_approvals_granted",
        "job_grant_revoked",
        "job_grant_scope",
        "job_handoff",
        "senses_block",
    )
    for key in wanted:
        check(f"契约里有 {key}", key in EXTRA_CONTRACT, f"契约 {len(EXTRA_CONTRACT)} 键")

    # 2) 三个写点都在审计范围内（否则这条闸会重新长霉）
    names = {p.name for p in TARGETS}
    for name in ("extra_contract.py", "proactive_trigger.py", "outbound.py",
                 "job_readout.py", "snapshot.py"):
        check(f"审计范围含 {name}", name in names, sorted(names))

    # 3) 现状：所有目标文件零未登记键
    unregistered = []
    for path in TARGETS:
        if not path.exists():
            continue
        for key in sorted(scan_file(path)):
            if key not in EXTRA_CONTRACT:
                unregistered.append(f"{path.name}:{key}")
    check("现状零未登记键", not unregistered, str(unregistered[:6]))

    # 4) 扫描器真的能抓到违规（否则这条闸是恒绿）
    fake = 'def f(extra):\n    extra["totally_new_key"] = 1\n'
    check("扫描器抓得到新键", scan_source(fake) == {"totally_new_key"}, str(scan_source(fake)))

    # 5) 运行期：登记键不抛，未登记键抛（strict 是默认档）
    os.environ.pop("YINYUE_EXTRA_CONTRACT_MODE", None)
    from agents.base import ExtraContractViolation, ExtraDict

    box = ExtraDict()
    ok = True
    try:
        for key in wanted:
            box[key] = 1
    except Exception as exc:  # noqa: BLE001
        ok = False
        detail = f"{type(exc).__name__}: {exc}"
    else:
        detail = ""
    check("运行期登记键不抛", ok, detail)

    raised = False
    try:
        box["totally_new_key"] = 1
    except ExtraContractViolation:
        raised = True
    except Exception:  # noqa: BLE001
        raised = False
    check("运行期未登记键被拒", raised)

    print(f"RESULT {len(FAILS)} failed / {COUNT} checks")
    return 0 if not FAILS else 1


if __name__ == "__main__":
    raise SystemExit(main())
