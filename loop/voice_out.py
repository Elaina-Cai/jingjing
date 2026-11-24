"""她说出来的话要不要变成声音（2026-10-27）。

主人 2026-10-27 的话：「语音包接进说话路径」——这里是那道闸门，不是语音实现。

三条现实（先写清楚，免得以后误以为"没生效是 bug"）：

* 微信 PC **没有发语音的接口**（`core/speech/voice_send.py` 的 CAPABILITIES 写着：
  silk 拖进窗口只会变成文件消息，不是语音条）。
* QQ 侧 napcat 支持 record 段，但本项目还没接线（`qq_not_wired`）。
* 所以本模块**默认 off**；打开后它能做到的只有：决策 + 合成 + 落一条 jsonl 记录，
  真发送那一步会如实返回 `platform_unsupported`，不会假装发出去。

开关（环境变量，改了下次启动生效）：

    YINYUE_VOICE_MODE = off | on_name | rate     # 默认 off
    YINYUE_VOICE_RATE = 0.0 ~ 1.0                # rate 模式下每句话变语音的概率，默认 0.12

设计原则沿用 loop 的规矩：**这里不判断该不该说话**（那是脑子的活），只回答
「这一句如果已经说出口了，要不要同时递一段声音」。
"""

from __future__ import annotations

import json
import os
import re
import time
from typing import Any
from core.infra.envflags import text
from core.infra import det

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG = os.path.join(ROOT, "data", "loop", "voice.log")

MODES = ("off", "on_name", "rate")
DEFAULT_MODE = "off"
DEFAULT_RATE = 0.12
MAX_CHARS = 60          # 太长的句子不合成（语音条 60 秒上限，正常语速约 200 字，保守取 60）
MIN_CHARS = 2

def mode() -> str:
    raw = text("YINYUE_VOICE_MODE").lower()
    return raw if raw in MODES else DEFAULT_MODE

def rate() -> float:
    try:
        val = float(text("YINYUE_VOICE_RATE") or DEFAULT_RATE)
    except (TypeError, ValueError):
        return DEFAULT_RATE
    return max(0.0, min(1.0, val))

def capability(platform: str) -> dict:
    try:
        from core.speech.voice_send import voice_capability
        return voice_capability(platform)
    except Exception as exc:  # noqa: BLE001
        return {"platform": str(platform or ""), "supported": False,
                "reason": "import_failed", "detail": "%s: %s" % (type(exc).__name__, exc)}

def _names(card: dict) -> list[str]:
    out: list[str] = []
    name = str(card.get("name") or "").strip()
    if name:
        out.append(name)
    for alias in card.get("aliases") or []:
        alias = str(alias or "").strip()
        if alias and alias not in out:
            out.append(alias)
    return out

def decide(*, platform: str, inbound: str, reply: str, card: dict | None = None,
           rnd: float | None = None) -> tuple[bool, str]:
    """这一句要不要同时递一段声音。返回 (要不要, 原因)。"""
    text = re.sub(r"\s+", "", str(reply or ""))
    if len(text) < MIN_CHARS:
        return False, "too_short"
    if len(text) > MAX_CHARS:
        return False, "too_long"
    if "```" in str(reply or "") or "http" in str(reply or ""):
        return False, "not_speakable"
    m = mode()
    if m == "off":
        return False, "off"
    if m == "on_name":
        hits = [n for n in _names(card or {}) if n and n in str(inbound or "")]
        return (True, "named:" + hits[0]) if hits else (False, "not_named")
    prob = rate() if rnd is None else max(0.0, min(1.0, float(rnd)))
    if m == "rate":
        # PIT-044：抽签的键就是这一问一答 —— 同一条消息重放，结论不变。
        draw = det.rng("voice", inbound, reply).random()
        return (True, "rate:%.3f<%.3f" % (draw, prob)) if draw < prob else (False, "rate_miss")
    return False, "unknown_mode"

def _gate(*, platform: str, chat_id: str) -> tuple[bool, str, str]:
    '''BR-062：这一句允许变语音吗 —— 主人最近开过口、这条消息还没用过。

    只在这里问、只问一次。拿不准就不发：宁可少一段声音，也别在群里对陌生人开口。
    '''
    try:
        from core.rules import voice_gate
        from . import voice_facts
        from .ports import pick_db
        seen = voice_facts.facts(pick_db() or '', chat_id=chat_id, platform=platform)
        used = voice_gate.used_keys()
        if voice_gate.ledger_corrupt():
            # CA-057：账本读坏 = 不知道这条消息换过声音没有；当空就是允许再发一次。
            # 所以收紧：宁可不发，也不赌。
            return False, 'ledger_corrupt', str(seen.get('owner_msg_id') or '')
        ok, why = voice_gate.allow(chat_kind=str(seen.get('chat_kind') or ''),
                                   who=str(seen.get('who') or ''),
                                   owner_spoke_at=seen.get('owner_spoke_at'),
                                   owner_key=str(seen.get('owner_key') or ''),
                                   used=used)
        return ok, why, str(seen.get('owner_msg_id') or '')
    except Exception as exc:  # noqa: BLE001
        return False, 'gate_error:%s' % type(exc).__name__, ''


def _log(record: dict) -> None:
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False)[:2000] + "\n")
    except OSError:
        pass

def maybe_send(text: str, *, platform: str = "wechat", chat_id: str = "",
               inbound: str = "", card: dict | None = None, dry_run: bool = False) -> dict:
    """已经发完文字之后调这个。永远不抛；返回一份能记日志/给面板看的回执。"""
    want, why = decide(platform=platform, inbound=inbound, reply=text, card=card)
    if not want:
        return {"ok": True, "voice": False, "reason": why}
    allowed, gate_why, owner_msg = _gate(platform=platform, chat_id=chat_id)
    if not allowed:
        _log({'event': 'trigger_gate', 'why': gate_why, 'platform': platform,
              'chat_id': chat_id, 'text': str(text or '')[:60]})
        return {'ok': True, 'voice': False, 'reason': 'gate:' + gate_why}
    cap = capability(platform)
    base = {"voice": True, "reason": why, "platform": platform, "chat_id": chat_id,
            "supported": bool(cap.get("supported")), "cap_reason": cap.get("reason") or ""}
    if not cap.get("supported"):
        # 平台上根本发不出语音：只在本地留痕（也顺手合成一次，好让主人知道"她本可以怎么说"）
        rec = dict(base, at=time.strftime("%Y-%m-%d %H:%M:%S"), sent=False,
                   text=str(text or "")[:120])
        try:
            from core.speech.voice_message import build_voice_message
            got = build_voice_message(text, slug=str((card or {}).get("voice") or ""),
                                      out_dir=os.path.join(ROOT, "data", "tts_cache", "loop"))
            rec["built"] = {k: got.get(k) for k in ("ok", "reason", "silk", "engine", "used_engine")}
            if got.get("ok"):
                rec["silk"] = os.path.relpath(str(got.get("silk") or ""), ROOT)
        except Exception as exc:  # noqa: BLE001
            rec["built"] = {"ok": False, "reason": "%s: %s" % (type(exc).__name__, exc)}
        _log(rec)
        return {"ok": True, "voice": False, "reason": "platform_unsupported",
                "cap_reason": cap.get("reason") or "", "built": rec.get("built")}
    try:
        from core.speech.voice_send import send_voice
        from core.speech.voice_message import build_voice_message
        got = build_voice_message(text, slug=str((card or {}).get("voice") or ""),
                                  out_dir=os.path.join(ROOT, "data", "tts_cache", "loop"))
        if not got.get("ok"):
            _log(dict(base, at=time.strftime("%Y-%m-%d %H:%M:%S"), sent=False,
                      build_reason=got.get("reason") or ""))
            return {"ok": False, "voice": False, "reason": "build_failed",
                    "build_reason": got.get("reason") or ""}
        sent = send_voice(platform=platform, target=chat_id, silk_path=str(got.get("silk") or ""),
                          text=str(text or "")[:120])
        _log(dict(base, at=time.strftime("%Y-%m-%d %H:%M:%S"), sent=bool(sent.get("ok")),
                  silk=os.path.relpath(str(got.get("silk") or ""), ROOT), send=sent))
        if sent.get('ok') and owner_msg and not dry_run:
            from core.rules import voice_gate
            voice_gate.mark_used(chat_id, owner_msg)
        return {"ok": bool(sent.get("ok")), "voice": bool(sent.get("ok")),
                "reason": "sent" if sent.get("ok") else str(sent.get("reason") or "send_failed")}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "voice": False, "reason": "%s: %s" % (type(exc).__name__, exc)}
