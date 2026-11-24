"""自动记忆上下文只读取当前会话或明确共享的记录。"""
import pytest

from loop import notes, memo, brief


@pytest.fixture
def rows(monkeypatch):
    data = [
        {"kind": "fact", "target": "wxid_a", "text": "加班私密甲"},
        {"kind": "fact", "target": "group:b", "text": "加班群乙"},
        {"kind": "note", "text": "加班来源未知"},
        {"kind": "fact", "visibility": "shared", "text": "加班共享"},
        {"kind": "diary", "target": "wxid_a", "text": "加班日记甲"},
        {"kind": "diary", "text": "加班混合日记"},
    ]
    monkeypatch.setattr(notes, "_all_rows", lambda: data)
    monkeypatch.setattr(memo, "_rows", lambda: data)
    return data


def test_resident_and_diary_context_filters_before_render(rows):
    text = "\n".join(notes.note_block("group:b", 1000))
    assert "群乙" in text and "共享" in text
    assert "私密甲" not in text and "日记甲" not in text
    assert "来源未知" not in text and "混合日记" not in text


def test_private_context_does_not_see_other_conversation(rows):
    text = "\n".join(notes.note_block("wxid_a", 1000))
    assert "私密甲" in text and "日记甲" in text
    assert "群乙" not in text and "混合日记" not in text


def test_legacy_resident_uses_same_policy(rows):
    text = memo.resident("group:b", budget=1000)
    assert "群乙" in text and "共享" in text
    assert "私密甲" not in text and "来源未知" not in text


def test_keyword_recall_cannot_bypass_scope(rows):
    text = brief.recall_line("加班", target="group:b", memo_rows=rows, history_rows=rows)
    assert "群乙" in text or "共享" in text
    assert "私密甲" not in text and "日记甲" not in text and "混合日记" not in text


def test_unknown_context_only_reads_explicit_shared(rows):
    text = "\n".join(notes.note_block("", 1000))
    assert "共享" in text
    assert "私密甲" not in text and "群乙" not in text and "来源未知" not in text


def test_deduplication_preserves_same_text_in_distinct_conversations(monkeypatch):
    monkeypatch.setattr(notes, "_load", lambda: [
        {"kind": "fact", "target": "wxid_a", "text": "同一句事实"},
        {"kind": "fact", "target": "group:b", "text": "同一句事实"}])
    monkeypatch.setattr(notes, "_legacy_rows", lambda: [])
    assert len(notes._all_rows()) == 2



def test_scoped_memory_replay_100_times_is_read_only(rows):
    import json
    original=json.dumps(rows,sort_keys=True,ensure_ascii=False)
    for _ in range(100):
        text=' '.join(notes.note_block('group:b',1000))+' '+memo.resident('group:b',budget=1000)+' '+brief.recall_line('加班',target='group:b',memo_rows=rows,history_rows=rows)
        assert '私密甲' not in text and '日记甲' not in text and '来源未知' not in text
        assert json.dumps(rows,sort_keys=True,ensure_ascii=False)==original
