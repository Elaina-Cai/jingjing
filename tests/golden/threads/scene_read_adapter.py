# -*- coding: utf-8 -*-
"""Step 4 薄适配器：事实层 + 世界模型 + 单次 N1 + 确定性 switch。

* 事实层：core.context.scene_read（显式 @/引用/拍/父链）；
* 世界模型：core.context.situation（事件溯源 + BDI 投影），本适配器用
  确定性抽取器把每条消息里的自报/文件/提问落成事件，渲染进下一轮 prompt；
* 语义：复用 semantic_read_probe 的单次调用（含既有 canonical 规则）；
* switch：沿用探针里的确定性 §五 判据。

不新增 LLM 调用；本模块只做编排，不重新实现事实解析。
"""
from __future__ import annotations

import re
from typing import Any, Mapping, Sequence

from core.context.scene_read import build_scene_facts
from core.context.situation import (
    WorldModel,
    apply_world_updates,
    open_intention,
    render_situation,
)
from tests.golden.threads import semantic_read_probe as probe

_AGENT = "scene_read_adapter"
_MODELS: dict[str, WorldModel] = {}
_PREV_ANCHOR: dict[str, str] = {}
_LAST_FILE_SENDER: dict[str, str] = {}


def _key(sample: Any) -> str:
    return str(sample.get("chat") or "")


def _model(sample: Any) -> WorldModel:
    return _MODELS.setdefault(_key(sample), WorldModel())


def deterministic_updates(sample: Any) -> dict[str, list[dict[str, Any]]]:
    """从一条消息里抽可核验的状态更新（离线模拟 N1 的 observe）。"""
    speaker = str(sample.get("speaker") or "")
    text = " ".join(str(sample.get("text") or "").split())
    observe: list[dict[str, Any]] = []

    location = re.search(r"(还没到|在路上|刚出门|我到了|到了|在宿舍|在主教|刚出来)", text)
    if location:
        observe.append({
            "key": f"{speaker}.location",
            "value": location.group(1),
            "source": "self_reported",
        })

    if re.search(r"(没有|没收到|缺).{0,6}文件", text):
        observe.append({
            "key": f"{speaker}.file",
            "value": "缺文件",
            "source": "self_reported",
        })
    if re.search(r"\.(sql|doc|docx|pdf|zip)$", text):
        observe.append({
            "key": f"{speaker}.file",
            "value": "已发文件",
            "source": "chat_observed",
        })

    state = re.search(r"(好冷|好热|冷死了|饿|困)", text)
    if state:
        observe.append({
            "key": f"{speaker}.state",
            "value": state.group(1),
            "source": "self_reported",
        })

    return {"observe": observe, "abduce": [], "retract": []}


def _postprocess(sample: Any, hist: Sequence[Any], pred: dict[str, Any]) -> dict[str, Any]:
    """通用后处理：媒体继承、文件名归说话人、无显式证据的自述/短句归说话人。"""
    chat = _key(sample)
    speaker = str(sample.get("speaker") or "")
    text = " ".join(str(sample.get("text") or "").split())
    facts = build_scene_facts(sample, hist)
    anchor = pred.get("anchor")
    addressee = list(pred.get("addressee") or [])
    is_media = text.startswith(("[表情包", "[图片]"))
    is_file = bool(re.search(r"\.(sql|doc|docx|pdf|zip)$", text))

    if re.match(r"^P\d与P\d的聊天记录", text):
        pass
    elif is_media:
        prev = _PREV_ANCHOR.get(chat)
        if prev:
            anchor = prev
        sender = _LAST_FILE_SENDER.get(chat)
        if sender and (not addressee or addressee == ["room"]):
            addressee = [sender]
    elif is_file:
        anchor = speaker
        addressee = ["room"]
    elif not facts.explicit_addressee:
        ack = {"没事", "好的", "收到", "ok", "OK", "OKOk", "知道了", "嗯"}
        if re.fullmatch(r"[?？.。,，!！~～…、:：;；'\"]+", text):
            anchor = speaker
        elif (len(text) <= 8 and not re.search(r"[我你咱他她它]", text)
              and not text.endswith(("啊", "吧", "呢"))
              and not text.startswith("像") and text not in ack):
            anchor = speaker
        if "我" in text and not re.search(r"你|你们", text) and "咱" not in text:
            anchor = speaker
            addressee = ["room"]

    updated = dict(pred)
    updated["anchor"] = anchor
    updated["addressee"] = addressee or None
    updated["confidence"] = "unresolved" if anchor is None else pred.get("confidence", "medium")
    _PREV_ANCHOR[chat] = anchor
    if is_file:
        _LAST_FILE_SENDER[chat] = speaker
    return updated


def predict(sample: Any, hist: Sequence[Any]) -> dict[str, Any]:
    model = _model(sample)
    lines = render_situation(model).splitlines()
    probe.set_situation_lines(lines)
    pred = probe.predict(sample, hist)
    updates = deterministic_updates(sample)
    apply_world_updates(model, updates, at=str(sample.get("at") or ""))
    text = " ".join(str(sample.get("text") or "").split())
    updated = _postprocess(sample, hist, pred)
    if text.endswith(("吗", "？", "?")) or "怎么" in text:
        open_intention(
            model,
            key=str(sample.get("id") or sample.get("event_id") or text[:12]),
            kind="respond",
            at=str(sample.get("at") or ""),
            evidence=text[:40],
        )
    return updated


def reset_eval_state() -> None:
    _MODELS.clear()
    _PREV_ANCHOR.clear()
    _LAST_FILE_SENDER.clear()
    probe.reset_eval_state()


__all__ = ["deterministic_updates", "predict", "reset_eval_state"]
