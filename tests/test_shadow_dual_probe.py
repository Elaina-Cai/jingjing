# -*- coding: utf-8 -*-
"""影子双轨探针 v1（scripts/shadow_dual_probe.py）的离线回归：不读生产库、不调 LLM。

覆盖：
  1) spec 校验（缺字段 / 错枚举 / 错 JSON / 文件不存在）；
  2) 确定性等距抽样；
  3) B 相对 A 的方向判据（±2 字同长、空回复）；
  4) 两轨 prompt 组装（current/window、extras、system_mode=file/online_append）；
  5) 双轨执行：dry-run 0 调用；regen 每回合 2 次；real 省 A 调用；
  6) 读数：分母 0 → n/a、错误/空样本剔除、方向与变化率计数；
  7) 报告含关键段落与样例；两个示例 spec 可加载。

Run directly:
    python tests/test_shadow_dual_probe.py
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from _tempdirs import temp_base  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "shadow_dual_probe", ROOT / "scripts" / "shadow_dual_probe.py"
)
sdp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sdp)  # type: ignore[union-attr]

TMP = temp_base() / "shadow_dual_probe_test"
TMP.mkdir(parents=True, exist_ok=True)


def _track(context="current", system_mode="online", mode=None, **extra):
    track = {"context": context, "system_mode": system_mode,
             "temperature": 0.7, "max_tokens": 180}
    if mode:
        track["mode"] = mode
    track.update(extra)
    return track


def _spec_file(name="t_spec", baseline=None, candidate=None, extras=None):
    data = {"name": name, "notes": "测试候选",
            "baseline": baseline or _track(mode="regen"),
            "candidate": candidate or _track()}
    if extras is not None:
        data["extras"] = extras
    path = TMP / f"{name}.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


def _turn(chat_id="x@chatroom"):
    return {"chat_id": chat_id, "at": "2026-10-10 12:00", "trigger": "在吗",
            "reply": "在的", "is_group": chat_id.endswith("@chatroom")}


def _rows():
    return [
        {"dt": datetime(2026, 10, 10, 11, 50), "occurred_at": "2026-10-10 11:50:00",
         "speaker": "鲸鲸", "content": "早"},
        {"dt": datetime(2026, 10, 10, 11, 55), "occurred_at": "2026-10-10 11:55:00",
         "speaker": "阿明", "content": "在吗"},
    ]


def test_spec_validation():
    valid = sdp.load_spec(_spec_file())
    assert valid["name"] == "t_spec" and len(valid["_hash"]) == 12

    def _write(name, payload, raw=False):
        p = TMP / name
        p.write_text(payload if raw else json.dumps(payload, ensure_ascii=False),
                     encoding="utf-8")
        return p

    cases = [
        (_write("bad_name.json", {"baseline": {}, "candidate": {}}), "name"),
        (_write("bad_context.json", {"name": "x",
                                     "baseline": _track(),
                                     "candidate": _track(context="yolo")}), "context"),
        (_write("bad_mode.json", {"name": "x",
                                  "baseline": _track(mode="yolo"),
                                  "candidate": _track()}), "baseline.mode"),
        (_write("no_file.json", {"name": "x",
                                 "baseline": _track(),
                                 "candidate": _track(system_mode="online_append")}),
         "system_file"),
        (_write("missing_file.json", {"name": "x",
                                      "baseline": _track(),
                                      "candidate": _track(system_mode="file",
                                                          system_file="no_such.txt")}),
         "不存在"),
        (_write("bad_json.json", "{nope", raw=True), "JSON"),
        (TMP / "no_spec.json", "spec 不存在"),
    ]
    for path, needle in cases:
        try:
            sdp.load_spec(path)
            raise AssertionError(f"{path} 应当报错")
        except ValueError as exc:
            assert needle in str(exc), (path, needle, str(exc))


def test_even_sample_is_deterministic():
    rows = [{"i": i} for i in range(10)]
    assert [r["i"] for r in sdp._even_sample(rows, 3)] == [0, 3, 6]
    assert sdp._even_sample(rows, 3) == sdp._even_sample(rows, 3)
    assert sdp._even_sample(rows, 0) == []
    assert sdp._even_sample([], 5) == []
    assert len(sdp._even_sample([{"i": 1}], 5)) == 1


def test_direction_boundaries():
    assert sdp._direction("你好", "你好啊") == "same"
    assert sdp._direction("你好", "你好啊啊啊") == "longer"
    assert sdp._direction("你好啊啊啊", "你好") == "shorter"
    assert sdp._direction("", "在") == "longer"
    assert sdp._direction("在", "") == "shorter"
    assert sdp._direction("", "") == "both_empty"


def test_compose_track_current_window_and_extras():
    spec = sdp.load_spec(_spec_file(
        candidate=_track(context="window"),
        extras={"x@chatroom": {"group_baseline": "这是群基线",
                               "stance": "她的立场X",
                               "person_notes": {"鲸鲸": "在场备注"}}}))
    turn, rows = _turn(), _rows()
    extras = spec["extras"].get("x@chatroom")
    a = sdp.compose_track(spec["baseline"], turn, "PERSONA", rows, extras)
    b = sdp.compose_track(spec["candidate"], turn, "PERSONA", rows, extras)
    a_sys, b_sys = a[0]["content"], b[0]["content"]
    assert "PERSONA" in a_sys and "[最近消息·仅作上下文]" in a_sys
    assert "[群聊规则]" in a_sys and "[最近对话·原文]" not in a_sys
    assert "[最近对话·原文]" in b_sys and "这是群基线" in b_sys
    assert "[她的立场]" in b_sys and "鲸鲸：在场备注" in b_sys
    assert turn["trigger"] in a[1]["content"] and turn["trigger"] in b[1]["content"]


def test_resolve_system_file_modes():
    f = TMP / "append.txt"
    f.write_text("追加条款", encoding="utf-8")
    append = _track(system_mode="online_append", system_file=str(f))
    text = sdp.resolve_system(append, "PERSONA")
    assert "PERSONA" in text and "追加条款" in text
    replace = _track(system_mode="file", system_file=str(f))
    assert sdp.resolve_system(replace, "PERSONA") == "追加条款"
    assert sdp.resolve_system(_track(), "PERSONA") == "PERSONA"


def test_run_probe_dry_run_makes_no_calls():
    calls: list = []

    def boom(*args, **kwargs):
        calls.append(1)
        raise AssertionError("dry-run 不许调 LLM")

    spec = sdp.load_spec(_spec_file())
    out = sdp.run_probe([_turn()], spec, "PERSONA", boom, dry_run=True,
                        rows_provider=lambda cid: _rows())
    assert calls == [] and len(out) == 1
    assert out[0]["a_msgs"] and out[0]["b_msgs"]
    assert out[0]["a_text"] == "" and out[0]["a_error"] is None


def test_run_probe_regen_calls_both_tracks():
    seen: list = []

    def fake(messages, **params):
        seen.append(messages)
        return "候选回复"

    spec = sdp.load_spec(_spec_file(candidate=_track(context="window")))
    out = sdp.run_probe([_turn()], spec, "PERSONA", fake,
                        rows_provider=lambda cid: _rows())
    assert len(seen) == 2 and out[0]["a_source"] == "regen"
    assert "[最近消息·仅作上下文]" in seen[0][0]["content"]
    assert "[最近对话·原文]" in seen[1][0]["content"]
    assert out[0]["a_text"] == "候选回复" and out[0]["b_text"] == "候选回复"
    assert out[0]["a_error"] is None and out[0]["b_error"] is None


def test_run_probe_real_baseline_skips_a_call():
    seen: list = []

    def fake(messages, **params):
        seen.append(messages)
        return "B 回复"

    spec = sdp.load_spec(_spec_file(baseline=_track(mode="real")))
    out = sdp.run_probe([_turn()], spec, "PERSONA", fake,
                        rows_provider=lambda cid: _rows())
    assert len(seen) == 1
    assert out[0]["a_source"] == "real" and out[0]["a_text"] == "在的"
    assert out[0]["b_text"] == "B 回复"


def test_metrics_counts_and_n_a():
    orig_canned, orig_residual = sdp._canned_hits, sdp._residual_hits
    sdp._canned_hits = lambda t: (["套"] if "套" in t else [])
    sdp._residual_hits = lambda t: (["尾"] if "尾" in t else [])
    try:
        rows = [
            {"a_text": "平", "b_text": "平", "a_error": None, "b_error": None,
             "reply": "真实一"},
            {"a_text": "带套的话", "b_text": "短", "a_error": None, "b_error": None,
             "reply": "真实二长一点"},
            {"a_text": "", "b_text": "只有B", "a_error": None, "b_error": None,
             "reply": "真实三"},
            {"a_text": "A好", "b_text": "B好", "a_error": "<err>", "b_error": None,
             "reply": "真实四"},
        ]
        m = sdp.compute_metrics(rows)
        assert m["n_total"] == 4 and m["n_both_ok"] == 2 and m["n_errors"] == 1
        assert m["dir_same"] == 1 and m["dir_shorter"] == 1
        assert m["changed"] == 1
        assert m["canned_a"] == 1 and m["canned_b"] == 0
        assert m["real_n"] == 2

        empty = sdp.compute_metrics([{"a_text": "", "b_text": "", "a_error": None,
                                      "b_error": None, "reply": ""}])
        assert empty["n_both_ok"] == 0 and empty["len_a_median"] is None
        assert sdp._pct(0, 0) == "n/a" and sdp._fmt_num(None) == "n/a"
    finally:
        sdp._canned_hits, sdp._residual_hits = orig_canned, orig_residual


def test_report_contains_sections_and_samples():
    spec = sdp.load_spec(_spec_file())
    results = [{"index": 1, "at": "2026-10-10 12:00", "chat_id": "x@chatroom",
                "is_group": True, "trigger": "在吗", "reply": "在的",
                "a_text": "A文本", "b_text": "B文本", "a_error": None, "b_error": None,
                "a_msgs": [], "b_msgs": [], "a_source": "regen"}]
    metrics = sdp.compute_metrics(results)
    report = sdp.build_report(results, metrics, spec, "deepseek-chat", calls=2)
    for needle in ("# 影子双轨探针", "逐项读数", "逐回合样例", "噪声地板",
                   "n/a", "A文本", "B文本", "在吗"):
        assert needle in report, needle


def test_example_specs_load():
    for name in ("example_null", "example_window_candidate"):
        spec = sdp.load_spec(ROOT / "configs" / "shadow_variants" / f"{name}.json")
        assert spec["name"] and spec["_hash"]


def main() -> int:
    tests = [
        test_spec_validation,
        test_even_sample_is_deterministic,
        test_direction_boundaries,
        test_compose_track_current_window_and_extras,
        test_resolve_system_file_modes,
        test_run_probe_dry_run_makes_no_calls,
        test_run_probe_regen_calls_both_tracks,
        test_run_probe_real_baseline_skips_a_call,
        test_metrics_counts_and_n_a,
        test_report_contains_sections_and_samples,
        test_example_specs_load,
    ]
    for test in tests:
        test()
    print(f"ALL PASSED ({len(tests)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
