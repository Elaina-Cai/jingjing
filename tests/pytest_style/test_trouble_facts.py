"""CB-133：她「身上哪儿坏了」的那段事实，必须是一个写口 + 一句第一人称（2026-10-29）。

锁的不是措辞，是三条容易退化的性质：
1. 台账只有一个写口（`core/infra/trouble.py`），别处不许直接写 `trouble.json`；
2. 失败分类只许取自 `trouble.REASONS`，不许在调用点自己编词（编了就又看不懂了）；
3. 这句话每拍都在上下文里（`loop/brain.py` 真的要调一次 `render()`），否则等于没做。

反例（这段之前的形状）：语音失败全摞在一个 `cv3_request_failed` 事件里。日志看得出"失败了"，
看不出"哪个部件、为什么、多久了"；而她本人根本不看日志——所以对"她自己"来说什么都没发生。
"""
from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parents[1]
ROOT = TESTS_DIR.parent


def _trouble():
    import sys

    sys.path.insert(0, str(ROOT))
    from core.infra import trouble

    return trouble


def test_note_refuses_unregistered_area_and_reason(tmp_path):
    trouble = _trouble()
    with pytest.raises(ValueError):
        trouble.note("no_such_area", "connect_refused", path=tmp_path / "t.json")
    with pytest.raises(ValueError):
        trouble.note("voice", "made_up_reason", path=tmp_path / "t.json")


def test_same_reason_accumulates_but_keeps_since(tmp_path):
    trouble = _trouble()
    p = tmp_path / "t.json"
    trouble.note("voice", "connect_refused", "boom", path=p, now=1000.0)
    trouble.note("voice", "connect_refused", "boom", path=p, now=1060.0)
    item = trouble.snapshot(path=p)["voice"]
    assert item["tries"] == 2
    assert item["since"] == 1000.0
    assert item["at"] == 1060.0


def test_changed_reason_resets_counters(tmp_path):
    trouble = _trouble()
    p = tmp_path / "t.json"
    trouble.note("voice", "connect_refused", path=p, now=1000.0)
    trouble.note("voice", "boot_timeout", path=p, now=1100.0)
    item = trouble.snapshot(path=p)["voice"]
    assert item["reason"] == "boot_timeout"
    assert item["tries"] == 1
    assert item["since"] == 1100.0


def test_render_speaks_in_first_person_and_says_how_long(tmp_path):
    trouble = _trouble()
    p = tmp_path / "t.json"
    trouble.note("voice", "connect_refused", "ConnectionRefusedError: [WinError 10061]", path=p, now=1000.0)
    trouble.note("voice", "connect_refused", "ConnectionRefusedError: [WinError 10061]", path=p, now=1360.0)
    text = trouble.render(path=p, now=1360.0)
    assert "我这会儿发不出语音" in text
    assert "打字不受影响" in text   # 2026-10-31：她把"发不出声音"读成"整个说不出话"，主动闭嘴一下午
    assert trouble.REASONS["connect_refused"] in text
    assert "已经试过 2 次" in text
    assert "系统告诉我" in text and "不是我自己想出来的" in text
    assert "ConnectionRefusedError" in text


def test_render_is_empty_when_healthy(tmp_path):
    trouble = _trouble()
    p = tmp_path / "t.json"
    assert trouble.render(path=p) == ""
    trouble.note("voice", "server_error", path=p)
    assert trouble.render(path=p) != ""
    trouble.clear("voice", path=p)
    assert trouble.render(path=p) == ""


def test_broken_ledger_is_not_a_crash(tmp_path):
    trouble = _trouble()
    p = tmp_path / "t.json"
    p.write_text("{not json", encoding="utf-8")
    assert trouble.snapshot(path=p) == {}
    assert trouble.render(path=p) == ""


def test_ledger_has_exactly_one_writer(tmp_path):
    """别处不许直接碰 trouble.json。"""
    hits = []
    for path in sorted(ROOT.glob("**/*.py")):
        rel = path.relative_to(ROOT).as_posix()
        if rel.startswith(("tests/", ".venv", "venv", "archive/", "backups/", ".test_tmp/")) or "__pycache__" in rel:
            continue
        if "trouble.json" in path.read_text(encoding="utf-8", errors="replace"):
            hits.append(rel)
    assert hits == ["core/infra/trouble.py"], hits


def test_tts_failures_use_registered_reasons():
    """`core/speech/tts_cosyvoice.py` 里的 _fail(...) 只许用 REASONS 的键。"""
    trouble = _trouble()
    tree = ast.parse((ROOT / "core" / "speech" / "tts_cosyvoice.py").read_text(encoding="utf-8"))
    reasons = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "_fail":
            assert node.args, "无参 _fail"
            arg = node.args[0]
            assert isinstance(arg, ast.Constant) and isinstance(arg.value, str), ast.dump(arg)
            reasons.append(arg.value)
    assert reasons, "一条 _fail 都没找到：失败又被吞回去了"
    unknown = [r for r in reasons if r not in trouble.REASONS]
    assert not unknown, unknown


def test_brain_actually_shows_it():
    """接线要么真在，要么这段事实永远不进上下文。"""
    src = (ROOT / "loop" / "brain.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    calls = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr == "render" and isinstance(node.func.value, ast.Name):
                calls.append(node.func.value.id)
    assert "_trouble" in calls, calls
