from __future__ import annotations

from typing import Sequence

from sqlalchemy import select

from db.models import Persona
from db.repo_base import BaseRepository


class PersonaRepository(BaseRepository):
    def get_by_id(self, persona_id: int) -> Persona | None:
        with self._session() as s:
            return s.get(Persona, persona_id)

    def get_by_name(self, name: str) -> Persona | None:
        with self._session() as s:
            return s.execute(
                select(Persona).where(Persona.name == name)
            ).scalar_one_or_none()

    def list_active(self) -> Sequence[Persona]:
        with self._session() as s:
            return s.execute(
                select(Persona).where(Persona.is_active == 1)
            ).scalars().all()

    def list_all(self) -> Sequence[Persona]:
        with self._session() as s:
            return s.execute(
                select(Persona).order_by(Persona.id)
            ).scalars().all()

    def add(self, persona: Persona) -> Persona:
        with self._session() as s:
            s.add(persona)
            s.commit()
            s.refresh(persona)
            return persona

    def update(self, persona_id: int, **fields):
        with self._session() as s:
            p = s.get(Persona, persona_id)
            if not p:
                return None
            for k, v in fields.items():
                if hasattr(p, k):
                    setattr(p, k, v)
            s.commit()
            s.refresh(p)
            return p

    def activate(self, persona_id: int):
        with self._session() as s:
            s.execute(
                select(Persona)
            ).scalars().all()
            for p in s.execute(select(Persona)).scalars():
                p.is_active = 0
            target = s.get(Persona, persona_id)
            if target:
                target.is_active = 1
            s.commit()

    def delete(self, persona_id: int):
        with self._session() as s:
            p = s.get(Persona, persona_id)
            if p:
                s.delete(p)
                s.commit()

    def upsert_from_dict(self, d: dict) -> Persona:
        with self._session() as s:
            pid = d.get("id")
            existing = s.get(Persona, pid) if pid else None
            if existing:
                for k, v in d.items():
                    if k == "id":
                        continue
                    if hasattr(existing, k):
                        setattr(existing, k, v)
                s.commit()
                s.refresh(existing)
                return existing
            p = Persona()
            for k, v in d.items():
                if hasattr(p, k):
                    setattr(p, k, v)
            s.add(p)
            s.commit()
            s.refresh(p)
            return p
