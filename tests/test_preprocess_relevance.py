# -*- coding: utf-8 -*-
"""``relevance.py`` 退役后**保留面**的回归：``ThreadPredictor`` / ``_extract_keywords`` / ``_semantic_text``。

背景（``docs/架构审计/A-入站与事件.md`` §4.2 / ``G2-2``）：``CoarseAssigner`` 整类
（281 行，话题簇写 ``threads`` / ``thread_msg_ref``）已退役，但同一个模块里的三个符号
**有真实消费方**——``agents/auto_dream.py:10`` 与 ``agents/feedback.py:7``
（``ThreadPredictor.from_snapshot`` / ``to_snapshot`` / ``_extract_keywords`` /
``_semantic_text``）。删类时最容易连带删掉它们，所以这里钉住：

* ① 三个符号仍定义在 ``core/preprocess/relevance.py``；
* ② 两个消费方仍在 import 它们（静态扫，不真跑 agent）；
* ③ 行为没被顺手改坏（快照往返、关键词、语义文本占位符、余弦边界）。

Run directly: python tests/test_preprocess_relevance.py
"""

from __future__ import annotations

import ast
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

ROOT = Path(__file__).resolve().parent.parent
_MODULE = "core/preprocess/relevance.py"
#: 保留面（G2-2 明确要求不删）。
_KEPT = ("ThreadPredictor", "_extract_keywords", "_semantic_text")
_CONSUMERS = ("agents/auto_dream.py", "agents/feedback.py")


def _defs(path: str) -> set[str]:
    tree = ast.parse((ROOT / path).read_text(encoding="utf-8-sig"))
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
    return names


def _imported_names(path: str) -> dict[str, set[str]]:
    """``from X import a, b`` → ``{X: {a, b}}``。"""
    tree = ast.parse((ROOT / path).read_text(encoding="utf-8-sig"))
    out: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            out.setdefault(node.module, set()).update(
                alias.name for alias in node.names
            )
    return out


# ------------------------------------------------------------------ ① ② 保留面
def test_kept_symbols_are_still_defined() -> None:
    """①三个符号仍在 relevance.py 里定义（删类不许连带删它们）。"""
    defined = _defs(_MODULE)
    missing = [name for name in _KEPT if name not in defined]
    assert not missing, f"{_MODULE} 少了保留面符号：{missing}（现有：{sorted(defined)}）"


def test_coarse_assigner_is_gone_from_the_module() -> None:
    """①′退役面确实走了：类与它的写库入口都不在模块里。"""
    defined = _defs(_MODULE)
    assert "CoarseAssigner" not in defined, sorted(defined)
    source = (ROOT / _MODULE).read_text(encoding="utf-8-sig")
    for name in ("CoarseAssigner", "commit_assignments", "assign_with_embedding"):
        assert name not in source, f"{_MODULE} 还留着 {name}"


def test_real_consumers_still_import_the_kept_symbols() -> None:
    """②两个 agent 仍从 relevance import 这三个符号（否则保留面可以再删一轮）。"""
    for path in _CONSUMERS:
        imported = _imported_names(path).get("core.preprocess.relevance", set())
        missing = [name for name in _KEPT if name not in imported]
        assert not missing, f"{path} 不再 import {missing}（实际：{sorted(imported)}）"


# --------------------------------------------------------------------- ③ 行为
def test_thread_predictor_snapshot_roundtrip_and_scores() -> None:
    """③快照往返不丢字段；相似消息的 surprise 更低（信号方向没反）。"""
    from core.preprocess.relevance import ThreadPredictor

    predictor = ThreadPredictor()
    assert predictor.msg_count == 0
    predictor.update(
        {"content": "明天下午三点开会讨论预算", "wxid": "wxid_a",
         "created_at": "2026-10-01T10:00:00"},
        [0.5] * 8,
    )
    assert predictor.msg_count == 1
    assert predictor.recent_keywords, "关键词没进预测器"
    assert predictor.active_members == {"wxid_a"}

    restored = ThreadPredictor.from_snapshot(predictor.to_snapshot())
    assert restored.msg_count == predictor.msg_count
    assert set(restored.recent_keywords) == set(predictor.recent_keywords)
    assert restored.active_members == predictor.active_members
    assert restored.to_snapshot() == predictor.to_snapshot(), "快照不是幂等的"

    same = restored.quick_surprise({"content": "明天下午三点开会讨论预算"})
    other = restored.quick_surprise({"content": "周末去海边露营看日出"})
    assert same < other, (same, other)

    keywords = {"开会", "预算"}
    overlap = restored.keyword_overlap_idf(keywords)
    assert 0.0 <= overlap <= 1.0, overlap
    assert restored.conversational_continuity([0.5] * 8) > 0.9
    assert restored.conversational_continuity([]) == 0.0


def test_semantic_text_placeholders_and_truncation() -> None:
    """③占位符与 200 字截断（消费方靠它把图片/表情包写成文字）。"""
    from core.preprocess.relevance import _semantic_text

    assert _semantic_text({"content": "", "msg_type": 3}) == "[图片]"
    assert _semantic_text({"content": "", "msg_type": 47}) == "[表情包]"
    assert _semantic_text({"content": "在吗"}) == "在吗"
    assert len(_semantic_text({"content": "长" * 500})) == 200


def test_extract_keywords_and_cosine_edges() -> None:
    """③关键词抽取返回集合；余弦对空/零向量不炸。"""
    from core.preprocess.relevance import _cosine_sim, _extract_keywords

    words = _extract_keywords("明天下午三点开会讨论预算，顺便把季度报表带上")
    assert isinstance(words, set) and words, words
    assert _extract_keywords("") == set()
    assert _cosine_sim([], [1.0]) == 0.0
    assert _cosine_sim([0.0] * 8, [0.0] * 8) == 0.0
    assert abs(_cosine_sim([1.0, 0.0], [1.0, 0.0]) - 1.0) < 1e-9


def main() -> int:
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    failed = 0
    for test in tests:
        try:
            test()
            print(f"PASS {test.__name__}")
        except AssertionError as exc:
            failed += 1
            print(f"FAIL {test.__name__}: {exc}")
        except Exception as exc:  # noqa: BLE001 - 脚本模式下也要看清是哪条炸了
            failed += 1
            print(f"ERROR {test.__name__}: {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
