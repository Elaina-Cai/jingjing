"""Observation：任意形式归一成的那一个东西。"""

from __future__ import annotations

from dataclasses import dataclass, field

FORM_TEXT = "text"
FORM_MEDIA = "media"
FORM_QUOTE = "quote"
FORM_SYSTEM = "system"
FORMS = (FORM_TEXT, FORM_MEDIA, FORM_QUOTE, FORM_SYSTEM)

WHERE_PRIVATE = "private"
WHERE_GROUP = "group"

TEXT_MAX_CHARS = 4000


@dataclass(frozen=True)
class Observation:
    """一条抵达的事实（不再是「消息」，而是观察）。"""

    where: str = WHERE_PRIVATE
    chat_id: str = ""
    by: str = ""
    by_id: str = ""
    form: str = FORM_TEXT
    text: str = ""
    media: tuple[dict, ...] = ()
    quoted: dict = field(default_factory=dict)
    mention_to_me: bool = False
    mention_by: str = ""
    system: dict = field(default_factory=dict)
    at: str = ""

    def is_group(self) -> bool:
        """是不是群/多人在场。"""
        return self.where == WHERE_GROUP

    def has_body(self) -> bool:
        """有没有可读的正文（媒体只有摘要时算没有）。"""
        return bool(self.text.strip())

    def headline(self, limit: int = 80) -> str:
        """一行摘要，给日志用。"""
        body = " ".join(self.text.split())
        if len(body) > limit:
            body = body[: max(0, limit - 1)] + "\u2026"
        kind = self.form
        return f"[{self.where}/{kind}] {body}" if body else f"[{self.where}/{kind}]"
