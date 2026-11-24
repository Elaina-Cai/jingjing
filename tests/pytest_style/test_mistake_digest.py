"""P3-1：把"她常犯的错"聚出来 —— 规则钉死，不许漂。"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core.quality import mistake_digest as md


def _row(i, text, reply, *, source="repeat", chat="c1", at="2026-10-21 14:28"):
    return {"id": i, "source": source, "chat_id": chat, "sample_text": text,
            "reply_preview": reply, "created_at": at}


def test_codes_are_declared_and_described():
    assert md.CODES == (md.CODE_EMPTY_REPLY, md.CODE_REFUSED_NO_PATH,
                        md.CODE_REFUSED_WITH_PATH, md.CODE_ANSWERED_STILL_REPEATED)
    for code in md.CODES:
        assert md.describe_code(code) != "未归类"
    assert md.describe_code("找不到的码") == "未归类"


def test_empty_reply_is_the_first_priority():
    d = md.build_digest([_row(1, "在群里说一句：带伞", ""),
                         _row(2, "在群里说一句：带伞", "")])
    assert d["clusters"][0]["code"] == md.CODE_EMPTY_REPLY


def test_refused_without_alternative():
    d = md.build_digest([_row(1, "在群里说一句：带伞", "发不了，我这没这个口。"),
                         _row(2, "在群里说一句：带伞", "发不了，我没权限。")])
    assert d["clusters"][0]["code"] == md.CODE_REFUSED_NO_PATH


def test_refused_with_alternative_is_a_different_bug():
    d = md.build_digest([_row(1, "在群里说一句：带伞", "我发不了，这句你自己去群里敲。"),
                         _row(2, "在群里说一句：带伞", "发不了，得你用手机敲一遍。")])
    assert d["clusters"][0]["code"] == md.CODE_REFUSED_WITH_PATH


def test_answered_but_repeated():
    d = md.build_digest([_row(1, "下周三天气", "10月28号查不到，预报只到25号。"),
                         _row(2, "下周三天气", "10月28号查不到，预报只到25号。")])
    assert d["clusters"][0]["code"] == md.CODE_ANSWERED_STILL_REPEATED


def test_min_count_filters_singletons():
    rows = [_row(1, "只问过一次的话", "答了。")]
    assert md.build_digest(rows, min_count=2)["clusters"] == []
    assert len(md.build_digest(rows, min_count=1)["clusters"]) == 1


def test_clusters_merge_chats_and_keep_ids_and_span():
    rows = [_row(1, "同一句话", "", chat="a", at="2026-10-10 10:00"),
            _row(2, "同一句话", "", chat="b", at="2026-10-21 14:00")]
    c = md.build_digest(rows)["clusters"][0]
    assert c["count"] == 2 and c["ids"] == [1, 2]
    assert c["chats"] == ["a", "b"]
    assert c["first_at"].startswith("2026-10-10") and c["last_at"].startswith("2026-10-21")


def test_shape_ignores_punctuation_and_space():
    assert md.sample_shape("在银月的测试群里说一句：今天下雨记得带伞") == md.sample_shape(
        "在银月的测试群里说一句, 今天下雨记得带伞。")


def test_top_is_at_most_three_and_sorted_by_count():
    rows = []
    for i in range(5):
        rows.append(_row(i + 1, "最常问的", ""))
    for i in range(3):
        rows.append(_row(100 + i, "第二常问的", ""))
    for i in range(2):
        rows.append(_row(200 + i, "第三常问的", ""))
    d = md.build_digest(rows)
    assert [c["shape"] for c in d["top"]] == ["最常问的", "第二常问的", "第三常问的"]
    assert len(d["top"]) == 3
    assert d["by_source"] == {"repeat": 10}


def test_digest_never_returns_more_than_the_rows_it_got():
    assert md.build_digest([])["total"] == 0
    assert md.build_digest(None)["clusters"] == []
