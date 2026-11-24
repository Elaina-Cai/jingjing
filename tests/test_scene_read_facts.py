# -*- coding: utf-8 -*-
"""Step 2：scene_read 事实层纯函数测试。"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.context.scene_read import build_scene_facts  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
INCIDENT = ROOT / "tests" / "golden" / "threads" / "incident_1011.jsonl"


def _rows() -> dict[str, dict]:
    out = {}
    for line in INCIDENT.read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            out[row["id"]] = row
    return out


def test_at_target_from_canonical_owner() -> None:
    row = _rows()["t2006"]
    facts = build_scene_facts(row)
    assert facts.at_targets == ("P2",)
    assert facts.explicit_addressee == ("P2",)


def test_quote_author_and_explicit_addressee() -> None:
    row = _rows()["t2012"]
    facts = build_scene_facts(row)
    assert facts.quote_author == "P2"
    assert facts.quote_unresolved is False
    assert "P2" in facts.explicit_addressee


def test_self_reference_has_no_explicit_addressee() -> None:
    row = _rows()["t2013"]
    facts = build_scene_facts(row)
    assert facts.explicit_addressee == ()
    assert facts.quote_author == ""


def test_echo_has_no_explicit_addressee() -> None:
    row = _rows()["t2021"]
    facts = build_scene_facts(row)
    assert facts.explicit_addressee == ()


def test_share_title_has_no_explicit_addressee() -> None:
    row = _rows()["t2001"]
    facts = build_scene_facts(row)
    assert facts.explicit_addressee == ()
    assert facts.quote_unresolved is False


def test_pat_target_from_canonical_owner() -> None:
    facts = build_scene_facts({"speaker": "P3", "text": '"P3" 拍了拍 "P5"'})
    assert facts.pat_targets == ("P5",)
    assert facts.explicit_addressee == ("P5",)


def test_unresolved_quote_is_not_an_author() -> None:
    facts = build_scene_facts({"speaker": "P3", "text": "[引用〔作者不明〕: 嗯] 好的"})
    assert facts.quote_author == ""
    assert facts.quote_unresolved is True
    assert facts.explicit_addressee == ()


def test_parent_ref_is_read_but_never_invented() -> None:
    facts = build_scene_facts(
        {"speaker": "P3", "text": "好的", "refs": {"parent_event_id": "e-1"}}
    )
    assert facts.parent_event_id == "e-1"
    assert facts.reply_to == ""
    assert facts.explicit_addressee == ()


def test_window_and_thread_result_are_read_only() -> None:
    row = _rows()["t2001"]
    facts = build_scene_facts(
        row,
        window_rows=[row],
        thread_result={"topic": "聊天记录", "members": ["P1", "P2"]},
    )
    assert facts.unit_summary == "聊天记录"
    assert facts.unit_members == ("P1", "P2")

if __name__ == "__main__":
    import sys as _sys
    from pathlib import Path as _Path

    for _p in (str(_Path(__file__).resolve().parent.parent), str(_Path(__file__).resolve().parent)):
        if _p not in _sys.path:
            _sys.path.insert(0, _p)
    from _mini_pytest import run_all as _run_all

    _sys.exit(0 if _run_all(globals()) else 1)