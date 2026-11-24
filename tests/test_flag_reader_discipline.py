#!/usr/bin/env python
"""WP-02 guard: three-state flags must be read with features.mode().

The static audit (`scripts/audit_flags.py`) is the real check; this file makes
it a test so `scripts/run_all_tests.py` fails when the discipline regresses.
It also proves the runtime hard stop is active, using a *dynamic* flag name so
the static guard does not need a whitelist for this file.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for extra in (str(ROOT), str(ROOT / "scripts")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

from core import features  # noqa: E402
import audit_flags  # noqa: E402


def main() -> bool:
    ok = True
    for rel, line, message in audit_flags.static_violations():
        print(f"STATIC {rel}:{line} {message}")
        ok = False
    for name, _line, message in audit_flags.registry_violations(False):
        print(f"REGISTRY {name} {message}")
        ok = False

    # 硬拦截必须真的生效；变量名保持动态，避免被当成误读。
    tri_name = "semantic_unit"
    try:
        features.enabled(tri_name)
        print("FAIL: features.enabled() 没有拦住三态 flag")
        ok = False
    except ValueError:
        pass

    if len(features.TRI_STATE_FLAGS) != len(features.FLAG_REGISTRY):
        print("FAIL: FLAG_REGISTRY 里出现非 tri 条目")
        ok = False
    # 2026-10-24 记数：三态旗标 62。
    # 更早的链条（23 → 70）见 git 历史；2026-10-22/23 删门之后：
    #   删门 -5（group_social_gate / grounding_guard / react_playbook_group /
    #     addressee_guard / media_integrity / reminder_dedup）-> 87；
    #   恒真收编 -5（identity_active / terminal_recording_enabled / event_stream_enabled /
    #     memory_gateway_enabled / persona_brain_enabled）-> 83；
    #   删正经事信号 SERIOUS_SIGNALS -> 82；删 SILENCE_CONSUMED -> 81；
    #   N1 退役 -7（turn_ownership / room_episode / reply_intent / addressee_contract /
    #     六个 addressee 旗标 / unknown_first_class / addressed_to_me_lens 合计）-> 68；
    #   读场 + 按需记忆整族 -5（read_block / needs_memory_inject / recall_in_window /
    #     memory_tool_visibility / memory_tool_fallback）-> 62；动因 veto 随 speak_drive 整链退役 -> 62。
    #   2026-11-03 P1-1 销账 +1（OPEN_LOOP_CLOSE，off/shadow/on，退役死线 2027-06-30）-> 58。
    if len(features.TRI_STATE_FLAGS) != 59:
        print(
            f"FAIL: 三态 flag 数量 {len(features.TRI_STATE_FLAGS)} != 59  "
            "新增必须一进一出并同步本测试（执行手册 §12.1）"
        )
        ok = False

    if ok:
        print(f"flag reader discipline: ok ({len(features.TRI_STATE_FLAGS)} tri-state flags)")
    return ok


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
