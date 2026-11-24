"""鲸鲸短句种子：只读加载 + 状态 × 场景选择。

数据文件：``configs/speech_seed/short_reply_seed.json``。
本模块只读、无副作用、不接生产行为；运行时只应在高置信信号下调用，
拿不准就返回空，不猜、不写库。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping
from safelog import warn

ROOT = Path(__file__).resolve().parents[2]
SEED_PATH = ROOT / "configs" / "speech_seed" / "short_reply_seed.json"


@dataclass(frozen=True)
class ShortReplyKey:
    state: str = ""
    scene: str = ""


@dataclass(frozen=True)
class ShortReplySeed:
    key: str = ""
    version: str = ""
    name: str = ""
    rules: Mapping[str, Any] = field(default_factory=dict)
    states: Mapping[str, Mapping[str, tuple[str, ...]]] = field(default_factory=dict)
    source: str = ""

    def lines(self, state: str, scene: str) -> tuple[str, ...]:
        return tuple(self.states.get(state, {}).get(scene, ()))

    def has(self, state: str, scene: str) -> bool:
        return bool(self.lines(state, scene))

    def scenes(self, state: str) -> tuple[str, ...]:
        return tuple((self.states.get(state) or {}).keys())

    def keys(self) -> tuple[ShortReplyKey, ...]:
        return tuple(
            ShortReplyKey(state=state, scene=scene)
            for state, scenes in self.states.items()
            for scene in scenes
        )

    def total_lines(self) -> int:
        return sum(
            len(lines)
            for scenes in self.states.values()
            for lines in scenes.values()
        )


def _as_lines(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return ()
    seen: set[str] = set()
    out: list[str] = []
    for item in value:
        text = str(item or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
    return tuple(out)


def count_chars(text: str) -> int:
    """与短句种子口径一致：中文逗号不计；'……' 算一个字；其余标点照算。"""
    return len(str(text).replace("……", "…").replace("，", "").replace(",", ""))


def load_short_reply_seed(path: str | Path | None = None) -> ShortReplySeed:
    """读取短句种子；文件缺失/损坏时返回空种子（不抛错、不阻断调用方）。"""
    target = Path(path) if path else SEED_PATH
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - 只读数据，坏了就当没有
        warn("core/speech/short_reply_seed.py:85 Exception", err=repr(__import__("sys").exc_info()[1]))
        return ShortReplySeed(source=str(target))
    if not isinstance(raw, dict):
        return ShortReplySeed(source=str(target))

    states: dict[str, dict[str, tuple[str, ...]]] = {}
    raw_states = raw.get("states") or {}
    if isinstance(raw_states, dict):
        for state, raw_scenes in raw_states.items():
            state_name = str(state or "").strip()
            if not state_name or not isinstance(raw_scenes, dict):
                continue
            scenes: dict[str, tuple[str, ...]] = {}
            for scene, raw_lines in raw_scenes.items():
                scene_name = str(scene or "").strip()
                lines = _as_lines(raw_lines)
                if scene_name and lines:
                    scenes[scene_name] = lines
            if scenes:
                states[state_name] = scenes

    rules = raw.get("rules")
    return ShortReplySeed(
        key=str(raw.get("key") or ""),
        version=str(raw.get("version") or ""),
        name=str(raw.get("name") or ""),
        rules=dict(rules) if isinstance(rules, dict) else {},
        states=states,
        source=str(target),
    )


# 归一化信号 -> 状态 × 场景。调用方负责把运行时信号归一成这些 key；
# 本模块不做文本语义判断。
SIGNAL_TO_KEY: Mapping[str, ShortReplyKey] = {
    "called_name": ShortReplyKey("平常", "叫名字"),
    "presence_check": ShortReplyKey("平常", "在吗"),
    "praised": ShortReplyKey("平常", "被夸"),
    "thanked": ShortReplyKey("平常", "被谢"),
    "asked_favor": ShortReplyKey("平常", "求她办事"),
    "pestered": ShortReplyKey("平常", "被麻烦"),
    "showing_off": ShortReplyKey("平常", "显摆"),
    "belittled": ShortReplyKey("平常", "被小看"),
    "briefed": ShortReplyKey("平常", "收到/被交代"),
    "cared": ShortReplyKey("平常", "被关心"),
    "other_is_sad": ShortReplyKey("平常", "对方难过"),
    "farewell": ShortReplyKey("平常", "道别"),
    "goodnight": ShortReplyKey("平常", "晚安"),
    "challenging": ShortReplyKey("平常", "抬杠"),
    "shocked": ShortReplyKey("平常", "被惊到"),
    "follow_up": ShortReplyKey("平常", "追问"),
    "sudden_recall": ShortReplyKey("平常", "突然想起"),
    "caught_waiting": ShortReplyKey("别扭", "被戳穿_你在等我吧"),
    "caught_caring": ShortReplyKey("别扭", "被戳穿_你是不是在意"),
    "accused_hard_mouth": ShortReplyKey("别扭", "被说_你嘴硬"),
    "she_apologized": ShortReplyKey("别扭", "她道歉"),
    "apologized_to_her": ShortReplyKey("别扭", "被道歉"),
    "low_battery_called": ShortReplyKey("低电量", "被叫"),
    "low_battery_presence_check": ShortReplyKey("低电量", "被问在吗"),
    "cold_offended": ShortReplyKey("冷", "被冒犯"),
    "cold_keep_teasing": ShortReplyKey("冷", "继续逗"),
    "serious_result": ShortReplyKey("正事", "查到了"),
    "reminder_recorded": ShortReplyKey("正事", "记提醒"),
    "refuse_relay": ShortReplyKey("正事", "拒接传话"),
    "task_done": ShortReplyKey("正事", "办成了"),
    "typo_other": ShortReplyKey("特殊", "打错字_对方打错"),
    "typo_self": ShortReplyKey("特殊", "打错字_自己手滑"),
    "confessed": ShortReplyKey("特殊", "被表白"),
    "messed_up": ShortReplyKey("特殊", "搞砸"),
    "caught_red_handed": ShortReplyKey("特殊", "被抓包"),
}

# 第一版只在这些高置信信号下注入；其余 key 先当占位，等真实判据接上再放开。
MVP_SIGNALS = frozenset({
    "called_name",
    "presence_check",
    "praised",
    "thanked",
    "asked_favor",
    "caught_waiting",
    "caught_caring",
    "low_battery_called",
    "low_battery_presence_check",
    "serious_result",
    "reminder_recorded",
    "task_done",
    "typo_other",
    "typo_self",
})


_PRESENCE_RE = re.compile(r"^(在吗|在么|在不在|在没在|在呢|在嘛)[呀啊哦噢]?$")
_THANKS_PREFIXES = ("谢谢", "谢了", "多谢", "感谢", "谢谢你", "谢谢啦", "感谢你", "太谢谢")
_FAVOR_PREFIXES = (
    "帮我", "替我", "帮忙", "帮个忙", "麻烦你",
    "帮我看", "帮我查", "帮我记", "帮我找", "帮我写",
    "帮我做", "帮我买", "帮我拿", "帮我订", "帮我催",
)
_CAUGHT_WAITING_RE = re.compile(r"(在等我|等我吧|是不是在等我|等我了|等我吗|等我么|你在等)")
_CAUGHT_CARING_RE = re.compile(r"(是不是在意|你在意|在意我|关心我|担心我|你在乎|在乎我)")
_PRAISED_RE = re.compile(r"(你真|你太|你好|你是真|你也是真)(厉害|棒|可爱|聪明|强|牛|优秀|好|行)")
_PRAISE_WORDS = ("厉害", "棒", "可爱", "聪明", "优秀", "牛")
_ADDRESS_GREETINGS = ("嘿", "嗨", "hi", "hello", "你好", "喂", "诶")
_ADDRESS_TRIM = "，,。.！!？?、~～ \t\r\n"


def _normalise_text(text: object) -> str:
    return re.sub(r"\s+", "", str(text or "")).strip().lower()


def _strip_punct(text: str) -> str:
    return re.sub(r"[?？!！。.\s]", "", text)


def _strip_address(text: str, names: tuple[str, ...]) -> str:
    out = _normalise_text(text)
    for name in names:
        normalised = _normalise_text(name)
        if normalised:
            out = out.replace(normalised, "")
    for greeting in _ADDRESS_GREETINGS:
        out = out.replace(greeting, "")
    return out.strip(_ADDRESS_TRIM)


def _has_name(text: str, names: tuple[str, ...]) -> bool:
    return any(
        _normalise_text(name) and _normalise_text(name) in text
        for name in names
    )


def derive_signal(
    *,
    inbound: object = "",
    trigger_reason: object = "",
    is_group: bool = False,
    is_owner: bool = False,
    explicit: object = "",
    names: tuple[str, ...] = ("鲸鲸",),
) -> str:
    """把一轮私聊主人的原始信号归一到 MVP 短句 signal。

    保守策略：拿不准返回 ""；只认显式 high-confidence signal、精确短文本模式，
    以及 trigger_reason 里已经规范化的 signal。所有会话范围都开。
    """
    explicit_name = str(explicit or "").strip()
    if explicit_name in MVP_SIGNALS:
        return explicit_name
    trigger_name = str(trigger_reason or "").strip()
    if trigger_name in MVP_SIGNALS:
        return trigger_name

    raw = _normalise_text(inbound)
    if not raw:
        return ""

    # 打错字优先：她自己打错 vs 对方打错，对应不同幕。
    if "你打错" in raw or "你刚打错" in raw:
        return "typo_self"
    if "打错字" in raw or re.search(r"打错(了|吧|字)", raw):
        return "typo_other"

    stripped = _strip_address(raw, names)
    if _PRESENCE_RE.fullmatch(_strip_punct(stripped)):
        return "presence_check"
    if _has_name(raw, names) and not stripped:
        return "called_name"
    if stripped.startswith(_THANKS_PREFIXES):
        return "thanked"
    if stripped.startswith(_FAVOR_PREFIXES) or ("帮我" in stripped and len(stripped) <= 50):
        return "asked_favor"
    if _CAUGHT_WAITING_RE.search(stripped or raw):
        return "caught_waiting"
    if _CAUGHT_CARING_RE.search(stripped or raw):
        return "caught_caring"
    if _PRAISED_RE.search(raw) or (
        _has_name(raw, names) and any(word in raw for word in _PRAISE_WORDS)
    ):
        return "praised"
    return ""

def classify_short_reply(
    signal: str,
    *,
    is_group: bool = False,
    is_owner: bool = False,
    mvp_only: bool = True,
) -> ShortReplyKey | None:
    """把已归一化的信号映射到状态 × 场景。

    MVP 范围：所有会话范围。空信号、未知信号、未列入 MVP 的信号
    （``mvp_only=True``）一律返回 ``None``。
    """
    name = str(signal or "").strip()
    if not name:
        return None
    if mvp_only and name not in MVP_SIGNALS:
        return None
    return SIGNAL_TO_KEY.get(name)


def select_lines(
    seed: ShortReplySeed,
    signal: str,
    *,
    is_group: bool = False,
    is_owner: bool = False,
    limit: int = 3,
    mvp_only: bool = True,
) -> tuple[str, ...]:
    """按信号取本轮候选短句；拿不准返回空元组。"""
    if limit <= 0:
        return ()
    key = classify_short_reply(
        signal,
        is_group=is_group,
        is_owner=is_owner,
        mvp_only=mvp_only,
    )
    if key is None:
        return ()
    return seed.lines(key.state, key.scene)[:limit]
