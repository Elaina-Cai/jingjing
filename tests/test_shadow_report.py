# -*- coding: utf-8 -*-
"""shadow 收数报告脚本（``scripts/report_group_scene_shadow.py``）的读数回归。

只喂合成 JSONL：事件解析、坏行跳过、since 过滤、门槛读数。

2026-10-22：``reply_decision_shadow`` / ``addressee_repaired`` 两条审计线已无生产者
（R0 删门），报告侧整块退役，这里的用例与样本行一并删掉。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import report_group_scene_shadow as report  # noqa: E402


def _write_log(tmp_path: Path, rows: list[dict]) -> Path:
    path = tmp_path / "yinyue.log"
    lines = [json.dumps(row, ensure_ascii=False) for row in rows]
    lines += ["启动横幅（不是 JSON）", "{坏 JSON", ""]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def _sample_rows() -> list[dict]:
    return [
        {"event": "scene_read_shadow", "ok": True, "present": True, "chat_id": "c1",
         "roles": {"her": 0.8, "owner": 0.2, "others": 0.1, "room": 0.0},
         "mentioned_her": True, "genre": "任务", "owner_state": "中性",
         "targets": ["P2", "P3"], "updates_n": 2, "speakers": ["P2"],
         "timestamp": "2026-10-12T01:00:00Z"},
        {"event": "scene_read_shadow", "ok": False, "present": True, "chat_id": "c1",
         "problems": ["scene_read.roles 未知键 'bot'"],
         "timestamp": "2026-10-12T01:01:00Z"},
        {"event": "scene_read_shadow", "ok": False, "present": False, "chat_id": "c1",
         "timestamp": "2026-10-12T01:01:30Z"},
        {"event": "social_gate_hit", "mode": "shadow", "rules": ["wrong_side"],
         "blocked": False, "chat_id": "c1", "who": "P3",
         "timestamp": "2026-10-12T01:05:00Z"},
        {"event": "intention_tick_shadow", "chat_id": "c1", "due": 2,
         "keys": ["reminder:1@2026-10-12 08:00"], "kinds": ["remind"],
         "timestamp": "2026-10-12T01:06:00Z"},
        {"event": "output_gate", "mode": "on", "action": "rewrite",
         "rule": "repeat", "chat_id": "c1", "timestamp": "2026-10-12T01:08:00Z"},
        {"event": "reply_intent_resolved", "chat_id": "c1",
         "timestamp": "2026-10-12T01:09:00Z"},
        {"event": "scene_read_shadow", "ok": True, "present": True, "chat_id": "c2",
         "roles": {"her": 0.4, "owner": 0.0, "others": 0.0, "room": 0.6},
         "mentioned_her": False, "genre": "起哄", "owner_state": "被逗",
         "targets": ["P5"], "timestamp": "2026-10-11T23:59:00Z"},
    ]


def test_aggregates_shadow_events_and_skips_bad_lines(tmp_path: Path) -> None:
    path = _write_log(tmp_path, _sample_rows())
    stats = report.ShadowStats()
    for event in report.iter_events([str(path)]):
        stats.add(event)

    data = stats.as_dict()
    scene = data["scene"]
    assert scene["turns"] == 4 and scene["ok"] == 2
    assert scene["legal_rate"] == 0.5
    assert scene["present"] == 3 and scene["missing"] == 1 and scene["invalid"] == 1
    assert scene["problems"] == {"scene_read.roles 未知键 'bot'": 1}
    assert scene["multi_target_turns"] == 1
    assert scene["targets_top"] == {"P2": 1, "P3": 1, "P5": 1}
    assert scene["updates"] == 2
    assert scene["turns_with_speaker"] == 1
    assert scene["speakers_top"] == {"P2": 1}
    assert scene["mentioned_her"] == 1
    assert scene["role_means"] == {"her": 0.6, "owner": 0.1, "others": 0.05, "room": 0.3}
    assert scene["genres"] == {"任务": 1, "起哄": 1}

    assert "decision" not in data, "沉默裁决那半边已退役，不该再出现在读数里"

    tick = data["intention_tick"]
    assert tick["due"] == 2 and tick["kinds"] == {"remind": 1}

    existing = data["existing_lines"]
    assert "addressee_guard" not in existing, "老 @ 守卫已退役，审计线不该留读数"
    assert existing["output_gate"]["hits"] == 1
    assert existing["output_gate"]["modes"] == {"on": 1}
    assert existing["reply_intent"] == {"mode": "on", "resolved": 1}


def test_since_filter_and_threshold_verdicts(tmp_path: Path) -> None:
    path = _write_log(tmp_path, _sample_rows())

    only_1012 = list(report.iter_events([str(path)], since="2026-10-12"))
    assert len(only_1012) == 7, only_1012   # 8 条里 10-11 的那条 scene 被过滤

    stats = report.ShadowStats()
    for event in only_1012:
        stats.add(event)
    verdict = stats.verdict
    assert verdict["scene_legal_rate>=0.99"] == "fail"          # 0.5 < 0.99
    assert verdict["scene_turns>=200"] == "insufficient"
    assert verdict["tick_due>=50"] == "insufficient"


def test_render_text_has_no_retired_lines(tmp_path: Path) -> None:
    """渲染出的报告不再提已退役的两条审计线，但仍要给出既有线参考读数。"""
    path = _write_log(tmp_path, _sample_rows())
    stats = report.ShadowStats()
    for event in report.iter_events([str(path)]):
        stats.add(event)

    text = report.render_text(stats)
    assert "OUTPUT_GATE(on)" in text and "REPLY_INTENT(on)" in text
    assert "决策 tau" not in text
    assert "人工复核候选" not in text


def test_missing_log_file_is_not_an_error(tmp_path: Path) -> None:
    stats = report.ShadowStats()
    for event in report.iter_events([str(tmp_path / "nope.log")]):
        stats.add(event)
    assert stats.scene_total == 0 and stats.tick_total == 0
    assert report.main(["--log", str(tmp_path / "nope.log"), "--json"]) == 0


if __name__ == "__main__":
    import sys as _sys
    from pathlib import Path as _Path

    for _p in (str(_Path(__file__).resolve().parent.parent), str(_Path(__file__).resolve().parent)):
        if _p not in _sys.path:
            _sys.path.insert(0, _p)
    from _mini_pytest import run_all as _run_all

    _sys.exit(0 if _run_all(globals()) else 1)
