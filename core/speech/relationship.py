from __future__ import annotations

from datetime import datetime

import structlog

log = structlog.get_logger()


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


class RelationshipManager:
    """Maintains the relationships table from real interactions.

    Historically the table existed but nothing in the main flow wrote it, so
    intimacy was effectively dormant.  This manager gives it a live source
    without putting behavioural rules into the sticker sender.
    """

    def __init__(self, persona_id: int = 1):
        self.persona_id = persona_id

    def observe(
        self,
        repo,
        *,
        target_wxid: str,
        is_owner: bool = False,
        is_group: bool = False,
        importance: float = 0.5,
        notes: str = "",
        speaker_wxid: str = "",
    ) -> None:
        if not target_wxid:
            return
        try:
            from db.models import Relationship
            existing = repo.relationships.get(
                persona_id=self.persona_id, target_wxid=target_wxid,
            )
            old_intimacy = existing.intimacy if existing and existing.intimacy is not None else 0.5
            old_type = (existing.relation_type if existing else "") or ""

            if is_group:
                relation_type = "群聊"
                intimacy = _clamp(max(old_intimacy, 0.3) + 0.005)
            elif is_owner:
                relation_type = "亲密"
                intimacy = 1.0
            else:
                # A reply/analysis is a small positive social signal; the
                # magnitude is intentionally conservative so a single
                # interaction cannot jump a contact to 亲密.
                delta = (float(importance or 0.5) - 0.3) * 0.03
                intimacy = _clamp(old_intimacy + delta)
                if old_type:
                    relation_type = old_type
                elif intimacy >= 0.8:
                    relation_type = "亲密"
                elif intimacy >= 0.5:
                    relation_type = "熟悉"
                else:
                    relation_type = "普通"

            repo.relationships.upsert(Relationship(
                persona_id=self.persona_id,
                target_wxid=target_wxid,
                relation_type=relation_type,
                intimacy=intimacy,
                notes=notes or (existing.notes if existing else ""),
                updated_at=datetime.now(),
            ))

            # A room row is keyed on the room, so a member who talks to her
            # every day still had no row of their own -- which meant no
            # per-person affect and a weaker sticker gate.  Give the speaker
            # theirs as well; the room's row above is left exactly as it was.
            if is_group and speaker_wxid and speaker_wxid != target_wxid:
                self._observe_speaker(
                    repo, Relationship, speaker_wxid,
                    is_owner=is_owner, importance=importance,
                )
        except Exception as exc:
            log.warning("relationship_observe_failed",
                        target=target_wxid, error=str(exc))

    def _observe_speaker(
        self,
        repo,
        relationship_cls,
        speaker_wxid: str,
        *,
        is_owner: bool = False,
        importance: float = 0.5,
    ) -> None:
        """Record the person who actually spoke, as a person rather than a room."""
        existing = repo.relationships.get(
            persona_id=self.persona_id, target_wxid=speaker_wxid,
        )
        old_intimacy = (
            existing.intimacy if existing and existing.intimacy is not None else 0.5
        )
        old_type = (existing.relation_type if existing else "") or ""

        if is_owner:
            relation_type, intimacy = "亲密", 1.0
        else:
            intimacy = _clamp(old_intimacy + (float(importance or 0.5) - 0.3) * 0.03)
            # "群聊" is a property of the room, never of a person, so it must
            # not be inherited as this member's tier.
            if old_type and old_type != "群聊":
                relation_type = old_type
            elif intimacy >= 0.8:
                relation_type = "亲密"
            elif intimacy >= 0.5:
                relation_type = "熟悉"
            else:
                relation_type = "普通"

        repo.relationships.upsert(relationship_cls(
            persona_id=self.persona_id,
            target_wxid=speaker_wxid,
            relation_type=relation_type,
            intimacy=intimacy,
            notes=existing.notes if existing else "",
            updated_at=datetime.now(),
        ))
