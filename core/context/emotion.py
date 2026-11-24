# -*- coding: utf-8 -*-
"""④情绪模块（新架构批 A3）：理解时刻的积分与衰减。

设计图口径：**只算不存**——状态归②，本模块在③理解之后把这一轮的
「行为事实 + 理解证据」折成一次状态更新，写回②；衰减在读时按真实时间计算。

科学口径（不重造轮子）：
* 动力学完全复用 ``core.affect.engine``（半衰期指数回归 + delta 阶梯）；
* **行为信号只来自可观测事实**（谁先来、她等了多久），消息内容一个字不进；
* **理解证据只来自③的结构化判定**（owner_state / genre / targets），
  不做二次语义解读；每条证据一个独立 reason key，权重集中在
  :data:`EMOTION_EVIDENCE` 一处，验收期可调、可审计。

写入纪律：只有「有增量」的那一轮才写 ``emotion_observed``（纯衰减不写事件），
因此事件流里每一条都能回答「为什么变了」。
"""
from __future__ import annotations

import os

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping

from core.affect import engine
from core.affect.settings import AffectDynamics
from core.contracts.affect import AffectContext, AffectSignals, AffectState
from safelog import warn

__all__ = [
    "EMOTION_EVIDENCE",
    "MOOD_HALF_LIFE_HOURS",
    "MoodState",
    "attitude_toward",
    "current_emotion",
    "current_mood",
    "derive_behavior_signals",
    "derive_understanding_evidence",
    "layers_digest",
    "layers_mode",
    "tone_hint",
    "tone_prompt_block",
    "update_emotion",
]

#: 理解证据 → ``(delta, reason)``。权重是**验收期调参点**；每条一个原因键。
#: 口径：维护她 = 被重视（正）；她被起哄 = 负；主人被晾着 = 她感到被冷落（负）。
EMOTION_EVIDENCE: Mapping[str, tuple[float, str]] = {
    "defended": (0.20, "defended"),
    "left_out": (-0.25, "left_out"),
    "teased": (-0.10, "teased"),
}

_STAMP_FORMATS = ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S")


def _default_dynamics() -> AffectDynamics:
    try:
        from core.affect import settings as affect_settings

        return affect_settings.load(None).dynamics
    except Exception:  # noqa: BLE001 - 配置读不到就用契约默认，绝不阻断
        warn("core/context/emotion.py:61 Exception", err=repr(__import__("sys").exc_info()[1]))
        return AffectDynamics()


def _row_get(row: Any, name: str, default: Any = None) -> Any:
    if isinstance(row, Mapping):
        return row.get(name, default)
    return getattr(row, name, default)


def _real_rows(rows: Any) -> list[Any]:
    out: list[Any] = []
    for row in rows or ():
        if _row_get(row, "is_pat", False) or _row_get(row, "is_revoke", False):
            continue
        out.append(row)
    return out


def _is_self_row(row: Any, self_names: Any) -> bool:
    if bool(_row_get(row, "is_self", False)):
        return True
    speaker = str(_row_get(row, "speaker", "") or "")
    names = {str(name).strip() for name in (self_names or ()) if str(name).strip()}
    return bool(speaker and speaker in names)


def _row_time(row: Any) -> datetime | None:
    value = _row_get(row, "occurred_at", None)
    return value if isinstance(value, datetime) else None


def derive_behavior_signals(data: Any) -> AffectSignals:
    """理解时刻的行为信号：谁先来 / 她等了多久（只读时序与角色，不读内容）。"""
    if bool(getattr(data, "is_group", False)):
        # 群聊语义修正（2026-10-16）：只有「这一轮确实在对她说 / 在说她」才算她收到正向信号。
        # 此前一律 owner_initiated=True —— N1 现在会跑很多「别人互聊」的 ambient 回合，
        # 每轮 +0.15 且群内间隔几秒衰减可忽略 → 情绪/mood 被顶到 1.0 饱和（验收单缺陷）。
        her_turn = (
            bool(getattr(data, "explicit_address", False))
            or bool(getattr(data, "summons", ()) or ())
            or str(getattr(data, "about_me", "") or "") == "me"
        )
        return AffectSignals(owner_initiated=True) if her_turn else AffectSignals()

    # 完整窗口优先："上一条是谁"是时序事实，准入子集是过滤后的证据，
    # 用它读时序会把被过滤掉的最后一条（例如她的发言）读丢。
    rows = list(getattr(data, "window_rows", ()) or ()) or list(
        getattr(data, "admitted_rows", ()) or ()
    )
    rows = _real_rows(rows)
    now = getattr(data, "now", None)
    if not isinstance(now, datetime):
        now = datetime.now()
    # 窗口若把本轮自己也带上了，先摘掉——信号必须来自「本轮之前」。
    if rows:
        last_text = " ".join(str(_row_get(rows[-1], "content", "") or "").split())
        current = " ".join(str(getattr(data, "current", "") or "").split())
        last_speaker = str(_row_get(rows[-1], "speaker", "") or "")
        sender = str(getattr(data, "sender_name", "") or "")
        last_time = _row_time(rows[-1])
        # 只有「同一发言人 + 同文 + 时间就在当下」才认定窗口把本轮自己带上了；
        # 否则 5 分钟前同人同文的一条历史消息会被误删。
        same_now = bool(
            last_time is not None
            and abs((now - last_time).total_seconds()) <= 90
        )
        if (
            last_text and last_text == current and same_now
            and (not sender or last_speaker == sender)
        ):
            rows = rows[:-1]
    if not rows:
        return AffectSignals()

    last = rows[-1]
    if _is_self_row(last, getattr(data, "self_names", ())):
        spoken_at = _row_time(last)
        waited = 0.0
        if spoken_at is not None:
            waited = max(0.0, (now - spoken_at).total_seconds() / 60.0)
        return AffectSignals(replied_to_her=True, waited_minutes=waited)
    return AffectSignals(owner_initiated=True)


def _her_names(data: Any) -> set[str]:
    return {
        str(name).strip()
        for name in (getattr(data, "self_names", ()) or ())
        if str(name).strip()
    }


def derive_understanding_evidence(data: Any, section: Any) -> tuple[float, str]:
    """③理解证据 → ``(delta, reason)``；只读结构化字段，不做二次解读。

    * ``owner_state`` 含「维护」→ 被维护（正）；
    * ``owner_state`` 含「晾着」→ 被冷落（负）；
    * ``genre`` 是起哄/玩笑/调侃 **且 targets 命中她** → 被起哄（负）。
      （只判「她是不是被打趣的对象」；主人被逗本身不移动她的状态。）
    """
    if not isinstance(section, Mapping):
        return 0.0, ""
    owner_state = str(section.get("owner_state") or "")
    if "维护" in owner_state:
        return EMOTION_EVIDENCE["defended"]
    if "晾着" in owner_state:
        return EMOTION_EVIDENCE["left_out"]
    genre = str(section.get("genre") or "")
    targets = {str(item) for item in (section.get("targets") or ())}
    if genre in ("起哄", "玩笑", "调侃") and targets & _her_names(data):
        return EMOTION_EVIDENCE["teased"]
    return 0.0, ""


def _parse_stamp(raw: Any) -> datetime | None:
    text = str(raw or "").strip()
    if not text:
        return None
    failed = 0
    for fmt in _STAMP_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            failed += 1
            continue
    # 2026-11-22：以前每试一种格式失败就 warn 一次，ISO 时间戳会稳定写两条噪声进
    # silent_warnings.log（第三种格式其实能解析）。只在全部失败时出声。
    warn("emotion_stamp_unparsed", value=text[:40])
    return None


def _to_affect_state(emotion: Any) -> AffectState:
    return AffectState(
        level=float(getattr(emotion, "level", 0.0) or 0.0),
        updated_at=_parse_stamp(getattr(emotion, "updated_at", "")),
        reason=str(getattr(emotion, "reason", "") or ""),
    )


def _previous_emotion_for(model: Any, who_id: str) -> Any:
    """同一个人自己的上一条情绪；群里换人发言不许继承别人的水平。

    单槽投影只装"最后一条"，所以先看投影；不是同一个人就回事件流反查
    （事件不可变、只追加，反查即最新）。查不到 = 这个人还没有历史，从基线起算。
    """
    from core.context.situation import EmotionState, project

    latest = project(model.events).emotion
    if not who_id or not latest.updated_at or latest.who_id == who_id:
        return latest
    for event in reversed(getattr(model, "events", ()) or ()):
        if getattr(event, "kind", "") != "emotion_observed":
            continue
        payload = getattr(event, "payload", {}) or {}
        if str(payload.get("who_id") or "") != who_id:
            continue
        return EmotionState(
            level=float(payload.get("level", 0.0) or 0.0),
            updated_at=str(getattr(event, "at", "") or ""),
            reason=str(payload.get("reason") or ""),
            who=str(payload.get("who") or ""),
            who_id=str(payload.get("who_id") or ""),
        )
    return EmotionState()


def current_emotion(model: Any, *, now: datetime | None = None) -> Any:
    """②情绪投影**此刻**的值；给了 ``now`` 就按真实时间衰减（只读不写）。"""
    from core.context.situation import EmotionState, project

    stored = project(model.events).emotion
    if now is None or not stored.updated_at:
        return stored
    faded = engine.decay(_to_affect_state(stored), now, dynamics=_default_dynamics())
    return EmotionState(
        level=faded.level, updated_at=stored.updated_at, reason=stored.reason,
        who=stored.who, who_id=stored.who_id,
    )


def update_emotion(
    model: Any,
    *,
    data: Any,
    section: Any,
    at: str = "",
    dynamics: AffectDynamics | None = None,
) -> Any:
    """③→④：衰减 + 行为信号 + 理解证据 → 有增量才写回②。

    行为通道完整复用 :func:`core.affect.engine.observe`（先衰减、再加 delta、
    起因 doctrine 一字不改）；理解证据按**同一条** doctrine 叠加在行为通道之上。
    没有增量的回合不写事件（纯衰减由读取侧 :func:`current_emotion` 按真实时间算）。
    返回新投影（:class:`EmotionState`）；没有增量返回 ``None``。
    """
    from core.affect.bridge import mirror_affect_to_world
    from core.context.situation import EmotionState, project

    who = str(getattr(data, "sender_name", "") or "")
    who_id = str(getattr(data, "sender_id", "") or "")
    # 按人续算：谁的情绪从谁自己那条事件衰减/积分，群聊换人不串账。
    previous = _previous_emotion_for(model, who_id)
    now = getattr(data, "now", None)
    if not isinstance(now, datetime):
        now = datetime.now()
    dyn = dynamics or _default_dynamics()

    behavior = derive_behavior_signals(data)
    behavior_delta, _ = engine.assess(behavior, dynamics=dyn)
    evidence_delta, evidence_reason = derive_understanding_evidence(data, section)
    if abs(behavior_delta) < 1e-9 and abs(evidence_delta) < 1e-9:
        return None

    # 行为通道：engine 既有的「先衰减、再加 delta、起因要能解释状态」整套口径。
    base = engine.observe(_to_affect_state(previous), behavior, now, dynamics=dyn)
    if abs(evidence_delta) < 1e-9:
        level, reason = base.level, base.reason
    else:
        level = engine.clamp(
            base.level + evidence_delta, dyn.min_level, dyn.max_level,
        )
        # 与 engine.observe 同一条 doctrine：新起因只有在把状态推向自己这一边时
        # 才准替换旧起因；否则旧起因才是当前水平的来源，保留它。
        reason = (
            evidence_reason
            if level * evidence_delta > 0
            else (base.reason or evidence_reason)
        )

    stamp = str(at or "") or now.strftime("%Y-%m-%d %H:%M")
    mirror_affect_to_world(
        chat_id=str(getattr(data, "chat_id", "") or ""),
        model=model,
        who=who or who_id,
        who_id=who_id,
        affect=AffectContext(
            state=AffectState(level=level, updated_at=now, reason=reason),
        ),
        at=stamp,
    )
    return EmotionState(
        level=level, updated_at=stamp, reason=reason, who=who or who_id, who_id=who_id,
    )


# ---------------------------------------------------------------------------
# 批D Step 35：情绪分层（心境慢层 / 对人的态度 / 语气提示）
# ---------------------------------------------------------------------------
MOOD_HALF_LIFE_HOURS = 48.0
_MOOD_HALF_LIFE_ENV = "YINYUE_MOOD_HALF_LIFE_HOURS"
_MOOD_HALF_LIFE_MIN = 1.0
_MOOD_HALF_LIFE_MAX = 720.0


@dataclass(frozen=True)
class MoodState:
    """慢层读数：level -1..+1（0 = 稳态）；samples = 参与折叠的事件条数。"""

    level: float = 0.0
    label: str = "平"
    updated_at: str = ""
    samples: int = 0


def layers_mode() -> str:
    """EMOTION_LAYERS 三态：off / shadow / on；读不到按 off，不阻断。"""
    try:
        from core import features

        return features.mode(features.EMOTION_LAYERS, features.MODE_OFF)
    except Exception:  # noqa: BLE001 - flag 读不到按 off
        warn("core/context/emotion.py:327 Exception", err=repr(__import__("sys").exc_info()[1]))
        return "off"


def mood_half_life_hours() -> float:
    raw = (os.environ.get(_MOOD_HALF_LIFE_ENV) or "").strip()
    if not raw:
        return MOOD_HALF_LIFE_HOURS
    try:
        value = float(raw)
    except ValueError:
        warn("core/context/emotion.py:337 ValueError", err=repr(__import__("sys").exc_info()[1]))
        return MOOD_HALF_LIFE_HOURS
    if value <= 0:
        return MOOD_HALF_LIFE_HOURS
    return min(max(value, _MOOD_HALF_LIFE_MIN), _MOOD_HALF_LIFE_MAX)


def _label_of(level: float) -> str:
    if level >= 0.5:
        return "很暖"
    if level >= 0.15:
        return "偏暖"
    if level > -0.15:
        return "平"
    if level > -0.5:
        return "偏冷"
    return "很冷"


def _fold_events(events, *, who_id="", half_life_hours=None, now=None):
    """按 0.5 ** (age_hours / H) 折叠任意 emotion_observed 事件流；返回 (level, samples, stamp)。

    2026-10-23：从 ``_fold_emotion_events`` 里提出来的纯函数——跨会话读数要能折叠
    **多份世界模型的事件**，而原来的写法只看得见一份 ``model.events``。
    """
    from datetime import datetime as _dt

    if now is None:
        now = _dt.now()
    hours = half_life_hours or mood_half_life_hours()
    total = 0.0
    samples = 0
    last_stamp = ""
    for event in events or ():
        if str(getattr(event, "kind", "") or "") != "emotion_observed":
            continue
        payload = getattr(event, "payload", {}) or {}
        if who_id and str(payload.get("who_id") or "") != who_id:
            continue
        stamp = _parse_stamp(getattr(event, "at", "") or "")
        if stamp is None:
            continue
        age_h = max(0.0, (now - stamp).total_seconds() / 3600.0)
        weight = 0.5 ** (age_h / hours) if hours > 0 else 0.0
        total += float(payload.get("level", 0.0) or 0.0) * weight
        samples += 1
        raw_stamp = str(getattr(event, "at", "") or "")
        if not last_stamp or raw_stamp > last_stamp:
            last_stamp = raw_stamp
    return engine.clamp(total, -1.0, 1.0), samples, last_stamp


def _fold_emotion_events(model, *, who_id="", half_life_hours=None, now=None):
    """单份世界模型的读数（老口径，行为逐字不变）。"""
    return _fold_events(
        getattr(model, "events", ()) or (),
        who_id=who_id,
        half_life_hours=half_life_hours,
        now=now,
    )


def current_mood(model, *, now=None, half_life_hours=None) -> MoodState:
    level, samples, stamp = _fold_emotion_events(
        model, half_life_hours=half_life_hours, now=now,
    )
    return MoodState(level=level, label=_label_of(level), updated_at=stamp, samples=samples)


def attitude_toward(model, *, who_id="", who="", now=None, half_life_hours=None) -> MoodState:
    level, samples, stamp = _fold_emotion_events(
        model, who_id=who_id, half_life_hours=half_life_hours, now=now,
    )
    return MoodState(level=level, label=_label_of(level), updated_at=stamp, samples=samples)


def tone_hint(mood, attitude, *, who: str = "") -> str:
    """表达侧可用的语气提示（确定性、无模型）：只读数，不判要不要发。"""
    mood = mood or MoodState()
    attitude = attitude or MoodState()
    name = who or "他"
    if mood.level <= -0.5 or attitude.level <= -0.5:
        return "此刻心境%s、对%s态度%s：收着点，少接话、别热络。" % (mood.label, name, attitude.label)
    if mood.level >= 0.35 or attitude.level >= 0.35:
        return "此刻心境%s、对%s态度%s：可以放松一点，主动也不突兀。" % (mood.label, name, attitude.label)
    if mood.level <= -0.15 or attitude.level <= -0.15:
        return "此刻心境%s、对%s态度%s：保持平常，不必刻意亲近。" % (mood.label, name, attitude.label)
    return "此刻心境%s、对%s态度%s：照常即可。" % (mood.label, name, attitude.label)


def layers_digest(model, *, who_id="", who="", now=None) -> dict:
    mood = current_mood(model, now=now)
    att = attitude_toward(model, who_id=who_id, who=who, now=now)
    return {
        "mood_level": round(mood.level, 3),
        "mood_label": mood.label,
        "mood_samples": mood.samples,
        "attitude_level": round(att.level, 3),
        "attitude_label": att.label,
        "attitude_samples": att.samples,
        "tone": tone_hint(mood, att, who=who),
    }


def tone_prompt_block(model, *, who_id: str = "", who: str = "", now=None) -> str:
    """生成侧 [情绪语气] 块；中性读数（照常）不注入，返回空串不占 prompt。"""
    digest = layers_digest(model, who_id=who_id, who=who, now=now)
    hint = str(digest.get("tone") or "")
    if not hint or "照常即可" in hint:
        return ""
    return f"[情绪语气] {hint}"


# ------------------------------------------------ 跨会话读数（工具拉取用）
# 2026-10-23 主人拍板：情绪层不进提示词，改由模型自己填 person_id 来取；
# 取的时候**跨会话合并**——她此刻的心境是整个人格的状态，不该按聊天窗口切开；
# 对某个人的态度同理（同一个人在不同群里的读数要合成一条）。
def models_for_reading(chat_id: str = "") -> list:
    """取材范围：给了 chat_id 就只读那一个会话，否则所有进程内已知的会话。"""
    from core.context.situation_store import all_world_models, get_world_model

    key = str(chat_id or "").strip()
    if key:
        return [get_world_model(key)]
    try:
        # 2026-10-23：跨会话合并要包括**盘上已落盘但本进程还没载入**的会话，
        # 否则"跨会话"等于"跨本进程开过的会话"，重启后情绪读数凭空归零。
        return list(all_world_models().values())
    except Exception:  # noqa: BLE001 - 读不到就当没有
        warn("core/context/emotion.py:465 Exception", err=repr(__import__("sys").exc_info()[1]))
        return []


def _events_of(models) -> list:
    out: list = []
    for model in models or ():
        out.extend(getattr(model, "events", ()) or ())
    return out


def mood_across_chats(*, chat_id: str = "", now=None, half_life_hours=None,
                      models=None) -> MoodState:
    picked = list(models) if models is not None else models_for_reading(chat_id)
    level, samples, stamp = _fold_events(
        _events_of(picked), half_life_hours=half_life_hours, now=now,
    )
    return MoodState(level=level, label=_label_of(level), updated_at=stamp, samples=samples)


def attitude_across_chats(who_id: str = "", *, chat_id: str = "", now=None,
                          half_life_hours=None, models=None) -> MoodState:
    picked = list(models) if models is not None else models_for_reading(chat_id)
    level, samples, stamp = _fold_events(
        _events_of(picked), who_id=str(who_id or ""), half_life_hours=half_life_hours, now=now,
    )
    return MoodState(level=level, label=_label_of(level), updated_at=stamp, samples=samples)
