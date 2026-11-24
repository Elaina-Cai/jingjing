"""把「打出来的字」变成「念得出来的话」。

2026-10-26 P4：TTS 之前必须过这一层。她打出来的东西里有大量**不该念**的字符
——表情、颜文字、括号里的动作、Markdown、链接——直接塞给引擎会念成"星星脸"
"括号笑"。沉默标记（[静默]/[silent]/[不说话]）更要在这里拦掉，否则她会把
"不说话"三个字念出来（`loop/brain.py` 的静默语义就是靠这些标记）。

纪律：
1. 只做删减，不改写措辞（改写属于人设层，不归这里）。
2. 永不抛：清洗失败退回原串截断，宁可念得糙，不要不出声。
3. 长度上限默认 200 字：一条语音条超过 40 秒没人听完。
"""

from __future__ import annotations

import re

SILENCE_MARKERS = ("[静默]", "[silent]", "[不说话]", "(静默)", "（静默）")

# 表情/符号：表情区、杂项符号、装饰符、变体选择符、零宽连接
_EMOJI = re.compile(
    "["
    "\U0001F300-\U0001FAFF"
    "\U00002600-\U000027BF"
    "\U0001F000-\U0001F0FF"
    "\U00002190-\U000021FF"
    "\U0000FE00-\U0000FE0F"
    "\U0000200D"
    "\U00002B00-\U00002BFF"
    "]+"
)
# 括号里的动作/补充：全半角圆括号、方括号、方头括号（不跨行、不超过 30 字）
_BRACKET = re.compile(r"[（(\[【][^（）()\[\]【】\n]{0,30}[）)\]】]")
_CODE = re.compile(r"```.*?```", re.S)
_INLINE_CODE = re.compile(r"`([^`\n]{1,80})`")
_URL = re.compile(r"(?:https?://|www\.)\S+")
_MD_LINK = re.compile(r"\[([^\]\n]{1,60})\]\((?:[^)\s]{1,200})\)")
_MD_EMPH = re.compile(r"(\*\*|__|~~|\*|_|#+\s?)")
_WS = re.compile(r"[ \t\u3000]+")
_MULTI_NL = re.compile(r"\n{2,}")
_MULTI_PUNCT = re.compile(r"([，。！？、,.!?~～…])\1+")
_LEAD_PUNCT = re.compile(r"^[，。！？、,.!?~～…：:；;\s]+")


def has_silence_marker(text: str) -> bool:
    """这段话是不是"我选择不说话"（含标记即视为沉默）。"""
    low = str(text or "").lower()
    return any(marker.lower() in low for marker in SILENCE_MARKERS)


def normalize_for_speech(text: str, *, max_chars: int = 200) -> str:
    """清洗成能念的一行话；空串表示"这句不用念"。"""
    try:
        raw = str(text or "")
        if not raw.strip() or has_silence_marker(raw):
            return ""
        out = _CODE.sub(" ", raw)
        out = _INLINE_CODE.sub(r"\1", out)
        out = _MD_LINK.sub(r"\1", out)
        out = _URL.sub(" ", out)
        out = _BRACKET.sub(" ", out)
        out = _EMOJI.sub("", out)
        out = _MD_EMPH.sub("", out)
        out = out.replace("\r", "\n")
        out = _MULTI_NL.sub("\n", out)
        out = _WS.sub(" ", out)
        out = _MULTI_PUNCT.sub(r"\1", out)
        out = "".join(ch for ch in out if ch == "\n" or ch >= " ")
        out = out.strip()
        out = _LEAD_PUNCT.sub("", out)
        out = out.strip()
        if max_chars and len(out) > max_chars:
            cut = out[:max_chars]
            for sep in ("。", "！", "？", "，", "、", " "):
                idx = cut.rfind(sep)
                if idx >= max_chars // 2:
                    cut = cut[: idx + 1]
                    break
            out = cut.strip()
        return out
    except Exception:  # noqa: BLE001 - 清洗失败也要能出声
        return str(text or "").strip()[: max(0, int(max_chars or 0))]


def is_speakable(text: str, *, max_chars: int = 200) -> bool:
    """有没有值得念的内容（清洗后至少两个字）。"""
    return len(normalize_for_speech(text, max_chars=max_chars)) >= 2
