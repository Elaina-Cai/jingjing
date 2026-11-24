"""批1 MemoryGate 测试：低分过滤 + 负知识 + 开关回滚。

Run directly:
    python tests/test_memory_gate.py
"""

from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# prompt/召回行为测试必须在模块顶固定批1开关：灰度里 .env 已把
# YINYUE_FEATURE_MEMORY_GATE/UNCERTAIN_VOICE 置 1 并注入进程（见 core/bootstrap.py
# load_yinyue_env_defaults），不固定的话下面每个用例的显式 _with_env 之外还会有
# 残留开关影响其它模块。本文件所有用例都自带 _with_env，故仅在测试内部生效。
os.environ["YINYUE_FEATURE_MEMORY_GATE"] = "0"
os.environ["YINYUE_FEATURE_UNCERTAIN_VOICE"] = "0"

from core.terminal.memory_gateway import (  # noqa: E402
    MemoryHit,
    _gate_hits,
    looks_like_question,
    memory_gate_enabled,
    memory_gate_min_score,
    negative_knowledge_block,
)

FLAG = "YINYUE_FEATURE_MEMORY_GATE"
THRESHOLD = "YINYUE_MEMORY_GATE_MIN_SCORE"


def _hit(memory_id: str, score: float) -> MemoryHit:
    return MemoryHit(
        memory_id=memory_id,
        content=f"content-{memory_id}",
        occurred_at=datetime.now(),
        event_type="message",
        speaker="",
        platform="wechat",
        bot_account_id="",
        chat_id="g1@chatroom",
        chat_type="group",
        visibility_scope=(),
        source_event_ids=(),
        score=score,
    )


def _with_env(**values: str | None):
    class _Ctx:
        def __enter__(self):
            self._saved = {key: os.environ.get(key) for key in values}
            for key, value in values.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

        def __exit__(self, *exc):
            for key, value in self._saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    return _Ctx()


def test_gate_off_passthrough() -> None:
    with _with_env(**{FLAG: None}):
        assert memory_gate_enabled() is False
        rows = [_hit("low", 0.10), _hit("high", 0.90)]
        assert [h.memory_id for h in _gate_hits(rows)] == ["low", "high"]


def test_gate_filters_below_default_threshold() -> None:
    with _with_env(**{FLAG: "1", THRESHOLD: None}):
        assert memory_gate_enabled() is True
        assert abs(memory_gate_min_score() - 0.45) < 1e-9
        rows = [_hit("low", 0.30), _hit("mid", 0.44), _hit("high", 0.60)]
        assert [h.memory_id for h in _gate_hits(rows)] == ["high"]


def test_gate_threshold_env() -> None:
    with _with_env(**{FLAG: "1", THRESHOLD: "0.25"}):
        rows = [_hit("a", 0.24), _hit("b", 0.26)]
        assert [h.memory_id for h in _gate_hits(rows)] == ["b"]


def test_gate_invalid_threshold_falls_back() -> None:
    with _with_env(**{FLAG: "1", THRESHOLD: "not-a-number"}):
        assert abs(memory_gate_min_score() - 0.45) < 1e-9
    with _with_env(**{FLAG: "1", THRESHOLD: "9.9"}):
        assert abs(memory_gate_min_score() - 1.5) < 1e-9


def test_looks_like_question() -> None:
    assert looks_like_question("你还记得吗") is True
    assert looks_like_question("fkxqs 是啥？") is True
    assert looks_like_question("钟凯宇是谁") is True
    assert looks_like_question("明天fkxqs浅尝一下吗") is True
    assert looks_like_question("哈哈哈哈") is False
    assert looks_like_question("") is False


def test_negative_knowledge_block() -> None:
    text = negative_knowledge_block()
    assert text.startswith("[事件记忆·无]")
    assert "没查到" in text
    assert "不要编" in text


def test_main_pins_batch1_flags_off() -> None:
    """批1 测试不得被 .env 灰度开关污染（本轮曾因此让 test_memory_gateway 挂）。"""
    assert os.environ.get("YINYUE_FEATURE_MEMORY_GATE") == "0"
    assert os.environ.get("YINYUE_FEATURE_UNCERTAIN_VOICE") == "0"


def main() -> int:
    tests = [
        test_gate_off_passthrough,
        test_gate_filters_below_default_threshold,
        test_gate_threshold_env,
        test_gate_invalid_threshold_falls_back,
        test_looks_like_question,
        test_negative_knowledge_block,
        test_main_pins_batch1_flags_off,
    ]
    for test in tests:
        test()
        print(f"OK {test.__name__}")
    print(f"ALL PASSED ({len(tests)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
