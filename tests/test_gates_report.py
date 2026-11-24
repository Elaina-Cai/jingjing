# -*- coding: utf-8 -*-
"""转正闸门总表检查：standalone + pytest 兼容。"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _mod():
    spec = importlib.util.spec_from_file_location("report_gates", ROOT / "scripts/report_gates.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_human_tone_gate():
    mod = _mod()
    events = [{"event": "human_tone_shadow", "model_like": False} for _ in range(29)]
    gate = next(g for g in mod.collect(events)["gates"] if g["name"] == "human_tone")
    assert gate["ready"] is False
    events += [{"event": "human_tone_shadow", "model_like": False}]
    gate = next(g for g in mod.collect(events)["gates"] if g["name"] == "human_tone")
    assert gate["ready"] is True
    events.append({"event": "human_tone_shadow", "model_like": True})
    gate = next(g for g in mod.collect(events)["gates"] if g["name"] == "human_tone")
    assert gate["ready"] is True or gate["current"].endswith("0.032")


def test_info_buckets():
    mod = _mod()
    events = [
        {"event": "llm_degradation", "stage": "planner", "failure": "invalid_steps"},
        {"event": "self_duplicate_draft"},
        {"event": "addressee_repaired", "from": "a", "to": "b", "applied": True},
    ]
    rep = mod.collect(events)
    assert rep["info"]["degradation"]["planner/invalid_steps"] == 1
    assert rep["info"]["continuity"]["self_duplicate_draft"] == 1
    assert rep["info"]["addressee"]["addressee_repaired"] == 1


def test_static_files_exist():
    assert (ROOT / "scripts/report_gates.py").exists()
    # 该文档在 4e2b8ce「删除 149 份过期文档」时被主动删掉，断言随之删除：
    # 门禁不该拿一份我们自己删掉的文档当凭据（要它回来，先写文档再写断言）。


TESTS = [
    test_human_tone_gate,
    test_info_buckets,
    test_static_files_exist,
]


def main() -> bool:
    ok = True
    for test in TESTS:
        try:
            test()
        except AssertionError as exc:
            print(f"FAIL {test.__name__}: {exc}")
            ok = False
        else:
            print(f"PASS {test.__name__}")
    if ok:
        print(f"gates-report checks: {len(TESTS)}/{len(TESTS)} passed")
    return ok


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
