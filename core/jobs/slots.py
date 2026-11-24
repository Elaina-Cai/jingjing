"""槽抽取：这件事「缺哪个参数」——问句由槽决定，不由说法决定。"""

from __future__ import annotations

import re
from typing import Iterable

_TIME = re.compile(
    r"\d{1,2}\s*[:：]\s*\d{2}|\d{1,2}\s*点|今晚|今早|明早|明晚|明天|后天|今天|周[一二三四五六日天]|下周|周末"
)
_CHAT_ID = re.compile(r"wxid_[0-9a-z]+|\d+@chatroom|group:\d+|user:\d+")
_QUOTED = re.compile(r"「([^」]{1,30})」")
_PRONOUN = ("帮他", "帮她", "给他", "给她", "发给他", "发给她", "跟他说", "跟她说", "替他", "替她")
_FILE_WORD = ("资料", "文件", "那个东西", "那份", "那张图", "附件")
_SEND_WORD = ("发", "说一句", "说句话", "告诉", "转告", "通知", "提醒一下", "喊")
ACTIONABLE = ("发", "说一句", "告诉", "转告", "提醒", "记", "搜", "查", "订", "转")


def looks_actionable(text: str) -> bool:
    body = str(text or "")
    return any(word in body for word in ACTIONABLE)


def has_chat(text: str, *, known: Iterable[str] = ()) -> bool:
    body = str(text or "")
    if _CHAT_ID.search(body):
        return True
    for name in known:
        if name and str(name) in body:
            return True
    if re.search(r"在[^，。；\s]{1,20}?(?:群|组|里)(?:说|发|聊)", body):
        return True
    for match in _QUOTED.finditer(body):
        candidate = (match.group(1) or "").strip()
        if candidate and not any(word in candidate for word in ("资料", "文件", "那个")):
            return True
    return False


def has_time(text: str) -> bool:
    return bool(_TIME.search(str(text or "")))


def has_path(text: str) -> bool:
    body = str(text or "")
    return bool(re.search(r"[A-Za-z]:\\|/|\.[A-Za-z0-9]{2,5}\b", body))


def missing_slots(text: str, *, known: Iterable[str] = ()) -> list:
    """返回缺的槽（可能为空）。顺序固定：chat, time, path。"""
    body = str(text or "")
    if not looks_actionable(body):
        return []
    missing = []
    wants_send = any(word in body for word in _SEND_WORD)
    if wants_send and not has_chat(body, known=known):
        if any(word in body for word in _PRONOUN) or "群里" in body:
            missing.append("chat")
    if "提醒" in body and not has_time(body):
        missing.append("time")
    if any(word in body for word in _FILE_WORD) and not has_path(body):
        missing.append("path")
    return missing


def ask_for(slots: list) -> str:
    """把缺的槽拼成一句问话（多个槽一次问全）。"""
    from core.jobs.capability_gate import slot_question

    if not slots:
        return ""
    if len(slots) == 1:
        return slot_question(slots[0])
    sep = chr(65307)
    return sep.join(slot_question(slot) for slot in slots)
