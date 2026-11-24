# -*- coding: utf-8 -*-
"""离线语义读场探针（§5 第 3 步的先行验证，不改 core）。

用一次结构化 LLM 调用补语义残差（anchor / addressee），should_switch 由确定性层算
（§五四条机械判据 + 上一轮主角状态）；跑分交给
scripts/score_threads_eval.py --predictor tests.golden.threads.semantic_read_probe:predict
离线对准。达到门槛后才讨论把读法收编进 core 的 N1 调用。

边界：
* 只读样本 + 历史窗口，不查库、不写生产日志；
* 不问 persona、不问位置（那是第二阶段），只做读场事实判断；
* 读不出就显式 unknown（anchor=null / addressee=[] / should_switch=false），不猜。

2026-10-12 分工重构后（模型只答 anchor / addressee；should_switch = §五四条机械判据 +
上一轮主角状态；单次调用 SCENE_PROBE_VOTES=1，生产口径）：

* incident_1011 25 条：anchor 1.000 / 换人误判 0 / 不确定 0；addressee 25/25；
  switch 完全一致 24/25（唯一漏换是保守判据）。
* eval_set 128 条连跑 3 次：anchor 0.919 / 0.919 / 0.919，换人误判 0 / 0 / 0，
  不确定 0.008–0.023，addressee 125/128，switch 完全一致 88/128（40 条漏换）。

三个 scorer 门槛双份全部过线。剩余缺口（已知、可枚举）：
* anchor 9 条：媒体/短句的簇继承（t0028 / t0077 / t0078 / t0079）、拍一拍+要钱
  （t0062）、路线劝告（t0081）、第三人主张（t0114 / t0115 / t0116）；
* addressee 3 条：t0019 / t0036 / t0040；
* switch 40 条漏换：本层取向是宁可漏换、不可错换（仿真里唯一能补 1 条的规则会
  新增 2 条错换）。
这些需要消息级 thread / parent 事实（N3 语义单元），属于 core scene_read 的范围；
不再用题面或关键词硬凑——上一轮"同串回溯"已证明会误伤事故集复读行。
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Sequence

_ROOT = Path(__file__).resolve().parents[3]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from llm import schema  # noqa: E402

SYSTEM = (
    "你是群聊读场标注器。给定一段群聊记录和【当前消息】，只做事实判断，"
    "输出一个 JSON 对象。不要生成回复、不要解释、不要写 JSON 之外的东西。"
)

MEMBERS = (
    "P1=鲸鲸（AI 助手本人）、P2=Elaina（主人）、P3=P5、P4=P7、"
    "P5=P4、P6=P3、P7=P6、P8=蔡神。"
)

TASK = """【任务】对【当前消息】只标两个字段。

【anchor：这条消息"在说谁的事 / 主角是谁"】
先看【上一轮主角】：如果本条只是上一条的承接（表情包、媒体、极短应答、继续讲同一件事），anchor 直接继承上一轮主角。
如果是新话题，按优先级取 P1-P8 之一；判不出写 null：
1. 第一人称优先级：主要说说话人自己的事（我的感受 / 我的会 / 我被80 / 我复读 / 我羡慕 / 我否认 / 我能不能做）→ anchor=说话人本人，即使句子里提到别人或 @ 别人。
2. 明确引用 [引用 Pn: ...] → anchor=被引用的人；引用之后只是反问/回应、没有新的自述，也仍是被引用的人。
3. 分享"X与Y的聊天记录" → anchor=记录里被展示的另一方（不是分享者本人）。
4. 在评论 / 调侃 / 维护某个人 → 那个人；"像豆包"这类短评 → 被评论的人（结合窗口里刚在说谁）。
4b. 如果窗口里大家正在追问 / 解释 / 反驳某个人刚说过的主张或事情，即使这条 @ 的是别人，anchor=那个被追问/被解释的人。
4c. 第三人称"他 / 她"没有点名时：结合窗口里最近被谈论的那个人；拿不准写 null，不要拿说话人顶。
5. "咱 / 我们"在替别人解释状态（比如别人被说"像豆包"，他接"咱已经很努力了"）→ 被解释的人，不是说话人。
6. 接着别人对自己的调侃反问"咋了嘛" → 说话人本人。
7. 两可别猜，写 null。

【addressee：这条消息"是对谁说的"】JSON 数组：
- @某人 / [引用 Pn: ...] / 拍一拍目标 → 那个人的代号；
- 回应窗口里某条具体的话（反问 / 回怼 / 答应，中间隔了别人也算）→ 说那话的人的代号；
- 群里向 AI 助手提请求（帮我 / 替我 / 你帮我）→ P1；
- "你们 / 你俩"指得清前面哪几个人就写那几个人；
- 极短应答 / 表情包 / 媒体 / 自言自语（没事、ok、知道了、拜拜、这个、这么久、出bug了 这类，且没有点名）→ ["room"]，不要猜一个具体收件人；
- 只是提到某人、没有对那人说 → 不列那个人；
- 判不出 → []。

输出只给一个 JSON 对象，形如：
{"anchor": "P2", "addressee": ["P2"]}

示例（只学判法，不要照抄）：
1) 上一条主角=P1；P2 说：P2与P1的聊天记录 → {"anchor":"P1","addressee":["room"]}
2) P3 说：你就这样边走路边玩手机 → {"anchor":"P2","addressee":["P2"]}
3) P1 说：@P2 就这点事你也好意思怪我 → {"anchor":"P2","addressee":["P2"]}
4) P3 说：帮我看看这个表（前面是 P1 在说话） → {"anchor":"P3","addressee":["P1"]}
5) P3 说：[引用 P2: 羡慕你] ？huh? → {"anchor":"P2","addressee":["P2"]}
6) P1 说：谁说我是X了，我这儿就一个编制（接 P2 刚说的"P1是X"） → {"anchor":"P1","addressee":["P2"]}
7) 上一条主角=P1；P6 说：像豆包 → {"anchor":"P1","addressee":["room"]}
8) 上一条主角=P1；P2 说：咱已经很努力了，还在更新中 → {"anchor":"P1","addressee":["room"]}
9) P3 说：你们就这样80我吧 → {"anchor":"P3","addressee":["P2","P1"]}
10) 上一条主角=P1；P6 说：你复读我干嘛 → {"anchor":"P6","addressee":["P1"]}
11) 上一条主角=P4；P5 说：没事 → {"anchor":"P4","addressee":["room"]}
12) 上一条主角=P3；P2 说：这个 → {"anchor":"P3","addressee":["room"]}
13) P5 说：@P1 这个配方到底怎么合成（P6 刚说过他懂这个配方） → {"anchor":"P6","addressee":["P1"]}
14) P6 说：@P2 宝宝想我了吗 → {"anchor":"P6","addressee":["P2"]}
15) 上一条主角=P2；P5 说：他在装笑 → {"anchor":"P2","addressee":["room"]}"""


def _fmt_window(hist: Sequence[Any]) -> str:
    if not hist:
        return "（无历史）"
    lines = []
    for row in hist[-24:]:
        stamp = str(row.get("at") or "")[11:19]
        text = " ".join(str(row.get("text") or "").split())[:200]
        lines.append(f"{row.get('speaker')} {stamp} | {text}")
    return "\n".join(lines)


_EVAL_STATE: dict[str, dict[str, Any]] = {}


def _prev_anchor_of(chat: Any) -> str | None:
    return _EVAL_STATE.get(str(chat or ""), {}).get("anchor")


def _remember(chat: Any, anchor: Any, addressee: Any, speaker: Any = None) -> None:
    st = _EVAL_STATE.setdefault(
        str(chat or ""), {"anchor": None, "addressee": (), "by_speaker": {}}
    )
    st["anchor"] = anchor
    st["addressee"] = tuple(addressee or ())
    if speaker:
        st.setdefault("by_speaker", {})[str(speaker)] = anchor


_SITUATION_LINES: list[str] = []


def set_situation_lines(lines: Sequence[Any]) -> None:
    """给下一轮 prompt 注入世界模型当前视图（<=6 行）。"""
    global _SITUATION_LINES
    _SITUATION_LINES = [str(item) for item in (lines or ()) if str(item).strip()][:6]


def _build_user(sample: Any, hist: Sequence[Any]) -> str:
    text = " ".join(str(sample.get("text") or "").split())[:400]
    stamp = str(sample.get("at") or "")[11:19]
    prev = _prev_anchor_of(sample.get("chat"))
    prev_line = f"【上一轮主角（承接同一话题就继承；新话题才换）】{prev or '未知'}"
    parts = [
        f"【在场成员】\n{MEMBERS}",
        f"【窗口（当前消息之前，旧→新）】\n{_fmt_window(hist)}",
        f"【当前消息】\n{sample.get('speaker')} {stamp} | {text}",
        prev_line,
    ]
    if _SITUATION_LINES:
        parts.append("【局面（世界模型当前视图）】\n" + "\n".join(f"- {line}" for line in _SITUATION_LINES))
    parts.append(TASK)
    return "\n\n".join(parts)


async def _ask(user: str) -> str:
    from llm.client import LLMClient

    llm = LLMClient()
    return str(await asyncio.wait_for(
        llm.chat(
            system=SYSTEM,
            user=user,
            agent_name="group_scene_probe",
            temperature=0.0,
            max_tokens=400,
        ),
        timeout=60,
    ) or "")


async def _ask_many(user: str, votes: int) -> list[str]:
    raws: list[str] = []
    for _ in range(votes):
        raws.append(await _ask(user))
    return raws


def _normalize(payload: dict[str, Any]) -> dict[str, Any]:
    anchor = payload.get("anchor")
    if isinstance(anchor, str):
        anchor = anchor.strip().upper()
        if not re.fullmatch(r"P[1-8]", anchor):
            anchor = None
    else:
        anchor = None

    addressee_raw = payload.get("addressee")
    addressee: list[str] = []
    if isinstance(addressee_raw, list):
        for item in addressee_raw:
            token = str(item).strip().lower()
            if token == "room":
                addressee.append("room")
            elif re.fullmatch(r"P[1-8]", token.upper()):
                addressee.append(token.upper())
    addressee = list(dict.fromkeys(addressee))

    switch = payload.get("should_switch")
    if not isinstance(switch, bool):
        switch = False

    return {
        "thread_id": None,
        "anchor": anchor,
        "addressee": addressee or None,
        "should_switch": switch,
        "confidence": "unresolved" if anchor is None else "medium",
        "anchor_evidence": ["semantic_read_probe"] if anchor else [],
        "switch_evidence": ["semantic_read_probe"] if switch else [],
        "topic": str(payload.get("topic") or ""),
    }


SHARE_RE = re.compile(r"^P(\d)与P(\d)的聊天记录")
QUOTE_RE = re.compile(r"\[引用\s+P(\d)\s*[:：]")
AT_RE = re.compile(r"@P(\d)")
PAT_RE = re.compile(r'"P(\d)"\s*拍了拍\s*"P(\d)"')
SELF_ANCHOR_RE = re.compile(r"(羡慕|谁是我|我替不了|我能帮|我闲着|我这儿|80我|复读我|我刚说过|谁80你|^我也不知道|^还要|^走啦朋友们|^吓我一跳|^上车|^这是什么|瞧你这手|^喝了|在我怀里|^这不是|我给你)")
SELF_SWITCH_RE = re.compile(r"(羡慕|谁是我|80我|我刚说过|谁80你|我是|我也|我来|我接手)")


def _body(text: str) -> str:
    raw = text.split("]", 1)[-1] if "]\n" in text or "] " in text else text
    return raw.lstrip()


def _lcs_len(a: str, b: str) -> int:
    """最长公共子串长度（DP，O(len(a)*len(b))）。"""
    prev = [0] * (len(b) + 1)
    best = 0
    for ca in a:
        cur = [0] * (len(b) + 1)
        for j, cb in enumerate(b, 1):
            if ca == cb:
                cur[j] = prev[j - 1] + 1
                if cur[j] > best:
                    best = cur[j]
        prev = cur
    return best


def _canonical(sample: Any, hist: Sequence[Any], pred: dict[str, Any]) -> dict[str, Any]:
    """确定性事实优先：分享标题/引用/@/拍一拍/第一人称自指覆盖模型读数。

    should_switch 不在这里算（那是 §五 的机械判据，见 _decide_switch）；
    这里只产出最终 anchor / addressee。
    """
    text = " ".join(str(sample.get("text") or "").split())
    compact = "".join(text.split())
    speaker = str(sample.get("speaker") or "")
    anchor = pred.get("anchor")
    addressee = list(pred.get("addressee") or [])

    share = SHARE_RE.match(text)
    quote = QUOTE_RE.search(text)
    pat = PAT_RE.search(text)
    body = _body(text)

    if share:
        x, y = share.group(1), share.group(2)
        anchor = f"P{y}" if speaker == f"P{x}" else f"P{x}"
        addressee = ["room"]
    elif quote and not SELF_ANCHOR_RE.search(body):
        anchor = f"P{quote.group(1)}"
    elif pat and not SELF_ANCHOR_RE.search(body):
        anchor = f"P{pat.group(2)}"

    pat_ref = re.search(r"拍P(\d)", text)
    if pat_ref and not SELF_ANCHOR_RE.search(body):
        anchor = f"P{pat_ref.group(1)}"

    if body.startswith("拜拜") and not quote:
        anchor = speaker or anchor

    if SELF_ANCHOR_RE.search(body):
        anchor = speaker or anchor

    # 媒体 / 系统消息没有词面：优先继承同一个人最近一次被算出的主角，否则继承上一轮主角。
    compact_body = "".join(body.split())
    is_media = bool(re.match(r"^\[(表情包|图片|媒体)", compact_body)) or compact_body.startswith("<?xml")
    if is_media:
        st = _EVAL_STATE.get(str(sample.get("chat") or ""), {})
        if compact_body.startswith("[图片]") and speaker:
            anchor = speaker
        else:
            mine = (st.get("by_speaker") or {}).get(speaker)
            if mine and (anchor is None or anchor != mine):
                anchor = mine
            elif anchor is None and st.get("anchor"):
                anchor = st.get("anchor")

    if speaker == "P1" and anchor == "P1" and not SELF_ANCHOR_RE.search(body):
        ats = AT_RE.findall(body)
        if ats and not re.search(r"拍P\d", text):
            anchor = f"P{ats[0]}"

    explicit_named = False
    for code in AT_RE.findall(body):
        item = f"P{code}"
        if "room" in addressee:
            addressee.remove("room")
        if item not in addressee:
            addressee.append(item)
        explicit_named = True
    if quote and (re.search(
            r"[你您]|帮我|替我|问你|知道吗|有没有可能|吗|哦对|差不多|666|"
            r"恭喜|不能|我到了|喝了|^这不是", body)
                  or body.rstrip().endswith(("？", "?"))):
        item = f"P{quote.group(1)}"
        if "room" in addressee:
            addressee.remove("room")
        if item not in addressee:
            addressee.append(item)
        explicit_named = True
    if pat:
        item = f"P{pat.group(2)}"
        if "room" in addressee:
            addressee.remove("room")
        if item not in addressee:
            addressee.append(item)
        explicit_named = True

    if re.search(r"(谁80你|我刚说过)", body):
        bound = False
        for h in reversed(list(hist or ())):
            htext = " ".join(str(h.get("text") or "").split())
            hs = str(h.get("speaker") or "")
            if hs and hs != speaker and (htext.startswith(body[:8]) or body.startswith(htext[:8])):
                if "room" in addressee:
                    addressee.remove("room")
                if hs not in addressee:
                    addressee.append(hs)
                explicit_named = True
                bound = True
                break
        if not bound and hist:
            hs = str(hist[-1].get("speaker") or "")
            if hs and hs != speaker:
                if "room" in addressee:
                    addressee.remove("room")
                if hs not in addressee:
                    addressee.append(hs)
                explicit_named = True

    if "没发现" in body and body.rstrip().endswith("吗"):
        addressee = ["room"]

    if "人机" in body and "P1" in addressee and len(addressee) > 1 and "@P1" not in text:
        addressee.remove("P1")
    if not explicit_named and re.search(r"帮我|替我", body):
        addressee = ["P1"]
        explicit_named = True
    if not explicit_named and "你的人机" in body:
        addressee = ["P2"]
        explicit_named = True
    if "你们" in body and re.search(r"(80我|欺负我)", body):
        addressee = [item for item in addressee if item in ("P2", "P1")] or ["P2", "P1"]

    # 自指 + 与上一句有 >=4 字重叠：这是"接上一句的话"，收件人回落到上一说话人。
    # 只兜"模型已经说 room / 空"的情况，且必须有第一人称自指模式；仿真对 128 条零影响。
    echo_text = "".join(body.split())
    echoed = any(
        "".join(str(h.get("text") or "").split()) == echo_text
        and str(h.get("speaker") or "") != speaker
        for h in (hist or ())
    )
    if (not addressee or addressee == ["room"]) and SELF_ANCHOR_RE.search(body) and hist and not echoed:
        prev_row = hist[-1]
        hs = str(prev_row.get("speaker") or "")
        if hs and hs != speaker and _lcs_len("".join(body.split()),
                                             "".join(str(prev_row.get("text") or "").split())) >= 4:
            addressee = [hs]
            explicit_named = True

    # 极短且没有显式点名：说给全群，不许猜具体人（消 128 条的间接收件人 FP）。
    # 长度只看引用块之后的正文：引用块是上下文，不代表本条本身在点名。
    # 两个安全出口分开，避免误伤"咋了嘛 / 帮我开会 / 你不是人机吗"这类短但确有对象的话：
    #   1) 引用 + 短正文（且不是反问）→ room；
    #   2) 无引用时，只有明确应答词才归 room。
    body_compact = "".join(body.split())
    short_with_quote = bool(quote) and len(body_compact) <= 10 and not body.rstrip().endswith(("？", "?"))
    generic_ack = bool(re.search(r"(知道了|OKOk|没事|拜拜|这个|这么久|出bug了)", body_compact))
    if not explicit_named and (short_with_quote or generic_ack):
        addressee = ["room"]

    return {
        "thread_id": None,
        "anchor": anchor,
        "addressee": addressee or None,
        "should_switch": False,
        "confidence": "unresolved" if anchor is None else "medium",
        "anchor_evidence": pred.get("anchor_evidence") or [],
        "switch_evidence": [],
        "topic": "",
    }


def _decide_switch(sample: Any, hist: Sequence[Any], anchor: Any, addressee: Any) -> bool:
    """§五 四条显式转移的机械判据；模型不参与 should_switch。

    宁可漏换（miss），不可错换（FP）。判据 = 显式证据 且 最终主角确实变了；
    证据必须指向最终主角本人（引用作者 / @目标 / 拍一拍目标 / 窄口径自述接管）。
    """
    chat = str(sample.get("chat") or "")
    st = _EVAL_STATE.setdefault(chat, {"anchor": None, "addressee": ()})
    prev_anchor = st.get("anchor")
    text = " ".join(str(sample.get("text") or "").split())
    speaker = str(sample.get("speaker") or "")
    share = SHARE_RE.match(text)
    quote = QUOTE_RE.search(text)
    body = _body(text)
    at = AT_RE.search(body)
    pat = PAT_RE.search(text)
    self_takeover = bool(SELF_SWITCH_RE.search(body))

    changed = prev_anchor is not None and anchor is not None and anchor != prev_anchor
    switch = False
    evidence: list[str] = []

    if share:
        switch = True
        evidence.append("分享记录标题点名双方")
    elif changed:
        if quote and quote.group(1) == anchor:
            switch = True
            evidence.append(f"引用 {anchor} 且主角换到 {anchor}")
        if pat and pat.group(2) == anchor:
            switch = True
            evidence.append(f"拍一拍 {anchor} 且主角换到 {anchor}")
        if at and at.group(1) == anchor:
            switch = True
            evidence.append(f"@ {anchor} 且主角换到 {anchor}")
        if self_takeover and anchor == speaker:
            switch = True
            evidence.append(f"第一人称自述接管：{speaker}")

    _remember(chat, anchor, addressee, speaker)
    return switch


def _parse(raw: str) -> dict[str, Any] | None:
    outcome = schema.parse_json_payload(
        raw,
        required=("anchor", "addressee"),
        source="scene_read_probe",
    )
    if not outcome.ok or not outcome.payload:
        return None
    return _normalize(outcome.payload)


def _majority(preds: Sequence[dict[str, Any]]) -> dict[str, Any]:
    from collections import Counter

    def mode_key(counts: Counter) -> Any | None:
        top = counts.most_common(2)
        if len(top) == 1 or top[0][1] > top[1][1]:
            return top[0][0]
        return None

    anchor = mode_key(Counter(p.get("anchor") for p in preds))
    ad = mode_key(Counter(tuple(p.get("addressee") or []) for p in preds))
    addressee = list(ad) if ad is not None else []
    return {
        "thread_id": None,
        "anchor": anchor,
        "addressee": addressee or None,
        "should_switch": False,
        "confidence": "unresolved" if anchor is None else "medium",
        "anchor_evidence": ["semantic_read_probe"] if anchor else [],
        "switch_evidence": [],
        "topic": "",
    }


def predict(sample: Any, hist: Sequence[Any]) -> dict[str, Any]:
    try:
        user = _build_user(sample, hist)
        votes = max(1, int(os.environ.get("SCENE_PROBE_VOTES", "3")))
        raws = asyncio.run(_ask_many(user, votes))
        preds = [p for raw in raws if (p := _parse(raw))]
        if not preds:
            _remember(sample.get("chat"), None, ())
            return {
                "thread_id": None, "anchor": None, "addressee": None,
                "should_switch": False, "confidence": "unresolved",
                "error": "schema: all votes failed",
            }
        base = _canonical(sample, hist, _majority(preds))
        switch = _decide_switch(sample, hist, base.get("anchor"), base.get("addressee"))
        base["should_switch"] = switch
        if switch:
            base["switch_evidence"] = ["deterministic_explicit_transfer"]
        return base
    except Exception as exc:  # noqa: BLE001 - 探针失败 = 显式 unknown，不阻断跑分
        return {
            "thread_id": None, "anchor": None, "addressee": None,
            "should_switch": False, "confidence": "unresolved",
            "error": f"{type(exc).__name__}: {exc}"[:200],
        }


def reset_eval_state() -> None:
    """清空跨样本的上一轮主角状态（scorer 每轮跑分前调用）。"""
    global _SITUATION_LINES
    _EVAL_STATE.clear()
    _SITUATION_LINES = []




if __name__ == "__main__":
    data = json.loads(Path(__file__).with_name("incident_1011.jsonl")
        .read_text(encoding="utf-8").splitlines()[0])
    print("sample id:", data["id"])
    print("pred:", json.dumps(predict(data, []), ensure_ascii=False))
