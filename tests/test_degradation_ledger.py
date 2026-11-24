# -*- coding: utf-8 -*-
"""模型输出降级账本检查：standalone + pytest 兼容。"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.context import degradation as deg  # noqa: E402

ENV = "YINYUE_FEATURE_LLM_DEGRADATION"


def test_mode_default_off():
    old = os.environ.get(ENV)
    try:
        os.environ.pop(ENV, None)
        assert deg.degradation_mode() == "off"
        os.environ[ENV] = "shadow"
        assert deg.degradation_mode() == "shadow"
        os.environ[ENV] = "on"
        assert deg.degradation_mode() == "on"
    finally:
        if old is None:
            os.environ.pop(ENV, None)
        else:
            os.environ[ENV] = old


def test_off_is_silent():
    from structlog.testing import capture_logs

    old = os.environ.pop(ENV, None)
    try:
        with capture_logs() as logs:
            deg.note_degradation(stage="planner", failure="invalid_steps", fallback="clarify_plan")
        assert not [x for x in logs if x.get("event") == "llm_degradation"]
    finally:
        if old is not None:
            os.environ[ENV] = old


def test_shadow_logs_fields():
    from structlog.testing import capture_logs

    old = os.environ.get(ENV)
    os.environ[ENV] = "shadow"
    try:
        with capture_logs() as logs:
            deg.note_degradation(
                stage="n1", failure="degraded", fallback="keep",
                chat_id="c1", detail="multi",
            )
        hits = [x for x in logs if x.get("event") == "llm_degradation"]
        assert len(hits) == 1
        assert hits[0]["stage"] == "n1" and hits[0]["failure"] == "degraded"
        assert hits[0]["fallback"] == "keep" and hits[0]["chat_id"] == "c1"
        assert hits[0]["detail"] == "multi"
    finally:
        if old is None:
            os.environ.pop(ENV, None)
        else:
            os.environ[ENV] = old


def test_static_hooks_and_purity():
    src = (ROOT / "core/context/degradation.py").read_text(encoding="utf-8")
    for banned in ("sqlite3", "core.bootstrap", "requests", "httpx", "core.context.situation"):
        assert banned not in src, banned
    pl = (ROOT / "agents/planner.py").read_text(encoding="utf-8")
    assert "note_degradation" in pl and "invalid_steps" in pl
    # N1 整模块已退役（2026-10-23，2026-10-23 晚删外壳）：判意图那一路不再存在，
    # 降级账本只剩 planner 与 llm 两个真写点；这里改钉"宿主模块真的不在了"，
    # 免得将来有人把 judge 悄悄接回账本却被当成恢复。
    assert not (ROOT / "core/context/intent.py").exists(), "N1 宿主模块又回来了"
    cl = (ROOT / "llm/client.py").read_text(encoding="utf-8")
    assert "note_degradation" in cl
    assert (ROOT / "scripts/report_degradation.py").exists()


def test_report_collect_shapes():
    events = [
        {"event": "llm_degradation", "stage": "planner", "failure": "invalid_steps", "fallback": "clarify_plan", "chat_id": "c1"},
        {"event": "llm_degradation", "stage": "planner", "failure": "invalid_steps", "fallback": "clarify_plan", "chat_id": "c1"},
        {"event": "llm_degradation", "stage": "llm", "failure": "json_parse", "fallback": "caller_fallback", "chat_id": ""},
    ]
    spec = importlib.util.spec_from_file_location("report_degradation", ROOT / "scripts/report_degradation.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    rep = mod.collect(events)
    assert rep["total"] == 3
    assert rep["stage"]["planner"] == 2
    assert rep["pairs"]["planner/invalid_steps"] == 2
    assert rep["chats"] == {"c1": 2}


TESTS = [
    test_mode_default_off,
    test_off_is_silent,
    test_shadow_logs_fields,
    test_static_hooks_and_purity,
    test_report_collect_shapes,
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
        print(f"degradation-ledger checks: {len(TESTS)}/{len(TESTS)} passed")
    return ok


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
