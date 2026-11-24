# -*- coding: utf-8 -*-
"""task-15 读时透镜 provider（notes）：三态 / 预算 / span 硬校验 / message_id 定位 / 确定性。

覆盖：
- flag 已登记（tri、默认 off、env_key 正确），provider 只读 ``features.mode``；
- ``off``：零 IO（不读笔记文件）、返回 []；``shadow``：只落 ``note_recall_shadow``、返回 []；
- ``on``：≤3 条、detail 带 picked / rejected / valid / span_broken / budget；
- span 硬校验：``quote`` 对不上的记录被丢弃（计 ``span_broken``、不进 items）；
- ``message_id`` 定位：窗口漂移（行号变了）后仍能定位；metadata 同时给当前行号与快照行号；
- 透镜：place 问句命中 place 档（``lens_kind``）；
- ``merge_with_chain``：同 span 去重（verbatim note 优先）、notes ≤ 3；
- 回填确定性：同一输入两次逐字节相同；``write_backfill`` 写文件 + manifest sha1；
- 纯度：源码不 import llm / 网络 / 数据库模块（只经 core.memory.recall_chain）。
"""
from __future__ import annotations

import ast
import hashlib
import inspect
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

ISO = ROOT / ".test_tmp" / "task15" / "iso_note_recall"
os.environ.setdefault("LOG_DIR", str(ISO / "logs"))
os.environ.setdefault("META_DB_PATH", str(ISO / "meta.db"))
os.environ.setdefault("PLATFORMS_DIR", str(ISO / "platforms"))
os.environ.setdefault("DATA_DIR", str(ISO / "data"))

from core import features  # noqa: E402
from core.memory import note_recall as nr  # noqa: E402
from core.memory.contracts import MemoryItem  # noqa: E402

ENV = "YINYUE_FEATURE_WORTH_REMEMBERING"
SOURCE_PATH = ROOT / "core" / "memory" / "note_recall.py"
CHAT = "test_chat@chatroom"
PLACE_Q = "我说过我是哪儿人来着？"


class _Env:
    """临时改环境变量并在退出时恢复（照 tests/test_note_writer.py）。"""

    def __init__(self, **kv):
        self.kv = kv
        self.old: dict[str, str | None] = {}

    def __enter__(self):
        for key, value in self.kv.items():
            self.old[key] = os.environ.get(key)
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = str(value)
        return self

    def __exit__(self, *exc):
        for key, value in self.old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        return False


class _FakeLogger:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    def info(self, event, **fields):
        self.calls.append((event, fields))


def _raw_rows() -> list[dict]:
    return [
        {
            "event_id": "m1", "at": "2026-10-10 14:00:00", "name": "P3",
            "actor": "wxid_a", "content": "我说过我老家是重庆", "chat_id": CHAT,
        },
        {
            "event_id": "m2", "at": "2026-10-11 09:30:00", "name": "Elaina",
            "actor": "wxid_b", "content": "帮我看下广州的天气", "chat_id": CHAT,
        },
        {
            "event_id": "m3", "at": "2026-10-12 20:00:00", "name": "P3",
            "actor": "wxid_a", "content": "等你们写完物理实验报告发我一份", "chat_id": CHAT,
        },
        {
            "event_id": "m4", "at": "2026-10-13 08:00:00", "name": "Elaina",
            "actor": "wxid_b", "content": "我来自外地", "chat_id": CHAT,
        },
    ]


def _pad_rows(offset: int = 2) -> list[dict]:
    pad = [
        {
            "event_id": f"pad{i}", "at": f"2026-10-0{i + 1} 08:00:00", "name": "其他人",
            "actor": "wxid_pad", "content": f"垫话 {i}", "chat_id": CHAT,
        }
        for i in range(offset)
    ]
    return pad + _raw_rows()


def _write_notes(directory: Path, notes: list[dict], chat_id: str = CHAT) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = nr.store_path(chat_id, directory=directory)
    body = "".join(json.dumps(note, ensure_ascii=False, sort_keys=True) + "\n" for note in notes)
    path.write_text(body, encoding="utf-8", newline="")
    return path


def _patch_rows(rows: list[dict]) -> None:
    nr.rc.load_rows = lambda chat_id, limit=None: list(rows)  # type: ignore[assignment]


def _restore_rows(original) -> None:
    nr.rc.load_rows = original  # type: ignore[assignment]


def _item(memory_id: str, kind: str, row_index: int) -> MemoryItem:
    return MemoryItem(
        memory_id=memory_id,
        kind=kind,
        content=f"[10-10 14:00] {memory_id}",
        chat_id=CHAT,
        metadata={"row_index": row_index, "note_id": memory_id, "source": kind},
    )


def _wide_rows() -> list[dict]:
    """≥ 20 行的窗口：``recall_chain.keyword_ranks`` 的 DF 过滤要求 token 出现在 ≤DF_MAX_RATIO(0.05) 的行里，
    小 fixture 上任何 token 都会被判「太常见」而丢光（那时 chain 透镜不可测）。
    最后一行是广州那条（供 recent_text / chain 透镜用例：它在「最近 3 行」里）。"""
    rows = [
        {
            "event_id": f"w{i}", "at": f"2026-10-0{1 + i % 9} 1{i % 6}:00:00", "name": "其他人",
            "actor": "wxid_pad", "content": f"闲聊第{i}条，今天推送有点多", "chat_id": CHAT,
        }
        for i in range(24)
    ]
    rows[9] = {"event_id": "g1", "at": "2026-10-04 10:00:00", "name": "P3", "actor": "wxid_a",
               "content": "除了我重庆人体质，别人都受不了", "chat_id": CHAT}
    rows[15] = {"event_id": "g2", "at": "2026-10-09 11:00:00", "name": "其他人", "actor": "wxid_pad",
                "content": "到杭州别急着赶路", "chat_id": CHAT}
    rows[23] = {"event_id": "g3", "at": "2026-10-15 12:00:00", "name": "Elaina", "actor": "wxid_b",
                "content": "帮我看看广州今天的天气", "chat_id": CHAT}
    return rows
def test_flag_registered_tri_default_off():
    assert features.WORTH_REMEMBERING == "worth_remembering"
    spec = features.FLAG_REGISTRY[features.WORTH_REMEMBERING]
    assert spec.kind == "tri", spec.kind
    assert spec.default == features.MODE_OFF, spec.default
    assert features.env_key(features.WORTH_REMEMBERING) == ENV
    assert nr.NOTE_KIND == "notes" and nr.NOTES_MAX_ITEMS == 3


def test_off_is_zero_io():
    directory = ROOT / ".test_tmp" / "task15" / "nr_zero_io"
    _write_notes(directory, nr.backfill_chat(_raw_rows()))
    original = nr.rc.load_rows
    original_load = nr.load_notes

    def _boom(*_a, **_k):
        raise AssertionError("off 档不许读笔记文件")

    nr.rc.load_rows = _boom  # type: ignore[assignment]
    nr.load_notes = _boom  # type: ignore[assignment]
    try:
        with _Env(**{ENV: "off"}):
            provider = nr.NoteRecallProvider(directory=directory)
            principal = SimpleNamespace(chat_id=CHAT, chat_type="group")
            assert provider.recall(principal, PLACE_Q, detail={}) == []
    finally:
        _restore_rows(original)
        nr.load_notes = original_load


def test_shadow_returns_empty_and_logs():
    directory = ROOT / ".test_tmp" / "task15" / "nr_shadow"
    _write_notes(directory, nr.backfill_chat(_raw_rows()))
    original = nr.rc.load_rows
    fake = _FakeLogger()
    original_log = nr.log
    _patch_rows(_raw_rows())
    nr.log = fake  # type: ignore[assignment]
    try:
        with _Env(**{ENV: "shadow"}):
            provider = nr.NoteRecallProvider(directory=directory)
            principal = SimpleNamespace(chat_id=CHAT, chat_type="group")
            assert provider.recall(principal, PLACE_Q, detail={}) == []
    finally:
        _restore_rows(original)
        nr.log = original_log
    events = [event for event, _fields in fake.calls]
    assert events == ["note_recall_shadow"], events
    fields = fake.calls[0][1]
    assert fields["mode"] == "shadow" and fields["chat_id"] == CHAT


def test_on_caps_three_and_reports_detail():
    directory = ROOT / ".test_tmp" / "task15" / "nr_on"
    notes = nr.backfill_chat(_raw_rows())
    assert len(notes) >= 3, notes
    _write_notes(directory, notes)
    original = nr.rc.load_rows
    _patch_rows(_raw_rows())
    detail: dict = {}
    try:
        with _Env(**{ENV: "on"}):
            provider = nr.NoteRecallProvider(directory=directory)
            principal = SimpleNamespace(chat_id=CHAT, chat_type="group")
            items = provider.recall(principal, PLACE_Q, limit=6, detail=detail)
    finally:
        _restore_rows(original)
    assert len(items) == nr.NOTES_MAX_ITEMS, len(items)
    info = detail["note_recall"]
    assert info["mode"] == "on" and info["budget"] == 3
    assert info["span_broken"] == 0 and info["valid"] == len(notes)
    assert len(info["picked"]) == len(items)
    assert all("lenses" in picked for picked in info["picked"])


def test_span_broken_is_dropped():
    directory = ROOT / ".test_tmp" / "task15" / "nr_broken"
    notes = nr.backfill_chat(_raw_rows())
    notes[0]["span"]["quote"] = "这段原文里没有"
    _write_notes(directory, notes)
    original = nr.rc.load_rows
    _patch_rows(_raw_rows())
    detail: dict = {}
    try:
        with _Env(**{ENV: "on"}):
            provider = nr.NoteRecallProvider(directory=directory)
            principal = SimpleNamespace(chat_id=CHAT, chat_type="group")
            items = provider.recall(principal, PLACE_Q, limit=6, detail=detail)
    finally:
        _restore_rows(original)
    info = detail["note_recall"]
    assert info["span_broken"] == 1, info
    assert info["valid"] == len(notes) - 1
    assert notes[0]["note_id"] not in {item.metadata.get("note_id") for item in items}
    assert all("没有" not in item.content for item in items)


def test_message_id_location_survives_window_drift():
    directory = ROOT / ".test_tmp" / "task15" / "nr_drift"
    notes = nr.backfill_chat(_raw_rows())
    assert any(note["row_index"] == 3 for note in notes), notes
    _write_notes(directory, notes)
    shifted = _pad_rows(2)
    original = nr.rc.load_rows
    _patch_rows(shifted)
    detail: dict = {}
    try:
        with _Env(**{ENV: "on"}):
            provider = nr.NoteRecallProvider(directory=directory)
            principal = SimpleNamespace(chat_id=CHAT, chat_type="group")
            items = provider.recall(principal, PLACE_Q, limit=6, detail=detail)
    finally:
        _restore_rows(original)
    assert detail["note_recall"]["span_broken"] == 0, detail["note_recall"]
    assert items, "漂移后仍该有条目"
    for item in items:
        current = int(item.metadata["row_index"])
        assert shifted[current]["event_id"] == item.metadata["span"]["message_id"]
        assert item.metadata["stored_row_index"] != current


def test_revoked_note_source_is_not_recalled(tmp_path, monkeypatch):
    from core.memory import belief_commit

    directory = tmp_path / "notes"
    rows = _raw_rows()
    notes = nr.backfill_chat(rows)
    target = next(note for note in notes if nr._message_id_of(note) == "m1")
    _write_notes(directory, notes)
    monkeypatch.setattr(nr.rc, "load_rows", lambda chat_id, limit=None: list(rows))
    principal = SimpleNamespace(
        chat_id=CHAT,
        chat_type="group",
        platform="wechat",
        bot_account_id="bot-a",
    )

    monkeypatch.setattr(
        belief_commit,
        "evidence_state",
        lambda ref, scope: "unknown" if ref == "m1" else "alive",
    )
    with _Env(**{ENV: "on"}):
        unknown = nr.NoteRecallProvider(
            directory=directory,
            verify_evidence=True,
        ).recall(principal, PLACE_Q, limit=6)
    hit = next(
        item for item in unknown
        if item.metadata.get("note_id") == target["note_id"]
    )
    assert hit.source_event_ids == ("m1",)
    assert hit.source_message_ids == ("m1",)
    assert hit.metadata["evidence_state"] == "unknown"

    monkeypatch.setattr(
        belief_commit,
        "evidence_state",
        lambda ref, scope: "dead" if ref == "m1" else "alive",
    )
    with _Env(**{ENV: "on"}):
        dead = nr.NoteRecallProvider(
            directory=directory,
            verify_evidence=True,
        ).recall(principal, PLACE_Q, limit=6)
    assert target["note_id"] not in {
        item.metadata.get("note_id") for item in dead
    }


def test_lens_kind_prefers_matching_note():
    rows = _raw_rows()
    notes = nr.backfill_chat(rows)
    by_kind = {note["kind"]: note for note in notes}
    assert "place" in by_kind and "promise" in by_kind, by_kind
    index = nr.index_by_message(rows)
    place_score, place_parts = nr.score_note(
        by_kind["place"], rows, query=PLACE_Q, wanted=("place",), index=index
    )
    promise_score, promise_parts = nr.score_note(
        by_kind["promise"], rows, query=PLACE_Q, wanted=("place",), index=index
    )
    assert place_parts["kind"] == 1.0 and promise_parts["kind"] == 0.0
    assert place_score > promise_score, (place_score, promise_score)
    assert nr.question_kinds(PLACE_Q) == ("place",)
    assert nr.question_kinds("国庆那天我让鲸鲸帮我查了哪座城市的天气？") == ("place",)


def test_merge_with_chain_dedupes_same_row():
    notes = [_item("note:a", "notes", 1), _item("note:b", "notes", 2), _item("note:c", "notes", 3)]
    chain = [_item("chain:1", "recall_chain", 2), _item("chain:2", "recall_chain", 9)]
    merged = nr.merge_with_chain(notes, chain)
    assert [item.memory_id for item in merged] == ["note:a", "note:b", "note:c", "chain:2"], merged
    many = nr.merge_with_chain(notes + [_item("note:d", "notes", 4)], chain)
    assert sum(1 for item in many if item.kind == "notes") <= nr.NOTES_MAX_ITEMS


def test_backfill_is_reproducible():
    first = nr.backfill_chat(_raw_rows())
    second = nr.backfill_chat(_raw_rows())
    dump = lambda rows: json.dumps(rows, ensure_ascii=False, sort_keys=True)  # noqa: E731
    assert dump(first) == dump(second)
    assert [note["note_id"] for note in first] == [note["note_id"] for note in second]


def test_write_backfill_files_and_manifest():
    target = ROOT / ".test_tmp" / "task15" / "nr_backfill"
    manifest = nr.write_backfill([CHAT], directory=target, rows_loader=lambda chat_id: _raw_rows())
    entry = manifest["chats"][CHAT]
    body = (target / entry["file"]).read_text(encoding="utf-8")
    assert entry["rows"] == len(_raw_rows())
    assert entry["notes"] == body.count("\n")
    assert entry["sha1"] == hashlib.sha1(body.encode("utf-8")).hexdigest()[:16]
    assert (target / "manifest.json").exists()


def test_notes_dir_env_override():
    override = ROOT / ".test_tmp" / "task15" / "nr_dir"
    with _Env(YINYUE_NOTES_DIR=str(override)):
        assert nr.notes_dir() == override
    with _Env(YINYUE_NOTES_DIR=None):
        assert nr.notes_dir().name == "notes_backfill"


def test_purity_imports():
    tree = ast.parse(SOURCE_PATH.read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module.split(".")[0])
    forbidden = {"sqlite3", "requests", "aiohttp", "openai", "chromadb", "llm", "os.urandom"}
    assert not (modules & forbidden), modules
    assert "core" in modules

def test_notes_budget_knob_default_is_three():
    """预算旋钮：默认 3（防挤占）；env 只在量具里用来对比更大预算。"""
    assert nr.notes_max_items() == nr.NOTES_MAX_ITEMS == 3
    with _Env(**{nr.NOTES_MAX_ENV: "6"}):
        assert nr.notes_max_items() == 6
    with _Env(**{nr.NOTES_MAX_ENV: "abc"}):
        assert nr.notes_max_items() == 3
    with _Env(**{nr.NOTES_MAX_ENV: "99"}):
        assert nr.notes_max_items() == 3



def test_speaker_lens_prefers_current_turn_speaker():
    """(c) 指代上下文：与当前回合说话人同人的笔记加权；名字出现在问句/最近窗口再加小分。"""
    rows = _wide_rows()
    place = [n for n in nr.backfill_chat(rows) if n["kind"] == "place"]
    assert len(place) == 3, [(n["kind"], n.get("quote")) for n in place]
    assert nr.speaker_key_of(SimpleNamespace(chat_id=CHAT, person_id="wxid_a"), rows) == "wxid_a"
    same = {n["span"]["quote"]: nr._lens_speaker(n, "", "", speaker_key="wxid_a", recent_text="") for n in place}
    assert same == {"重庆": 1.0, "广州": 0.0, "杭州": 0.0}, same
    by_name = {n["span"]["quote"]: nr._lens_speaker(n, "Elaina 呢", "", speaker_key="", recent_text="") for n in place}
    assert by_name["广州"] == 0.5 and by_name["重庆"] == 0.0, by_name


def test_recent_text_and_speaker_key_of():
    """(c)：最近窗口文本与说话人 id 的解析（context 显式值优先）。"""
    rows = _wide_rows()
    recent = nr.recent_text(rows)
    assert "广州" in recent and len(recent) < 400, recent
    assert nr.recent_text(rows, {"recent_text": "显式窗口"}) == "显式窗口"
    principal = SimpleNamespace(chat_id=CHAT, person_id="wxid_b")
    assert nr.speaker_key_of(principal, rows) == "wxid_b"
    assert nr.speaker_key_of(principal, rows, {"speaker_key": "wxid_ctx"}) == "wxid_ctx"
    assert nr.speaker_key_of(principal, rows, SimpleNamespace(sender_id="wxid_obj")) == "wxid_obj"
    assert nr.speaker_key_of(SimpleNamespace(chat_id=CHAT), rows) == "wxid_b"


def test_recent_rows_count_env_parse():
    """recent_rows 旋钮：1..6，非法落回 3。"""
    assert nr.recent_rows_count() == nr.RECENT_ROWS == 3
    with _Env(**{nr.RECENT_ROWS_ENV: "5"}):
        assert nr.recent_rows_count() == 5
    with _Env(**{nr.RECENT_ROWS_ENV: "9"}):
        assert nr.recent_rows_count() == 3
    with _Env(**{nr.RECENT_ROWS_ENV: "abc"}):
        assert nr.recent_rows_count() == 3


def test_chain_lens_follows_recent_window():
    """(c)：链扩展到「问句 + 最近窗口文本」后，近窗口里出现过的笔记能拿到 chain 分。"""
    rows = _wide_rows()
    assert len(rows) >= 20, "需 ≥20 行才能绕过 keyword_ranks 的 DF 过滤"
    index = nr.index_by_message(rows)
    place = [n for n in nr.backfill_chat(rows) if n["kind"] == "place"]
    chain_query = "上次说的那个地名 " + nr.recent_text(rows)
    ranks = list(nr.rc.keyword_ranks(rows, chain_query))
    chain_rows = set(ranks[: nr.CHAIN_TOPK])
    lenses = {n["span"]["quote"]: nr._lens_chain(n, chain_rows, index) for n in place}
    assert lenses["广州"] == 1.0, (lenses, ranks[:5])
    assert lenses["重庆"] == 0.0 and lenses["杭州"] == 0.0, lenses
    scored = sorted(
        ((nr.score_note(n, rows, query="上次说的那个地名", wanted=("place",),
                        chain_rows=chain_rows, index=index, speaker_key="wxid_b",
                        recent_text=nr.recent_text(rows)), n["span"]["quote"]) for n in place),
        reverse=True,
    )
    assert scored[0][1] == "广州", scored


def test_notes_gate_score_env_parse():
    """门控阈值：未设/非法 → None = 门控关闭。"""
    assert nr.notes_gate_score() is None
    with _Env(**{nr.GATE_SCORE_ENV: "3.2"}):
        assert nr.notes_gate_score() == 3.2
    with _Env(**{nr.GATE_SCORE_ENV: "abc"}):
        assert nr.notes_gate_score() is None
    with _Env(**{nr.GATE_SCORE_ENV: ""}):
        assert nr.notes_gate_score() is None


def test_merge_with_chain_gate_chain_priority():
    """门控：chain 够强 → 只发 chain（notes 不挤）；不够强/空 → 旧行为合并。"""
    def chain_item(score):
        return MemoryItem(memory_id="c1", kind="recall_chain", content="chain", chat_id=CHAT,
                          metadata={"row_index": 99, "score": score})

    notes = [_item("n1", "notes", 3)]
    with _Env(**{nr.GATE_SCORE_ENV: None}):
        assert [i.memory_id for i in nr.merge_with_chain(notes, [chain_item(5.0)])] == ["n1", "c1"]
    assert [i.memory_id for i in nr.merge_with_chain(notes, [chain_item(5.0)], gate_score=3.0)] == ["c1"]
    assert [i.memory_id for i in nr.merge_with_chain(notes, [chain_item(5.0)], gate_score=6.0)] == ["n1", "c1"]
    assert [i.memory_id for i in nr.merge_with_chain(notes, [], gate_score=3.0)] == ["n1"]


def test_provider_detail_records_referent_context():
    """(c)：读数里要能看到说话人/近窗口/门控（可复盘）。"""
    directory = ROOT / ".test_tmp" / "task15" / "nr_context"
    rows = _wide_rows()
    _write_notes(directory, nr.backfill_chat(rows))
    original = nr.rc.load_rows
    _patch_rows(rows)
    try:
        with _Env(**{ENV: "on", nr.GATE_SCORE_ENV: None}):
            detail: dict = {}
            provider = nr.NoteRecallProvider(directory=directory)
            principal = SimpleNamespace(chat_id=CHAT, chat_type="group", person_id="wxid_b")
            items = provider.recall(principal, PLACE_Q, detail=detail)
    finally:
        _restore_rows(original)
    assert items, "on 档应该有笔记可发"
    context = detail["note_recall"]["context"]
    assert context["speaker_key"] == "wxid_b", context
    assert context["recent_chars"] > 0 and context["recent_rows"] == nr.RECENT_ROWS, context
    assert context["gate_score"] is None, context
    assert context["chain_query_chars"] >= context["recent_chars"], context


def test_question_kind_does_not_treat_recall_verb_as_promise():
    """「你还记得…吗」是回忆动词，不能把笔记按 promise 透镜加权。"""
    assert "promise" not in nr.question_kinds("你还记得我说那老师也教过咱们哪门课吗？")
    assert "promise" in nr.question_kinds("你还记得我答应过你什么吗？")

def test_tool_description_notes_channel_follows_flag():
    """notes 通道与开关同门：off 时对模型不可见，on 时才出现；类属性不被污染。"""
    from agents.tools.memory import MemoryRecallTool
    with _Env(**{ENV: "off"}):
        off = MemoryRecallTool()
        assert "随手记" not in off.description, off.description
        assert "notes" not in off.parameters["kinds"]["description"]
    with _Env(**{ENV: "on"}):
        on = MemoryRecallTool()
        assert "随手记" in on.description, on.description
        assert "notes" in on.parameters["kinds"]["description"]
    assert "notes" not in MemoryRecallTool.parameters["kinds"]["description"]
def test_tool_execute_drops_notes_kind_when_off():
    """off 档模型显式点名 notes 也要被剔除；on 档保留。"""
    import asyncio
    from agents.tools import memory as mem
    from core.execution import data_channels as dc
    seen: dict = {}
    original = dc.recall_hub_items
    def fake_recall(principal, query, **kwargs):
        seen.update(kwargs)
        return []
    dc.recall_hub_items = fake_recall  # type: ignore[assignment]
    try:
        with _Env(**{ENV: "off"}):
            off_tool = mem.MemoryRecallTool()
            off_tool._principal = lambda: object()  # type: ignore[assignment]
            asyncio.run(off_tool.execute(query="广州天气", kinds="notes,fact"))
        assert "notes" not in (seen.get("kinds") or []), seen
        seen.clear()
        with _Env(**{ENV: "on"}):
            on_tool = mem.MemoryRecallTool()
            on_tool._principal = lambda: object()  # type: ignore[assignment]
            asyncio.run(on_tool.execute(query="广州天气", kinds="notes,fact"))
        assert "notes" in (seen.get("kinds") or []), seen
    finally:
        dc.recall_hub_items = original  # type: ignore[assignment]
def test_notes_scope_line_only_for_notes_hits():
    """范围说明只对 notes 命中出现，并按群聊/私聊给本群/本私聊 + 说话人。"""
    from agents.tools.memory import _notes_scope_line
    context = SimpleNamespace(is_group=True, sender_name="P3")
    line = _notes_scope_line([_item("note:scope", "notes", 3)], context)
    assert "本群" in line and "P3" in line and "不跨会话" in line, line
    assert _notes_scope_line([_item("episode:x", "episode", 1)], context) == ""
    assert _notes_scope_line([], context) == ""
    private = _notes_scope_line([_item("note:p", "notes", 2)], SimpleNamespace(is_group=False))
    assert "本私聊" in private, private
def test_span_relative_and_relocated():
    """span 三种命中方式：绝对偏移 / 相对 content_start / quote 唯一时重定位。"""
    rows = _raw_rows()
    notes = nr.backfill_chat(rows)
    assert notes, "样本应回填出至少一条笔记"
    note = notes[0]
    reason, how = nr.validate_note_ex(note, rows)
    assert reason == "" and how == "span", (reason, how)
    base = 7
    position = next(i for i, row in enumerate(rows) if str(row.get("event_id") or "") == note["span"]["message_id"])
    row = dict(rows[position])
    row["content_start"] = base
    moved = dict(note)
    moved["row_index"] = 0
    moved["span"] = dict(note["span"])
    moved["span"]["start"] = int(note["span"]["start"]) + base
    moved["span"]["end"] = int(note["span"]["end"]) + base
    reason2, how2 = nr.validate_note_ex(moved, [row])
    assert reason2 == "" and how2 == "relative", (reason2, how2)
    drift = dict(note)
    drift["span"] = dict(note["span"])
    drift["span"]["start"] = int(note["span"]["start"]) + 2
    drift["span"]["end"] = int(note["span"]["end"]) + 2
    reason3, how3 = nr.validate_note_ex(drift, rows)
    assert reason3 == "" and how3 in ("relocated", "span"), (reason3, how3)
    assert nr.validate_note(moved, [row]) == ""



def test_gate_decision_requires_cue_and_low_coverage():
    window = "闲聊第1条，今天推送有点多 闲聊第2条，今天推送有点多"
    fire = nr.gate_decision("上次说的那个地名你还记得吗", window)
    assert fire["cue"] is True and fire["fire"] is True, fire
    assert fire["cov"] < fire["cov_max"]
    covered = nr.gate_decision("上次那个 闲聊第1条 今天推送有点多", window)
    assert covered["cue"] is True and covered["fire"] is False, covered
    no_cue = nr.gate_decision("那件事你怎么看", window)
    assert no_cue["cue"] is False and no_cue["fire"] is False, no_cue


def test_gate_flag_registered_and_cov_env():
    assert features.NOTES_GATE == "notes_gate"
    spec = features.FLAG_REGISTRY[features.NOTES_GATE]
    assert spec.kind == "tri" and spec.default == features.MODE_OFF
    assert features.env_key(features.NOTES_GATE) == "YINYUE_FEATURE_NOTES_GATE"
    with _Env(**{"YINYUE_FEATURE_NOTES_GATE": None, "YINYUE_NOTES_GATE_COV": None}):
        assert nr.notes_gate_mode() == nr.NOTE_MODE_OFF
        assert nr.notes_gate_cov_max() == 0.35
    with _Env(**{"YINYUE_FEATURE_NOTES_GATE": "shadow", "YINYUE_NOTES_GATE_COV": "0.2"}):
        assert nr.notes_gate_mode() == "shadow"
        assert nr.notes_gate_cov_max() == 0.2
    with _Env(**{"YINYUE_FEATURE_NOTES_GATE": "nonsense", "YINYUE_NOTES_GATE_COV": "2"}):
        assert nr.notes_gate_mode() == nr.NOTE_MODE_OFF
        assert nr.notes_gate_cov_max() == 0.35


def test_gate_on_blocks_notes_when_not_fire():
    directory = ROOT / ".test_tmp" / "task15" / "nr_gate_on"
    _write_notes(directory, nr.backfill_chat(_raw_rows()))
    original = nr.rc.load_rows
    _patch_rows(_raw_rows())
    detail: dict = {}
    try:
        with _Env(**{ENV: "on", "YINYUE_FEATURE_NOTES_GATE": "on"}):
            provider = nr.NoteRecallProvider(directory=directory)
            principal = SimpleNamespace(chat_id=CHAT, chat_type="group")
            items = provider.recall(principal, PLACE_Q, limit=6, detail=detail)
    finally:
        _restore_rows(original)
    assert items == []
    gate = detail["note_recall"]["gate"]
    assert gate["fire"] is False and gate["cue"] is False


def test_gate_shadow_logs_would_fire_without_blocking():
    directory = ROOT / ".test_tmp" / "task15" / "nr_gate_shadow"
    _write_notes(directory, nr.backfill_chat(_raw_rows()))
    original = nr.rc.load_rows
    fake = _FakeLogger()
    original_log = nr.log
    _patch_rows(_raw_rows())
    nr.log = fake  # type: ignore[assignment]
    detail: dict = {}
    try:
        with _Env(**{ENV: "shadow", "YINYUE_FEATURE_NOTES_GATE": "shadow"}):
            provider = nr.NoteRecallProvider(directory=directory)
            principal = SimpleNamespace(chat_id=CHAT, chat_type="group")
            items = provider.recall(principal, "上次说的那个地名你还记得吗", limit=6, detail=detail)
    finally:
        _restore_rows(original)
        nr.log = original_log
    assert items == []
    fields = [item for event, item in fake.calls if event == "notes_gate_would_fire"]
    assert len(fields) == 1, [event for event, _f in fake.calls]
    assert fields[0]["fire"] is True and fields[0]["would_merge"] is True
    assert fields[0]["would_pick"] >= 1, fields[0]
    assert detail["note_recall"]["gate"]["fire"] is True

def test_referent_question_requires_cue_overlap_and_chain_score() -> None:
    question = "上次说的那个地名你还记得吗"
    weak = "今天食堂的饭很难吃"
    assert nr.is_referent_question(question, chain_content=weak, chain_score=4.0) is True
    assert nr.is_referent_question("食堂几点开门", chain_content=weak, chain_score=4.0) is False
    assert nr.is_referent_question(question, chain_content=question, chain_score=4.0) is False
    assert nr.is_referent_question(question, chain_content=weak, chain_score=9.0) is False
    assert nr.is_referent_question(question, chain_content="", chain_score=None) is True
    with _Env(**{"YINYUE_NOTES_REFERENT_OVERLAP": "1.0", "YINYUE_NOTES_REFERENT_CHAIN_SCORE": "20"}):
        assert nr.is_referent_question(question, chain_content=question, chain_score=9.0) is True


def test_merge_notes_first_semantics_and_cap() -> None:
    notes = [
        MemoryItem(memory_id=f"n{row}", kind="notes", content=f"note{row}", chat_id=CHAT,
                   metadata={"row_index": row, "span": {"message_id": f"m{row}"}})
        for row in (10, 11, 12)
    ]
    chain = [
        MemoryItem(memory_id=f"c{row}", kind="recall_chain", content=f"chain{row}", chat_id=CHAT,
                   metadata={"row_index": row, "score": 5.0})
        for row in (11, 13, 14, 15, 16, 17)
    ]
    merged = nr.merge_notes_first(notes, chain, notes_cap=2, total_cap=6)
    assert [item.metadata["route"] for item in merged] == ["notes", "notes"] + ["chain_fill"] * 4, merged
    assert [item.metadata["row_index"] for item in merged] == [10, 11, 13, 14, 15, 16], merged
    assert notes[0].metadata.get("route") is None, "不得改动传入条目"
    assert all(item.kind in ("notes", "recall_chain") for item in merged)

    chain_only = nr.merge_notes_first(notes, chain, notes_cap=0, total_cap=3)
    assert [item.metadata["route"] for item in chain_only] == ["chain_fill"] * 3, chain_only
    assert [item.metadata["row_index"] for item in chain_only] == [11, 13, 14], chain_only

    notes_only = nr.merge_notes_first(notes, [], notes_cap=3)
    assert [item.metadata["route"] for item in notes_only] == ["notes"] * 3, notes_only
    assert len(notes_only) == 3


def test_merge_notes_first_has_no_gate_inside() -> None:
    source = inspect.getsource(nr.merge_notes_first)
    assert "referent" not in source and "cue" not in source, "闸门必须在纯函数之外"
    assert "merge_notes_first" in nr.__all__
    assert nr.ROUTE_NOTES == "notes" and nr.ROUTE_CHAIN_FILL == "chain_fill"
    assert nr.NOTES_FIRST_TOTAL == 6


TESTS = [
    test_flag_registered_tri_default_off,
    test_off_is_zero_io,
    test_shadow_returns_empty_and_logs,
    test_on_caps_three_and_reports_detail,
    test_span_broken_is_dropped,
    test_message_id_location_survives_window_drift,
    # 这条要 tmp_path/monkeypatch，放在 tests/pytest_style/test_note_recall_revoked.py 跑
    test_lens_kind_prefers_matching_note,
    test_merge_with_chain_dedupes_same_row,
    test_backfill_is_reproducible,
    test_notes_budget_knob_default_is_three,
    test_write_backfill_files_and_manifest,
    test_notes_dir_env_override,
    test_purity_imports,
    test_question_kind_does_not_treat_recall_verb_as_promise,
    test_speaker_lens_prefers_current_turn_speaker,
    test_recent_text_and_speaker_key_of,
    test_recent_rows_count_env_parse,
    test_chain_lens_follows_recent_window,
    test_notes_gate_score_env_parse,
    test_gate_decision_requires_cue_and_low_coverage,
    test_gate_flag_registered_and_cov_env,
    test_gate_on_blocks_notes_when_not_fire,
    test_gate_shadow_logs_would_fire_without_blocking,
    test_merge_with_chain_gate_chain_priority,
    test_provider_detail_records_referent_context,
    test_tool_description_notes_channel_follows_flag,
    test_tool_execute_drops_notes_kind_when_off,
    test_notes_scope_line_only_for_notes_hits,
    test_span_relative_and_relocated,
    test_referent_question_requires_cue_overlap_and_chain_score,
    test_merge_notes_first_semantics_and_cap,
    test_merge_notes_first_has_no_gate_inside,
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
        print(f"note_recall checks: {len(TESTS)}/{len(TESTS)} passed")
    return ok


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
