"""「我还记得」的检索词不能只取快照开头。

2026-11-22 核对 02:55 的提示时发现：`recall_line(snap_text, ...)` 把整窗文本交给
`_words()`，而 `_words()` 只取**开头** 24 个二元词。快照开头是「群里有 N 个人」这类表头，
于是检索词几乎全被表头和名单占满，后面真正在说的话一个词都进不去。
"""
from __future__ import annotations

from loop import brief


def _rows():
    return [{"target": "wxid_a", "text": "10-27 他又加班到十点"}]


def test_tail_of_a_long_snapshot_drives_recall():
    filler = "一二三四五六七八九十百千万亿兆京垓秭穰沟涧正载极恒河沙阿僧祇那由他不可思议无量数"
    snap = "群里有 3 个人（主人在）：\n- Elaina\n- 小鲸鲸\n- 欢欢\n" + filler + "\n" + "甲乙丙丁戊己庚辛壬癸子丑寅卯辰巳午未申酉戌亥\n" + "他今天又加班"
    line = brief.recall_line(snap, "", target="wxid_a", memo_rows=_rows(), history_rows=[])
    assert "加班" in line


def test_short_snapshot_still_works():
    line = brief.recall_line("他今天又加班", "", target="wxid_a", memo_rows=_rows(), history_rows=[])
    assert "加班" in line
