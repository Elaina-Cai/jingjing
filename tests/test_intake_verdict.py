"""M1：归一 + 判读 + 矩阵（纯函数，零行为变更）。"""

from __future__ import annotations

import io
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.intake import normalize as N          # noqa: E402
from core.intake.observation import Observation  # noqa: E402
from core.verdict import matrix as M             # noqa: E402
from core.verdict import reading as R            # noqa: E402
from core.verdict import risk as K               # noqa: E402
from core.verdict import shadow as S             # noqa: E402

FAILS: list[str] = []
COUNT = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global COUNT
    COUNT += 1
    if cond:
        print(f"  ok  {name}")
    else:
        FAILS.append(f"{name} {detail}")
        print(f"  FAIL {name} {detail}")


def t_normalize() -> None:
    obs = N.normalize({"chat_id": "wxid_a", "text": "今天好累", "sender_name": "Elaina",
                       "chat_type": "private"})
    check("私聊归一", obs.where == "private" and obs.by == "Elaina" and obs.text == "今天好累")
    check("文本形式", obs.form == "text")
    grp = N.normalize({"chat_id": "10000000004@chatroom", "text": "在吗", "mention_to_me": True})
    check("群按 chat_id 形状判", grp.where == "group")
    check("@ 点我记录", grp.mention_to_me is True)
    media = N.normalize({"chat_id": "c", "media": [{"kind": "image", "摘要": "一只猫"}]})
    check("只有媒体 -> media", media.form == "media" and media.media[0]["kind"] == "image")
    quote = N.normalize({"chat_id": "c", "text": "这个呢", "quoted": {"who": "小奇", "text": "原话"}})
    check("引用+正文 -> quote", quote.form == "quote" and quote.quoted["who"] == "小奇")
    sysmsg = N.normalize({"chat_id": "c", "text": "拍了拍我", "system": {"kind": "pat"}})
    check("系统事件 -> system", sysmsg.form == "system" and sysmsg.system["kind"] == "pat")
    long_text = N.normalize({"chat_id": "c", "text": "x" * 9000})
    check("正文截断", len(long_text.text) == N.TEXT_MAX_CHARS)
    check("坏输入不抛", N.normalize(object()).form in ("text", "media", "quote", "system"))
    check("headline 一行", "\n" not in obs.headline())
    check("has_body 判空", not N.normalize({"chat_id": "c"}).has_body())


def t_reading() -> None:
    good = ('```json\n{"quotes":["上次说的那个资料"],"slots":{"target":"小奇"},'
            '"gaps":1,"timing":1,"affect":-0.4,"confidence":0.7}\n```')
    reading = R.parse_reading(good)
    check("解析围栏 JSON", reading is not None)
    check("quotes 落库", reading.quotes == ("上次说的那个资料",))
    check("slots 落库", reading.slots == {"target": "小奇"})
    check("gaps/timing/affect/confidence", (reading.gaps, reading.timing, reading.affect, reading.confidence) == (1, 1, -0.4, 0.7))
    check("has_quote/wants_action", reading.has_quote() and reading.wants_action())
    check("坏 JSON 返 None", R.parse_reading("不是 json") is None)
    check("非对象返 None", R.parse_reading("[1,2]") is None)
    clamp = R.parse_reading({"affect": -9, "confidence": 9, "gaps": -3, "timing": 99})
    check("affect 夹到 -1", clamp.affect == -1.0)
    check("confidence 夹到 1", clamp.confidence == 1.0)
    check("gaps 不为负", clamp.gaps == 0)
    check("timing 非法归 0", clamp.timing == 0)
    many = R.parse_reading({"quotes": ["a", "b", "c", "d"], "slots": {f"s{i}": "v" for i in range(9)}})
    check("quotes 最多 3", len(many.quotes) == 3)
    check("slots 最多 6", len(many.slots) == 6)
    prompt = R.build_reading_prompt(N.normalize({"chat_id": "c", "text": "帮我查天气", "sender_name": "Elaina"}))
    check("prompt 两段", len(prompt) == 2 and prompt[0]["role"] == "system")
    check("prompt 带原话", "帮我查天气" in prompt[1]["content"])


def t_risk() -> None:
    check("read=0", K.risk_of("read") == 0)
    check("write_local=1", K.risk_of("write_local") == 1)
    check("ui_control=1", K.risk_of("ui_control") == 1)
    check("external_send=2", K.risk_of("external_send") == 2)
    check("多效果取最高", K.risk_of(("read", "external_send")) == 2)
    check("不认识的当最高", K.risk_of("quantum_teleport") == 2)
    check("None 当只读", K.risk_of(None) == 0)
    priv = Observation(where="private")
    grp = Observation(where="group")
    check("私聊在场=0", K.others_of(priv) == 0)
    check("群在场=1", K.others_of(grp) == 1)
    check("要发出去=需第三方", K.others_of(priv, risk=2) == 2)
    check("risk_hint 说人话", "收不回来" in K.risk_hint(2))


def t_matrix() -> None:
    plain = R.Reading(confidence=0.8)
    check("没这个手 -> abstain", M.decide(plain, capability=False).disposition == "abstain")
    check("没这个手原因", M.decide(plain, capability=False).reason == "no_capability")
    acting = R.Reading(slots={"target": "小奇"}, confidence=0.9)
    check("要动手却没原文 -> 弃权", M.decide(acting).disposition == "abstain")
    check("弃权原因=no_quote", M.decide(acting).reason == "no_quote")
    unsure = R.Reading(quotes=("原话",), confidence=0.3)
    # 2026-10-21 去掉 confidence 门：confidence 校准差、不可验证，让它当门等于让
    # 模型给个随机数决定要不要说话。有原文、没有缺槽 ⇒ 直接说。
    check("confidence 低但可验证 -> say", M.decide(unsure).disposition == "say")
    check("say 原因 = short", M.decide(unsure).reason == "short")
    missing = R.Reading(quotes=("原话",), slots={"target": "小奇"}, gaps=1, confidence=0.9)
    decision = M.decide(missing)
    check("缺槽 -> ask", decision.disposition == "ask")
    check("ask 指出缺哪个", decision.ask_slot == "target")
    risky = R.Reading(quotes=("原话",), slots={"body": "下雨带伞"}, confidence=0.9)
    decision = M.decide(risky, risk=2)
    check("要对外发 -> do 但要批准", decision.disposition == "do" and decision.reason == "needs_confirm")
    check("do 带后果提示", "收不回来" in decision.hints[0])
    local = M.decide(risky, risk=1)
    check("本机可改 -> 直接办", local.disposition == "do" and local.reason == "direct")
    sad = M.decide(R.Reading(affect=-0.7, confidence=0.9))
    check("情绪负且无事 -> 只说人话", sad.disposition == "say" and sad.reason == "comfort")
    chats = M.decide(R.Reading(affect=0.2, confidence=0.9))
    check("无事无情绪 -> 短回", chats.disposition == "say" and chats.reason == "short")
    check("四处置封闭", set(M.DISPOSITIONS) == {"say", "ask", "do", "abstain"})


def t_examples() -> None:
    """17 个例子里挑 9 个：表格即规格。"""
    cases = [
        ("今天好累", {"affect": -0.7, "confidence": 0.8}, 0, "say"),
        ("在吗", {"confidence": 0.9}, 0, "say"),
        ("帮我查下杭州天气", {"quotes": ["查下杭州天气"], "slots": {"q": "杭州天气"}, "confidence": 0.9}, 0, "do"),
        ("在群里说一句下雨带伞", {"quotes": ["下雨带伞"], "slots": {"body": "下雨带伞"}, "confidence": 0.9}, 2, "do"),
        ("你看着办吧", {"confidence": 0.2}, 0, "say"),
        ("上次那事你办了吗", {"quotes": [], "slots": {"什么": ""}, "gaps": 1, "confidence": 0.9}, 0, "abstain"),
        ("把那个发我", {"quotes": ["那个"], "slots": {}, "gaps": 1, "confidence": 0.7}, 2, "ask"),
        ("别在群里说了", {"affect": -0.4, "confidence": 0.8}, 0, "say"),
        ("转房租给我妈", {"quotes": ["转房租"], "slots": {"to": "妈"}, "confidence": 0.9}, 2, "do"),
    ]
    for text, payload, risk, expected in cases:
        decision = M.decide(R.parse_reading(payload), risk=risk)
        check(f"例子：{text} -> {expected}", decision.disposition == expected, f"实际={decision.disposition}/{decision.reason}")


def t_shadow() -> None:
    check("旧意图映射", S.map_old("指令") == "do" and S.map_old("闲聊") == "say")
    check("认不出返空串", S.map_old("天知道") == "")
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    logger = logging.getLogger("core.verdict.shadow")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        agree = S.compare(old_intent="闲聊", decision=M.decide(R.Reading(confidence=0.9)),
                          chat_id="c1", where="private")
        disagree = S.compare(old_intent="指令", decision=M.decide(R.Reading(confidence=0.9)),
                            chat_id="c2", old_would_reply=True)
        forced = S.compare(old_intent="闲聊", decision=M.decide(R.Reading(confidence=0.9)),
                           old_would_reply=False)
        text = stream.getvalue()
    finally:
        logger.removeHandler(handler)
    check("一致的判一致", agree is True)
    check("不一致的判不一致", disagree is False)
    check("旧路不回而新路说 -> 不一致", forced is False)
    check("日志一行含 agree", "verdict_shadow" in text and "agree=1" in text)


def main() -> int:
    for fn in (t_normalize, t_reading, t_risk, t_matrix, t_examples, t_shadow):
        print(fn.__name__)
        fn()
    print(f"RESULT {len(FAILS)} failed / {COUNT} checks")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
