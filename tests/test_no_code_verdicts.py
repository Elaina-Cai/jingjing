"""「回不回」这条路里不许再有代码判决。

2026-10-23 主人定案：**语义交回模型**。这条路上的代码只许做机械事——
自己发的跳过、拍别人跳过、群纯拍挂宽限、刷屏计数（零 token）、然后进模型。

本文件锁的是「删干净了，且不许长回来」：
- 触发/直回路径不再构造 ``RelevanceFacts``，也没有 skip 分支；
- 正经事信号（词表猜「提问 / 转达 / 承诺 / 撤回 / 有截止时间」）只留下
  一份字面词表 ``URGENT_MARKERS``（两个既有消费者用它，不是判断）；
- 那个旗标整体退役（``features.SERIOUS_SIGNALS`` 不许回来）。
"""
import inspect
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core.routing import serious_signals, turn_pipeline  # noqa: E402
from core import features  # noqa: E402


def test_trigger_and_direct_paths_have_no_gate() -> None:
    src = inspect.getsource(turn_pipeline)
    for dead in ("_decide_trigger_relevance", "_decide_direct_relevance",
                 "_serious_inputs", "RelevanceFacts(", "_relevance_new",
                 "group_turn_skipped"):
        assert dead not in src, f"格门残留：{dead}"
    assert "turn_model_decides" in src, "该留下的那行账（这一轮交给模型）不见了"
    assert "mark_question_replied" in src, "「这一问处理过了」这个副作用不该跟着消失"


def test_serious_signals_module_only_owns_the_wordlist() -> None:
    assert serious_signals.__all__ == ["URGENT_MARKERS"], serious_signals.__all__
    for gone in ("serious_mode", "serious_signals_for", "serious_direct_override",
                 "SeriousSignals", "serious_take_key"):
        assert not hasattr(serious_signals, gone), f"{gone} 应已退役"
    assert isinstance(serious_signals.URGENT_MARKERS, frozenset)


def test_the_flag_is_gone_from_the_registry() -> None:
    assert not hasattr(features, "SERIOUS_SIGNALS")
    assert "serious_signals" not in features.FLAG_REGISTRY
    assert "serious_signals" not in features.TRI_STATE_FLAGS


def test_dead_turn_modules_are_gone() -> None:
    assert not os.path.exists(os.path.join(ROOT, "core", "turn", "serious.py"))
    assert not os.path.exists(os.path.join(ROOT, "agents", "fast_path.py")), (
        "关键词分类器（7 组正则猜意图）不许长回来：它替模型判断"
    )


def test_conversational_has_no_fast_path_wiring() -> None:
    from agents import conversational

    src = inspect.getsource(conversational)
    for dead in ("should_fast_path(", "FastPathResult", "fast_path_check",
                 "fast_path_invalid", "_execute_fast_path", "_lightweight_reply"):
        assert dead not in src, f"快路径残留：{dead}"
    assert conversational.ConversationalAgent.__dict__.get("_execute_fast_path") is None


def test_the_verdict_module_is_gone() -> None:
    import importlib.util

    for dead in ("core.turn.verdict", "core.routing.relevance"):
        assert importlib.util.find_spec(dead) is None, f"{dead} 应已删除"
    from core.turn.addressing import addressed_to_her

    assert callable(addressed_to_her)


def test_silence_flag_is_gone() -> None:
    assert not hasattr(features, "SILENCE_CONSUMED")
    assert "silence_consumed" not in features.FLAG_REGISTRY


def main() -> bool:
    print("=" * 60)
    print("no code verdicts on the reply path")
    print("=" * 60)
    names = sorted(k for k in globals() if k.startswith("test_"))
    results = []
    for name in names:
        try:
            globals()[name]()
            results.append((name, True))
        except Exception:
            import traceback
            traceback.print_exc()
            results.append((name, False))
    for name, ok in results:
        print(f"  {'PASS' if ok else 'FAIL'} {name}")
    failed = [n for n, ok in results if not ok]
    print(f"no-verdict checks: {len(results) - len(failed)}/{len(results)} passed")
    return not failed


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)