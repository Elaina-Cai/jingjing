"""出稿层的静默认定 ＋ 「一回合只生成一次」的负锁。

2026-10-23（Step 3 第五刀）：出稿层不再改写。随之一起退役的三件东西：
- ``_synthesize_reply_stream``：兜底综合，另起一次调用要第二次稿；
- ``_log_render_pair``：草稿→成稿对照语料，只有「改写」才产得出 pair；
- ``_self_duplicate_audit``：复读审计，唯一调用点在改路径里。

它们一起没了，所以「草稿 / 成稿」这对概念从出稿层消失——
``logs/render_pairs.jsonl`` 只剩读它的两个报表脚本，不再有新行。
"""
import inspect
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import agents.conversational as conv  # noqa: E402


def test_is_silent_reply_recognizes_only_markers():
    """静默出稿靠方括号标记认定——空串／省略号／「嗯。」都不算静默。"""
    silent = ["[silent]", "【静默】", "[不说话]", "【silent】。", "  [ SILENT ] "]
    spoken = ["", "   ", "……", "嗯。", "silent", "[静默]好", "她没说话", "不说话了"]
    for text in silent:
        assert conv.ConversationalAgent._is_silent_reply(text) is True, f"应判静默：{text!r}"
    for text in spoken:
        assert conv.ConversationalAgent._is_silent_reply(text) is False, f"不应判静默：{text!r}"


def test_one_turn_generates_exactly_once():
    """没有第二次生成：兜底综合 / 改写留痕 / 复读审计三件都不许回来。"""
    cls = conv.ConversationalAgent
    for gone in ("_synthesize_reply_stream", "_log_render_pair"):
        assert not hasattr(cls, gone), f"{gone} 不许回来：那就是第二个她"
    assert not hasattr(conv, "_self_duplicate_audit"), "复读审计的调用点已随改写一起退役"
    assert not hasattr(conv, "_render_pairs_path"), "对照语料的写入口已退役"

    src = inspect.getsource(cls._run_persona_brain_turn_stream)
    seg = src[src.index("elif not reply and not speak_handled:"):][:600]
    assert "silent = True" in seg and "turn_no_draft" in seg, seg


def main() -> bool:
    print("=" * 60)
    print("silent reply markers / one generation per turn")
    print("=" * 60)
    results = []
    for test in [test_is_silent_reply_recognizes_only_markers, test_one_turn_generates_exactly_once]:
        try:
            test()
            results.append((test.__name__, True))
        except Exception:
            import traceback

            traceback.print_exc()
            results.append((test.__name__, False))
    for name, ok in results:
        print(f"  {'PASS' if ok else 'FAIL'} {name}")
    failed = [n for n, ok in results if not ok]
    print(f"silent reply checks: {len(results) - len(failed)}/{len(results)} passed")
    return not failed


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)