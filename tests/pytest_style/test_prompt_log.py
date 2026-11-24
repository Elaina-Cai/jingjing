"""提示词请求台账的锁（施工文档 Step 2）。

钉住四件事：
1. 没有 turn 就不写文件（单测/验证脚本不污染台账）；
2. 每一轮一条记录，`i` 从 1 递增，`messages`/`tools` 原样保存；
3. `tools` 为空也要有记录（note=no_tools），查看器必须明说「tools 为空」；
4. 记录点在 llm 客户端的**早退之前**，`loop/brain.py` 每拍都绑定 turn。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from core.infra import promptlog  # noqa: E402

TOOLS = [
    {"type": "function", "function": {"name": "say", "parameters": {}}},
    {"type": "function", "function": {"name": "look", "parameters": {}}},
    {"type": "function", "function": {"name": "do", "parameters": {}}},
]


def _msgs(n: int = 2) -> list[dict]:
    return [{"role": "system", "content": "s" * 30}, {"role": "user", "content": "u" * 12}][:n]


def test_without_turn_nothing_is_written(tmp_path, monkeypatch):
    monkeypatch.setenv("YINYUE_PROMPT_DIR", str(tmp_path))
    promptlog.set_turn("")
    assert promptlog.record(messages=_msgs(), tools=TOOLS) is None
    assert not list(tmp_path.rglob("request.json"))


def test_rounds_are_appended_with_index(tmp_path, monkeypatch):
    monkeypatch.setenv("YINYUE_PROMPT_DIR", str(tmp_path))
    promptlog.set_turn("t1")
    first = promptlog.record(provider="p", model="m", params={"temperature": 0.7},
                             messages=_msgs(), tools=TOOLS)
    second = promptlog.record(provider="p", model="m", messages=_msgs() + [{"role": "tool", "content": "ok"}],
                              tools=TOOLS)
    assert first and second and Path(first) == Path(second)
    data = json.loads(Path(first).read_text(encoding="utf-8"))
    assert data["turn_id"] == "t1"
    assert [r["i"] for r in data["rounds"]] == [1, 2]
    assert [len(r["messages"]) for r in data["rounds"]] == [2, 3]
    assert data["rounds"][0]["params"]["temperature"] == 0.7


def test_tools_are_stored_verbatim(tmp_path, monkeypatch):
    monkeypatch.setenv("YINYUE_PROMPT_DIR", str(tmp_path))
    promptlog.set_turn("t2")
    dst = promptlog.record(messages=_msgs(), tools=TOOLS)
    data = json.loads(Path(dst).read_text(encoding="utf-8"))
    got = data["rounds"][0]["tools"]
    assert len(got) == len(TOOLS)
    assert [t["function"]["name"] for t in got] == ["say", "look", "do"]


def test_empty_tools_are_recorded_not_skipped(tmp_path, monkeypatch):
    monkeypatch.setenv("YINYUE_PROMPT_DIR", str(tmp_path))
    promptlog.set_turn("t3")
    dst = promptlog.record(messages=_msgs(), tools=[])
    data = json.loads(Path(dst).read_text(encoding="utf-8"))
    assert data["rounds"][0]["tools"] == []
    assert data["rounds"][0]["note"] == "no_tools"


def test_write_failure_returns_none_and_does_not_raise(tmp_path, monkeypatch):
    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setenv("YINYUE_PROMPT_DIR", str(blocker / "inside"))
    promptlog.set_turn("t4")
    assert promptlog.record(messages=_msgs(), tools=TOOLS) is None


def test_client_records_before_the_no_tools_early_return():
    src = (ROOT / "llm" / "client.py").read_text(encoding="utf-8")
    body = src[src.index("async def _routed_chat_tools"):]
    assert "promptlog.record(" in body
    assert body.index("promptlog.record(") < body.index('if not tools:')


def test_brain_binds_a_turn_each_beat():
    src = (ROOT / "loop" / "brain.py").read_text(encoding="utf-8")
    assert "promptlog.set_turn(turn_id)" in src
    # 试衣间不写台账（与 trace 同口径）
    idx = src.index("promptlog.set_turn(turn_id)")
    assert "if not rehearse:" in src[max(0, idx - 200):idx]


def test_viewer_says_tools_empty(tmp_path):
    env = dict(os.environ)
    env["YINYUE_PROMPT_DIR"] = str(tmp_path)
    env["PYTHONIOENCODING"] = "utf-8"
    target = tmp_path / "t9" / "request.json"
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps({"turn_id": "t9", "rounds": [
        {"i": 1, "provider": "p", "model": "m", "params": {}, "note": "no_tools",
         "messages": _msgs(), "tools": []}]}, ensure_ascii=False), encoding="utf-8")
    out = subprocess.run([sys.executable, str(ROOT / "scripts" / "prompt_show.py"), "t9"],
                         cwd=str(ROOT), env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
    assert out.returncode == 0, out.stderr[-400:]
    assert "tools 为空" in out.stdout
    assert "no_tools" in out.stdout


def test_viewer_shows_rounds_and_roles(tmp_path):
    env = dict(os.environ)
    env["YINYUE_PROMPT_DIR"] = str(tmp_path)
    env["PYTHONIOENCODING"] = "utf-8"
    ta = tmp_path / "ta" / "request.json"
    ta.parent.mkdir(parents=True)
    ta.write_text(json.dumps({"turn_id": "ta", "rounds": [
        {"i": 1, "provider": "p", "model": "m", "params": {"temperature": 0.7},
         "messages": _msgs(), "tools": TOOLS},
        {"i": 2, "provider": "p", "model": "m", "params": {},
         "messages": _msgs() + [{"role": "tool", "content": "ok", "tool_call_id": "c1"}], "tools": TOOLS},
    ]}, ensure_ascii=False), encoding="utf-8")
    tb = tmp_path / "tb" / "request.json"
    tb.parent.mkdir(parents=True)
    tb.write_text(json.dumps({"turn_id": "tb", "rounds": [
        {"i": 1, "provider": "p", "model": "m", "params": {},
         "messages": _msgs() + [{"role": "assistant", "content": "x"}], "tools": TOOLS},
    ]}, ensure_ascii=False), encoding="utf-8")
    run = [sys.executable, str(ROOT / "scripts" / "prompt_show.py")]
    out = subprocess.run(run + ["ta"], cwd=str(ROOT), env=env, capture_output=True,
                         text=True, encoding="utf-8", errors="replace", timeout=120)
    assert out.returncode == 0, out.stderr[-400:]
    assert "比上一轮 +1 条" in out.stdout, out.stdout
    out = subprocess.run(run + ["--diff", "ta", "tb"], cwd=str(ROOT), env=env, capture_output=True,
                         text=True, encoding="utf-8", errors="replace", timeout=120)
    assert out.returncode == 0, out.stderr[-400:]
    assert "轮 1" in out.stdout and "messages 2 -> 3" in out.stdout, out.stdout
