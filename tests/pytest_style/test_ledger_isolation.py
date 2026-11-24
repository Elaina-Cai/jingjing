"""测试写流水不能落进生产账本。

2026-11-22 实测：直接跑全量时，`data/loop/ledger-2026-11-22.jsonl` 里出现了
`to c1: hello` 和 8 条重复的「同学你好，关于报到这件事我再说一遍…」——都是用例会话，
却会被「最近开口」当成她真说过的话。
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from core.infra import actions
from loop import ledger

ROOT = Path(__file__).resolve().parents[2]
PRODUCTION = ROOT / "data" / "loop"


def test_ledger_dir_is_redirected(tmp_path):
    assert Path(actions.DIR) != PRODUCTION
    assert Path(actions.DIR).is_relative_to(tmp_path) or str(tmp_path) in str(actions.DIR)


def test_appending_a_receipt_does_not_touch_the_production_ledger():
    before = sorted(os.listdir(PRODUCTION)) if PRODUCTION.exists() else []
    sizes = {name: os.path.getsize(PRODUCTION / name) for name in before}
    ledger.append_receipt(act="say", target="c1", why="isolation probe",
                          result="sent", turn_id="isolation-probe")
    after = sorted(os.listdir(PRODUCTION)) if PRODUCTION.exists() else []
    assert after == before
    for name in before:
        assert os.path.getsize(PRODUCTION / name) == sizes[name], name
    rows = [json.loads(line) for line in Path(actions.path()).read_text(encoding="utf-8").splitlines()]
    assert any(row.get("turn_id") == "isolation-probe" for row in rows)
