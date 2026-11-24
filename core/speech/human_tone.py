# -*- coding: utf-8 -*-
"""说话质感读数（批D 路线 6 人话工作流，第一步：确定性审计）。

回答一个问题：这一句像不像真人说的（还是模型腔）。
* 只审计、不改文本：off 一行不走；shadow/on 都记 ``human_tone_shadow``；
* 确定性：负特征词表 + 结构特征（编号/加粗/连接词堆叠）+ 人味特征（语气词/短句/表情）；
* 纯函数 + 一次 log；不读库、不调 LLM。
"""
from __future__ import annotations

import re

import structlog
from safelog import warn

log = structlog.get_logger(__name__)

__all__ = ["human_tone_mode", "humanize", "rewrite_v0", "tone_flags", "audit_fields", "note_outbound", "MODEL_MARKERS", "HUMANIZER_MARKERS", "COLLOQUIAL"]

#: 模型腔负特征（命中即记一条；词表是纪律：只加"模型味"的，不加个人口癖）。
MODEL_MARKERS = (
    "作为一个",
    "我可以帮你",
    "如果你需要",
    "希望对你有帮助",
    "希望能帮到你",
    "需要注意的是",
    "总的来说",
    "总结一下",
    "以下",
    "不要犹豫",
    "随时告诉我",
    "当然可以",
    "让我来",
    "让我们",
    "首先",
    "其次",
    "总之",
    "好的，",
    "。希望",
)

#: humanizer skill（blader/humanizer，MIT）映射后的中文模型腔词表（2026-10-15）。
#: 来源 26 条模式中适用于群聊短句的部分；长文结构级模式（排比/收尾句/标题装饰）
#: 与冲突项（§2 短句碎片=鲸鲸风格）不在词表里，见 docs/decisions/humanizer_映射.md。
HUMANIZER_MARKERS = (
    # §3 deep sayings
    "归根结底", "说到底", "事实上", "本质上", "真正的",
    # §4 staged run-up
    "让我先", "先说明一下", "请注意", "提醒一下",
    # §5 arguing with no one
    "有人可能会说", "也许你会问", "你可能会觉得", "你可能以为", "别误会", "这不是说", "我要澄清",
    # §9 stacked qualifiers
    "某种意义上", "某种程度上", "或许可以说", "可以说", "可能大概",
    # §12 overused AI words
    "赋能", "打造", "助力", "全方位", "一站式", "解锁", "至关重要", "深入探讨", "彰显", "谱写",
    # §13 inflated significance
    "标志着", "里程碑", "关键的转折",
    # §16 sales language
    "无与伦比", "令人惊叹", "美不胜收",
    # §17 borrowed authority
    "专家认为", "业内人士", "数据显示", "据报道",
    # §18 avoids is/are/has
    "发挥着", "扮演着", "充当着",
    # §22 chatbot residue
    "还有什么需要", "需要我继续吗", "很高兴", "希望这对你",
    # §23 knowledge-limit disclaimers
    "据我所知", "根据我的了解", "截至我",
)

#: 人味正特征（语气词；只作读数）。
COLLOQUIAL = ("啊", "呀", "吧", "嘛", "呢", "哈", "诶", "哦", "噢", "嘿", "唉", "嗯", "嘞", "呗")
_EMOJI_RE = re.compile(r"[🌀-🫿☀-➿]")
_CONNECTORS = ("首先", "其次", "然后", "最后", "总之", "综上")
_LIST_RE = re.compile(r"(?m)^\s*(?:\d+[.、)]|[-*])\s+")


#: 套话开头（删除时连同其后的逗号/句号一起去掉）。
_OPENING_FILLERS = (
    "好的，", "好的。", "好的呀，", "好嘞，", "没问题，", "没问题。",
    "当然，", "当然。", "首先，", "其次，", "总之，", "总的来说，",
    "说白了，", "讲真，", "说实话，", "让我先说明一下，",
)


def humanize(text: str) -> str:
    """人化减法层（确定性）：只删不写，删不动就返回原文。

    规则（群聊短句口径）：1) 开头套话前缀直接删；2) "（笑）" 类动作标记删；
    3) 全句唯一的末尾句号删（多句保留）；4) 删完为空/只剩标点 -> 返回原文。
    """
    raw = str(text or "").strip()
    if not raw:
        return raw
    out = raw
    for filler in _OPENING_FILLERS:
        if out.startswith(filler) and len(out) > len(filler) + 1:
            out = out[len(filler):].lstrip("，。 ")
            break
    out = out.replace("（笑）", "").replace("(笑)", "")
    if out.count("。") == 1 and out.endswith("。") and len(out) > 1:
        out = out[:-1]
    out = out.strip()
    if not out or not any(ch.isalnum() or "一" <= ch <= "鿿" for ch in out):
        return raw
    return out


_DASH = chr(0x2014)
_REWRITE_OPENING_RE = re.compile(r"(^|[。！？；\n])\s*(?:其实|说实话|说白了|讲真|老实说)[，,]\s*")
_REWRITE_TAIL_MARKERS = ("其实我", "我这边", "不然的话", "免得", "顺便说", "还有就是")


def rewrite_v0(text: str) -> tuple[str, tuple[str, ...]]:
    """确定性人话重写 v0（只做减法；影子期只记不改）。

    规则：①破折号→逗号；②删句首填充词（其实/说实话/说白了/讲真/老实说）；
    ③超 30 字含逗号：尾句是补充说明就删掉，否则把第一个逗号换成换行（分条近似）。
    """
    out = str(text or "")
    rules: list[str] = []
    if _DASH in out:
        out = re.sub(_DASH + r"+", "，", out)
        out = re.sub(r"，{2,}", "，", out)
        rules.append("dash")
    new, n = _REWRITE_OPENING_RE.subn(r"\1", out)
    if n:
        out = new
        rules.append("filler")
    if len(out) > 30 and "，" in out:
        parts = out.split("，")
        if len(parts) > 2 and any(parts[-1].startswith(m) for m in _REWRITE_TAIL_MARKERS):
            out = "，".join(parts[:-1])
            rules.append("tail")
        else:
            cut = out.find("，")
            if 0 < cut < len(out) - 1:
                out = out[:cut] + "\n" + out[cut + 1:]
                rules.append("split")
    return out, tuple(rules)


def human_tone_mode() -> str:
    """``YINYUE_FEATURE_HUMAN_TONE`` 三态；读不到按 off。"""
    try:
        from core import features

        return features.mode(features.HUMAN_TONE, features.MODE_OFF)
    except Exception:  # noqa: BLE001 - flag 读不到按 off，不阻断
        warn("core/speech/human_tone.py:148 Exception", err=repr(__import__("sys").exc_info()[1]))
        return "off"


def tone_flags(text: str) -> tuple[str, ...]:
    """命中的模型腔特征（可回放；不含人味读数）。"""
    body = str(text or "")
    flags = [marker for marker in MODEL_MARKERS if marker in body]
    flags += [marker for marker in HUMANIZER_MARKERS if marker in body]
    if len([c for c in _CONNECTORS if c in body]) >= 2:
        flags.append("连接词堆叠")
    if body.count("——") >= 2 or body.count("—") >= 3:
        flags.append("破折号堆叠")
    if _LIST_RE.search(body):
        flags.append("编号/列表")
    if "**" in body:
        flags.append("加粗")
    return tuple(dict.fromkeys(flags))


def audit_fields(text: str) -> dict:
    """收数用字段：长度 / 负特征 / 人味读数 / 是否模型腔。"""
    body = str(text or "")
    flags = tone_flags(body)
    sentences = [s for s in re.split(r"[。！？!?\n]+", body) if s.strip()]
    short = [s for s in sentences if len(s.strip()) <= 10]
    colloquial = [c for c in COLLOQUIAL if c in body]
    return {
        "chars": len(body),
        "flags": list(flags),
        "flag_n": len(flags),
        "colloquial_n": len(colloquial),
        "short_ratio": round(len(short) / len(sentences), 2) if sentences else 0.0,
        "emoji": bool(_EMOJI_RE.search(body)),
        "model_like": len(flags) >= 2,
    }


def note_outbound(text: str, *, chat_id: str = "") -> dict:
    """记一条出站说话质感读数；off 直接返回空 dict（零副作用）。"""
    if human_tone_mode() == "off":
        return {}
    fields = audit_fields(text)
    try:
        log.info("human_tone_shadow", chat_id=str(chat_id or ""), **fields)
    except Exception:  # noqa: BLE001 - 审计失败绝不拖垮发送
        warn("core/speech/human_tone.py:193 Exception", err=repr(__import__("sys").exc_info()[1]))
        return fields
    return fields
