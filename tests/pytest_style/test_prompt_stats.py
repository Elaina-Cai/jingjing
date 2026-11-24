"""`scripts/prompt_stats.py` 的账要算对：形状判定、段出现率、沉默理由的空值统计。"""
from __future__ import annotations

import importlib.util
import json
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _load():
    spec = importlib.util.spec_from_file_location("prompt_stats", ROOT / "scripts" / "prompt_stats.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def _turn(root: Path, name: str, *, sysmsg: str, user: str, rounds: int = 2) -> None:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "request.json").write_text(
        json.dumps({"rounds": [{"messages": [{"role": "system", "content": sysmsg},
                                             {"role": "user", "content": user}],
                                "tools": [{"name": "say"}]}] * rounds},
                   ensure_ascii=False),
        encoding="utf-8",
    )


def test_shape_says_v2_when_a_daily_section_is_in_system(tmp_path: Path) -> None:
    mod = _load()
    _turn(tmp_path, "a", sysmsg="这三天的账 400", user="此刻")
    _turn(tmp_path, "b", sysmsg="人设", user="这三天的账 400")
    _turn(tmp_path, "c", sysmsg="人设", user="此刻")
    turns = mod.load_turns(tmp_path, days=1)
    assert [t["shape"] for t in turns] == ["v2", "v1", "unknown"]


def test_summary_counts_rates_and_silent_why(tmp_path: Path, tmp_path_factory) -> None:
    mod = _load()
    _turn(tmp_path, "a", sysmsg="这三天的账", user="此刻 以下是这个世界里发生的事")
    trace = tmp_path / ("trace-" + datetime.now().strftime("%Y-%m-%d") + ".jsonl")
    rows = [
        {"outcome": {"silent": True, "silent_why": "她觉得没什么要说的"}},
        {"outcome": {"silent": True, "silent_why": ""}},
        {"outcome": {"silent": False, "said": ["嗯"], "blocked": ["x"]}},
    ]
    trace.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows), encoding="utf-8")
    turns = mod.load_turns(tmp_path, days=1)
    res = mod.summarize(turns, mod.load_trace(tmp_path, days=1))
    assert res["turns"] == 1
    assert res["shape"] == {"v2": 1}
    assert res["daily_rate"] == 1.0 and res["e1_rate"] == 1.0
    assert res["trace_turns"] == 3 and res["silent_turns"] == 2
    assert res["said_turns"] == 1 and res["blocked"] == 1
    assert res["silent_why_empty"] == 1
    assert res["silent_why_top"][0][1] == 1


def test_render_is_one_block_per_line(tmp_path: Path) -> None:
    mod = _load()
    res = mod.summarize([], [])
    out = mod.render(res)
    assert "turns=0" in out and "silent_why" in out
    lines = out.splitlines()
    assert len(lines) == 7, lines   # turns/chars/sections/behavior/silent_why/empty/...
    assert any(l.startswith("empty:") for l in lines)


def test_empty_turns_are_not_silences():
    """silent=None（没轮到窗口）不能算进「她选择不说」，否则沉默率永远虚高。"""
    from scripts.prompt_stats import summarize

    trace = [
        {"at": "1", "outcome": {"silent": None, "said": [], "blocked": []}},
        {"at": "2", "outcome": {"silent": True, "said": [], "blocked": [],
                                "silent_why": "刚说过，不必再说"}},
        {"at": "3", "outcome": {"silent": False, "said": ["嗯"], "blocked": []}},
    ]
    res = summarize([], trace)
    assert res["trace_turns"] == 3
    assert res["empty_turns"] == 1
    assert res["silent_turns"] == 1
    assert res["said_turns"] == 1
