"""出口侧的**读数**（observe-only）：语气形态 / 情绪分层 / 人味质感 / 自我投影。

从 ``core/speech/outbound.py`` 抽出的同职责块（2026-10-22，R2 拆大文件第一刀）。
四个函数都是读数：**没有返回值、不改草稿、不拦发送**，异常一律吞掉——它们是量具，
不是判据（``GATE_RULES`` 里没有它们，见 ``tests/test_persona_tone_target.py``）。

为什么能整块搬：四个函数体里没有一处 ``self.``（不碰 store、不碰网关状态）；
``outbound.py`` 里的调用点本来就是裸语句（返回值被丢弃）。
类里保留同名私有方法做一行委托，历史 import 面与 monkeypatch 面保持不变。

注意：这里的 import 全在函数内，是为了让测试能 monkeypatch
``core.speech.tone_target.tone_readout`` / ``core.observability.decision_trace.record_decision``
——**不要提到模块顶部**，否则补丁打不进去（测试会静默失真）。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog
from safelog import warn

if TYPE_CHECKING:  # 只用于注解：运行时不需要 outbound，避免 import 环
    from core.speech.domain import OutboundDraft, SpeechSnapshot

log = structlog.get_logger()


def observe_human_tone(draft: OutboundDraft, snapshot: SpeechSnapshot) -> None:
    """说话质感读数（observe-only）：模型腔标记 + 人味读数；只审计，不改文本。"""
    try:
        from core.speech.human_tone import human_tone_mode, note_outbound

        if human_tone_mode() == "off":
            return
        target = getattr(draft, "target", None)
        note_outbound(
            str(getattr(draft, "content", "") or ""),
            chat_id=str(getattr(target, "chat_id", "") or ""),
        )
    except Exception:  # noqa: BLE001 - 读数失败绝不拦发送
        warn("core/speech/tone_readings.py:40 Exception", err=repr(__import__("sys").exc_info()[1]))
        return


def observe_tone_target(draft: OutboundDraft, snapshot: SpeechSnapshot) -> None:
    """把要发出去的那句话读一遍语气形态，**只写审计**。

    owner 是 ``core.speech.tone_target``（形态契约表 + 探针）；这里只做
    "读档位 → 算读数 → 留痕"三步。档位在 persona 数据
    （``scene_styles.<scene>.tone_target``，三态，缺省 ``off``）：

    * ``off``：一行都不做（连读数都不算，不写任何审计）；
    * ``shadow`` / ``on``：都写读数——两档的区别只在 **prompt 侧**
      （``core/terminal/persona_brain`` 只对 ``on`` 注入形态段）。

    **它不改文本、不拦发送、不是 ``GATE_RULES`` 的一条**：没有返回值，
    异常一律吞掉。结构守卫见 ``tests/test_persona_tone_target.py::
    test_readout_is_observe_only_not_a_gate``。
    """
    try:
        from core.speech.tone_target import MODE_OFF, tone_mode, tone_readout

        text = str(getattr(draft, "content", "") or "")
        if not text.strip():
            return
        target = getattr(draft, "target", None)
        scene = (
            "group"
            if str(getattr(target, "chat_type", "") or "") == "group"
            else "private"
        )
        mode = tone_mode(getattr(snapshot, "persona", None), scene)
        if mode == MODE_OFF:
            return
        readout = tone_readout(text)
    except Exception as exc:  # noqa: BLE001 - 读数故障不能让回复发不出去
        log.debug("tone_target_readout_failed", error=str(exc)[:120])
        return
    record = {
        "kind": "tone_target_readout",
        "mode": str(mode),
        # 恒 False：这条链上根本没有"改正"这个动作（改动文本的不是它）。
        "applied": False,
        "scene": scene,
        "chat_id": str(
            getattr(draft, "who", "")
            or getattr(target, "chat_id", "")
            or ""
        ),
        "provider": str(getattr(target, "platform", "") or ""),
        "chars": int(readout.chars),
        "sentences": int(readout.sentences),
        "hits": list(readout.hits),
        "forms": [name for name, ok in readout.forms.items() if ok],
    }
    try:
        log.info("tone_target_readout", **record)
    except Exception:  # noqa: BLE001 - 日志失败不影响发送
        warn("core/speech/tone_readings.py:97 Exception", err=repr(__import__("sys").exc_info()[1]))
        pass  # noqa: silent（有意忽略，见 CONTRACT 第八条）
    try:
        from core.observability.decision_trace import record_decision

        record_decision(record, force=True)
    except Exception as exc:  # noqa: BLE001 - 审计失败不影响发送
        log.debug("tone_target_audit_failed", error=str(exc)[:120])


def observe_emotion_layers(draft: OutboundDraft, snapshot: SpeechSnapshot) -> None:
    """出口读一遍情绪分层（observe-only；owner: ``core.context.emotion``）。

    shadow/on 都写读数；off 一行不做。只写审计：不改文本、不拦发送。
    """
    try:
        from core.context import emotion as emotion_mod
        from core.execution.data_channels import get_world

        mode = emotion_mod.layers_mode()
        if mode == "off":
            return
        chat_id = str(
            getattr(draft, "who", "")
            or getattr(getattr(draft, "target", None), "chat_id", "")
            or ""
        )
        if not chat_id:
            return
        model = get_world(chat_id)
        if model is None:
            return
        digest = emotion_mod.layers_digest(model)
    except Exception as exc:  # noqa: BLE001 - 读数故障不能让回复发不出去
        log.debug("emotion_layers_readout_failed", error=str(exc)[:120])
        return
    record = {
        "kind": "emotion_layers_readout",
        "mode": str(mode),
        "applied": False,
        "chat_id": chat_id,
        **digest,
    }
    try:
        log.info("emotion_layers_outbound", **record)
    except Exception:  # noqa: BLE001 - 日志失败不影响发送
        warn("core/speech/tone_readings.py:142 Exception", err=repr(__import__("sys").exc_info()[1]))
        pass  # noqa: silent（有意忽略，见 CONTRACT 第八条）
    try:
        from core.observability.decision_trace import record_decision

        record_decision(record, force=True)
    except Exception:  # noqa: BLE001 - 审计失败不影响发送
        warn("core/speech/tone_readings.py:148 Exception", err=repr(__import__("sys").exc_info()[1]))
        pass  # noqa: silent（有意忽略，见 CONTRACT 第八条）


def note_self_message(draft: OutboundDraft, snapshot: SpeechSnapshot) -> None:
    """把「她说过的话」写进自我模型（SELF_MODEL 三态，默认 off）。

    off 一行不做；shadow 只审计（不写自我模型）；on 才写 self_message 并落盘。
    只记内容/时间/形态，不做语义加工。放在发送成功后调用。
    """
    try:
        from core import features
        from core.context import self_contract as self_contract_mod
        from core.context import situation as situation_mod
        from core.execution import data_channels as channels

        mode = features.mode(features.SELF_MODEL, features.MODE_OFF)
        if mode == "off":
            return
        chat_id = str(
            getattr(draft, "who", "")
            or getattr(getattr(draft, "target", None), "chat_id", "")
            or ""
        )
        content = str(getattr(draft, "content", "") or "")
        if not chat_id or not content:
            return
        if not self_contract_mod.in_continuity_scope(chat_id):
            return
        from datetime import datetime as _datetime

        at = _datetime.now().strftime("%Y-%m-%d %H:%M")
        kind = (
            "media"
            if str(getattr(draft, "kind", "") or "") in ("file", "image")
            else "reply"
        )
        record = {
            "kind": "self_model_readout",
            "mode": str(mode),
            "applied": mode == "on",
            "chat_id": chat_id,
            "chars": len(content),
            "msg_kind": kind,
        }
        if mode == "on":
            model = channels.get_world(chat_id)
            if model is not None:
                situation_mod.note_self_message(
                    model, content=content, at=at, kind=kind,
                )
                channels.save_world(chat_id)
        try:
            log.info(
                "self_model_write" if mode == "on" else "self_model_shadow",
                **record,
            )
        except Exception:  # noqa: BLE001 - 日志失败不影响发送
            warn("core/speech/tone_readings.py:205 Exception", err=repr(__import__("sys").exc_info()[1]))
            pass  # noqa: silent（有意忽略，见 CONTRACT 第八条）
        try:
            from core.observability.decision_trace import record_decision

            record_decision(record, force=True)
        except Exception:  # noqa: BLE001 - 审计失败不影响发送
            warn("core/speech/tone_readings.py:211 Exception", err=repr(__import__("sys").exc_info()[1]))
            pass  # noqa: silent（有意忽略，见 CONTRACT 第八条）
    except Exception as exc:  # noqa: BLE001 - 自我投影失败不能让发送失败
        log.debug("self_model_failed", error=str(exc)[:120])
