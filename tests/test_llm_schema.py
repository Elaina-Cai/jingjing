# -*- coding: utf-8 -*-
"""``llm/schema.py`` 守卫（审计 G5-15）：按 schema 校验 + 失败重试一次 + 显式报错。

验收钉四件事：

① **校验**：必填缺失 / 类型不符 / 顶层非对象 / 概率越界，报错必须带**字段路径**
   （``candidates[0].intent`` 这种能照着改的写法）；
② **禁止 ``_raw`` 透传**：``{"_raw": …}`` 一律判不合法——旧读法把"解析失败"做成一个
   仍然合法的 dict 往下传（``llm/client.py:310-313``，调用方
   ``agents/base.py:178`` 当正常参数用），这条断言就是那个兜底的墓志铭；
③ **重试一次**：第一次失败 → 给 ``repair`` 一次机会；仍失败 → 显式失败，
   ``ok=False``（**绝不**返回 ``{"_raw": …}``）；
④ **``chat_tools`` 接线**：工具参数坏 → 请模型重发一次；两次都坏 → 信封里带
   ``error`` 且那次调用**不执行**（不是"用空参数偷偷跑一遍"）。

Run directly:
    python tests/test_llm_schema.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import traceback
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

ROOT = Path(__file__).resolve().parent.parent
# 隔离前导（照 tests/test_reply_intent_contract.py）：本文件只用纯函数 + 一个假 provider，
# 但保持同一纪律。
_TEST_LOG_DIR = str(Path("data") / "_test_logs_llm_schema")
Path(_TEST_LOG_DIR).mkdir(parents=True, exist_ok=True)
os.environ.setdefault("LOG_DIR", _TEST_LOG_DIR)
_ISO_ROOT = (ROOT / "data" / "_llm_schema_test" / "_iso").resolve()
_ISO_ROOT.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("META_DB_PATH", str(_ISO_ROOT / "meta.db"))
os.environ.setdefault("PLATFORMS_DIR", str(_ISO_ROOT / "platforms"))
os.environ.setdefault("DATA_DIR", str(_ISO_ROOT / "data"))

from llm import schema  # noqa: E402
from llm.schema import (  # noqa: E402
    RAW_KEY,
    JsonPayloadError,
    expect_json,
    parse_json_payload,
    parse_payload_with_retry,
    validate_payload,
)


def _paths(problems) -> list[str]:
    return [item.path for item in problems]


def _reasons(problems) -> str:
    return "；".join(str(item) for item in problems)


# ---------------------------------------------------------------------------
# ① 解析 + 校验
# ---------------------------------------------------------------------------
def test_valid_payload_passes_and_keeps_no_raw() -> None:
    """合法 JSON 对象 → ``ok=True``，载荷就是原对象（``_raw`` 不在其中）。"""
    outcome = parse_json_payload('{"a": 1, "b": "x"}')
    assert outcome.ok is True
    assert outcome.payload == {"a": 1, "b": "x"}
    assert RAW_KEY not in (outcome.payload or {})
    assert outcome.attempts == 1
    # 围栏 / 前后缀也能取出来（模型很少只输出 JSON）
    fenced = parse_json_payload('说明\n```json\n{"a": 2}\n```\n尾巴')
    assert fenced.ok is True and fenced.payload == {"a": 2}
    surrounded = parse_json_payload('前缀 {"a": 3} 后缀')
    assert surrounded.ok is True and surrounded.payload == {"a": 3}


def test_required_and_types_are_reported_with_field_paths() -> None:
    """必填缺失 + 类型不符 → 报错带字段路径（不是笼统的"校验失败"）。"""
    problems = validate_payload(
        {"n": True, "s": 5, "extra": 1},
        required=("missing",),
        types={"n": "int", "s": "str"},
        source="intent",
    )
    paths = _paths(problems)
    assert "intent.missing" in paths, paths
    assert "intent.n" in paths, paths  # bool 不是合法的 int 参数
    assert "intent.s" in paths, paths
    assert "类型不符" in _reasons(problems)
    # 边界：给了的类型都对 → 没有 type 类问题（int 接受 int；float 接受 int）
    ok = validate_payload({"n": 3, "f": 1, "b": False, "l": [], "d": {}},
                          types={"n": "int", "f": "float", "b": "bool",
                                 "l": "list", "d": "dict"})
    assert ok == [], ok


def test_top_level_must_be_an_object() -> None:
    """顶层非对象（数组 / 裸字符串）→ 判不合法（工具参数必须是对象）。"""
    for bad in ("[1, 2]", '"字符串"', "42"):
        outcome = parse_json_payload(bad)
        assert outcome.ok is False, bad
        assert "顶层必须是 JSON 对象" in _reasons(outcome.problems), bad


# ---------------------------------------------------------------------------
# ② 禁止 _raw 透传（审计 G5-15）
# ---------------------------------------------------------------------------
def test_raw_key_is_rejected_by_construction() -> None:
    """``{"_raw": …}`` 一律不合法——这就是"禁止 ``_raw`` 透传"的强制点。"""
    problems = validate_payload({"_raw": "模型给的原文"})
    assert problems, "顶层 _raw 必须判不合法"
    assert "禁止 _raw 透传" in _reasons(problems)
    assert _paths(problems) == [f"payload.{RAW_KEY}"]

    outcome = parse_json_payload('{"_raw": "不是我想要的 JSON"}')
    assert outcome.ok is False
    with_raw = False
    try:
        expect_json('{"_raw": "x"}')
    except JsonPayloadError as exc:
        with_raw = "禁止 _raw 透传" in str(exc)
    assert with_raw, "解析路径也必须挡住 _raw"


def test_invalid_payload_never_becomes_a_usable_dict() -> None:
    """任何失败路径都不许返回一个"能当数据用"的 dict。"""
    for text in ("不是 JSON", "", "```json\n{坏的}\n```"):
        outcome = parse_json_payload(text, required=("a",))
        assert outcome.ok is False, text
        assert outcome.payload is None, text
        # 原始文本只挂在 outcome.text / 异常上（进日志/审计），不是数据
        assert isinstance(outcome.text, str)
        try:
            outcome.raise_for_status()
        except JsonPayloadError as exc:
            assert exc.text == outcome.text
            assert exc.problems
        else:  # pragma: no cover - 上面必须抛
            raise AssertionError("ok=False 时 raise_for_status 必须抛 JsonPayloadError")


# ---------------------------------------------------------------------------
# ③ 重试一次
# ---------------------------------------------------------------------------
def test_retry_once_then_success() -> None:
    """第一次不合法 → 给 repair 一次机会 → 用它的新文本再校验一轮。"""
    seen: list[str] = []

    def repair(failed: schema.JsonParseOutcome) -> str:
        seen.append(_reasons(failed.problems))
        return '{"a": 7}'

    outcome = parse_payload_with_retry(
        "坏掉的输出", repair=repair, required=("a",), types={"a": "int"},
    )
    assert outcome.ok is True
    assert outcome.attempts == 2
    assert outcome.payload == {"a": 7}
    assert len(seen) == 1 and "不是合法 JSON" in seen[0]


def test_retry_once_then_explicit_failure() -> None:
    """重试仍不合法 → 显式失败（``ok=False`` + 原因），**绝不** ``_raw`` 兜底。"""
    calls: list[int] = []

    def repair(_failed: schema.JsonParseOutcome) -> str:
        calls.append(1)
        return "还是坏的"

    outcome = parse_payload_with_retry(
        "坏的", repair=repair, required=("a",), max_attempts=2,
    )
    assert outcome.ok is False
    assert outcome.payload is None
    assert outcome.attempts == 2
    assert len(calls) == 1, "只许重试一次"
    assert "不是合法 JSON" in _reasons(outcome.problems)
    # 没有 repair（无法重试）时只有一轮，仍然是显式失败
    lonely = parse_payload_with_retry("坏的", required=("a",))
    assert lonely.ok is False and lonely.attempts == 1


def test_expect_json_raises_with_actionable_message() -> None:
    """``expect_json`` 的报错要能直接照着改：带尝试次数与全部问题。"""
    try:
        expect_json(
            '{"intent": 5}',
            required=("intent", "attitude"),
            types={"intent": "str"},
            source="intent",
        )
    except JsonPayloadError as exc:
        message = str(exc)
        assert "intent.intent" in message and "intent.attitude" in message, message
        assert "尝试 1 次" in message
    else:  # pragma: no cover
        raise AssertionError("不合法时必须抛 JsonPayloadError")


def test_probability_helpers_are_shared() -> None:
    """概率类判据只有一处 owner（``number_in_range`` / ``is_finite_number``）。"""
    assert schema.number_in_range(0.0) and schema.number_in_range(1.0)
    assert not schema.number_in_range(-0.01) and not schema.number_in_range(1.01)
    assert not schema.number_in_range(float("nan"))
    assert not schema.number_in_range(float("inf"))
    assert not schema.number_in_range("0.5")  # 字符串概率 = 没按 schema 来
    assert schema.is_finite_number(3) and not schema.is_finite_number("3")
    assert schema.json_dumps({"中文": 1}) == '{"中文":1}'


# ---------------------------------------------------------------------------
# ④ chat_tools 接线（工具参数坏 → 重发一次 → 仍坏显式报错）
# ---------------------------------------------------------------------------
class _FakeFunction:
    def __init__(self, name: str, arguments: str) -> None:
        self.name = name
        self.arguments = arguments


class _FakeCall:
    def __init__(self, call_id: str, name: str, arguments: str) -> None:
        self.id = call_id
        self.function = _FakeFunction(name, arguments)


class _FakeMessage:
    def __init__(self, calls) -> None:
        self.content = ""
        self.tool_calls = calls


class _FakeChoice:
    def __init__(self, message) -> None:
        self.message = message


class _FakeResponse:
    def __init__(self, message) -> None:
        self.choices = [_FakeChoice(message)]
        self.usage = None


class _FakeCompletions:
    def __init__(self, responses) -> None:
        self._responses = list(responses)
        self.requests = 0

    async def create(self, **kwargs):
        self.requests += 1
        return self._responses.pop(0)


class _FakeChat:
    def __init__(self, completions) -> None:
        self.completions = completions


class _FakeOpenAI:
    def __init__(self, responses) -> None:
        self.completions = _FakeCompletions(responses)
        self.chat = _FakeChat(self.completions)


def _client_with(responses):
    """造一个 LLMClient，provider/客户端全部假掉（不碰网络、不碰 provider 语义）。"""
    from llm.client import LLMClient

    client = LLMClient()
    fake = _FakeOpenAI(responses)
    client._get_client = lambda provider: fake  # type: ignore[assignment]
    client._get_model = lambda provider: "fake-model"  # type: ignore[assignment]
    client._filter_usable_providers = (  # type: ignore[assignment]
        lambda chain, vision_mode=False: (list(chain), [])
    )
    client._circuit.allow = lambda name: True  # type: ignore[assignment]
    return client, fake


def test_chat_tools_bad_arguments_are_repaired_once() -> None:
    """参数坏 → 修正请求一次拿到合法参数；工具名与原 id 不变。"""
    broken = _FakeResponse(_FakeMessage([_FakeCall("c1", "search", "{坏的")]))
    fixed = _FakeResponse(_FakeMessage([]))  # 占位（不会用到）
    client, fake = _client_with([broken, fixed])

    repair_reply = json.dumps({
        "calls": [{"id": "c1", "name": "search", "arguments": {"q": "天气"}}],
    }, ensure_ascii=False)
    repair_calls: list[dict] = []

    async def fake_chat(**kwargs):
        repair_calls.append(kwargs)
        return repair_reply

    client.chat = fake_chat  # type: ignore[assignment]

    result = asyncio.run(client.chat_tools(
        system="s", user="u", tools=[{"type": "function"}], agent_name="t",
    ))
    assert result.get("error") is None, result
    assert len(repair_calls) == 1, repair_calls
    assert repair_calls[0]["temperature"] == 0.0
    calls = result["tool_calls"]
    assert calls[0]["name"] == "search" and calls[0]["id"] == "c1"
    assert calls[0]["arguments"] == {"q": "天气"}
    assert "_raw" not in calls[0]["arguments"]
    assert "error" not in calls[0]
    assert fake.completions.requests == 1, "修复不该重跑 provider 链"


def test_chat_tools_still_broken_is_an_explicit_error() -> None:
    """两次都坏 → 信封带 ``error``、参数为空、**没有** ``_raw``（不是静默降级）。"""
    broken = _FakeResponse(_FakeMessage([_FakeCall("c9", "search", "{{{")]))
    client, _fake = _client_with([broken])

    async def fake_chat(**kwargs):
        return "修复也失败"

    client.chat = fake_chat  # type: ignore[assignment]

    result = asyncio.run(client.chat_tools(
        system="s", user="u", tools=[{"type": "function"}], agent_name="t",
    ))
    assert "tool_arguments_invalid" in str(result.get("error")), result
    call = result["tool_calls"][0]
    assert call["arguments"] == {}, "坏参数不许当数据用"
    assert "_raw" not in call["arguments"]
    assert call["error"] and call["raw_text"], "原始文本只进审计字段"


def test_chat_tools_good_arguments_do_not_trigger_repair() -> None:
    """参数合法时一个字节都不改：不额外调用、不留审计字段、没有 error。"""
    good = _FakeResponse(_FakeMessage([
        _FakeCall("c1", "search", json.dumps({"q": "天气"})),
    ]))
    client, _fake = _client_with([good])

    async def fake_chat(**kwargs):  # pragma: no cover - 不该被调用
        raise AssertionError("参数合法时不该发修正请求")

    client.chat = fake_chat  # type: ignore[assignment]
    result = asyncio.run(client.chat_tools(
        system="s", user="u", tools=[{"type": "function"}], agent_name="t",
    ))
    assert result.get("error") is None
    assert result["tool_calls"][0]["arguments"] == {"q": "天气"}
    assert "raw_text" not in result["tool_calls"][0]


def test_two_json_objects_are_merged_into_one_payload() -> None:
    """N1 真实形状（2026-10-13 实查）：意图段 + scene_read 拆成两个对象输出。

    旧读法整段报 ``Extra data`` → 意图降级、scene_read 丢失；现在两段合并。
    """
    text = (
        '{"intent": "提问", "attitude": "关心", "candidates": {}, "evidence": []}\n'
        '{"scene_read": {"roles": {"her": 0.9}, "genre": "任务"}}'
    )
    outcome = parse_json_payload(
        text,
        required=("intent", "attitude", "candidates", "evidence"),
        types={"intent": "str", "candidates": "dict", "evidence": "list"},
        source="intent",
    )
    assert outcome.ok is True, outcome.problems
    assert outcome.payload["intent"] == "提问"
    assert outcome.payload["scene_read"]["roles"]["her"] == 0.9


def test_two_fenced_blocks_are_merged() -> None:
    """两个围栏块各装一段：旧读法只取第一块，现在还拿得到第二块。"""
    text = '```json\n{"a": 1}\n```\n说明\n```json\n{"b": 2}\n```'
    outcome = parse_json_payload(text)
    assert outcome.ok is True
    assert outcome.payload == {"a": 1, "b": 2}


def test_duplicate_top_level_key_first_wins() -> None:
    """同键出现两次：第一次说了算（不拿后段的垃圾覆盖已解出的段）。"""
    outcome = parse_json_payload('{"a": 1}\n{"a": 2, "b": 3}')
    assert outcome.ok is True
    assert outcome.payload == {"a": 1, "b": 3}


def test_single_object_with_trailing_garbage_still_parses() -> None:
    """单个对象 + 尾随文本（含花括号）：保住已解出的对象，不再整段失败。"""
    outcome = parse_json_payload('{"a": 1}\n就这些 {不是 JSON}')
    assert outcome.ok is True
    assert outcome.payload == {"a": 1}


def test_broken_fragment_is_skipped() -> None:
    """坏片段跳过后继续找下一个，不因中间一段坏而丢掉好的两段。"""
    outcome = parse_json_payload('{"a": 1}\n{坏片段\n{"b": 2}')
    assert outcome.ok is True
    assert outcome.payload == {"a": 1, "b": 2}


def main() -> bool:
    tests = [fn for name, fn in sorted(globals().items()) if name.startswith("test_")]
    failed = 0
    for fn in tests:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception:
            failed += 1
            print(f"FAIL {fn.__name__}")
            traceback.print_exc()
    print(f"{len(tests) - failed}/{len(tests)} passed")
    return failed == 0


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
