"""出站节奏：把一条完整回复切成最多 N 个气泡（连发），每个气泡仍走完整网关。

- 纯策略：无损（拼回去一字不差）、只按段落/句末切、短回复不切、规划失败退单条。
- 投递：deliver() 按计划逐条调用 gateway.send_text；每条各自过闸门/审计/去重/发送日志，
  因此网关的单条语义、审计和回滚位都不动。
- 三态：YINYUE_FEATURE_OUTBOUND_RHYTHM（off/shadow/on）；作用域
  YINYUE_RHYTHM_CHATS（显式名单，空 = 不开）。
- 失败即停：已发的不回滚、不伪造完成；任何异常退单条（deliver 返回 None，调用方走原路径）。
"""
from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import os
import re

import structlog

from core import features
from safelog import warn
from core.infra.envflags import text

log = structlog.get_logger()

ENDERS = set("。！？!?\u2026")
CLOSERS = set("\u201d\u2019\u300d\u300f\uff09)]")
MIN_CHARS = 45
MAX_BUBBLES = 3
MAX_BUBBLE_CHARS = 65


def _env_int(name: str, default: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    try:
        value = int(raw)
    except ValueError:
        warn("core/speech/rhythm.py:35 ValueError", err=repr(__import__("sys").exc_info()[1]))
        return default
    return value if value > 0 else default


def scope_allows(chat_id: str) -> bool:
    """显式名单才开；空名单=不开。"""
    raw = text("YINYUE_RHYTHM_CHATS")
    if not raw:
        return False
    allowed = {part.strip() for part in raw.split(",") if part.strip()}
    return str(chat_id or "") in allowed


def mode() -> str:
    return features.mode(features.OUTBOUND_RHYTHM, features.MODE_OFF)


def sentence_units(text: str) -> list[str]:
    parts: list[str] = []
    buf: list[str] = []
    i = 0
    while i < len(text):
        ch = text[i]
        buf.append(ch)
        i += 1
        if ch in ENDERS:
            while i < len(text) and text[i] in CLOSERS:
                buf.append(text[i])
                i += 1
            parts.append("".join(buf))
            buf = []
    if buf:
        parts.append("".join(buf))
    return [p for p in parts if p.strip()]


def paragraph_units(text: str) -> list[str]:
    chunks = re.split(r"(\n{2,})", text)
    out: list[str] = []
    for i, chunk in enumerate(chunks):
        if i % 2 == 1:
            if out:
                out[-1] += chunk
            elif chunk:
                out.append(chunk)
        elif chunk:
            out.append(chunk)
    return [p for p in out if p.strip()]


def pack_sentences(sentences: list[str], *, max_chars: int) -> list[str]:
    out: list[str] = []
    cur = ""
    for sentence in sentences:
        if not cur:
            cur = sentence
        elif len(cur) + len(sentence) <= max_chars:
            cur += sentence
        else:
            out.append(cur)
            cur = sentence
    if cur:
        out.append(cur)
    return [u for u in out if u.strip()]

def plan(text: str, *, min_chars: int | None = None,
         max_bubbles: int | None = None,
         max_bubble_chars: int | None = None) -> list[str]:
    """无损切分；不满足条件或任何异常都返回 [原文]。"""
    text = str(text or "")
    min_chars = min_chars or _env_int("YINYUE_RHYTHM_MIN_CHARS", MIN_CHARS)
    max_bubbles = max_bubbles or _env_int("YINYUE_RHYTHM_MAX_BUBBLES", MAX_BUBBLES)
    max_bubble_chars = max_bubble_chars or _env_int(
        "YINYUE_RHYTHM_MAX_BUBBLE_CHARS", MAX_BUBBLE_CHARS
    )
    if len(text) < min_chars:
        return [text]
    units: list[str] = []
    for para in paragraph_units(text):
        if len(para) <= max_bubble_chars:
            units.append(para)
        else:
            units.extend(
                pack_sentences(sentence_units(para), max_chars=max_bubble_chars)
            )
    if len(units) <= 1:
        return [text]
    if len(units) > max_bubbles:
        units = units[: max_bubbles - 1] + ["".join(units[max_bubbles - 1:])]
    merged: list[str] = []
    for unit in units:
        if merged and len(unit.strip()) <= 3:
            merged[-1] += unit
        else:
            merged.append(unit)
    if len(merged) <= 1 or "".join(merged) != text:
        return [text]
    return merged


def delays(bubbles) -> list[int]:
    out: list[int] = []
    for index, bubble in enumerate(bubbles or ()):
        if index == 0:
            out.append(0)
        else:
            out.append(min(2500, 700 + 250 * index + len(str(bubble)) * 6))
    return out


async def deliver(gateway, draft, snapshot, context, *, who: str = ""):
    """按计划逐条发送；返回最后一个 OutboundResult，或 None 让调用方走单条原路径。"""
    try:
        current = mode()
        target = getattr(getattr(snapshot, "turn", None), "target", None)
        chat = str(
            getattr(target, "chat_id", "")
            or who
            or getattr(draft, "who", "")
            or ""
        )
        if current == features.MODE_OFF or not chat or not scope_allows(chat):
            return None
        if str(getattr(draft, "kind", "text")) != "text":
            return None
        text = str(getattr(draft, "content", "") or "")
        if not text.strip():
            return None
        bubbles = plan(text)
        if len(bubbles) <= 1:
            return None
        delay_list = delays(bubbles)
        digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]
        if current == features.MODE_SHADOW:
            log.info(
                "rhythm_shadow", chat_id=chat, bubbles=len(bubbles),
                lengths=[len(b) for b in bubbles], delays=delay_list,
                block_hash=digest,
            )
            return None
        last = None
        for index, bubble in enumerate(bubbles):
            if index:
                await asyncio.sleep(delay_list[index] / 1000.0)
            part = dataclasses.replace(draft, content=bubble)
            last = await gateway.send_text(part, snapshot, context)
            if not bool(getattr(last, "ok", False)):
                log.warning(
                    "rhythm_part_failed", chat_id=chat, part=index + 1,
                    total=len(bubbles), error=str(getattr(last, "error", ""))[:120],
                )
                return last
            log.info(
                "rhythm_part_sent", chat_id=chat, part=index + 1,
                total=len(bubbles), chars=len(bubble), delay_ms=delay_list[index],
            )
        log.info("rhythm_delivered", chat_id=chat, bubbles=len(bubbles),
                 block_hash=digest)
        return last
    except Exception as exc:  # noqa: BLE001 - 节奏坏了退回单条
        log.warning("rhythm_failed", error=f"{type(exc).__name__}: {exc}"[:120])
        return None
