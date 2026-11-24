# -*- coding: utf-8 -*-
"""坏 JSON 不许静默变成「没有」（CA-057）。

这份文件守的是一句话：**坏数据和「本来就没有」不是一回事**。
两者都回默认值，看起来一样，但后果相反：

- 小本本（notes）少一行，是少一条记性 —— 可以接受，只要数得出来；
- 语音账本（voice_gate）当空，等于「这条主人消息没换过声音」，于是**再发一次**。

所以读出三态（ok / missing / corrupt），闸门自己决定坏的时候收紧。

反例（改之前真在跑的形状）：
    got = read_json(MARKER, default={})      # 坏文件 == 空账本 == 允许再发
    data = json.loads(path.read_text())      # self_look 自造第三份「坏就当空」
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _src(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def _corrupt(tmp_path: Path) -> Path:
    p = tmp_path / "state.json"
    p.write_text('{"a": 1,', encoding="utf-8")    # 截断：断电或两个进程同写的典型残骸
    return p


# ---- 读口本身：三态分得开 -------------------------------------------------

def test_three_states_are_distinct(tmp_path):
    from core.infra import jsonio

    assert jsonio.read_json_checked(tmp_path / "nope.json", default={}) == ({}, "missing")
    good = tmp_path / "good.json"
    good.write_text('{"a": 1}', encoding="utf-8")
    assert jsonio.read_json_checked(good, default={}) == ({"a": 1}, "ok")
    assert jsonio.read_json_checked(_corrupt(tmp_path), default={}) == ({}, "corrupt")


def test_corrupt_file_is_backed_up_once(tmp_path):
    from core.infra import jsonio

    p = _corrupt(tmp_path)
    for _ in range(3):
        jsonio.read_json_checked(p, default={})
    assert len(list(tmp_path.glob("state.json.bad-*"))) == 1


def test_plain_reader_keeps_old_behaviour(tmp_path):
    from core.infra import jsonio

    assert jsonio.read_json(_corrupt(tmp_path), default={"d": 1}) == {"d": 1}


def test_unreadable_is_corrupt_not_missing(tmp_path):
    from core.infra import jsonio

    d = tmp_path / "adir.json"
    d.mkdir()                                   # read_text 会抛 OSError
    assert jsonio.read_json_checked(d, default=None)[1] == "corrupt"


# ---- 闸门：账本坏了就收紧 -------------------------------------------------

def test_voice_ledger_corrupt_is_visible(tmp_path, monkeypatch):
    from core.rules import voice_gate

    monkeypatch.setattr(voice_gate, "MARKER", str(_corrupt(tmp_path)))
    voice_gate.reset()
    assert voice_gate.used_keys() == frozenset()
    assert voice_gate.ledger_corrupt() is True


def test_voice_gate_blocks_when_ledger_corrupt(monkeypatch):
    from core.rules import voice_gate
    from loop import ports, voice_facts, voice_out

    monkeypatch.setattr(ports, "pick_db", lambda: "")
    monkeypatch.setattr(voice_facts, "facts",
                        lambda *a, **k: {"owner_msg_id": "m1", "chat_kind": "group", "who": "owner"})
    monkeypatch.setattr(voice_gate, "ledger_corrupt", lambda: True)
    assert voice_out._gate(platform="wechat", chat_id="c1") == (False, "ledger_corrupt", "m1")


# ---- 小本本：坏行跳过，但要数得出来 ---------------------------------------

def test_notes_bad_line_is_counted(tmp_path, monkeypatch):
    from loop import notes

    p = tmp_path / "notes.jsonl"
    p.write_text('{"kind": "note", "text": "好的"}\n{"kind": "note", \n', encoding="utf-8")
    monkeypatch.setattr(notes, "PATH", str(p))
    monkeypatch.setattr(notes, "_legacy_rows", lambda: [])   # 不掺仓库里的真数据
    monkeypatch.setattr(notes, "_journal_path", lambda: "")  # 仓库里 active 的日志库也不掺
    notes._CACHE.update({"mtime": None, "rows": []})
    notes._BAD.update({"n": 0, "warned": False})
    assert len(notes._all_rows()) == 1
    assert notes.bad_line_count() == 1


# ---- 结构锁：别把「收紧」改回去 -------------------------------------------

def test_voice_out_asks_the_ledger():
    assert "ledger_corrupt()" in _src("loop/voice_out.py")


def test_voice_gate_reads_with_status():
    assert "read_json_checked(" in _src("core/rules/voice_gate.py")


def test_self_look_has_one_reader():
    s = _src("core/identity/self_look.py")
    assert "json.loads" not in s
    assert "core.infra.jsonio" in s


def test_notes_wraps_every_line_parse():
    # 5 个 jsonl 解析点全部改成「跳过但计数」，外加 1 处是 _bad_line 自己的定义。
    assert _src("loop/notes.py").count("_bad_line(exc)") == 6
