# -*- coding: utf-8 -*-
"""WP-06 §6.2 步骤3 回归：extra 键契约审计脚本必须可跑、可红。

* 真实仓库跑 ``scripts/audit_extra_contract.py`` → exit 0；
* 注一个未登记键到临时源文件 → ``audit()`` 必须把它列进 unregistered；
* 契约类型/覆盖率（扫描键 ⊆ EXTRA_CONTRACT）。

Run directly: python tests/test_extra_contract.py
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SCRIPT = ROOT / "scripts" / "audit_extra_contract.py"
sys.path.insert(0, str(ROOT / "scripts"))


def test_script_exits_zero_on_real_repo() -> None:
    proc = subprocess.run(
        [sys.executable, "-u", str(SCRIPT)],
        cwd=str(ROOT),
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )
    assert proc.returncode == 0, proc.stdout[-800:]
    assert "PASSED" in proc.stdout, proc.stdout[-800:]


def test_unregistered_key_is_flagged() -> None:
    import audit_extra_contract as audit_mod
    from core.routing.extra_contract import EXTRA_CONTRACT

    tmp = ROOT / ".test_tmp" / f"extra_contract_{os.getpid()}.py"
    tmp.parent.mkdir(parents=True, exist_ok=True)
    try:
        tmp.write_text(
            'def fake(ctx):\n    ctx.extra["wp06_rogue_key"] = 1\n'
            '    return ctx.extra.get("wp06_rogue_get", 0)\n',
            encoding="utf-8",
        )
        written, unregistered, contract = audit_mod.audit([tmp])
        assert "wp06_rogue_key" in written, written
        assert "wp06_rogue_get" in written, written
        assert set(unregistered) >= {"wp06_rogue_key", "wp06_rogue_get"}, unregistered
        assert contract == set(EXTRA_CONTRACT)
    finally:
        if tmp.exists():
            tmp.unlink()


def test_contract_is_registered_frozenset() -> None:
    from core.routing import extra_contract as tp

    assert isinstance(tp.EXTRA_CONTRACT, frozenset)
    assert len(tp.EXTRA_CONTRACT) >= 30
    for key in ("session_id", "world_state"):
        assert key in tp.EXTRA_CONTRACT, key
    for retired in ("should_send_reply", "reply_intent", "reply_intent_line",
                    "reply_intent_need_clarify", "content_plan", "_policy_decision",
                    "turn_intent", "unknown_answer", "trigger_reason",
                    "trigger_reasons", "defer_sticker_until_after_reply"):
        assert retired not in tp.EXTRA_CONTRACT, retired


def test_scan_covers_real_assembly_files() -> None:
    import audit_extra_contract as audit_mod
    from core.routing.extra_contract import EXTRA_CONTRACT

    written = audit_mod.scan_files()
    assert written, "扫描结果为空，路径坏了？"
    assert written <= set(EXTRA_CONTRACT), sorted(written - set(EXTRA_CONTRACT))


def main() -> bool:
    print("=" * 60)
    print("extra contract audit checks (WP-06 §6.2-3)")
    print("=" * 60)
    tests = (
        test_script_exits_zero_on_real_repo,
        test_unregistered_key_is_flagged,
        test_contract_is_registered_frozenset,
        test_scan_covers_real_assembly_files,
    )
    failed = 0
    for test in tests:
        try:
            test()
            print(f"  PASS {test.__name__}")
        except AssertionError as exc:
            failed += 1
            print(f"  FAIL {test.__name__}: {exc}")
        except Exception as exc:  # noqa: BLE001 - diagnostics only
            failed += 1
            import traceback

            traceback.print_exc()
            print(f"  FAIL {test.__name__}: {type(exc).__name__}: {exc}")
    print(f"{len(tests) - failed}/{len(tests)} passed")
    return failed == 0


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
