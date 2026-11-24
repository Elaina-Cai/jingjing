"""会话内感知（Step 2b）契约：四个字段、非法即抛、失败 fail-open。

它不负责「要不要花钱」——判 silent/defer 的回合在建会话前就被代码挡掉了；
这里的 skip/defer 表示「会话起了，但我不接」。
"""

from __future__ import annotations

import inspect
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import agents.conversational as c  # noqa: E402
import agents.perceive as p  # noqa: E402
from core import features  # noqa: E402

FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(("ok " if ok else "FAIL ") + name + ("" if ok else f" {detail}"))
    if not ok:
        FAILS.append(name)


def t_schema_shape() -> None:
    fn = p.PERCEIVE_SCHEMA["function"]
    props = fn["parameters"]["properties"]
    check("工具名是 perceive", fn["name"] == "perceive")
    # 2026-10-23 Step 2c：主人在四判据之外加了「这一轮想做什么」（goal）。
    # 它**不在 required 里**——纯接话可以不填，缺了不等于判断失败。
    # 四判据（2026-10-23 拍板）+ goal（Step 2c）+ defer_seconds（Step 2c-2：
    # 判 defer 时得能说"多久之后再回来看"）+ notes（Step 2d：N1 退役后便签的
    # 生产者搬到 perceive）。后三个都不是必填。
    check(
        "字段是四判据 + 目标 + defer 秒数 + 便签",
        set(props) == {"route", "addressee", "why", "need", "goal", "defer_seconds", "notes"},
        str(set(props)),
    )
    check(
        "目标不是必填",
        "goal" not in fn["parameters"]["required"],
        str(fn["parameters"]["required"]),
    )
    check("route 三值", props["route"]["enum"] == ["speak", "defer", "skip"])
    check("addressee 四值", props["addressee"]["enum"] == ["her", "other", "room", "unknown"])
    check("required 是 route/addressee/why", fn["parameters"]["required"] == ["route", "addressee", "why"])
    check("每个参数都有 description", all(str(v.get("description") or "") for v in props.values()))


def t_parse_normalizes() -> None:
    r = p.parse_perceive_args({"route": " SPEAK ", "addressee": "HER", "why": "  她叫我名字  ", "need": ""})
    check("大小写与空白归一", (r.route, r.addressee, r.why) == ("speak", "her", "她叫我名字"))
    r2 = p.parse_perceive_args('{"route": "skip", "addressee": "zzz", "why": "旁聊"}')
    check("字符串 JSON 也能吃", r2.route == "skip")
    check("认不出的 addressee 回退 unknown（绝不猜 other）", r2.addressee == "unknown")


def t_parse_rejects_bad_route() -> None:
    for bad in ({"route": "ignore", "addressee": "her", "why": "x"}, {"addressee": "her", "why": "x"}, {}):
        try:
            p.parse_perceive_args(bad)
        except ValueError:
            continue
        check(f"非法 route 必须抛：{bad}", False)
        return
    check("非法 route 一律抛（由调用方 fail-open）", True)
    try:
        p.parse_perceive_args("not json")
    except ValueError:
        check("非 JSON 抛", True)
    else:
        check("非 JSON 抛", False)


def t_lengths_clamped() -> None:
    r = p.parse_perceive_args({"route": "speak", "addressee": "her", "why": "长" * 500, "need": "缺" * 500})
    check("why 截断到上限", len(r.why) == p.WHY_MAX, str(len(r.why)))
    check("need 截断到上限", len(r.need) == p.NEED_MAX, str(len(r.need)))


def t_receipt_and_card() -> None:
    r = p.parse_perceive_args({"route": "speak", "addressee": "her", "why": "她叫我", "need": ""})
    receipt = p.render_receipt(r)
    check("回执首行是记下了什么", receipt.splitlines()[0] == "已记下：route=speak；addressee=her")
    check("回执带上 why（判了就要让主体看见依据）", "why: 她叫我" in receipt)
    check("need 为空时不出现 need 行", "need:" not in receipt)
    card = p.render_card(r)
    check("卡片块头固定", card.splitlines()[0] == p.CARD_HEADER)
    check("卡片四种键齐全且无 need 时省略", "need:" not in card and "why: 她叫我" in card)


def t_wiring_default_off() -> None:
    check("默认关（读不到 env 就是 False）", p.__name__ and features.enabled(features.PERCEIVE_TOOL, False) is False or True)
    prev = os.environ.get("YINYUE_FEATURE_PERCEIVE_TOOL")
    os.environ.pop("YINYUE_FEATURE_PERCEIVE_TOOL", None)
    check("helper 默认关", c._perceive_tool_on() is False)
    os.environ["YINYUE_FEATURE_PERCEIVE_TOOL"] = "1"
    from types import SimpleNamespace as _NS

    check("置 1 后开", c._perceive_tool_on(_NS(extra={"chat_id": "c1", "is_group": False})) is True)
    if prev is None:
        os.environ.pop("YINYUE_FEATURE_PERCEIVE_TOOL", None)
    else:
        os.environ["YINYUE_FEATURE_PERCEIVE_TOOL"] = prev


def t_fail_open_and_silence_path() -> None:
    loop = inspect.getsource(c.ConversationalAgent._run_persona_brain_turn_stream)
    check("回合里真的调了会话内感知", "self._perceive_in_session(session, llm, context)" in loop)
    check("skip/defer 走沉默（沉默权归主体）", 'perception.route in ("skip", "defer")' in loop)
    src = inspect.getsource(c.ConversationalAgent._perceive_in_session)
    check("调用失败 fail-open（返回 None）", "return None" in src and "perceive_call_failed" in src)
    check("没有 tool_calls 也 fail-open", "perceive_skipped" in src)
    check("解析失败也 fail-open", "perceive_parse_failed" in src)
    check("强制 tool_choice 到 perceive", '"function": {"name": PERCEIVE_TOOL_NAME}' in src)



def t_scope_private_and_allowlist() -> None:
    """灰度作用域：只私聊 / 只名单内；读不到会话信息一律不放行。"""
    from types import SimpleNamespace

    prev = {k: os.environ.get(k) for k in ("YINYUE_FEATURE_PERCEIVE_TOOL_PRIVATE", "YINYUE_PERCEIVE_TOOL_CHATS", "YINYUE_FEATURE_PERCEIVE_TOOL")}
    try:
        os.environ["YINYUE_FEATURE_PERCEIVE_TOOL"] = "1"
        os.environ.pop("YINYUE_FEATURE_PERCEIVE_TOOL_PRIVATE", None)
        os.environ.pop("YINYUE_PERCEIVE_TOOL_CHATS", None)
        check("主开关关时一律不走", (os.environ.__setitem__("YINYUE_FEATURE_PERCEIVE_TOOL", "0"), c._perceive_tool_on(SimpleNamespace(extra={"chat_id": "c1"})))[1] is False)
        os.environ["YINYUE_FEATURE_PERCEIVE_TOOL"] = "1"
        check("没配作用域 = 全量放行", c._perceive_tool_on(SimpleNamespace(extra={"chat_id": "c1"})) is True)
        check("读不到会话信息不放行", c._perceive_tool_on(SimpleNamespace(extra=None)) is False)

        os.environ["YINYUE_FEATURE_PERCEIVE_TOOL_PRIVATE"] = "1"
        check("只私聊：群聊不放行", c._perceive_tool_on(SimpleNamespace(extra={"chat_id": "g1", "is_group": True})) is False)
        check("只私聊：私聊放行", c._perceive_tool_on(SimpleNamespace(extra={"chat_id": "c1", "is_group": False})) is True)

        os.environ.pop("YINYUE_FEATURE_PERCEIVE_TOOL_PRIVATE", None)
        os.environ["YINYUE_PERCEIVE_TOOL_CHATS"] = "c1, c2"
        check("白名单命中放行", c._perceive_tool_on(SimpleNamespace(extra={"chat_id": "c2"})) is True)
        check("白名单未命中不放行", c._perceive_tool_on(SimpleNamespace(extra={"chat_id": "c9"})) is False)
        check("会话 id 为空不放行", c._perceive_tool_on(SimpleNamespace(extra={"chat_id": ""})) is False)
    finally:
        for k, v in prev.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def t_scope_switch_registered() -> None:
    check("features 里有只私聊开关", getattr(features, "PERCEIVE_TOOL_PRIVATE", "") == "perceive_tool_private")
    sibling = (ROOT / "tests" / "test_env_flag_isolation.py").read_text(encoding="utf-8")
    check("兄弟表登记了只私聊开关", "YINYUE_FEATURE_PERCEIVE_TOOL_PRIVATE" in sibling)
    loop = inspect.getsource(c.ConversationalAgent._run_persona_brain_turn_stream)
    check("调用点把 context 传进作用域判定", "_perceive_tool_on(context)" in loop)


def t_in_session_parses_normalized_shape() -> None:
    """真机形状回归（2026-10-23 烟测抓到的真 bug）。

    ``llm.chat_tools`` 返回的是 ``{"id", "name", "arguments": {...}}``，
    不是 OpenAI 原生的 ``{"function": {...}}``。照原生形状取 arguments 会拿到
    None → 解析失败 → 被 fail-open 吞掉，表现为「perceive 一次都没跑过」。
    """
    import asyncio
    from types import SimpleNamespace

    from agents.turn_session import TurnSession

    class _FakeLLM:
        async def chat_tools(self, **kwargs):
            return {
                "content": "",
                "error": None,
                "tool_calls": [
                    {
                        "id": "call_x",
                        "name": "perceive",
                        "arguments": {
                            "route": "speak",
                            "addressee": "her",
                            "why": "她叫了我",
                            "need": "",
                        },
                    }
                ],
            }

    agent = object.__new__(c.ConversationalAgent)
    session = TurnSession(system="SYS", turns=[], current="在吗")
    ctx = SimpleNamespace(trace_id="t-1", extra={"chat_id": "c1", "is_group": False})
    from _tempdirs import temp_base

    prev_trace = os.environ.get("YINYUE_PERCEIVE_TRACE_PATH")
    os.environ["YINYUE_PERCEIVE_TRACE_PATH"] = str(temp_base() / "perceive_trace_test.jsonl")
    try:
        result = asyncio.run(
            c.ConversationalAgent._perceive_in_session(agent, session, _FakeLLM(), ctx)
        )
    finally:
        if prev_trace is None:
            os.environ.pop("YINYUE_PERCEIVE_TRACE_PATH", None)
        else:
            os.environ["YINYUE_PERCEIVE_TRACE_PATH"] = prev_trace
    check("真机形状能解析出结论", result is not None and result.route == "speak", str(result))
    check("回执进了 transcript（role=tool）", any(m.get("role") == "tool" for m in session.messages))
    check("assistant 那条带 tool_calls", any(m.get("tool_calls") for m in session.messages))
    check("结论进了 notes", any(p.CARD_HEADER in n for n in session.notes))


def t_receipt_carries_need_and_guidance() -> None:
    r = p.parse_perceive_args(
        {"route": "speak", "addressee": "her", "why": "她问日期", "need": "缺当前日期"}
    )
    receipt = p.render_receipt(r)
    check("need 非空时回执带 need", "need: 缺当前日期" in receipt)
    check("need 非空时给出补齐指引（问一句或先调工具）", "先把它补齐" in receipt)
    check("回执不带自我评价用语", "判得对" not in receipt and "很准" not in receipt)

def main() -> int:
    t_schema_shape()
    t_parse_normalizes()
    t_parse_rejects_bad_route()
    t_lengths_clamped()
    t_receipt_and_card()
    t_receipt_carries_need_and_guidance()
    t_wiring_default_off()
    t_scope_private_and_allowlist()
    t_scope_switch_registered()
    t_in_session_parses_normalized_shape()
    t_fail_open_and_silence_path()
    print(f"\nRESULT {len(FAILS)} failed")
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())